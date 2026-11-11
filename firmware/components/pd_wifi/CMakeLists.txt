# firmware/ is added as a private include dir so that the optional, git-ignored
# firmware/pd_secrets.h can provide default Wi-Fi credentials.
idf_component_register(SRCS "pd_wifi.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "../.."
                       REQUIRES esp_wifi esp_netif esp_event
                       PRIV_REQUIRES nvs_flash esp_timer)
