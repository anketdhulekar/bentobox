// One binary, for every board: "--board" picks its facts at start, before anything reads them. The board folders'
// files define BOARD; here each is compiled under its own name (see the Makefile).
#include <stdbool.h>
#include <string.h>

#define BOARD BOARD_as_seen_by_others
#include "../hal/hal.h"
#undef BOARD

#ifndef SIM_RENDER
#define SIM_RENDER 1
#endif

extern const board_info_t BOARD_knob_lcd_1_8, BOARD_amoled_2_16;

board_info_t BOARD; // the UX reads it, and only this file writes it

bool sim_select_board(const char *name);
void sim_view_board(int width, int height, int shape, int mount_deg);

void sim_view_board(int width, int height, int shape, int mount_deg)
{
    BOARD.width = width;
    BOARD.height = height;
    BOARD.shape = (screen_shape_t)shape;
    BOARD.mount_deg = mount_deg;
}

bool sim_select_board(const char *name)
{
    if (!strcmp(name, BOARD_knob_lcd_1_8.name)) BOARD = BOARD_knob_lcd_1_8;
    else if (!strcmp(name, BOARD_amoled_2_16.name)) BOARD = BOARD_amoled_2_16;
    else return false;
    // The UX scales everything from the panel's width, so a bigger panel is all it takes to draw at a higher resolution.
    BOARD.width *= SIM_RENDER;
    BOARD.height *= SIM_RENDER;
    return true;
}
