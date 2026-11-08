// A rotary knob where each direction pulses its own line low, rather than a quadrature pair.
#pragma once
#include "driver_api.h"

typedef struct {
    int clockwise, anticlockwise; // the line that pulses for each direction
} knob_pulse_cfg_t;

extern const knob_driver_t KNOB_PULSE;
