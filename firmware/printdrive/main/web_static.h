#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/** Routes: / and /index.html (embedded, gzip-compressed web UI). */
esp_err_t web_static_register(httpd_handle_t server);

/**
 * Handler for HTTPD_404_NOT_FOUND and HTTPD_405_METHOD_NOT_ALLOWED (httpd_register_err_handler,
 * see web_server.c). Captive portal:
 * a request that came in through the setup access point and names a foreign host (an OS
 * connectivity check) gets "302 Found" to http://<ap-ip>/?captive=1. Everything else gets the
 * HTTP server's default answer (error response, connection closed). Needs no login and no URI
 * handler slot. A module that wants its own 404 handling should call this for requests it does
 * not handle itself.
 */
esp_err_t web_static_not_found(httpd_req_t *req, httpd_err_code_t error);
