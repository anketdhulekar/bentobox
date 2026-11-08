// The portable facts about this board. The simulator compiles this file too, so it includes only the HAL's header.
#include "hal.h"

const board_info_t BOARD = {
    .name = "BOARD_NAME", // the name of this folder
    .title = "Bento BOARD_TITLE", // what people see in Bluetooth and the desktop app, such as "Bento Knob"
    .width = 480,
    .height = 480,
    .shape = SHAPE_SQUARE,
    .panel = PANEL_AMOLED,
    .mount_deg = 0, // 180 if the panel's top is at the bottom when the board is held the right way up
    .has = {
        .knob = false,
        .haptics = false,
        .mic = false,
        .speaker = false,
        .motion = false,
        .battery = false,
    },
};
