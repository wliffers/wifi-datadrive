// Wi-Fi for WiFi DataDrive.
//
// Behaviour:
//  - Station mode with up to PD_WIFI_MAX_NETWORKS stored networks (NVS, set via web UI). Credentials
//    of version 2 (single network) or from firmware/pd_secrets.h (optional, git-ignored) are
//    migrated into slot 0 on first start.
//  - When not connected, the device scans and tries the stored networks seen, strongest signal
//    first; networks not seen (hidden / out of range) follow in stored order.
//  - If there are no networks or no connection after PD_WIFI_AP_FALLBACK_S seconds,
//    a WPA2 access point "DataDrive-XXXX" is started for setup. Its password is
//    generated once per device, stored in NVS and shown on the LCD.
//  - Once the station is connected, the access point is switched off again after a delay.
//  - mDNS: http://<hostname>.local
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool sta_configured;        // at least one network stored
    bool sta_connected;         // got an IP address
    char sta_ssid[33];          // network currently connected or being tried
    char sta_ip[16];
    int8_t sta_rssi;
    bool ap_active;
    char ap_ssid[33];
    char ap_pass[17];
    char ap_ip[16];
    uint8_t ap_clients;
    char hostname[32];
} pd_wifi_status_t;

#define PD_WIFI_MAX_NETWORKS 5
#define PD_WIFI_ERR_LIMIT    ESP_ERR_NO_MEM   // pd_wifi_add(): 5 networks already stored

typedef struct {
    char ssid[33];
    bool active;   // currently connected (has an IP)
} pd_wifi_net_t;

/** Start Wi-Fi. Requires nvs_flash_init() to have been called. */
esp_err_t pd_wifi_start(void);

void pd_wifi_get_status(pd_wifi_status_t *out);

/** Stored networks (never passwords). Returns the number written to out (at most max). */
size_t pd_wifi_list(pd_wifi_net_t *out, size_t max);

/**
 * Add a network or replace the password of a stored one, then reconnect, preferring it once.
 * ssid: 1..32 bytes, pass: empty (open network) or 8..63 bytes.
 * ESP_ERR_INVALID_ARG on bad lengths, PD_WIFI_ERR_LIMIT if full.
 */
esp_err_t pd_wifi_add(const char *ssid, const char *pass);

/** Remove a stored network. ESP_ERR_NOT_FOUND if unknown. Removing the active one disconnects. */
esp_err_t pd_wifi_remove(const char *ssid);

typedef struct {
    char ssid[33];
    int8_t rssi;
    bool secure;   // authmode != open
    bool saved;    // already stored
} pd_wifi_scan_entry_t;

#define PD_WIFI_SCAN_MAX   20
#define PD_WIFI_SCAN_BUSY  (-1)   // a scan or connection attempt is in progress, try again later
#define PD_WIFI_SCAN_FAIL  (-2)

/**
 * Scan for networks in range (blocks 1-3 s). Duplicate SSIDs are merged (strongest kept), hidden
 * ones dropped, sorted by RSSI descending, at most max entries. Works in setup mode and while
 * connected. Returns the number of entries, PD_WIFI_SCAN_BUSY or PD_WIFI_SCAN_FAIL.
 */
int pd_wifi_scan(pd_wifi_scan_entry_t *out, size_t max);

/** Wrapper for pd_wifi_add() (version 2 name). */
esp_err_t pd_wifi_set_credentials(const char *ssid, const char *pass);

#ifdef __cplusplus
}
#endif
