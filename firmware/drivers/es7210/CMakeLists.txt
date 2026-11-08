idf_component_register(
    SRCS "es7210.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api i2s_codec_bus
    PRIV_REQUIRES esp_driver_i2c)
