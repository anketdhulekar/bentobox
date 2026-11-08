#include "gpio_buttons.h"

#include "driver/gpio.h"

static const gpio_buttons_cfg_t *s_cfg;

static bool init(const void *c)
{
    s_cfg = c;
    for (int i = 0; i < s_cfg->count; i++) {
        const gpio_button_t *b = &s_cfg->list[i];
        const gpio_config_t io = {
            .pin_bit_mask = 1ULL << b->pin,
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = b->active_high ? GPIO_PULLUP_DISABLE : GPIO_PULLUP_ENABLE,
        };
        if (gpio_config(&io) != ESP_OK) return false;
    }
    return true;
}

static uint32_t read_buttons(void)
{
    uint32_t held = 0;
    for (int i = 0; i < s_cfg->count; i++) {
        const gpio_button_t *b = &s_cfg->list[i];
        if (gpio_get_level(b->pin) == (b->active_high ? 1 : 0)) held |= 1u << b->button;
    }
    return held;
}

const buttons_driver_t GPIO_BUTTONS = {.init = init, .read = read_buttons};
