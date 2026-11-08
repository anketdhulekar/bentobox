// The outbox: every change to a run, saved in flash until the Mac acknowledges it, so nothing is lost while it's away.
// At most 64. When full, the oldest update goes first, then the oldest of any kind. A message's id never repeats, even
// across restarts. A board that doesn't know the time yet holds its messages, then fills in "at" from how long ago each
// one happened.
#include <stdio.h>
#include <string.h>

#include "../hal/hal.h"
#include "json_out.h"
#include "link_internal.h"

typedef enum { OP_START, OP_UPDATE, OP_SHIFT, OP_STOP } op_t;

typedef struct {
    uint32_t version_size; // guards a saved message's layout
    uint32_t id, rid;
    uint32_t boot;      // the restart it happened in
    int64_t mono_us;    // when it happened, in that restart's time
    int64_t at;         // Unix seconds, 0 until known
    int64_t start_mono_us; // shift: the new start
    int64_t task_id, project;
    uint8_t op, fields, at_known, slot;
    char desc[LINK_TASK_LEN];
    uint32_t of; // the uid of the board whose run this is; 0: this board's
} ob_msg_t;

// `of` sits where the struct had 4 bytes of padding, which add() always zeroed: so a message saved before it existed
// keeps the same size, still loads, and reads as this board's own.
_Static_assert(sizeof(ob_msg_t) == 160, "ob_msg_t's saved layout changed: old outboxes would be dropped");

static struct {
    ob_msg_t *q;          // oldest first
    bool *sent;
    int count;
    uint32_t next_id, next_rid, boot;
    bool dropped;
} O;

// A saved message's version and size together. Version 1 was the size alone; its layout is today's, with `of` zero.
#define MSG_TAG ((2u << 24) | (uint32_t)sizeof(ob_msg_t))
#define MSG_TAG_V1 ((uint32_t)sizeof(ob_msg_t))

static void slot_key(char key[8], int slot) { snprintf(key, 8, "ob%d", slot); }

static void save_msg(const ob_msg_t *m)
{
    char key[8];
    slot_key(key, m->slot);
    hal_store_set(key, m, sizeof *m);
}

static void erase_msg(const ob_msg_t *m)
{
    char key[8];
    slot_key(key, m->slot);
    hal_store_erase(key);
}

void outbox_init(void)
{
    O.q = hal_alloc_large(LINK_OUTBOX_SIZE * sizeof(ob_msg_t));
    O.sent = hal_alloc_large(LINK_OUTBOX_SIZE * sizeof(bool));
    O.count = 0;
    size_t n = sizeof O.next_id;
    if (!hal_store_get("ob_next", &O.next_id, &n) || n != sizeof O.next_id) O.next_id = 1;
    n = sizeof O.next_rid;
    if (!hal_store_get("ob_rid", &O.next_rid, &n) || n != sizeof O.next_rid) O.next_rid = 1;
    n = sizeof O.boot;
    if (!hal_store_get("ob_boot", &O.boot, &n) || n != sizeof O.boot) O.boot = 0;
    O.boot++;
    hal_store_set("ob_boot", &O.boot, sizeof O.boot);
    if (!O.q || !O.sent) return;
    memset(O.sent, 0, LINK_OUTBOX_SIZE * sizeof(bool));
    for (int slot = 0; slot < LINK_OUTBOX_SIZE; slot++) {
        char key[8];
        slot_key(key, slot);
        ob_msg_t m;
        n = sizeof m;
        if (!hal_store_get(key, &m, &n)) continue;
        if (n != sizeof m || (m.version_size != MSG_TAG && m.version_size != MSG_TAG_V1) || m.op > OP_STOP) {
            hal_store_erase(key);
            continue;
        }
        m.slot = (uint8_t)slot;
        m.desc[sizeof m.desc - 1] = 0;
        int at = O.count++;
        while (at > 0 && O.q[at - 1].id > m.id) { // in id order
            O.q[at] = O.q[at - 1];
            at--;
        }
        O.q[at] = m;
        if (m.id >= O.next_id) O.next_id = m.id + 1;
        if (m.rid >= O.next_rid) O.next_rid = m.rid + 1;
    }
}

static void remove_at(int i)
{
    erase_msg(&O.q[i]);
    memmove(&O.q[i], &O.q[i + 1], (size_t)(O.count - i - 1) * sizeof(ob_msg_t));
    memmove(&O.sent[i], &O.sent[i + 1], (size_t)(O.count - i - 1) * sizeof(bool));
    O.count--;
}

static int free_slot(void)
{
    for (int slot = 0; slot < LINK_OUTBOX_SIZE; slot++) {
        bool used = false;
        for (int i = 0; i < O.count; i++) {
            if (O.q[i].slot == slot) used = true;
        }
        if (!used) return slot;
    }
    return 0;
}

static ob_msg_t *add(op_t op, uint32_t rid, int64_t now_us)
{
    if (!O.q) return NULL;
    if (O.count >= LINK_OUTBOX_SIZE) {
        int victim = 0;
        for (int i = 0; i < O.count; i++) {
            if (O.q[i].op == OP_UPDATE) {
                victim = i;
                break;
            }
        }
        remove_at(victim);
        O.dropped = true;
    }
    ob_msg_t *m = &O.q[O.count];
    memset(m, 0, sizeof *m);
    m->version_size = MSG_TAG;
    m->id = O.next_id++;
    m->rid = rid;
    m->boot = O.boot;
    m->mono_us = now_us;
    m->op = (uint8_t)op;
    m->slot = (uint8_t)free_slot();
    O.sent[O.count] = false;
    O.count++;
    hal_store_set("ob_next", &O.next_id, sizeof O.next_id);
    return m;
}

static bool resolve(ob_msg_t *m, int64_t now_us);

// Saves a new message. If the clock is known, its real time goes with it, so a restart can't take that away.
static void store_new(ob_msg_t *m, int64_t now_us)
{
    if (!resolve(m, now_us)) save_msg(m);
}

uint32_t outbox_new_rid(void)
{
    const uint32_t rid = O.next_rid++;
    hal_store_set("ob_rid", &O.next_rid, sizeof O.next_rid);
    return rid;
}

void outbox_note_rid(uint32_t rid)
{
    if (rid < O.next_rid) return;
    O.next_rid = rid + 1;
    hal_store_set("ob_rid", &O.next_rid, sizeof O.next_rid);
}

void outbox_note_rid_of(uint32_t rid, uint32_t of)
{
    if (!of) outbox_note_rid(rid);
}

void outbox_start(uint32_t rid, const char *desc, int64_t task_id, int64_t project, int64_t now_us)
{
    ob_msg_t *m = add(OP_START, rid, now_us);
    if (!m) return;
    utf8_copy(m->desc, sizeof m->desc, desc);
    m->task_id = task_id;
    m->project = project;
    store_new(m, now_us);
}

void outbox_update(uint32_t rid, uint32_t of, unsigned fields, const char *desc, int64_t task_id, int64_t project, int64_t now_us)
{
    ob_msg_t *m = add(OP_UPDATE, rid, now_us);
    if (!m) return;
    m->of = of;
    m->fields = (uint8_t)fields;
    utf8_copy(m->desc, sizeof m->desc, desc);
    m->task_id = task_id;
    m->project = project;
    store_new(m, now_us);
}

void outbox_shift(uint32_t rid, uint32_t of, int64_t start_mono_us, int64_t now_us)
{
    ob_msg_t *m = add(OP_SHIFT, rid, now_us);
    if (!m) return;
    m->of = of;
    m->start_mono_us = start_mono_us;
    store_new(m, now_us);
}

void outbox_stop(uint32_t rid, uint32_t of, int64_t at_us)
{
    const int64_t now_us = hal_now_us();
    ob_msg_t *m = add(OP_STOP, rid, at_us < now_us ? at_us : now_us);
    if (!m) return;
    m->of = of;
    store_new(m, now_us);
}

void outbox_ack(uint32_t id)
{
    for (int i = 0; i < O.count; i++) {
        if (O.q[i].id == id) {
            remove_at(i);
            return;
        }
    }
}

void outbox_on_connected(void)
{
    if (O.sent) memset(O.sent, 0, LINK_OUTBOX_SIZE * sizeof(bool));
}

// How long the restarts after this one, and before the current one, ran: from each one's first saved event to its last,
// for those with no clock known. Their times are worked out back to back (see resolve).
static int64_t later_restarts_s(uint32_t boot)
{
    int64_t total_us = 0;
    for (int i = 0; i < O.count; i++) {
        const uint32_t b = O.q[i].boot;
        if (b <= boot || b >= O.boot || O.q[i].at_known) continue;
        bool first = true; // count each restart once: at its first message in the queue
        for (int j = 0; j < i; j++) {
            if (O.q[j].boot == b && !O.q[j].at_known) first = false;
        }
        if (!first) continue;
        int64_t lo = O.q[i].mono_us, hi = O.q[i].mono_us;
        for (int j = i; j < O.count; j++) {
            if (O.q[j].boot != b || O.q[j].at_known) continue;
            const int64_t t = O.q[j].op == OP_SHIFT && O.q[j].start_mono_us < O.q[j].mono_us ? O.q[j].start_mono_us : O.q[j].mono_us;
            if (t < lo) lo = t;
            if (O.q[j].mono_us > hi) hi = O.q[j].mono_us;
        }
        total_us += hi - lo;
    }
    return total_us / 1000000;
}

// Fills in the times, if the clock now allows it. false if it must wait.
static bool resolve(ob_msg_t *m, int64_t now_us)
{
    if (m->at_known) return true;
    if (!link_clock_known()) return false;
    const int64_t unix_now = link_unix_now(now_us);
    if (m->boot == O.boot) {
        m->at = unix_now - (now_us - m->mono_us) / 1000000;
        if (m->op == OP_SHIFT) m->at = unix_now - (now_us - m->start_mono_us) / 1000000;
    } else {
        // From before a restart, with no clock known then. That restart took the time base with it, so the best guess
        // is that the restarts with no clock ran back to back, up to this one: the newest ended when this restart began,
        // and each one before it ended where the next began. The events keep their spacing, so a run keeps its length:
        // if they all got the same time, the run would look like an accidental tap, and be deleted.
        int64_t last_us = m->mono_us;
        for (int i = 0; i < O.count; i++) {
            if (O.q[i].boot == m->boot && O.q[i].mono_us > last_us) last_us = O.q[i].mono_us;
        }
        const int64_t began = unix_now - now_us / 1000000;
        const int64_t when_us = m->op == OP_SHIFT ? m->start_mono_us : m->mono_us;
        m->at = began - later_restarts_s(m->boot) - (last_us - when_us) / 1000000;
    }
    m->at_known = 1;
    save_msg(m);
    return true;
}

static size_t format_msg(const ob_msg_t *m, char *buf, size_t cap)
{
    static const char *const OPS[4] = {"start", "update", "shift", "stop"};
    jw_t w;
    jw_init(&w, buf, cap);
    jw_begin(&w, NULL);
    jw_str(&w, "t", "toggl");
    jw_i64(&w, "id", m->id);
    jw_str(&w, "op", OPS[m->op]);
    jw_i64(&w, "rid", m->rid);
    if (m->of && m->op != OP_START) {
        char of[9];
        snprintf(of, sizeof of, "%08lx", (unsigned long)m->of);
        jw_str(&w, "of", of);
    }
    switch (m->op) {
    case OP_START:
        jw_i64(&w, "at", m->at);
        jw_str(&w, "desc", m->desc);
        jw_i64(&w, "task_id", m->task_id);
        jw_i64(&w, "project", m->project);
        break;
    case OP_UPDATE:
        if (m->fields & LINK_UPD_DESC) jw_str(&w, "desc", m->desc);
        if (m->fields & LINK_UPD_TASK) jw_i64(&w, "task_id", m->task_id);
        if (m->fields & LINK_UPD_PROJECT) jw_i64(&w, "project", m->project);
        break;
    case OP_SHIFT:
        jw_i64(&w, "start", m->at);
        break;
    default:
        jw_i64(&w, "at", m->at);
        break;
    }
    jw_end(&w);
    return jw_finish(&w) ? w.len : 0;
}

void outbox_flush(int64_t now_us)
{
    char line[512];
    int budget = 8;
    for (int i = 0; i < O.count && budget > 0; i++) {
        if (O.sent[i]) continue;
        if (!resolve(&O.q[i], now_us)) return; // order holds: the rest wait too
        const size_t n = format_msg(&O.q[i], line, sizeof line);
        if (!n || !link_send_line(line, n)) return;
        O.sent[i] = true;
        budget--;
    }
}

bool outbox_take_dropped(void)
{
    const bool d = O.dropped;
    O.dropped = false;
    return d;
}

int outbox_count(void) { return O.count; }
uint32_t outbox_next_id(void) { return O.next_id; }
