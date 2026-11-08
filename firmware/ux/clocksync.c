// The clocks every board shows, kept in step through the Mac (protocol/PROTOCOL.md, `clock`).
//
// A change made here (a tap, a hold, the knob, a new task or project) goes to the Mac, which passes it to every other
// board. A change made on another board arrives from the Mac and is shown here. The newest change wins: while a change of
// this board's is on its way, only a newer one is shown, and the Mac's answer to it settles which.
//
// The Toggl run stays one run. A board showing another board's run names it with `of` when it stops or changes it, and
// starting again is always a new run of this board's own. When the Mac's clocks replace a run this board started, this
// board stops that run where the change happened. Every stop is safe to send twice: the Mac keeps the first.
#include <string.h>

#include "app.h"

#ifdef BENTO_DEBUG_INTERFACE
#include <stdio.h>
// A clock event with its numbers, for the debug log: seq, for, rev and the Pomodoro's length, received and shown.
static void clock_log(const char *what, const link_clock_t *c)
{
    char detail[150];
    const int64_t now = hal_now_us();
    if (c) {
        snprintf(detail, sizeof detail, "%s seq=%lu for=%ld rev=%lld from=%lx pomo=%d/%d/%llds shown=%d/%d/%llds", what,
                 (unsigned long)c->seq, c->has_for ? (long)c->for_seq : -1L, (long long)c->rev, (unsigned long)c->from, c->pomo.st,
                 c->pomo.set_min, (long long)(c->pomo.left_us / SEC_US), S.p.state, S.p.set_min, (long long)(pomo_remaining(now) / SEC_US));
    } else {
        snprintf(detail, sizeof detail, "%s seq=%lu shown=%d/%d/%llds", what, (unsigned long)S.cs.sent_seq, S.p.state, S.p.set_min,
                 (long long)(pomo_remaining(now) / SEC_US));
    }
    ux_log("clock", detail);
}
#define CLOCK_LOG(what, c) clock_log(what, c)
#else
#define CLOCK_LOG(what, c) ux_log("clock", what)
#endif

void clock_changed(int64_t now)
{
    S.cs.want_send = true;
    S.cs.pending = true;
    S.cs.mine = true;
    S.cs.has_at = true;
    S.cs.at_us = now;
}

void clocksync_connected(void)
{
    S.cs.want_send = true; // say what's shown, changed or not
    S.cs.rev = 0;          // the Mac's count is only in order within one connection
}

static void snapshot(link_clock_t *c, int64_t now)
{
    memset(c, 0, sizeof *c);
    c->has_at = S.cs.has_at;
    c->at_us = S.cs.at_us;
    utf8_copy(c->task, sizeof c->task, S.task);
    c->task_id = S.task_id;
    c->project = S.project;
    c->timer.st = S.t.state;
    c->timer.acc_us = S.t.session_us;
    c->timer.start_us = S.t.start_us;
    c->timer.rid = S.t.rid;
    c->timer.of = S.t.of;
    c->pomo.st = S.p.state;
    c->pomo.set_min = S.p.set_min;
    c->pomo.total_us = S.p.total_us;
    // What was left when the clocks last changed: a running countdown has run on by itself since.
    int64_t left = pomo_remaining(now);
    if (S.p.state == RUN_RUNNING && S.cs.has_at && now > S.cs.at_us) left += now - S.cs.at_us;
    if (left > S.p.total_us) left = S.p.total_us;
    c->pomo.left_us = S.p.state == RUN_DONE ? 0 : left;
    c->pomo.rid = S.p.rid;
    c->pomo.of = S.p.of;
}

void clocksync_tick(int64_t now)
{
    if (!S.cs.want_send || !link_ready()) return;
    static link_clock_t c;
    snapshot(&c, now);
    const uint32_t seq = (S.cs.seq + 1) & 0x3FFFFFFF;
    if (!link_send_clock(&c, S.cs.pending, seq, now)) return;
    S.cs.seq = S.cs.sent_seq = seq;
    S.cs.sent_at_us = S.cs.at_us;
    S.cs.want_send = false;
    CLOCK_LOG(S.cs.pending ? "sent changed" : "sent", NULL);
}

static int64_t not_after(int64_t t, int64_t now) { return t > now ? now : t; }

// A run this board started, replaced by the Mac's clocks: it stops where the change happened, and never before it began.
static void stop_own(uint32_t rid, int64_t began_us, int64_t at_us, int64_t now)
{
    link_toggl_stop(rid, 0, not_after(at_us > began_us ? at_us : began_us, now));
}

static void apply_timer(const link_clock_part_t *p, int64_t at_us, int64_t now)
{
    const bool same = p->st == RUN_RUNNING && p->rid == S.t.rid && p->of == S.t.of;
    if (S.t.state == RUN_RUNNING && !S.t.of && !same) stop_own(S.t.rid, S.t.start_us, at_us, now);
    S.t.state = p->st == RUN_RUNNING ? RUN_RUNNING : p->st == RUN_PAUSED ? RUN_PAUSED : RUN_IDLE;
    S.t.session_us = p->acc_us;
    if (S.t.state == RUN_RUNNING) S.t.start_us = not_after(p->start_us, now);
    S.t.rid = p->rid;
    S.t.of = p->of;
    S.t.shift_pending = false; // a turn here was overtaken
}

static void apply_pomo(const link_clock_part_t *p, int64_t at_us, int64_t now)
{
    const bool same = p->st == RUN_RUNNING && p->rid == S.p.rid && p->of == S.p.of;
    if (S.p.state == RUN_RUNNING && !S.p.of && !same) stop_own(S.p.rid, S.p.run_start_us, at_us, now);
    S.p.set_min = p->set_min;
    S.p.total_us = p->total_us > 0 ? p->total_us : p->set_min * MIN_US;
    S.p.rid = p->rid;
    S.p.of = p->of;
    if (p->st == RUN_RUNNING) {
        const int64_t left = p->left_us - (now - at_us);
        if (left > 0) {
            if (!same) S.p.run_start_us = not_after(at_us, now);
            S.p.state = RUN_RUNNING;
            S.p.remaining_us = left;
            S.p.last_us = now;
        } else { // it ran out while this board was away: it's done, with no alarm now
            S.p.state = RUN_DONE;
            S.p.remaining_us = 0;
            if (p->rid) link_toggl_stop(p->rid, p->of, not_after(at_us + p->left_us, now));
        }
    } else if (p->st == RUN_DONE) {
        S.p.state = RUN_DONE;
        S.p.remaining_us = 0;
    } else {
        S.p.state = p->st == RUN_PAUSED ? RUN_PAUSED : RUN_IDLE;
        S.p.remaining_us = p->left_us > 0 ? p->left_us : S.p.total_us;
    }
    if (S.p.state != RUN_DONE) ux_quiet(); // the time's-up was dismissed on the other board
}

void clocksync_on_mac(const link_clock_t *c, int64_t now)
{
    if (c->rev < S.cs.rev) { // older than one already shown in this connection
        CLOCK_LOG("old rev", c);
        return;
    }
    const bool answers_latest = c->has_for && c->for_seq == S.cs.sent_seq;
    // A change made here that hasn't gone out yet is newer than anything the Mac can have: an answer about this board's
    // own changes is overtaken, and another board's only wins if it's newer. Taking the answer to the last change sent
    // used to settle it, so the newer change went out as a mere report, the Mac answered with the older one, and the
    // knob's number jumped back as it stopped turning.
    const bool unsent = S.cs.pending && S.cs.mine && S.cs.has_at && S.cs.at_us > S.cs.sent_at_us;
    if (unsent && (!c->from || !c->has_at || c->at_us <= S.cs.at_us)) {
        CLOCK_LOG("kept", c);
        return;
    }
    // One of this board's own earlier changes, coming back while a later one is on its way: that's already overtaken
    // here. Its time can't settle it: the Mac's `now` is stamped before the trip, so on a quick turn of the knob an old
    // answer looked newer than the latest change, and the digits flicked back to it.
    if (S.cs.pending && !answers_latest && !c->from && c->seq != S.cs.sent_seq) {
        CLOCK_LOG("kept", c);
        return;
    }
    if (S.cs.pending && !answers_latest && (!c->has_at || c->at_us <= S.cs.at_us)) {
        CLOCK_LOG("kept", c); // the change made here is newer, and on its way
        return;
    }
    S.cs.rev = c->rev;
    S.cs.pending = false;
    if (!c->from && c->seq == S.cs.sent_seq && S.cs.mine) {
        CLOCK_LOG("taken", c); // this board's own change: it's what's shown
        return;
    }
    utf8_copy(S.task, sizeof S.task, c->task);
    S.task_id = c->task_id;
    S.project = c->project;
    if (S.project.id > 0) {
        const link_project_t *known = picker_find_project(S.project.id);
        if (known) S.project = *known;
    }
    apply_timer(&c->timer, c->at_us, now);
    apply_pomo(&c->pomo, c->at_us, now);
    S.cs.mine = false;
    S.cs.has_at = c->has_at;
    S.cs.at_us = c->at_us;
    ux_mark_dirty(now); // the task, the project and the Pomodoro's length are saved settings too
    CLOCK_LOG("applied", c);
}
