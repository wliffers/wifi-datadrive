#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: / and /index.html (embedded, gzip-compressed web UI). */
esp_err_t web_static_register(httpd_handle_t server);
