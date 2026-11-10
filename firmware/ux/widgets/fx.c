// The effects around the agent on screen: confetti, smoke, dust, sparkles and floating Z's, what its body does (a hop, a
// drop, a squash, a shake) and its speech bubble. Only that agent plays any, and only on the Agents screen: there can be
// 512 agents, and the dots show the others.
//
// Everything moves once a frame in fx_tick. The draw functions change nothing, as the screen is drawn at least twice a
// frame. Positions are in mascot pixels from the character's feet, so the effects scale with the character and the layout.
// The numbers are the preview's, and came from Agent Zoo, whose particles were in pixels at 6 per mascot pixel.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"

// Small blocks only, and few: the knob's frame budget has no room for more. An event's own count stays well under the
// pool, and add() refuses whatever doesn't fit.
#define FX_MAX 48
#define CONFETTI_N 24
#define SMOKE_N 14
#define DUST_LAND 6
#define DUST_HOP 4

#define LEAVE_POOF_S 0.38f
#define LEAVE_END_S 1.5f
#define HOP_DONE 40.0f
#define HOP_GONE 33.3f
#define SHAKE_S 0.45f
#define BUBBLE_FADE_S 0.25f
#define BUBBLE_DONE_S 2.2f
#define BUBBLE_WORKING_S 2.6f
#define BUBBLE_TAP_S 1.1f

typedef enum { P_DUST, P_CONFETTI, P_SMOKE, P_WISP, P_SPARKLE, P_Z, P_RING, P_HAT, P_COUNT } kind_t;
typedef enum { FX_NONE, FX_DONE, FX_GONE, FX_NEW, FX_IDLE, FX_WORKING, FX_FAILED, FX_TAP, FX_EVENTS } fx_event_t;
static const char *const EVENT_NAMES[FX_EVENTS] = {"", "done", "gone", "new", "idle", "working", "failed", "tap"};

static const struct { float g, drag; } PHYS[P_COUNT] = {
    [P_DUST] = {26.7f, 0}, [P_CONFETTI] = {120, 0.6f}, [P_SMOKE] = {-3, 2.6f}, [P_WISP] = {-0.5f, 0}, [P_HAT] = {150, 0},
};

typedef struct {
    float x, y, vx, vy; // mascot pixels, and per second; y goes down
    float age, rate;    // rate: 1 / its life
    float size;         // or how fast it tumbles
    uint16_t c;
    uint8_t kind;
} particle_t;

static struct {
    particle_t p[FX_MAX];
    int n;
    char id[sizeof(((link_agent_t *)0)->id)]; // the agent these effects belong to
    agent_status_t st;
    float next_fx;
    float h, v;        // a hop: height and speed, up
    bool air;
    float sy, svy;     // squash and stretch
    bool dropping;
    float drop_y, drop_v;
    float shake, shake_age;
    float text_a;
    int icon;          // bubble_t, or -1
    float em_age, em_left; // em_left < 0: it stays
    // The agent that's going: the list has already moved on, so it's drawn from this copy.
    bool leaving, poofed, drop_next;
    float leave_t;
    agent_t copy;
    char after[sizeof(((link_agent_t *)0)->id)]; // who the list has chosen meanwhile; anyone else means someone switched
} X = {.sy = 1, .text_a = 1, .icon = -1};

static int s_peak, s_refused;
static fx_event_t s_last;
#ifdef BENTO_DEBUG_INTERFACE
static uint32_t s_seed; // a test's fixed sequence, so that two builds can draw the same frame
static bool s_quiet;    // the stand-in comes and goes with no effect of its own
#endif

static uint32_t next_random(void)
{
#ifdef BENTO_DEBUG_INTERFACE
    if (s_seed) {
        s_seed ^= s_seed << 13;
        s_seed ^= s_seed >> 17;
        s_seed ^= s_seed << 5;
        return s_seed;
    }
#endif
    return hal_random();
}

static float rnd(float a, float b) { return a + (b - a) * (float)(next_random() & 0xFFFF) * (1.0f / 65535.0f); }

// The Agents screen, with nothing over it. A locked board shows it too, until the keypad comes up.
static bool visible(void)
{
#ifdef BENTO_DEBUG_INTERFACE
    if (s_quiet) return false;
#endif
    return S.mode == &MODE_ZOO && !S.menu.open && !S.list.open && !S.card && !(S.lock.open && S.lock.pad);
}

static const agent_t *on_screen(void) { return S.n_agents ? &S.agents[S.sel] : NULL; }

// ---- Particles ------------------------------------------------------------------------------------------------------

static void add(kind_t kind, float x, float y, float vx, float vy, float life, float size, uint32_t rgb)
{
    if (X.n >= FX_MAX) {
        s_refused++;
        return;
    }
    X.p[X.n++] = (particle_t){.x = x, .y = y, .vx = vx, .vy = vy, .rate = 1 / life, .size = size, .c = rgb565(rgb), .kind = (uint8_t)kind};
    if (X.n > s_peak) s_peak = X.n;
}

static void dust(int n)
{
    for (int i = 0; i < n; i++) {
        const float d = next_random() & 1 ? 1.0f : -1.0f;
        add(P_DUST, d * rnd(1.7f, 6.7f), -rnd(0, 0.4f), d * rnd(5, 15), -rnd(3.3f, 11.7f), rnd(0.35f, 0.55f), rnd(0.5f, 0.85f), 0x6B6A66);
    }
}

static void confetti(void)
{
    static const uint32_t COLORS[7] = {0xF2C14E, 0xE5484D, 0x3B82F6, 0x22C55E, 0xF472B6, 0x22D3EE, 0xFFFFFF};
    for (int i = 0; i < CONFETTI_N; i++) {
        const uint32_t pick = next_random() % 7, rgb = pick == 6 && S.theme ? 0x2A2825 : COLORS[pick];
        add(P_CONFETTI, rnd(-1.7f, 1.7f), -6.7f, rnd(-25, 25), -rnd(36.7f, 66.7f), rnd(1.1f, 1.6f), rnd(4, 10), rgb);
    }
}

static void sparkle(void) { add(P_SPARKLE, rnd(-10, 10), -rnd(6.7f, 15), 0, -1, 0.6f, 0, S.theme ? 0x1F8A50 : 0xB8F5C8); }

static void floating_z(void) { add(P_Z, 6.7f, -11.7f, 1, -3.7f, 2.4f, 0, S.theme ? 0x66666B : 0xC9C7C1); }

static void wisp(void)
{
    add(P_WISP, rnd(-3.3f, 3.3f), -11, rnd(-1.3f, 1.3f), -rnd(4.2f, 7.5f), 1.3f, rnd(0.8f, 1.3f), next_random() & 1 ? 0x6E6E73 : 0x8E8E94);
}

static void poof(void)
{
    static const uint32_t WHITE[3] = {0xFFFFFF, 0xF4F2EC, 0xE3E1DB}, GRAY[3] = {0x8E8E94, 0x6E6E73, 0xA1A1A6};
    const bool grey = X.copy.a.st == AG_FAILED || S.theme;
    const float cy = -5.7f - X.h + X.drop_y;
    for (int i = 0; i < SMOKE_N; i++) {
        const float ang = rnd(0, 6.2832f), sp = rnd(8.3f, 28.3f), cs = cosf(ang), sn = sinf(ang);
        add(P_SMOKE, cs * 1.3f, cy + sn * 1.3f, cs * sp, sn * sp - 2, rnd(0.6f, 1.0f), rnd(1.2f, 2.5f), (grey ? GRAY : WHITE)[next_random() % 3]);
    }
    add(P_RING, 0, cy, 0, 0, 0.3f, 9.7f, X.copy.a.st == AG_FAILED ? 0xE5484D : S.theme ? 0x2A2825 : 0xFFFFFF);
    // Its position is the feet the hat would sit on, so it starts where it was.
    add(P_HAT, 0, -X.h + X.drop_y, rnd(-11.7f, 11.7f), -rnd(43.3f, 56.7f), 2.2f, rnd(3, 6), 0);
    X.poofed = true;
    X.dropping = false;
    X.air = false;
    X.icon = -1;
    ux_feedback(FB_POOF);
}

// ---- The body and the bubble ------------------------------------------------------------------------------------------

static void bubble(bubble_t icon, float seconds)
{
    X.icon = (int)icon;
    X.em_age = 0;
    X.em_left = seconds < 0 ? -1 : seconds + BUBBLE_FADE_S;
}

static void hop(float v)
{
    if (X.air) return;
    X.air = true;
    X.v = v;
    X.sy = 1.15f;
    X.svy = 0;
}

static void squash(float sy)
{
    X.sy = sy;
    X.svy = 0;
}

bubble_t fx_activity_icon(const char *act)
{
    if (strstr(act, "subagent")) return BUBBLE_SUB;
    if (!strncmp(act, "Bash", 4) || !strncmp(act, "Running a command", 17)) return BUBBLE_BASH;
    if (!strncmp(act, "Editing", 7) || !strncmp(act, "Writing", 7)) return BUBBLE_EDIT;
    if (!strncmp(act, "Reading", 7)) return BUBBLE_READ;
    if (strstr(act, "web") || !strncmp(act, "Fetching", 8)) return BUBBLE_WEB;
    if (!strncmp(act, "Search", 6)) return BUBBLE_SEARCH;
    return BUBBLE_THINK;
}

// ---- Who's on screen -------------------------------------------------------------------------------------------------

static void stop_all(void)
{
    X.n = 0;
    X.next_fx = 0;
    X.h = X.v = 0;
    X.air = false;
    X.sy = 1;
    X.svy = 0;
    X.dropping = false;
    X.drop_y = X.drop_v = 0;
    X.shake = 0;
    X.text_a = 1;
    X.icon = -1;
    X.leaving = X.poofed = X.drop_next = false;
    X.id[0] = 0;
}

// Whoever is on screen now, with nothing carried over: switching to an agent doesn't replay what happened to it.
static void follow(void)
{
    const agent_t *a = on_screen();
    const char *id = a ? a->a.id : "";
    if (X.leaving) {
        if (!strcmp(id, X.after)) return;
        stop_all(); // someone turned to another agent
    }
    if (!strcmp(id, X.id)) return;
    stop_all();
    if (!a) return;
    memcpy(X.id, id, sizeof X.id);
    X.st = a->a.st;
    X.next_fx = rnd(0.5f, 1.5f);
    if (a->a.st == AG_FAILED) {
        bubble(BUBBLE_X, -1); // it stays, so it's already there
        X.em_age = 1;
    }
}

static void log_event(fx_event_t ev)
{
    s_last = ev;
    ux_log("fx", EVENT_NAMES[ev]);
}

// A state it has just entered. known: it was here before, rather than arriving like this.
static void enter_state(const agent_t *a, agent_status_t st, bool known)
{
    X.st = st;
    switch (st) {
    case AG_NEEDS: // its own bubble and jump take over, and its glow repaints every band, so nothing adds to that
        X.n = 0;
        X.icon = -1;
        break;
    case AG_DONE:
        if (known) {
            log_event(FX_DONE);
            confetti();
            ux_feedback(FB_YAY);
            hop(HOP_DONE);
        }
        bubble(BUBBLE_CHECK, BUBBLE_DONE_S);
        break;
    case AG_FAILED:
        if (known) log_event(FX_FAILED);
        bubble(BUBBLE_X, -1);
        squash(0.75f);
        break;
    case AG_IDLE:
        if (known) log_event(FX_IDLE);
        X.icon = -1;
        X.next_fx = 0.4f;
        break;
    case AG_WORKING:
        X.icon = -1;
        if (known) {
            log_event(FX_WORKING);
            bubble(fx_activity_icon(a->a.act), BUBBLE_WORKING_S);
        }
        break;
    }
}

static void start_drop(const agent_t *a)
{
    log_event(FX_NEW);
    X.dropping = true;
    X.drop_y = -rnd(50, 60);
    X.drop_v = 0;
    X.text_a = 0;
    ux_feedback(FB_DROP);
    enter_state(a, a->a.st, false);
}

static void start_leaving(const agent_t *a)
{
    log_event(FX_GONE);
    X.copy = *a;
    X.leaving = true;
    X.poofed = false;
    X.drop_next = false;
    X.leave_t = 0;
    X.icon = -1;
    hop(HOP_GONE);
}

void fx_on_row(const agent_t *a, int was_st, bubble_t was_icon)
{
    if (!visible()) return;
    if (X.leaving) {
        // The last one is going and another arrives: it takes the place, and drops in when the smoke clears.
        if (was_st < 0) {
            memcpy(X.after, a->a.id, sizeof X.after);
            X.drop_next = !S.agents_syncing;
        }
        return;
    }
    follow();
    if (was_st < 0) {
        // A whole list isn't news: it says who's there.
        if (!S.agents_syncing) start_drop(a);
        return;
    }
    if ((int)a->a.st != was_st) {
        enter_state(a, a->a.st, true);
    } else if (a->a.st == AG_WORKING) {
        const bubble_t icon = fx_activity_icon(a->a.act);
        if (icon != was_icon) bubble(icon, BUBBLE_WORKING_S);
    }
}

void fx_on_gone(const agent_t *a)
{
    if (!visible() || X.leaving) return; // while one is going, the next isn't on screen yet
    follow();
    start_leaving(a);
}

void fx_after_removal(void)
{
    if (!X.leaving) return;
    const agent_t *a = on_screen();
    memcpy(X.after, a ? a->a.id : "", a ? sizeof X.after : 1);
}

void fx_on_tap(bool pane)
{
    if (!visible() || X.leaving) return;
    follow();
    log_event(FX_TAP);
    X.shake = SHAKE_S;
    X.shake_age = 0;
    const bool stays = X.icon >= 0 && X.em_left < 0;
    if (X.st != AG_NEEDS && !stays) bubble(pane ? BUBBLE_ARROW : BUBBLE_QUESTION, BUBBLE_TAP_S);
}

const agent_t *fx_leaving(void) { return X.leaving ? &X.copy : NULL; }

bool fx_poofed(void) { return X.leaving && X.poofed; }

// ---- A frame --------------------------------------------------------------------------------------------------------

static void update_particles(float dt)
{
    const lay_zoo_t *Z = zoo_layout();
    const float below = (Z->nameY - Z->name - Z->feet) / Z->sc, side = F.L->base / 2 / Z->sc + 3;
    for (int i = X.n - 1; i >= 0; i--) {
        particle_t *q = &X.p[i];
        q->age += dt;
        bool out = q->age * q->rate >= 1;
        const float g = PHYS[q->kind].g, drag = PHYS[q->kind].drag;
        q->vy += g * dt;
        if (drag > 0) {
            const float d = 1 - drag * dt;
            q->vx *= d;
            q->vy *= d;
        }
        q->x += q->vx * dt;
        q->y += q->vy * dt;
        // Past the clip nothing shows, so it needn't wait for the end of its life. A hat's y is its feet, 18 rows under it.
        if (q->vy > 0 && q->y > (q->kind == P_HAT ? below + 18 : below)) out = true;
        if (q->x > side || q->x < -side) out = true;
        if (out) X.p[i] = X.p[--X.n];
    }
}

static void ambient(float dt)
{
    X.next_fx -= dt;
    if (X.next_fx > 0 || X.dropping) return;
    switch (X.st) {
    case AG_IDLE:
        floating_z();
        X.next_fx = 1.4f;
        break;
    case AG_FAILED:
        wisp();
        X.next_fx = 0.35f;
        break;
    case AG_WORKING:
    case AG_DONE:
        sparkle();
        X.next_fx = rnd(0.8f, 1.6f);
        break;
    default:
        X.next_fx = 0.5f;
        break;
    }
}

static void finish_leaving(void)
{
    const bool drop = X.drop_next;
    stop_all();
    follow();
    const agent_t *a = on_screen();
    if (drop && a) {
        start_drop(a);
    } else if (a) {
        S.pos = (float)S.sel - 0.5f; // it slides in a little, as a newly chosen agent does
    }
}

static void step(float dt)
{
    if (X.dropping) {
        X.drop_v += 383 * dt;
        X.drop_y += X.drop_v * dt;
        if (X.drop_y >= 0) {
            X.drop_y = 0;
            X.dropping = false;
            squash(0.7f);
            dust(DUST_LAND);
            ux_feedback(FB_LAND);
        }
    }
    if (X.air) {
        X.v -= 250 * dt;
        X.h += X.v * dt;
        if (X.h <= 0) {
            X.h = 0;
            X.air = false;
            squash(0.78f);
            dust(DUST_HOP);
        }
    }
    if (X.sy != 1 || X.svy != 0) {
        X.svy += (-220 * (X.sy - 1) - 15 * X.svy) * dt;
        X.sy += X.svy * dt;
        if (fabsf(X.sy - 1) < 0.002f && fabsf(X.svy) < 0.02f) {
            X.sy = 1;
            X.svy = 0;
        }
    }
    if (X.shake > 0) {
        X.shake -= dt;
        X.shake_age += dt;
    }
    if (!X.dropping && X.text_a < 1) {
        X.text_a += dt * 4;
        if (X.text_a > 1) X.text_a = 1;
    }
    if (X.icon >= 0) {
        X.em_age += dt;
        if (X.em_left >= 0) {
            X.em_left -= dt;
            if (X.em_left <= 0) X.icon = -1;
        }
    }
    if (X.leaving) {
        X.leave_t += dt;
        if (!X.poofed && X.leave_t >= LEAVE_POOF_S) poof();
        if (X.leave_t >= LEAVE_END_S) {
            finish_leaving();
            return;
        }
    } else if (X.id[0]) {
        ambient(dt);
    }
    update_particles(dt);
}

void fx_tick(void)
{
    if (!visible()) {
        if (X.id[0] || X.leaving || X.n) stop_all();
        return;
    }
    follow();
    if (!S.has_anim_override) step(F.dt_ms / 1000.0f);
}

// ---- Drawing: these only read ---------------------------------------------------------------------------------------

bool fx_body(pose_t *p, float *x, float *feet, float sc)
{
    p->sy *= X.sy;
    p->sx *= 1 + (1 - X.sy) * 0.85f;
    if (X.sy != 1 || X.air) {
        const float lag = X.air ? X.v * -0.06f : 0;
        p->hat_dy += (lag < -1.5f ? -1.5f : lag > 1.5f ? 1.5f : lag) + (1 - X.sy) * 3;
    }
    if (X.leaving) {
        p->eyes = 2;
        p->arm_l = p->arm_r = -2;
    }
    if (X.shake > 0) *x += sinf(X.shake_age * 60) * 0.83f * (X.shake / SHAKE_S) * sc;
    *feet += (X.drop_y - X.h) * sc;
    return X.dropping || X.air;
}

// In a few steps: text that fades every frame would repaint its rows every frame.
int fx_text_alpha(void)
{
    float a = X.text_a;
    if (X.leaving) {
        const float end = LEAVE_END_S - X.leave_t;
        a *= end < 0 ? 0 : end > 1 ? 1 : end;
    }
    const int v = (int)(a * 8 + 0.5f) * 32;
    return v > 255 ? 255 : v;
}

// To the 32 levels that painting can show, so that a slow fade doesn't repaint a band for nothing.
static int fade(float v)
{
    const int a = v <= 0 ? 0 : v >= 1 ? 255 : (int)(v * 32) * 8;
    return a;
}

void fx_draw_bubble(float x, float feet, float sc)
{
    if (X.icon < 0) return;
    // Pops up, overshooting a little.
    const float k = X.em_age >= 0.22f ? 1 : X.em_age * 4.545f, e = k - 1;
    const float pop = 1 + 2.70158f * e * e * e + 1.70158f * e * e;
    uint16_t fill, ink;
    switch (X.icon) {
    case BUBBLE_CHECK: fill = rgb565(0x3FB950); ink = 0xFFFF; break;
    case BUBBLE_X: fill = rgb565(0xE5484D); ink = 0xFFFF; break;
    case BUBBLE_QUESTION: fill = rgb565(COLOR_AMBER); ink = rgb565(0x1A1200); break;
    default: fill = rgb565(S.theme ? 0x2A2825 : 0xF4F2EC); ink = rgb565(S.theme ? 0xF4F2EC : 0x1A1A1A); break;
    }
    const int alpha = X.em_left < 0 ? 255 : fade(X.em_left / BUBBLE_FADE_S);
    character_bubble((bubble_t)X.icon, x - sc * 5, feet - sc * 9.5f, fill, ink, sc / 7 * pop, alpha);
}

static inline void block(float x, float y, float w, float h, uint16_t c, int alpha) { gfx_block(x, y, x + w, y + h, c, alpha); }

static inline float at_least_1(float v) { return v < 1 ? 1 : v; }

static void draw_ring(float x, float y, float life, const particle_t *q, float sc)
{
    static const float UNIT[16][2] = {{1, 0}, {0.924f, 0.383f}, {0.707f, 0.707f}, {0.383f, 0.924f}, {0, 1}, {-0.383f, 0.924f}, {-0.707f, 0.707f}, {-0.924f, 0.383f},
                                      {-1, 0}, {-0.924f, -0.383f}, {-0.707f, -0.707f}, {-0.383f, -0.924f}, {0, -1}, {0.383f, -0.924f}, {0.707f, -0.707f}, {0.924f, -0.383f}};
    const float r = q->size * (1.2f - 0.95f * life) * sc, s = at_least_1((0.2f + 0.6f * life) * sc);
    const int a = fade(life * 1.5f);
    for (int k = 0; k < 16; k++) block(x + UNIT[k][0] * r - s / 2, y + UNIT[k][1] * r - s / 2, s, s, q->c, a);
}

static void draw_z(float x, float y, float life, const particle_t *q, float sc)
{
    const float s = at_least_1((0.33f + 0.27f * q->age) * sc), zx = x + sinf(q->age * 3) * 0.67f * sc;
    const float appear = q->age * 3;
    const int a = fade((appear > 1 ? 1 : appear) * life);
    block(zx, y, s * 4, s, q->c, a);
    block(zx + 2 * s, y + s, s, s, q->c, a);
    block(zx + s, y + 2 * s, s, s, q->c, a);
    block(zx, y + 3 * s, s * 4, s, q->c, a);
}

// Each is kept to the rows it touches, between the clock's section (top) and the agent's name.
void fx_draw_particles(const lay_zoo_t *Z, float top)
{
    if (!X.n) return;
    const float sc = Z->sc, ax = F.L->base / 2, ay = Z->feet;
    gfx_clip_rows(top, Z->nameY - Z->name);
    for (int i = 0; i < X.n; i++) {
        const particle_t *q = &X.p[i];
        const float x = ax + q->x * sc, y = ay + q->y * sc, life = 1 - q->age * q->rate;
        switch (q->kind) {
        case P_DUST:
        case P_SMOKE:
        case P_WISP: {
            const float s = at_least_1(q->size * (0.4f + 0.6f * life) * sc);
            block(x - s / 2, y - s / 2, s, s, q->c, fade(life * 1.6f));
            break;
        }
        case P_CONFETTI: {
            // Two shapes, as if it were turning: no rotation per pixel.
            const float w = at_least_1(((int)(q->age * q->size) & 1 ? 0.2f : 0.6f) * sc), h = at_least_1(0.8f * sc);
            block(x - w / 2, y - h / 2, w, h, q->c, fade(life * 2));
            break;
        }
        case P_SPARKLE: {
            const float len = at_least_1(1.2f * sc), thick = at_least_1(0.4f * sc);
            const int a = fade(life);
            block(x - thick / 2, y - len / 2, thick, len, q->c, a);
            if ((int)(q->age * 14) % 2 == 0) block(x - len / 2, y - thick / 2, len, thick, q->c, a);
            break;
        }
        case P_Z:
            draw_z(x, y, life, q, sc);
            break;
        case P_RING:
            draw_ring(x, y, life, q, sc);
            break;
        case P_HAT:
            if (X.leaving) character_draw_hat(&X.copy, (int)(q->age * q->size) & 3, x, y, sc);
            break;
        default:
            break;
        }
    }
    gfx_clip_reset();
}

// ---- The debug interface ----------------------------------------------------------------------------------------------

#ifdef BENTO_DEBUG_INTERFACE
void fx_debug_read(fx_debug_t *out)
{
    static const char *const KINDS[P_COUNT] = {"dust", "confetti", "smoke", "wisp", "sparkle", "z", "ring", "hat"};
    static const char *const BUBBLES[BUBBLE_COUNT] = {"question", "check", "x", "arrow", "bash", "edit", "read", "web", "search", "think", "sub"};
    _Static_assert(P_COUNT == FX_KINDS, "STATE lists every kind");
    memset(out, 0, sizeof *out);
    for (int i = 0; i < P_COUNT; i++) out->kinds[i] = KINDS[i];
    for (int i = 0; i < X.n; i++) out->counts[X.p[i].kind]++;
    out->particles = X.n;
    out->peak = s_peak;
    out->cap = FX_MAX;
    out->refused = s_refused;
    out->bubble = X.icon >= 0 ? BUBBLES[X.icon] : "";
    out->last = EVENT_NAMES[s_last];
    out->leaving_name = X.leaving ? X.copy.a.name : "";
    out->leaving = X.leaving;
    out->poofed = X.leaving && X.poofed;
    out->dropping = X.dropping;
    out->hopping = X.air;
    out->shaking = X.shake > 0;
    out->squash = X.sy;
}

// With no agent, one stands in for it, so that an effect needs no Mac.
static void stand_in(int64_t now)
{
    link_agent_t r;
    memset(&r, 0, sizeof r);
    snprintf(r.id, sizeof r.id, "fxtest");
    snprintf(r.name, sizeof r.name, "fx-test");
    snprintf(r.act, sizeof r.act, "Running the tests");
    r.pane = true;
    s_quiet = true;
    zoo_agents_row(&r, now);
    zoo_agents_end(false, now);
    s_quiet = false;
}

bool fx_debug_command(const char *arg, int64_t now)
{
    unsigned n;
    if (sscanf(arg, "seed %u", &n) == 1) {
        s_seed = n;
        return true;
    }
    if (sscanf(arg, "step %u", &n) == 1) { // moves on while #anim holds everything still, always the same way
        for (n = n > 60000 ? 60000 : n; n >= 20; n -= 20) step(0.02f);
        return true;
    }
    if (!strcmp(arg, "clear")) {
        s_quiet = true;
        zoo_agents_gone("fxtest");
        zoo_agents_end(false, now);
        s_quiet = false;
        stop_all();
        return true;
    }
    int ev = FX_NONE;
    for (int i = 1; i < FX_EVENTS; i++) {
        if (!strcmp(arg, EVENT_NAMES[i])) ev = i;
    }
    if (ev == FX_NONE || !visible() || X.leaving) return false;
    if (!S.n_agents) stand_in(now);
    follow();
    const agent_t *a = on_screen();
    switch (ev) {
    case FX_DONE: enter_state(a, AG_DONE, true); break;
    case FX_GONE:
        start_leaving(a);
        memcpy(X.after, a->a.id, sizeof X.after);
        break;
    case FX_NEW: start_drop(a); break;
    case FX_IDLE: enter_state(a, AG_IDLE, true); break;
    case FX_WORKING: enter_state(a, AG_WORKING, true); break;
    case FX_FAILED: enter_state(a, AG_FAILED, true); break;
    default: fx_on_tap(a->a.pane); break;
    }
    return true;
}
#endif
