#include "cst816.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "cst816";

static i2c_master_dev_handle_t s_dev;

// The chip sleeps between touches and NACKs polls. That's expected, so the bus doesn't log it, but only for this chip's
// own transfers: another chip's errors on the same bus still show.
static esp_err_t quiet_transfer(const uint8_t *out, size_t out_len, uint8_t *in, size_t in_len, int timeout_ms)
{
    esp_log_level_set("i2c.master", ESP_LOG_NONE);
    const esp_err_t e = in ? i2c_master_transmit_receive(s_dev, out, out_len, in, in_len, timeout_ms) : i2c_master_transmit(s_dev, out, out_len, timeout_ms);
    esp_log_level_set("i2c.master", ESP_LOG_WARN);
    return e;
}

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const cst816_cfg_t *cfg = c;
    // Reset pulse, then switch to normal mode.
    const gpio_config_t rst = {.pin_bit_mask = 1ULL << cfg->rst, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&rst);
    gpio_set_level(cfg->rst, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(cfg->rst, 1);
    vTaskDelay(pdMS_TO_TICKS(60));

    const i2c_device_config_t dev = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = cfg->addr,
        .scl_speed_hz = 300000,
    };
    if (i2c_master_bus_add_device(bus, &dev, &s_dev) != ESP_OK) return false;
    uint8_t normal_mode[2] = {0x00, 0x00};
    if (quiet_transfer(normal_mode, 2, NULL, 0, 50) != ESP_OK) {
        ESP_LOGW(TAG, "didn't answer at 0x%02X (it may be asleep until touched)", cfg->addr);
    }
    ESP_LOGI(TAG, "ready at 0x%02X", cfg->addr);
    return true;
}

static bool read_touch(int *x, int *y)
{
    uint8_t reg = 0x00, d[7];
    if (quiet_transfer(&reg, 1, d, sizeof d, 20) != ESP_OK) return false;
    if ((d[2] & 0x0F) == 0) return false;
    *x = ((d[3] & 0x0F) << 8) | d[4];
    *y = ((d[5] & 0x0F) << 8) | d[6];
    return true;
}

const touch_driver_t CST816 = {.init = init, .read = read_touch};
