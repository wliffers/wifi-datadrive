// Firmware update over the web UI with automatic rollback (package P5).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

/** Call once at start-up, after the web server is running: confirms a freshly updated
 *  firmware as good after a grace period (otherwise the bootloader rolls back). */
esp_err_t ota_init(void);

/** Label of the running firmware partition ("ota_0"/"ota_1"/"factory") and whether the image
 *  is still waiting for its confirmation (state PENDING_VERIFY). */
void ota_get_info(char *partition_label, size_t len, bool *pending_verify);
