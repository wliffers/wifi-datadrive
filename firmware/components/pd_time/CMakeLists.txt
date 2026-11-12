idf_component_register(SRCS "pd_time.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES esp_netif esp_event lwip nvs_flash log)
