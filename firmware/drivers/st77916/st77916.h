// ST77916 LCD panel over QSPI, with its backlight on a PWM pin.
#pragma once
#include "driver_api.h"

typedef struct {
    int sclk, d0, d1, d2, d3, cs, rst, backlight;
    int width, height;
} st77916_cfg_t;

extern const screen_driver_t ST77916;
