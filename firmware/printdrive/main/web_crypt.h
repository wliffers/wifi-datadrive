#pragma once

#include <stdbool.h>
#include "drive.h"
#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: /api/crypt, /api/crypt/setup|unlock|lock|password|disable|recovery|erase (API v4, section 2). */
esp_err_t web_crypt_register(httpd_handle_t server);

/** DRIVE_ERR_LOCKED while the drive is encrypted and locked, DRIVE_ERR_CRYPT_DAMAGED while its key
 *  header is damaged, else DRIVE_OK (API v4, 2.3): file/folder/upload/download/push/owner/format
 *  handlers answer 423 with this result before looking at anything else. */
drive_result_t web_crypt_block(void);

/** True if web_crypt_block() is not DRIVE_OK (locked or damaged). */
bool web_crypt_locked(void);
