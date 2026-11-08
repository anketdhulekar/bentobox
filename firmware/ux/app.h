// The UX's state and the functions its parts share. Everything runs on one task, in ux_frame, so nothing here locks.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "../hal/hal.h"
#include "../link/link.h"
#include "color.h"
#include "gfx/gfx.h"
#include "layouts/layout.h"

#define MIN_US 60000000LL
#define SEC_US 1000000LL
#define MS_US 1000LL
#define PI_F 3.14159265f
#define BENTO_FW_VERSION "0.2.0" // what `board` says, and Settings › Device shows

typedef enum { RUN_IDLE, RUN_RUNNING, RUN_PAUSED, RUN_DONE } run_t;
typedef enum { SWIPE_UP, SWIPE_DOWN, SWIPE_LEFT, SWIPE_RIGHT } swipe_t;
typedef enum { WHERE_CLOCK, WHERE_ARC, WHERE_ZOO, WHERE_LIST } status_where_t;
typedef enum { VOICE_IDLE, VOICE_LISTENING, VOICE_CLEANING, VOICE_SENDING, VOICE_WAITING } voice_state_t;
typedef enum { LISTSYNC_IDLE, LISTSYNC_LOADING, LISTSYNC_FAILED } listsync_t;

typedef struct {
    int64_t now;
    float t;            // seconds, for the animations
    float dt_ms;
    const layout_t *L;
    const theme_t *T;
    bool capture_mask;  // drawing for a screen capture: hide the pairing code
} frame_t;

extern frame_t F;

// One mode.
typedef struct ux_mode {
    const char *key, *label;
    uint32_t color;               // its compartment in the menu, 0xRRGGBB
    bool (*choose)(int64_t now);  // an action rather than a screen (Lock)
    void (*enter)(int64_t now);   // about to open it (S.mode is still the one before)
    bool (*running)(void);        // a clock: true while it runs; only one clock runs at a time
    void (*draw)(void);
    void (*turn)(int dir, int64_t now);
    void (*tap)(float u, float v, int64_t now);
    void (*hold)(int64_t now);
    void (*swipe)(swipe_t dir, int64_t now);
    status_where_t status;
} ux_mode_t;

extern const ux_mode_t *const MODES[];
extern const int MODE_COUNT;
extern const ux_mode_t MODE_TIMER, MODE_POMODORO, MODE_SETTINGS, MODE_ZOO, MODE_LOCK, MODE_CLOCK;

// Split view: a running clock in the top arc, and below it the agents or the Clock, as last chosen.
typedef enum { SPLIT_AGENTS, SPLIT_CLOCK } split_t;
const ux_mode_t *mode_by_key(const char *key);
const ux_mode_t *clock_running(void); // the clock that runs, or NULL

typedef struct {
    link_agent_t a;
    int64_t since_us, started_us;
    uint8_t hat, outfit, acc; // the costume, from its id
    bool seen; // named by the whole list now arriving
} agent_t;

typedef struct {
    const ux_mode_t *mode;
    int theme;
    char task[LINK_TASK_LEN];
    int64_t task_id;
    link_project_t project;
    struct {
        run_t state;
        int64_t session_us, start_us;
        uint32_t rid;
        uint32_t of;      // the uid of the board that started the run; 0: this board
        int64_t shift_at; // a knob shift waits for the knob to rest
        bool shift_pending;
    } t;
    struct {
        run_t state;
        int set_min;
        int64_t remaining_us, total_us, last_us;
        uint32_t rid;
        uint32_t of;          // as the Timer's
        int64_t run_start_us; // when the running stretch began
    } p;
    // The clocks kept in step with the other boards, through the Mac (clocksync.c)
    struct {
        uint32_t seq, sent_seq; // this board's numbers for its `clock` messages, and the last one sent
        bool want_send;         // a `clock` is due: after a change here, or on a new connection
        bool pending;           // a change made here that the Mac hasn't answered yet
        bool mine;              // what's shown came from a change made here
        bool has_at;
        int64_t at_us;          // when what's shown last changed
        int64_t sent_at_us;     // when the clocks last sent had last changed: older than at_us, a change here hasn't gone out
        int64_t rev;            // the latest `clock` from the Mac in this connection
    } cs;
    int64_t alarm_until; // time's up: the alarm stops by itself then. 0 = quiet
    struct {
        bool open;
        int64_t until;
    } menu;
    struct {
        int page;  // settings.c
        int key;
        const ux_mode_t *from;
        float pos;
        float scroll; // a row list's, in layout units
        int forget_row;
        int64_t forget_until;
    } set;
    split_t split;     // what Split view shows below the clock: the last of Agents and Clock opened from the menu
    bool auto_switch;  // the Timer or Pomodoro screen moves to Split view by itself...
    int split_after_s; // ... after this long untouched, while a clock runs
    int64_t input_at;  // the last touch, knob turn or button
    int bright;
    bool autoscroll;
    int vol;
    bool autorotate;
    bool connected;   // a Mac is connected
    int64_t offline_since;
    bool offline_at_once; // a test: "Mac offline" shows now, rather than after 30 s
    link_toggl_t toggl;
    bool voice_ok;
    // Agents
    agent_t *agents;
    int n_agents;
    int n_needing;
    int sel;
    float pos;
    float knob_rate;      // how fast the knob's turning: it moves further when it's fast
    int64_t knob_at;
    float fling_v, fling_acc; // after a flick, agents a second, and what's built up towards the next
    bool agents_syncing;  // a whole list is arriving
    int64_t sync_begun, resync_at;
    int count_off;        // `state` messages in a row whose count isn't what the board holds, seconds apart
    int64_t count_off_at;
    int64_t picked_at, next_scroll;
    bool card;
    int64_t card_until;
    // Rotation
    float tilt;
    bool flat, face_down;
    int orient;
    bool turning;
    int turn_from, turn_to;
    int64_t turn_at;
    bool wanted;
    int wanted_to;
    int64_t wanted_since;
    // Messages
    char toast[64];
    int64_t toast_until;
    char hint[64];
    int64_t hint_until;
    // The lock. The code is set on the Mac; only a salted hash is kept here.
    struct {
        bool open, pad;
        int64_t pad_until;
        bool has_code;
        uint8_t salt[32];
        int salt_len;
        uint32_t iter;
        uint8_t hash[32];
        char entry[5];
        char msg[24];
        int tries;
        int strikes; // waits in a row: each one twice as long as the last, up to LOCK_WAIT_MAX
        int64_t until;
        int64_t shake;
    } lock;
    // The tasks and projects lists
    link_project_t *projects;
    int n_projects;
    link_task_t *tasks;
    int n_tasks;
    struct {
        bool open;
        link_list_t kind;
        int idx;
        float pos;
        bool touched;
        listsync_t sync;
        int req;
        int64_t until, sync_since;
        float scroll;
    } list;
    // Swipe-down check of Toggl's timer
    bool syncing;
    int sync_req;
    int64_t sync_since;
    float press_v; // where the finger is, for a hold; -1 if there's none
    // Voice
    voice_state_t voice;
    float mic_level;
    uint8_t wave[21];
    // Power
    hal_power_t power;
    // Saving
    int64_t dirty_since;
    // Test hooks
    float anim_override;
    bool has_anim_override;
    float pulse_override;
    bool has_pulse_override;
    bool has_burn_override;
    int burn_x, burn_y;
    bool has_tilt_override, flat_override, battery_override;
    int battery_percent;
    bool battery_charging;
} app_t;

extern app_t S;

// ---- Feedback and messages -----------------------------------------------------------------------------------------

void ux_feedback(feedback_t kind);
void ux_bump(const char *hint); // NULL: only the bump
void ux_hint(const char *text, int ms);
void ux_toast(const char *text, int ms);
bool ux_hinting(int64_t now);
void ux_mark_dirty(int64_t now);  // settings saved 2 s later
void ux_quiet(void);              // any touch or knob turn stops the time's-up alarm
void ux_log(const char *event, const char *detail); // a LOG line for the harness; no user text in detail

// The input router, from gestures and from the debug interface. As in the preview's turn, tap, hold and swipe.
void app_turn(int dir, int64_t now);
void app_tap(float u, float v, int64_t now);
void app_hold(int64_t now);
void app_swipe(swipe_t dir, int64_t now);
void app_button(hw_button_t b, int64_t now);
bool app_list_drag_wanted(void);
void app_list_drag(float dv, int64_t now);
float app_list_scroll(void); // how far the list on screen is scrolled: at 0, a pull down closes it instead
bool app_clock_drag_wanted(void);

// input.c
void input_poll(int64_t now);
void input_inject(const hw_event_t *e, int64_t now); // the same path as the panel's events
float input_hold_progress(int64_t now); // how far a press is towards a hold: 0 .. 1, and 0 if it can't become one

// store.c
void store_load(void);
void store_save_settings(void);
void store_save_lock(void);
void store_save_lock_code(void);
void store_save_list(link_list_t kind);

// modes/clock.c: what the Timer and the Pomodoro share, and the section above an agent
bool clock_tap_common(float u, float v, int64_t now);
void clock_draw(bool pomo);
void clock_draw_arch(const ux_mode_t *run);
void clock_format(bool pomo, int64_t now, char out[12]);
const char *clock_status_text(bool pomo, int64_t now, bool *amber);
void clock_tick(int64_t now);
void clock_set_task(const char *text, int64_t task_id, bool renames_entry, int64_t now);
void clock_set_project(const link_project_t *p, bool push, int64_t now);
void clock_clear_task_project(int64_t now);
bool clock_task_project_cleared(void);
float clock_pulse(int64_t now);
uint16_t clock_warm565(int64_t remaining_us);

// clocksync.c: the clocks every board shows, kept in step through the Mac
void clock_changed(int64_t now); // after any change made here to the Timer, the Pomodoro, the task or the project
void clocksync_connected(void);
void clocksync_tick(int64_t now);
void clocksync_on_mac(const link_clock_t *c, int64_t now);

// modes/timer.c
int64_t timer_elapsed(int64_t now);
void timer_flush_shift(int64_t now);
void timer_tick(int64_t now);
void timer_on_sync(const link_sync_t *s, int64_t now);
void timer_start_at(int64_t now);
void timer_pause_at(int64_t now);

// modes/pomodoro.c
int64_t pomo_remaining(int64_t now);
void pomo_reset(void);
float pomo_fraction(int64_t now);
void pomo_tick(int64_t now);
void pomo_set_remaining(int64_t remaining_us, int64_t now);

// modes/settings.c
void settings_tick(int64_t now);
bool settings_drag_wanted(void);
void settings_drag(float dv, int64_t now);
void settings_debug_rows(void (*emit)(const char *label, const char *value));
const char *settings_page_name(void); // for the debug interface: "main", "device", "connect", "agents", "clocks", "macs", "pair", "bright"
void settings_apply_all(void);

// modes/dayclock.c: the time of day, from the Mac's time
typedef struct { int hour, hour12, minute, day, month, year, weekday; bool pm; } daytime_t; // hour 0..23, month 1..12, weekday 0 Sunday
typedef enum { DT_TIME, DT_AMPM, DT_DATE, DT_DAY, DT_SHORT_DATE } daytime_text_t; // "8:02", "AM", "7th November 2026", "Saturday", "Sat 7th Nov"
bool daytime_get(int64_t now, daytime_t *d); // false until a Mac has told the time
const char *daytime_ordinal(int day);
void daytime_text(const daytime_t *d, daytime_text_t what, char *out, size_t n);
void daytime_draw_time(const daytime_t *d, float cx, float y, float size, float ampm, uint16_t ink, uint16_t sub, bool tabular);

// modes/zoo.c
#define ZOO_SCRUB_UNITS 28.0f // one agent per this much sideways drag
void zoo_tick(int64_t now);
void zoo_on_count(int n, int64_t now);
void zoo_agents_begin(int64_t now);
void zoo_agents_row(const link_agent_t *r, int64_t now);
void zoo_agents_gone(const char *id);
void zoo_agents_end(bool whole, int64_t now);
bool zoo_scrub_wanted(void);
void zoo_scrub(int dir, int64_t now);
void zoo_fling(float units_per_ms);
bool zoo_stop_fling(void); // true if there was one
agent_t *zoo_first_needing(void);
bool zoo_mac_offline(int64_t now);
const lay_zoo_t *zoo_layout(void); // alone, or smaller under a running clock

// modes/lock.c
void lock_engage(void);
void lock_unlock(void);
bool lock_wake(int64_t now);
void lock_draw_pad(void);
void lock_tap(float u, float v, int64_t now);
void lock_swipe(swipe_t dir);
void lock_hold(void);
void lock_tick(int64_t now);
void lock_on_code(bool clear, const uint8_t *salt, int salt_len, uint32_t iter, const uint8_t hash[32]);
const char *lock_message(int64_t now, bool *alert);

// widgets
void menu_prepare(void);
void menu_open(int64_t now);
void menu_close(void);
void menu_tap(float u, float v, int64_t now);
void menu_tick(int64_t now);
void menu_draw(void);

void status_draw(status_where_t where);
void toast_draw(void);

float rim_arch_start(float inset, float radius, float y);
void rim_point(float f, float inset, float radius, float *x, float *y);
void rim_draw_clock_ring(uint16_t color);
void rim_draw_clock_arc(uint16_t color, float fraction);
void rim_draw_hold(uint16_t color, float fraction);
void rim_draw_arch_ring(uint16_t color);
void rim_draw_arch_arc(uint16_t color, float fraction);
void rim_glow(uint16_t color, float strength, float inset);

// widgets/list.c
typedef enum { DOT_NONE, DOT_FILLED, DOT_HOLLOW } list_dot_t;
typedef struct {
    const char *label;
    const char *value;   // right-aligned, or NULL
    bool chevron, check, muted, danger;
    list_dot_t dot;
    uint32_t dot_rgb;
} list_row_t;
typedef struct {
    const char *title;
    const char *hint1, *hint2; // a wheel's two lines
    const char *below;         // a row list's line under the rows
    int count, cursor;
    float pos;                 // the wheel's glide
    float scroll;              // a row list's scroll, in layout units from its top
    bool scrollable;           // rows keep their height and scroll, rather than shrinking to fit
    void (*row)(int i, list_row_t *out);
} list_view_t;
void list_draw(const list_view_t *v);
int list_row_at(const list_view_t *v, float v_pos); // -1 if none
float list_row_h(const list_view_t *v);
float list_max_scroll(const list_view_t *v);

// widgets/picker.c: the tasks and projects lists
void picker_open(link_list_t kind, int64_t now);
void picker_close(bool choose, int64_t now);
void picker_turn(int dir, int64_t now);
void picker_tap(float u, float v, int64_t now);
void picker_tick(int64_t now);
void picker_draw(void);
void picker_on_list(link_list_t kind, int req, const void *rows, int count, int64_t now);
void picker_on_fetch_failed(link_list_t kind, int req, int64_t now);
void picker_remember_task(const char *desc, int64_t task_id, const link_project_t *project);
void picker_drag(float dv, int64_t now);
const link_project_t *picker_find_project(int64_t id);

// widgets/character.c
typedef struct {
    float sx, sy, alpha;
    uint32_t body;
    int eyes; // 0 open, 1 closed, 2 happy, 3 wide, 4 x
    int look, arm_l, arm_r, prop;
    bool leg_a, leg_b;
    float hat_dy, flutter, t;
} pose_t;
// A speech bubble's picture.
typedef enum { BUBBLE_QUESTION, BUBBLE_CHECK, BUBBLE_X, BUBBLE_ARROW, BUBBLE_BASH, BUBBLE_EDIT, BUBBLE_READ, BUBBLE_WEB, BUBBLE_SEARCH, BUBBLE_THINK, BUBBLE_SUB, BUBBLE_COUNT } bubble_t;
#define BODY_CLAUDE 0xD97757u
#define BODY_SICK 0xA47E73u
pose_t character_pose(agent_status_t st, float t, int i);
void character_costume(agent_t *a);
void character_draw(const agent_t *a, const pose_t *p, float x, float feet, float sc);
void character_draw_default(const pose_t *p, float x, float feet, float sc);
void character_draw_hat(const agent_t *a, int frame, float x, float feet, float sc); // alone: frame 0 .. 3 of its tumble
void character_bubble(bubble_t icon, float x, float y, uint16_t fill, uint16_t ink, float scale, int alpha);

// widgets/fx.c: the effects around the agent on screen. Nothing runs for any other agent.
void fx_tick(void);
void fx_on_row(const agent_t *a, int was_st, bubble_t was_icon); // was_st: -1 if it's new
void fx_on_gone(const agent_t *a); // before it leaves the list
void fx_after_removal(void);
void fx_on_tap(bool rex);
const agent_t *fx_leaving(void);   // the copy of the agent that's going, while it shows
bool fx_poofed(void);              // it's gone in the smoke: draw no character
bubble_t fx_activity_icon(const char *act);
bool fx_body(pose_t *p, float *x, float *feet, float sc); // hop, drop, shake and squash; true if it's in the air
int fx_text_alpha(void);
void fx_draw_bubble(float x, float feet, float sc);
void fx_draw_particles(const lay_zoo_t *Z, float top);
#ifdef BENTO_DEBUG_INTERFACE
#define FX_KINDS 8
typedef struct {
    int particles, peak, cap, refused;
    int counts[FX_KINDS];
    const char *kinds[FX_KINDS];
    const char *bubble, *last, *leaving_name;
    bool leaving, poofed, dropping, hopping, shaking;
    float squash;
} fx_debug_t;
void fx_debug_read(fx_debug_t *out);
bool fx_debug_command(const char *arg, int64_t now);
#endif

// widgets/icons.c
void icon_mic(float x, float y, float s, uint16_t c);
void icon_status_lock(float x, float y, float s, uint16_t c);
void icon_status_lock_contrast(float x, float y, float s);
void icon_check(float x, float y, float size, uint16_t c, int alpha);
void icon_delete_key(float x, float y, float key, uint16_t c, int alpha);

// widgets/keypad.c
void keypad_draw(bool waiting);
const char *keypad_key_at(float u, float v);

// voice.c
void voice_init(void);
void voice_start(int64_t now);
void voice_stop(int64_t now);   // a tap while listening
void voice_update(int64_t now);
void voice_on_transcript(const link_transcript_t *t, int64_t now);
void voice_cancel(void);

// screen.c: the frame goes out, rotated if the board is turned
void screen_present(uint64_t dirty_bands, int orient, bool turning, float shown_deg);
const uint16_t *screen_shown(void); // the frame as the panel shows it

// debug.c
#ifdef BENTO_DEBUG_INTERFACE
void debug_init(void);
void debug_poll(int64_t now);
void debug_frame_stats(int64_t record_us, int64_t paint_us, int64_t present_us, int bands, int64_t frame_us);
void debug_why(uint64_t dirty);
void debug_fpu(const char *step); // the FPU's control register, checked after each step of a frame
bool debug_verify_on(void);
void debug_verify_result(int stale_bands);
#endif

// ux.c
void ux_redraw_for_capture(uint16_t *target);
