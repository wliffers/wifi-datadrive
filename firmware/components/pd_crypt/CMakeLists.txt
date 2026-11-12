idf_component_register(SRCS "pd_crypt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES sdmmc wear_levelling
                       PRIV_REQUIRES fatfs mbedtls hal esp_timer esp_system)
