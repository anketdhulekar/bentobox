#include "axp2101.h"

#include "esp_log.h"

static const char *TAG = "axp2101";

static i2c_master_dev_handle_t s_dev;

static int rd(uint8_t reg)
{
    uint8_t v;
    return s_dev && i2c_master_transmit_receive(s_dev, &reg, 1, &v, 1, 50) == ESP_OK ? v : -1;
}

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const axp2101_cfg_t *cfg = c;
    const i2c_device_config_t dev = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = cfg->addr, .scl_speed_hz = 400000};
    if (i2c_master_bus_add_device(bus, &dev, &s_dev) != ESP_OK) return false;
    const int id = rd(0x03);
    if (id != 0x4A) {
        ESP_LOGW(TAG, "not found (id %d)", id);
        s_dev = NULL;
        return false;
    }
    // The only change to the chip: turn on the battery-voltage reading (ADC enable 0x30 bit 0) for the fallback below.
    const int adc = rd(0x30);
    if (adc >= 0 && !(adc & 1)) {
        const uint8_t b[2] = {0x30, (uint8_t)(adc | 1)};
        i2c_master_transmit(s_dev, b, 2, 50);
    }
    ESP_LOGI(TAG, "ready");
    return true;
}

static void read_power(hal_power_t *out)
{
    *out = (hal_power_t){.percent = -1};
    const int s1 = rd(0x00), s2 = rd(0x01);
    if (s1 < 0 || s2 < 0) return;
    out->ok = true;
    out->usb = (s1 & 0x20) && !(s2 & 0x08);
    out->charging = (s2 >> 5) == 1;
    if (!(s1 & 0x08)) return; // no battery
    int pct = rd(0xA4);
    if (pct <= 0 || pct > 100) { // the fuel gauge has nothing yet: estimate from the voltage
        const int hi = rd(0x34), lo = rd(0x35);
        if (hi >= 0 && lo >= 0) {
            const int mv = ((hi & 0x1F) << 8) | lo;
            pct = mv <= 3300 ? 0 : mv >= 4150 ? 100 : (mv - 3300) * 100 / 850;
        }
    }
    out->percent = pct > 100 ? -1 : pct;
}

const power_driver_t AXP2101 = {.init = init, .read = read_power};
