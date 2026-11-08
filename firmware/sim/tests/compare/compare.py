#!/usr/bin/env python3
"""Puts the approved preview and the simulator into the same scenes, and lays them side by side: preview, simulator, and
where they differ. Sheets go to firmware/sim/out/compare/. It reports a number for each scene, but the point is to look.

    tools/.venv/bin/python firmware/sim/tests/compare/compare.py [--node-path FOLDER] [--only NAME,NAME]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageChops, ImageDraw, ImageStat

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from harness import Sim  # noqa: E402

SIMDIR = os.path.dirname(os.path.dirname(HERE))
TASK, PROJECT = "Write the Q4 plan", (123, "#4FB3F6", "Deep work")
AGENTS = [
    {"id": "a1000e7", "name": "api-refactor-12", "proj": "api-server", "kind": "i", "st": "working", "act": "Editing main.c", "note": "", "age": 240, "up": 1200, "rex": 1},
    {"id": "a1005e7", "name": "docs-site-3f", "proj": "docs", "kind": "i", "st": "working", "act": "Editing index.md", "note": "", "age": 120, "up": 400, "rex": 1, "agent": "codex"},
    {"id": "a1001e7", "name": "ml-eval-5d", "proj": "ml", "kind": "i", "st": "working", "act": "Reading train.py", "note": "", "age": 660, "up": 900, "rex": 1, "agent": "grok"},
    {"id": "a1006e7", "name": "ci-cleanup-8b", "proj": "infra", "kind": "i", "st": "done", "act": "Finished", "note": "", "age": 60, "up": 3000, "rex": 1},
    {"id": "a1002e7", "name": "landing-page-5b", "proj": "web", "kind": "i", "st": "idle", "act": "Waiting for a task", "note": "", "age": 1560, "up": 5000, "rex": 1, "agent": "codex"},
]

# Each scene: what to do to the preview (JS), and to the simulator (a list of commands; a number is a pause).
# Time doesn't stop in the preview, so its clocks are set just before the shot, and text that ticks may differ by a digit.
OPEN_ZOO = 'S.mode = "zoo";'
STOP_RUN = ""


def S_js(body):
    return "(() => { const S = window.__S, b = window.__b; const now = performance.now(); " + body + " })()"


SCENES = [
    {"name": "timer-rest", "js": S_js(""), "sim": []},
    {"name": "timer-running", "js": S_js('S.t.state = "running"; S.t.start = now - 754000;'),
     "sim": ["s", 0.05, "#elapsed 754", 0.05]},
    {"name": "timer-paused", "js": S_js('S.t.state = "paused"; S.t.session = 754000;'), "sim": ["#session 754"]},
    {"name": "timer-cleared", "js": S_js('S.task = ""; S.project = null;'), "sim": ["#task ", "#project 0"]},
    {"name": "timer-hours", "js": S_js('S.t.state = "paused"; S.t.session = 3725000;'), "sim": ["#session 3725"]},
    {"name": "pomodoro-rest", "js": S_js('S.mode = "pomodoro";'), "sim": ["#mode pomodoro"]},
    {"name": "pomodoro-running", "js": S_js('S.mode = "pomodoro"; S.p.state = "running"; S.p.remaining = 1499000; S.p.last = now;'),
     "sim": ["#mode pomodoro", "#remaining 1499", "s", 0.03]},
    {"name": "pomodoro-paused", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 1499000;'),
     "sim": ["#mode pomodoro", "#remaining 1499"]},
    {"name": "pomodoro-10-00", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 600000;'), "sim": ["#mode pomodoro", "#remaining 600"]},
    {"name": "pomodoro-07-30", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 450000;'), "sim": ["#mode pomodoro", "#remaining 450"]},
    {"name": "pomodoro-05-00", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 300000;'), "sim": ["#mode pomodoro", "#remaining 300"]},
    {"name": "pomodoro-03-00", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 180000;'), "sim": ["#mode pomodoro", "#remaining 180"]},
    {"name": "pomodoro-01-00", "js": S_js('S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 60000;'), "sim": ["#mode pomodoro", "#remaining 60"]},
    {"name": "pomodoro-pulse-peak", "js": S_js('S.mode = "pomodoro"; S.p.state = "running"; S.p.remaining = 40000; S.p.last = now;'),
     "sim": ["#mode pomodoro", "#remaining 40", "s", 0.03, "#pulse 0.99"], "peak": True},
    {"name": "zoo-pomodoro-pulse-peak", "js": S_js(OPEN_ZOO + ' S.p.state = "running"; S.p.remaining = 40000; S.p.last = now;'),
     "sim": ["#mode pomodoro", "#remaining 40", "s", 0.03, "#pulse 0.99", "#mode zoo"], "peak": True},
    {"name": "menu", "js": S_js('b.swipe("knob", "up", now);'), "sim": ["u"], "wait": 500},
    {"name": "menu-running", "js": S_js('S.t.state = "running"; S.t.start = now - 754000; b.swipe("knob", "up", now);'),
     "sim": ["s", 0.03, "#elapsed 754", "u"], "wait": 500},
    {"name": "settings", "js": S_js('b.swipe("knob", "up", now); b.tap("knob", 110, 290, now);'),
     "sim": ["#mode settings"], "wait": 500},
    {"name": "settings-inverted", "js": S_js('S.theme = "inverted"; b.swipe("knob", "up", now); b.tap("knob", 110, 290, now);'),
     "sim": ["#theme inverted", "#mode settings"], "wait": 500},
    {"name": "settings-macs", "js": S_js('b.swipe("knob", "up", now); b.tap("knob", 110, 290, now); S.set.page = "macs"; S.set.key = "Any Mac";'),
     "sim": ["%mac MacBook Pro", "%mac Mac mini", "#mode settings", ("mac-page",)], "wait": 500},
    {"name": "settings-pair", "js": S_js('b.swipe("knob", "up", now); b.tap("knob", 110, 290, now); S.set.page = "pair";'),
     "sim": ["#mode settings", ("pair-page",), "%pair on"], "wait": 500, "panel": True},
    {"name": "zoo", "js": S_js(OPEN_ZOO), "sim": ["#mode zoo"], "wait": 500},
    {"name": "zoo-codex", "js": S_js(OPEN_ZOO + " S.sel = 1;"), "sim": ["#mode zoo", ("sel", 1)], "wait": 700},
    {"name": "zoo-grok", "js": S_js(OPEN_ZOO + " S.sel = 2;"), "sim": ["#mode zoo", ("sel", 2)], "wait": 700},
    {"name": "zoo-done", "js": S_js(OPEN_ZOO + " S.sel = 3;"), "sim": ["#mode zoo", ("sel", 3)], "wait": 700},
    {"name": "zoo-idle", "js": S_js(OPEN_ZOO + " S.sel = 4;"), "sim": ["#mode zoo", ("sel", 4)], "wait": 700},
    {"name": "zoo-with-timer", "js": S_js(OPEN_ZOO + ' S.t.state = "running"; S.t.start = now - 754000;'),
     "sim": ["s", 0.03, "#elapsed 754", "#mode zoo"], "wait": 500},
    {"name": "zoo-with-long-timer", "js": S_js(OPEN_ZOO + ' S.t.state = "running"; S.t.start = now - 3725000;'),
     "sim": ["s", 0.03, "#elapsed 3725", "#mode zoo"], "wait": 500},
    {"name": "zoo-long-project", "js": S_js(OPEN_ZOO + ' S.t.state = "running"; S.t.start = now - 754000; S.project = { name: "Acme customer portal redesign and migration", color: "#F2C14E" };'),
     "sim": ["s", 0.03, "#elapsed 754", "#project 999 #F2C14E Acme customer portal redesign and migration", "#mode zoo"], "wait": 500},
    {"name": "zoo-pomodoro-half", "js": S_js(OPEN_ZOO + ' S.p.setMin = 50; S.p.total = 3000000; S.p.state = "running"; S.p.remaining = 1500000; S.p.last = now;'),
     "sim": ["#set 50", "#mode pomodoro", "s", 0.03, "#remaining 1500", "#mode zoo"], "wait": 400},
    {"name": "zoo-needs-you", "js": S_js(OPEN_ZOO + ' b.needsYou(now); const a = S.agents[0]; S.sel = 0;'),
     "sim": ["#mode zoo", ("needs", 0)], "wait": 600},
    {"name": "zoo-needs-you-grok", "js": S_js(OPEN_ZOO + ' const a = S.agents[2]; a.st = "needs"; a.act = "Waiting for your answer"; a.since = now; S.sel = 2;'),
     "sim": ["#mode zoo", ("needs", 2)], "wait": 600},
    {"name": "zoo-needs-you-codex", "js": S_js(OPEN_ZOO + ' const a = S.agents[1]; a.st = "needs"; a.act = "Waiting for your answer"; a.since = now; S.sel = 1;'),
     "sim": ["#mode zoo", ("needs", 1)], "wait": 600},
    {"name": "zoo-offline", "js": S_js(OPEN_ZOO + " S.connected = false;"), "sim": ["#mode zoo", "#mac off"], "wait": 400},
    {"name": "zoo-empty", "js": S_js(OPEN_ZOO + " S.agents.length = 0;"), "sim": ["#mode zoo", ("agents", 0)], "wait": 400},
    {"name": "timer-needs-you", "js": S_js("b.needsYou(now);"), "sim": [("needs", 0)], "wait": 500},
    {"name": "inverted-timer", "js": S_js('S.theme = "inverted";'), "sim": ["#theme inverted"]},
    {"name": "inverted-pomodoro-05-00", "js": S_js('S.theme = "inverted"; S.mode = "pomodoro"; S.p.state = "paused"; S.p.remaining = 300000;'), "sim": ["#theme inverted", "#mode pomodoro", "#remaining 300"]},
    {"name": "inverted-menu", "js": S_js('S.theme = "inverted"; b.swipe("knob", "up", now);'), "sim": ["#theme inverted", "u"], "wait": 500},
    {"name": "inverted-zoo", "js": S_js('S.theme = "inverted";' + OPEN_ZOO), "sim": ["#theme inverted", "#mode zoo"], "wait": 500},
    {"name": "inverted-zoo-grok", "js": S_js('S.theme = "inverted";' + OPEN_ZOO + " S.sel = 2;"), "sim": ["#theme inverted", "#mode zoo", ("sel", 2)], "wait": 700},
    {"name": "inverted-zoo-with-timer", "js": S_js('S.theme = "inverted";' + OPEN_ZOO + ' S.t.state = "running"; S.t.start = now - 754000;'), "sim": ["#theme inverted", "s", 0.03, "#elapsed 754", "#mode zoo"], "wait": 500},
    {"name": "status-battery-low", "js": S_js("S.battery = 10;"), "sim": ["#battery 10"]},
    {"name": "status-battery-charging", "js": S_js("S.battery = 50; S.charging = true;"), "sim": ["#battery 50 charging"]},
    {"name": "status-zoo-charging", "js": S_js(OPEN_ZOO + " S.battery = 50; S.charging = true;"), "sim": ["#mode zoo", "#battery 50 charging"], "wait": 400},
    {"name": "lock-normal", "js": S_js('b.swipe("knob", "up", now); b.tap("knob", 250, 290, now);'), "sim": ["#lock"], "wait": 400},
    {"name": "lock-keypad", "js": S_js('b.swipe("knob", "up", now); b.tap("knob", 250, 290, now); b.tap("knob", 180, 180, now);'),
     "sim": ["#lock", "t"], "wait": 400},
    {"name": "lock-keypad-running", "js": S_js('S.t.state = "running"; S.t.start = now - 754000; S.lock.open = true; S.lock.pad = true; S.lock.padUntil = now + 10000;'),
     "sim": ["s", 0.03, "#elapsed 754", "#lock", "t"], "wait": 300},
    {"name": "lock-wrong", "js": S_js('S.lock.open = true; S.lock.pad = true; S.lock.padUntil = now + 10000; S.lock.msg = "Wrong code";'),
     "sim": ["#lock", "t", ("keys", "9999")], "wait": 650},
    {"name": "lock-entry", "js": S_js('S.lock.open = true; S.lock.pad = true; S.lock.padUntil = now + 10000; S.lock.entry = "12";'),
     "sim": ["#lock", "t", ("keys", "12")], "wait": 300},
    {"name": "lock-wait", "js": S_js('S.lock.open = true; S.lock.pad = true; S.lock.padUntil = now + 10000; S.lock.until = now + 29500;'),
     "sim": ["#lock", "t", ("keys", "99999999999999999999")], "wait": 650},
    {"name": "inverted-keypad", "js": S_js('S.theme = "inverted"; S.lock.open = true; S.lock.pad = true; S.lock.padUntil = now + 10000;'),
     "sim": ["#theme inverted", "#lock", "t"], "wait": 300},
    {"name": "inverted-zoo-needs-you", "js": S_js('S.theme = "inverted";' + OPEN_ZOO + " b.needsYou(now); S.sel = 0;"),
     "sim": ["#theme inverted", "#mode zoo", ("needs", 0)], "wait": 600},
    {"name": "toast", "js": S_js('window.__b; S.toast = { text: "Focusing in Rex", until: now + 5000 }; ' + OPEN_ZOO), "sim": ["#mode zoo", ("tap_center",)], "wait": 350},
]

SQUARE_ONLY = {
    "rotate-90": {"js": S_js("b.setTilt(90);"), "sim": ["#tilt 90", 1.3], "wait": 1300},
    "rotate-180": {"js": S_js("b.setTilt(180);"), "sim": ["#tilt 180", 1.3], "wait": 1300},
    "rotate-270": {"js": S_js("b.setTilt(270);"), "sim": ["#tilt 270", 1.3], "wait": 1300},
}


def sim_run(s, c, board):
    """One step of a scene."""
    if isinstance(c, (int, float)):
        time.sleep(c)
    elif isinstance(c, tuple):
        kind = c[0]
        if kind == "sel":
            for _ in range(c[1]):
                s.cmd("#turn 1") if board.startswith("knob") else s.key("<")
        elif kind == "needs":
            a = [dict(x) for x in AGENTS]
            a[c[1]]["st"] = "needs"
            a[c[1]]["act"] = "Waiting for your answer"
            a[c[1]]["age"] = 0
            mac_state(s, a)
            time.sleep(0.05)
        elif kind == "agents":
            mac_state(s, AGENTS[:c[1]])
        elif kind == "keys":
            round_ = board.startswith("knob")
            K = (180 - 108, 96, 72, 54) if round_ else (240 - 165, 112, 110, 80)
            for d in c[1]:
                v = int(d)
                col, row = (1, 3) if v == 0 else ((v - 1) % 3, (v - 1) // 3)
                s.cmd(f"#tap {K[0] + K[2] * (col + .5):.0f} {K[1] + K[3] * (row + .5):.0f}")
        elif kind == "mac-page":
            st = s.state()
            row = [r["label"] for r in st["settings"]["rows"]].index("Mac")
            if board.startswith("knob"):
                s.cmd(f"#turn {row}")
                time.sleep(0.3)
                s.key("t")
            else:
                s.cmd(f"#tap 240 {66 + row * 54 + 27}")
        elif kind == "pair-page":
            st = s.state()
            row = [r["label"] for r in st["settings"]["rows"]].index("Pair a new Mac")
            if board.startswith("knob"):
                s.cmd(f"#turn {row}")
                time.sleep(0.3)
                s.key("t")
            else:
                s.cmd(f"#tap 240 {66 + row * 54 + 27}")
        elif kind == "tap_center":
            mid = 180 if board.startswith("knob") else 240
            s.cmd(f"#tap {mid} {mid}")
    elif c.startswith("%"):
        s.simcmd(c[1:])
    elif c.startswith("#"):
        s.cmd(c)
    else:
        s.key(c)


def mac_state(s, agents):
    s.mac({"t": "state", "v": 3, "seq": 1, "now": 1793826000, "utc": 0, "toggl": "ok", "voice": True, "agents": agents})


def sim_scene(binary, board, scene, tmp, base_port, out):
    s = Sim(binary, board, os.path.join(tmp, "unused"), base_port, os.path.join(tmp, f"store-{board}.bin"))
    s.start()
    try:
        s.cmd("#anim 0.1")
        s.cmd("#burnin 0 0")
        s.link_usb()
        s.mac({"t": "hello", "v": 3, "host": "MacBook Pro"})
        mac_state(s, AGENTS)
        s.mac({"t": "lock_code", "salt": "000102030405060708090a0b0c0d0e0f", "iter": 2000, "hash": "c41892d2ceec337a4279f5fad0221e9c160182318f46d81616410f17ac895a36"})
        s.cmd(f"#task {TASK}")
        s.cmd(f"#project {PROJECT[0]} {PROJECT[1]} {PROJECT[2]}")
        time.sleep(0.05)
        s.cmd("#quiet")
        for c in scene["sim"]:
            sim_run(s, c, board)
        time.sleep(0.25 + scene.get("wait", 0) / 1000 * 0.6)
        path = os.path.join(out, f"{scene['name']}-sim.png")
        if scene.get("panel"): # what a person sees, code and all: a capture never shows it
            ppm = path + ".ppm"
            s.simcmd("panel " + ppm)
            Image.open(ppm).convert("RGB").save(path)
        else:
            f = s.frame()
            Image.frombuffer("RGB", (f.w, f.h), bytes(f.to_rgb())).save(path)
    finally:
        s.stop()


def sheet(name, board, preview_path, sim_path, out):
    p = Image.open(preview_path).convert("RGB")
    a = Image.open(sim_path).convert("RGB")
    p1 = p.resize(a.size, Image.BOX)
    w, h = a.size
    if board.startswith("knob"):
        mask = Image.new("L", (w * 2, h * 2), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, w * 2 - 1, h * 2 - 1], fill=255)
        mask = mask.resize((w, h), Image.BOX)
        black = Image.new("RGB", (w, h), 0)
        p1 = Image.composite(p1, black, mask)
        a = Image.composite(a, black, mask)
    diff = ImageChops.difference(p1, a)
    mean = sum(ImageStat.Stat(diff).mean) / 3
    out_img = Image.new("RGB", (w * 3 + 20, h), (40, 40, 44))
    out_img.paste(p1, (0, 0))
    out_img.paste(a, (w + 10, 0))
    out_img.paste(diff, (2 * w + 20, 0))
    out_img.save(os.path.join(out, f"{name}-{board}.png"))
    return mean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-path", default=os.environ.get("NODE_PATH", ""))
    ap.add_argument("--only")
    ap.add_argument("--board", action="append")
    args = ap.parse_args()
    out = os.path.join(SIMDIR, "out", "compare")
    os.makedirs(out, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    scenes = [sc for sc in SCENES if not only or sc["name"] in only]
    extra = [dict(v, name=k) for k, v in SQUARE_ONLY.items() if not only or k in only]
    binary = os.path.join(SIMDIR, "build", "bento-sim")
    with tempfile.TemporaryDirectory(prefix="bento-compare-") as tmp:
        spath = os.path.join(tmp, "scenes.json")
        json.dump([{"name": sc["name"], "js": sc["js"], "wait": sc.get("wait", 450), "peak": sc.get("peak", False)} for sc in scenes + extra], open(spath, "w"))
        prev = os.path.join(tmp, "preview")
        env = dict(os.environ, NODE_PATH=args.node_path)
        subprocess.run(["node", os.path.join(HERE, "preview_shots.js"), spath, prev], check=True, env=env)
        results = []
        for board, id_ in (("knob-lcd-1.8", "knob"), ("amoled-2.16", "square")):
            if args.board and board not in args.board:
                continue
            bdir = os.path.join(tmp, board)
            os.makedirs(bdir)
            todo = scenes + (extra if id_ == "square" else [])
            for i, sc in enumerate(todo):
                port = 7700 + (0 if id_ == "knob" else 100) + (i % 40) * 2
                sim_scene(binary, board, sc, bdir, port, bdir)
                results.append((sc["name"], board, sheet(sc["name"], board, os.path.join(prev, f"{sc['name']}-{id_}.png"), os.path.join(bdir, f"{sc['name']}-sim.png"), out)))
    results.sort(key=lambda r: -r[2])
    print("mean difference per channel, out of 255 (biggest first):")
    for name, board, m in results:
        print(f"{m:7.2f}  {board:<14} {name}")


if __name__ == "__main__":
    main()
