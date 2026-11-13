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
//  - While the access point is on, a captive-portal DNS responder (pd_dns.c, Kconfig
//    PD_WIFI_CAPTIVE_PORTAL) resolves every name to the AP address, and DHCP option 114
//    announces http://<ap-ip>/. The HTTP redirect itself is done by the web server.
//  - Once the station is connected, the access point is switched off again after a delay.
//  - mDNS: http://<hostname>.local
//  - Network settings (NVS): DHCP (default) or a static IPv4 address for the station interface,
//    and the hostname (default CONFIG_PD_WIFI_HOSTNAME; DHCP option 12, mDNS, status). One
//    setting for all stored networks. The setup access point always keeps its own fixed address.
//    pd_wifi_reset_network() (button held 10 s) goes back to DHCP and the default hostname.
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
    char hostname[32];          // hostname in effect (DHCP, mDNS)
    bool sta_dhcp;              // station address from DHCP (false: static setting in effect)
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

// ---- network settings: DHCP or static IPv4, hostname -------------------------------------

#define PD_WIFI_HOSTNAME_MAX 31

/** IPv4 addresses as dotted quads ("" = not set / unknown). */
typedef struct {
    char ip[16];
    char mask[16];
    char gateway[16];
    char dns[16];
} pd_wifi_ipv4_t;

typedef struct {
    bool dhcp;
    pd_wifi_ipv4_t addr;                    // static setting (kept while DHCP is selected)
    char hostname[PD_WIFI_HOSTNAME_MAX + 1];
} pd_wifi_netcfg_t;

/** Which field pd_wifi_set_netcfg() refused. */
typedef enum {
    PD_WIFI_NET_OK = 0,
    PD_WIFI_NET_BAD_IP,         // not a.b.c.d, not unicast, network/broadcast address, setup-AP address
    PD_WIFI_NET_BAD_MASK,       // not contiguous, or prefix outside /8../30
    PD_WIFI_NET_BAD_GATEWAY,    // not in the subnet of ip, equal to ip, network/broadcast address
    PD_WIFI_NET_BAD_DNS,        // not a unicast a.b.c.d
    PD_WIFI_NET_BAD_HOSTNAME,   // see pd_wifi_hostname_valid()
} pd_wifi_net_check_t;

/** 1..31 characters a-z, 0-9 and '-', not starting or ending with '-'. */
bool pd_wifi_hostname_valid(const char *hostname);

/** Stored network settings (static addresses "" if never set). */
void pd_wifi_get_netcfg(pd_wifi_netcfg_t *out);

/** Addresses the station interface has right now (from DHCP or static; DNS = first DNS server).
 *  Returns true if the station is connected; otherwise all fields are "". */
bool pd_wifi_get_current_ipv4(pd_wifi_ipv4_t *out);

/**
 * Validate and store new network settings; they are applied about 1-2 s later by the Wi-Fi task
 * (so that an HTTP answer still reaches the client before the address changes).
 * cfg->dhcp true: the stored static addresses are kept, cfg->addr is ignored.
 * cfg->dhcp false: ip, mask, gateway required; dns "" = use the gateway.
 * cfg->hostname "" = keep the current one.
 * ESP_ERR_INVALID_ARG with *bad (may be NULL) naming the field, or an NVS error.
 */
esp_err_t pd_wifi_set_netcfg(const pd_wifi_netcfg_t *cfg, pd_wifi_net_check_t *bad);

/** Back to DHCP and the default hostname (NVS keys erased), applied at once. For the 10-s button
 *  reset; may be called from any task. ESP_ERR_INVALID_STATE before pd_wifi_start(). */
esp_err_t pd_wifi_reset_network(void);

#define PD_WIFI_CHG_HOSTNAME 0x01   // the hostname in effect changed
#define PD_WIFI_CHG_ADDRESS  0x02   // the station has a different IPv4 address after a settings change

/**
 * Called once a changed setting is in effect and the station has its address (from the Wi-Fi
 * task: keep it short, do not block). Used to renew the HTTPS certificate,
 * which names the hostname and the IP address. One callback; NULL removes it.
 */
typedef void (*pd_wifi_netcfg_cb_t)(unsigned changes);
void pd_wifi_set_netcfg_callback(pd_wifi_netcfg_cb_t cb);

#ifdef __cplusplus
}
#endif
