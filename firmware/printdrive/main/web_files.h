#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: /api/files, /api/download, /api/upload/check, /api/upload, /api/delete, /api/rename. */
esp_err_t web_files_register(httpd_handle_t server);
