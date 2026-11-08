// CST816 touch controller on I2C.
#pragma once
#include "driver_api.h"

typedef struct {
    uint8_t addr;
    int rst;
} cst816_cfg_t;

extern const touch_driver_t CST816;
