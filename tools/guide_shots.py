#!/usr/bin/env python3
"""Takes the user guide's screenshots (docs/USER-GUIDE.md) from the simulator, on both boards, into docs/guide/<board>/.

It drives the real board UX through its debug interface and acts as the Mac with the test suite's sample agents,
projects and tasks, so the pictures match what a board shows. It takes only the pictures the guide uses, and removes
the ones it no longer does: to add one, reference guide/<board>/<name>.png in the guide, with a step here that takes
<name>. Run it after a visible change:

    make -C firmware/sim all && python3 tools/guide_shots.py            # both boards
    python3 tools/guide_shots.py --board knob-lcd-1.8 --only menu,timer-running
"""
import argparse
import calendar
import math
import os
import re
import struct
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware", "sim", "tests"))
from harness import Sim  # noqa: E402
from run_tests import SAMPLE_AGENTS, Case  # noqa: E402

BOARDS = {"knob-lcd-1.8": "knob", "amoled-2.16": "square"}
NOW = calendar.timegm((2026, 11, 7, 9, 41, 0, 0, 0, 0))  # 9:41 AM on Saturday 7th November 2026, shown in UTC
GUIDE = os.path.join(ROOT, "docs", "USER-GUIDE.md")
OUT = os.path.join(ROOT, "docs", "guide")


def wanted(folder):
    """The pictures the guide shows for one board."""
    return set(re.findall(r"guide/%s/([\w-]+)\.png" % re.escape(folder), open(GUIDE).read()))


def png(path, w, h, rgb, round_screen):
    """An RGBA PNG: a round screen is cut to its circle, with a soft edge, so it sits on any page color."""
    rows = bytearray()
    c = w / 2
    for y in range(h):
        rows.append(0)
        for x in range(w):
            i = (y * w + x) * 3
            a = 255
            if round_screen:
                a = int(255 * max(0.0, min(1.0, c - math.hypot(x + 0.5 - c, y + 0.5 - c))))
            rows += bytes((rgb[i], rgb[i + 1], rgb[i + 2], a))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


class Guide:
    def __init__(self, sim, board, only):
        self.s, self.c = sim, Case(sim, board)
        self.round = self.c.round
        self.dir = os.path.join(OUT, BOARDS[board])
        self.wanted = wanted(BOARDS[board])
        self.only = only
        self.seq = 10
        self.taken = []
        os.makedirs(self.dir, exist_ok=True)

    def skip(self, name):
        return name not in self.wanted or (self.only and name not in self.only)

    def tidy(self):
        """Removes the pictures the guide no longer shows, and says which it shows but nothing here takes."""
        for f in os.listdir(self.dir):
            if f.endswith(".png") and f[:-4] not in self.wanted:
                os.unlink(os.path.join(self.dir, f))
        if not self.only:
            missing = sorted(self.wanted - set(self.taken))
            if missing:
                sys.exit(f"{os.path.relpath(self.dir, ROOT)}: the guide shows {', '.join(missing)}, which no step takes")

    def shot(self, name, settle=0.25):
        if self.skip(name):
            return
        self.ping()
        self.s.settle(settle)
        f = self.s.frame()
        png(os.path.join(self.dir, name + ".png"), f.w, f.h, f.to_rgb(), self.round)
        self.taken.append(name)

    def panel_shot(self, name):
        """What a person sees, read from the panel itself: the pairing page's code shows only there."""
        if self.skip(name):
            return
        path = os.path.join(tempfile.mkdtemp(), "panel.ppm")
        self.s.simcmd("panel " + path)
        data = open(path, "rb").read()
        parts = data.split(b"\n", 3)  # P6, "W H", 255, pixels
        w, h = (int(n) for n in parts[1].split())
        png(os.path.join(self.dir, name + ".png"), w, h, parts[3][:w * h * 3], self.round)
        os.unlink(path)
        self.taken.append(name)

    def ping(self):
        """The Mac's state, so the time reads 9:41 AM and the board never thinks the Mac has gone."""
        self.seq += 1
        self.s.mac({"t": "state", "v": 4, "seq": self.seq, "now": NOW, "utc": 0, "toggl": "ok", "voice": True,
                    "n": len(SAMPLE_AGENTS)})

    def agent(self, i, **change):
        self.s.mac({"t": "agents", "rows": [dict(SAMPLE_AGENTS[i], **change)]})

    def wait(self, pred, what):
        return self.s.wait_state(pred, what=what)

    def run(self):
        s, c = self.s, self.c
        c.connect_mac(lock=True)
        self.ping()
        s.cmd("#burnin 0 0")
        s.cmd("#fx seed 7")
        s.cmd("#anim 1.25")
        s.cmd("#quiet")

        # The menu
        s.cmd("#mode timer")
        s.key("u")
        self.wait(lambda v: v["menu"]["open"], "the menu")
        self.shot("menu", 0.6)
        s.key("p")
        self.wait(lambda v: not v["menu"]["open"], "the menu closed")

        # The Timer, with a project and a task
        s.cmd("#project 123")
        s.cmd("#task Write the Q4 plan")
        s.cmd("#quiet")
        self.shot("timer-idle")
        s.key("o")
        self.wait(lambda v: v["list"]["open"], "the projects list")
        self.shot("projects", 0.6)
        s.key("p")
        self.wait(lambda v: not v["list"]["open"], "the list closed")
        s.key("e")
        self.wait(lambda v: v["list"]["open"], "the tasks list")
        self.shot("tasks", 0.6)
        s.key("p")
        self.wait(lambda v: not v["list"]["open"], "the list closed")
        s.key("s")
        self.wait(lambda v: v["timer"]["state"] == "running", "the Timer running")
        s.cmd("#elapsed 1513")
        c.drain()
        s.cmd("#quiet")
        self.shot("timer-running")

        # An agent that needs you, seen from the Timer
        self.agent(2, st="needs", act="Waiting for your answer")
        self.wait(lambda v: "needs you" in v["status"], "needs you")
        self.shot("needs-you", 0.5)
        self.agent(2)
        s.settle(0.3)

        # The lock
        c.menu_choose("lock")
        self.wait(lambda v: v["lock"]["on"], "locked")
        self.shot("locked", 0.5)
        s.cmd(f"#tap {c.base // 2} {c.base // 2}")
        self.wait(lambda v: v["lock"]["pad"], "the keypad")
        self.shot("keypad", 0.4)
        s.cmd("#unlock")
        s.settle(0.2)

        # Split view: the Clock under the running Timer
        c.menu_choose("clock")
        self.wait(lambda v: v["mode"] == "clock" and not v["menu"]["open"], "the Clock")
        self.shot("split-clock", 0.6)

        # The Inverted profile
        s.cmd("#mode timer")
        s.cmd("#theme inverted")
        self.shot("timer-inverted", 0.4)
        s.cmd("#theme mono")

        # Voice: listening
        s.key("m")
        self.wait(lambda v: v["voice"] == "listening", "listening")
        self.shot("voice", 0.3)
        s.key("t")
        try:
            end = s.wait_link(lambda m: m.get("t") == "voice_end", timeout=5, what="the clip's end")
            s.mac({"t": "transcript", "vid": end["vid"], "text": "Acme design review", "project": 101})
        except TimeoutError:
            pass
        s.settle(0.5)
        s.cmd("#quiet")

        # Back to rest
        s.cmd("#mode timer")
        if s.state()["timer"]["state"] == "running":
            s.key("s")
        s.key("r")
        c.drain()

        # The Pomodoro
        s.cmd("#mode pomodoro")
        s.cmd("#set 25")
        s.cmd("#project 101")
        s.cmd("#task Design review")  # short enough to stand still on a round screen
        s.cmd("#quiet")
        self.shot("pomodoro-set")
        s.key("s")
        self.wait(lambda v: v["pomo"]["state"] == "running", "the Pomodoro running")
        s.cmd("#remaining 1122")
        c.drain()
        s.cmd("#quiet")
        self.shot("pomodoro-running")
        s.cmd("#remaining 200")
        self.shot("pomodoro-warm")

        # Split view: Agents under the running Pomodoro
        c.menu_choose("zoo")
        self.wait(lambda v: v["mode"] == "zoo" and not v["menu"]["open"], "Agents")
        s.cmd("#fx clear")
        self.shot("split-agents", 0.8)

        # Time's up
        s.cmd("#mode pomodoro")
        s.cmd("#remaining 2")
        self.wait(lambda v: v["pomo"]["state"] == "done", "time's up")
        s.cmd("#quiet")
        self.shot("pomodoro-done", 0.5)
        s.key("s")
        s.key("r")
        c.drain()

        # Agents, with no clock running
        s.cmd("#mode zoo")
        s.cmd("#fx clear")
        self.shot("agents", 0.8)
        s.key("r")
        self.wait(lambda v: v["card"], "the details card")
        self.shot("agents-card", 0.5)
        s.key("p")
        self.wait(lambda v: not v["card"], "the card closed")
        for _ in range(3):  # to ci-cleanup-8b, which has just finished
            s.key("<")
            s.settle(0.15)
        s.settle(0.8)
        s.cmd("#fx done")
        s.cmd("#fx step 700")
        self.shot("agents-done", 0.3)
        s.cmd("#fx clear")

        # The Clock alone
        s.cmd("#mode clock")
        self.shot("clock", 0.6)

        # Settings
        s.cmd("#mode settings")
        self.wait(lambda v: v["settings"]["page"] == "main", "Settings")
        self.shot("settings", 0.6)
        c.settings_row("Device")
        self.wait(lambda v: v["settings"]["page"] == "device", "Device")
        self.shot("settings-device", 0.6)
        c.settings_row("Brightness")
        self.wait(lambda v: v["settings"]["page"] == "bright", "Brightness")
        self.shot("settings-brightness", 0.5)
        s.key("p")
        self.wait(lambda v: v["settings"]["page"] == "device", "back to Device")
        s.key("p")
        self.wait(lambda v: v["settings"]["page"] == "main", "back to Settings")
        c.settings_row("Timer & Pomodoro")
        self.wait(lambda v: v["settings"]["page"] == "clocks", "Timer & Pomodoro")
        self.shot("settings-clocks", 0.6)
        s.key("p")
        self.wait(lambda v: v["settings"]["page"] == "main", "back to Settings")
        c.settings_row("Connectivity")
        self.wait(lambda v: v["settings"]["page"] == "connect", "Connectivity")
        self.shot("settings-connect", 0.6)
        c.settings_row("Pair a new Mac")
        self.wait(lambda v: v["settings"]["page"] == "pair", "pairing")
        s.simcmd("pair on")
        s.settle(0.5)
        self.ping()
        self.panel_shot("settings-pair")
        s.simcmd("pair off")
        s.key("t")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board", choices=sorted(BOARDS), action="append", help="one board (default: both)")
    ap.add_argument("--only", help="comma-separated screenshot names")
    ap.add_argument("--sim", default=os.path.join(ROOT, "firmware", "sim", "build", "bento-sim"))
    args = ap.parse_args()
    if not os.path.exists(args.sim):
        sys.exit(f"no simulator at {args.sim}: build it with  make -C firmware/sim all")
    only = set(args.only.split(",")) if args.only else None
    tmp = tempfile.mkdtemp()
    for n, board in enumerate(args.board or BOARDS):
        # --demo only for its paired Macs, so Connectivity names one: the link then moves to the debug interface.
        sim = Sim(args.sim, board, os.path.join(tmp, board), 7960 + 4 * n, os.path.join(tmp, board + ".bin"), demo=True,
                  log_path=os.path.join(tmp, board + ".log"))
        sim.start()
        try:
            g = Guide(sim, board, only)
            g.run()
            g.tidy()
            print(f"{board}: {len(g.taken)} screenshots in {os.path.relpath(g.dir, ROOT)}")
        finally:
            sim.stop()


if __name__ == "__main__":
    main()
