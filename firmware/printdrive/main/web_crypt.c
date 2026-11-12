#include "web_crypt.h"

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "mbedtls/platform_util.h"
#include "anim.h"
#include "web_common.h"

// Drive password limits of the contract (API v4, 2.2). The layer accepts up to 128 bytes.
#define PW_MIN_CHARS 12
#define PW_MAX_BYTES 64

#define BODY_MAX  1024      // urlencoded body: a 64-byte password can grow 3x when percent-encoded
#define FIELD_MAX 400       // one decoded field; longer values are refused

static const char *TAG = "web";

drive_result_t web_crypt_block(void)
{
    drive_crypt_info_t ci;
    drive_crypt_get_info(&ci);
    if (ci.state == DRIVE_CRYPT_LOCKED) {
        return DRIVE_ERR_LOCKED;
    }
    return ci.state == DRIVE_CRYPT_DAMAGED ? DRIVE_ERR_CRYPT_DAMAGED : DRIVE_OK;
}

bool web_crypt_locked(void)
{
    return web_crypt_block() != DRIVE_OK;
}

// ------------------------------------------------------------------ answers

/** Answer for a drive result; retry_s adds Retry-After for 429. extra: optional JSON members
 *  (starting with a comma) appended to the object, may hold the recovery key. */
static esp_err_t send_crypt(httpd_req_t *req, drive_result_t r, uint32_t retry_s, const char *extra)
{
    char buf[400] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":%s,\"code\":\"%s\",\"error\":", r == DRIVE_OK ? "true" : "false",
                web_result_code(r));
    web_json_str(buf, sizeof(buf), r == DRIVE_OK ? "" : drive_result_str(r));
    if (extra) {
        web_appendf(buf, sizeof(buf), "%s", extra);
    }
    web_appendf(buf, sizeof(buf), "}");
    if (r == DRIVE_ERR_RETRY_LATER) {
        char ra[12];
        snprintf(ra, sizeof(ra), "%lu", (unsigned long)(retry_s ? retry_s : 1));
        httpd_resp_set_hdr(req, "Retry-After", ra);
    }
    const esp_err_t err = web_send_json(req, web_result_http_status(r), buf);
    mbedtls_platform_zeroize(buf, sizeof(buf));
    return err;
}

// ------------------------------------------------------------------ form parsing

typedef enum {
    F_OK,
    F_MISSING,   // absent or empty
    F_BAD,       // too long or not decodable
} field_t;

static field_t get_field(const char *body, const char *key, char *out, size_t len)
{
    char raw[FIELD_MAX * 3 + 1];
    field_t res = F_OK;
    const esp_err_t e = httpd_query_key_value(body, key, raw, sizeof(raw));
    if (e == ESP_ERR_NOT_FOUND) {
        res = F_MISSING;
    } else if (e != ESP_OK || !web_url_decode(raw) || strlen(raw) >= len) {
        res = F_BAD;
    } else if (!raw[0]) {
        res = F_MISSING;
    } else {
        memcpy(out, raw, strlen(raw) + 1);
    }
    mbedtls_platform_zeroize(raw, sizeof(raw));
    return res;
}

/** Number of Unicode characters of a UTF-8 string (continuation bytes are not counted). */
static size_t utf8_chars(const char *s)
{
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if ((*p & 0xC0) != 0x80) {
            n++;
        }
    }
    return n;
}

/** The body and the decoded fields live on the stack and are wiped before every answer. */
typedef struct {
    char body[BODY_MAX];
    char f1[FIELD_MAX];
    char f2[FIELD_MAX];
} form_t;

static void form_wipe(form_t *f)
{
    mbedtls_platform_zeroize(f, sizeof(*f));
}

/** Read the body into f. Returns ESP_OK to go on, otherwise the value the handler must return:
 *  ESP_ERR_INVALID_SIZE (a 400 has been sent) or ESP_FAIL (connection lost). */
static esp_err_t form_read(httpd_req_t *req, form_t *f)
{
    memset(f, 0, sizeof(*f));
    const esp_err_t e = web_read_body(req, f->body, sizeof(f->body));
    if (e != ESP_OK) {
        form_wipe(f);
    }
    return e;
}

#define FORM_READ(req, f)                                                 \
    do {                                                                  \
        const esp_err_t fr_ = form_read((req), (f));                      \
        if (fr_ != ESP_OK) {                                              \
            return fr_ == ESP_ERR_INVALID_SIZE ? ESP_OK : ESP_FAIL;       \
        }                                                                 \
    } while (0)

static esp_err_t bad_request(httpd_req_t *req, form_t *f, const char *msg)
{
    form_wipe(f);
    return web_send_error(req, "400 Bad Request", "BAD_REQUEST", msg);
}

static esp_err_t finish(httpd_req_t *req, form_t *f, drive_result_t r, uint32_t retry_s)
{
    form_wipe(f);
    return send_crypt(req, r, retry_s, NULL);
}

/** New drive password: 12 characters at least (CRYPT_WEAK_PASSWORD), 64 bytes at most
 *  (BAD_REQUEST). Returns DRIVE_OK, DRIVE_ERR_WEAK_PASSWORD or DRIVE_ERR_BAD_NAME (= too long). */
static drive_result_t check_new_password(const char *pw)
{
    if (strlen(pw) > PW_MAX_BYTES) {
        return DRIVE_ERR_BAD_NAME;
    }
    return utf8_chars(pw) < PW_MIN_CHARS ? DRIVE_ERR_WEAK_PASSWORD : DRIVE_OK;
}

static esp_err_t answer_bad_password(httpd_req_t *req, form_t *f, drive_result_t r)
{
    if (r == DRIVE_ERR_BAD_NAME) {
        return bad_request(req, f, "Das Laufwerks-Passwort darf höchstens 64 Bytes lang sein.");
    }
    return finish(req, f, r, 0);
}

// ------------------------------------------------------------------ handlers

static esp_err_t h_crypt(httpd_req_t *req)
{
    drive_crypt_info_t ci;
    drive_crypt_get_info(&ci);
    char buf[128];
    snprintf(buf, sizeof(buf), "{\"ok\":true,\"enabled\":%s,\"unlocked\":%s,\"damaged\":%s,\"retry_after\":%lu}",
             ci.state != DRIVE_CRYPT_OFF ? "true" : "false",
             (ci.state == DRIVE_CRYPT_OFF || ci.state == DRIVE_CRYPT_UNLOCKED) ? "true" : "false",
             ci.state == DRIVE_CRYPT_DAMAGED ? "true" : "false", (unsigned long)ci.retry_after_s);
    return web_send_json(req, "200 OK", buf);
}

/** POST /api/crypt/setup: password. Erases and formats the whole medium (minutes: the request blocks). */
static esp_err_t h_setup(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    if (get_field(f.body, "password", f.f1, sizeof(f.f1)) != F_OK) {
        return bad_request(req, &f, "password fehlt oder ist ungültig");
    }
    const drive_result_t chk = check_new_password(f.f1);
    if (chk != DRIVE_OK) {
        return answer_bad_password(req, &f, chk);
    }
    char recovery[DRIVE_CRYPT_RECOVERY_LEN] = "";
    ESP_LOGI(TAG, "drive encryption: setup (formats the drive)");
    anim_set_busy(true);
    const drive_result_t r = drive_crypt_setup(f.f1, recovery, sizeof(recovery));
    anim_set_busy(false);
    form_wipe(&f);
    ESP_LOGI(TAG, "drive encryption: setup %s", drive_result_str(r));
    // Whenever a key came out the drive IS encrypted: hand it over even if formatting failed.
    char extra[80] = "";
    if (recovery[0]) {
        web_appendf(extra, sizeof(extra), ",\"recovery_key\":");
        web_json_str(extra, sizeof(extra), recovery);
    }
    const esp_err_t err = send_crypt(req, r, 0, extra[0] ? extra : NULL);
    mbedtls_platform_zeroize(recovery, sizeof(recovery));
    mbedtls_platform_zeroize(extra, sizeof(extra));
    return err;
}

/** POST /api/crypt/unlock: password or recovery_key. */
static esp_err_t h_unlock(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    field_t sel = get_field(f.body, "password", f.f1, sizeof(f.f1));
    const char *secret = f.f1;
    if (sel == F_MISSING) {
        sel = get_field(f.body, "recovery_key", f.f2, sizeof(f.f2));
        secret = f.f2;
    }
    if (sel == F_MISSING) {
        return bad_request(req, &f, "password oder recovery_key fehlt");
    }
    if (sel == F_BAD || strlen(secret) > PW_MAX_BYTES) {
        return bad_request(req, &f, "Eingabe ungültig oder zu lang.");
    }
    uint32_t retry = 0;
    const drive_result_t r = drive_crypt_unlock(secret, &retry);
    ESP_LOGI(TAG, "drive encryption: unlock %s", drive_result_str(r));
    return finish(req, &f, r, retry);
}

static esp_err_t h_lock(httpd_req_t *req)
{
    // Contract: a drive the USB device is using is locked as well (only HOST_WRITING and BUSY
    // refuse), so force = true.
    const drive_result_t r = drive_crypt_lock(true);
    ESP_LOGI(TAG, "drive encryption: lock %s", drive_result_str(r));
    return send_crypt(req, r, 0, NULL);
}

/** POST /api/crypt/password: old (password or recovery key), new. */
static esp_err_t h_password(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    const field_t o = get_field(f.body, "old", f.f1, sizeof(f.f1));
    const field_t n = get_field(f.body, "new", f.f2, sizeof(f.f2));
    if (o != F_OK || n != F_OK) {
        return bad_request(req, &f, "old oder new fehlt oder ist ungültig");
    }
    if (strlen(f.f1) > PW_MAX_BYTES) {
        return bad_request(req, &f, "Eingabe zu lang.");
    }
    const drive_result_t chk = check_new_password(f.f2);
    if (chk != DRIVE_OK) {
        return answer_bad_password(req, &f, chk);
    }
    uint32_t retry = 0;
    const drive_result_t r = drive_crypt_change_password(f.f1, f.f2, &retry);
    ESP_LOGI(TAG, "drive encryption: password change %s", drive_result_str(r));
    return finish(req, &f, r, retry);
}

/** POST /api/crypt/disable: password. Formats the drive plain. */
static esp_err_t h_disable(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    if (get_field(f.body, "password", f.f1, sizeof(f.f1)) != F_OK) {
        return bad_request(req, &f, "password fehlt oder ist ungültig");
    }
    if (strlen(f.f1) > PW_MAX_BYTES) {
        return bad_request(req, &f, "Eingabe zu lang.");
    }
    uint32_t retry = 0;
    anim_set_busy(true);
    const drive_result_t r = drive_crypt_disable(f.f1, &retry);
    anim_set_busy(false);
    ESP_LOGI(TAG, "drive encryption: disable %s", drive_result_str(r));
    return finish(req, &f, r, retry);
}

/** POST /api/crypt/recovery: password (drive password or current recovery key). New recovery key. */
static esp_err_t h_recovery(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    if (get_field(f.body, "password", f.f1, sizeof(f.f1)) != F_OK) {
        return bad_request(req, &f, "password fehlt oder ist ungültig");
    }
    if (strlen(f.f1) > PW_MAX_BYTES) {
        return bad_request(req, &f, "Eingabe zu lang.");
    }
    char recovery[DRIVE_CRYPT_RECOVERY_LEN] = "";
    const drive_result_t r = drive_crypt_new_recovery_key(f.f1, recovery, sizeof(recovery));
    form_wipe(&f);
    ESP_LOGI(TAG, "drive encryption: new recovery key %s", drive_result_str(r));
    char extra[80] = "";
    if (r == DRIVE_OK && recovery[0]) {
        web_appendf(extra, sizeof(extra), ",\"recovery_key\":");
        web_json_str(extra, sizeof(extra), recovery);
    }
    const esp_err_t err = send_crypt(req, r, 0, extra[0] ? extra : NULL);
    mbedtls_platform_zeroize(recovery, sizeof(recovery));
    mbedtls_platform_zeroize(extra, sizeof(extra));
    return err;
}

/** POST /api/crypt/erase: confirm=ERASE. Locked or damaged drive, secrets lost: erase and format plain. */
static esp_err_t h_erase(httpd_req_t *req)
{
    form_t f;
    FORM_READ(req, &f);
    const field_t c = get_field(f.body, "confirm", f.f1, sizeof(f.f1));
    if (c != F_OK || strcmp(f.f1, "ERASE") != 0) {
        return bad_request(req, &f, "Bestätigung fehlt");
    }
    form_wipe(&f);
    ESP_LOGW(TAG, "drive encryption: erase and format anyway");
    anim_set_busy(true);
    const drive_result_t r = drive_crypt_force_format();
    anim_set_busy(false);
    ESP_LOGW(TAG, "drive encryption: erase %s", drive_result_str(r));
    return send_crypt(req, r, 0, NULL);
}

static const web_route_t s_routes[] = {
    {"/api/crypt", HTTP_GET, h_crypt, WEB_ROLE_USER},
    {"/api/crypt/setup", HTTP_POST, h_setup, WEB_ROLE_ADMIN},
    {"/api/crypt/unlock", HTTP_POST, h_unlock, WEB_ROLE_USER},
    {"/api/crypt/lock", HTTP_POST, h_lock, WEB_ROLE_USER},
    {"/api/crypt/password", HTTP_POST, h_password, WEB_ROLE_ADMIN},
    {"/api/crypt/disable", HTTP_POST, h_disable, WEB_ROLE_ADMIN},
    {"/api/crypt/recovery", HTTP_POST, h_recovery, WEB_ROLE_ADMIN},
    {"/api/crypt/erase", HTTP_POST, h_erase, WEB_ROLE_ADMIN},
};

esp_err_t web_crypt_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
