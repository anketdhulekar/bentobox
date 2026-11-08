idf_component_register(
    SRCS "st77916.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api panel_bands
    PRIV_REQUIRES esp_driver_spi esp_driver_ledc esp_lcd)
