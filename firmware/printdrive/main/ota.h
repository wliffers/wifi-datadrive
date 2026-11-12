// Firmware update over the web UI with automatic rollback (package P5).
#pragma once

#include "esp_err.h"

/** Call once at start-up, after the web server is running: confirms a freshly updated
 *  firmware as good after a grace period (otherwise the bootloader rolls back). */
esp_err_t ota_init(void);
