#include "web_ota.h"

#include <stdlib.h>
#include <string.h>
#include "drive.h"
#include "esp_app_format.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ota.h"
#include "sdkconfig.h"
#include "web_common.h"

static const char *TAG = "ota";

#define HDR_BYTES (sizeof(esp_image_header_t))
#define LOG_STEP  (256 * 1024)

#define MSG_INVALID       "Das ist keine gültige Gerätesoftware für dieses Laufwerk."
#define MSG_IO            "Fehler beim Schreiben in den Speicher des Geräts."
#define MSG_BAD_SIGNATURE "Die Update-Datei ist nicht mit dem Schlüssel dieses Geräts signiert."
#define MSG_UNSIGNED      "Die Update-Datei ist nicht signiert. Dieses Gerät nimmt nur signierte Updates an."

#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
/** esp_ota_end() refused the image (signed updates required): tell UNSIGNED from BAD_SIGNATURE
 *  (wrong key, or changed/damaged after signing - the signature no longer matches). */
static esp_err_t send_signature_error(httpd_req_t *req, const esp_partition_t *part, size_t total)
{
    char got[9], own[9];
    if (!ota_upload_has_signature(part, total, got, sizeof(got))) {
        ESP_LOGW(TAG, "rejected: image carries no signature");
        return web_send_error(req, "422 Unprocessable Entity", "UNSIGNED", MSG_UNSIGNED);
    }
    ota_get_signing(NULL, own, sizeof(own));
    ESP_LOGW(TAG, "rejected: signature does not verify (image key id %s, device key id %s)", got, own);
    return web_send_error(req, "422 Unprocessable Entity", "BAD_SIGNATURE", MSG_BAD_SIGNATURE);
}
#endif

static bool drain_body(httpd_req_t *req, size_t remaining)
{
    int timeouts = 0;
    while (remaining > 0) {
        const int n = httpd_req_recv(req, (char *)web_io_buf(),
                                     remaining < WEB_IO_BUF_SIZE ? remaining : WEB_IO_BUF_SIZE);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            return false;
        }
        remaining -= (size_t)n;
    }
    return true;
}

/** Drain the unread body, then answer. */
static esp_err_t fail(httpd_req_t *req, size_t remaining, const char *status, const char *code, const char *msg)
{
    if (!drain_body(req, remaining)) {
        return ESP_FAIL;
    }
    return web_send_error(req, status, code, msg);
}

static esp_err_t h_ota(httpd_req_t *req)
{
    uint8_t *const buf = web_io_buf();
    const size_t total = req->content_len;

    drive_status_t d;
    drive_get_status(&d);
    if (d.upload_active) {
        return fail(req, total, "409 Conflict", "BUSY",
                    "Es läuft gerade eine Übertragung. Bitte warten, bis sie fertig ist.");
    }
    if (d.host_write_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
        return fail(req, total, "409 Conflict", "HOST_WRITING",
                    "Das USB-Gerät schreibt gerade auf das Laufwerk. Bitte warten, bis es fertig ist.");
    }

    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    if (!part) {
        return fail(req, total, "500 Internal Server Error", "IO", "Es gibt keinen Platz für die neue Gerätesoftware.");
    }
    if (total == 0 || total > part->size) {
        return fail(req, total, "413 Payload Too Large", "TOO_LARGE",
                    "Die Gerätesoftware ist zu groß für den Speicherplatz im Gerät.");
    }
    if (total < HDR_BYTES) {
        return fail(req, total, "422 Unprocessable Entity", "INVALID_FILE", MSG_INVALID);
    }

    ESP_LOGI(TAG, "update start: %u bytes -> %s", (unsigned)total, part->label);
    esp_ota_handle_t handle = 0;
    bool begun = false;
    size_t remaining = total, written = 0, next_log = LOG_STEP, fill = 0;
    int timeouts = 0;
    const int64_t t0 = esp_timer_get_time();

    while (remaining > 0) {
        const size_t want = remaining < WEB_IO_BUF_SIZE - fill ? remaining : WEB_IO_BUF_SIZE - fill;
        const int n = httpd_req_recv(req, (char *)buf + fill, want);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            ESP_LOGW(TAG, "connection lost (%d), update aborted", n);
            if (begun) {
                esp_ota_abort(handle);
            }
            return ESP_FAIL;
        }
        timeouts = 0;
        fill += (size_t)n;
        remaining -= (size_t)n;

        if (!begun) {
            if (fill < HDR_BYTES) {
                continue;   // need the whole header first (total >= HDR_BYTES, so more will come)
            }
            const esp_image_header_t *h = (const esp_image_header_t *)buf;
            if (h->magic != ESP_IMAGE_HEADER_MAGIC || h->chip_id != ESP_CHIP_ID_ESP32S3) {
                ESP_LOGW(TAG, "rejected: not an ESP32-S3 image");
                return fail(req, remaining, "422 Unprocessable Entity", "INVALID_FILE", MSG_INVALID);
            }
#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
            // A signed file always ends with a 4 KiB signature sector on a 4 KiB boundary
            // (espsecure.py sign_data): any other size cannot be signed - refuse before erasing.
            if (total % 4096 != 0) {
                ESP_LOGW(TAG, "rejected: size %u is no multiple of 4096, cannot carry a signature", (unsigned)total);
                return fail(req, remaining, "422 Unprocessable Entity", "UNSIGNED", MSG_UNSIGNED);
            }
#endif
            const esp_err_t err = esp_ota_begin(part, total, &handle);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(err));
                return fail(req, remaining, "500 Internal Server Error", "IO", MSG_IO);
            }
            begun = true;
        }
        if (fill == WEB_IO_BUF_SIZE || remaining == 0) {
            const esp_err_t err = esp_ota_write(handle, buf, fill);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_write: %s", esp_err_to_name(err));
                esp_ota_abort(handle);
                if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
                    return fail(req, remaining, "422 Unprocessable Entity", "INVALID_FILE", MSG_INVALID);
                }
                return fail(req, remaining, "500 Internal Server Error", "IO", MSG_IO);
            }
            written += fill;
            fill = 0;
            if (written >= next_log) {
                ESP_LOGI(TAG, "written %u / %u bytes", (unsigned)written, (unsigned)total);
                next_log = written + LOG_STEP;
            }
        }
    }

    // Validates the image (checksum, hash and - with signed updates - the signature against the
    // key of the running firmware); frees the handle. On failure nothing else changes: the boot
    // partition still points to the running firmware.
    esp_err_t err = esp_ota_end(handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end: %s", esp_err_to_name(err));
        if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
#if CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT
            return send_signature_error(req, part, total);
#else
            return web_send_error(req, "422 Unprocessable Entity", "INVALID_FILE", MSG_INVALID);
#endif
        }
        return web_send_error(req, "500 Internal Server Error", "IO", MSG_IO);
    }
    err = esp_ota_set_boot_partition(part);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition: %s", esp_err_to_name(err));
        return web_send_error(req, "500 Internal Server Error", "IO", MSG_IO);
    }
    ESP_LOGI(TAG, "update ok: %u bytes in %lld ms, restarting into %s", (unsigned)total,
             (esp_timer_get_time() - t0) / 1000, part->label);

    // The image is in place. Wait (up to 10 s) until the USB host is not writing, then stop host
    // I/O (blocks permanently) - forced only after the timeout. The wait happens before the
    // answer, which keeps the web client waiting for the answer meanwhile (acceptable, and the
    // forced step then never drops the host's data without need).
    drive_result_t r = drive_prepare_restart(false);
    for (int i = 0; i < 20 && (r == DRIVE_ERR_HOST_WRITING || r == DRIVE_ERR_BUSY); i++) {
        vTaskDelay(pdMS_TO_TICKS(500));
        r = drive_prepare_restart(false);
    }
    if (r != DRIVE_OK) {
        ESP_LOGW(TAG, "drive_prepare_restart: %s - forcing", drive_result_str(r));
        r = drive_prepare_restart(true);
        if (r != DRIVE_OK) {
            ESP_LOGW(TAG, "drive_prepare_restart(force): %s", drive_result_str(r));
        }
    }
    web_send_json(req, "200 OK", "{\"ok\":true,\"restart\":true}");
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK;
}

static const web_route_t s_routes[] = {
    {"/api/ota", HTTP_POST, h_ota, WEB_ROLE_ADMIN},
};

esp_err_t web_ota_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
