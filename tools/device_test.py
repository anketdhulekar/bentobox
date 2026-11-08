#!/usr/bin/env python3
"""End-to-end tests for Bento: the real desktop app, talking to a real board or the simulator.

Run it through tools/bento:

    tools/bento test sim --board knob-lcd-1.8           # the simulator, with fake Toggl and speech-to-text
    tools/bento test knob-lcd-1.8 --deploy              # build with the debug interface, back up if needed, flash, then test on the board
    tools/bento test knob-lcd-1.8 --apis real           # the board, with real Toggl and speech-to-text
    tools/bento test knob-lcd-1.8 --link ble            # over real Bluetooth (needs the board paired: --pair)
    tools/bento test sim --board amoled-2.16 --only T1,L1 --quick

docs/TESTS.md describes every case. Each one drives the device as a person would, through its debug interface
(docs/DEBUG.md), and checks what the board shows and what really reached Toggl.

What's real and what isn't:
    desktop app       always the real one, in a temporary home, so nothing touches your own config or hooks
    --apis fake       local fake servers for Toggl and speech-to-text, and made-up keys
    --apis real       the real services, with your keys from the Keychain. It creates entries named "bentobox-test ...",
                      and deletes them at the end. Toggl's free plan allows few requests, so it runs fewer cases, and
                      a rate limit skips the rest instead of failing them.
    --link usb        the link's lines over the debug interface, bridged to the app. No pairing needed
    --link ble        real Bluetooth, on a paired board
    --link tcp        the simulator's own link (its default)

Keys and lock codes never appear in the output, and transcripts aren't logged.
Output goes to test-output/<target>-<apis>-<link>/ (git-ignored): report.json, device.log, desktop.log and PNG captures.
"""
import argparse
import base64
import fnmatch
import glob
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESKTOP = os.path.join(ROOT, "desktop")
SIM_DIR = os.path.join(ROOT, "firmware", "sim")
SIM_BIN = os.path.join(SIM_DIR, "build", "bento-sim")
SIM_SHARP_BIN = os.path.join(SIM_DIR, "build", "bento-sim-sharp")  # with --show: the same UX, at twice the resolution
PANEL_FILE = os.path.join(ROOT, "test-output", ".sim-panel")  # with --show: what the one window shows
CRASH = re.compile(r"Guru Meditation|abort\(\) was called|panic'ed|Backtrace:|assert failed|Stack canary|task_wdt:|Rebooting\.\.\.")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PREFIX = "bentobox-test"
OLD_PREFIXES = ("bento-test",)  # entries left by runs from before the app's rename: cleaned up too
LOCK_CODE = "4826"
NEW_LOCK_CODE = "7193"
RUN_TAG = time.strftime("%H%M%S")
# The keypad, in layout units: x0, width, top, height. The same numbers as the UX's layouts.
KEYPAD = {"round": (72, 72, 96, 54), "square": (75, 110, 112, 80)}
KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "del"]
FRAME_BUDGET_MS = 45
MIN_INTERNAL_KB = 20


class Failed(Exception):
    pass


class Skipped(Exception):
    pass


def check(ok, what):
    if not ok:
        raise Failed(what)


# ---- Connections to the device -------------------------------------------------------------------------------------


class TcpPort:
    def __init__(self, port):
        self.port = port
        self.sock = None

    def open(self, timeout=10):
        end = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                self.sock.settimeout(0.05)
                return
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.05)

    def read(self):
        try:
            data = self.sock.recv(1 << 16)
        except socket.timeout:
            return b""
        if not data:
            raise OSError("closed")
        return data

    def write(self, data):
        self.sock.sendall(data)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


class SerialPort:
    def __init__(self, path):
        import serial  # from the ESP-IDF Python environment; tools/bento runs this file with it
        self.serial, self.path = serial, path
        self.s = None

    def open(self, timeout=20):
        end = time.time() + timeout
        while True:
            try:
                s = self.serial.Serial()
                s.port, s.baudrate, s.timeout = self.path, 115200, 0.05
                s.dtr = s.rts = False
                s.open()
                self.s = s
                return
            except (self.serial.SerialException, OSError):
                if time.time() > end:
                    raise
                time.sleep(0.2)

    def read(self):
        return self.s.read(1 << 16)

    # The board's USB driver drops bytes that arrive while its receive buffer is full, and the board empties that buffer
    # once a frame, about every 20 ms. A long line, such as a page of agents, would lose its middle and its end, and
    # the board would then wait for that line to end. So a long write goes in pieces, at a pace a slow frame still keeps up with.
    PIECE = 256
    PIECE_GAP_S = 0.01

    def write(self, data):
        for i in range(0, len(data), self.PIECE):
            if i:
                time.sleep(self.PIECE_GAP_S)
            self.s.write(data[i:i + self.PIECE])
            self.s.flush()

    def close(self):
        if self.s:
            try:
                self.s.close()
            except (self.serial.SerialException, OSError):
                pass
            self.s = None


class Device:
    """The debug interface: a reader thread collects lines, and helpers wait for them."""

    def __init__(self, port, log_path):
        self.port = port
        self.lines = []
        self.cond = threading.Condition()
        self.alive = True
        self.connected = False
        self.capture_buf = None
        self.captures = []
        self.on_link = None
        self.crashes = []
        self.log = open(log_path, "w", encoding="utf-8")
        self.write_lock = threading.Lock()

    def start(self):
        self.port.open()
        self.connected = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while self.alive:
            try:
                data = self.port.read()
            except Exception:  # a restart: USB goes away and comes back, and the simulator's socket closes
                self.connected = False
                self.port.close()
                buf = b""
                try:
                    self.port.open()
                except Exception:
                    time.sleep(0.2)
                    continue
                self.connected = True
                continue
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._line(ANSI.sub("", raw.decode("utf-8", "replace").rstrip("\r")))

    def _line(self, text):
        if self.capture_buf is not None:
            if text == "CAPTURE END":
                with self.cond:
                    self.captures.append(self.capture_buf)
                    self.capture_buf = None
                    self.cond.notify_all()
            elif re.fullmatch(r"[A-Za-z0-9+/=]+", text):
                self.capture_buf[2].extend(base64.b64decode(text))
            return
        m = re.fullmatch(r"CAPTURE (\d+) (\d+)", text)
        if m:
            self.capture_buf = [int(m[1]), int(m[2]), bytearray()]
            return
        if text.startswith("LINK ") and self.on_link:
            self.on_link(text[5:])
        if not text.startswith("LINK "):  # the bridge's traffic could hold a task or a transcript
            self.log.write(text + "\n")
            self.log.flush()
        if CRASH.search(text):
            self.crashes.append(text)
        with self.cond:
            self.lines.append(text)
            self.cond.notify_all()

    def close(self):
        self.alive = False
        time.sleep(0.1)
        self.port.close()
        self.log.close()

    def mark(self):
        with self.cond:
            return len(self.lines)

    def since(self, i=0):
        with self.cond:
            return self.lines[i:]

    def send(self, text):
        with self.write_lock:
            try:
                self.port.write(text.encode("utf-8"))
            except Exception:
                pass  # a restart: the reader thread is connecting again

    def wait(self, pattern, timeout, since=None):
        rx = re.compile(pattern)
        i = self.mark() if since is None else since
        end = time.time() + timeout
        with self.cond:
            while True:
                for line in self.lines[i:]:
                    m = rx.search(line)
                    if m:
                        return m
                i = len(self.lines)
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def keys(self, keys, gap=0.18):
        for c in keys:
            self.send(c)
            time.sleep(gap)

    def cmd(self, line, timeout=5, expect=True):
        i = self.mark()
        self.send("#" + line + "\n")
        name = line.split(" ", 1)[0]
        m = self.wait(rf"^CMD (ok|error) {re.escape(name)}\b", timeout, since=i)
        ok = bool(m and m[1] == "ok")
        if expect is not None and ok != expect:
            raise Failed(f"#{name}: {m[0] if m else 'no answer'}")
        return ok

    def state(self, timeout=4):
        for _ in range(4):
            i = self.mark()
            self.send("d")
            m = self.wait(r"^STATE (\{.*\})$", timeout, since=i)
            if m:
                try:
                    return json.loads(m[1])
                except json.JSONDecodeError:
                    continue
        raise Failed("no STATE reply: is the device alive?")

    def wait_state(self, test, timeout, what="the expected state", every=0.25):
        end = time.time() + timeout
        st = None
        while time.time() < end:
            st = self.state()
            if test(st):
                return st
            time.sleep(every)
        raise Failed(f"never reached {what}")

    def frame(self, timeout=30):
        with self.cond:
            self.captures.clear()
        self.send("c")
        end = time.time() + timeout
        with self.cond:
            while not self.captures:
                left = end - time.time()
                if left <= 0:
                    raise Failed("no screen capture")
                self.cond.wait(left)
            w, h, data = self.captures.pop()
        check(len(data) == w * h * 2, f"a capture of {len(data)} bytes for {w} x {h}")
        return Frame(w, h, bytes(data))


class Frame:
    def __init__(self, w, h, data):
        self.w, self.h, self.data = w, h, data

    def rgb(self, x, y):
        i = (y * self.w + x) * 2
        v = self.data[i] | self.data[i + 1] << 8
        return (v >> 11) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31

    def count(self, x0, y0, x1, y1, test):
        return sum(1 for y in range(y0, y1) for x in range(x0, x1) if test(self.rgb(x, y)))

    def png(self, path):
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)
            for x in range(self.w):
                raw.extend(self.rgb(x, y))

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)) +
                    chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def near(c, target, tol=40):
    return all(abs(p - q) <= tol for p, q in zip(c, target))


# ---- The link to the desktop app, when it goes over the debug interface --------------------------------------------


class Bridge:
    """Stands where a board would be for the app's TCP link, and passes lines to the board as LINK lines."""

    def __init__(self, device):
        self.device = device
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.client = None
        self.up = True
        device.on_link = self._to_app
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            if not self.up:
                client.close()
                continue
            self.client = client
            buf = b""
            while True:
                try:
                    data = client.recv(1 << 16)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self.device.send("LINK " + line.decode("utf-8") + "\n")
                    except Exception:
                        pass
            self.client = None

    def _to_app(self, line):
        client = self.client
        if client:
            try:
                client.sendall(line.encode("utf-8") + b"\n")
            except OSError:
                pass

    def drop(self):
        """The Mac goes away: the app sees its connection close, and says hello again when it's back."""
        client = self.client
        if client:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

    def set_up(self, up):
        self.up = up
        if not up:
            self.drop()

    def close(self):
        self.server.close()
        self.drop()


# ---- The desktop app ------------------------------------------------------------------------------------------------


class Desktop:
    """The real app in a temporary home. Its keys come from a small stand-in for the `security` tool."""

    def __init__(self, a, out, link, display_port, toggl_url, stt_url, keys, org):
        self.a, self.out = a, out
        self.home = tempfile.mkdtemp(prefix="bento-e2e-")
        self.python = os.path.join(DESKTOP, ".venv", "bin", "python")
        if not os.path.exists(self.python):
            self.python = sys.executable
        cfgdir = os.path.join(self.home, ".config", "bentobox")
        os.makedirs(cfgdir)
        self.http_port = free_port()
        tools = os.path.join(self.home, "tools")
        os.makedirs(tools)
        security = os.path.join(tools, "security")
        if keys is None:  # the real Keychain, whose search list depends on the real home
            script = f'#!/bin/sh\nHOME="{os.path.expanduser("~")}" exec /usr/bin/security "$@"\n'
        else:
            lines = ["#!/bin/sh", 'name=""', 'read=0', 'while [ $# -gt 0 ]; do case "$1" in -a) name="$2"; shift;; -w) read=1;; esac; shift; done']
            for name, value in keys.items():
                lines.append(f'if [ "$name" = "{name}" ]; then [ $read = 1 ] && echo "{value}"; exit 0; fi')
            lines.append("exit 44")
            script = "\n".join(lines) + "\n"
        with open(security, "w") as f:
            f.write(script)
        os.chmod(security, 0o700)
        none = os.path.join(tools, "none")  # stands in for `claude` and `rex`: this run sees only the test's agents
        with open(none, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(none, 0o755)
        cfg = {"link": link, "display_host": "127.0.0.1", "display_port": display_port, "http_port": self.http_port,
               "toggl_org": org, "toggl_api": toggl_url, "stt_url": stt_url, "security_bin": security,
               "claude_bin": none, "rex_bin": none, "poll_seconds": 3600}
        with open(os.path.join(cfgdir, "config.json"), "w") as f:
            json.dump(cfg, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("REX_", "CLAUDE"))}
        self.env.update(HOME=self.home, BENTO_PORT=str(self.http_port))
        self.proc = None
        self.log_path = os.path.join(out, "desktop.log")

    def start(self):
        self.logf = open(self.log_path, "a")
        self.proc = subprocess.Popen([self.python, os.path.join(DESKTOP, "bentobox.py"), "run"], env=self.env, cwd=DESKTOP,
                                     stdout=self.logf, stderr=subprocess.STDOUT)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def close(self):
        self.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def log_text(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def hook(self, event):
        """Run the real hook script, as Claude Code would."""
        subprocess.run([self.python, os.path.join(DESKTOP, "agents", "hook.py")], input=json.dumps(event).encode(),
                       env=self.env, timeout=10, check=False)

    def lock_code(self, action, code=None):
        return subprocess.run([self.python, os.path.join(DESKTOP, "bentobox.py"), "lock-code", action],
                              input=(code + "\n").encode() if code else b"", env=self.env, cwd=DESKTOP, timeout=20,
                              capture_output=True, check=False).returncode


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- Toggl, as the tests see it --------------------------------------------------------------------------------------


class Toggl:
    """Checks what really reached Toggl, the same way for the fake server and the real one."""

    def __init__(self, url, get_key, org):
        sys.path.insert(0, DESKTOP)
        from toggl.client import TogglClient
        self.client = TogglClient(url, get_key, lambda: org)
        self.requests = 0
        self.limited = False

    def _ready(self):
        self.requests += 0 if self.client.wid else 1
        if not self.client.ensure():
            if self.client.rate_limited():
                self.limited = True
                raise Skipped("Toggl's rate limit")
            raise Failed("the test can't reach Toggl")

    def current(self):
        self._ready()
        self.requests += 1
        cur = self.client.current()
        if cur is None:
            if self.client.rate_limited():
                self.limited = True
                raise Skipped("Toggl's rate limit")
            raise Failed("the test couldn't ask Toggl what's running")
        return cur

    def wait_current(self, test, timeout, what, every=2.0):
        end = time.time() + timeout
        while True:
            cur = self.current()
            if test(cur):
                return cur
            if time.time() > end:
                raise Failed(f"Toggl never showed {what}")
            time.sleep(every)

    def entries(self):
        self._ready()
        self.requests += 1
        data = self.client.get_entries(50)
        if data is None:
            if self.client.rate_limited():
                self.limited = True
                raise Skipped("Toggl's rate limit")
            raise Failed("the test couldn't list Toggl's entries")
        rows = data.get("data") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []

    def find(self, desc):
        return [e for e in self.entries() if e.get("description") == desc]

    def wait_entry(self, desc, timeout, every=3.0):
        end = time.time() + timeout
        while True:
            rows = self.find(desc)
            if rows:
                return rows[0]
            if time.time() > end:
                raise Failed(f'Toggl never showed the entry "{desc}"')
            time.sleep(every)

    def start_foreign(self, desc):
        """A timer started on another device."""
        self._ready()
        self.requests += 1
        from toggl.client import iso
        reply = self.client.start_timer({"type": "activity", "description": desc, "start": iso(time.time())})
        check(reply.ok, f"couldn't start a foreign timer: HTTP {reply.status}")
        return reply

    def stop_foreign(self):
        self._ready()
        self.requests += 1
        self.client.stop_timer(time.time())

    def cleanup(self):
        """Removes this harness's entries from Toggl. Only ever entries with its own prefix."""
        if self.client.rate_limited():
            return "skipped: rate limited"
        try:
            cur = self.current()
            if cur.id and cur.desc.startswith((PREFIX,) + OLD_PREFIXES):
                self.stop_foreign()
            removed = 0
            for e in self.entries():
                if isinstance(e.get("description"), str) and e["description"].startswith((PREFIX,) + OLD_PREFIXES) and e.get("id"):
                    self.requests += 1
                    if self.client.delete_entry(e["id"]).ok:
                        removed += 1
            return f"removed {removed}"
        except (Failed, Skipped) as e:
            return f"stopped early: {e}"


def entry_seconds(e):
    d = e.get("duration")
    if isinstance(d, (int, float)) and d >= 0:
        return d
    sys.path.insert(0, DESKTOP)
    from toggl.client import parse_iso
    end = e.get("end") or e.get("stop")
    if isinstance(end, str) and isinstance(e.get("start"), str):
        return parse_iso(end) - parse_iso(e["start"])
    return -1


# ---- What the cases share ----------------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, a, out):
        self.a, self.out = a, out
        self.d = None           # Device
        self.app = None         # Desktop
        self.toggl = None       # Toggl
        self.bridge = None
        self.fake_toggl = None
        self.fake_speech = None
        self.sim_proc = None
        self.has = {}
        self.shape = "round"
        self.tag = RUN_TAG
        self.lock_code = None
        self.real_project = 0

    @property
    def real(self):
        return self.a.apis == "real"

    def name(self, what):
        return f"{PREFIX} {what} {self.tag}"

    def shot(self, name):
        f = self.d.frame()
        f.png(os.path.join(self.out, name + ".png"))
        return f

    def idle(self, mode="timer", theme="mono"):
        """A stopped, reset clock in this mode: no menu, no list, nothing listening, not locked."""
        d = self.d
        st = d.state()
        if st["lock"]["on"]:
            d.cmd("unlock")
            st = d.state()
        if st["voice"] not in ("idle",):
            d.keys("t")
            d.wait_state(lambda s: s["voice"] == "idle", 30, "voice to finish")
        if st.get("alarm"):
            d.keys("s")
        if st["menu"]["open"] or st["list"]["open"] or st.get("card"):
            d.keys("p")
        for key, clock in (("timer", st["timer"]), ("pomodoro", st["pomo"])):
            if clock["state"] != "idle":
                d.cmd(f"mode {key}")
                if clock["state"] == "running":
                    d.keys("s")
                d.keys("r")
        d.cmd(f"mode {mode}")
        d.cmd(f"theme {theme}")
        d.cmd("quiet")
        return d.wait_state(lambda s: s["mode"] == mode and s["screen"] == mode and s["running"] in (None, "", False), 8,
                            f"a stopped {mode}")

    def wait_ready(self, timeout=40):
        return self.d.wait_state(lambda s: s["link"]["connected"] and s["link"]["ready"] and s["link"]["clock_known"], timeout,
                                 "the link to the desktop app")

    def mac_off(self):
        """The Mac goes away: the real app stops, and the board sees the link close."""
        self.app.stop()
        if self.bridge:
            self.d.cmd("mac off")
            self.bridge.set_up(False)
        self.d.wait_state(lambda s: not s["link"]["connected"], 20, "the Mac going away")

    def mac_on(self):
        if self.bridge:
            self.d.cmd("mac on")
            self.bridge.set_up(True)
        self.app.start()
        self.wait_ready()

    def reboot(self):
        d = self.d
        before = d.state()["uptime_ms"]
        d.send("#reboot\n")
        if self.bridge:
            self.bridge.drop()
        end = time.time() + 40
        while True:
            time.sleep(0.5)
            try:
                if d.state(timeout=2)["uptime_ms"] < before:
                    break
            except Failed:
                pass
            check(time.time() < end, "no restart")
        time.sleep(0.5)
        if self.bridge:
            d.cmd("link usb")
            self.bridge.drop()

    def menu_choose(self, key):
        """Opens the menu if it's closed, and taps a compartment."""
        d = self.d
        if not d.state()["menu"]["open"]:
            d.keys("u")
            d.wait_state(lambda s: s["menu"]["open"], 4, "the menu")
        if self.shape == "round":  # six round the middle, clockwise from the upper left
            a = (["timer", "pomodoro", "zoo", "clock", "lock", "settings"].index(key) - 0.5) * 2 * math.pi / 6
            x, y = 180 + 132 * math.sin(a), 180 - 132 * math.cos(a)
        else:  # two by three
            x, y = {"timer": (124, 86), "pomodoro": (355, 86), "zoo": (124, 240), "clock": (355, 240), "settings": (124, 394),
                    "lock": (355, 394)}[key]
        d.cmd(f"tap {x:.0f} {y:.0f}")

    def pick_row(self, row, cursor=0, rows=None):
        """Chooses a row of the list on screen: turning to it and tapping, or on a board with no knob, a tap on it. A
        square board's lists show five rows of 76 units, and these cases only pick rows on the first screen. (rows: kept
        for the callers; the rows no longer shrink to fit.)"""
        d = self.d
        if self.has.get("knob"):
            if row != cursor:
                d.cmd(f"turn {row - cursor}")
            d.keys("t")
        else:
            d.cmd(f"tap 240 {66 + 76 * (row + .5):.0f}")

    def settings_open(self, *labels):
        """From the Settings page shown, opens each row in turn by its label: settings_open("Device")."""
        d = self.d
        for label in labels:
            st = d.wait_state(lambda s: s["settings"].get("rows"), 3, "the Settings rows")
            names = [r["label"] for r in st["settings"]["rows"]]
            check(label in names, f"no {label} in {names}")
            self.pick_row(names.index(label), cursor=st["settings"]["key"])
            d.wait_state(lambda s: s["settings"]["page"] != st["settings"]["page"], 3, f"{label} opened")

    def key_xy(self, key):
        x0, kw, top, kh = KEYPAD[self.shape]
        i = KEYS.index(key)
        return int(x0 + kw * (i % 3 + .5)), int(top + kh * (i // 3 + .5))

    def press_code(self, code):
        for c in code:
            x, y = self.key_xy(c)
            self.d.cmd(f"tap {x} {y}")


CASES = []


def case(cid, title, apis="both", needs=None, side_effect=False, slow=False, targets="both"):
    def wrap(fn):
        CASES.append(dict(id=cid, title=title, fn=fn, apis=apis, needs=needs, side_effect=side_effect, slow=slow, targets=targets))
        return fn
    return wrap


# ---- Boot and health -------------------------------------------------------------------------------------------------


@case("B1", "It starts, and knows what the board has")
def b1(c):
    st = c.d.state()
    check(st["screen"] != "", "no screen in STATE")
    want = "keypad" if st["lock"]["on"] else None
    if want:
        c.d.cmd("unlock")
    return f"{c.a.board}: has {sorted(k for k, v in st['has'].items() if v)}"


@case("B2", "The desktop app connects, and the board learns the clock and its status")
def b2(c):
    st = c.wait_ready()
    check(st["link"]["uid"] and re.fullmatch(r"[0-9a-f]{8}", st["link"]["uid"]), f"a bad uid: {st['link']['uid']}")
    check(st["link"]["toggl"] == "ok", f"the board says Toggl is {st['link']['toggl']}")
    check(st["link"]["voice_ok"] is True, "the board says voice isn't available")
    c.uid = st["link"]["uid"]
    return f"connected; Toggl ok; voice ok; uid {st['link']['uid'][:2]}…"


@case("B3", "The uid and the settings survive a restart", slow=True)
def b3(c):
    st0 = c.idle()
    c.d.cmd("task bento-b3-keep")
    c.d.cmd("theme inverted")
    i = c.d.mark()
    check(c.d.wait(r"^LOG saved settings", 6, since=i), "the settings were never saved")
    c.reboot()
    c.wait_ready()
    st = c.d.state()
    check(st["link"]["uid"] == st0["link"]["uid"], "the uid changed")
    check(st["task"] == "bento-b3-keep", "the task was lost")
    check(st["theme"] == "inverted", "the color profile was lost")
    c.d.cmd("task ")
    c.d.cmd("theme mono")
    return "the uid, task and color profile came back"


@case("B9", "Frame times and memory stay within budget", slow=True)
def b9(c):
    c.idle("pomodoro")
    d = c.d
    d.cmd("set 25")
    d.cmd("telemetry on")
    d.keys("s")
    d.cmd("remaining 40")  # the last-minute pulse is the heaviest clock screen
    i = d.mark()
    samples = []
    end = time.time() + 16
    while time.time() < end:
        m = d.wait(r"^TELEMETRY (\{.*\})$", 7, since=i)
        if not m:
            break
        i = d.mark()
        try:
            samples.append(json.loads(m[1]))
        except json.JSONDecodeError:
            pass
    d.cmd("telemetry off")
    d.keys("s")
    d.keys("r")
    check(samples, "no telemetry")
    worst = max(s["paint_ms_avg"] + s["present_ms_avg"] for s in samples)
    low = min(s["min_free_internal_kb"] for s in samples)
    detail = f"worst drawn frame {worst:.1f} ms; lowest free internal RAM {low} KB"
    if c.a.target != "sim":
        check(worst <= FRAME_BUDGET_MS, f"over the {FRAME_BUDGET_MS} ms budget: {detail}")
        check(low >= MIN_INTERNAL_KB, f"too little internal RAM: {detail}")
    return detail


@case("B10", "The heaviest effect stays within the frame budget", slow=True)
def b10(c):
    c.idle("zoo")
    d = c.d
    sid = "e2e00000-0000-4000-8000-000000000010"
    c.app.hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": "/tmp/bento-e2e-project", "source": "startup"})
    d.wait_state(lambda s: s["n_agents"] >= 1, 15, "an agent on the board")
    d.cmd("telemetry on")

    def measure(name, event, every):
        # The same effect again before the last one ends, so that the whole of each 5 s sample is under it.
        i = d.mark()
        end = time.time() + 16
        while time.time() < end:
            d.cmd(f"fx {event}", expect=None)
            time.sleep(every)
        rows = []
        for line in d.since(i):
            m = re.match(r"^TELEMETRY (\{.*\})$", line)
            if m:
                try:
                    rows.append(json.loads(m[1]))
                except json.JSONDecodeError:
                    pass
        rows = rows[1:]  # the first covers the screen changing
        check(rows, f"no telemetry for {name}")
        worst = max(rows, key=lambda r: r["paint_ms_avg"] + r["present_ms_avg"])
        return name, worst["paint_ms_avg"] + worst["present_ms_avg"], worst.get("drawn_ms_max", 0), worst["bands_avg"]

    results = [measure("confetti", "done", 1.2), measure("smoke", "gone", 1.7)]
    d.cmd("theme inverted")
    results.append(measure("confetti, Inverted", "done", 1.2))
    d.cmd("theme mono")
    # Under a Pomodoro's last minute: the heaviest clock there is, with the effect below it.
    d.cmd("mode pomodoro")
    d.cmd("set 25")
    d.keys("s")
    d.cmd("remaining 58")
    d.cmd("mode zoo")
    results.append(measure("confetti under a last minute", "done", 1.2))
    d.cmd("telemetry off")
    d.cmd("mode pomodoro")
    d.keys("s")
    d.keys("r")
    c.app.hook({"hook_event_name": "SessionEnd", "session_id": sid, "cwd": "/tmp/bento-e2e-project", "reason": "other"})
    d.wait_state(lambda s: not s["agents"], 10, "the agent gone")
    time.sleep(1.8)  # the effect's own 1.5 s
    detail = "; ".join(f"{n}: {avg:.1f} ms, worst single frame {mx:.1f} ms, {bands:.1f} bands" for n, avg, mx, bands in results)
    if c.a.target != "sim":
        over = [n for n, avg, _, _ in results if avg > FRAME_BUDGET_MS]
        check(not over, f"over the {FRAME_BUDGET_MS} ms budget in {over}: {detail}")
    return detail


# ---- The screens (no API traffic) --------------------------------------------------------------------------------------


@case("U1", "The menu's compartments each open what they say, by touch")
def u1(c):
    c.idle()
    d = c.d
    d.keys("u")
    d.wait_state(lambda s: s["menu"]["open"], 4, "the menu")
    time.sleep(.3)
    c.shot("U1-menu")
    d.cmd("turn 1")
    st = d.state()
    check(st["menu"]["open"] and st["mode"] == "timer", "a knob turn changed something")
    c.menu_choose("pomodoro")
    d.wait_state(lambda s: s["mode"] == "pomodoro" and not s["menu"]["open"], 4, "Pomodoro from the menu")
    c.menu_choose("settings")
    d.wait_state(lambda s: s["mode"] == "settings", 4, "Settings from the menu")
    d.keys("p")
    d.wait_state(lambda s: s["mode"] == "pomodoro", 4, "back out of Settings")
    c.menu_choose("zoo")
    d.wait_state(lambda s: s["mode"] == "zoo", 4, "Agent Zoo from the menu")
    c.shot("U1-zoo")
    c.menu_choose("timer")
    d.wait_state(lambda s: s["mode"] == "timer", 4, "the Timer from the menu")
    return "Pomodoro, Settings, Agents and Timer each opened from their compartment"


@case("U2", "A running clock can't switch to the other clock, but can open Agent Zoo")
def u2(c):
    c.idle()
    d = c.d
    d.keys("s")
    d.wait_state(lambda s: s["running"] == "timer", 4, "the Timer running")
    c.menu_choose("pomodoro")
    st = d.wait_state(lambda s: s["menu"]["open"] and "Pause" in s["toast"], 3, 'the "Pause to switch" message')
    check(st["mode"] == "timer", "the mode changed")
    c.menu_choose("zoo")
    d.wait_state(lambda s: s["mode"] == "zoo" and s["running"] == "timer", 4, "Agent Zoo over the Timer")
    c.shot("U2-zoo-with-timer")
    d.cmd("tap 180 40" if c.shape == "round" else "tap 240 40")
    d.wait_state(lambda s: s["mode"] == "timer", 3, "a tap on the clock's section")
    d.keys("s")
    d.keys("r")
    return "blocked the switch; Agent Zoo opened; a tap on the section went back"


@case("U3", "The color profile changes only in Settings, and applies to every screen")
def u3(c):
    c.idle()
    d = c.d
    d.cmd("swipe left")
    d.cmd("swipe right")
    check(d.state()["theme"] == "mono", "a swipe changed the color profile")
    d.cmd("mode settings")
    c.settings_open("Device")
    st = d.wait_state(lambda s: s["settings"].get("rows"), 3, "the Device rows")
    labels = [r if isinstance(r, str) else r.get("key", r.get("label", "")) for r in st["settings"]["rows"]]
    i = next((n for n, l in enumerate(labels) if str(l).lower() in ("theme", "color")), None)
    check(i is not None, f"no Color row in {labels}")
    c.pick_row(i, cursor=st["settings"]["key"], rows=len(labels))
    d.wait_state(lambda s: s["theme"] == "inverted", 3, "Inverted from the Settings row")
    w = d.state()["has"]
    px = (60, 110) if c.shape == "round" else (40, 60)
    k = c.d.frame().w / (360 if c.shape == "round" else 480)
    results = []
    for mode in ("settings", "timer", "pomodoro", "zoo"):
        d.cmd(f"mode {mode}")
        d.cmd("quiet")
        time.sleep(.3)
        f = c.shot(f"U3-inverted-{mode}")
        results.append((mode, f.rgb(int(px[0] * k), int(px[1] * k))))
    bad = [m for m, rgb in results if not near(rgb, (255, 255, 255), 8)]
    check(not bad, f"not white on: {bad}")
    d.cmd("theme mono")
    return f"white on every screen; swipes did nothing (has {sum(1 for v in w.values() if v)} features)"


@case("U4", "The Pomodoro warms up at 10:00, 5:00 and 1:00")
def u4(c):
    c.idle("pomodoro")
    d = c.d
    d.cmd("set 25")
    d.keys("s")
    d.wait_state(lambda s: s["running"] == "pomodoro", 4, "the Pomodoro running")
    results = {}
    for name, secs, target in (("white", 700, None), ("orange", 300, (255, 77, 6)), ("crimson", 59, (232, 20, 58))):
        d.cmd(f"remaining {secs}")
        d.cmd("pulse 0")
        time.sleep(.3)
        f = c.shot(f"U4-{name}-{secs}")
        h, w = f.h, f.w
        band = (w // 6, int(h * .46), w * 5 // 6, int(h * .62))
        if target:
            n = f.count(*band, lambda p: near(p, target, 45))
            results[name] = n
            check(n > 150, f"too few {name} pixels at {secs} s: {n}")
        else:
            n = f.count(*band, lambda p: near(p, (255, 255, 255), 30))
            results[name] = n
            check(n > 150, f"too few white pixels at {secs} s: {n}")
    d.cmd("pulse off")
    d.cmd("remaining 40")
    st = d.wait_state(lambda s: s["pulsing"], 3, "the last-minute pulse")
    check(st["pulsing"], "no pulse in the last minute")
    d.keys("s")
    d.keys("r")
    return f"pixels of each color: {results}"


@case("U5", "The arc above an agent is relative to this countdown")
def u5(c):
    c.idle("pomodoro")
    d = c.d
    fractions = {}
    for length in (25, 50):
        d.cmd(f"set {length}")
        d.keys("s")
        st = d.wait_state(lambda s: s["running"] == "pomodoro", 4, "the Pomodoro running")
        fractions[length] = st["pomo"]["fraction"]
        check(st["pomo"]["fraction"] > .98, f"a {length}-minute countdown started at {st['pomo']['fraction']}")
        d.cmd(f"remaining {length * 30}")
        st = d.state()
        check(abs(st["pomo"]["fraction"] - .5) < .02, f"half of {length} minutes shows {st['pomo']['fraction']}")
        d.cmd("mode zoo")
        time.sleep(.3)
        c.shot(f"U5-arc-{length}-half")
        d.cmd("mode pomodoro")
        d.keys("s")
        d.keys("r")
    return "both start full, and both show half at half time"


@case("U6", "Device shows the battery, firmware and MAC address; Connectivity the Mac connected")
def u6(c):
    st = c.idle()
    d = c.d

    def row(s, label):
        return next((r["value"] for r in s["settings"]["rows"] if r["label"] == label), None)

    d.cmd("mode settings")
    c.settings_open("Connectivity")
    st = d.wait_state(lambda s: row(s, "Connected to"), 3, "Connectivity")
    check(row(st, "Connected to") not in (None, "No Mac"), f"Connected to says {row(st, 'Connected to')}")
    d.keys("p")
    c.settings_open("Device")
    st = d.wait_state(lambda s: row(s, "Firmware"), 3, "Device")
    check(row(st, "Firmware") == st["link"].get("fw", row(st, "Firmware")), "the firmware's version")
    check(re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", row(st, "MAC address") or ""), f"MAC address: {row(st, 'MAC address')}")
    if not st["has"].get("battery"):
        d.cmd("mode timer")
        return "this board has no battery reading; firmware, MAC and the Mac connected report"
    time.sleep(.2)
    c.shot("U6-device")
    d.cmd("battery 12")
    d.wait_state(lambda s: row(s, "Battery") == "12%", 3, "the battery at 12%")
    d.cmd("battery 80 charging")
    d.wait_state(lambda s: row(s, "Battery") == "80%, charging", 3, "the battery charging")
    d.cmd("battery off")
    d.cmd("mode timer")
    return "the battery, firmware, MAC address and the Mac connected report"


@case("U7", "Rotating the board turns the screen, after a wait, with touch following", needs="motion")
def u7(c):
    c.idle()
    d = c.d
    d.cmd("autorotate on")
    d.cmd("tilt 90")
    time.sleep(.3)
    check(d.state()["rotation"]["orient"] == 0, "the screen turned right away")
    d.cmd("tilt 0")
    time.sleep(1.1)
    check(d.state()["rotation"]["orient"] == 0, "a brief wobble turned the screen")
    for deg in (90, 180, 270):
        d.cmd(f"tilt {deg}")
        st = d.wait_state(lambda s: s["rotation"]["orient"] == deg and not s["rotation"]["turning"], 4, f"the screen at {deg}°")
        c.shot(f"U7-rotated-{deg}")
        d.keys("u")
        d.wait_state(lambda s: s["menu"]["open"], 3, f"a swipe up at {deg}°")
        d.keys("p")
    d.cmd("flat on")
    d.cmd("tilt 180")
    time.sleep(1.2)
    check(d.state()["rotation"]["orient"] == 270, "lying flat didn't hold the orientation")
    d.cmd("flat off")
    d.cmd("tilt 0")
    d.wait_state(lambda s: s["rotation"]["orient"] == 0, 4, "the screen upright again")
    d.cmd("tilt_off")
    return "waits, ignores a wobble, turns at 90/180/270, holds when flat"


@case("U9", "With no knob, dragging sideways on a clock does what the knob does", needs="no_knob")
def u9(c):
    c.idle("pomodoro")
    d = c.d
    d.cmd("set 25")
    d.cmd("swipe 150 300 330 300")
    st = d.state()
    check(st["pomo"]["set_min"] == 40, f"180 units right gave {st['pomo']['set_min']} minutes, not 40")
    check(st["pomo"]["remaining_s"] == 40 * 60, "the time didn't follow the length")
    time.sleep(.2)
    c.shot("U9-dragged")
    d.cmd("swipe 330 300 150 300")
    d.cmd("swipe 330 300 270 300")
    st = d.state()
    check(st["pomo"]["set_min"] == 20, f"going back gave {st['pomo']['set_min']} minutes, not 20")
    check(st["theme"] == "mono", "a sideways drag changed the color profile")
    d.keys("u")
    d.wait_state(lambda s: s["menu"]["open"], 3, "a swipe up still opening the menu")
    d.keys("p")
    d.cmd("set 25")
    return "25 to 40 and back down to 20 minutes, and a swipe up still opens the menu"


@case("U8", "The screen never shows stale bands", slow=True)
def u8(c):
    c.idle()
    d = c.d
    d.cmd("verify on")
    i = d.mark()
    d.cmd("task bento-u8 verifying the renderer")
    d.keys("s")
    d.keys("u")
    d.keys("+++")
    d.keys("t")
    time.sleep(.5)
    d.cmd("mode zoo")
    time.sleep(.5)
    d.cmd("theme inverted")
    d.cmd("mode pomodoro")
    d.cmd("mode settings")
    time.sleep(.5)
    d.cmd("theme mono")
    d.cmd("mode timer")
    st = d.state()
    d.cmd("verify off")
    d.cmd("task ")
    if st["running"]:
        d.keys("s")
    d.keys("r")
    bad = [l for l in d.since(i) if l.startswith("LOG verify_fail")]
    check(not bad and st["render"]["verify_fails"] == 0, f"{len(bad)} stale frames")
    return f"{st['render']['frames']} frames, no stale bands"


# ---- Agents ------------------------------------------------------------------------------------------------------------


@case("A1", "An agent's hook events reach the board, one agent showing at a time")
def a1(c):
    sid = "e2e00000-0000-4000-8000-000000000001"
    c.idle("zoo")
    c.app.hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": "/tmp/bento-e2e-project", "source": "startup"})
    st = c.d.wait_state(lambda s: any(a["st"] == "working" or a["st"] == "idle" for a in s["agents"]), 15, "the agent on the board")
    check(len(st["agents"]) == 1, f"{len(st['agents'])} agents; this run should see only the test's")
    c.app.hook({"hook_event_name": "PreToolUse", "session_id": sid, "cwd": "/tmp/bento-e2e-project", "tool_name": "Read", "tool_input": {"file_path": "/tmp/x/main.c"}})
    c.d.wait_state(lambda s: s["agents"] and s["agents"][0]["st"] == "working", 8, "the agent working")
    time.sleep(.3)
    c.shot("A1-agent")
    c.sid = sid
    return "the agent appeared and went to work"


@case("A2", "An agent that needs you shows on the board")
def a2(c):
    i = c.d.mark()
    c.app.hook({"hook_event_name": "PermissionRequest", "session_id": c.sid, "cwd": "/tmp/bento-e2e-project", "tool_name": "Bash"})
    st = c.d.wait_state(lambda s: s["agents"] and s["agents"][0]["st"] == "needs", 8, "the agent needing you")
    check(c.d.wait(r"^LOG needs_you", 3, since=i), "no needs_you log")
    time.sleep(.3)
    c.shot("A2-needs-you")
    c.d.cmd("mode timer")
    time.sleep(.3)
    st = c.d.state()
    check(st["status_amber"], "a clock's status didn't turn amber")
    c.shot("A2-needs-you-on-clock")
    c.d.cmd("mode zoo")
    c.app.hook({"hook_event_name": "PostToolUse", "session_id": c.sid, "cwd": "/tmp/bento-e2e-project", "tool_name": "Bash"})
    c.d.wait_state(lambda s: s["agents"] and s["agents"][0]["st"] == "working", 8, "the agent back at work")
    return f"needs-you on Agent Zoo and on a clock's status line ({st['status'][:30]})"


@case("A3", "A tap on the character reaches the desktop app")
def a3(c):
    c.d.cmd("mode zoo")
    before = c.app.log_text()
    x, y = (180, 170) if c.shape == "round" else (240, 200)
    c.d.cmd(f"tap {x} {y}")
    st = c.d.wait_state(lambda s: s["toast"], 4, "the toast")
    time.sleep(.4)
    return f'the board said "{st["toast"]}" ({len(c.app.log_text()) - len(before)} new log bytes)'


@case("A4", "An agent that ends goes away")
def a4(c):
    c.app.hook({"hook_event_name": "SessionEnd", "session_id": c.sid, "cwd": "/tmp/bento-e2e-project", "reason": "other"})
    c.d.wait_state(lambda s: not s["agents"], 10, "the agent gone")
    time.sleep(1.8)  # its poof takes 1.5 s
    c.shot("A4-empty")
    return "the board shows no agents"


@case("A5", "Hundreds of agents reach the board, and it can move through them", slow=True)
def a5(c):
    d = c.d
    count = 300
    c.idle("zoo")
    sids = [f"e2e50000-0000-4000-8000-{i:012d}" for i in range(count)]
    t0 = time.time()

    def start(i):
        c.app.hook({"hook_event_name": "SessionStart", "session_id": sids[i], "cwd": f"/tmp/bento-e2e-{i % 7}", "source": "startup"})

    # As fast as the Mac can run the hook script.
    workers = []
    for lot in range(8):
        t = threading.Thread(target=lambda lot=lot: [start(i) for i in range(lot, count, 8)])
        t.start()
        workers.append(t)
    for t in workers:
        t.join()
    started = time.time() - t0
    st = d.wait_state(lambda s: s["n_agents"] >= count, 120, f"all {count} agents on the board")
    arrived = time.time() - t0
    c.shot("A5-crowd")
    # The strip wraps: before the first is the last.
    if c.has.get("knob"):
        time.sleep(1)
        before = d.state()["sel"]
        d.cmd("turn -1")
        after = d.wait_state(lambda s: s["sel"] != before, 3, "a knob turn")["sel"]
        check(after == (before - 1) % count, f"from {before}, a turn back went to {after}")
    # One that needs you gets through at once, ahead of everything else.
    c.app.hook({"hook_event_name": "PermissionRequest", "session_id": sids[count // 2], "cwd": "/tmp/bento-e2e-3", "tool_name": "Bash"})
    t1 = time.time()
    d.wait_state(lambda s: s["n_needing"] >= 1, 15, "the one that needs you")
    needs_s = time.time() - t1
    d.wait_state(lambda s: s["sel_name"], 3, "a chosen agent")
    time.sleep(.5)
    c.shot("A5-needs-you")
    # All of them end.
    t2 = time.time()

    def end(i):
        c.app.hook({"hook_event_name": "SessionEnd", "session_id": sids[i], "cwd": f"/tmp/bento-e2e-{i % 7}", "reason": "other"})

    workers = []
    for lot in range(8):
        t = threading.Thread(target=lambda lot=lot: [end(i) for i in range(lot, count, 8)])
        t.start()
        workers.append(t)
    for t in workers:
        t.join()
    d.wait_state(lambda s: s["n_agents"] == 0, 60, "all of them gone")
    return f"{count} agents on the board {arrived:.1f} s after the first started (the hooks took {started:.1f} s); one that needed you got through in {needs_s:.1f} s; all gone in {time.time() - t2:.1f} s"


# ---- Toggl, end to end ----------------------------------------------------------------------------------------------


def set_task(c, what, project=True):
    desc = c.name(what)
    c.d.cmd("task " + desc)
    return desc


@case("T1", "Starting and pausing makes one entry in Toggl", side_effect=True, slow=True)
def t1(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T1")
    t0 = time.time()
    d.keys("s")
    c.toggl.wait_current(lambda cur: cur.desc == desc, 25, "the running entry")
    check(time.time() - t0 < 25, "the entry took too long to start")
    time.sleep(max(0, 11.5 - (time.time() - t0)))
    d.keys("s")
    c.toggl.wait_current(lambda cur: cur.desc != desc, 15, "the entry stopped")
    e = c.toggl.wait_entry(desc, 15)
    secs = entry_seconds(e)
    check(secs >= 9, f"the entry lasted {secs} s")
    d.keys("r")
    d.wait_state(lambda s: s["link"]["outbox"] == 0, 10, "the outbox empty")
    return f"one entry of {secs} s; the outbox emptied"


@case("T2", "A start stopped within 10 s leaves nothing in Toggl", side_effect=True)
def t2(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T2")
    d.keys("s")
    c.toggl.wait_current(lambda cur: cur.desc == desc, 8, "the running entry", every=1)
    d.keys("s")
    c.toggl.wait_current(lambda cur: cur.desc != desc, 15, "the entry gone from running")
    time.sleep(1.5)
    rows = c.toggl.find(desc)
    check(not rows, f"{len(rows)} entries were left")
    d.keys("r")
    return "the accidental start was deleted"


@case("T3", "The knob moves the start, and sends one small update", side_effect=True, slow=True)
def t3(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T3")
    d.keys("s")
    cur = c.toggl.wait_current(lambda x: x.desc == desc, 25, "the running entry")
    start0 = cur.start
    if c.fake_toggl:
        c.fake_toggl.clear()
    d.keys("+++", gap=.12)
    st = d.wait_state(lambda s: s["timer"]["elapsed_s"] >= 180, 3, "the board's count moved 3 minutes")
    cur = c.toggl.wait_current(lambda x: x.desc == desc and x.start <= start0 - 170, 15, "the start moved 3 minutes")
    detail = f"start moved {start0 - cur.start} s"
    if c.fake_toggl:
        patches = c.fake_toggl.reqs("PATCH")
        check(len(patches) == 1 and set(patches[0].body) == {"start"}, f"the updates: {patches}")
        detail += "; one PATCH with only the start"
    d.keys("s")
    c.toggl.wait_current(lambda x: x.desc != desc, 15, "the entry stopped")
    d.keys("r")
    return f"{detail} (board at {st['timer']['elapsed_s']} s)"


@case("T4", "Sync from Toggl reloads a list with one request", side_effect=True)
def t4(c):
    c.idle()
    d = c.d
    if c.fake_toggl:
        c.fake_toggl.clear()
    i = d.mark()
    d.keys("o")
    d.wait_state(lambda s: s["list"]["open"] and s["list"]["kind"] == "projects", 4, "the projects list")
    c.pick_row(0, 1)
    check(d.wait(r"^LOG list_loaded projects", 25, since=i), "the projects never loaded")
    st = d.wait_state(lambda s: s["list"]["sync"] != "syncing", 5, "the sync finishing")
    check(st["list"]["sync"] == "idle", f"the sync says {st['list']['sync']}")
    c.shot("T4-projects")
    d.keys("p")
    if c.fake_toggl:
        n = len(c.fake_toggl.reqs("GET", "projects"))
        check(n == 1, f"{n} requests for the list")
    return f"{st['list']['projects']} projects loaded"


@case("T5", "Picking a past task while tracking switches entries", side_effect=True, slow=True)
def t5(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T5-first")
    d.keys("s")
    c.toggl.wait_current(lambda x: x.desc == desc, 25, "the first entry")
    time.sleep(11)
    d.keys("e")
    st = d.wait_state(lambda s: s["list"]["open"] and s["list"]["kind"] == "tasks", 4, "the tasks list")
    check(st["list"]["tasks"] > 1, f"{st['list']['tasks']} past tasks; this needs another one to pick")
    c.pick_row(3, 1)  # past the running task, which is the first of the tasks
    st = d.wait_state(lambda s: not s["list"]["open"], 4, "the list closed")
    cur = c.toggl.wait_current(lambda x: x.id and x.desc != desc, 20, "another entry running")
    check(st["running"] == "timer", "the counter stopped")
    d.keys("s")
    c.toggl.wait_current(lambda x: x.id != cur.id, 15, "the second entry stopped")
    d.keys("r")
    return "the first entry stopped and a second started, with the counter still going"


@case("T6", "A swipe down adopts a timer from elsewhere, and notices when it stops", side_effect=True, slow=True)
def t6(c):
    c.idle()
    d = c.d
    desc = c.name("T6-elsewhere")
    c.toggl.start_foreign(desc)
    d.keys("p")
    st = d.wait_state(lambda s: s["running"] == "timer", 25, "the timer adopted")
    check(st["task"] == desc, "adopted the wrong task")
    time.sleep(1.0)
    c.toggl.stop_foreign()
    d.keys("p")
    d.wait_state(lambda s: s["running"] != "timer", 25, "the board pausing")
    d.keys("p")
    time.sleep(3)
    d.keys("r")
    e = c.toggl.wait_entry(desc, 10)
    return f"adopted, then paused; the entry stayed in Toggl ({entry_seconds(e)} s)"


@case("T7", "Bentobox never stops a timer it didn't start", side_effect=True, slow=True)
def t7(c):
    c.idle()
    d = c.d
    foreign = c.name("T7-elsewhere")
    mine = set_task(c, "T7-mine")
    c.toggl.start_foreign(foreign)
    cur0 = c.toggl.current()
    check(cur0.desc == foreign, "the foreign timer didn't start")
    d.keys("s")
    time.sleep(12)
    cur = c.toggl.current()
    d.keys("s")
    time.sleep(4)
    after = c.toggl.current()
    d.keys("r")
    if after.desc == foreign:
        c.toggl.stop_foreign()
    detail = f"while running: {cur.desc == foreign and 'foreign kept' or 'foreign replaced'}; after: {after.desc == foreign and 'foreign kept' or 'foreign gone'}"
    check(after.id == cur0.id, f"the foreign timer was stopped ({detail})")
    e = c.toggl.wait_entry(mine, 20)
    return f"{detail}; my run was logged afterwards ({entry_seconds(e)} s)"


@case("T8", "A run with no Mac is logged when the Mac comes back, with its real times", side_effect=True, slow=True)
def t8(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T8-offline")
    c.mac_off()
    d.keys("s")
    time.sleep(12)
    d.keys("s")
    st = d.state()
    check(st["link"]["outbox"] >= 2, f"the outbox holds {st['link']['outbox']}")
    d.keys("r")
    c.mac_on()
    e = c.toggl.wait_entry(desc, 45)
    secs = entry_seconds(e)
    check(10 <= secs <= 16, f"the entry lasted {secs} s, not about 12")
    d.wait_state(lambda s: s["link"]["outbox"] == 0, 15, "the outbox empty")
    return f"logged at {secs} s after reconnecting"


@case("T9", "Unsent runs survive a restart", side_effect=True, slow=True)
def t9(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T9-restart")
    c.mac_off()
    d.keys("s")
    time.sleep(11.5)
    d.keys("s")
    d.keys("r")
    check(d.state()["link"]["outbox"] >= 2, "nothing waiting to send")
    time.sleep(.5)
    c.reboot()
    check(d.state()["link"]["outbox"] >= 2, "the restart lost the waiting runs")
    c.mac_on()
    e = c.toggl.wait_entry(desc, 45)
    secs = entry_seconds(e)
    check(10 <= secs <= 15, f"the entry lasted {secs} s, not about 11.5")
    return f"logged after the restart, with its real length ({secs} s)"


@case("T13", "A run from before the board ever knew the time keeps its length", side_effect=True, slow=True)
def t13(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T13-no-clock")
    c.mac_off()
    c.reboot()  # no Mac since this restart, so the board has no idea what time it is
    st = d.state()
    check(not st["link"]["clock_known"], "the board shouldn't know the time")
    d.keys("s")
    time.sleep(11.5)
    d.keys("s")
    d.keys("r")
    c.reboot()  # and now even the time base is gone
    check(d.state()["link"]["outbox"] >= 2, "the restart lost the waiting runs")
    c.mac_on()
    e = c.toggl.wait_entry(desc, 45)
    secs = entry_seconds(e)
    check(10 <= secs <= 15, f"the entry lasted {secs} s, not about 11.5")
    return f"logged with its real length ({secs} s), though its time of day is a guess"


@case("T10", 'A run Toggl refuses shows "not saved"', apis="fake", side_effect=True)
def t10(c):
    c.idle()
    d = c.d
    set_task(c, "T10")
    c.fake_toggl.fail_next(400, body={"error": "archived project"})
    i = d.mark()
    d.keys("s")
    d.wait_state(lambda s: "saved" in (s["status"] + s["hint"]), 15, '"not saved in Toggl"')
    c.fake_toggl.script.clear()
    d.keys("s")
    d.keys("r")
    return f"the board said so ({len(d.since(i))} log lines)"


@case("T11", "After a 429 the app waits, and says so", apis="fake", side_effect=True, slow=True)
def t11(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T11")
    c.fake_toggl.fail_next(429, headers={"Retry-After": "8"})
    d.keys("s")
    d.wait_state(lambda s: s["link"]["toggl"] == "backoff", 15, "the backoff state")
    n = c.fake_toggl.count()
    time.sleep(3)
    check(c.fake_toggl.count() == n, "it kept asking during the backoff")
    c.toggl.wait_current(lambda x: x.desc == desc, 30, "the entry after the wait")
    d.keys("s")
    time.sleep(1)
    d.keys("r")
    return "no requests during the backoff; the run went through after it"


@case("T12", "With Toggl down, the app retries and the board says so", apis="fake", side_effect=True, slow=True)
def t12(c):
    c.idle()
    d = c.d
    desc = set_task(c, "T12")
    c.fake_toggl.down = True
    d.keys("s")
    d.wait_state(lambda s: s["link"]["toggl"] == "offline", 20, "the offline state")
    c.fake_toggl.down = False
    c.toggl.wait_current(lambda x: x.desc == desc, 45, "the entry after Toggl came back", every=1)
    d.wait_state(lambda s: s["link"]["toggl"] == "ok", 15, "the ok state")
    d.keys("s")
    time.sleep(1)
    d.keys("r")
    return "offline shown; the run went through when Toggl came back"


@case("P1", "The Pomodoro logs to Toggl, and time's up stops the entry", side_effect=True, slow=True)
def p1(c):
    c.idle("pomodoro")
    d = c.d
    desc = set_task(c, "P1")
    d.cmd("set 25")
    d.keys("s")
    c.toggl.wait_current(lambda x: x.desc == desc, 25, "the Pomodoro's entry")
    time.sleep(11)
    i = d.mark()
    d.cmd("remaining 2")
    check(d.wait(r"^LOG times_up", 8, since=i), "no time's up")
    st = d.wait_state(lambda s: s["pomo"]["state"] in ("done", "alarm"), 3, "time's up")
    check(st["mode"] == "pomodoro", "not on the Pomodoro's screen")
    c.shot("P1-times-up")
    c.toggl.wait_current(lambda x: x.desc != desc, 15, "the entry stopped")
    d.keys("s")
    d.wait_state(lambda s: s["pomo"]["state"] == "idle", 4, "dismissed")
    return "the entry stopped at time's up, and a tap dismissed it"


# ---- Voice ---------------------------------------------------------------------------------------------------------------


@case("V1", "A spoken task becomes the task, with a project it names", needs="mic", side_effect=True, slow=True)
def v1(c):
    st = c.idle()
    d = c.d
    check(st["link"]["voice_ok"], "the board says voice isn't available")
    i = d.mark()
    if c.a.target == "sim":
        d.keys("m")  # the simulator's mic plays its WAV file
    else:
        d.keys("m")
        time.sleep(.5)
        subprocess.Popen(["say", c.a.phrase])  # through the Mac's speakers, into the board's mic
    check(d.wait(r"^LOG voice (listening|sending)", 6, since=i), "the mic never started")
    m = d.wait(r"^LOG voice (transcript|failed)", 90, since=i)
    check(m, "no result from speech-to-text")
    check(m[1] == "transcript", "speech-to-text failed")
    st = d.wait_state(lambda s: s["voice"] == "idle", 5, "voice finished")
    if c.fake_speech:
        check(st["task"].startswith("Acme design review"), "the fake transcript didn't become the task")
        check(st["project"]["id"] == 101, f"the named project wasn't chosen: {st['project']['id']}")
        keywords = c.fake_speech.requests[-1]["keywords"]
        check("Acme" in keywords, "the project names weren't sent as keywords")
        got = "the fake transcript"
    else:
        said = set(re.findall(r"[a-z0-9]+", c.a.phrase.lower()))
        heard = set(re.findall(r"[a-z0-9]+", st["task"].lower()))
        check(len(said & heard) >= len(said) // 2, f"{len(said & heard)} of {len(said)} words heard")
        got = f"{len(said & heard)} of {len(said)} words"
    d.cmd("task ")
    return f"set the task from {got}"


# ---- Lock ----------------------------------------------------------------------------------------------------------------


def open_lock_from_menu(c):
    c.menu_choose("lock")


@case("L1", "With no code, Lock asks for one on the Mac, and saving one doesn't lock")
def l1(c):
    c.idle()
    d = c.d
    check(c.app.lock_code("remove") == 0, "couldn't remove the code")
    d.wait_state(lambda s: not s["lock"]["code_set"], 8, "no code on the board")
    open_lock_from_menu(c)
    st = d.wait_state(lambda s: s["menu"]["open"] and "Mac" in s["toast"], 3, 'the "Set a code" message')
    check(not st["lock"]["on"], "it locked with no code")
    d.keys("p")
    i = d.mark()
    check(c.app.lock_code("set", LOCK_CODE) == 0, "couldn't set the code")
    check(d.wait(r"^LOG lock code_set", 10, since=i), "the code never reached the board")
    st = d.state()
    check(st["lock"]["code_set"] and not st["lock"]["on"], "saving the code locked the board")
    return "asked for a code; saving one left the board unlocked"


@case("L2", "A locked board keeps its screen, with an icon, until touched")
def l2(c):
    c.idle()
    d = c.d
    d.cmd("task bento-lock-screen")
    before = c.shot("L2-before")
    i = d.mark()
    open_lock_from_menu(c)
    check(d.wait(r"^LOG lock locked", 4, since=i), "it didn't lock")
    st = d.state()
    check(st["lock"]["on"] and not st["lock"]["pad"], "the keypad showed before a touch")
    check(st["screen"] == "timer", f"the screen is {st['screen']}")
    time.sleep(.2)
    after = c.shot("L2-locked")
    w, h = after.w, after.h
    changed = sum(1 for y in range(0, h // 8) for x in range(w * 3 // 8, w * 5 // 8) if before.rgb(x, y) != after.rgb(x, y))
    check(changed > 10, f"no lock icon near twelve o'clock ({changed} pixels changed)")
    d.keys("t")
    st = d.wait_state(lambda s: s["lock"]["pad"], 3, "the keypad")
    check(st["lock"]["entry_len"] == 0, "the first touch pressed a key")
    time.sleep(.2)
    c.shot("L2-keypad")
    return f"same screen with an icon ({changed} pixels changed); the first touch only showed the keypad"


@case("L3", "A knob turn shows the keypad, and wrong codes don't unlock", needs="knob")
def l3(c):
    d = c.d
    c.idle()
    d.cmd("lock")
    st = d.wait_state(lambda s: s["lock"]["on"], 3, "locked")
    check(not st["lock"]["pad"], "the keypad was already up")
    d.keys("+")
    st = d.wait_state(lambda s: s["lock"]["pad"], 3, "the keypad after a knob turn")
    check(st["lock"]["entry_len"] == 0, "a knob turn entered a digit")
    time.sleep(.2)
    c.shot("L3-keypad")
    d.keys("+")
    check(d.state()["lock"]["entry_len"] == 0, "the knob entered a digit")
    return "a turn brought up the whole keypad, and the knob entered nothing"


@case("L4", "Wrong codes stay locked; the right one unlocks; nothing else opens")
def l4(c):
    d = c.d
    c.idle()
    d.cmd("lock")
    d.keys("t")
    d.wait_state(lambda s: s["lock"]["pad"], 3, "the keypad")
    d.keys("u")
    st = d.state()
    check(not st["menu"]["open"], "the menu opened while locked")
    wrong = "".join("0" if ch != "0" else "1" for ch in LOCK_CODE)
    i = d.mark()
    c.press_code(wrong)
    check(d.wait(r"^LOG lock wrong", 4, since=i), "no wrong-code log")
    check(d.state()["lock"]["on"], "a wrong code unlocked it")
    c.shot("L4-wrong")
    i = d.mark()
    c.press_code(LOCK_CODE)
    check(d.wait(r"^LOG lock unlocked", 4, since=i), "the right code didn't unlock")
    return "the menu stayed closed, the wrong code was refused, and the right one unlocked"


@case("L5", "A locked board makes no sound, not even the alarm")
def l5(c):
    c.idle("pomodoro")
    d = c.d
    d.cmd("set 25")
    d.keys("s")
    d.wait_state(lambda s: s["running"] == "pomodoro", 4, "the Pomodoro running")
    d.cmd("lock")
    i = d.mark()
    d.cmd("remaining 2")
    check(d.wait(r"^LOG times_up", 8, since=i), "no time's up while locked")
    time.sleep(.5)
    feedback = [l for l in d.since(i) if l.startswith("LOG feedback")]
    check(feedback, "no feedback at all")
    loud = [l for l in feedback if "sound=1" in l]
    check(not loud, f"sound was allowed: {loud[:2]}")
    st = d.state()
    check(st["lock"]["on"], "time's up unlocked the board")
    d.cmd("unlock")
    d.keys("s")
    d.wait_state(lambda s: s["pomo"]["state"] == "idle", 4, "dismissed")
    return f"{len(feedback)} feedback events, all silent"


@case("L6", "The lock survives a restart, and a new code from the Mac replaces a forgotten one", slow=True)
def l6(c):
    c.idle()
    d = c.d
    d.cmd("lock")
    time.sleep(2.5)
    c.reboot()
    c.wait_ready()
    st = d.state()
    check(st["lock"]["on"], "a restart unlocked the board")
    i = d.mark()
    check(c.app.lock_code("set", NEW_LOCK_CODE) == 0, "couldn't set a new code")
    check(d.wait(r"^LOG lock code_set", 10, since=i), "the new code never reached the board")
    check(d.state()["lock"]["on"], "a new code unlocked the board")
    d.keys("t")
    d.wait_state(lambda s: s["lock"]["pad"], 3, "the keypad")
    c.press_code(LOCK_CODE)
    check(d.state()["lock"]["on"], "the old code still worked")
    i = d.mark()
    c.press_code(NEW_LOCK_CODE)
    check(d.wait(r"^LOG lock unlocked", 4, since=i), "the new code didn't unlock")
    return "stayed locked through a restart; the Mac's new code replaced the old one"


# ---- Bluetooth --------------------------------------------------------------------------------------------------------


@case("Z1", "Bluetooth: a paired Mac connects over an encrypted link", needs="ble")
def z1(c):
    st = c.wait_ready(60)
    check(not st["link"]["usb"], "the link is still over USB")
    return f"connected over Bluetooth; uid {st['link']['uid'][:2]}…"


# ---- Running ---------------------------------------------------------------------------------------------------------------


def pick_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        others = glob.glob("/dev/cu.usbserial*")
        hint = " A /dev/cu.usbserial port is there: flip the USB-C plug at the knob end." if others else ""
        sys.exit("No board on /dev/cu.usbmodem*." + hint)
    return ports[0]


def deploy(a):
    tool = os.path.join(HERE, "bento")
    if subprocess.call([sys.executable, tool, "build", a.board, "--debug"]) != 0:
        sys.exit("The build failed.")
    if not glob.glob(os.path.join(ROOT, "backup", a.board + "-*.bin")):
        print(f"Backing up {a.board}'s flash first (a minute or two)...", flush=True)
        if subprocess.call([sys.executable, tool, "backup", a.board]) != 0:
            sys.exit("The backup failed, so nothing was flashed.")
    if subprocess.call([sys.executable, tool, "flash", a.board, "--debug"]) != 0:
        sys.exit("The flash failed.")


def setup_real(a):
    # The app's own Keychain and config code, so the harness reads the same keys from the same place as the app.
    sys.path.insert(0, os.path.join(ROOT, "desktop"))
    import config as app_config
    from secrets_store import KeychainStore, SecretStoreError
    store = KeychainStore()
    try:
        missing = [n for n in ("toggl", "speech") if not store.has(n)]
    except SecretStoreError as e:
        sys.exit(f"--apis real needs the Keychain: {e}.")
    if missing:
        sys.exit("--apis real needs your keys in the Keychain. Missing: " + ", ".join(missing) + ".\n"
                 "Run these yourself: desktop/.venv/bin/python desktop/bentobox.py set-key toggl (and speech), then set-org.")

    def key():  # read when it's needed, and never kept or printed
        try:
            return store.get("toggl")
        except SecretStoreError:
            return None

    return key, app_config.load_config().get("toggl_org", 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="sim, or a board's folder name such as knob-lcd-1.8")
    ap.add_argument("--board", help="with sim: which board to simulate")
    ap.add_argument("--apis", choices=["fake", "real"], default="fake")
    ap.add_argument("--link", choices=["usb", "ble", "tcp"])
    ap.add_argument("--deploy", action="store_true", help="build, back up if there's no backup, and flash first")
    ap.add_argument("--pair", action="store_true", help="open pair mode, and wait for you to type the code on the Mac")
    ap.add_argument("--port", help="the board's serial port")
    ap.add_argument("--only", help="comma-separated case ids, such as T1,L2 (a wildcard like T* works)")
    ap.add_argument("--quick", action="store_true", help="skip slow cases")
    ap.add_argument("--phrase", default="Review the quarterly planning document")
    ap.add_argument("--list", action="store_true", help="list the cases")
    ap.add_argument("--show", action="store_true", help="with sim: open the simulator's window, to watch the run")
    a = ap.parse_args()

    if a.list:
        for x in CASES:
            print(f"{x['id']:<4} {x['title']}")
        return 0

    is_sim = a.target == "sim"
    a.board = a.board or ("knob-lcd-1.8" if is_sim else a.target)
    a.link = a.link or ("tcp" if is_sim else "usb")
    if is_sim and a.link == "ble":
        sys.exit("The simulator has no Bluetooth: use --link tcp or usb.")
    if a.deploy and is_sim:
        sys.exit("--deploy is for a board.")

    out = os.path.join(ROOT, "test-output", f"{'sim-' + a.board if is_sim else a.board}-{a.apis}-{a.link}")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    c = Ctx(a, out)

    get_key, org = (None, 0)
    if a.apis == "real":
        get_key, org = setup_real(a)
    if a.deploy:
        deploy(a)

    # The fakes come from the desktop app's own tests. That module redirects HOME for this process, on purpose.
    keys = None
    if a.apis == "fake":
        sys.path.insert(0, os.path.join(DESKTOP, "tests"))
        real_home = os.environ.get("HOME")
        import helpers
        os.environ["HOME"] = real_home or ""
        c.fake_toggl = helpers.FakeToggl()
        c.fake_toggl.projects = [{"id": 101, "name": "Acme", "color": "#F2C14E", "active": True},
                                 {"id": 102, "name": "Deep work", "color": "#4FB3F6", "active": True}]
        c.fake_speech = helpers.FakeSpeech()
        keys = {"toggl": helpers.KEY, "speech": helpers.SPEECH_KEY}
        toggl_url, stt_url, org = c.fake_toggl.url, c.fake_speech.url, helpers.ORG
        get_key = lambda: helpers.KEY  # noqa: E731
    else:
        toggl_url, stt_url = "https://focus.toggl.com/api", "https://api.openai.com/v1/audio/transcriptions"
    c.toggl = Toggl(toggl_url, get_key, org)

    # The device
    wav = None
    link_port = None
    if is_sim:
        if subprocess.call(["make", "-s", "-C", SIM_DIR, "sharp" if a.show else "all"]) != 0:
            sys.exit("The simulator didn't build.")
        link_port, debug_port = free_port(), free_port()
        wav = os.path.join(out, "speech.wav")
        subprocess.run(["say", "-o", wav, "--data-format=LEI16@16000", "--file-format=WAVE", a.phrase], check=False)
        if a.show:
            # One window for every run, through every restart. A second viewer on the file just exits, and closing the
            # window doesn't touch the test.
            os.makedirs(os.path.dirname(PANEL_FILE), exist_ok=True)
            subprocess.Popen([SIM_SHARP_BIN, "--view", PANEL_FILE], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = [SIM_SHARP_BIN if a.show else SIM_BIN, "--board", a.board, "--headless"] + (["--panel-file", PANEL_FILE] if a.show else []) + [
            "--store", os.path.join(out, "store.bin"), "--link-port", str(link_port), "--debug-port", str(debug_port), "--wav", wav]
        c.sim_proc = subprocess.Popen(cmd, stdout=open(os.path.join(out, "sim.log"), "w"), stderr=subprocess.STDOUT)
        port = TcpPort(debug_port)
    else:
        port = SerialPort(a.port or pick_port())
    c.d = Device(port, os.path.join(out, "device.log"))
    c.d.start()
    print(f"{a.target}" + (f" ({a.board})" if is_sim else "") + f": APIs {a.apis}, link {a.link}. Output: {out}", flush=True)
    if not c.d.wait(r"^LOG boot", 8, since=0):
        c.d.send("#reboot\n")  # the port opened without a reset: make it a fresh start
        c.d.wait(r"^LOG boot", 30, since=0)
    time.sleep(.5)
    st = c.d.state()
    c.has = st["has"]
    c.shape = "square" if a.board.startswith("amoled") else "round"
    if st["lock"]["on"]:
        c.d.cmd("unlock")

    # The desktop app
    if a.link == "usb":
        c.d.cmd("link usb")
        c.bridge = Bridge(c.d)
    app_link = {"tcp": "tcp", "usb": "tcp", "ble": "ble"}[a.link]
    display_port = c.bridge.port if c.bridge else (link_port or 0)
    c.app = Desktop(a, out, app_link, display_port, toggl_url, stt_url, keys, org)
    if a.apis == "real" and not org:
        print("  (no organization id in ~/.config/bentobox/config.json: the app will look for one)", flush=True)
    c.app.start()

    if a.pair:
        d = c.d
        d.cmd("mode settings")
        st = d.wait_state(lambda s: s["settings"].get("rows"), 3, "Settings")
        print("Open pair mode on the board now: Settings › Pair a new Mac. Then type the board's code into the Mac's dialog.", flush=True)
        input("Press Return once it's paired... ")

    # The cases
    only = {x.strip().upper() for x in a.only.split(",")} if a.only else None
    results = {}
    t0 = time.time()
    limited = False
    for x in CASES:
        cid = x["id"]
        reason = None
        if only and not any(fnmatch.fnmatch(cid, p) for p in only):
            continue
        if x["apis"] != "both" and x["apis"] != a.apis:
            reason = f"only with --apis {x['apis']}"
        elif a.quick and x["slow"]:
            reason = "--quick"
        elif x["needs"] == "ble" and a.link != "ble":
            reason = "needs --link ble"
        elif x["needs"] in ("mic", "knob", "motion") and not c.has.get(x["needs"]):
            reason = f"this board has no {x['needs']}"
        elif x["needs"] == "no_knob" and c.has.get("knob"):
            reason = "this board has a knob"
        elif limited and x["side_effect"]:
            reason = "Toggl's rate limit"
        if reason:
            results[cid] = {"result": "skip", "detail": reason, "title": x["title"]}
            print(f"  skip {cid:<4} {x['title']}: {reason}", flush=True)
            continue
        start = time.time()
        if not c.real:
            c.toggl.client.rate_until = 0
            c.toggl.client.next_try = 0
        try:
            detail = x["fn"](c) or ""
            outcome = "pass"
        except Skipped as e:
            outcome, detail = "skip", str(e)
            limited = limited or (c.real and c.toggl.limited)
        except Failed as e:
            outcome, detail = "FAIL", str(e)
        except Exception as e:  # a harness or device problem
            outcome, detail = "FAIL", f"{type(e).__name__}: {e}"
            traceback.print_exc(file=open(os.path.join(out, "harness.log"), "a"))
        if c.sim_proc and c.sim_proc.poll() is not None:
            outcome, detail = "FAIL", f"the simulator died (exit {c.sim_proc.returncode}); {detail}"
        results[cid] = {"result": outcome, "detail": detail, "title": x["title"], "seconds": round(time.time() - start, 1)}
        print(f"  {outcome:<4} {cid:<4} {x['title']}: {detail}  [{time.time() - start:.1f} s]", flush=True)
        if outcome == "FAIL":
            try:
                c.shot(f"FAILED-{cid}")
                c.idle()
            except Exception:
                pass

    crash = list(c.d.crashes)
    if crash:
        results["CRASH"] = {"result": "FAIL", "detail": crash[0][:200], "title": "No crashes during the whole run"}
        print(f"  FAIL CRASH {crash[0][:200]}", flush=True)
    else:
        results["CRASH"] = {"result": "pass", "detail": "none", "title": "No crashes during the whole run"}

    # Clean up
    cleanup = None
    secrets = []
    try:
        with open(os.path.join(c.app.home, ".config", "bento", "lock.json")) as f:
            stored = json.load(f)
        secrets += [str(v) for k, v in stored.items() if k in ("salt", "hash") and v]
    except (OSError, ValueError):
        pass
    if keys:
        secrets += list(keys.values())
    try:
        if not only and c.has:
            c.app.lock_code("remove")
    except Exception:
        pass
    if a.apis == "real":
        cleanup = c.toggl.cleanup()
        print(f"  Toggl clean-up: {cleanup} (this harness made about {c.toggl.requests} of its own requests)", flush=True)
    c.app.close()
    c.d.close()
    if c.bridge:
        c.bridge.close()
        c.d.send("#link ble\n")  # as it was: a board left on USB would ignore Bluetooth until it restarts
        time.sleep(.3)
    if c.sim_proc:
        c.sim_proc.terminate()
    if c.fake_toggl:
        c.fake_toggl.close()
        c.fake_speech.close()

    # A last look for anything that shouldn't be in the logs
    secretish = []
    for name in os.listdir(out):
        if name.endswith((".log", ".json")):
            with open(os.path.join(out, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
            found = re.search(r"toggl_sk_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{20,}", text) or any(v in text for v in secrets)
            # The codes themselves, wherever a command or a log line could carry them. STATE is full of unrelated numbers.
            for line in text.splitlines():
                if not line.startswith(("STATE ", "TELEMETRY ")) and re.search(r"\b(" + LOCK_CODE + "|" + NEW_LOCK_CODE + r")\b", line):
                    found = True
            if found:
                secretish.append(name)
    results["LEAK"] = {"result": "FAIL" if secretish else "pass", "detail": f"found in {secretish}" if secretish else "none",
                       "title": "No keys or lock codes in the logs"}
    print(f"  {results['LEAK']['result']:<4} LEAK No keys or lock codes in the logs: {results['LEAK']['detail']}", flush=True)

    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump({"target": a.target, "board": a.board, "apis": a.apis, "link": a.link, "seconds": round(time.time() - t0),
                   "toggl_cleanup": cleanup, "results": results}, f, indent=1)
    counts = {k: sum(1 for r in results.values() if r["result"] == k) for k in ("pass", "FAIL", "skip")}
    print(f"{counts['pass']} passed, {counts['FAIL']} failed, {counts['skip']} skipped in {time.time() - t0:.0f} s.", flush=True)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
