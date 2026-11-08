#include "qmi8658.h"

#include "esp_log.h"

static const char *TAG = "qmi8658";

static i2c_master_dev_handle_t s_dev;

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const qmi8658_cfg_t *cfg = c;
    const i2c_device_config_t dev = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = cfg->addr, .scl_speed_hz = 400000};
    if (i2c_master_bus_add_device(bus, &dev, &s_dev) != ESP_OK) return false;
    uint8_t reg = 0x00, who = 0;
    if (i2c_master_transmit_receive(s_dev, &reg, 1, &who, 1, 50) != ESP_OK || who != 0x05) {
        ESP_LOGW(TAG, "not found (id %d)", who);
        s_dev = NULL;
        return false;
    }
    const uint8_t setup[][2] = {
        {0x02, 0x60}, // auto-increment, little-endian
        {0x03, 0x06}, // accelerometer: +-2 g, 125 Hz
        {0x08, 0x01}, // accelerometer on
    };
    for (size_t i = 0; i < sizeof setup / sizeof setup[0]; i++) i2c_master_transmit(s_dev, setup[i], 2, 50);
    ESP_LOGI(TAG, "ready");
    return true;
}

static bool read_accel(float accel_g[3])
{
    if (!s_dev) return false;
    uint8_t reg = 0x35, d[6];
    if (i2c_master_transmit_receive(s_dev, &reg, 1, d, sizeof d, 50) != ESP_OK) return false;
    for (int i = 0; i < 3; i++) accel_g[i] = (float)(int16_t)(d[i * 2] | d[i * 2 + 1] << 8) / 16384.0f; // 16384 = 1 g
    return true;
}

const motion_driver_t QMI8658 = {.init = init, .read = read_accel};
