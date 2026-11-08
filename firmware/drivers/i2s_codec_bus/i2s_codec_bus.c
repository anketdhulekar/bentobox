#include "i2s_codec_bus.h"

#include <stdbool.h>

#include "esp_log.h"

static const char *TAG = "i2s_codec_bus";

const i2s_codec_bus_t *i2s_codec_bus_get(const i2s_codec_bus_cfg_t *cfg)
{
    static i2s_codec_bus_t s_bus;
    static bool s_ready;
    if (s_ready) return &s_bus;

    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG((i2s_port_t)cfg->port, I2S_ROLE_MASTER);
    chan.auto_clear = true; // silence, not old samples, once playback runs out
    if (i2s_new_channel(&chan, &s_bus.tx, &s_bus.rx) != ESP_OK) {
        ESP_LOGW(TAG, "no I2S channels");
        return NULL;
    }
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(cfg->sample_rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.mclk = cfg->mclk, .bclk = cfg->bclk, .ws = cfg->ws, .dout = cfg->dout, .din = cfg->din},
    };
    if (i2s_channel_init_std_mode(s_bus.tx, &std) != ESP_OK) return NULL;
    // Capture takes both slots: the capture chip's two inputs share this one data line.
    std.slot_cfg = (i2s_std_slot_config_t)I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
    if (i2s_channel_init_std_mode(s_bus.rx, &std) != ESP_OK) return NULL;
    if (i2s_channel_enable(s_bus.tx) != ESP_OK) return NULL;
    s_ready = true;
    ESP_LOGI(TAG, "I2S%d ready at %u Hz", cfg->port, (unsigned)cfg->sample_rate);
    return &s_bus;
}
