#include "web_system.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "anim.h"
#include "applog.h"
#include "auth.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ota.h"
#include "pd_time.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "tls.h"
#include "ui.h"
#include "web_common.h"
#include "web_crypt.h"
#include "web_dav.h"

static const char *TAG = "web";

#if CONFIG_PD_UPLOAD_ALL_TYPES
#define UPLOAD_TYPES "all"
#elif CONFIG_PD_ALLOW_GCODE
#define UPLOAD_TYPES ".3mf,.gcode"
#else
#define UPLOAD_TYPES ".3mf"
#endif

#define STATUS_BUF_SIZE 3072     // /api/status answer (heap)
#define STATUS_SLACK    8        // closer than this to the end = possibly cut off (see h_status())

static const char *owner_str(drive_owner_t o)
{
    return o == DRIVE_OWNER_USB ? "usb" : (o == DRIVE_OWNER_APP ? "app" : "none");
}

static long idle_json(uint32_t ms)
{
    return ms == UINT32_MAX ? -1 : (long)ms;
}

// ---- clock, auto-lock, display language (0.5.0) ---------------------------------------------
// Status fields (about 200 bytes at most, the TZ string is <= 63 characters):
//   "time":{"epoch":N,"synced":B,"tz":"..."},
//   "autolock":{"minutes":N,"on_detach":B,"remaining_s":S,"waiting":B}, "lang":"de|en"
// remaining_s: seconds until the timer locks if nothing happens, -1 = no countdown; 0 with
// waiting=true: time is up but the lock is postponed (see drive_autolock_remaining_s()). Reading
// it does not count as activity, so an open page does not keep the drive unlocked.
static void append_clock_settings(char *buf, size_t len)
{
    char tz[PD_TIME_TZ_MAX + 1];
    pd_time_get_tz(tz, sizeof(tz));
    web_appendf(buf, len, ",\"time\":{\"epoch\":%lu,\"synced\":%s,\"tz\":", (unsigned long)pd_time_now(),
                pd_time_synced() ? "true" : "false");
    web_json_str(buf, len, tz);
    uint16_t minutes;
    bool on_detach;
    drive_autolock_get(&minutes, &on_detach);
    bool waiting = false;
    const int32_t remaining = drive_autolock_remaining_s(&waiting);
    web_appendf(buf, len,
                "},\"autolock\":{\"minutes\":%u,\"on_detach\":%s,\"remaining_s\":%ld,\"waiting\":%s},\"lang\":\"%s\"",
                (unsigned)minutes, on_detach ? "true" : "false", (long)remaining, waiting ? "true" : "false",
                ui_get_lang());
}

// POST /api/autolock/touch (user): "still working" - the auto-lock period starts again.
// Answer {"ok":true,"remaining_s":S}, S = -1 if no countdown runs.
static esp_err_t h_autolock_touch(httpd_req_t *req)
{
    char json[48];
    snprintf(json, sizeof(json), "{\"ok\":true,\"remaining_s\":%ld}", (long)drive_autolock_touch());
    return web_send_json(req, "200 OK", json);
}

/** Unsigned decimal query parameter (1..10 digits) -> *out. */
static bool query_uint(httpd_req_t *req, const char *key, uint32_t *out)
{
    char v[12];
    if (!web_query_param(req, key, v, sizeof(v)) || !v[0] || strlen(v) > 10) {
        return false;
    }
    for (const char *p = v; *p; p++) {
        if (*p < '0' || *p > '9') {
            return false;
        }
    }
    const unsigned long long n = strtoull(v, NULL, 10);
    if (n > UINT32_MAX) {
        return false;
    }
    *out = (uint32_t)n;
    return true;
}

// POST /api/settings/time?tz=<POSIX TZ> (admin). '+' must be sent as %2B (a raw '+' means space).
static esp_err_t h_set_tz(httpd_req_t *req)
{
    char tz[PD_TIME_TZ_MAX + 2];
    if (!web_query_param(req, "tz", tz, sizeof(tz)) || !pd_time_tz_valid(tz)) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                              "Ungültige Zeitzone (POSIX-TZ, höchstens 63 Zeichen)");
    }
    if (pd_time_set_tz(tz) != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Einstellung konnte nicht gespeichert werden");
    }
    return web_send_result(req, DRIVE_OK);
}

// POST /api/time?epoch=<unix seconds>[&force=1] (user): taken only while the clock is unknown
// (no NTP, not set by anyone since power-up); force=1 (admin only, else 403 FORBIDDEN_ROLE) also
// replaces a known clock, but never one set by NTP.
// Answer {"ok":true,"set":true|false}; set=false: the clock was already known (or NTP has synced)
// and was left alone.
static esp_err_t h_set_time(httpd_req_t *req)
{
    uint32_t epoch;
    if (!query_uint(req, "epoch", &epoch)) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "epoch fehlt");
    }
    // Refuse nonsense also while NTP is in charge (2023-11-14 .. 2100), then let pd_time decide.
    if (epoch < 1700000000u || epoch >= 4102444800u) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Uhrzeit nicht plausibel");
    }
    const bool force = web_query_flag(req, "force");
    if (force && auth_current_role(req) != WEB_ROLE_ADMIN) {
        return auth_send_error(req, AUTH_ERR_FORBIDDEN_ROLE);
    }
    const esp_err_t err = force ? pd_time_set_admin(epoch) : pd_time_set_client(epoch);
    if (err == ESP_ERR_INVALID_STATE) {
        return web_send_json(req, "200 OK", "{\"ok\":true,\"set\":false}");
    }
    if (err != ESP_OK) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Uhrzeit nicht plausibel");
    }
    return web_send_json(req, "200 OK", "{\"ok\":true,\"set\":true}");
}

// POST /api/settings/autolock?minutes=N&on_detach=0|1 (admin)
static esp_err_t h_set_autolock(httpd_req_t *req)
{
    // Either parameter may be omitted: it then keeps its value.
    uint16_t cur_min = 0;
    bool on_detach = false;
    drive_autolock_get(&cur_min, &on_detach);
    uint32_t minutes = cur_min;
    char det[8];
    const bool has_min = query_uint(req, "minutes", &minutes);
    const bool has_det = web_query_param(req, "on_detach", det, sizeof(det));
    if ((!has_min && !has_det) || minutes > DRIVE_AUTOLOCK_MINUTES_MAX ||
        (has_det && strcmp(det, "0") && strcmp(det, "1") && strcmp(det, "true") && strcmp(det, "false"))) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                              "minutes (0-1440) oder on_detach (0/1) erforderlich");
    }
    if (has_det) {
        on_detach = strcmp(det, "1") == 0 || strcmp(det, "true") == 0;
    }
    if (drive_autolock_set((uint16_t)minutes, on_detach) != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Einstellung konnte nicht gespeichert werden");
    }
    return web_send_result(req, DRIVE_OK);
}

// POST /api/settings/lang?value=de|en (user): the web UI calls it when the user switches language.
static esp_err_t h_set_lang(httpd_req_t *req)
{
    char v[4];
    if (!web_query_param(req, "value", v, sizeof(v)) || (strcmp(v, "de") && strcmp(v, "en"))) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "value muss de oder en sein");
    }
    if (ui_set_lang(v) != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Einstellung konnte nicht gespeichert werden");
    }
    return web_send_result(req, DRIVE_OK);
}

static esp_err_t h_status(httpd_req_t *req)
{
    drive_status_t d;
    pd_wifi_status_t w;
    drive_get_status(&d);
    pd_wifi_get_status(&w);
    // Locked (API v4, 2.3): no owner, no sizes. The drive layer reports owner APP with fs_ok=false.
    const bool locked = d.crypt == DRIVE_CRYPT_LOCKED || d.crypt == DRIVE_CRYPT_DAMAGED;
    if (locked) {
        d.owner = DRIVE_OWNER_NONE;
        d.fs_ok = false;
        d.needs_format = false;
        d.total_bytes = d.free_bytes = 0;
        d.file_count = 0;
    }

    // Heap, not the httpd task stack (10/12 KiB, shared with FatFs/mbedTLS): about 1.6 KiB is
    // typical, the worst case (escaped SSIDs, hostname, resumable path, all names at their limits)
    // stays below 2.5 KiB.
    char *const buf = malloc(STATUS_BUF_SIZE);
    if (!buf) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
    }
    buf[0] = '\0';
    web_appendf(buf, STATUS_BUF_SIZE, "{\"version\":");
    web_json_str(buf, STATUS_BUF_SIZE, esp_app_get_description()->version);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"uptime_s\":%lld,\"heap\":%lu,\"types\":\"%s\"",
                esp_timer_get_time() / 1000000, (unsigned long)esp_get_free_heap_size(), UPLOAD_TYPES);
    web_appendf(buf, STATUS_BUF_SIZE,
                ",\"drive\":{\"owner\":\"%s\",\"fs_ok\":%s,\"needs_format\":%s,\"usb_connected\":%s,"
                "\"usb_suspended\":%s,\"host_ejected\":%s,\"host_writable\":%s,\"host_idle_ms\":%ld,"
                "\"host_write_idle_ms\":%ld,\"write_guard_ms\":%d,\"total\":%llu,\"free\":%llu,\"files\":%lu,"
                "\"sector\":%lu}",
                owner_str(d.owner), d.fs_ok ? "true" : "false", d.needs_format ? "true" : "false",
                d.usb_connected ? "true" : "false", d.usb_suspended ? "true" : "false",
                d.host_ejected ? "true" : "false", d.host_writable ? "true" : "false",
                idle_json(d.host_idle_ms), idle_json(d.host_write_idle_ms), CONFIG_PD_HOST_WRITE_GUARD_MS,
                (unsigned long long)d.total_bytes, (unsigned long long)d.free_bytes,
                (unsigned long)d.file_count, (unsigned long)d.sector_size);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"medium\":{\"sd\":%s,\"name\":", d.on_sd ? "true" : "false");
    web_json_str(buf, STATUS_BUF_SIZE, d.medium_name);
    web_appendf(buf, STATUS_BUF_SIZE, "}");
    web_appendf(buf, STATUS_BUF_SIZE, ",\"upload\":{\"active\":%s,\"name\":", d.upload_active ? "true" : "false");
    web_json_str(buf, STATUS_BUF_SIZE, d.upload_name);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"received\":%lu,\"total\":%lu,\"resumable\":", (unsigned long)d.upload_received,
                (unsigned long)d.upload_total);
    {   // interrupted upload kept for resuming (web_files.c, /api/upload/resume), else null
        char rp[DRIVE_PATH_MAX + 1];
        uint32_t rsize = 0, roff = 0;
        if (drive_upload_resumable(rp, sizeof(rp), &rsize, &roff)) {
            web_appendf(buf, STATUS_BUF_SIZE, "{\"path\":");
            web_json_str(buf, STATUS_BUF_SIZE, rp);
            web_appendf(buf, STATUS_BUF_SIZE, ",\"size\":%lu,\"offset\":%lu}}", (unsigned long)rsize, (unsigned long)roff);
        } else {
            web_appendf(buf, STATUS_BUF_SIZE, "null}");
        }
    }
    web_appendf(buf, STATUS_BUF_SIZE, ",\"wifi\":{\"sta_configured\":%s,\"sta_connected\":%s,\"ssid\":",
                w.sta_configured ? "true" : "false", w.sta_connected ? "true" : "false");
    web_json_str(buf, STATUS_BUF_SIZE, w.sta_ssid);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"ip\":\"%s\",\"rssi\":%d,\"ap_active\":%s,\"ap_ssid\":", w.sta_ip,
                w.sta_rssi, w.ap_active ? "true" : "false");
    web_json_str(buf, STATUS_BUF_SIZE, w.ap_ssid);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"ap_ip\":\"%s\",\"hostname\":", w.ap_ip);
    web_json_str(buf, STATUS_BUF_SIZE, w.hostname);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"dhcp\":%s}", w.sta_dhcp ? "true" : "false");
    char part[17], key_id[9];
    bool pending, signed_required;
    ota_get_info(part, sizeof(part), &pending);
    ota_get_signing(&signed_required, key_id, sizeof(key_id));   // docs/17_signierte_updates.md
    web_appendf(buf, STATUS_BUF_SIZE,
                ",\"ota\":{\"partition\":\"%s\",\"pending_verify\":%s,\"signed_required\":%s,\"key_id\":\"%s\"}",
                part, pending ? "true" : "false", signed_required ? "true" : "false", key_id);
    web_appendf(buf, STATUS_BUF_SIZE, ",\"webdav\":%s", web_dav_enabled() ? "true" : "false");
    web_appendf(buf, STATUS_BUF_SIZE, ",\"crypt\":{\"enabled\":%s,\"unlocked\":%s,\"damaged\":%s}",
                d.crypt != DRIVE_CRYPT_OFF ? "true" : "false", locked ? "false" : "true",
                d.crypt == DRIVE_CRYPT_DAMAGED ? "true" : "false");
    append_clock_settings(buf, STATUS_BUF_SIZE);   // "time", "autolock", "lang" - before the HTTPS fields
#ifdef CONFIG_PD_HTTPS_OPTION
    pd_tls_info_t t;
    pd_tls_get_info(&t);
    web_appendf(buf, STATUS_BUF_SIZE,
                ",\"https\":%s,\"https_active\":%s,\"https_restart\":%s,\"cert_sha256\":\"%s\",\"ca_sha256\":\"%s\","
                "\"cert_expires\":\"%s\"}",
                t.enabled ? "true" : "false", t.active ? "true" : "false", t.restart_pending ? "true" : "false",
                t.cert_sha256, t.ca_sha256, t.cert_expires);
#else
    web_appendf(buf, STATUS_BUF_SIZE, "}");   // without the fields the web UI hides its HTTPS section
#endif
    // web_appendf()/web_json_str() cut silently at the end of the buffer: an answer that came that
    // close may be incomplete, and broken JSON is worse than an error.
    esp_err_t err;
    if (strlen(buf) + STATUS_SLACK >= STATUS_BUF_SIZE) {
        ESP_LOGE(TAG, "status answer too long (%u bytes)", (unsigned)strlen(buf));
        err = web_send_error(req, "500 Internal Server Error", "IO", "Statusantwort zu lang");
    } else {
        err = web_send_json(req, "200 OK", buf);
    }
    free(buf);
    return err;
}

static esp_err_t h_mode(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char target[8];
    if (!web_query_param(req, "target", target, sizeof(target))) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "target fehlt");
    }
    drive_owner_t o;
    if (strcmp(target, "usb") == 0) {
        o = DRIVE_OWNER_USB;
    } else if (strcmp(target, "app") == 0) {
        o = DRIVE_OWNER_APP;
    } else {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "target muss usb oder app sein");
    }
    return web_send_result(req, drive_set_owner(o, web_query_flag(req, "force")));
}

static esp_err_t h_host_writable(httpd_req_t *req)
{
    drive_set_host_writable(web_query_flag(req, "value"));
    return web_send_result(req, DRIVE_OK);
}

static esp_err_t h_format(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char confirm[16];
    if (!web_query_param(req, "confirm", confirm, sizeof(confirm)) || strcmp(confirm, "FORMAT") != 0) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Bestätigung fehlt");
    }
    drive_status_t d;
    drive_get_status(&d);
    if (d.owner != DRIVE_OWNER_APP) {
        return web_send_result(req, DRIVE_ERR_WRONG_OWNER);
    }
    if (d.crypt != DRIVE_CRYPT_OFF) {
        // Encrypted and unlocked: formatted right away, no restart (the drive stays unlocked).
        anim_set_busy(true);
        const drive_result_t r = drive_format();
        anim_set_busy(false);
        if (r != DRIVE_OK) {
            ESP_LOGE(TAG, "format failed: %s", drive_result_str(r));
            return web_send_result(req, r);
        }
        return web_send_json(req, "200 OK", "{\"ok\":true,\"restart\":false}");
    }
    // Not encrypted: drive_format() restarts the device on success; answer first.
    web_send_json(req, "200 OK", "{\"ok\":true,\"restart\":true}");
    vTaskDelay(pdMS_TO_TICKS(200));
    anim_set_busy(true);
    const drive_result_t r = drive_format();
    anim_set_busy(false);
    ESP_LOGE(TAG, "format failed: %s", drive_result_str(r));
    return ESP_OK;
}

static esp_err_t h_reboot(httpd_req_t *req)
{
    char mode[16] = "normal";
    web_query_param(req, "mode", mode, sizeof(mode));
    const drive_result_t r = drive_prepare_restart(web_query_flag(req, "force"));
    if (r != DRIVE_OK) {
        return web_send_result(req, r);
    }
    web_send_json(req, "200 OK", "{\"ok\":true}");
    vTaskDelay(pdMS_TO_TICKS(300));
    if (strcmp(mode, "download") == 0) {
        drive_restart_to_download();
    }
    esp_restart();
    return ESP_OK;
}

static esp_err_t h_log(httpd_req_t *req)
{
    char *buf = malloc(8193);
    if (!buf) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Kein Speicher");
    }
    const size_t n = applog_read(buf, 8193);
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    const esp_err_t err = httpd_resp_send(req, buf, n);
    free(buf);
    return err;
}

static const web_route_t s_routes[] = {
    {"/api/status", HTTP_GET, h_status, WEB_ROLE_USER},
    {"/api/mode", HTTP_POST, h_mode, WEB_ROLE_USER},
    {"/api/settings/host_writable", HTTP_POST, h_host_writable, WEB_ROLE_ADMIN},
    {"/api/format", HTTP_POST, h_format, WEB_ROLE_ADMIN},
    {"/api/reboot", HTTP_POST, h_reboot, WEB_ROLE_ADMIN},
    {"/api/log", HTTP_GET, h_log, WEB_ROLE_ADMIN},
    {"/api/settings/time", HTTP_POST, h_set_tz, WEB_ROLE_ADMIN},
    {"/api/time", HTTP_POST, h_set_time, WEB_ROLE_USER},
    {"/api/settings/autolock", HTTP_POST, h_set_autolock, WEB_ROLE_ADMIN},
    {"/api/settings/lang", HTTP_POST, h_set_lang, WEB_ROLE_USER},
    {"/api/autolock/touch", HTTP_POST, h_autolock_touch, WEB_ROLE_USER},
};

esp_err_t web_system_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
