// The two color profiles, the Pomodoro's warm-up, and a project's readable text. Colors as in preview/bento.html.
#pragma once
#include <stdint.h>

typedef struct {
    uint16_t face, ink, soft, sub, faint, amber, ok, toastBg, toastInk, offlineBg;
    uint16_t st[5]; // by agent_status_t
    uint16_t highlight; // a wheel list's selected row
} theme_t;

extern const theme_t THEMES[2]; // Mono, Inverted

#define COLOR_AMBER 0xF5A623u
#define COLOR_ORANGE 0xFF4D06u
#define COLOR_CRIMSON 0xE8143Au

// The warm-up. From 10 minutes left the color fades from the ink to orange at 5:00, then to crimson at 1:00, in OKLCH.
uint32_t color_warm(uint32_t ink_rgb, float minutes_left);
// Text on a filled color: near black on a light one, white on a dark one.
uint32_t color_readable_on(uint32_t rgb);
// a at t = 0 to b at t = 1, in plain RGB.
uint16_t color_mix565(uint16_t a, uint16_t b, float t);
