idf_component_register(
    SRCS "pdm_mic.c"
    INCLUDE_DIRS "."
    REQUIRES driver_api
    PRIV_REQUIRES esp_driver_i2s)
