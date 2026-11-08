idf_component_register(
    SRCS "cst816.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api
    PRIV_REQUIRES esp_driver_gpio)
