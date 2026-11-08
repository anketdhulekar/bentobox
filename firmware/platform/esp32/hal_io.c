// The screen, input, motion, power and the microphone: the HAL's face for whichever chips the board has.
#include <math.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hal_io";

#define HW BOARD_HW

// ---- The screen ---------------------------------------------------------------------------------------------

static int s_brightness = HAL_BRIGHT_LEVELS - 1;
static bool s_lit; // the screen stays dark until the first frame is on it
static bool s_screen_ok;

void hal_screen_present(const uint16_t *fb, int y0, int rows)
{
    if (!s_screen_ok) return; // it didn't come up: the log says why
    HW.screen->present(fb, y0, rows);
    if (!s_lit) {
        s_lit = true;
        HW.screen->brightness(HW.brightness[s_brightness]);
    }
}

void hal_screen_brightness(int level)
{
    s_brightness = level < 0 ? 0 : level >= HAL_BRIGHT_LEVELS ? HAL_BRIGHT_LEVELS - 1 : level;
    if (s_lit) HW.screen->brightness(HW.brightness[s_brightness]);
}

// ---- Input --------------------------------------------------------------------------------------------------

#define INPUT_PERIOD_MS 10
#define RELEASE_POLLS 5  // a lift counts after this many empty polls: the touch chips drop out for a read now and then
#define BUTTON_POLLS 3   // a press counts after this many equal reads
#define EVENT_QUEUE 32

static QueueHandle_t s_events;
static bool s_touch_ok, s_knob_ok, s_buttons_ok;

static void post(const hw_event_t *e)
{
    if (xQueueSend(s_events, e, 0) == pdTRUE) return;
    if (e->kind == HW_TOUCH_MOVE) return; // a full queue loses a move before it loses anything that matters
    hw_event_t oldest;
    xQueueReceive(s_events, &oldest, 0);
    xQueueSend(s_events, e, 0);
}

static void poll_touch(void)
{
    static bool down;
    static int last_x, last_y, empty;
    int x, y;
    if (HW.touch->read(&x, &y)) {
        empty = 0;
        x = x < 0 ? 0 : x >= BOARD.width ? BOARD.width - 1 : x;
        y = y < 0 ? 0 : y >= BOARD.height ? BOARD.height - 1 : y;
        if (!down || x != last_x || y != last_y) {
            const hw_event_t e = {.kind = down ? HW_TOUCH_MOVE : HW_TOUCH_DOWN, .x = x, .y = y};
            post(&e);
        }
        down = true;
        last_x = x;
        last_y = y;
    } else if (down && ++empty >= RELEASE_POLLS) {
        down = false;
        const hw_event_t e = {.kind = HW_TOUCH_UP, .x = last_x, .y = last_y};
        post(&e);
    }
}

static void poll_knob(void)
{
    for (int steps = HW.knob->take_steps(); steps != 0; steps += steps > 0 ? -1 : 1) {
        const hw_event_t e = {.kind = HW_KNOB, .step = steps > 0 ? 1 : -1};
        post(&e);
    }
}

static void poll_buttons(void)
{
    static uint32_t stable, last_raw;
    static int same;
    const uint32_t raw = HW.buttons->read();
    if (raw != last_raw) {
        last_raw = raw;
        same = 0;
        return;
    }
    if (same < BUTTON_POLLS) {
        same++;
        return;
    }
    if (raw == stable) return;
    const uint32_t pressed = raw & ~stable;
    stable = raw;
    for (int b = 0; b < 32; b++) {
        if (pressed & (1u << b)) {
            const hw_event_t e = {.kind = HW_BUTTON, .button = (hw_button_t)b};
            post(&e);
        }
    }
}

static void input_task(void *arg)
{
    for (;;) {
        if (s_touch_ok) poll_touch();
        if (s_knob_ok) poll_knob();
        if (s_buttons_ok) poll_buttons();
        vTaskDelay(pdMS_TO_TICKS(INPUT_PERIOD_MS));
    }
}

bool hal_input_poll(hw_event_t *e) { return s_events && xQueueReceive(s_events, e, 0) == pdTRUE; }

// ---- Motion and power ---------------------------------------------------------------------------------------

static bool s_motion_ok, s_power_ok;

bool hal_motion_read(float *x, float *y, float *z)
{
    float a[3];
    if (!s_motion_ok || !HW.motion->read(a)) return false;
    float g[3];
    for (int i = 0; i < 3; i++) {
        const int axis = HW.motion_axes[i];
        // An accelerometer at rest reads the support pushing up, so gravity is the opposite of what it reads.
        g[i] = axis > 0 ? -a[axis - 1] : a[-axis - 1];
    }
    *x = g[0];
    *y = g[1];
    *z = g[2];
    return true;
}

// The UX may ask every frame, and this is slow I2C or ADC work that changes slowly: refresh it every couple of seconds.
#define POWER_REFRESH_US (2 * 1000 * 1000)

void hal_power_read(hal_power_t *out)
{
    static hal_power_t s_cached = {.percent = -1};
    static int64_t s_next;
    const int64_t now = esp_timer_get_time();
    if (s_power_ok && now >= s_next) {
        HW.power->read(&s_cached);
        s_next = now + POWER_REFRESH_US;
    }
    *out = s_cached;
}

// ---- The microphone -----------------------------------------------------------------------------------------

// A task reads the chip into a ring that hal_mic_read empties, so hal_mic_read never blocks. The ring lives in PSRAM and
// holds about 2 s, enough for the UX to fall behind while it sends over Bluetooth. One task writes and one reads.
#define MIC_RING_SAMPLES 32768 // a power of two, so the counts below wrap cleanly
#define MIC_CHUNK 256 // 16 ms, which bounds how long a stop waits

static bool s_mic_ok;
static int16_t *s_mic_ring;
static volatile uint32_t s_mic_head, s_mic_tail; // count samples; the ring's position is the count modulo its size
static volatile bool s_mic_run, s_mic_busy;
static TaskHandle_t s_mic_task;

static void mic_task(void *arg)
{
    int16_t chunk[MIC_CHUNK];
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        while (s_mic_run) {
            const int n = HW.mic->read(chunk, MIC_CHUNK, 100);
            uint32_t head = s_mic_head;
            for (int i = 0; i < n; i++) s_mic_ring[head++ % MIC_RING_SAMPLES] = chunk[i];
            __atomic_store_n(&s_mic_head, head, __ATOMIC_RELEASE);
        }
        HW.mic->stop();
        s_mic_busy = false;
    }
}

bool hal_mic_start(void)
{
    if (!s_mic_ok) return false;
    if (!s_mic_ring) {
        s_mic_ring = hal_alloc_large(MIC_RING_SAMPLES * sizeof(int16_t));
        if (!s_mic_ring) return false;
    }
    if (!s_mic_task && xTaskCreatePinnedToCore(mic_task, "mic", 3072, NULL, 6, &s_mic_task, 1) != pdPASS) {
        s_mic_task = NULL;
        return false;
    }
    if (s_mic_run) return true; // already recording
    for (int i = 0; s_mic_busy; i++) { // the last recording is still stopping
        if (i >= 20) return false;
        hal_sleep_ms(10);
    }
    if (!HW.mic->start()) {
        ESP_LOGW(TAG, "the microphone didn't start");
        return false;
    }
    __atomic_store_n(&s_mic_tail, __atomic_load_n(&s_mic_head, __ATOMIC_ACQUIRE), __ATOMIC_RELEASE);
    s_mic_busy = true;
    s_mic_run = true;
    xTaskNotifyGive(s_mic_task);
    return true;
}

int hal_mic_read(int16_t *out, int max)
{
    if (!s_mic_ring) return 0;
    const uint32_t head = __atomic_load_n(&s_mic_head, __ATOMIC_ACQUIRE);
    uint32_t tail = s_mic_tail;
    if (head - tail > MIC_RING_SAMPLES) tail = head - MIC_RING_SAMPLES; // it fell behind: the oldest samples are gone
    int n = 0;
    while (n < max && tail != head) out[n++] = s_mic_ring[tail++ % MIC_RING_SAMPLES];
    s_mic_tail = tail;
    return n;
}

void hal_mic_stop(void) { s_mic_run = false; }

// ---- Start --------------------------------------------------------------------------------------------------

void hal_io_init(i2c_master_bus_handle_t bus)
{
    // The power chip first, in case a later board needs it to turn on a rail for the others.
    s_power_ok = HW.power && HW.power->init(HW.power_cfg, bus);
    s_screen_ok = HW.screen->init(HW.screen_cfg);
    if (!s_screen_ok) ESP_LOGE(TAG, "no screen");
    s_touch_ok = HW.touch && HW.touch->init(HW.touch_cfg, bus);
    s_knob_ok = HW.knob && HW.knob->init(HW.knob_cfg);
    s_buttons_ok = HW.buttons && HW.buttons->init(HW.buttons_cfg);
    s_motion_ok = HW.motion && HW.motion->init(HW.motion_cfg, bus);
    s_mic_ok = HW.mic && HW.mic->init(HW.mic_cfg, bus);
    s_events = xQueueCreate(EVENT_QUEUE, sizeof(hw_event_t));
    if (s_events) xTaskCreatePinnedToCore(input_task, "input", 4096, NULL, 4, NULL, 0);
}
