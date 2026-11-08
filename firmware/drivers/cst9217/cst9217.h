// CST9217 touch controller on I2C.
#pragma once
#include "driver_api.h"

typedef struct {
    int rst, irq;
    int width, height;
    bool swap_xy, mirror_x, mirror_y; // how its axes map to the panel's
} cst9217_cfg_t;

extern const touch_driver_t CST9217;
