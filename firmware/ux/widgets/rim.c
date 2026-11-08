// The rim around a clock, the arc above an agent, and the soft glow when an agent needs you.
//
// Round or rounded-square, these are big anti-aliased shapes, some redrawn every frame. So each is prebuilt once, as a
// list of the pixels it can touch: for each, how far it lies from the path, and how far along it. A frame then only compares integers and looks up a table, with no float math per pixel.
#include <math.h>
#include <string.h>

#include "../app.h"
#include "../gfx/gfx_internal.h"

#define HOLD_RING_R 128.0f // in the units of a 360 panel
#define HOLD_RING_W 4.0f

typedef struct { int16_t y, x0; uint16_t len; uint32_t off; } span_t;

typedef struct {
    span_t *spans;
    int nspans;
    uint16_t *g;   // how far along: 0 .. 65535
    uint8_t *d;    // distance from the path, in pixels x the map's steps
    float perim;   // the path's length in px
    float margin;  // g runs a little past each end of an arc that isn't the whole path: room for anti-aliasing
    uint8_t band[256];
    int id;
} rim_map_t;

typedef struct {
    span_t *spans;
    int nspans;
    uint8_t *v;
} glow_map_t;

static rim_map_t s_clock, s_arch, s_hold;
static glow_map_t s_glow_edge, s_glow_rim;
static uint8_t s_glow_lut[16][64];
static bool s_ready;
static float s_arch_f0;

void rim_point(float f, float inset, float radius, float *px, float *py)
{
    const float size = F.L->base, c = size / 2;
    f -= floorf(f);
    if (radius == 0) {
        const float a = f * PI_F * 2 - PI_F / 2, r = c - inset;
        *px = c + r * cosf(a);
        *py = c + r * sinf(a);
        return;
    }
    const float m = inset, r = radius, side = size - 2 * m - 2 * r, q = PI_F * r / 2, P = 4 * side + 4 * q;
    float s = f * P;
    if (s <= side / 2) { *px = c + s; *py = m; return; }
    s -= side / 2;
    const float cx[4] = {size - m - r, size - m - r, m + r, m + r};
    const float cy[4] = {m + r, size - m - r, size - m - r, m + r};
    const float a0[4] = {-PI_F / 2, 0, PI_F / 2, PI_F};
    for (int i = 0; i < 4; i++) {
        if (s <= q) {
            const float a = a0[i] + (s / q) * PI_F / 2;
            *px = cx[i] + r * cosf(a);
            *py = cy[i] + r * sinf(a);
            return;
        }
        s -= q;
        const float len = i < 3 ? side : side / 2;
        if (s <= len) {
            if (i == 0) { *px = size - m; *py = m + r + s; }
            else if (i == 1) { *px = size - m - r - s; *py = size - m; }
            else if (i == 2) { *px = m; *py = size - m - r - s; }
            else { *px = m + r + s; *py = m; }
            return;
        }
        s -= len;
    }
    *px = c;
    *py = m;
}

float rim_arch_start(float inset, float radius, float y)
{
    float lo = 0.5f, hi = 1;
    for (int i = 0; i < 24; i++) {
        const float f = (lo + hi) / 2;
        float px, py;
        rim_point(f, inset, radius, &px, &py);
        if (py > y) lo = f;
        else hi = f;
    }
    return lo;
}

// Signed distance from a point to the path (outward positive), and, in *rp, the radius around its corner's center.
static float path_distance(float x, float y, float inset, float radius, float *rp)
{
    const float size = F.L->base, c = size / 2;
    if (radius == 0) {
        const float dx = x - c, dy = y - c, r = gfx_sqrt(dx * dx + dy * dy);
        *rp = r;
        return r - (c - inset);
    }
    const float m = inset, xl = m + radius, xr = size - m - radius;
    const float qx = x > xr ? x - xr : x < xl ? xl - x : 0, qy = y > xr ? y - xr : y < xl ? xl - y : 0;
    *rp = 0;
    if (qx > 0 && qy > 0) {
        const float r = gfx_sqrt(qx * qx + qy * qy);
        *rp = r;
        return r - radius;
    }
    const float dl = m - x, dr = x - (size - m), dt = m - y, db = y - (size - m);
    float d = dl;
    if (dr > d) d = dr;
    if (dt > d) d = dt;
    if (db > d) d = db;
    return d;
}

// How far along the path (0 .. 1, clockwise from the top's middle) a point near it lies.
static float path_param(float x, float y, float inset, float radius)
{
    const float size = F.L->base, c = size / 2;
    if (radius == 0) {
        float a = atan2f(x - c, c - y);
        if (a < 0) a += 2 * PI_F;
        return a / (2 * PI_F);
    }
    const float m = inset, r = radius, side = size - 2 * m - 2 * r, q = PI_F * r / 2, P = 4 * side + 4 * q;
    const float xl = m + r, xr = size - m - r;
    float s;
    if (x > xr && y < xl) s = side / 2 + atan2f(x - xr, xl - y) * r;
    else if (x > xr && y > xr) s = side / 2 + q + side + atan2f(y - xr, x - xr) * r;
    else if (x < xl && y > xr) s = side / 2 + 2 * q + 2 * side + atan2f(xl - x, y - xr) * r;
    else if (x < xl && y < xl) s = side / 2 + 3 * q + 3 * side + atan2f(xl - y, xl - x) * r;
    else if (x >= xl && x <= xr) {
        if (y < c) s = x >= c ? x - c : P - (c - x);
        else s = side / 2 + 2 * q + side + (xr - x);
    } else {
        if (x > c) s = side / 2 + q + (y - xl);
        else s = side / 2 + 3 * q + 2 * side + (xr - y);
    }
    return s / P;
}

static float path_length(float inset, float radius)
{
    const float size = F.L->base;
    if (radius == 0) return 2 * PI_F * (size / 2 - inset);
    const float side = size - 2 * inset - 2 * radius;
    return 4 * side + 2 * PI_F * radius;
}

// f0 .. f1 (which may pass 1): the part of the path the map covers. A map of the whole path keeps g = f.
static void build_rim_map(rim_map_t *m, int id, float inset, float radius, float width, float f0, float f1)
{
    const int W = G.w, H = G.h;
    const float k = G.k, invk = 1.0f / k;
    // Anti-aliasing works in pixels, and the path is in layout units. A byte holds the distance, 16 steps a pixel when
    // the band's that narrow, and fewer when a bigger panel makes it wider.
    const float reach_px = width * k / 2 + 1.5f, reach = reach_px * invk;
    const float steps = 255.0f / reach_px < 16.0f ? 255.0f / reach_px : 16.0f;
    const bool whole = f1 - f0 >= 0.999f;
    const float span = f1 - f0;
    const float len = path_length(inset, radius);
    const float margin = whole ? 0 : 2.0f / (len * span); // room for the ends' anti-aliasing, in g

    // First a count, to allocate only what's needed.
    int count = 0, nspans = 0;
    for (int pass = 0; pass < 2; pass++) {
        if (pass) {
            m->spans = hal_alloc_large((size_t)nspans * sizeof(span_t));
            m->g = hal_alloc_large((size_t)count * sizeof(uint16_t));
            m->d = hal_alloc_large((size_t)count);
            if (!m->spans || !m->g || !m->d) return;
            count = 0;
            nspans = 0;
        }
        for (int y = 0; y < H; y++) {
            const float ly = ((float)y + 0.5f) * invk;
            int run = -1;
            for (int x = 0; x <= W; x++) {
                bool in = false;
                float d = 0, rp = 0;
                if (x < W) {
                    const float lx = ((float)x + 0.5f) * invk;
                    d = path_distance(lx, ly, inset, radius, &rp);
                    in = d > -reach && d < reach;
                    float f = 0;
                    if (in && (!whole || pass)) f = path_param(lx, ly, inset, radius);
                    float g = f;
                    if (in && !whole) {
                        g = f - f0;
                        if (g < -0.5f) g += 1; // past the top
                        g /= span;
                        if (g < -margin || g > 1 + margin) in = false;
                        else g = (g + margin) / (1 + 2 * margin);
                    }
                    if (in && pass) {
                        m->g[count + (run >= 0 ? x - run : 0)] = (uint16_t)(g * 65535.0f);
                        const float ad = d < 0 ? -d : d;
                        const float q = ad * k * steps;
                        m->d[count + (run >= 0 ? x - run : 0)] = q > 255 ? 255 : (uint8_t)q;
                    }
                }
                if (in && run < 0) {
                    run = x; // a span starts here: this pixel was just written at its offset 0, above
                } else if (!in && run >= 0) {
                    if (pass) {
                        m->spans[nspans] = (span_t){(int16_t)y, (int16_t)run, (uint16_t)(x - run), (uint32_t)count};
                    }
                    nspans++;
                    count += x - run;
                    run = -1;
                }
            }
        }
    }
    m->nspans = nspans;
    m->id = id;
    m->margin = margin;
    m->perim = len * k * (whole ? 1 : span);
    for (int i = 0; i < 256; i++) {
        const float dist = (float)i / steps;
        m->band[i] = (uint8_t)(gfx_clamp01(width * k / 2 + 0.5f - dist) * 255.0f);
    }
}

static void build_glow_map(glow_map_t *m, float inset, float radius)
{
    const int W = G.w, H = G.h;
    const float invk = 1.0f / G.k, K360 = F.L->base / 360.0f;
    // The preview draws 18 strokes, from 40 wide down to 2, each at 3.4% (times the strength): what matters at a
    // distance is how many of them reach it. In steps of 1/4 px, in units of 1/14 stroke.
    uint8_t profile[256];
    for (int i = 0; i < 256; i++) {
        const float dist = (float)i / 4.0f;
        float sum = 0;
        for (int s = 0; s < 18; s++) {
            const float w = (40.0f + (2.0f - 40.0f) * (float)s / 17.0f) * K360;
            sum += gfx_clamp01(w / 2 + 0.5f - dist);
        }
        const float v = sum * 14.0f;
        profile[i] = v > 255 ? 255 : (uint8_t)v;
    }
    const float reach = 21.0f * K360;
    int count = 0, nspans = 0;
    for (int pass = 0; pass < 2; pass++) {
        if (pass) {
            m->spans = hal_alloc_large((size_t)nspans * sizeof(span_t));
            m->v = hal_alloc_large((size_t)count);
            if (!m->spans || !m->v) return;
            count = 0;
            nspans = 0;
        }
        for (int y = 0; y < H; y++) {
            const float ly = ((float)y + 0.5f) * invk;
            int run = -1;
            for (int x = 0; x <= W; x++) {
                bool in = false;
                uint8_t val = 0;
                if (x < W) {
                    float rp;
                    const float d = path_distance(((float)x + 0.5f) * invk, ly, inset, radius, &rp);
                    in = d > -reach && d < reach;
                    if (in) {
                        const float ad = (d < 0 ? -d : d) * 4.0f;
                        val = profile[ad > 255 ? 255 : (int)ad];
                        if (!val) in = false;
                    }
                }
                if (in && pass) m->v[count + (run >= 0 ? x - run : 0)] = val;
                if (in && run < 0) {
                    run = x;
                } else if (!in && run >= 0) {
                    if (pass) m->spans[nspans] = (span_t){(int16_t)y, (int16_t)run, (uint16_t)(x - run), (uint32_t)count};
                    nspans++;
                    count += x - run;
                    run = -1;
                }
            }
        }
    }
    m->nspans = nspans;
}

static void rim_build_all(void)
{
    s_ready = true;
    const layout_t *L = F.L;
    build_rim_map(&s_clock, 1, L->rim.inset, L->rim.radius, L->rim.width, 0, 1);
    const lay_arch_t *A = &L->arch;
    // On a circle, a wide band reaches the arch's bottom later at its outer edge than along its middle. So it runs on until
    // that edge does, and clock_draw_arch cuts it off flat at the bottom.
    s_arch_f0 = rim_arch_start(A->radius ? A->inset : A->inset - A->width / 2, A->radius, A->bottom);
    build_rim_map(&s_arch, 2, A->inset, A->radius, A->width, s_arch_f0, 2 - s_arch_f0);
    const float k360 = L->base / 360.0f;
    build_rim_map(&s_hold, 3, L->base / 2 - HOLD_RING_R * k360, 0, HOLD_RING_W * k360, 0, 1);
    const float glow_r = L->rim.radius;
    const float r0 = glow_r ? 40.0f : 0, r1 = glow_r ? (40 - L->rim.width > 8 ? 40 - L->rim.width : 8) : 0;
    build_glow_map(&s_glow_edge, 0, r0);
    build_glow_map(&s_glow_rim, L->rim.width, r1);
    for (int level = 0; level < 16; level++) {
        const float strength = (float)level / 15.0f;
        for (int v = 0; v < 64; v++) {
            const float a = 1.0f - powf(1.0f - 0.034f * strength, (float)(v * 4) / 14.0f);
            s_glow_lut[level][v] = (uint8_t)(a * 32.0f + 0.5f);
        }
    }
}

static void draw_map(const rim_map_t *m, float from, float to, uint16_t c, int base256)
{
    if (!s_ready) rim_build_all();
    if (!m->spans) return;
    // A quarter of a pixel is too small to show: this keeps a slow arc from redrawing the whole rim every frame.
    const int step = (int)(65535.0f / (m->perim * 2.0f)) + 1;
    const float scale = 65535.0f / (1 + 2 * m->margin);
    int g1 = (int)((to + m->margin) * scale);
    g1 -= g1 % step;
    const int g0 = (int)((from + m->margin) * scale);
    const bool full = m->margin == 0 && from <= 0 && to >= 0.9995f;
    if (full) g1 = 65535;
    const int ox = G.ox, oy = G.oy;
    if (G.recording) {
        const uint32_t w[7] = {(uint32_t)m->id, (uint32_t)g0, (uint32_t)g1, c, (uint32_t)base256, (uint32_t)ox, (uint32_t)oy};
        // The whole of the path's rows: an arc's end moving anywhere only redraws them about once a second.
        const int y0 = m->spans[0].y + oy, y1 = m->spans[m->nspans - 1].y + 1 + oy;
        gfx_mark_rows(y0, y1, gfx_hash_words(w, 7));
        return;
    }
    const uint32_t split = GFX_SPLIT(c);
    const int ke = (int)(256.0f * 256.0f * m->perim / 65535.0f) + 1; // coverage per g, 8.8
    const int cy0 = G.cy0, cy1 = G.cy1, W = G.w;
    for (int sp = 0; sp < m->nspans; sp++) {
        const span_t *s = &m->spans[sp];
        const int Y = s->y + oy;
        if (Y < cy0 || Y >= cy1 || !G.row_on[Y]) continue;
        uint16_t *row = G.target + Y * W;
        const uint16_t *pg = m->g + s->off;
        const uint8_t *pd = m->d + s->off;
        for (int i = 0; i < s->len; i++) {
            const int X = s->x0 + i + ox;
            if (X < 0 || X >= W) continue;
            const int dv = pd[i];
            int a = m->band[dv];
            if (!full) {
                const int g = pg[i];
                const int e0 = g - g0, e1 = g1 - g, e = e0 < e1 ? e0 : e1;
                int cov = e <= 0 ? 0 : (e * ke) >> 8;
                if (cov > 256) cov = 256;
                const int out = (a * base256) >> 8;
                a = out + (((a - out) * cov) >> 8);
            }
            if (a > 0) row[X] = gfx_blend(row[X], split, (uint32_t)(a + 4) >> 3);
        }
    }
}

void rim_draw_clock_ring(uint16_t color) { draw_map(&s_clock, 0, 1, color, 256); }

void rim_draw_clock_arc(uint16_t color, float fraction)
{
    draw_map(&s_clock, 0, fraction, color, 41); // the rest of the ring at 16%
}

// A press on its way to a hold: this ring fills from twelve o'clock, and the hold fires as it closes.
void rim_draw_hold(uint16_t color, float fraction) { draw_map(&s_hold, 0, fraction, color, 0); }

void rim_draw_arch_ring(uint16_t color) { draw_map(&s_arch, 0, 1, color, 0); }

void rim_draw_arch_arc(uint16_t color, float fraction)
{
    draw_map(&s_arch, 0, fraction, color, 41); // the rest of the arc at 16%, as on the full rim
}

void rim_glow(uint16_t color, float strength, float inset)
{
    if (!s_ready) rim_build_all();
    const glow_map_t *m = inset > 0 ? &s_glow_rim : &s_glow_edge;
    if (!m->spans) return;
    int level = (int)(gfx_clamp01(strength) * 15.0f + 0.5f);
    const int ox = G.ox, oy = G.oy;
    if (G.recording) {
        const uint32_t w[5] = {inset > 0 ? 77u : 78u, (uint32_t)level, color, (uint32_t)ox, (uint32_t)oy};
        gfx_mark_rows(0, G.h, gfx_hash_words(w, 5));
        return;
    }
    const uint32_t split = GFX_SPLIT(color);
    const uint8_t *lut = s_glow_lut[level];
    const int W = G.w;
    for (int sp = 0; sp < m->nspans; sp++) {
        const span_t *s = &m->spans[sp];
        const int Y = s->y + oy;
        if (Y < G.cy0 || Y >= G.cy1 || !G.row_on[Y]) continue;
        uint16_t *row = G.target + Y * W;
        const uint8_t *pv = m->v + s->off;
        for (int i = 0; i < s->len; i++) {
            const int X = s->x0 + i + ox;
            if (X < 0 || X >= W) continue;
            const uint32_t a = lut[pv[i] >> 2];
            if (a) row[X] = gfx_blend(row[X], split, a);
        }
    }
}
