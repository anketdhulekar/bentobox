// The link to the desktop app: protocol/PROTOCOL.md. JSON lines over hal_link_*. Everything arrives through
// link_poll, on the UX's task, and goes to the handlers given to link_init. Nothing here trusts what it receives: any
// line, however long, deep or broken, is either understood or dropped.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LINK_TASK_LEN 96
#define LINK_NAME_LEN 48
#define LINK_MAX_PROJECTS 64
#define LINK_MAX_TASKS 20
#define LINK_MAX_AGENTS 512
#define LINK_MAX_SUBS 6
#define LINK_OUTBOX_SIZE 64

typedef struct {
    int64_t id;    // 0 = none, -1 = never chosen
    uint32_t color; // 0xRRGGBB
    char name[LINK_NAME_LEN];
} link_project_t;

typedef struct {
    char desc[LINK_TASK_LEN];
    int64_t task_id;
    link_project_t project;
} link_task_t;

typedef enum { AG_WORKING, AG_NEEDS, AG_DONE, AG_IDLE, AG_FAILED } agent_status_t;
typedef enum { KIND_CLAUDE, KIND_CODEX, KIND_GROK } agent_kind_t;

typedef struct {
    char id[12], type[28], act[52];
} link_sub_t;

typedef struct {
    char id[12];
    char name[44], proj[44], act[52], note[68], where[64];
    agent_status_t st;
    agent_kind_t kind;
    bool background, pane; // pane: a tap can bring its terminal pane to the front (in Rex or Herdr)
    int32_t age_s, up_s;
    uint8_t nsubs;
    link_sub_t subs[LINK_MAX_SUBS];
} link_agent_t;

typedef enum { TOGGL_UNKNOWN, TOGGL_OK, TOGGL_NO_KEY, TOGGL_OFFLINE, TOGGL_BACKOFF } link_toggl_t;

typedef struct {
    link_toggl_t toggl;
    bool voice, has_voice;
    int n; // how many agents the Mac has, or -1 if it didn't say
} link_state_t;

typedef enum { LIST_PROJECTS, LIST_TASKS } link_list_t;

typedef enum { SYNC_IN_SYNC, SYNC_ADOPTED, SYNC_STOPPED, SYNC_FAILED } link_sync_result_t;

typedef struct {
    int req;
    link_sync_result_t result;
    uint32_t rid;  // adopted: the app's number for this run, already reserved. 0 if it sent none we can use
    char desc[LINK_TASK_LEN];
    int64_t task_id;
    link_project_t project; // id 0 if it has none
    int64_t start; // Unix seconds
} link_sync_t;

// The clocks every board shows (PROTOCOL.md, `clock`). Times are this board's, in hal_now_us microseconds: the link
// turns the other side's into these, from how long before its `now` each one was.
typedef struct {
    int st;               // 0 idle, 1 running, 2 paused, 3 done: the UX's run_t
    int64_t acc_us;       // Timer: counted before the running stretch
    int64_t start_us;     // Timer: when the running stretch began (running only)
    int set_min;          // Pomodoro: its length setting
    int64_t total_us;     // Pomodoro: the length of this countdown
    int64_t left_us;      // Pomodoro: what was left at `at`
    uint32_t rid;         // the Toggl run of the stretch that runs, or ran last
    uint32_t of;          // the uid of the board that started it; 0: this board
} link_clock_part_t;

typedef struct {
    int64_t at_us;        // when the clocks last changed
    bool has_at;          // false: they haven't changed since this board started
    char task[LINK_TASK_LEN];
    int64_t task_id;
    link_project_t project; // id -1: never chosen, 0: "No project"
    link_clock_part_t timer, pomo;
    // From the Mac only
    int64_t rev;          // its count of changes taken
    uint32_t from;        // the board that made this change; 0: this board
    uint32_t seq;         // that board's number for it
    bool has_for;
    uint32_t for_seq;     // the answer to this board's `clock` with this seq
} link_clock_t;

typedef struct {
    int vid;
    const char *text;  // NULL if it failed
    int64_t project;   // 0 if none
    bool no_speech;
} link_transcript_t;

typedef struct {
    void (*connected)(const char *host);
    void (*disconnected)(void);
    void (*state)(const link_state_t *s);
    // The agents arrive in pieces. A whole list: begin, then its pages. Any `agents` message: its rows, then who's gone,
    // then end. whole is true at the end of the last page of a whole list, when anyone it didn't name is gone too.
    void (*agents_begin)(void);
    void (*agent_row)(const link_agent_t *a);
    void (*agent_gone)(const char *id);
    void (*agents_end)(bool whole);
    void (*entry_rejected)(uint32_t rid);
    // A whole list has arrived. rows are link_project_t or link_task_t. req is the fetch it answers, or 0 if it came
    // unasked, as the first load does.
    void (*list)(link_list_t kind, int req, const void *rows, int count);
    void (*fetch_failed)(link_list_t kind, int req);
    void (*sync)(const link_sync_t *s);
    void (*transcript)(const link_transcript_t *t);
    // A new lock code (clear: removed). Salt and hash are already checked.
    void (*lock_code)(bool clear, const uint8_t *salt, int salt_len, uint32_t iter, const uint8_t hash[32]);
    void (*clock)(const link_clock_t *c);
} link_handlers_t;

void link_init(const link_handlers_t *h, const char *fw_version, bool (*lock_is_set)(void));
void link_poll(int64_t now_us);

const char *link_uid(void); // the board's own 8 hex digits, for `board`
uint32_t link_uid_value(void); // the same, as a number: what `of` and `from` hold
bool link_up(void);     // a Mac is connected, whether or not it has said hello
bool link_ready(void);  // ... and it has said hello

bool link_clock_known(void);
int64_t link_unix_now(int64_t now_us);   // Unix seconds; only when the clock is known
int32_t link_utc_offset(void);

// ---- Messages to the Mac -------------------------------------------------------------------------------------------

bool link_send_tap(const char *agent_id);
bool link_send_resync(void); // the agents don't match the count in `state`: the whole list again, please

// Every change to a run goes to the outbox, saved in flash, so it reaches the Mac even if it's away now.
// A run is named by its rid, and by `of`, the uid of the board that started it: 0 is this board. A start is always this
// board's own. Project 0 means none.
uint32_t link_toggl_start(const char *desc, int64_t task_id, int64_t project, int64_t now_us); // returns the new rid
uint32_t link_toggl_new_rid(void);
enum { LINK_UPD_DESC = 1, LINK_UPD_TASK = 2, LINK_UPD_PROJECT = 4 };
void link_toggl_update(uint32_t rid, uint32_t of, unsigned fields, const char *desc, int64_t task_id, int64_t project, int64_t now_us);
void link_toggl_shift(uint32_t rid, uint32_t of, int64_t start_mono_us, int64_t now_us);
void link_toggl_stop(uint32_t rid, uint32_t of, int64_t at_us); // at_us: when it stopped, now or earlier
bool link_take_dropped(void); // true once after the outbox had to drop a message

int link_fetch(link_list_t kind); // returns req, 0 if no Mac
int link_sync(uint32_t rid);      // returns req, 0 if no Mac
// This board's clocks, after a change made here (changed) or once after each connection. false if no Mac listens.
bool link_send_clock(const link_clock_t *c, bool changed, uint32_t seq, int64_t now_us);

bool link_send_line(const char *line, size_t len); // a whole line, with its newline. false if no Mac listens.

// ---- Voice ---------------------------------------------------------------------------------------------------------

// IMA ADPCM, 4 bits a sample, low nibble first: the same steps as protocol/gen_vectors.py.
typedef struct { int predictor, index; } adpcm_state_t;
size_t adpcm_encode(const int16_t *pcm, size_t samples, adpcm_state_t *st, uint8_t *out);
size_t b64_encode(const uint8_t *data, size_t len, char *out); // adds a NUL; returns the length without it

// The longest part of src that fits in n bytes with its NUL, without cutting a UTF-8 character.
void utf8_copy(char *dst, size_t n, const char *src);

// ---- For the debug interface -----------------------------------------------------------------------------------------

typedef bool (*link_tx_fn)(const char *line, size_t len);
void link_override_transport(link_tx_fn tx); // non-NULL: lines go here and come from link_feed_line, with no Bluetooth
void link_feed_line(const char *line);       // one line, as if it came from the Mac
void link_force_offline(bool off);           // as if no Mac were connected
int link_outbox_count(void);
uint32_t link_outbox_next_id(void);
void link_lines_stats(uint32_t *good, uint32_t *bad);
