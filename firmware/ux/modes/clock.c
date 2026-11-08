// What the Timer and the Pomodoro share: their face, the section above an agent, the task and the project.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"

#define MARQUEE_REST_US (2 * SEC_US) // a long task rests at its start this long before each pass
#define MARQUEE_PX_PER_S 30
#define MARQUEE_GAP 48

// ---- Time and color ------------------------------------------------------------------------------------------------

void clock_format(bool pomo, int64_t now, char out[12])
{
    if (pomo) {
        const int64_t rem = pomo_remaining(now);
        const int s = (int)((rem + SEC_US - 1) / SEC_US);
        snprintf(out, 12, "%02d:%02d", s / 60 % 100, s % 60);
    } else {
        int64_t s = timer_elapsed(now) / SEC_US;
        if (s > 99 * 3600 + 3599) s = 99 * 3600 + 3599;
        const int h = (int)(s / 3600), m = (int)(s / 60 % 60), x = (int)(s % 60);
        if (h) snprintf(out, 12, "%02d:%02d:%02d", h, m, x);
        else snprintf(out, 12, "%02d:%02d", m, x);
    }
}

static uint32_t clock_warm_rgb(int64_t remaining_us)
{
    static int64_t last_sec = -1;
    static int last_theme = -1;
    static uint32_t last_rgb;
    const int64_t sec = remaining_us / SEC_US; // the color moves too slowly to need more
    if (sec != last_sec || S.theme != last_theme) {
        last_sec = sec;
        last_theme = S.theme;
        last_rgb = color_warm(S.theme ? 0x000000u : 0xFFFFFFu, (float)remaining_us / (float)MIN_US);
    }
    return last_rgb;
}

uint16_t clock_warm565(int64_t remaining_us) { return rgb565(clock_warm_rgb(remaining_us)); }

// The last minute's pulse, like a heartbeat: 0 to 1, once a second.
float clock_pulse(int64_t now)
{
    if (S.has_pulse_override) return S.pulse_override;
    if (!(S.p.state == RUN_DONE || (S.p.state == RUN_RUNNING && pomo_remaining(now) <= MIN_US))) return 0;
    const float ph = (float)(now / MS_US % 1000) / 1000.0f;
    return ph < 0.18f ? sinf((ph / 0.18f) * PI_F / 2) : 0.5f + 0.5f * cosf(((ph - 0.18f) / 0.82f) * PI_F);
}

// ---- The task and the project ---------------------------------------------------------------------------------------

bool clock_task_project_cleared(void) { return !S.task[0] && !S.task_id && S.project.id <= 0; }

void clock_clear_task_project(int64_t now)
{
    S.task[0] = 0;
    S.task_id = 0;
    memset(&S.project, 0, sizeof S.project); // id 0: "No project" was chosen
    ux_mark_dirty(now);
}

static int64_t project_id(void) { return S.project.id > 0 ? S.project.id : 0; }

static const ux_mode_t *running_clock_rid(uint32_t *rid, uint32_t *of)
{
    const ux_mode_t *run = clock_running();
    *rid = run == &MODE_TIMER ? S.t.rid : S.p.rid;
    *of = run == &MODE_TIMER ? S.t.of : S.p.of;
    return run;
}

void clock_set_project(const link_project_t *p, bool push, int64_t now)
{
    if (p) S.project = *p;
    else memset(&S.project, 0, sizeof S.project);
    ux_mark_dirty(now);
    clock_changed(now);
    uint32_t rid, of;
    if (push && running_clock_rid(&rid, &of)) {
        if (clock_running() == &MODE_TIMER) timer_flush_shift(now);
        link_toggl_update(rid, of, LINK_UPD_PROJECT | LINK_UPD_TASK, S.task, S.task_id, project_id(), now);
        picker_remember_task(S.task, S.task_id, &S.project);
    }
}

// split: picking a task while the Timer runs starts a new entry, and the counter keeps going.
// Otherwise a running entry is renamed.
void clock_set_task(const char *text, int64_t task_id, bool split, int64_t now)
{
    char copy[LINK_TASK_LEN];
    utf8_copy(copy, sizeof copy, text);
    memcpy(S.task, copy, sizeof S.task);
    S.task_id = task_id;
    ux_mark_dirty(now);
    clock_changed(now);
    uint32_t rid, of;
    const ux_mode_t *run = running_clock_rid(&rid, &of);
    if (!run) return;
    if (run == &MODE_TIMER) timer_flush_shift(now);
    if (split && run == &MODE_TIMER) {
        S.t.session_us += now - S.t.start_us;
        S.t.start_us = now;
        S.t.rid = link_toggl_start(S.task, S.task_id, project_id(), now); // (the app stops the entry before it)
        S.t.of = 0;
    } else {
        link_toggl_update(rid, of, LINK_UPD_DESC | LINK_UPD_TASK | LINK_UPD_PROJECT, S.task, S.task_id, project_id(), now);
    }
    picker_remember_task(S.task, S.task_id, &S.project);
}

// ---- Input -----------------------------------------------------------------------------------------------------------

static lay_clock_t clock_layout(void)
{
    lay_clock_t C = F.L->clock;
    if (!BOARD.has.mic) {
        C.pillY += C.noMicShift;
        C.taskY += C.noMicShift;
        C.timeY += C.noMicShift;
        C.statusY += C.noMicShift;
    }
    return C;
}

static bool mic_shown(void) { return BOARD.has.mic && S.voice_ok; }

bool clock_tap_common(float u, float v, int64_t now)
{
    const lay_clock_t C = clock_layout();
    const float mid = F.L->base / 2;
    if (mic_shown()) {
        const float dx = u - mid, dy = v - C.micY, d2 = dx * dx + dy * dy;
        const float hit = C.micR + 10, dead = C.micR + 20;
        if (d2 < hit * hit) {
            if (S.voice == VOICE_IDLE) voice_start(now);
            return true;
        }
        if (d2 < dead * dead && v > C.statusY + 5) { // a near miss on the mic: don't start the clock
            ux_feedback(FB_BUMP);
            return true;
        }
    }
    if (v < C.taskY + 10) { // the pill, or the task above the time
        picker_open(v < C.pillY + C.pill * 0.95f + 6 ? LIST_PROJECTS : LIST_TASKS, now);
        return true;
    }
    return false;
}

// ---- The face ----------------------------------------------------------------------------------------------------------

const char *clock_status_text(bool pomo, int64_t now, bool *amber)
{
    static char buf[80];
    const run_t st = pomo ? S.p.state : S.t.state;
    *amber = false;
    if (ux_hinting(now)) return S.hint;
    if (S.voice == VOICE_LISTENING) return "";
    if (S.voice != VOICE_IDLE) return "transcribing\xE2\x80\xA6";
    if (S.syncing && !pomo) return "syncing\xE2\x80\xA6";
    const agent_t *a = zoo_first_needing();
    if (a) {
        snprintf(buf, sizeof buf, "%s needs you", a->a.name);
        *amber = true;
        return buf;
    }
    if (!S.connected && st == RUN_RUNNING) return "no Mac: saved for later";
    // While a clock runs, the moving time says so: the line stays empty.
    if (st == RUN_RUNNING) return "";
    if (st == RUN_PAUSED) return "paused";
    if (st == RUN_DONE) return "time\xE2\x80\x99s up";
    return pomo ? "tap to start" : "tap to track";
}

// The project: filled with its color, or an outlined "No project".
static void draw_pill(float x, float y, float size, float maxw, uint16_t outline)
{
    const bool has = S.project.id > 0;
    const char *label = has ? S.project.name : "No project";
    float w = gfx_text_width(label, size, 700) + size * 1.9f;
    if (w > maxw) w = maxw;
    const float h = size * 1.9f;
    if (has) gfx_rrect(x - w / 2, y - h / 2, w, h, h / 2, rgb565(S.project.color), 255);
    else gfx_rrect_line(x - w / 2, y - h / 2, w, h, h / 2, 1.4f, outline, 255);
    gfx_text(label, x, y + size * 0.36f, size, 700, has ? rgb565(color_readable_on(S.project.color)) : outline, ALIGN_CENTER, w - size, 255);
}

// The task on the full face: one line. Wider than the line, it rests, then scrolls through in a loop with faded edges.
static void draw_task(const lay_clock_t *C)
{
    const theme_t *T = F.T;
    const float mid = F.L->base / 2;
    const char *task = S.task[0] ? S.task : "Untitled";
    const uint16_t color = S.task[0] ? T->ink : T->sub;
    const float w = gfx_text_width(task, C->task, 500);
    if (w <= C->taskW) {
        gfx_text(task, mid, C->taskY, C->task, 500, color, ALIGN_CENTER, 0, 255);
        return;
    }
    static char last[LINK_TASK_LEN];
    static int64_t since;
    if (strcmp(last, task)) {
        utf8_copy(last, sizeof last, task);
        since = F.now;
    }
    const int64_t pass_us = (int64_t)(w + MARQUEE_GAP) * SEC_US / MARQUEE_PX_PER_S;
    const int64_t t = (F.now - since) % (MARQUEE_REST_US + pass_us);
    const float scroll = t < MARQUEE_REST_US ? 0 : (float)(t - MARQUEE_REST_US) * MARQUEE_PX_PER_S / (float)SEC_US;
    const float x0 = mid - C->taskW / 2, x1 = mid + C->taskW / 2;
    gfx_text_window(task, x0 - scroll, C->taskY, C->task, 500, color, x0, x1, 20, 255);
    gfx_text_window(task, x0 - scroll + w + MARQUEE_GAP, C->taskY, C->task, 500, color, x0, x1, 20, 255);
}

// The voice, mirrored about the middle: the newest level in the centre bar, older ones moving out to both sides, so
// the shape is always symmetric. S.wave holds the last 21 levels, oldest first.
static void draw_wave(float mid, uint16_t color)
{
    const lay_wave_t *W = &F.L->wave;
    for (int i = 0; i < 21; i++) {
        const int age = i < 10 ? 10 - i : i - 10;
        const float h = 3 + (W->maxH - 3) * (float)S.wave[20 - age] / 255.0f;
        gfx_rrect(mid + (float)(i - 10) * W->step - W->width / 2, W->cy - h / 2, W->width, h, W->width / 2, color, 255);
    }
}

void clock_draw(bool pomo)
{
    const lay_clock_t C = clock_layout();
    const layout_t *L = F.L;
    const theme_t *T = F.T;
    const float mid = L->base / 2;
    const int64_t now = F.now;
    const uint16_t rim_col = S.project.id > 0 ? rgb565(S.project.color) : T->ink;
    gfx_clear(T->face);
    if (zoo_first_needing()) rim_glow(rgb565(COLOR_AMBER), 0.55f + 0.45f * fabsf(sinf(F.t * 2)), L->rim.width);
    if (pomo) {
        const int64_t rem = S.p.state == RUN_DONE ? 0 : pomo_remaining(now);
        const float frac = (float)rem / (float)(60 * MIN_US);
        rim_draw_clock_arc(rim_col, frac > 1 ? 1 : frac);
    } else {
        rim_draw_clock_ring(rim_col);
    }
    draw_pill(mid, C.pillY, C.pill, C.taskW * 0.72f, T->sub);
    draw_task(&C);
    char str[12];
    clock_format(pomo, now, str);
    const bool long_time = strlen(str) > 5;
    const float size = long_time ? C.timeLong : C.time;
    // The warm-up colors the time, the status and the mic, also before the Pomodoro starts.
    const int64_t ms = pomo ? pomo_remaining(now) : 3600 * SEC_US;
    const uint16_t live = pomo ? clock_warm565(ms) : T->ink;
    const bool warmed = pomo && ms < 10 * MIN_US;
    const bool paused = (pomo ? S.p.state : S.t.state) == RUN_PAUSED;
    const float f = pomo ? clock_pulse(now) : 0;
    gfx_time(str, mid, C.timeY - (long_time ? (C.time - C.timeLong) * 0.36f : 0), size, live, 1 + 0.07f * f, paused ? 140 : 255, f);
    bool amber;
    const char *status = clock_status_text(pomo, now, &amber);
    if (S.voice == VOICE_LISTENING && !ux_hinting(now)) draw_wave(mid, warmed ? live : T->sub);
    gfx_text(status, mid, C.statusY, C.status, 500, amber ? T->amber : warmed ? live : T->sub, ALIGN_CENTER, C.taskW, 255);
    if (mic_shown()) {
        const float lw = S.voice == VOICE_LISTENING ? 1.5f + 2.5f * S.mic_level : 1.5f;
        gfx_ring(mid, C.micY, C.micR, lw, live, 255);
        icon_mic(mid, C.micY, C.micR * 0.8f, live);
    }
    // Holding resets it, so a press shows how far along that is. At rest there's nothing to reset.
    const float hold = input_hold_progress(now);
    if (hold > 0 && ((pomo ? S.p.state : S.t.state) != RUN_IDLE || !clock_task_project_cleared())) rim_draw_hold(T->ink, hold);
}

// The clock's section: only the top arc of a round screen, or the top third of a square one. It has no gray: only the
// screen's black or white, its ink and the project's color.
void clock_draw_arch(const ux_mode_t *run)
{
    const lay_arch_t *A = &F.L->arch;
    const theme_t *T = F.T;
    const float base = F.L->base, mid = base / 2;
    const bool pomo = run == &MODE_POMODORO;
    const uint16_t col = S.project.id > 0 ? rgb565(S.project.color) : T->ink;
    gfx_rect(0, 0, base, A->bottom, T->face, 255);
    gfx_rect(0, A->bottom - 1, base, 1.5f, col, 255);
    gfx_clip_rows(0, A->bottom);
    if (pomo) rim_draw_arch_arc(col, pomo_fraction(F.now));
    else rim_draw_arch_ring(col);
    gfx_clip_reset();
    draw_pill(mid, A->pillY, A->pill, A->pillW, T->ink);
    char str[12];
    clock_format(pomo, F.now, str);
    const bool long_time = strlen(str) > 5;
    const int64_t ms = pomo ? pomo_remaining(F.now) : 3600 * SEC_US;
    const float f = pomo ? clock_pulse(F.now) : 0;
    gfx_time(str, mid, A->timeY, long_time ? A->timeLong : A->time, pomo ? clock_warm565(ms) : T->ink, 1 + 0.05f * f, 255, f);
    gfx_text(S.task[0] ? S.task : "Untitled", mid, A->taskY, A->task, 500, T->ink, ALIGN_CENTER, A->taskW, 255);
}

void clock_tick(int64_t now)
{
    timer_tick(now);
    pomo_tick(now);
    clocksync_tick(now);
}
