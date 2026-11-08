// The portable facts about this board. The simulator compiles this file too, so it includes only the HAL's header.
#include "hal.h"

const board_info_t BOARD = {
    .name = "amoled-2.16",
    .title = "Bento Square",
    .width = 480,
    .height = 480,
    .shape = SHAPE_SQUARE,
    .panel = PANEL_AMOLED,
    .has = {
        .knob = false,
        .haptics = false,
        .mic = true,     // two analog mics on an ES7210: see README.md
        .speaker = true,
        .motion = true,
        .battery = true,
    },
};
