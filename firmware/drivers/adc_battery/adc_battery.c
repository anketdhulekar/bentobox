#include "adc_battery.h"

#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"

static const char *TAG = "adc_battery";

static const adc_battery_cfg_t *s_cfg;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static adc_channel_t s_channel;
static float s_smooth_mv; // readings jump about 100 mV between samples, and more when the motor kicks

static bool init(const void *c, i2c_master_bus_handle_t bus)
{
    s_cfg = c;
    adc_unit_t unit;
    if (adc_oneshot_io_to_channel(s_cfg->pin, &unit, &s_channel) != ESP_OK) return false;
    const adc_oneshot_unit_init_cfg_t unit_cfg = {.unit_id = unit};
    if (adc_oneshot_new_unit(&unit_cfg, &s_adc) != ESP_OK) return false;
    const adc_oneshot_chan_cfg_t chan = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12};
    if (adc_oneshot_config_channel(s_adc, s_channel, &chan) != ESP_OK) return false;
    const adc_cali_curve_fitting_config_t cali = {.unit_id = unit, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12};
    if (adc_cali_create_scheme_curve_fitting(&cali, &s_cali) != ESP_OK) s_cali = NULL;
    ESP_LOGI(TAG, "ready on GPIO %d%s", s_cfg->pin, s_cali ? "" : " (uncalibrated)");
    return true;
}

// A lithium cell's voltage at each 10 % of charge, under the light load a board draws.
static int percent_from_mv(int mv)
{
    static const struct { int mv, pct; } CURVE[] = {
        {3300, 0}, {3550, 5}, {3680, 10}, {3740, 20}, {3770, 30}, {3790, 40},
        {3820, 50}, {3870, 60}, {3920, 70}, {3980, 80}, {4060, 90}, {4150, 100},
    };
    const int n = sizeof CURVE / sizeof CURVE[0];
    if (mv <= CURVE[0].mv) return 0;
    for (int i = 1; i < n; i++) {
        if (mv < CURVE[i].mv) {
            return CURVE[i - 1].pct + (mv - CURVE[i - 1].mv) * (CURVE[i].pct - CURVE[i - 1].pct) / (CURVE[i].mv - CURVE[i - 1].mv);
        }
    }
    return 100;
}

static void read_power(hal_power_t *out)
{
    *out = (hal_power_t){.percent = -1};
    if (!s_adc) return;
    int sum = 0, samples = 0;
    for (int i = 0; i < 16; i++) {
        int raw;
        if (adc_oneshot_read(s_adc, s_channel, &raw) == ESP_OK) {
            sum += raw;
            samples++;
        }
    }
    if (!samples) return;
    int pin_mv = sum / samples * 3300 / 4095;
    if (s_cali) adc_cali_raw_to_voltage(s_cali, sum / samples, &pin_mv);
    const float mv = (float)(pin_mv * s_cfg->divider);
    s_smooth_mv = s_smooth_mv > 0 ? s_smooth_mv + (mv - s_smooth_mv) / 8 : mv;
    // For checking this board's numbers on the hardware. A level comes from these volts, so a wrong level starts here.
    static int s_reads;
    if (s_reads++ % 15 == 0) ESP_LOGI(TAG, "rail %d mV", (int)mv);
    out->ok = true;
    // USB shows at once in the raw reading, and the level comes from the smoothed one.
    out->usb = mv > (float)s_cfg->usb_above_mv;
    if (out->usb) {
        s_smooth_mv = 0; // start again from the cell's real voltage after USB is unplugged
    } else {
        out->percent = percent_from_mv((int)s_smooth_mv);
    }
}

const power_driver_t ADC_BATTERY = {.init = init, .read = read_power};
