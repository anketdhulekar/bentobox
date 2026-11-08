idf_component_register(
    SRCS "gpio_buttons.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api
    PRIV_REQUIRES esp_driver_gpio)
