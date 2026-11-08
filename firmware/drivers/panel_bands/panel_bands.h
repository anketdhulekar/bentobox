// The band pipeline both QSPI panels share. Each band is copied, with its bytes swapped, into one of two DMA buffers
// in internal RAM, so the frame buffer in PSRAM can change as soon as this returns, and a flash write can't starve
// the transfer.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

// Pass this to the panel IO's config as its on_color_trans_done, before panel_bands_init.
bool panel_bands_on_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *e, void *ctx);

// Call this before any color transfer. Returns the size of a band's buffer, for the SPI bus's max_transfer_sz.
size_t panel_bands_bytes(int width);
bool panel_bands_init(esp_lcd_panel_handle_t panel, int width, int height);

void panel_bands_clear(void);
// y0 and rows are multiples of HAL_BAND_ROWS. Anything else is widened to whole bands, and every band starts on an
// even row and ends on an odd one, which the CO5300 needs for its window.
void panel_bands_present(const uint16_t *fb, int y0, int rows);
