// Lists: on a round screen a wheel, with the selected row in the middle; on a square one, rows from the top.
// Settings, the Mac list and the tasks and projects lists all draw through here.
#include <math.h>

#include "../app.h"

float list_row_h(const list_view_t *v)
{
    const lay_list_t *G_ = &F.L->list;
    if (G_->wheel || v->scrollable) return G_->rowH;
    const float fit = (G_->maxY - G_->y0) / (float)(v->count > 0 ? v->count : 1);
    return fit < G_->rowH ? fit : G_->rowH;
}

float list_max_scroll(const list_view_t *v)
{
    const lay_list_t *G_ = &F.L->list;
    if (G_->wheel || !v->scrollable) return 0;
    const float over = (float)v->count * G_->rowH - (G_->maxY - G_->y0);
    return over > 0 ? over : 0;
}

int list_row_at(const list_view_t *v, float pos)
{
    const lay_list_t *G_ = &F.L->list;
    if (G_->wheel) return v->cursor;
    if (pos < G_->y0 || pos >= G_->maxY) return -1;
    const float rh = list_row_h(v);
    const int i = (int)((pos - G_->y0 + v->scroll) / rh);
    return i >= 0 && i < v->count ? i : -1;
}

static void draw_value(const list_row_t *r, float x1, float base, float size, uint16_t c, int alpha)
{
    if (r->check) icon_check(x1 - size * 0.42f, base - size * 0.36f, size, c, alpha);
    else if (r->value) gfx_text(r->value, x1, base, size, 600, c, ALIGN_RIGHT, 0, alpha);
}

static float value_width(const list_row_t *r, float size)
{
    if (r->check) return size * 0.9f;
    if (r->value && r->value[0]) return gfx_text_width(r->value, size, 600);
    return 0;
}

static void draw_wheel(const list_view_t *v)
{
    const lay_list_t *G_ = &F.L->list;
    const theme_t *T = F.T;
    const float base = F.L->base, mid = base / 2;
    for (int i = 0; i < v->count; i++) {
        const float d = (float)i - v->pos, far = fabsf(d);
        if (far > 2.4f) continue;
        const float y = mid + d * G_->rowH;
        const bool centre = far < 0.5f;
        // In the preview a row's size glides from big to small. Two sizes, swapped halfway, look the same in motion.
        const float size = centre ? G_->big : G_->small;
        float a = 1 - far * 0.36f;
        if (a < 0.14f) a = 0.14f;
        const int alpha = (int)(a * 255);
        const float dy = y - mid, r2 = mid * mid - dy * dy, hw = (r2 > 0 ? sqrtf(r2) : 0) - 22;
        const float x0 = mid - hw > G_->x0 ? mid - hw : G_->x0, x1 = mid + hw < G_->x1 ? mid + hw : G_->x1;
        if (centre) gfx_rrect(x0 - 12, y - G_->rowH / 2 + 3, x1 - x0 + 24, G_->rowH - 6, 16, T->highlight, 255);
        list_row_t r = {0};
        v->row(i, &r);
        if (r.chevron && !r.check && !(r.value && r.value[0])) r.value = "\xE2\x80\xBA"; // a page to open: "›"
        const float vw = value_width(&r, size), vgap = vw > 0 ? vw + 10 : 0;
        float text_x = x0;
        const float cy = y;
        if (r.dot != DOT_NONE) {
            const float dr = centre ? 6 : 4.5f;
            if (r.dot == DOT_FILLED) gfx_disc(x0 + 6, cy, dr, rgb565(r.dot_rgb), alpha);
            else gfx_ring(x0 + 6, cy, dr, 1.5f, T->sub, alpha);
            text_x = x0 + 20;
        }
        const uint16_t ink = r.danger ? T->amber : r.muted ? T->sub : T->ink;
        gfx_text(r.label, text_x, y + size * 0.35f, size, centre ? 700 : 500, ink, ALIGN_LEFT, x1 - text_x - vgap, alpha);
        draw_value(&r, x1, y + size * 0.35f, size, T->sub, alpha);
    }
    gfx_text(v->title, mid, G_->titleY, G_->title, 700, T->sub, ALIGN_CENTER, 0, 255);
    if (v->hint1) gfx_text(v->hint1, mid, G_->hintY - 9, G_->hint, 500, T->sub, ALIGN_CENTER, 230, 255);
    if (v->hint2) gfx_text(v->hint2, mid, G_->hintY + 8, G_->hint, 500, T->sub, ALIGN_CENTER, 230, 255);
}

static void draw_rows(const list_view_t *v)
{
    const lay_list_t *G_ = &F.L->list;
    const theme_t *T = F.T;
    const float mid = F.L->base / 2, rh = list_row_h(v);
    gfx_text(v->title, mid, G_->titleY, G_->title, 700, T->ink, ALIGN_CENTER, 0, 255);
    if (v->scrollable) gfx_clip_rows(G_->y0, G_->maxY);
    const float top = G_->y0 - v->scroll;
    for (int i = 0; i < v->count; i++) {
        const float y = top + (float)i * rh + rh / 2;
        if (y + rh / 2 < G_->y0 || y - rh / 2 > G_->maxY) continue;
        if (i) gfx_rect(G_->x0, y - rh / 2 - 0.5f, G_->x1 - G_->x0, 1, T->faint, 255);
        list_row_t r = {0};
        v->row(i, &r);
        const float size = G_->big, cw = r.chevron ? size * 0.85f : 0, vw = value_width(&r, size) + cw, vgap = vw > 0 ? vw + 14 : 0;
        float text_x = G_->x0;
        if (r.dot != DOT_NONE) {
            if (r.dot == DOT_FILLED) gfx_disc(G_->x0 + 7, y, 7, rgb565(r.dot_rgb), 255);
            else gfx_ring(G_->x0 + 7, y, 7, 1.5f, T->sub, 255);
            text_x = G_->x0 + 24;
        }
        const uint16_t ink = r.danger ? T->amber : r.muted ? T->sub : T->ink;
        gfx_text(r.label, text_x, y + size * 0.35f, size, 600, ink, ALIGN_LEFT, G_->x1 - text_x - vgap, 255);
        if (r.chevron) gfx_text("\xE2\x80\xBA", G_->x1, y + size * 0.35f, size * 1.2f, 500, T->sub, ALIGN_RIGHT, 0, 255);
        if (r.check || (r.value && r.value[0])) draw_value(&r, G_->x1 - cw, y + size * 0.35f, size, T->sub, 255);
    }
    gfx_clip_reset();
    // A list longer than a screen: a thin bar on the right says where this screen is in it.
    const float extra = list_max_scroll(v);
    if (extra > 0) {
        const float track = G_->maxY - G_->y0, h = track * track / (track + extra), y = G_->y0 + (track - h) * (v->scroll / extra);
        gfx_rrect(F.L->base - 12, G_->y0, 4, track, 2, T->faint, 255);
        gfx_rrect(F.L->base - 12, y, 4, h, 2, T->sub, 255);
    }
    if (v->below && (!G_->rows || v->count < G_->rows)) {
        gfx_text(v->below, mid, G_->y0 + (float)v->count * rh + 34, G_->small * 0.8f, 500, T->sub, ALIGN_CENTER, 0, 255);
    }
}

void list_draw(const list_view_t *v)
{
    gfx_clear(F.T->face);
    if (F.L->list.wheel) draw_wheel(v);
    else draw_rows(v);
}
