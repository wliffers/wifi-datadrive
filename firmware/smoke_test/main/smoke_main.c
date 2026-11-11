// Phase 0 hardware smoke test for the LILYGO T-Dongle-S3 (LCD).
//
// Checks: LCD (colour bars + text), RGB LED, BOOT button, real flash size,
// partition table, Wi-Fi station / setup access point.
// Expected serial output (COM port of the USB-Serial/JTAG, 115200):
//   "SMOKE: flash size 16777216 bytes", "SMOKE: LCD ok", Wi-Fi state every 5 s.
#include <stdio.h>
#include <string.h>
#include "board.h"
#include "display.h"
#include "esp_chip_info.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "pd_wifi.h"

static const char *TAG = "SMOKE";

static void print_hardware_info(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    ESP_LOGI(TAG, "chip ESP32-S3 rev v%d.%d, %d cores, features 0x%lx",
             chip.revision / 100, chip.revision % 100, chip.cores, (unsigned long)chip.features);
    ESP_LOGI(TAG, "flash size %lu bytes", (unsigned long)board_flash_size());
    ESP_LOGI(TAG, "MAC %02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "free heap %lu bytes (internal)", (unsigned long)esp_get_free_heap_size());

    esp_partition_iterator_t it = esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL);
    for (; it; it = esp_partition_next(it)) {
        const esp_partition_t *p = esp_partition_get(it);
        ESP_LOGI(TAG, "partition %-8s type %d sub 0x%02x @0x%06lx size 0x%06lx",
                 p->label, p->type, p->subtype, (unsigned long)p->address, (unsigned long)p->size);
    }
}

static void colour_test(void)
{
    const uint16_t colours[] = {DISPLAY_RED, DISPLAY_GREEN, DISPLAY_BLUE, DISPLAY_WHITE};
    const char *names[] = {"ROT", "GRUEN", "BLAU", "WEISS"};
    for (int i = 0; i < 4; i++) {
        display_fill(colours[i]);
        display_text(4, 4, names[i], DISPLAY_BLACK, colours[i], 2);
        display_flush();
        board_led_set(i == 0 ? 255 : 0, i == 1 ? 255 : 0, i == 2 ? 255 : 0, 4);
        vTaskDelay(pdMS_TO_TICKS(600));
    }
    board_led_set(0, 0, 0, 0);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(board_init());
    print_hardware_info();

    if (display_init() == ESP_OK) {
        colour_test();
        ESP_LOGI(TAG, "LCD ok (check colours: red, green, blue, white)");
    } else {
        ESP_LOGE(TAG, "LCD init FAILED");
    }

    ESP_ERROR_CHECK(pd_wifi_start());

    char line[40];
    int64_t last_log = 0;
    while (true) {
        pd_wifi_status_t w;
        pd_wifi_get_status(&w);
        const bool btn = board_button_pressed();

        display_fill(DISPLAY_BLACK);
        display_rect(0, 0, DISPLAY_W, 16, DISPLAY_BLUE);
        display_text(2, 1, "SMOKE TEST", DISPLAY_WHITE, DISPLAY_BLUE, 2);
        snprintf(line, sizeof(line), "Flash %lu MB", (unsigned long)(board_flash_size() >> 20));
        display_text(2, 20, line, DISPLAY_WHITE, DISPLAY_BLACK, 1);
        if (w.sta_connected) {
            display_text(2, 32, "WLAN verbunden", DISPLAY_GREEN, DISPLAY_BLACK, 1);
            display_text(2, 44, w.sta_ip, DISPLAY_YELLOW, DISPLAY_BLACK, 1);
            snprintf(line, sizeof(line), "RSSI %d dBm", w.sta_rssi);
            display_text(2, 56, line, DISPLAY_WHITE, DISPLAY_BLACK, 1);
        } else if (w.ap_active) {
            display_text(2, 32, w.ap_ssid, DISPLAY_ORANGE, DISPLAY_BLACK, 1);
            snprintf(line, sizeof(line), "PW %s", w.ap_pass);
            display_text(2, 44, line, DISPLAY_ORANGE, DISPLAY_BLACK, 1);
            display_text(2, 56, w.ap_ip, DISPLAY_YELLOW, DISPLAY_BLACK, 1);
        } else {
            display_text(2, 32, "WLAN verbinde...", DISPLAY_WHITE, DISPLAY_BLACK, 1);
        }
        display_text(2, 70, btn ? "BOOT gedrückt" : "BOOT: drücken", btn ? DISPLAY_GREEN : DISPLAY_GREY,
                     DISPLAY_BLACK, 1);
        display_flush();
        board_led_set(0, btn ? 255 : 0, w.sta_connected ? 0 : 255, 2);

        const int64_t now = esp_timer_get_time() / 1000;
        if (now - last_log > 5000) {
            last_log = now;
            ESP_LOGI(TAG, "wifi sta=%s ip=%s ap=%s button=%d heap=%lu",
                     w.sta_connected ? "connected" : (w.sta_configured ? "connecting" : "unconfigured"),
                     w.sta_ip[0] ? w.sta_ip : "-", w.ap_active ? w.ap_ssid : "off", btn,
                     (unsigned long)esp_get_free_heap_size());
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}
