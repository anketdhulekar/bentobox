#include "es7210.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"

static const char *TAG = "es7210";

static const es7210_cfg_t *s_cfg;
static const audio_codec_if_t *s_codec;
static i2s_chan_handle_t s_rx;

#define CHUNK_FRAMES 256

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    s_cfg = c;
    const i2s_codec_bus_t *i2s = i2s_codec_bus_get(&s_cfg->i2s);
    if (!i2s) return false;
    s_rx = i2s->rx;
    if (i2c_master_probe(bus, s_cfg->addr, 50) != ESP_OK) {
        ESP_LOGW(TAG, "not found at 0x%02X; no microphone", s_cfg->addr);
        return false;
    }
    audio_codec_i2c_cfg_t i2c_cfg = {.port = I2C_NUM_0, .addr = (uint8_t)(s_cfg->addr << 1), .bus_handle = bus}; // it wants 8 bits
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    es7210_codec_cfg_t es = {.ctrl_if = ctrl_if, .master_mode = false, .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2};
    s_codec = es7210_codec_new(&es);
    if (!s_codec) {
        ESP_LOGW(TAG, "didn't start");
        return false;
    }
    ESP_LOGI(TAG, "ready at 0x%02X", s_cfg->addr);
    return true;
}

// The codec's control interface is used directly, not through esp_codec_dev, because that layer also reconfigures the I2S
// channels on every open and close. The clocks here are shared with playback, which keeps running.
static bool start(void)
{
    if (!s_codec) return false;
    esp_codec_dev_sample_info_t fs = {.sample_rate = s_cfg->i2s.sample_rate, .channel = 2, .bits_per_sample = 16};
    if (s_codec->set_fs(s_codec, &fs) != ESP_CODEC_DEV_OK || s_codec->enable(s_codec, true) != ESP_CODEC_DEV_OK) return false;
    s_codec->set_mic_gain(s_codec, s_cfg->gain_db);
    return i2s_channel_enable(s_rx) == ESP_OK;
}

static int read_samples(int16_t *out, int max, int timeout_ms)
{
    int16_t frames[CHUNK_FRAMES * 2];
    if (max > CHUNK_FRAMES) max = CHUNK_FRAMES;
    size_t got = 0;
    i2s_channel_read(s_rx, frames, (size_t)max * 2 * sizeof(int16_t), &got, timeout_ms);
    const int n = (int)(got / (2 * sizeof(int16_t)));
    for (int i = 0; i < n; i++) out[i] = (int16_t)((frames[i * 2] + frames[i * 2 + 1]) / 2);
    return n;
}

static void stop(void)
{
    i2s_channel_disable(s_rx);
    s_codec->enable(s_codec, false);
}

const mic_driver_t ES7210 = {.init = init, .start = start, .read = read_samples, .stop = stop};
