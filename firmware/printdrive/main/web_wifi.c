#include "web_wifi.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "tls.h"
#include "web_common.h"

static httpd_handle_t s_server;   // for httpd_queue_work() (certificate renewal)

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

/** True if s is well-formed UTF-8 (SSIDs are raw bytes; others cannot be sent as JSON text). */
static bool utf8_ok(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        int extra;
        if (*p < 0x80) {
            extra = 0;
        } else if (*p >= 0xC2 && *p <= 0xDF) {
            extra = 1;
        } else if (*p >= 0xE0 && *p <= 0xEF) {
            extra = 2;
        } else if (*p >= 0xF0 && *p <= 0xF4) {
            extra = 3;
        } else {
            return false;
        }
        p++;
        while (extra--) {
            if ((*p & 0xC0) != 0x80) {
                return false;
            }
            p++;
        }
    }
    return true;
}

#define SCAN_JSON_SIZE (PD_WIFI_SCAN_MAX * 280 + 64)   // worst case: 32 control bytes -> 192 chars per SSID

static esp_err_t h_scan(httpd_req_t *req)
{
    pd_wifi_scan_entry_t *nets = malloc(PD_WIFI_SCAN_MAX * sizeof(*nets));
    char *json = malloc(SCAN_JSON_SIZE);
    if (!nets || !json) {
        free(nets);
        free(json);
        return web_send_error(req, "500 Internal Server Error", "IO", "Zu wenig Speicher für die WLAN-Suche.");
    }
    const int n = pd_wifi_scan(nets, PD_WIFI_SCAN_MAX);
    esp_err_t r;
    if (n == PD_WIFI_SCAN_BUSY) {
        r = web_send_error(req, "409 Conflict", "BUSY",
                           "Das WLAN ist gerade beschäftigt. Bitte in ein paar Sekunden erneut suchen.");
    } else if (n < 0) {
        r = web_send_error(req, "500 Internal Server Error", "IO", "Die WLAN-Suche ist fehlgeschlagen.");
    } else {
        strcpy(json, "{\"ok\":true,\"networks\":[");
        bool first = true;
        for (int i = 0; i < n; i++) {
            if (!utf8_ok(nets[i].ssid)) {
                continue;
            }
            web_appendf(json, SCAN_JSON_SIZE, "%s{\"ssid\":", first ? "" : ",");
            first = false;
            web_json_str(json, SCAN_JSON_SIZE, nets[i].ssid);
            web_appendf(json, SCAN_JSON_SIZE, ",\"rssi\":%d,\"secure\":%s,\"saved\":%s}", nets[i].rssi,
                        nets[i].secure ? "true" : "false", nets[i].saved ? "true" : "false");
        }
        web_appendf(json, SCAN_JSON_SIZE, "]}");
        r = web_send_json(req, "200 OK", json);
    }
    free(nets);
    free(json);
    return r;
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

// ---- network settings: DHCP / static IPv4, hostname -------------------------------------------

// GET /api/network (admin): stored setting (static addresses "" if never set) and what the
// station interface has right now ("current", from DHCP or static), so the web UI can prefill the
// static form with the addresses DHCP assigned.
static esp_err_t h_network(httpd_req_t *req)
{
    pd_wifi_netcfg_t c;
    pd_wifi_ipv4_t cur;
    pd_wifi_get_netcfg(&c);
    const bool connected = pd_wifi_get_current_ipv4(&cur);
    // All values are validated dotted quads / hostname characters: no JSON escaping needed.
    char json[448];
    snprintf(json, sizeof(json),
             "{\"ok\":true,\"dhcp\":%s,\"ip\":\"%s\",\"mask\":\"%s\",\"gateway\":\"%s\",\"dns\":\"%s\",\"hostname\":\"%s\","
             "\"current\":{\"ip\":\"%s\",\"mask\":\"%s\",\"gateway\":\"%s\",\"dns\":\"%s\"},\"connected\":%s}",
             c.dhcp ? "true" : "false", c.addr.ip, c.addr.mask, c.addr.gateway, c.addr.dns, c.hostname, cur.ip, cur.mask,
             cur.gateway, cur.dns, connected ? "true" : "false");
    return web_send_json(req, "200 OK", json);
}

typedef enum {
    PARAM_MISSING,
    PARAM_OK,
    PARAM_BAD,      // malformed or too long for out
} param_t;

static param_t net_param(const char *params, const char *key, char *out, size_t len)
{
    char raw[100];
    const esp_err_t err = httpd_query_key_value(params, key, raw, sizeof(raw));
    if (err == ESP_ERR_NOT_FOUND) {
        return PARAM_MISSING;
    }
    if (err != ESP_OK || !web_url_decode(raw) || strlen(raw) >= len) {
        return PARAM_BAD;
    }
    memcpy(out, raw, strlen(raw) + 1);
    return PARAM_OK;
}

static const char *net_error_text(pd_wifi_net_check_t bad)
{
    switch (bad) {
    case PD_WIFI_NET_BAD_IP:
        return "Ungültige IP-Adresse: bitte im Format a.b.c.d angeben (nicht die Netz- oder Broadcast-Adresse und "
               "nicht die Adresse des Einrichtungs-WLANs).";
    case PD_WIFI_NET_BAD_MASK:
        return "Ungültige Subnetzmaske: zum Beispiel 255.255.255.0 (von 255.0.0.0 bis 255.255.255.252).";
    case PD_WIFI_NET_BAD_GATEWAY:
        return "Ungültiges Gateway: Es muss im selben Netz wie die IP-Adresse liegen und darf nicht die IP-Adresse "
               "selbst sein.";
    case PD_WIFI_NET_BAD_DNS:
        return "Ungültiger DNS-Server: bitte im Format a.b.c.d angeben oder leer lassen (dann gilt das Gateway).";
    case PD_WIFI_NET_BAD_HOSTNAME:
        return "Ungültiger Gerätename: 1 bis 31 Zeichen, nur a–z, 0–9 und Bindestrich, nicht am Anfang oder Ende.";
    default:
        return "Ungültige Netzwerkeinstellung.";
    }
}

// POST /api/settings/network (admin): dhcp=0|1, with dhcp=0 also ip, mask, gateway (required) and
// dns (empty = gateway); optional hostname. Form body or query string. Answers BEFORE the change
// takes effect: pd_wifi applies it 1-2 s later, the client may lose the connection then.
// {"ok":true,"restart":false,"ip":"<static address, empty with DHCP>","hostname":"..."}
static esp_err_t h_set_network(httpd_req_t *req)
{
    char params[384] = "";
    if (req->content_len > 0) {
        const esp_err_t err = web_read_body(req, params, sizeof(params));
        if (err != ESP_OK) {
            return err == ESP_ERR_INVALID_SIZE ? ESP_OK : err;   // too large: already answered
        }
    }
    if (!params[0]) {
        const esp_err_t qerr = httpd_req_get_url_query_str(req, params, sizeof(params));
        if (qerr == ESP_ERR_HTTPD_RESULT_TRUNC) {
            return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "Anfrage zu groß");
        }
        if (qerr != ESP_OK) {
            params[0] = '\0';
        }
    }

    pd_wifi_netcfg_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    char v[8];
    if (net_param(params, "dhcp", v, sizeof(v)) != PARAM_OK ||
        (strcmp(v, "0") && strcmp(v, "1") && strcmp(v, "true") && strcmp(v, "false"))) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                              "Bitte „dhcp“ angeben: 1 = Adresse per DHCP, 0 = feste Adresse.");
    }
    cfg.dhcp = strcmp(v, "1") == 0 || strcmp(v, "true") == 0;

    pd_wifi_net_check_t bad = PD_WIFI_NET_OK;
    if (!cfg.dhcp) {
        if (net_param(params, "ip", cfg.addr.ip, sizeof(cfg.addr.ip)) != PARAM_OK) {
            bad = PD_WIFI_NET_BAD_IP;
        } else if (net_param(params, "mask", cfg.addr.mask, sizeof(cfg.addr.mask)) != PARAM_OK) {
            bad = PD_WIFI_NET_BAD_MASK;
        } else if (net_param(params, "gateway", cfg.addr.gateway, sizeof(cfg.addr.gateway)) != PARAM_OK) {
            bad = PD_WIFI_NET_BAD_GATEWAY;
        } else if (net_param(params, "dns", cfg.addr.dns, sizeof(cfg.addr.dns)) == PARAM_BAD) {
            bad = PD_WIFI_NET_BAD_DNS;
        }
    }
    if (bad == PD_WIFI_NET_OK) {
        const param_t hp = net_param(params, "hostname", cfg.hostname, sizeof(cfg.hostname));
        if (hp == PARAM_BAD) {
            bad = PD_WIFI_NET_BAD_HOSTNAME;
        } else if (hp == PARAM_OK) {
            for (char *p = cfg.hostname; *p; p++) {   // host names are case-insensitive: store lower case
                if (*p >= 'A' && *p <= 'Z') {
                    *p = (char)(*p - 'A' + 'a');
                }
            }
        }
    }
    if (bad == PD_WIFI_NET_OK) {
        const esp_err_t err = pd_wifi_set_netcfg(&cfg, &bad);
        if (err != ESP_OK && bad == PD_WIFI_NET_OK) {
            return web_send_error(req, "500 Internal Server Error", "IO",
                                  "Die Netzwerkeinstellungen konnten nicht gespeichert werden.");
        }
    }
    if (bad != PD_WIFI_NET_OK) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", net_error_text(bad));
    }

    pd_wifi_netcfg_t now;
    pd_wifi_get_netcfg(&now);
    char json[128];
    snprintf(json, sizeof(json), "{\"ok\":true,\"restart\":false,\"ip\":\"%s\",\"hostname\":\"%s\"}",
             now.dhcp ? "" : now.addr.ip, now.hostname);
    return web_send_json(req, "200 OK", json);
}

#ifdef CONFIG_PD_HTTPS_OPTION
// The server certificate names the hostname and the station IP (tls.c). After a settings change
// it is renewed as soon as the new name/address is in effect (also after the 10-s button reset);
// like the "renew" button it takes effect with the next restart (status: https_restart).
// Runs in the HTTP server task via httpd_queue_work(): tls.c is not thread-safe and is otherwise
// only used from that task.
static const char *TAG = "web";

static void tls_followup(void *arg)
{
    const unsigned changes = (unsigned)(uintptr_t)arg;
    pd_tls_info_t t;
    pd_tls_get_info(&t);
    if (!t.cert_sha256[0] && !t.ca_sha256[0]) {
        return;   // no key material yet: it is made with the current name and address when HTTPS is switched on
    }
    esp_err_t err;
    if (changes & PD_WIFI_CHG_HOSTNAME) {
        // The root CA's name constraints permit "*.local", the private IPv4 ranges and the bare
        // hostname it was made for. A server certificate for a new bare hostname would violate
        // them (clients reject the whole chain), so a new CA is made - it must be installed on
        // the clients again.
        err = pd_tls_regenerate();
    } else {
        err = pd_tls_renew_cert();   // new IP address, same CA
    }
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "HTTPS certificate renewed for the new %s (in effect after a restart)",
                 (changes & PD_WIFI_CHG_HOSTNAME) ? "hostname, new root CA" : "address");
    } else {
        ESP_LOGE(TAG, "HTTPS certificate not renewed: %s", esp_err_to_name(err));
    }
}

/** pd_wifi callback (Wi-Fi task): hand the work to the HTTP server task. */
static void on_netcfg_changed(unsigned changes)
{
    if (s_server && httpd_queue_work(s_server, tls_followup, (void *)(uintptr_t)changes) != ESP_OK) {
        ESP_LOGW(TAG, "HTTPS certificate renewal not queued - renew it in the HTTPS settings");
    }
}
#endif

static const web_route_t s_routes[] = {
    {"/api/network", HTTP_GET, h_network, WEB_ROLE_ADMIN},
    {"/api/settings/network", HTTP_POST, h_set_network, WEB_ROLE_ADMIN},
    {"/api/wifi/list", HTTP_GET, h_list, WEB_ROLE_ADMIN},
    {"/api/wifi/scan", HTTP_GET, h_scan, WEB_ROLE_ADMIN},
    {"/api/wifi/add", HTTP_POST, h_add, WEB_ROLE_ADMIN},
    {"/api/wifi/delete", HTTP_POST, h_delete, WEB_ROLE_ADMIN},
    {"/api/wifi", HTTP_POST, h_add, WEB_ROLE_ADMIN},   // alias of version 2
};

esp_err_t web_wifi_register(httpd_handle_t server)
{
    s_server = server;
#ifdef CONFIG_PD_HTTPS_OPTION
    pd_wifi_set_netcfg_callback(on_netcfg_changed);
#endif
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
