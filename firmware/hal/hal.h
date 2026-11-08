// The interface between the UX and the hardware. firmware/ux/ and firmware/link/ include only this file for
// anything that touches a board, so they compile on the Mac for the simulator. A board implements all of it,
// through firmware/boards/, firmware/drivers/ and firmware/platform/. The simulator implements it in firmware/sim/.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ---- The board's facts ------------------------------------------------------------------------------------

typedef enum { SHAPE_ROUND, SHAPE_SQUARE } screen_shape_t;
typedef enum { PANEL_LCD, PANEL_AMOLED } panel_t;

typedef struct {
    bool knob;    // a rotary knob: HW_KNOB events
    bool haptics; // a vibration motor
    bool mic;     // hal_mic_* works
    bool speaker; // sound through hal_feedback
    bool motion;  // hal_motion_read works: auto-rotate and face-down mute
    bool battery; // hal_power_read reports a level
} board_has_t;

typedef struct {
    const char *name; // the folder under firmware/boards/, such as "knob-lcd-1.8"
    const char *title; // what people see, in Bluetooth and the desktop app, such as "Bento Knob"
    int width, height;
    screen_shape_t shape;
    panel_t panel;
    int mount_deg;     // how the panel sits on the board: the upright screen is this far round from the panel's own
    board_has_t has;
} board_info_t;

// Defined in firmware/boards/<board>/board_info.c, which holds only these portable facts.
// The simulator fills BOARD in at start, so there it's writable (the Makefile sets BOARD_CONST to nothing); on a board
// it's a constant.
#ifndef BOARD_CONST
#define BOARD_CONST const
#endif
extern BOARD_CONST board_info_t BOARD;

void hal_init(void); // brings up everything below; call it once before anything else

// ---- Time and memory ----------------------------------------------------------------------------------------

int64_t hal_now_us(void);    // monotonic, since start
void hal_sleep_ms(int ms);   // yields to other tasks
void *hal_alloc_large(size_t bytes); // for frame buffers and big tables: PSRAM on a board. NULL on failure.
uint32_t hal_random(void);   // for the board's uid; a real random source on a board
void hal_device_mac(uint8_t mac[6]); // the chip's own MAC address, as esptool reads it: to show in Settings

// ---- The screen ---------------------------------------------------------------------------------------------

// Send rows y0 .. y0 + rows - 1 of a full frame to the panel. fb holds BOARD.width x BOARD.height native
// RGB565 pixels, row by row. y0 and rows are multiples of HAL_BAND_ROWS. The driver swaps bytes if its panel needs
// it, and may return before the transfer ends, but it must have copied what it needs: fb can change right after.
#define HAL_BAND_ROWS 24
void hal_screen_present(const uint16_t *fb, int y0, int rows);
#define HAL_BRIGHT_LEVELS 10
void hal_screen_brightness(int level); // 0 dimmest .. HAL_BRIGHT_LEVELS - 1 brightest

// ---- Input --------------------------------------------------------------------------------------------------

typedef enum { HW_NONE, HW_TOUCH_DOWN, HW_TOUCH_MOVE, HW_TOUCH_UP, HW_KNOB, HW_BUTTON } hw_kind_t;
typedef enum { BUTTON_MENU, BUTTON_UP, BUTTON_DOWN } hw_button_t; // the amoled-2.16's PWR, User and BOOT

typedef struct {
    hw_kind_t kind;
    int x, y;           // HW_TOUCH_*: panel pixels, before any rotation
    int step;           // HW_KNOB: +1 clockwise, -1 anticlockwise, one per detent
    hw_button_t button; // HW_BUTTON: a press, already debounced
} hw_event_t;

// The next event, oldest first. false when there are none. Gestures (tap, hold, swipe) are the UX's job.
bool hal_input_poll(hw_event_t *e);

// ---- Feedback -----------------------------------------------------------------------------------------------

typedef enum {
    FB_TICK,   // a knob detent, or a key
    FB_BUMP,   // a limit, or something that isn't available
    FB_TAP,    // start, pause, change
    FB_DOUBLE, // reset, unlock
    FB_ALERT,  // an agent needs you
    FB_ALARM,  // time's up: vibrate for 30 s, or sound an alarm, until FB_ALARM_STOP
    FB_ALARM_STOP,
    FB_YAY,    // the agent on screen finished
    FB_POOF,   // the agent on screen ended
    FB_DROP,   // a new agent falls onto the screen
    FB_LAND,   // and lands
    FB_COUNT
} feedback_t;

// The UX asks by name, and the board gives what it has. sound_allowed is false while locked or muted:
// the board stays silent then, and vibration, if it has any, still plays.
#define HAL_ALARM_S 30 // time's up vibrates (and sounds) this long at most: 30 s continuous is as long as a motor should run
void hal_feedback(feedback_t kind, bool sound_allowed);
void hal_volume(int level); // 0 (off) .. 10

// ---- Motion, power, microphone ------------------------------------------------------------------------------

// Gravity in g along the panel: +x is the panel's right, +y its bottom, +z out of the screen. false if unavailable.
bool hal_motion_read(float *x, float *y, float *z);

typedef struct {
    bool ok;
    int percent;   // 0 .. 100, -1 = unknown
    bool usb;      // powered from USB
    bool charging;
} hal_power_t;
void hal_power_read(hal_power_t *out);

// 16 kHz mono, 16-bit. Speech tracking and clean-up are the UX's job.
bool hal_mic_start(void);
int hal_mic_read(int16_t *out, int max); // samples copied; 0 if none are ready; never blocks
void hal_mic_stop(void);

// ---- Storage and crypto -------------------------------------------------------------------------------------

// Key/value storage that survives power-off. Keys are at most 15 characters.
bool hal_store_get(const char *key, void *out, size_t *len); // *len: in, the buffer's size; out, the value's size
bool hal_store_set(const char *key, const void *data, size_t len);
void hal_store_erase(const char *key);

void hal_pbkdf2_sha256(const char *password, const uint8_t *salt, size_t salt_len, uint32_t iterations, uint8_t out[32]);

// ---- The link to the desktop app (protocol/PROTOCOL.md) -------------------------------------------------------

typedef struct {
    bool connected;  // a paired Mac, over an encrypted link
    bool pairing;    // a new Mac is pairing: show the code
    uint32_t code;   // the pairing code, 0 .. 999999. Show it on screen only; never log it.
} hal_link_status_t;
void hal_link_status(hal_link_status_t *out);

int hal_link_read(uint8_t *out, int max); // bytes from the desktop app; 0 if none; never blocks; lines may split
bool hal_link_send(const char *data, size_t len); // false if no paired Mac listens; splits data to fit the link

// When a hello arrives: may this Mac use the board now? NULL = yes, else the reason for a busy answer.
const char *hal_link_refuse_reason(void);
void hal_link_kick(const char *reason);      // send busy, and disconnect shortly after
void hal_link_remember_host(const char *name); // the connected Mac's name, for Settings

// ---- The debug interface, for the test harness (docs/DEBUG.md) --------------------------------------------------

// Text over USB on a board, and a socket in the simulator. Compiled in when BENTO_DEBUG_INTERFACE is defined.
int hal_debug_read(char *out, int max);               // bytes from the harness; 0 if none; never blocks
void hal_debug_write(const char *data, size_t len);   // one whole write, so other logs can't split a line
// Optional: debug.c has weak stand-ins, so a platform that leaves these out still links. For #reboot and TELEMETRY.
void hal_restart(void);                               // save nothing, and start again
void hal_memory(size_t *free_internal, size_t *min_free_internal, size_t *free_large);
uint32_t hal_fpu_control(void);                       // the FPU's control register (its rounding), for the state dump

#define HAL_MACS_MAX 4
#define HAL_PAIR_SECONDS 120

typedef struct {
    char key[16];  // its Bluetooth identity address in hex
    char name[44]; // from its hello, or "A Mac" before one
    bool connected;
} hal_mac_t;

int hal_macs_list(hal_mac_t *out, int max);
void hal_macs_chosen(char *out, size_t n);  // "" = any Mac
void hal_macs_choose(const char *key);      // "" = any Mac; another connected Mac is sent away
void hal_macs_forget(const char *key);      // deletes its keys and name; it must pair again
void hal_macs_pair_start(void);             // pair mode: a new Mac may pair; a connected Mac is sent away meanwhile
void hal_macs_pair_cancel(void);
int hal_macs_pair_left(void);               // seconds left in pair mode, 0 = off

#ifdef __cplusplus
}
#endif
