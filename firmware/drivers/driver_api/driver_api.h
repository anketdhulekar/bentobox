// What each kind of chip offers the platform code. A driver in drivers/<chip>/ fills one of these and takes its pins
// from a config struct that the board's board.c provides, so two boards with the same chip share its driver.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "hal.h"

typedef struct {
    bool (*init)(const void *cfg); // the panel ends up cleared to black and dark
    void (*present)(const uint16_t *fb, int y0, int rows); // copies what it needs before returning (see hal.h)
    void (*brightness)(uint8_t level);                     // 0 .. 255
} screen_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    bool (*read)(int *x, int *y); // true while touched; panel pixels
} touch_driver_t;

typedef struct {
    bool (*init)(const void *cfg);
    int (*take_steps)(void); // detents since the last call: clockwise is positive
} knob_driver_t;

typedef struct {
    bool (*init)(const void *cfg);
    uint32_t (*read)(void); // a bit per hw_button_t held down, raw: the caller debounces
} buttons_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    void (*play)(const uint8_t effects[8]); // effect numbers from the chip's library; 0 ends the list
    void (*continuous)(bool on);            // vibrate at full scale until turned off
} haptics_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    bool (*write)(const int16_t *pcm, size_t samples); // 16 kHz mono; blocks until queued
    void (*volume)(int level);                         // 0 (off) .. 10
} speaker_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    bool (*read)(float accel_g[3]); // the chip's own axes, as its accelerometer reports them
} motion_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    void (*read)(hal_power_t *out);
} power_driver_t;

typedef struct {
    bool (*init)(const void *cfg, i2c_master_bus_handle_t bus);
    bool (*start)(void);
    int (*read)(int16_t *out, int max, int timeout_ms); // 16 kHz mono; samples read, waiting at most timeout_ms
    void (*stop)(void);
} mic_driver_t;

// A sound on a board with a speaker: a sweep from f0 to f1 Hz, starting at start seconds, lasting dur seconds, at
// volume vol (0 .. 1). tone_render mixes a list of them into 16-bit mono samples.
typedef enum { WAVE_SINE, WAVE_TRIANGLE, WAVE_SQUARE } tone_wave_t;

typedef struct {
    float f0, f1, start, dur, vol;
    tone_wave_t wave;
} tone_t;

#define TONE_RATE 16000

size_t tone_render(const tone_t *tones, int count, int16_t *out, size_t max_samples);
