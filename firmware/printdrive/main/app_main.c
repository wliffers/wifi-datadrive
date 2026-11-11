// WiFi PrintDrive - main firmware for the LILYGO T-Dongle-S3 (LCD).
#include "applog.h"
#include "board.h"
#include "drive.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "pd_wifi.h"
#include "ui.h"
#include "web_server.h"

static const char *TAG = "main";

void app_main(void)
{
    applog_init();
    ESP_LOGI(TAG, "WiFi DataDrive %s", esp_app_get_description()->version);

    // NVS holds Wi-Fi credentials and drive flags. Only the NVS partition is erased
    // if its format is outdated; firmware and file storage are never touched here.
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS format changed - erasing NVS partition");
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(board_init());
    if (ui_init() != ESP_OK) {
        ESP_LOGE(TAG, "LCD init failed - continuing without display");
    }

    err = drive_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "storage/USB init failed: %s", esp_err_to_name(err));
        ui_show_fatal("Speicher/USB Init");
        return;   // no USB, no web: do not risk anything
    }

    err = pd_wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi start failed: %s", esp_err_to_name(err));
    } else if (web_server_start() != ESP_OK) {
        ESP_LOGE(TAG, "web server start failed");
    }

    ui_start();
    ESP_LOGI(TAG, "start-up complete");
}
