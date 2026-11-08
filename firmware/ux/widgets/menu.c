// The menu: the Bento box. Fixed compartments with color icons, opened by a tap. On a round screen they circle the clock;
// on a square one they're tiles. Which goes where is in the layouts, as the owner chose it for each shape.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"
#include "../assets/icons.h"
#include "../gfx/gfx_internal.h"

#define MENU_IDLE_US (10 * SEC_US)
#define BLOCKED_ALPHA 87 // a clock that can't open while the other one runs

// ---- Actions -------------------------------------------------------------------------------------------------------

void menu_open(int64_t now)
{
    S.menu.open = true;
    S.menu.until = now + MENU_IDLE_US;
    ux_feedback(FB_TAP);
    ux_log("menu", "open");
}

void menu_close(void)
{
    S.menu.open = false;
    ux_feedback(FB_TAP);
}

static bool blocked(const ux_mode_t *m)
{
    const ux_mode_t *run = clock_running();
    return run && m->running && m != run;
}

static void choose(const ux_mode_t *m, int64_t now)
{
    if (blocked(m)) {
        S.menu.until = now + MENU_IDLE_US;
        ux_toast("Pause to switch", 1600);
        ux_feedback(FB_BUMP);
        return;
    }
    if (m->choose) {
        (void)m->choose(now);
        return;
    }
    S.menu.open = false;
    if (m == &MODE_ZOO || m == &MODE_CLOCK) S.split = m == &MODE_CLOCK ? SPLIT_CLOCK : SPLIT_AGENTS; // what Split view shows now
    if (m->enter && S.mode != m) m->enter(now);
    S.mode = m;
    ux_mark_dirty(now);
    ux_feedback(FB_TAP);
    ux_log("mode", m->key);
}

// Which compartment a touch lands in. The middle of the round screen and the gaps are none.
static const ux_mode_t *cell_at(float u, float v)
{
    const lay_menu_t *M = &F.L->menu;
    if (M->order[0]) {
        const float mid = F.L->base / 2, dx = u - mid, dy = v - mid;
        if (dx * dx + dy * dy < M->ri * M->ri) return NULL;
        const float step = 2 * PI_F / MENU_CELLS;
        const int slot = (int)floorf(atan2f(dx, -dy) / step - M->first + 0.5f);
        return mode_by_key(M->order[((slot % MENU_CELLS) + MENU_CELLS) % MENU_CELLS]);
    }
    for (int i = 0; i < MENU_CELLS; i++) {
        const lay_tile_t *t = &M->tiles[i];
        if (t->key && u >= t->x && u < t->x + t->w && v >= t->y && v < t->y + t->h) return mode_by_key(t->key);
    }
    return NULL;
}

void menu_tap(float u, float v, int64_t now)
{
    const ux_mode_t *m = cell_at(u, v);
    if (m) choose(m, now);
    else if (F.L->menu.order[0]) menu_close(); // a tap on the round screen's clock
}

void menu_tick(int64_t now)
{
    if (S.menu.open && now > S.menu.until) S.menu.open = false;
}

// ---- Colors --------------------------------------------------------------------------------------------------------

// A compartment's color: a little of the mode's color in the face.
static uint16_t cell_color(const ux_mode_t *m, bool dim)
{
    const uint32_t share = S.theme ? 51 : 67; // of 256
    const uint32_t base = S.theme ? 0xFFFFFF : 0x0B0B0D, face = S.theme ? 0xFFFFFF : 0x000000;
    uint32_t out = 0;
    for (int s = 0; s <= 16; s += 8) {
        uint32_t c = (((m->color >> s) & 255) * share + ((base >> s) & 255) * (256 - share)) >> 8;
        if (dim) c = (c * BLOCKED_ALPHA + ((face >> s) & 255) * (255 - BLOCKED_ALPHA)) / 255;
        out |= c << s;
    }
    return rgb565(out);
}

// ---- The round screen's compartments: a map of the ring, built once --------------------------------------------------

typedef struct { int16_t y, x0; uint16_t len; uint32_t off; } span_t;

static struct {
    span_t *spans;
    int nspans;
    uint8_t *cov, *cell; // per pixel of the ring: its coverage, and which slot it's in
    bool built;
} R;

// How far a point is inside a rounded corner's two edges, in layout units: negative inside.
static float rounded_inside(float a, float b, float corner)
{
    const float q1 = corner - a, q2 = corner - b;
    const float m1 = q1 > 0 ? q1 : 0, m2 = q2 > 0 ? q2 : 0;
    const float outside = sqrtf(m1 * m1 + m2 * m2), inside = q1 > q2 ? q1 : q2;
    return outside + (inside < 0 ? inside : 0) - corner;
}

static void build_ring(void)
{
    R.built = true;
    const lay_menu_t *M = &F.L->menu;
    const int W = G.w, H = G.h;
    const float k = G.k, invk = 1 / k, mid = F.L->base / 2;
    const float ro_out = (M->ro + 1) * (M->ro + 1), ri_in = (M->ri - 1) * (M->ri - 1);
    int count = 0, nspans = 0;
    for (int y = 0; y < H; y++) {
        const float ly = ((float)y + 0.5f) * invk - mid;
        int run = 0;
        for (int x = 0; x <= W; x++) {
            const float lx = ((float)x + 0.5f) * invk - mid, rr = lx * lx + ly * ly;
            const bool in = x < W && rr < ro_out && rr > ri_in;
            if (in) count++;
            if (in && !run) nspans++;
            run = in;
        }
    }
    R.spans = hal_alloc_large((size_t)nspans * sizeof(span_t));
    R.cov = hal_alloc_large((size_t)count);
    R.cell = hal_alloc_large((size_t)count);
    if (!R.spans || !R.cov || !R.cell) return;
    // Each slot's two sides, as lines through the middle with their inward normals.
    const float step = 2 * PI_F / MENU_CELLS;
    float nx[MENU_CELLS][2], ny[MENU_CELLS][2];
    for (int i = 0; i < MENU_CELLS; i++) {
        const float a = ((float)i + M->first) * step, a0 = a - step / 2, a1 = a + step / 2;
        // Clockwise from twelve o'clock, a side at angle t points to (sin t, -cos t). Inward is a quarter turn from it.
        nx[i][0] = cosf(a0);
        ny[i][0] = sinf(a0);
        nx[i][1] = -cosf(a1);
        ny[i][1] = -sinf(a1);
    }
    int off = 0;
    R.nspans = 0;
    for (int y = 0; y < H; y++) {
        const float ly = ((float)y + 0.5f) * invk - mid;
        int start = -1, start_off = 0;
        for (int x = 0; x <= W; x++) {
            const float lx = ((float)x + 0.5f) * invk - mid, rr = lx * lx + ly * ly;
            const bool in = x < W && rr < ro_out && rr > ri_in;
            if (in) {
                if (start < 0) {
                    start = x;
                    start_off = off;
                }
                const float r = sqrtf(rr), radial = M->ro - r < r - M->ri ? M->ro - r : r - M->ri;
                uint8_t best_cov = 0, best_cell = 0;
                for (int i = 0; i < MENU_CELLS; i++) {
                    const float s0 = lx * nx[i][0] + ly * ny[i][0], s1 = lx * nx[i][1] + ly * ny[i][1];
                    const float side = (s0 < s1 ? s0 : s1) - M->gap / 2;
                    if (side < -1) continue;
                    const float cov = gfx_clamp01(0.5f - rounded_inside(side, radial, M->corner) * k) * 255.0f;
                    if (cov > (float)best_cov) {
                        best_cov = (uint8_t)cov;
                        best_cell = (uint8_t)i;
                    }
                }
                R.cov[off] = best_cov;
                R.cell[off] = best_cell;
                off++;
            } else if (start >= 0) {
                R.spans[R.nspans++] = (span_t){(int16_t)y, (int16_t)start, (uint16_t)(x - start), (uint32_t)start_off};
                start = -1;
            }
        }
    }
}

static void draw_ring(const uint16_t colors[MENU_CELLS])
{
    if (!R.built) build_ring();
    if (!R.nspans) return;
    const int ox = G.ox, oy = G.oy;
    if (G.recording) {
        uint32_t w[MENU_CELLS + 2];
        for (int i = 0; i < MENU_CELLS; i++) w[i] = colors[i];
        w[MENU_CELLS] = (uint32_t)ox;
        w[MENU_CELLS + 1] = (uint32_t)oy;
        gfx_mark_rows(R.spans[0].y + oy, R.spans[R.nspans - 1].y + 1 + oy, gfx_hash_words(w, MENU_CELLS + 2));
        return;
    }
    uint32_t split[MENU_CELLS];
    for (int i = 0; i < MENU_CELLS; i++) split[i] = GFX_SPLIT(colors[i]);
    const int W = G.w;
    for (int sp = 0; sp < R.nspans; sp++) {
        const span_t *s = &R.spans[sp];
        const int Y = s->y + oy;
        if (Y < G.cy0 || Y >= G.cy1 || !G.row_on[Y]) continue;
        uint16_t *row = G.target + Y * W;
        const uint8_t *cov = R.cov + s->off, *cell = R.cell + s->off;
        for (int i = 0; i < s->len; i++) {
            const int X = s->x0 + i + ox;
            if (X < 0 || X >= W || !cov[i]) continue;
            row[X] = gfx_blend(row[X], split[cell[i]], (uint32_t)(cov[i] + 4) >> 3);
        }
    }
}

void menu_prepare(void)
{
    if (F.L->menu.order[0]) build_ring();
}

// ---- Drawing -------------------------------------------------------------------------------------------------------

static void draw_image(const char *key, float cx, float cy, float size, int alpha)
{
    const int want = (int)(size * G.k + 0.5f);
    const icon_image_t *img = NULL;
    int best = 1 << 30;
    for (int i = 0; i < ICON_IMAGE_COUNT; i++) {
        const icon_image_t *c = &ICON_IMAGES[i];
        if (strcmp(c->key, key)) continue;
        const int d = c->size > want ? c->size - want : want - c->size;
        if (d < best) {
            best = d;
            img = c;
        }
    }
    if (!img) return;
    const int s = img->size, x0 = (int)(gfx_dx(cx) + 0.5f) - s / 2, gy = (int)(gfx_dy(cy) + 0.5f) - s / 2;
    if (G.recording) {
        const uint32_t w[4] = {(uint32_t)(uintptr_t)img, (uint32_t)x0, (uint32_t)gy, (uint32_t)alpha};
        gfx_mark_rows(gy, gy + s, gfx_hash_words(w, 4));
        return;
    }
    int y0 = gy, y1 = gy + s;
    if (!gfx_rows(&y0, &y1)) return;
    const uint32_t a256 = (uint32_t)alpha + ((uint32_t)alpha >> 7);
    for (int y = y0; y < y1; y++) {
        if (!G.row_on[y]) continue;
        const int off = (y - gy) * s;
        uint16_t *row = G.target + y * G.w;
        for (int i = 0; i < s; i++) {
            const int X = x0 + i;
            if (X < 0 || X >= G.w || !img->alpha[off + i]) continue;
            const uint32_t a = (((uint32_t)img->alpha[off + i] * a256 >> 8) + 4) >> 3;
            if (a >= 32) row[X] = img->rgb[off + i];
            else if (a) row[X] = gfx_blend(row[X], GFX_SPLIT(img->rgb[off + i]), a);
        }
    }
}

// An agent needs you: a dot on the Agents icon.
static void draw_icon(const ux_mode_t *m, float x, float y, float size, int alpha)
{
    draw_image(m->key, x, y, size, alpha);
    if (m == &MODE_ZOO && S.n_needing > 0) gfx_disc(x + size * 0.46f, y - size * 0.46f, size * 0.13f, F.T->amber, alpha);
}

// What's there now, for a compartment with room to say it. Empty if there's nothing to say.
static uint16_t brief(const ux_mode_t *m, char *out, size_t n, bool *strong)
{
    const theme_t *T = F.T;
    *strong = false;
    out[0] = 0;
    if (blocked(m)) {
        snprintf(out, n, "Pause to switch");
    } else if (clock_running() == m) {
        char time[12];
        clock_format(m == &MODE_POMODORO, F.now, time);
        snprintf(out, n, "%s", time);
        *strong = true;
        return rgb565(m->color);
    } else if (m == &MODE_TIMER) {
        snprintf(out, n, "Track time");
    } else if (m == &MODE_POMODORO) {
        snprintf(out, n, "%02d:00", S.p.set_min);
    } else if (m == &MODE_ZOO) {
        if (S.n_needing > 0) {
            snprintf(out, n, "%d need%s you", S.n_needing, S.n_needing == 1 ? "s" : "");
            return T->amber;
        }
        if (S.n_agents == 1) snprintf(out, n, "1 agent");
        else if (S.n_agents) snprintf(out, n, "%d agents", S.n_agents);
        else snprintf(out, n, "No agents");
    } else if (m == &MODE_CLOCK) {
        daytime_t d;
        if (daytime_get(F.now, &d)) {
            char hm[8], ap[4];
            daytime_text(&d, DT_TIME, hm, sizeof hm);
            daytime_text(&d, DT_AMPM, ap, sizeof ap);
            snprintf(out, n, "%s %s", hm, ap);
        }
    }
    return T->sub;
}

// The round screen's middle: the clock, or the clock that's running.
static void draw_hub(void)
{
    const lay_menu_t *M = &F.L->menu;
    const theme_t *T = F.T;
    const float mid = F.L->base / 2, y = mid + M->hub * 0.18f, y2 = y + M->hub_small * 1.5f;
    const ux_mode_t *run = clock_running();
    char line[32];
    if (run) {
        char time[12];
        clock_format(run == &MODE_POMODORO, F.now, time);
        gfx_text(time, mid, y, M->hub, 700, rgb565(run->color), ALIGN_CENTER, 0, 255);
        snprintf(line, sizeof line, "%s running", run->label);
        gfx_text(line, mid, y2, M->hub_small, 600, T->sub, ALIGN_CENTER, 0, 255);
        return;
    }
    daytime_t d;
    if (!daytime_get(F.now, &d)) {
        gfx_text("--:--", mid, y, M->hub, 700, T->ink, ALIGN_CENTER, 0, 255);
    } else {
        daytime_draw_time(&d, mid, y, M->hub * 0.86f, M->hub * 0.4f, T->ink, T->sub, false);
        if (!S.n_needing) {
            daytime_text(&d, DT_SHORT_DATE, line, sizeof line); // "Sat 7th Nov"
            gfx_text(line, mid, y2, M->hub_small, 500, T->sub, ALIGN_CENTER, 0, 255);
        }
    }
    if (S.n_needing) gfx_text("Agent needs you", mid, y2, M->hub_small, 600, T->amber, ALIGN_CENTER, 0, 255);
}

void menu_draw(void)
{
    const layout_t *L = F.L;
    const lay_menu_t *M = &L->menu;
    const theme_t *T = F.T;
    const float mid = L->base / 2;
    gfx_clear(T->face);
    if (M->order[0]) {
        uint16_t colors[MENU_CELLS];
        const ux_mode_t *modes[MENU_CELLS];
        for (int i = 0; i < MENU_CELLS; i++) {
            modes[i] = mode_by_key(M->order[i]);
            colors[i] = cell_color(modes[i], blocked(modes[i]));
        }
        draw_ring(colors);
        const float step = 2 * PI_F / MENU_CELLS;
        for (int i = 0; i < MENU_CELLS; i++) {
            const float a = ((float)i + M->first) * step, x = mid + M->mid * sinf(a), y = mid - M->mid * cosf(a);
            const int alpha = blocked(modes[i]) ? BLOCKED_ALPHA : 255;
            draw_icon(modes[i], x, y + M->icon_dy, M->icon, alpha);
            gfx_text(modes[i]->label, x, y + M->label_dy, M->label, 700, T->ink, ALIGN_CENTER, 0, alpha);
        }
        draw_hub();
        return;
    }
    for (int i = 0; i < MENU_CELLS; i++) {
        const lay_tile_t *t = &M->tiles[i];
        const ux_mode_t *m = mode_by_key(t->key);
        const bool dim = blocked(m);
        const int alpha = dim ? BLOCKED_ALPHA : 255;
        gfx_rrect(t->x, t->y, t->w, t->h, M->radius, cell_color(m, dim), 255);
        draw_icon(m, t->ix, t->iy, t->icon, alpha);
        gfx_text(m->label, t->lx, t->ly, t->label, 700, T->ink, ALIGN_LEFT, 0, alpha);
        if (t->sy) {
            char line[32];
            bool strong;
            const uint16_t c = brief(m, line, sizeof line, &strong);
            gfx_text(line, t->lx, t->sy, M->sub, strong ? 700 : 600, c, ALIGN_LEFT, 0, alpha);
        }
    }
}
