#include "web_server.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "applog.h"
#include "drive.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pd_wifi.h"
#include "sdkconfig.h"

static const char *TAG = "web";

extern const char index_html_start[] asm("_binary_index_html_start");
extern const char index_html_end[] asm("_binary_index_html_end");

#ifdef CONFIG_PD_ALLOW_GCODE
#define ALLOW_GCODE "true"
#else
#define ALLOW_GCODE "false"
#endif

#define UPLOAD_BUF_SIZE (16 * 1024)
#define MAX_LIST        64

static uint8_t *s_upload_buf;

// ============================================================== helpers

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/** In-place URL decoding (%XX and '+'). Returns false on malformed input. */
static bool url_decode(char *s)
{
    char *o = s;
    for (char *p = s; *p; p++) {
        if (*p == '%') {
            const int h = hexval(p[1]), l = h < 0 ? -1 : hexval(p[2]);
            if (h < 0 || l < 0) {
                return false;
            }
            *o++ = (char)(h * 16 + l);
            p += 2;
        } else if (*p == '+') {
            *o++ = ' ';
        } else {
            *o++ = *p;
        }
    }
    *o = '\0';
    return true;
}

/** Read a decoded query parameter. Returns false if missing. */
static bool query_param(httpd_req_t *req, const char *key, char *out, size_t len)
{
    // The raw value is percent-encoded and can be up to 3x longer than the decoded one.
    char query[CONFIG_HTTPD_MAX_URI_LEN + 1];
    char raw[3 * DRIVE_FILE_NAME_MAX + 1];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
        httpd_query_key_value(query, key, raw, sizeof(raw)) != ESP_OK || !url_decode(raw) ||
        strlen(raw) >= len) {
        return false;
    }
    memcpy(out, raw, strlen(raw) + 1);
    return true;
}

static bool query_flag(httpd_req_t *req, const char *key)
{
    char v[8];
    return query_param(req, key, v, sizeof(v)) && (strcmp(v, "1") == 0 || strcmp(v, "true") == 0);
}

/** Append s as a JSON string literal (with quotes) to buf. */
static void json_str(char *buf, size_t len, const char *s)
{
    size_t n = strlen(buf);
    if (n + 2 >= len) {
        return;
    }
    buf[n++] = '"';
    for (const unsigned char *p = (const unsigned char *)s; *p && n + 7 < len; p++) {
        if (*p == '"' || *p == '\\') {
            buf[n++] = '\\';
            buf[n++] = (char)*p;
        } else if (*p < 0x20) {
            n += snprintf(buf + n, len - n, "\\u%04x", *p);
        } else {
            buf[n++] = (char)*p;
        }
    }
    buf[n++] = '"';
    buf[n] = '\0';
}

static void appendf(char *buf, size_t len, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
static void appendf(char *buf, size_t len, const char *fmt, ...)
{
    const size_t n = strlen(buf);
    if (n >= len) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf + n, len - n, fmt, ap);
    va_end(ap);
}

static const char *result_code(drive_result_t r)
{
    switch (r) {
    case DRIVE_OK:               return "OK";
    case DRIVE_ERR_BUSY:         return "BUSY";
    case DRIVE_ERR_HOST_ACTIVE:  return "HOST_ACTIVE";
    case DRIVE_ERR_HOST_WRITING: return "HOST_WRITING";
    case DRIVE_ERR_WRONG_OWNER:  return "WRONG_OWNER";
    case DRIVE_ERR_NO_FS:        return "NO_FS";
    case DRIVE_ERR_BAD_NAME:     return "BAD_NAME";
    case DRIVE_ERR_BAD_TYPE:     return "BAD_TYPE";
    case DRIVE_ERR_EXISTS:       return "EXISTS";
    case DRIVE_ERR_NOT_FOUND:    return "NOT_FOUND";
    case DRIVE_ERR_NO_SPACE:     return "NO_SPACE";
    case DRIVE_ERR_INVALID_FILE: return "INVALID_FILE";
    case DRIVE_ERR_INCOMPLETE:   return "INCOMPLETE";
    default:                     return "IO";
    }
}

static const char *result_http_status(drive_result_t r)
{
    switch (r) {
    case DRIVE_OK:               return "200 OK";
    case DRIVE_ERR_BAD_NAME:
    case DRIVE_ERR_INCOMPLETE:   return "400 Bad Request";
    case DRIVE_ERR_NOT_FOUND:    return "404 Not Found";
    case DRIVE_ERR_BAD_TYPE:     return "415 Unsupported Media Type";
    case DRIVE_ERR_INVALID_FILE: return "422 Unprocessable Entity";
    case DRIVE_ERR_NO_SPACE:     return "507 Insufficient Storage";
    case DRIVE_ERR_IO:           return "500 Internal Server Error";
    default:                     return "409 Conflict";
    }
}

static esp_err_t send_json(httpd_req_t *req, const char *status, const char *json)
{
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, json);
}

static esp_err_t send_result(httpd_req_t *req, drive_result_t r)
{
    char buf[400] = "";
    appendf(buf, sizeof(buf), "{\"ok\":%s,\"code\":\"%s\",\"error\":", r == DRIVE_OK ? "true" : "false",
            result_code(r));
    json_str(buf, sizeof(buf), r == DRIVE_OK ? "" : drive_result_str(r));
    appendf(buf, sizeof(buf), "}");
    return send_json(req, result_http_status(r), buf);
}

static esp_err_t send_error(httpd_req_t *req, const char *status, const char *code, const char *msg)
{
    char buf[300] = "";
    appendf(buf, sizeof(buf), "{\"ok\":false,\"code\":\"%s\",\"error\":", code);
    json_str(buf, sizeof(buf), msg);
    appendf(buf, sizeof(buf), "}");
    return send_json(req, status, buf);
}

/** CSRF protection for state-changing requests. Returns true if the request may proceed. */
static bool check_csrf(httpd_req_t *req)
{
    char v[4];
    if (httpd_req_get_hdr_value_str(req, "X-PrintDrive", v, sizeof(v)) == ESP_OK && strcmp(v, "1") == 0) {
        return true;
    }
    send_error(req, "403 Forbidden", "FORBIDDEN", "Header X-PrintDrive fehlt");
    return false;
}

static const char *owner_str(drive_owner_t o)
{
    return o == DRIVE_OWNER_USB ? "usb" : (o == DRIVE_OWNER_APP ? "app" : "none");
}

static long idle_json(uint32_t ms)
{
    return ms == UINT32_MAX ? -1 : (long)ms;
}

// ============================================================== handlers

static esp_err_t h_index(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
    return httpd_resp_send(req, index_html_start, index_html_end - index_html_start - 1);
}

static esp_err_t h_status(httpd_req_t *req)
{
    drive_status_t d;
    pd_wifi_status_t w;
    drive_get_status(&d);
    pd_wifi_get_status(&w);

    char buf[1400] = "";
    appendf(buf, sizeof(buf), "{\"version\":");
    json_str(buf, sizeof(buf), esp_app_get_description()->version);
    appendf(buf, sizeof(buf), ",\"uptime_s\":%lld,\"heap\":%lu,\"allow_gcode\":%s",
            esp_timer_get_time() / 1000000, (unsigned long)esp_get_free_heap_size(),
            ALLOW_GCODE);
    appendf(buf, sizeof(buf),
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
    appendf(buf, sizeof(buf), ",\"upload\":{\"active\":%s,\"name\":", d.upload_active ? "true" : "false");
    json_str(buf, sizeof(buf), d.upload_name);
    appendf(buf, sizeof(buf), ",\"received\":%lu,\"total\":%lu}", (unsigned long)d.upload_received,
            (unsigned long)d.upload_total);
    appendf(buf, sizeof(buf), ",\"wifi\":{\"sta_configured\":%s,\"sta_connected\":%s,\"ssid\":",
            w.sta_configured ? "true" : "false", w.sta_connected ? "true" : "false");
    json_str(buf, sizeof(buf), w.sta_ssid);
    appendf(buf, sizeof(buf), ",\"ip\":\"%s\",\"rssi\":%d,\"ap_active\":%s,\"ap_ssid\":", w.sta_ip, w.sta_rssi,
            w.ap_active ? "true" : "false");
    json_str(buf, sizeof(buf), w.ap_ssid);
    appendf(buf, sizeof(buf), ",\"ap_ip\":\"%s\",\"hostname\":", w.ap_ip);
    json_str(buf, sizeof(buf), w.hostname);
    appendf(buf, sizeof(buf), "}}");
    return send_json(req, "200 OK", buf);
}

static esp_err_t h_files(httpd_req_t *req)
{
    drive_file_t *files = calloc(MAX_LIST, sizeof(drive_file_t));
    if (!files) {
        return send_error(req, "500 Internal Server Error", "NO_MEM", "Kein Speicher");
    }
    size_t count = 0;
    const drive_result_t r = drive_list(files, MAX_LIST, &count);
    if (r != DRIVE_OK) {
        free(files);
        return send_result(req, r);
    }
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_sendstr_chunk(req, "{\"ok\":true,\"files\":[");
    char item[DRIVE_FILE_NAME_MAX * 2 + 64];
    for (size_t i = 0; i < count; i++) {
        item[0] = '\0';
        appendf(item, sizeof(item), "%s{\"name\":", i ? "," : "");
        json_str(item, sizeof(item), files[i].name);
        appendf(item, sizeof(item), ",\"size\":%lu}", (unsigned long)files[i].size);
        httpd_resp_sendstr_chunk(req, item);
    }
    snprintf(item, sizeof(item), "],\"truncated\":%s}", count == MAX_LIST ? "true" : "false");
    httpd_resp_sendstr_chunk(req, item);
    free(files);
    return httpd_resp_sendstr_chunk(req, NULL);
}

/** Validate an upload before sending data: name, type, existence, free space. */
static esp_err_t h_upload_check(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char name[DRIVE_NAME_MAX + 1], size_s[16];
    if (!query_param(req, "name", name, sizeof(name)) || !query_param(req, "size", size_s, sizeof(size_s))) {
        return send_result(req, DRIVE_ERR_BAD_NAME);
    }
    drive_result_t r = drive_check_name(name);
    if (r == DRIVE_OK) {
        // Begin + abort performs exactly the checks the real upload will do.
        r = drive_upload_begin(name, (uint32_t)strtoul(size_s, NULL, 10), query_flag(req, "overwrite"));
        if (r == DRIVE_OK) {
            drive_upload_abort();
        }
    }
    return send_result(req, r);
}

static esp_err_t h_upload(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char name[DRIVE_NAME_MAX + 1];
    if (!query_param(req, "name", name, sizeof(name))) {
        return send_result(req, DRIVE_ERR_BAD_NAME);
    }
    const size_t total = req->content_len;
    drive_result_t r = drive_upload_begin(name, (uint32_t)total, query_flag(req, "overwrite"));
    if (r != DRIVE_OK) {
        send_result(req, r);
        return ESP_FAIL;   // close the connection instead of draining a large body
    }

    const int64_t t0 = esp_timer_get_time();
    size_t remaining = total, fill = 0;
    int timeouts = 0;
    while (remaining > 0) {
        const size_t want = remaining < UPLOAD_BUF_SIZE - fill ? remaining : UPLOAD_BUF_SIZE - fill;
        const int n = httpd_req_recv(req, (char *)s_upload_buf + fill, want);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            ESP_LOGW(TAG, "upload connection lost (%d)", n);
            drive_upload_abort();
            return ESP_FAIL;   // client is gone, nothing to answer
        }
        timeouts = 0;
        fill += (size_t)n;
        remaining -= (size_t)n;
        if (fill == UPLOAD_BUF_SIZE || remaining == 0) {
            r = drive_upload_write(s_upload_buf, fill);
            fill = 0;
            if (r != DRIVE_OK) {
                drive_upload_abort();
                send_result(req, r);
                return ESP_FAIL;
            }
        }
    }
    r = drive_upload_finish();
    const int64_t dt_ms = (esp_timer_get_time() - t0) / 1000;
    ESP_LOGI(TAG, "upload '%s' %u bytes in %lld ms (%.1f KB/s): %s", name, (unsigned)total, dt_ms,
             dt_ms ? (double)total / dt_ms : 0.0, drive_result_str(r));
    return send_result(req, r);
}

static esp_err_t h_delete(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char name[DRIVE_FILE_NAME_MAX + 1];
    if (!query_param(req, "name", name, sizeof(name))) {
        return send_result(req, DRIVE_ERR_BAD_NAME);
    }
    return send_result(req, drive_delete(name));
}

static esp_err_t h_download(httpd_req_t *req)
{
    char name[DRIVE_FILE_NAME_MAX + 1];
    if (!query_param(req, "name", name, sizeof(name))) {
        return send_result(req, DRIVE_ERR_BAD_NAME);
    }
    int fd = -1;
    uint32_t size = 0;
    const drive_result_t r = drive_download_open(name, &fd, &size);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Content-Disposition", "attachment");
    esp_err_t err = ESP_OK;
    ssize_t n;
    while ((n = read(fd, s_upload_buf, UPLOAD_BUF_SIZE)) > 0) {
        if ((err = httpd_resp_send_chunk(req, (const char *)s_upload_buf, n)) != ESP_OK) {
            break;
        }
    }
    drive_download_close(fd);
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, NULL, 0);
    }
    return err;
}

static esp_err_t h_mode(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char target[8];
    if (!query_param(req, "target", target, sizeof(target))) {
        return send_error(req, "400 Bad Request", "BAD_REQUEST", "target fehlt");
    }
    drive_owner_t o;
    if (strcmp(target, "usb") == 0) {
        o = DRIVE_OWNER_USB;
    } else if (strcmp(target, "app") == 0) {
        o = DRIVE_OWNER_APP;
    } else {
        return send_error(req, "400 Bad Request", "BAD_REQUEST", "target muss usb oder app sein");
    }
    return send_result(req, drive_set_owner(o, query_flag(req, "force")));
}

static esp_err_t h_host_writable(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    drive_set_host_writable(query_flag(req, "value"));
    return send_result(req, DRIVE_OK);
}

static esp_err_t h_wifi(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char body[256] = "";
    if (req->content_len >= sizeof(body)) {
        return send_error(req, "400 Bad Request", "BAD_REQUEST", "Anfrage zu groß");
    }
    int got = 0;
    while (got < (int)req->content_len) {
        const int n = httpd_req_recv(req, body + got, req->content_len - got);
        if (n <= 0) {
            return ESP_FAIL;
        }
        got += n;
    }
    body[got] = '\0';
    char ssid[64] = "", pass[128] = "";
    if (httpd_query_key_value(body, "ssid", ssid, sizeof(ssid)) != ESP_OK || !url_decode(ssid)) {
        return send_error(req, "400 Bad Request", "BAD_REQUEST", "SSID fehlt");
    }
    if (httpd_query_key_value(body, "pass", pass, sizeof(pass)) == ESP_OK) {
        url_decode(pass);
    }
    if (pd_wifi_set_credentials(ssid, pass) != ESP_OK) {
        return send_error(req, "400 Bad Request", "BAD_REQUEST",
                          "Ungültig: SSID 1-32 Zeichen, Passwort leer oder 8-63 Zeichen");
    }
    return send_json(req, "200 OK", "{\"ok\":true}");
}

static esp_err_t h_format(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char confirm[16];
    if (!query_param(req, "confirm", confirm, sizeof(confirm)) || strcmp(confirm, "FORMAT") != 0) {
        return send_error(req, "400 Bad Request", "BAD_REQUEST", "Bestätigung fehlt");
    }
    // drive_format() restarts the device on success; answer first.
    drive_status_t d;
    drive_get_status(&d);
    if (d.owner != DRIVE_OWNER_APP) {
        return send_result(req, DRIVE_ERR_WRONG_OWNER);
    }
    send_json(req, "200 OK", "{\"ok\":true,\"restart\":true}");
    vTaskDelay(pdMS_TO_TICKS(200));
    const drive_result_t r = drive_format();
    ESP_LOGE(TAG, "format failed: %s", drive_result_str(r));
    return ESP_OK;
}

static esp_err_t h_reboot(httpd_req_t *req)
{
    if (!check_csrf(req)) {
        return ESP_OK;
    }
    char mode[16] = "normal";
    query_param(req, "mode", mode, sizeof(mode));
    const drive_result_t r = drive_prepare_restart(query_flag(req, "force"));
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    send_json(req, "200 OK", "{\"ok\":true}");
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
        return send_error(req, "500 Internal Server Error", "NO_MEM", "Kein Speicher");
    }
    const size_t n = applog_read(buf, 8193);
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    const esp_err_t err = httpd_resp_send(req, buf, n);
    free(buf);
    return err;
}

// ============================================================== start

esp_err_t web_server_start(void)
{
    s_upload_buf = malloc(UPLOAD_BUF_SIZE);
    ESP_RETURN_ON_FALSE(s_upload_buf, ESP_ERR_NO_MEM, TAG, "upload buffer");

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 10240;   // FatFs + LFN + URL/query buffers
    cfg.max_uri_handlers = 20;
    cfg.lru_purge_enable = true;
    cfg.recv_wait_timeout = 10;
    cfg.send_wait_timeout = 10;
    httpd_handle_t server = NULL;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &cfg), TAG, "httpd_start");

    const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = h_index},
        {.uri = "/index.html", .method = HTTP_GET, .handler = h_index},
        {.uri = "/api/status", .method = HTTP_GET, .handler = h_status},
        {.uri = "/api/files", .method = HTTP_GET, .handler = h_files},
        {.uri = "/api/download", .method = HTTP_GET, .handler = h_download},
        {.uri = "/api/log", .method = HTTP_GET, .handler = h_log},
        {.uri = "/api/upload/check", .method = HTTP_POST, .handler = h_upload_check},
        {.uri = "/api/upload", .method = HTTP_POST, .handler = h_upload},
        {.uri = "/api/delete", .method = HTTP_POST, .handler = h_delete},
        {.uri = "/api/mode", .method = HTTP_POST, .handler = h_mode},
        {.uri = "/api/settings/host_writable", .method = HTTP_POST, .handler = h_host_writable},
        {.uri = "/api/wifi", .method = HTTP_POST, .handler = h_wifi},
        {.uri = "/api/format", .method = HTTP_POST, .handler = h_format},
        {.uri = "/api/reboot", .method = HTTP_POST, .handler = h_reboot},
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uris[i]), TAG, "register %s", uris[i].uri);
    }
    ESP_LOGI(TAG, "web server started on port %d", cfg.server_port);
    return ESP_OK;
}
