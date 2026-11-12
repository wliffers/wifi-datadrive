#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Login, session and user management routes (package P1). */
esp_err_t web_auth_register(httpd_handle_t server);
