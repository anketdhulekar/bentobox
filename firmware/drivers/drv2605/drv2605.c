// The setup matches Waveshare's demo.
#include "drv2605.h"

#include "esp_log.h"

static const char *TAG = "drv2605";

enum {
    REG_STATUS = 0x00,
    REG_MODE = 0x01,
    REG_RTPIN = 0x02,
    REG_LIBRARY = 0x03,
    REG_WAVESEQ1 = 0x04,
    REG_GO = 0x0C,
    REG_OVERDRIVE = 0x0D,
    REG_SUSTAINPOS = 0x0E,
    REG_SUSTAINNEG = 0x0F,
    REG_BREAK = 0x10,
    REG_AUDIOMAX = 0x13,
    REG_FEEDBACK = 0x1A,
    REG_CONTROL3 = 0x1D,
};

static i2c_master_dev_handle_t s_dev;

static void wr(uint8_t reg, uint8_t v)
{
    uint8_t b[2] = {reg, v};
    i2c_master_transmit(s_dev, b, 2, 20);
}

static int rd(uint8_t reg)
{
    uint8_t v;
    return i2c_master_transmit_receive(s_dev, &reg, 1, &v, 1, 20) == ESP_OK ? v : -1;
}

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const drv2605_cfg_t *cfg = c;
    const i2c_device_config_t dev = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = cfg->addr,
        .scl_speed_hz = 300000,
    };
    if (i2c_master_bus_add_device(bus, &dev, &s_dev) != ESP_OK) return false;
    const int status = rd(REG_STATUS);
    if (status < 0) {
        ESP_LOGW(TAG, "not found at 0x%02X; no vibration", cfg->addr);
        return false;
    }
    wr(REG_MODE, 0x00); // out of standby, internal trigger
    wr(REG_RTPIN, 0x00);
    wr(REG_WAVESEQ1, 1);
    wr(REG_WAVESEQ1 + 1, 0);
    wr(REG_OVERDRIVE, 0);
    wr(REG_SUSTAINPOS, 0);
    wr(REG_SUSTAINNEG, 0);
    wr(REG_BREAK, 0);
    wr(REG_AUDIOMAX, 0x64);
    const int fb = rd(REG_FEEDBACK);
    if (fb >= 0) wr(REG_FEEDBACK, (uint8_t)(fb & 0x7F)); // ERM mode
    const int c3 = rd(REG_CONTROL3);
    if (c3 >= 0) wr(REG_CONTROL3, (uint8_t)(c3 | 0x20)); // ERM open loop
    wr(REG_LIBRARY, 5);
    ESP_LOGI(TAG, "ready (device id %d)", status >> 5);
    return true;
}

static void play(const uint8_t effects[8])
{
    wr(REG_GO, 0);
    for (int i = 0; i < 8; i++) {
        wr(REG_WAVESEQ1 + i, effects[i]);
        if (effects[i] == 0) break;
    }
    wr(REG_GO, 1);
}

// Real-time playback at full scale: the motor runs at the driver's full output (its open-loop clamp, the same level as
// the strongest library effect) until switched off. Don't raise the voltage clamp: 30 s of this could overheat the motor.
static void continuous(bool on)
{
    if (on) {
        wr(REG_GO, 0);
        wr(REG_RTPIN, 0x7F); // full scale (signed data format, the default)
        wr(REG_MODE, 0x05);  // real-time playback
    } else {
        wr(REG_RTPIN, 0);
        wr(REG_MODE, 0x00); // back to internal trigger (library effects)
    }
}

const haptics_driver_t DRV2605 = {.init = init, .play = play, .continuous = continuous};
