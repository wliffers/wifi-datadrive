#include "web_wifi.h"

#include "pd_wifi.h"
#include "web_common.h"

static esp_err_t h_wifi(httpd_req_t *req)
{
    char body[256] = "";
    const esp_err_t err = web_read_body(req, body, sizeof(body));
    if (err != ESP_OK) {
        return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;   // too large: already answered
    }
    char ssid[64] = "", pass[128] = "";
    if (httpd_query_key_value(body, "ssid", ssid, sizeof(ssid)) != ESP_OK || !web_url_decode(ssid)) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Bitte den WLAN-Namen angeben.");
    }
    if (httpd_query_key_value(body, "pass", pass, sizeof(pass)) == ESP_OK) {
        web_url_decode(pass);
    }
    if (pd_wifi_set_credentials(ssid, pass) != ESP_OK) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                              "Bitte einen WLAN-Namen (höchstens 32 Zeichen) und ein Passwort mit 8 bis 63 Zeichen angeben – oder das Passwort leer lassen.");
    }
    return web_send_json(req, "200 OK", "{\"ok\":true}");
}

static const web_route_t s_routes[] = {
    {"/api/wifi", HTTP_POST, h_wifi, WEB_ROLE_ADMIN},
};

esp_err_t web_wifi_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
