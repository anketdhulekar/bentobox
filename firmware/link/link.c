#include "link.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../hal/hal.h"
#include "cJSON.h"
#include "json_out.h"
#include "link_internal.h"

#define LINE_MAX_BYTES (16 * 1024)
#define ARENA_BYTES (96 * 1024)   // what a parsed 16 KB line can need; more just fails to parse
#define MAX_DEPTH 12              // the deepest message has 5 levels. cJSON recurses, and a board's stack is small
#define READ_PER_POLL 4096
#define LINES_PER_POLL 8

typedef struct {
    bool active;
    int req, page, count;
} incoming_list_t;

static struct {
    const link_handlers_t *h;
    uint32_t uid_value;
    const char *fw;
    bool (*lock_is_set)(void);
    char *line;
    size_t len;
    bool skip;
    bool up, allowed, forced_off;
    link_tx_fn tx;
    bool clock_known;
    int64_t clock_unix, clock_mono;
    int32_t utc;
    link_agent_t *row;       // one agent, as it's parsed
    int agents_next_page;    // of a whole list; -1 if one went missing
    int next_req;
    uint32_t good, bad;
    char uid[9]; // 8 hex digits, made once and kept: id and rid are only unique within it
    // A list arriving in pages, one per kind, so that the two can arrive interleaved.
    link_project_t *project_rows;
    link_task_t *task_rows;
    incoming_list_t incoming[2];
} L;

// ---- cJSON, in an arena ------------------------------------------------------------------------------------------
// Every allocation for a line comes from one block in large memory and goes away together. So a line can't fragment
// the small internal heap, and a line that needs too much just fails to parse.

static uint8_t *s_arena;
static size_t s_arena_used;

static void *arena_malloc(size_t n)
{
    n = (n + 7) & ~(size_t)7;
    if (!s_arena || n > ARENA_BYTES - s_arena_used) return NULL;
    void *p = s_arena + s_arena_used;
    s_arena_used += n;
    return p;
}

static void arena_free(void *p) { (void)p; }

// ---- Small helpers ---------------------------------------------------------------------------------------------------

void utf8_copy(char *dst, size_t n, const char *src)
{
    if (!n) return;
    size_t len = strlen(src);
    if (len >= n) {
        len = n - 1;
        while (len > 0 && ((uint8_t)src[len] & 0xC0) == 0x80) len--; // don't end inside a character
    }
    memmove(dst, src, len);
    dst[len] = 0;
}

static const char *get_str(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) && v->valuestring ? v->valuestring : "";
}

static bool has_str(const cJSON *obj, const char *key) { return cJSON_IsString(cJSON_GetObjectItemCaseSensitive(obj, key)); }

// A number from outside might be anything, such as 1e999.
static int64_t get_i64(const cJSON *obj, const char *key, int64_t def)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsNumber(v)) return def;
    const double d = v->valuedouble;
    if (!(d >= -9e15 && d <= 9e15)) return def;
    return (int64_t)d;
}

static int32_t clamp_i32(int64_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : v > hi ? hi : (int32_t)v;
}

static int hex_val(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hex_bytes(const char *s, uint8_t *out, int max)
{
    const size_t len = strlen(s);
    if (len == 0 || len % 2 || len / 2 > (size_t)max) return -1;
    for (size_t i = 0; i < len / 2; i++) {
        const int a = hex_val(s[i * 2]), b = hex_val(s[i * 2 + 1]);
        if (a < 0 || b < 0) return -1;
        out[i] = (uint8_t)(a << 4 | b);
    }
    return (int)(len / 2);
}

// A uid as a number. This board's own, or anything that isn't 8 hex digits, is 0.
static uint32_t parse_uid(const char *s)
{
    if (strlen(s) != 8) return 0;
    uint32_t v = 0;
    for (int i = 0; i < 8; i++) {
        const int h = hex_val(s[i]);
        if (h < 0) return 0;
        v = v << 4 | (uint32_t)h;
    }
    return v == L.uid_value ? 0 : v;
}

static uint32_t parse_color(const char *s)
{
    if (s[0] != '#' || strlen(s) != 7) return 0x8E8E94;
    uint32_t v = 0;
    for (int i = 1; i < 7; i++) {
        const int h = hex_val(s[i]);
        if (h < 0) return 0x8E8E94;
        v = v << 4 | (uint32_t)h;
    }
    return v;
}

static void parse_project(const cJSON *obj, link_project_t *p)
{
    memset(p, 0, sizeof *p);
    if (!cJSON_IsObject(obj)) return;
    p->id = get_i64(obj, "id", 0);
    if (p->id < 0) p->id = 0;
    p->color = parse_color(get_str(obj, "color"));
    utf8_copy(p->name, sizeof p->name, get_str(obj, "name"));
}

// ---- Depth check: cJSON recurses for each level, so a line of 16,000 '[' must never reach it --------------------------

static bool depth_ok(const char *s, size_t len)
{
    int depth = 0;
    bool in_str = false, esc = false;
    for (size_t i = 0; i < len; i++) {
        const char c = s[i];
        if (!c) return false; // no NUL belongs in a line, and text after one would escape other checks
        if (in_str) {
            if (esc) esc = false;
            else if (c == '\\') esc = true;
            else if (c == '"') in_str = false;
        } else if (c == '"') {
            in_str = true;
        } else if (c == '{' || c == '[') {
            if (++depth > MAX_DEPTH) return false;
        } else if (c == '}' || c == ']') {
            if (depth > 0) depth--; // a stray closer doesn't buy room for more openers later
        }
    }
    return true;
}

// ---- Messages from the Mac --------------------------------------------------------------------------------------------

static void send_board(void)
{
    char line[256];
    jw_t w;
    jw_init(&w, line, sizeof line);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "board");
    jw_i64(&w, "v", 4);
    if (BOARD.title) jw_str(&w, "title", BOARD.title);
    jw_str(&w, "uid", L.uid);
    jw_str(&w, "name", BOARD.name);
    jw_str(&w, "fw", L.fw);
    jw_i64(&w, "w", BOARD.width);
    jw_i64(&w, "h", BOARD.height);
    jw_array(&w, "has");
    if (BOARD.has.knob) jw_str(&w, NULL, "knob");
    if (BOARD.has.haptics) jw_str(&w, NULL, "haptics");
    if (BOARD.has.mic) jw_str(&w, NULL, "mic");
    if (BOARD.has.speaker) jw_str(&w, NULL, "speaker");
    if (BOARD.has.motion) jw_str(&w, NULL, "motion");
    if (BOARD.has.battery) jw_str(&w, NULL, "battery");
    jw_array_end(&w);
    jw_bool(&w, "lock", L.lock_is_set && L.lock_is_set());
    jw_end(&w);
    if (jw_finish(&w)) link_send_line(line, w.len);
}

static void on_hello(const cJSON *msg)
{
    const char *host = get_str(msg, "host");
    if (!L.tx) {
        hal_link_remember_host(host);
        const char *refuse = hal_link_refuse_reason();
        if (refuse) {
            hal_link_kick(refuse);
            return;
        }
    }
    L.allowed = true;
    send_board();
    outbox_on_connected(); // after the board message, all saved messages again, in order
    if (L.h->connected) L.h->connected(host[0] ? host : "a Mac");
}

static agent_status_t parse_status(const char *s)
{
    if (!strcmp(s, "needs")) return AG_NEEDS;
    if (!strcmp(s, "done")) return AG_DONE;
    if (!strcmp(s, "idle")) return AG_IDLE;
    if (!strcmp(s, "failed")) return AG_FAILED;
    return AG_WORKING;
}

static void on_state(const cJSON *msg, int64_t now_us)
{
    const cJSON *now = cJSON_GetObjectItemCaseSensitive(msg, "now");
    if (cJSON_IsNumber(now) && now->valuedouble > 0 && now->valuedouble < 9e15) {
        L.clock_unix = (int64_t)now->valuedouble;
        L.clock_mono = now_us;
        L.utc = clamp_i32(get_i64(msg, "utc", 0), -14 * 3600, 14 * 3600);
        L.clock_known = true;
    }
    link_state_t st = {0};
    const char *toggl = get_str(msg, "toggl");
    st.toggl = !strcmp(toggl, "ok") ? TOGGL_OK : !strcmp(toggl, "no_key") ? TOGGL_NO_KEY : !strcmp(toggl, "offline") ? TOGGL_OFFLINE : !strcmp(toggl, "backoff") ? TOGGL_BACKOFF : TOGGL_UNKNOWN;
    const cJSON *voice = cJSON_GetObjectItemCaseSensitive(msg, "voice");
    st.has_voice = cJSON_IsBool(voice);
    st.voice = cJSON_IsTrue(voice);
    st.n = -1;
    const cJSON *n = cJSON_GetObjectItemCaseSensitive(msg, "n");
    if (cJSON_IsNumber(n)) st.n = clamp_i32(get_i64(msg, "n", 0), 0, 1000000);
    if (L.h->state) L.h->state(&st);
}

static void parse_agent(const cJSON *it, link_agent_t *a)
{
    memset(a, 0, sizeof *a);
    utf8_copy(a->id, sizeof a->id, get_str(it, "id"));
    utf8_copy(a->name, sizeof a->name, get_str(it, "name"));
    utf8_copy(a->proj, sizeof a->proj, get_str(it, "proj"));
    utf8_copy(a->act, sizeof a->act, get_str(it, "act"));
    utf8_copy(a->note, sizeof a->note, get_str(it, "note"));
    utf8_copy(a->where, sizeof a->where, get_str(it, "where"));
    a->st = parse_status(get_str(it, "st"));
    const char *agent = get_str(it, "agent");
    a->kind = !strcmp(agent, "codex") ? KIND_CODEX : !strcmp(agent, "grok") ? KIND_GROK : KIND_CLAUDE;
    a->background = get_str(it, "kind")[0] == 'b';
    a->rex = get_i64(it, "rex", 0) != 0 || cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(it, "rex"));
    a->age_s = clamp_i32(get_i64(it, "age", 0), 0, 100000000);
    a->up_s = clamp_i32(get_i64(it, "up", 0), 0, 100000000);
    const cJSON *subs = cJSON_GetObjectItemCaseSensitive(it, "subs"), *s;
    if (cJSON_IsArray(subs)) {
        cJSON_ArrayForEach(s, subs)
        {
            if (a->nsubs >= LINK_MAX_SUBS) break;
            if (!cJSON_IsObject(s)) continue;
            link_sub_t *z = &a->subs[a->nsubs];
            utf8_copy(z->id, sizeof z->id, get_str(s, "id"));
            utf8_copy(z->type, sizeof z->type, get_str(s, "type"));
            utf8_copy(z->act, sizeof z->act, get_str(s, "act"));
            a->nsubs++;
        }
    }
}

static void on_agents(const cJSON *msg)
{
    if (!L.row) return;
    const int64_t page = get_i64(msg, "page", -1), pages = get_i64(msg, "pages", -1);
    const bool in_list = page >= 0 && pages > 0 && page < pages;
    if (in_list && page == 0) {
        L.agents_next_page = 0;
        if (L.h->agents_begin) L.h->agents_begin();
    }
    const cJSON *rows = cJSON_GetObjectItemCaseSensitive(msg, "rows"), *it;
    if (cJSON_IsArray(rows)) {
        cJSON_ArrayForEach(it, rows)
        {
            if (!cJSON_IsObject(it) || !get_str(it, "id")[0]) continue;
            parse_agent(it, L.row);
            if (L.h->agent_row) L.h->agent_row(L.row);
        }
    }
    const cJSON *gone = cJSON_GetObjectItemCaseSensitive(msg, "gone");
    if (cJSON_IsArray(gone)) {
        cJSON_ArrayForEach(it, gone)
        {
            if (!cJSON_IsString(it) || !it->valuestring[0]) continue;
            char id[sizeof L.row->id];
            utf8_copy(id, sizeof id, it->valuestring);
            if (L.h->agent_gone) L.h->agent_gone(id);
        }
    }
    bool whole = false;
    if (in_list) {
        // A page that went missing leaves everyone in place: the count in `state` will ask for the list again.
        if (page == L.agents_next_page) {
            L.agents_next_page++;
            whole = page == pages - 1;
        } else {
            L.agents_next_page = -1;
        }
    }
    if (L.h->agents_end) L.h->agents_end(whole);
}

// A list arrives in pages; the cached list is replaced only when the last one has. The first load comes unasked, with
// req 0, whenever the app starts, so it can land in the middle of a fetch's answer: the answer wins, because someone is
// waiting for it and it holds the same rows. A page of some other req never disturbs the list being put together.
static void on_list_page(const cJSON *msg, link_list_t kind)
{
    const int64_t req64 = get_i64(msg, "req", -1), page = get_i64(msg, "page", -1), pages = get_i64(msg, "pages", -1);
    const cJSON *rows = cJSON_GetObjectItemCaseSensitive(msg, "rows");
    if (req64 < 0 || req64 > 1000000000 || page < 0 || pages < 1 || pages > 1000 || page >= pages || !cJSON_IsArray(rows)) return;
    if (!L.project_rows || !L.task_rows) return;
    const int req = (int)req64;
    incoming_list_t *in = &L.incoming[kind];
    if (page == 0) {
        if (in->active && in->req != 0 && req == 0) return; // an unasked load, during an answer
        in->active = true;
        in->req = req;
        in->page = 0;
        in->count = 0;
    } else if (!in->active || in->req != req) {
        return; // not the list being put together
    } else if (in->page != page) {
        in->active = false; // a page is missing
        return;
    }
    const int max = kind == LIST_PROJECTS ? LINK_MAX_PROJECTS : LINK_MAX_TASKS;
    const cJSON *r;
    cJSON_ArrayForEach(r, rows)
    {
        if (in->count >= max) break;
        if (!cJSON_IsObject(r)) continue;
        if (kind == LIST_PROJECTS) {
            link_project_t *p = &L.project_rows[in->count];
            parse_project(r, p);
            if (p->id <= 0 || !p->name[0]) continue;
        } else {
            link_task_t *t = &L.task_rows[in->count];
            memset(t, 0, sizeof *t);
            utf8_copy(t->desc, sizeof t->desc, get_str(r, "desc"));
            t->task_id = get_i64(r, "task_id", 0);
            parse_project(cJSON_GetObjectItemCaseSensitive(r, "project"), &t->project); // null: no project
        }
        in->count++;
    }
    in->page = (int)page + 1;
    if (page == pages - 1) {
        in->active = false;
        if (L.h->list) L.h->list(kind, req, kind == LIST_PROJECTS ? (const void *)L.project_rows : (const void *)L.task_rows, in->count);
    }
}

static void on_sync_result(const cJSON *msg)
{
    link_sync_t s;
    memset(&s, 0, sizeof s);
    s.req = clamp_i32(get_i64(msg, "req", 0), -1000000000, 1000000000);
    const char *r = get_str(msg, "result");
    s.result = !strcmp(r, "adopted") ? SYNC_ADOPTED : !strcmp(r, "stopped") ? SYNC_STOPPED : !strcmp(r, "in_sync") ? SYNC_IN_SYNC : SYNC_FAILED;
    if (s.result == SYNC_ADOPTED) {
        // The app chose this rid. The board's own count moves past it, whether or not the UX still wants this answer,
        // so that a later run can't take the same number.
        const int64_t rid = get_i64(msg, "rid", 0);
        if (rid > 0 && rid < 0xFFFFFFF0LL) {
            s.rid = (uint32_t)rid;
            outbox_note_rid(s.rid);
        }
        utf8_copy(s.desc, sizeof s.desc, get_str(msg, "desc"));
        s.task_id = get_i64(msg, "task_id", 0);
        parse_project(cJSON_GetObjectItemCaseSensitive(msg, "project"), &s.project); // null: no project
        s.start = get_i64(msg, "start", 0);
    }
    if (L.h->sync) L.h->sync(&s);
}

static void on_transcript(const cJSON *msg)
{
    link_transcript_t t = {0};
    t.vid = clamp_i32(get_i64(msg, "vid", -1), -1, 2000000000);
    const char *err = get_str(msg, "error");
    if (has_str(msg, "text") && !err[0]) {
        t.text = get_str(msg, "text");
        t.project = get_i64(msg, "project", 0);
    } else {
        t.no_speech = !strcmp(err, "no_speech");
    }
    if (L.h->transcript) L.h->transcript(&t);
}

// ---- `clock`: the Mac's times are Unix milliseconds; each becomes this board's, as that long before the Mac's `now`.

static int run_state(const char *s, bool done_ok)
{
    if (!strcmp(s, "running")) return 1;
    if (!strcmp(s, "paused")) return 2;
    if (done_ok && !strcmp(s, "done")) return 3;
    return 0;
}

// A Mac's `now` that can't be one: before 2020 or after 2200, in Unix milliseconds. Such a `clock` is dropped.
#define CLOCK_NOW_MIN_MS 1577836800000LL
#define CLOCK_NOW_MAX_MS 7258118400000LL
// The furthest back any time in a `clock` can be: 400 days. Older is taken as that old, so no sum can overflow later.
#define CLOCK_SPAN_MS (400LL * 24 * 3600 * 1000)

// A time from the Mac, as this board's: that long before the Mac's `now`, never later than now, never older than the span.
static int64_t mac_to_local(int64_t mac_ms, int64_t mac_now, int64_t now_us)
{
    int64_t ago = mac_now - mac_ms; // both are bounded (±9e15 and the plausible range), so this can't overflow
    if (ago < 0) ago = 0;
    if (ago > CLOCK_SPAN_MS) ago = CLOCK_SPAN_MS;
    return now_us - ago * 1000;
}

static int64_t span_ms_to_us(int64_t ms) { return (ms < 0 ? 0 : ms > CLOCK_SPAN_MS ? CLOCK_SPAN_MS : ms) * 1000; }

static uint32_t parse_rid(const cJSON *obj)
{
    const int64_t rid = get_i64(obj, "rid", 0);
    return rid > 0 && rid < 0xFFFFFFF0LL ? (uint32_t)rid : 0;
}

static void on_clock(const cJSON *msg, int64_t now_us)
{
    if (!L.h->clock) return;
    const int64_t mac_now = get_i64(msg, "now", 0);
    if (mac_now < CLOCK_NOW_MIN_MS || mac_now > CLOCK_NOW_MAX_MS) return; // not a time a Mac could send
    static link_clock_t c;
    memset(&c, 0, sizeof c);
    c.rev = get_i64(msg, "rev", 0);
    c.from = parse_uid(get_str(msg, "from"));
    c.seq = (uint32_t)clamp_i32(get_i64(msg, "seq", 0), 0, 2147483647);
    const cJSON *fr = cJSON_GetObjectItemCaseSensitive(msg, "for");
    c.has_for = cJSON_IsNumber(fr);
    c.for_seq = (uint32_t)clamp_i32(get_i64(msg, "for", 0), 0, 2147483647);
    c.has_at = cJSON_IsNumber(cJSON_GetObjectItemCaseSensitive(msg, "at"));
    c.at_us = c.has_at ? mac_to_local(get_i64(msg, "at", 0), mac_now, now_us) : 0;
    const cJSON *task = cJSON_GetObjectItemCaseSensitive(msg, "task");
    utf8_copy(c.task, sizeof c.task, get_str(task, "desc"));
    c.task_id = get_i64(task, "task_id", 0);
    if (c.task_id < 0) c.task_id = 0;
    const cJSON *proj = cJSON_GetObjectItemCaseSensitive(task, "project");
    parse_project(proj, &c.project);
    if (cJSON_IsObject(proj) && get_i64(proj, "id", 0) == -1) c.project.id = -1; // never chosen
    const cJSON *t = cJSON_GetObjectItemCaseSensitive(msg, "timer");
    c.timer.st = run_state(get_str(t, "st"), false);
    c.timer.acc_us = span_ms_to_us(get_i64(t, "acc", 0));
    c.timer.start_us = c.timer.st == 1 ? mac_to_local(get_i64(t, "start", mac_now), mac_now, now_us) : 0;
    c.timer.rid = parse_rid(t);
    c.timer.of = parse_uid(get_str(t, "of"));
    const cJSON *p = cJSON_GetObjectItemCaseSensitive(msg, "pomo");
    c.pomo.st = run_state(get_str(p, "st"), true);
    c.pomo.set_min = clamp_i32(get_i64(p, "set", 25), 1, 60);
    c.pomo.total_us = (int64_t)clamp_i32(get_i64(p, "total", 0), 0, 2 * 3600 * 1000) * 1000;
    c.pomo.left_us = (int64_t)clamp_i32(get_i64(p, "left", 0), 0, 2 * 3600 * 1000) * 1000;
    if (c.pomo.left_us > c.pomo.total_us) c.pomo.left_us = c.pomo.total_us;
    c.pomo.rid = parse_rid(p);
    c.pomo.of = parse_uid(get_str(p, "of"));
    if (c.timer.rid) outbox_note_rid_of(c.timer.rid, c.timer.of);
    if (c.pomo.rid) outbox_note_rid_of(c.pomo.rid, c.pomo.of);
    L.h->clock(&c);
}

static void on_lock_code(const cJSON *msg)
{
    if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(msg, "clear"))) {
        if (L.h->lock_code) L.h->lock_code(true, NULL, 0, 0, NULL);
        return;
    }
    uint8_t salt[32], hash[32];
    const int salt_len = hex_bytes(get_str(msg, "salt"), salt, sizeof salt);
    const int64_t iter = get_i64(msg, "iter", 0);
    // A huge iteration count would freeze the board on every try.
    if (salt_len < 1 || iter < 1 || iter > 100000 || hex_bytes(get_str(msg, "hash"), hash, sizeof hash) != 32) return;
    if (L.h->lock_code) L.h->lock_code(false, salt, salt_len, (uint32_t)iter, hash);
}

static void dispatch(const char *line, size_t len, int64_t now_us)
{
    if (!depth_ok(line, len)) {
        L.bad++;
        return;
    }
    s_arena_used = 0;
    cJSON *msg = cJSON_ParseWithLength(line, len);
    if (!msg) {
        L.bad++;
        return;
    }
    L.good++;
    const char *type = cJSON_IsObject(msg) ? get_str(msg, "t") : "";
    if (!strcmp(type, "hello")) on_hello(msg);
    else if (!L.allowed) { /* nothing else until this Mac has said hello */ }
    else if (!strcmp(type, "state")) on_state(msg, now_us);
    else if (!strcmp(type, "agents")) on_agents(msg);
    else if (!strcmp(type, "ack")) {
        const int64_t id = get_i64(msg, "id", -1);
        if (id >= 0 && id <= 0xFFFFFFFFLL) outbox_ack((uint32_t)id);
    } else if (!strcmp(type, "entry_status")) {
        if (!strcmp(get_str(msg, "status"), "rejected") && L.h->entry_rejected) L.h->entry_rejected((uint32_t)clamp_i32(get_i64(msg, "rid", 0), 0, 2000000000));
    } else if (!strcmp(type, "projects")) on_list_page(msg, LIST_PROJECTS);
    else if (!strcmp(type, "tasks")) on_list_page(msg, LIST_TASKS);
    else if (!strcmp(type, "fetch_failed")) {
        if (L.h->fetch_failed) L.h->fetch_failed(!strcmp(get_str(msg, "what"), "projects") ? LIST_PROJECTS : LIST_TASKS, clamp_i32(get_i64(msg, "req", 0), -1000000000, 1000000000));
    } else if (!strcmp(type, "sync_result")) on_sync_result(msg);
    else if (!strcmp(type, "transcript")) on_transcript(msg);
    else if (!strcmp(type, "lock_code")) on_lock_code(msg);
    else if (!strcmp(type, "clock")) on_clock(msg, now_us);
    cJSON_Delete(msg);
    s_arena_used = 0;
}

void link_feed_line(const char *line)
{
    if (!line || L.forced_off) return;
    dispatch(line, strlen(line), hal_now_us());
}

// ---- The transport -----------------------------------------------------------------------------------------------------

static bool transport_up(void)
{
    if (L.forced_off) return false;
    if (L.tx) return true;
    hal_link_status_t st;
    hal_link_status(&st);
    return st.connected;
}

static void reset_session(void)
{
    L.len = 0;
    L.skip = false;
    L.allowed = false;
    L.incoming[LIST_PROJECTS].active = false;
    L.incoming[LIST_TASKS].active = false;
}

void link_poll(int64_t now_us)
{
    const bool up = transport_up();
    if (up != L.up) {
        L.up = up;
        reset_session();
        if (!up && L.h->disconnected) L.h->disconnected();
    }
    if (!up) {
        if (L.forced_off && !L.tx) { // what arrives meanwhile is lost, as it would be
            static uint8_t drop[256];
            while (hal_link_read(drop, sizeof drop) > 0) {}
        }
        return;
    }
    if (!L.tx && L.line) {
        static uint8_t chunk[512];
        int budget = READ_PER_POLL, lines = LINES_PER_POLL;
        while (budget > 0 && lines > 0) {
            const int got = hal_link_read(chunk, sizeof chunk < (size_t)budget ? (int)sizeof chunk : budget);
            if (got <= 0) break;
            budget -= got;
            for (int i = 0; i < got; i++) {
                const char c = (char)chunk[i];
                if (c == '\n') {
                    if (!L.skip && L.len) {
                        L.line[L.len] = 0;
                        dispatch(L.line, L.len, now_us);
                        lines--;
                    }
                    L.len = 0;
                    L.skip = false;
                } else if (!L.skip) {
                    if (L.len >= LINE_MAX_BYTES - 1) { // too long: drop the rest of it
                        L.skip = true;
                        L.len = 0;
                        L.bad++;
                    } else {
                        L.line[L.len++] = c;
                    }
                }
            }
        }
    }
    if (L.allowed) outbox_flush(now_us);
}

// Made once, the first time the board starts, and kept. Erasing a board gives it a new one.
static void load_uid(void)
{
    char saved[9];
    size_t n = sizeof saved;
    if (hal_store_get("uid", saved, &n) && n == 8) {
        bool ok = true;
        for (int i = 0; i < 8; i++) {
            if (hex_val(saved[i]) < 0) ok = false;
        }
        if (ok) {
            memcpy(L.uid, saved, 8);
            L.uid[8] = 0;
            return;
        }
    }
    snprintf(L.uid, sizeof L.uid, "%08lx", (unsigned long)hal_random());
    hal_store_set("uid", L.uid, 8);
}

static uint32_t uid_number(const char *s)
{
    uint32_t v = 0;
    for (int i = 0; i < 8 && s[i]; i++) v = v << 4 | (uint32_t)hex_val(s[i]);
    return v;
}

void link_init(const link_handlers_t *h, const char *fw_version, bool (*lock_is_set)(void))
{
    memset(&L, 0, sizeof L);
    L.h = h;
    L.fw = fw_version;
    L.lock_is_set = lock_is_set;
    L.next_req = 1;
    L.line = hal_alloc_large(LINE_MAX_BYTES);
    L.row = hal_alloc_large(sizeof(link_agent_t));
    L.project_rows = hal_alloc_large(LINK_MAX_PROJECTS * sizeof(link_project_t));
    L.task_rows = hal_alloc_large(LINK_MAX_TASKS * sizeof(link_task_t));
    if (!s_arena) s_arena = hal_alloc_large(ARENA_BYTES);
    cJSON_Hooks hooks = {.malloc_fn = arena_malloc, .free_fn = arena_free};
    cJSON_InitHooks(&hooks);
    load_uid();
    L.uid_value = uid_number(L.uid);
    outbox_init();
}

const char *link_uid(void) { return L.uid; }
uint32_t link_uid_value(void) { return L.uid_value; }
bool link_up(void) { return L.up; }
bool link_ready(void) { return L.up && L.allowed; }

bool link_clock_known(void) { return L.clock_known; }
int64_t link_unix_now(int64_t now_us) { return L.clock_unix + (now_us - L.clock_mono) / 1000000; }
int32_t link_utc_offset(void) { return L.utc; }

bool link_send_line(const char *line, size_t len)
{
    if (L.forced_off) return false;
    if (L.tx) return L.tx(line, len);
    return hal_link_send(line, len);
}

bool link_send_tap(const char *agent_id)
{
    if (!link_ready()) return false;
    char line[96];
    jw_t w;
    jw_init(&w, line, sizeof line);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "tap");
    jw_str(&w, "id", agent_id);
    jw_end(&w);
    return jw_finish(&w) && link_send_line(line, w.len);
}

bool link_send_resync(void)
{
    if (!link_ready()) return false;
    static const char line[] = "{\"t\":\"resync\"}\n";
    return link_send_line(line, sizeof line - 1);
}

uint32_t link_toggl_new_rid(void) { return outbox_new_rid(); }

uint32_t link_toggl_start(const char *desc, int64_t task_id, int64_t project, int64_t now_us)
{
    const uint32_t rid = outbox_new_rid();
    outbox_start(rid, desc, task_id, project, now_us);
    return rid;
}

void link_toggl_update(uint32_t rid, uint32_t of, unsigned fields, const char *desc, int64_t task_id, int64_t project, int64_t now_us) { outbox_update(rid, of, fields, desc, task_id, project, now_us); }
void link_toggl_shift(uint32_t rid, uint32_t of, int64_t start_mono_us, int64_t now_us) { outbox_shift(rid, of, start_mono_us, now_us); }
void link_toggl_stop(uint32_t rid, uint32_t of, int64_t at_us) { outbox_stop(rid, of, at_us); }
bool link_take_dropped(void) { return outbox_take_dropped(); }

int link_fetch(link_list_t kind)
{
    if (!link_ready()) return 0;
    const int req = L.next_req++;
    char line[96];
    jw_t w;
    jw_init(&w, line, sizeof line);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "fetch");
    jw_i64(&w, "req", req);
    jw_str(&w, "what", kind == LIST_PROJECTS ? "projects" : "tasks");
    jw_end(&w);
    return jw_finish(&w) && link_send_line(line, w.len) ? req : 0;
}

int link_sync(uint32_t rid)
{
    if (!link_ready()) return 0;
    const int req = L.next_req++;
    char line[96];
    jw_t w;
    jw_init(&w, line, sizeof line);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "sync");
    jw_i64(&w, "req", req);
    jw_i64(&w, "rid", rid);
    jw_end(&w);
    return jw_finish(&w) && link_send_line(line, w.len) ? req : 0;
}

// ---- `clock` to the Mac: times in this board's own milliseconds, with `now` to read them by

static void write_uid_field(jw_t *w, const char *key, uint32_t uid)
{
    if (!uid) return; // this board's own: no `of`
    char s[9];
    snprintf(s, sizeof s, "%08lx", (unsigned long)uid);
    jw_str(w, key, s);
}

static void write_project(jw_t *w, const link_project_t *p)
{
    jw_begin(w, "project");
    jw_i64(w, "id", p->id < 0 ? -1 : p->id);
    if (p->id > 0) {
        char color[8];
        snprintf(color, sizeof color, "#%06lX", (unsigned long)(p->color & 0xFFFFFF));
        jw_str(w, "name", p->name);
        jw_str(w, "color", color);
    }
    jw_end(w);
}

// with_text: the task's description and the project's name. Without them a line always fits.
static size_t format_clock(char *line, size_t cap, const link_clock_t *c, bool changed, uint32_t seq, int64_t now_us, bool with_text)
{
    static const char *const TIMER[] = {"idle", "running", "paused", "idle"};
    static const char *const POMO[] = {"idle", "running", "paused", "done"};
    jw_t w;
    jw_init(&w, line, cap);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "clock");
    jw_i64(&w, "seq", seq);
    jw_bool(&w, "changed", changed);
    jw_i64(&w, "now", now_us / 1000);
    if (c->has_at) jw_i64(&w, "at", c->at_us / 1000);
    jw_begin(&w, "task");
    jw_str(&w, "desc", with_text ? c->task : "");
    jw_i64(&w, "task_id", c->task_id > 0 ? c->task_id : 0);
    link_project_t project = c->project;
    if (!with_text) project.name[0] = 0;
    write_project(&w, &project);
    jw_end(&w);
    jw_begin(&w, "timer");
    jw_str(&w, "st", TIMER[c->timer.st & 3]);
    jw_i64(&w, "acc", c->timer.acc_us / 1000);
    if (c->timer.st == 1) jw_i64(&w, "start", c->timer.start_us / 1000);
    jw_i64(&w, "rid", c->timer.rid);
    write_uid_field(&w, "of", c->timer.of);
    jw_end(&w);
    jw_begin(&w, "pomo");
    jw_str(&w, "st", POMO[c->pomo.st & 3]);
    jw_i64(&w, "set", c->pomo.set_min);
    jw_i64(&w, "total", c->pomo.total_us / 1000);
    jw_i64(&w, "left", c->pomo.left_us / 1000);
    jw_i64(&w, "rid", c->pomo.rid);
    write_uid_field(&w, "of", c->pomo.of);
    jw_end(&w);
    jw_end(&w);
    return jw_finish(&w) ? w.len : 0;
}

bool link_send_clock(const link_clock_t *c, bool changed, uint32_t seq, int64_t now_us)
{
    if (!link_ready()) return false;
    char line[1024];
    // Text full of characters that need escaping can outgrow the line: then it goes without the text, rather than not
    // at all (a board that kept trying would never send its clocks again). The Mac keeps the clocks; only the names
    // are missing, and the next change sends them again.
    size_t n = format_clock(line, sizeof line, c, changed, seq, now_us, true);
    if (!n) n = format_clock(line, sizeof line, c, changed, seq, now_us, false);
    return n && link_send_line(line, n);
}

void link_override_transport(link_tx_fn tx)
{
    L.tx = tx;
    reset_session();
    // Up at once with an override, so that a hello in the same breath isn't lost to the next poll's new session.
    L.up = tx != NULL && !L.forced_off;
}

void link_force_offline(bool off) { L.forced_off = off; }
int link_outbox_count(void) { return outbox_count(); }
uint32_t link_outbox_next_id(void) { return outbox_next_id(); }

void link_lines_stats(uint32_t *good, uint32_t *bad)
{
    *good = L.good;
    *bad = L.bad;
}
