#include "knob_pulse.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "knob_pulse";

// A step counts when a line returns high after being low for at least two 3 ms samples (debounce), matching
// Waveshare's driver for this knob.
#define SAMPLE_US 3000

typedef struct {
    int pin, dir;
    uint8_t prev, low;
} line_t;

static line_t s_cw, s_ccw;
static int s_steps;

static void sample(line_t *c)
{
    const int level = gpio_get_level(c->pin);
    if (level == 0) {
        if (c->prev == 0) {
            if (c->low < 255) c->low++;
        } else {
            c->low = 0;
        }
    } else if (c->prev == 0) {
        if (c->low >= 1) __atomic_add_fetch(&s_steps, c->dir, __ATOMIC_RELAXED);
        c->low = 0;
    }
    c->prev = (uint8_t)level;
}

static void on_timer(void *arg)
{
    sample(&s_cw);
    sample(&s_ccw);
}

static bool init(const void *c)
{
    const knob_pulse_cfg_t *cfg = c;
    const gpio_config_t io = {
        .pin_bit_mask = (1ULL << cfg->clockwise) | (1ULL << cfg->anticlockwise),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    if (gpio_config(&io) != ESP_OK) return false;
    s_cw = (line_t){.pin = cfg->clockwise, .dir = +1, .prev = (uint8_t)gpio_get_level(cfg->clockwise)};
    s_ccw = (line_t){.pin = cfg->anticlockwise, .dir = -1, .prev = (uint8_t)gpio_get_level(cfg->anticlockwise)};
    const esp_timer_create_args_t args = {.callback = on_timer, .name = "knob"};
    esp_timer_handle_t timer;
    if (esp_timer_create(&args, &timer) != ESP_OK || esp_timer_start_periodic(timer, SAMPLE_US) != ESP_OK) return false;
    ESP_LOGI(TAG, "ready on GPIO %d and %d", cfg->clockwise, cfg->anticlockwise);
    return true;
}

static int take_steps(void) { return __atomic_exchange_n(&s_steps, 0, __ATOMIC_RELAXED); }

const knob_driver_t KNOB_PULSE = {.init = init, .take_steps = take_steps};
