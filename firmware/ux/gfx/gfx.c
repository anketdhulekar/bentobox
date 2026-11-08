#include "gfx_internal.h"

#include <math.h>
#include <stdint.h>
#include <string.h>

#include "../../hal/hal.h"

gfx_state_t G;

void gfx_init(int width, int height, float k)
{
    G.w = width;
    G.h = height;
    G.k = k;
    G.bands = (height + HAL_BAND_ROWS - 1) / HAL_BAND_ROWS;
    if (G.bands > GFX_MAX_BANDS) G.bands = GFX_MAX_BANDS;
    G.fb = hal_alloc_large((size_t)width * (size_t)height * sizeof(uint16_t));
    G.row_on = hal_alloc_large((size_t)height);
    if (G.fb) memset(G.fb, 0, (size_t)width * (size_t)height * sizeof(uint16_t));
    G.cy0 = 0;
    G.cy1 = height;
    G.have_prev = false;
    gfx_text_init();
}

int gfx_width(void) { return G.w; }
int gfx_height(void) { return G.h; }
float gfx_scale(void) { return G.k; }
uint16_t *gfx_framebuffer(void) { return G.fb; }
int gfx_band_count(void) { return G.bands; }

void gfx_set_offset(int ox, int oy)
{
    G.ox = (int)lroundf((float)ox * G.k);
    G.oy = (int)lroundf((float)oy * G.k);
}

#ifdef BENTO_DEBUG_INTERFACE
#define WHY_MAX 384
static struct {
    bool on;
    int cur, n[2];
    char label[16];
    uint32_t words[18];
    gfx_mark_t (*m)[WHY_MAX]; // two frames' marks, in PSRAM: taken the first time it's asked for
} W;

void gfx_why(bool on)
{
    if (on && !W.m) W.m = hal_alloc_large(2 * WHY_MAX * sizeof(gfx_mark_t));
    W.on = on && W.m;
    W.n[0] = W.n[1] = 0;
    W.label[0] = 0;
}

void gfx_why_label(const char *s, int len)
{
    if (!W.on) return;
    if (len < 0 || len > (int)sizeof W.label - 1) len = (int)sizeof W.label - 1;
    int i = 0;
    for (; i < len && s[i]; i++) W.label[i] = s[i];
    W.label[i] = 0;
}

void gfx_why_words(const void *words, int n)
{
    if (!W.on) return;
    memset(W.words, 0, sizeof W.words);
    memcpy(W.words, words, (size_t)(n < 18 ? n : 18) * 4);
}

int gfx_why_diff(gfx_mark_t *out, gfx_mark_t *was_out, int max, int *n, int *prev_n, uint32_t *ref)
{
    *n = *prev_n = 0;
    *ref = 0;
    if (!W.m) return 0;
    const gfx_mark_t *now = W.m[W.cur], *was = W.m[W.cur ^ 1];
    *n = W.n[W.cur];
    *prev_n = W.n[W.cur ^ 1];
    *ref = (uint32_t)(uintptr_t)&gfx_mark_rows;
    int k = 0;
    for (int i = 0; i < *n && k < max; i++) {
        const bool same = i < *prev_n && now[i].hash == was[i].hash && now[i].y0 == was[i].y0 && now[i].y1 == was[i].y1;
        if (same) continue;
        out[k] = now[i];
        if (i < *prev_n) was_out[k] = was[i];
        else memset(&was_out[k], 0, sizeof was_out[k]);
        k++;
    }
    return k;
}
#endif

void gfx_record_begin(void)
{
#ifdef BENTO_DEBUG_INTERFACE
    if (W.on) {
        W.cur ^= 1;
        W.n[W.cur] = 0;
    }
#endif
    G.recording = true;
    G.cy0 = 0;
    G.cy1 = G.h;
    for (int i = 0; i < G.bands; i++) G.hash[i] = 2166136261u;
}

uint64_t gfx_record_end(void)
{
    G.recording = false;
    uint64_t dirty = 0;
    for (int i = 0; i < G.bands; i++) {
        if (!G.have_prev || G.hash[i] != G.prev[i]) dirty |= 1ULL << i;
        G.prev[i] = G.hash[i];
    }
    G.have_prev = true;
    return dirty;
}

void gfx_paint_begin(uint64_t bands, uint16_t *target)
{
    G.recording = false;
    G.target = target ? target : G.fb;
    G.cy0 = 0;
    G.cy1 = G.h;
    for (int y = 0; y < G.h; y++) {
        const int b = y / HAL_BAND_ROWS;
        G.row_on[y] = b < GFX_MAX_BANDS && ((bands >> b) & 1);
    }
}

void gfx_paint_end(void) { G.target = G.fb; }

void gfx_clip_rows(float y0, float y1)
{
    G.cy0 = gfx_floor(gfx_dy(y0));
    G.cy1 = gfx_ceil(gfx_dy(y1));
    if (G.cy0 < 0) G.cy0 = 0;
    if (G.cy1 > G.h) G.cy1 = G.h;
}

void gfx_clip_reset(void)
{
    G.cy0 = 0;
    G.cy1 = G.h;
}

uint32_t gfx_hash_words(const void *words, int n)
{
    const uint32_t *p = words;
    uint32_t h = 2166136261u;
    for (int i = 0; i < n; i++) {
        h ^= p[i];
        h *= 16777619u;
        h ^= h >> 15;
    }
    return h;
}

void gfx_mark_rows(int y0, int y1, uint32_t h)
{
#ifdef BENTO_DEBUG_INTERFACE
    if (W.on && W.n[W.cur] < WHY_MAX) {
        gfx_mark_t *m = &W.m[W.cur][W.n[W.cur]++];
        m->hash = h;
        m->at = (uint32_t)(uintptr_t)__builtin_return_address(0);
        m->y0 = (int16_t)y0;
        m->y1 = (int16_t)y1;
        memcpy(m->text, W.label, sizeof m->text);
        memcpy(m->words, W.words, sizeof m->words);
        W.label[0] = 0;
        memset(W.words, 0, sizeof W.words);
    }
#endif
    if (!gfx_rows(&y0, &y1)) return;
    const int last = (y1 - 1) / HAL_BAND_ROWS;
    for (int b = y0 / HAL_BAND_ROWS; b <= last && b < G.bands; b++) G.hash[b] = (G.hash[b] ^ h) * 16777619u;
}

// Primitive kinds, so that the same numbers from two primitives hash differently.
enum { K_RECT = 1, K_BLOCK, K_DISC, K_RING, K_RRECT, K_RRLINE, K_ICON, K_CLEAR };

static void mark_shape(uint32_t kind, float a, float b, float c, float d, float e, float f, uint16_t color, int alpha, int y0, int y1)
{
    const uint32_t w[9] = {gfx_hash_q(a, GFX_Q_PX), gfx_hash_q(b, GFX_Q_PX), gfx_hash_q(c, GFX_Q_PX), gfx_hash_q(d, GFX_Q_PX),
                           gfx_hash_q(e, GFX_Q_PX), gfx_hash_q(f, GFX_Q_PX), kind, color, (uint32_t)alpha};
#ifdef BENTO_DEBUG_INTERFACE
    gfx_why_words(w, 6);
#endif
    gfx_mark_rows(y0, y1, gfx_hash_words(w, 9));
}

// ---- Filled spans -------------------------------------------------------------------------------------------------

static inline void fill_span(uint16_t *row, int x0, int x1, uint16_t c, uint32_t s, uint32_t a32)
{
    if (a32 >= 32) {
        for (int x = x0; x < x1; x++) row[x] = c;
    } else if (a32) {
        for (int x = x0; x < x1; x++) row[x] = gfx_blend(row[x], s, a32);
    }
}

static inline uint32_t to_a32(int alpha) { return (uint32_t)(alpha + 4) >> 3; }

// A pixel with this coverage (0..1) of a color at this alpha.
static inline void put_cov(uint16_t *row, int x, float cov, uint32_t s, int alpha)
{
    const uint32_t a = (uint32_t)(cov * (float)alpha + 0.5f);
    if (a) row[x] = gfx_blend(row[x], s, (a + 4) >> 3);
}

void gfx_clear(uint16_t c)
{
    if (G.recording) {
        const uint32_t w[2] = {K_CLEAR, c};
        gfx_mark_rows(0, G.h, gfx_hash_words(w, 2));
        return;
    }
    const uint32_t s = GFX_SPLIT(c);
    for (int y = 0; y < G.h; y++) {
        if (G.row_on[y]) fill_span(G.target + y * G.w, 0, G.w, c, s, 32);
    }
}

void gfx_rect(float x, float y, float w, float h, uint16_t c, int alpha)
{
    if (alpha <= 0 || w <= 0 || h <= 0) return;
    const float X0 = gfx_sx(x), Y0 = gfx_sy(y), X1 = gfx_sx(x + w), Y1 = gfx_sy(y + h);
    int y0 = gfx_floor(Y0), y1 = gfx_ceil(Y1);
    if (G.recording) {
        mark_shape(K_RECT, X0, Y0, X1, Y1, 0, 0, c, alpha, y0, y1);
        return;
    }
    if (!gfx_rows(&y0, &y1)) return;
    int xa = gfx_floor(X0), xb = gfx_ceil(X1);
    if (xa < 0) xa = 0;
    if (xb > G.w) xb = G.w;
    if (xb <= xa) return;
    const uint32_t s = GFX_SPLIT(c);
    const float left = (float)(xa + 1) - X0, right = X1 - (float)(xb - 1);
    const bool soft_l = X0 > (float)xa, soft_r = X1 < (float)xb;
    for (int yy = y0; yy < y1; yy++) {
        if (!G.row_on[yy]) continue;
        uint16_t *row = G.target + yy * G.w;
        float rc = 1;
        if ((float)yy < Y0) rc = (float)(yy + 1) - Y0;
        if ((float)(yy + 1) > Y1) rc -= (float)(yy + 1) - Y1;
        if (rc <= 0) continue;
        const int a = (int)(rc * (float)alpha + 0.5f);
        int xs = xa, xe = xb;
        if (soft_l) put_cov(row, xs++, gfx_clamp01(left), s, a);
        if (soft_r && xe > xs) put_cov(row, --xe, gfx_clamp01(right), s, a);
        if (a >= 252) fill_span(row, xs, xe, c, s, 32);
        else fill_span(row, xs, xe, c, s, to_a32(a));
    }
}

void gfx_block(float x1, float y1, float x2, float y2, uint16_t c, int alpha)
{
    if (alpha <= 0) return;
    int xa = (int)lroundf(gfx_dx(x1)), xb = (int)lroundf(gfx_dx(x2));
    int ya = (int)lroundf(gfx_dy(y1)), yb = (int)lroundf(gfx_dy(y2));
    if (xb <= xa || yb <= ya) return;
    if (G.recording) {
        const int32_t w[6] = {K_BLOCK, xa, xb, ya, c, alpha};
        const int32_t w2[2] = {(int32_t)gfx_hash_words(w, 6), yb};
        gfx_mark_rows(ya, yb, gfx_hash_words(w2, 2));
        return;
    }
    if (!gfx_rows(&ya, &yb)) return;
    if (xa < 0) xa = 0;
    if (xb > G.w) xb = G.w;
    const uint32_t a32 = to_a32(alpha), s = GFX_SPLIT(c);
    for (int y = ya; y < yb; y++) {
        if (G.row_on[y]) fill_span(G.target + y * G.w, xa, xb, c, s, a32);
    }
}

void gfx_disc(float cx, float cy, float r, uint16_t c, int alpha)
{
    if (alpha <= 0 || r <= 0) return;
    const float X = gfx_sx(cx), Y = gfx_sy(cy), R = gfx_spx(r * G.k);
    int y0 = gfx_floor(Y - R - 1), y1 = gfx_ceil(Y + R + 1);
    if (G.recording) {
        mark_shape(K_DISC, X, Y, R, 0, 0, 0, c, alpha, y0, y1);
        return;
    }
    if (!gfx_rows(&y0, &y1)) return;
    const uint32_t s = GFX_SPLIT(c), a32 = to_a32(alpha);
    const float ro2 = (R + 0.5f) * (R + 0.5f), ri = R - 0.5f, ri2 = ri > 0 ? ri * ri : 0;
    for (int y = y0; y < y1; y++) {
        if (!G.row_on[y]) continue;
        const float dy = (float)y + 0.5f - Y, dy2 = dy * dy;
        if (dy2 >= ro2) continue;
        const float xo = gfx_sqrt(ro2 - dy2), xi = dy2 < ri2 ? gfx_sqrt(ri2 - dy2) : -1;
        uint16_t *row = G.target + y * G.w;
        int xa = gfx_floor(X - xo), xb = gfx_ceil(X + xo);
        if (xa < 0) xa = 0;
        if (xb > G.w) xb = G.w;
        int sa = xb, sb = xb; // the solid middle
        if (xi >= 0) {
            sa = gfx_ceil(X - xi);
            sb = gfx_floor(X + xi);
            if (sa < xa) sa = xa;
            if (sb > xb) sb = xb;
            if (sb < sa) sb = sa;
        }
        for (int x = xa; x < sa; x++) {
            const float dx = (float)x + 0.5f - X;
            const float cov = R + 0.5f - gfx_sqrt(dx * dx + dy2);
            if (cov > 0) put_cov(row, x, cov > 1 ? 1 : cov, s, alpha);
        }
        fill_span(row, sa, sb, c, s, a32);
        for (int x = sb; x < xb; x++) {
            const float dx = (float)x + 0.5f - X;
            const float cov = R + 0.5f - gfx_sqrt(dx * dx + dy2);
            if (cov > 0) put_cov(row, x, cov > 1 ? 1 : cov, s, alpha);
        }
    }
}

void gfx_ring(float cx, float cy, float r, float lw, uint16_t c, int alpha)
{
    if (alpha <= 0 || r <= 0 || lw <= 0) return;
    const float X = gfx_sx(cx), Y = gfx_sy(cy), R = gfx_spx(r * G.k), HW = gfx_spx(lw * G.k * 0.5f);
    const float RO = R + HW, RI = R - HW;
    int y0 = gfx_floor(Y - RO - 1), y1 = gfx_ceil(Y + RO + 1);
    if (G.recording) {
        mark_shape(K_RING, X, Y, R, HW, 0, 0, c, alpha, y0, y1);
        return;
    }
    if (!gfx_rows(&y0, &y1)) return;
    const uint32_t s = GFX_SPLIT(c);
    const float ro2 = (RO + 0.5f) * (RO + 0.5f), rh = RI - 0.5f, rh2 = rh > 0 ? rh * rh : 0;
    for (int y = y0; y < y1; y++) {
        if (!G.row_on[y]) continue;
        const float dy = (float)y + 0.5f - Y, dy2 = dy * dy;
        if (dy2 >= ro2) continue;
        const float xo = gfx_sqrt(ro2 - dy2), xh = dy2 < rh2 ? gfx_sqrt(rh2 - dy2) : 0;
        uint16_t *row = G.target + y * G.w;
        for (int side = 0; side < 2; side++) {
            int xa = side ? gfx_floor(X + xh) : gfx_floor(X - xo);
            int xb = side ? gfx_ceil(X + xo) : gfx_ceil(X - xh);
            if (xa < 0) xa = 0;
            if (xb > G.w) xb = G.w;
            for (int x = xa; x < xb; x++) {
                const float dx = (float)x + 0.5f - X;
                const float d = gfx_sqrt(dx * dx + dy2);
                const float dist = d > R ? d - R : R - d;
                const float cov = HW + 0.5f - dist;
                if (cov > 0) put_cov(row, x, cov > 1 ? 1 : cov, s, alpha);
            }
        }
    }
}

// The coverage of a rounded rectangle at a pixel's center. (x0..y1 are its edges; r, its corners.)
static inline float rr_cov(float px, float py, float x0, float y0, float x1, float y1, float r)
{
    const float qx = px < x0 + r ? x0 + r - px : px > x1 - r ? px - (x1 - r) : -1;
    const float qy = py < y0 + r ? y0 + r - py : py > y1 - r ? py - (y1 - r) : -1;
    float cov;
    if (qx > 0 && qy > 0) {
        cov = r + 0.5f - gfx_sqrt(qx * qx + qy * qy);
    } else {
        const float a = px - x0, b = x1 - px, c = py - y0, d = y1 - py;
        float m = a < b ? a : b;
        if (c < m) m = c;
        if (d < m) m = d;
        cov = m + 0.5f;
    }
    return cov < 0 ? 0 : cov > 1 ? 1 : cov;
}

void gfx_rrect(float x, float y, float w, float h, float r, uint16_t c, int alpha)
{
    if (alpha <= 0 || w <= 0 || h <= 0) return;
    const float X0 = gfx_sx(x), Y0 = gfx_sy(y), X1 = gfx_sx(x + w), Y1 = gfx_sy(y + h);
    float R = gfx_spx(r * G.k);
    if (R * 2 > X1 - X0) R = (X1 - X0) * 0.5f;
    if (R * 2 > Y1 - Y0) R = (Y1 - Y0) * 0.5f;
    int ya = gfx_floor(Y0), yb = gfx_ceil(Y1);
    if (G.recording) {
        mark_shape(K_RRECT, X0, Y0, X1, Y1, R, 0, c, alpha, ya, yb);
        return;
    }
    if (!gfx_rows(&ya, &yb)) return;
    int xa = gfx_floor(X0), xb = gfx_ceil(X1);
    if (xa < 0) xa = 0;
    if (xb > G.w) xb = G.w;
    const uint32_t s = GFX_SPLIT(c), a32 = to_a32(alpha);
    // The columns that are solid in a row with no corner.
    int sa = gfx_ceil(X0 + R + 0.5f), sb = gfx_floor(X1 - R - 0.5f);
    if (sa < xa) sa = xa;
    if (sb > xb) sb = xb;
    for (int yy = ya; yy < yb; yy++) {
        if (!G.row_on[yy]) continue;
        uint16_t *row = G.target + yy * G.w;
        const float py = (float)yy + 0.5f;
        for (int xx = xa; xx < sa && xx < xb; xx++) {
            const float cov = rr_cov((float)xx + 0.5f, py, X0, Y0, X1, Y1, R);
            if (cov > 0) put_cov(row, xx, cov, s, alpha);
        }
        if (sb > sa) {
            if (py - Y0 >= 0.5f && Y1 - py >= 0.5f) {
                fill_span(row, sa, sb, c, s, a32);
            } else { // the top or bottom row's partial coverage
                const float cov = rr_cov((float)sa + 0.5f, py, X0, Y0, X1, Y1, R);
                if (cov > 0) {
                    const uint32_t a = (uint32_t)(cov * (float)alpha + 0.5f);
                    fill_span(row, sa, sb, c, s, (a + 4) >> 3);
                }
            }
        }
        for (int xx = sb > sa ? sb : sa; xx < xb; xx++) {
            const float cov = rr_cov((float)xx + 0.5f, py, X0, Y0, X1, Y1, R);
            if (cov > 0) put_cov(row, xx, cov, s, alpha);
        }
    }
}

void gfx_rrect_line(float x, float y, float w, float h, float r, float lw, uint16_t c, int alpha)
{
    if (alpha <= 0 || w <= 0 || h <= 0 || lw <= 0) return;
    const float HW = gfx_spx(lw * G.k * 0.5f);
    const float X0 = gfx_sx(x), Y0 = gfx_sy(y), X1 = gfx_sx(x + w), Y1 = gfx_sy(y + h);
    float R = gfx_spx(r * G.k);
    if (R * 2 > X1 - X0) R = (X1 - X0) * 0.5f;
    if (R * 2 > Y1 - Y0) R = (Y1 - Y0) * 0.5f;
    int ya = gfx_floor(Y0 - HW), yb = gfx_ceil(Y1 + HW);
    if (G.recording) {
        mark_shape(K_RRLINE, X0, Y0, X1, Y1, R, HW, c, alpha, ya, yb);
        return;
    }
    if (!gfx_rows(&ya, &yb)) return;
    int xa = gfx_floor(X0 - HW), xb = gfx_ceil(X1 + HW);
    if (xa < 0) xa = 0;
    if (xb > G.w) xb = G.w;
    const uint32_t s = GFX_SPLIT(c);
    const float Ro = R + HW, Ri = R > HW ? R - HW : 0;
    for (int yy = ya; yy < yb; yy++) {
        if (!G.row_on[yy]) continue;
        uint16_t *row = G.target + yy * G.w;
        const float py = (float)yy + 0.5f;
        for (int xx = xa; xx < xb; xx++) {
            const float px = (float)xx + 0.5f;
            const float co = rr_cov(px, py, X0 - HW, Y0 - HW, X1 + HW, Y1 + HW, Ro);
            if (co <= 0) continue;
            const float ci = rr_cov(px, py, X0 + HW, Y0 + HW, X1 - HW, Y1 - HW, Ri);
            if (ci >= 1) { // the hole: jump across it
                const int jump = gfx_floor(X1 - HW - R) - 1;
                if (jump > xx) xx = jump;
                continue;
            }
            const float cov = co - ci;
            if (cov > 0) put_cov(row, xx, cov, s, alpha);
        }
        (void)s;
    }
}

// ---- Icons --------------------------------------------------------------------------------------------------------

#define ICON_MAX 128

static struct {
    uint8_t *mask; // ICON_MAX x ICON_MAX, in large memory
    int x0, y0, w, h;
    float fy0, fy1;
    bool skip;
    uint32_t hash;
#ifdef BENTO_DEBUG_INTERFACE
    uint32_t why[18]; // the first three parts' inputs, for "#why"
    int why_n;
#endif
} I;

void gfx_icon_begin(float x0, float y0, float x1, float y1)
{
    if (!I.mask) I.mask = hal_alloc_large(ICON_MAX * ICON_MAX);
    const int X0 = gfx_floor(gfx_dx(x0)), Y0 = gfx_floor(gfx_dy(y0));
    int X1 = gfx_ceil(gfx_dx(x1)), Y1 = gfx_ceil(gfx_dy(y1));
    if (X1 - X0 > ICON_MAX) X1 = X0 + ICON_MAX;
    if (Y1 - Y0 > ICON_MAX) Y1 = Y0 + ICON_MAX;
    I.x0 = X0;
    I.y0 = Y0;
    I.w = X1 - X0;
    I.h = Y1 - Y0;
    I.hash = K_ICON;
#ifdef BENTO_DEBUG_INTERFACE
    memset(I.why, 0, sizeof I.why);
    I.why_n = 0;
#endif
    I.skip = !I.mask || I.w <= 0 || I.h <= 0;
    if (G.recording || I.skip) return;
    I.skip = true;
    for (int y = Y0 < 0 ? 0 : Y0; y < Y1 && y < G.h; y++) {
        if (G.row_on[y]) {
            I.skip = false;
            break;
        }
    }
    if (!I.skip) memset(I.mask, 0, (size_t)ICON_MAX * (size_t)I.h);
}

// a, b, c, d and e are pixels, or for an arc or a polygon, a hash of its own (rounded) numbers.
static void icon_hash(uint32_t kind, float a, float b, float c, float d, float e)
{
    const uint32_t v[6] = {kind, gfx_hash_q(a, GFX_Q_PX), gfx_hash_q(b, GFX_Q_PX), gfx_hash_q(c, GFX_Q_PX), gfx_hash_q(d, GFX_Q_PX),
                           gfx_hash_q(e, GFX_Q_PX)};
#ifdef BENTO_DEBUG_INTERFACE
    if (I.why_n + 6 <= 18) {
        memcpy(&I.why[I.why_n], v, sizeof v);
        I.why_n += 6;
    }
#endif
    const uint32_t w[2] = {I.hash, gfx_hash_words(v, 6)};
    I.hash = gfx_hash_words(w, 2);
}

static inline void icon_put(int x, int y, float cov)
{
    const int v = (int)(cov * 255.0f + 0.5f);
    uint8_t *p = I.mask + y * ICON_MAX + x;
    if (v > *p) *p = (uint8_t)v;
}

void gfx_icon_line(float ax, float ay, float bx, float by, float lw, bool round_caps)
{
    ax = gfx_sx(ax);
    ay = gfx_sy(ay);
    bx = gfx_sx(bx);
    by = gfx_sy(by);
    const float hw = gfx_spx(lw * G.k * 0.5f);
    if (G.recording) {
        icon_hash(1 + round_caps, ax, ay, bx, by, hw);
        return;
    }
    if (I.skip) return;
    const float dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    const float inv = len2 > 0 ? 1.0f / len2 : 0, len = gfx_sqrt(len2);
    const float invlen = len > 0 ? 1.0f / len : 0;
    const float m = hw + 1;
    int x0 = gfx_floor((ax < bx ? ax : bx) - m) - I.x0, x1 = gfx_ceil((ax > bx ? ax : bx) + m) - I.x0;
    int y0 = gfx_floor((ay < by ? ay : by) - m) - I.y0, y1 = gfx_ceil((ay > by ? ay : by) + m) - I.y0;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > I.w) x1 = I.w;
    if (y1 > I.h) y1 = I.h;
    for (int y = y0; y < y1; y++) {
        const float py = (float)(y + I.y0) + 0.5f - ay;
        for (int x = x0; x < x1; x++) {
            const float px = (float)(x + I.x0) + 0.5f - ax;
            const float t = (px * dx + py * dy) * inv;
            float cov;
            if (round_caps) {
                const float k = t < 0 ? 0 : t > 1 ? 1 : t;
                const float qx = px - k * dx, qy = py - k * dy;
                cov = hw + 0.5f - gfx_sqrt(qx * qx + qy * qy);
            } else {
                float perp = (px * dy - py * dx) * invlen;
                if (perp < 0) perp = -perp;
                cov = hw + 0.5f - perp;
                const float along = t * len, e0 = along + 0.5f, e1 = len - along + 0.5f;
                if (e0 < cov) cov = e0;
                if (e1 < cov) cov = e1;
            }
            if (cov > 0) icon_put(x, y, cov > 1 ? 1 : cov);
        }
    }
}

void gfx_icon_arc(float cx, float cy, float r, float lw, float a0, float a1, bool round_caps)
{
    cx = gfx_snap(cx, GFX_Q_UNIT);
    cy = gfx_snap(cy, GFX_Q_UNIT);
    r = gfx_snap(r, GFX_Q_UNIT);
    lw = gfx_snap(lw, GFX_Q_UNIT);
    a0 = gfx_snap(a0, GFX_Q_ANGLE);
    a1 = gfx_snap(a1, GFX_Q_ANGLE);
    if (G.recording) {
        const uint32_t v[6] = {gfx_hash_q(cx, GFX_Q_UNIT), gfx_hash_q(cy, GFX_Q_UNIT), gfx_hash_q(r, GFX_Q_UNIT), gfx_hash_q(lw, GFX_Q_UNIT),
                               gfx_hash_q(a0, GFX_Q_ANGLE), gfx_hash_q(a1, GFX_Q_ANGLE)};
        const uint32_t h = gfx_hash_words(v, 6);
        icon_hash(3 + round_caps, gfx_dx(0), gfx_dy(0), (float)(h & 0xFFFF), (float)(h >> 16), 0); // halves: exact as floats
        return;
    }
    if (I.skip) return;
    float da = a1 - a0;
    const float len = fabsf(da) * r * G.k;
    int n = (int)(len / 2.0f) + 2;
    if (n > 48) n = 48;
    da /= (float)n;
    float px = cx + r * cosf(a0), py = cy + r * sinf(a0);
    for (int i = 1; i <= n; i++) {
        const float a = a0 + da * (float)i, qx = cx + r * cosf(a), qy = cy + r * sinf(a);
        const bool first_or_last = i == 1 || i == n;
        gfx_icon_line(px, py, qx, qy, lw, round_caps || !first_or_last);
        px = qx;
        py = qy;
    }
}

void gfx_icon_ring(float cx, float cy, float r, float lw)
{
    const float X = gfx_sx(cx), Y = gfx_sy(cy), R = gfx_spx(r * G.k), HW = gfx_spx(lw * G.k * 0.5f);
    if (G.recording) {
        icon_hash(5, X, Y, R, HW, 0);
        return;
    }
    if (I.skip) return;
    const float m = R + HW + 1;
    int x0 = gfx_floor(X - m) - I.x0, x1 = gfx_ceil(X + m) - I.x0, y0 = gfx_floor(Y - m) - I.y0, y1 = gfx_ceil(Y + m) - I.y0;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > I.w) x1 = I.w;
    if (y1 > I.h) y1 = I.h;
    for (int y = y0; y < y1; y++) {
        const float dy = (float)(y + I.y0) + 0.5f - Y;
        for (int x = x0; x < x1; x++) {
            const float dx = (float)(x + I.x0) + 0.5f - X;
            const float d = gfx_sqrt(dx * dx + dy * dy);
            const float dist = d > R ? d - R : R - d;
            const float cov = HW + 0.5f - dist;
            if (cov > 0) icon_put(x, y, cov > 1 ? 1 : cov);
        }
    }
}

void gfx_icon_rrect(float x, float y, float w, float h, float r)
{
    const float X0 = gfx_sx(x), Y0 = gfx_sy(y), X1 = gfx_sx(x + w), Y1 = gfx_sy(y + h), R = gfx_spx(r * G.k);
    if (G.recording) {
        icon_hash(7, X0, Y0, X1, Y1, R);
        return;
    }
    if (I.skip) return;
    int x0 = gfx_floor(X0) - I.x0, x1 = gfx_ceil(X1) - I.x0, y0 = gfx_floor(Y0) - I.y0, y1 = gfx_ceil(Y1) - I.y0;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > I.w) x1 = I.w;
    if (y1 > I.h) y1 = I.h;
    for (int yy = y0; yy < y1; yy++) {
        const float py = (float)(yy + I.y0) + 0.5f;
        for (int xx = x0; xx < x1; xx++) {
            const float cov = rr_cov((float)(xx + I.x0) + 0.5f, py, X0, Y0, X1, Y1, R);
            if (cov > 0) icon_put(xx, yy, cov);
        }
    }
}

// Even-odd fill, by four sub-scanlines a row.
void gfx_icon_poly(const float *xy, int points)
{
    if (points < 3 || points > 32) return;
    if (G.recording) {
        uint32_t q[64];
        for (int i = 0; i < points * 2; i++) q[i] = gfx_hash_q(xy[i], GFX_Q_UNIT);
        const uint32_t h = gfx_hash_words(q, points * 2);
        icon_hash(8, gfx_dx(0), gfx_dy(0), (float)(h & 0xFFFF), (float)(h >> 16), G.k);
        return;
    }
    if (I.skip) return;
    float px[32], py[32];
    float ymin = 1e9f, ymax = -1e9f;
    for (int i = 0; i < points; i++) {
        px[i] = gfx_dx(gfx_snap(xy[i * 2], GFX_Q_UNIT)) - (float)I.x0; // the points as hashed
        py[i] = gfx_dy(gfx_snap(xy[i * 2 + 1], GFX_Q_UNIT)) - (float)I.y0;
        if (py[i] < ymin) ymin = py[i];
        if (py[i] > ymax) ymax = py[i];
    }
    int y0 = gfx_floor(ymin), y1 = gfx_ceil(ymax);
    if (y0 < 0) y0 = 0;
    if (y1 > I.h) y1 = I.h;
    float acc[ICON_MAX];
    for (int y = y0; y < y1; y++) {
        memset(acc, 0, sizeof acc);
        for (int sub = 0; sub < 4; sub++) {
            const float sy = (float)y + 0.125f + 0.25f * (float)sub;
            float xs[32];
            int n = 0;
            for (int i = 0, j = points - 1; i < points; j = i++) {
                if ((py[i] > sy) != (py[j] > sy)) {
                    const float t = (sy - py[j]) / (py[i] - py[j]);
                    xs[n++] = px[j] + t * (px[i] - px[j]);
                }
            }
            for (int a = 1; a < n; a++) { // sort
                const float v = xs[a];
                int b = a - 1;
                while (b >= 0 && xs[b] > v) {
                    xs[b + 1] = xs[b];
                    b--;
                }
                xs[b + 1] = v;
            }
            for (int k = 0; k + 1 < n; k += 2) {
                float l = xs[k], r = xs[k + 1];
                if (l < 0) l = 0;
                if (r > (float)I.w) r = (float)I.w;
                if (r <= l) continue;
                const int il = (int)l, ir = (int)r;
                if (il == ir) {
                    acc[il] += (r - l) * 0.25f;
                    continue;
                }
                acc[il] += ((float)(il + 1) - l) * 0.25f;
                for (int x = il + 1; x < ir; x++) acc[x] += 0.25f;
                if (ir < I.w) acc[ir] += (r - (float)ir) * 0.25f;
            }
        }
        for (int x = 0; x < I.w; x++) {
            if (acc[x] > 0) icon_put(x, y, acc[x] > 1 ? 1 : acc[x]);
        }
    }
}

void gfx_icon_end_contrast(void)
{
    if (G.recording) {
        // What it depends on underneath marks these rows itself.
        const uint32_t w[3] = {I.hash, (uint32_t)I.x0, (uint32_t)I.y0};
        gfx_mark_rows(I.y0, I.y0 + I.h, gfx_hash_words(w, 3));
        return;
    }
    if (I.skip) return;
    const uint32_t black = GFX_SPLIT(0x0000), white = GFX_SPLIT(0xFFFF);
    for (int y = 0; y < I.h; y++) {
        const int Y = y + I.y0;
        if (Y < G.cy0 || Y >= G.cy1 || !G.row_on[Y]) continue;
        const uint8_t *m = I.mask + y * ICON_MAX;
        uint16_t *row = G.target + Y * G.w;
        for (int x = 0; x < I.w; x++) {
            const uint32_t v = m[x];
            if (!v) continue;
            const int X = x + I.x0;
            if (X < 0 || X >= G.w) continue;
            const uint32_t d = row[X], lum = 4 * (d >> 11) + 5 * ((d >> 5) & 63) + 2 * (d & 31); // 0 .. 504
            row[X] = gfx_blend(row[X], lum > 170 ? black : white, (v + 4) >> 3);
        }
    }
}

void gfx_icon_end(uint16_t c, int alpha)
{
    if (G.recording) {
        const uint32_t w[5] = {I.hash, c, (uint32_t)alpha, (uint32_t)I.x0, (uint32_t)I.y0};
#ifdef BENTO_DEBUG_INTERFACE
        gfx_why_words(I.why, 18);
#endif
        gfx_mark_rows(I.y0, I.y0 + I.h, gfx_hash_words(w, 5));
        return;
    }
    if (I.skip || alpha <= 0) return;
    const uint32_t s = GFX_SPLIT(c), a256 = (uint32_t)alpha + (uint32_t)(alpha >> 7);
    for (int y = 0; y < I.h; y++) {
        const int Y = y + I.y0;
        if (Y < G.cy0 || Y >= G.cy1 || !G.row_on[Y]) continue;
        const uint8_t *m = I.mask + y * ICON_MAX;
        uint16_t *row = G.target + Y * G.w;
        for (int x = 0; x < I.w; x++) {
            const uint32_t v = m[x];
            if (!v) continue;
            const int X = x + I.x0;
            if (X < 0 || X >= G.w) continue;
            row[X] = gfx_blend(row[X], s, ((v * a256 >> 8) + 4) >> 3);
        }
    }
}
