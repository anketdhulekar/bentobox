// The lock. The code is set on the Mac, and only a salted hash of it is kept here. Locked, the screen keeps its normal
// content and clocks keep running. The first touch or knob turn only brings up the keypad, and digits come from touch
// on every board. Sounds stay off until it's unlocked; vibration stays on.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"

#define LOCK_TRIES 5
#define LOCK_WAIT_US (30 * SEC_US)      // the first wait after LOCK_TRIES wrong codes; each one after it, twice as long...
#define LOCK_WAIT_MAX_US (15 * MIN_US)  // ... up to this: trying all 10,000 codes takes weeks
#define LOCK_PAD_US (10 * SEC_US)

void lock_engage(void)
{
    S.lock.open = true;
    S.lock.pad = false;
    S.lock.entry[0] = 0;
    S.lock.msg[0] = 0;
    S.lock.tries = 0;
    S.menu.open = false;
    S.card = false;
    voice_cancel(); // a locked board doesn't keep listening, and a late transcript can't change its task
    store_save_lock();
    ux_log("lock", "locked");
}

void lock_unlock(void)
{
    S.lock.open = false;
    S.lock.pad = false;
    S.lock.tries = 0;
    S.lock.strikes = 0;
    S.lock.until = 0;
    S.lock.entry[0] = 0;
    S.lock.msg[0] = 0;
    store_save_lock();
    ux_log("lock", "unlocked");
}

static bool lock_choose(int64_t now)
{
    (void)now;
    if (!S.lock.has_code) {
        S.menu.until = now + 10 * SEC_US;
        ux_toast("Set a code on your Mac", 1600);
        ux_feedback(FB_BUMP);
        return true;
    }
    S.menu.open = false;
    ux_feedback(FB_TAP);
    lock_engage();
    return true;
}

// True if this input only brought up the keypad, so it shouldn't do anything else.
bool lock_wake(int64_t now)
{
    S.lock.pad_until = now + LOCK_PAD_US;
    if (S.lock.pad) return false;
    S.lock.pad = true;
    S.lock.entry[0] = 0;
    S.lock.msg[0] = 0;
    ux_feedback(FB_TICK);
    ux_log("lock", "pad");
    return true;
}

static void lock_sleep(void)
{
    S.lock.pad = false;
    S.lock.entry[0] = 0;
    S.lock.msg[0] = 0;
}

static void lock_fail(const char *msg, int64_t now)
{
    utf8_copy(S.lock.msg, sizeof S.lock.msg, msg);
    S.lock.entry[0] = 0;
    S.lock.shake = now;
    ux_feedback(FB_BUMP);
}

static bool code_matches(const char *code)
{
    uint8_t out[32];
    hal_pbkdf2_sha256(code, S.lock.salt, (size_t)S.lock.salt_len, S.lock.iter, out);
    return S.lock.has_code && !memcmp(out, S.lock.hash, 32);
}

static void lock_push(const char *digit, int64_t now)
{
    if (now < S.lock.until) {
        ux_feedback(FB_BUMP);
        return;
    }
    const size_t n = strlen(S.lock.entry);
    if (n < 4) {
        S.lock.entry[n] = digit[0];
        S.lock.entry[n + 1] = 0;
    }
    S.lock.msg[0] = 0;
    if (strlen(S.lock.entry) < 4) {
        ux_feedback(FB_TICK);
        return;
    }
    char code[5];
    memcpy(code, S.lock.entry, sizeof code);
    S.lock.entry[0] = 0;
    if (code_matches(code)) {
        memset(code, 0, sizeof code);
        lock_unlock();
        ux_feedback(FB_DOUBLE);
        return;
    }
    memset(code, 0, sizeof code);
    S.lock.tries++;
    if (S.lock.tries >= LOCK_TRIES) {
        S.lock.tries = 0;
        int64_t wait = LOCK_WAIT_US;
        for (int i = 0; i < S.lock.strikes && wait < LOCK_WAIT_MAX_US; i++) wait *= 2;
        S.lock.until = now + (wait < LOCK_WAIT_MAX_US ? wait : LOCK_WAIT_MAX_US);
        if (S.lock.strikes < 100) S.lock.strikes++;
        ux_log("lock", "wait");
    }
    store_save_lock();
    lock_fail("Wrong code", now);
    ux_log("lock", "wrong");
}

static void lock_delete(void)
{
    const size_t n = strlen(S.lock.entry);
    if (!n) {
        ux_feedback(FB_BUMP);
        return;
    }
    S.lock.entry[n - 1] = 0;
    ux_feedback(FB_TICK);
}

void lock_tap(float u, float v, int64_t now)
{
    const char *key = keypad_key_at(u, v);
    if (!strcmp(key, "del")) lock_delete();
    else if (key[0]) lock_push(key, now);
}

void lock_swipe(swipe_t dir)
{
    if (dir == SWIPE_LEFT) {
        lock_delete();
        return;
    }
    ux_feedback(FB_BUMP); // locked, nothing else opens
}

void lock_hold(void)
{
    if (!S.lock.entry[0]) {
        ux_feedback(FB_BUMP);
        return;
    }
    S.lock.entry[0] = 0;
    ux_feedback(FB_DOUBLE);
}

void lock_tick(int64_t now)
{
    if (S.lock.open && S.lock.pad && now > S.lock.pad_until) lock_sleep();
}

const char *lock_message(int64_t now, bool *alert)
{
    static char buf[32];
    *alert = true;
    if (now < S.lock.until) {
        int s = (int)((S.lock.until - now + SEC_US - 1) / SEC_US);
        if (s > LOCK_WAIT_MAX_US / SEC_US) s = (int)(LOCK_WAIT_MAX_US / SEC_US); // never more than the longest wait
        if (s < 60) snprintf(buf, sizeof buf, "Try again in %d s", s);
        else snprintf(buf, sizeof buf, "Try again in %d:%02d", s / 60, s % 60);
        return buf;
    }
    if (S.lock.msg[0]) return S.lock.msg;
    if (S.p.state == RUN_DONE) return "Time\xE2\x80\x99s up";
    if (zoo_first_needing()) return "An agent needs you"; // no name while locked
    *alert = false;
    return "Enter code";
}

void lock_draw_pad(void)
{
    const lay_lock_t *K = &F.L->lock;
    const theme_t *T = F.T;
    const float mid = F.L->base / 2;
    const int64_t now = F.now;
    const float age_ms = (float)(now - S.lock.shake) / 1000.0f;
    const float shake = S.lock.shake && age_ms < 420 ? sinf(age_ms / 18) * 10 * (1 - age_ms / 420) : 0;
    gfx_clear(T->face);
    if (zoo_first_needing()) rim_glow(rgb565(COLOR_AMBER), 0.55f + 0.45f * fabsf(sinf(F.t * 2.2f)), 0);
    // A running clock's time stays visible above the keypad.
    const ux_mode_t *run = clock_running();
    if (run) {
        char str[12];
        const bool pomo = run == &MODE_POMODORO;
        clock_format(pomo, now, str);
        const uint16_t col = pomo ? clock_warm565(pomo_remaining(now)) : T->ink;
        if (K->topX > 0) gfx_text(str, K->topX, K->topY, K->top, 600, col, ALIGN_LEFT, 0, 255);
        else gfx_text(str, mid, K->topY, K->top, 600, col, ALIGN_CENTER, 0, 255);
    }
    bool alert;
    const char *msg = lock_message(now, &alert);
    gfx_text(msg, mid + shake, K->titleY, K->title, 600, alert ? T->amber : T->ink, ALIGN_CENTER, K->titleW, 255);
    const int len = (int)strlen(S.lock.entry);
    for (int i = 0; i < 4; i++) {
        const float x = mid + shake + ((float)i - 1.5f) * K->dotGap;
        if (i < len) gfx_disc(x, K->dotsY, K->dotR, T->ink, 255);
        else gfx_ring(x, K->dotsY, K->dotR, 1.6f, i == len ? T->ink : T->sub, 255);
    }
    keypad_draw(now < S.lock.until);
}

// From the desktop app. A new code replaces a forgotten one, also while locked. Removing the code unlocks the board.
void lock_on_code(bool clear, const uint8_t *salt, int salt_len, uint32_t iter, const uint8_t hash[32])
{
    if (clear) {
        S.lock.has_code = false;
        memset(S.lock.hash, 0, sizeof S.lock.hash);
        S.lock.salt_len = 0;
        store_save_lock_code();
        lock_unlock();
        ux_log("lock", "code_removed");
        return;
    }
    S.lock.has_code = true;
    memcpy(S.lock.salt, salt, (size_t)salt_len);
    S.lock.salt_len = salt_len;
    S.lock.iter = iter;
    memcpy(S.lock.hash, hash, 32);
    S.lock.entry[0] = 0;
    S.lock.msg[0] = 0;
    S.lock.tries = 0;
    S.lock.strikes = 0;
    S.lock.until = 0;
    store_save_lock_code();
    store_save_lock();
    ux_toast("Lock code updated", 1600);
    ux_log("lock", "code_set");
}

const ux_mode_t MODE_LOCK = {
    .key = "lock", .label = "Lock", .color = 0xF59E0B,
    .choose = lock_choose,
};
