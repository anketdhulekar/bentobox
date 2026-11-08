#include "co5300.h"

#include "driver/spi_master.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "panel_bands.h"

static const char *TAG = "co5300";

// The vendor init list from Waveshare's BSP (sleep out, 16-bit color, display on), with the brightness register
// (0x51) at 0 rather than full, so the panel's random contents never show before the first frame.
static const co5300_lcd_init_cmd_t INIT_CMDS[] = {
    {0x11, (uint8_t[]){0x00}, 0, 600},
    {0xFE, (uint8_t[]){0x20}, 1, 0},
    {0x19, (uint8_t[]){0x10}, 1, 0},
    {0x1C, (uint8_t[]){0xA0}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){0x00}, 1, 0},
    {0x63, (uint8_t[]){0xFF}, 1, 0},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x36, (uint8_t[]){0xA0}, 1, 0},
    {0x29, (uint8_t[]){0x00}, 0, 600},
};

static esp_lcd_panel_io_handle_t s_io;

// A step of bringing the panel up: on failure, say which, and report no screen rather than stop the board.
#define TRY(x)                                                       \
    do {                                                             \
        const esp_err_t try_err = (x);                               \
        if (try_err != ESP_OK) {                                     \
            ESP_LOGE(TAG, "%s: %s", #x, esp_err_to_name(try_err)); \
            return false;                                            \
        }                                                            \
    } while (0)

static bool init(const void *c)
{
    const co5300_cfg_t *cfg = c;
    const spi_bus_config_t bus = CO5300_PANEL_BUS_QSPI_CONFIG(cfg->sclk, cfg->d0, cfg->d1, cfg->d2, cfg->d3, panel_bands_bytes(cfg->width));
    TRY(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO));
    esp_lcd_panel_io_spi_config_t io_cfg = CO5300_PANEL_IO_QSPI_CONFIG(cfg->cs, panel_bands_on_done, NULL);
    io_cfg.trans_queue_depth = 3;
    TRY(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io_cfg, &s_io));
    co5300_vendor_config_t vendor = {
        .init_cmds = INIT_CMDS,
        .init_cmds_size = sizeof INIT_CMDS / sizeof INIT_CMDS[0],
        .flags = {.use_qspi_interface = 1},
    };
    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = cfg->rst,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = &vendor,
    };
    esp_lcd_panel_handle_t panel = NULL;
    TRY(esp_lcd_new_panel_co5300(s_io, &panel_cfg, &panel));
    TRY(esp_lcd_panel_reset(panel));
    TRY(esp_lcd_panel_init(panel));
    if (!panel_bands_init(panel, cfg->width, cfg->height)) return false;
    panel_bands_clear();
    TRY(esp_lcd_panel_disp_on_off(panel, true));
    ESP_LOGI(TAG, "ready, %d x %d, %d-row bands", cfg->width, cfg->height, HAL_BAND_ROWS);
    return true;
}

static void brightness(uint8_t level)
{
    // In QSPI mode a command goes out as (0x02 << 24) | (cmd << 8).
    esp_lcd_panel_io_tx_param(s_io, (0x02 << 24) | (0x51 << 8), &level, 1);
}

const screen_driver_t CO5300 = {.init = init, .present = panel_bands_present, .brightness = brightness};
