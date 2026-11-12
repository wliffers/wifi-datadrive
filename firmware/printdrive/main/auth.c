#include "auth.h"

// PLACEHOLDER for work package P1: no authentication yet, everybody is allowed everything.
// Replace this file; keep the two function signatures from auth.h.

bool auth_authorize(httpd_req_t *req, web_role_t min_role)
{
    (void)req;
    (void)min_role;
    return true;
}

const char *auth_current_user(httpd_req_t *req)
{
    (void)req;
    return "";
}

esp_err_t auth_init(void)
{
    return ESP_OK;
}

esp_err_t auth_reset_users(void)
{
    return ESP_OK;
}
