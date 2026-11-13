/*
 * Firmware update over HTTP with automatic rollback.
 *
 * Update flow (web_ota.c, POST /api/ota):
 *   1. Refuse while a file transfer runs (BUSY) or the USB host wrote recently (HOST_WRITING).
 *   2. Check Content-Length against the size of the other app partition (413), then check the
 *      first bytes (image magic 0xE9, chip id ESP32-S3) BEFORE anything is erased (422).
 *   3. esp_ota_begin() on the inactive app slot, esp_ota_write() while streaming the body,
 *      esp_ota_end() validates the image (checksum/hash). Any error -> esp_ota_abort(); the
 *      running firmware is never touched.
 *   4. esp_ota_set_boot_partition(), answer {"ok":true,"restart":true}, then restart.
 *
 * Rollback: CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE makes the bootloader start a freshly written
 * image in state PENDING_VERIFY. ota_init() (called once the web server runs) starts a 15 s
 * timer which then calls esp_ota_mark_app_valid_cancel_rollback(). If the new firmware crashes
 * or resets before that, the bootloader marks it ABORTED at the next start and boots the
 * previous image again.
 *
 * First installation: the rollback option changes the second-stage bootloader, and OTA only
 * replaces the app slots, never the bootloader or partition table. So the first firmware with
 * this option must be flashed over USB; only later updates can use the web UI.
 *
 * Power loss during an update: the data goes to the inactive slot and the boot partition
 * (otadata) is switched only at the very end, after the image has been validated. So the old
 * firmware keeps running and the half-written slot is simply ignored and overwritten next time.
 *
 * Signed updates (CONFIG_PD_OTA_SIGNED -> CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT, see
 * docs/17_signierte_updates.md): ESP-IDF's "signed app verification without hardware secure
 * boot". esp_ota_end() (esp_image_verify) then also checks the Secure Boot V2 signature block
 * (RSA-3072, RSA-PSS) appended to the image, against the public key in the signature block of
 * the RUNNING firmware (only block 0 counts). A failed check returns ESP_ERR_OTA_VALIDATE_FAILED
 * before esp_ota_set_boot_partition() runs (which verifies once more), so a refused image never
 * becomes bootable. The bootloader does not check signatures in this mode (RSA scheme), so no new
 * bootloader is needed and the rollback after a crash works as before. At start-up ESP-IDF aborts
 * an app built with this option that carries no signature (esp_secure_boot_init_checks()) - over
 * OTA the bootloader then rolls back, over USB a signed image must be flashed.
 */
#include "ota.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
#if !CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME
#error "Signed updates on the ESP32-S3 use the RSA-3072 scheme (CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME)"
#endif
#include "esp_rom_crc.h"
#include "esp_secure_boot.h"
#include "mbedtls/sha256.h"
#endif

static const char *TAG = "ota";

#define OTA_CONFIRM_DELAY_US (15LL * 1000000)

static esp_timer_handle_t s_confirm_timer;

#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
#define SIG_SECTOR_SIZE 4096u   // the signature sector espsecure.py appends (4 KiB aligned)

static char s_key_id[9];        // key id of the running firmware, computed once
static int s_key_id_done;       // 1 once s_key_id is complete (__atomic acquire/release)

static void key_id_from_digest(const uint8_t *digest, char *out, size_t len)
{
    snprintf(out, len, "%02x%02x%02x%02x", digest[0], digest[1], digest[2], digest[3]);
}

/** Key id of the running firmware. Only block 0 is used for update checks in this mode. */
static const char *running_key_id(void)
{
    if (__atomic_load_n(&s_key_id_done, __ATOMIC_ACQUIRE)) {
        return s_key_id;
    }
    esp_image_sig_public_key_digests_t d = {0};
    if (esp_secure_boot_get_signature_blocks_for_running_app(true, &d) != ESP_OK || d.num_digests == 0) {
        return "";   // not reachable in practice: ESP-IDF aborts an unsigned app at start-up
    }
    // Two first callers at the same time write the same bytes; readers wait for the flag.
    key_id_from_digest(d.key_digests[0], s_key_id, sizeof(s_key_id));
    __atomic_store_n(&s_key_id_done, 1, __ATOMIC_RELEASE);
    return s_key_id;
}
#endif

void ota_get_signing(bool *required, char *key_id, size_t len)
{
#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
    if (required) {
        *required = true;
    }
    if (key_id && len) {
        strlcpy(key_id, running_key_id(), len);
    }
#else
    if (required) {
        *required = false;
    }
    if (key_id && len) {
        key_id[0] = '\0';
    }
#endif
}

bool ota_upload_has_signature(const esp_partition_t *part, size_t image_size, char *key_id, size_t len)
{
    if (key_id && len) {
        key_id[0] = '\0';
    }
#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
    // espsecure.py sign_data pads the image to 4 KiB and appends one 4 KiB signature sector, so
    // a signed file is a multiple of 4 KiB and its signature blocks start 4 KiB before the end.
    if (!part || image_size < 2 * SIG_SECTOR_SIZE || image_size % SIG_SECTOR_SIZE != 0) {
        return false;
    }
    ets_secure_boot_sig_block_t *blk = malloc(sizeof(*blk));   // 1216 bytes, not on the httpd stack
    if (!blk) {
        return false;
    }
    bool found = false;
    if (esp_partition_read(part, image_size - SIG_SECTOR_SIZE, blk, sizeof(*blk)) == ESP_OK &&
        blk->magic_byte == ETS_SECURE_BOOT_V2_SIGNATURE_MAGIC &&
        blk->block_crc == esp_rom_crc32_le(0, (const uint8_t *)blk, CRC_SIGN_BLOCK_LEN)) {
        found = true;
        uint8_t digest[32];
        if (key_id && len && mbedtls_sha256((const unsigned char *)&blk->key, sizeof(blk->key), digest, 0) == 0) {
            key_id_from_digest(digest, key_id, len);
        }
    }
    free(blk);
    return found;
#else
    (void)part;
    (void)image_size;
    return false;
#endif
}

static void confirm_cb(void *arg)
{
    (void)arg;
    const esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "new firmware confirmed as good, rollback cancelled");
    } else {
        ESP_LOGE(TAG, "could not confirm firmware: %s", esp_err_to_name(err));
    }
}

static bool running_pending_verify(void)
{
    esp_ota_img_states_t st;
    const esp_partition_t *run = esp_ota_get_running_partition();
    return run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY;
}

void ota_get_info(char *partition_label, size_t len, bool *pending_verify)
{
    const esp_partition_t *run = esp_ota_get_running_partition();
    if (partition_label && len) {
        strlcpy(partition_label, run ? run->label : "?", len);
    }
    if (pending_verify) {
        *pending_verify = running_pending_verify();
    }
}

esp_err_t ota_init(void)
{
#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
    ESP_LOGI(TAG, "web updates must be signed, key id %s", running_key_id());
#else
    ESP_LOGI(TAG, "web updates are not signature checked (CONFIG_PD_OTA_SIGNED off)");
#endif
    if (!running_pending_verify()) {
        return ESP_OK;
    }
    ESP_LOGW(TAG, "new firmware is pending verification, confirming in 15 s");
    const esp_timer_create_args_t args = {.callback = confirm_cb, .name = "ota_confirm"};
    esp_err_t err = esp_timer_create(&args, &s_confirm_timer);
    if (err == ESP_OK) {
        err = esp_timer_start_once(s_confirm_timer, OTA_CONFIRM_DELAY_US);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "confirm timer failed: %s (rollback will happen at next start)", esp_err_to_name(err));
    }
    return err;
}
