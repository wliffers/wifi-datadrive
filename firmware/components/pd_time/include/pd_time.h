// System clock: SNTP once the station has an IP, or set once from a browser; time zone setting.
//
// The system clock holds real UTC (unix seconds). The time zone (POSIX TZ string, NVS) is applied
// with setenv("TZ") / tzset(), so localtime() gives local time everywhere. That also covers the
// file system: ESP-IDF's FatFs port (components/fatfs/diskio/diskio.c, get_fattime()) stamps new
// and changed files with localtime(time(NULL)), and the VFS stat()/utime() convert FAT's local
// date/time with mktime()/localtime(). File times in the API are therefore real unix seconds.
//
// Clock unknown (no NTP, not set from a browser since power-up): time(NULL) counts from 1970-01-01
// (seconds since boot). get_fattime() clamps every year before 1980 to 1980, so files written then
// get a date in 1980 (normally 1980-01-01). pd_time_from_fat() and the drive listing report any
// 1980 date as 0 = "unknown" - such files simply show no date; nothing breaks. A soft restart keeps
// the clock (the RTC keeps running), a power cycle loses it.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PD_TIME_TZ_MAX      63                              // max length of a POSIX TZ string
#define PD_TIME_TZ_DEFAULT  "CET-1CEST,M3.5.0,M10.5.0/3"    // Central Europe

/** Load the time zone from NVS and apply it. Call right after nvs_flash_init(), before files are written. */
esp_err_t pd_time_init(void);

/**
 * Set up SNTP (pool.ntp.org, plus the server announced by DHCP if CONFIG_LWIP_DHCP_GET_NTP_SRV is
 * on and CONFIG_LWIP_SNTP_MAX_SERVERS >= 2). Requests start whenever the station gets an IP.
 * Call after pd_wifi_start() (needs esp_netif and the default event loop).
 */
esp_err_t pd_time_start(void);

/** Current time in unix seconds; 0 while the clock is unknown (before the build date). */
uint32_t pd_time_now(void);

/** True once SNTP has set the clock since power-up. */
bool pd_time_synced(void);

/** Current time zone string (always NUL-terminated, at most PD_TIME_TZ_MAX characters). */
void pd_time_get_tz(char *out, size_t len);

/** Syntax check of a TZ string: 1..PD_TIME_TZ_MAX characters of A-Z a-z 0-9 + - , . / : < >. */
bool pd_time_tz_valid(const char *tz);

/** Validate, store in NVS and apply a time zone. ESP_ERR_INVALID_ARG for a bad string. */
esp_err_t pd_time_set_tz(const char *tz);

/**
 * Set the clock from a client (browser). Refused while NTP has synced (ESP_ERR_INVALID_STATE).
 * ESP_ERR_INVALID_ARG if epoch is before the firmware's build date or after 2100.
 */
esp_err_t pd_time_set_client(uint32_t epoch);

/**
 * FAT date/time (local time, as in FILINFO.fdate/ftime) -> unix seconds using the current time
 * zone. 0 for invalid fields and for every date in 1980 (written while the clock was unknown).
 */
uint32_t pd_time_from_fat(uint16_t fdate, uint16_t ftime);

#ifdef __cplusplus
}
#endif
