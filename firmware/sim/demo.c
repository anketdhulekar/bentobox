// --demo: a stand-in for the desktop app, so that the screens can be reviewed alone. The sample agents are the
// preview's. It speaks the real protocol through the real link code: nothing in firmware/ux knows it's here.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "sim.h"

static char s_in[512 * 1024];
static size_t s_in_len, s_in_pos;
static bool s_on = true;
static int s_needs = -1;
static int64_t s_last_state, s_t0;
static bool s_helloed;
static int s_seq;
static int s_count = 5;
static volatile bool s_changed; // from the window's thread: the UX's thread sends the news
static int s_sent_count, s_sent_needs = -1;

typedef struct { const char *id, *name, *proj, *agent, *st, *act; int age_min; } sample_t;
static const sample_t AGENTS[5] = {
    {"a1000e7", "api-refactor-12", "api-server", "", "working", "Editing main.c", 4},
    {"a1005e7", "docs-site-3f", "docs", "codex", "working", "Editing index.md", 2},
    {"a1001e7", "ml-eval-5d", "ml", "grok", "working", "Reading train.py", 11},
    {"a1006e7", "ci-cleanup-8b", "infra", "", "done", "Finished", 1},
    {"a1002e7", "landing-page-5b", "web", "codex", "idle", "Waiting for a task", 26},
};

// Past those five, made up, so that a crowded zoo can be tried.
#define DEMO_MAX_AGENTS 512
#define ROWS_PER_PAGE 32
static const char *const MORE_NAMES[25] = {
    "auth-flow", "billing-api", "search-index", "mobile-app", "data-pipeline", "design-tokens", "release-notes", "load-tests", "k8s-upgrade",
    "onboarding", "telemetry", "payments", "email-digest", "feature-flags", "cache-layer", "a11y-audit", "invoice-pdf", "sso-login",
    "rate-limits", "db-migration", "webhooks", "cli-tools", "dark-mode", "changelog", "notify-queue"};

static sample_t sample_at(int i, char *id_buf, size_t id_size, char *name_buf, size_t name_size)
{
    if (i < 5) return AGENTS[i];
    static const char *const KINDS[3] = {"", "codex", "grok"};
    static const char *const STATES[5] = {"working", "working", "idle", "working", "done"};
    const char *st = STATES[i % 5];
    snprintf(id_buf, id_size, "a2%04xe", i);
    snprintf(name_buf, name_size, "%s-%d", MORE_NAMES[i % 25], i);
    return (sample_t){id_buf, name_buf, MORE_NAMES[i % 25], KINDS[i % 3], st,
                      !strcmp(st, "idle") ? "Waiting for a task" : !strcmp(st, "done") ? "Finished" : "Running the tests", 3 + i % 40};
}

static void push(const char *line)
{
    const size_t n = strlen(line);
    if (s_in_pos == s_in_len) s_in_pos = s_in_len = 0;
    if (s_in_len + n + 1 > sizeof s_in) return;
    memcpy(s_in + s_in_len, line, n);
    s_in_len += n;
    s_in[s_in_len++] = '\n';
}

static void send_state(void)
{
    char line[256];
    const int64_t now_s = (int64_t)time(NULL);
    struct tm local;
    const time_t tt = (time_t)now_s;
    localtime_r(&tt, &local);
    snprintf(line, sizeof line, "{\"t\":\"state\",\"v\":4,\"seq\":%d,\"now\":%lld,\"utc\":%ld,\"toggl\":\"ok\",\"voice\":true,\"n\":%d}", ++s_seq, (long long)now_s, (long)local.tm_gmtoff, s_count);
    push(line);
    s_last_state = hal_now_us();
}

static int append_row(char *line, size_t cap, int n, int i, bool first)
{
    char id[12], name[32];
    const sample_t a = sample_at(i, id, sizeof id, name, sizeof name);
    const bool needs = i == s_needs;
    const int64_t up = (hal_now_us() - s_t0) / 1000000;
    return n + snprintf(line + n, cap - (size_t)n,
                        "%s{\"id\":\"%s\",\"name\":\"%s\",\"proj\":\"%s\",\"kind\":\"i\",\"st\":\"%s\",\"act\":\"%s\",\"note\":\"\",\"age\":%lld,\"up\":%lld,\"rex\":1,\"where\":\"Rex session \\u201cwork\\u201d\"%s%s%s}",
                        first ? "" : ",", a.id, a.name, a.proj, needs ? "needs" : a.st, needs ? "Waiting for your answer" : a.act,
                        (long long)(a.age_min * 60 + up), (long long)(a.age_min * 180 + up), a.agent[0] ? ",\"agent\":\"" : "", a.agent, a.agent[0] ? "\"" : "");
}

static void send_whole_list(void)
{
    static char line[16 * 1024];
    const int pages = s_count ? (s_count + ROWS_PER_PAGE - 1) / ROWS_PER_PAGE : 1;
    for (int p = 0; p < pages; p++) {
        int n = snprintf(line, sizeof line, "{\"t\":\"agents\",\"page\":%d,\"pages\":%d,\"rows\":[", p, pages);
        for (int i = p * ROWS_PER_PAGE; i < s_count && i < (p + 1) * ROWS_PER_PAGE; i++) n = append_row(line, sizeof line, n, i, i == p * ROWS_PER_PAGE);
        snprintf(line + n, sizeof line - (size_t)n, "]}");
        push(line);
    }
    s_sent_count = s_count;
    s_sent_needs = s_needs;
}

// What changed since the last time: new agents, ended ones, and who needs you.
static void send_changes(void)
{
    static char line[16 * 1024];
    int n = snprintf(line, sizeof line, "{\"t\":\"agents\",\"rows\":[");
    bool first = true;
    for (int i = s_sent_count; i < s_count; i++) {
        n = append_row(line, sizeof line, n, i, first);
        first = false;
        if (n > 11000) { // a line at a time, as the Mac does
            snprintf(line + n, sizeof line - (size_t)n, "]}");
            push(line);
            n = snprintf(line, sizeof line, "{\"t\":\"agents\",\"rows\":[");
            first = true;
        }
    }
    if (s_sent_needs != s_needs) {
        for (int k = 0; k < 2; k++) {
            const int i = k ? s_needs : s_sent_needs;
            if (i >= 0 && i < s_sent_count && i < s_count) {
                n = append_row(line, sizeof line, n, i, first);
                first = false;
            }
        }
    }
    n += snprintf(line + n, sizeof line - (size_t)n, "],\"gone\":[");
    first = true;
    for (int i = s_count; i < s_sent_count; i++) {
        char id[12], name[32];
        const sample_t a = sample_at(i, id, sizeof id, name, sizeof name);
        n += snprintf(line + n, sizeof line - (size_t)n, "%s\"%s\"", first ? "" : ",", a.id);
        first = false;
    }
    snprintf(line + n, sizeof line - (size_t)n, "]}");
    push(line);
    s_sent_count = s_count;
    s_sent_needs = s_needs;
}

static void send_projects(int req)
{
    char line[512];
    snprintf(line, sizeof line,
             "{\"t\":\"projects\",\"req\":%d,\"page\":0,\"pages\":1,\"rows\":[{\"id\":101,\"name\":\"Acme\",\"color\":\"#F2C14E\"},{\"id\":102,\"name\":\"Admin\",\"color\":\"#9CA3AF\"},"
             "{\"id\":123,\"name\":\"Deep work\",\"color\":\"#4FB3F6\"},{\"id\":104,\"name\":\"Writing\",\"color\":\"#E86FA6\"}]}", req);
    push(line);
}

static void send_tasks(int req)
{
    char line[1024];
    snprintf(line, sizeof line,
             "{\"t\":\"tasks\",\"req\":%d,\"page\":0,\"pages\":1,\"rows\":["
             "{\"desc\":\"Write the Q4 plan\",\"task_id\":0,\"project\":{\"id\":123,\"name\":\"Deep work\",\"color\":\"#4FB3F6\"}},"
             "{\"desc\":\"Acme design review\",\"task_id\":0,\"project\":{\"id\":101,\"name\":\"Acme\",\"color\":\"#F2C14E\"}},"
             "{\"desc\":\"Inbox\",\"task_id\":0,\"project\":{\"id\":102,\"name\":\"Admin\",\"color\":\"#9CA3AF\"}},"
             "{\"desc\":\"Quarterly report\",\"task_id\":0,\"project\":{\"id\":104,\"name\":\"Writing\",\"color\":\"#E86FA6\"}}]}", req);
    push(line);
}

void demo_start(void)
{
    s_t0 = hal_now_us();
    s_on = true;
    if (SIM.agents > 0) demo_set_agents(SIM.agents);
    push("{\"t\":\"hello\",\"v\":4,\"host\":\"MacBook Pro\"}");
    s_helloed = false;
}

bool demo_connected(void) { return s_on; }

void demo_toggle_mac(void)
{
    s_on = !s_on;
    if (s_on) {
        s_in_pos = s_in_len = 0;
        push("{\"t\":\"hello\",\"v\":4,\"host\":\"MacBook Pro\"}");
        s_helloed = false;
    }
}

void demo_toggle_needs(void)
{
    s_needs = s_needs >= 0 ? -1 : 2;
    s_changed = true;
}

void demo_set_agents(int n)
{
    s_count = n < 0 ? 0 : n > DEMO_MAX_AGENTS ? DEMO_MAX_AGENTS : n;
    s_changed = true;
}

int demo_agent_count(void) { return s_count; }

void demo_poll(void)
{
    if (!s_on) return;
    if (!s_helloed) return;
    if (s_changed) {
        s_changed = false;
        send_changes();
        send_state();
    } else if (hal_now_us() - s_last_state > 5000000) {
        send_state();
    }
}

int demo_read(uint8_t *out, int max)
{
    const size_t left = s_in_len - s_in_pos;
    if (!left) return 0;
    const size_t n = left < (size_t)max ? left : (size_t)max;
    memcpy(out, s_in + s_in_pos, n);
    s_in_pos += n;
    return (int)n;
}

// What the board sends. The answers are what the desktop app would say.
void demo_on_line(const char *line)
{
    int v = 0;
    const char *p;
    if (strstr(line, "\"t\":\"board\"")) {
        s_helloed = true;
        send_state();
        send_whole_list();
        send_projects(0);
        send_tasks(0);
        // The lock code from protocol/samples/vectors.json: 1234. The Mac sends only its salted hash.
        push("{\"t\":\"lock_code\",\"salt\":\"000102030405060708090a0b0c0d0e0f\",\"iter\":2000,\"hash\":\"c41892d2ceec337a4279f5fad0221e9c160182318f46d81616410f17ac895a36\"}");
    } else if (strstr(line, "\"t\":\"resync\"")) {
        send_whole_list();
    } else if (strstr(line, "\"t\":\"toggl\"") && (p = strstr(line, "\"id\":")) && sscanf(p + 5, "%d", &v) == 1) {
        char ack[64];
        snprintf(ack, sizeof ack, "{\"t\":\"ack\",\"id\":%d}", v);
        push(ack);
    } else if (strstr(line, "\"t\":\"fetch\"") && (p = strstr(line, "\"req\":")) && sscanf(p + 6, "%d", &v) == 1) {
        if (strstr(line, "\"projects\"")) send_projects(v);
        else send_tasks(v);
    } else if (strstr(line, "\"t\":\"sync\"") && (p = strstr(line, "\"req\":")) && sscanf(p + 6, "%d", &v) == 1) {
        char reply[64];
        snprintf(reply, sizeof reply, "{\"t\":\"sync_result\",\"req\":%d,\"result\":\"in_sync\"}", v);
        push(reply);
    } else if (strstr(line, "\"t\":\"voice_end\"") && (p = strstr(line, "\"vid\":")) && sscanf(p + 6, "%d", &v) == 1) {
        char reply[128];
        snprintf(reply, sizeof reply, "{\"t\":\"transcript\",\"vid\":%d,\"text\":\"Acme design review\",\"project\":101}", v);
        push(reply);
    }
}
