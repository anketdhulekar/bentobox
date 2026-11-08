// Text: UTF-8 with a fallback for anything the font doesn't have, kerning as the browser applies it, and the clock's
// tabular digits with their pulse and glow. Names come from outside, so nothing here trusts a string's bytes.
#include <math.h>
#include <string.h>

#include "../../hal/hal.h"
#include "../assets/fonts.h"
#include "gfx_internal.h"

// ---- UTF-8 --------------------------------------------------------------------------------------------------------

// The next character, stopping at the NUL. A broken sequence gives U+FFFD and moves one byte, so it can't run off.
static uint32_t next_cp(const char **ps)
{
    const uint8_t *p = (const uint8_t *)*ps;
    const uint32_t b = *p;
    if (b < 0x80) {
        *ps = (const char *)(p + 1);
        return b;
    }
    int extra = b >= 0xF0 && b < 0xF5 ? 3 : b >= 0xE0 && b < 0xF0 ? 2 : b >= 0xC2 && b < 0xE0 ? 1 : -1;
    if (extra < 0) {
        *ps = (const char *)(p + 1);
        return 0xFFFD;
    }
    uint32_t cp = b & (0x3Fu >> extra);
    for (int i = 1; i <= extra; i++) {
        if ((p[i] & 0xC0) != 0x80) {
            *ps = (const char *)(p + 1);
            return 0xFFFD;
        }
        cp = cp << 6 | (p[i] & 0x3Fu);
    }
    *ps = (const char *)(p + extra + 1);
    return cp;
}

// ---- Fonts --------------------------------------------------------------------------------------------------------

// What a string needs a font to hold. Some sizes hold only digits or only letters, so the nearest size isn't always one
// that can show it.
static uint8_t kinds_of(const char *s)
{
    uint8_t k = 0;
    for (; *s; s++) {
        const uint8_t c = (uint8_t)*s;
        if ((c >= '0' && c <= '9') || c == ':') k |= FONT_DIGITS;
        else if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')) k |= FONT_LETTERS;
        else if (c != ' ') k |= FONT_OTHER;
    }
    return k;
}

static const font_t *font_for(float size, int weight, uint8_t kinds)
{
    const int want = (int)(size * G.k * 10.0f + 0.5f);
    const font_t *best = NULL;
    int best_cost = 1 << 30;
    for (int i = 0; i < FONT_COUNT; i++) {
        const font_t *f = FONTS[i];
        if (kinds & ~f->kinds) continue;
        const int ds = f->size10 > want ? f->size10 - want : want - f->size10;
        const int dw = f->weight > weight ? f->weight - weight : weight - f->weight;
        const int cost = ds * 4 + dw / 10;
        if (cost < best_cost) {
            best_cost = cost;
            best = f;
        }
    }
    return best;
}

static const glyph_t *find_cp(const font_t *f, uint32_t cp)
{
    if (cp > 0xFFFF) return NULL;
    int lo = 0, hi = f->count - 1;
    while (lo <= hi) {
        const int mid = (lo + hi) / 2;
        const uint32_t c = f->glyphs[mid].cp;
        if (c == cp) return &f->glyphs[mid];
        if (c < cp) lo = mid + 1;
        else hi = mid - 1;
    }
    return NULL;
}

// Latin-1 and Latin Extended-A, as their base letters, for the sizes that have no accents.
static uint32_t strip_accent(uint32_t cp)
{
    static const char L1[] = "AAAAAAACEEEEIIIIDNOOOOOxOUUUUYTsaaaaaaaceeeeiiiidnooooo/ouuuuyty"; // U+00C0 .. U+00FF
    static const char LA[] =
        "AaAaAaCcCcCcCcDdDdEeEeEeEeEeGgGgGgGgHhHhIiIiIiIiIiJjJjKkkLlLlLlLlLlNnNnNnnNnOoOoOoOoRrRrRrSsSsSsSsTtTtTtUuUuUuUuUuUuWwYyYZzZzZzs";
    if (cp >= 0xC0 && cp <= 0xFF) return (uint8_t)L1[cp - 0xC0];
    if (cp >= 0x100 && cp <= 0x17F && cp - 0x100 < sizeof LA - 1) return (uint8_t)LA[cp - 0x100];
    return cp;
}

// The glyph for a character, or NULL if it shows nothing at all.
static const glyph_t *glyph_for(const font_t *f, uint32_t cp)
{
    if (cp == '\t' || cp == '\n' || cp == '\r' || cp == 0xA0 || cp == 0x2009 || cp == 0x202F) cp = ' ';
    // Marks and joiners take no space.
    if ((cp >= 0x300 && cp <= 0x36F) || cp == 0x200B || cp == 0x200C || cp == 0x200D || (cp >= 0xFE00 && cp <= 0xFE0F) || cp == 0xFEFF) return NULL;
    if (cp < 32 || cp == 0x7F) return NULL;
    const glyph_t *g = find_cp(f, cp);
    if (g) return g;
    uint32_t alt = cp;
    switch (cp) {
    case 0x2018: case 0x2019: case 0x201A: case 0x2032: alt = '\''; break;
    case 0x201C: case 0x201D: case 0x201E: case 0x2033: alt = '"'; break;
    case 0x2010: case 0x2011: case 0x2012: case 0x2013: case 0x2014: case 0x2212: alt = '-'; break;
    case 0x2022: case 0xB7: alt = '.'; break;
    default: alt = strip_accent(cp); break;
    }
    if (alt != cp && (g = find_cp(f, alt))) return g;
    return find_cp(f, FONT_FALLBACK);
}

// In 1/16 px, the kerning between two characters.
static int kern16(const font_t *f, uint32_t a, uint32_t b)
{
    if (a < 32 || a > 126 || b < 32 || b > 126) return 0;
    const kern_table_t *t = &KERNING[f->weight <= 500 ? 0 : f->weight <= 600 ? 1 : 2];
    const uint16_t key = (uint16_t)(a << 8 | b);
    int lo = 0, hi = (int)t->count - 1;
    while (lo <= hi) {
        const int mid = (lo + hi) / 2;
        const uint16_t p = t->pairs[mid].pair;
        if (p == key) return t->pairs[mid].k * f->size10 * 16 / 10000; // k/1000 em, at size10/10 px
        if (p < key) lo = mid + 1;
        else hi = mid - 1;
    }
    return 0;
}

// The width of s up to `end` (NULL: all of it), in 1/16 px.
static int width16(const font_t *f, const char *s, const char *end)
{
    int w = 0;
    uint32_t prev = 0;
    while (*s && (!end || s < end)) {
        const uint32_t cp = next_cp(&s);
        const glyph_t *g = glyph_for(f, cp);
        if (!g) continue;
        w += kern16(f, prev, cp) + g->adv16;
        prev = cp;
    }
    return w;
}

// Where to cut s so that it plus an ellipsis fits maxw (1/16 px). NULL if it fits whole.
static const char *cut_point(const font_t *f, const char *s, int maxw16, int *w_out)
{
    const int whole = width16(f, s, NULL);
    *w_out = whole;
    if (maxw16 <= 0 || whole <= maxw16) return NULL;
    const glyph_t *ell = find_cp(f, 0x2026);
    const int ew = ell ? ell->adv16 : 0;
    const char *p = s, *keep = s;
    int w = 0;
    uint32_t prev = 0;
    while (*p) {
        const char *q = p;
        const uint32_t cp = next_cp(&q);
        const glyph_t *g = glyph_for(f, cp);
        const int adv = g ? g->adv16 + kern16(f, prev, cp) : 0;
        if (w + adv + ew > maxw16) break;
        w += adv;
        p = q;
        prev = cp;
        if (cp != ' ') keep = p;
    }
    *w_out = width16(f, s, keep) + ew;
    return keep;
}

// ---- Drawing glyphs ----------------------------------------------------------------------------------------------

// A browser thickens text as it draws it, more for light text than for dark, so the approved design carries that weight:
// plain coverage is about 20% thinner on small sizes. These curves were fitted against the preview's own pixels.
#define TEXT_GAMMA_LIGHT 1.5f
#define TEXT_GAMMA_DARK 1.25f
static uint8_t s_cov[2][256];

void gfx_text_init(void)
{
    for (int i = 0; i < 256; i++) {
        const float c = (float)i / 255.0f;
        s_cov[0][i] = (uint8_t)(powf(c, 1.0f / TEXT_GAMMA_DARK) * 255.0f + 0.5f);
        s_cov[1][i] = (uint8_t)(powf(c, 1.0f / TEXT_GAMMA_LIGHT) * 255.0f + 0.5f);
    }
}

static const uint8_t *cov_for(uint16_t c)
{
    const int lum = 2 * ((c >> 11) << 1) + 5 * ((c >> 5) & 63) + ((c & 31) << 1); // 0 .. 504
    return s_cov[lum > 200];
}

static inline void blit_glyph(const font_t *f, const glyph_t *g, int pen16, int base, uint32_t s, uint32_t a256, const uint8_t *cov)
{
    if (!g->w) return;
    const int gx = ((pen16 + 8) >> 4) + g->ox, gy = base + g->oy;
    const uint8_t *bits = f->bits + g->off;
    int y0 = gy, y1 = gy + g->h;
    if (!gfx_rows(&y0, &y1)) return;
    int mx0 = 0, mx1 = g->w;
    if (gx < 0) mx0 = -gx;
    if (gx + mx1 > G.w) mx1 = G.w - gx;
    if (mx1 <= mx0) return;
    for (int y = y0; y < y1; y++) {
        if (!G.row_on[y]) continue;
        const uint8_t *src = bits + (y - gy) * g->w;
        uint16_t *row = G.target + y * G.w + gx;
        if (a256 >= 256) {
            for (int x = mx0; x < mx1; x++) {
                const uint32_t v = cov[src[x]];
                if (v) row[x] = gfx_blend(row[x], s, (v + 4) >> 3);
            }
        } else {
            for (int x = mx0; x < mx1; x++) {
                const uint32_t v = cov[src[x]] * a256 >> 8;
                if (v) row[x] = gfx_blend(row[x], s, (v + 4) >> 3);
            }
        }
    }
}

static inline uint32_t to_a256(int alpha) { return (uint32_t)alpha + (uint32_t)(alpha >> 7); }

float gfx_text(const char *s, float x, float y, float size, int weight, uint16_t c, gfx_align_t align, float maxw, int alpha)
{
    if (!s || !*s) return 0;
    const font_t *f = font_for(size, weight, kinds_of(s));
    if (!f) return 0;
    int w16;
    const char *cut = cut_point(f, s, (int)(maxw * G.k * 16.0f), &w16);
    const float X = gfx_dx(x);
    const int base = (int)(gfx_dy(y) + 0.5f);
    int pen16 = (int)(X * 16.0f);
    if (align == ALIGN_CENTER) pen16 -= w16 / 2;
    else if (align == ALIGN_RIGHT) pen16 -= w16;
    const float width = (float)w16 / (16.0f * G.k);
    if (alpha <= 0) return width;
    if (G.recording) {
        uint32_t words[5] = {(uint32_t)f->size10 << 16 | f->weight, (uint32_t)pen16, (uint32_t)base, c, (uint32_t)alpha};
        uint32_t h = gfx_hash_words(words, 5);
        for (const char *p = s; *p && (!cut || p < cut); p++) h = (h ^ (uint8_t)*p) * 16777619u;
        if (cut) h ^= 0x2026;
#ifdef BENTO_DEBUG_INTERFACE
        gfx_why_label(s, cut ? (int)(cut - s) : -1);
#endif
        gfx_mark_rows(base + f->ymin, base + f->ymax, h);
        return width;
    }
    const uint32_t split = GFX_SPLIT(c), a256 = to_a256(alpha);
    const uint8_t *cov = cov_for(c);
    uint32_t prev = 0;
    const char *p = s;
    while (*p && (!cut || p < cut)) {
        const uint32_t cp = next_cp(&p);
        const glyph_t *g = glyph_for(f, cp);
        if (!g) continue;
        pen16 += kern16(f, prev, cp);
        blit_glyph(f, g, pen16, base, split, a256, cov);
        pen16 += g->adv16;
        prev = cp;
    }
    if (cut) {
        const glyph_t *g = find_cp(f, 0x2026);
        if (g) blit_glyph(f, g, pen16, base, split, a256, cov);
    }
    return width;
}

float gfx_text_width(const char *s, float size, int weight)
{
    if (!s) return 0;
    const font_t *f = font_for(size, weight, kinds_of(s));
    if (!f) return 0;
    return (float)width16(f, s, NULL) / (16.0f * G.k);
}

void gfx_text_window(const char *s, float x, float y, float size, int weight, uint16_t c, float wx0, float wx1, float fade, int alpha)
{
    if (!s || !*s || alpha <= 0) return;
    const font_t *f = font_for(size, weight, kinds_of(s));
    if (!f) return;
    int pen16 = (int)(gfx_dx(x) * 16.0f);
    const int base = (int)(gfx_dy(y) + 0.5f);
    const int W0 = (int)gfx_dx(wx0), W1 = (int)gfx_dx(wx1), F = (int)(fade * G.k);
    if (G.recording) {
        uint32_t words[7] = {(uint32_t)f->size10 << 16 | f->weight, (uint32_t)pen16, (uint32_t)base, c, (uint32_t)W0, (uint32_t)W1, (uint32_t)alpha};
        uint32_t h = gfx_hash_words(words, 7);
        for (const char *p = s; *p; p++) h = (h ^ (uint8_t)*p) * 16777619u;
#ifdef BENTO_DEBUG_INTERFACE
        gfx_why_label(s, -1);
#endif
        gfx_mark_rows(base + f->ymin, base + f->ymax, h);
        return;
    }
    const uint32_t split = GFX_SPLIT(c), a256 = to_a256(alpha);
    const int finv = F > 0 ? 256 / F : 256; // the fade's slope; one division, not per pixel
    const uint8_t *cov = cov_for(c);
    uint32_t prev = 0;
    const char *p = s;
    while (*p) {
        const uint32_t cp = next_cp(&p);
        const glyph_t *g = glyph_for(f, cp);
        if (!g) continue;
        pen16 += kern16(f, prev, cp);
        const int gx = ((pen16 + 8) >> 4) + g->ox, gy = base + g->oy;
        if (g->w && gx < W1 && gx + g->w > W0) {
            int y0 = gy, y1 = gy + g->h;
            if (gfx_rows(&y0, &y1)) {
                const uint8_t *bits = f->bits + g->off;
                for (int yy = y0; yy < y1; yy++) {
                    if (!G.row_on[yy]) continue;
                    const uint8_t *src = bits + (yy - gy) * g->w;
                    uint16_t *row = G.target + yy * G.w;
                    for (int i = 0; i < g->w; i++) {
                        const int X = gx + i;
                        if (X < W0 || X >= W1 || !src[i]) continue;
                        const int e0 = X - W0, e1 = W1 - 1 - X, e = e0 < e1 ? e0 : e1;
                        uint32_t v = cov[src[i]] * a256 >> 8;
                        if (e < F) v = v * (uint32_t)(e * finv) >> 8;
                        if (v) row[X] = gfx_blend(row[X], split, (v + 4) >> 3);
                    }
                }
            }
        }
        pen16 += g->adv16;
        prev = cp;
    }
}

// ---- The clock's digits --------------------------------------------------------------------------------------------

// A glyph with its middle at the cell's middle, scaled around (scx, scy). Fixed point: 16.16, nothing per pixel in float.
static void blit_scaled(const font_t *f, const glyph_t *g, float left, float base, float scale, float scx, float scy, uint32_t split, uint32_t a256, const uint8_t *cov)
{
    if (!g->w) return;
    const float gl = left + (float)g->ox, gt = base + (float)g->oy;
    const int X0 = gfx_floor(scx + (gl - scx) * scale) - 1, X1 = gfx_ceil(scx + (gl + (float)g->w - scx) * scale) + 1;
    int Y0 = gfx_floor(scy + (gt - scy) * scale) - 1, Y1 = gfx_ceil(scy + (gt + (float)g->h - scy) * scale) + 1;
    if (!gfx_rows(&Y0, &Y1)) return;
    const float inv = 1.0f / scale;
    const int du = (int)(inv * 65536.0f);
    const int xa = X0 < 0 ? 0 : X0, xb = X1 > G.w ? G.w : X1;
    const int u0 = (int)((scx + ((float)xa + 0.5f - scx) * inv - gl - 0.5f) * 65536.0f);
    const uint8_t *bits = f->bits + g->off;
    const int w = g->w, h = g->h;
    for (int Y = Y0; Y < Y1; Y++) {
        if (!G.row_on[Y]) continue;
        const float vf = scy + ((float)Y + 0.5f - scy) * inv - gt - 0.5f;
        const int vi = gfx_floor(vf);
        if (vi < -1 || vi >= h) continue;
        const uint32_t fv = (uint32_t)((vf - (float)vi) * 256.0f);
        const uint8_t *r0 = vi >= 0 ? bits + vi * w : NULL, *r1 = vi + 1 < h ? bits + (vi + 1) * w : NULL;
        uint16_t *row = G.target + Y * G.w;
        int u = u0;
        for (int X = xa; X < xb; X++, u += du) {
            const int ui = u >> 16;
            if (ui < -1 || ui >= w) continue;
            const uint32_t fu = (uint32_t)(u & 0xFFFF) >> 8;
            const uint32_t a = r0 && ui >= 0 ? r0[ui] : 0, b = r0 && ui + 1 < w ? r0[ui + 1] : 0;
            const uint32_t c = r1 && ui >= 0 ? r1[ui] : 0, d = r1 && ui + 1 < w ? r1[ui + 1] : 0;
            const uint32_t top = a * (256 - fu) + b * fu, bot = c * (256 - fu) + d * fu;
            uint32_t v = cov[(top * (256 - fv) + bot * fv) >> 16 & 255];
            if (a256 < 256) v = v * a256 >> 8;
            if (v) row[X] = gfx_blend(row[X], split, (v + 4) >> 3);
        }
    }
}

// The glow's picture: the blurred digits, scaled up to full size into one mask. Rebuilt when the digits change, about
// once a second, so that each frame only multiplies it by the pulse's strength.
static struct {
    uint8_t *px;
    int cap_w, cap_h;
    int x0, y0, w, h; // in pixels
    uint32_t key;
} L;

static const font_t *glow_font(const font_t *digits)
{
    for (int i = 0; i < GLOW_FONT_COUNT; i++) {
        if (GLOW_FONTS[i]->size10 == digits->size10) return GLOW_FONTS[i];
    }
    return NULL;
}

static void glow_build(const font_t *gf, const char *s, const int *cell_x16, int n, int base)
{
    if (!L.px) {
        L.cap_w = G.w;
        L.cap_h = G.h * 3 / 4;
        L.px = hal_alloc_large((size_t)L.cap_w * (size_t)L.cap_h);
    }
    if (!L.px) return;
    // Where the digits' blur falls.
    int x0 = G.w, x1 = 0, y0 = G.h, y1 = 0;
    for (int i = 0; i < n; i++) {
        const glyph_t *g = find_cp(gf, (uint8_t)s[i]);
        if (!g) continue;
        const int left = ((cell_x16[i] - g->adv16 / 2 + 8) >> 4) + g->ox * GLOW_SCALE, top = base + g->oy * GLOW_SCALE;
        if (left < x0) x0 = left;
        if (left + g->w * GLOW_SCALE > x1) x1 = left + g->w * GLOW_SCALE;
        if (top < y0) y0 = top;
        if (top + g->h * GLOW_SCALE > y1) y1 = top + g->h * GLOW_SCALE;
    }
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > G.w) x1 = G.w;
    if (y1 > G.h) y1 = G.h;
    L.x0 = x0;
    L.y0 = y0;
    L.w = x1 - x0;
    L.h = y1 - y0;
    if (L.h > L.cap_h) L.h = L.cap_h;
    if (L.w <= 0 || L.h <= 0) return;
    memset(L.px, 0, (size_t)L.w * (size_t)L.h);
    for (int i = 0; i < n; i++) {
        const glyph_t *g = find_cp(gf, (uint8_t)s[i]);
        if (!g) continue;
        const int left = ((cell_x16[i] - g->adv16 / 2 + 8) >> 4) + g->ox * GLOW_SCALE, top = base + g->oy * GLOW_SCALE;
        const uint8_t *bits = gf->bits + g->off;
        const int gw = g->w, gh = g->h;
        for (int yy = 0; yy < gh * GLOW_SCALE; yy++) {
            const int Y = top + yy - L.y0;
            if (Y < 0 || Y >= L.h) continue;
            // In eighths of a cell: a cell's value is at its middle.
            const int v8 = yy * 2 - 3, vi = v8 >> 3;
            const uint32_t fv = (uint32_t)(v8 & 7);
            const uint8_t *r0 = vi >= 0 && vi < gh ? bits + vi * gw : NULL, *r1 = vi + 1 >= 0 && vi + 1 < gh ? bits + (vi + 1) * gw : NULL;
            uint8_t *out = L.px + Y * L.w;
            for (int xx = 0; xx < gw * GLOW_SCALE; xx++) {
                const int X = left + xx - L.x0;
                if (X < 0 || X >= L.w) continue;
                const int u8 = xx * 2 - 3, ui = u8 >> 3;
                const uint32_t fu = (uint32_t)(u8 & 7);
                const uint32_t a = r0 && ui >= 0 && ui < gw ? r0[ui] : 0, b = r0 && ui + 1 >= 0 && ui + 1 < gw ? r0[ui + 1] : 0;
                const uint32_t c = r1 && ui >= 0 && ui < gw ? r1[ui] : 0, d = r1 && ui + 1 >= 0 && ui + 1 < gw ? r1[ui + 1] : 0;
                const uint32_t v = ((a * (8 - fu) + b * fu) * (8 - fv) + (c * (8 - fu) + d * fu) * fv) >> 6;
                const uint32_t sum = out[X] + v;
                out[X] = sum > 255 ? 255 : (uint8_t)sum;
            }
        }
    }
}

float gfx_time_width(const char *s, float size)
{
    const font_t *f = font_for(size, 600, FONT_DIGITS);
    if (!f || !s) return 0;
    const glyph_t *zero = find_cp(f, '0'), *colon = find_cp(f, ':');
    if (!zero || !colon) return 0;
    const int dw = zero->adv16, cw = colon->adv16 * 9 / 10;
    int w16 = 0;
    for (int i = 0; s[i] && i < 8; i++) w16 += s[i] == ':' ? cw : dw;
    return (float)w16 / 16.0f / G.k;
}

void gfx_time(const char *s, float cx, float y, float size, uint16_t c, float scale, int alpha, float shine)
{
    scale = gfx_snap(scale, GFX_Q_ANGLE); // as hashed: the pulse and its glow draw from what the change check saw
    shine = gfx_snap(shine, GFX_Q_ANGLE);
    const font_t *f = font_for(size, 600, FONT_DIGITS);
    if (!f || !s) return;
    const glyph_t *zero = find_cp(f, '0'), *colon = find_cp(f, ':');
    if (!zero || !colon) return;
    const int dw = zero->adv16, cw = colon->adv16 * 9 / 10;
    int n = (int)strlen(s), w16 = 0;
    if (n > 8) n = 8;
    for (int i = 0; i < n; i++) w16 += s[i] == ':' ? cw : dw;
    const float CX = gfx_dx(cx);
    const int base = (int)(gfx_dy(y) + 0.5f);
    int cells[8];
    int px = (int)(CX * 16.0f) - w16 / 2;
    for (int i = 0; i < n; i++) {
        const int w = s[i] == ':' ? cw : dw;
        cells[i] = px + w / 2;
        px += w;
    }
    const float scy = (float)base - (float)f->size10 * 0.036f;
    const font_t *gf = shine > 0.004f ? glow_font(f) : NULL;
    if (G.recording) {
        const uint32_t words[8] = {(uint32_t)f->size10, (uint32_t)cells[0], (uint32_t)base, c, (uint32_t)alpha, (uint32_t)n,
                                   gfx_hash_q(scale, GFX_Q_ANGLE), gfx_hash_q(shine, GFX_Q_ANGLE)};
        uint32_t h = gfx_hash_words(words, 8);
        for (int i = 0; i < n; i++) h = (h ^ (uint8_t)s[i]) * 16777619u;
        // The scaled digits, and the glow around them.
        const int grow = gf ? f->size10 * 65 / 1000 + 6 : (int)((float)f->size10 * 0.1f * (scale - 1)) + 3;
#ifdef BENTO_DEBUG_INTERFACE
        gfx_why_label(s, n);
#endif
        gfx_mark_rows(base + f->ymin - grow, base + f->ymax + grow, h);
        return;
    }
    if (gf) {
        // Only the digits' string and place decide the picture.
        uint32_t key = (uint32_t)gf->size10 * 31 + (uint32_t)cells[0] * 7 + (uint32_t)base;
        for (int i = 0; i < n; i++) key = key * 16777619u ^ (uint8_t)s[i];
        if (key != L.key) {
            glow_build(gf, s, cells, n, base);
            L.key = key;
        }
        if (L.px && L.w > 0) {
            const uint32_t split = GFX_SPLIT(c);
            const uint32_t k = (uint32_t)(gfx_clamp01(shine) * (float)to_a256(alpha));
            for (int yy = 0; yy < L.h; yy++) {
                const int Y = L.y0 + yy;
                if (Y < G.cy0 || Y >= G.cy1 || !G.row_on[Y]) continue;
                const uint8_t *m = L.px + yy * L.w;
                uint16_t *row = G.target + Y * G.w + L.x0;
                for (int x = 0; x < L.w; x++) {
                    const uint32_t v = m[x] * k >> 8;
                    if (v) row[x] = gfx_blend(row[x], split, (v + 4) >> 3);
                }
            }
        }
    }
    const uint32_t split = GFX_SPLIT(c), a256 = to_a256(alpha);
    const uint8_t *cov = cov_for(c);
    const bool plain = scale > 0.9995f && scale < 1.0005f;
    for (int i = 0; i < n; i++) {
        const glyph_t *g = find_cp(f, (uint8_t)s[i]);
        if (!g) continue;
        const int pen16 = cells[i] - g->adv16 / 2;
        if (plain) blit_glyph(f, g, pen16, base, split, a256, cov);
        else blit_scaled(f, g, (float)pen16 / 16.0f, (float)base, scale, CX, scy, split, a256, cov);
    }
}
