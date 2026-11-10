// Agent Zoo: one agent at a time, with dots for the others. With a clock running, the clock takes only the top arc or
// the top third. Hold for the details card.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"

#define AUTO_SCROLL_US (8 * SEC_US)
#define MANUAL_HOLD_US (20 * SEC_US)
#define CARD_US (10 * SEC_US)
#define OFFLINE_US (30 * SEC_US)
#define RESYNC_GAP_US (30 * SEC_US)
#define COUNT_GAP_US (3 * SEC_US)

static int mod_n(int v, int n) { return ((v % n) + n) % n; }

agent_t *zoo_first_needing(void)
{
    if (!S.n_needing) return NULL;
    for (int i = 0; i < S.n_agents; i++) {
        if (S.agents[i].a.st == AG_NEEDS) return &S.agents[i];
    }
    return NULL;
}

const lay_zoo_t *zoo_layout(void) { return clock_running() ? &F.L->zooClock : &F.L->zoo; }

// Heard from nobody for this long: say so. (A clock's status and the Bluetooth mark react at once.)
bool zoo_mac_offline(int64_t now) { return !S.connected && (S.offline_at_once || now - S.offline_since >= OFFLINE_US); }

static void select_agent(int i, bool manual, int64_t now)
{
    const int n = S.n_agents;
    if (!n) return;
    S.sel = mod_n(i, n);
    if (manual) {
        S.picked_at = now;
        ux_feedback(FB_TICK);
    }
}

// The agents' strip: a row on a square screen, along the rim on a round one. Up to a screenful, every agent has a dot and
// the ring moves between them. Past that it's endless: the chosen dot stays in the middle, the strip slides under it and
// wraps from the last agent to the first, and the dots shrink and fade towards the ends, where there are more.
#define STRIP_MAX 32

static bool strip_endless(void) { return S.n_agents > F.L->stripHalf * 2 + 1; }

static void strip_point(float d, float *x, float *y)
{
    const layout_t *L = F.L;
    if (L->hasDots) {
        *x = L->base / 2 + d * L->dotsGap;
        *y = L->dotsY;
    } else {
        rim_point(0.5f - d * L->stripStep, L->dotsRimInset, 0, x, y);
    }
}

// Which dots show now: which agent, and where along the strip. Its cost doesn't depend on how many agents there are.
static int strip_dots(int who[STRIP_MAX], float at[STRIP_MAX])
{
    const int n = S.n_agents, half = F.L->stripHalf;
    int count = 0;
    if (!strip_endless()) {
        for (int i = 0; i < n && count < STRIP_MAX; i++, count++) {
            who[count] = i;
            at[count] = (float)i - (float)(n - 1) / 2;
        }
        return count;
    }
    const int base = (int)floorf(S.pos + 0.5f);
    for (int k = -half - 1; k <= half + 1 && count < STRIP_MAX; k++) {
        const float d = (float)(base + k) - S.pos;
        if (fabsf(d) >= (float)half + 0.5f) continue;
        who[count] = mod_n(base + k, n);
        at[count++] = d;
    }
    return count;
}

static int others_needing(void)
{
    const int others = S.n_needing - (S.n_agents && S.agents[S.sel].a.st == AG_NEEDS ? 1 : 0);
    return others > 0 ? others : 0;
}

// "12 / 240", over the chosen dot.
static void draw_count(void)
{
    char text[24];
    snprintf(text, sizeof text, "%d / %d", S.sel + 1, S.n_agents);
    gfx_text(text, F.L->base / 2, F.L->countY, F.L->countSize, 600, F.T->sub, ALIGN_CENTER, 0, 255);
}

static void draw_strip(void)
{
    if (!S.n_agents) return;
    const theme_t *T = F.T;
    const float small = F.L->hasDots ? 5 : 3.2f, big = F.L->hasDots ? 7.5f : 5.5f, ring = F.L->hasDots ? 11 : 8;
    const float reach = (float)F.L->stripHalf + 0.5f;
    const bool endless = strip_endless();
    int who[STRIP_MAX];
    float at[STRIP_MAX];
    const int count = strip_dots(who, at);
    for (int i = 0; i < count; i++) {
        float x, y, fade = 0, near = who[i] == S.sel ? 1 : 0;
        strip_point(at[i], &x, &y);
        if (endless) {
            const float e = (fabsf(at[i]) / reach - 0.6f) / 0.4f;
            const float closeness = 1 - fabsf(at[i]);
            fade = e < 0 ? 0 : e > 1 ? 1 : e;
            near = closeness < 0 ? 0 : closeness;
        }
        gfx_disc(x, y, (small + (big - small) * near) * (1 - 0.6f * fade), T->st[S.agents[who[i]].a.st], (int)(255 * (1 - 0.8f * fade)));
    }
    float x, y;
    strip_point(endless ? 0 : (float)S.sel - (float)(S.n_agents - 1) / 2, &x, &y);
    gfx_ring(x, y, ring, 1.5f, T->ink, 255);
    draw_count();
}

static bool strip_tap(float u, float v, int64_t now)
{
    const layout_t *L = F.L;
    const lay_zoo_t *Z = zoo_layout();
    if (others_needing() && fabsf(v - (Z->lineY - Z->line * 0.35f)) < Z->line && fabsf(u - L->base / 2) < L->base * 0.3f) {
        for (int k = 1; k < S.n_agents; k++) {
            const int i = mod_n(S.sel + k, S.n_agents);
            if (S.agents[i].a.st == AG_NEEDS) {
                select_agent(i, true, now);
                return true;
            }
        }
    }
    const float limit = 22 * L->base / 360;
    float best_d = limit * limit;
    int best = -1, who[STRIP_MAX];
    float at[STRIP_MAX];
    const int count = strip_dots(who, at);
    for (int i = 0; i < count; i++) {
        float x, y;
        strip_point(at[i], &x, &y);
        const float d = (x - u) * (x - u) + (y - v) * (y - v);
        if (d < best_d) {
            best = who[i];
            best_d = d;
        }
    }
    if (best < 0) return false;
    select_agent(best, true, now);
    return true;
}

static void format_ago(int64_t us, char *out, size_t n)
{
    const int s = us < 0 ? 0 : (int)(us / SEC_US);
    if (s < 60) snprintf(out, n, "%d s", s);
    else if (s < 3600) snprintf(out, n, "%d min", s / 60);
    else snprintf(out, n, "%d h %d min", s / 3600, (s / 60) % 60);
}

static void draw_card(const agent_t *a)
{
    const lay_card_t *C = &F.L->card;
    const theme_t *T = F.T;
    const float base = F.L->base;
    gfx_clear(T->face);
    pose_t p = character_pose(a->a.st, F.t, S.sel);
    character_draw(a, &p, C->charX, C->charFeet, C->charSc);
    const gfx_align_t align = C->centered ? ALIGN_CENTER : ALIGN_LEFT;
    gfx_text(a->a.name, C->nameX, C->nameY, C->name, 700, T->ink, align, C->nameW, 255);
    static const char *const KINDS[3] = {"Claude", "Codex", "Grok"};
    char sub[80];
    if (a->a.proj[0]) snprintf(sub, sizeof sub, "%s \xC2\xB7 %s%s", a->a.proj, KINDS[a->a.kind], a->a.background ? " \xC2\xB7 background" : "");
    else snprintf(sub, sizeof sub, "%s%s", KINDS[a->a.kind], a->a.background ? " \xC2\xB7 background" : "");
    gfx_text(sub, C->nameX, C->subY, C->sub, 500, T->sub, align, C->nameW, 255);
    char state[64], d[24], started[40], subs[120] = "none";
    format_ago(F.now - a->since_us, d, sizeof d);
    static const char *const NAMES[5] = {"Working", "Needs you", "Done", "Idle", "Failed"};
    switch (a->a.st) {
    case AG_WORKING: snprintf(state, sizeof state, "%s, for %s", NAMES[a->a.st], d); break;
    case AG_NEEDS: snprintf(state, sizeof state, "%s, waiting %s", NAMES[a->a.st], d); break;
    case AG_DONE:
    case AG_FAILED: snprintf(state, sizeof state, "%s, %s ago", NAMES[a->a.st], d); break;
    default: snprintf(state, sizeof state, "%s, %s", NAMES[a->a.st], d); break;
    }
    format_ago(F.now - a->started_us, d, sizeof d);
    snprintf(started, sizeof started, "%s ago", d);
    if (a->a.nsubs) {
        size_t n = 0;
        subs[0] = 0;
        for (int i = 0; i < a->a.nsubs && i < 3 && n + 1 < sizeof subs; i++) {
            const int w = snprintf(subs + n, sizeof subs - n, "%s%s: %s", i ? " \xC2\xB7 " : "", a->a.subs[i].type, a->a.subs[i].act);
            if (w <= 0) break;
            n += (size_t)w;
            if (n >= sizeof subs) n = sizeof subs - 1;
        }
    }
    const char *const labels[6] = {"State", "Activity", "Notice", "Subagents", "Started", "Where"};
    const char *const values[6] = {state, a->a.act, a->a.note[0] ? a->a.note : "\xE2\x80\x94", subs, started, a->a.where[0] ? a->a.where : "\xE2\x80\x94"};
    for (int i = 0; i < 6; i++) {
        const float y = C->rowY + (float)i * C->rowH;
        gfx_text(labels[i], C->labelX, y, C->row, 500, T->sub, ALIGN_LEFT, 0, 255);
        gfx_text(values[i], C->valueX, y, C->row, 500, T->ink, ALIGN_LEFT, C->valueW, 255);
    }
    gfx_text("Tap to close", base / 2, C->hintY, C->hint, 500, T->sub, ALIGN_CENTER, 0, 255);
}

static void zoo_draw(void)
{
    const layout_t *L = F.L;
    const theme_t *T = F.T;
    const float base = L->base, mid = base / 2;
    const int64_t now = F.now;
    const int n = S.n_agents;
    const agent_t *active = n ? &S.agents[S.sel < n ? S.sel : 0] : NULL;
    if (S.card && active) {
        draw_card(active);
        return;
    }
    // An agent that has ended has left the list, and stays on screen, from a copy, until its effect is over.
    const agent_t *leaving = fx_leaving(), *shown = leaving ? leaving : active;
    const ux_mode_t *run = clock_running();
    const lay_zoo_t *Z = zoo_layout();
    const bool needs = shown && !leaving && shown->a.st == AG_NEEDS;
    gfx_clear(T->face);
    if (needs) rim_glow(rgb565(COLOR_AMBER), 0.55f + 0.45f * fabsf(sinf(F.t * 2.2f)), 0);
    if (run) clock_draw_arch(run);
    if (shown) {
        // One agent at a time. A newly chosen one slides in a little and fades up.
        float moving = 0;
        if (n > 1) {
            float d = (float)S.sel - S.pos + (float)n / 2;
            d -= floorf(d / (float)n) * (float)n;
            moving = d - (float)n / 2;
        }
        float x = mid + moving * Z->sc * 6;
        const float jump = needs ? fabsf(sinf(F.t * 4)) * Z->sc * 1.8f : shown->a.st == AG_DONE ? fabsf(sinf(F.t * 3)) * Z->sc : 0;
        pose_t p = character_pose(shown->a.st, F.t, S.sel);
        const float alpha = 1 - fabsf(moving) * 1.3f;
        p.alpha = alpha < 0 ? 0 : alpha > 1 ? 1 : alpha;
        float feet = Z->feet - jump;
        const bool in_air = fx_body(&p, &x, &feet, Z->sc);
        if (run && in_air) gfx_clip_rows(L->arch.bottom, base); // a hop or a drop stays out of the clock's section
        if (!fx_poofed()) character_draw(shown, &p, x, feet, Z->sc);
        if (needs) character_bubble(BUBBLE_QUESTION, x - Z->sc * 5, feet - Z->sc * 9.5f, rgb565(COLOR_AMBER), rgb565(0x1A1200), Z->sc / 7, 255);
        else fx_draw_bubble(x, feet, Z->sc);
        gfx_clip_reset();
        fx_draw_particles(Z, run ? L->arch.bottom : 0);
        const int text_a = fx_text_alpha();
        gfx_text(shown->a.name, mid, Z->nameY, Z->name, 700, T->ink, ALIGN_CENTER, Z->textW, text_a);
        const char *act = !leaving ? shown->a.act : leaving->a.st == AG_FAILED ? "Stopped with an error" : "All done!";
        gfx_text(act, mid, Z->actY, Z->act, 500, needs ? T->amber : T->soft, ALIGN_CENTER, Z->textW, text_a);
        if (!leaving && !zoo_mac_offline(now)) {
            // Under the agent: how long it's been at this, unless others need you. A tap on that goes to them.
            const int others = others_needing();
            const int m = (int)((float)(now - shown->since_us) / (float)MIN_US + 0.5f);
            char line[32] = "just now";
            if (others == 1) snprintf(line, sizeof line, "1 other needs you");
            else if (others) snprintf(line, sizeof line, "%d others need you", others);
            else if (m > 0) snprintf(line, sizeof line, "for %d min", m);
            gfx_text(line, mid, Z->lineY, Z->line, others ? 600 : 500, others ? T->amber : T->sub, ALIGN_CENTER, Z->textW, text_a);
        }
    } else {
        gfx_text("No agents running", mid, base * 0.55f, L->zoo.act, 500, T->sub, ALIGN_CENTER, 0, 255);
    }
    if (!run && L->hasDots) {
        char clock_str[8] = "--:--";
        daytime_t d;
        if (daytime_get(now, &d)) snprintf(clock_str, sizeof clock_str, "%02d:%02d", d.hour, d.minute);
        gfx_text(clock_str, mid, L->zoo.topY, L->zoo.top, 600, T->sub, ALIGN_CENTER, 0, 255);
    }
    draw_strip();
    if (zoo_mac_offline(now)) {
        const float size = L->toast;
        const char *label = "Mac offline";
        const float w = gfx_text_width(label, size, 600) + size * 2, y = Z->lineY;
        gfx_rrect(mid - w / 2, y - size * 1.2f, w, size * 1.9f, size, T->offlineBg, 255);
        gfx_text(label, mid, y + size * 0.3f, size, 600, T->amber, ALIGN_CENTER, 0, 255);
    }
}

static void zoo_turn(int dir, int64_t now)
{
    if (!S.n_agents) {
        ux_feedback(FB_BUMP);
        return;
    }
    // Turning fast moves further, so that hundreds of agents are a spin away and the next one is still one click.
    int step = 1;
    if (strip_endless()) {
        S.knob_rate = S.knob_rate * expf(-(float)(now - S.knob_at) / 300000.0f) + 1;
        step = S.knob_rate >= 15 ? 8 : S.knob_rate >= 8 ? 4 : S.knob_rate >= 4 ? 2 : 1;
    }
    S.knob_at = now;
    select_agent(S.sel + dir * step, true, now);
}

static void zoo_tap(float u, float v, int64_t now)
{
    const layout_t *L = F.L;
    const ux_mode_t *run = clock_running();
    if (run && v < L->arch.bottom) { // the clock's section: back to its face
        S.mode = run;
        ux_mark_dirty(now);
        ux_feedback(FB_TAP);
        ux_log("mode", run->key);
        return;
    }
    if (strip_tap(u, v, now)) return;
    if (!S.n_agents || fx_leaving()) return; // the agent that's going can't be reached, and the next isn't showing yet
    const agent_t *a = &S.agents[S.sel];
    fx_on_tap(a->a.pane);
    if (!a->a.pane) {
        ux_toast(a->a.background ? "No terminal (background agent)" : "No terminal known yet", 1600);
        ux_feedback(FB_BUMP);
        return;
    }
    const bool sent = link_send_tap(a->a.id);
    ux_toast(sent ? "Opening its terminal" : "No Mac", 1600);
    ux_feedback(sent ? FB_TAP : FB_BUMP);
}

static void zoo_hold(int64_t now)
{
    if (fx_leaving()) return;
    if (!S.n_agents) {
        ux_feedback(FB_BUMP);
        return;
    }
    S.card = true;
    S.card_until = now + CARD_US;
    ux_feedback(FB_TICK);
    ux_log("card", "open");
}

static void zoo_swipe(swipe_t dir, int64_t now)
{
    if (dir == SWIPE_DOWN) return;
    if (!S.n_agents) {
        ux_feedback(FB_BUMP);
        return;
    }
    select_agent(S.sel + (dir == SWIPE_LEFT ? 1 : -1), true, now);
}

// Past a screenful, dragging sideways scrubs through the agents, and a flick carries on by itself. With fewer, a swipe
// is simply the next one.
bool zoo_scrub_wanted(void)
{
    return S.mode == &MODE_ZOO && !S.lock.open && !S.menu.open && !S.list.open && !S.card && strip_endless();
}

void zoo_scrub(int dir, int64_t now) { select_agent(S.sel + dir, true, now); }

void zoo_fling(float units_per_ms)
{
    if (fabsf(units_per_ms) <= 0.5f) return;
    S.fling_v = -units_per_ms * 1000.0f / ZOO_SCRUB_UNITS;
    S.fling_acc = 0;
}

bool zoo_stop_fling(void)
{
    const bool was = S.fling_v != 0;
    S.fling_v = 0;
    return was;
}

void zoo_tick(int64_t now)
{
    const int n = S.n_agents;
    if (S.fling_v != 0) {
        S.fling_acc += S.fling_v * F.dt_ms / 1000.0f;
        while (S.fling_acc >= 1 && n) {
            S.fling_acc -= 1;
            S.sel = mod_n(S.sel + 1, n);
        }
        while (S.fling_acc <= -1 && n) {
            S.fling_acc += 1;
            S.sel = mod_n(S.sel - 1, n);
        }
        S.picked_at = now;
        S.fling_v *= expf(-F.dt_ms / 500.0f);
        if (fabsf(S.fling_v) < 3 || !zoo_scrub_wanted()) {
            S.fling_v = 0;
            ux_feedback(FB_TICK);
        }
    }
    if (S.card && (now >= S.card_until || !n)) S.card = false;
    if (n) {
        const float ease = 1 - expf(-F.dt_ms / 70);
        float d = (float)S.sel - S.pos + (float)n / 2;
        d -= floorf(d / (float)n) * (float)n;
        d -= (float)n / 2;
        S.pos = fabsf(d) < 0.002f ? (float)S.sel : S.pos + d * ease;
    }
    // Auto-scroll: the next agent every 8 s; a manual pick holds for 20 s; agents that need you come first.
    if (S.mode == &MODE_ZOO && !S.menu.open && !S.card && now - S.picked_at > MANUAL_HOLD_US && n && !fx_leaving()) {
        const agent_t *need = zoo_first_needing();
        if (need && S.agents[S.sel].a.st != AG_NEEDS) {
            S.sel = (int)(need - S.agents);
        } else if (S.autoscroll && n > 1) {
            if (now > S.next_scroll) {
                if (S.next_scroll) S.sel = mod_n(S.sel + 1, n);
                S.next_scroll = now + AUTO_SCROLL_US;
            }
        }
    }
}

// ---- The agents, as they arrive -----------------------------------------------------------------------------------------

static char s_new_needs[12]; // the first agent in this message who newly needs you

static int find_agent(const char *id)
{
    for (int i = 0; i < S.n_agents; i++) {
        if (!strcmp(S.agents[i].a.id, id)) return i;
    }
    return -1;
}

// The Mac says how many it has. A different number here is normal while rows are on their way, and the Mac sends a
// `state` whenever its count moves, so several can come at once. So it only counts if nothing about agents has arrived
// between two of them, seconds apart. Then something was lost: ask for the whole list.
void zoo_on_count(int n, int64_t now)
{
    if (n < 0) return;
    const int want = n > LINK_MAX_AGENTS ? LINK_MAX_AGENTS : n;
    const bool arriving = S.agents_syncing && now - S.sync_begun < RESYNC_GAP_US;
    if (want == S.n_agents || arriving) {
        S.count_off = 0;
        return;
    }
    if (S.count_off && now - S.count_off_at < COUNT_GAP_US) return;
    S.count_off_at = now;
    if (++S.count_off >= 2 && (!S.resync_at || now - S.resync_at >= RESYNC_GAP_US)) {
        S.resync_at = now;
        S.count_off = 0;
        link_send_resync();
    }
}

void zoo_agents_begin(int64_t now)
{
    for (int i = 0; i < S.n_agents; i++) S.agents[i].seen = false;
    S.agents_syncing = true;
    S.sync_begun = now;
}

// The same agent stays chosen, if it's still there. If it's gone, the next one takes its place.
static void after_removal(int removed_below_sel, bool sel_removed)
{
    S.sel -= removed_below_sel;
    if (S.sel >= S.n_agents) S.sel = S.n_agents ? S.n_agents - 1 : 0;
    if (removed_below_sel || sel_removed) S.pos = (float)S.sel;
}

// Whoever the whole list hasn't named is gone, all at once: one pass, however many there are.
static void remove_unseen(void)
{
    int kept = 0, below = 0;
    bool sel_removed = false;
    if (S.n_agents && !S.agents[S.sel].seen) fx_on_gone(&S.agents[S.sel]);
    for (int i = 0; i < S.n_agents; i++) {
        if (S.agents[i].seen) {
            if (kept != i) S.agents[kept] = S.agents[i];
            kept++;
        } else if (i < S.sel) {
            below++;
        } else if (i == S.sel) {
            sel_removed = true;
        }
    }
    S.n_agents = kept;
    after_removal(below, sel_removed);
    fx_after_removal();
}

void zoo_agents_row(const link_agent_t *r, int64_t now)
{
    int i = find_agent(r->id);
    const bool known = i >= 0;
    if (!known) {
        // Old agents mustn't take up room the new list needs. One it names later comes back at the end.
        if (S.n_agents >= LINK_MAX_AGENTS && S.agents_syncing) remove_unseen();
        if (S.n_agents >= LINK_MAX_AGENTS) return;
        i = S.n_agents++;
    }
    agent_t *a = &S.agents[i];
    const bool was_needing = known && a->a.st == AG_NEEDS;
    const int was_st = known ? (int)a->a.st : -1;
    const bubble_t was_icon = i == S.sel ? fx_activity_icon(a->a.act) : BUBBLE_THINK;
    a->a = *r;
    a->since_us = now - (int64_t)r->age_s * SEC_US;
    a->started_us = now - (int64_t)r->up_s * SEC_US;
    a->seen = true;
    character_costume(a);
    if (i == S.sel) fx_on_row(a, was_st, was_icon);
    if (r->st == AG_NEEDS && !was_needing && !s_new_needs[0]) memcpy(s_new_needs, r->id, sizeof s_new_needs);
}

void zoo_agents_gone(const char *id)
{
    const int i = find_agent(id);
    if (i < 0) return;
    if (i == S.sel) fx_on_gone(&S.agents[i]);
    memmove(&S.agents[i], &S.agents[i + 1], (size_t)(S.n_agents - i - 1) * sizeof(agent_t));
    S.n_agents--;
    after_removal(i < S.sel ? 1 : 0, i == S.sel);
    fx_after_removal();
}

void zoo_agents_end(bool whole, int64_t now)
{
    if (whole) {
        remove_unseen();
        S.agents_syncing = false;
    }
    int needing = 0;
    for (int i = 0; i < S.n_agents; i++) {
        if (S.agents[i].a.st == AG_NEEDS) needing++;
    }
    S.n_needing = needing;
    S.count_off = 0; // rows are still coming, so nothing's lost yet
    if (!S.n_agents) S.card = false;
    if (s_new_needs[0]) {
        const int i = find_agent(s_new_needs);
        s_new_needs[0] = 0;
        if (i >= 0) {
            ux_feedback(FB_ALERT);
            ux_log("needs_you", "");
            if (S.mode == &MODE_ZOO && !S.menu.open) S.sel = i;
        }
    }
    (void)now;
}

const ux_mode_t MODE_ZOO = {
    .key = "zoo", .label = "Agents", .color = 0x8B5CF6,
    .draw = zoo_draw,
    .turn = zoo_turn, .tap = zoo_tap, .hold = zoo_hold, .swipe = zoo_swipe,
    .status = WHERE_ZOO,
};
