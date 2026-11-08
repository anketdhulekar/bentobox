idf_component_register(
    SRCS "adc_battery.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api
    PRIV_REQUIRES esp_adc)
