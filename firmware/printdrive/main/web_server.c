#include "web_server.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "auth.h"
#include "esp_check.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "web_common.h"
#include "web_auth.h"
#include "web_files.h"
#include "web_ota.h"
#include "web_static.h"
#include "web_system.h"
#include "web_wifi.h"

static const char *TAG = "web";

static uint8_t *s_io_buf;

uint8_t *web_io_buf(void)
{
    return s_io_buf;
}

// ============================================================== helpers

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool web_url_decode(char *s)
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

bool web_query_param(httpd_req_t *req, const char *key, char *out, size_t len)
{
    // The raw value is percent-encoded and can be up to 3x longer than the decoded one.
    char query[CONFIG_HTTPD_MAX_URI_LEN + 1];
    char raw[3 * DRIVE_FILE_NAME_MAX + 1];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
        httpd_query_key_value(query, key, raw, sizeof(raw)) != ESP_OK || !web_url_decode(raw) ||
        strlen(raw) >= len) {
        return false;
    }
    memcpy(out, raw, strlen(raw) + 1);
    return true;
}

bool web_query_flag(httpd_req_t *req, const char *key)
{
    char v[8];
    return web_query_param(req, key, v, sizeof(v)) && (strcmp(v, "1") == 0 || strcmp(v, "true") == 0);
}

esp_err_t web_read_body(httpd_req_t *req, char *buf, size_t len)
{
    if (req->content_len >= len) {
        web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Anfrage zu groß");
        return ESP_ERR_INVALID_SIZE;
    }
    int got = 0;
    while (got < (int)req->content_len) {
        const int n = httpd_req_recv(req, buf + got, req->content_len - got);
        if (n <= 0) {
            return ESP_FAIL;
        }
        got += n;
    }
    buf[got] = '\0';
    return ESP_OK;
}

void web_json_str(char *buf, size_t len, const char *s)
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

void web_appendf(char *buf, size_t len, const char *fmt, ...)
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

const char *web_result_code(drive_result_t r)
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
    case DRIVE_ERR_TOO_LARGE:    return "TOO_LARGE";
    case DRIVE_ERR_DIR_NOT_EMPTY: return "DIR_NOT_EMPTY";
    case DRIVE_ERR_IS_DIR:       return "IS_DIR";
    default:                     return "IO";
    }
}

const char *web_result_http_status(drive_result_t r)
{
    switch (r) {
    case DRIVE_OK:               return "200 OK";
    case DRIVE_ERR_BAD_NAME:
    case DRIVE_ERR_IS_DIR:
    case DRIVE_ERR_INCOMPLETE:   return "400 Bad Request";
    case DRIVE_ERR_DIR_NOT_EMPTY: return "409 Conflict";
    case DRIVE_ERR_TOO_LARGE:    return "413 Payload Too Large";
    case DRIVE_ERR_NOT_FOUND:    return "404 Not Found";
    case DRIVE_ERR_BAD_TYPE:     return "415 Unsupported Media Type";
    case DRIVE_ERR_INVALID_FILE: return "422 Unprocessable Entity";
    case DRIVE_ERR_NO_SPACE:     return "507 Insufficient Storage";
    case DRIVE_ERR_IO:           return "500 Internal Server Error";
    default:                     return "409 Conflict";
    }
}

esp_err_t web_send_json(httpd_req_t *req, const char *status, const char *json)
{
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, json);
}

esp_err_t web_send_result(httpd_req_t *req, drive_result_t r)
{
    char buf[400] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":%s,\"code\":\"%s\",\"error\":", r == DRIVE_OK ? "true" : "false",
                web_result_code(r));
    web_json_str(buf, sizeof(buf), r == DRIVE_OK ? "" : drive_result_str(r));
    web_appendf(buf, sizeof(buf), "}");
    return web_send_json(req, web_result_http_status(r), buf);
}

esp_err_t web_send_error(httpd_req_t *req, const char *status, const char *code, const char *msg)
{
    char buf[300] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":false,\"code\":\"%s\",\"error\":", code);
    web_json_str(buf, sizeof(buf), msg);
    web_appendf(buf, sizeof(buf), "}");
    return web_send_json(req, status, buf);
}

// ============================================================== dispatcher

/** CSRF protection for state-changing requests. Returns true if the request may proceed. */
static bool check_csrf(httpd_req_t *req)
{
    char v[4];
    if (httpd_req_get_hdr_value_str(req, "X-PrintDrive", v, sizeof(v)) == ESP_OK && strcmp(v, "1") == 0) {
        return true;
    }
    web_send_error(req, "403 Forbidden", "FORBIDDEN", "Anfrage nicht erlaubt.");
    return false;
}

/** Registered for every route (route in user_ctx): authorization, then CSRF, then the handler
 *  (an unauthenticated POST gets 401, as in the API contract and the mock server).
 *  Both checks have already answered the client when they fail, so we return ESP_OK. */
static esp_err_t dispatch(httpd_req_t *req)
{
    const web_route_t *route = req->user_ctx;
    if (!auth_authorize(req, route->role)) {
        return ESP_OK;
    }
    if (req->method == HTTP_POST && !check_csrf(req)) {
        return ESP_OK;
    }
    return route->handler(req);
}

esp_err_t web_register_routes(httpd_handle_t server, const web_route_t *routes, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        const httpd_uri_t uri = {
            .uri = routes[i].uri,
            .method = routes[i].method,
            .handler = dispatch,
            .user_ctx = (void *)&routes[i],
        };
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri), TAG, "register %s", routes[i].uri);
    }
    return ESP_OK;
}

// ============================================================== start

esp_err_t web_server_start(void)
{
    s_io_buf = malloc(WEB_IO_BUF_SIZE);
    ESP_RETURN_ON_FALSE(s_io_buf, ESP_ERR_NO_MEM, TAG, "I/O buffer");

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 10240;   // FatFs + LFN + URL/query buffers
    cfg.max_uri_handlers = 48;
    cfg.lru_purge_enable = true;
    cfg.recv_wait_timeout = 10;
    cfg.send_wait_timeout = 10;
    httpd_handle_t server = NULL;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &cfg), TAG, "httpd_start");

    ESP_RETURN_ON_ERROR(web_static_register(server), TAG, "static routes");
    ESP_RETURN_ON_ERROR(web_files_register(server), TAG, "file routes");
    ESP_RETURN_ON_ERROR(web_system_register(server), TAG, "system routes");
    ESP_RETURN_ON_ERROR(web_wifi_register(server), TAG, "wifi routes");
    ESP_RETURN_ON_ERROR(web_auth_register(server), TAG, "auth routes");
    ESP_RETURN_ON_ERROR(web_ota_register(server), TAG, "ota routes");
    ESP_LOGI(TAG, "web server started on port %d", cfg.server_port);
    return ESP_OK;
}
