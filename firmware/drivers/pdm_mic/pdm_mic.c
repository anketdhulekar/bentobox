#include "pdm_mic.h"

#include "driver/i2s_pdm.h"
#include "esp_log.h"

static const char *TAG = "pdm_mic";

static i2s_chan_handle_t s_rx;

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const pdm_mic_cfg_t *cfg = c;
    const i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG((i2s_port_t)cfg->port, I2S_ROLE_MASTER);
    if (i2s_new_channel(&chan, NULL, &s_rx) != ESP_OK) {
        ESP_LOGW(TAG, "no I2S channel");
        s_rx = NULL;
        return false;
    }
    i2s_pdm_rx_config_t pdm = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(cfg->sample_rate),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.clk = cfg->clk, .din = cfg->data},
    };
    // 16x down-sampling runs the mic's clock at 2.048 MHz, the middle of its normal range (the default 8x gives
    // 1.024 MHz, right at the bottom edge).
    pdm.clk_cfg.dn_sample_mode = I2S_PDM_DSR_16S;
    if (i2s_channel_init_pdm_rx_mode(s_rx, &pdm) != ESP_OK) {
        ESP_LOGW(TAG, "PDM setup failed");
        s_rx = NULL;
        return false;
    }
    ESP_LOGI(TAG, "ready at %u Hz", (unsigned)cfg->sample_rate);
    return true;
}

static bool start(void) { return s_rx && i2s_channel_enable(s_rx) == ESP_OK; }

static int read_samples(int16_t *out, int max, int timeout_ms)
{
    size_t got = 0;
    i2s_channel_read(s_rx, out, (size_t)max * sizeof(int16_t), &got, timeout_ms);
    return (int)(got / sizeof(int16_t));
}

static void stop(void) { i2s_channel_disable(s_rx); }

const mic_driver_t PDM_MIC = {.init = init, .start = start, .read = read_samples, .stop = stop};
