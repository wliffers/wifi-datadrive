#pragma once

/**
 * Shared infrastructure of the HTTP server: request helpers, JSON helpers, the route table and
 * the central dispatcher. Feature modules (web_files.c, web_system.c, ...) only declare a
 * route table and call web_register_routes().
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "drive.h"
#include "esp_err.h"
#include "esp_http_server.h"

/** Minimum role needed for a route. Checked by auth_authorize() (see auth.h). */
typedef enum {
    WEB_ROLE_NONE = 0,   // public, no login needed
    WEB_ROLE_USER,
    WEB_ROLE_ADMIN,
} web_role_t;

/** One HTTP route. The table passed to web_register_routes() must stay valid (use static). */
typedef struct {
    const char *uri;
    httpd_method_t method;
    esp_err_t (*handler)(httpd_req_t *req);
    web_role_t role;
} web_route_t;

/**
 * Register all routes with one central dispatcher. For every request the dispatcher
 * (1) enforces the CSRF header on POST, (2) calls auth_authorize() with the route's role and
 * (3) only then calls the route's handler. Handlers therefore must not check CSRF/auth.
 */
esp_err_t web_register_routes(httpd_handle_t server, const web_route_t *routes, size_t count);

// ------------------------------------------------------------------ shared I/O buffer

#define WEB_IO_BUF_SIZE (16 * 1024)

/** One 16 KiB buffer shared by all handlers (upload, download, drain). The HTTP server is
 *  single-threaded in practice (one active big transfer), so handlers must not hold it
 *  across requests. NULL before web_server_start(). */
uint8_t *web_io_buf(void);

// ------------------------------------------------------------------ request helpers

/** In-place URL decoding (%XX and '+'). Returns false on malformed input. */
bool web_url_decode(char *s);

/** Read a decoded query parameter. Returns false if missing or too long for len. */
bool web_query_param(httpd_req_t *req, const char *key, char *out, size_t len);

/** True if the query parameter is "1" or "true". */
bool web_query_flag(httpd_req_t *req, const char *key);

/** Read a small request body (e.g. urlencoded form) completely into buf and terminate it.
 *  Returns ESP_OK; ESP_ERR_INVALID_SIZE if the body does not fit (a 400 answer has already been
 *  sent, the handler should return ESP_OK); ESP_FAIL if the connection broke (nothing to answer). */
esp_err_t web_read_body(httpd_req_t *req, char *buf, size_t len);

// ------------------------------------------------------------------ response helpers

/** Append s as a JSON string literal (with quotes) to buf. */
void web_json_str(char *buf, size_t len, const char *s);

/** printf-style append to the string in buf (size len). */
void web_appendf(char *buf, size_t len, const char *fmt, ...) __attribute__((format(printf, 3, 4)));

/** Send a JSON document with the given status line ("200 OK") and Cache-Control: no-store. */
esp_err_t web_send_json(httpd_req_t *req, const char *status, const char *json);

/** Send {"ok":..,"code":..,"error":..} for a drive result, with the matching HTTP status. */
esp_err_t web_send_result(httpd_req_t *req, drive_result_t r);

/** Send {"ok":false,"code":code,"error":msg} with the given status line. */
esp_err_t web_send_error(httpd_req_t *req, const char *status, const char *code, const char *msg);

/** Result -> short machine-readable code ("OK", "BUSY", ...). */
const char *web_result_code(drive_result_t r);

/** Result -> HTTP status line ("409 Conflict", ...). */
const char *web_result_http_status(drive_result_t r);
