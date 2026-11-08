idf_component_register(
    SRCS "knob_pulse.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api
    PRIV_REQUIRES esp_driver_gpio esp_timer)
