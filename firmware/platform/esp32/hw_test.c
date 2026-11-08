// A hardware test, for bring-up: color bars, a dot wherever you touch, a counter for the knob or buttons, feedback on
// every input, and a log of Bluetooth, motion and power once a second. Uses only the HAL, so it also exercises the HAL.
// Commands over USB, one letter each: p pair mode, a alarm, x stop the alarm, m microphone on or off, f forget all Macs,
// v volume down, V volume up, b next brightness, k check PBKDF2.
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hw_test";

static uint16_t *s_fb;
static int s_w, s_h;

static inline uint16_t rgb(int r, int g, int b) { return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)); }

static void fill(int x0, int y0, int w, int h, uint16_t color)
{
    for (int y = y0 < 0 ? 0 : y0; y < y0 + h && y < s_h; y++) {
        uint16_t *row = s_fb + (size_t)y * s_w;
        for (int x = x0 < 0 ? 0 : x0; x < x0 + w && x < s_w; x++) row[x] = color;
    }
}

// Eight vertical bars, to show the panel's byte order and that every column reaches it.
static void draw_bars(void)
{
    static const uint8_t BARS[8][3] = {{255, 255, 255}, {255, 255, 0}, {0, 255, 255}, {0, 255, 0}, {255, 0, 255}, {255, 0, 0}, {0, 0, 255}, {0, 0, 0}};
    for (int i = 0; i < 8; i++) fill(i * s_w / 8, 0, s_w / 8 + 1, s_h, rgb(BARS[i][0], BARS[i][1], BARS[i][2]));
}

// A 3 x 5 digit, each cell a square of `size` pixels.
static void draw_digit(int x, int y, int size, int d, uint16_t color)
{
    static const uint16_t FONT[10] = {0x7B6F, 0x2C97, 0x73E7, 0x73CF, 0x5BC9, 0x79CF, 0x79EF, 0x7249, 0x7BEF, 0x7BCF};
    for (int row = 0; row < 5; row++) {
        for (int col = 0; col < 3; col++) {
            if (FONT[d] & (1 << ((4 - row) * 3 + (2 - col)))) fill(x + col * size, y + row * size, size, size, color);
        }
    }
}

// Digits of value, least significant at the right, in a black box in the middle of the screen.
#define BOX_DIGITS 6
#define DIGIT_SIZE 8

static void draw_number(int value, int digits, uint16_t color)
{
    const int w = BOX_DIGITS * 4 * DIGIT_SIZE, h = 7 * DIGIT_SIZE, x0 = (s_w - w) / 2, y0 = (s_h - h) / 2;
    fill(x0, y0, w, h, 0);
    int x = x0 + w - 3 * DIGIT_SIZE - DIGIT_SIZE / 2;
    const int negative = value < 0;
    int v = abs(value);
    for (int i = 0; i < digits; i++, v /= 10) {
        draw_digit(x, y0 + DIGIT_SIZE, DIGIT_SIZE, v % 10, color);
        x -= 4 * DIGIT_SIZE;
    }
    if (negative) fill(x, y0 + 3 * DIGIT_SIZE, 3 * DIGIT_SIZE, DIGIT_SIZE, color);
}

static void present_rows(int y0, int rows)
{
    const int first = y0 < 0 ? 0 : y0 / HAL_BAND_ROWS;
    const int y1 = y0 + rows > s_h ? s_h : y0 + rows;
    const int last = (y1 + HAL_BAND_ROWS - 1) / HAL_BAND_ROWS;
    hal_screen_present(s_fb, first * HAL_BAND_ROWS, (last - first) * HAL_BAND_ROWS);
}

static void show_number(int value, int digits, uint16_t color)
{
    draw_number(value, digits, color);
    present_rows((s_h - 7 * DIGIT_SIZE) / 2, 7 * DIGIT_SIZE);
}

static void check_pbkdf2(void)
{
    // PBKDF2-HMAC-SHA256 of "password", salt "salt", 1 iteration, as Python's hashlib computes it.
    static const uint8_t WANT[32] = {0x12, 0x0f, 0xb6, 0xcf, 0xfc, 0xf8, 0xb3, 0x2c, 0x43, 0xe7, 0x22, 0x52, 0x56, 0xc4, 0xf8, 0x37,
                                     0xa8, 0x65, 0x48, 0xc9, 0x2c, 0xcc, 0x35, 0x48, 0x08, 0x05, 0x98, 0x7c, 0xb7, 0x0b, 0xe1, 0x7b};
    uint8_t got[32];
    const int64_t t0 = hal_now_us();
    hal_pbkdf2_sha256("password", (const uint8_t *)"salt", 4, 1, got);
    ESP_LOGI(TAG, "PBKDF2: %s, %d us", memcmp(got, WANT, 32) ? "WRONG" : "right", (int)(hal_now_us() - t0));
}

static void log_state(int mic_peak, bool mic_on)
{
    hal_link_status_t link;
    hal_link_status(&link);
    hal_mac_t macs[HAL_MACS_MAX];
    const int n_macs = hal_macs_list(macs, HAL_MACS_MAX);
    float x = 0, y = 0, z = 0;
    const bool motion = hal_motion_read(&x, &y, &z);
    hal_power_t power;
    hal_power_read(&power);
    // Not the pairing code: a real one appears only on the screen.
    ESP_LOGI(TAG, "link: connected %d, pairing %d, pair mode %d s, %d Macs known", link.connected, link.pairing, hal_macs_pair_left(), n_macs);
    if (motion) ESP_LOGI(TAG, "gravity: x %.2f y %.2f z %.2f", x, y, z);
    else ESP_LOGI(TAG, "gravity: no motion sensor");
    ESP_LOGI(TAG, "power: ok %d, battery %d %%, usb %d, charging %d; %u KB internal (%u KB at the lowest), %u KB PSRAM", power.ok, power.percent, power.usb, power.charging,
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024), (unsigned)(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
    if (mic_on) ESP_LOGI(TAG, "microphone: loudest sample %d in the last second", mic_peak);
}

void hw_test_run(void)
{
    s_w = BOARD.width;
    s_h = BOARD.height;
    s_fb = hal_alloc_large((size_t)s_w * s_h * sizeof(uint16_t));
    if (!s_fb) {
        ESP_LOGE(TAG, "no memory for a frame buffer");
        return;
    }
    ESP_LOGI(TAG, "hardware test on %s (%d x %d); commands over USB: p a x m f v V b k", BOARD.name, s_w, s_h);
    draw_bars();
    hal_screen_present(s_fb, 0, s_h);
    hal_feedback(FB_TAP, true);

    int count = 0, volume = 7, brightness = HAL_BRIGHT_LEVELS - 1, mic_peak = 0;
    bool mic_on = false, pairing_shown = false;
    int64_t next_log = hal_now_us();
    show_number(count, 3, rgb(255, 255, 255));

    for (;;) {
        // Input: every event is logged, gets feedback, and shows on the screen.
        hw_event_t e;
        while (hal_input_poll(&e)) {
            switch (e.kind) {
            case HW_TOUCH_DOWN: // the last touch's dots stay until the next one, to compare them with where it landed
                ESP_LOGI(TAG, "touch down at %d, %d", e.x, e.y);
                hal_feedback(FB_TAP, true);
                draw_bars();
                draw_number(count, 3, rgb(255, 255, 255));
                fill(e.x - 4, e.y - 4, 8, 8, rgb(255, 255, 255));
                hal_screen_present(s_fb, 0, s_h);
                break;
            case HW_TOUCH_MOVE:
                fill(e.x - 4, e.y - 4, 8, 8, rgb(255, 255, 255));
                present_rows(e.y - 4, 8);
                break;
            case HW_TOUCH_UP:
                ESP_LOGI(TAG, "touch up at %d, %d", e.x, e.y);
                break;
            case HW_KNOB:
                count += e.step;
                ESP_LOGI(TAG, "knob %+d, count %d", e.step, count);
                hal_feedback(FB_TICK, true);
                show_number(count, 3, rgb(255, 255, 255));
                break;
            case HW_BUTTON:
                count += e.button == BUTTON_UP ? 1 : e.button == BUTTON_DOWN ? -1 : 100;
                ESP_LOGI(TAG, "button %d, count %d", e.button, count);
                hal_feedback(e.button == BUTTON_MENU ? FB_DOUBLE : FB_TAP, true);
                show_number(count, 3, rgb(255, 255, 255));
                break;
            default:
                break;
            }
        }

        // Commands over USB.
        char cmd;
        while (hal_debug_read(&cmd, 1) == 1) {
            switch (cmd) {
            case 'p': hal_macs_pair_start(); break;
            case 'a': hal_feedback(FB_ALARM, true); ESP_LOGI(TAG, "alarm on"); break;
            case 'x': hal_feedback(FB_ALARM_STOP, true); ESP_LOGI(TAG, "alarm off"); break;
            case 'm':
                if (mic_on) hal_mic_stop();
                else ESP_LOGI(TAG, "microphone: %s", hal_mic_start() ? "started" : "didn't start");
                mic_on = !mic_on;
                break;
            case 'f': {
                hal_mac_t macs[HAL_MACS_MAX];
                const int n = hal_macs_list(macs, HAL_MACS_MAX);
                for (int i = 0; i < n; i++) hal_macs_forget(macs[i].key);
                break;
            }
            case 'v': case 'V':
                volume += cmd == 'V' ? 1 : -1;
                volume = volume < 0 ? 0 : volume > 10 ? 10 : volume;
                hal_volume(volume);
                hal_feedback(FB_TAP, true);
                ESP_LOGI(TAG, "volume %d", volume);
                break;
            case 'b':
                brightness = (brightness + 1) % HAL_BRIGHT_LEVELS;
                hal_screen_brightness(brightness);
                ESP_LOGI(TAG, "brightness %d", brightness);
                break;
            case 'k': check_pbkdf2(); break;
            default: break;
            }
        }

        // A new Mac pairing: the code goes on the screen, and only there. When it's over, the counter comes back.
        hal_link_status_t link;
        hal_link_status(&link);
        if (link.pairing != pairing_shown) {
            pairing_shown = link.pairing;
            if (pairing_shown) show_number((int)link.code, 6, rgb(0, 255, 255));
            else show_number(count, 3, rgb(255, 255, 255));
        }

        if (mic_on) {
            int16_t samples[256];
            int n;
            while ((n = hal_mic_read(samples, 256)) > 0) {
                for (int i = 0; i < n; i++) {
                    const int a = abs(samples[i]);
                    if (a > mic_peak) mic_peak = a;
                }
            }
        }

        const int64_t now = hal_now_us();
        if (now >= next_log) {
            log_state(mic_peak, mic_on);
            mic_peak = 0;
            next_log = now + 1000000;
        }
        hal_sleep_ms(10);
    }
}
