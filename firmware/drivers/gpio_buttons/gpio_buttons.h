// Buttons on GPIO pins.
#pragma once
#include "driver_api.h"

typedef struct {
    hw_button_t button;
    int pin;
    bool active_high; // a press reads high (an external transistor), else low with the internal pull-up
} gpio_button_t;

typedef struct {
    const gpio_button_t *list;
    int count;
} gpio_buttons_cfg_t;

extern const buttons_driver_t GPIO_BUTTONS;
