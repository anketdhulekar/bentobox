#include "es8311.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"

static const char *TAG = "es8311";

static esp_codec_dev_handle_t s_dev;

// The codec's output volume for each level: 0 is silent; 1..10 go from quiet to loud.
static void volume(int level)
{
    if (!s_dev) return;
    esp_codec_dev_set_out_vol(s_dev, level <= 0 ? 0 : 40 + level * 5);
    esp_codec_dev_set_out_mute(s_dev, level <= 0);
}

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const es8311_cfg_t *cfg = c;
    const i2s_codec_bus_t *i2s = i2s_codec_bus_get(&cfg->i2s);
    if (!i2s) return false;
    audio_codec_i2s_cfg_t i2s_cfg = {.port = (uint8_t)cfg->i2s.port, .tx_handle = i2s->tx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);
    audio_codec_i2c_cfg_t i2c_cfg = {.port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = bus};
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    es8311_codec_cfg_t es = {
        .ctrl_if = ctrl_if,
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = (int16_t)cfg->amp_enable,
        .pa_reverted = false,
        .master_mode = false,
        .use_mclk = true,
        .hw_gain = {.pa_voltage = 5.0, .codec_dac_voltage = 3.3}, // as Waveshare's BSP
    };
    const audio_codec_if_t *codec = es8311_codec_new(&es);
    if (!codec) {
        ESP_LOGW(TAG, "not found");
        return false;
    }
    esp_codec_dev_cfg_t dev = {.dev_type = ESP_CODEC_DEV_TYPE_OUT, .codec_if = codec, .data_if = data_if};
    s_dev = esp_codec_dev_new(&dev);
    if (!s_dev) return false;
    esp_codec_dev_sample_info_t fs = {.sample_rate = cfg->i2s.sample_rate, .channel = 1, .bits_per_sample = 16};
    if (esp_codec_dev_open(s_dev, &fs) != ESP_CODEC_DEV_OK) {
        ESP_LOGW(TAG, "didn't open");
        s_dev = NULL;
        return false;
    }
    ESP_LOGI(TAG, "ready at %u Hz", (unsigned)cfg->i2s.sample_rate);
    return true;
}

static bool write_pcm(const int16_t *pcm, size_t samples)
{
    return s_dev && esp_codec_dev_write(s_dev, (void *)pcm, (int)(samples * sizeof(int16_t))) == ESP_CODEC_DEV_OK;
}

const speaker_driver_t ES8311 = {.init = init, .write = write_pcm, .volume = volume};
