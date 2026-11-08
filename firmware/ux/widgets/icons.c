// The small drawings: the mic and the lock. The same strokes as the preview's.
#include <math.h>

#include "../app.h"

void icon_mic(float x, float y, float s, uint16_t c)
{
    const float lw = s * 0.13f;
    gfx_icon_begin(x - s * 0.7f, y - s * 0.75f, x + s * 0.7f, y + s * 0.75f);
    gfx_icon_rrect(x - s * 0.2f, y - s * 0.55f, s * 0.4f, s * 0.75f, s * 0.2f);
    gfx_icon_arc(x, y - s * 0.08f, s * 0.4f, lw, 0, PI_F, true);
    gfx_icon_line(x, y + s * 0.32f, x, y + s * 0.55f, lw, true);
    gfx_icon_end(c, 255);
}

static void lock_shape(float x, float y, float s)
{
    const float lw = s * 0.13f > 1.3f ? s * 0.13f : 1.3f;
    gfx_icon_begin(x - s * 0.6f, y - s * 0.6f, x + s * 0.6f, y + s * 0.65f);
    gfx_icon_arc(x, y - s * 0.14f, s * 0.24f, lw, PI_F, 2 * PI_F, false);
    gfx_icon_line(x + s * 0.24f, y - s * 0.14f, x + s * 0.24f, y, lw, false);
    gfx_icon_line(x - s * 0.24f, y - s * 0.14f, x - s * 0.24f, y, lw, false);
    gfx_icon_rrect(x - s * 0.4f, y - s * 0.04f, s * 0.8f, s * 0.52f, s * 0.1f);
}

void icon_status_lock(float x, float y, float s, uint16_t c)
{
    lock_shape(x, y, s);
    gfx_icon_end(c, 255);
}

void icon_status_lock_contrast(float x, float y, float s)
{
    lock_shape(x, y, s);
    gfx_icon_end_contrast();
}

void icon_check(float x, float y, float size, uint16_t c, int alpha)
{
    const float s = size * 0.42f, lw = size * 0.13f;
    gfx_icon_begin(x - s - 2, y - s - 2, x + s + 2, y + s + 2);
    gfx_icon_line(x - s * 0.9f, y + s * 0.05f, x - s * 0.25f, y + s * 0.7f, lw, true);
    gfx_icon_line(x - s * 0.25f, y + s * 0.7f, x + s * 0.95f, y - s * 0.75f, lw, true);
    gfx_icon_end(c, alpha);
}

void icon_delete_key(float x, float y, float key, uint16_t c, int alpha)
{
    const float w = key * 0.62f, h = key * 0.42f;
    gfx_icon_begin(x - w - 3, y - h - 3, x + w + 3, y + h + 3);
    gfx_icon_line(x - w, y, x - w * 0.45f, y - h, 2, true);
    gfx_icon_line(x - w * 0.45f, y - h, x + w, y - h, 2, true);
    gfx_icon_line(x + w, y - h, x + w, y + h, 2, true);
    gfx_icon_line(x + w, y + h, x - w * 0.45f, y + h, 2, true);
    gfx_icon_line(x - w * 0.45f, y + h, x - w, y, 2, true);
    gfx_icon_line(x - h * 0.35f, y - h * 0.45f, x + h * 0.55f, y + h * 0.45f, 2, false);
    gfx_icon_line(x + h * 0.55f, y - h * 0.45f, x - h * 0.35f, y + h * 0.45f, 2, false);
    gfx_icon_end(c, alpha);
}

// The charging bolt, with a thin edge so that it shows over the battery's fill.
