#include "web_wifi.h"

#include <string.h>
#include "pd_wifi.h"
#include "web_common.h"

static esp_err_t h_list(httpd_req_t *req)
{
    pd_wifi_net_t nets[PD_WIFI_MAX_NETWORKS];
    const size_t n = pd_wifi_list(nets, PD_WIFI_MAX_NETWORKS);
    char json[1536] = "{\"ok\":true,\"networks\":[";
    for (size_t i = 0; i < n; i++) {
        web_appendf(json, sizeof(json), "%s{\"ssid\":", i ? "," : "");
        web_json_str(json, sizeof(json), nets[i].ssid);
        web_appendf(json, sizeof(json), ",\"active\":%s}", nets[i].active ? "true" : "false");
    }
    web_appendf(json, sizeof(json), "],\"max\":%d}", PD_WIFI_MAX_NETWORKS);
    return web_send_json(req, "200 OK", json);
}

static esp_err_t h_add(httpd_req_t *req)
{
    char body[512] = "";
    const esp_err_t err = web_read_body(req, body, sizeof(body));
    if (err != ESP_OK) {
        return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;   // too large: already answered
    }
    char ssid[128] = "", pass[256] = "";
    if (httpd_query_key_value(body, "ssid", ssid, sizeof(ssid)) != ESP_OK || !web_url_decode(ssid) || !ssid[0]) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Bitte den WLAN-Namen angeben.");
    }
    if (httpd_query_key_value(body, "pass", pass, sizeof(pass)) == ESP_OK) {
        web_url_decode(pass);
    }
    const esp_err_t r = pd_wifi_add(ssid, pass);
    memset(pass, 0, sizeof(pass));
    memset(body, 0, sizeof(body));
    if (r == PD_WIFI_ERR_LIMIT) {
        return web_send_error(req, "409 Conflict", "WIFI_LIMIT", "Es sind bereits 5 WLANs gespeichert.");
    }
    if (r == ESP_ERR_INVALID_ARG) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                              "Bitte einen WLAN-Namen (höchstens 32 Zeichen) und ein Passwort mit 8 bis 63 Zeichen angeben – oder das Passwort leer lassen.");
    }
    if (r != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Das WLAN konnte nicht gespeichert werden.");
    }
    return web_send_json(req, "200 OK", "{\"ok\":true}");
}

static esp_err_t h_delete(httpd_req_t *req)
{
    char ssid[128] = "";
    if (!web_query_param(req, "ssid", ssid, sizeof(ssid)) || !ssid[0]) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Bitte den WLAN-Namen angeben.");
    }
    const esp_err_t r = pd_wifi_remove(ssid);
    if (r == ESP_ERR_NOT_FOUND) {
        return web_send_error(req, "404 Not Found", "NOT_FOUND", "Dieses WLAN ist nicht gespeichert.");
    }
    if (r != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Das WLAN konnte nicht entfernt werden.");
    }
    return web_send_json(req, "200 OK", "{\"ok\":true}");
}

static const web_route_t s_routes[] = {
    {"/api/wifi/list", HTTP_GET, h_list, WEB_ROLE_ADMIN},
    {"/api/wifi/add", HTTP_POST, h_add, WEB_ROLE_ADMIN},
    {"/api/wifi/delete", HTTP_POST, h_delete, WEB_ROLE_ADMIN},
    {"/api/wifi", HTTP_POST, h_add, WEB_ROLE_ADMIN},   // alias of version 2
};

esp_err_t web_wifi_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
