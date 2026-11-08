// What the simulator's window (main.m) and its HAL (hal_sim.c) share.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "../hal/hal.h"

// How many pixels the UX draws per panel pixel. It's 1 in the plain and sanitized builds, which match the firmware's
// pixels, and 2 in bento-sim-sharp, the one with a window worth looking at (see the Makefile).
#ifndef SIM_RENDER
#define SIM_RENDER 1
#endif

typedef struct {
    const char *board;
    bool headless, demo;
    const char *wav;
    const char *store_path;
    int link_port, debug_port;
    int scale;
    int agents; // with --demo: how many to start with
    const char *panel_file; // --panel-file: also keep the panel in this file, for a viewer
    const char *view;       // --view: no UX at all, only a window on another simulator's panel file
    int argc;
    char **argv;
} sim_options_t;

extern sim_options_t SIM;

bool sim_select_board(const char *name); // false if there's no such board
void sim_view_board(int width, int height, int shape, int mount_deg);
bool sim_view_open(const char *path);    // --view: false if another viewer already has this file
void sim_view_sync(void);                // --view: whichever board is on it now
void sim_run_ux(void);                    // hal_init, ux_init and the loop: never returns

// From the window, on its own thread.
void sim_touch(hw_kind_t kind, int x, int y);
void sim_knob(int step);
void sim_button(hw_button_t b);
void sim_set_tilt(float deg);
float sim_tilt(void);
void sim_set_flat(bool flat);
bool sim_flat(void);
void sim_copy_panel(uint16_t *out);        // BOARD.width x BOARD.height
uint64_t sim_panel_generation(void);       // changes when the panel does
int sim_brightness(void);
void sim_feedback_text(char *out, int n);  // what the board just did, for a moment
void sim_toggle_mac(void);                 // with --demo
void sim_toggle_needs(void);               // with --demo
void sim_add_agents(int delta);            // with --demo
int sim_agent_count(void);                 // with --demo

// A command for the window, from the debug interface: "window PATH", "click X Y", "key K", "scroll N". For checking the
// window itself. Set by main.m; NULL when headless.
extern void (*sim_window_command)(const char *cmd);

// demo.c: a stand-in for the desktop app, with --demo. Real messages through the real link code.
void demo_start(void);
void demo_poll(void);
void demo_on_line(const char *line);
int demo_read(uint8_t *out, int max);
bool demo_connected(void);
void demo_toggle_mac(void);
void demo_toggle_needs(void);
void demo_set_agents(int n);
int demo_agent_count(void);
