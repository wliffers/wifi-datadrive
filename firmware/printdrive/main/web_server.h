// HTTP server: web UI (embedded index.html) and JSON API.
//
// All state-changing requests must be POST and carry the header "X-PrintDrive: 1".
// Browsers cannot add that header cross-origin without a CORS preflight (which this
// server never allows), so other web pages cannot trigger actions on the dongle.
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t web_server_start(void);

#ifdef __cplusplus
}
#endif
