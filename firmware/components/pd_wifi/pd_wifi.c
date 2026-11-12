#include "pd_wifi.h"

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
    if (h) {
        nvs_close(h);
    }

    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(s_st.ap_ssid, sizeof(s_st.ap_ssid), "DataDrive-%02X%02X", mac[4], mac[5]);
    strlcpy(s_st.hostname, CONFIG_PD_WIFI_HOSTNAME, sizeof(s_st.hostname));
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

    if (count > 1) {
        // Short active scan: little time away from the channel of the setup access point.
        wifi_scan_config_t sc = {0};
        sc.show_hidden = false;
        sc.scan_type = WIFI_SCAN_TYPE_ACTIVE;
        sc.scan_time.active.min = 60;
        sc.scan_time.active.max = 110;
        if (esp_wifi_scan_start(&sc, true) == ESP_OK) {
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
        ESP_LOGW(TAG, "setup access point on: SSID '%s' (password on the LCD), http://192.168.4.1", s_st.ap_ssid);
    } else {
        esp_wifi_set_mode(WIFI_MODE_STA);
        ESP_LOGI(TAG, "setup access point off");
    }
    lock();
    s_st.ap_active = on;
    s_st.ap_clients = 0;
    strlcpy(s_st.ap_ip, on ? "192.168.4.1" : "", sizeof(s_st.ap_ip));
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

static void wifi_task(void *arg)
{
    while (true) {
        const int64_t t = now_ms();

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
                s_connecting = true;
                s_next_retry_ms = t + 20000;   // safety net if no event arrives
                unlock();
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
    load_config();

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    esp_err_t err = esp_event_loop_create_default();
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "event loop");
    s_sta_netif = esp_netif_create_default_wifi_sta();
    s_ap_netif = esp_netif_create_default_wifi_ap();
    esp_netif_set_hostname(s_sta_netif, s_st.hostname);

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
