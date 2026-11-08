// DRV2605 haptic driver on I2C, with an ERM motor.
#pragma once
#include "driver_api.h"

typedef struct {
    uint8_t addr;
} drv2605_cfg_t;

// In an effect list, a value with the top bit set waits (value & 0x7F) x 10 ms instead of playing an effect.
#define DRV2605_WAIT_MS(ms) (0x80 | ((ms) / 10))

extern const haptics_driver_t DRV2605;
