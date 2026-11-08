// hal_init, time and memory.
#include "esp_heap_caps.h"
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hal";

static i2c_master_bus_handle_t i2c_start(void)
{
    if (BOARD_HW.i2c_sda < 0) return NULL;
    const i2c_master_bus_config_t cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = I2C_NUM_0,
        .sda_io_num = BOARD_HW.i2c_sda,
        .scl_io_num = BOARD_HW.i2c_scl,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true, // the boards also have external pull-ups
    };
    i2c_master_bus_handle_t bus = NULL;
    ESP_ERROR_CHECK(i2c_new_master_bus(&cfg, &bus));
    return bus;
}

void hal_init(void)
{
    ESP_LOGI(TAG, "starting on %s", BOARD.name);
    hal_debug_init();
    hal_store_init();
    i2c_master_bus_handle_t bus = i2c_start();
    hal_io_init(bus);
    hal_feedback_init(bus);
    hal_link_init();
}

int64_t hal_now_us(void) { return esp_timer_get_time(); }

uint32_t hal_random(void) { return esp_random(); }

void hal_device_mac(uint8_t mac[6])
{
    if (esp_efuse_mac_get_default(mac) != ESP_OK) memset(mac, 0, 6);
}

void hal_restart(void) { esp_restart(); }

uint32_t hal_fpu_control(void)
{
    uint32_t fcr;
    __asm__ volatile("rur.fcr %0" : "=a"(fcr));
    return fcr;
}

void hal_memory(size_t *free_internal, size_t *min_free_internal, size_t *free_large)
{
    *free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    *min_free_internal = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    *free_large = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
}

void hal_sleep_ms(int ms)
{
    const TickType_t ticks = pdMS_TO_TICKS(ms);
    vTaskDelay(ticks > 0 ? ticks : 1); // always yields, also for 0
}

void *hal_alloc_large(size_t bytes) { return heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT); }
