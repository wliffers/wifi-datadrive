#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Firmware update route /api/ota (package P5). */
esp_err_t web_ota_register(httpd_handle_t server);
