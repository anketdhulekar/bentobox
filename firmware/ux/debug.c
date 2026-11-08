// The debug interface, for the test harness: docs/DEBUG.md. Text over hal_debug_*, compiled in under
// BENTO_DEBUG_INTERFACE. It never shows a real pairing code, a lock code or its hash, and logs no transcript.
#ifdef BENTO_DEBUG_INTERFACE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../link/json_out.h"
#include "app.h"

#define DEBUG_AGENTS_SHOWN 32

#define LINE_CAP (17 * 1024)
#define STATE_CAP (24 * 1024)

__attribute__((weak)) void hal_restart(void) { abort(); }

__attribute__((weak)) void hal_memory(size_t *free_internal, size_t *min_free_internal, size_t *free_large)
{
    *free_internal = 0;
    *min_free_internal = 0;
    *free_large = 0;
}

__attribute__((weak)) uint32_t hal_fpu_control(void) { return 0; }

// ---- The FPU's rounding, step by step through a frame ----------------------------------------------------------------
// The change check once hashed raw floats, and on a board their last bits changed from frame to frame. This finds the
// step after which the FPU's control register differs, if it ever does.
static struct {
    uint32_t last, from, to;
    const char *step;
    int64_t changes;
} FPU;

void debug_fpu(const char *step)
{
    const uint32_t now = hal_fpu_control();
    if (now != FPU.last) {
        FPU.changes++;
        FPU.from = FPU.last;
        FPU.to = now;
        FPU.step = step;
        FPU.last = now;
    }
}

static struct {
    char *line;
    int len;
    bool in_line;
    char *state;
    bool telemetry, verify, usb;
    int verify_fails;
    int64_t tele_at;
    int64_t frames, frame_us, frame_max, record_us, paint_us, present_us, bands, painted, drawn_max;
    int64_t total_frames, total_painted;
    uint16_t *scratch;
} D;

static void emit(const char *prefix, const char *text, size_t len)
{
    const size_t plen = strlen(prefix);
    char head[32];
    if (plen + len + 2 < sizeof head) {
        memcpy(head, prefix, plen);
        memcpy(head + plen, text, len);
        head[plen + len] = '\n';
        hal_debug_write(head, plen + len + 1);
        return;
    }
    // In one write, so nothing else can split the line.
    char *buf = D.state;
    if (text >= D.state && text < D.state + STATE_CAP) {
        static char *second;
        if (!second) second = hal_alloc_large(STATE_CAP + 64);
        if (!second) return;
        buf = second;
    }
    if (plen + len + 2 > STATE_CAP) return;
    memcpy(buf, prefix, plen);
    memcpy(buf + plen, text, len);
    buf[plen + len] = '\n';
    hal_debug_write(buf, plen + len + 1);
}

static void reply(bool ok, const char *cmd, const char *arg)
{
    char line[200];
    const int n = snprintf(line, sizeof line, "CMD %s %s %.120s\n", ok ? "ok" : "error", cmd, arg);
    hal_debug_write(line, (size_t)(n < (int)sizeof line ? n : (int)sizeof line - 1));
}

// ---- STATE --------------------------------------------------------------------------------------------------------

static const char *run_name(run_t r)
{
    static const char *const N[4] = {"idle", "running", "paused", "done"};
    return N[r];
}

static jw_t *s_rows_w;
static void emit_row(const char *label, const char *value)
{
    jw_begin(s_rows_w, NULL);
    jw_str(s_rows_w, "label", label);
    jw_str(s_rows_w, "value", value);
    jw_end(s_rows_w);
}

static void state_dump(int64_t now)
{
    static const char *const AG[5] = {"working", "needs", "done", "idle", "failed"};
    static const char *const VOICES[5] = {"idle", "listening", "cleaning", "sending", "waiting"};
    static const char *const SYNCS[3] = {"idle", "syncing", "failed"};
    static const char *const TOGGLS[5] = {"unknown", "ok", "no_key", "offline", "backoff"};
    jw_t w;
    jw_init(&w, D.state, STATE_CAP);
    jw_begin(&w, NULL);
    jw_i64(&w, "uptime_ms", now / MS_US);
    jw_str(&w, "board", BOARD.name);
    jw_str(&w, "shape", BOARD.shape == SHAPE_ROUND ? "round" : "square");
    jw_i64(&w, "mount", BOARD.mount_deg);
    jw_i64(&w, "base", (int64_t)F.L->base);
    jw_str(&w, "mode", S.mode->key);
    jw_str(&w, "screen", S.lock.open && S.lock.pad ? "keypad" : S.menu.open ? "menu" : S.list.open ? "list" : S.card ? "card" : S.mode->key);
    jw_str(&w, "theme", S.theme ? "inverted" : "mono");
    char t[12];
    const bool pomo = S.mode == &MODE_POMODORO;
    clock_format(pomo, now, t);
    jw_str(&w, "time", t);
    bool amber = false;
    const char *status = S.mode->running ? clock_status_text(pomo, now, &amber) : "";
    jw_str(&w, "status", status);
    jw_bool(&w, "status_amber", amber);
    jw_str(&w, "hint", ux_hinting(now) ? S.hint : "");
    jw_str(&w, "toast", now < S.toast_until ? S.toast : "");
    jw_str(&w, "task", S.task);
    jw_i64(&w, "task_id", S.task_id);
    jw_begin(&w, "project");
    jw_i64(&w, "id", S.project.id);
    jw_str(&w, "name", S.project.id > 0 ? S.project.name : "");
    char color[8];
    snprintf(color, sizeof color, "#%06lX", (unsigned long)(S.project.color & 0xFFFFFF));
    jw_str(&w, "color", color);
    jw_end(&w);
    jw_begin(&w, "timer");
    jw_str(&w, "state", run_name(S.t.state));
    jw_i64(&w, "elapsed_s", timer_elapsed(now) / SEC_US);
    jw_i64(&w, "rid", S.t.rid);
    jw_i64(&w, "of", S.t.of);
    jw_end(&w);
    jw_begin(&w, "pomo");
    jw_str(&w, "state", run_name(S.p.state));
    jw_i64(&w, "remaining_s", (pomo_remaining(now) + SEC_US - 1) / SEC_US);
    jw_i64(&w, "set_min", S.p.set_min);
    jw_i64(&w, "total_s", S.p.total_us / SEC_US);
    jw_float(&w, "fraction", pomo_fraction(now));
    jw_i64(&w, "rid", S.p.rid);
    jw_i64(&w, "of", S.p.of);
    jw_end(&w);
    jw_begin(&w, "sync");
    jw_bool(&w, "pending", S.cs.pending);
    jw_bool(&w, "mine", S.cs.mine);
    jw_i64(&w, "rev", S.cs.rev);
    jw_i64(&w, "seq", S.cs.sent_seq);
    jw_end(&w);
    jw_bool(&w, "alarm", S.alarm_until != 0);
    jw_bool(&w, "pulsing", clock_pulse(now) > 0 || S.p.state == RUN_DONE || (S.p.state == RUN_RUNNING && pomo_remaining(now) <= MIN_US));
    const ux_mode_t *run = clock_running();
    jw_str(&w, "running", run ? run->key : "");
    // The time of day as the Clock and the menu show it, from the same functions.
    jw_begin(&w, "daytime");
    daytime_t dt;
    if (daytime_get(now, &dt)) {
        static const struct { const char *key; daytime_text_t what; } PARTS[] = {
            {"time", DT_TIME}, {"ampm", DT_AMPM}, {"date", DT_DATE}, {"day", DT_DAY}, {"short", DT_SHORT_DATE}};
        for (size_t i = 0; i < sizeof PARTS / sizeof PARTS[0]; i++) {
            char text[48];
            daytime_text(&dt, PARTS[i].what, text, sizeof text);
            jw_str(&w, PARTS[i].key, text);
        }
    }
    jw_end(&w);
    jw_begin(&w, "split");
    jw_str(&w, "show", S.split == SPLIT_CLOCK ? "clock" : "agents");
    jw_bool(&w, "auto", S.auto_switch);
    jw_i64(&w, "after_s", S.split_after_s);
    jw_end(&w);
    jw_bool(&w, "syncing", S.syncing);
    jw_begin(&w, "menu");
    jw_bool(&w, "open", S.menu.open);
    jw_i64(&w, "options", MODE_COUNT);
    jw_end(&w);
    jw_begin(&w, "settings");
    jw_str(&w, "page", settings_page_name());
    jw_float(&w, "scroll", S.set.scroll);
    jw_i64(&w, "key", S.set.key);
    jw_str(&w, "from", S.set.from ? S.set.from->key : "");
    if (S.mode == &MODE_SETTINGS) {
        jw_array(&w, "rows");
        s_rows_w = &w;
        settings_debug_rows(emit_row);
        jw_array_end(&w);
    }
    jw_end(&w);
    jw_i64(&w, "bright", S.bright);
    jw_bool(&w, "autoscroll", S.autoscroll);
    jw_i64(&w, "vol", S.vol);
    jw_bool(&w, "autorotate", S.autorotate);
    jw_begin(&w, "list");
    jw_bool(&w, "open", S.list.open);
    jw_str(&w, "kind", S.list.kind == LIST_PROJECTS ? "projects" : "tasks");
    jw_i64(&w, "idx", S.list.idx);
    jw_float(&w, "scroll", S.list.scroll);
    jw_str(&w, "sync", SYNCS[S.list.sync]);
    jw_i64(&w, "projects", S.n_projects);
    jw_i64(&w, "tasks", S.n_tasks);
    jw_end(&w);
    // There can be hundreds: the first few, and what's chosen. n_agents says how many there are.
    jw_array(&w, "agents");
    for (int i = 0; i < S.n_agents && i < DEBUG_AGENTS_SHOWN; i++) {
        jw_begin(&w, NULL);
        jw_str(&w, "id", S.agents[i].a.id);
        jw_str(&w, "name", S.agents[i].a.name);
        jw_str(&w, "st", AG[S.agents[i].a.st]);
        jw_end(&w);
    }
    jw_array_end(&w);
    jw_i64(&w, "n_agents", S.n_agents);
    jw_i64(&w, "n_needing", S.n_needing);
    jw_str(&w, "sel_name", S.n_agents ? S.agents[S.sel].a.name : "");
    jw_float(&w, "fling", S.fling_v);
    jw_i64(&w, "sel", S.sel);
    jw_bool(&w, "card", S.card);
    fx_debug_t fx;
    fx_debug_read(&fx);
    jw_begin(&w, "fx");
    jw_i64(&w, "particles", fx.particles);
    jw_i64(&w, "peak", fx.peak);
    jw_i64(&w, "cap", fx.cap);
    jw_i64(&w, "refused", fx.refused);
    jw_begin(&w, "kinds");
    for (int i = 0; i < FX_KINDS; i++) jw_i64(&w, fx.kinds[i], fx.counts[i]);
    jw_end(&w);
    jw_str(&w, "bubble", fx.bubble);
    jw_str(&w, "last", fx.last);
    jw_bool(&w, "leaving", fx.leaving);
    jw_str(&w, "leaving_name", fx.leaving_name);
    jw_bool(&w, "poofed", fx.poofed);
    jw_bool(&w, "dropping", fx.dropping);
    jw_bool(&w, "hopping", fx.hopping);
    jw_bool(&w, "shaking", fx.shaking);
    jw_float(&w, "squash", fx.squash);
    jw_end(&w);
    jw_begin(&w, "rotation");
    jw_i64(&w, "orient", S.orient);
    jw_bool(&w, "turning", S.turning);
    jw_float(&w, "tilt", S.tilt);
    jw_bool(&w, "flat", S.flat);
    jw_bool(&w, "wanted", S.wanted);
    jw_bool(&w, "face_down", S.face_down);
    jw_end(&w);
    jw_begin(&w, "lock");
    jw_bool(&w, "on", S.lock.open);
    jw_bool(&w, "pad", S.lock.pad);
    jw_bool(&w, "code_set", S.lock.has_code);
    jw_i64(&w, "entry_len", (int64_t)strlen(S.lock.entry));
    jw_i64(&w, "tries", S.lock.tries);
    jw_i64(&w, "strikes", S.lock.strikes);
    jw_i64(&w, "wait_s", now < S.lock.until ? (S.lock.until - now) / SEC_US + 1 : 0);
    bool alert;
    jw_str(&w, "message", lock_message(now, &alert));
    jw_end(&w);
    jw_begin(&w, "power");
    jw_i64(&w, "percent", S.battery_override ? S.battery_percent : S.power.percent);
    jw_bool(&w, "charging", S.battery_override ? S.battery_charging : S.power.charging);
    jw_end(&w);
    hal_link_status_t ls;
    hal_link_status(&ls);
    jw_begin(&w, "link");
    jw_str(&w, "uid", link_uid());
    jw_bool(&w, "connected", S.connected);
    jw_bool(&w, "mac_offline", zoo_mac_offline(now));
    jw_bool(&w, "ready", link_ready());
    jw_bool(&w, "usb", D.usb);
    jw_bool(&w, "pairing", ls.pairing); // never the code
    jw_i64(&w, "pair_left", hal_macs_pair_left());
    jw_i64(&w, "outbox", link_outbox_count());
    jw_i64(&w, "next_id", link_outbox_next_id());
    jw_bool(&w, "clock_known", link_clock_known());
    jw_str(&w, "toggl", TOGGLS[S.toggl]);
    jw_bool(&w, "voice_ok", S.voice_ok);
    uint32_t good, bad;
    link_lines_stats(&good, &bad);
    jw_i64(&w, "lines_ok", good);
    jw_i64(&w, "lines_bad", bad);
    jw_end(&w);
    jw_str(&w, "voice", VOICES[S.voice]);
    jw_begin(&w, "has");
    jw_bool(&w, "knob", BOARD.has.knob);
    jw_bool(&w, "haptics", BOARD.has.haptics);
    jw_bool(&w, "mic", BOARD.has.mic);
    jw_bool(&w, "speaker", BOARD.has.speaker);
    jw_bool(&w, "motion", BOARD.has.motion);
    jw_bool(&w, "battery", BOARD.has.battery);
    jw_end(&w);
    jw_begin(&w, "fpu");
    char fcr[24];
    snprintf(fcr, sizeof fcr, "%lx", (unsigned long)FPU.last);
    jw_str(&w, "control", fcr);
    jw_i64(&w, "changes", FPU.changes);
    jw_str(&w, "after", FPU.step ? FPU.step : "");
    snprintf(fcr, sizeof fcr, "%lx>%lx", (unsigned long)FPU.from, (unsigned long)FPU.to);
    jw_str(&w, "last", fcr);
    jw_end(&w);
    jw_begin(&w, "render");
    jw_i64(&w, "frames", D.total_frames);
    jw_i64(&w, "painted", D.total_painted);
    jw_i64(&w, "verify_fails", D.verify_fails);
    jw_end(&w);
    jw_end(&w);
    if (!jw_finish(&w)) {
        hal_debug_write("STATE {\"error\":\"too long\"}\n", 27);
        return;
    }
    w.buf[--w.len] = 0;
    emit("STATE ", D.state, w.len);
}

static void lists_dump(void)
{
    jw_t w;
    jw_init(&w, D.state, STATE_CAP);
    jw_array(&w, NULL);
    for (int i = 0; i < S.n_projects; i++) {
        jw_begin(&w, NULL);
        jw_i64(&w, "id", S.projects[i].id);
        jw_str(&w, "name", S.projects[i].name);
        jw_end(&w);
    }
    jw_array_end(&w);
    if (jw_finish(&w)) emit("PROJECTS ", D.state, w.len - 1);
    jw_init(&w, D.state, STATE_CAP);
    jw_array(&w, NULL);
    for (int i = 0; i < S.n_tasks; i++) {
        jw_begin(&w, NULL);
        jw_str(&w, "desc", S.tasks[i].desc);
        jw_i64(&w, "task_id", S.tasks[i].task_id);
        jw_i64(&w, "project", S.tasks[i].project.id);
        jw_end(&w);
    }
    jw_array_end(&w);
    if (jw_finish(&w)) emit("TASKS ", D.state, w.len - 1);
}

// ---- CAPTURE ------------------------------------------------------------------------------------------------------

// The screen as base64 RGB565, 768 bytes a line. A real pairing code stays off the wire: while pairing, it's drawn again
// without one.
static void capture_send(void)
{
    const int W = BOARD.width, H = BOARD.height;
    const size_t total = (size_t)W * (size_t)H * 2;
    // The screen as a person holding the board sees it. Upright is the frame itself, whichever way the panel is mounted.
    const uint8_t *src = (const uint8_t *)(S.orient == BOARD.mount_deg ? gfx_framebuffer() : screen_shown());
    hal_link_status_t ls;
    hal_link_status(&ls);
    if (ls.pairing) {
        if (!D.scratch) D.scratch = hal_alloc_large(total);
        if (!D.scratch) return;
        ux_redraw_for_capture(D.scratch);
        src = (const uint8_t *)D.scratch;
    }
    char head[40];
    const int n = snprintf(head, sizeof head, "CAPTURE %d %d\n", W, H);
    hal_debug_write(head, (size_t)n);
    static char line[1100];
    int lines = 0;
    for (size_t off = 0; off < total; off += 768) {
        const size_t count = total - off < 768 ? total - off : 768;
        size_t len = b64_encode(src + off, count, line);
        line[len++] = '\n';
        hal_debug_write(line, len);
        if (++lines % 16 == 0) hal_sleep_ms(1); // let others run
    }
    hal_debug_write("CAPTURE END\n", 12);
}

// ---- TELEMETRY ----------------------------------------------------------------------------------------------------

static void telemetry(int64_t now)
{
    if (!D.telemetry || now - D.tele_at < 5 * SEC_US) return;
    D.tele_at = now;
    size_t internal = 0, min_internal = 0, large = 0;
    hal_memory(&internal, &min_internal, &large);
    const float frames = D.frames ? (float)D.frames : 1, painted = D.painted ? (float)D.painted : 1;
    char buf[400];
    jw_t w;
    jw_init(&w, buf, sizeof buf);
    jw_begin(&w, NULL);
    jw_i64(&w, "uptime_s", now / SEC_US);
    jw_i64(&w, "frames", D.frames);
    jw_i64(&w, "painted", D.painted);
    jw_float(&w, "frame_ms_avg", (float)D.frame_us / frames / 1000.0f);
    jw_float(&w, "frame_ms_max", (float)D.frame_max / 1000.0f);
    jw_float(&w, "record_ms_avg", (float)D.record_us / frames / 1000.0f);
    jw_float(&w, "paint_ms_avg", (float)D.paint_us / painted / 1000.0f);
    jw_float(&w, "present_ms_avg", (float)D.present_us / painted / 1000.0f);
    jw_float(&w, "drawn_ms_max", (float)D.drawn_max / 1000.0f);
    jw_float(&w, "bands_avg", (float)D.bands / painted);
    jw_i64(&w, "free_internal_kb", (int64_t)(internal / 1024));
    jw_i64(&w, "min_free_internal_kb", (int64_t)(min_internal / 1024));
    jw_i64(&w, "free_large_kb", (int64_t)(large / 1024));
    jw_end(&w);
    if (jw_finish(&w)) emit("TELEMETRY ", buf, w.len - 1);
    D.frames = D.frame_us = D.frame_max = D.record_us = D.paint_us = D.present_us = D.bands = D.painted = D.drawn_max = 0;
}

// ---- Per-action timing, for tools/perf_test.py --------------------------------------------------------------------
// "#perf start" arms it, and the next input or Mac message starts the clock: a tap, a swipe, a hold, a knob step, a button
// or a LINK line. From then on every frame counts, until "#perf stop", and "#perf peek" reports without stopping. Times
// run from that input to the end of a frame, so they include drawing it and sending it to the panel.
static struct {
    bool armed, running;
    int64_t t0, first_us, settle_us, last_start;
    int64_t frames, painted, bands;
    int64_t frame_us, frame_max, logic_us, logic_max, record_us, paint_us, present_us, gap_max;
} P;

static void perf_input(void)
{
    if (P.armed && !P.running) {
        P.running = true;
        P.t0 = hal_now_us();
    }
}

static void perf_frame(int64_t record_us, int64_t paint_us, int64_t present_us, int bands, int64_t frame_us)
{
    if (!P.running) return;
    const int64_t end = hal_now_us(), start = end - frame_us;
    if (P.last_start && start - P.last_start > P.gap_max) P.gap_max = start - P.last_start; // a stall shows here
    P.last_start = start;
    P.frames++;
    P.frame_us += frame_us;
    if (frame_us > P.frame_max) P.frame_max = frame_us;
    const int64_t logic = frame_us - record_us - paint_us - present_us; // input, the link, the clocks and the rest
    P.logic_us += logic;
    if (logic > P.logic_max) P.logic_max = logic;
    P.record_us += record_us;
    if (!bands) return;
    P.painted++;
    P.bands += bands;
    P.paint_us += paint_us;
    P.present_us += present_us;
    if (!P.first_us) P.first_us = end - P.t0;
    P.settle_us = end - P.t0;
}

static void perf_report(void)
{
    const float frames = P.frames ? (float)P.frames : 1, painted = P.painted ? (float)P.painted : 1;
    const int64_t quiet_from = P.t0 + P.settle_us; // the last frame that painted, or the input itself
    char buf[512];
    jw_t w;
    jw_init(&w, buf, sizeof buf);
    jw_begin(&w, NULL);
    jw_bool(&w, "started", P.running);
    jw_float(&w, "first_ms", P.first_us ? (float)P.first_us / 1000.0f : -1);
    jw_float(&w, "settle_ms", (float)P.settle_us / 1000.0f);
    jw_float(&w, "quiet_ms", P.running ? (float)(hal_now_us() - quiet_from) / 1000.0f : 0);
    jw_i64(&w, "frames", P.frames);
    jw_i64(&w, "painted", P.painted);
    jw_i64(&w, "bands", P.bands);
    jw_float(&w, "work_ms", (float)P.frame_us / 1000.0f);
    jw_float(&w, "frame_ms_avg", (float)P.frame_us / frames / 1000.0f);
    jw_float(&w, "frame_ms_max", (float)P.frame_max / 1000.0f);
    jw_float(&w, "logic_ms_avg", (float)P.logic_us / frames / 1000.0f);
    jw_float(&w, "logic_ms_max", (float)P.logic_max / 1000.0f);
    jw_float(&w, "record_ms_avg", (float)P.record_us / frames / 1000.0f);
    jw_float(&w, "paint_ms_avg", (float)P.paint_us / painted / 1000.0f);
    jw_float(&w, "present_ms_avg", (float)P.present_us / painted / 1000.0f);
    jw_float(&w, "gap_ms_max", (float)P.gap_max / 1000.0f);
    jw_end(&w);
    if (jw_finish(&w)) emit("PERF ", buf, w.len - 1);
}

// ---- Why a frame repainted ----------------------------------------------------------------------------------------
// "#why N": for the next N frames that repaint, a WHY line with the marks that differ from the frame before.
static int s_why_left;

void debug_why(uint64_t dirty)
{
    static bool primed; // the first frame after "#why" has nothing to compare with
    if (s_why_left <= 0) return;
    if (!primed) {
        primed = true;
        return;
    }
    if (!dirty) return;
    static gfx_mark_t marks[8], was[8];
    int n, prev_n;
    uint32_t ref;
    const int k = gfx_why_diff(marks, was, 8, &n, &prev_n, &ref);
    static char buf[6000];
    jw_t w;
    jw_init(&w, buf, sizeof buf);
    jw_begin(&w, NULL);
    char hex[24];
    snprintf(hex, sizeof hex, "%llx", (unsigned long long)dirty);
    jw_str(&w, "dirty", hex);
    jw_i64(&w, "marks", n);
    jw_i64(&w, "marks_before", prev_n);
    snprintf(hex, sizeof hex, "0x%08lx", (unsigned long)ref);
    jw_str(&w, "ref", hex);
    jw_array(&w, "changed");
    for (int i = 0; i < k; i++) {
        jw_begin(&w, NULL);
        snprintf(hex, sizeof hex, "0x%08lx", (unsigned long)marks[i].at);
        jw_str(&w, "at", hex);
        jw_i64(&w, "y0", marks[i].y0);
        jw_i64(&w, "y1", marks[i].y1);
        if (marks[i].text[0]) jw_str(&w, "text", marks[i].text);
        char words[2][18 * 9 + 1];
        for (int f = 0; f < 2; f++) {
            const uint32_t *v = f ? was[i].words : marks[i].words;
            int len = 0;
            for (int j = 0; j < 18; j++) len += snprintf(words[f] + len, sizeof words[f] - (size_t)len, j ? " %08lx" : "%08lx", (unsigned long)v[j]);
        }
        jw_str(&w, "words", words[0]);
        jw_str(&w, "before", words[1]);
        jw_end(&w);
    }
    jw_array_end(&w);
    jw_end(&w);
    if (jw_finish(&w)) emit("WHY ", buf, w.len - 1);
    if (--s_why_left == 0) {
        gfx_why(false);
        primed = false;
    }
}

void debug_frame_stats(int64_t record_us, int64_t paint_us, int64_t present_us, int bands, int64_t frame_us)
{
    perf_frame(record_us, paint_us, present_us, bands, frame_us);
    D.frames++;
    D.total_frames++;
    D.frame_us += frame_us;
    if (frame_us > D.frame_max) D.frame_max = frame_us;
    D.record_us += record_us;
    if (bands) {
        D.painted++;
        D.total_painted++;
        D.paint_us += paint_us;
        D.present_us += present_us;
        D.bands += bands;
        if (paint_us + present_us > D.drawn_max) D.drawn_max = paint_us + present_us;
    }
}

bool debug_verify_on(void) { return D.verify; }

void debug_verify_result(int stale_bands)
{
    if (!stale_bands) return;
    D.verify_fails++;
    char line[64];
    const int n = snprintf(line, sizeof line, "LOG verify_fail stale_bands=%d\n", stale_bands);
    hal_debug_write(line, (size_t)n);
}

// ---- Commands -------------------------------------------------------------------------------------------------------

static bool debug_link_tx(const char *line, size_t len)
{
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) len--;
    emit("LINK ", line, len);
    return true;
}

// Touches as the panel would report them, so that gestures and rotation go the real way.
static void raw_touch(hw_kind_t kind, int x, int y, int64_t now)
{
    hw_event_t e = {.kind = kind, .x = x, .y = y};
    input_inject(&e, now);
}

// Layout units, upright, to panel pixels at the current orientation.
static void layout_to_panel(float u, float v, int *px, int *py)
{
    const float k = gfx_scale(), W = (float)gfx_width(), H = (float)gfx_height();
    const float x = u * k - W / 2, y = v * k - H / 2;
    float cx, cy;
    switch (S.orient) {
    case 90: cx = y; cy = -x; break;
    case 180: cx = -x; cy = -y; break;
    case 270: cx = -y; cy = x; break;
    default: cx = x; cy = y; break;
    }
    *px = (int)(cx + W / 2);
    *py = (int)(cy + H / 2);
}

static void swipe_panel(float x0, float y0, float x1, float y1, int64_t now)
{
    int ax, ay, bx, by;
    layout_to_panel(x0, y0, &ax, &ay);
    layout_to_panel(x1, y1, &bx, &by);
    raw_touch(HW_TOUCH_DOWN, ax, ay, now);
    raw_touch(HW_TOUCH_MOVE, (ax + bx) / 2, (ay + by) / 2, now);
    raw_touch(HW_TOUCH_MOVE, bx, by, now);
    raw_touch(HW_TOUCH_UP, bx, by, now);
}

static void swipe_dir(swipe_t dir, int64_t now)
{
    const float m = F.L->base / 2, d = F.L->base / 4;
    switch (dir) {
    case SWIPE_UP: swipe_panel(m, m + d, m, m - d, now); break;
    case SWIPE_DOWN: swipe_panel(m, m - d, m, m + d, now); break;
    case SWIPE_LEFT: swipe_panel(m + d, m, m - d, m, now); break;
    default: swipe_panel(m - d, m, m + d, m, now); break;
    }
}

static bool parse_on(const char *arg) { return strcmp(arg, "off") != 0 && strcmp(arg, "0") != 0; }

static void line_command(char *cmd, int64_t now)
{
    char *arg = strchr(cmd, ' ');
    if (arg) *arg++ = 0;
    else arg = cmd + strlen(cmd);
    bool ok = true;
    static const char *const INPUTS[] = {"tap", "rawtap", "hold", "swipe", "turn", "button"};
    for (size_t i = 0; i < sizeof INPUTS / sizeof INPUTS[0]; i++) {
        if (!strcmp(cmd, INPUTS[i])) perf_input();
    }
    if (!strcmp(cmd, "tap")) {
        float x, y;
        ok = sscanf(arg, "%f %f", &x, &y) == 2;
        if (ok) app_tap(x, y, now);
    } else if (!strcmp(cmd, "rawtap")) { // panel pixels, before any rotation
        int x, y;
        ok = sscanf(arg, "%d %d", &x, &y) == 2;
        if (ok) {
            raw_touch(HW_TOUCH_DOWN, x, y, now);
            raw_touch(HW_TOUCH_UP, x, y, now);
        }
    } else if (!strcmp(cmd, "hold")) {
        float x = F.L->base / 2, y = F.L->base / 2;
        if (*arg) ok = sscanf(arg, "%f %f", &x, &y) == 2;
        if (ok) {
            S.press_v = y;
            app_hold(now);
            S.press_v = -1;
        }
    } else if (!strcmp(cmd, "swipe")) {
        float a, b, c, d;
        if (!strcmp(arg, "up")) swipe_dir(SWIPE_UP, now);
        else if (!strcmp(arg, "down")) swipe_dir(SWIPE_DOWN, now);
        else if (!strcmp(arg, "left")) swipe_dir(SWIPE_LEFT, now);
        else if (!strcmp(arg, "right")) swipe_dir(SWIPE_RIGHT, now);
        else if (sscanf(arg, "%f %f %f %f", &a, &b, &c, &d) == 4) swipe_panel(a, b, c, d, now);
        else ok = false;
    } else if (!strcmp(cmd, "turn")) {
        int n = atoi(arg);
        if (n > 200) n = 200;
        if (n < -200) n = -200;
        for (; n > 0; n--) app_turn(1, now);
        for (; n < 0; n++) app_turn(-1, now);
    } else if (!strcmp(cmd, "button")) {
        if (!strcmp(arg, "menu")) app_button(BUTTON_MENU, now);
        else if (!strcmp(arg, "up")) app_button(BUTTON_UP, now);
        else if (!strcmp(arg, "down")) app_button(BUTTON_DOWN, now);
        else ok = false;
    } else if (!strcmp(cmd, "task")) {
        utf8_copy(S.task, sizeof S.task, arg);
        S.task_id = 0;
        ux_mark_dirty(now);
    } else if (!strcmp(cmd, "task_id")) {
        S.task_id = strtoll(arg, NULL, 10);
        ux_mark_dirty(now);
    } else if (!strcmp(cmd, "project")) { // "#project ID", or "#project ID #RRGGBB NAME"
        char *rest;
        const int64_t id = strtoll(arg, &rest, 10);
        if (id <= 0) {
            clock_set_project(NULL, false, now);
        } else {
            const link_project_t *p = picker_find_project(id);
            link_project_t given;
            memset(&given, 0, sizeof given);
            given.id = id;
            unsigned rgb;
            if (!p && sscanf(rest, " #%6x", &rgb) == 1 && strlen(rest) > 9) {
                given.color = rgb;
                utf8_copy(given.name, sizeof given.name, rest + 9);
                p = &given;
            }
            ok = p != NULL;
            if (ok) clock_set_project(p, false, now);
        }
    } else if (!strcmp(cmd, "set")) {
        const int n = atoi(arg);
        ok = S.p.state == RUN_IDLE && n >= 1 && n <= 60;
        if (ok) {
            S.p.set_min = n;
            pomo_reset();
            ux_mark_dirty(now);
        }
    } else if (!strcmp(cmd, "session")) {
        const long long secs = atoll(arg);
        ok = S.t.state != RUN_RUNNING && secs >= 0;
        if (ok) {
            S.t.session_us = secs * SEC_US;
            S.t.state = secs ? RUN_PAUSED : RUN_IDLE;
        }
    } else if (!strcmp(cmd, "elapsed")) { // the running Timer has been going this long
        const long long secs = atoll(arg);
        ok = S.t.state == RUN_RUNNING && secs >= 0;
        if (ok) {
            S.t.session_us = 0;
            S.t.start_us = now - secs * SEC_US;
        }
    } else if (!strcmp(cmd, "remaining")) { // "#remaining N": paused, unless it's running
        const long long secs = atoll(arg);
        ok = secs >= 1 && secs <= 3600;
        if (ok) {
            if (S.p.state != RUN_RUNNING) S.p.state = RUN_PAUSED;
            pomo_set_remaining(secs * SEC_US, now);
        }
    } else if (!strcmp(cmd, "mode")) {
        const ux_mode_t *m = mode_by_key(arg);
        ok = m && m->draw;
        if (ok) {
            S.menu.open = false;
            S.list.open = false;
            S.card = false;
            if (m->enter && S.mode != m) m->enter(now);
            S.mode = m;
            S.input_at = now; // as a choice in the menu is: Auto switch waits from here
            ux_mark_dirty(now);
        }
    } else if (!strcmp(cmd, "theme")) {
        ok = !strcmp(arg, "mono") || !strcmp(arg, "inverted");
        if (ok) {
            S.theme = !strcmp(arg, "inverted");
            ux_mark_dirty(now);
        }
    } else if (!strcmp(cmd, "bright")) {
        const int n = atoi(arg);
        ok = n >= 0 && n < HAL_BRIGHT_LEVELS;
        if (ok) {
            S.bright = n;
            hal_screen_brightness(n);
            ux_mark_dirty(now);
        }
    } else if (!strcmp(cmd, "autoscroll")) {
        S.autoscroll = parse_on(arg);
        ux_mark_dirty(now);
    } else if (!strcmp(cmd, "autorotate")) {
        S.autorotate = parse_on(arg);
        ux_mark_dirty(now);
    } else if (!strcmp(cmd, "vol")) {
        const int n = atoi(arg);
        ok = n >= 0 && n <= 10;
        if (ok) {
            S.vol = n;
            hal_volume(n);
            ux_mark_dirty(now);
        }
    } else if (!strcmp(cmd, "lock")) {
        ok = S.lock.has_code;
        if (ok) lock_engage();
    } else if (!strcmp(cmd, "lock_wait_end")) { // a test: the wait after wrong codes is over, as if its time had passed
        S.lock.until = 0;
    } else if (!strcmp(cmd, "unlock")) {
        lock_unlock();
    } else if (!strcmp(cmd, "tilt")) {
        S.has_tilt_override = true;
        S.tilt = (float)atof(arg);
    } else if (!strcmp(cmd, "tilt_off")) {
        S.has_tilt_override = false;
    } else if (!strcmp(cmd, "flat")) {
        S.flat_override = parse_on(arg);
        S.flat = S.flat_override;
    } else if (!strcmp(cmd, "battery")) { // "#battery N [charging]", or "#battery off"
        if (!strcmp(arg, "off")) {
            S.battery_override = false;
        } else {
            S.battery_override = true;
            S.battery_percent = atoi(arg);
            S.battery_charging = strstr(arg, "charging") != NULL;
        }
    } else if (!strcmp(cmd, "mac")) {
        const bool on = parse_on(arg);
        link_force_offline(!on);
        S.offline_at_once = !on; // say "Mac offline" at once, rather than after 30 s
    } else if (!strcmp(cmd, "link")) {
        if (!strcmp(arg, "usb")) {
            D.usb = true;
            link_override_transport(debug_link_tx);
        } else if (!strcmp(arg, "ble")) {
            D.usb = false;
            link_override_transport(NULL);
        } else {
            ok = false;
        }
    } else if (!strcmp(cmd, "telemetry")) {
        D.telemetry = parse_on(arg);
    } else if (!strcmp(cmd, "why")) { // "#why N": the next N repainting frames each say what changed
        s_why_left = atoi(arg);
        if (s_why_left > 50) s_why_left = 50;
        gfx_why(s_why_left > 0);
    } else if (!strcmp(cmd, "perf")) {
        if (!strcmp(arg, "start")) {
            memset(&P, 0, sizeof P);
            P.armed = true;
        } else if (!strcmp(arg, "peek")) {
            perf_report();
        } else if (!strcmp(arg, "stop")) {
            perf_report();
            memset(&P, 0, sizeof P);
        } else {
            ok = false;
        }
    } else if (!strcmp(cmd, "verify")) {
        D.verify = parse_on(arg);
    } else if (!strcmp(cmd, "list")) {
        lists_dump();
    } else if (!strcmp(cmd, "anim")) { // freeze the animations at this time, in seconds; "#anim off" lets them run
        if (!strcmp(arg, "off")) {
            S.has_anim_override = false;
        } else {
            S.has_anim_override = true;
            S.anim_override = (float)atof(arg);
        }
    } else if (!strcmp(cmd, "pulse")) { // hold the pulse at this strength; "#pulse off" lets it beat
        if (!strcmp(arg, "off")) {
            S.has_pulse_override = false;
        } else {
            S.has_pulse_override = true;
            S.pulse_override = (float)atof(arg);
        }
    } else if (!strcmp(cmd, "burnin")) { // hold the burn-in shift at "X Y"; "#burnin off" lets it drift
        if (!strcmp(arg, "off")) {
            S.has_burn_override = false;
        } else {
            ok = sscanf(arg, "%d %d", &S.burn_x, &S.burn_y) == 2;
            S.has_burn_override = ok;
        }
    } else if (!strcmp(cmd, "fx")) { // an effect on the agent on screen; "step", "seed" and "clear" are for tests
        ok = fx_debug_command(arg, now);
    } else if (!strcmp(cmd, "quiet")) { // no hint or toast showing: a test's next look is at what's underneath
        S.hint_until = 0;
        S.toast_until = 0;
    } else if (!strcmp(cmd, "reboot")) {
        store_save_settings();
        reply(true, cmd, "");
        hal_restart();
        return;
    } else {
        ok = false;
    }
    reply(ok, cmd, arg);
}

static void single_key(char c, int64_t now)
{
    const float m = F.L->base / 2;
    const layout_t *L = F.L;
    if (strchr("sr+-eomtup<>b", c)) perf_input();
    switch (c) {
    case 's': app_tap(m, BOARD.has.mic ? L->clock.statusY : m, now); break; // the screen, away from the mic and the lists
    case 'r': S.press_v = -1; app_hold(now); break;
    case '+': app_turn(1, now); break;
    case '-': app_turn(-1, now); break;
    case 'e': app_tap(m, L->clock.taskY - L->clock.task * 0.3f, now); break;
    case 'o': app_tap(m, L->clock.pillY, now); break;
    case 'm': app_tap(m, L->clock.micY, now); break;
    case 't': app_tap(m, m, now); break;
    case 'u': swipe_dir(SWIPE_UP, now); break;
    case 'p': swipe_dir(SWIPE_DOWN, now); break;
    case '<': swipe_dir(SWIPE_LEFT, now); break;
    case '>': swipe_dir(SWIPE_RIGHT, now); break;
    case 'b': app_button(BUTTON_MENU, now); break;
    case 'd': state_dump(now); break;
    case 'c': capture_send(); break;
    default: break;
    }
}

void debug_init(void)
{
    memset(&D, 0, sizeof D);
    D.line = hal_alloc_large(LINE_CAP);
    D.state = hal_alloc_large(STATE_CAP + 64);
}

void debug_poll(int64_t now)
{
    if (!D.line || !D.state) return;
    static char chunk[2048];
    int budget = 16;
    while (budget-- > 0) {
        const int got = hal_debug_read(chunk, sizeof chunk);
        if (got <= 0) break;
        for (int i = 0; i < got; i++) {
            const char c = chunk[i];
            if (D.in_line) {
                if (c == '\n' || c == '\r') {
                    D.line[D.len] = 0;
                    D.in_line = false;
                    if (D.line[0] == '#') line_command(D.line + 1, now);
                    else if (!strncmp(D.line, "LINK ", 5) && D.usb) {
                        perf_input();
                        link_feed_line(D.line + 5);
                    }
                } else if (D.len < LINE_CAP - 1) {
                    D.line[D.len++] = c;
                }
                continue;
            }
            if (c == '\n' || c == '\r') continue;
            if (c == '#' || c == 'L') {
                D.in_line = true;
                D.len = 0;
                D.line[D.len++] = c;
                continue;
            }
            single_key(c, now);
        }
    }
    telemetry(now);
}

#else
typedef int bento_debug_interface_is_off; // ISO C needs something in every file
#endif // BENTO_DEBUG_INTERFACE
