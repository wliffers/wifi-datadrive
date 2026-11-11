// Wi-Fi for WiFi DataDrive.
//
// Behaviour:
//  - Station mode with credentials from NVS (set via web UI) or, as a fallback,
//    from firmware/pd_secrets.h (optional, git-ignored).
//  - If there are no credentials or no connection after PD_WIFI_AP_FALLBACK_S seconds,
//    a WPA2 access point "DataDrive-XXXX" is started for setup. Its password is
//    generated once per device, stored in NVS and shown on the LCD.
//  - Once the station is connected, the access point is switched off again after a delay.
//  - mDNS: http://<hostname>.local
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool sta_configured;        // credentials available
    bool sta_connected;         // got an IP address
    char sta_ssid[33];
    char sta_ip[16];
    int8_t sta_rssi;
    bool ap_active;
    char ap_ssid[33];
    char ap_pass[17];
    char ap_ip[16];
    uint8_t ap_clients;
    char hostname[32];
} pd_wifi_status_t;

/** Start Wi-Fi. Requires nvs_flash_init() to have been called. */
esp_err_t pd_wifi_start(void);

void pd_wifi_get_status(pd_wifi_status_t *out);

/**
 * Store new station credentials in NVS and reconnect.
 * ssid: 1..32 bytes, pass: empty (open network) or 8..63 bytes.
 */
esp_err_t pd_wifi_set_credentials(const char *ssid, const char *pass);

#ifdef __cplusplus
}
#endif
