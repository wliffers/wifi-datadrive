#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/**
 * GET /api/zip?path=<folder> (role user, registered in web_files.c's route table): streams the
 * folder with all sub-folders as a ZIP archive (method "stored", data descriptors, ZIP64 when
 * needed), chunked, "Content-Disposition: attachment". path "" = root folder.
 */
esp_err_t web_zip_get(httpd_req_t *req);
