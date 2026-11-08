// The debug interface for the test harness: text over USB Serial/JTAG (docs/DEBUG.md). The console's log lines share
// the same port, so a write has to land in one piece.
#include <stdarg.h>
#include <stdio.h>

#include "board_hw.h"
#include "hal.h"

#ifdef BENTO_DEBUG_INTERFACE

#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#define TX_BUFFER 4096 // room for screen captures
// The driver drops what arrives while this is full, and the UX empties it once a frame. So the host paces long lines
// (tools/device_test.py's SerialPort), and this holds a few frames of them. Up to 16 KB stays in internal RAM.
#define RX_BUFFER 4096
#define PIECE 2048     // at most this much goes into the driver's buffer at once

static bool s_ok;

void hal_debug_init(void)
{
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = TX_BUFFER;
    cfg.rx_buffer_size = RX_BUFFER;
    if (usb_serial_jtag_driver_install(&cfg) != ESP_OK) return;
    usb_serial_jtag_vfs_use_driver(); // logs go through the driver too, so they can't land inside a piece
    s_ok = true;
}

int hal_debug_read(char *out, int max)
{
    if (!s_ok || max <= 0) return 0;
    const int n = usb_serial_jtag_read_bytes(out, (uint32_t)max, 0);
    return n > 0 ? n : 0;
}

static int no_logs(const char *fmt, va_list args) { return 0; }

void hal_debug_write(const char *data, size_t len)
{
    if (!s_ok || !usb_serial_jtag_is_connected()) return; // nobody is listening: never wait for them
    // Something longer than a piece, like a screen capture, goes with logging paused, or a log line could land inside it.
    const vprintf_like_t logs = len > PIECE ? esp_log_set_vprintf(no_logs) : NULL;
    while (len > 0) {
        const size_t n = len > PIECE ? PIECE : len;
        const int sent = usb_serial_jtag_write_bytes(data, n, pdMS_TO_TICKS(500));
        if (sent <= 0) break;
        data += sent;
        len -= (size_t)sent;
    }
    if (logs) esp_log_set_vprintf(logs);
}

#else // no debug interface: the firmware never reads USB input

void hal_debug_init(void) {}
int hal_debug_read(char *out, int max) { return 0; }
void hal_debug_write(const char *data, size_t len) {}

#endif
