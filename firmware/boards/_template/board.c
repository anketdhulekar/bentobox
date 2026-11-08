// BOARD_NAME: pins and chips. Take them from the board's schematic, not a guess: a wrong pin here is a wrong pin on a
// real board. A chip the board doesn't have stays out of BOARD_HW (its driver pointer stays NULL). See
// firmware/platform/esp32/board_hw.h for every field, and the other boards here for examples.
#include "board_hw.h"
#include "co5300.h"
#include "cst9217.h"

static const co5300_cfg_t SCREEN = {
    .sclk = -1, .d0 = -1, .d1 = -1, .d2 = -1, .d3 = -1, .cs = -1, .rst = -1,
    .width = 480, .height = 480,
};

static const cst9217_cfg_t TOUCH = {.rst = -1, .irq = -1, .width = 480, .height = 480};

const board_hw_t BOARD_HW = {
    .i2c_sda = -1,
    .i2c_scl = -1,

    .screen = &CO5300,
    .screen_cfg = &SCREEN,
    .brightness = {1, 3, 6, 10, 18, 30, 50, 85, 150, 255},

    .touch = &CST9217,
    .touch_cfg = &TOUCH,

    // How hal_feedback maps to this board's vibration motor (.effects) or speaker (.tones). See the other boards.
    .feedback = {
        [FB_TICK] = {.min_gap_ms = 40},
    },
};
