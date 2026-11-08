// ES7210 microphone ADC: I2C for control, I2S for samples. MIC1 and MIC2 are mixed to mono.
#pragma once
#include "driver_api.h"
#include "i2s_codec_bus.h"

typedef struct {
    i2s_codec_bus_cfg_t i2s;
    uint8_t addr; // 7-bit
    float gain_db;
} es7210_cfg_t;

extern const mic_driver_t ES7210;
