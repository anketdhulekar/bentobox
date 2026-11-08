// The characters: the Claude Code mascot in costumes, the Codex slime and the Grok ball. Pixel art, the same rows as
// the preview's. Each frame draws them as blocks; the renderer's band hashing makes that cheap when nothing moved.
#include <math.h>
#include <string.h>

#include "../app.h"
#include "../assets/sprites.h"

// One letter per color, as in the preview.
static uint16_t pal(char ch)
{
    switch (ch) {
    case 'K': return rgb565(0x1d1d1f); case 'k': return rgb565(0x55555a); case 'R': return rgb565(0xE5484D);
    case 'V': return rgb565(0xB91C1C); case 'Y': return rgb565(0xF2C14E); case 'y': return rgb565(0xB7791F);
    case 'W': return rgb565(0xFFFFFF); case 'w': return rgb565(0xD4D4D4); case 'G': return rgb565(0x2BA36B);
    case 'g': return rgb565(0x1E7A4F); case 'B': return rgb565(0x3B82F6); case 'L': return rgb565(0x22C55E);
    case 'N': return rgb565(0x3D4DB7); case 'n': return rgb565(0x2E3A94); case 'F': return rgb565(0xFDE047);
    case 'P': return rgb565(0x7C5CFF); case 'p': return rgb565(0x5B45D6); case 'M': return rgb565(0xE86FA6);
    case 'm': return rgb565(0xF9A8D4); case 'C': return rgb565(0x22D3EE); case 'c': return rgb565(0x06B6D4);
    case 'Q': return rgb565(0xF472B6); case 'T': return rgb565(0x14B8A6); case 't': return rgb565(0x0F9488);
    case 'O': return rgb565(0x8B5A2B); case 'o': return rgb565(0xA0692F); case 'D': return rgb565(0x5C3A1A);
    case 'S': return rgb565(0x9CA3AF); case 'E': return rgb565(0x7BD88F); case 'e': return rgb565(0x3FA65A);
    case 'H': return rgb565(0x2E7D46);
    default: return 0xF81F; // magenta: a typo in a row
    }
}

typedef struct {
    float ax, ay, sc, sx, sy;
    int alpha;
    bool edge;    // this pass draws the dark edge of a character on a white screen
    float edge_d;
    uint16_t edge_c;
    bool mirror;  // left to right, about the middle
} ctx_t;

static void put(const ctx_t *c, float u, float v, uint16_t col)
{
    if (c->mirror) u = 17 - u;
    const float x1 = c->ax + (u - 9) * c->sc * c->sx, x2 = c->ax + (u + 1 - 9) * c->sc * c->sx;
    const float y1 = c->ay - (10 - v) * c->sc * c->sy, y2 = c->ay - (10 - v - 1) * c->sc * c->sy;
    if (c->edge) gfx_block(x1 - c->edge_d, y1 - c->edge_d, x2 + c->edge_d, y2 + c->edge_d, c->edge_c, c->alpha);
    else gfx_block(x1, y1, x2, y2, col, c->alpha);
}

static void draw_part(const ctx_t *c, const part_t *part, float dy)
{
    for (int g = 0; g < part->count; g++) {
        const group_t *gr = &part->groups[g];
        for (int j = 0; j < gr->n; j++) {
            const char *row = gr->rows[j];
            for (int i = 0; row[i]; i++) {
                if (row[i] != '.') put(c, (float)(gr->x0 + i), (float)(gr->y0 + j) + dy, pal(row[i]));
            }
        }
    }
}

// ---- The Codex slime --------------------------------------------------------------------------------------------

static const float SLIME_HALF[10] = {2.6f, 4.6f, 5.6f, 6.2f, 6.6f, 6.9f, 7.0f, 7.0f, 7.0f, 6.6f};
static bool slime_in(int x, int y) { return y >= 0 && y < 10 && fabsf((float)x + 0.5f - 9) <= SLIME_HALF[y]; }

static void draw_slime(const ctx_t *c, const pose_t *p)
{
    const bool sick = p->body == BODY_SICK;
    const uint16_t out = rgb565(sick ? 0x4A4F66 : 0x24358F), fill = rgb565(sick ? 0x9097B0 : 0x5B8CFF);
    const uint16_t light = rgb565(sick ? 0xADB3C8 : 0x86AAFF), shade = rgb565(sick ? 0x7D849D : 0x4673E8);
    for (int y = 0; y < 10; y++) {
        for (int x = 0; x < 18; x++) {
            if (!slime_in(x, y)) continue;
            const bool edge = !slime_in(x + 1, y) || !slime_in(x - 1, y) || !slime_in(x, y + 1) || !slime_in(x, y - 1);
            put(c, (float)x, (float)y, edge ? out : y >= 8 ? shade : (y <= 3 && x <= 8) ? light : fill);
        }
    }
    const uint16_t shine = rgb565(sick ? 0xE6E8EE : 0xF2F6FF);
    put(c, 5, 2, shine);
    put(c, 6, 2, shine);
    put(c, 4, 3, shine);
    // Little hands: down at rows 7..8; up as the arms go up, and a small paddle while it works.
    const int arms[2] = {p->arm_l, p->arm_r};
    const bool steps[2] = {p->leg_a, p->leg_b};
    for (int k = 0; k < 2; k++) {
        const int x0 = k ? 16 : 0, hy = 7 + arms[k] * 2 - (steps[k] ? 1 : 0);
        put(c, (float)x0, (float)hy, k ? fill : out);
        put(c, (float)(x0 + 1), (float)hy, k ? out : fill);
        put(c, (float)x0, (float)(hy + 1), out);
        put(c, (float)(x0 + 1), (float)(hy + 1), out);
    }
    const face_t *f = &SLIME_FACES[p->eyes >= 0 && p->eyes <= 4 ? p->eyes : 0];
    const int g = p->eyes == 0 ? p->look : 0;
    for (int i = 0; i < f->ne; i++) put(c, (float)(f->eyes[i].x + g), f->eyes[i].y, rgb565(0x121933));
    for (int i = 0; i < f->ns; i++) put(c, (float)(f->spark[i].x + g), f->spark[i].y, 0xFFFF);
    for (int i = 0; i < f->nm; i++) put(c, (float)(f->mouth[i].x + g), f->mouth[i].y, rgb565(0x1B2452));
    if (!sick) {
        const uint16_t blush = rgb565(0xFF9EC8);
        put(c, 3, 7, blush);
        put(c, 4, 7, blush);
        put(c, 13, 7, blush);
        put(c, 14, 7, blush);
    }
}

// ---- The Grok ball --------------------------------------------------------------------------------------------------

static bool ball_in(int x, int y)
{
    const float dx = (float)x + 0.5f - 9, dy = (float)y + 0.5f - 4;
    return x >= 3 && x <= 14 && y >= -2 && y <= 9 && dx * dx + dy * dy <= 37;
}

static bool away(int x, int y, float cx, float cy, float r)
{
    const float dx = (float)x + 0.5f - cx, dy = (float)y + 0.5f - cy;
    return dx * dx + dy * dy > r * r;
}

static int ball_tone(int x, int y)
{
    if (away(x, y, 10, 3, 6.2f)) return 3;
    if (away(x, y, 10.8f, 2.2f, 7.4f)) return 2;
    const float hx = (float)x + 0.5f - 11.2f, hy = (float)y + 0.5f - 0.8f;
    return hx * hx + hy * hy < 2.6f ? 0 : 1;
}

static void draw_ball(const ctx_t *c, const pose_t *p)
{
    static const uint32_t TONES[2][4] = {{0xFFFFFF, 0xF0EEEA, 0xD2CEC7, 0xA9A49D}, {0xD9D7D2, 0xBDBAB4, 0x9E9B96, 0x7C7975}};
    const uint32_t *tone = TONES[p->body == BODY_SICK];
    for (int y = -2; y <= 9; y++) {
        for (int x = 3; x <= 14; x++) {
            if (ball_in(x, y)) put(c, (float)x, (float)y, rgb565(tone[ball_tone(x, y)]));
        }
    }
    const face_t *f = &BALL_FACES[p->eyes >= 0 && p->eyes <= 4 ? p->eyes : 0];
    const int g = p->eyes == 0 ? p->look : 0;
    const uint16_t eye = rgb565(0x0B0B0D), gloss = rgb565(0x6E6E76);
    for (int i = 0; i < f->ne; i++) {
        if (ball_in(f->eyes[i].x + g, f->eyes[i].y)) put(c, (float)(f->eyes[i].x + g), f->eyes[i].y, eye);
    }
    for (int i = 0; i < f->ns; i++) put(c, (float)(f->spark[i].x + g), f->spark[i].y, gloss);
}

// ---- The mascot --------------------------------------------------------------------------------------------------

static void draw_mascot(const ctx_t *c, int kind, int hat, int outfit, int acc, const pose_t *p)
{
    if (kind == KIND_CODEX || kind == KIND_GROK) {
        float lift = 0;
        if (kind == KIND_GROK) {
            draw_ball(c, p);
            lift = -1; // its hat sits one row higher
        } else {
            draw_slime(c, p);
        }
        draw_part(c, &HATS[hat], lift + p->hat_dy);
        if (hat == HAT_PROPELLER) draw_part(c, &PROP[p->prop & 3], lift + p->hat_dy);
        return;
    }
    if (outfit == OUT_CAPE) {
        const group_t *gr = &CAPE_BACK.groups[0];
        for (int j = 0; j < gr->n; j++) {
            const int v = gr->y0 + j;
            const float shift = v >= 5 ? sinf(p->t * 9 + (float)v * 0.9f) * 0.45f * p->flutter : 0;
            for (int i = 0; gr->rows[j][i]; i++) {
                if (gr->rows[j][i] != '.') put(c, (float)(gr->x0 + i) + shift, (float)v, pal(gr->rows[j][i]));
            }
        }
    }
    const uint16_t body = rgb565(p->body), eye = rgb565(0x141414), red = rgb565(0xE5484D);
    static const int legs_a[2] = {4, 11}, legs_b[2] = {6, 13};
    for (int i = 0; i < 2; i++) {
        put(c, (float)legs_a[i], 8, body);
        if (!p->leg_a) put(c, (float)legs_a[i], 9, body);
        put(c, (float)legs_b[i], 8, body);
        if (!p->leg_b) put(c, (float)legs_b[i], 9, body);
    }
    for (int y = 0; y < 8; y++) {
        for (int x = 3; x <= 14; x++) put(c, (float)x, (float)y, body);
    }
    for (int y = 4; y < 6; y++) {
        put(c, 1, (float)(y + p->arm_l), body);
        put(c, 2, (float)(y + p->arm_l), body);
        put(c, 15, (float)(y + p->arm_r), body);
        put(c, 16, (float)(y + p->arm_r), body);
    }
    draw_part(c, &OUTFITS[outfit], 0);
    const int ex[2] = {5 + p->look, 12 + p->look};
    for (int i = 0; i < 2; i++) {
        switch (p->eyes) {
        case 4: put(c, (float)ex[i], 2, red); put(c, (float)ex[i], 3, red); break;
        case 1: put(c, (float)ex[i], 3, eye); break;
        case 2: put(c, (float)ex[i], 2, eye); break;
        case 3: put(c, (float)ex[i], 1, eye); put(c, (float)ex[i], 2, eye); put(c, (float)ex[i], 3, eye); break;
        default: put(c, (float)ex[i], 2, eye); put(c, (float)ex[i], 3, eye); break;
        }
    }
    if (acc == ACC_BALLOON) {
        const float sway = sinf(p->t * 1.7f) * 0.5f;
        const part_t *a = &ACCS[acc];
        for (int g = 0; g < a->count; g++) {
            const group_t *gr = &a->groups[g];
            for (int j = 0; j < gr->n; j++) {
                const char *row = gr->rows[j];
                const int v = gr->y0 + j;
                const bool floaty = v < 4;
                float k = (4 - (float)v) / 8.0f;
                k = k < 0 ? 0 : k > 1 ? 1 : k;
                for (int i = 0; row[i]; i++) {
                    if (row[i] == '.') continue;
                    put(c, (float)(gr->x0 + i) + (floaty ? sway * k : 0), (float)v + (floaty ? (float)p->arm_r * 0.5f : 0), pal(row[i]));
                }
            }
        }
    } else {
        draw_part(c, &ACCS[acc], 0);
    }
    draw_part(c, &HATS[hat], p->hat_dy);
    if (hat == HAT_PROPELLER) draw_part(c, &PROP[p->prop & 3], p->hat_dy);
}

// On a white screen, a thin dark edge keeps a character's white parts visible, such as the Grok ball or a coffee cup.
static void draw_with_edge(int kind, int hat, int outfit, int acc, const pose_t *p, float x, float feet, float sc)
{
    ctx_t c = {.ax = x, .ay = feet, .sc = sc, .sx = p->sx, .sy = p->sy, .alpha = (int)(p->alpha * 255)};
    if (S.theme == 1 && p->alpha >= 0.99f) {
        // A band of edge color around every cell: the same as the preview's eight offset copies, drawn once.
        c.edge = true;
        c.edge_d = sc * 0.3f > 1.5f ? sc * 0.3f : 1.5f;
        c.edge_c = rgb565(0x2A2825);
        draw_mascot(&c, kind, hat, outfit, acc, p);
        c.edge = false;
    }
    draw_mascot(&c, kind, hat, outfit, acc, p);
}

void character_draw(const agent_t *a, const pose_t *p, float x, float feet, float sc)
{
    draw_with_edge(a->a.kind, a->hat, a->outfit, a->acc, p, x, feet, sc);
}

// A hat that has come off, tumbling: face on, edge on, then the same mirrored. No rotation: blocks only.
void character_draw_hat(const agent_t *a, int frame, float x, float feet, float sc)
{
    ctx_t c = {.ax = x, .ay = feet, .sc = sc, .sx = frame & 1 ? 0.55f : 1, .sy = 1, .alpha = 255, .mirror = frame & 2};
    const float lift = a->a.kind == KIND_GROK ? -1 : 0;
    const int prop = frame & 3;
    if (S.theme == 1) {
        c.edge = true;
        c.edge_d = sc * 0.3f > 1.5f ? sc * 0.3f : 1.5f;
        c.edge_c = rgb565(0x2A2825);
        draw_part(&c, &HATS[a->hat], lift);
        if (a->hat == HAT_PROPELLER) draw_part(&c, &PROP[prop], lift);
        c.edge = false;
    }
    draw_part(&c, &HATS[a->hat], lift);
    if (a->hat == HAT_PROPELLER) draw_part(&c, &PROP[prop], lift);
}

void character_draw_default(const pose_t *p, float x, float feet, float sc)
{
    ctx_t c = {.ax = x, .ay = feet, .sc = sc, .sx = p->sx, .sy = p->sy, .alpha = (int)(p->alpha * 255)};
    draw_mascot(&c, KIND_CLAUDE, HAT_BEANIE, OUT_NONE, ACC_NONE, p);
}

pose_t character_pose(agent_status_t st, float t, int i)
{
    pose_t p = {.sx = 1, .sy = 1, .alpha = 1, .body = BODY_CLAUDE, .prop = (int)(t * 8) % 4, .flutter = 0.5f, .t = t};
    switch (st) {
    case AG_WORKING: {
        const bool step = (int)(t * 6 + (float)i) % 2 == 0;
        p.leg_a = step;
        p.leg_b = !step;
        p.look = (int)(t / 2 + (float)i) % 3 == 0 ? 1 : 0;
        break;
    }
    case AG_NEEDS: {
        const bool up = (int)(t * 4) % 2;
        p.eyes = 3;
        p.arm_r = up ? -2 : -1;
        p.arm_l = up ? -1 : -2;
        break;
    }
    case AG_DONE:
        p.eyes = 2;
        p.arm_l = p.arm_r = -2;
        break;
    case AG_IDLE:
        p.eyes = 1;
        break;
    case AG_FAILED:
        p.eyes = 4;
        p.body = BODY_SICK;
        break;
    }
    return p;
}

void character_costume(agent_t *a)
{
    uint32_t h = 2166136261u;
    for (const uint8_t *s = (const uint8_t *)a->a.id; *s; s++) {
        h ^= *s;
        h *= 16777619u;
    }
    a->hat = (uint8_t)(h % HAT_COUNT);
    if (a->a.kind != KIND_CLAUDE) {
        a->outfit = OUT_NONE;
        a->acc = ACC_NONE;
    } else {
        a->outfit = (uint8_t)((h >> 8) % OUT_COUNT);
        a->acc = (uint8_t)((h >> 16) % ACC_COUNT);
    }
}

// The speech bubble. x, y: the tip of its tail; it sits up and to the left.
void character_bubble(bubble_t icon, float x, float y, uint16_t fill, uint16_t ink, float scale, int alpha)
{
    static const char *const ICONS[BUBBLE_COUNT][5] = {
        [BUBBLE_QUESTION] = {".###.", "...#.", "..#..", ".....", "..#.."},
        [BUBBLE_CHECK] = {".....", "....#", "...#.", "#.#..", ".#..."},
        [BUBBLE_X] = {"#...#", ".#.#.", "..#..", ".#.#.", "#...#"},
        [BUBBLE_ARROW] = {"..#..", "...#.", "#####", "...#.", "..#.."},
        [BUBBLE_BASH] = {"#....", ".#...", "#....", ".....", "..###"},
        [BUBBLE_EDIT] = {"...##", "..###", ".###.", "###..", "#...."},
        [BUBBLE_READ] = {"####.", "#..##", "#...#", "#...#", "#####"},
        [BUBBLE_WEB] = {".###.", "#.#.#", "#####", "#.#.#", ".###."},
        [BUBBLE_SEARCH] = {".##..", "#..#.", "#..#.", ".###.", "....#"},
        [BUBBLE_THINK] = {".....", ".....", "#.#.#", ".....", "....."},
        [BUBBLE_SUB] = {"..#..", "..#..", "#####", "..#..", "..#.."},
    };
    const float P = 3 * scale;
    const float rx = roundf(x), ry = roundf(y), ox = rx - 8 * P, oy = ry - 11 * P;
    // Rows that don't overlap, so that it fades evenly: a square with its corners cut, then the tail.
    gfx_block(ox + P, oy, ox + 8 * P, oy + P, fill, alpha);
    gfx_block(ox, oy + P, ox + 9 * P, oy + 8 * P, fill, alpha);
    gfx_block(ox + P, oy + 8 * P, ox + 8 * P, oy + 9 * P, fill, alpha);
    gfx_block(ox + 6 * P, oy + 9 * P, ox + 8 * P, oy + 10 * P, fill, alpha);
    gfx_block(ox + 7 * P, oy + 10 * P, ox + 8 * P, oy + 11 * P, fill, alpha);
    for (int j = 0; j < 5; j++) {
        const char *row = ICONS[icon][j];
        for (int i = 0; i < 5; i++) {
            if (row[i] == '#') gfx_block(ox + (float)(2 + i) * P, oy + (float)(2 + j) * P, ox + (float)(3 + i) * P, oy + (float)(3 + j) * P, ink, alpha);
        }
    }
}
