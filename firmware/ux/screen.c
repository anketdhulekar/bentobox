// The frame goes out. The UX draws upright; if the board is turned, the pixels turn here, and touch turns in input.c.
// In the usual case the frame buffer is the panel's picture. At 90 and 270 degrees a logical row is a panel column, so
// only the changed rows are copied, as tiles (the cache keeps up), and a panel band goes out only if a pixel in it
// really changed. During the animated turn, every pixel is looked up along a rotated line, in fixed point.
#include <math.h>
#include <string.h>

#include "app.h"

#define TILE 32

static uint16_t *s_phys;
static int s_last = -1; // what the panel shows: an orientation, or -1 in the middle of a turn

static void present_mask(const uint16_t *fb, uint64_t mask)
{
    const int H = BOARD.height, bands = gfx_band_count();
    for (int b = 0; b < bands;) {
        if (!((mask >> b) & 1)) {
            b++;
            continue;
        }
        int e = b;
        while (e < bands && ((mask >> e) & 1)) e++; // a run of bands, in one call
        const int y0 = b * HAL_BAND_ROWS;
        int rows = (e - b) * HAL_BAND_ROWS;
        if (y0 + rows > H) rows = H - y0;
        hal_screen_present(fb, y0, rows);
        b = e;
    }
}

static uint64_t all_bands(void)
{
    const int n = gfx_band_count();
    return n >= 64 ? ~0ULL : (1ULL << n) - 1;
}

static uint64_t rotate_180(const uint16_t *src, uint64_t dirty)
{
    const int W = BOARD.width, H = BOARD.height;
    uint64_t out = 0;
    for (int ly = 0; ly < H; ly++) {
        if (!((dirty >> (ly / HAL_BAND_ROWS)) & 1)) continue;
        const int py = H - 1 - ly;
        const uint16_t *s = src + ly * W;
        uint16_t *d = s_phys + py * W + W - 1;
        for (int x = 0; x < W; x++) *d-- = s[x];
        out |= 1ULL << (py / HAL_BAND_ROWS);
    }
    return out;
}

// 90: the panel's (x, y) shows logical (W - 1 - y, x). 270: logical (y, H - 1 - x). A logical row is a panel column.
// A square panel only: ux.c never turns a wide or tall one a quarter (orient_allowed).
static uint64_t rotate_quarter(const uint16_t *src, uint64_t dirty, int orient)
{
    const int W = BOARD.width, H = BOARD.height;
    if (W != H) return 0;
    const bool q90 = orient == 90;
    uint64_t out = 0;
    for (int band = 0; band < gfx_band_count(); band++) {
        if (!((dirty >> band) & 1)) continue;
        const int ly0 = band * HAL_BAND_ROWS;
        int ly1 = ly0 + HAL_BAND_ROWS;
        if (ly1 > H) ly1 = H;
        for (int lx0 = 0; lx0 < W; lx0 += TILE) {
            const int lx1 = lx0 + TILE < W ? lx0 + TILE : W;
            for (int ly = ly0; ly < ly1; ly++) {
                const uint16_t *s = src + ly * W;
                const int px = q90 ? ly : H - 1 - ly;
                for (int lx = lx0; lx < lx1; lx++) {
                    const int py = q90 ? W - 1 - lx : lx;
                    uint16_t *d = s_phys + py * W + px;
                    if (*d != s[lx]) {
                        *d = s[lx];
                        out |= 1ULL << (py / HAL_BAND_ROWS);
                    }
                }
            }
        }
    }
    return out;
}

static void rotate_free(const uint16_t *src, float deg)
{
    const int W = BOARD.width, H = BOARD.height;
    const float a = deg * PI_F / 180.0f;
    const int32_t c = (int32_t)(cosf(a) * 65536.0f), s = (int32_t)(sinf(a) * 65536.0f);
    const uint16_t face = F.T->face;
    // A panel pixel's place in the logical frame, center to center, stepping along each row.
    const int32_t half_w = W << 15, half_h = H << 15;
    for (int y = 0; y < H; y++) {
        const int32_t dy = ((y << 16) + 0x8000) - half_h;
        int32_t u = (int32_t)(((int64_t)(0x8000 - half_w) * c - (int64_t)dy * s) >> 16) + half_w;
        int32_t v = (int32_t)(((int64_t)(0x8000 - half_w) * s + (int64_t)dy * c) >> 16) + half_h;
        uint16_t *d = s_phys + y * W;
        for (int x = 0; x < W; x++) {
            const int ix = u >> 16, iy = v >> 16;
            d[x] = (ix >= 0 && ix < W && iy >= 0 && iy < H) ? src[iy * W + ix] : face;
            u += c;
            v += s;
        }
    }
}

void screen_present(uint64_t dirty, int orient, bool turning, float shown_deg)
{
    const uint16_t *fb = gfx_framebuffer();
    if (!turning && orient == 0) {
        if (s_last != 0) dirty = all_bands();
        s_last = 0;
        if (dirty) present_mask(fb, dirty);
        return;
    }
    if (!s_phys) {
        s_phys = hal_alloc_large((size_t)BOARD.width * (size_t)BOARD.height * sizeof(uint16_t));
        if (!s_phys) { // no memory for it: stay upright
            if (dirty) present_mask(fb, dirty);
            return;
        }
        memcpy(s_phys, fb, (size_t)BOARD.width * (size_t)BOARD.height * sizeof(uint16_t));
    }
    if (turning) {
        rotate_free(fb, shown_deg);
        s_last = -1;
        present_mask(s_phys, all_bands());
        return;
    }
    if (s_last != orient) dirty = all_bands();
    const bool whole = s_last != orient;
    s_last = orient;
    if (!dirty) return;
    const uint64_t changed = orient == 180 ? rotate_180(fb, dirty) : rotate_quarter(fb, dirty, orient);
    present_mask(s_phys, whole ? all_bands() : changed);
}

const uint16_t *screen_shown(void) { return s_last == 0 || !s_phys ? gfx_framebuffer() : s_phys; }
