#!/usr/bin/env python3
"""Runs the simulator headless on every board, drives every screen through the debug interface, saves PNG screenshots to
firmware/sim/out/<board>/, and fuzzes the link with malformed lines. With --asan (the default), under AddressSanitizer
and UBSan. Exits non-zero if anything failed.

    make -C firmware/sim asan && python3 firmware/sim/tests/run_tests.py
    python3 firmware/sim/tests/run_tests.py --board amoled-2.16 --only lock,rotation --plain
"""
import argparse
import base64
import json
import math
import os
import random
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sim, sleep  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SIMDIR = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SIMDIR))
VECTORS = os.path.join(ROOT, "protocol", "samples", "vectors.json")

SAMPLE_AGENTS = [
    {"id": "a1000e7", "name": "api-refactor-12", "proj": "api-server", "kind": "i", "st": "working", "act": "Editing main.c", "note": "", "age": 240, "up": 1200, "rex": 1, "where": "Rex session work"},
    {"id": "a1005e7", "name": "docs-site-3f", "proj": "docs", "kind": "i", "st": "working", "act": "Editing index.md", "note": "", "age": 120, "up": 400, "rex": 1, "agent": "codex"},
    {"id": "a1001e7", "name": "ml-eval-5d", "proj": "ml", "kind": "b", "st": "working", "act": "Reading train.py", "note": "", "age": 660, "up": 900, "rex": 0, "agent": "grok"},
    {"id": "a1006e7", "name": "ci-cleanup-8b", "proj": "infra", "kind": "i", "st": "done", "act": "Finished", "note": "All green", "age": 60, "up": 3000, "rex": 1, "subs": [{"id": "9f8e7d6c", "type": "Explore", "act": "Reading routes.ts"}]},
    {"id": "a1002e7", "name": "landing-page-5b", "proj": "web", "kind": "i", "st": "idle", "act": "Waiting for a task", "note": "", "age": 1560, "up": 5000, "rex": 1, "agent": "codex"},
]
PROJECTS = [{"id": 101, "name": "Acme", "color": "#F2C14E"}, {"id": 102, "name": "Admin", "color": "#9CA3AF"}, {"id": 123, "name": "Deep work", "color": "#4FB3F6"}, {"id": 104, "name": "Writing", "color": "#E86FA6"}]
TASKS = [
    {"desc": "Write the Q4 plan", "task_id": 0, "project": PROJECTS[2]},
    {"desc": "Acme design review", "task_id": 0, "project": PROJECTS[0]},
    {"desc": "Inbox", "task_id": 7, "project": PROJECTS[1]},
    {"desc": "No project here", "task_id": 0, "project": None},
]
LOCK = json.load(open(VECTORS))["lock"]
LOCK_MSG = {"t": "lock_code", "salt": LOCK["salt"], "iter": LOCK["iter"], "hash": LOCK["hash"]}


class Failed(Exception):
    pass


class Case:
    def __init__(self, sim, board):
        self.s, self.board = sim, board
        st = sim.state()  # the board's facts, from the board: never from its name
        self.round = st["shape"] == "round"
        self.mount = st["mount"]  # how the panel sits on the board
        self.base = st["base"]
        self.has = st["has"]
        self.has_knob = self.has["knob"]
        self.passes = 0

    def check(self, cond, what):
        if not cond:
            raise Failed(what)
        self.passes += 1

    def eq(self, got, want, what):
        if got != want:
            raise Failed(f"{what}: got {got!r}, wanted {want!r}")
        self.passes += 1

    # ---- Helpers ----

    def mac_state(self, agents=SAMPLE_AGENTS, **extra):
        msg = {"t": "state", "v": 4, "seq": 1, "now": 1793826000, "utc": -28800, "toggl": "ok", "voice": True, "n": len(agents)}
        msg.update(extra)
        self.s.mac(msg)
        self.mac_agents(agents)

    def mac_agents(self, agents, per_page=40):
        """The whole list, in pages, as the Mac sends it."""
        pages = max(1, (len(agents) + per_page - 1) // per_page)
        for p in range(pages):
            self.s.mac({"t": "agents", "page": p, "pages": pages, "rows": agents[p * per_page:(p + 1) * per_page]})

    def connect_mac(self, agents=SAMPLE_AGENTS, lists=True, lock=False):
        s = self.s
        s.link_usb()
        s.clear_links()
        s.mac({"t": "hello", "v": 4, "host": "MacBook Pro"})
        s.wait_link(lambda m: m.get("t") == "board", what="the board message")
        self.mac_state(agents)
        self.drain()
        if lists:
            s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS})
            s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": TASKS})
        if lock:
            s.mac(LOCK_MSG)
        s.settle()

    def drain(self):
        """Acknowledges everything saved in the outbox, as the app would, so a case starts with it empty."""
        s = self.s
        deadline = time.time() + 5
        while time.time() < deadline:
            if s.state()["link"]["outbox"] == 0:
                s.clear_links()
                return
            try:
                m = s.wait_link(lambda m: m.get("t") == "toggl", timeout=0.2)
            except TimeoutError:
                continue
            s.mac({"t": "ack", "id": m["id"]})
        raise Failed("the outbox didn't drain")

    def hello(self):
        s = self.s
        s.clear_links()
        s.mac({"t": "hello", "v": 4, "host": "MacBook Pro"})
        return s.wait_link(lambda m: m.get("t") == "board", what="the board message")

    def take_toggl(self, count):
        return [self.s.wait_link(lambda m: m.get("t") == "toggl", what=f"message {i + 1} of {count}") for i in range(count)]

    def pick_project_row(self, row):
        s = self.s
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="the projects list")
        if self.has_knob:
            s.cmd(f"#turn {row - 1}")
            s.key("t")
        else:
            s.cmd(f"#tap 240 {66 + (row + .5) * 76:.0f}")
        s.wait_state(lambda v: not v["list"]["open"], what="a project picked")

    def reset(self):
        """Back to a known state between cases."""
        s = self.s
        s.cmd("#unlock")
        s.cmd("#mac on")
        s.cmd("#anim off")
        s.cmd("#pulse off")
        s.cmd("#battery off")
        s.cmd("#theme mono")
        s.cmd("#flat off")
        s.cmd("#burnin 0 0")
        if self.s.state()["rotation"]["orient"] != self.mount:
            s.cmd("#tilt 0")
            s.wait_state(lambda st: st["rotation"]["orient"] == self.mount and not st["rotation"]["turning"], timeout=4, what="upright")
        s.cmd("#tilt_off")
        s.cmd("#mode timer")
        st = s.state()
        if st["timer"]["state"] == "running":
            s.key("s")
        s.cmd("#mode pomodoro")
        st = s.state()
        if st["pomo"]["state"] == "running":
            s.key("s")
        s.key("r")
        s.cmd("#mode timer")
        s.key("r")
        s.cmd("#task ")
        s.cmd("#project 0")
        s.cmd("#set 25")
        s.cmd("#quiet")
        s.settle(0.05)
        s.clear_links()

    def touch(self, kind, x, y):
        """A raw touch at an upright position: the panel's own pixels are turned by how it's mounted."""
        if self.mount == 180:
            x, y = self.base - 1 - x, self.base - 1 - y
        self.s.simcmd(f"touch {kind} {x} {y}")

    def brightness_bar(self, level):
        """A tap on one of the brightness page's ten bars. They're laid out in shares of the screen."""
        base = self.base
        self.s.cmd(f"#tap {base * .16 + base * .68 / 10 * (level + .5):.0f} {base * .62:.0f}")
        self.s.settle()

    def menu_spot(self, key):
        """Somewhere inside a mode's compartment, in layout units: six round the middle, clockwise from the upper left, or
        two by three."""
        if self.round:
            slot = ["timer", "pomodoro", "zoo", "clock", "lock", "settings"].index(key) - 0.5
            a = slot * 2 * math.pi / 6
            return 180 + 132 * math.sin(a), 180 - 132 * math.cos(a)
        return {"timer": (124, 86), "pomodoro": (355, 86), "zoo": (124, 240), "clock": (355, 240), "settings": (124, 394),
                "lock": (355, 394)}[key]

    def settings_row(self, label):
        """The Settings row that starts with this label, on the page shown: the knob turns to it and taps; a square board
        taps it, five rows to a screen."""
        s = self.s
        st = s.state()
        rows = [r["label"] for r in st["settings"]["rows"]]
        i = next(n for n, r in enumerate(rows) if r.startswith(label))
        if self.round:
            s.cmd(f"#turn {i - st['settings']['key']}")
            s.settle(0.2)
            s.key("t")
        else:
            s.cmd(f"#tap 240 {66 + (i + .5) * 76 - st['settings']['scroll']:.0f}")
        s.settle(0.1)

    def settings_value(self, label):
        return next(r["value"] for r in self.s.state()["settings"]["rows"] if r["label"] == label)

    def menu_choose(self, key):
        """Opens the menu if it's closed, and taps a compartment."""
        s = self.s
        if not s.state()["menu"]["open"]:
            s.key("u")
            s.wait_state(lambda v: v["menu"]["open"], what="the menu open")
        x, y = self.menu_spot(key)
        s.cmd(f"#tap {x:.0f} {y:.0f}")

    # ---- Cases ----

    def boot(self):
        s = self.s
        st = s.state()
        self.eq(st["mode"], "timer", "opens on the Timer")
        self.eq(st["theme"], "mono", "profile")
        self.eq(st["time"], "00:00", "time")
        self.eq(st["status"], "tap to track", "status at rest")
        self.eq(st["menu"]["open"], False, "the menu is closed")
        self.eq(st["link"]["connected"], False, "no Mac yet")
        s.shot("timer-rest-no-mac")
        s.cmd("#mode zoo")
        s.settle()
        st = s.state()
        self.eq(len(st["agents"]), 0, "no desktop app: no agents")
        s.shot("zoo-empty")
        s.cmd("#mode timer")

    def timer(self):
        s = self.s
        self.connect_mac()
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.settle()
        st = s.state()
        self.eq(st["project"]["name"], "Deep work", "project")
        s.shot("timer-rest")
        s.key("s")
        st = s.wait_state(lambda v: v["timer"]["state"] == "running", what="the Timer running")
        self.eq(st["status"], "", "while a clock runs, its status line stays empty")
        m = s.wait_link(lambda m: m.get("t") == "toggl" and m.get("op") == "start", what="a start message")
        self.eq(m["desc"], "Write the Q4 plan", "start: desc")
        self.eq(m["project"], 123, "start: project")
        self.check(abs(m["at"] - 1793826000) < 5, f"start: at {m['at']}")
        rid = m["rid"]
        s.mac({"t": "ack", "id": m["id"]})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="the ack to clear it")
        sleep(1.1)
        st = s.state()
        self.check(st["timer"]["elapsed_s"] >= 1, "it counts")
        s.shot("timer-running")
        s.key("s")
        st = s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        self.eq(st["status"], "paused", "paused status")
        m = s.wait_link(lambda m: m.get("op") == "stop", what="a stop message")
        self.eq(m["rid"], rid, "stop: rid")
        s.mac({"t": "ack", "id": m["id"]})
        elapsed = st["timer"]["elapsed_s"]
        sleep(0.5)
        self.eq(s.state()["timer"]["elapsed_s"], elapsed, "the total stays put while paused")
        s.shot("timer-paused")
        # Hours use the smaller digits.
        s.cmd("#session 3725")
        s.settle()
        st = s.state()
        self.eq(st["time"], "01:02:05", "hours")
        s.shot("timer-hours")
        # Hold: reset, and clear the task and the project.
        s.key("r")
        st = s.wait_state(lambda v: v["timer"]["state"] == "idle", what="reset")
        self.eq(st["task"], "", "a hold clears the task")
        self.eq(st["project"]["id"], 0, "a hold clears the project")
        self.eq(st["hint"], "reset", "the hint")
        # Another hold changes nothing: only a bump.
        s.clear_logs()
        s.key("r")
        s.wait_log("feedback bump")
        # Knob while running: minutes; back no further than the stretch's start. One message when it rests.
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        m = s.wait_link(lambda m: m.get("op") == "start", what="a start")
        s.mac({"t": "ack", "id": m["id"]})
        s.cmd("#turn 3")
        st = s.state()
        self.check(st["timer"]["elapsed_s"] >= 180, "three clicks add three minutes")
        m = s.wait_link(lambda m: m.get("op") == "shift", timeout=2, what="one shift")
        s.mac({"t": "ack", "id": m["id"]})
        self.check(1793826000 - 200 <= m["start"] <= 1793826000 - 170, f"shift start {m['start']}")
        self.check(not any(x.get("op") == "shift" for x in s.links), "a spin is one message")
        s.cmd("#turn -2")
        self.check(s.state()["timer"]["elapsed_s"] >= 60, "two clicks back")
        s.cmd("#turn -2")
        self.check(s.state()["timer"]["elapsed_s"] < 65, "never past the stretch's start")
        s.key("s")
        s.clear_links()
        # Swipe left or right does nothing here, least of all the color.
        for ch in "<>":
            s.key(ch)
        s.settle()
        self.eq(s.state()["theme"], "mono", "swiping never changes the profile")

    def sync(self):
        s = self.s
        self.connect_mac()
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="a sync message")
        self.eq(m["rid"], 0, "not running: rid 0")
        st = s.state()
        self.eq(st["status"], "syncing…", "the status while it asks")
        s.mac({"t": "sync_result", "req": m["req"], "result": "in_sync"})
        st = s.wait_state(lambda v: v["hint"] == "in sync", what="in sync")
        # A timer started elsewhere: adopted.
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="another sync")
        s.mac({"t": "sync_result", "req": m["req"], "result": "adopted", "rid": 813, "desc": "From my phone", "task_id": 0, "project": PROJECTS[0], "start": 1793826000 - 300})
        st = s.wait_state(lambda v: v["timer"]["state"] == "running", what="adopted")
        self.eq(st["task"], "From my phone", "adopted task")
        self.eq(st["project"]["id"], 101, "adopted project")
        self.check(295 <= st["timer"]["elapsed_s"] <= 310, f"adopted time {st['timer']['elapsed_s']}")
        self.eq(st["timer"]["rid"], 813, "the app's rid is used for that run")
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="sync with a running timer")
        self.eq(m["rid"], 813, "its rid")
        s.mac({"t": "sync_result", "req": m["req"], "result": "stopped"})
        st = s.wait_state(lambda v: v["timer"]["state"] == "paused", what="stopped elsewhere")
        self.eq(st["hint"], "stopped in Toggl", "hint")
        # The board's own count moved past the app's number.
        s.clear_links()
        s.key("s")
        m = s.wait_link(lambda m: m.get("op") == "start", what="a start after adopting")
        self.check(m["rid"] > 813, f"the next run's rid moved past 813: {m['rid']}")
        s.mac({"t": "ack", "id": m["id"]})
        highest = m["rid"]
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        s.clear_links()
        # A lower number from the app never moves it back, and the project can be null.
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="a sync")
        s.mac({"t": "sync_result", "req": m["req"], "result": "adopted", "rid": 5, "desc": "No project on this one", "task_id": 0, "project": None, "start": 1793826000 - 60})
        st = s.wait_state(lambda v: v["timer"]["state"] == "running", what="adopted with no project")
        self.eq(st["project"]["id"], 0, "a null project is no project")
        self.eq(st["timer"]["rid"], 5, "the app's rid again")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        s.clear_links()
        s.key("s")
        m = s.wait_link(lambda m: m.get("op") == "start", what="a start")
        self.check(m["rid"] > highest, f"a lower number doesn't move it back: {m['rid']} after {highest}")
        s.mac({"t": "ack", "id": m["id"]})
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        s.clear_links()
        # An adopted result with no usable rid still runs, on the board's own number.
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="a sync")
        s.mac({"t": "sync_result", "req": m["req"], "result": "adopted", "desc": "No rid", "task_id": 0, "project": None, "start": 1793826000 - 60})
        st = s.wait_state(lambda v: v["task"] == "No rid", what="adopted with no rid")
        self.check(st["timer"]["rid"] > highest, f"its own number: {st['timer']['rid']}")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        s.clear_links()
        # It lasts: past a restart, and even if the answer came too late for the UX to use it.
        s.key("p")
        m = s.wait_link(lambda m: m.get("t") == "sync", what="a sync")
        s.cmd("#mode pomodoro")
        s.mac({"t": "sync_result", "req": m["req"], "result": "adopted", "rid": 5000, "desc": "Too late", "task_id": 0, "project": None, "start": 1793826000})
        s.settle()
        self.check(s.state()["task"] != "Too late", "an answer after switching modes isn't applied")
        s.cmd("#mode timer")
        s.reboot()
        self.connect_mac()
        s.key("s")
        m = s.wait_link(lambda m: m.get("op") == "start", what="a start after a restart")
        self.check(m["rid"] > 5000, f"the count survives a restart: {m['rid']}")
        s.mac({"t": "ack", "id": m["id"]})
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        s.clear_links()
        # Pomodoro: tracking only.
        s.cmd("#mode pomodoro")
        s.key("p")
        st = s.wait_state(lambda v: v["hint"] == "tracking only", what="tracking only")

    def clocks(self):
        """The clocks kept in step with other boards through the Mac: what this board sends after a change, what it shows
        from the Mac, and the Toggl runs it names with `of`."""
        s = self.s
        other = "5be0e1d2"
        mac_now = 1793826000000

        def mac_clock(rev, at_ago_ms, timer, pomo=None, frm=other, seq=1, answer=None, desc="From the knob"):
            msg = {"t": "clock", "rev": rev, "from": frm, "seq": seq, "now": mac_now, "at": mac_now - at_ago_ms,
                   "task": {"desc": desc, "task_id": 0, "project": PROJECTS[1]}, "timer": timer,
                   "pomo": pomo or {"st": "idle", "set": 25, "total": 1500000, "left": 1500000, "rid": 0, "of": frm}}
            if answer is not None:
                msg["for"] = answer
            s.mac(msg)

        self.connect_mac()
        uid = self.hello()["uid"]
        first = s.wait_link(lambda m: m.get("t") == "clock", what="a clock after each hello")
        self.check(isinstance(first["now"], int) and isinstance(first["seq"], int), "now and seq")
        # A start here goes to the Mac, changed, with this board's own run.
        s.clear_links()
        s.key("s")
        start = s.wait_link(lambda m: m.get("op") == "start", what="the start")
        mine = s.wait_link(lambda m: m.get("t") == "clock", what="a clock after the start")
        self.eq((mine["changed"], mine["timer"]["st"], mine["timer"]["rid"]), (True, "running", start["rid"]), "what's sent")
        self.check("of" not in mine["timer"], "its own run has no of")
        self.check(mine["at"] <= mine["now"] and mine["timer"]["start"] <= mine["now"], "times by its own clock, up to now")
        self.check(s.state()["sync"]["pending"], "waiting for the Mac's answer")
        # The answer: its own change, taken. Nothing moves.
        before = s.state()["timer"]["elapsed_s"]
        mac_clock(1, 0, {"st": "running", "acc": 0, "start": mac_now - 1000, "rid": start["rid"], "of": uid}, frm=uid,
                  seq=mine["seq"], answer=mine["seq"])
        st = s.wait_state(lambda v: not v["sync"]["pending"], what="the answer taken")
        self.eq((st["timer"]["state"], st["timer"]["rid"], st["timer"]["of"]), ("running", start["rid"], 0), "still its own run")
        self.check(st["timer"]["elapsed_s"] >= before, "the count went on")
        # Another board's newer change: its run shows here, and this board's own run stops.
        s.clear_links()
        mac_clock(2, 1000, {"st": "running", "acc": 0, "start": mac_now - 754000, "rid": 77, "of": other})
        stop = s.wait_link(lambda m: m.get("op") == "stop", what="its own run stopping")
        self.eq(stop["rid"], start["rid"], "the run it started")
        self.check("of" not in stop, "its own: no of")
        st = s.wait_state(lambda v: v["timer"]["rid"] == 77, what="the other board's run")
        self.eq((st["timer"]["state"], st["timer"]["of"], st["task"]), ("running", int(other, 16), "From the knob"), "shown")
        self.check(752 <= st["timer"]["elapsed_s"] <= 758, f"its time, counted from the Mac's now: {st['timer']['elapsed_s']}")
        self.eq(st["project"]["id"], PROJECTS[1]["id"], "its project")
        s.settle(0.4)
        self.check(not any(m.get("t") == "clock" for m in list(s.links)), "a change from the Mac isn't sent back")
        # A tap here stops the other board's run, naming it, and says so.
        s.clear_links()
        s.key("s")
        stop = s.wait_link(lambda m: m.get("op") == "stop", what="the other board's run stopping")
        self.eq((stop["rid"], stop.get("of")), (77, other), "named with of")
        paused = s.wait_link(lambda m: m.get("t") == "clock", what="a clock after the pause")
        self.eq((paused["changed"], paused["timer"]["st"], paused["timer"]["of"]), (True, "paused", other), "paused, still its run")
        # While that change is on its way, an older one from the Mac doesn't win, but the answer to it does.
        mac_clock(3, 60000, {"st": "running", "acc": 0, "start": mac_now - 900000, "rid": 77, "of": other})
        s.settle(0.3)
        self.eq(s.state()["timer"]["state"], "paused", "an older change doesn't win over the one on its way")
        mac_clock(4, 0, {"st": "running", "acc": 0, "start": mac_now - 5000, "rid": 78, "of": other}, seq=9,
                  answer=paused["seq"])
        st = s.wait_state(lambda v: v["timer"]["rid"] == 78, what="the Mac's answer, which beat it")
        self.eq((st["timer"]["state"], st["sync"]["pending"]), ("running", False), "settled")
        # A clock older than one already shown in this connection is dropped.
        mac_clock(3, 0, {"st": "idle", "acc": 0, "rid": 0, "of": other})
        s.settle(0.3)
        self.eq(s.state()["timer"]["rid"], 78, "a lower rev changes nothing")
        # A Pomodoro that ran out while the board was away: done, with no alarm, and its run stopped.
        s.clear_links()
        mac_clock(5, 120000, {"st": "idle", "acc": 0, "rid": 78, "of": other},
                  pomo={"st": "running", "set": 25, "total": 1500000, "left": 60000, "rid": 90, "of": other})
        stop = s.wait_link(lambda m: m.get("op") == "stop" and m.get("rid") == 90, what="the Pomodoro's run stopping")
        self.eq(stop.get("of"), other, "named")
        st = s.wait_state(lambda v: v["pomo"]["state"] == "done", what="done")
        self.check(not st["alarm"], "no alarm for a Pomodoro that ended while away")
        self.eq(st["timer"]["state"], "idle", "the Timer as the Mac has it")
        # Times that can't be: a `now` no Mac could send is dropped, and a start far in the past counts as 400 days, so
        # nothing overflows (the sanitizers would stop the simulator).
        s.mac({"t": "clock", "rev": 6, "from": other, "seq": 1, "now": 9000000000000000, "at": -9000000000000000,
               "task": {"desc": "x"}, "timer": {"st": "running", "start": -9000000000000000, "acc": 9e15, "rid": 79, "of": other},
               "pomo": {"st": "idle", "set": 25, "total": 1500000, "left": 1500000}})
        s.settle(0.3)
        self.eq(s.state()["timer"]["rid"], 78, "an impossible now: dropped")
        mac_clock(6, 0, {"st": "running", "acc": 9e15, "start": -9000000000000000, "rid": 79, "of": other})
        st = s.wait_state(lambda v: v["timer"]["rid"] == 79, what="a start from long ago")
        self.check(st["timer"]["elapsed_s"] <= 2 * 400 * 24 * 3600, f"bounded: {st['timer']['elapsed_s']} s")
        self.check(s.alive(), "alive")
        mac_clock(7, 0, {"st": "idle", "acc": 0, "rid": 0, "of": other})
        s.wait_state(lambda v: v["timer"]["state"] == "idle", what="idle again")
        self.hello()  # what this case didn't acknowledge comes again after a hello
        self.drain()

    def dayclock(self):
        """The Clock's words and the menu's short date, as STATE reports them from the same functions the screens draw
        with: 12-hour time with no leading zero, "7th November 2026", the day, "Sat 7th Nov". Checked against Python's
        own calendar, through ordinals, noon and midnight, a leap day, and offsets either side of UTC."""
        import datetime
        s = self.s
        self.connect_mac()

        def words(t, utc):
            local = datetime.datetime.fromtimestamp(t + utc, datetime.timezone.utc)
            suffix = "th" if 11 <= local.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(local.day % 10, "th")
            return {"time": f"{local.hour % 12 or 12}:{local.minute:02d}", "ampm": "AM" if local.hour < 12 else "PM",
                    "date": f"{local.day}{suffix} {local.strftime('%B')} {local.year}", "day": local.strftime("%A"),
                    "short": f"{local.strftime('%a')} {local.day}{suffix} {local.strftime('%b')}"}

        cases = [(1793826000, -28800),           # the tests' usual time
                 (1709208000, 0),                # 29th February 2024, noon
                 (1735689600, 0),                # 1st January 2025, midnight
                 (1767243540, -18000),           # 11:59 PM on 31st December, New York
                 (1700100000, 19800),            # India, half an hour off
                 (1704112200 + 11 * 86400, 0),   # the 12th: "th", not "nd"
                 (1704112200 + 21 * 86400, 0),   # the 22nd
                 (1704112200 + 22 * 86400, 0)]   # the 23rd
        for t, utc in cases:
            s.mac({"t": "state", "v": 4, "seq": 2, "now": t, "utc": utc, "toggl": "ok", "voice": True, "n": len(SAMPLE_AGENTS)})
            st = s.wait_state(lambda v: any(v["daytime"] == words(t + k, utc) for k in range(0, 3)), what=f"the words for {t}, {utc}")
            self.check(st["daytime"]["time"][0] != "0", f"no leading zero: {st['daytime']['time']}")
        s.cmd("#mode clock")
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"], what="the menu, with the time in its middle")
        s.settle(0.3)
        s.shot("menu-daytime")
        s.key("p")
        self.mac_state()
        s.cmd("#mode timer")

    def clock_late(self):
        """Quick changes here, then the Mac's answers to the earlier ones arriving late, as over Bluetooth while the knob
        turns: an answer that this board's own later change has overtaken never shows, though the Mac's `now` (stamped
        before the trip) makes it look newer. The Pomodoro's digits flickered back and forth on a quick turn."""
        s = self.s
        self.connect_mac()
        uid = self.hello()["uid"]
        s.wait_link(lambda m: m.get("t") == "clock", what="the clock after hello")
        s.cmd("#mode pomodoro")
        s.cmd("#set 25")
        s.settle(0.4)
        s.clear_links()
        knob = s.state()["has"]["knob"]
        for _ in range(5 if knob else 3):
            s.key("+" if knob else ">")
            sleep(0.05)
        sent = []
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                m = s.wait_link(lambda m: m.get("t") == "clock" and m.get("changed"), timeout=0.3)
            except TimeoutError:
                if len(sent) >= 2:
                    break
                continue
            sent.append((m, int(time.time() * 1000)))  # when the Mac got it, by its own clock
        self.check(len(sent) >= 2, f"several changes went out: {len(sent)}")
        want = s.state()["pomo"]["set_min"]
        for rev, (m, got_ms) in enumerate(sent, start=100):
            sleep(0.2)  # the answer's trip: it arrives well after the Mac stamped it
            ago = int(m["now"]) - int(m["at"])
            pomo = dict(m["pomo"], of=m["pomo"].get("of") or uid)
            timer = dict(m["timer"], of=m["timer"].get("of") or uid)
            if "start" in timer:
                timer["start"] = got_ms - (int(m["now"]) - int(timer["start"]))
            s.mac({"t": "clock", "rev": rev, "from": uid, "seq": m["seq"], "for": m["seq"], "now": got_ms, "at": got_ms - ago,
                   "task": m["task"], "timer": timer, "pomo": pomo})
            s.settle(0.1)
            shown = s.state()["pomo"]["set_min"]
            self.check(shown == want, f"after the late answer to change {m['seq']}: shows {shown} minutes, not {want}")
        self.check(not s.state()["sync"]["pending"], "the answer to the last change settles it")
        s.cmd("#set 25")

    def clock_unsent(self):
        """The Mac's answer to this board's last change arriving after a newer change was made here but before it went
        out: the newer one must still go as a change. It went as a mere report, the Mac answered with its latest, the
        older one, and the knob's number jumped back as it stopped turning."""
        s = self.s
        self.connect_mac()
        uid = self.hello()["uid"]
        s.wait_link(lambda m: m.get("t") == "clock", what="the clock after hello")
        s.cmd("#mode pomodoro")
        s.cmd("#set 25")
        s.settle(0.4)
        s.clear_links()
        step = "+" if s.state()["has"]["knob"] else ">"
        s.key(step)
        a = s.wait_link(lambda m: m.get("t") == "clock" and m.get("changed"), what="the first change")
        now_ms = int(time.time() * 1000)

        def answer(m, latest):  # as the Mac answers m: with the latest it has, which is latest
            ago = int(latest["now"]) - int(latest["at"])
            return {"t": "clock", "rev": answer.rev, "from": uid, "seq": latest["seq"], "for": m["seq"], "now": now_ms,
                    "at": now_ms - ago, "task": latest["task"], "timer": dict(latest["timer"], of=uid),
                    "pomo": dict(latest["pomo"], of=uid)}
        answer.rev = 200
        # The next step, and the answer to the first change, in the same frame: the step first.
        s.send(step + "LINK " + json.dumps(answer(a, a)))
        b = s.wait_link(lambda m: m.get("t") == "clock", what="the second change")
        self.check(b.get("changed") is True, f"the newer change goes as a change: {b.get('changed')}")
        want = s.state()["pomo"]["set_min"]
        answer.rev += 1
        s.mac(answer(b, b if b.get("changed") else a))  # a change is taken; a mere report gets the Mac's latest
        s.settle(0.2)
        shown = s.state()["pomo"]["set_min"]
        self.check(shown == want, f"after the Mac's answer it still shows {want} minutes: {shown}")
        s.cmd("#set 25")

    def clock_edges(self):
        """More of the clocks kept in step: a new connection's count, another board's Pomodoro, this board's own run
        replaced, a message with no time while a change is on its way, a time's-up dismissed elsewhere, and a knob turn
        overtaken by another board's change."""
        s = self.s
        other = "5be0e1d2"
        mac_now = 1793826000000
        idle_pomo = {"st": "idle", "set": 25, "total": 1500000, "left": 1500000, "rid": 0, "of": other}

        def clock(rev, timer=None, pomo=None, at_ago=0, frm=other, seq=1, answer=None, no_at=False):
            msg = {"t": "clock", "rev": rev, "from": frm, "seq": seq, "now": mac_now,
                   "task": {"desc": "Elsewhere", "task_id": 0, "project": PROJECTS[1]},
                   "timer": timer or {"st": "idle", "acc": 0, "rid": 0, "of": frm}, "pomo": pomo or idle_pomo}
            if not no_at:
                msg["at"] = mac_now - at_ago
            if answer is not None:
                msg["for"] = answer
            s.mac(msg)

        self.connect_mac()
        self.hello()
        # A new connection starts the Mac's count again: after a hello, a low rev is taken.
        clock(9, {"st": "running", "acc": 0, "start": mac_now - 60000, "rid": 40, "of": other})
        s.wait_state(lambda v: v["timer"]["rid"] == 40, what="rev 9")
        self.hello()
        clock(1, {"st": "paused", "acc": 30000, "rid": 40, "of": other})
        st = s.wait_state(lambda v: v["timer"]["state"] == "paused", what="rev 1, after a hello")
        self.eq(st["timer"]["elapsed_s"], 30, "its count")
        # Another board's Pomodoro: running, then paused.
        clock(2, pomo={"st": "running", "set": 25, "total": 1500000, "left": 600000, "rid": 41, "of": other})
        st = s.wait_state(lambda v: v["pomo"]["state"] == "running", what="a running Pomodoro from elsewhere")
        self.check(595 <= st["pomo"]["remaining_s"] <= 600, f"its time left: {st['pomo']['remaining_s']}")
        self.eq((st["pomo"]["rid"], st["pomo"]["of"]), (41, int(other, 16)), "its run")
        clock(3, pomo={"st": "paused", "set": 25, "total": 1500000, "left": 300000, "rid": 41, "of": other})
        st = s.wait_state(lambda v: v["pomo"]["state"] == "paused", what="paused elsewhere")
        self.eq(st["pomo"]["remaining_s"], 300, "what's left")
        # Started here, it's this board's own run; replaced by another board's Timer, that run stops here.
        s.cmd("#mode pomodoro")
        s.clear_links()
        s.key("s")
        start = s.wait_link(lambda m: m.get("op") == "start", what="this board's own run")
        s.wait_link(lambda m: m.get("t") == "clock", what="the change")
        clock(5, {"st": "running", "acc": 0, "start": mac_now - 1000, "rid": 42, "of": other})
        stop = s.wait_link(lambda m: m.get("op") == "stop" and m.get("rid") == start["rid"], what="its own run stopping")
        self.check("of" not in stop, "its own: no of")
        st = s.wait_state(lambda v: v["timer"]["rid"] == 42 and v["pomo"]["state"] == "idle", what="the other board's Timer")
        # A change here is on its way; a message with no time can't be newer: it's kept out.
        s.cmd("#mode timer")
        s.clear_links()
        s.clear_logs()
        s.key("s")
        mine = s.wait_link(lambda m: m.get("t") == "clock", what="the pause, on its way")
        clock(6, {"st": "running", "acc": 0, "start": mac_now - 5000, "rid": 43, "of": other}, no_at=True)
        s.wait_log("clock kept")
        self.eq(s.state()["timer"]["state"], "paused", "the change made here stays")
        clock(7, {"st": "paused", "acc": 1000, "rid": 42, "of": other}, answer=mine["seq"])
        s.wait_state(lambda v: not v["sync"]["pending"], what="settled")
        # A time's-up dismissed on another board stops the alarm here.
        clock(8, pomo={"st": "running", "set": 25, "total": 1500000, "left": 1500, "rid": 44, "of": other})
        st = s.wait_state(lambda v: v["pomo"]["state"] == "done" and v["alarm"], timeout=6, what="time's up here too")
        clock(9, pomo=idle_pomo)
        st = s.wait_state(lambda v: not v["alarm"], what="dismissed elsewhere")
        self.eq(st["pomo"]["state"], "idle", "idle, as the Mac has it")
        # A knob turn here, overtaken by another board's change before it went out: no shift is sent for it.
        clock(10, {"st": "running", "acc": 0, "start": mac_now - 120000, "rid": 45, "of": other})
        s.wait_state(lambda v: v["timer"]["rid"] == 45, what="another board's running Timer")
        s.clear_links()
        s.key("+")
        clock(11, {"st": "running", "acc": 0, "start": mac_now - 2000, "rid": 46, "of": other})
        s.settle(0.6)
        self.check(not any(m.get("op") == "shift" for m in list(s.links)), "the overtaken turn isn't sent")
        self.eq(s.state()["timer"]["rid"], 46, "the newer change")
        clock(12, {"st": "idle", "acc": 0, "rid": 0, "of": other})
        s.wait_state(lambda v: v["timer"]["state"] == "idle", what="idle")
        self.hello()
        self.drain()

    def split_more(self):
        """Auto switch from the Pomodoro too; any input starts the wait again; never over the menu, a list or a sync on
        its way; and Split view's settings survive a restart."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        for _ in range(4):
            self.settings_row("Switch after")
        self.eq(self.settings_value("Switch after"), "5 s", "a wait of 5 s")
        show = s.state()["split"]["show"]
        to = "clock" if show == "clock" else "zoo"
        # From the Pomodoro.
        s.cmd("#mode pomodoro")
        s.key("s")
        t0 = time.time()
        s.wait_state(lambda v: v["mode"] == to, timeout=8, what="Split view, from the Pomodoro")
        self.check(time.time() - t0 > 4.5, f"after the wait: {time.time() - t0:.1f} s")
        # A turn of the knob, even one that only bumps, starts the wait again.
        s.cmd("#mode pomodoro")
        s.settle(3)
        s.key("+")
        s.settle(3)
        self.eq(s.state()["mode"], "pomodoro", "the turn started the wait again")
        s.wait_state(lambda v: v["mode"] == to, timeout=4, what="Split view, once the wait is over")
        # Never over the menu or a list.
        for opener, what in (("u", "the menu"), ("o", "a list")):
            s.cmd("#mode pomodoro")
            s.key(opener)
            s.settle(6)
            self.eq(s.state()["mode"], "pomodoro", f"not over {what}")
            s.key("p")
        # Nor during a swipe-down check of Toggl, on the Timer's screen.
        s.cmd("#mode timer")
        s.clear_links()
        s.key("p")
        sync = s.wait_link(lambda m: m.get("t") == "sync", what="a check on its way")
        s.settle(6)
        self.eq(s.state()["mode"], "timer", "not during a check")
        s.mac({"t": "sync_result", "req": sync["req"], "result": "in_sync"})
        s.wait_state(lambda v: not v["syncing"], what="the check answered")
        s.cmd("#mode pomodoro")
        s.key("s")  # pause it
        s.wait_state(lambda v: v["pomo"]["state"] == "paused", what="paused")
        # The settings survive a restart: Clock, off, 30 s.
        self.menu_choose("clock")
        s.wait_state(lambda v: v["mode"] == "clock" and not v["menu"]["open"], what="the Clock")
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        for _ in range(3):
            self.settings_row("Switch after")
        self.settings_row("Auto switch")
        want = {"show": "clock", "auto": False, "after_s": 30}
        self.eq(s.state()["split"], want, "set")
        s.settle(2.5)  # settings are saved 2 s after a change
        s.reboot()
        self.eq(s.state()["split"], want, "the same after a restart")
        # As it was: Auto switch on, 15 s, Agents.
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        self.settings_row("Auto switch")
        for _ in range(5):
            self.settings_row("Switch after")
        self.menu_choose("zoo")
        st = s.wait_state(lambda v: v["mode"] == "zoo" and not v["menu"]["open"], what="Agents")
        self.eq(st["split"], {"show": "agents", "auto": True, "after_s": 15}, "as it was")
        s.cmd("#mode timer")
        self.connect_mac()
        self.drain()

    def pomodoro(self):
        s = self.s
        self.connect_mac()
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.cmd("#mode pomodoro")
        s.settle()
        st = s.state()
        self.eq(st["time"], "25:00", "the set length")
        self.eq(st["status"], "tap to start", "status")
        s.shot("pomodoro-rest")
        s.cmd("#turn 1")
        self.eq(s.state()["pomo"]["set_min"], 26, "knob sets the length")
        s.cmd("#turn -1")
        s.cmd("#set 1")
        s.clear_logs()
        s.cmd("#turn -1")
        s.wait_log("feedback bump")
        self.eq(s.state()["pomo"]["set_min"], 1, "no less than 1 minute")
        s.cmd("#set 60")
        s.settle()
        self.eq(s.state()["time"], "60:00", "60 minutes as 60:00")
        s.shot("pomodoro-60")
        s.cmd("#set 25")
        s.key("s")
        st = s.wait_state(lambda v: v["pomo"]["state"] == "running", what="running")
        s.wait_link(lambda m: m.get("op") == "start", what="a start")
        self.eq(st["status"], "", "status stays empty")
        s.cmd("#turn 1")
        st = s.wait_state(lambda v: v["hint"] == "pause to adjust", what="pause to adjust")
        sleep(1.1)
        self.check(s.state()["pomo"]["remaining_s"] < 1500, "it counts down")
        s.shot("pomodoro-running")
        s.key("s")
        st = s.wait_state(lambda v: v["pomo"]["state"] == "paused", what="paused")
        s.shot("pomodoro-paused")
        rem = st["pomo"]["remaining_s"]
        s.cmd("#turn -1")
        self.eq(s.state()["pomo"]["remaining_s"], 1440, "paused: the whole minute below")
        s.cmd("#remaining 1498")
        s.cmd("#turn 1")
        self.eq(s.state()["pomo"]["remaining_s"], 1500, "paused: the whole minute above")
        self.eq(rem < 1500, True, "it had moved")
        # The warm-up: the color of the time, at each point where it changes. Running, because paused it's drawn at 55%.
        s.cmd("#remaining 600")
        s.key("s")
        s.wait_state(lambda v: v["pomo"]["state"] == "running", what="running")
        s.cmd("#pulse 0")
        def time_color(f):
            # The commonest color in the time's row band: its digits' solid parts.
            y0, y1 = (165, 205) if self.round else (215, 280)
            counts = {}
            for y in range(y0, y1, 2):
                for x in range(40, self.base - 40, 2):
                    c = f.rgb(x, y)
                    if c != (0, 0, 0):
                        counts[c] = counts.get(c, 0) + 1
            return max(counts, key=counts.get)
        probes = {}
        for label, secs in (("10-00", 600), ("07-30", 450), ("05-00", 300), ("03-00", 180), ("01-00", 60)):
            s.cmd(f"#remaining {secs}")
            s.settle()
            probes[label] = time_color(s.shot(f"pomodoro-warm-{label}"))
        self.check(min(probes["10-00"]) > 235, f"near white at 10:00: {probes['10-00']}")
        self.check(abs(probes["05-00"][0] - 0xFF) < 10 and abs(probes["05-00"][1] - 0x4D) < 12 and abs(probes["05-00"][2] - 0x06) < 12, f"orange at 5:00: {probes['05-00']}")
        self.check(abs(probes["01-00"][0] - 0xE8) < 12 and abs(probes["01-00"][1] - 0x14) < 12 and abs(probes["01-00"][2] - 0x3A) < 12, f"crimson at 1:00: {probes['01-00']}")
        # A fade never sweeps through purple: red stays well above blue at every step, and orange stays between.
        for secs in range(570, 60, -30):
            s.cmd(f"#remaining {secs}")
            s.settle(0.05)
            r, g, b = time_color(s.frame())
            self.check(r >= b + 8 or min(r, g, b) > 200, f"no purple at {secs} s: {(r, g, b)}")
        # The last minute's pulse, held at its peak.
        s.cmd("#remaining 40")
        s.cmd("#pulse off")
        st = s.state()
        self.eq(st["pulsing"], True, "pulses in the last minute")
        s.cmd("#pulse 1")
        s.settle(0.1)
        s.shot("pomodoro-pulse-peak")
        s.cmd("#pulse 0.5")
        s.settle(0.1)
        s.shot("pomodoro-pulse-half")
        s.cmd("#pulse off")
        s.key("s")
        s.wait_state(lambda v: v["pomo"]["state"] == "paused", what="paused")
        s.clear_links()
        # Time's up: the alarm, back on its face wherever you are; a tap resets.
        s.cmd("#remaining 2")
        s.key("s")
        s.cmd("#mode zoo")
        s.clear_logs()
        st = s.wait_state(lambda v: v["pomo"]["state"] == "done", timeout=5, what="time's up")
        self.eq(st["mode"], "pomodoro", "back to its face")
        self.eq(st["alarm"], True, "the alarm")
        self.eq(st["status"], "time’s up", "status")
        s.wait_log("feedback alarm")
        s.cmd("#pulse 1")
        s.settle()
        s.shot("pomodoro-times-up")
        s.cmd("#pulse off")
        s.cmd("#turn 1")
        st = s.state()
        self.eq(st["alarm"], False, "a knob turn silences it")
        self.eq(st["pomo"]["state"], "done", "and it stays on time's up")
        s.key("s")
        st = s.wait_state(lambda v: v["pomo"]["state"] == "idle", what="the tap resets")
        self.eq(st["time"], "25:00", "back to the set length")

    def menu(self):
        s = self.s
        self.connect_mac()
        s.key("u")
        st = s.wait_state(lambda v: v["menu"]["open"], what="the menu")
        self.eq(st["screen"], "menu", "screen")
        s.settle(0.3)
        s.shot("menu")
        # It's touch only: a knob turn or a sideways swipe moves nothing, and picks nothing.
        s.cmd("#turn 1")
        s.key("<")
        st = s.state()
        self.eq((st["menu"]["open"], st["mode"]), (True, "timer"), "nothing moves")
        # Each compartment opens what it says.
        for key in ("pomodoro", "settings", "zoo", "clock", "timer"):
            self.menu_choose(key)
            st = s.wait_state(lambda v: not v["menu"]["open"], what=f"{key} chosen")
            self.eq(st["mode"], key, "a tap opens it")
        # A swipe down closes it, with the mode as it was.
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"], what="open")
        s.key("p")
        st = s.wait_state(lambda v: not v["menu"]["open"], what="closed by a swipe down")
        self.eq(st["mode"], "timer", "unchanged")
        # On a round screen, so does a tap on the clock in the middle.
        if self.round:
            s.key("u")
            s.wait_state(lambda v: v["menu"]["open"], what="open")
            s.cmd("#tap 180 180")
            st = s.wait_state(lambda v: not v["menu"]["open"], what="closed by a tap on the clock")
            self.eq(st["mode"], "timer", "unchanged")
        # With a clock running, the other clock is blocked.
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"], what="open")
        s.settle(0.25)
        s.shot("menu-running")
        self.menu_choose("pomodoro")
        st = s.wait_state(lambda v: v["toast"] == "Pause to switch", what="pause to switch")
        self.eq(st["menu"]["open"], True, "it stays open")
        self.eq(st["mode"], "timer", "it doesn't switch")
        s.shot("menu-blocked")
        s.key("p")
        s.key("s")
        s.clear_links()
        # Lock with no code says to set one.
        s.cmd("#mode timer")
        self.menu_choose("lock")
        st = s.wait_state(lambda v: v["toast"] == "Set a code on your Mac", what="set a code")
        self.eq(st["lock"]["on"], False, "no code: it doesn't lock")
        s.shot("menu-lock-no-code")
        s.key("p")
        # An agent that needs you shows on the Agents compartment.
        agents = [dict(a) for a in SAMPLE_AGENTS]
        agents[1]["st"] = "needs"
        self.mac_state(agents)
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"] and v["n_needing"] == 1, what="one needing")
        s.settle(0.25)
        s.shot("menu-needs-you")
        s.key("p")
        self.mac_state()

    def timeouts(self):
        """The menu and the details card close by themselves after 10 s with no input."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode zoo")
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"], what="the menu")
        t0 = time.time()
        s.wait_state(lambda v: not v["menu"]["open"], timeout=11.5, what="the menu closing")
        self.check(time.time() - t0 > 9, f"after 10 s: {time.time() - t0:.1f}")
        s.key("r")
        s.wait_state(lambda v: v["card"], what="the card")
        t0 = time.time()
        s.wait_state(lambda v: not v["card"], timeout=11.5, what="the card closing")
        self.check(time.time() - t0 > 9, f"after 10 s: {time.time() - t0:.1f}")
        s.cmd("#mode timer")

    def busy(self):
        """A Mac that may not use the board now: over the real TCP link, as the protocol says."""
        s = self.s
        s.cmd("#link ble")
        def connect_and_hello(host):
            c = socket.create_connection(("127.0.0.1", s.link_port), timeout=3)
            c.settimeout(3)
            c.sendall(json.dumps({"t": "hello", "v": 4, "host": host}).encode() + b"\n")
            return c
        def read_until_closed(c):
            got = b""
            try:
                while True:
                    d = c.recv(65536)
                    if not d:
                        return got, True
                    got += d
            except socket.timeout:
                return got, False
        tap_row = self.settings_row
        c = connect_and_hello("Busy Mac")
        got = c.recv(4096)
        self.check(b'"t":"board"' in got, "any Mac may connect at first")
        s.simcmd("mac Other Mac")
        s.cmd("#mode settings")
        s.settle(0.1)
        tap_row("Connectivity")
        s.wait_state(lambda v: v["settings"]["page"] == "connect", what="Connectivity")
        tap_row("Connected to")
        s.wait_state(lambda v: v["settings"]["page"] == "macs", what="the Mac list")
        s.settle(1.1) # (the list refreshes once a second)
        rows = s.state()["settings"]["rows"]
        self.check(any(r["label"] == "Busy Mac" and r["value"] == "connected" for r in rows), f"it's listed as connected: {rows}")
        # Choosing another Mac sends this one away.
        tap_row("Other Mac")
        got, closed = read_until_closed(c)
        self.check(b'"t":"busy"' in got and b"another Mac is chosen" in got, f"busy: {got[:100]}")
        self.check(closed, "and it disconnects")
        # It can't come back while another is chosen.
        c = connect_and_hello("Busy Mac")
        got, closed = read_until_closed(c)
        self.check(b"another Mac is chosen" in got and closed, f"refused on hello: {got[:100]}")
        self.check(b'"t":"board"' not in got, "with no board message")
        # Any Mac again.
        tap_row("Any Mac")
        s.wait_state(lambda v: v["settings"]["rows"][0]["value"] == "check", what="Any Mac chosen")
        c = connect_and_hello("Busy Mac")
        got = c.recv(4096)
        self.check(b'"t":"board"' in got, "back in")
        # Pair mode sends a connected Mac away, and refuses others until it ends.
        s.key("p")
        s.wait_state(lambda v: v["settings"]["page"] == "connect", what="Connectivity")
        s.settle(0.1)
        tap_row("Pair a new Mac")
        got, closed = read_until_closed(c)
        self.check(b'"reason": "pairing"' in got or b'"reason":"pairing"' in got, f"pairing: {got[:100]}")
        self.check(closed, "and it disconnects")
        c = connect_and_hello("Busy Mac")
        got, closed = read_until_closed(c)
        self.check(b"pairing" in got and closed, f"refused during pair mode: {got[:100]}")
        s.key("t")
        s.wait_state(lambda v: v["settings"]["page"] == "connect", what="back")
        self.eq(s.state()["link"]["pair_left"], 0, "pair mode ended")
        c = connect_and_hello("Busy Mac")
        got = c.recv(4096)
        self.check(b'"t":"board"' in got, "back in once it's over")
        c.close()
        s.settle(0.15)
        s.key("p")
        s.cmd("#mode timer")

    def settings(self):
        s = self.s
        self.connect_mac()
        self.menu_choose("settings")
        st = s.wait_state(lambda v: v["mode"] == "settings", what="Settings")
        self.eq(st["settings"]["from"], "timer", "where it came from")
        self.eq([r["label"] for r in st["settings"]["rows"]], ["Device", "Connectivity", "Agents", "Timer & Pomodoro"], "the groups")
        s.settle(0.3)
        s.shot("settings")
        # Device: the screen, the sound and facts about the board.
        self.settings_row("Device")
        st = s.wait_state(lambda v: v["settings"]["page"] == "device", what="Device")
        labels = [r["label"] for r in st["settings"]["rows"]]
        # The rows a board has are decided by what it has, as Settings decides them.
        want = (["Brightness"] + (["Sound"] if self.has["speaker"] else []) + ["Color"] + (["Auto-rotate"] if self.has["motion"] else [])
                + (["Battery"] if self.has["battery"] else []) + ["Firmware", "MAC address"])
        self.eq(labels, want, "Device's rows on this board")
        self.eq(self.settings_value("Firmware"), "0.2.0", "the firmware's version")
        mac = self.settings_value("MAC address")
        self.check(re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", mac), f"a MAC address: {mac}")
        s.settle(0.3)
        s.shot("settings-device")
        if not self.round and len(labels) > 5:
            # More than five rows: five show, and the rest scroll into view under the finger.
            s.key("u")
            st = s.wait_state(lambda v: v["settings"]["scroll"] > 100, what="the list scrolled")
            self.eq(st["settings"]["page"], "device", "a drag up scrolls rather than leaving")
            s.settle(0.3)
            s.shot("settings-device-scrolled")
            self.settings_row("MAC address")
            self.eq(s.state()["settings"]["page"], "device", "a fact only bumps")
            s.key("p")  # scrolled down, a pull down scrolls back up; only at the top does it go back
            st = s.wait_state(lambda v: v["settings"]["scroll"] == 0, what="scrolled back to the top")
            self.eq(st["settings"]["page"], "device", "still Device")
        self.settings_row("Brightness")
        st = s.wait_state(lambda v: v["settings"]["page"] == "bright", what="the brightness page")
        self.eq(st["bright"], 9, "it opens where it is")
        if self.round:
            s.cmd("#turn 1")
            self.eq(s.state()["bright"], 9, "it doesn't wrap round to the dimmest")
            s.cmd("#turn -9")
            s.settle()
            self.eq(s.state()["bright"], 0, "the knob sets the dimmest")
            s.shot("settings-brightness-low")
            s.cmd("#turn -1")
            self.eq(s.state()["bright"], 0, "it doesn't wrap round to the brightest")
            self.brightness_bar(4)
            self.eq(s.state()["bright"], 4, "a tap on a bar sets it")
            s.cmd("#bright 9")
        else:
            self.brightness_bar(0)
            self.eq(s.state()["bright"], 0, "a tap on a bar sets it")
            s.shot("settings-brightness-low")
            self.brightness_bar(9)
            self.eq(s.state()["bright"], 9, "back to the brightest")
        s.key("p")
        st = s.wait_state(lambda v: v["settings"]["page"] == "device", what="back to Device")
        self.eq(self.settings_value("Brightness"), "10", "the row says the level")
        self.settings_row("Color")
        s.settle(0.25)
        self.eq(s.state()["theme"], "inverted", "the color row")
        s.shot("settings-inverted")
        self.settings_row("Color")
        if self.has["speaker"]:
            self.settings_row("Sound")
            self.eq(s.state()["vol"], 8, "sound steps by two")
            s.shot("settings-sound")
            self.settings_row("Sound")
            self.settings_row("Sound")
            self.eq(s.state()["vol"], 0, "and off")
            self.eq(self.settings_value("Sound"), "Off", "Sound: Off")
            s.shot("settings-sound-off")
            for _ in range(3):
                self.settings_row("Sound")
            self.eq(s.state()["vol"], 6, "back to 6")
        if self.has["motion"]:
            self.settings_row("Auto-rotate")
            self.eq(s.state()["autorotate"], False, "auto-rotate off")
            self.settings_row("Auto-rotate")
        s.key("p")
        st = s.wait_state(lambda v: v["settings"]["page"] == "main", what="back to the groups")
        self.eq(st["settings"]["key"], 0, "on the row that opened it")
        # Agents.
        self.settings_row("Agents")
        st = s.wait_state(lambda v: v["settings"]["page"] == "agents", what="Agents")
        self.eq([r["label"] for r in st["settings"]["rows"]], ["Auto-scroll"], "Agents' rows")
        self.settings_row("Auto-scroll")
        self.eq(s.state()["autoscroll"], True, "auto-scroll on")
        self.settings_row("Auto-scroll")
        s.key("p")
        st = s.wait_state(lambda v: v["settings"]["page"] == "main", what="back")
        self.eq(st["settings"]["key"], 2, "on Agents")
        # Timer & Pomodoro: Split view, Auto switch, and its wait only while it's on.
        self.settings_row("Timer & Pomodoro")
        st = s.wait_state(lambda v: v["settings"]["page"] == "clocks", what="Timer & Pomodoro")
        was = st["split"]["show"]  # the last of Agents and Clock opened from the menu, in an earlier case
        other = "agents" if was == "clock" else "clock"
        self.eq([(r["label"], r["value"]) for r in st["settings"]["rows"]],
                [("Split view", was.capitalize()), ("Auto switch", "On"), ("Switch after", "15 s")], "its rows")
        s.settle(0.3)
        s.shot("settings-clocks")
        self.settings_row("Split view")
        self.eq(s.state()["split"]["show"], other, "Split view changes")
        self.settings_row("Auto switch")
        st = s.state()
        self.eq([r["label"] for r in st["settings"]["rows"]], ["Split view", "Auto switch"], "off: no wait to set")
        s.settle(0.3)
        s.shot("settings-clocks-off")
        self.settings_row("Auto switch")
        self.settings_row("Switch after")
        self.eq(self.settings_value("Switch after"), "30 s", "the next wait")
        for _ in range(5):
            self.settings_row("Switch after")
        self.eq(self.settings_value("Switch after"), "15 s", "round to 15 s again")
        self.settings_row("Split view")
        self.eq(s.state()["split"], {"show": was, "auto": True, "after_s": 15}, "as it was")
        s.key("p")
        s.wait_state(lambda v: v["settings"]["page"] == "main", what="back")
        if not self.round:
            # The buttons: Menu leaves, Up and Down set the volume.
            s.cmd("#button menu")
            st = s.wait_state(lambda v: v["mode"] == "timer", what="Menu closes it")
            s.cmd("#button up")
            st = s.state()
            self.eq(st["vol"], 7, "Up")
            self.eq(st["toast"], "Volume 7", "toast")
            s.settle()
            s.shot("toast-volume")
            for _ in range(7):
                s.cmd("#button down")
            st = s.state()
            self.eq(st["toast"], "Muted", "Muted")
            s.cmd("#vol 6")
            s.cmd("#button menu")
            s.wait_state(lambda v: v["mode"] == "settings", what="Menu opens it")
        # Swipe down leaves.
        s.key("p")
        st = s.wait_state(lambda v: v["mode"] == "timer", what="swipe down leaves")

    def split(self):
        """Split view: what shows below a running clock is the last of Agents and Clock opened from the menu, and Auto
        switch goes there by itself once the Timer or Pomodoro screen has been left alone for its wait. Off, never."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        self.menu_choose("clock")
        st = s.wait_state(lambda v: v["mode"] == "clock" and not v["menu"]["open"], what="the Clock")
        self.eq(st["split"]["show"], "clock", "opening the Clock sets it")
        s.settle(0.3)
        s.shot("clock-alone")
        self.menu_choose("zoo")
        st = s.wait_state(lambda v: v["mode"] == "zoo" and not v["menu"]["open"], what="Agents")
        self.eq(st["split"]["show"], "agents", "opening Agents sets it")
        # A wait of 5 s: Settings › Timer & Pomodoro › Switch after, from 15 s round to 5.
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        for _ in range(4):
            self.settings_row("Switch after")
        self.eq(self.settings_value("Switch after"), "5 s", "a wait of 5 s")
        s.cmd("#mode timer")
        s.key("s")
        t0 = time.time()
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.settle(3)
        self.eq(s.state()["mode"], "timer", "not before the wait")
        st = s.wait_state(lambda v: v["mode"] == "zoo", timeout=6, what="Split view, with Agents")
        self.check(time.time() - t0 > 4.5, f"after the wait: {time.time() - t0:.1f} s")
        self.eq(st["running"], "timer", "the Timer still runs, in the arc")
        # The Clock, picked while it runs, then the arc back to the Timer: it splits to the Clock this time.
        self.menu_choose("clock")
        s.wait_state(lambda v: v["mode"] == "clock" and not v["menu"]["open"], what="the Clock")
        s.settle(0.3)
        s.shot("clock-under-timer")
        s.cmd(f"#tap {self.base / 2:.0f} {self.base * .2:.0f}")
        s.wait_state(lambda v: v["mode"] == "timer", what="the arc opens the Timer")
        st = s.wait_state(lambda v: v["mode"] == "clock", timeout=7, what="Split view, with the Clock")
        # Off: the Timer stays.
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        self.settings_row("Auto switch")
        self.eq(s.state()["split"]["auto"], False, "Auto switch off")
        s.cmd("#mode timer")
        s.settle(6.5)
        self.eq(s.state()["mode"], "timer", "off: it never switches")
        # As it was.
        s.cmd("#mode settings")
        self.settings_row("Timer & Pomodoro")
        self.settings_row("Auto switch")
        for _ in range(2):
            self.settings_row("Switch after")
        self.settings_row("Split view")
        self.eq(s.state()["split"], {"show": "agents", "auto": True, "after_s": 15}, "as it was")
        s.cmd("#mode timer")
        s.key("s")
        self.drain()

    def macs(self):
        s = self.s
        self.connect_mac()
        s.simcmd("mac MacBook Pro") # (over #link usb no real Mac says hello to the Bluetooth side)
        s.simcmd("mac Mac mini")
        s.cmd("#mode settings")
        s.settle()
        self.settings_row("Connectivity")
        st = s.wait_state(lambda v: v["settings"]["page"] == "connect", what="Connectivity")
        self.eq([r["label"] for r in st["settings"]["rows"]], ["Connected to", "Pair a new Mac"], "Connectivity's rows")
        s.settle(0.3)
        s.shot("settings-connect")
        self.settings_row("Connected to")
        st = s.wait_state(lambda v: v["settings"]["page"] == "macs", what="the Mac list")
        rows = st["settings"]["rows"]
        self.eq(rows[0]["label"], "Any Mac", "Any Mac first")
        self.eq(rows[0]["value"], "check", "chosen")
        self.check(any(r["label"] == "MacBook Pro" for r in rows), f"the Mac that said hello: {rows}")
        self.check(any(r["label"] == "Mac mini" for r in rows), "a paired Mac")
        s.settle(0.3)
        s.shot("settings-macs")
        idx = [r["label"] for r in rows].index("Mac mini")
        if self.round:
            s.cmd(f"#turn {idx}")
            s.settle(0.25)
            s.key("t")
        else:
            s.cmd(f"#tap 240 {66 + idx * 76 + 38}")
        st = s.wait_state(lambda v: v["settings"]["rows"][idx]["value"] == "check", what="choosing a Mac")
        s.shot("settings-macs-chosen")
        # Hold to forget: it asks, then a tap does it.
        if self.round:
            s.key("r")
        else:
            s.cmd(f"#hold 240 {66 + idx * 76 + 38}")
        st = s.wait_state(lambda v: v["settings"]["rows"][idx]["label"].startswith("Tap again"), what="asks first")
        s.settle()
        s.shot("settings-macs-forget")
        if self.round:
            s.key("t")
        else:
            s.cmd(f"#tap 240 {66 + idx * 76 + 38}")
        st = s.wait_state(lambda v: not any(r["label"] == "Mac mini" for r in v["settings"]["rows"]), what="forgotten")
        self.eq(st["toast"], "Forgot Mac mini", "toast")
        s.key("p")
        st = s.wait_state(lambda v: v["settings"]["page"] == "connect", what="back to Connectivity")
        self.eq(st["settings"]["key"], 0, "on Connected to")
        # Pairing: the wait, then the code on screen only.
        self.settings_row("Pair a new Mac")
        st = s.wait_state(lambda v: v["settings"]["page"] == "pair", what="the pair page")
        self.check(st["link"]["pair_left"] > 100, "two minutes")
        s.settle()
        s.shot("settings-pair-waiting")
        s.simcmd("pair on")
        s.settle()
        st = s.state()
        self.eq(st["link"]["pairing"], True, "pairing")
        text = json.dumps(st)
        self.check("482913" not in text and "482 913" not in text, "STATE never holds the code")
        panel = os.path.join(s.out_dir, "settings-pair-code-panel.ppm")
        s.simcmd("panel " + panel)
        self.check(open(panel, "rb").read(9).startswith(b"P6"), "the panel dump")
        # The capture shows the page, with no real code in it: it must differ from what the panel shows, and only where
        # the code is. Both must really have digits: an empty box looks the same whatever it stands for.
        f = s.shot("settings-pair-code-capture-masked")
        import harness
        h = open(panel, "rb").read()
        offset = len(h) - f.w * f.h * 3
        panel_rgb = h[offset:]
        diff = sum(1 for i in range(0, len(panel_rgb), 3) if tuple(panel_rgb[i:i + 3]) != f.rgb((i // 3) % f.w, (i // 3) // f.w))
        self.check(diff > 100, f"a capture must not show the code ({diff} pixels differ)")
        y0, y1 = int(self.base * 0.52 - self.base * 0.13), int(self.base * 0.52 + 4)
        rows_differ = sum(1 for i in range(0, len(panel_rgb), 3) if not (y0 <= (i // 3) // f.w < y1) and tuple(panel_rgb[i:i + 3]) != f.rgb((i // 3) % f.w, (i // 3) // f.w))
        self.eq(rows_differ, 0, "only the code's own rows differ")
        harness.write_png(os.path.join(s.out_dir, "settings-pair-code.png"), f.w, f.h, bytearray(panel_rgb))
        os.unlink(panel)
        s.simcmd("pair off")
        s.key("t")
        st = s.wait_state(lambda v: v["settings"]["page"] == "connect", what="a tap goes back")
        self.eq(st["link"]["pair_left"], 0, "pair mode ends")
        s.key("p")
        s.key("p")

    def zoo(self):
        s = self.s
        self.connect_mac()
        s.cmd("#mode zoo")
        s.cmd("#anim 0.1")
        s.settle(0.3)
        st = s.state()
        self.eq(st["sel"], 0, "the first agent")
        f = s.shot("zoo-working")
        # Alone: no project pill. The clock's arch isn't drawn at all.
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.settle()
        f = s.shot("zoo-no-pill")
        blue = lambda c: abs(c[0] - 0x4F) < 16 and abs(c[1] - 0xB3) < 16 and abs(c[2] - 0xF6) < 16
        self.eq(f.count(100, 20, self.base - 100, 70, blue), 0, "Agent Zoo alone has no pill")
        for i, name in enumerate(["next-codex", "next-grok", "next-done", "next-idle"]):
            if self.has_knob:
                s.cmd("#turn 1")
            else:
                s.key("<")
            st = s.wait_state(lambda v: v["sel"] == i + 1, what="the next agent")
            s.settle(0.45)
            s.shot("zoo-" + name)
        # A dot: square only.
        if not self.round:
            s.cmd("#tap 188 448")
            s.wait_state(lambda v: v["sel"] == 0, what="a tap on a dot")
        else:
            s.cmd("#turn 1")
            s.wait_state(lambda v: v["sel"] == 0, what="round again")
        s.settle(0.45)
        # A tap focuses its Rex pane.
        s.clear_links()
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        m = s.wait_link(lambda m: m.get("t") == "tap", what="a tap message")
        self.eq(m["id"], "a1000e7", "its id")
        self.eq(s.state()["toast"], "Focusing in Rex", "toast")
        # An agent with no Rex pane says why.
        if self.has_knob:
            s.cmd("#turn 2")
        else:
            s.key("<")
            s.key("<")
        s.wait_state(lambda v: v["sel"] == 2, what="the background agent")
        s.settle(0.3)
        s.clear_links()
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        st = s.wait_state(lambda v: "Rex" in v["toast"], what="a toast")
        self.eq(st["toast"], "Not in Rex (background agent)", "toast")
        self.check(not any(m.get("t") == "tap" for m in s.links), "no message")
        s.settle()
        s.shot("zoo-toast")
        # The details card: hold; a tap closes it.
        s.cmd("#mode zoo")
        if self.has_knob:
            s.cmd("#turn 1")
        else:
            s.key("<")
        s.wait_state(lambda v: v["sel"] == 3, what="the agent with a subagent")
        s.key("r")
        st = s.wait_state(lambda v: v["card"], what="the card")
        self.eq(st["screen"], "card", "screen")
        s.cmd("#quiet")
        s.settle()
        s.shot("zoo-card")
        s.key("t")
        st = s.wait_state(lambda v: not v["card"], what="closed")
        # With a clock: top arc, or top third; the pill present.
        s.cmd("#mode timer")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.cmd("#mode zoo")
        s.settle(0.3)
        f = s.shot("zoo-with-timer")
        self.check(f.count(100, 20, self.base - 100, 70, blue) > 200, "the pill is there with a clock")
        # A long time, a long project.
        s.cmd("#elapsed 3725")
        s.settle()
        s.shot("zoo-with-long-timer")
        s.cmd("#project 999 #F2C14E Acme customer portal redesign and migration")
        s.settle()
        s.shot("zoo-long-project")
        s.cmd("#project 123")
        # Tapping the arch goes back to the clock's face.
        s.cmd(f"#tap {self.base // 2} 40")
        st = s.wait_state(lambda v: v["mode"] == "timer", what="tap on the arch")
        s.key("s")
        s.clear_links()
        # The Pomodoro's arc: relative to this countdown, full at the start whatever its length.
        s.cmd("#mode pomodoro")
        s.cmd("#set 50")
        s.key("s")
        s.wait_state(lambda v: v["pomo"]["state"] == "running", what="running")
        st = s.state()
        self.check(st["pomo"]["fraction"] > 0.99, f"full at the start: {st['pomo']['fraction']}")
        s.cmd("#mode zoo")
        s.settle(0.2)
        s.shot("zoo-pomodoro-full")
        s.cmd("#remaining 1500")
        s.settle(0.2)
        st = s.state()
        self.check(0.49 < st["pomo"]["fraction"] < 0.51, f"half: {st['pomo']['fraction']}")
        s.shot("zoo-pomodoro-half")
        s.cmd("#remaining 40")
        s.cmd("#pulse 1")
        s.settle(0.2)
        s.shot("zoo-pomodoro-last-minute")
        s.cmd("#pulse off")
        s.cmd("#mode pomodoro")
        s.key("s")
        s.key("r")
        s.cmd("#set 25")
        s.clear_links()
        s.cmd("#anim off")

    def crowd(self):
        """Hundreds of agents: an endless strip, and a list that arrives in pieces."""
        s = self.s
        self.connect_mac([])
        s.cmd("#mode zoo")
        words = ["auth", "billing", "search", "mobile", "data"]
        states = ["working", "working", "idle", "working", "done"]
        def row(i, st=None):
            return {"id": f"c{i:07x}", "name": f"{words[i % 5]}-{i}", "proj": words[i % 5], "kind": "i", "st": st or states[i % 5], "act": "Running the tests", "age": 30, "up": 300, "rex": 1}
        crowd = [row(i) for i in range(300)]
        # The whole list, in pages, with the count.
        self.mac_state(crowd)
        st = s.wait_state(lambda v: v["n_agents"] == 300, what="all 300")
        self.eq(st["sel"], 0, "the first is chosen")
        self.eq(len(st["agents"]), 32, "STATE names only the first few")
        self.eq(st["sel_name"], "auth-0", "and what's chosen")
        s.settle(0.5)
        s.shot("crowd-first")
        # It wraps both ways: the turn goes from the first to the last, and on to the first.
        s.settle(0.5)
        if self.has_knob:
            s.cmd("#turn -1")
            st = s.wait_state(lambda v: v["sel"] == 299, what="wraps to the last")
            s.settle(0.5)
            s.shot("crowd-last")
            s.settle(0.5)
            s.cmd("#turn 1")
            s.wait_state(lambda v: v["sel"] == 0, what="wraps to the first")
            # A slow turn moves one. A fast spin moves further.
            s.settle(1.0)
            s.cmd("#turn 1")
            s.wait_state(lambda v: v["sel"] == 1, what="one click, one agent")
            s.settle(1.0)
            start = s.state()["sel"]
            for _ in range(20):
                s.cmd("#turn 1")
            moved = s.state()["sel"] - start
            self.check(moved > 25, f"a fast spin moved {moved} agents for 20 clicks")
        # A drag scrubs through them, and a flick carries on.
        self.mac_agents(crowd)
        s.cmd("#mode zoo")
        k = self.base / 360
        y = int(self.base * 0.4)
        start = s.state()["sel"]
        self.touch("down", int(300 * k), y)
        for i in range(1, 9):
            sleep(0.012)
            self.touch("move", int((300 - i * 28) * k), y)
        self.touch("up", int((300 - 8 * 28) * k), y)
        at_release = s.state()["sel"]
        self.check(at_release - start >= 6, f"a drag of 8 agents' width moved {at_release - start}")
        st = s.state()
        self.check(st["fling"] > 3, f"a flick carries on: {st['fling']:.1f} agents a second")
        s.wait_state(lambda v: v["fling"] == 0, timeout=5, what="the flick coming to rest")
        coasted = s.state()["sel"]
        self.check(coasted > at_release, f"it coasted from {at_release} to {coasted}")
        # A touch stops a flick, and does nothing else.
        self.touch("down", int(300 * k), y)
        for i in range(1, 9):
            sleep(0.012)
            self.touch("move", int((300 - i * 28) * k), y)
        self.touch("up", int((300 - 8 * 28) * k), y)
        s.clear_links()
        s.cmd("#anim 0.4")
        self.touch("down", int(180 * k), y)
        self.touch("up", int(180 * k), y)
        st = s.wait_state(lambda v: v["fling"] == 0, timeout=1, what="the flick stopped")
        s.settle(0.1)
        self.check(not [m for m in s.links if m.get("t") == "tap"], "the touch that stopped it isn't a tap")
        s.cmd("#anim off")
        # A change is one row. The chosen agent stays chosen as others come and go.
        s.cmd("#mode zoo")
        self.mac_agents(crowd)
        st = s.state()
        chosen = st["sel"]
        name = st["sel_name"]
        s.mac({"t": "agents", "gone": [crowd[0]["id"], crowd[1]["id"]]})
        st = s.wait_state(lambda v: v["n_agents"] == 298, what="two gone")
        if chosen >= 2:
            self.eq(st["sel_name"], name, "the chosen agent stays chosen")
        s.mac({"t": "agents", "rows": [row(1000)]})
        st = s.wait_state(lambda v: v["n_agents"] == 299, what="a new one")
        self.eq(st["sel_name"], name, "and still, with a new one at the end")
        # Who needs you, out of sight: counted beside x / y, and a tap goes there.
        far = row(200, "needs")
        s.clear_logs()
        s.cmd("#mode timer")
        s.mac({"t": "agents", "rows": [far]})
        s.wait_log("needs_you")
        st = s.wait_state(lambda v: v["n_needing"] == 1, what="one needing")
        s.cmd("#mode zoo")
        s.settle(0.5)
        s.shot("crowd-needs-you")
        layout_y = 282 if self.round else 366
        s.cmd(f"#tap {self.base // 2} {layout_y}")
        st = s.wait_state(lambda v: v["sel_name"] == far["name"], what="a tap on that line goes to who needs you")
        s.settle(0.5)
        s.shot("crowd-chosen-needs-you")
        far2 = dict(far, st="working")
        s.mac({"t": "agents", "rows": [far2]})
        s.wait_state(lambda v: v["n_needing"] == 0, what="answered")
        # Fewer than a screenful: every agent has a dot, and a swipe is one agent.
        self.mac_state(SAMPLE_AGENTS)
        st = s.wait_state(lambda v: v["n_agents"] == 5, what="the whole list names five: the rest are gone")
        s.settle(0.5)
        s.shot("crowd-back-to-five")
        # A count that doesn't match asks for the whole list only if it stays that way, seconds apart, with no agents
        # arriving in between. Then not again for 30 s.
        s.clear_links()
        base = {"t": "state", "v": 4, "seq": 2, "now": 1793826000, "utc": -28800, "toggl": "ok", "voice": True}
        no_resync = lambda: not [m for m in s.links if m.get("t") == "resync"]
        s.mac(dict(base, n=6))
        s.mac(dict(base, n=6))
        s.mac(dict(base, n=6))
        s.settle(0.2)
        self.check(no_resync(), "several at once could be changes on their way")
        s.settle(3.1)
        s.mac({"t": "agents", "rows": [SAMPLE_AGENTS[0]]})
        s.mac(dict(base, n=6))
        s.settle(0.2)
        self.check(no_resync(), "rows still arriving: nothing's lost")
        s.settle(3.1)
        s.mac(dict(base, n=6))
        s.wait_link(lambda m: m.get("t") == "resync", what="a resync")
        s.clear_links()
        s.settle(3.1)
        s.mac(dict(base, n=6))
        s.settle(3.1)
        s.mac(dict(base, n=6))
        s.settle(0.2)
        self.check(no_resync(), "not again so soon")
        # A page that goes missing leaves everyone in place.
        s.mac({"t": "agents", "page": 0, "pages": 3, "rows": [row(1)]})
        s.mac({"t": "agents", "page": 2, "pages": 3, "rows": [row(2)]})
        st = s.wait_state(lambda v: v["n_agents"] == 7, what="the rows that came")
        self.mac_agents(SAMPLE_AGENTS)
        s.wait_state(lambda v: v["n_agents"] == 5, what="the next whole list settles it")
        # The board keeps the first 512, and ignores the rest.
        self.mac_state([row(i) for i in range(600)])
        st = s.wait_state(lambda v: v["n_agents"] == 512, what="512 and no more")
        s.settle(0.3)
        s.shot("crowd-512")
        self.mac_state()
        s.wait_state(lambda v: v["n_agents"] == 5, what="back to the sample")
        s.cmd("#mode timer")

    def needs(self):
        s = self.s
        self.connect_mac()
        s.cmd("#anim 0.4")
        s.cmd("#mode zoo")
        agents = [dict(a) for a in SAMPLE_AGENTS]
        s.clear_logs()
        agents[2]["st"] = "needs"
        agents[2]["act"] = "Waiting for your answer"
        self.mac_state(agents)
        s.wait_log("feedback alert")
        st = s.wait_state(lambda v: v["sel"] == 2, what="it goes to the one that needs you")
        s.settle(0.5)
        s.shot("zoo-needs-you")
        s.cmd("#mode timer")
        s.settle()
        st = s.state()
        self.eq(st["status"], "ml-eval-5d needs you", "a clock's status names it")
        self.eq(st["status_amber"], True, "in amber")
        s.shot("timer-needs-you")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.cmd("#mode zoo")
        s.settle(0.3)
        s.shot("zoo-needs-you-with-timer")
        s.cmd("#theme inverted")
        s.settle(0.3)
        s.shot("zoo-needs-you-inverted")
        s.cmd("#theme mono")
        s.cmd("#mode timer")
        s.key("s")
        self.mac_state()
        s.cmd("#anim off")
        s.clear_links()

    def effects(self):
        """What the agent on screen does when something happens to it, and that nothing happens for any other."""
        s = self.s
        words = ["auth", "billing", "search", "mobile", "data"]
        def row(i, st="working", act="Running the tests", rex=1):
            return {"id": f"e{i:07x}", "name": f"{words[i % 5]}-{i}", "proj": "fx", "kind": "i", "st": st, "act": act, "age": 30, "up": 300, "rex": rex}
        def fx():
            return s.state()["fx"]
        def logged(prefix):
            with s.cond:
                return [ln for ln in s.logs if ln.startswith(prefix)]
        def fresh():
            s.clear_logs()
            with s.cond:
                s.logs.clear()
        def send(*rows, gone=()):
            s.mac({"t": "agents", "rows": list(rows), "gone": list(gone)})
        def next_agent(n=1):
            if self.has_knob:
                s.cmd(f"#turn {n}")
            else:
                for _ in range(n):
                    s.key("<")
        section_y = 134 if self.round else 180
        def same_rows(a, b, y0, y1):
            w = a.w * 2
            return a.data[y0 * w:y1 * w] == b.data[y0 * w:y1 * w]

        self.connect_mac([row(0), row(1), row(2), row(3)], lock=True)
        s.cmd("#mode zoo")
        s.cmd("#quiet")
        s.settle(0.2)

        # It finishes: confetti, a hop, a yay and a check mark for about 2.2 s.
        fresh()
        t0 = time.time()
        send(row(0, "done", "Finished"))
        s.wait_log("fx done")
        s.wait_log("feedback yay sound=1")
        st = fx()
        self.check(st["kinds"]["confetti"] >= 20, f"confetti: {st['kinds']}")
        self.eq(st["bubble"], "check", "the bubble")
        self.check(st["hopping"], "it hops")
        self.check(st["particles"] <= st["cap"], "within the pool")
        s.shot("fx-done")
        s.wait_state(lambda v: not v["fx"]["hopping"], timeout=2, what="it lands")
        s.wait_state(lambda v: v["fx"]["bubble"] == "", timeout=5, what="the bubble going")
        self.check(1.8 < time.time() - t0 < 3.6, f"the check mark lasted {time.time() - t0:.1f} s")

        # Nothing for an agent that isn't on screen, whatever happens to it.
        fresh()
        send(row(1, "done", "Finished"), row(2, "failed", "Stopped"), row(3, "idle", "Waiting"))
        s.wait_state(lambda v: [a["st"] for a in v["agents"][1:4]] == ["done", "failed", "idle"], what="others changing")
        s.settle(0.2)
        self.eq(logged("fx "), [], "no effect for an agent off screen")
        self.eq(logged("feedback yay"), [], "and no sound")
        # Turning to one doesn't replay what happened to it.
        fresh()
        next_agent()
        st = s.wait_state(lambda v: v["sel"] == 1, what="the one that finished")
        s.settle(0.1)
        st = fx()
        self.eq(st["kinds"]["confetti"], 0, "no confetti for what's past")
        self.eq(st["bubble"], "", "no check mark for what's past")
        self.eq(logged("fx "), [], "nothing replayed")
        next_agent()
        s.wait_state(lambda v: v["sel"] == 2, what="the one that failed")
        s.wait_state(lambda v: v["fx"]["bubble"] == "x", timeout=1, what="its X, which stays")
        self.check(fx()["squash"] == 1, "no squash for what's past")
        self.eq(logged("fx "), [], "nothing replayed")
        next_agent()
        s.wait_state(lambda v: v["sel"] == 3, what="the idle one")
        s.wait_state(lambda v: v["fx"]["kinds"]["z"] > 0, timeout=3, what="Z's")
        next_agent()
        st = s.wait_state(lambda v: v["sel"] == 0, what="the first again")

        # It goes idle: Z's.
        fresh()
        send(row(0, "idle", "Waiting for a task"))
        s.wait_log("fx idle")
        s.wait_state(lambda v: v["fx"]["kinds"]["z"] > 0, timeout=2, what="Z's")
        s.shot("fx-idle")
        # It works: a bubble for about 2.6 s, then now and then a sparkle. A new kind of activity shows too.
        fresh()
        t0 = time.time()
        send(row(0, "working", "Editing main.c"))
        s.wait_log("fx working")
        s.wait_state(lambda v: v["fx"]["bubble"] == "edit", timeout=1, what="the activity's bubble")
        s.shot("fx-working")
        s.wait_state(lambda v: v["fx"]["bubble"] == "", timeout=5, what="the bubble going")
        self.check(2.2 < time.time() - t0 < 4, f"the bubble lasted {time.time() - t0:.1f} s")
        s.wait_state(lambda v: v["fx"]["kinds"]["sparkle"] > 0, timeout=4, what="a sparkle")
        send(row(0, "working", "Reading train.py"))
        s.wait_state(lambda v: v["fx"]["bubble"] == "read", timeout=1, what="the new activity")
        # It fails: an X that stays, and a squash.
        fresh()
        send(row(0, "failed", "Stopped"))
        s.wait_log("fx failed")
        st = fx()
        self.eq(st["bubble"], "x", "the bubble")
        self.check(st["squash"] < 1, f"a squash: {st['squash']}")
        s.settle(3.2)
        self.eq(fx()["bubble"], "x", "the X stays")
        s.shot("fx-failed")
        # A tap: a little shake, and the toast it always had.
        send(row(0))
        s.settle(0.1)
        fresh()
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        s.wait_log("fx tap")
        st = fx()
        self.check(st["shaking"], "it shakes")
        self.eq(st["bubble"], "arrow", "going to Rex")
        self.eq(s.state()["toast"], "Focusing in Rex", "toast")
        s.wait_state(lambda v: not v["fx"]["shaking"], timeout=2, what="the shake ending")
        s.cmd("#quiet")

        # Needs you takes over: its bubble and jump, and no particles under them.
        send(row(0, "done", "Finished"))
        s.wait_state(lambda v: v["fx"]["kinds"]["confetti"] > 0, timeout=1, what="confetti")
        send(row(0, "needs", "Waiting for your answer"))
        s.wait_state(lambda v: v["fx"]["bubble"] == "" and v["fx"]["kinds"]["confetti"] == 0, timeout=1, what="the confetti cleared")
        send(row(0))

        # Over a menu, a list, a card or another mode, nothing plays, and it isn't kept for later.
        for name, go, back in (("the menu", lambda: s.key("u"), lambda: s.key("p")),
                               ("a card", lambda: s.key("r"), lambda: s.key("t")),
                               ("a clock", lambda: s.cmd("#mode timer"), lambda: s.cmd("#mode zoo"))):
            go()
            s.settle(0.1)
            fresh()
            s.cmd("#fx done", expect=False)
            send(row(0, "done", "Finished"))
            s.wait_state(lambda v: v["agents"][0]["st"] == "done", what="its state changing")
            s.settle(0.1)
            self.eq(logged("fx "), [], f"no effect under {name}")
            self.eq(logged("feedback yay"), [], f"no sound under {name}")
            self.eq(fx()["particles"], 0, f"no particles under {name}")
            back()
            s.settle(0.15)
            self.eq(fx()["kinds"]["confetti"], 0, f"nothing replayed after {name}")
            send(row(0))
            s.settle(0.05)
        s.cmd("#mode timer")
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="a list")
        fresh()
        send(row(0, "done", "Finished"))
        s.settle(0.2)
        self.eq(logged("fx "), [], "no effect under a list")
        s.key("p")
        s.wait_state(lambda v: not v["list"]["open"], what="the list closed")
        s.cmd("#mode zoo")
        send(row(0))
        s.settle(0.1)

        # Locked: it still draws and vibrates, with no sound. With the keypad up, it isn't on screen.
        s.cmd("#lock")
        fresh()
        send(row(0, "done", "Finished"))
        s.wait_log("fx done")
        s.wait_log("feedback yay sound=0")
        s.wait_state(lambda v: v["fx"]["kinds"]["confetti"] > 0, timeout=1, what="confetti while locked")
        s.settle(0.1)
        s.key("t")
        s.wait_state(lambda v: v["lock"]["pad"], what="the keypad")
        s.settle(0.1)
        self.eq(fx()["particles"], 0, "nothing under the keypad")
        fresh()
        send(row(0, "failed", "Stopped"))
        s.settle(0.2)
        self.eq(logged("fx "), [], "no effect under the keypad")
        s.cmd("#unlock")
        send(row(0))
        s.settle(0.1)

        # It ends, with others left: the list changes at once, and it's drawn from a copy until the effect ends.
        before = s.state()
        name = before["sel_name"]
        fresh()
        t0 = time.time()
        send(gone=[row(0)["id"]])
        st = s.wait_state(lambda v: v["n_agents"] == before["n_agents"] - 1, timeout=1, what="the list changing at once")
        self.check(st["fx"]["leaving"], "it's still leaving")
        self.eq(st["fx"]["leaving_name"], name, "from a copy")
        self.check(st["sel_name"] != name, "the next is chosen already")
        s.wait_log("fx gone")
        # A tap meanwhile reaches nobody: the next isn't showing yet.
        s.clear_links()
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        s.settle(0.05)
        self.check(not [m for m in s.links if m.get("t") == "tap"], "no tap for an agent that's going")
        s.wait_log("feedback poof sound=1", timeout=2)
        st = s.wait_state(lambda v: v["fx"]["poofed"], timeout=1, what="the poof")["fx"]
        self.check(st["kinds"]["smoke"] > 8, f"smoke: {st['kinds']}")
        self.eq(st["kinds"]["hat"], 1, "its hat flies off")
        s.shot("fx-gone")
        s.wait_state(lambda v: not v["fx"]["leaving"], timeout=3, what="the effect ending")
        self.check(1.1 < time.time() - t0 < 2.6, f"the next took its place after {time.time() - t0:.1f} s")
        st = fx()
        self.eq(st["particles"], st["kinds"]["sparkle"] + st["kinds"]["z"] + st["kinds"]["wisp"], "nothing left of it")
        # Another off screen goes: nothing shows.
        fresh()
        st = s.state()
        other = next(a for a in st["agents"] if a["name"] != st["sel_name"])
        send(gone=[other["id"]])
        s.wait_state(lambda v: v["n_agents"] == st["n_agents"] - 1, what="one off screen gone")
        s.settle(0.15)
        self.eq(logged("fx "), [], "no effect for an agent off screen")
        self.eq(logged("feedback poof"), [], "and no sound")
        # Turning to another during a poof moves on at once.
        st = s.state()
        send(gone=[st["agents"][st["sel"]]["id"]], *[row(0)])
        s.wait_state(lambda v: v["fx"]["leaving"], timeout=1, what="leaving")
        next_agent()
        s.wait_state(lambda v: not v["fx"]["leaving"], timeout=0.5, what="a turn ending the effect")

        # The last one leaves: "No agents running" comes after the effect, and the screen is just as it was with none.
        self.mac_state([])
        s.wait_state(lambda v: v["n_agents"] == 0 and not v["fx"]["leaving"], timeout=3, what="no agents")
        s.cmd("#quiet")
        s.settle(0.15)
        empty = s.frame()
        fresh()
        send(row(5))
        s.wait_log("fx new")
        s.wait_log("feedback drop sound=1")
        self.check(fx()["dropping"], "it falls")
        s.wait_log("feedback land sound=1", timeout=2)
        st = fx()
        self.check(not st["dropping"], "and lands")
        self.check(st["kinds"]["dust"] > 0, f"kicking up dust: {st['kinds']}")
        self.check(st["squash"] != 1, "squashed")
        # One more, with this one on screen: only its dot shows.
        fresh()
        send(row(6))
        s.wait_state(lambda v: v["n_agents"] == 2, what="a second")
        s.settle(0.1)
        self.eq(logged("fx "), [], "no effect for an agent off screen")
        self.eq(logged("feedback drop"), [], "and no sound")
        self.eq(s.state()["sel_name"], row(5)["name"], "the one on screen stays")
        send(gone=[row(6)["id"]])
        s.settle(0.8)
        fresh()
        send(gone=[row(5)["id"]])
        st = s.wait_state(lambda v: v["n_agents"] == 0, timeout=1, what="the list emptied")
        self.check(st["fx"]["leaving"], "the last one still leaves")
        s.settle(0.3)
        during = s.frame()
        self.check(not same_rows(during, empty, 70, self.base), "still its effect, not the empty screen")
        self.eq(s.state()["fx"]["leaving_name"], row(5)["name"], "from its copy")
        s.wait_state(lambda v: not v["fx"]["leaving"], timeout=3, what="the effect ending")
        s.settle(0.1)
        self.check(same_rows(s.frame(), empty, 70, self.base), "now the screen with no agents")
        # One arriving during the poof takes the place, and drops in when the smoke clears.
        send(row(7))
        s.wait_log("fx new")
        s.settle(0.8)
        fresh()
        send(gone=[row(7)["id"]])
        s.wait_log("fx gone")
        s.settle(0.2)
        send(row(8))
        s.wait_state(lambda v: v["n_agents"] == 1, what="one arriving")
        self.eq(logged("fx new"), [], "it waits")
        s.wait_log("fx new", timeout=3)
        s.settle(1.0)

        # Hundreds arriving and leaving at once: one effect each way, however many, and never past the pool.
        self.mac_state([])
        s.wait_state(lambda v: v["n_agents"] == 0 and not v["fx"]["leaving"], timeout=3, what="no agents")
        fresh()
        crowd = [row(100 + i) for i in range(300)]
        for i in range(0, 300, 40):
            send(*crowd[i:i + 40])
        s.wait_state(lambda v: v["n_agents"] == 300, timeout=5, what="all of them")
        self.eq(len(logged("fx new")), 1, "one drop for 300 arrivals")
        self.eq(len(logged("feedback drop")), 1, "and one sound")
        s.settle(0.8)
        fresh()
        for i in range(0, 300, 40):
            send(gone=[a["id"] for a in crowd[i:i + 40]])
        s.wait_state(lambda v: v["n_agents"] == 0, timeout=5, what="all gone")
        self.eq(len(logged("fx gone")), 1, "one poof for 300 leaving")
        s.wait_state(lambda v: not v["fx"]["leaving"], timeout=3, what="the effect ending")
        send(row(9))
        s.settle(0.8)
        # The pool holds, whatever the Mac says and however fast.
        before = fx()
        for _ in range(30):
            send(*[row(9, "done", "Finished") if k % 2 == 0 else row(9) for k in range(20)])
        s.settle(0.3)
        st = fx()
        self.check(st["particles"] <= st["cap"], f"{st['particles']} particles in a pool of {st['cap']}")
        self.check(st["peak"] <= st["cap"], f"a peak of {st['peak']} in a pool of {st['cap']}")
        self.check(st["peak"] == st["cap"], f"this really filled it: {st['peak']}")
        self.check(st["refused"] > before["refused"], "and the rest were refused")
        send(row(9))
        s.wait_state(lambda v: v["fx"]["particles"] == v["fx"]["kinds"]["sparkle"], timeout=3, what="the burst dying down")

        # Under a clock: smaller, and kept out of the clock's section.
        s.cmd("#mode timer")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.cmd("#mode zoo")
        s.cmd("#anim 1")
        s.cmd("#fx seed 11")
        s.settle(0.1)
        for ev, ms, want in (("done", 450, "confetti"), ("gone", 500, "smoke"), ("new", 200, None)):
            s.cmd("#elapsed 100")
            s.settle(0.1)
            a = s.frame()
            s.cmd(f"#fx {ev}")
            s.cmd(f"#fx step {ms}")
            s.settle(0.1)
            b = s.frame()
            s.shot("fx-under-clock-" + ev, b)
            st = fx()
            if want:
                self.check(st["kinds"][want] > 0, f"{want} under a clock: {st['kinds']}")
            self.check(a.data != b.data, "it shows")
            if s.state()["time"] == "01:40":  # the digits only differ if a second went by
                self.check(same_rows(a, b, 0, section_y), f"{ev}: the clock's section is untouched")
            s.cmd("#fx step 3000")
            s.settle(0.05)
        s.cmd("#anim off")
        s.cmd("#mode timer")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] != "running", what="paused")
        s.key("r")
        # The Inverted profile, while it runs for real.
        s.cmd("#theme inverted")
        s.cmd("#mode zoo")
        send(row(9, "done", "Finished"))
        s.settle(0.5)
        s.shot("fx-done-inverted")
        send(gone=[row(9)["id"]])
        s.settle(0.6)
        s.shot("fx-gone-inverted")
        s.wait_state(lambda v: not v["fx"]["leaving"], timeout=3, what="the effect ending")
        s.cmd("#theme mono")
        # No Mac needed: a stand-in, for tests and for the board on its own.
        s.cmd("#fx done")
        self.eq(s.state()["sel_name"], "fx-test", "the stand-in")
        s.cmd("#fx nonsense", expect=False)
        s.cmd("#fx clear")
        self.eq(s.state()["n_agents"], 0, "and it's gone")
        s.cmd("#mode timer")

    def auto_scroll(self):
        """Runs early, before anything has picked an agent by hand: such a pick holds it for 20 s."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode zoo")
        s.cmd("#autoscroll on")
        s.settle()
        first = s.state()["sel"]
        t0 = time.time()
        st = s.wait_state(lambda v: v["sel"] != first, timeout=10, what="the next agent")
        self.check(6 < time.time() - t0 < 9.5, f"every 8 s: {time.time() - t0:.1f}")
        # A pick by hand holds for longer than that.
        if self.has_knob:
            s.cmd("#turn 1")
        else:
            s.key("<")
        picked = s.wait_state(lambda v: v["sel"] != st["sel"], what="a pick by hand")["sel"]
        sleep(9)
        self.eq(s.state()["sel"], picked, "a pick by hand holds")
        s.cmd("#autoscroll off")
        s.cmd("#mode timer")

    def inverted(self):
        s = self.s
        self.connect_mac()
        s.cmd("#anim 0.1")
        s.cmd("#theme inverted")
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.cmd("#mode timer")
        s.settle()
        f = s.shot("inverted-timer")
        self.eq(f.rgb(self.base // 2, self.base // 2 + 40), (255, 255, 255), "the face is white")
        s.cmd("#mode pomodoro")
        s.settle()
        s.shot("inverted-pomodoro")
        s.cmd("#remaining 300")
        s.settle()
        s.shot("inverted-pomodoro-warm")
        s.key("r")
        s.key("u")
        s.wait_state(lambda v: v["menu"]["open"], what="the menu")
        s.settle(0.3)
        s.shot("inverted-menu")
        s.key("p")
        s.cmd("#mode zoo")
        s.settle(0.3)
        s.shot("inverted-zoo")
        for i in range(2):
            if self.has_knob:
                s.cmd("#turn 1")
            else:
                s.key("<")
            s.settle(0.4)
        f = s.shot("inverted-zoo-grok")
        # White character: a dark edge around it. The ball's own left edge meets the screen's white through it.
        dark = lambda c: abs(c[0] - 0x28) < 12 and abs(c[1] - 0x28) < 12 and abs(c[2] - 0x25) < 12
        self.check(f.count(0, 0, self.base, self.base, dark) > 100, "the dark edge shows")
        s.cmd("#mode settings")
        s.settle(0.3)
        s.shot("inverted-settings")
        s.key("p")
        s.cmd("#mode timer")
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="the projects list")
        s.settle(0.3)
        s.shot("inverted-projects")
        s.key("p")
        s.cmd("#mode zoo")
        s.key("r")
        s.wait_state(lambda v: v["card"], what="the card")
        s.settle()
        s.shot("inverted-card")
        s.key("t")
        s.cmd("#mode timer")
        s.mac(LOCK_MSG)
        s.settle()
        s.cmd("#lock")
        s.key("t")
        s.settle()
        s.shot("inverted-keypad")
        s.cmd("#unlock")
        # Inverted with a clock above an agent.
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.cmd("#mode zoo")
        s.settle(0.3)
        s.shot("inverted-zoo-with-timer")
        s.cmd("#mode timer")
        s.key("s")
        s.cmd("#theme mono")
        s.cmd("#anim off")
        s.clear_links()

    def rotation(self):
        s = self.s
        if self.round:
            st = s.state()
            s.cmd("#tilt 90")
            sleep(1.2)
            self.eq(s.state()["rotation"]["orient"], self.mount, "no motion sensor: it never turns")
            s.cmd("#tilt_off")
            return
        self.connect_mac()
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.settle()
        # Base picture.
        up = s.frame()
        # A brief wobble doesn't move it.
        s.cmd("#tilt 90")
        sleep(0.25)
        s.cmd("#tilt 0")
        sleep(0.9)
        self.eq(s.state()["rotation"]["orient"], 0, "a wobble doesn't turn it")
        # Within the hysteresis: 50 degrees isn't past 45 + 12.
        s.cmd("#tilt 50")
        sleep(0.9)
        self.eq(s.state()["rotation"]["orient"], 0, "not past the hysteresis")
        s.cmd("#tilt 0")
        # The turn: it waits under a second, then animates.
        t0 = time.time()
        s.cmd("#tilt 90")
        st = s.wait_state(lambda v: v["rotation"]["turning"], timeout=2, what="the turn starting")
        waited = time.time() - t0
        self.check(0.55 < waited < 1.0, f"TURN_WAIT: {waited:.2f} s")
        mid = s.shot("rotate-turning")
        self.check(sum(1 for y in range(0, 480, 8) for x in range(0, 480, 8) if mid.rgb(x, y) != up.rgb(x, y)) > 50, "the picture is moving")
        st = s.wait_state(lambda v: not v["rotation"]["turning"], timeout=1.5, what="the turn ending")
        self.eq(st["rotation"]["orient"], 90, "orientation")
        s.settle(0.15)
        f90 = s.shot("rotate-90")
        for deg in (180, 270):
            s.cmd(f"#tilt {deg}")
            s.wait_state(lambda v: v["rotation"]["orient"] == deg and not v["rotation"]["turning"], timeout=2.5, what=f"{deg} degrees")
            s.settle(0.15)
            f = s.shot(f"rotate-{deg}")
            # Pixels really are the upright picture, turned. (Away from anything moving: the rim and pill.)
            for (ux, uy) in ((240, 66), (240, 10), (10, 240), (240, 470)):
                if deg == 180:
                    px, py = 479 - ux, 479 - uy
                else:
                    px, py = 479 - uy, ux
                self.eq(f.rgb(px, py), up.rgb(ux, uy), f"pixel ({ux},{uy}) at {deg}")
        # Touch turns with it: a tap at the panel's place for the pill opens the projects list.
        s.cmd("#tilt 90")
        s.wait_state(lambda v: v["rotation"]["orient"] == 90 and not v["rotation"]["turning"], timeout=2.5, what="back at 90")
        # Upright (240, 70) is at the panel's (70, 480 - 240) at 90 degrees.
        s.cmd("#rawtap 74 240")
        st = s.wait_state(lambda v: v["list"]["open"], timeout=1, what="the pill, rotated")
        self.eq(st["list"]["kind"], "projects", "the pill")
        s.key("p")
        s.wait_state(lambda v: not v["list"]["open"], what="closed")
        # And a swipe up, in the screen's direction. Turned clockwise by 90 degrees the panel's +x points down, so up is -x.
        s.cmd("#rawtap 240 240") # a tap in the middle starts it
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="a tap")
        s.key("s")
        s.clear_links()
        s.simcmd("touch down 360 240")
        s.simcmd("touch move 240 240")
        s.simcmd("touch move 120 240")
        s.simcmd("touch up 120 240")
        st = s.wait_state(lambda v: v["menu"]["open"], timeout=1, what="a swipe up, in the screen's direction")
        s.settle(0.3)
        s.shot("rotate-90-menu")
        s.key("p")
        # At 180: the rim's six o'clock is at the panel's top.
        s.cmd("#tilt 180")
        s.wait_state(lambda v: v["rotation"]["orient"] == 180 and not v["rotation"]["turning"], timeout=2.5, what="at 180")
        s.cmd("#rawtap 240 406")
        st = s.wait_state(lambda v: v["list"]["open"], timeout=1, what="the pill at 180")
        s.key("p")
        s.cmd("#tilt 270")
        s.wait_state(lambda v: v["rotation"]["orient"] == 270 and not v["rotation"]["turning"], timeout=2.5, what="at 270")
        s.cmd("#rawtap 406 240")
        st = s.wait_state(lambda v: v["list"]["open"], timeout=1, what="the pill at 270")
        s.key("p")
        # Flat: gravity gives no direction, so nothing moves.
        s.cmd("#flat on")
        s.cmd("#tilt 0")
        sleep(1.2)
        self.eq(s.state()["rotation"]["orient"], 270, "flat: it stays")
        s.cmd("#flat off")
        s.wait_state(lambda v: v["rotation"]["orient"] == 0 and not v["rotation"]["turning"], timeout=2.5, what="upright again")
        # Auto-rotate off.
        s.cmd("#autorotate off")
        s.cmd("#tilt 180")
        sleep(1.1)
        self.eq(s.state()["rotation"]["orient"], 0, "auto-rotate off")
        s.cmd("#autorotate on")
        s.cmd("#tilt 0")
        s.cmd("#tilt_off")
        # The real sensor path: the simulator's motion reading, through hal_motion_read.
        s.simcmd("tilt 90")
        s.wait_state(lambda v: v["rotation"]["orient"] == 90, timeout=2.5, what="the sensor's tilt")
        s.simcmd("tilt 0")
        s.wait_state(lambda v: v["rotation"]["orient"] == 0 and not v["rotation"]["turning"], timeout=2.5, what="the sensor's tilt back")
        s.simcmd("flat 1")
        s.settle(0.25)
        self.eq(s.state()["rotation"]["flat"], True, "flat from the sensor")
        s.simcmd("flat 0")
        s.settle(0.25)

    def steady(self):
        """Screens that aren't changing don't repaint, even when the FPU rounds differently from one frame to the next,
        as a board's did: the change check hashes coordinates rounded to 1/64 pixel, not their last bits."""
        s = self.s
        self.connect_mac()
        s.simcmd("rounding flip")
        try:
            for mode in ("timer", "pomodoro", "settings", "clock"):
                s.cmd(f"#mode {mode}")
                s.cmd("#quiet")
                s.settle(0.8)
                before = s.state()["render"]["painted"]
                s.settle(1.0)
                painted = s.state()["render"]["painted"] - before
                self.check(painted == 0, f"{mode}: {painted} repaints in 1 s with nothing changing")
            # A real change still repaints: the Pomodoro's length.
            s.cmd("#mode pomodoro")
            s.settle(0.4)
            before = s.state()["render"]["painted"]
            s.cmd("#set 26")
            s.settle(0.3)
            self.check(s.state()["render"]["painted"] > before, "a real change still repaints")
            s.cmd("#set 25")
        finally:
            s.simcmd("rounding steady")

    def perf(self):
        """#perf, which tools/perf_test.py times each action with: the clock starts at the input, not at #perf start."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        s.cmd("#quiet")
        s.settle(0.6)

        def perf(arg):
            s.send(f"#perf {arg}")
            line = s._wait("perf", timeout=5)
            s._wait("cmd", pred=lambda v: v.split(" ")[1] == "perf")  # its own reply, so a later #perf can't take it
            return json.loads(line)

        self.check(not perf("peek")["started"], "nothing armed: not started")
        s.cmd("#perf start")
        s.settle(0.3)
        idle = perf("peek")
        self.check(not idle["started"] and idle["frames"] == 0, f"armed, with no input yet: nothing counted ({idle})")
        s.key("u")  # the menu: the input that starts it
        deadline = time.time() + 3
        p = perf("peek")
        while time.time() < deadline and p["quiet_ms"] < 300:
            s.settle(0.1)
            p = perf("peek")
        self.check(p["started"] and p["painted"] >= 1 and p["bands"] >= 1, f"the menu counted: {p}")
        self.check(0 < p["first_ms"] <= p["settle_ms"] < 3000, f"first change, then settled: {p}")
        self.check(p["frames"] >= p["painted"] and p["work_ms"] >= p["frame_ms_max"] > 0, f"frames and their times: {p}")
        self.check(p["quiet_ms"] >= 300, f"quiet since: {p}")
        done = perf("stop")
        self.check(done["started"] and done["painted"] == p["painted"], f"stop reports the same run: {done}")
        self.check(not perf("peek")["started"], "and disarms")
        # A message from the Mac starts it too.
        s.cmd("#perf start")
        s.mac({"t": "state", "v": 4, "seq": 5, "now": int(time.time()), "utc": -28800, "toggl": "ok", "voice": True, "n": 0})
        s.settle(0.3)
        self.check(perf("stop")["started"], "a LINK line starts it")
        self.check(not s.cmd("#perf sideways", expect=None), "an unknown #perf is an error")
        s.key("p")
        # #why: a running Timer's repaint names the digits that changed, and nothing else.
        s.cmd("#mode timer")
        s.key("s")
        s.settle(0.3)
        s.cmd("#why 1")
        why = json.loads(s._wait("other", timeout=4, pred=lambda v: v.startswith("WHY "))[4:])
        texts = [c.get("text", "") for c in why["changed"]]
        self.check(any(re.fullmatch(r"\d+:\d\d", t) for t in texts), f"#why names the time: {why}")
        self.check(len(why["changed"]) == 1, f"and only the time: {texts}")
        s.key("r")

    def burn_in(self):
        s = self.s
        self.connect_mac()
        s.cmd("#task Write the Q4 plan")
        s.settle()
        a = s.frame()
        s.cmd("#mode timer")
        s.settle()
        a = s.frame()
        s.cmd("#burnin 2 1")
        s.settle()
        b = s.frame()
        s.shot("burn-in-shifted", b)
        if not self.round:
            # The edge it moved away from is the face, with no old rim left behind in it.
            self.check(all(b.rgb(x, y) == (0, 0, 0) for y in range(40, 440, 7) for x in (0, 1)), "the edge is clean")
            self.check(all(b.rgb(x, 0) == (0, 0, 0) for x in range(40, 440, 7)), "the top too")
        same = sum(1 for y in range(100, self.base - 100, 3) for x in range(100, self.base - 100, 3) if a.rgb(x, y) == b.rgb(x, y))
        moved = sum(1 for y in range(100, self.base - 100, 3) for x in range(100, self.base - 100, 3) if a.rgb(x, y) == b.rgb(x + 2, y + 1))
        total = len(range(100, self.base - 100, 3)) ** 2
        self.eq(moved, total, "the whole picture moved by exactly the shift" if not self.round else "an LCD has no shift: nothing moved... ") if not self.round else self.eq(same, total, "an LCD has no shift")
        s.cmd("#burnin 0 0")

    def hold_ring(self):
        """A press on a clock shows a ring filling up, and the reset fires as it closes."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode pomodoro")
        s.cmd("#set 25")
        s.key("s")
        s.wait_state(lambda v: v["pomo"]["state"] == "running", what="running")
        s.settle(0.2)
        mid = self.base // 2
        r = 128 * self.base / 360
        ink = lambda c: min(c) > 200
        at = lambda f, a: f.rgb(int(mid + r * math.sin(a)), int(mid - r * math.cos(a)))
        self.touch("down", mid, mid)
        sleep(0.42)  # about halfway: a quarter of the ring is certainly done, and three quarters certainly aren't
        # The frame and the state first: saving a PNG takes a slow machine long enough for the hold to end.
        f = s.frame()
        still = s.state()["pomo"]["state"]
        s.shot("hold-ring-halfway", f)
        self.check(ink(at(f, math.pi / 2)), "the ring has reached three o'clock")
        self.check(not ink(at(f, math.pi * 1.6)), "and it hasn't closed")
        self.eq(still, "running", "nothing reset yet")
        sleep(0.45)
        st = s.wait_state(lambda v: v["pomo"]["state"] == "idle", what="the reset")
        self.eq(st["hint"], "reset", "and it says so")
        self.touch("up", mid, mid)
        s.settle(0.2)
        f = s.shot("hold-ring-gone")
        self.check(not ink(at(f, math.pi / 2)), "the ring is gone")
        # At rest there's nothing to reset, so no ring. Held past the point of a tap, so that it doesn't start the clock.
        s.cmd("#task ")
        s.cmd("#project 0")
        s.settle(0.2)
        self.touch("down", mid, mid)
        sleep(0.42)
        f = s.frame()
        s.shot("hold-ring-at-rest", f)
        self.check(not ink(at(f, math.pi / 2)), "no ring at rest")
        sleep(0.45)
        self.touch("up", mid, mid)
        s.settle(0.2)
        self.eq(s.state()["pomo"]["state"], "idle", "still at rest")
        s.cmd("#mode timer")

    def status_cluster(self):
        """Bluetooth and battery are on no main screen: the battery is in Settings › Device, and the Mac that's connected
        in Settings › Connectivity."""
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        s.settle()
        f = s.shot("timer-rim-whole")
        mid, last = self.base // 2, self.base - 1
        for y, where in ((12, "twelve"), (last - 12, "six")):
            self.check(f.rgb(mid, y) != (0, 0, 0), f"nothing is set into the rim at {where} o'clock")
        row = lambda v, label: next((r["value"] for r in v["settings"]["rows"] if r["label"] == label), None)
        s.cmd("#mode settings")
        s.wait_state(lambda v: v["settings"].get("rows"), what="the Settings rows")
        self.settings_row("Connectivity")
        st = s.wait_state(lambda v: row(v, "Connected to"), what="Connectivity")
        self.check(row(st, "Connected to") != "No Mac", f"a Mac is connected: {row(st, 'Connected to')}")
        s.key("p")
        s.wait_state(lambda v: v["settings"]["page"] == "main", what="back")
        self.settings_row("Device")
        st = s.wait_state(lambda v: v["settings"]["page"] == "device", what="Device")
        self.check(row(st, "Battery") is not None, "the battery has a row")
        s.settle(0.3)
        s.shot("device")
        s.cmd("#battery 10")
        s.wait_state(lambda v: row(v, "Battery") == "10%", what="10%")
        s.cmd("#battery 80 charging")
        s.wait_state(lambda v: row(v, "Battery") == "80%, charging", what="charging")
        s.settle(0.3)
        s.shot("device-charging")
        s.cmd("#battery off")
        s.cmd("#mode timer")
    def offline(self):
        s = self.s
        self.connect_mac()
        s.cmd("#mode zoo")
        s.cmd("#anim 0.1")
        s.settle()
        s.cmd("#mac off")
        s.settle(0.15)
        st = s.state()
        self.eq(st["link"]["connected"], False, "no Mac")
        self.eq(st["link"]["mac_offline"], True, "Mac offline")
        f = s.shot("zoo-offline")
        y = 286 if self.round else 372 # the line under the activity
        amber = lambda c: c[0] > 200 and 120 < c[1] < 200 and c[2] < 90
        self.check(f.count(self.base // 2 - 60, y - 16, self.base // 2 + 60, y + 10, amber) > 30, "the pill is on the screen")
        s.cmd("#mode timer")
        s.cmd("#task Offline work")
        s.key("s")
        st = s.wait_state(lambda v: v["timer"]["state"] == "running", what="it counts with no Mac")
        self.eq(st["status"], "no Mac: saved for later", "status")
        self.check(st["link"]["outbox"] >= 1, "saved in the outbox")
        s.shot("timer-offline")
        id0 = st["link"]["next_id"]
        s.clear_links()
        sleep(0.3)
        s.cmd("#anim off")
        # Swiping down asks nothing of a Mac that isn't there.
        s.key("p")
        st = s.wait_state(lambda v: v["hint"] == "no Mac", what="no Mac")
        s.key("s")
        st = s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        count = st["link"]["outbox"]
        self.check(count >= 2, "both messages saved")
        s.cmd("#mac on")
        sleep(0.1)
        s.clear_links()
        s.mac({"t": "hello", "v": 4, "host": "MacBook Pro"})
        s.wait_link(lambda m: m.get("t") == "board", what="the board message first")
        self.mac_state()
        msgs = []
        for _ in range(count):
            msgs.append(s.wait_link(lambda m: m.get("t") == "toggl", what="the saved messages"))
        ids = [m["id"] for m in msgs]
        self.eq(ids, sorted(ids), "in order")
        self.eq(msgs[0]["op"], "start", "the start first")
        self.check(any(m["op"] == "stop" for m in msgs), "the stop")
        for m in msgs:
            s.mac({"t": "ack", "id": m["id"]})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="acks clear them")
        s.key("r")
        s.clear_links()

    def outbox(self):
        s = self.s
        # No clock yet: messages wait for it, and "at" is filled in from how long ago each happened.
        self.connect_mac()
        s.cmd("#mode timer")
        s.reboot()
        s.link_usb()
        self.eq(s.state()["link"]["clock_known"], False, "a restart forgets the time")
        s.cmd("#mode timer")
        s.cmd("#mac off")
        for _ in range(3):
            s.key("s")
            s.key("s")
        s.wait_state(lambda v: v["link"]["outbox"] == 6, what="six saved")
        sleep(1.1)
        s.cmd("#mac on")
        s.settle(0.05)
        self.hello()
        sleep(0.3)
        self.check(not any(m.get("t") == "toggl" for m in s.links), "no time from the Mac yet: it holds them")
        self.mac_state([])
        msgs = self.take_toggl(6)
        ids = [m["id"] for m in msgs]
        self.eq(ids, list(range(ids[0], ids[0] + 6)), "in order, and counting up")
        self.eq([m["op"] for m in msgs], ["start", "stop"] * 3, "start, stop")
        self.eq(msgs[0]["rid"], msgs[1]["rid"], "a stretch's start and stop share a rid")
        self.check(msgs[1]["rid"] != msgs[2]["rid"], "each stretch has its own")
        self.check(all(1793826000 - 10 < m["at"] < 1793826000 for m in msgs), f"at, from how long ago: {[m['at'] for m in msgs]}")
        # A second connection before the ack: the same ones again, with the same ids. The app treats those as handled.
        self.hello()
        self.eq([m["id"] for m in self.take_toggl(6)], ids, "sent again, unchanged, until acknowledged")
        for i in ids:
            s.mac({"t": "ack", "id": i})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="acks clear them")
        # An ack for something else is harmless.
        s.mac({"t": "ack", "id": 999999})
        # Saved messages and the id count survive a restart.
        s.cmd("#mac off")
        s.key("s")
        s.key("s")
        st = s.wait_state(lambda v: v["link"]["outbox"] == 2, what="two saved")
        next_id = st["link"]["next_id"]
        self.check(next_id > ids[-1], "ids count on")
        s.reboot()
        st = s.state()
        self.eq(st["link"]["outbox"], 2, "the outbox survives a restart")
        self.check(st["link"]["next_id"] >= next_id, "ids never repeat across a restart")
        s.link_usb()
        self.hello()
        self.mac_state([])
        msgs = self.take_toggl(2)
        self.check(all(m["id"] < next_id for m in msgs), "the saved ones")
        self.check(all(abs(m["at"] - 1793826000) < 30 for m in msgs), f"from before the restart: its best guess at the time: {[m['at'] for m in msgs]}")
        for m in msgs:
            s.mac({"t": "ack", "id": m["id"]})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="all acknowledged")
        s.cmd("#mode timer")
        # Full, with no updates: the oldest of any kind goes, and it says so.
        first_id = s.state()["link"]["next_id"]
        s.cmd("#mac off")
        s.clear_logs()
        for _ in range(40):
            s.key("s")
            s.key("s")
        st = s.wait_state(lambda v: v["link"]["next_id"] == first_id + 80, what="eighty made")
        s.wait_log("feedback bump", timeout=2)
        st = s.state()
        self.eq(st["link"]["outbox"], 64, "at most 64")
        self.eq(st["hint"], "not saved in Toggl", "it says so")
        s.cmd("#mac on")
        s.settle(0.05)
        self.hello()
        self.mac_state([])
        msgs = self.take_toggl(64)
        self.eq([m["id"] for m in msgs], list(range(first_id + 16, first_id + 80)), "the oldest sixteen went")
        for m in msgs:
            s.mac({"t": "ack", "id": m["id"]})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="all acknowledged")
        # Full, with updates in it: the oldest update goes first, and the start stays.
        s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS})
        s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": TASKS})
        s.settle()
        s.cmd("#mac off")
        start_id = s.state()["link"]["next_id"]
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        row = 2
        while s.state()["link"]["outbox"] < 64:
            self.pick_project_row(row)
            row = 5 - row
        self.pick_project_row(row)
        st = s.state()
        self.eq(st["link"]["outbox"], 64, "still 64")
        self.eq(st["link"]["next_id"], start_id + 65, "sixty-five made")
        s.cmd("#mac on")
        s.settle(0.05)
        self.hello()
        self.mac_state([])
        msgs = self.take_toggl(64)
        self.eq(msgs[0]["op"], "start", "the start stays")
        self.eq(msgs[0]["id"], start_id, "its id")
        self.eq(msgs[1]["op"], "update", "then the updates")
        self.eq(msgs[1]["id"], start_id + 2, "the oldest update went")
        self.eq(msgs[-1]["id"], start_id + 64, "the newest stays")
        for m in msgs:
            s.mac({"t": "ack", "id": m["id"]})
        s.wait_state(lambda v: v["link"]["outbox"] == 0, what="all acknowledged")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "paused", what="paused")
        self.drain()

    def persistence(self):
        s = self.s
        self.connect_mac(lock=True)
        # The board's uid: 8 hex digits, the same in `board` before and after a restart.
        s.clear_links()
        s.mac({"t": "hello", "v": 4, "host": "MacBook Pro"})
        uid = s.wait_link(lambda m: m.get("t") == "board", what="board")["uid"]
        self.check(len(uid) == 8 and all(c in "0123456789abcdef" for c in uid), f"uid: {uid!r}")
        self.eq(s.state()["link"]["uid"], uid, "the same in STATE")
        s.cmd("#task Survives a restart")
        s.cmd("#project 104")
        s.cmd("#theme inverted")
        s.cmd("#bright 1")
        s.cmd("#autoscroll on")
        s.cmd("#vol 4")
        s.cmd("#mode pomodoro")
        s.cmd("#set 17")
        s.clear_logs()
        s.wait_log("saved settings", timeout=4)
        st = s.state()
        self.check(st["uptime_ms"] > 0, "saved 2 s after the change")
        s.reboot()
        st = s.state()
        self.eq(st["mode"], "pomodoro", "mode")
        self.eq(st["theme"], "inverted", "profile")
        self.eq(st["pomo"]["set_min"], 17, "length")
        self.eq(st["pomo"]["remaining_s"], 17 * 60, "time")
        self.eq(st["task"], "Survives a restart", "task")
        self.eq(st["project"]["id"], 104, "project")
        self.eq(st["project"]["name"], "Writing", "project name")
        self.eq(st["bright"], 1, "brightness")
        self.eq(st["autoscroll"], True, "auto-scroll")
        self.eq(st["vol"], 4, "volume")
        self.eq(st["lock"]["code_set"], True, "the code's hash")
        self.eq(st["list"]["projects"], 4, "cached projects")
        self.eq(st["list"]["tasks"], 4, "cached tasks")
        self.eq(st["link"]["connected"], False, "the Mac isn't back yet")
        self.eq(st["link"]["uid"], uid, "the uid survives a restart")
        s.settle()
        s.shot("restart-pomodoro-inverted")
        s.key("o")
        st = s.wait_state(lambda v: v["list"]["open"], what="lists work with no Mac")
        s.key("p")
        s.wait_state(lambda v: not v["list"]["open"], what="closed")
        # A locked board stays locked.
        s.cmd("#lock")
        s.reboot()
        st = s.state()
        self.eq(st["lock"]["on"], True, "a locked board stays locked")
        self.eq(st["lock"]["pad"], False, "showing its normal screen")
        s.cmd("#unlock")
        s.cmd("#theme mono")
        s.cmd("#bright 2")
        s.cmd("#autoscroll off")
        s.cmd("#vol 6")
        s.cmd("#set 25")
        s.cmd("#mode timer")
        self.connect_mac()
        s.mac({"t": "lock_code", "clear": True})
        s.settle()

    def lock(self):
        s = self.s
        self.connect_mac(lock=False)
        s.cmd("#task Write the Q4 plan")
        s.cmd("#project 123")
        s.settle()
        # Setting a code doesn't lock.
        s.mac(LOCK_MSG)
        st = s.wait_state(lambda v: v["toast"] == "Lock code updated", what="the code saved")
        self.eq(st["lock"]["code_set"], True, "code set")
        self.eq(st["lock"]["on"], False, "saving a code never locks")
        text = json.dumps(st)
        self.check(LOCK["hash"] not in text and LOCK["salt"] not in text, "STATE holds no hash and no salt")
        s.settle()
        s.shot("toast-lock-code")
        self.menu_choose("lock")
        st = s.wait_state(lambda v: v["lock"]["on"], what="locked")
        self.eq(st["mode"], "timer", "it doesn't change the mode underneath")
        self.eq(st["lock"]["pad"], False, "the normal screen")
        s.settle(0.1)
        s.shot("lock-normal-screen")
        # Clocks keep running, and sound stays off.
        s.clear_logs()
        s.cmd("#unlock")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.cmd("#lock")
        s.settle(1.1)
        st = s.state()
        self.check(st["timer"]["elapsed_s"] >= 1, "clocks keep running")
        s.shot("lock-timer-running")
        # First touch only brings up the keypad, and presses nothing. The menu and Settings stay closed.
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        st = s.wait_state(lambda v: v["lock"]["pad"], what="the keypad")
        self.eq(st["timer"]["state"], "running", "the tap didn't pause the Timer")
        self.eq(st["lock"]["entry_len"], 0, "and pressed no key")
        s.settle(0.1)
        s.shot("lock-keypad")
        # Knob: doesn't enter digits. Its first turn only wakes, later ones bump.
        s.clear_logs()
        s.cmd("#turn 1")
        s.wait_log("feedback bump")
        self.eq(s.state()["lock"]["entry_len"], 0, "the knob doesn't enter digits")
        s.key("u")
        s.settle()
        st = s.state()
        self.eq(st["menu"]["open"], False, "the menu stays closed")
        self.eq(st["mode"], "timer", "and Settings")
        if not self.round:
            s.cmd("#button menu")
            s.settle()
            self.eq(s.state()["mode"], "timer", "the Menu button too")
        # Keys, by position.
        K = (360 / 2 - 72 * 1.5, 96, 72, 54) if self.round else (480 / 2 - 110 * 1.5, 112, 110, 80)
        def key(d):
            pos = {"1": (0, 0), "2": (1, 0), "3": (2, 0), "4": (0, 1), "5": (1, 1), "6": (2, 1), "7": (0, 2), "8": (1, 2), "9": (2, 2), "0": (1, 3), "del": (2, 3)}[d]
            s.cmd(f"#tap {K[0] + K[2] * (pos[0] + .5):.0f} {K[1] + K[3] * (pos[1] + .5):.0f}")
        key("1")
        key("2")
        st = s.state()
        self.eq(st["lock"]["entry_len"], 2, "two digits")
        s.settle()
        s.shot("lock-entry")
        key("del")
        self.eq(s.state()["lock"]["entry_len"], 1, "delete")
        s.key("<")
        s.settle(0.05)
        self.eq(s.state()["lock"]["entry_len"], 0, "a swipe left deletes too")
        # Silence while locked: every feedback says no sound.
        s.clear_logs()
        s.key("r")
        line = s.wait_log("feedback")
        self.check("sound=0" in line, line)
        # A wrong code: it shakes, says so, and clears.
        for d in "9999":
            key(d)
        st = s.wait_state(lambda v: v["lock"]["message"] == "Wrong code", what="wrong code")
        self.eq(st["lock"]["entry_len"], 0, "cleared")
        self.eq(st["lock"]["tries"], 1, "one try")
        s.settle(0.5)
        s.shot("lock-wrong-code")
        # 5 tries, then a wait.
        for i in range(4):
            for d in "9999":
                key(d)
        st = s.wait_state(lambda v: v["lock"]["wait_s"] > 0, what="the wait")
        self.check(st["lock"]["wait_s"] <= 30, "30 s")
        self.check(st["lock"]["message"].startswith("Try again in"), st["lock"]["message"])
        s.settle()
        s.shot("lock-wait")
        key("1")
        self.eq(s.state()["lock"]["entry_len"], 0, "keys do nothing during the wait")
        self.eq(s.state()["lock"]["strikes"], 1, "the first wait")
        # Each wait after that is twice as long: 1 min, then 2, up to 15.
        for want_max, strikes in ((60, 2), (120, 3)):
            s.cmd("#lock_wait_end")
            for i in range(5):
                for d in "9999":
                    key(d)
            st = s.wait_state(lambda v: v["lock"]["wait_s"] > 0, what=f"wait {strikes}")
            self.check(want_max // 2 < st["lock"]["wait_s"] <= want_max, f"wait {strikes}: {st['lock']['wait_s']} s")
            self.eq(st["lock"]["strikes"], strikes, "strikes")
        self.eq(st["lock"]["message"][:13], "Try again in ", "the message")
        self.check(re.match(r"^Try again in \d+:\d\d$", st["lock"]["message"]), f"minutes and seconds: {st['lock']['message']}")
        # A new code from the Mac replaces a forgotten one, also while locked, and ends the wait.
        s.mac(LOCK_MSG)
        st = s.wait_state(lambda v: v["lock"]["wait_s"] == 0, what="a new code clears the wait")
        self.eq(st["lock"]["on"], True, "it stays locked")
        self.eq(st["lock"]["strikes"], 0, "and starts the waits over")
        # 10 s with no input: the keypad goes away and clears its digits.
        key("5")
        s.wait_state(lambda v: not v["lock"]["pad"], timeout=11.5, what="the keypad's timeout")
        st = s.state()
        self.eq(st["lock"]["entry_len"], 0, "cleared")
        self.eq(st["lock"]["on"], True, "still locked")
        # The Mac's agent needing you shows in the keypad's title, with no name.
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        s.wait_state(lambda v: v["lock"]["pad"], what="the keypad")
        agents = [dict(a) for a in SAMPLE_AGENTS]
        agents[1]["st"] = "needs"
        self.mac_state(agents)
        st = s.wait_state(lambda v: v["lock"]["message"] == "An agent needs you", what="an agent needs you")
        self.check("docs-site" not in st["lock"]["message"], "no name while locked")
        s.settle(0.15)
        s.shot("lock-agent-needs")
        self.mac_state()
        # The right code unlocks: 1234, from the vectors, hashed the same way.
        s.clear_logs()
        for d in "1234":
            key(d)
        st = s.wait_state(lambda v: not v["lock"]["on"], what="unlocked")
        self.eq(st["lock"]["pad"], False, "the keypad goes")
        s.wait_log("feedback double sound=1")
        self.eq(st["timer"]["state"], "running", "the Timer still runs")
        s.key("s")
        # Time's up is silent while locked: the alarm plays no sound, vibration stays.
        s.cmd("#mode pomodoro")
        s.cmd("#remaining 2")
        s.key("s")
        s.cmd("#lock")
        s.clear_logs()
        st = s.wait_state(lambda v: v["pomo"]["state"] == "done", timeout=5, what="time's up")
        line = s.wait_log("feedback alarm")
        self.check("sound=0" in line, f"silent: {line}")
        s.settle()
        s.shot("lock-times-up-normal")
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        s.wait_state(lambda v: v["lock"]["pad"], what="the keypad")
        st = s.wait_state(lambda v: v["lock"]["message"] == "Time’s up", what="its title")
        s.settle()
        s.shot("lock-times-up-keypad")
        # Removing the code unlocks.
        s.mac({"t": "lock_code", "clear": True})
        st = s.wait_state(lambda v: not v["lock"]["on"], what="removing the code unlocks")
        self.eq(st["lock"]["code_set"], False, "gone")
        s.key("s")
        s.cmd("#mode timer")
        # A restart keeps the strikes and the wait: switching it off and on doesn't start the waits over.
        s.mac(LOCK_MSG)
        s.wait_state(lambda v: v["lock"]["code_set"], what="a code again")
        s.cmd("#lock")
        s.cmd(f"#tap {self.base // 2} {self.base // 2}")
        s.wait_state(lambda v: v["lock"]["pad"], what="the keypad")
        for _ in range(5):
            for d in "9999":
                key(d)
        s.wait_state(lambda v: v["lock"]["wait_s"] > 0, what="a wait")
        s.reboot()
        st = s.state()
        self.eq((st["lock"]["on"], st["lock"]["strikes"]), (True, 1), "locked, with its strike, after a restart")
        self.check(0 < st["lock"]["wait_s"] <= 30, f"still waiting: {st['lock']['wait_s']} s")
        self.connect_mac(lock=False)
        s.mac({"t": "lock_code", "clear": True})
        s.wait_state(lambda v: not v["lock"]["on"], what="unlocked again")
        s.clear_links()

    def lists(self):
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        # The pill: projects.
        s.key("o")
        st = s.wait_state(lambda v: v["list"]["open"], what="projects")
        self.eq(st["list"]["kind"], "projects", "kind")
        self.eq(st["list"]["idx"], 1, "the cursor opens on No project")
        s.settle(0.25)
        s.shot("list-projects")
        if self.has_knob:
            # A tap without a turn only closes the list.
            s.key("t")
            st = s.wait_state(lambda v: not v["list"]["open"], what="closed")
            self.eq(st["project"]["id"], 0, "unchanged")
            s.key("o")
            s.wait_state(lambda v: v["list"]["open"], what="open")
            s.cmd("#turn 2")
            s.settle(0.3)
            s.shot("list-projects-turned")
            s.key("t")
            st = s.wait_state(lambda v: not v["list"]["open"], what="picked")
            self.eq(st["project"]["name"], "Admin", "turn, then tap")
        else:
            # Row 0 is at y0; Admin is two rows below No project.
            s.cmd("#tap 240 {}".format(66 + 3 * 76 + 38))
            st = s.wait_state(lambda v: not v["list"]["open"], what="picked")
            self.eq(st["project"]["name"], "Admin", "a tap picks a row")
        # The task: past tasks.
        s.key("e")
        st = s.wait_state(lambda v: v["list"]["open"], what="tasks")
        self.eq(st["list"]["kind"], "tasks", "kind")
        s.settle(0.25)
        s.shot("list-tasks")
        if self.has_knob:
            s.cmd("#turn 1")
            s.settle(0.2)
            s.key("t")
        else:
            s.cmd("#tap 240 {}".format(66 + 2 * 76 + 38))
        st = s.wait_state(lambda v: not v["list"]["open"], what="picked")
        self.eq(st["task"], "Write the Q4 plan", "the task")
        self.eq(st["project"]["name"], "Deep work", "its project")
        # While the Timer runs, picking a task is a new entry and the counter keeps going.
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        m = s.wait_link(lambda m: m.get("op") == "start", what="start")
        s.mac({"t": "ack", "id": m["id"]})
        s.key("e")
        s.wait_state(lambda v: v["list"]["open"], what="tasks")
        if self.has_knob:
            s.cmd("#turn 2")
            s.settle(0.2)
            s.key("t")
        else:
            s.cmd("#tap 240 {}".format(66 + 3 * 76 + 38))
        st = s.wait_state(lambda v: not v["list"]["open"], what="picked")
        self.eq(st["timer"]["state"], "running", "keeps running")
        m2 = s.wait_link(lambda x: x.get("op") == "start", what="a new entry")
        self.check(m2["rid"] != m["rid"], "a new stretch")
        s.mac({"t": "ack", "id": m2["id"]})
        # A project picked while it runs goes as an update.
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="projects")
        if self.has_knob:
            s.cmd("#turn 2")
            s.settle(0.2)
            s.key("t")
        else:
            s.cmd("#tap 240 {}".format(66 + 3 * 76 + 38))
        s.wait_state(lambda v: not v["list"]["open"], what="picked")
        m3 = s.wait_link(lambda x: x.get("op") == "update", what="an update")
        self.check("project" in m3, "the project changed")
        self.check("desc" not in m3, "only what changed")
        s.mac({"t": "ack", "id": m3["id"]})
        s.key("s")
        s.clear_links()
        # A task the board started goes to the top, with no request.
        s.send("#list")
        rows = json.loads(s._wait("tasks"))
        s._wait("cmd")
        self.check(len(rows) >= 4, "the list")
        s.clear_links()
        s.cmd("#task A brand new task")
        s.key("s")
        s.wait_state(lambda v: v["timer"]["state"] == "running", what="running")
        s.send("#list")
        rows = json.loads(s._wait("tasks"))
        s._wait("cmd")
        self.eq(rows[0]["desc"], "A brand new task", "to the top of the cached list")
        self.check(not any(m.get("t") == "fetch" for m in s.links), "with no request")
        s.key("s")
        s.clear_links()
        # Sync from Toggl: one fetch, and the list stays open.
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="projects")
        if self.has_knob:
            s.cmd("#turn -1")
            s.settle(0.2)
            s.shot("list-sync-row")
            s.key("t")
        else:
            s.cmd("#tap 240 {}".format(66 + 38))
        m = s.wait_link(lambda x: x.get("t") == "fetch", what="a fetch")
        self.eq(m["what"], "projects", "what")
        st = s.state()
        self.eq(st["list"]["sync"], "syncing", "syncing")
        s.settle()
        s.shot("list-syncing")
        s.mac({"t": "projects", "req": m["req"], "page": 0, "pages": 2, "rows": PROJECTS[:2]})
        s.settle()
        self.eq(s.state()["list"]["projects"], 4, "replaced only when the last page arrives")
        s.mac({"t": "projects", "req": m["req"], "page": 1, "pages": 2, "rows": PROJECTS[2:3]})
        st = s.wait_state(lambda v: v["list"]["sync"] == "idle", what="the sync ending")
        self.eq(st["list"]["projects"], 3, "the new rows")
        self.eq(st["list"]["open"], True, "it stays open")
        self.eq(st["list"]["idx"], 1, "back on No project")
        # The first load comes unasked, with req 0: it updates the cache, and answers nothing.
        def open_sync_row():
            if self.has_knob:
                s.cmd("#turn -1")
                s.settle(0.2)
                s.key("t")
            else:
                s.cmd("#tap 240 {}".format(66 + 38))
            return s.wait_link(lambda x: x.get("t") == "fetch", what="a fetch")
        m = open_sync_row()
        s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS[:2]})
        st = s.wait_state(lambda v: v["list"]["projects"] == 2, what="an unasked load in the cache")
        self.eq(st["list"]["sync"], "syncing", "an unasked load doesn't answer the fetch")
        # An answer in pages, with an unasked load of the same list landing between them, and the other list too.
        s.mac({"t": "projects", "req": m["req"], "page": 0, "pages": 2, "rows": PROJECTS[:1]})
        s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS[:2]})
        s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 2, "rows": TASKS[:1]})
        s.mac({"t": "projects", "req": 0, "page": 1, "pages": 2, "rows": PROJECTS[:1]})
        s.mac({"t": "tasks", "req": 0, "page": 1, "pages": 2, "rows": TASKS[1:2]})
        s.settle()
        st = s.state()
        self.eq(st["list"]["sync"], "syncing", "still waiting for the answer's last page")
        self.eq(st["list"]["tasks"], 2, "the other list, arriving in between")
        s.mac({"t": "projects", "req": m["req"], "page": 1, "pages": 2, "rows": PROJECTS[1:4]})
        st = s.wait_state(lambda v: v["list"]["sync"] == "idle", what="the answer, whole")
        self.eq(st["list"]["projects"], 4, "the answer's rows")
        s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": TASKS})
        # Rows replaced under an open list keep the cursor on what it's on.
        if self.has_knob:
            s.cmd("#turn 3") # No project, Acme, Admin, Deep work
            s.settle(0.1)
            s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": [PROJECTS[2], PROJECTS[0], PROJECTS[3]]})
            st = s.wait_state(lambda v: v["list"]["projects"] == 3, what="an unasked reload")
            self.eq(st["list"]["idx"], 2, "the cursor follows Deep work")
            s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": [PROJECTS[0]]})
            st = s.wait_state(lambda v: v["list"]["projects"] == 1, what="a reload without it")
            self.eq(st["list"]["idx"], 1, "it's gone: back to No project")
            chosen = st["project"]["id"]
            s.key("t")
            st = s.wait_state(lambda v: not v["list"]["open"], what="closed")
            self.eq(st["project"]["id"], chosen, "and a tap only closes, rather than picking some other row")
            s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS})
            s.key("o")
            s.wait_state(lambda v: v["list"]["open"], what="open")
        # A failed fetch says so.
        if self.has_knob:
            s.cmd("#turn -1")
            s.settle(0.2)
            s.key("t")
        else:
            s.cmd("#tap 240 {}".format(66 + 38))
        m = s.wait_link(lambda x: x.get("t") == "fetch", what="another fetch")
        s.mac({"t": "fetch_failed", "req": m["req"], "what": "projects"})
        st = s.wait_state(lambda v: v["list"]["sync"] == "failed", what="the failure")
        s.settle()
        s.shot("list-sync-failed")
        s.key("p")
        s.wait_state(lambda v: not v["list"]["open"], what="closed")
        # Toggl not set up: lists say so.
        self.mac_state(SAMPLE_AGENTS, toggl="no_key")
        s.settle()
        s.key("o")
        st = s.wait_state(lambda v: "set up" in v["hint"], what="no key")
        self.eq(st["list"]["open"], False, "it doesn't open")
        s.shot("timer-toggl-not-set-up")
        self.mac_state()
        s.settle()
        # Dragging a long list, with no knob.
        if not self.has_knob:
            rows = [{"desc": f"Task number {i}", "task_id": 0, "project": PROJECTS[i % 4]} for i in range(20)]
            s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": rows})
            s.settle()
            s.key("e")
            s.wait_state(lambda v: v["list"]["open"], what="open")
            s.settle()
            s.simcmd("touch down 240 400")
            s.simcmd("touch move 240 340")
            s.simcmd("touch move 240 200")
            s.simcmd("touch up 240 200")
            st = s.wait_state(lambda v: v["list"]["scroll"] > 100, what="a drag scrolling the list")
            self.eq(st["list"]["open"], True, "a drag doesn't close it")
            s.settle()
            s.shot("list-scrolled")
            # A tap on a scrolled row picks that row.
            scroll = st["list"]["scroll"]
            row = int((300 - 66 + scroll) / 76)
            s.cmd("#tap 240 300")
            st = s.wait_state(lambda v: not v["list"]["open"], what="a tap on a scrolled row")
            self.eq(st["task"], f"Task number {row - 2}", "the row under the finger")
            # At its top, a pull down closes it.
            s.key("e")
            s.wait_state(lambda v: v["list"]["open"], what="open")
            s.key("p")
            s.wait_state(lambda v: not v["list"]["open"], what="closed by a swipe down at its top")
            s.cmd("#task ")
            s.cmd("#project 0")
        # A list closes by itself after 10 s.
        s.key("o")
        s.wait_state(lambda v: v["list"]["open"], what="open")
        s.wait_state(lambda v: not v["list"]["open"], timeout=11.5, what="closes after 10 s")
        s.mac({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": TASKS})
        s.mac({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS})
        s.clear_links()

    def voice(self):
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        s.settle()
        s.key("m")
        st = s.wait_state(lambda v: v["voice"] == "listening", what="listening")
        sleep(0.9)
        s.shot("voice-listening")
        s.clear_links()
        m = s.wait_link(lambda x: x.get("t") == "voice_start", timeout=12, what="voice_start")
        self.eq(m["rate"], 16000, "rate")
        vid = m["vid"]
        audio = []
        while True:
            x = s.wait_link(lambda v: v.get("t") in ("audio", "voice_end"), timeout=5, what="audio")
            if x["t"] == "voice_end":
                self.eq(x["packets"], len(audio), "packets")
                break
            audio.append(x)
        self.check(len(audio) >= 5, f"audio packets: {len(audio)}")
        # Every packet decodes on its own, and the state in it follows from the one before.
        pcm = []
        p, i = 0, 0
        for k, a in enumerate(audio):
            self.eq(a["seq"], k, "seq")
            self.eq(a["vid"], vid, "vid")
            self.eq((a["p"], a["i"]), (p, i), "the packet's decoder state")
            data = base64.b64decode(a["b64"])
            self.check(a["n"] <= 1600, "packet size")
            out, p, i = adpcm_decode(data, a["n"], p, i)
            pcm += out
        peak = max(abs(v) for v in pcm)
        self.check(peak > 12000, f"normalized: peak {peak}")
        s.settle()
        st = s.state()
        self.eq(st["voice"], "waiting", "waiting")
        self.eq(st["status"], "transcribing…", "status")
        s.shot("voice-transcribing")
        s.mac({"t": "transcript", "vid": vid, "text": "Acme design review", "project": 101})
        st = s.wait_state(lambda v: v["voice"] == "idle", what="the transcript")
        self.eq(st["task"], "Acme design review", "becomes the task")
        self.eq(st["project"]["id"], 101, "naming a project selects it")
        # Locking stops it: the Mac is told, and a late transcript can't change the task.
        s.mac(LOCK_MSG)
        s.key("m")
        s.wait_state(lambda v: v["voice"] == "listening", what="listening")
        m = s.wait_link(lambda x: x.get("t") == "voice_start", timeout=12, what="voice_start")
        s.wait_link(lambda x: x.get("t") == "voice_end", timeout=8, what="voice_end")
        s.cmd("#lock")
        c = s.wait_link(lambda x: x.get("t") == "voice_cancel", what="voice_cancel")
        self.eq(c["vid"], m["vid"], "its vid")
        st = s.state()
        self.eq(st["voice"], "idle", "it stops")
        s.mac({"t": "transcript", "vid": m["vid"], "text": "Too late"})
        s.settle()
        self.eq(s.state()["task"], "Acme design review", "a late transcript changes nothing")
        s.cmd("#unlock")
        s.mac({"t": "lock_code", "clear": True})
        s.settle(0.05)
        # An error, then a stale answer.
        s.key("m")
        s.wait_state(lambda v: v["voice"] == "listening", what="listening")
        m = s.wait_link(lambda x: x.get("t") == "voice_start", timeout=12, what="voice_start")
        s.wait_link(lambda x: x.get("t") == "voice_end", timeout=8, what="voice_end")
        s.mac({"t": "transcript", "vid": m["vid"] + 5, "text": "An old answer"})
        s.settle()
        self.eq(s.state()["voice"], "waiting", "an old answer is ignored")
        s.mac({"t": "transcript", "vid": m["vid"], "error": "no_speech"})
        st = s.wait_state(lambda v: v["voice"] == "idle", what="no speech")
        self.eq(st["hint"], "didn’t catch that", "hint")
        self.eq(st["task"], "Acme design review", "the task stays")
        # With no Mac: it says so.
        s.cmd("#mac off")
        s.settle(0.05)
        s.key("m")
        st = s.wait_state(lambda v: v["hint"] == "no Mac", what="no Mac")
        s.cmd("#mac on")
        s.settle(0.05)
        s.mac({"t": "hello", "v": 4, "host": "MacBook Pro"})
        self.mac_state()
        # Hidden with no key: the layout stays the same, and the tap just starts the clock.
        self.mac_state(SAMPLE_AGENTS, voice=False)
        s.settle(0.1)
        f = s.shot("timer-no-voice-key")
        self.eq(s.state()["link"]["voice_ok"], False, "voice_ok")
        s.cmd(f"#tap {self.base // 2} {282 if self.round else 392}")
        st = s.wait_state(lambda v: v["timer"]["state"] == "running", what="a tap where the mic was")
        s.key("s")
        self.mac_state(SAMPLE_AGENTS, voice=True)
        s.key("r")
        s.clear_links()

    def text(self):
        s = self.s
        self.connect_mac()
        s.cmd("#mode timer")
        # UTF-8 from outside: accents, the box for what no font has, broken bytes. None of it may crash or overrun.
        s.cmd("#task Café résumé Straße")
        s.settle()
        s.shot("text-accents")
        s.cmd("#task 日本語のタスク \U0001F600 emoji")
        s.settle()
        s.shot("text-fallback")
        s.cmd("#task Write a very long task name that goes on and on and on past the end of the line")
        s.settle()
        f1 = s.shot("text-long-resting")
        sleep(3.2)
        f2 = s.shot("text-long-scrolling")
        changed = sum(1 for y in range(self.base // 4, self.base // 2, 2) for x in range(20, self.base - 20, 2) if f1.rgb(x, y) != f2.rgb(x, y))
        self.check(changed > 20, f"a long task scrolls: {changed}")
        s.cmd("#mode pomodoro")
        s.settle()
        s.cmd("#mode timer")
        s.send("#task \xff\xfe broken \xc3")  # (as Latin-1 by way of UTF-8 here: harmless)
        s._wait("cmd", pred=lambda v: v.split(" ")[1] == "task")
        s.sock.sendall(b"#task raw \xff\xfe\xc3 bytes \xe2\x80\n")
        s._wait("cmd", pred=lambda v: v.split(" ")[1] == "task")
        s.settle()
        s.shot("text-broken-bytes")
        st = s.state()
        s.cmd("#task ")
        names = [dict(a) for a in SAMPLE_AGENTS]
        names[0]["name"] = "éèê-日本語-\U0001F916-a-very-long-name-indeed-yes"
        names[0]["act"] = "Café: “quoted” — and so on, for quite a long way"
        self.mac_state(names)
        s.cmd("#mode zoo")
        s.settle(0.2)
        s.shot("text-agent-names")
        self.mac_state()
        s.cmd("#mode timer")

    def fuzz_debug_link(self):
        """Malformed lines through the debug interface's LINK path, after a hello, on every message type."""
        s = self.s
        self.connect_mac(lock=True)
        random.seed(self.board)
        for line in malformed_lines(random.Random(1234)):
            if len(line) > 15000 or "\n" in line:
                continue
            s.send("LINK " + line)
        s.settle(0.3)
        st = s.state()
        self.check(s.alive(), "alive after the fuzz")
        self.eq(st["board"], self.board, "it answers")
        self.connect_mac(lock=False)
        s.mac({"t": "lock_code", "clear": True})
        s.settle()

    def fuzz_tcp(self):
        """The same, and worse, over the real TCP link: raw bytes, 16 KB lines and beyond, no hello at all."""
        s = self.s
        s.cmd("#link ble")
        rng = random.Random(99)
        bad_before = s.state()["link"]["lines_bad"]
        def client():
            c = socket.create_connection(("127.0.0.1", s.link_port), timeout=3)
            c.settimeout(3)
            return c
        c = client()
        sleep(0.15)
        self.eq(s.state()["link"]["connected"], True, "the TCP client")
        # With no hello, nothing is listened to.
        before = s.state()["agents"]
        c.sendall(b'{"t":"agents","rows":[{"id":"x","name":"ignored"}]}\n')
        s.settle(0.15)
        self.eq(s.state()["agents"], before, "ignored before hello")
        c.sendall(b'{"t":"hello","v":4,"host":"Fuzzer"}\n')
        reply = c.recv(4096)
        self.check(b'"t":"board"' in reply, f"board: {reply[:80]}")
        board = json.loads(reply.split(b"\n")[0])
        self.eq(board["name"], self.board, "board name")
        self.eq(board["v"], 4, "protocol version")
        self.eq(board["w"], self.base, "width")
        # Lines exactly at, just over and far over the limit: the rest of a long line is dropped.
        c.sendall(b'{"t":"state","pad":"' + b"x" * (16 * 1024 - 40) + b'"}\n')
        c.sendall(b'{"t":"state","pad":"' + b"x" * (16 * 1024 + 10) + b'"}\n')
        c.sendall(b"y" * (200 * 1024) + b"\n")
        c.sendall(b"[" * 16000 + b"\n")
        c.sendall(b'{"a":' * 3000 + b"\n")
        c.sendall(b'{"t":"state","x":"\x00",' + b'"b":[' * 2000 + b"\n")
        # Random bytes, with and without newlines; partial lines split across writes.
        for _ in range(300):
            n = rng.randint(1, 2000)
            c.sendall(bytes(rng.getrandbits(8) for _ in range(n)) + (b"\n" if rng.random() < 0.7 else b""))
        c.sendall(b"\n")
        for line in malformed_lines(rng):
            data = line.encode("utf-8", "surrogatepass") + b"\n"
            cut = rng.randint(0, len(data))
            c.sendall(data[:cut])
            c.sendall(data[cut:])
            # Keep reading, so that its replies don't fill the socket.
            c.setblocking(False)
            try:
                while c.recv(65536):
                    pass
            except (BlockingIOError, OSError):
                pass
            c.setblocking(True)
            c.settimeout(3)
        sleep(0.5)
        st = s.state()
        self.check(s.alive(), "alive after the fuzz")
        # Drive every screen with whatever state that left, to catch anything stored badly.
        for mode in ("zoo", "timer", "pomodoro", "settings", "zoo"):
            s.cmd(f"#mode {mode}")
            s.settle(0.08)
            s.frame()
        # The link still works afterwards.
        c.sendall(b'{"t":"hello","v":4,"host":"Fuzzer"}\n')
        # All the garbage above is still queued ahead of it, at a few KB a frame.
        c.settimeout(2)
        got = b""
        deadline = time.time() + 60
        while b'"t":"board"' not in got and time.time() < deadline:
            try:
                got += c.recv(65536)
            except socket.timeout:
                pass
        self.check(b'"t":"board"' in got, "still answers a hello")
        bad = s.state()["link"]["lines_bad"] - bad_before
        self.check(bad > 100, f"bad lines are counted, and dropped: {bad}")
        # A client that vanishes in the middle of a line.
        c.sendall(b'{"t":"agents","rows":[{"id":"half')
        c.close()
        s.settle(0.2)
        s.wait_state(lambda v: not v["link"]["connected"], timeout=5, what="the client gone")
        self.check(s.alive(), "alive")
        s.cmd("#mode timer")
        s.cmd("#task ")
        s.cmd("#project 0")

    def no_stale_bands(self):
        st = self.s.state()
        self.eq(st["render"]["verify_fails"], 0, "every frame's bands matched a full repaint")
        self.check(st["render"]["painted"] > 0, "it painted")


def adpcm_decode(data, samples, predictor, index):
    STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
             107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
             876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
             4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
             22385, 24623, 27086, 29794, 32767]
    INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
    out = []
    for byte in data:
        for nib in (byte & 15, byte >> 4):
            if len(out) == samples:
                return out, predictor, index
            step = STEPS[index]
            diff = step >> 3
            if nib & 1:
                diff += step >> 2
            if nib & 2:
                diff += step >> 1
            if nib & 4:
                diff += step
            predictor = predictor - diff if nib & 8 else predictor + diff
            predictor = max(-32768, min(32767, predictor))
            index = max(0, min(88, index + INDEX[nib & 7]))
            out.append(predictor)
    return out, predictor, index


def malformed_lines(rng):
    """Broken, hostile or just odd lines, for every message the board reads."""
    junk = [None, True, False, 0, -1, 1e999, -1e999, 1.5, 2 ** 70, -(2 ** 70), "", "x" * 300, "日本語" * 40, "\ud83d", [], {}, [[]], {"a": {}}, "#ZZZZZZ", "#123", "\u0000", "'\"\\/\b\f\n\r\t"]
    keys = {
        "state": ["seq", "now", "utc", "toggl", "voice", "n"],
        "agents": ["page", "pages", "rows", "gone"],
        "ack": ["id"],
        "entry_status": ["rid", "status"],
        "projects": ["req", "page", "pages", "rows"],
        "tasks": ["req", "page", "pages", "rows"],
        "fetch_failed": ["req", "what"],
        "sync_result": ["req", "result", "rid", "desc", "task_id", "project", "start"],
        "transcript": ["vid", "text", "project", "error"],
        "lock_code": ["salt", "iter", "hash", "clear"],
        "hello": ["v", "host"],
        "clock": ["rev", "from", "seq", "for", "now", "at", "task", "timer", "pomo"],
    }
    agent_keys = ["id", "name", "proj", "kind", "st", "act", "note", "age", "up", "rex", "where", "agent", "subs"]
    def dumps(o):
        return json.dumps(o, ensure_ascii=rng.random() < 0.5, allow_nan=True)
    out = []
    for t, ks in keys.items():
        for k in ks:
            for j in junk:
                out.append(dumps({"t": t, k: j}))
        out.append(dumps({"t": t}))
        for _ in range(6):
            msg = {"t": t}
            for k in ks:
                msg[k] = rng.choice(junk)
            out.append(dumps(msg))
    for k in agent_keys:
        for j in junk:
            out.append(dumps({"t": "agents", "rows": [{"id": "abc", k: j}]}))
    for j in junk:
        out.append(dumps({"t": "agents", "rows": [j, j], "gone": [j, j]}))
        out.append(dumps({"t": "agents", "page": j, "pages": j, "rows": [{"id": "abc"}]}))
        out.append(dumps({"t": "agents", "rows": [{"id": "abc", "subs": [j, {"id": j, "type": j, "act": j}]}]}))
        out.append(dumps({"t": "projects", "req": 1, "page": 0, "pages": 1, "rows": [j, {"id": j, "name": j, "color": j}]}))
        out.append(dumps({"t": "tasks", "req": 1, "page": 0, "pages": 1, "rows": [j, {"desc": j, "task_id": j, "project": j}]}))
        out.append(dumps({"t": "sync_result", "req": 1, "result": "adopted", "rid": j, "desc": "x", "project": j, "start": j}))
    # The clocks: every field broken in turn, inside a message that's otherwise good, and times far apart or far off.
    good_clock = lambda: {"t": "clock", "rev": 1, "from": "5be0e1d2", "seq": 1, "now": 1793826000000, "at": 1793825990000,
                          "task": {"desc": "x", "task_id": 0, "project": {"id": 1, "name": "p", "color": "#123456"}},
                          "timer": {"st": "running", "acc": 0, "start": 1793825000000, "rid": 5, "of": "5be0e1d2"},
                          "pomo": {"st": "running", "set": 25, "total": 1500000, "left": 60000, "rid": 6, "of": "5be0e1d2"}}
    for j in junk:
        for part, keys_ in (("task", ["desc", "task_id", "project"]), ("timer", ["st", "acc", "start", "rid", "of"]),
                            ("pomo", ["st", "set", "total", "left", "rid", "of"])):
            for k in keys_:
                m = good_clock()
                m[part][k] = j
                out.append(dumps(m))
        m = good_clock()
        m["task"]["project"] = {"id": j, "name": j, "color": j}
        out.append(dumps(m))
    for now, at, start in ((9000000000000000, -9000000000000000, -9000000000000000), (-9000000000000000, 9000000000000000, 0),
                           (1793826000000, -9000000000000000, -9000000000000000), (1793826000000, 9e15, 9e15), (0, 0, 0)):
        m = good_clock()
        m["now"], m["at"], m["timer"]["start"] = now, at, start
        m["timer"]["acc"] = 9e15
        out.append(dumps(m))
    # Too many of everything.
    out.append(dumps({"t": "agents", "rows": [{"id": f"a{i}", "name": "n" * 500, "act": "a" * 500, "subs": [{"id": "s", "type": "t" * 200, "act": "a" * 200}] * 50} for i in range(30)]})[:15000])
    out.append(dumps({"t": "agents", "rows": [{"id": f"a{i}", "name": "n", "st": "needs"} for i in range(200)]}))
    out.append(dumps({"t": "projects", "req": 1, "page": 0, "pages": 1, "rows": [{"id": i + 1, "name": "p" * 80, "color": "#123456"} for i in range(100)]}))
    out.append(dumps({"t": "tasks", "req": 1, "page": 0, "pages": 1, "rows": [{"desc": "d" * 200, "task_id": i} for i in range(100)]}))
    out.append(dumps({"t": "projects", "req": 2, "page": 5, "pages": 9, "rows": []}))
    out.append(dumps({"t": "projects", "req": 2, "page": 0, "pages": 1000000, "rows": []}))
    out.append(dumps({"t": "lock_code", "salt": "00", "iter": 10 ** 9, "hash": "aa" * 32}))
    out.append(dumps({"t": "lock_code", "salt": "zz", "iter": 2000, "hash": "aa" * 32}))
    out.append(dumps({"t": "lock_code", "salt": "aa" * 200, "iter": 2000, "hash": "aa" * 32}))
    out.append(dumps({"t": "lock_code", "salt": "aa", "iter": 2000, "hash": "aa" * 31}))
    # Not objects, not JSON, half of JSON.
    out += ["", " ", "null", "true", "1", "[]", "{}", '"text"', "{", "}", "[", "]", '{"t"', '{"t":', '{"t":"state"', '{"t":"state",', "not json at all", "日本語", '{"t":"state","agents":[{', '{"t":"\\ud83d"}', '{"t":"state","now":1e400}', '{"t":"\\u0000state"}', '{"t":1}', '{"t":null}', '{"t":{"t":"state"}}']
    out += ["[" * 40, "{" * 40, '{"a":' * 40, "[" * 12 + "]" * 12, "[" * 13 + "]" * 13]
    rng.shuffle(out)
    return out


CASES = ["boot", "auto_scroll", "timer", "sync", "clocks", "clock_late", "clock_unsent", "clock_edges", "dayclock", "split", "split_more", "pomodoro", "hold_ring", "menu", "timeouts", "settings", "macs", "zoo", "crowd", "needs", "effects", "inverted", "rotation", "burn_in", "perf", "steady", "status_cluster", "offline", "outbox", "lists", "text", "lock", "persistence", "voice", "busy", "fuzz_debug_link", "fuzz_tcp"]


def make_speech_wav(path):
    """Two seconds of something voice-like, after a little room noise, then quiet: enough for the mic's tracker."""
    rng = random.Random(7)
    rate = 16000
    samples = []
    for i in range(int(rate * 4.5)):
        t = i / rate
        noise = rng.randint(-8, 8)
        if 0.4 < t < 2.4:
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 3 * t)
            v = 2500 * envelope * (math.sin(2 * math.pi * 220 * t) + 0.6 * math.sin(2 * math.pi * 660 * t) + 0.3 * math.sin(2 * math.pi * 1400 * t))
            samples.append(int(v) + noise)
        else:
            samples.append(noise)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def run_board(board, binary, base_port, only, out_root, tmp, panel_file=None):
    print(f"\n=== {board} ===", flush=True)
    wav = os.path.join(tmp, "speech.wav")
    make_speech_wav(wav)
    errlog = os.path.join(tmp, f"stderr-{board}.log")
    if os.path.exists(errlog):
        os.unlink(errlog)
    s = Sim(binary, board, os.path.join(out_root, board), base_port, os.path.join(tmp, f"store-{board}.bin"), wav=wav, log_path=errlog,
            panel_file=panel_file)
    s.start()
    c = Case(s, board)
    results = []
    s.cmd("#verify on")
    try:
        for name in CASES:
            if only and name not in only:
                continue
            t0 = time.time()
            start_passes = c.passes
            try:
                if name not in ("boot",):
                    c.reset()
                getattr(c, name)()
                c.no_stale_bands()
                print(f"PASS {name:<16} {c.passes - start_passes:>3} checks  {time.time() - t0:5.1f} s", flush=True)
                results.append((name, True, ""))
            except Exception as e:  # noqa: BLE001: report it, and go on if the simulator is still up
                detail = f"{type(e).__name__}: {e}"
                print(f"FAIL {name:<16} {detail}", flush=True)
                if not isinstance(e, Failed):
                    traceback.print_exc()
                results.append((name, False, detail))
                if not s.alive():
                    print(f"     the simulator exited with {s.proc.poll()}", flush=True)
                    break
                # Clear any half-done state before the next case.
                try:
                    s.cmd("#unlock")
                except Exception:  # noqa: BLE001
                    pass
    finally:
        s.stop()
    errtext = open(errlog, errors="replace").read() if os.path.exists(errlog) else ""
    bad = [ln for ln in errtext.splitlines() if "ERROR: AddressSanitizer" in ln or "runtime error:" in ln or "SUMMARY:" in ln]
    if bad:
        print("SANITIZER OUTPUT:\n" + errtext[:6000], flush=True)
        results.append(("sanitizers", False, bad[0]))
    else:
        print("PASS sanitizers       no errors in the simulator's output", flush=True)
        results.append(("sanitizers", True, ""))
    return results, c.passes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="append")
    ap.add_argument("--only", help="comma-separated cases")
    ap.add_argument("--plain", action="store_true", help="the usual build, not the sanitized one")
    ap.add_argument("--out", default=os.path.join(SIMDIR, "out"))
    ap.add_argument("--show", action="store_true", help="show every run in one window, the device harness's viewer")
    args = ap.parse_args()
    binary = os.path.join(SIMDIR, "build", "bento-sim" if args.plain else "bento-sim-asan")
    if not os.path.exists(binary):
        sys.exit(f"build it first: make -C firmware/sim {'all' if args.plain else 'asan'}")
    only = set(args.only.split(",")) if args.only else None
    boards = args.board or ["knob-lcd-1.8", "amoled-2.16"]
    all_results = []
    total = 0
    # The protocol's test values.
    print("=== protocol vectors ===", flush=True)
    # test_vectors_utf8.c carries a copy of link.c's utf8_copy, so the vectors build without the HAL: it must be the same.
    def utf8_copy_of(path):
        text = open(path).read()
        start = text.index("void utf8_copy(")
        return " ".join(text[start:text.index("\n}\n", start)].split())
    same = utf8_copy_of(os.path.join(SIMDIR, "tests", "test_vectors_utf8.c")) == utf8_copy_of(os.path.join(SIMDIR, "..", "link", "link.c"))
    print(("PASS" if same else "FAIL") + " utf8_copy        test_vectors_utf8.c's copy matches link/link.c", flush=True)
    all_results.append(("utf8_copy copy", same, "" if same else "test_vectors_utf8.c's utf8_copy differs from link/link.c"))
    r = subprocess.run(["make", "-s", "-C", SIMDIR, "build/test_vectors"], capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr)
        all_results.append(("vectors build", False, r.stderr[:200]))
    else:
        r = subprocess.run([os.path.join(SIMDIR, "build", "test_vectors"), VECTORS], capture_output=True, text=True, env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
        print(r.stdout + r.stderr, end="")
        all_results.append(("vectors", r.returncode == 0, ""))
    panel_file = None
    if args.show:
        # One window for every board and every restart, as tools/device_test.py --show has it. A second viewer on the
        # same file just exits. The cases run at 1x, so the window is softer than `make run`'s.
        panel_file = os.path.join(ROOT, "test-output", ".sim-panel")
        os.makedirs(os.path.dirname(panel_file), exist_ok=True)
        viewer = os.path.join(SIMDIR, "build", "bento-sim-sharp")
        if not os.path.exists(viewer):
            sys.exit("build the viewer first: make -C firmware/sim sharp")
        subprocess.Popen([viewer, "--view", panel_file], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="bento-sim-") as tmp:
        for i, board in enumerate(boards):
            results, passes = run_board(board, binary, 7800 + i * 10, only, args.out, tmp, panel_file=panel_file)
            all_results += [(f"{board}: {n}", ok, d) for n, ok, d in results]
            total += passes
    failed = [r for r in all_results if not r[1]]
    print(f"\n{len(all_results) - len(failed)} of {len(all_results)} cases passed, {total} checks. Screenshots are in {args.out}.", flush=True)
    for n, _, d in failed:
        print(f"  FAILED {n}: {d}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
