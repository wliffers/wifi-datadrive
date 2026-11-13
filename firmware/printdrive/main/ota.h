// Firmware update over the web UI with automatic rollback (package P5).
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "esp_partition.h"

/** Call once at start-up, after the web server is running: confirms a freshly updated
 *  firmware as good after a grace period (otherwise the bootloader rolls back). */
esp_err_t ota_init(void);

/** Label of the running firmware partition ("ota_0"/"ota_1"/"factory") and whether the image
 *  is still waiting for its confirmation (state PENDING_VERIFY). */
void ota_get_info(char *partition_label, size_t len, bool *pending_verify);

/** Signed updates (CONFIG_PD_OTA_SIGNED, docs/17_signierte_updates.md): whether this firmware
 *  installs only updates signed with its own key, and the key id - the first 8 hex digits of
 *  the SHA-256 of the public key in the running firmware's signature block (the value
 *  tools/signing prints). key_id is "" when signed updates are not required. len >= 9. */
void ota_get_signing(bool *required, char *key_id, size_t len);

/** Only meaningful when signed updates are required, after esp_ota_end() refused an upload of
 *  image_size bytes written to part: whether the upload carries a Secure Boot V2 signature
 *  block (last 4 KiB, magic and CRC valid). If so, key_id receives the key id of its first
 *  block (same format as ota_get_signing(), may be NULL). Used only to explain the refusal;
 *  the decision itself is esp_ota_end()'s. */
bool ota_upload_has_signature(const esp_partition_t *part, size_t image_size, char *key_id, size_t len);
