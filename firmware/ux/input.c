// Gestures from raw touch: a hold after 700 ms, and the preview's thresholds for a swipe. Touches arrive in panel
// pixels and leave in layout units on the upright screen, so the whole screen and its touch input turn together.
#include <math.h>

#include "app.h"

#define HOLD_US (700 * MS_US)
#define HOLD_SHOWS_US (120 * MS_US) // before this it could still be a tap, so nothing shows
#define MOVE_UNITS 14.0f
#define SWIPE_UNITS 36.0f
#define KNOB_PER_FRAME 32
#define CLOCK_DRAG_UNITS 12.0f // one step per this much sideways drag, on a board with no knob

static struct {
    bool down, moved, held, dragging;
    bool stopped_fling; // this touch stopped a flick, and does nothing else unless it moves
    float x0, y0, x, y, last_x, last_y;
    float vx, prev_x;   // units a millisecond, while scrubbing
    int64_t t0, prev_t;
} P;

static void to_layout(int px, int py, float *u, float *v)
{
    const float k = gfx_scale(), w = (float)gfx_width(), h = (float)gfx_height();
    const float cx = (float)px + 0.5f - w / 2, cy = (float)py + 0.5f - h / 2;
    float x, y;
    switch (S.orient) {
    case 90: x = -cy; y = cx; break;
    case 180: x = -cx; y = -cy; break;
    case 270: x = cy; y = -cx; break;
    default: x = cx; y = cy; break;
    }
    *u = (x + w / 2) / k;
    *v = (y + h / 2) / k;
}

static void touch_down(float u, float v, int64_t now)
{
    P.down = true;
    P.moved = false;
    P.held = false;
    P.dragging = false;
    P.x0 = P.x = u;
    P.y0 = P.y = P.last_y = v;
    P.t0 = now;
    P.stopped_fling = zoo_stop_fling();
    S.press_v = v;
}

static void touch_move(float u, float v, int64_t now)
{
    if (!P.down) return;
    P.x = u;
    P.y = v;
    S.press_v = v;
    const float dx = u - P.x0, dy = v - P.y0;
    if (!P.moved && dx * dx + dy * dy > MOVE_UNITS * MOVE_UNITS) P.moved = true;
    if (P.moved && !P.held && app_list_drag_wanted() && (P.dragging || fabsf(dy) > fabsf(dx))) {
        // A list scrolls under the finger; at its top, a pull down still closes it.
        if (P.dragging || app_list_scroll() > 0 || dy < 0) {
            P.dragging = true;
            app_list_drag(v - P.last_y, now);
        }
    } else if (P.moved && !P.held && app_clock_drag_wanted() && (P.dragging || fabsf(dx) > fabsf(dy))) {
        // With no knob, dragging sideways on a clock does what turning the knob does.
        if (!P.dragging) {
            P.dragging = true;
            P.last_x = P.x0;
        }
        for (; u - P.last_x >= CLOCK_DRAG_UNITS; P.last_x += CLOCK_DRAG_UNITS) app_turn(1, now);
        for (; P.last_x - u >= CLOCK_DRAG_UNITS; P.last_x -= CLOCK_DRAG_UNITS) app_turn(-1, now);
    } else if (P.moved && !P.held && zoo_scrub_wanted() && (P.dragging || fabsf(dx) > fabsf(dy))) {
        if (!P.dragging) {
            P.dragging = true;
            P.last_x = P.prev_x = P.x0;
            P.prev_t = P.t0;
            P.vx = 0;
        }
        for (; P.last_x - u >= ZOO_SCRUB_UNITS; P.last_x -= ZOO_SCRUB_UNITS) zoo_scrub(1, now);
        for (; u - P.last_x >= ZOO_SCRUB_UNITS; P.last_x += ZOO_SCRUB_UNITS) zoo_scrub(-1, now);
        if (now > P.prev_t) {
            P.vx = P.vx * 0.6f + (u - P.prev_x) / ((float)(now - P.prev_t) / 1000.0f) * 0.4f;
            P.prev_x = u;
            P.prev_t = now;
        }
    }
    P.last_y = v;
}

static void touch_up(float u, float v, int64_t now)
{
    if (!P.down) return;
    P.down = false;
    S.press_v = -1;
    (void)u;
    (void)v;
    if (P.dragging && zoo_scrub_wanted()) zoo_fling(P.vx);
    if (P.held || P.dragging || (P.stopped_fling && !P.moved)) return;
    const float dx = P.x - P.x0, dy = P.y - P.y0;
    if (P.moved && dx * dx + dy * dy > SWIPE_UNITS * SWIPE_UNITS) {
        if (fabsf(dy) > fabsf(dx)) app_swipe(dy < 0 ? SWIPE_UP : SWIPE_DOWN, now);
        else app_swipe(dx < 0 ? SWIPE_LEFT : SWIPE_RIGHT, now);
    } else if (!P.moved) {
        app_tap(P.x0, P.y0, now);
    }
}

void input_inject(const hw_event_t *e, int64_t now)
{
    float u, v;
    switch (e->kind) {
    case HW_TOUCH_DOWN:
        to_layout(e->x, e->y, &u, &v);
        touch_down(u, v, now);
        break;
    case HW_TOUCH_MOVE:
        to_layout(e->x, e->y, &u, &v);
        touch_move(u, v, now);
        break;
    case HW_TOUCH_UP:
        to_layout(e->x, e->y, &u, &v);
        touch_up(u, v, now);
        break;
    case HW_KNOB:
        if (e->step) app_turn(e->step > 0 ? 1 : -1, now);
        break;
    case HW_BUTTON:
        app_button(e->button, now);
        break;
    default:
        break;
    }
}

float input_hold_progress(int64_t now)
{
    if (!P.down || P.moved || P.held) return 0;
    const float t = (float)(now - P.t0 - HOLD_SHOWS_US) / (float)(HOLD_US - HOLD_SHOWS_US);
    return t < 0 ? 0 : t > 1 ? 1 : t;
}

void input_poll(int64_t now)
{
    hw_event_t e;
    for (int n = 0; n < KNOB_PER_FRAME && hal_input_poll(&e); n++) input_inject(&e, now);
    if (P.down && !P.moved && !P.held && now - P.t0 >= HOLD_US) {
        P.held = true;
        app_hold(now);
    }
}
