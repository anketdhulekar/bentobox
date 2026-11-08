idf_component_register(
    SRCS "co5300.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api panel_bands
    PRIV_REQUIRES esp_driver_spi esp_lcd)
