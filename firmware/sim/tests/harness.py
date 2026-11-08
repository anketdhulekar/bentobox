"""Drives bento-sim through its debug interface: the same text a board speaks over USB. See docs/DEBUG.md."""
import base64
import collections
import json
import os
import select
import socket
import struct
import subprocess
import threading
import time
import zlib

# macOS lets a sleep end late to save power: a few milliseconds late in a terminal, and up to 100 ms late for a process it
# treats as background, such as a CI runner's. A case that paces a drag or times a hold needs its sleeps on time, so on
# macOS they're a kqueue timer marked critical, as the simulator's frames are (hal_sim.c).
_NOTE_USECONDS, _NOTE_CRITICAL = 0x2, 0x20  # <sys/event.h>


def sleep(seconds):
    if seconds <= 0:
        return
    if not hasattr(select, "kqueue"):
        time.sleep(seconds)
        return
    kq = select.kqueue()
    try:
        timer = select.kevent(1, filter=select.KQ_FILTER_TIMER, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=_NOTE_USECONDS | _NOTE_CRITICAL, data=max(1, int(seconds * 1e6)))
        kq.control([timer], 1, None)
    finally:
        kq.close()


def rgb565_to_rgb(p):
    return ((p >> 8 & 0xF8) | (p >> 13), (p >> 3 & 0xFC) | (p >> 9 & 3), (p << 3 & 0xF8) | (p >> 2 & 7))


def write_png(path, w, h, rgb):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 3)) + chunk(b"IEND", b""))


class Frame:
    """A screen capture: RGB565 pixels, read as (r, g, b)."""

    def __init__(self, w, h, data):
        self.w, self.h, self.data = w, h, data

    def px565(self, x, y):
        i = (y * self.w + x) * 2
        return self.data[i] | self.data[i + 1] << 8

    def rgb(self, x, y):
        return rgb565_to_rgb(self.px565(x, y))

    def to_rgb(self):
        out = bytearray(self.w * self.h * 3)
        d = self.data
        for i in range(self.w * self.h):
            p = d[i * 2] | d[i * 2 + 1] << 8
            out[i * 3] = (p >> 8 & 0xF8) | (p >> 13)
            out[i * 3 + 1] = (p >> 3 & 0xFC) | (p >> 9 & 3)
            out[i * 3 + 2] = (p << 3 & 0xF8) | (p >> 2 & 7)
        return out

    def count(self, x0, y0, x1, y1, pred):
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                if pred(self.rgb(x, y)):
                    n += 1
        return n


class Sim:
    def __init__(self, binary, board, out_dir, base_port, store, wav=None, demo=False, log_path=None, panel_file=None):
        self.binary, self.board, self.out_dir = binary, board, out_dir
        self.panel_file = panel_file  # --show: the panel goes to this file, for the one viewer window
        self.link_port, self.debug_port = base_port, base_port + 1
        self.store, self.wav, self.demo = store, wav, demo
        self.log_path = log_path
        self.proc = None
        self.sock = None
        self.lines = collections.deque()
        self.links = collections.deque()
        self.logs = collections.deque(maxlen=4000)
        self.cond = threading.Condition()
        self.capture = None
        self.crashed_output = []
        os.makedirs(out_dir, exist_ok=True)

    # ---- The process ------------------------------------------------------------------------------------------------

    def start(self, fresh=True):
        if fresh and os.path.exists(self.store):
            os.unlink(self.store)
        cmd = [self.binary, "--board", self.board, "--headless", "--store", self.store, "--link-port", str(self.link_port), "--debug-port", str(self.debug_port)]
        if self.wav:
            cmd += ["--wav", self.wav]
        if self.demo:
            cmd.append("--demo")
        if self.panel_file:
            cmd += ["--panel-file", self.panel_file]
        env = dict(os.environ, ASAN_OPTIONS="abort_on_error=0:detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1")
        self.errlog = open(self.log_path or os.devnull, "ab")
        self.proc = subprocess.Popen(cmd, stdout=self.errlog, stderr=self.errlog, env=env)
        self.connect()

    def connect(self, timeout=8):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.debug_port), timeout=1)
                break
            except OSError:
                if time.time() > deadline or (self.proc and self.proc.poll() is not None):
                    raise RuntimeError(f"can't reach the simulator on port {self.debug_port}: exit {self.proc.poll() if self.proc else None}")
                sleep(0.05)
        self.sock.settimeout(None)
        self.reader = threading.Thread(target=self._read, args=(self.sock,), daemon=True)
        self.reader.start()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self.sock.close()
        except OSError:
            pass

    def reboot(self):
        """#reboot, and back. The start's own log lines go out before anyone can be connected, so it's told by the uptime."""
        self.send("#reboot")
        sleep(0.2)
        try:
            self.sock.close()
        except OSError:
            pass
        sleep(0.2)
        with self.cond:
            self.lines.clear()
            self.links.clear()
        self.connect()
        self.wait_state(lambda st: st["uptime_ms"] < 8000, timeout=8, what="a fresh start")
        self.cmd("#verify on")

    # ---- Reading ----------------------------------------------------------------------------------------------------

    def _read(self, sock):
        buf = b""
        while True:
            try:
                data = sock.recv(1 << 18)
            except OSError:
                return
            if not data:
                return
            buf += data
            parts = buf.split(b"\n")
            buf = parts.pop()
            with self.cond:
                for raw in parts:
                    self._line(raw.decode("utf-8", "replace"))
                self.cond.notify_all()

    def _line(self, line):
        if self.capture is not None:
            if line == "CAPTURE END":
                self.lines.append(("capture", self.capture))
                self.capture = None
            else:
                self.capture[2].extend(base64.b64decode(line))
            return
        if line.startswith("CAPTURE "):
            _, w, h = line.split()
            self.capture = [int(w), int(h), bytearray()]
        elif line.startswith("LINK "):
            self.links.append(json.loads(line[5:]))
        elif line.startswith("LOG "):
            self.logs.append(line[4:])
            self.lines.append(("log", line[4:]))
        elif line.startswith("STATE "):
            self.lines.append(("state", line[6:]))
        elif line.startswith("CMD "):
            self.lines.append(("cmd", line[4:]))
        elif line.startswith("SIM "):
            self.lines.append(("sim", line[4:]))
        elif line.startswith(("PROJECTS ", "TASKS ", "TELEMETRY ", "PERF ")):
            kind, rest = line.split(" ", 1)
            self.lines.append((kind.lower(), rest))
        else:
            self.lines.append(("other", line))

    def _wait(self, kind, timeout=5, pred=None):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for i, (k, v) in enumerate(self.lines):
                    if k == kind and (pred is None or pred(v)):
                        del self.lines[i]
                        return v
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"no {kind} line within {timeout} s")
                self.cond.wait(remaining)

    def clear(self):
        with self.cond:
            self.lines.clear()

    # ---- Commands ---------------------------------------------------------------------------------------------------

    def send(self, text):
        self.sock.sendall(text.encode("utf-8", "surrogatepass") + b"\n") # a lone surrogate is a test too

    def cmd(self, text, expect=True):
        """A # command: waits for its CMD line, and returns whether it said ok."""
        name = text[1:].split(" ")[0]
        self.send(text)
        reply = self._wait("cmd", pred=lambda v: v.split(" ")[1] == name, timeout=8)
        ok = reply.startswith("ok")
        if expect is not None and ok != expect:
            raise AssertionError(f"{text}: {reply}")
        return ok

    def simcmd(self, text):
        self.send("%" + text)
        self._wait("sim", timeout=5)

    def key(self, ch):
        self.send(ch)

    def state(self):
        self.send("d")
        return json.loads(self._wait("state", timeout=8))

    def wait_state(self, pred, timeout=5, what="a state"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.state()
            if pred(last):
                return last
            sleep(0.03)
        raise TimeoutError(f"never reached {what}; last: {json.dumps(last)[:500] if last else None}")

    def settle(self, seconds=0.12):
        """Long enough for a frame to be drawn, and for glides to mostly finish."""
        sleep(seconds)

    def wait_log(self, prefix, timeout=5):
        return self._wait("log", timeout=timeout, pred=lambda v: v.startswith(prefix))

    def clear_logs(self):
        with self.cond:
            self.lines = collections.deque(x for x in self.lines if x[0] != "log")

    def frame(self):
        self.send("c")
        w, h, data = self._wait("capture", timeout=20)
        return Frame(w, h, bytes(data))

    def round_screen(self):
        """From the board's own state, once: its shape, never its name."""
        if not hasattr(self, "_round"):
            self._round = self.state()["shape"] == "round"
        return self._round

    def shot(self, name, frame=None):
        """Saves what the debug interface's capture shows, as a PNG, in the board's real shape."""
        f = frame or self.frame()
        rgb = f.to_rgb()
        if self.round_screen():
            c = f.w / 2
            r2 = c * c
            for y in range(f.h):
                for x in range(f.w):
                    if (x + 0.5 - c) ** 2 + (y + 0.5 - c) ** 2 > r2:
                        i = (y * f.w + x) * 3
                        rgb[i] = rgb[i + 1] = rgb[i + 2] = 0
        write_png(os.path.join(self.out_dir, name + ".png"), f.w, f.h, rgb)
        return f

    # ---- Acting as the Mac, over the debug interface ------------------------------------------------------------------

    def link_usb(self):
        self.cmd("#link usb")
        sleep(0.05)

    def mac(self, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        self.send("LINK " + text)

    def wait_link(self, pred, timeout=5, what="a link message"):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                for i, m in enumerate(self.links):
                    if pred(m):
                        del self.links[i]
                        return m
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"never got {what}; have {list(self.links)[:5]}")
                self.cond.wait(remaining)

    def clear_links(self):
        with self.cond:
            self.links.clear()
