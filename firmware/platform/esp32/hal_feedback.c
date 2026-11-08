// hal_feedback: the UX asks by name, and the board's table (board.c) says what that is on its chips.
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hal_feedback";

#define HW BOARD_HW
#define ALARM_SECONDS HAL_ALARM_S
#define MAX_SOUND_SECONDS 1.2f
#define WRITE_CHUNK 512  // samples at a time, so a stop is heard within about 30 ms

static bool s_haptics_ok, s_speaker_ok;
static volatile bool s_buzzing, s_alarm_sound; // alarm is running on the motor, on the speaker
static int s_volume = 7;
static int64_t s_last_us[FB_COUNT];
static esp_timer_handle_t s_alarm_timer;

// ---- Sound --------------------------------------------------------------------------------------------------

static QueueHandle_t s_sounds;
static int16_t *s_pcm;
static volatile bool s_stop_sound;

static void play_sound(feedback_t kind)
{
    const feedback_map_t *m = &HW.feedback[kind];
    const size_t n = tone_render(m->tones, m->tone_count, s_pcm, (size_t)(MAX_SOUND_SECONDS * TONE_RATE));
    for (size_t off = 0; off < n && !s_stop_sound; off += WRITE_CHUNK) {
        HW.speaker->write(s_pcm + off, n - off < WRITE_CHUNK ? n - off : WRITE_CHUNK);
    }
}

static void sound_task(void *arg)
{
    for (;;) {
        // While the alarm sounds, it repeats; otherwise wait for the next sound.
        feedback_t kind;
        if (xQueueReceive(s_sounds, &kind, s_alarm_sound ? 0 : portMAX_DELAY) == pdTRUE) {
            s_stop_sound = false;
            if (!s_alarm_sound) play_sound(kind); // a sound that was waiting doesn't interrupt the alarm
        }
        if (s_alarm_sound) {
            s_stop_sound = false;
            play_sound(FB_ALARM);
            vTaskDelay(pdMS_TO_TICKS(250));
        }
    }
}

// ---- The alarm ----------------------------------------------------------------------------------------------

static void alarm_off(void)
{
    if (s_buzzing) {
        s_buzzing = false;
        HW.haptics->continuous(false);
    }
    s_alarm_sound = false;
    s_stop_sound = true;
}

static void on_alarm_timer(void *arg)
{
    ESP_LOGI(TAG, "the alarm ran its full %d s", ALARM_SECONDS);
    alarm_off();
}

static void alarm_on(bool sound_allowed)
{
    if (s_haptics_ok && !s_buzzing) {
        s_buzzing = true;
        HW.haptics->continuous(true);
    }
    if (s_speaker_ok && sound_allowed && s_volume > 0 && !s_alarm_sound) {
        s_alarm_sound = true;
        const feedback_t wake = FB_ALARM;
        xQueueSend(s_sounds, &wake, 0); // wakes the task, which then repeats it
    }
    if (s_alarm_timer) {
        esp_timer_stop(s_alarm_timer);
        esp_timer_start_once(s_alarm_timer, (uint64_t)ALARM_SECONDS * 1000000);
    }
}

// ---- The HAL ------------------------------------------------------------------------------------------------

void hal_feedback(feedback_t kind, bool sound_allowed)
{
    if (kind >= FB_COUNT) return;
    if (kind == FB_ALARM) {
        alarm_on(sound_allowed);
        return;
    }
    if (kind == FB_ALARM_STOP) {
        if (s_alarm_timer) esp_timer_stop(s_alarm_timer);
        alarm_off();
        return;
    }
    // While the time's-up alarm runs, other feedback is skipped. Touches and knob turns stop it first, so theirs still plays.
    if (s_buzzing || s_alarm_sound) return;
    const feedback_map_t *m = &HW.feedback[kind];
    if (m->min_gap_ms) {
        const int64_t now = esp_timer_get_time();
        if (now - s_last_us[kind] < (int64_t)m->min_gap_ms * 1000) return;
        s_last_us[kind] = now;
    }
    if (s_haptics_ok && m->effects[0]) HW.haptics->play(m->effects);
    if (s_speaker_ok && sound_allowed && s_volume > 0 && m->tone_count) xQueueSend(s_sounds, &kind, 0);
}

void hal_volume(int level)
{
    s_volume = level < 0 ? 0 : level > 10 ? 10 : level;
    if (s_speaker_ok) HW.speaker->volume(s_volume);
    if (s_volume == 0) s_stop_sound = true;
}

void hal_feedback_init(i2c_master_bus_handle_t bus)
{
    s_haptics_ok = HW.haptics && HW.haptics->init(HW.haptics_cfg, bus);
    s_speaker_ok = HW.speaker && HW.speaker->init(HW.speaker_cfg, bus);
    if (s_speaker_ok) {
        s_pcm = hal_alloc_large((size_t)(MAX_SOUND_SECONDS * TONE_RATE) * sizeof(int16_t));
        s_sounds = xQueueCreate(8, sizeof(feedback_t));
        if (!s_pcm || !s_sounds || xTaskCreatePinnedToCore(sound_task, "sound", 4096, NULL, 3, NULL, 0) != pdPASS) {
            ESP_LOGW(TAG, "no memory for sound");
            s_speaker_ok = false;
        } else {
            HW.speaker->volume(s_volume);
        }
    }
    if (s_haptics_ok || s_speaker_ok) {
        const esp_timer_create_args_t args = {.callback = on_alarm_timer, .name = "alarm"};
        if (esp_timer_create(&args, &s_alarm_timer) != ESP_OK) s_alarm_timer = NULL;
    }
}
