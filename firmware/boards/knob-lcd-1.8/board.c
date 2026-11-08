// Waveshare ESP32-S3-Knob-Touch-LCD-1.8: pins and chips. Pins are from Waveshare's schematic and demos.
#include "adc_battery.h"
#include "board_hw.h"
#include "cst816.h"
#include "drv2605.h"
#include "knob_pulse.h"
#include "pdm_mic.h"
#include "st77916.h"

static const st77916_cfg_t SCREEN = {
    .sclk = 13, .d0 = 15, .d1 = 16, .d2 = 17, .d3 = 18, .cs = 14, .rst = 21, .backlight = 47,
    .width = 360, .height = 360,
};

static const cst816_cfg_t TOUCH = {.addr = 0x15, .rst = 10};

static const knob_pulse_cfg_t KNOB = {.clockwise = 8, .anticlockwise = 7};

static const drv2605_cfg_t HAPTICS = {.addr = 0x5A};

// MSM261D4030H1CPM with its L/R pin grounded.
static const pdm_mic_cfg_t MIC = {.port = 0, .clk = 45, .data = 46, .sample_rate = 16000};

// BATT_ADC: two 10K resistors from the board's 5V rail to ground, with GPIO1 between them. Waveshare's demo reads about
// 4.8 V on USB, and that's USB rather than the cell. What it reads on battery is still to check: see README.md.
static const adc_battery_cfg_t POWER = {.pin = 1, .divider = 2, .usb_above_mv = 4400};

const board_hw_t BOARD_HW = {
    .i2c_sda = 11,
    .i2c_scl = 12,

    .screen = &ST77916,
    .screen_cfg = &SCREEN,
    .brightness = {1, 2, 4, 7, 12, 22, 40, 75, 140, 255}, // mostly at the dim end, for a dark room

    .touch = &CST816,
    .touch_cfg = &TOUCH,

    .knob = &KNOB_PULSE,
    .knob_cfg = &KNOB,

    .haptics = &DRV2605,
    .haptics_cfg = &HAPTICS,

    .power = &ADC_BATTERY,
    .power_cfg = &POWER,

    .mic = &PDM_MIC,
    .mic_cfg = &MIC,

    // Effect numbers from the DRV2605's ERM library. FB_ALARM is continuous vibration: see drv2605.c.
    .feedback = {
        // Each kick draws a pulse of current. On battery, a full-strength tick per detent dipped the supply enough to
        // make the backlight flicker during a turn, so detents use a softer tick (Sharp Tick 3, 60 %), and at most
        // one per 40 ms. Don't make it stronger without checking this on battery.
        [FB_TICK] = {.effects = {26}, .min_gap_ms = 40},
        [FB_BUMP] = {.effects = {7}},   // Soft Bump
        [FB_TAP] = {.effects = {1}},    // Strong Click
        [FB_DOUBLE] = {.effects = {10}}, // Double Click
        [FB_ALERT] = {.effects = {1, DRV2605_WAIT_MS(120), 1, DRV2605_WAIT_MS(120), 1}}, // three Strong Clicks
        // The agent on screen. No speaker here, so these stand in for its sounds, and they stay at 60 % or less.
        [FB_YAY] = {.effects = {23, DRV2605_WAIT_MS(90), 19}, .min_gap_ms = 300},  // Medium Click 3, then Strong Click 3
        [FB_POOF] = {.effects = {8, DRV2605_WAIT_MS(40), 9}, .min_gap_ms = 300},   // Soft Bump, 60 % then 30 %
        [FB_DROP] = {.effects = {26}, .min_gap_ms = 300},                          // Sharp Tick 3
        [FB_LAND] = {.effects = {8}, .min_gap_ms = 300},                           // Soft Bump 60 %
    },
};
