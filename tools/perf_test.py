#!/usr/bin/env python3
"""Performance tests for Bento: the same user actions every time, each one timed by the firmware itself.

Run it through tools/bento:

    tools/bento perf sim                          # the simulator, both boards
    tools/bento perf sim --board knob-lcd-1.8
    tools/bento perf knob-lcd-1.8                 # a board running a debug build (tools/bento flash knob-lcd-1.8 --debug)
    tools/bento perf knob-lcd-1.8 --baseline      # this run becomes the baseline that later runs are compared with
    tools/bento perf sim --only list --runs 3     # the actions whose names start with "list", three times each
    tools/bento perf sim --list                   # the actions, and nothing run

Each action starts from the same screen, set up through the debug interface, and is then done as a person would: a
swipe, a tap, a knob step or a drag, or a message from the Mac. The firmware times it from that input ("#perf" in
docs/DEBUG.md):

    response   from the input to the end of the first frame that shows a change: what you feel as lag
    settled    to the end of the last frame that changed, once 300 ms pass with nothing new: how long it animates
    worst      the slowest single frame in that time
    work       every frame's time added up: how busy the board was
    bands      24-row strips repainted: how much of the screen changed
    stall      the longest time between two frames starting. A frame is due every 20 ms, so much more means the
               board was held up

Each action runs several times, and the report shows the medians. A run is compared with the target's baseline: a time
that grew by more than 25 % (and by more than a few milliseconds) is slower, and so is a count of bands or frames that
grew by more than 10 %. On a board, times over the budgets (a 100 ms response, a 45 ms frame) are marked too.

Where results go:
    perf/baselines/<target>.json     the baseline, written by --baseline
    perf/history/<target>.jsonl      one line per run, to follow a trend
    test-output/perf/<target>.json   every sample of the last run (git-ignored)

The simulator's times are the Mac's, many times faster than a board's, and they move with whatever else the Mac is doing.
Compare them only with a baseline from the same Mac. Bands and frames are the firmware's own, and carry over.

On a board, the harness stands in for the Mac over USB. It answers the board's clock changes and acknowledges its Toggl
messages, so nothing it does reaches Toggl or the other boards. It uses the board's own lists and lock code, and never
sends new ones. At the end it resets the clocks it started, switches the board back to Bluetooth and restarts it, so the
board takes the real Mac's clocks again, your running timer included.
"""
import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import device_test as dt  # noqa: E402  (the debug interface's transport, shared with the device tests)

PERF_DIR = os.path.join(ROOT, "perf")
OUT_DIR = os.path.join(ROOT, "test-output", "perf")
VECTORS = os.path.join(ROOT, "protocol", "samples", "vectors.json")
BOARDS = ("knob-lcd-1.8", "amoled-2.16")

QUIET_MS = 300      # an action has settled when this long passes with nothing repainted
WINDOW_S = 3.0      # an action still animating after this long is cut off there
BEFORE_S = 1.5      # at most this long waiting for the screen to stop changing before an action
TIME_TOL, COUNT_TOL = 0.25, 0.10
FLOOR_MS = {"sim": 1.0, "board": 3.0}  # smaller changes than this are noise, whatever the percentage
COUNT_FLOOR = 2
BUDGET_MS = {"first_ms": 100, "frame_ms_max": dt.FRAME_BUDGET_MS}  # on a board only

# The report's columns: (heading, the PERF field, unit). Durations in ms, counts without a unit.
METRICS = [
    ("response", "first_ms", "ms"),
    ("settled", "settle_ms", "ms"),
    ("worst", "frame_ms_max", "ms"),
    ("work", "work_ms", "ms"),
    ("bands", "bands", ""),
    ("frames", "painted", ""),
    ("stall", "gap_ms_max", "ms"),
]

# What the stand-in Mac sends: about what a real day looks like.
PROJECT_NAMES = ["Acme", "Admin", "Bentobox", "Deep work", "Errands", "Finance", "Hiring", "Home", "Learning", "Meetings",
                 "Open source", "Reading", "Support", "Writing"]
COLORS = ["#F2C14E", "#9CA3AF", "#4FB3F6", "#E86FA6", "#7BD389", "#FF8A5B", "#B39DDB"]
PROJECTS = [{"id": 1000 + i, "name": n, "color": COLORS[i % len(COLORS)]} for i, n in enumerate(PROJECT_NAMES)]
TASKS = [{"desc": d, "task_id": 0, "project": PROJECTS[i % len(PROJECTS)]} for i, d in enumerate([
    "Write the Q4 plan", "Acme design review", "Inbox", "Fix the flaky test", "Weekly review", "Read the RFC",
    "Interview prep", "Answer support tickets", "Pay the invoices", "Draft the blog post", "Plan the sprint",
    "Refactor the parser", "Call the bank", "Update the roadmap", "Clean the backlog", "Release notes",
    "Prepare the demo", "Groceries", "Expense report", "One-on-one"])]
STATES = ["working", "working", "idle", "done", "working"]  # none that need you: that pulses (idle.needs measures it)
AGENTS = [{"id": f"{0xa0000000 + i * 7919:08x}", "name": f"agent-{i:02d}", "proj": PROJECT_NAMES[i % 14].lower(),
           "kind": "i" if i % 3 else "b", "st": STATES[i % len(STATES)], "act": "Editing main.c", "age": 30 + i,
           "up": 600 + i * 60, "pane": 1} for i in range(30)]


# ---- The stand-in Mac -------------------------------------------------------------------------------------------------

class FakeMac:
    """The desktop app's side of the link, over the debug interface's LINK lines: it answers the board as the app would,
    but keeps everything to itself."""

    def __init__(self, dev):
        self.dev = dev
        self.rev = 0
        self.seq = 0
        self.uid = None
        self.fw = None
        self.board = threading.Event()
        self.lock = threading.Lock()
        dev.on_link = self._from_board

    def send(self, obj):
        self.dev.send("LINK " + json.dumps(obj, ensure_ascii=False) + "\n")

    def _from_board(self, text):
        try:
            m = json.loads(text)
        except ValueError:
            return
        t = m.get("t")
        if t == "board":
            self.uid, self.fw = m.get("uid"), m.get("fw")
            self.board.set()
        elif t == "toggl":
            self.send({"t": "ack", "id": m["id"]})  # taken, and kept here: nothing reaches Toggl
        elif t == "clock":
            self.send(self.clock_answer(m))
        elif t == "resync":
            self.send_agents()
        elif t == "sync":
            self.send({"t": "sync_result", "req": m.get("req", 0), "result": "in_sync"})
        elif t == "fetch":
            self.send({"t": "fetch_failed", "req": m.get("req", 0), "what": m.get("what", "")})

    def clock_answer(self, m):
        """The board's own change, taken: as the app answers it, with the times moved to the Mac's clock."""
        with self.lock:
            self.rev += 1
            rev = self.rev
        mac_now = int(time.time() * 1000)
        shift = mac_now - int(m.get("now", 0))
        timer, pomo = dict(m.get("timer") or {}), dict(m.get("pomo") or {})
        if "start" in timer:
            timer["start"] = int(timer["start"]) + shift
        for run in (timer, pomo):
            run["of"] = run.get("of") or self.uid
        return {"t": "clock", "rev": rev, "from": self.uid, "seq": m.get("seq", 0), "for": m.get("seq", 0), "now": mac_now,
                "at": int(m.get("at", m.get("now", 0))) + shift, "task": m.get("task"), "timer": timer, "pomo": pomo}

    def another_board(self, task):
        """A change made on another board, as the app passes it on."""
        with self.lock:
            self.rev += 1
            rev = self.rev
        now = int(time.time() * 1000)
        idle = {"st": "idle", "acc": 0, "rid": 0, "of": "0badc0de"}
        return {"t": "clock", "rev": rev, "from": "0badc0de", "seq": rev, "now": now, "at": now, "task": task,
                "timer": idle, "pomo": {"st": "idle", "set": 25, "total": 1500000, "left": 1500000, "rid": 0, "of": "0badc0de"}}

    def state(self):
        self.seq += 1
        return {"t": "state", "v": 4, "seq": self.seq, "now": int(time.time()), "utc": -time.timezone, "toggl": "ok",
                "voice": True, "n": len(AGENTS)}

    def send_agents(self):
        self.send({"t": "agents", "page": 0, "pages": 1, "rows": AGENTS})

    def connect(self, with_lists):
        self.dev.cmd("link usb")
        self.send({"t": "hello", "v": 4, "host": "Bento perf"})
        if not self.board.wait(8):
            raise dt.Failed("the board didn't answer hello over USB")
        self.send(self.state())
        self.send_agents()
        if with_lists:
            self.send({"t": "projects", "req": 0, "page": 0, "pages": 1, "rows": PROJECTS})
            self.send({"t": "tasks", "req": 0, "page": 0, "pages": 1, "rows": TASKS})


# ---- A target: the simulator at one board's size, or a board -----------------------------------------------------------

class Target:
    def __init__(self, a, board, out):
        self.a, self.board, self.out = a, board, out
        self.kind = "sim" if a.target == "sim" else "board"
        self.name = f"sim-{board}" if self.kind == "sim" else board
        self.proc = None
        self.dev = None
        self.mac = None
        self.st = {}
        self.start_lock = False

    def start(self):
        os.makedirs(self.out, exist_ok=True)
        log = os.path.join(self.out, f"{self.name}-device.log")
        if self.kind == "sim":
            work = tempfile.mkdtemp(prefix="bento-perf-")
            debug_port = dt.free_port()
            cmd = [dt.SIM_BIN, "--board", self.board, "--headless", "--store", os.path.join(work, "store.bin"),
                   "--link-port", str(dt.free_port()), "--debug-port", str(debug_port)]
            self.proc = subprocess.Popen(cmd, stdout=open(os.path.join(self.out, f"{self.name}-sim.log"), "w"), stderr=subprocess.STDOUT)
            port = dt.TcpPort(debug_port)
        else:
            port = dt.SerialPort(self.a.port or dt.pick_port())
        self.dev = dt.Device(port, log)
        self.dev.start()
        if not self.dev.wait(r"^LOG boot", 8, since=0):
            self.dev.send("#reboot\n")  # the port opened without a reset: start from a fresh boot
            self.dev.wait(r"^LOG boot", 30, since=0)
        time.sleep(0.5)
        if not self.dev.cmd("perf stop", expect=None):
            raise dt.Failed("no #perf on this board: flash a debug build of the current firmware "
                            f"(tools/bento flash {self.board} --debug), and reflash an everyday one afterwards")
        st = self.dev.state()
        if st["board"] != self.board:
            raise dt.Failed(f"this is {st['board']}, not {self.board}")
        self.start_lock = st["lock"]["on"]
        if st["lock"]["on"]:
            self.dev.cmd("unlock")
        self.mac = FakeMac(self.dev)
        self.mac.connect(with_lists=self.kind == "sim")  # a board keeps its own lists: new ones would stay after the run
        if self.kind == "sim":
            lock = json.load(open(VECTORS))["lock"]
            self.mac.send({"t": "lock_code", "salt": lock["salt"], "iter": lock["iter"], "hash": lock["hash"]})
        time.sleep(0.5)
        self.refresh()

    def refresh(self):
        self.st = self.dev.state()
        return self.st

    @property
    def round(self):
        return self.st.get("shape") == "round"

    @property
    def knob(self):
        return bool(self.st.get("has", {}).get("knob"))

    def settle_link(self, timeout=4):
        """Until the board has nothing waiting for an answer: no Toggl message and no clock change on its way."""
        end = time.time() + timeout
        while time.time() < end:
            st = self.refresh()
            if st["link"]["outbox"] == 0 and not st["sync"]["pending"]:
                return st
            time.sleep(0.1)
        return self.st

    def screen_still(self):
        """Until a frame passes with nothing repainted, or BEFORE_S if the screen animates without end."""
        end = time.time() + BEFORE_S
        last = self.refresh()["render"]["painted"]
        while time.time() < end:
            time.sleep(0.15)
            now = self.refresh()["render"]["painted"]
            if now == last:
                return
            last = now

    def finish(self):
        """Leaves the board as the harness found it, as far as the real Mac can't put it back by itself."""
        if not self.dev:
            return
        try:
            if self.kind == "board":
                d = self.dev
                d.cmd("unlock", expect=None)
                for mode, field in (("timer", "timer"), ("pomodoro", "pomo")):
                    d.cmd(f"mode {mode}", expect=None)
                    if self.refresh()[field]["state"] != "idle":
                        d.keys("r", gap=0.3)  # the hold that resets it, so no run of the harness is left going
                self.settle_link()
                d.cmd("mode timer", expect=None)
                if self.start_lock:
                    d.cmd("lock", expect=None)
                d.send("#link ble\n")
                time.sleep(0.2)
                d.send("#reboot\n")  # a fresh connection, so the board takes the real Mac's clocks again
                time.sleep(0.5)
        finally:
            self.dev.close()
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()


# ---- The actions ------------------------------------------------------------------------------------------------------

class Action:
    """setup: brings the board to the starting screen (not timed). act: the input, as text for the debug interface, or a
    list of texts and pauses (seconds) for a gesture spread over time. expect: what the state must show afterwards, so an
    action that did nothing can't pass for a fast one. needs: "knob", "touch-only" or "lock". animates: the screen never
    stops changing there (the agents move), so the action is timed over a fixed window instead of until it settles.
    after: puts back what the action changed, such as a lock, before the next one."""

    def __init__(self, key, title, setup, act, expect, needs=None, animates=False, window=WINDOW_S, after=None):
        self.key, self.title, self.setup, self.act, self.expect = key, title, setup, act, expect
        self.needs, self.animates, self.window, self.after = needs, animates, window, after

    def applies(self, t):
        if self.key.startswith("list.") and not (t.st["list"]["projects"] > 5 and t.st["list"]["tasks"] > 5):
            return False  # a board with hardly any lists yet: they come from Toggl
        if self.needs == "knob":
            return t.knob
        if self.needs == "touch-only":
            return not t.knob
        if self.needs == "lock":
            return t.st["lock"]["code_set"]
        return True


def menu_spot(t, key):
    """Somewhere inside a mode's compartment, in layout units: as in the simulator tests."""
    if t.round:
        a = (["timer", "pomodoro", "zoo", "clock", "lock", "settings"].index(key) - 0.5) * 2 * math.pi / 6
        return 180 + 132 * math.sin(a), 180 - 132 * math.cos(a)
    return {"timer": (124, 86), "pomodoro": (355, 86), "zoo": (124, 240), "clock": (355, 240), "settings": (124, 394),
            "lock": (355, 394)}[key]


def tap(t, key):
    x, y = menu_spot(t, key)
    return f"#tap {x:.0f} {y:.0f}\n"


def go(t, mode):
    t.dev.cmd(f"mode {mode}")
    t.dev.cmd("quiet")


def clock_state(t, mode, field, want):
    """The Timer or the Pomodoro, idle, running or paused."""
    go(t, mode)
    for _ in range(4):
        st = t.refresh()[field]["state"]
        if st == want:
            break
        t.dev.keys("r" if want == "idle" else "s", gap=0.3)  # a hold resets; a tap goes idle, running, paused, running…
        t.settle_link()
    else:
        raise dt.Failed(f"the {mode} never got to {want}")
    t.settle_link()
    t.dev.cmd("quiet")


def menu_open_on(t, mode):
    go(t, mode)
    t.dev.keys("u", gap=0.1)
    t.dev.wait_state(lambda s: s["menu"]["open"], 4, "the menu")


def list_open(t, kind):
    clock_state(t, "timer", "timer", "idle")
    t.dev.keys("o" if kind == "projects" else "e", gap=0.1)
    t.dev.wait_state(lambda s: s["list"]["open"] and s["list"]["kind"] == kind, 4, f"the {kind} list")


def steps(key, n, gap=0.04):
    """n knob detents, gap seconds apart: a quick turn."""
    out = []
    for i in range(n):
        out += [key] + ([gap] if i < n - 1 else [])
    return out


def drag(t, dy):
    """A finger drag up the middle of the screen, in layout units."""
    m = t.st["base"] / 2
    return f"#swipe {m:.0f} {m + dy / 2:.0f} {m:.0f} {m - dy / 2:.0f}\n"


def settings_main(t):
    go(t, "settings")
    for _ in range(3):  # back out of a group page
        if t.refresh()["settings"]["page"] == "main":
            break
        t.dev.keys("p", gap=0.3)
    if t.knob and t.st["settings"]["key"]:
        t.dev.cmd(f"turn {-t.st['settings']['key']}")  # the first row, Device
        time.sleep(0.3)


def build_actions():
    A = Action
    idx = {}

    def remember(t):
        idx["list"] = t.refresh()["list"]["idx"]
        idx["sel"] = t.st["sel"]
        idx["set"] = t.st["pomo"]["set_min"]
        idx["key"] = t.st["settings"]["key"]
        idx["scroll"] = t.st["list"]["scroll"]

    def then(setup):
        def run(t):
            setup(t)
            remember(t)
        return run

    def needs_one(t):
        go(t, "timer")
        t.mac.send({"t": "agents", "rows": [dict(AGENTS[0], st="needs", act="Waiting for you")]})

    def last_minute(t):
        clock_state(t, "pomodoro", "pomo", "idle")
        t.dev.cmd("set 25")
        t.dev.keys("s", gap=0.3)
        t.dev.cmd("remaining 40")  # the last minute pulses: the heaviest clock screen
        t.settle_link()

    def pomo_25(t):
        clock_state(t, "pomodoro", "pomo", "idle")
        t.dev.cmd("set 25")  # each run from the same length: a drag moves ten minutes, and 60 is the most
        time.sleep(0.2)

    def tick(t):
        return "LINK " + json.dumps(t.mac.state()) + "\n"  # a harmless message, to start the clock

    return [
        # Two seconds of screens that change by themselves: the work a board does while nobody touches it.
        A("idle.timer", "Two seconds of a running Timer", then(lambda t: clock_state(t, "timer", "timer", "running")), tick,
          lambda s: s["timer"]["state"] == "running", animates=True, window=2.0),
        A("idle.needs", "Two seconds of the Timer while an agent needs you", then(needs_one), tick,
          lambda s: s["n_needing"] >= 1, animates=True, window=2.0, after=lambda t: t.mac.send_agents()),
        A("idle.agents", "Two seconds of the Agents screen", then(lambda t: go(t, "zoo")), tick,
          lambda s: s["mode"] == "zoo", animates=True, window=2.0),
        A("idle.last_minute", "Two seconds of a Pomodoro's last minute", then(last_minute), tick,
          lambda s: s["pulsing"], animates=True, window=2.0, after=lambda t: clock_state(t, "pomodoro", "pomo", "idle")),
        A("menu.open", "Swipe up for the menu", then(lambda t: go(t, "timer")), lambda t: "u",
          lambda s: s["menu"]["open"]),
        A("menu.close", "Swipe down to close the menu", then(lambda t: menu_open_on(t, "timer")), lambda t: "p",
          lambda s: not s["menu"]["open"]),
        A("menu.timer", "Tap Timer in the menu", then(lambda t: menu_open_on(t, "clock")), lambda t: tap(t, "timer"),
          lambda s: s["mode"] == "timer" and not s["menu"]["open"]),
        A("menu.agents", "Tap Agents in the menu", then(lambda t: menu_open_on(t, "timer")), lambda t: tap(t, "zoo"),
          lambda s: s["mode"] == "zoo", animates=True, window=1.5),
        A("menu.clock", "Tap Clock in the menu", then(lambda t: menu_open_on(t, "timer")), lambda t: tap(t, "clock"),
          lambda s: s["mode"] == "clock"),
        A("menu.settings", "Tap Settings in the menu", then(lambda t: menu_open_on(t, "timer")), lambda t: tap(t, "settings"),
          lambda s: s["mode"] == "settings"),
        A("timer.start", "Tap to start the Timer", then(lambda t: clock_state(t, "timer", "timer", "idle")), lambda t: "s",
          lambda s: s["timer"]["state"] == "running"),
        A("timer.pause", "Tap to pause the Timer", then(lambda t: clock_state(t, "timer", "timer", "running")), lambda t: "s",
          lambda s: s["timer"]["state"] == "paused"),
        A("timer.reset", "Hold to reset the Timer", then(lambda t: clock_state(t, "timer", "timer", "paused")), lambda t: "r",
          lambda s: s["timer"]["state"] == "idle"),
        A("list.projects.open", "Tap the project to open the list", then(lambda t: clock_state(t, "timer", "timer", "idle")),
          lambda t: "o", lambda s: s["list"]["open"] and s["list"]["kind"] == "projects"),
        A("list.projects.step", "One knob step in the project list", then(lambda t: list_open(t, "projects")),
          lambda t: "+", lambda s: s["list"]["idx"] != idx["list"], needs="knob"),
        A("list.projects.turn", "A quick turn of five steps in the project list", then(lambda t: list_open(t, "projects")),
          lambda t: steps("+", 5), lambda s: s["list"]["idx"] != idx["list"], needs="knob"),
        A("list.projects.drag", "Drag the project list up", then(lambda t: list_open(t, "projects")),
          lambda t: drag(t, 160), lambda s: s["list"]["open"] and s["list"]["scroll"] != idx["scroll"], needs="touch-only"),
        A("list.projects.pick", "Tap a project to choose it", then(lambda t: list_open(t, "projects")), lambda t: "t",
          lambda s: not s["list"]["open"]),
        A("list.tasks.open", "Tap the task to open the list", then(lambda t: clock_state(t, "timer", "timer", "idle")),
          lambda t: "e", lambda s: s["list"]["open"] and s["list"]["kind"] == "tasks"),
        A("list.tasks.turn", "A quick turn of five steps in the task list", then(lambda t: list_open(t, "tasks")),
          lambda t: steps("+", 5), lambda s: s["list"]["idx"] != idx["list"], needs="knob"),
        A("list.tasks.drag", "Drag the task list up", then(lambda t: list_open(t, "tasks")), lambda t: drag(t, 160),
          lambda s: s["list"]["open"] and s["list"]["scroll"] != idx["scroll"], needs="touch-only"),
        A("list.tasks.pick", "Tap a task to choose it", then(lambda t: list_open(t, "tasks")), lambda t: "t",
          lambda s: not s["list"]["open"]),
        A("pomodoro.set", "One knob step sets the Pomodoro's length", then(pomo_25),
          lambda t: "+", lambda s: s["pomo"]["set_min"] != idx["set"], needs="knob"),
        A("pomodoro.drag", "Drag sideways to set the Pomodoro's length", then(pomo_25), lambda t: ">",
          lambda s: s["pomo"]["set_min"] != idx["set"], needs="touch-only"),
        A("agents.step", "One knob step to the next agent", then(lambda t: go(t, "zoo")), lambda t: "+",
          lambda s: s["sel"] != idx["sel"], needs="knob", animates=True, window=1.2),
        A("agents.turn", "A quick turn of five agents", then(lambda t: go(t, "zoo")), lambda t: steps("+", 5),
          lambda s: s["sel"] != idx["sel"], needs="knob", animates=True, window=1.5),
        A("agents.swipe", "Swipe to the next agent", then(lambda t: go(t, "zoo")), lambda t: "<",
          lambda s: s["sel"] != idx["sel"], needs="touch-only", animates=True, window=1.2),
        A("settings.step", "One knob step in Settings", then(settings_main), lambda t: "+",
          lambda s: s["settings"]["key"] != idx["key"], needs="knob"),
        A("settings.device", "Open Settings › Device", then(settings_main),
          lambda t: "t" if t.knob else "#tap 240 104\n", lambda s: s["settings"]["page"] == "device"),
        A("lock.keypad", "The first touch on a locked board brings up the keypad",
          then(lambda t: (go(t, "timer"), t.dev.cmd("lock"))), lambda t: "t", lambda s: s["lock"]["pad"], needs="lock",
          after=lambda t: t.dev.cmd("unlock")),
        A("mac.agent", "The Mac says an agent needs you", then(lambda t: (go(t, "zoo"), t.mac.send_agents())),
          lambda t: "LINK " + json.dumps({"t": "agents", "rows": [dict(AGENTS[0], st="needs", act="Waiting for you")]}) + "\n",
          lambda s: s["agents"][0]["st"] == "needs", animates=True, window=1.2),
        A("mac.agents", "The Mac sends all 30 agents again", then(lambda t: go(t, "zoo")),
          lambda t: "LINK " + json.dumps({"t": "agents", "page": 0, "pages": 1, "rows": AGENTS}) + "\n",
          lambda s: s["n_agents"] == len(AGENTS), animates=True, window=1.2),
        A("mac.clock", "Another board changes the task", then(lambda t: clock_state(t, "timer", "timer", "idle")),
          lambda t: "LINK " + json.dumps(t.mac.another_board({"desc": f"Other board {t.mac.rev % 10}", "task_id": 0,
                                                               "project": PROJECTS[3]})) + "\n",
          lambda s: s["task"].startswith("Other board")),  # short: a long task scrolls, and never settles
        A("mac.state", "The Mac's routine state message", then(lambda t: (go(t, "timer"), t.dev.cmd("task Inbox"))),
          lambda t: "LINK " + json.dumps(t.mac.state()) + "\n", lambda s: s["link"]["connected"]),
    ]


ACTIONS = build_actions()


# ---- Measuring ---------------------------------------------------------------------------------------------------------

def perf_line(dev, cmd):
    i = dev.mark()
    dev.send(f"#perf {cmd}\n")
    m = dev.wait(r"^PERF (\{.*\})$", 4, since=i)
    if not m:
        raise dt.Failed(f"no PERF answer to #perf {cmd}")
    return json.loads(m[1])


def measure(t, action):
    """One run of an action: the sample from the firmware, with whether it did what it should."""
    action.setup(t)
    t.settle_link()
    if not action.animates:
        t.screen_still()
    parts = action.act(t)
    parts = parts if isinstance(parts, list) else [parts]
    t.dev.send("#perf start\n" + parts[0])  # in one write: armed, then the input starts the clock
    for p in parts[1:]:
        if isinstance(p, float):
            time.sleep(p)
        else:
            t.dev.send(p)
    end = time.time() + action.window
    settled = False
    if action.animates:
        time.sleep(action.window)
    else:
        while time.time() < end:
            time.sleep(0.1)
            p = perf_line(t.dev, "peek")
            if p["started"] and p["quiet_ms"] >= QUIET_MS:
                settled = True
                break
    sample = perf_line(t.dev, "stop")
    ok = sample["started"] and action.expect(t.refresh())
    if action.after:
        action.after(t)
    sample["ok"] = bool(ok)
    sample["cut"] = not action.animates and not settled
    if action.animates:
        sample["settle_ms"] = None  # it never settles there: only the response and the frames mean something
    if action.key.startswith("idle."):
        sample["first_ms"] = None  # no input to respond to: the screen changes by itself
    if sample["first_ms"] is not None and sample["first_ms"] < 0:
        sample["first_ms"] = None  # nothing repainted
    return sample


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def summarize(samples):
    """The medians of each action's good samples, and how many there were."""
    out = {}
    for key, runs in samples.items():
        good = [s for s in runs if s.get("ok")]
        row = {field: median([s.get(field) for s in good]) for _, field, _ in METRICS}
        row["worst_of_runs"] = max((s["frame_ms_max"] for s in good), default=None)
        row.update(n=len(good), failed=len(runs) - len(good), cut=sum(1 for s in good if s.get("cut")))
        out[key] = row
    return out


def compare(current, baseline, kind):
    """Each (action, field) that moved past the tolerances: {(action, field): (baseline, now, "slower" or "faster")}."""
    moved = {}
    for key, row in current.items():
        base = (baseline or {}).get(key)
        if not base:
            continue
        for _, field, unit in METRICS:
            b, c = base.get(field), row.get(field)
            if b is None or c is None:
                continue
            if unit == "ms":
                tol, floor = TIME_TOL, FLOOR_MS[kind]
            else:
                tol, floor = COUNT_TOL, COUNT_FLOOR
            if c > b * (1 + tol) and c - b >= floor:
                moved[(key, field)] = (b, c, "slower")
            elif c < b * (1 - tol) and b - c >= floor:
                moved[(key, field)] = (b, c, "faster")
    return moved


def over_budget(current, kind):
    if kind != "board":
        return {}
    return {(key, field): limit for key, row in current.items() for field, limit in BUDGET_MS.items()
            if row.get(field) is not None and row[field] > limit}


def cell(row, field, unit, mark):
    v = row.get(field)
    if v is None:
        return "—"
    text = f"{v:.1f}" if unit == "ms" else f"{v:.0f}"
    return text + mark


def report(target, current, moved, budget, base_info):
    names = {a.key: a for a in ACTIONS}
    print(f"\n{target}: medians of {max((r['n'] for r in current.values()), default=0)} runs, in ms"
          + (f"; compared with the baseline of {base_info}" if base_info else "; no baseline yet (--baseline makes one)"))
    head = f"  {'action':22}" + "".join(f"{h:>11}" for h, _, _ in METRICS)
    print(head)
    for key, row in current.items():
        cells = []
        for _, field, unit in METRICS:
            mark = ""
            if (key, field) in moved:
                b, c, how = moved[(key, field)]
                pct = (c - b) / b * 100 if b else 0
                mark = f" ▲{pct:+.0f}%" if how == "slower" else f" ▼{pct:+.0f}%"
            if (key, field) in budget:
                mark += " !"
            cells.append(f"{cell(row, field, unit, mark):>11}")
        notes = []
        if row["failed"]:
            notes.append(f"{row['failed']} didn't do what they should")
        if row["cut"]:
            notes.append(f"{row['cut']} still moving after {names[key].window:.0f} s")
        print(f"  {key:22}" + "".join(cells) + (f"   ({'; '.join(notes)})" if notes else ""))
    slower = sorted({k for (k, _), v in moved.items() if v[2] == "slower"})
    faster = sorted({k for (k, _), v in moved.items() if v[2] == "faster"})
    if base_info:
        print(f"  slower: {', '.join(slower) or 'none'}; faster: {', '.join(faster) or 'none'}")
    if budget:
        print(f"  over budget (! — a {BUDGET_MS['first_ms']} ms response, a {BUDGET_MS['frame_ms_max']} ms frame): "
              + ", ".join(sorted({k for k, _ in budget})))


def git_commit():
    try:
        rev = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()
        return rev + ("+changes" if dirty else "")
    except OSError:
        return ""


def run_target(a, board):
    t = Target(a, board, OUT_DIR)
    chosen = [x for x in ACTIONS if not a.only or any(x.key.startswith(p.strip()) for p in a.only.split(","))]
    samples = {}
    try:
        t.start()
        todo = [x for x in chosen if x.applies(t)]
        print(f"{t.name}: {len(todo)} actions, {a.runs} runs each", flush=True)
        for action in todo:
            runs = []
            for _ in range(a.runs):
                try:
                    runs.append(measure(t, action))
                except dt.Failed as e:
                    runs.append({"ok": False, "error": str(e)})
            samples[action.key] = runs
            bad = sum(1 for s in runs if not s.get("ok"))
            print(f"  {action.key:22} {action.title}" + (f"  ({bad} of {len(runs)} didn't do what they should)" if bad else ""), flush=True)
    finally:
        t.finish()
    return t, samples


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="sim, or a board's name: " + ", ".join(BOARDS))
    p.add_argument("--board", choices=BOARDS, help="with sim: only this board's size (both otherwise)")
    p.add_argument("--port", help="a board's serial port, if there are several")
    p.add_argument("--runs", type=int, default=5, help="times to run each action (default 5)")
    p.add_argument("--only", help="actions whose names start with these, comma-separated: menu, list.tasks, mac …")
    p.add_argument("--baseline", action="store_true", help="save this run as the target's baseline")
    p.add_argument("--list", action="store_true", help="list the actions, and run nothing")
    a = p.parse_args()
    if a.list:
        for x in ACTIONS:
            print(f"{x.key:22} {x.title}" + (f"  [{x.needs}]" if x.needs else ""))
        return 0
    if a.target != "sim" and a.target not in BOARDS:
        p.error(f"no target called {a.target}")
    if a.target == "sim" and subprocess.call(["make", "-s", "-C", dt.SIM_DIR, "all"]) != 0:
        sys.exit("The simulator didn't build.")
    boards = [a.board] if a.board else list(BOARDS) if a.target == "sim" else [a.target]
    slower_anywhere = False
    for board in boards:
        t, samples = run_target(a, board)
        current = summarize(samples)
        when = time.strftime("%Y-%m-%d %H:%M")
        result = {"target": t.name, "when": when, "commit": git_commit(), "firmware": t.mac.fw if t.mac else None,
                  "runs": a.runs, "actions": current}
        base_path = os.path.join(PERF_DIR, "baselines", f"{t.name}.json")
        baseline = json.load(open(base_path)) if os.path.exists(base_path) else None
        moved = compare(current, baseline["actions"] if baseline else None, t.kind)
        budget = over_budget(current, t.kind)
        base_info = f"{baseline['when']} ({baseline.get('commit') or 'no commit'})" if baseline else ""
        report(t.name, current, moved, budget, base_info)
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, f"{t.name}.json"), "w") as f:
            json.dump(dict(result, samples=samples), f, indent=1)
        os.makedirs(os.path.join(PERF_DIR, "history"), exist_ok=True)
        with open(os.path.join(PERF_DIR, "history", f"{t.name}.jsonl"), "a") as f:
            f.write(json.dumps(dict(result, slower=sorted({k for (k, _), v in moved.items() if v[2] == "slower"}))) + "\n")
        if a.baseline:
            saved = result
            if a.only and baseline:  # some actions again: they replace theirs, and the rest of the baseline stays
                saved = dict(baseline, actions=dict(baseline["actions"], **current), when=when, commit=result["commit"])
            os.makedirs(os.path.dirname(base_path), exist_ok=True)
            with open(base_path, "w") as f:
                json.dump(saved, f, indent=1)
                f.write("\n")
            print(f"  saved as the baseline: {os.path.relpath(base_path, ROOT)}"
                  + (f" ({len(current)} of its actions)" if saved is not result else ""))
        slower_anywhere |= any(v[2] == "slower" for v in moved.values())
    return 1 if slower_anywhere and not a.baseline else 0


if __name__ == "__main__":
    sys.exit(main())
