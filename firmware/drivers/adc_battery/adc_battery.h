// A battery seen only through a resistor divider on an ADC pin. A board with no power chip gets the voltage and nothing
// else, so "charging" is never known, and while USB holds the rail above any cell's voltage, neither is the level.
#pragma once
#include "driver_api.h"

typedef struct {
    int pin;
    int divider;     // the rail's voltage over the pin's voltage
    int usb_above_mv; // a higher reading means USB power, not the cell
} adc_battery_cfg_t;

extern const power_driver_t ADC_BATTERY;
