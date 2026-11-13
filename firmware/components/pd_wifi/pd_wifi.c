#include "pd_wifi.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mdns.h"
#include "nvs.h"
#include "pd_dns.h"
#include "sdkconfig.h"

// Optional compile-time default credentials (file is git-ignored, see pd_secrets.h.example).
#if __has_include("pd_secrets.h")
#include "pd_secrets.h"
#endif
#ifndef PD_WIFI_SSID
#define PD_WIFI_SSID ""
#endif
#ifndef PD_WIFI_PASS
#define PD_WIFI_PASS ""
#endif

static const char *TAG = "pd_wifi";
static const char *NVS_NS = "pd_wifi";

#define AP_FALLBACK_MS   (CONFIG_PD_WIFI_AP_FALLBACK_S * 1000LL)
#define AP_LINGER_MS     (CONFIG_PD_WIFI_AP_LINGER_S * 1000LL)
#define RETRY_MIN_MS     2000
#define RETRY_MAX_MS     30000

static SemaphoreHandle_t s_lock;
static pd_wifi_status_t s_st;
static esp_netif_t *s_sta_netif;
static esp_netif_t *s_ap_netif;
static esp_ip4_addr_t s_ap_ip = {.addr = ESP_IP4TOADDR(192, 168, 4, 1)};   // read from the netif at start
static char s_ap_ip_str[16] = "192.168.4.1";
#if CONFIG_PD_WIFI_CAPTIVE_DHCP_URI
static char s_portal_uri[24];   // the DHCP server keeps this pointer (option 114): must stay valid
#endif

// Stored networks (NVS keys s0..s4 = SSID, p0..p4 = password). Guarded by s_lock; s_cfg_lock
// serialises add/remove so that the NVS write and the RAM update stay consistent.
typedef struct {
    bool used;
    char ssid[33];
    char pass[64];
} net_t;
static net_t s_nets[PD_WIFI_MAX_NETWORKS];
static SemaphoreHandle_t s_cfg_lock;
static int s_cur_slot = -1;                       // network connected / being tried
static int s_prefer_slot = -1;                    // try this one first in the next round
static uint8_t s_order[PD_WIFI_MAX_NETWORKS];     // candidates of the current round
static int s_order_n, s_order_idx;
#define SCAN_MAX_RECORDS 24
static wifi_ap_record_t s_scan_recs[SCAN_MAX_RECORDS];

// State owned by the wifi task (and the event handler under s_lock).
static bool s_connecting;
static int64_t s_last_ip_change_ms;   // time of last connect or disconnect
static int64_t s_next_retry_ms;
static int s_retry_delay_ms = RETRY_MIN_MS;
static volatile bool s_reconfigure;
static SemaphoreHandle_t s_scan_lock;   // one scan at a time (round scan and web scan)
static bool s_web_scan;                 // web scan running; guarded by s_lock, blocks new connect attempts

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void lock(void)   { xSemaphoreTake(s_lock, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_lock); }

static void nvs_get_str_or(nvs_handle_t h, const char *key, char *out, size_t len, const char *fallback)
{
    size_t n = len;
    if (h == 0 || nvs_get_str(h, key, out, &n) != ESP_OK) {
        strlcpy(out, fallback, len);
    }
}

// ---- network settings: DHCP or static IPv4, hostname ----------------------------------------
// ONE setting for the station interface, whatever stored network it connects to (not per
// network): a static address fits one LAN only, so with several networks DHCP is the right choice.
// The setup access point is a separate interface with its own fixed address and DHCP server and
// is never affected. No reachability test (the device cannot verify it): strict validation, the
// usual fallback to the setup access point when no network can be joined at all, and the 10-s
// button reset (pd_wifi_reset_network()) as the way back.
// Addresses in host byte order (a.b.c.d = a << 24 | b << 16 | c << 8 | d), 0 = not set.
// NVS keys (namespace pd_wifi): "dhcp" u8, "ip" / "mask" / "gateway" / "dns" u32, "hostname" str.

#define NET_APPLY_DELAY_MS 1000   // after the HTTP answer has left

typedef struct {
    bool dhcp;
    uint32_t ip, mask, gw, dns;
} netaddr_t;

static netaddr_t s_net = {.dhcp = true};             // stored setting (s_lock; writers also hold s_cfg_lock)
static char s_cfg_host[PD_WIFI_HOSTNAME_MAX + 1];    // stored hostname; s_st.hostname = the one in effect
static volatile bool s_net_apply;                    // stored setting not applied yet ...
static int64_t s_net_apply_at_ms;                    // ... apply from this time on (s_lock)
static bool s_net_addr_dirty;                        // address part changed since the last apply (s_lock)
static volatile uint32_t s_dns_applied;              // static DNS server in effect, 0 with DHCP
static uint8_t s_net_cb_pending;                     // PD_WIFI_CHG_* waiting for the station address (s_lock)
static bool s_ip_event, s_ip_event_changed;          // GOT_IP seen (event task -> wifi task; s_lock)
static pd_wifi_netcfg_cb_t s_net_cb;
static bool s_mdns_ok;

bool pd_wifi_hostname_valid(const char *h)
{
    const size_t n = h ? strlen(h) : 0;
    if (n == 0 || n > PD_WIFI_HOSTNAME_MAX || h[0] == '-' || h[n - 1] == '-') {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        const char c = h[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) {
            return false;
        }
    }
    return true;
}

static const char *default_hostname(void)
{
    return pd_wifi_hostname_valid(CONFIG_PD_WIFI_HOSTNAME) ? CONFIG_PD_WIFI_HOSTNAME : "datadrive";
}

/** Strict "a.b.c.d": decimal, 1-3 digits per part, no leading zeros (no octal ambiguity). */
static bool parse_ip4(const char *s, uint32_t *out)
{
    if (!s) {
        return false;
    }
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) {
        const char *start = s;
        unsigned part = 0;
        int digits = 0;
        while (*s >= '0' && *s <= '9') {
            if (++digits > 3) {
                return false;
            }
            part = part * 10 + (unsigned)(*s++ - '0');
        }
        if (digits == 0 || part > 255 || (digits > 1 && *start == '0') || *s != (i < 3 ? '.' : '\0')) {
            return false;
        }
        if (i < 3) {
            s++;
        }
        v = (v << 8) | part;
    }
    *out = v;
    return true;
}

static void ip4_str(uint32_t h, char out[16])
{
    if (h == 0) {
        out[0] = '\0';
        return;
    }
    snprintf(out, 16, "%u.%u.%u.%u", (unsigned)(h >> 24), (unsigned)((h >> 16) & 255u), (unsigned)((h >> 8) & 255u),
             (unsigned)(h & 255u));
}

static esp_ip4_addr_t ip4_net(uint32_t h)
{
    const esp_ip4_addr_t a = {.addr = esp_netif_htonl(h)};
    return a;
}

static uint32_t ip4_host(esp_ip4_addr_t a)
{
    return esp_netif_htonl(a.addr);
}

static bool ip4_unicast(uint32_t a)
{
    const uint32_t first = a >> 24;
    return first != 0 && first != 127 && first < 224;
}

static pd_wifi_net_check_t check_static(uint32_t ip, uint32_t mask, uint32_t gw, uint32_t dns)
{
    const uint32_t host = ~mask;
    if ((host & (host + 1)) != 0 || mask < 0xFF000000u || mask > 0xFFFFFFFCu) {
        return PD_WIFI_NET_BAD_MASK;          // not contiguous, or not /8../30
    }
    if (!ip4_unicast(ip) || (ip & host) == 0 || (ip & host) == host || ip == ip4_host(s_ap_ip)) {
        return PD_WIFI_NET_BAD_IP;
    }
    if (!ip4_unicast(gw) || gw == ip || (gw & mask) != (ip & mask) || (gw & host) == 0 || (gw & host) == host) {
        return PD_WIFI_NET_BAD_GATEWAY;
    }
    if (!ip4_unicast(dns)) {
        return PD_WIFI_NET_BAD_DNS;
    }
    return PD_WIFI_NET_OK;
}

static void set_static_dns(void)
{
    const uint32_t d = s_dns_applied;
    if (d == 0 || !s_sta_netif) {
        return;
    }
    esp_netif_dns_info_t dns = {0};
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    dns.ip.u_addr.ip4 = ip4_net(d);
    const esp_err_t err = esp_netif_set_dns_info(s_sta_netif, ESP_NETIF_DNS_MAIN, &dns);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "DNS server not set: %s", esp_err_to_name(err));
    }
}

/** DHCP client on, or off with the static address (Wi-Fi task, or pd_wifi_start() before the
 *  first connect). With the station connected the change is immediate: a static address posts
 *  IP_EVENT_STA_GOT_IP at once, the DHCP client posts it with the lease. */
static void apply_ip_mode(const netaddr_t *n)
{
    esp_err_t err;
    if (n->dhcp) {
        s_dns_applied = 0;
        err = esp_netif_dhcpc_start(s_sta_netif);
        if (err != ESP_OK && err != ESP_ERR_ESP_NETIF_DHCP_ALREADY_STARTED) {
            ESP_LOGE(TAG, "DHCP client start failed: %s", esp_err_to_name(err));
        }
    } else {
        err = esp_netif_dhcpc_stop(s_sta_netif);
        if (err != ESP_OK && err != ESP_ERR_ESP_NETIF_DHCP_ALREADY_STOPPED) {
            ESP_LOGE(TAG, "DHCP client stop failed: %s", esp_err_to_name(err));
        }
        s_dns_applied = n->dns;             // before the GOT_IP event below re-applies it
        const esp_netif_ip_info_t info = {.ip = ip4_net(n->ip), .netmask = ip4_net(n->mask), .gw = ip4_net(n->gw)};
        err = esp_netif_set_ip_info(s_sta_netif, &info);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "static address not set: %s", esp_err_to_name(err));
        }
        set_static_dns();                   // after esp_netif_set_ip_info(), which clears the DNS servers
    }
    lock();
    s_st.sta_dhcp = n->dhcp;
    unlock();
}

/** Deliver pending PD_WIFI_CHG_* (call without s_lock). ip_changed false drops CHG_ADDRESS. */
static void fire_net_cb(bool ip_changed)
{
    lock();
    unsigned bits = s_net_cb_pending;
    s_net_cb_pending = 0;
    const pd_wifi_netcfg_cb_t cb = s_net_cb;
    unlock();
    if (!ip_changed) {
        bits &= ~(unsigned)PD_WIFI_CHG_ADDRESS;
    }
    if (bits && cb) {
        cb(bits);
    }
}

/** Apply the stored setting (Wi-Fi task). */
static void apply_netcfg(void)
{
    char host[PD_WIFI_HOSTNAME_MAX + 1];
    lock();
    s_net_apply = false;
    const netaddr_t n = s_net;
    const bool addr = s_net_addr_dirty;
    s_net_addr_dirty = false;
    strlcpy(host, s_cfg_host, sizeof(host));
    const bool host_changed = strcmp(host, s_st.hostname) != 0;
    if (host_changed) {
        strlcpy(s_st.hostname, host, sizeof(s_st.hostname));
    }
    s_net_cb_pending |= (host_changed ? PD_WIFI_CHG_HOSTNAME : 0) | (addr ? PD_WIFI_CHG_ADDRESS : 0);
    const bool connected = s_st.sta_connected;
    unlock();

    if (host_changed) {
        esp_netif_set_hostname(s_sta_netif, host);   // DHCP option 12 from the next DHCP message on
        if (s_mdns_ok) {
            const esp_err_t err = mdns_hostname_set(host);   // re-announces, the service follows the name
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "mDNS hostname not changed: %s", esp_err_to_name(err));
            }
        }
        ESP_LOGI(TAG, "hostname now '%s' (http://%s.local)", host, host);
    }
    if (addr) {
        if (n.dhcp) {
            ESP_LOGI(TAG, "station address: DHCP");
        } else {
            char a[16], m[16], g[16], d[16];
            ip4_str(n.ip, a);
            ip4_str(n.mask, m);
            ip4_str(n.gw, g);
            ip4_str(n.dns, d);
            ESP_LOGI(TAG, "station address: static %s mask %s gateway %s DNS %s", a, m, g, d);
        }
        apply_ip_mode(&n);
    } else if (connected) {
        fire_net_cb(false);                 // nothing to wait for (hostname only)
    }
}

static void load_config(void)
{
    nvs_handle_t h = 0;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        h = 0;
    }

    // One-time migration of the single credentials of version 2 (or the compile-time default) to slot 0.
    uint8_t migrated = 0;
    if (h) {
        nvs_get_u8(h, "mig", &migrated);
    }
    if (!migrated && h) {
        char old_ssid[33], old_pass[65];
        nvs_get_str_or(h, "ssid", old_ssid, sizeof(old_ssid), PD_WIFI_SSID);
        nvs_get_str_or(h, "pass", old_pass, sizeof(old_pass), PD_WIFI_PASS);
        const size_t ls = strlen(old_ssid), lp = strlen(old_pass);
        if (ls >= 1 && (lp == 0 || (lp >= 8 && lp <= 63))) {
            if (nvs_set_str(h, "s0", old_ssid) == ESP_OK && nvs_set_str(h, "p0", old_pass) == ESP_OK &&
                nvs_set_u8(h, "mig", 1) == ESP_OK && nvs_commit(h) == ESP_OK) {
                nvs_erase_key(h, "ssid");
                nvs_erase_key(h, "pass");
                nvs_commit(h);
                ESP_LOGI(TAG, "migrated stored network '%s' to slot 0", old_ssid);
            }
        } else {
            nvs_set_u8(h, "mig", 1);
            nvs_commit(h);
        }
    }

    memset(s_nets, 0, sizeof(s_nets));
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        char ks[3] = {'s', (char)('0' + i), 0}, kp[3] = {'p', (char)('0' + i), 0};
        size_t n = sizeof(s_nets[i].ssid);
        if (h && nvs_get_str(h, ks, s_nets[i].ssid, &n) == ESP_OK && s_nets[i].ssid[0]) {
            s_nets[i].used = true;
            n = sizeof(s_nets[i].pass);
            if (nvs_get_str(h, kp, s_nets[i].pass, &n) != ESP_OK) {
                s_nets[i].pass[0] = '\0';
            }
        } else {
            s_nets[i].ssid[0] = '\0';
        }
        s_st.sta_configured |= s_nets[i].used;
    }
    s_st.sta_ssid[0] = '\0';

    // Per-device access point password, generated once.
    nvs_get_str_or(h, "ap_pass", s_st.ap_pass, sizeof(s_st.ap_pass), "");
    if (strlen(s_st.ap_pass) < 8) {
        static const char alphabet[] = "abcdefghjkmnpqrstuvwxyz23456789";   // no 0/O, 1/l/i
        for (int i = 0; i < 10; i++) {
            s_st.ap_pass[i] = alphabet[esp_random() % (sizeof(alphabet) - 1)];
        }
        s_st.ap_pass[10] = '\0';
        if (h) {
            nvs_set_str(h, "ap_pass", s_st.ap_pass);
            nvs_commit(h);
        }
    }

    // Network settings (see "network settings" above). A stored static setting that does not
    // validate (any more) falls back to DHCP.
    uint8_t dhcp = 1;
    netaddr_t n = {0};
    if (h) {
        nvs_get_u8(h, "dhcp", &dhcp);
        nvs_get_u32(h, "ip", &n.ip);
        nvs_get_u32(h, "mask", &n.mask);
        nvs_get_u32(h, "gateway", &n.gw);
        nvs_get_u32(h, "dns", &n.dns);
    }
    n.dhcp = dhcp != 0;
    if (!n.dhcp && check_static(n.ip, n.mask, n.gw, n.dns) != PD_WIFI_NET_OK) {
        ESP_LOGW(TAG, "stored static address not valid - using DHCP");
        n.dhcp = true;
    }
    s_net = n;
    nvs_get_str_or(h, "hostname", s_cfg_host, sizeof(s_cfg_host), "");
    if (!pd_wifi_hostname_valid(s_cfg_host)) {
        strlcpy(s_cfg_host, default_hostname(), sizeof(s_cfg_host));
    }
    if (h) {
        nvs_close(h);
    }

    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(s_st.ap_ssid, sizeof(s_st.ap_ssid), "DataDrive-%02X%02X", mac[4], mac[5]);
    strlcpy(s_st.hostname, s_cfg_host, sizeof(s_st.hostname));
    s_st.sta_dhcp = s_net.dhcp;
}

static void slot_key(char *out, char kind, int slot)
{
    out[0] = kind;
    out[1] = (char)('0' + slot);
    out[2] = '\0';
}

static int count_nets_locked(void)
{
    int n = 0;
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        n += s_nets[i].used;
    }
    return n;
}

static void apply_sta_config(const char *ssid, const char *pass)
{
    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, pass, sizeof(cfg.sta.password));
    cfg.sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
}

/** Start a new round: scan (if it helps), order the stored networks by signal. Returns false if
 *  a reconfiguration came in meanwhile (the round is then dropped). */
static bool build_round(void)
{
    char ssids[PD_WIFI_MAX_NETWORKS][33];
    bool used[PD_WIFI_MAX_NETWORKS];
    int rssi[PD_WIFI_MAX_NETWORKS];
    int count = 0;
    lock();
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        used[i] = s_nets[i].used;
        strlcpy(ssids[i], s_nets[i].ssid, sizeof(ssids[i]));
        rssi[i] = -1000;   // not seen
        count += used[i];
    }
    unlock();

    if (count > 1 && xSemaphoreTake(s_scan_lock, pdMS_TO_TICKS(10000)) == pdTRUE) {
        // Short active scan: little time away from the channel of the setup access point.
        wifi_scan_config_t sc = {0};
        sc.show_hidden = false;
        sc.scan_type = WIFI_SCAN_TYPE_ACTIVE;
        sc.scan_time.active.min = 60;
        sc.scan_time.active.max = 110;
        const esp_err_t se = esp_wifi_scan_start(&sc, true);
        if (se == ESP_OK) {
            uint16_t n = SCAN_MAX_RECORDS;
            if (esp_wifi_scan_get_ap_records(&n, s_scan_recs) == ESP_OK) {
                for (int k = 0; k < n; k++) {
                    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
                        if (used[i] && strcmp((const char *)s_scan_recs[k].ssid, ssids[i]) == 0 &&
                            s_scan_recs[k].rssi > rssi[i]) {
                            rssi[i] = s_scan_recs[k].rssi;
                        }
                    }
                }
            }
        } else {
            ESP_LOGW(TAG, "scan failed - trying stored networks in order");
        }
        xSemaphoreGive(s_scan_lock);
    }

    // Seen networks by descending RSSI, then the unseen ones in stored order (hidden SSID / out of range).
    uint8_t ord[PD_WIFI_MAX_NETWORKS];
    int n = 0;
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        if (!used[i]) {
            continue;
        }
        int j = n++;
        while (j > 0 && rssi[ord[j - 1]] < rssi[i]) {
            ord[j] = ord[j - 1];
            j--;
        }
        ord[j] = (uint8_t)i;
    }

    lock();
    if (s_reconfigure) {
        unlock();
        return false;
    }
    int m = 0;
    if (s_prefer_slot >= 0 && s_nets[s_prefer_slot].used) {
        s_order[m++] = (uint8_t)s_prefer_slot;
    }
    for (int k = 0; k < n; k++) {
        if (ord[k] != s_prefer_slot) {
            s_order[m++] = ord[k];
        }
    }
    s_prefer_slot = -1;
    s_order_n = m;
    s_order_idx = 0;
    unlock();
    return true;
}

/** Next stored network to try (copies SSID/password), or -1 if there is none. */
static int next_candidate(char *ssid, char *pass)
{
    lock();
    const bool fresh = s_order_idx >= s_order_n;
    unlock();
    if (fresh && !build_round()) {
        return -1;
    }
    int slot = -1;
    lock();
    while (s_order_idx < s_order_n) {
        const int c = s_order[s_order_idx++];
        if (s_nets[c].used) {
            slot = c;
            strlcpy(ssid, s_nets[c].ssid, 33);
            strlcpy(pass, s_nets[c].pass, 64);
            s_cur_slot = c;
            strlcpy(s_st.sta_ssid, ssid, sizeof(s_st.sta_ssid));
            break;
        }
    }
    unlock();
    return slot;
}

static void set_ap(bool on)
{
    if (on == s_st.ap_active) {
        return;
    }
    if (on) {
        wifi_config_t ap = {0};
        strlcpy((char *)ap.ap.ssid, s_st.ap_ssid, sizeof(ap.ap.ssid));
        ap.ap.ssid_len = strlen(s_st.ap_ssid);
        strlcpy((char *)ap.ap.password, s_st.ap_pass, sizeof(ap.ap.password));
        ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
        ap.ap.max_connection = 3;
        ap.ap.channel = 1;
        ap.ap.pmf_cfg.capable = true;
        esp_wifi_set_mode(WIFI_MODE_APSTA);
        esp_wifi_set_config(WIFI_IF_AP, &ap);
        // The password is only shown on the LCD (the log is readable via the web UI).
        ESP_LOGW(TAG, "setup access point on: SSID '%s' (password on the LCD), http://%s", s_st.ap_ssid, s_ap_ip_str);
#if CONFIG_PD_WIFI_CAPTIVE_PORTAL
        // Captive portal: every name resolves to the access point while it is on.
        const esp_err_t dns_err = pd_dns_start(s_ap_ip.addr);
        if (dns_err != ESP_OK) {
            ESP_LOGW(TAG, "captive portal DNS not started: %s", esp_err_to_name(dns_err));
        }
#endif
    } else {
#if CONFIG_PD_WIFI_CAPTIVE_PORTAL
        pd_dns_stop();
#endif
        esp_wifi_set_mode(WIFI_MODE_STA);
        ESP_LOGI(TAG, "setup access point off");
    }
    lock();
    s_st.ap_active = on;
    s_st.ap_clients = 0;
    strlcpy(s_st.ap_ip, on ? s_ap_ip_str : "", sizeof(s_st.ap_ip));
    unlock();
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = data;
        lock();
        const bool was_connected = s_st.sta_connected;
        s_st.sta_connected = false;
        s_st.sta_ip[0] = '\0';
        s_connecting = false;
        if (was_connected) {
            s_last_ip_change_ms = now_ms();
            s_retry_delay_ms = RETRY_MIN_MS;
            s_order_n = s_order_idx = 0;   // connection lost: rescan and start a fresh round
        }
        if (s_order_idx < s_order_n) {
            s_next_retry_ms = now_ms() + 300;   // next stored network of this round, no backoff
        } else {
            s_next_retry_ms = now_ms() + s_retry_delay_ms;
            s_retry_delay_ms = s_retry_delay_ms * 2 > RETRY_MAX_MS ? RETRY_MAX_MS : s_retry_delay_ms * 2;
        }
        unlock();
        ESP_LOGW(TAG, "station disconnected (reason %d)", d ? d->reason : -1);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        lock();
        s_st.sta_connected = false;
        s_st.sta_ip[0] = '\0';
        s_last_ip_change_ms = now_ms();
        unlock();
        ESP_LOGW(TAG, "station lost its IP address - reconnecting");
        esp_wifi_disconnect();   // DISCONNECTED event schedules the reconnect
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        lock();
        s_st.sta_connected = true;
        s_connecting = false;
        s_retry_delay_ms = RETRY_MIN_MS;
        s_last_ip_change_ms = now_ms();
        snprintf(s_st.sta_ip, sizeof(s_st.sta_ip), IPSTR, IP2STR(&e->ip_info.ip));
        // DNS and the settings callback are handled by the wifi task (small event task stack).
        s_ip_event = true;
        s_ip_event_changed = s_ip_event_changed || e->ip_changed;
        unlock();
        ESP_LOGI(TAG, "connected to '%s', IP %s", s_st.sta_ssid, s_st.sta_ip);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        lock();
        s_st.ap_clients++;
        unlock();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        lock();
        if (s_st.ap_clients) {
            s_st.ap_clients--;
        }
        unlock();
    }
}

// ---- switching away from a weak network ---------------------------------------------
// The station stays associated as long as the access point answers at all, even at -94 dBm with a
// few kB/s. If the signal stays weak, look whether another stored network (or another access point
// of the same network) is clearly stronger and reconnect to it.
#define ROAM_RSSI        (-80)      // weaker than this counts as weak
#define ROAM_GAIN        12         // the alternative must be this many dB stronger
#define ROAM_AFTER_MS    30000      // weak for this long before the first look
#define ROAM_PERIOD_MS   120000     // at most one look (scan) per period

static int64_t s_weak_since_ms, s_next_roam_ms;

static void roam_check(int rssi_now)
{
    const int64_t t = now_ms();
    if (rssi_now > ROAM_RSSI) {
        s_weak_since_ms = 0;
        return;
    }
    if (s_weak_since_ms == 0) {
        s_weak_since_ms = t;
        return;
    }
    if (t - s_weak_since_ms < ROAM_AFTER_MS || t < s_next_roam_ms) {
        return;
    }
    s_next_roam_ms = t + ROAM_PERIOD_MS;

    char ssids[PD_WIFI_MAX_NETWORKS][33];
    bool used[PD_WIFI_MAX_NETWORKS];
    lock();
    const int cur = s_cur_slot;
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        used[i] = s_nets[i].used;
        strlcpy(ssids[i], s_nets[i].ssid, sizeof(ssids[i]));
    }
    unlock();
    if (xSemaphoreTake(s_scan_lock, 0) != pdTRUE) {
        return;   // a web scan is running: next period
    }
    int best = -1, best_rssi = -1000;
    wifi_scan_config_t sc = {0};
    sc.scan_type = WIFI_SCAN_TYPE_ACTIVE;
    sc.scan_time.active.min = 60;
    sc.scan_time.active.max = 110;
    if (esp_wifi_scan_start(&sc, true) == ESP_OK) {
        uint16_t n = SCAN_MAX_RECORDS;
        if (esp_wifi_scan_get_ap_records(&n, s_scan_recs) == ESP_OK) {
            for (int k = 0; k < n; k++) {
                for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
                    if (used[i] && strcmp((const char *)s_scan_recs[k].ssid, ssids[i]) == 0 &&
                        s_scan_recs[k].rssi > best_rssi) {
                        best = i;
                        best_rssi = s_scan_recs[k].rssi;
                    }
                }
            }
        }
    }
    xSemaphoreGive(s_scan_lock);
    if (best < 0 || best_rssi < rssi_now + ROAM_GAIN) {
        return;
    }
    ESP_LOGW(TAG, "weak signal (%d dBm): switching to '%s' (%d dBm)%s", rssi_now, ssids[best], best_rssi,
             best == cur ? ", other access point" : "");
    lock();
    s_prefer_slot = best;
    s_reconfigure = true;   // wifi_task disconnects and starts a new round, preferred network first
    unlock();
    s_weak_since_ms = 0;
}

static void wifi_task(void *arg)
{
    while (true) {
        const int64_t t = now_ms();

        if (s_net_apply) {
            lock();
            const bool due = t >= s_net_apply_at_ms;
            unlock();
            if (due) {
                apply_netcfg();
            }
        }
        lock();
        const bool ip_event = s_ip_event, ip_changed = s_ip_event_changed;
        s_ip_event = s_ip_event_changed = false;
        unlock();
        if (ip_event) {
            set_static_dns();               // static setting only (no-op with DHCP)
            fire_net_cb(ip_changed);        // a changed setting is in effect now (no-op if none pending)
        }

        if (s_reconfigure) {
            s_reconfigure = false;
            esp_wifi_disconnect();
            lock();
            s_order_n = s_order_idx = 0;   // new round (scan), preferred network first
            s_connecting = false;
            s_retry_delay_ms = RETRY_MIN_MS;
            s_next_retry_ms = t + 500;   // give the disconnect event time to arrive
            unlock();
        }

        lock();
        const bool connected = s_st.sta_connected;
        const bool configured = s_st.sta_configured;
        const bool connecting = s_connecting;
        const int64_t since_change = t - s_last_ip_change_ms;
        const bool retry_due = t >= s_next_retry_ms;
        unlock();

        if (!connected) {
            if (!configured || since_change > AP_FALLBACK_MS) {
                set_ap(true);
            }
            if (configured && connecting && retry_due) {
                // Safety net: associated but no IP (e.g. DHCP never answered) - start over.
                ESP_LOGW(TAG, "no IP address after 20 s - retrying");
                lock();
                s_connecting = false;
                unlock();
                esp_wifi_disconnect();
            } else if (configured && !connecting && retry_due) {
                lock();
                const bool web_scan = s_web_scan;
                if (!web_scan) {
                    s_connecting = true;
                    s_next_retry_ms = t + 20000;   // safety net if no event arrives
                }
                unlock();
                if (web_scan) {
                    vTaskDelay(pdMS_TO_TICKS(1000));   // a web scan is running: try again after it
                    continue;
                }
                char ssid[33], pass[64];
                const int slot = next_candidate(ssid, pass);
                if (slot < 0) {
                    lock();
                    s_connecting = false;
                    s_next_retry_ms = now_ms() + 1000;
                    unlock();
                } else {
                    ESP_LOGI(TAG, "connecting to '%s' ...", ssid);
                    apply_sta_config(ssid, pass);
                    memset(pass, 0, sizeof(pass));
                    lock();
                    s_next_retry_ms = now_ms() + 20000;   // safety net if no event arrives
                    unlock();
                    if (esp_wifi_connect() != ESP_OK) {
                        lock();
                        s_connecting = false;
                        s_next_retry_ms = now_ms() + RETRY_MAX_MS;
                        unlock();
                    }
                }
            }
        } else if (s_st.ap_active && since_change > AP_LINGER_MS) {
            set_ap(false);
        }

        if (connected) {
            wifi_ap_record_t ap;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
                lock();
                s_st.sta_rssi = ap.rssi;
                unlock();
                roam_check(ap.rssi);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void start_mdns(void)
{
    if (mdns_init() != ESP_OK) {
        ESP_LOGW(TAG, "mDNS init failed");
        return;
    }
    s_mdns_ok = true;
    mdns_hostname_set(s_st.hostname);
    mdns_instance_name_set("WiFi DataDrive");
    mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    ESP_LOGI(TAG, "mDNS: http://%s.local", s_st.hostname);
}

esp_err_t pd_wifi_start(void)
{
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_lock, ESP_ERR_NO_MEM, TAG, "mutex");
    s_cfg_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_cfg_lock, ESP_ERR_NO_MEM, TAG, "mutex");
    s_scan_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_scan_lock, ESP_ERR_NO_MEM, TAG, "mutex");
    load_config();

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    esp_err_t err = esp_event_loop_create_default();
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "event loop");
    s_sta_netif = esp_netif_create_default_wifi_sta();
    s_ap_netif = esp_netif_create_default_wifi_ap();
    esp_netif_set_hostname(s_sta_netif, s_st.hostname);
    if (!s_net.dhcp) {
        // Static setting: DHCP client off before the first connect; the address is taken over
        // with every association (esp_netif posts IP_EVENT_STA_GOT_IP itself).
        char a[16];
        ip4_str(s_net.ip, a);
        ESP_LOGI(TAG, "station address: static %s", a);
        apply_ip_mode(&s_net);
    }
    esp_netif_ip_info_t ap_info;
    if (esp_netif_get_ip_info(s_ap_netif, &ap_info) == ESP_OK && ap_info.ip.addr != 0) {
        s_ap_ip = ap_info.ip;
        snprintf(s_ap_ip_str, sizeof(s_ap_ip_str), IPSTR, IP2STR(&s_ap_ip));
    }
#if CONFIG_PD_WIFI_CAPTIVE_DHCP_URI
    // DHCP option 114 (RFC 8910). Must be set while the DHCP server is not running (it starts
    // with the access point). Clients that ignore it still find the portal via DNS + HTTP.
    snprintf(s_portal_uri, sizeof(s_portal_uri), "http://%s/", s_ap_ip_str);
    err = esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_CAPTIVEPORTAL_URI, s_portal_uri,
                                 strlen(s_portal_uri));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "DHCP captive portal URI not set: %s", esp_err_to_name(err));
    }
#endif

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init), TAG, "wifi init");
    ESP_RETURN_ON_ERROR(esp_wifi_set_storage(WIFI_STORAGE_RAM), TAG, "storage");
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_LOST_IP, on_event, NULL));

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "mode");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start");
    esp_wifi_set_ps(WIFI_PS_NONE);   // lower latency for uploads; device is USB powered

    s_last_ip_change_ms = now_ms();
    s_next_retry_ms = 0;
    ESP_LOGI(TAG, "started, station %s", s_st.sta_configured ? "with stored networks" : "(no network stored)");

    start_mdns();
    xTaskCreate(wifi_task, "pd_wifi", 5120, NULL, 4, NULL);
    return ESP_OK;
}

void pd_wifi_get_status(pd_wifi_status_t *out)
{
    lock();
    *out = s_st;
    unlock();
}


size_t pd_wifi_list(pd_wifi_net_t *out, size_t max)
{
    size_t n = 0;
    lock();
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS && n < max; i++) {
        if (s_nets[i].used) {
            strlcpy(out[n].ssid, s_nets[i].ssid, sizeof(out[n].ssid));
            out[n].active = s_st.sta_connected && s_cur_slot == i;
            n++;
        }
    }
    unlock();
    return n;
}

#define WEB_SCAN_RECORDS 40

int pd_wifi_scan(pd_wifi_scan_entry_t *out, size_t max)
{
    if (!out || max == 0) {
        return PD_WIFI_SCAN_FAIL;
    }
    // Not while a connection attempt is running (a scan would abort it), nor during another scan.
    lock();
    if (s_connecting || s_web_scan || s_reconfigure) {
        unlock();
        return PD_WIFI_SCAN_BUSY;
    }
    if (xSemaphoreTake(s_scan_lock, 0) != pdTRUE) {
        unlock();
        return PD_WIFI_SCAN_BUSY;
    }
    s_web_scan = true;   // the wifi task starts no connect attempt until we are done
    unlock();

    int result = PD_WIFI_SCAN_FAIL;
    wifi_ap_record_t *recs = calloc(WEB_SCAN_RECORDS, sizeof(*recs));
    pd_wifi_scan_entry_t *ent = calloc(WEB_SCAN_RECORDS, sizeof(*ent));
    if (recs && ent) {
        wifi_scan_config_t sc = {0};
        sc.show_hidden = false;
        sc.scan_type = WIFI_SCAN_TYPE_ACTIVE;
        sc.scan_time.active.min = 80;
        sc.scan_time.active.max = 150;
        esp_err_t err = esp_wifi_scan_start(&sc, true);
        if (err == ESP_OK) {
            uint16_t n = WEB_SCAN_RECORDS;
            err = esp_wifi_scan_get_ap_records(&n, recs);   // also frees the internal list
            if (err == ESP_OK) {
                int cnt = 0;
                lock();
                for (int k = 0; k < n; k++) {
                    const char *ssid = (const char *)recs[k].ssid;
                    if (!ssid[0]) {
                        continue;   // hidden
                    }
                    int j = 0;
                    while (j < cnt && strcmp(ent[j].ssid, ssid) != 0) {
                        j++;
                    }
                    if (j < cnt) {
                        if (recs[k].rssi > ent[j].rssi) {
                            ent[j].rssi = recs[k].rssi;
                            ent[j].secure = recs[k].authmode != WIFI_AUTH_OPEN;
                        }
                        continue;
                    }
                    strlcpy(ent[cnt].ssid, ssid, sizeof(ent[cnt].ssid));
                    ent[cnt].rssi = recs[k].rssi;
                    ent[cnt].secure = recs[k].authmode != WIFI_AUTH_OPEN;
                    ent[cnt].saved = false;
                    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
                        if (s_nets[i].used && strcmp(s_nets[i].ssid, ssid) == 0) {
                            ent[cnt].saved = true;
                        }
                    }
                    cnt++;
                }
                unlock();
                // insertion sort, strongest first
                for (int a = 1; a < cnt; a++) {
                    pd_wifi_scan_entry_t tmp = ent[a];
                    int b = a;
                    while (b > 0 && ent[b - 1].rssi < tmp.rssi) {
                        ent[b] = ent[b - 1];
                        b--;
                    }
                    ent[b] = tmp;
                }
                size_t m = (size_t)cnt;
                if (m > max) {
                    m = max;
                }
                if (m > PD_WIFI_SCAN_MAX) {
                    m = PD_WIFI_SCAN_MAX;
                }
                memcpy(out, ent, m * sizeof(*out));
                result = (int)m;
            }
        } else if (err == ESP_ERR_WIFI_STATE) {
            result = PD_WIFI_SCAN_BUSY;   // station is connecting right now
        }
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "web scan failed: %s", esp_err_to_name(err));
        }
    }
    free(recs);
    free(ent);
    lock();
    s_web_scan = false;
    unlock();
    xSemaphoreGive(s_scan_lock);
    return result;
}

esp_err_t pd_wifi_add(const char *ssid, const char *pass)
{
    const size_t ls = ssid ? strlen(ssid) : 0, lp = pass ? strlen(pass) : 0;
    ESP_RETURN_ON_FALSE(ls >= 1 && ls <= 32, ESP_ERR_INVALID_ARG, TAG, "bad ssid length");
    ESP_RETURN_ON_FALSE(lp == 0 || (lp >= 8 && lp <= 63), ESP_ERR_INVALID_ARG, TAG, "bad password length");
    if (!pass) {
        pass = "";
    }

    xSemaphoreTake(s_cfg_lock, portMAX_DELAY);
    int slot = -1, freeslot = -1;
    lock();
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        if (s_nets[i].used && strcmp(s_nets[i].ssid, ssid) == 0) {
            slot = i;
            break;
        }
        if (!s_nets[i].used && freeslot < 0) {
            freeslot = i;
        }
    }
    unlock();
    if (slot < 0) {
        slot = freeslot;
    }
    if (slot < 0) {
        xSemaphoreGive(s_cfg_lock);
        return PD_WIFI_ERR_LIMIT;
    }

    char ks[3], kp[3];
    slot_key(ks, 's', slot);
    slot_key(kp, 'p', slot);
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_str(h, ks, ssid);
        if (err == ESP_OK) {
            err = nvs_set_str(h, kp, pass);
        }
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        xSemaphoreGive(s_cfg_lock);
        ESP_LOGE(TAG, "nvs write failed: %s", esp_err_to_name(err));
        return err;
    }

    lock();
    s_nets[slot].used = true;
    strlcpy(s_nets[slot].ssid, ssid, sizeof(s_nets[slot].ssid));
    strlcpy(s_nets[slot].pass, pass, sizeof(s_nets[slot].pass));
    s_st.sta_configured = true;
    s_prefer_slot = slot;
    unlock();
    s_reconfigure = true;
    xSemaphoreGive(s_cfg_lock);
    ESP_LOGI(TAG, "network '%s' stored in slot %d", ssid, slot);
    return ESP_OK;
}

esp_err_t pd_wifi_set_credentials(const char *ssid, const char *pass)
{
    return pd_wifi_add(ssid, pass);
}

esp_err_t pd_wifi_remove(const char *ssid)
{
    ESP_RETURN_ON_FALSE(ssid && ssid[0], ESP_ERR_INVALID_ARG, TAG, "no ssid");
    xSemaphoreTake(s_cfg_lock, portMAX_DELAY);
    int slot = -1;
    lock();
    for (int i = 0; i < PD_WIFI_MAX_NETWORKS; i++) {
        if (s_nets[i].used && strcmp(s_nets[i].ssid, ssid) == 0) {
            slot = i;
            break;
        }
    }
    unlock();
    if (slot < 0) {
        xSemaphoreGive(s_cfg_lock);
        return ESP_ERR_NOT_FOUND;
    }

    char ks[3], kp[3];
    slot_key(ks, 's', slot);
    slot_key(kp, 'p', slot);
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        nvs_erase_key(h, kp);
        err = nvs_erase_key(h, ks);
        if (err == ESP_ERR_NVS_NOT_FOUND) {
            err = ESP_OK;
        }
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        xSemaphoreGive(s_cfg_lock);
        return err;
    }

    lock();
    memset(&s_nets[slot], 0, sizeof(s_nets[slot]));
    s_st.sta_configured = count_nets_locked() > 0;
    if (s_prefer_slot == slot) {
        s_prefer_slot = -1;
    }
    if (s_cur_slot == slot) {
        s_cur_slot = -1;
        s_st.sta_ssid[0] = '\0';
        s_reconfigure = true;   // disconnect and re-evaluate
    }
    unlock();
    xSemaphoreGive(s_cfg_lock);
    ESP_LOGI(TAG, "network '%s' removed", ssid);
    return ESP_OK;
}

// ---- network settings (public) -------------------------------------------------------------

void pd_wifi_get_netcfg(pd_wifi_netcfg_t *out)
{
    memset(out, 0, sizeof(*out));
    lock();
    const netaddr_t n = s_net;
    strlcpy(out->hostname, s_cfg_host, sizeof(out->hostname));
    unlock();
    out->dhcp = n.dhcp;
    ip4_str(n.ip, out->addr.ip);
    ip4_str(n.mask, out->addr.mask);
    ip4_str(n.gw, out->addr.gateway);
    ip4_str(n.dns, out->addr.dns);
}

bool pd_wifi_get_current_ipv4(pd_wifi_ipv4_t *out)
{
    memset(out, 0, sizeof(*out));
    lock();
    const bool connected = s_st.sta_connected;
    unlock();
    if (!connected || !s_sta_netif) {
        return false;
    }
    esp_netif_ip_info_t info;
    if (esp_netif_get_ip_info(s_sta_netif, &info) == ESP_OK) {
        ip4_str(ip4_host(info.ip), out->ip);
        ip4_str(ip4_host(info.netmask), out->mask);
        ip4_str(ip4_host(info.gw), out->gateway);
    }
    esp_netif_dns_info_t dns;
    if (esp_netif_get_dns_info(s_sta_netif, ESP_NETIF_DNS_MAIN, &dns) == ESP_OK && dns.ip.type == ESP_IPADDR_TYPE_V4) {
        ip4_str(ip4_host(dns.ip.u_addr.ip4), out->dns);
    }
    return true;
}

static esp_err_t store_netcfg(const netaddr_t *n, const char *host)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_u8(h, "dhcp", n->dhcp ? 1 : 0);
    if (err == ESP_OK) {
        err = nvs_set_u32(h, "ip", n->ip);
    }
    if (err == ESP_OK) {
        err = nvs_set_u32(h, "mask", n->mask);
    }
    if (err == ESP_OK) {
        err = nvs_set_u32(h, "gateway", n->gw);
    }
    if (err == ESP_OK) {
        err = nvs_set_u32(h, "dns", n->dns);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(h, "hostname", host);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

/** New stored setting in RAM, applied by the Wi-Fi task from now + delay_ms (caller holds s_cfg_lock). */
static void schedule_netcfg(const netaddr_t *n, const char *host, int64_t delay_ms)
{
    lock();
    const netaddr_t old = s_net;
    if (n->dhcp != old.dhcp ||
        (!n->dhcp && (n->ip != old.ip || n->mask != old.mask || n->gw != old.gw || n->dns != old.dns))) {
        s_net_addr_dirty = true;
    }
    s_net = *n;
    strlcpy(s_cfg_host, host, sizeof(s_cfg_host));
    s_net_apply_at_ms = now_ms() + delay_ms;
    s_net_apply = true;
    unlock();
}

esp_err_t pd_wifi_set_netcfg(const pd_wifi_netcfg_t *cfg, pd_wifi_net_check_t *bad)
{
    pd_wifi_net_check_t dummy;
    if (!bad) {
        bad = &dummy;
    }
    *bad = PD_WIFI_NET_OK;
    ESP_RETURN_ON_FALSE(cfg && s_lock, ESP_ERR_INVALID_ARG, TAG, "no config / not started");

    xSemaphoreTake(s_cfg_lock, portMAX_DELAY);
    lock();
    netaddr_t n = s_net;
    char host[PD_WIFI_HOSTNAME_MAX + 1];
    strlcpy(host, s_cfg_host, sizeof(host));
    unlock();

    if (cfg->hostname[0]) {
        if (!pd_wifi_hostname_valid(cfg->hostname)) {
            *bad = PD_WIFI_NET_BAD_HOSTNAME;
        } else {
            strlcpy(host, cfg->hostname, sizeof(host));
        }
    }
    n.dhcp = cfg->dhcp;
    if (*bad == PD_WIFI_NET_OK && !cfg->dhcp) {
        uint32_t ip = 0, mask = 0, gw = 0, dns = 0;
        if (!parse_ip4(cfg->addr.ip, &ip)) {
            *bad = PD_WIFI_NET_BAD_IP;
        } else if (!parse_ip4(cfg->addr.mask, &mask)) {
            *bad = PD_WIFI_NET_BAD_MASK;
        } else if (!parse_ip4(cfg->addr.gateway, &gw)) {
            *bad = PD_WIFI_NET_BAD_GATEWAY;
        } else if (cfg->addr.dns[0] && !parse_ip4(cfg->addr.dns, &dns)) {
            *bad = PD_WIFI_NET_BAD_DNS;
        } else {
            if (!cfg->addr.dns[0]) {
                dns = gw;                   // no DNS server given: the router usually is one
            }
            *bad = check_static(ip, mask, gw, dns);
        }
        n.ip = ip;
        n.mask = mask;
        n.gw = gw;
        n.dns = dns;
    }
    if (*bad != PD_WIFI_NET_OK) {
        xSemaphoreGive(s_cfg_lock);
        return ESP_ERR_INVALID_ARG;
    }

    const esp_err_t err = store_netcfg(&n, host);
    if (err != ESP_OK) {
        xSemaphoreGive(s_cfg_lock);
        ESP_LOGE(TAG, "network settings not stored: %s", esp_err_to_name(err));
        return err;
    }
    schedule_netcfg(&n, host, NET_APPLY_DELAY_MS);
    xSemaphoreGive(s_cfg_lock);
    return ESP_OK;
}

esp_err_t pd_wifi_reset_network(void)
{
    if (!s_lock || !s_cfg_lock) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_cfg_lock, portMAX_DELAY);
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        static const char *const keys[] = {"dhcp", "ip", "mask", "gateway", "dns", "hostname"};
        for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
            const esp_err_t e = nvs_erase_key(h, keys[i]);
            if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND && err == ESP_OK) {
                err = e;
            }
        }
        const esp_err_t c = nvs_commit(h);
        if (err == ESP_OK) {
            err = c;
        }
        nvs_close(h);
    }
    // In effect even if NVS failed (the owner pressed the button): DHCP and the default name now.
    const netaddr_t n = {.dhcp = true};
    schedule_netcfg(&n, default_hostname(), 0);
    xSemaphoreGive(s_cfg_lock);
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "network settings reset: DHCP, hostname '%s'", default_hostname());
    } else {
        ESP_LOGE(TAG, "network settings reset in RAM only (NVS: %s)", esp_err_to_name(err));
    }
    return err;
}

void pd_wifi_set_netcfg_callback(pd_wifi_netcfg_cb_t cb)
{
    if (!s_lock) {
        s_net_cb = cb;
        return;
    }
    lock();
    s_net_cb = cb;
    unlock();
}
