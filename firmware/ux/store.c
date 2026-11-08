// What survives a restart, through hal_store: settings (2 s after a change), the lock state and the code's hash (at
// once), and the cached lists (when they change). A value that doesn't look right is ignored.
#include <string.h>

#include "app.h"

#define SETTINGS_VERSION 3
#define SETTINGS_THREE_LEVELS 2 // brightness was low, medium or high

typedef struct {
    uint8_t version;
    uint8_t theme, bright, autoscroll, vol, autorotate, voice_ok, set_min;
    char mode[16];
    char task[LINK_TASK_LEN];
    int64_t task_id;
    link_project_t project;
} saved_settings_t;

typedef struct {
    uint8_t version, open, tries, strikes; // version 1 had a zeroed pad byte where strikes is: it reads as no strikes
    int32_t wait_s; // what was left of a wait, when it was saved
} saved_lock_t;

typedef struct {
    uint8_t version, salt_len, has_code, pad_;
    uint32_t iter;
    uint8_t salt[32];
    uint8_t hash[32];
} saved_code_t;

// Split view's settings, under their own key: the main settings' layout stays as it was.
typedef struct {
    uint8_t version, split, auto_switch, pad_;
    uint16_t after_s, pad2_;
} saved_split_t;

// What's saved is these records, byte for byte. A change to one of them (or to a protocol size inside, such as
// LINK_TASK_LEN or LINK_NAME_LEN) must come with a new version number and a way to read the old one, or a board loses
// its settings: these checks make sure nobody changes one by accident.
_Static_assert(sizeof(saved_settings_t) == 192, "saved_settings_t changed: give it a new SETTINGS_VERSION and read the old one");
_Static_assert(sizeof(saved_lock_t) == 8, "saved_lock_t changed: give it a new version and read the old one");
_Static_assert(sizeof(saved_code_t) == 72, "saved_code_t changed: give it a new version and read the old one");
_Static_assert(sizeof(saved_split_t) == 8, "saved_split_t changed: give it a new version and read the old one");
_Static_assert(sizeof(link_project_t) == 64 && sizeof(link_task_t) == 168, "the cached lists' rows changed: bump LIST_FORMAT");

// The cached lists are plain arrays of rows. Their format goes beside them, so rows of another layout are never read.
#define LIST_FORMAT ((1u << 24) | ((uint32_t)sizeof(link_project_t) << 12) | (uint32_t)sizeof(link_task_t))

static const uint16_t SPLIT_AFTERS[] = {5, 10, 15, 30, 60, 300};

static void load_split(void)
{
    saved_split_t v;
    size_t n = sizeof v;
    if (!hal_store_get("split", &v, &n) || n != sizeof v || v.version != 1) return;
    S.split = v.split == SPLIT_CLOCK ? SPLIT_CLOCK : SPLIT_AGENTS;
    S.auto_switch = v.auto_switch != 0;
    for (size_t i = 0; i < sizeof SPLIT_AFTERS / sizeof SPLIT_AFTERS[0]; i++) {
        if (v.after_s == SPLIT_AFTERS[i]) S.split_after_s = v.after_s;
    }
}

static void load_settings(void)
{
    saved_settings_t v;
    size_t n = sizeof v;
    if (!hal_store_get("settings", &v, &n) || n != sizeof v || (v.version != SETTINGS_VERSION && v.version != SETTINGS_THREE_LEVELS)) return;
    S.theme = v.theme ? 1 : 0;
    if (v.version == SETTINGS_THREE_LEVELS) S.bright = v.bright == 0 ? 6 : v.bright == 1 ? 8 : HAL_BRIGHT_LEVELS - 1; // about the same light
    else S.bright = v.bright < HAL_BRIGHT_LEVELS ? v.bright : HAL_BRIGHT_LEVELS - 1;
    S.autoscroll = v.autoscroll != 0;
    S.vol = v.vol <= 10 ? v.vol : 6;
    S.autorotate = v.autorotate != 0;
    S.voice_ok = v.voice_ok != 0;
    S.p.set_min = v.set_min >= 1 && v.set_min <= 60 ? v.set_min : 25;
    v.mode[sizeof v.mode - 1] = 0;
    const ux_mode_t *m = mode_by_key(v.mode);
    if (m && m->draw && m != &MODE_SETTINGS) S.mode = m;
    v.task[sizeof v.task - 1] = 0;
    v.project.name[sizeof v.project.name - 1] = 0;
    memcpy(S.task, v.task, sizeof S.task);
    S.task_id = v.task_id;
    S.project = v.project;
}

void store_save_settings(void)
{
    saved_settings_t v;
    memset(&v, 0, sizeof v);
    v.version = SETTINGS_VERSION;
    v.theme = (uint8_t)S.theme;
    v.bright = (uint8_t)S.bright;
    v.autoscroll = S.autoscroll;
    v.vol = (uint8_t)S.vol;
    v.autorotate = S.autorotate;
    v.voice_ok = S.voice_ok;
    v.set_min = (uint8_t)S.p.set_min;
    const ux_mode_t *m = S.mode == &MODE_SETTINGS && S.set.from ? S.set.from : S.mode;
    utf8_copy(v.mode, sizeof v.mode, m->key);
    memcpy(v.task, S.task, sizeof v.task);
    v.task_id = S.task_id;
    v.project = S.project;
    hal_store_set("settings", &v, sizeof v);
    saved_split_t sp = {.version = 1, .split = (uint8_t)S.split, .auto_switch = S.auto_switch, .after_s = (uint16_t)S.split_after_s};
    hal_store_set("split", &sp, sizeof sp);
    ux_log("saved", "settings");
}

void store_save_lock(void)
{
    saved_lock_t v;
    memset(&v, 0, sizeof v);
    v.version = 2;
    v.open = S.lock.open;
    v.tries = (uint8_t)S.lock.tries;
    v.strikes = (uint8_t)(S.lock.strikes < 255 ? S.lock.strikes : 255);
    const int64_t now = hal_now_us();
    v.wait_s = now < S.lock.until ? (int32_t)((S.lock.until - now) / SEC_US + 1) : 0;
    hal_store_set("lock", &v, sizeof v);
}

void store_save_lock_code(void)
{
    saved_code_t v;
    memset(&v, 0, sizeof v);
    v.version = 1;
    v.has_code = S.lock.has_code;
    if (S.lock.has_code) {
        v.salt_len = (uint8_t)S.lock.salt_len;
        v.iter = S.lock.iter;
        memcpy(v.salt, S.lock.salt, sizeof v.salt);
        memcpy(v.hash, S.lock.hash, sizeof v.hash);
        hal_store_set("lock_code", &v, sizeof v);
    } else {
        hal_store_erase("lock_code");
    }
}

static void load_lock(void)
{
    saved_code_t c;
    size_t n = sizeof c;
    if (hal_store_get("lock_code", &c, &n) && n == sizeof c && c.version == 1 && c.has_code && c.salt_len >= 1 && c.salt_len <= sizeof c.salt && c.iter >= 1) {
        S.lock.has_code = true;
        S.lock.salt_len = c.salt_len;
        S.lock.iter = c.iter;
        memcpy(S.lock.salt, c.salt, sizeof c.salt);
        memcpy(S.lock.hash, c.hash, sizeof c.hash);
    }
    saved_lock_t l;
    n = sizeof l;
    if (hal_store_get("lock", &l, &n) && n == sizeof l && (l.version == 1 || l.version == 2)) {
        S.lock.open = l.open != 0;
        S.lock.tries = l.tries < 5 ? l.tries : 0;
        S.lock.strikes = l.version >= 2 && l.strikes <= 100 ? l.strikes : 0;
        if (l.wait_s > 0 && l.wait_s <= 15 * 60) S.lock.until = hal_now_us() + (int64_t)l.wait_s * SEC_US;
    }
}

void store_save_list(link_list_t kind)
{
    const uint32_t format = LIST_FORMAT;
    hal_store_set("c_fmt", &format, sizeof format);
    if (kind == LIST_PROJECTS) {
        hal_store_set("c_projects", S.projects, (size_t)S.n_projects * sizeof(link_project_t));
    } else {
        hal_store_set("c_tasks", S.tasks, (size_t)S.n_tasks * sizeof(link_task_t));
    }
}

static void load_lists(void)
{
    // No format key: saved before there was one, in today's layout. Another format: rows of a layout this can't read.
    uint32_t format = 0;
    size_t fn = sizeof format;
    if (hal_store_get("c_fmt", &format, &fn) && (fn != sizeof format || format != LIST_FORMAT)) return;
    size_t n = LINK_MAX_PROJECTS * sizeof(link_project_t);
    if (S.projects && hal_store_get("c_projects", S.projects, &n) && n % sizeof(link_project_t) == 0 && n <= LINK_MAX_PROJECTS * sizeof(link_project_t)) {
        S.n_projects = (int)(n / sizeof(link_project_t));
        for (int i = 0; i < S.n_projects; i++) S.projects[i].name[sizeof S.projects[i].name - 1] = 0;
    }
    n = LINK_MAX_TASKS * sizeof(link_task_t);
    if (S.tasks && hal_store_get("c_tasks", S.tasks, &n) && n % sizeof(link_task_t) == 0 && n <= LINK_MAX_TASKS * sizeof(link_task_t)) {
        S.n_tasks = (int)(n / sizeof(link_task_t));
        for (int i = 0; i < S.n_tasks; i++) {
            S.tasks[i].desc[sizeof S.tasks[i].desc - 1] = 0;
            S.tasks[i].project.name[sizeof S.tasks[i].project.name - 1] = 0;
        }
    }
}

void store_load(void)
{
    load_settings();
    load_split();
    load_lock();
    load_lists();
}
