// What firmware/boards/<board>/board.c tells the platform code: which chips the board has, their pins (in each chip's
// config struct), and how feedback maps to vibration or sound. All facts, so a board with chips Bento already supports
// is only a table. A chip the board doesn't have stays NULL. board_info.c holds the portable facts the UX reads.
#pragma once
#include "driver_api.h"

typedef struct {
    uint8_t effects[8];  // on a vibration motor: the chip's effect numbers; 0 ends the list
    uint16_t min_gap_ms; // at most one of these per this long (a fast spin of the knob)
    const tone_t *tones; // on a speaker
    uint8_t tone_count;
} feedback_map_t;

typedef struct {
    int i2c_sda, i2c_scl; // -1 if the board has nothing on I2C

    const screen_driver_t *screen;
    const void *screen_cfg;
    uint8_t brightness[HAL_BRIGHT_LEVELS]; // the screen driver's level for hal_screen_brightness's low, medium and high

    const touch_driver_t *touch;
    const void *touch_cfg;

    const knob_driver_t *knob;
    const void *knob_cfg;

    const buttons_driver_t *buttons;
    const void *buttons_cfg;

    const haptics_driver_t *haptics;
    const void *haptics_cfg;

    const speaker_driver_t *speaker;
    const void *speaker_cfg;

    const motion_driver_t *motion;
    const void *motion_cfg;
    // The chip axis along the panel's +x, +y and +z: 1, 2 or 3 for its X, Y or Z, negative if it points the other way.
    int8_t motion_axes[3];

    const power_driver_t *power;
    const void *power_cfg;

    const mic_driver_t *mic;
    const void *mic_cfg;

    feedback_map_t feedback[FB_COUNT];
} board_hw_t;

extern const board_hw_t BOARD_HW;

// Shared between the platform's files.
void hal_store_init(void);
void hal_io_init(i2c_master_bus_handle_t bus);
void hal_feedback_init(i2c_master_bus_handle_t bus);
void hal_link_init(void);
void hal_debug_init(void);
void hw_test_run(void);
