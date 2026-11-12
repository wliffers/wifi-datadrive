#include "web_system.h"

#include <stdlib.h>
#include <string.h>
#include "anim.h"
#include "applog.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ota.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "tls.h"
#include "web_common.h"
#include "web_crypt.h"

static const char *TAG = "web";

#if CONFIG_PD_UPLOAD_ALL_TYPES
#define UPLOAD_TYPES "all"
#elif CONFIG_PD_ALLOW_GCODE
#define UPLOAD_TYPES ".3mf,.gcode"
#else
#define UPLOAD_TYPES ".3mf"
#endif

static const char *owner_str(drive_owner_t o)
{
    return o == DRIVE_OWNER_USB ? "usb" : (o == DRIVE_OWNER_APP ? "app" : "none");
}

static long idle_json(uint32_t ms)
{
    return ms == UINT32_MAX ? -1 : (long)ms;
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

    char buf[2048] = "";
    web_appendf(buf, sizeof(buf), "{\"version\":");
    web_json_str(buf, sizeof(buf), esp_app_get_description()->version);
    web_appendf(buf, sizeof(buf), ",\"uptime_s\":%lld,\"heap\":%lu,\"types\":\"%s\"",
                esp_timer_get_time() / 1000000, (unsigned long)esp_get_free_heap_size(), UPLOAD_TYPES);
    web_appendf(buf, sizeof(buf),
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
    web_appendf(buf, sizeof(buf), ",\"medium\":{\"sd\":%s,\"name\":", d.on_sd ? "true" : "false");
    web_json_str(buf, sizeof(buf), d.medium_name);
    web_appendf(buf, sizeof(buf), "}");
    web_appendf(buf, sizeof(buf), ",\"upload\":{\"active\":%s,\"name\":", d.upload_active ? "true" : "false");
    web_json_str(buf, sizeof(buf), d.upload_name);
    web_appendf(buf, sizeof(buf), ",\"received\":%lu,\"total\":%lu}", (unsigned long)d.upload_received,
                (unsigned long)d.upload_total);
    web_appendf(buf, sizeof(buf), ",\"wifi\":{\"sta_configured\":%s,\"sta_connected\":%s,\"ssid\":",
                w.sta_configured ? "true" : "false", w.sta_connected ? "true" : "false");
    web_json_str(buf, sizeof(buf), w.sta_ssid);
    web_appendf(buf, sizeof(buf), ",\"ip\":\"%s\",\"rssi\":%d,\"ap_active\":%s,\"ap_ssid\":", w.sta_ip,
                w.sta_rssi, w.ap_active ? "true" : "false");
    web_json_str(buf, sizeof(buf), w.ap_ssid);
    web_appendf(buf, sizeof(buf), ",\"ap_ip\":\"%s\",\"hostname\":", w.ap_ip);
    web_json_str(buf, sizeof(buf), w.hostname);
    web_appendf(buf, sizeof(buf), "}");
    char part[17];
    bool pending;
    ota_get_info(part, sizeof(part), &pending);
    web_appendf(buf, sizeof(buf), ",\"ota\":{\"partition\":\"%s\",\"pending_verify\":%s}", part,
                pending ? "true" : "false");
    web_appendf(buf, sizeof(buf), ",\"crypt\":{\"enabled\":%s,\"unlocked\":%s,\"damaged\":%s}",
                d.crypt != DRIVE_CRYPT_OFF ? "true" : "false", locked ? "false" : "true",
                d.crypt == DRIVE_CRYPT_DAMAGED ? "true" : "false");
#ifdef CONFIG_PD_HTTPS_OPTION
    pd_tls_info_t t;
    pd_tls_get_info(&t);
    web_appendf(buf, sizeof(buf),
                ",\"https\":%s,\"https_active\":%s,\"https_restart\":%s,\"cert_sha256\":\"%s\",\"ca_sha256\":\"%s\","
                "\"cert_expires\":\"%s\"}",
                t.enabled ? "true" : "false", t.active ? "true" : "false", t.restart_pending ? "true" : "false",
                t.cert_sha256, t.ca_sha256, t.cert_expires);
#else
    web_appendf(buf, sizeof(buf), "}");   // without the fields the web UI hides its HTTPS section
#endif
    return web_send_json(req, "200 OK", buf);
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
};

esp_err_t web_system_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
