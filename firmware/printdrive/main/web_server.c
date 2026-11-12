#include "web_server.h"

#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/time.h>
#include "auth.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_https_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "tls.h"
#include "web_common.h"
#include "web_auth.h"
#include "web_crypt.h"
#include "web_dav.h"
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
    if (web_dav_route(route)) {
        // WebDAV (/dav, OPTIONS/PROPFIND on "/"): HTTP Basic only, never the session cookie, and no
        // CSRF header (clients cannot send it; no POST there). Why that is safe: web_dav.c, top.
        return web_dav_authorize(req, route->role) ? route->handler(req) : rejected(req);
    }
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

// ------------------------------------------------------------- port 80 while HTTPS is on
//
// No second esp_http_server instance (5 KiB stack + ~2 KiB state + up to five sockets): one small
// task with a blocking listen socket answers every request with a 307 to https:// and closes the
// connection. 307 (temporary, not cached by browsers) so that switching HTTPS off works at once.
// A request body is never read. Only created while HTTPS runs: no RAM at all otherwise.

#define REDIR_STACK     3584
#define REDIR_REQ_SIZE  512     // request line + headers read; a longer target redirects to "/"
#define REDIR_RESP_SIZE 768     // 51 + host (< 64) + target (< 512) + 72 always fit
#define REDIR_TIMEOUT_S 3

/** Local IPv4 address of the connection (dual-stack socket: IPv4 shows up as ::ffff:a.b.c.d). */
static bool redirect_local_ip(int fd, char out[16])
{
    struct sockaddr_storage addr;
    socklen_t alen = sizeof(addr);
    if (getsockname(fd, (struct sockaddr *)&addr, &alen) != 0) {
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
    size_t n = 0;   // no printf: keeps the task stack small
    for (int i = 0; i < 4; i++) {
        const unsigned v = ip[i];
        if (v >= 100) {
            out[n++] = (char)('0' + v / 100);
        }
        if (v >= 10) {
            out[n++] = (char)('0' + v / 10 % 10);
        }
        out[n++] = (char)('0' + v % 10);
        if (i < 3) {
            out[n++] = '.';
        }
    }
    out[n] = '\0';
    return true;
}

/** Request target of the request line if it is a plain origin-form path, else "/". */
static void redirect_target(const char *req, const char **target, size_t *len)
{
    *target = "/";
    *len = 1;
    const char *eol = strstr(req, "\r\n");
    const char *sp = strchr(req, ' ');
    if (!eol || !sp || sp > eol || sp[1] != '/') {
        return;
    }
    const char *s = sp + 1, *e = s;
    while (e < eol && *e != ' ') {
        if ((unsigned char)*e <= 0x20 || (unsigned char)*e >= 0x7F) {
            return;   // never copy control characters into the Location header
        }
        e++;
    }
    if (e == eol) {
        return;       // no " HTTP/x.y" in what was read
    }
    *target = s;
    *len = (size_t)(e - s);
}

/** Host header without port and trailing dot if it is a plain name or IPv4 address, else "". */
static void redirect_find_host(const char *req, char host[64])
{
    host[0] = '\0';
    for (const char *p = strstr(req, "\r\n"); p; p = strstr(p, "\r\n")) {
        p += 2;
        if (p[0] == '\r' || p[0] == '\0') {
            return;   // end of the headers (or of what was read)
        }
        if (strncasecmp(p, "Host:", 5) != 0) {
            continue;
        }
        p += 5;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        const char *end = strstr(p, "\r\n");
        if (!end) {
            return;   // cut off
        }
        size_t n = 0;
        while (p + n < end && p[n] != ':') {
            const char ch = p[n];
            const bool ok = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
                            ch == '.' || ch == '-';
            if (!ok || n >= 63) {
                return;
            }
            n++;
        }
        if (n > 0 && p[n - 1] == '.') {
            n--;      // fully qualified form "name."
        }
        memcpy(host, p, n);
        host[n] = '\0';
        return;
    }
}

/** Names of the stick itself: an IPv4 address, <hostname>, <hostname>.local. */
static bool redirect_host_is_ours(const char *h, const pd_wifi_status_t *w)
{
    const size_t n = strlen(h), hl = strlen(w->hostname);
    if (strspn(h, "0123456789.") == n) {
        return true;
    }
    if (hl == 0) {
        return false;
    }
    return strcasecmp(h, w->hostname) == 0 ||
           (n == hl + 6 && strncasecmp(h, w->hostname, hl) == 0 && strcasecmp(h + hl, ".local") == 0);
}

static void redirect_send(int fd, const char *s, size_t n)
{
    while (n > 0) {
        const ssize_t k = send(fd, s, n, 0);
        if (k <= 0) {
            return;
        }
        s += k;
        n -= (size_t)k;
    }
}

static size_t redirect_put(char *buf, size_t pos, const char *s, size_t n)
{
    if (pos + n >= REDIR_RESP_SIZE) {
        return pos;   // cannot happen (sizes above), but never overflow
    }
    memcpy(buf + pos, s, n);
    return pos + n;
}

static void redirect_answer(int fd, char *req, char *resp)
{
    size_t n = 0;
    while (n < REDIR_REQ_SIZE - 1) {
        const ssize_t got = recv(fd, req + n, REDIR_REQ_SIZE - 1 - n, 0);
        if (got <= 0) {
            break;
        }
        n += (size_t)got;
        req[n] = '\0';
        if (strstr(req, "\r\n\r\n")) {
            break;
        }
    }
    req[n] = '\0';
    if (n == 0) {
        return;       // nothing arrived (timeout or closed)
    }
    pd_wifi_status_t w;
    pd_wifi_get_status(&w);
    char host[64], ip[16];
    const bool have_ip = redirect_local_ip(fd, ip);
    const char *target;
    size_t tlen;
    redirect_target(req, &target, &tlen);
    redirect_find_host(req, host);
    if (host[0] && !redirect_host_is_ours(host, &w)) {
        // A foreign name: a captive-portal check (the setup AP's DNS answers every name with the
        // stick) or a DNS alias. The certificate only names the stick, so go to its address -
        // on the setup AP to the setup page, as web_static_not_found() does without HTTPS.
        const bool via_ap = have_ip && w.ap_active && w.ap_ip[0] && strcmp(ip, w.ap_ip) == 0;
        host[0] = '\0';
        target = via_ap ? "/?captive=1" : "/";
        tlen = strlen(target);
    }
    if (!host[0]) {
        if (!have_ip) {
            static const char bad[] = "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n"
                                      "Content-Length: 12\r\nConnection: close\r\n\r\nUse https://";
            redirect_send(fd, bad, sizeof(bad) - 1);
            return;
        }
        memcpy(host, ip, strlen(ip) + 1);
    }
    static const char head[] = "HTTP/1.1 307 Temporary Redirect\r\nLocation: https://";
    static const char tail[] = "\r\nCache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
    size_t len = redirect_put(resp, 0, head, sizeof(head) - 1);
    len = redirect_put(resp, len, host, strlen(host));
    len = redirect_put(resp, len, target, tlen);
    len = redirect_put(resp, len, tail, sizeof(tail) - 1);
    redirect_send(fd, resp, len);
}

static void redirect_task(void *arg)
{
    const int lfd = (int)(intptr_t)arg;
    char req[REDIR_REQ_SIZE], resp[REDIR_RESP_SIZE];
    while (true) {
        const int fd = accept(lfd, NULL, NULL);
        if (fd < 0) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }
        const struct timeval tv = {.tv_sec = REDIR_TIMEOUT_S, .tv_usec = 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        redirect_answer(fd, req, resp);
        closesocket(fd);
    }
}

static esp_err_t start_redirect_server(void)
{
#if CONFIG_LWIP_IPV6
    const int fd = socket(PF_INET6, SOCK_STREAM, 0);
    struct in6_addr inaddr_any = IN6ADDR_ANY_INIT;
    struct sockaddr_in6 addr = {
        .sin6_family = PF_INET6,
        .sin6_addr = inaddr_any,
        .sin6_port = htons(80),
    };
#else
    const int fd = socket(PF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr = {
        .sin_family = PF_INET,
        .sin_addr = {.s_addr = htonl(INADDR_ANY)},
        .sin_port = htons(80),
    };
#endif
    if (fd < 0) {
        ESP_LOGE(TAG, "port 80: no socket (%d)", errno);
        return ESP_FAIL;
    }
    const int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(fd, 4) != 0) {
        ESP_LOGE(TAG, "port 80: bind/listen failed (%d)", errno);
        closesocket(fd);
        return ESP_FAIL;
    }
    if (xTaskCreate(redirect_task, "http80", REDIR_STACK, (void *)(intptr_t)fd, tskIDLE_PRIORITY + 4, NULL) != pdPASS) {
        closesocket(fd);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

/** TLS session opened/closed (HTTPS server task): heap figures for the measurements in the log
 *  (/api/log), at most one line per 5 s. */
static void https_session_cb(esp_https_server_user_cb_arg_t *arg)
{
    static int64_t s_last_us = INT64_MIN / 2;
    const int64_t now = esp_timer_get_time();
    if (now - s_last_us < 5000000) {
        return;
    }
    s_last_us = now;
    ESP_LOGI(TAG, "TLS session %s: heap free %u, largest block %u, lowest since boot %u, buffer misses %lu",
             arg->user_cb_state == HTTPD_SSL_USER_CB_SESS_CREATE ? "open" : "closed",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
             (unsigned)esp_get_minimum_free_heap_size(), (unsigned long)pd_tls_buffer_misses());
}

/**
 * Start the HTTPS server on 443. Memory (CONFIG_MBEDTLS_DYNAMIC_BUFFER off): each connection has
 * fixed record buffers (16.3 KiB in + 4.3 KiB out) for its whole life. They are reserved once at
 * boot (pd_tls_reserve_buffers()), and the server keeps ONE connection: a new one closes the idle
 * one first (LRU purge, close before accept), so the reserved buffers are always free for it.
 * Per connection the heap still provides ~6 KiB in small blocks (contexts, parsed certificate),
 * plus ~6 KiB more during a full handshake.
 */
static esp_err_t start_https_server(const httpd_config_t *base, httpd_handle_t *server)
{
    const uint8_t *crt, *key;
    size_t crt_len, key_len;
    if (!pd_tls_server_credentials(&crt, &crt_len, &key, &key_len)) {
        return ESP_ERR_INVALID_STATE;
    }
    const esp_err_t pool = pd_tls_reserve_buffers();
    if (pool != ESP_OK) {
        ESP_LOGW(TAG, "TLS buffers not reserved (%s): taken from the heap per connection", esp_err_to_name(pool));
    }
    httpd_ssl_config_t cfg = HTTPD_SSL_CONFIG_DEFAULT();
    cfg.httpd = *base;
    cfg.httpd.stack_size = 12288;       // handlers + mbedTLS record processing
    // One TLS connection: the web UI sends one request at a time anyway; parallel browser
    // connections (page load) close the idle one and are retried by the browser.
    cfg.httpd.max_open_sockets = 1;
    cfg.httpd.lru_purge_enable = true;
    // Detect dead peers (phone left the network) after ~25 s idle instead of holding the slot.
    cfg.httpd.keep_alive_enable = true;
    cfg.httpd.keep_alive_idle = 10;
    cfg.httpd.keep_alive_interval = 5;
    cfg.httpd.keep_alive_count = 3;
    // recv/send timeouts stay as for HTTP (10 s): under TLS a single receive timeout already
    // ends an upload (esp-tls does not report it as HTTPD_SOCK_ERR_TIMEOUT).
    cfg.servercert = crt;
    cfg.servercert_len = crt_len;
    cfg.prvtkey_pem = key;              // DER is accepted as well
    cfg.prvtkey_len = key_len;
    cfg.transport_mode = HTTPD_SSL_TRANSPORT_SECURE;
    cfg.port_secure = 443;
    cfg.session_tickets = true;         // browsers resume (no ECDHE/ECDSA) instead of a full handshake
    cfg.tls_handshake_timeout_ms = 5000;   // a client that stalls in the handshake blocks the server
    cfg.user_cb = https_session_cb;
    const esp_err_t err = httpd_ssl_start(server, &cfg);
    pd_tls_release_server_credentials();   // the server keeps its own copy
    if (err != ESP_OK) {
        pd_tls_release_buffers();          // plain HTTP follows: give the memory back
    }
    return err;
}

// ============================================================== start

esp_err_t web_server_start(void)
{
    s_io_buf = malloc(WEB_IO_BUF_SIZE);
    ESP_RETURN_ON_FALSE(s_io_buf, ESP_ERR_NO_MEM, TAG, "I/O buffer");

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 10240;   // FatFs + LFN + URL/query buffers
    cfg.max_uri_handlers = 80;   // 70 routes with HTTPS (auth 10, crypt 8, files 11, ota 1, https 2, static 6, system 10, wifi 5, webdav 15, zip in files)
    // Wildcard templates (WebDAV "/dav/?*"). Templates without '*'/'?' still match exactly as with
    // the default matcher (same length + same bytes of the path without the query string).
    cfg.uri_match_fn = httpd_uri_match_wildcard;
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
    ESP_RETURN_ON_ERROR(web_dav_register(server), TAG, "webdav routes");   // before any catch-all route
    // Unknown URI (404) or method (405, e.g. next to wildcard routes): captive portal redirect on
    // the setup access point for foreign hosts, else the HTTP server's default answer.
    ESP_RETURN_ON_ERROR(httpd_register_err_handler(server, HTTPD_404_NOT_FOUND, web_static_not_found), TAG, "404 handler");
    ESP_RETURN_ON_ERROR(httpd_register_err_handler(server, HTTPD_405_METHOD_NOT_ALLOWED, web_static_not_found), TAG, "405 handler");
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
