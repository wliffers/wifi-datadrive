#pragma once

/**
 * PLACEHOLDER for work package P1 (authentication). The current implementation in auth.c
 * allows everything; another developer replaces auth.c without touching the web modules.
 */

#include <stdbool.h>
#include "esp_err.h"
#include "esp_http_server.h"
#include "web_common.h"

/** Called by the central dispatcher before every handler. Returns true if the request may
 *  proceed. Contract: when it returns false it has ALREADY sent the error response. */
bool auth_authorize(httpd_req_t *req, web_role_t min_role);

/** Name of the logged-in user of this request, "" if none. Never NULL. */
const char *auth_current_user(httpd_req_t *req);

/** Load users from NVS (creates admin/admin if there are none). Call once before the web server starts. */
esp_err_t auth_init(void);

/** Delete all users and sessions and re-create admin/admin (BOOT button held 10 s). */
esp_err_t auth_reset_users(void);
