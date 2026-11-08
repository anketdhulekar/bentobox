// Waveshare ESP32-S3-Touch-AMOLED-2.16: pins and chips. Pins are from Waveshare's BSP
// (waveshare/esp32_s3_touch_amoled_2_16 2.0.1) and the board's schematic.
#include "axp2101.h"
#include "board_hw.h"
#include "co5300.h"
#include "cst9217.h"
#include "es7210.h"
#include "es8311.h"
#include "gpio_buttons.h"
#include "qmi8658.h"

static const co5300_cfg_t SCREEN = {
    .sclk = 38, .d0 = 4, .d1 = 5, .d2 = 6, .d3 = 7, .cs = 12, .rst = 39,
    .width = 480, .height = 480,
};

// The same mapping Waveshare's BSP uses with this panel orientation (MADCTL 0xA0), checked on the device with the zoo.
static const cst9217_cfg_t TOUCH = {.rst = 40, .irq = 11, .width = 480, .height = 480, .swap_xy = true, .mirror_y = true};

static const gpio_button_t BUTTON_LIST[] = {
    {.button = BUTTON_MENU, .pin = 16, .active_high = true}, // PWR, through a transistor (holding it still powers off)
    {.button = BUTTON_UP, .pin = 18},                        // User
    {.button = BUTTON_DOWN, .pin = 0},                       // BOOT (held at reset, it starts the chip in download mode)
};
static const gpio_buttons_cfg_t BUTTONS = {.list = BUTTON_LIST, .count = sizeof BUTTON_LIST / sizeof BUTTON_LIST[0]};

// One I2S port for both audio chips, with shared clocks: ES8311 for the speaker, ES7210 for the two mics.
#define AUDIO_I2S {.port = 1, .mclk = 42, .bclk = 9, .ws = 45, .dout = 8, .din = 10, .sample_rate = 16000}

static const es8311_cfg_t SPEAKER = {.i2s = AUDIO_I2S, .amp_enable = 46};

// MIC1 and MIC2 are analog MEMS mics on the ES7210's inputs. A0 and A1 are grounded, which makes its address 0x40.
// (Its MIC3 input hears the speaker, for echo cancelling, and nothing here uses it.) 30 dB is the codec library's default.
static const es7210_cfg_t MIC = {.i2s = AUDIO_I2S, .addr = 0x40, .gain_db = 30.0f};

static const qmi8658_cfg_t MOTION = {.addr = 0x6B};

static const axp2101_cfg_t POWER = {.addr = 0x34};

// How sounds map to feedback. They're the zoo's sounds, which match its preview.
static const tone_t TICK[] = {{1800, 1200, 0, 0.02f, 0.06f, WAVE_SINE}};
static const tone_t BUMP[] = {{180, 90, 0, 0.08f, 0.1f, WAVE_SQUARE}};
static const tone_t TAP[] = {{1000, 300, 0, 0.12f, 0.1f, WAVE_TRIANGLE}};
static const tone_t DOUBLE[] = {{784, 784, 0, 0.12f, 0.12f, WAVE_SQUARE}, {1046.5f, 1046.5f, 0.1f, 0.18f, 0.12f, WAVE_SQUARE}};
static const tone_t CHIME[] = {{659.25f, 659.25f, 0, 0.4f, 0.32f, WAVE_SINE}, {987.77f, 987.77f, 0.15f, 0.6f, 0.26f, WAVE_SINE}};
static const tone_t YAY[] = {{784, 784, 0, 0.12f, 0.12f, WAVE_SQUARE}, {1046.5f, 1046.5f, 0.1f, 0.18f, 0.12f, WAVE_SQUARE}};
static const tone_t POOF[] = {{520, 120, 0, 0.22f, 0.18f, WAVE_TRIANGLE}};
static const tone_t DROP[] = {{900, 300, 0, 0.25f, 0.08f, WAVE_SINE}};
static const tone_t LAND[] = {{180, 90, 0, 0.08f, 0.1f, WAVE_SQUARE}};

#define SOUND(list) .tones = list, .tone_count = sizeof list / sizeof list[0]

const board_hw_t BOARD_HW = {
    .i2c_sda = 15,
    .i2c_scl = 14,

    .screen = &CO5300,
    .screen_cfg = &SCREEN,
    .brightness = {1, 3, 6, 10, 18, 30, 50, 85, 150, 255}, // mostly at the dim end, for a dark room

    .touch = &CST9217,
    .touch_cfg = &TOUCH,

    .buttons = &GPIO_BUTTONS,
    .buttons_cfg = &BUTTONS,

    .speaker = &ES8311,
    .speaker_cfg = &SPEAKER,

    // The QMI8658 is on the side of the board facing the panel, and the board's silk shows its axes: seen from the front
    // with the buttons at the top, X points right, Y up toward the buttons, and Z out of the screen. The panel's +y is down.
    .motion = &QMI8658,
    .motion_cfg = &MOTION,
    .motion_axes = {1, -2, 3},

    .power = &AXP2101,
    .power_cfg = &POWER,

    .mic = &ES7210,
    .mic_cfg = &MIC,

    .feedback = {
        [FB_TICK] = {.min_gap_ms = 40, SOUND(TICK)},
        [FB_BUMP] = {SOUND(BUMP)},
        [FB_TAP] = {SOUND(TAP)},
        [FB_DOUBLE] = {SOUND(DOUBLE)},
        [FB_ALERT] = {SOUND(CHIME)},
        [FB_ALARM] = {SOUND(CHIME)}, // repeats until FB_ALARM_STOP or 30 s
        [FB_YAY] = {.min_gap_ms = 300, SOUND(YAY)},
        [FB_POOF] = {.min_gap_ms = 300, SOUND(POOF)},
        [FB_DROP] = {.min_gap_ms = 300, SOUND(DROP)},
        [FB_LAND] = {.min_gap_ms = 300, SOUND(LAND)},
    },
};
