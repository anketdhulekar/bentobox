// The Pomodoro. It counts down from 1 to 60 minutes, and logs its running stretches to Toggl too.
#include "../app.h"

#define MAX_MIN 60
#define ALARM_US (HAL_ALARM_S * SEC_US) // as long as the board's alarm runs

static int64_t project_id(void) { return S.project.id > 0 ? S.project.id : 0; }

int64_t pomo_remaining(int64_t now)
{
    if (S.p.state != RUN_RUNNING) return S.p.remaining_us;
    // A change from the Mac can land during a frame, timed after the frame's own `now`: that's no time run yet, not a
    // moment back (the time would show a second more for one frame).
    const int64_t ran = now > S.p.last_us ? now - S.p.last_us : 0;
    const int64_t left = S.p.remaining_us - ran;
    return left > 0 ? left : 0;
}

void pomo_reset(void)
{
    S.p.state = RUN_IDLE;
    S.p.remaining_us = S.p.total_us = S.p.set_min * MIN_US;
}

// How much of this countdown is left: 1 at the start, whatever its length, and 0 at zero.
float pomo_fraction(int64_t now)
{
    if (S.p.total_us <= 0) return 0;
    const float f = (float)pomo_remaining(now) / (float)S.p.total_us;
    return f < 0 ? 0 : f > 1 ? 1 : f;
}

static bool pomo_running(void) { return S.p.state == RUN_RUNNING; }

static void pomo_draw(void) { clock_draw(true); }

void pomo_set_remaining(int64_t remaining_us, int64_t now)
{
    S.p.remaining_us = remaining_us;
    S.p.last_us = now;
    if (S.p.remaining_us > S.p.total_us) S.p.total_us = S.p.remaining_us;
}

static void pomo_tap(float u, float v, int64_t now)
{
    if (clock_tap_common(u, v, now)) return;
    if (S.p.state == RUN_DONE) {
        pomo_reset();
        clock_changed(now);
        ux_feedback(FB_TAP);
        return;
    }
    if (S.p.state == RUN_RUNNING) {
        S.p.remaining_us = pomo_remaining(now);
        S.p.state = RUN_PAUSED;
        link_toggl_stop(S.p.rid, S.p.of, now);
    } else {
        S.p.last_us = now;
        S.p.run_start_us = now;
        S.p.state = RUN_RUNNING;
        S.p.rid = link_toggl_start(S.task, S.task_id, project_id(), now);
        S.p.of = 0;
        picker_remember_task(S.task, S.task_id, &S.project);
    }
    clock_changed(now);
    ux_feedback(FB_TAP);
}

static void pomo_hold(int64_t now)
{
    if (S.p.state == RUN_IDLE && clock_task_project_cleared()) {
        ux_bump(NULL);
        return;
    }
    if (S.p.state == RUN_RUNNING) link_toggl_stop(S.p.rid, S.p.of, now);
    ux_quiet();
    pomo_reset();
    clock_clear_task_project(now);
    clock_changed(now);
    ux_hint("reset", 1400);
    ux_feedback(FB_DOUBLE);
}

static void pomo_turn(int dir, int64_t now)
{
    switch (S.p.state) {
    case RUN_IDLE: {
        const int next = S.p.set_min + dir;
        if (next < 1 || next > MAX_MIN) {
            ux_bump(NULL);
            return;
        }
        S.p.set_min = next;
        pomo_reset();
        ux_mark_dirty(now);
        clock_changed(now);
        ux_feedback(FB_TICK);
        break;
    }
    case RUN_PAUSED: {
        // To the next whole minute. The set length stays, and "full" grows only if the time goes past it.
        const int64_t whole = S.p.remaining_us / MIN_US;
        int64_t next = dir > 0 ? whole + 1 : (S.p.remaining_us % MIN_US ? whole : whole - 1);
        if (next < 1) next = 1;
        if (next > MAX_MIN) next = MAX_MIN;
        if (next * MIN_US == S.p.remaining_us) {
            ux_bump(NULL);
            return;
        }
        S.p.remaining_us = next * MIN_US;
        if (S.p.remaining_us > S.p.total_us) S.p.total_us = S.p.remaining_us;
        clock_changed(now);
        ux_feedback(FB_TICK);
        break;
    }
    case RUN_DONE:
        ux_bump(NULL);
        break;
    default:
        ux_bump("pause to adjust");
        break;
    }
}

static void pomo_swipe(swipe_t dir, int64_t now)
{
    (void)now;
    if (dir == SWIPE_DOWN) ux_bump("tracking only");
    else ux_feedback(FB_BUMP);
}

// The Pomodoro runs out: back to its face, wherever you are.
void pomo_tick(int64_t now)
{
    if (S.p.state == RUN_RUNNING && pomo_remaining(now) <= 0) {
        S.p.state = RUN_DONE;
        S.p.remaining_us = 0;
        S.menu.open = false;
        S.card = false;
        if (S.list.open) picker_close(false, now);
        S.mode = &MODE_POMODORO;
        link_toggl_stop(S.p.rid, S.p.of, now); // every board showing it sends this: the Mac keeps the first
        ux_feedback(FB_ALARM);
        S.alarm_until = now + ALARM_US;
        ux_log("times_up", "");
    }
    if (S.alarm_until && now >= S.alarm_until) ux_quiet();
}

const ux_mode_t MODE_POMODORO = {
    .key = "pomodoro", .label = "Pomodoro", .color = 0xEF4A35,
    .running = pomo_running, .draw = pomo_draw,
    .turn = pomo_turn, .tap = pomo_tap, .hold = pomo_hold, .swipe = pomo_swipe,
    .status = WHERE_CLOCK,
};
