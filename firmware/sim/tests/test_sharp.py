#!/usr/bin/env python3
"""The sharp window must show the firmware's picture, only with more pixels.

bento-sim-sharp draws the UX at twice the resolution, so any size written in pixels where it should be in layout units
shows up as a wrong proportion. Both current boards draw at a scale of exactly 1, so nothing else would catch that, and
a new board with another panel size would hit it for real. Each screen here is captured from both builds and compared.

    make -C firmware/sim all sharp && python3 firmware/sim/tests/test_sharp.py
"""
import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import Sim  # noqa: E402

BOARDS = {"knob-lcd-1.8": 360, "amoled-2.16": 480}
# (name, commands). The clock's digits and the rim are what scale, so they fill most of what's compared.
SCREENS = [
    ("timer", ["#mode timer", "#task Review the budget", "#project 7 #2FA866 Writing", "#session 754"]),
    ("pomodoro", ["#mode pomodoro", "#set 25"]),
    ("warm", ["#mode pomodoro", "#remaining 200"]),
    ("settings", ["#mode settings"]),
    ("zoo", ["#mode zoo"]),
    ("dial", ["#mode timer", "#swipe up"]),
    # Effects held still, from the same random numbers in both builds. Sizes come from layout units, so they must double.
    ("effect-done", ["#mode zoo", "#anim 1", "#fx seed 7", "#fx done", "#fx step 600"]),
    ("effect-gone", ["#fx step 3000", "#fx gone", "#fx step 500"]),
]
FEET = {"knob-lcd-1.8": (192, 7, 210), "amoled-2.16": (244, 10, 260)}  # the zoo layout: feet, scale, and the name's top
ALLOWED_DIFFERENT = 0.015  # text edges differ a little, as each size has its own rasterization
FACE_MAX = 40              # a face pixel's brightest channel, on the Mono profile


def lum(rgb, w, x, y):
    i = (y * w + x) * 3
    return max(rgb[i], rgb[i + 1], rgb[i + 2])


def rim_width(rgb, w):
    """How many pixels of rim there are at three o'clock."""
    y, x = w // 2, w - 1
    while x > 0 and lum(rgb, w, x, y) <= FACE_MAX:  # a corner of the panel, outside the rim
        x -= 1
    start = x
    while x > 0 and lum(rgb, w, x, y) > FACE_MAX:
        x -= 1
    return start - x


def particle_pixels(rgb, w, board, k, beside):
    """How many lit pixels there are above the agent's name. beside: only those beside the character, where only particles reach."""
    _, sc, bottom = FEET[board]
    n = 0
    for y in range(bottom * k):
        for x in range(w):
            if (not beside or abs(x - w / 2) > 11 * sc * k) and lum(rgb, w, x, y) > FACE_MAX:
                n += 1
    return n


def different_share(native, sharp, w):
    """The share of pixels that differ clearly once the sharp frame is averaged down to the panel's size."""
    wide = w * 2
    n = 0
    for y in range(w):
        for x in range(w):
            for c in range(3):
                a = (y * 2 * wide + x * 2) * 3 + c
                avg = (sharp[a] + sharp[a + 3] + sharp[a + wide * 3] + sharp[a + wide * 3 + 3]) / 4
                if abs(avg - native[(y * w + x) * 3 + c]) > 64:
                    n += 1
                    break
    return n / (w * w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=os.path.join(HERE, "..", "build"), help="the folder holding both binaries")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "out", "sharp"), help="where to keep the captures")
    args = ap.parse_args()
    failed = []

    def check(ok, what):
        print(f"  {'pass' if ok else 'FAIL'} {what}", flush=True)
        if not ok:
            failed.append(what)

    with tempfile.TemporaryDirectory(prefix="bento-sharp-") as tmp:
        for i, (board, size) in enumerate(BOARDS.items()):
            print(f"=== {board} ===", flush=True)
            sims = []
            for j, name in enumerate(("bento-sim", "bento-sim-sharp")):
                s = Sim(os.path.join(args.build, name), board, os.path.join(args.out, board, name), 7900 + i * 10 + j * 4, os.path.join(tmp, f"{board}-{j}.bin"))
                s.start()
                sims.append(s)
            try:
                time.sleep(.6)
                native_sim, sharp_sim = sims
                for name, cmds in SCREENS:
                    for s in sims:
                        for c in cmds:
                            s.cmd(c)
                        s.cmd("#quiet")
                        time.sleep(.5)
                    native = native_sim.shot(name)
                    sharp = sharp_sim.shot(name)
                    check((sharp.w, sharp.h) == (size * 2, size * 2), f"{name}: the sharp frame is {sharp.w} x {sharp.h}")
                    share = different_share(native.to_rgb(), sharp.to_rgb(), size)
                    check(share <= ALLOWED_DIFFERENT, f"{name}: {share:.2%} of pixels differ")
                    if name.startswith("effect"):
                        beside = name == "effect-done"  # after a poof there's no character left to go around
                        a = particle_pixels(native.to_rgb(), size, board, 1, beside)
                        b = particle_pixels(sharp.to_rgb(), size * 2, board, 2, beside)
                        check(a > 0 and 0.8 < b / (a * 4) < 1.25, f"{name}: {a} lit pixels of particles, and {b} when sharp")
                    if name == "timer":
                        a, b = native.to_rgb(), sharp.to_rgb()
                        ra, rb = rim_width(a, size), rim_width(b, size * 2)
                        check(abs(rb - ra * 2) <= 2, f"the rim is {ra} px wide, and {rb} when sharp")
            finally:
                for s in sims:
                    s.stop()
    print(f"\n{'All passed.' if not failed else str(len(failed)) + ' failed:'}")
    for f in failed:
        print("  " + f)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
