// A digital PDM microphone on the I2S port's PDM receiver.
#pragma once
#include "driver_api.h"

typedef struct {
    int port;
    int clk, data;
    uint32_t sample_rate;
} pdm_mic_cfg_t;

extern const mic_driver_t PDM_MIC;
