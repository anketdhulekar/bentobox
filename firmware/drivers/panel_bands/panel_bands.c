#include "panel_bands.h"

#include <string.h>

#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "hal.h"

static const char *TAG = "panel_bands";

static esp_lcd_panel_handle_t s_panel;
static int s_w, s_h;
static SemaphoreHandle_t s_free; // band buffers not in flight
static uint16_t *s_band[2];
static int s_next;

// Runs in the SPI interrupt, also while a flash write has the caches off, so it stays in IRAM.
IRAM_ATTR bool panel_bands_on_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *e, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_free, &woken);
    return woken == pdTRUE;
}

size_t panel_bands_bytes(int width) { return (size_t)width * HAL_BAND_ROWS * sizeof(uint16_t); }

bool panel_bands_init(esp_lcd_panel_handle_t panel, int width, int height)
{
    s_panel = panel;
    s_w = width;
    s_h = height;
    const size_t bytes = panel_bands_bytes(width);
    for (int i = 0; i < 2; i++) {
        s_band[i] = heap_caps_malloc(bytes, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_band[i]) {
            ESP_LOGE(TAG, "no internal RAM for the display's band buffers");
            return false;
        }
    }
    s_free = xSemaphoreCreateCounting(2, 2);
    return s_free != NULL;
}

// The next free buffer. It waits if both are in flight, so a band can't overtake the panel.
static uint16_t *take_band(void)
{
    xSemaphoreTake(s_free, portMAX_DELAY);
    return s_band[s_next];
}

static void send_band(int y, int rows)
{
    if (esp_lcd_panel_draw_bitmap(s_panel, 0, y, s_w, y + rows, s_band[s_next]) != ESP_OK) {
        xSemaphoreGive(s_free); // no transfer started, so no interrupt will hand this buffer back
        return;
    }
    s_next ^= 1;
}

void panel_bands_clear(void)
{
    for (int y = 0; y < s_h; y += HAL_BAND_ROWS) {
        const int rows = s_h - y < HAL_BAND_ROWS ? s_h - y : HAL_BAND_ROWS;
        memset(take_band(), 0, (size_t)s_w * rows * sizeof(uint16_t));
        send_band(y, rows);
    }
}

void panel_bands_present(const uint16_t *fb, int y0, int rows)
{
    if (!s_free || !fb) return;
    int end = y0 + rows;
    if (y0 < 0) y0 = 0;
    y0 -= y0 % HAL_BAND_ROWS;
    if (end > s_h) end = s_h;
    for (int y = y0; y < end; y += HAL_BAND_ROWS) {
        const int band = s_h - y < HAL_BAND_ROWS ? s_h - y : HAL_BAND_ROWS;
        uint16_t *out = take_band();
        const uint16_t *in = fb + (size_t)y * s_w;
        const int n = s_w * band;
        for (int i = 0; i < n; i++) out[i] = (uint16_t)((in[i] >> 8) | (in[i] << 8)); // the panels take big-endian
        send_band(y, band);
    }
}
