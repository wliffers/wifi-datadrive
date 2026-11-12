// Login, session and user management routes (package P1, API contract v3 section 2.5).
// Authorization (role) and CSRF are enforced by the central dispatcher; the per-account rules
// (own password, last admin, ...) live in auth.c.
#include "web_auth.h"

#include <stdio.h>
#include <string.h>
#include "auth.h"
#include "mbedtls/platform_util.h"
#include "sdkconfig.h"
#include "tls.h"
#include "web_common.h"

/** Cookie attribute suffix: "; Secure" while the web UI runs over HTTPS. */
#define COOKIE_SECURE() (pd_tls_active() ? "; Secure" : "")

#define FORM_MAX  512   // urlencoded body limit (like the mock server)
#define FIELD_MAX 200   // raw (still encoded) value: 64 password bytes need up to 192 chars
#define ROLE_MAX  16

typedef enum { FIELD_OK, FIELD_MISSING, FIELD_BAD, FIELD_LONG } field_t;

/** Read and URL-decode key from an urlencoded string (body or query). FIELD_LONG if the value
 *  does not fit (out is "" then), FIELD_BAD if it cannot be decoded or contains %00. */
static field_t get_field(const char *src, const char *key, char *out, size_t len)
{
    out[0] = '\0';
    const esp_err_t err = httpd_query_key_value(src, key, out, len);
    if (err == ESP_ERR_NOT_FOUND) {
        return FIELD_MISSING;
    }
    if (err == ESP_ERR_HTTPD_RESULT_TRUNC) {
        out[0] = '\0';
        return FIELD_LONG;
    }
    if (err != ESP_OK) {
        return FIELD_BAD;
    }
    for (const char *p = strchr(out, '%'); p; p = strchr(p + 1, '%')) {
        if (p[1] == '0' && p[2] == '0') {
            mbedtls_platform_zeroize(out, len);
            return FIELD_BAD;   // would silently cut the value short
        }
    }
    if (!web_url_decode(out)) {
        mbedtls_platform_zeroize(out, len);
        return FIELD_BAD;
    }
    return FIELD_OK;
}

static bool present(field_t f)
{
    return f == FIELD_OK || f == FIELD_LONG;
}

static bool parse_role(const char *s, web_role_t *role)
{
    if (strcmp(s, "admin") == 0) {
        *role = WEB_ROLE_ADMIN;
        return true;
    }
    if (strcmp(s, "user") == 0) {
        *role = WEB_ROLE_USER;
        return true;
    }
    return false;
}

static const char *role_name(web_role_t r)
{
    return r == WEB_ROLE_ADMIN ? "admin" : "user";
}

/** Read the query string of req into buf. False (and buf "") if there is none or it is too long. */
static bool get_query(httpd_req_t *req, char *buf, size_t len)
{
    buf[0] = '\0';
    const size_t qlen = httpd_req_get_url_query_len(req);
    return qlen > 0 && qlen < len && httpd_req_get_url_query_str(req, buf, len) == ESP_OK;
}

/** Answer for the result of a user operation ({"ok":true} or the error). */
static esp_err_t send_result(httpd_req_t *req, auth_result_t r)
{
    return r == AUTH_OK ? web_send_json(req, "200 OK", "{\"ok\":true}") : auth_send_error(req, r);
}

/** {"ok":true,"user":..,"role":..,"default_password":..} for the identity of this request. */
static esp_err_t send_session(httpd_req_t *req, const char *set_cookie)
{
    char buf[160] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":true,\"user\":");
    web_json_str(buf, sizeof(buf), auth_current_user(req));
    web_appendf(buf, sizeof(buf), ",\"role\":\"%s\",\"default_password\":%s}", role_name(auth_current_role(req)),
                auth_current_default_password(req) ? "true" : "false");
    if (set_cookie) {
        httpd_resp_set_hdr(req, "Set-Cookie", set_cookie);
    }
    return web_send_json(req, "200 OK", buf);
}

// ------------------------------------------------------------------ handlers

static esp_err_t h_session(httpd_req_t *req)
{
    const auth_result_t r = auth_identify(req);
    return r == AUTH_OK ? send_session(req, NULL) : auth_send_error(req, r);
}

static esp_err_t h_login(httpd_req_t *req)
{
    char body[FORM_MAX];
    const esp_err_t err = web_read_body(req, body, sizeof(body));
    if (err != ESP_OK) {
        return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;   // too large: already answered
    }
    char user[FIELD_MAX], pass[FIELD_MAX], remember[8], device[FIELD_MAX];
    const field_t fu = get_field(body, "user", user, sizeof(user));
    const field_t fp = get_field(body, "pass", pass, sizeof(pass));
    const field_t fr = get_field(body, "remember", remember, sizeof(remember));
    get_field(body, "device", device, sizeof(device));   // missing/bad/too long: "" (auth.c names it)
    mbedtls_platform_zeroize(body, sizeof(body));
    if (!present(fu) || !present(fp)) {
        mbedtls_platform_zeroize(pass, sizeof(pass));
        return auth_send_error(req, AUTH_ERR_BAD_REQUEST);
    }
    const bool want_device = fr == FIELD_OK && (strcmp(remember, "1") == 0 || strcmp(remember, "true") == 0);
    // Over-long values are "" here: they can never match and count as a failed attempt.
    char token[AUTH_TOKEN_HEX + 1];
    bool persistent = false;
    const auth_result_t r = auth_login(req, user, pass, want_device ? device : NULL, token, &persistent);
    mbedtls_platform_zeroize(pass, sizeof(pass));
    if (r != AUTH_OK) {
        return auth_send_error(req, r);
    }
    char cookie[AUTH_TOKEN_HEX + 96];
    if (persistent) {
        snprintf(cookie, sizeof(cookie), AUTH_COOKIE_NAME "=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s",
                 token, AUTH_DEVICE_MAX_AGE_S, COOKIE_SECURE());
    } else {
        snprintf(cookie, sizeof(cookie), AUTH_COOKIE_NAME "=%s; Path=/; HttpOnly; SameSite=Strict%s", token,
                 COOKIE_SECURE());
    }
    const esp_err_t sent = send_session(req, cookie);   // header is sent before cookie goes out of scope
    mbedtls_platform_zeroize(token, sizeof(token));
    mbedtls_platform_zeroize(cookie, sizeof(cookie));
    return sent;
}

static esp_err_t h_logout(httpd_req_t *req)
{
    auth_logout(req);
    httpd_resp_set_hdr(req, "Set-Cookie",
                       pd_tls_active() ? AUTH_COOKIE_NAME "=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict; Secure"
                                    : AUTH_COOKIE_NAME "=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict");
    return web_send_json(req, "200 OK", "{\"ok\":true}");
}

static esp_err_t h_users(httpd_req_t *req)
{
    auth_user_info_t users[AUTH_USERS_MAX];
    const size_t n = auth_list_users(users, AUTH_USERS_MAX);
    char buf[640] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":true,\"users\":[");
    for (size_t i = 0; i < n; i++) {
        web_appendf(buf, sizeof(buf), "%s{\"name\":", i ? "," : "");
        web_json_str(buf, sizeof(buf), users[i].name);
        web_appendf(buf, sizeof(buf), ",\"role\":\"%s\"}", role_name(users[i].role));
    }
    web_appendf(buf, sizeof(buf), "],\"max\":%d}", AUTH_USERS_MAX);
    return web_send_json(req, "200 OK", buf);
}

static esp_err_t h_users_add(httpd_req_t *req)
{
    char body[FORM_MAX];
    const esp_err_t err = web_read_body(req, body, sizeof(body));
    if (err != ESP_OK) {
        return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;
    }
    char user[FIELD_MAX], pass[FIELD_MAX], role_s[ROLE_MAX];
    const field_t fu = get_field(body, "user", user, sizeof(user));
    const field_t fp = get_field(body, "pass", pass, sizeof(pass));
    const field_t fr = get_field(body, "role", role_s, sizeof(role_s));
    mbedtls_platform_zeroize(body, sizeof(body));
    web_role_t role = WEB_ROLE_USER;
    auth_result_t r;
    if (!present(fu) || !present(fp) || !present(fr) || !parse_role(role_s, &role)) {
        r = AUTH_ERR_BAD_REQUEST;
    } else {
        r = auth_user_add(user, pass, role);   // over-long user/pass are "" -> BAD_USER / BAD_PASSWORD
    }
    mbedtls_platform_zeroize(pass, sizeof(pass));
    return send_result(req, r);
}

static esp_err_t h_users_delete(httpd_req_t *req)
{
    char query[CONFIG_HTTPD_MAX_URI_LEN + 1];
    char user[FIELD_MAX];
    get_query(req, query, sizeof(query));
    const field_t fu = get_field(query, "user", user, sizeof(user));
    if (!present(fu)) {
        return auth_send_error(req, AUTH_ERR_BAD_REQUEST);
    }
    return send_result(req, auth_user_delete(user));   // over-long: "" -> NOT_FOUND
}

static esp_err_t h_users_role(httpd_req_t *req)
{
    char query[CONFIG_HTTPD_MAX_URI_LEN + 1];
    char user[FIELD_MAX], role_s[ROLE_MAX];
    get_query(req, query, sizeof(query));
    const field_t fu = get_field(query, "user", user, sizeof(user));
    const field_t fr = get_field(query, "role", role_s, sizeof(role_s));
    web_role_t role = WEB_ROLE_USER;
    if (!present(fu) || !present(fr) || !parse_role(role_s, &role)) {
        return auth_send_error(req, AUTH_ERR_BAD_REQUEST);
    }
    return send_result(req, auth_user_set_role(user, role));
}

static esp_err_t h_users_password(httpd_req_t *req)
{
    char body[FORM_MAX];
    const esp_err_t err = web_read_body(req, body, sizeof(body));
    if (err != ESP_OK) {
        return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;
    }
    char user[FIELD_MAX], pass[FIELD_MAX], old[FIELD_MAX];
    const field_t fu = get_field(body, "user", user, sizeof(user));
    const field_t fp = get_field(body, "pass", pass, sizeof(pass));
    const field_t fo = get_field(body, "old", old, sizeof(old));
    mbedtls_platform_zeroize(body, sizeof(body));
    auth_result_t r;
    if (!present(fu) || !present(fp)) {
        r = AUTH_ERR_BAD_REQUEST;
    } else {
        // A missing old password is NULL; an undecodable or over-long one is "" (wrong).
        r = auth_user_set_password(req, user, pass, fo == FIELD_MISSING ? NULL : old);
    }
    mbedtls_platform_zeroize(pass, sizeof(pass));
    mbedtls_platform_zeroize(old, sizeof(old));
    return send_result(req, r);
}

static esp_err_t h_devices(httpd_req_t *req)
{
    auth_device_info_t devs[AUTH_DEVICES_MAX];
    const size_t n = auth_list_devices(devs, AUTH_DEVICES_MAX);
    // Per device at most: 8 id + 24 user + 64 (label of 32 bytes, each '"' or '\' escaped) + keys.
    char buf[1400] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":true,\"devices\":[");
    for (size_t i = 0; i < n; i++) {
        web_appendf(buf, sizeof(buf), "%s{\"id\":\"%s\",\"user\":", i ? "," : "", devs[i].id);
        web_json_str(buf, sizeof(buf), devs[i].user);
        web_appendf(buf, sizeof(buf), ",\"label\":");
        web_json_str(buf, sizeof(buf), devs[i].label);
        web_appendf(buf, sizeof(buf), ",\"current\":%s}", devs[i].current ? "true" : "false");
    }
    web_appendf(buf, sizeof(buf), "],\"max\":%d}", AUTH_DEVICES_MAX);
    return web_send_json(req, "200 OK", buf);
}

static esp_err_t h_devices_delete(httpd_req_t *req)
{
    char query[CONFIG_HTTPD_MAX_URI_LEN + 1];
    char id[16];
    get_query(req, query, sizeof(query));
    const field_t fi = get_field(query, "id", id, sizeof(id));
    if (!present(fi)) {
        return auth_send_error(req, AUTH_ERR_BAD_REQUEST);
    }
    return send_result(req, auth_device_delete(id));   // over-long: "" -> NOT_FOUND
}

// ------------------------------------------------------------------ routes

static const web_route_t s_routes[] = {
    {"/api/session", HTTP_GET, h_session, WEB_ROLE_NONE},
    {"/api/login", HTTP_POST, h_login, WEB_ROLE_NONE},
    {"/api/logout", HTTP_POST, h_logout, WEB_ROLE_USER},
    {"/api/users", HTTP_GET, h_users, WEB_ROLE_ADMIN},
    {"/api/users/add", HTTP_POST, h_users_add, WEB_ROLE_ADMIN},
    {"/api/users/delete", HTTP_POST, h_users_delete, WEB_ROLE_ADMIN},
    {"/api/users/role", HTTP_POST, h_users_role, WEB_ROLE_ADMIN},
    {"/api/users/password", HTTP_POST, h_users_password, WEB_ROLE_USER},
    {"/api/devices", HTTP_GET, h_devices, WEB_ROLE_USER},
    {"/api/devices/delete", HTTP_POST, h_devices_delete, WEB_ROLE_USER},
};

esp_err_t web_auth_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
