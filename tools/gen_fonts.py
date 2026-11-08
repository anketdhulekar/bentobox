#!/usr/bin/env python3
"""Generate firmware/ux/assets/fonts.c and fonts.h: Manrope as 8-bit alpha masks.

Every size and weight below comes from the LAYOUTS tables in preview/bento.html, as used by firmware/ux/layouts/.
Glyphs are rasterized at 8x and box-filtered down, with no hinting, so they have the same shapes as the browser's.
Sizes that show text from outside (task, project, agent and Mac names) get Latin-1 too when they're small; big ones
get ASCII only, and the text code strips accents for those. Sizes that only show the clock's digits get digits only.

Needs Pillow with raqm and fontTools, from the tools environment:
    python3.13 -m venv tools/.venv && tools/.venv/bin/pip install pillow fonttools
    tools/.venv/bin/python tools/gen_fonts.py

With --sharp OUT.c it writes the same tables at twice the sizes, for the simulator's window, which renders the UX at
twice the panel's resolution. That file is a build product, and the firmware never sees it (see firmware/sim/Makefile).
Manrope is under the SIL Open Font License (tools/fonts/OFL.txt).
"""
import hashlib
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
TTF = ROOT / "tools" / "fonts" / "Manrope-VariableFont_wght.ttf"
OUT_C = ROOT / "firmware" / "ux" / "assets" / "fonts.c"
OUT_H = ROOT / "firmware" / "ux" / "assets" / "fonts.h"

SS = 8  # supersampling
ASCII = [chr(c) for c in range(32, 127)]
PUNCT = list("…›·—–‘’“”•×")
LATIN1 = [chr(c) for c in range(0xC0, 0x100)] + list("¡¿£€°")
EXTERNAL = ASCII + PUNCT + LATIN1  # names from outside, small sizes
EXTERNAL_BIG = ASCII + PUNCT       # names from outside, big sizes: accents are stripped at run time
FIXED = ASCII + list("…›·’")       # only our own strings
LETTERS = [chr(c) for c in range(65, 91)] + [chr(c) for c in range(97, 123)] + [" "]  # the dial's labels
DIGITS = list("0123456789:")
CODE = list("0123456789: ")  # the pairing code, and pair mode's countdown
FALLBACK = "�"  # drawn as an empty box: anything with no glyph

# (size in px, weight, characters). Sizes are the LAYOUTS tables', for 360 and 480.
FONTS = [
    # The clock's digits. Digits only.
    (120, 600, DIGITS), (88, 600, DIGITS), (84, 600, DIGITS), (72, 600, DIGITS), (62, 600, DIGITS), (54, 600, DIGITS),
    (48, 600, DIGITS), (36, 600, DIGITS), (30, 600, DIGITS), (19, 600, FIXED),
    # Pairing code.
    (63, 700, CODE), (45, 700, CODE),
    # Project pill, agent name, a list's selected row.
    (12.5, 700, EXTERNAL), (15, 700, EXTERNAL), (17, 700, EXTERNAL), (19, 700, EXTERNAL), (20, 700, EXTERNAL),
    (22, 700, EXTERNAL), (24, 700, EXTERNAL), (30, 700, EXTERNAL_BIG), (36, 700, EXTERNAL_BIG),
    # Dial labels, a round list's title.
    (42, 700, LETTERS), (14, 700, FIXED),
    # Task, status, activity, list rows, small labels.
    (12, 500, EXTERNAL), (13, 500, EXTERNAL), (14, 500, EXTERNAL), (15, 500, EXTERNAL), (17, 500, EXTERNAL),
    (18, 500, EXTERNAL), (19, 500, EXTERNAL), (20, 500, EXTERNAL), (22, 500, EXTERNAL), (26, 500, EXTERNAL_BIG),
    (28, 500, EXTERNAL_BIG), (36, 500, EXTERNAL_BIG), (16, 500, FIXED),
    # Toasts, list values, Mac names; the lock's title and the dial's "running" line.
    (13, 600, EXTERNAL), (17, 600, EXTERNAL), (18, 600, EXTERNAL), (22, 600, EXTERNAL),
    (12, 600, FIXED), (14, 600, FIXED), (16, 600, FIXED), (21, 600, FIXED),
    # The menu tiles' names.
    (28, 700, FIXED),
]

# A blurred copy of the digits for the last-minute pulse's glow, at the sizes a Pomodoro's time shows: stored at 1/4
# size (a blur has no detail), and drawn scaled up. The preview's shadow peaks at a blur of .55 x .85 x the size, so
# a sigma of half that.
GLOW_SIZES = [120, 88, 72, 62, 48]
GLOW_SCALE = 4
GLOW_SIGMA = 0.20  # x the size


def load(size, weight, engine=ImageFont.Layout.BASIC):
    font = ImageFont.truetype(str(TTF), size, layout_engine=engine)
    font.set_variation_by_axes([weight])
    return font


def raster_glyph(hi, ch, size):
    """Returns (w, h, ox, oy, bytes) at 1x, from the 8x font, with its origin on a whole pixel."""
    if ch == FALLBACK:
        return box_glyph(size)
    x0, y0, x1, y1 = hi.getbbox(ch, anchor="ls")
    if x1 <= x0 or y1 <= y0:
        return 0, 0, 0, 0, b""
    px0, py0 = x0 // SS, y0 // SS
    px1, py1 = -((-x1) // SS), -((-y1) // SS)
    w, h = px1 - px0, py1 - py0
    img = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(img).text((-px0 * SS, -py0 * SS), ch, font=hi, fill=255, anchor="ls")
    return w, h, px0, py0, img.resize((w, h), Image.BOX).tobytes()


def box_glyph(size):
    """An empty box as tall as a capital letter: the mark for a character the font doesn't have."""
    cap, wid, stroke = size * 0.72, size * 0.55, max(1.0, size * 0.07)
    ox, oy = round(size * 0.06), -int(cap + 0.999)
    w, h = int(wid + 0.999) + 1, int(cap + 0.999)
    img = Image.new("L", (w * SS, h * SS), 0)
    s = round(stroke * SS)
    ImageDraw.Draw(img).rectangle([0, 0, wid * SS - 1, h * SS - 1], outline=255, width=s)
    return w, h, ox, oy, img.resize((w, h), Image.BOX).tobytes()


def advance(hi, ch, size):
    if ch == FALLBACK:
        return size * 0.67
    return hi.getlength(ch) / SS


def c_bytes(name, data):
    lines = [f"static const uint8_t {name}[{max(1, len(data))}] = {{"]
    if not data:
        lines.append("0,")
    for i in range(0, len(data), 48):
        lines.append(",".join(str(b) for b in data[i:i + 48]) + ",")
    lines.append("};")
    return lines


def tag(size, weight):
    return f"{str(size).replace('.', '_')}_{weight}"


def build_font(size, weight, chars):
    hi = load(size * SS, weight)
    glyphs, bits = [], bytearray()
    full = chars if chars in (DIGITS, CODE) else chars + [FALLBACK]
    for ch in sorted(set(full), key=ord):
        w, h, ox, oy, data = raster_glyph(hi, ch, size)
        glyphs.append((ord(ch), ox, oy, w, h, round(advance(hi, ch, size) * 16), len(bits)))
        bits.extend(data)
    return glyphs, bits


def build_glow(size):
    """Each digit blurred at full size, then scaled down."""
    hi = load(size * SS, 600)
    sigma = GLOW_SIGMA * size
    pad = int(3 * sigma)
    glyphs, bits = [], bytearray()
    for ch in sorted(DIGITS, key=ord):
        x0, y0, x1, y1 = hi.getbbox(ch, anchor="ls")
        px0, py0 = x0 // SS, y0 // SS
        px1, py1 = -((-x1) // SS), -((-y1) // SS)
        # The mask's cells must line up with whole multiples of the scale from the pen.
        ax0 = (px0 - pad) // GLOW_SCALE * GLOW_SCALE
        ay0 = (py0 - pad) // GLOW_SCALE * GLOW_SCALE
        w = (-((ax0 - (px1 + pad)) // GLOW_SCALE)) * GLOW_SCALE
        h = (-((ay0 - (py1 + pad)) // GLOW_SCALE)) * GLOW_SCALE
        img = Image.new("L", (w * 2, h * 2), 0)  # at 2x: enough for a wide blur, and quick
        d = load(size * 2, 600)
        ImageDraw.Draw(img).text((-ax0 * 2, -ay0 * 2), ch, font=d, fill=255, anchor="ls")
        img = img.filter(ImageFilter.GaussianBlur(sigma * 2)).resize((w // GLOW_SCALE, h // GLOW_SCALE), Image.BOX)
        glyphs.append((ord(ch), ax0 // GLOW_SCALE, ay0 // GLOW_SCALE, w // GLOW_SCALE, h // GLOW_SCALE,
                       round(hi.getlength(ch) / SS * 16), len(bits)))
        bits.extend(img.tobytes())
    return glyphs, bits


def build_kerning(weight):
    """Pairs of ASCII characters that the font kerns, as the browser does, in 1/1000 em."""
    font = load(1000, weight, ImageFont.Layout.RAQM)
    single = {ch: font.getlength(ch) for ch in ASCII}
    pairs = []
    for a in ASCII:
        for b in ASCII:
            k = round(font.getlength(a + b) - single[a] - single[b])
            if abs(k) >= 8:
                pairs.append((ord(a) << 8 | ord(b), k))
    return pairs


def check_fits(glyphs, size, offset_max):
    """A glyph's fields have fixed widths: stop here rather than wrap around in the C tables."""
    for cp, ox, oy, w, h, adv, off in glyphs:
        if w > 255 or h > 255 or adv > 65535 or abs(ox) > offset_max or abs(oy) > offset_max:
            sys.exit(f"U+{cp:04X} at {size} px doesn't fit a glyph_t: {ox},{oy} {w}x{h}, advance {adv}")


def source_hash():
    """What the fonts are made from: the table of sizes, the glow, the supersampling and the font file itself.
    tools/test_bento.py compares this with fonts.c's, so a change here can't go out without regenerating."""
    h = hashlib.sha256()
    h.update(repr((FONTS, GLOW_SIZES, GLOW_SCALE, GLOW_SIGMA, SS)).encode())
    h.update(TTF.read_bytes())
    return h.hexdigest()[:16]


def main(sharp_out=None):
    global SS
    render = 2 if sharp_out else 1
    offset_max = 32767 if sharp_out else 127
    if sharp_out:
        SS = 4  # the same detail per panel pixel as 8x at the usual sizes, and much quicker
    c = ["// Generated by tools/gen_fonts.py from Manrope (SIL Open Font License). Do not edit.",
         f"// source-hash: {source_hash()}", '#include "fonts.h"', ""]
    total, names = 0, []
    for size, weight, chars in FONTS:
        size *= render
        glyphs, bits = build_font(size, weight, chars)
        check_fits(glyphs, size, offset_max)
        t = tag(size, weight)
        c += c_bytes(f"bits_{t}", bits)
        c.append(f"static const glyph_t glyphs_{t}[{len(glyphs)}] = {{")
        c += [f"{{{cp},{ox},{oy},{w},{h},{adv},{off}}}," for cp, ox, oy, w, h, adv, off in glyphs]
        c.append("};")
        ymin = min((g[2] for g in glyphs if g[4]), default=0)
        ymax = max((g[2] + g[4] for g in glyphs if g[4]), default=0)
        kinds = (1 if "0" in chars else 0) | (2 if "A" in chars else 0) | (4 if "." in chars else 0)
        c.append(f"static const font_t font_{t} = {{glyphs_{t}, bits_{t}, {len(glyphs)}, {round(size * 10)}, {weight}, {ymin}, {ymax}, {kinds}}};")
        names.append(f"&font_{t}")
        total += len(bits) + len(glyphs) * 12
        print(f"{size:>5} {weight} {len(glyphs):>4} glyphs {len(bits) // 1024:>4} KB")
    c.append(f"const font_t *const FONTS[{len(names)}] = {{{', '.join(names)}}};")
    c.append(f"const int FONT_COUNT = {len(names)};")
    c.append("")
    glow_names = []
    for size in GLOW_SIZES:
        size *= render
        glyphs, bits = build_glow(size)
        check_fits(glyphs, size, offset_max)
        t = f"glow_{size}"
        c += c_bytes(f"bits_{t}", bits)
        c.append(f"static const glyph_t glyphs_{t}[{len(glyphs)}] = {{")
        c += [f"{{{cp},{ox},{oy},{w},{h},{adv},{off}}}," for cp, ox, oy, w, h, adv, off in glyphs]
        c.append("};")
        c.append(f"static const font_t font_{t} = {{glyphs_{t}, bits_{t}, {len(glyphs)}, {size * 10}, 600, 0, 0, 1}};")
        glow_names.append(f"&font_{t}")
        total += len(bits) + len(glyphs) * 12
        print(f"{size:>5} glow {len(glyphs):>4} glyphs {len(bits) // 1024:>4} KB")
    c.append(f"const font_t *const GLOW_FONTS[{len(glow_names)}] = {{{', '.join(glow_names)}}};")
    c.append(f"const int GLOW_FONT_COUNT = {len(glow_names)};")
    c.append("")
    for weight in (500, 600, 700):
        pairs = build_kerning(weight)
        c.append(f"static const kern_t kern_{weight}[{len(pairs)}] = {{")
        c += [f"{{{key},{k}}}," for key, k in sorted(pairs)]
        c.append("};")
        total += len(pairs) * 4
        print(f"kerning {weight}: {len(pairs)} pairs")
    c.append("const kern_table_t KERNING[3] = {")
    for weight in (500, 600, 700):
        c.append(f"{{{weight}, kern_{weight}, sizeof kern_{weight} / sizeof kern_{weight}[0]}},")
    c.append("};")
    if sharp_out:
        Path(sharp_out).write_text("\n".join(c) + "\n")
        print(f"wrote {sharp_out}: about {total // 1024} KB")
        return
    OUT_C.parent.mkdir(parents=True, exist_ok=True)
    OUT_C.write_text("\n".join(c) + "\n")
    OUT_H.write_text(f"""// Generated by tools/gen_fonts.py from Manrope (SIL Open Font License). Do not edit.
// Manrope: Copyright 2018 The Manrope Project Authors (https://github.com/googlefonts/manrope). The license is in
// tools/fonts/OFL.txt: these glyphs are a Font Software derivative, under the same license.
#pragma once
#include <stdint.h>

#ifndef GLYPH_OFFSET_T
#define GLYPH_OFFSET_T int8_t // the simulator's sharp window has bigger glyphs
#endif

typedef struct {{
    uint16_t cp;   // Unicode code point
    GLYPH_OFFSET_T ox, oy; // the mask's top-left, relative to the pen on the baseline
    uint8_t w, h;  // the mask's size
    uint16_t adv16; // advance, in 1/16 px
    uint32_t off;  // offset into bits
}} glyph_t;

typedef struct {{
    const glyph_t *glyphs; // sorted by code point
    const uint8_t *bits;   // 8-bit alpha
    uint16_t count;
    uint16_t size10;       // size in px x 10
    uint16_t weight;
    int16_t ymin, ymax;    // the rows any glyph can touch, relative to the baseline
    uint8_t kinds;         // what it can show: FONT_DIGITS, FONT_LETTERS, FONT_OTHER
}} font_t;

enum {{ FONT_DIGITS = 1, FONT_LETTERS = 2, FONT_OTHER = 4 }};

typedef struct {{
    uint16_t pair; // the first character << 8 | the second
    int16_t k;     // 1/1000 em
}} kern_t;

typedef struct {{
    uint16_t weight;
    const kern_t *pairs; // sorted by pair
    uint32_t count;
}} kern_table_t;

#define FONT_FALLBACK 0xFFFD // an empty box, in every font that shows text from outside
#define GLOW_SCALE {GLOW_SCALE}         // a glow mask's cell is this many pixels wide and tall

extern const font_t *const FONTS[];
extern const int FONT_COUNT;
extern const font_t *const GLOW_FONTS[]; // the digits, blurred, at the sizes a Pomodoro's time shows
extern const int GLOW_FONT_COUNT;
extern const kern_table_t KERNING[3];
""")
    print(f"wrote {OUT_C.relative_to(ROOT)}: about {total // 1024} KB in flash, {OUT_C.stat().st_size // 1024} KB of source")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--sharp":
        main(sys.argv[2])
    elif len(sys.argv) == 1:
        main()
    else:
        sys.exit("usage: gen_fonts.py [--sharp OUT.c]")
