// ES8311 codec, used for playback: I2C for control, I2S for samples, and an amplifier enable pin.
#pragma once
#include "driver_api.h"
#include "i2s_codec_bus.h"

typedef struct {
    i2s_codec_bus_cfg_t i2s;
    int amp_enable; // active high
} es8311_cfg_t;

extern const speaker_driver_t ES8311;
