// The portable facts about this board. The simulator compiles this file too, so it includes only the HAL's header.
#include "hal.h"

const board_info_t BOARD = {
    .name = "knob-lcd-1.8",
    .title = "Bento Knob",
    .width = 360,
    .height = 360,
    .shape = SHAPE_ROUND,
    .panel = PANEL_LCD,
    .mount_deg = 180, // the panel's top is at the bottom, as the board is held: the old timer firmware's UI_ROTATION
    .has = {
        .knob = true,
        .haptics = true,
        .mic = true,
        .speaker = false, // a DAC and a 3.5 mm jack, but no speaker on board: see README.md
        .motion = false,  // no motion sensor on the schematic
        .battery = true,  // a divider on GPIO1: see README.md for what it can and can't tell
    },
};
