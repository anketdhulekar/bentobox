// AXP2101 power chip on I2C: USB, charging and the battery's level.
#pragma once
#include "driver_api.h"

typedef struct {
    uint8_t addr;
} axp2101_cfg_t;

extern const power_driver_t AXP2101;
