// Where a board starts: the hardware layer, then the UX (or the hardware test), on the core Bluetooth doesn't use.
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "board_hw.h"
#include "hal.h"

#ifndef BENTO_RUN_HW_TEST
#include "ux.h"
#endif

static const char *TAG = "bento";

// Rendering and the JSON link run here, so this stack is generous. It stays in internal RAM: PSRAM stacks were slow.
#define UX_STACK_BYTES (16 * 1024)

static void ux_task(void *arg)
{
#ifdef BENTO_RUN_HW_TEST
    hw_test_run();
#else
    ux_init();
    for (;;) ux_frame();
#endif
}

void app_main(void)
{
    hal_init();
    ESP_LOGI(TAG, "%s ready; %u KB internal, %u KB PSRAM free", BOARD.name, (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
    xTaskCreatePinnedToCore(ux_task, "ux", UX_STACK_BYTES, NULL, 5, NULL, 1);
}
