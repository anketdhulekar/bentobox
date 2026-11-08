#include "color.h"

#include <math.h>

#include "gfx/gfx.h"

// As rgb565 in gfx.h, for a constant.
#define C(hex) ((uint16_t)((((((hex) >> 16) & 0xFF) * 249 + 1014) >> 11) << 11 | ((((hex) >> 8 & 0xFF) * 253 + 505) >> 10) << 5 | ((((hex) & 0xFF) * 249 + 1014) >> 11)))

const theme_t THEMES[2] = {
    {.face = C(0x000000), .ink = C(0xFFFFFF), .soft = C(0xCFCCC5), .sub = C(0x9A9A9F), .faint = C(0x2A2A2D), .amber = C(0xF5A623), .ok = C(0x7BD88F),
     .toastBg = C(0x262523), .toastInk = C(0xEDEBE5), .offlineBg = C(0x3A2A10),
     .st = {C(0x7BD88F), C(0xF5A623), C(0x5B8CFF), C(0x6F6C66), C(0xE5484D)}, .highlight = C(0x1E1E20)},
    {.face = C(0xFFFFFF), .ink = C(0x000000), .soft = C(0x3A3A3D), .sub = C(0x66666B), .faint = C(0xDCDCE0), .amber = C(0xA86400), .ok = C(0x1F8A50),
     .toastBg = C(0x1F1E1D), .toastInk = C(0xFFFFFF), .offlineBg = C(0xFFF1D6),
     .st = {C(0x1F9D57), C(0xD98300), C(0x2F66E0), C(0x8A8780), C(0xD32F34)}, .highlight = C(0xEDEDF0)},
};

typedef struct { float L, a, b; } lab_t;

static float to_lin(float c) { return c <= 0.04045f ? c / 12.92f : powf((c + 0.055f) / 1.055f, 2.4f); }
static float to_srgb(float c) { return c <= 0.0031308f ? 12.92f * c : 1.055f * powf(c, 1.0f / 2.4f) - 0.055f; }

static lab_t rgb_to_oklab(uint32_t rgb)
{
    const float r = to_lin((float)((rgb >> 16) & 0xFF) / 255.0f);
    const float g = to_lin((float)((rgb >> 8) & 0xFF) / 255.0f);
    const float b = to_lin((float)(rgb & 0xFF) / 255.0f);
    const float l = cbrtf(0.4122214708f * r + 0.5363325363f * g + 0.0514459929f * b);
    const float m = cbrtf(0.2119034982f * r + 0.6806995451f * g + 0.1073969566f * b);
    const float s = cbrtf(0.0883024619f * r + 0.2817188376f * g + 0.6299787005f * b);
    return (lab_t){
        0.2104542553f * l + 0.7936177850f * m - 0.0040720468f * s,
        1.9779984951f * l - 2.4285922050f * m + 0.4505937099f * s,
        0.0259040371f * l + 0.7827717662f * m - 0.8086757660f * s,
    };
}

static uint32_t chan(float c)
{
    c = to_srgb(c < 0 ? 0 : c);
    if (c > 1) c = 1;
    return (uint32_t)lroundf(c * 255.0f);
}

static uint32_t oklab_to_rgb(lab_t c)
{
    float l = c.L + 0.3963377774f * c.a + 0.2158037573f * c.b;
    float m = c.L - 0.1055613458f * c.a - 0.0638541728f * c.b;
    float s = c.L - 0.0894841775f * c.a - 1.2914855480f * c.b;
    l = l * l * l;
    m = m * m * m;
    s = s * s * s;
    const float r = 4.0767416621f * l - 3.3077115913f * m + 0.2309699292f * s;
    const float g = -1.2684380046f * l + 2.6097574011f * m - 0.3413193965f * s;
    const float b = -0.0041960863f * l - 0.7034186147f * m + 1.7076147010f * s;
    return chan(r) << 16 | chan(g) << 8 | chan(b);
}

#define PI_F 3.14159265f

static int crosses_purple(float h, float dh)
{
    for (int k = 1; k < 24; k++) {
        float deg = fmodf((h + dh * (float)k / 24.0f) * 180.0f / PI_F, 360.0f);
        if (deg < 0) deg += 360.0f;
        if (deg > 285.0f && deg < 345.0f) return 1;
    }
    return 0;
}

// A fade around the OKLCH hue wheel that never sweeps through purple, unless it starts there.
static lab_t lch_mix(lab_t p, lab_t q, float t)
{
    const float C1 = hypotf(p.a, p.b), C2 = hypotf(q.a, q.b);
    const float h2 = atan2f(q.b, q.a);
    float h1 = atan2f(p.b, p.a);
    if (C1 < 0.02f) h1 = h2; // near-white has no real hue
    float dh = h2 - h1;
    if (dh > PI_F) dh -= 2 * PI_F;
    else if (dh < -PI_F) dh += 2 * PI_F;
    float deg1 = fmodf(h1 * 180.0f / PI_F, 360.0f);
    if (deg1 < 0) deg1 += 360.0f;
    const int starts_purple = C1 >= 0.02f && deg1 > 285.0f && deg1 < 345.0f;
    if (!starts_purple && crosses_purple(h1, dh)) dh += dh > 0 ? -2 * PI_F : 2 * PI_F;
    const float L = p.L + (q.L - p.L) * t, C = C1 + (C2 - C1) * t, h = h1 + dh * t;
    return (lab_t){L, C * cosf(h), C * sinf(h)};
}

uint32_t color_warm(uint32_t ink_rgb, float min)
{
    if (min >= 10) return ink_rgb;
    if (min < 1) return COLOR_CRIMSON;
    const lab_t orange = rgb_to_oklab(COLOR_ORANGE);
    if (min >= 5) return oklab_to_rgb(lch_mix(rgb_to_oklab(ink_rgb), orange, (10 - min) / 5));
    return oklab_to_rgb(lch_mix(orange, rgb_to_oklab(COLOR_CRIMSON), (5 - min) / 4));
}

uint32_t color_readable_on(uint32_t rgb)
{
    const float v = 0.2126f * (float)((rgb >> 16) & 0xFF) / 255.0f + 0.7152f * (float)((rgb >> 8) & 0xFF) / 255.0f + 0.0722f * (float)(rgb & 0xFF) / 255.0f;
    return v > 0.55f ? 0x111111u : 0xFFFFFFu;
}

uint16_t color_mix565(uint16_t a, uint16_t b, float t)
{
    if (t <= 0) return a;
    if (t >= 1) return b;
    const int k = (int)(t * 256.0f);
    const int ar = a >> 11, ag = (a >> 5) & 63, ab = a & 31, br = b >> 11, bg = (b >> 5) & 63, bb = b & 31;
    return (uint16_t)((ar + (((br - ar) * k) >> 8)) << 11 | (ag + (((bg - ag) * k) >> 8)) << 5 | (ab + (((bb - ab) * k) >> 8)));
}
