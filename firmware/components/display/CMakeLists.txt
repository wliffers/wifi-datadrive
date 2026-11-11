idf_component_register(SRCS "display.c" "font5x7.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_lcd esp_driver_spi esp_driver_gpio
                       PRIV_REQUIRES board st7735)
