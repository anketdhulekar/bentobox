#pragma once
#include "link.h"

void outbox_init(void);
uint32_t outbox_new_rid(void);
void outbox_note_rid(uint32_t rid); // the app chose this rid: our own count moves past it, for good
void outbox_note_rid_of(uint32_t rid, uint32_t of); // a run named in `clock`: if it's this board's own, the same
void outbox_start(uint32_t rid, const char *desc, int64_t task_id, int64_t project, int64_t now_us);
void outbox_update(uint32_t rid, uint32_t of, unsigned fields, const char *desc, int64_t task_id, int64_t project, int64_t now_us);
void outbox_shift(uint32_t rid, uint32_t of, int64_t start_mono_us, int64_t now_us);
void outbox_stop(uint32_t rid, uint32_t of, int64_t at_us);
void outbox_ack(uint32_t id);
void outbox_on_connected(void);
void outbox_flush(int64_t now_us);
bool outbox_take_dropped(void);
int outbox_count(void);
uint32_t outbox_next_id(void);
