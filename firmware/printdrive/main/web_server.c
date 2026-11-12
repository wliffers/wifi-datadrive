#include "web_server.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "auth.h"
#include "esp_check.h"
#include "esp_https_server.h"
#include "esp_log.h"
#include "lwip/sockets.h"
#include "sdkconfig.h"
#include "tls.h"
#include "web_common.h"
#include "web_auth.h"
#include "web_crypt.h"
#include "web_files.h"
#include "web_ota.h"
#include "web_static.h"
#include "web_system.h"
#include "web_wifi.h"

#ifdef CONFIG_PD_HTTPS_OPTION
#define PD_HTTPS 1
#else
#define PD_HTTPS 0   // see Kconfig: uploads fail with TLS on this board without PSRAM
#endif

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
    case DRIVE_ERR_LOCKED:       return "DRIVE_LOCKED";
    case DRIVE_ERR_AUTH:         return "CRYPT_WRONG_PASSWORD";
    case DRIVE_ERR_RETRY_LATER:  return "LOCKED";
    case DRIVE_ERR_WEAK_PASSWORD: return "CRYPT_WEAK_PASSWORD";
    case DRIVE_ERR_NOT_ENCRYPTED: return "CRYPT_NOT_ENABLED";
    case DRIVE_ERR_ALREADY_ENCRYPTED: return "CRYPT_ALREADY_ENABLED";
    case DRIVE_ERR_CRYPT_UNAVAILABLE: return "CRYPT_UNAVAILABLE";
    case DRIVE_ERR_CRYPT_DAMAGED: return "CRYPT_DAMAGED";
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
    case DRIVE_ERR_LOCKED:
    case DRIVE_ERR_CRYPT_DAMAGED: return "423 Locked";
    case DRIVE_ERR_AUTH:         return "403 Forbidden";
    case DRIVE_ERR_RETRY_LATER:  return "429 Too Many Requests";
    case DRIVE_ERR_WEAK_PASSWORD: return "400 Bad Request";
    case DRIVE_ERR_CRYPT_UNAVAILABLE: return "503 Service Unavailable";
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

/** A request was refused and already answered. A small body (form fields) is read and
 *  dropped so the client receives the answer instead of a connection reset; a large one
 *  (upload) is not worth reading: ESP_FAIL closes the connection. */
static esp_err_t rejected(httpd_req_t *req)
{
    if (req->content_len == 0) {
        return ESP_OK;
    }
    if (req->content_len > 4096) {
        return ESP_FAIL;
    }
    char tmp[128];
    size_t left = req->content_len;
    while (left > 0) {
        const int n = httpd_req_recv(req, tmp, left < sizeof(tmp) ? left : sizeof(tmp));
        if (n <= 0) {
            return ESP_FAIL;
        }
        left -= (size_t)n;
    }
    return ESP_OK;
}

/** Registered for every route (route in user_ctx): authorization, then CSRF, then the handler
 *  (an unauthenticated POST gets 401, as in the API contract and the mock server).
 *  Both checks have already answered the client when they fail, so we return ESP_OK. */
static esp_err_t dispatch(httpd_req_t *req)
{
    const web_route_t *route = req->user_ctx;
    if (!auth_authorize(req, route->role)) {
        return rejected(req);
    }
    if (req->method == HTTP_POST && !check_csrf(req)) {
        return rejected(req);
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

// ============================================================== HTTPS (optional, see tls.h)

static esp_err_t send_https_state(httpd_req_t *req)
{
    pd_tls_info_t t;
    pd_tls_get_info(&t);
    char buf[400] = "";
    web_appendf(buf, sizeof(buf),
                "{\"ok\":true,\"https\":%s,\"https_active\":%s,\"https_restart\":%s,\"cert_sha256\":\"%s\","
                "\"ca_sha256\":\"%s\",\"cert_expires\":\"%s\"}",
                t.enabled ? "true" : "false", t.active ? "true" : "false", t.restart_pending ? "true" : "false",
                t.cert_sha256, t.ca_sha256, t.cert_expires);
    return web_send_json(req, "200 OK", buf);
}

/** POST /api/settings/https?value=0|1 [&renew=1] [&regenerate=1] (admin). Takes effect after a
 *  restart. renew: new server certificate (current IP), same CA. regenerate: new CA too. */
static esp_err_t h_settings_https(httpd_req_t *req)
{
    char v[8];
    const bool has_value = web_query_param(req, "value", v, sizeof(v));
    const bool regen = web_query_flag(req, "regenerate");
    const bool renew = web_query_flag(req, "renew");
    if (!has_value && !regen && !renew) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "value, renew oder regenerate fehlt");
    }
    esp_err_t err = ESP_OK;
    if (regen) {
        err = pd_tls_regenerate();
    } else if (renew) {
        err = pd_tls_renew_cert();
    }
    if (err == ESP_OK && has_value) {
        err = pd_tls_set_enabled(strcmp(v, "1") == 0 || strcmp(v, "true") == 0);
    }
    if (err != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Zertifikat konnte nicht erzeugt werden");
    }
    return send_https_state(req);
}

/** GET /api/https/ca.cer: the device's root certificate (DER) for installing it on clients. */
static esp_err_t h_https_ca(httpd_req_t *req)
{
    uint8_t *der;
    size_t len;
    if (pd_tls_read_ca(&der, &len) != ESP_OK) {
        return web_send_error(req, "404 Not Found", "NOT_FOUND", "Noch kein Zertifikat vorhanden");
    }
    // iOS offers to install a profile for this type; Windows opens the certificate dialog.
    httpd_resp_set_type(req, "application/x-x509-ca-cert");
    httpd_resp_set_hdr(req, "Content-Disposition", "attachment; filename=\"DataDrive-CA.cer\"");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    const esp_err_t sent = httpd_resp_send(req, (const char *)der, len);
    free(der);
    return sent;
}

static const web_route_t s_https_routes[] = {
    {"/api/settings/https", HTTP_POST, h_settings_https, WEB_ROLE_ADMIN},
    {"/api/https/ca.cer", HTTP_GET, h_https_ca, WEB_ROLE_USER},
};

/** Host for the redirect: the request's Host header (without port) if it is a plain host name
 *  or IPv4 address, else the local IPv4 address of the connection. */
static bool redirect_host(httpd_req_t *req, char *host, size_t len)
{
    if (httpd_req_get_hdr_value_str(req, "Host", host, len) == ESP_OK) {
        char *colon = strchr(host, ':');
        if (colon) {
            *colon = '\0';
        }
        bool ok = host[0] != '\0';
        for (const char *p = host; *p && ok; p++) {
            ok = (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '.' ||
                 *p == '-';
        }
        if (ok) {
            return true;
        }
    }
    struct sockaddr_storage addr;
    socklen_t alen = sizeof(addr);
    if (getsockname(httpd_req_to_sockfd(req), (struct sockaddr *)&addr, &alen) != 0) {
        return false;
    }
    const uint8_t *ip = NULL;
    if (addr.ss_family == AF_INET) {
        ip = (const uint8_t *)&((struct sockaddr_in *)&addr)->sin_addr.s_addr;
#if CONFIG_LWIP_IPV6
    } else if (addr.ss_family == AF_INET6) {
        const uint8_t *a = (const uint8_t *)&((struct sockaddr_in6 *)&addr)->sin6_addr;
        static const uint8_t v4mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF};
        if (memcmp(a, v4mapped, sizeof(v4mapped)) == 0) {
            ip = a + 12;
        }
#endif
    }
    if (!ip) {
        return false;
    }
    snprintf(host, len, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
    return true;
}

/** Port 80 while HTTPS is on: every request is answered with a redirect to https://.
 *  307 (temporary, not cached by browsers) so that switching HTTPS off again works at once.
 *  A request body is never read: the connection is closed instead. */
static esp_err_t h_redirect(httpd_req_t *req)
{
    char loc[CONFIG_HTTPD_MAX_URI_LEN + 80];   // on the stack: no static RAM while HTTPS is off
    char host[64];
    if (!redirect_host(req, host, sizeof(host))) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Use https://");
        return ESP_FAIL;
    }
    snprintf(loc, sizeof(loc), "https://%s%s", host, req->uri);
    httpd_resp_set_status(req, "307 Temporary Redirect");
    httpd_resp_set_hdr(req, "Location", loc);
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    if (req->content_len > 0) {
        httpd_resp_set_hdr(req, "Connection", "close");
    }
    httpd_resp_send(req, NULL, 0);
    return req->content_len > 0 ? ESP_FAIL : ESP_OK;
}

static esp_err_t start_redirect_server(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.ctrl_port = ESP_HTTPD_DEF_CTRL_PORT + 1;   // the HTTPS server uses the default one
    cfg.stack_size = 5120;   // h_redirect keeps the Location URL (~1.1 KB) on the stack
    cfg.max_open_sockets = 2;
    cfg.max_uri_handlers = 5;
    cfg.lru_purge_enable = true;
    cfg.recv_wait_timeout = 5;
    cfg.send_wait_timeout = 5;
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    httpd_handle_t server = NULL;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &cfg), TAG, "redirect server");
    static const httpd_method_t methods[] = {HTTP_GET, HTTP_HEAD, HTTP_POST, HTTP_PUT, HTTP_DELETE};
    for (size_t i = 0; i < sizeof(methods) / sizeof(methods[0]); i++) {
        const httpd_uri_t uri = {.uri = "/*", .method = methods[i], .handler = h_redirect};
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &uri), TAG, "redirect handler");
    }
    return ESP_OK;
}

/**
 * Start the HTTPS server on 443. Memory: one TLS session is a few KB while idle (mbedTLS
 * dynamic buffers) and about 21 KB more while a record is being received/sent, so the number
 * of open connections is kept small; the server handles one request at a time anyway.
 */
static esp_err_t start_https_server(const httpd_config_t *base, httpd_handle_t *server)
{
    const uint8_t *crt, *key;
    size_t crt_len, key_len;
    if (!pd_tls_server_credentials(&crt, &crt_len, &key, &key_len)) {
        return ESP_ERR_INVALID_STATE;
    }
    httpd_ssl_config_t cfg = HTTPD_SSL_CONFIG_DEFAULT();
    cfg.httpd = *base;
    cfg.httpd.stack_size = 12288;     // handlers + mbedTLS record processing
    cfg.httpd.max_open_sockets = 3;   // each open TLS session costs heap
    cfg.servercert = crt;
    cfg.servercert_len = crt_len;
    cfg.prvtkey_pem = key;            // DER is accepted as well
    cfg.prvtkey_len = key_len;
    cfg.transport_mode = HTTPD_SSL_TRANSPORT_SECURE;
    cfg.port_secure = 443;
    cfg.session_tickets = true;       // browsers resume instead of a full handshake per connection
    cfg.tls_handshake_timeout_ms = 8000;
    const esp_err_t err = httpd_ssl_start(server, &cfg);
    pd_tls_release_server_credentials();   // the server keeps its own copy
    return err;
}

// ============================================================== start

esp_err_t web_server_start(void)
{
    s_io_buf = malloc(WEB_IO_BUF_SIZE);
    ESP_RETURN_ON_FALSE(s_io_buf, ESP_ERR_NO_MEM, TAG, "I/O buffer");

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 10240;   // FatFs + LFN + URL/query buffers
    cfg.max_uri_handlers = 56;   // 47 routes (static 6, files 9, system 6, wifi 5, crypt 8, auth 10, ota 1, https 2)
    cfg.lru_purge_enable = true;
    cfg.recv_wait_timeout = 10;
    cfg.send_wait_timeout = 10;
    httpd_handle_t server = NULL;

    // HTTPS if enabled; on any failure plain HTTP as before, so the device stays reachable.
    const esp_err_t tls_err = pd_tls_init();
    if (PD_HTTPS && pd_tls_enabled()) {
        esp_err_t err = tls_err == ESP_OK ? start_https_server(&cfg, &server) : tls_err;
        if (err == ESP_OK) {
            pd_tls_set_active(true);
            ESP_LOGI(TAG, "HTTPS server started on port 443");
            if (start_redirect_server() != ESP_OK) {
                ESP_LOGE(TAG, "port 80 redirect not available");
            }
        } else {
            ESP_LOGE(TAG, "HTTPS start failed (%s) - falling back to HTTP", esp_err_to_name(err));
            server = NULL;
            pd_tls_release_server_credentials();
        }
    }
    if (!server) {
        ESP_RETURN_ON_ERROR(httpd_start(&server, &cfg), TAG, "httpd_start");
        ESP_LOGI(TAG, "web server started on port %d", cfg.server_port);
    }

    ESP_RETURN_ON_ERROR(web_static_register(server), TAG, "static routes");
    ESP_RETURN_ON_ERROR(web_files_register(server), TAG, "file routes");
    ESP_RETURN_ON_ERROR(web_system_register(server), TAG, "system routes");
    ESP_RETURN_ON_ERROR(web_wifi_register(server), TAG, "wifi routes");
    ESP_RETURN_ON_ERROR(web_crypt_register(server), TAG, "crypt routes");
    ESP_RETURN_ON_ERROR(web_auth_register(server), TAG, "auth routes");
    ESP_RETURN_ON_ERROR(web_ota_register(server), TAG, "ota routes");
    if (PD_HTTPS) {
        ESP_RETURN_ON_ERROR(web_register_routes(server, s_https_routes, sizeof(s_https_routes) / sizeof(s_https_routes[0])),
                            TAG, "https routes");
    }
    return ESP_OK;
}
