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
 */
#include "ota.h"

#include <string.h>
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"

static const char *TAG = "ota";

#define OTA_CONFIRM_DELAY_US (15LL * 1000000)

static esp_timer_handle_t s_confirm_timer;

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
