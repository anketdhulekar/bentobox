#include "layout.h"

const layout_t LAYOUT_SQUARE = {
    .base = 480,
    .rim = {.inset = 13, .width = 26, .radius = 27},
    .clock = {.pillY = 74, .pill = 19, .taskY = 156, .task = 36, .taskW = 380, .timeY = 286, .time = 120, .timeLong = 84,
              .statusY = 324, .status = 19, .micY = 392, .micR = 30, .noMicShift = 34},
    // The top of the screen: well under half of it.
    .arch = {.bottom = 184, .inset = 13, .width = 26, .radius = 27, .pillY = 52, .pill = 17, .pillW = 260, .timeY = 138,
             .time = 88, .timeLong = 62, .taskY = 168, .task = 20, .taskW = 400},
    .zoo = {.feet = 244, .sc = 10, .nameY = 296, .name = 36, .actY = 336, .act = 26, .lineY = 372, .line = 20, .textW = 430, .topY = 46, .top = 17},
    .zooClock = {.feet = 296, .sc = 6, .nameY = 334, .name = 30, .actY = 368, .act = 22, .lineY = 396, .line = 17, .textW = 420},
    .dayFull = {.timeY = 250, .time = 120, .ampm = 42, .dateY = 310, .date = 28, .dayY = 348, .day = 22},
    .dayLow = {.timeY = 318, .time = 88, .ampm = 36, .dateY = 366, .date = 24, .dayY = 400, .day = 20},
    .hasDots = true, .dotsY = 448, .dotsGap = 26, .stripHalf = 6, .countY = 428, .countSize = 16,
    .status = {.h = 13, .clock = {240, 13, true}, .arc = {240, 13, true}, .zoo = {240, 16, false}, .list = {240, 16, false}},
    .type = {.label = 42, .sub = 19, .hint = 16},
    // Two by three: Timer and Pomodoro, Agents and Clock, Settings and Lock.
    .menu = {.radius = 24, .sub = 18, .tiles = {
        {"timer", 14, 14, 221, 144, 58, 63, 60, 24, 112, 26, 137},
        {"pomodoro", 245, 14, 221, 144, 58, 294, 60, 255, 112, 26, 137},
        {"zoo", 14, 168, 221, 144, 58, 63, 214, 24, 266, 26, 291},
        {"clock", 245, 168, 221, 144, 58, 294, 214, 255, 266, 26, 291},
        {"settings", 14, 322, 221, 144, 58, 63, 368, 24, 420, 26, 0},
        {"lock", 245, 322, 221, 144, 58, 294, 368, 255, 420, 26, 0},
    }},
    // Five rows a screen at most, and a longer list scrolls.
    .list = {.wheel = false, .rows = 5, .rowH = 76, .y0 = 66, .maxY = 446, .x0 = 34, .x1 = 446, .titleY = 42, .title = 22, .big = 28, .small = 24},
    .lock = {.topX = 34, .topY = 50, .top = 19, .titleY = 50, .title = 21, .titleW = 270, .dotsY = 86, .dotR = 8, .dotGap = 32,
             .keyTop = 112, .keyH = 80, .keyW = 110, .keyR = 33, .key = 30},
    .card = {.centered = false, .charX = 76, .charFeet = 110, .charSc = 4, .nameX = 150, .nameY = 66, .name = 30, .nameW = 304, .subY = 96, .sub = 20,
             .rowY = 160, .rowH = 42, .row = 22, .labelX = 26, .valueX = 152, .valueW = 302, .hintY = 442, .hint = 20},
    .wave = {.cy = 318, .step = 9, .width = 5, .maxH = 23},
    .toastY = 402,
    .toast = 18,
};
