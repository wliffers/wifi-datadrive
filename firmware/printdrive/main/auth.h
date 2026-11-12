#pragma once

/**
 * Authentication and user management (work package P1, API contract v3 section 2).
 *
 * - Users live in NVS (namespace "pd_auth"), at most AUTH_USERS_MAX. Per user: name, role,
 *   16-byte salt and PBKDF2-HMAC-SHA256 (2000 rounds, 32 bytes). Never the password.
 * - Sessions live in RAM (cookie "dd_session"), HTTP Basic works on every request.
 * - 5 failed password checks in a row lock every password check for 30 s (429 LOCKED).
 *
 * All state is protected by one mutex, so auth_reset_users() may be called from any task.
 * The per-request identity (auth_current_*) is only meaningful in the HTTP server task
 * during the handler of the request that went through auth_authorize().
 */

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "esp_http_server.h"
#include "web_common.h"

#define AUTH_USERS_MAX   8
#define AUTH_NAME_MAX    24   // characters (bytes), without terminator
#define AUTH_PASS_MIN    5    // bytes
#define AUTH_PASS_MAX    64   // bytes
#define AUTH_TOKEN_HEX   64   // session token: 256 bit as hex
#define AUTH_COOKIE_NAME "dd_session"
#define AUTH_DEVICES_MAX 8    // trusted devices (persistent sessions) in total
#define AUTH_DEVICE_LABEL_MAX 32   // bytes, without terminator
#define AUTH_DEVICE_MAX_AGE_S 31536000   // cookie Max-Age of a trusted device

/** Result of an authentication or user-management operation. */
typedef enum {
    AUTH_OK = 0,
    AUTH_ERR_BAD_REQUEST,     // 400 parameter missing or not decodable
    AUTH_ERR_BAD_USER,        // 400 invalid user name
    AUTH_ERR_BAD_PASSWORD,    // 400 password length
    AUTH_ERR_DEFAULT_PASSWORD,// 400 code BAD_PASSWORD: new password is the delivery password "admin"
    AUTH_ERR_UNAUTHORIZED,    // 401 not logged in
    AUTH_ERR_BAD_LOGIN,       // 401 wrong user name or password
    AUTH_ERR_FORBIDDEN_ROLE,  // 403 role not sufficient
    AUTH_ERR_MUST_CHANGE_PASSWORD, // 403 account still has the delivery password
    AUTH_ERR_NOT_FOUND,       // 404 no such user
    AUTH_ERR_EXISTS,          // 409 user exists
    AUTH_ERR_LAST_ADMIN,      // 409 last administrator
    AUTH_ERR_USER_LIMIT,      // 409 AUTH_USERS_MAX users exist
    AUTH_ERR_LOCKED,          // 429 too many failed attempts
    AUTH_ERR_IO,              // 500 user database could not be saved
} auth_result_t;

/** Machine-readable code ("BAD_LOGIN", ...). */
const char *auth_result_code(auth_result_t r);
/** German plain-language text for end users. */
const char *auth_result_str(auth_result_t r);
/** HTTP status line ("401 Unauthorized", ...). */
const char *auth_result_http_status(auth_result_t r);
/** Send {"ok":false,"code":..,"error":..} for r (r != AUTH_OK) with the matching status. */
esp_err_t auth_send_error(httpd_req_t *req, auth_result_t r);

// ------------------------------------------------------------------ dispatcher interface

/** Called by the central dispatcher before every handler. Returns true if the request may
 *  proceed. Contract: when it returns false it has ALREADY sent the error response.
 *  WEB_ROLE_NONE routes always pass; for them only a session cookie is resolved (no Basic,
 *  no failure counting) - use auth_identify() where the full identity is needed.
 *  Forced password change (contract 2.4): an identity that still has the delivery password
 *  (admin/admin) only passes on POST /api/logout and POST /api/users/password (recognised by
 *  req->uri); every other protected route answers 403 MUST_CHANGE_PASSWORD. */
bool auth_authorize(httpd_req_t *req, web_role_t min_role);

/** Full identification of the caller for a public route (/api/session): session cookie
 *  first, then HTTP Basic (which counts failures and honours the lock-out). Returns AUTH_OK,
 *  AUTH_ERR_UNAUTHORIZED or AUTH_ERR_LOCKED; sends nothing. */
auth_result_t auth_identify(httpd_req_t *req);

/** Name of the logged-in user of this request, "" if none. Never NULL. */
const char *auth_current_user(httpd_req_t *req);

/** Role of the logged-in user of this request, WEB_ROLE_NONE if none. */
web_role_t auth_current_role(httpd_req_t *req);

/** True while the logged-in user is "admin" with the password "admin". */
bool auth_current_default_password(httpd_req_t *req);

// ------------------------------------------------------------------ endpoints (web_auth.c)

/** Check user/pass (counts failures, honours the lock-out) and create a session. With
 *  device_label != NULL a trusted device (persistent, contract 2.2a) is created instead; the
 *  label is sanitised here. If it cannot be saved, a volatile session is created and
 *  *persistent stays false. On AUTH_OK token_hex receives the cookie value (AUTH_TOKEN_HEX
 *  chars + NUL) and the request identity is set to this user.
 *  Errors: AUTH_ERR_BAD_LOGIN, AUTH_ERR_LOCKED. */
auth_result_t auth_login(httpd_req_t *req, const char *user, const char *pass, const char *device_label,
                         char token_hex[AUTH_TOKEN_HEX + 1], bool *persistent);

/** End the session or trusted device this request was authenticated with (no-op for Basic). */
void auth_logout(httpd_req_t *req);

typedef struct {
    char id[9];                        // first 8 hex chars of the stored hash
    char user[AUTH_NAME_MAX + 1];
    char label[AUTH_DEVICE_LABEL_MAX + 1];
    bool current;                      // the device of this request
} auth_device_info_t;

/** Trusted devices visible to the caller (own ones; admin: all). Returns the count. */
size_t auth_list_devices(auth_device_info_t *out, size_t max);

/** Remove a trusted device by id: own one, or any as admin.
 *  Errors: AUTH_ERR_NOT_FOUND, AUTH_ERR_FORBIDDEN_ROLE, AUTH_ERR_IO (removed in RAM anyway). */
auth_result_t auth_device_delete(const char *id);

typedef struct {
    char name[AUTH_NAME_MAX + 1];
    web_role_t role;
} auth_user_info_t;

/** Copy the user list (in storage order). Returns the number of users. */
size_t auth_list_users(auth_user_info_t *out, size_t max);

/** Errors: AUTH_ERR_BAD_USER, AUTH_ERR_BAD_PASSWORD, AUTH_ERR_DEFAULT_PASSWORD (password
 *  "admin"), AUTH_ERR_EXISTS, AUTH_ERR_USER_LIMIT, AUTH_ERR_IO. */
auth_result_t auth_user_add(const char *name, const char *pass, web_role_t role);

/** Ends all sessions of the user. Errors: AUTH_ERR_NOT_FOUND, AUTH_ERR_LAST_ADMIN, AUTH_ERR_IO. */
auth_result_t auth_user_delete(const char *name);

/** Errors: AUTH_ERR_NOT_FOUND, AUTH_ERR_LAST_ADMIN, AUTH_ERR_IO. */
auth_result_t auth_user_set_role(const char *name, web_role_t role);

/** Password change on behalf of the caller of req. Own account: old must be correct (old may
 *  be NULL = missing; a wrong one counts as a failure, AUTH_ERR_BAD_LOGIN / AUTH_ERR_LOCKED).
 *  Other account: refused with AUTH_ERR_MUST_CHANGE_PASSWORD while the caller still has the
 *  delivery password; caller must be admin (AUTH_ERR_FORBIDDEN_ROLE, checked before
 *  existence), then AUTH_ERR_NOT_FOUND. Then AUTH_ERR_BAD_PASSWORD (length),
 *  AUTH_ERR_DEFAULT_PASSWORD (new password "admin"), AUTH_ERR_IO. On success all other
 *  sessions of that user end (the caller's own session survives an own change and is no
 *  longer restricted). */
auth_result_t auth_user_set_password(httpd_req_t *req, const char *name, const char *pass, const char *old);

/** True if name follows the rules (1..24 chars of a-z A-Z 0-9 . _ -). */
bool auth_valid_name(const char *name);

// ------------------------------------------------------------------ lifecycle

/** Load users from NVS (creates admin/admin if there are none). Call once before the web
 *  server starts. On a storage error the users are admin/admin in RAM only (logged) and an
 *  error is returned; authentication is still enforced. */
esp_err_t auth_init(void);

/** Delete all users, sessions and trusted devices and re-create admin/admin (BOOT button held
 *  10 s; the forced password change applies again). Also clears the lock-out. Safe to call
 *  from any task. */
esp_err_t auth_reset_users(void);
