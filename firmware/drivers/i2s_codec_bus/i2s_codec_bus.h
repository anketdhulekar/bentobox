// One I2S port shared by a playback chip and a capture chip, which see the same clocks. The first driver to ask sets
// it up. Playback runs all the time, because in full duplex the transmit side makes the clocks: the receive side is
// enabled only while the mic runs, by the capture chip's driver.
#pragma once
#include "driver/i2s_std.h"

typedef struct {
    int port;
    int mclk, bclk, ws, dout, din;
    uint32_t sample_rate;
} i2s_codec_bus_cfg_t;

typedef struct {
    i2s_chan_handle_t tx, rx;
} i2s_codec_bus_t;

const i2s_codec_bus_t *i2s_codec_bus_get(const i2s_codec_bus_cfg_t *cfg); // NULL on failure
