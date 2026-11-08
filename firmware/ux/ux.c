// The frame loop, and where every input goes. See ux.h.
#include "ux.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app.h"

#define SAVE_DELAY_US (2 * SEC_US)
#define FRAME_US (20 * MS_US) // at most 50 frames a second
#define HYSTERESIS 12.0f      // degrees past the halfway point, so the screen doesn't flip back and forth
#define TURN_WAIT_US (700 * MS_US) // the board must stay turned this long before the screen follows
#define TURN_US (320 * MS_US)      // how long the screen takes to turn
#define MOTION_EVERY_US (100 * MS_US)

app_t S;
frame_t F;

// ---- Feedback and messages -----------------------------------------------------------------------------------------

void ux_feedback(feedback_t kind)
{
    // While locked: no sound at all, not even the alarm. Vibration stays on. A board lying face down stays quiet too.
    const bool sound = S.vol > 0 && !S.lock.open && !S.face_down;
    hal_feedback(kind, sound);
#ifdef BENTO_DEBUG_INTERFACE
    static const char *const NAMES[FB_COUNT] = {"tick", "bump", "tap", "double", "alert", "alarm", "alarm_stop", "yay", "poof", "drop", "land"};
    char detail[32];
    snprintf(detail, sizeof detail, "%s sound=%d", NAMES[kind], sound);
    ux_log("feedback", detail);
#endif
}

void ux_hint(const char *text, int ms)
{
    utf8_copy(S.hint, sizeof S.hint, text);
    S.hint_until = hal_now_us() + ms * MS_US;
}

void ux_toast(const char *text, int ms)
{
    utf8_copy(S.toast, sizeof S.toast, text);
    S.toast_until = hal_now_us() + ms * MS_US;
}

bool ux_hinting(int64_t now) { return now < S.hint_until; }

void ux_bump(const char *hint)
{
    ux_feedback(FB_BUMP);
    if (hint) ux_hint(hint, 1400);
}

void ux_mark_dirty(int64_t now) { S.dirty_since = now ? now : 1; }

// Any touch or knob turn stops the time's-up alarm.
void ux_quiet(void)
{
    if (!S.alarm_until) return;
    S.alarm_until = 0;
    ux_feedback(FB_ALARM_STOP);
}

// ---- Input: gestures come here, and the UX decides ------------------------------------------------------------------

void app_turn(int dir, int64_t now)
{
    ux_log("step", dir > 0 ? "+1" : "-1");
    S.input_at = now;
    ux_quiet();
    if (now < S.hint_until) S.hint_until = now;
    if (S.lock.open) { // digits come from touch
        if (!lock_wake(now)) ux_feedback(FB_BUMP);
        return;
    }
    if (S.card) {
        S.card = false;
        return;
    }
    if (S.menu.open) return; // the menu is touch only
    if (S.list.open) {
        picker_turn(dir, now);
        return;
    }
    if (S.mode->turn) S.mode->turn(dir, now);
}

void app_tap(float u, float v, int64_t now)
{
    S.input_at = now;
    ux_quiet();
    if (S.lock.open) {
        if (!lock_wake(now)) lock_tap(u, v, now);
        return;
    }
    if (S.card) {
        S.card = false;
        ux_feedback(FB_TAP);
        return;
    }
    if (S.menu.open) {
        menu_tap(u, v, now);
        return;
    }
    if (S.list.open) {
        picker_tap(u, v, now);
        return;
    }
    if (S.voice == VOICE_LISTENING) { // any tap stops listening
        voice_stop(now);
        return;
    }
    if (S.mode->tap) S.mode->tap(u, v, now);
}

void app_hold(int64_t now)
{
    S.input_at = now;
    ux_quiet();
    if (S.lock.open) {
        if (!lock_wake(now)) lock_hold();
        return;
    }
    if (S.card || S.menu.open || S.list.open || S.voice == VOICE_LISTENING) return;
    if (S.mode->hold) S.mode->hold(now);
    else ux_feedback(FB_BUMP);
}

void app_swipe(swipe_t dir, int64_t now)
{
    S.input_at = now;
    ux_quiet();
    if (S.lock.open) {
        if (!lock_wake(now)) lock_swipe(dir);
        return;
    }
    if (S.card) {
        S.card = false;
        return;
    }
    if (S.voice == VOICE_LISTENING) return;
    if (S.list.open) {
        if (dir == SWIPE_UP || dir == SWIPE_DOWN) picker_close(false, now);
        return;
    }
    if (dir == SWIPE_UP) {
        if (S.menu.open) menu_close();
        else menu_open(now);
        return;
    }
    if (dir == SWIPE_DOWN) {
        if (S.menu.open) menu_close();
        else if (S.mode->swipe) S.mode->swipe(dir, now);
        return;
    }
    if (S.menu.open) return;
    // Elsewhere these swipes do nothing: the color profile changes only in Settings.
    if (S.mode->swipe) S.mode->swipe(dir, now);
    else ux_feedback(FB_BUMP);
}

void app_button(hw_button_t b, int64_t now)
{
    S.input_at = now;
    ux_quiet();
    if (S.lock.open) {
        if (!lock_wake(now)) ux_feedback(FB_BUMP);
        return;
    }
    if (b == BUTTON_MENU) {
        S.card = false;
        if (S.list.open) picker_close(false, now);
        if (S.mode == &MODE_SETTINGS) {
            MODE_SETTINGS.swipe(SWIPE_DOWN, now);
        } else {
            S.menu.open = false;
            MODE_SETTINGS.enter(now);
            S.mode = &MODE_SETTINGS;
            ux_feedback(FB_TAP);
            ux_log("mode", S.mode->key);
        }
        return;
    }
    int vol = S.vol + (b == BUTTON_UP ? 1 : -1);
    vol = vol < 0 ? 0 : vol > 10 ? 10 : vol;
    S.vol = vol;
    hal_volume(vol);
    char msg[16];
    if (vol) snprintf(msg, sizeof msg, "Volume %d", vol);
    else snprintf(msg, sizeof msg, "Muted");
    ux_toast(msg, 1600);
    if (vol) ux_feedback(FB_TICK);
    ux_mark_dirty(now);
}

bool app_list_drag_wanted(void)
{
    if (BOARD.has.knob || S.lock.open || S.menu.open) return false;
    if (S.list.open) return true;
    return S.mode == &MODE_SETTINGS && settings_drag_wanted();
}

void app_list_drag(float dv, int64_t now)
{
    S.input_at = now;
    if (S.list.open) picker_drag(dv, now);
    else settings_drag(dv, now);
}

float app_list_scroll(void) { return S.list.open ? S.list.scroll : S.set.scroll; }

// With no knob, dragging sideways on a clock does what turning the knob does.
bool app_clock_drag_wanted(void)
{
    if (BOARD.has.knob || S.lock.open || S.menu.open || S.list.open || S.card) return false;
    return S.mode == &MODE_TIMER || S.mode == &MODE_POMODORO;
}

// ---- Rotation: the motion sensor reports a tilt, and the UX redraws the screen -----------------------------------------

static float angle_diff(float a, float b)
{
    float d = fmodf(a - b + 180.0f, 360.0f);
    if (d < 0) d += 360.0f;
    return d - 180.0f;
}

static void read_motion(int64_t now)
{
    static int64_t last;
    if (!BOARD.has.motion || now - last < MOTION_EVERY_US) return;
    last = now;
    float x, y, z;
    if (hal_motion_read(&x, &y, &z)) {
        // Gravity along the panel: upright it points to the panel's bottom.
        if (!S.has_tilt_override) {
            const float flat = z < 0 ? -z : z;
            if (!S.flat_override) S.flat = flat > 0.85f;
            if (!S.flat) {
                float a = atan2f(x, y) * 180.0f / PI_F;
                if (a < 0) a += 360.0f;
                S.tilt = a;
            }
        }
        S.face_down = z > 0.7f;
    }
}

// A quarter turn only works on a square panel: the frame turned 90° wouldn't fit a wide one. A wide or tall panel turns
// only upside down.
static bool orient_allowed(int deg)
{
    if (BOARD.width == BOARD.height) return deg % 90 == 0;
    return (deg - BOARD.mount_deg) % 180 == 0;
}

static void check_orient(int64_t now)
{
    if (!S.autorotate || S.flat || fabsf(angle_diff(S.tilt, (float)S.orient)) <= 45 + HYSTERESIS) {
        S.wanted = false;
        return;
    }
    int to = (int)lroundf(S.tilt / 90.0f) * 90 % 360;
    if (to < 0) to += 360;
    if (!orient_allowed(to)) {
        S.wanted = false;
        return;
    }
    if (!S.wanted || S.wanted_to != to) {
        S.wanted = true;
        S.wanted_to = to;
        S.wanted_since = now;
    }
}

static void update_turn(int64_t now)
{
    if (!BOARD.has.motion) return;
    read_motion(now);
    if (S.turning && now - S.turn_at >= TURN_US) {
        S.turning = false;
        ux_log("turn", "done");
    }
    if (S.turning) return;
    check_orient(now);
    if (S.wanted && now - S.wanted_since >= TURN_WAIT_US) {
        S.turning = true;
        S.turn_from = S.orient;
        S.turn_to = S.wanted_to;
        S.turn_at = now;
        S.orient = S.wanted_to;
        S.wanted = false;
    }
}

static float shown_orient(int64_t now)
{
    if (!S.turning) return (float)S.orient;
    float p = (float)(now - S.turn_at) / (float)TURN_US;
    p = p < 0 ? 0 : p > 1 ? 1 : p;
    const float q = 1 - p, eased = 1 - q * q * q;
    return (float)S.turn_from + angle_diff((float)S.turn_to, (float)S.turn_from) * eased;
}

// ---- Drawing ----------------------------------------------------------------------------------------------------------

static void draw_screen(void)
{
    if (S.lock.open && S.lock.pad) {
        lock_draw_pad();
    } else if (S.menu.open) {
        menu_draw();
    } else if (S.list.open) {
        picker_draw();
    } else {
        S.mode->draw();
    }
    if (!(S.lock.open && S.lock.pad) && !S.menu.open) { // the keypad needs no lock to say it's locked
        if (S.list.open || S.mode == &MODE_SETTINGS) {
            status_draw(WHERE_LIST);
        } else if (S.mode == &MODE_ZOO || S.mode == &MODE_CLOCK) {
            if (!S.card) status_draw(clock_running() ? WHERE_ARC : WHERE_ZOO);
        } else {
            status_draw(S.mode->status);
        }
    }
    toast_draw();
}

// A full paint into another buffer, with a real pairing code hidden. The screen stays as it was.
void ux_redraw_for_capture(uint16_t *target)
{
    F.capture_mask = true;
    gfx_paint_begin(~0ULL, target);
    draw_screen();
    gfx_paint_end();
    F.capture_mask = false;
}

static void burn_in(int64_t now)
{
    if (BOARD.panel != PANEL_AMOLED) return;
    if (S.has_burn_override) {
        gfx_set_offset(S.burn_x, S.burn_y);
        return;
    }
    // Fixed elements need a small shift: a slow drift of up to 2 pixels.
    const double t = (double)now / 1e6;
    gfx_set_offset((int)lround(2 * sin(t / 47)), (int)lround(2 * sin(t / 61)));
}

// ---- Handlers from the link ---------------------------------------------------------------------------------------------

static void on_connected(const char *host)
{
    (void)host;
    clocksync_connected();
    ux_log("link", "hello");
}

static void on_disconnected(void)
{
    S.agents_syncing = false;
    ux_log("link", "closed");
}

static void on_state(const link_state_t *s)
{
    const int64_t now = hal_now_us();
    S.toggl = s->toggl;
    if (s->has_voice && s->voice != S.voice_ok) {
        S.voice_ok = s->voice;
        ux_mark_dirty(now);
    }
    zoo_on_count(s->n, now);
}

static void on_agents_begin(void) { zoo_agents_begin(hal_now_us()); }
static void on_agent_row(const link_agent_t *a) { zoo_agents_row(a, hal_now_us()); }
static void on_agents_end(bool whole) { zoo_agents_end(whole, hal_now_us()); }

static void on_rejected(uint32_t rid)
{
    (void)rid;
    ux_bump("not saved in Toggl");
}

static void on_list(link_list_t kind, int req, const void *rows, int count) { picker_on_list(kind, req, rows, count, hal_now_us()); }
static void on_fetch_failed(link_list_t kind, int req) { picker_on_fetch_failed(kind, req, hal_now_us()); }
static void on_sync(const link_sync_t *s) { timer_on_sync(s, hal_now_us()); }
static void on_transcript(const link_transcript_t *t) { voice_on_transcript(t, hal_now_us()); }
static void on_clock(const link_clock_t *c) { clocksync_on_mac(c, hal_now_us()); }
static bool lock_is_set(void) { return S.lock.has_code; }

static const link_handlers_t HANDLERS = {
    .connected = on_connected,
    .disconnected = on_disconnected,
    .state = on_state,
    .agents_begin = on_agents_begin,
    .agent_row = on_agent_row,
    .agent_gone = zoo_agents_gone,
    .agents_end = on_agents_end,
    .entry_rejected = on_rejected,
    .list = on_list,
    .fetch_failed = on_fetch_failed,
    .sync = on_sync,
    .transcript = on_transcript,
    .lock_code = lock_on_code,
    .clock = on_clock,
};

// ---- Auto switch: while a clock runs, its screen left untouched gives way to Split view --------------------------------

static void auto_switch(int64_t now)
{
    if (!S.auto_switch || !clock_running() || (S.mode != &MODE_TIMER && S.mode != &MODE_POMODORO)) return;
    // Not over something open, and not in the middle of something on that screen.
    if (S.menu.open || S.list.open || S.card || (S.lock.open && S.lock.pad) || S.voice != VOICE_IDLE || S.syncing) return;
    if (now - S.input_at < (int64_t)S.split_after_s * SEC_US) return;
    const ux_mode_t *to = S.split == SPLIT_CLOCK ? &MODE_CLOCK : &MODE_ZOO;
    if (to->enter) to->enter(now);
    S.mode = to;
    ux_mark_dirty(now);
    ux_log("mode", to->key);
}

// ---- The loop -----------------------------------------------------------------------------------------------------------

void ux_log(const char *event, const char *detail)
{
#ifdef BENTO_DEBUG_INTERFACE
    char line[176];
    const int n = snprintf(line, sizeof line, "LOG %s %s\n", event, detail ? detail : "");
    if (n > 0) hal_debug_write(line, (size_t)(n < (int)sizeof line ? n : (int)sizeof line - 1));
#else
    (void)event;
    (void)detail;
#endif
}

void ux_init(void)
{
    memset(&S, 0, sizeof S);
    memset(&F, 0, sizeof F);
    F.L = BOARD.shape == SHAPE_ROUND ? &LAYOUT_ROUND : &LAYOUT_SQUARE;
    F.T = &THEMES[0];
    S.orient = BOARD.mount_deg;
    if (BOARD.width != BOARD.height && BOARD.mount_deg % 180) {
        ux_log("error", "a non-square panel mounted a quarter turn: shown upright instead"); // see docs/ADDING-A-BOARD.md
        S.orient = 0;
    }
    gfx_init(BOARD.width, BOARD.height, (float)BOARD.width / F.L->base);
    menu_prepare();

    S.agents = hal_alloc_large(LINK_MAX_AGENTS * sizeof(agent_t));
    S.projects = hal_alloc_large(LINK_MAX_PROJECTS * sizeof(link_project_t));
    S.tasks = hal_alloc_large(LINK_MAX_TASKS * sizeof(link_task_t));
    if (!S.agents || !S.projects || !S.tasks) {
        // Every board has the room (PSRAM): with none, the rest of the UX can't work, so stop here and say why, rather
        // than check these on every use.
        printf("ux: no memory for the agents and lists (PSRAM missing?)\n");
        abort();
    }
    memset(S.agents, 0, LINK_MAX_AGENTS * sizeof(agent_t));
    memset(S.projects, 0, LINK_MAX_PROJECTS * sizeof(link_project_t));
    memset(S.tasks, 0, LINK_MAX_TASKS * sizeof(link_task_t));

    S.mode = &MODE_TIMER;
    S.split = SPLIT_AGENTS;
    S.auto_switch = true;
    S.split_after_s = 15;
    S.bright = HAL_BRIGHT_LEVELS - 1;
    S.vol = 6;
    S.autorotate = true;
    S.voice_ok = true;
    S.p.set_min = 25;
    S.project.id = -1;
    S.picked_at = INT64_MIN / 2; // long ago: nothing was picked by hand
    S.set.forget_row = -1;
    S.press_v = -1;
    store_load();
    pomo_reset();
    settings_apply_all();
    hal_power_read(&S.power);
    F.T = &THEMES[S.theme ? 1 : 0];
    link_init(&HANDLERS, BENTO_FW_VERSION, lock_is_set);
    S.cs.seq = hal_random() & 0x3FFFFFFF; // numbers from before a restart can't be mistaken for these
    voice_init();
    S.offline_since = hal_now_us();
    S.input_at = hal_now_us(); // a clock running since before a restart still waits its time
#ifdef BENTO_DEBUG_INTERFACE
    debug_init();
#endif
    ux_log("boot", BOARD.shape == SHAPE_ROUND ? "round" : "square");
}

void ux_frame(void)
{
    static int64_t last, power_at;
    const int64_t t_start = hal_now_us();
    const int64_t now = t_start;
    F.now = now;
    F.dt_ms = last ? (float)(now - last) / 1000.0f : 16;
    if (F.dt_ms > 100) F.dt_ms = 100;
    last = now;
    F.t = S.has_anim_override ? S.anim_override : (float)fmod((double)now / 1e6, 3600.0);
    F.L = BOARD.shape == SHAPE_ROUND ? &LAYOUT_ROUND : &LAYOUT_SQUARE;

#ifdef BENTO_DEBUG_INTERFACE
#define FPU_STEP(name) debug_fpu(name)
#else
#define FPU_STEP(name)
#endif
    FPU_STEP("sleep"); // what ran while this task slept, before this frame
    input_poll(now);
    FPU_STEP("input");
    link_poll(now);
    FPU_STEP("link");
    const bool was = S.connected;
    S.connected = link_up();
    if (was && !S.connected) S.offline_since = now;
    if (link_take_dropped()) ux_bump("not saved in Toggl");
    voice_update(now);
    FPU_STEP("voice");
#ifdef BENTO_DEBUG_INTERFACE
    debug_poll(now);
#endif
    FPU_STEP("debug");
    clock_tick(now);
    auto_switch(now);
    menu_tick(now);
    picker_tick(now);
    settings_tick(now);
    zoo_tick(now);
    lock_tick(now);
    fx_tick();
    update_turn(now);
    if (now - power_at > 5 * SEC_US) {
        power_at = now;
        hal_power_read(&S.power);
    }
    if (S.dirty_since && now - S.dirty_since > SAVE_DELAY_US) {
        store_save_settings();
        S.dirty_since = 0;
    }

    F.T = &THEMES[S.theme ? 1 : 0];
    burn_in(now);
#ifdef BENTO_DEBUG_INTERFACE
    const int64_t t_record = hal_now_us();
#endif
    FPU_STEP("ticks");
    gfx_record_begin();
    draw_screen();
    const uint64_t dirty = gfx_record_end();
    FPU_STEP("record");
#ifdef BENTO_DEBUG_INTERFACE
    debug_why(dirty);
    const int64_t t_paint = hal_now_us();
#endif
    if (dirty) {
        gfx_paint_begin(dirty, NULL);
        draw_screen();
        gfx_paint_end();
    }
    FPU_STEP("paint");
#ifdef BENTO_DEBUG_INTERFACE
    const int64_t t_present = hal_now_us();
#endif
    screen_present(dirty, S.orient, S.turning, shown_orient(now));
    FPU_STEP("present");
#ifdef BENTO_DEBUG_INTERFACE
    const int64_t t_end = hal_now_us();
    int bands = 0;
    for (uint64_t d = dirty; d; d &= d - 1) bands++;
    debug_frame_stats(t_paint - t_record, t_present - t_paint, t_end - t_present, bands, t_end - t_start);
    if (debug_verify_on()) {
        static uint16_t *scratch;
        if (!scratch) scratch = hal_alloc_large((size_t)BOARD.width * (size_t)BOARD.height * sizeof(uint16_t));
        if (scratch) {
            gfx_paint_begin(~0ULL, scratch);
            draw_screen();
            gfx_paint_end();
            const uint16_t *fb = gfx_framebuffer();
            int stale = 0;
            for (int b = 0; b < gfx_band_count(); b++) {
                const int y0 = b * HAL_BAND_ROWS;
                int rows = BOARD.height - y0;
                if (rows > HAL_BAND_ROWS) rows = HAL_BAND_ROWS;
                if (memcmp(fb + y0 * BOARD.width, scratch + y0 * BOARD.width, (size_t)rows * (size_t)BOARD.width * 2)) stale++;
            }
            debug_verify_result(stale);
        }
    }
#endif
    const int64_t spent = hal_now_us() - t_start;
    hal_sleep_ms(spent < FRAME_US ? (int)((FRAME_US - spent) / MS_US) + 1 : 1);
}
