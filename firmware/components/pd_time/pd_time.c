#include "pd_time.h"

#include "sdkconfig.h"
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "time";
static const char *NVS_NS = "pd_time";
static const char *NVS_KEY_TZ = "tz";

#define EPOCH_MAX  4102444800UL      // 2100-01-01: later client times are rejected

// NTP server from DHCP (option 42) needs CONFIG_LWIP_DHCP_GET_NTP_SRV and a free slot after the
// DHCP servers for pool.ntp.org (CONFIG_LWIP_SNTP_MAX_SERVERS).
#if defined(CONFIG_LWIP_DHCP_GET_NTP_SRV)
#if CONFIG_LWIP_SNTP_MAX_SERVERS > CONFIG_LWIP_DHCP_MAX_NTP_SERVERS
#define USE_DHCP_NTP 1
#endif
#endif
#ifndef USE_DHCP_NTP
#define USE_DHCP_NTP 0
#endif

static char s_tz[PD_TIME_TZ_MAX + 1] = PD_TIME_TZ_DEFAULT;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool s_synced;
static volatile bool s_admin_set;    // set by an admin (pd_time_set_admin()) since power-up
static bool s_setting;               // a client/admin set is in progress (guarded by s_mux)
static uint32_t s_build_epoch;       // UTC midnight of the build date (__DATE__ of this file)

/** Days since 1970-01-01 of a civil date (proleptic Gregorian; H. Hinnant's algorithm). */
static int32_t days_from_civil(int y, int m, int d)
{
    y -= m <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const int yoe = y - era * 400;
    const int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

/** __DATE__ ("Nov 12 2026") -> unix seconds of that day 00:00 UTC. */
static uint32_t build_epoch(void)
{
    static const char months[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    const char *d = __DATE__;
    int m = 1;
    for (; m <= 12 && strncmp(months + (m - 1) * 3, d, 3) != 0; m++) {
    }
    const int day = atoi(d + 4), year = atoi(d + 7);
    if (m > 12 || day < 1 || year < 2024) {
        return 1735689600UL;          // 2025-01-01, should never happen
    }
    return (uint32_t)days_from_civil(year, m, day) * 86400UL;
}

static void apply_tz(const char *tz)
{
    setenv("TZ", tz, 1);
    tzset();
}

esp_err_t pd_time_init(void)
{
    s_build_epoch = build_epoch();
    char tz[PD_TIME_TZ_MAX + 1] = PD_TIME_TZ_DEFAULT;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(tz);
        if (nvs_get_str(h, NVS_KEY_TZ, tz, &len) != ESP_OK || !pd_time_tz_valid(tz)) {
            strlcpy(tz, PD_TIME_TZ_DEFAULT, sizeof(tz));
        }
        nvs_close(h);
    }
    taskENTER_CRITICAL(&s_mux);
    strlcpy(s_tz, tz, sizeof(s_tz));
    taskEXIT_CRITICAL(&s_mux);
    apply_tz(tz);
    ESP_LOGI(TAG, "time zone %s, clock %s", tz, pd_time_now() ? "known (kept over restart)" : "unknown");
    return ESP_OK;
}

// Runs in the lwIP thread after SNTP has set the system time.
static void on_sync(struct timeval *tv)
{
    const bool first = !s_synced;
    s_synced = true;
    if (first) {
        ESP_LOGI(TAG, "clock set by NTP: %lld", (long long)tv->tv_sec);
    }
}

// Every new station IP (also after a reconnect): ask the servers right away instead of waiting
// for the back-off of the requests that failed without a network.
static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_netif_sntp_start();
}

esp_err_t pd_time_start(void)
{
#if USE_DHCP_NTP
    // DHCP-announced servers take the first slots; pool.ntp.org follows (re-set after every IP).
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    cfg.server_from_dhcp = true;
    cfg.renew_servers_after_new_IP = true;
    cfg.index_of_first_server = CONFIG_LWIP_DHCP_MAX_NTP_SERVERS;
#else
    // Without CONFIG_LWIP_DHCP_GET_NTP_SRV (and a 2nd server slot) only the public pool is used.
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
#endif
    cfg.start = false;               // started on IP_EVENT_STA_GOT_IP
    cfg.wait_for_sync = false;       // no semaphore needed
    cfg.sync_cb = on_sync;
    esp_err_t err = esp_netif_sntp_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SNTP init failed: %s", esp_err_to_name(err));
        return err;
    }
    // Registered after esp_netif_sntp_init(): its server renewal on the same event runs first.
    err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_got_ip, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "IP event handler: %s", esp_err_to_name(err));
    }
    return err;
}

uint32_t pd_time_now(void)
{
    const time_t t = time(NULL);
    return (t >= (time_t)s_build_epoch && t < (time_t)EPOCH_MAX) ? (uint32_t)t : 0;
}

bool pd_time_synced(void)
{
    return s_synced;
}

void pd_time_get_tz(char *out, size_t len)
{
    taskENTER_CRITICAL(&s_mux);
    strlcpy(out, s_tz, len);
    taskEXIT_CRITICAL(&s_mux);
}

bool pd_time_tz_valid(const char *tz)
{
    const size_t n = tz ? strlen(tz) : 0;
    if (n == 0 || n > PD_TIME_TZ_MAX) {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        const char c = tz[i];
        const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                        strchr("+-,./:<>", c) != NULL;
        if (!ok) {
            return false;
        }
    }
    // A POSIX TZ starts with the zone name: letters, or a quoted name like "<+03>".
    return (tz[0] >= 'A' && tz[0] <= 'Z') || (tz[0] >= 'a' && tz[0] <= 'z') || tz[0] == '<';
}

esp_err_t pd_time_set_tz(const char *tz)
{
    if (!pd_time_tz_valid(tz)) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_str(h, NVS_KEY_TZ, tz);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "time zone not saved: %s", esp_err_to_name(err));
        return err;
    }
    taskENTER_CRITICAL(&s_mux);
    strlcpy(s_tz, tz, sizeof(s_tz));
    taskEXIT_CRITICAL(&s_mux);
    apply_tz(tz);
    ESP_LOGI(TAG, "time zone set to %s", tz);
    return ESP_OK;
}

/** Common part of pd_time_set_client() / pd_time_set_admin(). */
static esp_err_t set_clock(uint32_t epoch, bool admin)
{
    if (epoch < s_build_epoch || epoch >= EPOCH_MAX) {
        return ESP_ERR_INVALID_ARG;
    }
    // One setter at a time, so two clients cannot both see "unknown" and both set the clock.
    bool busy;
    taskENTER_CRITICAL(&s_mux);
    busy = s_setting;
    s_setting = true;
    taskEXIT_CRITICAL(&s_mux);
    if (busy) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = ESP_OK;
    if (s_synced || (!admin && pd_time_now() != 0)) {
        err = ESP_ERR_INVALID_STATE;     // NTP is in charge, or the clock is already known
    } else {
        const struct timeval tv = {.tv_sec = (time_t)epoch};
        if (settimeofday(&tv, NULL) != 0) {
            err = ESP_FAIL;
        } else {
            if (admin) {
                s_admin_set = true;
            }
            ESP_LOGI(TAG, "clock set by %s: %lu", admin ? "an admin" : "a client", (unsigned long)epoch);
        }
    }
    taskENTER_CRITICAL(&s_mux);
    s_setting = false;
    taskEXIT_CRITICAL(&s_mux);
    return err;
}

esp_err_t pd_time_set_client(uint32_t epoch)
{
    return set_clock(epoch, false);
}

esp_err_t pd_time_set_admin(uint32_t epoch)
{
    return set_clock(epoch, true);
}

bool pd_time_trusted(void)
{
    return s_synced || (s_admin_set && pd_time_now() != 0);
}

uint32_t pd_time_from_fat(uint16_t fdate, uint16_t ftime)
{
    const int year = 1980 + (fdate >> 9), mon = (fdate >> 5) & 0x0F, mday = fdate & 0x1F;
    const int hour = ftime >> 11, min = (ftime >> 5) & 0x3F, sec = (ftime & 0x1F) * 2;
    if (year == 1980 || mon < 1 || mon > 12 || mday < 1 || mday > 31 || hour > 23 || min > 59 || sec > 59) {
        return 0;
    }
    struct tm tm = {
        .tm_year = year - 1900,
        .tm_mon = mon - 1,
        .tm_mday = mday,
        .tm_hour = hour,
        .tm_min = min,
        .tm_sec = sec,
        .tm_isdst = -1,              // let the time zone rules decide (summer time)
    };
    const time_t t = mktime(&tm);
    return (t > 0 && t < (time_t)EPOCH_MAX) ? (uint32_t)t : 0;
}
