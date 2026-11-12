#pragma once

/**
 * WebDAV access to the drive at /dav/ (class 1 plus the minimal class-2 locking that Windows and
 * macOS need before they allow writing). Every file operation goes through the drive layer
 * (drive.h) with the same rules as the web file manager; only while the file manager owns the
 * drive. Authentication: HTTP Basic only (see web_dav_authorize()).
 */

#include <stdbool.h>
#include "esp_err.h"
#include "esp_http_server.h"
#include "web_common.h"

/** Routes: OPTIONS and PROPFIND on exactly "/", all WebDAV methods on "/dav", "/dav/..." and
 *  POST /api/settings/webdav (admin). Needs uri_match_fn = httpd_uri_match_wildcard and must be
 *  registered before any catch-all route. Uses 15 URI handler slots. */
esp_err_t web_dav_register(httpd_handle_t server);

/** True if route belongs to the WebDAV table: the dispatcher then calls web_dav_authorize()
 *  instead of auth_authorize() and the CSRF check. */
bool web_dav_route(const web_route_t *route);

/**
 * Authorization of a WebDAV request (called by the dispatcher). Returns true if the request may
 * proceed; on false the answer has already been sent. WEB_ROLE_NONE (OPTIONS) always passes.
 * Otherwise an "Authorization: Basic" header is required - a session cookie alone is never
 * enough - and a missing or wrong one is answered with 401 and WWW-Authenticate.
 */
bool web_dav_authorize(httpd_req_t *req, web_role_t min_role);

/** WebDAV switched on (setting in NVS, default on). For "webdav" in /api/status. */
bool web_dav_enabled(void);
