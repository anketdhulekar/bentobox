// CO5300 AMOLED panel over QSPI.
#pragma once
#include "driver_api.h"

typedef struct {
    int sclk, d0, d1, d2, d3, cs, rst;
    int width, height;
} co5300_cfg_t;

extern const screen_driver_t CO5300;
