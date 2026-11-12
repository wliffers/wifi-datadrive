#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: /api/wifi. */
esp_err_t web_wifi_register(httpd_handle_t server);
