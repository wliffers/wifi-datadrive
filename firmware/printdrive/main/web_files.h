#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes (all role user): /api/files, /api/download, /api/upload/check, /api/upload, /api/push,
 *  /api/mkdir, /api/delete, /api/move, /api/copy. */
esp_err_t web_files_register(httpd_handle_t server);
