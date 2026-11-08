// The whole keypad, on every board. Digits come from touch.
#include "../app.h"

static const char *const KEYS[12] = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "del"};

void keypad_draw(bool waiting)
{
    const lay_lock_t *G_ = &F.L->lock;
    const theme_t *T = F.T;
    const float mid = F.L->base / 2, x0 = mid - G_->keyW * 1.5f;
    const int alpha = waiting ? 77 : 255; // .3 while it waits
    for (int i = 0; i < 12; i++) {
        const char *key = KEYS[i];
        if (!key[0]) continue;
        const float x = x0 + G_->keyW * ((float)(i % 3) + 0.5f), y = G_->keyTop + G_->keyH * ((float)(i / 3) + 0.5f);
        if (key[1]) {
            icon_delete_key(x, y, G_->key, T->ink, alpha);
        } else {
            gfx_disc(x, y, G_->keyR, T->faint, alpha);
            gfx_text(key, x, y + G_->key * 0.36f, G_->key, 600, T->ink, ALIGN_CENTER, 0, alpha);
        }
    }
}

const char *keypad_key_at(float u, float v)
{
    const lay_lock_t *G_ = &F.L->lock;
    const float x0 = F.L->base / 2 - G_->keyW * 1.5f;
    const float col = (u - x0) / G_->keyW, row = (v - G_->keyTop) / G_->keyH;
    if (col < 0 || col >= 3 || row < 0 || row >= 4) return "";
    return KEYS[(int)row * 3 + (int)col];
}
