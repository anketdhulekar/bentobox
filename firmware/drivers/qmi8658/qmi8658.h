// QMI8658 motion sensor on I2C. Only its accelerometer is used.
#pragma once
#include "driver_api.h"

typedef struct {
    uint8_t addr;
} qmi8658_cfg_t;

extern const motion_driver_t QMI8658;
