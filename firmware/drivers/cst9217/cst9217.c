#include "cst9217.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_touch_cst9217.h"
#include "esp_log.h"

static const char *TAG = "cst9217";

static esp_lcd_touch_handle_t s_tp;

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    const cst9217_cfg_t *cfg = c;
    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_i2c_config_t io_cfg = ESP_LCD_TOUCH_IO_I2C_CST9217_CONFIG();
    io_cfg.scl_speed_hz = 400000;
    if (esp_lcd_new_panel_io_i2c(bus, &io_cfg, &io) != ESP_OK) {
        ESP_LOGW(TAG, "no I2C device");
        return false;
    }
    const esp_lcd_touch_config_t tp_cfg = {
        .x_max = cfg->width,
        .y_max = cfg->height,
        .rst_gpio_num = cfg->rst,
        .int_gpio_num = cfg->irq,
        .levels = {.reset = 0, .interrupt = 0},
        .flags = {.swap_xy = cfg->swap_xy, .mirror_x = cfg->mirror_x, .mirror_y = cfg->mirror_y},
    };
    if (esp_lcd_touch_new_i2c_cst9217(io, &tp_cfg, &s_tp) != ESP_OK) {
        ESP_LOGW(TAG, "didn't answer");
        s_tp = NULL;
        return false;
    }
    ESP_LOGI(TAG, "ready");
    return true;
}

static bool read_touch(int *x, int *y)
{
    if (!s_tp || esp_lcd_touch_read_data(s_tp) != ESP_OK) return false;
    esp_lcd_touch_point_data_t pt[1];
    uint8_t n = 0;
    if (esp_lcd_touch_get_data(s_tp, pt, &n, 1) != ESP_OK || n == 0) return false;
    *x = pt[0].x;
    *y = pt[0].y;
    return true;
}

const touch_driver_t CST9217 = {.init = init, .read = read_touch};
