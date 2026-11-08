// While locked, a lock at twelve o'clock, on the rim if there is one. Nothing else shows there: the battery is in
// Settings › Device, and the Mac that's connected in Settings › Connectivity.
#include "../app.h"

static const lay_point_t *where_point(status_where_t where)
{
    const lay_status_t *st = &F.L->status;
    switch (where) {
    case WHERE_CLOCK: return &st->clock;
    case WHERE_ARC: return &st->arc;
    case WHERE_ZOO: return &st->zoo;
    default: return &st->list;
    }
}

void status_draw(status_where_t where)
{
    if (!S.lock.open) return;
    const lay_point_t *P = where_point(where);
    const float size = F.L->status.h * 1.25f;
    // On the rim, black on the solid part and white on the shade, whichever is under each pixel.
    if (P->on_rim) icon_status_lock_contrast(P->x, P->y, size);
    else icon_status_lock(P->x, P->y, size, F.T->ink);
}

void toast_draw(void)
{
    if (F.now >= S.toast_until) return;
    const float mid = F.L->base / 2, size = F.L->toast;
    const float w = gfx_text_width(S.toast, size, 600) + size * 2, h = size * 2.1f;
    gfx_rrect(mid - w / 2, F.L->toastY - h / 2, w, h, h / 2, F.T->toastBg, 255);
    gfx_text(S.toast, mid, F.L->toastY + size * 0.35f, size, 600, F.T->toastInk, ALIGN_CENTER, 0, 255);
}
