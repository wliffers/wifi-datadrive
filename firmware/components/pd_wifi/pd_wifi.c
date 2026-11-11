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
static char s_sta_pass[65];

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
    nvs_get_str_or(h, "ssid", s_st.sta_ssid, sizeof(s_st.sta_ssid), PD_WIFI_SSID);
    nvs_get_str_or(h, "pass", s_sta_pass, sizeof(s_sta_pass), PD_WIFI_PASS);
    s_st.sta_configured = s_st.sta_ssid[0] != '\0';

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
    snprintf(s_st.ap_ssid, sizeof(s_st.ap_ssid), "PrintDrive-%02X%02X", mac[4], mac[5]);
    strlcpy(s_st.hostname, CONFIG_PD_WIFI_HOSTNAME, sizeof(s_st.hostname));
}

static void apply_sta_config(void)
{
    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, s_st.sta_ssid, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, s_sta_pass, sizeof(cfg.sta.password));
    cfg.sta.threshold.authmode = s_sta_pass[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
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
        ESP_LOGW(TAG, "setup access point on: SSID '%s', password '%s', http://192.168.4.1",
                 s_st.ap_ssid, s_st.ap_pass);
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
        }
        s_next_retry_ms = now_ms() + s_retry_delay_ms;
        s_retry_delay_ms = s_retry_delay_ms * 2 > RETRY_MAX_MS ? RETRY_MAX_MS : s_retry_delay_ms * 2;
        unlock();
        ESP_LOGW(TAG, "station disconnected (reason %d)", d ? d->reason : -1);
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
            apply_sta_config();
            lock();
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
            if (configured && !connecting && retry_due) {
                lock();
                s_connecting = true;
                s_next_retry_ms = t + 20000;   // safety net if no event arrives
                unlock();
                ESP_LOGI(TAG, "connecting to '%s' ...", s_st.sta_ssid);
                if (esp_wifi_connect() != ESP_OK) {
                    lock();
                    s_connecting = false;
                    s_next_retry_ms = t + RETRY_MAX_MS;
                    unlock();
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
    mdns_instance_name_set("WiFi PrintDrive");
    mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    ESP_LOGI(TAG, "mDNS: http://%s.local", s_st.hostname);
}

esp_err_t pd_wifi_start(void)
{
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_lock, ESP_ERR_NO_MEM, TAG, "mutex");
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

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "mode");
    apply_sta_config();
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start");
    esp_wifi_set_ps(WIFI_PS_NONE);   // lower latency for uploads; device is USB powered

    s_last_ip_change_ms = now_ms();
    s_next_retry_ms = 0;
    ESP_LOGI(TAG, "started, station %s", s_st.sta_configured ? s_st.sta_ssid : "(not configured)");

    start_mdns();
    xTaskCreate(wifi_task, "pd_wifi", 4096, NULL, 4, NULL);
    return ESP_OK;
}

void pd_wifi_get_status(pd_wifi_status_t *out)
{
    lock();
    *out = s_st;
    unlock();
}

esp_err_t pd_wifi_set_credentials(const char *ssid, const char *pass)
{
    const size_t ls = ssid ? strlen(ssid) : 0, lp = pass ? strlen(pass) : 0;
    ESP_RETURN_ON_FALSE(ls >= 1 && ls <= 32, ESP_ERR_INVALID_ARG, TAG, "bad ssid length");
    ESP_RETURN_ON_FALSE(lp == 0 || (lp >= 8 && lp <= 63), ESP_ERR_INVALID_ARG, TAG, "bad password length");

    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NS, NVS_READWRITE, &h), TAG, "nvs open");
    esp_err_t err = nvs_set_str(h, "ssid", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "pass", pass ? pass : "");
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    ESP_RETURN_ON_ERROR(err, TAG, "nvs write");

    lock();
    strlcpy(s_st.sta_ssid, ssid, sizeof(s_st.sta_ssid));
    strlcpy(s_sta_pass, pass ? pass : "", sizeof(s_sta_pass));
    s_st.sta_configured = true;
    unlock();
    s_reconfigure = true;
    ESP_LOGI(TAG, "new credentials stored for '%s'", ssid);
    return ESP_OK;
}
