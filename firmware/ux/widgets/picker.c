// The tasks and projects lists. Both come from a cache in flash, so they open at once, work with no Mac and survive a
// restart. Toggl is asked only when "Sync from Toggl" is chosen. Row 0 is that, row 1 is "Untitled" or "No project",
// where the cursor opens, and the cached rows follow. With a knob: turn, then tap, because a tap without a turn only
// closes the list. With no knob: drag to scroll, and a tap picks a row.
#include <math.h>
#include <string.h>

#include "../app.h"

#define LIST_IDLE_US (10 * SEC_US)
#define SYNC_TIMEOUT_US (15 * SEC_US)
enum { ROW_SYNC, ROW_NONE, ROW_FIRST };

static int row_count(void) { return ROW_FIRST + (S.list.kind == LIST_PROJECTS ? S.n_projects : S.n_tasks); }

const link_project_t *picker_find_project(int64_t id)
{
    for (int i = 0; i < S.n_projects; i++) {
        if (S.projects[i].id == id) return &S.projects[i];
    }
    return NULL;
}

static void row_of(int i, list_row_t *r)
{
    const bool knobless = !F.L->list.wheel;
    if (i == ROW_SYNC) {
        r->label = "Sync from Toggl";
        r->muted = true;
        if (knobless && S.list.sync == LISTSYNC_LOADING) r->label = "Syncing\xE2\x80\xA6";
        if (knobless && S.list.sync == LISTSYNC_FAILED) {
            r->label = "Couldn\xE2\x80\x99t reach Toggl";
            r->danger = true;
        }
        return;
    }
    if (i == ROW_NONE) {
        r->label = S.list.kind == LIST_PROJECTS ? "No project" : "Untitled";
        r->dot = DOT_HOLLOW;
        return;
    }
    if (S.list.kind == LIST_PROJECTS) {
        const link_project_t *p = &S.projects[i - ROW_FIRST];
        r->label = p->name;
        r->dot = DOT_FILLED;
        r->dot_rgb = p->color;
    } else {
        const link_task_t *t = &S.tasks[i - ROW_FIRST];
        r->label = t->desc[0] ? t->desc : "Untitled";
        r->dot = t->project.id > 0 ? DOT_FILLED : DOT_HOLLOW;
        r->dot_rgb = t->project.color;
    }
}

static const char *hint_line(void)
{
    if (S.list.sync == LISTSYNC_LOADING) return "syncing\xE2\x80\xA6";
    if (S.list.sync == LISTSYNC_FAILED) return "couldn\xE2\x80\x99t reach Toggl";
    if (S.list.idx == ROW_SYNC) return "tap to sync";
    const int n = S.list.kind == LIST_PROJECTS ? S.n_projects : S.n_tasks;
    if (!n) return S.list.kind == LIST_PROJECTS ? "no projects yet" : "no recent entries";
    return "tap to choose";
}

static void build_view(list_view_t *v)
{
    memset(v, 0, sizeof *v);
    v->title = S.list.kind == LIST_PROJECTS ? "Projects" : "Past tasks";
    v->hint1 = hint_line();
    v->hint2 = "swipe down to close";
    v->count = row_count();
    v->cursor = S.list.idx;
    v->pos = S.list.pos;
    v->scroll = S.list.scroll;
    v->scrollable = true;
    v->row = row_of;
}

void picker_draw(void)
{
    list_view_t v;
    build_view(&v);
    list_draw(&v);
}

void picker_open(link_list_t kind, int64_t now)
{
    if (S.toggl == TOGGL_NO_KEY) {
        ux_bump("Toggl isn\xE2\x80\x99t set up");
        return;
    }
    S.list.open = true;
    S.list.kind = kind;
    S.list.idx = ROW_NONE;
    S.list.pos = (float)ROW_NONE;
    S.list.touched = false;
    S.list.sync = LISTSYNC_IDLE;
    S.list.until = now + LIST_IDLE_US;
    S.list.scroll = 0;
    ux_feedback(FB_TAP);
    ux_log("list", kind == LIST_PROJECTS ? "projects" : "tasks");
}

static void pick_row(int idx, int64_t now)
{
    if (S.list.kind == LIST_PROJECTS) {
        if (idx >= row_count()) return;
        const link_project_t *p = idx >= ROW_FIRST ? &S.projects[idx - ROW_FIRST] : NULL;
        const int64_t id = p ? p->id : 0;
        if (id == (S.project.id > 0 ? S.project.id : 0) && S.project.id >= 0) return; // unchanged
        S.task_id = 0; // a Toggl task belongs to its project
        clock_set_project(p, true, now);
    } else if (idx == ROW_NONE) {
        clock_set_task("", 0, true, now);
    } else if (idx < row_count()) {
        const link_task_t *e = &S.tasks[idx - ROW_FIRST];
        // Copy: picking it moves it to the top of the list.
        link_task_t t = *e;
        const link_project_t *known = t.project.id > 0 ? picker_find_project(t.project.id) : NULL;
        if (known) t.project = *known;
        const bool same = !strcmp(t.desc, S.task) && t.task_id == S.task_id && t.project.id == (S.project.id > 0 ? S.project.id : 0);
        if (same) return;
        S.project = t.project;
        clock_set_task(t.desc, t.task_id, true, now);
    }
}

void picker_close(bool choose, int64_t now)
{
    if (!S.list.open) return;
    S.list.open = false;
    ux_feedback(FB_TAP);
    if (choose && S.list.touched && S.list.idx != ROW_SYNC) pick_row(S.list.idx, now);
    ux_log("list", "closed");
}

static void sync_now(int64_t now)
{
    if (S.list.sync == LISTSYNC_LOADING) {
        ux_bump(NULL);
        return;
    }
    S.list.until = now + LIST_IDLE_US;
    if (!S.connected) {
        S.list.sync = LISTSYNC_FAILED;
        ux_bump(NULL);
        return;
    }
    const int req = link_fetch(S.list.kind);
    if (!req) {
        S.list.sync = LISTSYNC_FAILED;
        ux_bump(NULL);
        return;
    }
    S.list.req = req;
    S.list.sync = LISTSYNC_LOADING;
    S.list.sync_since = now;
    ux_feedback(FB_TICK);
}

void picker_turn(int dir, int64_t now)
{
    S.list.until = now + LIST_IDLE_US;
    S.list.touched = true;
    if (S.list.sync == LISTSYNC_FAILED) S.list.sync = LISTSYNC_IDLE;
    const int next = S.list.idx + dir;
    if (next < 0 || next >= row_count()) {
        ux_bump(NULL);
        return;
    }
    S.list.idx = next;
    ux_feedback(FB_TICK);
}

void picker_drag(float dv, int64_t now)
{
    S.list.until = now + LIST_IDLE_US;
    list_view_t v;
    build_view(&v);
    if (F.L->list.wheel) {
        // A round screen with no knob: the finger moves the cursor.
        S.list.pos -= dv / F.L->list.rowH;
        if (S.list.pos < 0) S.list.pos = 0;
        if (S.list.pos > (float)(row_count() - 1)) S.list.pos = (float)(row_count() - 1);
        S.list.idx = (int)(S.list.pos + 0.5f);
        S.list.touched = true;
        return;
    }
    S.list.scroll -= dv;
    const float max = list_max_scroll(&v);
    if (S.list.scroll < 0) S.list.scroll = 0;
    if (S.list.scroll > max) S.list.scroll = max;
}

void picker_tap(float u, float pos, int64_t now)
{
    (void)u;
    S.list.until = now + LIST_IDLE_US;
    list_view_t v;
    build_view(&v);
    if (!F.L->list.wheel) {
        const int i = list_row_at(&v, pos);
        if (i < 0) return;
        S.list.idx = i;
        S.list.touched = true;
    }
    if (S.list.idx == ROW_SYNC) sync_now(now);
    else picker_close(true, now);
}

void picker_tick(int64_t now)
{
    if (!S.list.open) return;
    const float ease = 1 - expf(-F.dt_ms / 70);
    S.list.pos += ((float)S.list.idx - S.list.pos) * ease;
    if (fabsf((float)S.list.idx - S.list.pos) < 0.002f) S.list.pos = (float)S.list.idx;
    if (S.list.sync == LISTSYNC_LOADING && now - S.list.sync_since > SYNC_TIMEOUT_US) {
        S.list.sync = LISTSYNC_FAILED;
        S.list.until = now + LIST_IDLE_US;
        ux_bump(NULL);
    }
    if (now > S.list.until && S.list.sync != LISTSYNC_LOADING) picker_close(false, now);
}

void picker_on_list(link_list_t kind, int req, const void *rows, int count, int64_t now)
{
    // The rows can change under an open list: the first load comes unasked (req 0), and an old fetch can still answer.
    // The cursor stays on what it's on, by what that is, so that a tap can't pick some other row.
    const bool open_here = S.list.open && S.list.kind == kind;
    const bool on_row = open_here && S.list.idx >= ROW_FIRST && S.list.idx < row_count();
    link_project_t was_project;
    link_task_t was_task;
    memset(&was_project, 0, sizeof was_project);
    memset(&was_task, 0, sizeof was_task);
    if (on_row && kind == LIST_PROJECTS) was_project = S.projects[S.list.idx - ROW_FIRST];
    if (on_row && kind == LIST_TASKS) was_task = S.tasks[S.list.idx - ROW_FIRST];

    if (kind == LIST_PROJECTS) {
        const int n = count > LINK_MAX_PROJECTS ? LINK_MAX_PROJECTS : count;
        if (n > 0) memcpy(S.projects, rows, (size_t)n * sizeof(link_project_t));
        S.n_projects = n;
        // Keep the chosen project's name and color current. Never chosen: "Work", if there's one.
        if (S.project.id > 0) {
            const link_project_t *p = picker_find_project(S.project.id);
            if (p) S.project = *p;
        } else if (S.project.id < 0) {
            const link_project_t *work = NULL;
            for (int i = 0; i < S.n_projects && !work; i++) {
                const char *a = S.projects[i].name;
                if ((a[0] == 'W' || a[0] == 'w') && (a[1] == 'o' || a[1] == 'O') && (a[2] == 'r' || a[2] == 'R') && (a[3] == 'k' || a[3] == 'K') && !a[4]) work = &S.projects[i];
            }
            if (work || n) clock_set_project(work, true, now);
        }
    } else {
        const int n = count > LINK_MAX_TASKS ? LINK_MAX_TASKS : count;
        if (n > 0) memcpy(S.tasks, rows, (size_t)n * sizeof(link_task_t));
        S.n_tasks = n;
    }
    store_save_list(kind);
    ux_log("list_loaded", kind == LIST_PROJECTS ? "projects" : "tasks");
    if (!open_here) return;
    // Only the answer to this list's own fetch ends its "syncing". S.list.req is never 0, so an unasked load can't.
    if (S.list.sync == LISTSYNC_LOADING && req == S.list.req) {
        // The list goes back to how it opened, with the new rows.
        S.list.sync = LISTSYNC_IDLE;
        S.list.idx = ROW_NONE;
        S.list.touched = false;
        S.list.scroll = 0;
        S.list.until = now + LIST_IDLE_US;
        ux_feedback(FB_TAP);
        return;
    }
    if (on_row) {
        int found = -1;
        for (int i = 0; kind == LIST_PROJECTS && i < S.n_projects; i++) {
            if (S.projects[i].id == was_project.id) found = i;
        }
        for (int i = 0; kind == LIST_TASKS && i < S.n_tasks; i++) {
            if (!strcmp(S.tasks[i].desc, was_task.desc) && S.tasks[i].project.id == was_task.project.id) found = i;
        }
        if (found >= 0) {
            S.list.idx = ROW_FIRST + found;
        } else { // it's gone: back to where a list opens, and a tap only closes it again
            S.list.idx = ROW_NONE;
            S.list.touched = false;
        }
        S.list.pos = (float)S.list.idx;
    }
    list_view_t v;
    build_view(&v);
    const float max = list_max_scroll(&v);
    if (S.list.scroll > max) S.list.scroll = max;
}

void picker_on_fetch_failed(link_list_t kind, int req, int64_t now)
{
    if (S.list.open && S.list.kind == kind && S.list.sync == LISTSYNC_LOADING && req == S.list.req) {
        S.list.sync = LISTSYNC_FAILED;
        S.list.until = now + LIST_IDLE_US;
        ux_bump(NULL);
    }
}

// A task the board starts or renames goes to the top of the cached list, with no request to Toggl.
void picker_remember_task(const char *desc, int64_t task_id, const link_project_t *project)
{
    if (!desc[0]) return;
    int found = -1;
    const int64_t pid = project && project->id > 0 ? project->id : 0;
    for (int i = 0; i < S.n_tasks; i++) {
        if (!strcmp(S.tasks[i].desc, desc) && (S.tasks[i].project.id > 0 ? S.tasks[i].project.id : 0) == pid) found = i;
    }
    if (found == 0) return;
    const int last = found >= 0 ? found : (S.n_tasks < LINK_MAX_TASKS ? S.n_tasks++ : LINK_MAX_TASKS - 1);
    memmove(&S.tasks[1], &S.tasks[0], (size_t)last * sizeof(link_task_t));
    memset(&S.tasks[0], 0, sizeof S.tasks[0]);
    utf8_copy(S.tasks[0].desc, sizeof S.tasks[0].desc, desc);
    S.tasks[0].task_id = task_id;
    if (project && project->id > 0) S.tasks[0].project = *project;
    store_save_list(LIST_TASKS);
}
