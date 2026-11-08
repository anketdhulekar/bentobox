// The Timer, which logs to Toggl. It counts up; each running stretch is its own entry.
#include <string.h>

#include "../app.h"

#define SYNC_TIMEOUT_US (15 * SEC_US)

static int64_t project_id(void) { return S.project.id > 0 ? S.project.id : 0; }

// A start set during a frame can be later than the frame's own `now`: count that as no time yet, never less than none.
int64_t timer_elapsed(int64_t now) { return S.t.session_us + (S.t.state == RUN_RUNNING && now > S.t.start_us ? now - S.t.start_us : 0); }

static bool timer_running(void) { return S.t.state == RUN_RUNNING; }

// A fast spin of the knob is one message, sent once the knob rests.
void timer_flush_shift(int64_t now)
{
    if (!S.t.shift_pending) return;
    S.t.shift_pending = false;
    link_toggl_shift(S.t.rid, S.t.of, S.t.start_us, now);
    clock_changed(now);
}

void timer_start_at(int64_t now)
{
    if (S.t.state == RUN_IDLE) S.t.session_us = 0;
    S.t.start_us = now;
    S.t.state = RUN_RUNNING;
    S.t.rid = link_toggl_start(S.task, S.task_id, project_id(), now);
    S.t.of = 0;
    picker_remember_task(S.task, S.task_id, &S.project);
    clock_changed(now);
}

void timer_pause_at(int64_t now)
{
    timer_flush_shift(now);
    S.t.session_us += now - S.t.start_us;
    S.t.state = RUN_PAUSED;
    link_toggl_stop(S.t.rid, S.t.of, now);
    clock_changed(now);
}

static void timer_draw(void) { clock_draw(false); }

static void timer_tap(float u, float v, int64_t now)
{
    if (clock_tap_common(u, v, now)) return;
    if (S.t.state == RUN_RUNNING) timer_pause_at(now);
    else timer_start_at(now);
    ux_feedback(FB_TAP);
}

static void timer_hold(int64_t now)
{
    if (S.t.state == RUN_IDLE && clock_task_project_cleared()) {
        ux_bump(NULL);
        return;
    }
    if (S.t.state == RUN_RUNNING) {
        timer_flush_shift(now);
        link_toggl_stop(S.t.rid, S.t.of, now);
    }
    S.t.state = RUN_IDLE;
    S.t.session_us = 0;
    clock_clear_task_project(now);
    clock_changed(now);
    ux_hint("reset", 1400);
    ux_feedback(FB_DOUBLE);
}

static void timer_turn(int dir, int64_t now)
{
    if (S.t.state != RUN_RUNNING) {
        ux_bump(NULL);
        return;
    }
    if (dir < 0 && now - S.t.start_us < MIN_US) {
        ux_bump(NULL); // never past the start of the running stretch
        return;
    }
    S.t.start_us -= dir * MIN_US;
    S.t.shift_pending = true;
    S.t.shift_at = now;
    ux_feedback(FB_TICK);
}

// Swipe down: one check of what's running in Toggl.
static void timer_swipe(swipe_t dir, int64_t now)
{
    if (dir != SWIPE_DOWN) {
        ux_feedback(FB_BUMP);
        return;
    }
    if (!S.connected) {
        ux_bump("no Mac");
        return;
    }
    if (S.toggl == TOGGL_NO_KEY) {
        ux_bump("Toggl isn\xE2\x80\x99t set up");
        return;
    }
    if (S.syncing) return;
    timer_flush_shift(now);
    const int req = link_sync(S.t.state == RUN_RUNNING ? S.t.rid : 0);
    if (!req) {
        ux_bump("no Mac");
        return;
    }
    S.syncing = true;
    S.sync_req = req;
    S.sync_since = now;
    S.hint_until = 0;
    ux_feedback(FB_TAP);
}

void timer_on_sync(const link_sync_t *s, int64_t now)
{
    if (!S.syncing || s->req != S.sync_req) return;
    S.syncing = false;
    if (S.mode != &MODE_TIMER) return; // (switched modes meanwhile)
    switch (s->result) {
    case SYNC_ADOPTED: { // a timer started elsewhere: show it and count on
        utf8_copy(S.task, sizeof S.task, s->desc);
        S.task_id = s->task_id;
        S.project = s->project;
        const link_project_t *known = s->project.id > 0 ? picker_find_project(s->project.id) : NULL;
        if (known) S.project = *known;
        S.t.session_us = 0;
        int64_t start = now;
        if (link_clock_known() && s->start > 0) {
            const int64_t ago = link_unix_now(now) - s->start;
            if (ago > 0 && ago < 400 * 24 * 3600) start = now - ago * SEC_US;
        }
        S.t.start_us = start;
        S.t.state = RUN_RUNNING;
        S.t.shift_pending = false; // a turn meant for the run before this one
        S.t.rid = s->rid ? s->rid : link_toggl_new_rid();
        S.t.of = 0; // the Mac gave the run to this board
        ux_mark_dirty(now);
        clock_changed(now);
        ux_hint("synced from Toggl", 1400);
        ux_feedback(FB_TAP);
        break;
    }
    case SYNC_STOPPED:
        if (S.t.state == RUN_RUNNING) {
            S.t.session_us += now - S.t.start_us;
            S.t.state = RUN_PAUSED;
            clock_changed(now);
        }
        ux_hint("stopped in Toggl", 1400);
        ux_feedback(FB_TAP);
        break;
    case SYNC_IN_SYNC:
        ux_hint("in sync", 1400);
        ux_feedback(FB_TAP);
        break;
    default:
        ux_bump("couldn\xE2\x80\x99t reach Toggl");
        break;
    }
}

void timer_tick(int64_t now)
{
    if (S.t.shift_pending && now - S.t.shift_at > 250 * MS_US) timer_flush_shift(now);
    if (S.syncing && now - S.sync_since > SYNC_TIMEOUT_US) {
        S.syncing = false;
        ux_bump("couldn\xE2\x80\x99t reach Toggl");
    }
}

const ux_mode_t MODE_TIMER = {
    .key = "timer", .label = "Timer", .color = 0x14B8A6,
    .running = timer_running, .draw = timer_draw,
    .turn = timer_turn, .tap = timer_tap, .hold = timer_hold, .swipe = timer_swipe,
    .status = WHERE_CLOCK,
};
