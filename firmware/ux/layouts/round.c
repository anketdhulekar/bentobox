#include "layout.h"

const layout_t LAYOUT_ROUND = {
    .base = 360,
    .rim = {.inset = 12.5f, .width = 25, .radius = 0},
    .clock = {.pillY = 64, .pill = 15, .taskY = 123, .task = 28, .taskW = 250, .timeY = 212, .time = 88, .timeLong = 62,
              .statusY = 238, .status = 14, .micY = 282, .micR = 23},
    // The top arc: well under half the height.
    .arch = {.bottom = 138, .inset = 12.5f, .width = 25, .radius = 0, .pillY = 46, .pill = 12.5f, .pillW = 124, .timeY = 108,
             .time = 62, .timeLong = 48, .taskY = 127, .task = 13, .taskW = 230},
    .zoo = {.feet = 192, .sc = 7, .nameY = 234, .name = 24, .actY = 262, .act = 17, .lineY = 286, .line = 13, .textW = 250, .topY = 40, .top = 13},
    .zooClock = {.feet = 225, .sc = 5, .nameY = 253, .name = 20, .actY = 276, .act = 15, .lineY = 296, .line = 12, .textW = 250},
    .dayFull = {.timeY = 188, .time = 88, .ampm = 36, .dateY = 232, .date = 20, .dayY = 260, .day = 17},
    .dayLow = {.timeY = 232, .time = 62, .ampm = 24, .dateY = 268, .date = 16, .dayY = 292, .day = 14},
    .dotsRimInset = 24,
    .stripHalf = 5,
    .stripStep = 0.028f,
    .countY = 318,
    .countSize = 12,
    .status = {.h = 10, .clock = {180, 12.5f, true}, .arc = {180, 12.5f, true}, .zoo = {180, 14, false}, .list = {180, 14, false}},
    .type = {.label = 30, .sub = 14, .hint = 12},
    // Six compartments clockwise from the upper left, with Timer and Pomodoro side by side on top as on the square.
    .menu = {.order = {"timer", "pomodoro", "zoo", "clock", "lock", "settings"}, .first = -0.5f, .ro = 177, .ri = 88, .gap = 7, .corner = 13,
             .mid = 132, .icon = 44, .icon_dy = -9, .label_dy = 29, .label = 14, .hub = 34, .hub_small = 12},
    .list = {.wheel = true, .rowH = 50, .x0 = 56, .x1 = 304, .titleY = 48, .title = 14, .hintY = 322, .hint = 13, .big = 22, .small = 17},
    .lock = {.topY = 36, .top = 14, .titleY = 58, .title = 14, .titleW = 220, .dotsY = 78, .dotR = 5, .dotGap = 20, .keyTop = 96,
             .keyH = 54, .keyW = 72, .keyR = 23, .key = 22},
    .card = {.centered = true, .charX = 180, .charFeet = 74, .charSc = 3, .nameX = 180, .nameY = 100, .name = 20, .nameW = 240, .subY = 118, .sub = 12,
             .rowY = 144, .rowH = 25, .row = 13, .labelX = 44, .valueX = 118, .valueW = 196, .hintY = 312, .hint = 12},
    .wave = {.cy = 233, .step = 7, .width = 4, .maxH = 17},
    .toastY = 316,
    .toast = 13,
};
