// What the renderer's own files, and the widgets that draw from prebuilt maps, share.
#pragma once
#include "gfx.h"

#define GFX_MAX_BANDS 64

typedef struct {
    int w, h, bands;
    float k;
    int ox, oy; // the burn-in shift, in pixels
    uint16_t *fb;     // the frame
    uint16_t *target; // where this pass paints
    uint8_t *row_on;  // per row: in a band being painted
    int cy0, cy1;     // the clip, in rows
    bool recording;
    uint32_t hash[GFX_MAX_BANDS], prev[GFX_MAX_BANDS];
    bool have_prev;
} gfx_state_t;

extern gfx_state_t G;

#define GFX_SPLIT(c) (((uint32_t)(c) | ((uint32_t)(c) << 16)) & 0x07E0F81Fu)

// Blend the color s (already split) over d. a32 is 0 .. 32.
static inline uint16_t gfx_blend(uint16_t d, uint32_t s, uint32_t a32)
{
    uint32_t dd = GFX_SPLIT(d);
    dd = (dd + (((s - dd) * a32) >> 5)) & 0x07E0F81Fu;
    return (uint16_t)(dd | (dd >> 16));
}

static inline int gfx_floor(float v) { const int i = (int)v; return (float)i > v ? i - 1 : i; }
static inline int gfx_ceil(float v) { const int i = (int)v; return (float)i < v ? i + 1 : i; }
static inline float gfx_clamp01(float v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
// A float as the change check hashes it: in steps of 1/steps (a power of two, so the scaling is exact). The last bits of
// a float differ with the FPU's rounding, and on a board they do from one frame to the next: hashed raw, they made bands
// that looked the same repaint at full rate. Blending has 32 levels, so a move under 1/64 pixel can't change a pixel.
static inline uint32_t gfx_hash_q(float v, float steps)
{
    const float s = v * steps;
    if (!(s > -2e9f && s < 2e9f)) return 0x7fffffffu; // NaN, or too far off to matter
    const int i = (int)s;
    return (uint32_t)((s - (float)i >= 0.5f) ? i + 1 : (s - (float)i < -0.5f) ? i - 1 : i);
}
#define GFX_Q_PX 64.0f      // pixels
#define GFX_Q_UNIT 128.0f   // layout units, which can be half a pixel on the simulator's sharp build
#define GFX_Q_ANGLE 4096.0f // radians, and scales and strengths around 1

// sqrt with no division and no libm call: an inverse-square-root estimate and two Newton steps.
static inline float gfx_sqrt(float x)
{
    if (x <= 0) return 0;
    union { float f; uint32_t u; } v = {x};
    v.u = 0x5F375A86u - (v.u >> 1);
    float y = v.f;
    const float half = 0.5f * x;
    y = y * (1.5f - half * y * y);
    y = y * (1.5f - half * y * y);
    return x * y;
}

static inline float gfx_dx(float x) { return x * G.k + (float)G.ox; }
static inline float gfx_dy(float y) { return y * G.k + (float)G.oy; }
// The same value on the grid the change check hashes, so that what's drawn follows only from what's hashed: a change
// the hash can't see can't change a pixel either. Primitives draw from these.
static inline float gfx_snap(float v, float steps) { return (float)(int32_t)gfx_hash_q(v, steps) / steps; }
static inline float gfx_sx(float x) { return gfx_snap(gfx_dx(x), GFX_Q_PX); }
static inline float gfx_sy(float y) { return gfx_snap(gfx_dy(y), GFX_Q_PX); }
static inline float gfx_spx(float v) { return gfx_snap(v, GFX_Q_PX); } // a length in pixels

void gfx_text_init(void);
uint32_t gfx_hash_words(const void *words, int n); // n 32-bit words, no padding
void gfx_mark_rows(int y0, int y1, uint32_t h);    // mixes h into the bands of rows [y0, y1)

// Rows [y0, y1) limited to the screen and the clip. false if none are left.
static inline bool gfx_rows(int *y0, int *y1)
{
    if (*y0 < G.cy0) *y0 = G.cy0;
    if (*y1 > G.cy1) *y1 = G.cy1;
    return *y1 > *y0;
}
