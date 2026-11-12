#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: /api/status, /api/mode, /api/settings/host_writable, /api/format, /api/reboot, /api/log. */
esp_err_t web_system_register(httpd_handle_t server);
