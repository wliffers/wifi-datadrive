#include "web_static.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>
#include "lwip/sockets.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "web_common.h"

// index.html is gzip-compressed at build time (see CMakeLists.txt).
extern const char index_html_gz_start[] asm("_binary_index_html_gz_start");
extern const char index_html_gz_end[] asm("_binary_index_html_gz_end");
extern const char manifest_start[] asm("_binary_manifest_webmanifest_start");
extern const char manifest_end[] asm("_binary_manifest_webmanifest_end");
extern const char icon180_start[] asm("_binary_icon_180_png_start");
extern const char icon180_end[] asm("_binary_icon_180_png_end");
extern const char icon192_start[] asm("_binary_icon_192_png_start");
extern const char icon192_end[] asm("_binary_icon_192_png_end");
extern const char icon512_start[] asm("_binary_icon_512_png_start");
extern const char icon512_end[] asm("_binary_icon_512_png_end");

// ============================================================== captive portal
//
// While the setup access point is on, its DNS responder (pd_wifi/pd_dns.c) resolves every name
// to the stick. A request that came in through the access point but names another host (the
// connectivity checks: captive.apple.com/hotspot-detect.html, connectivitycheck.gstatic.com/
// generate_204, www.msftconnecttest.com/connecttest.txt, detectportal.firefox.com/canonical.html,
// ...) gets a 302 to the setup page, never a "success" answer, so the OS opens its sign-in sheet.
// Requests addressed to the stick itself (AP address, hostname, hostname.local) and everything
// arriving over the station interface are handled exactly as without the portal.

/** True if the request arrived on the setup access point's address (fills w either way). */
static bool via_setup_ap(httpd_req_t *req, pd_wifi_status_t *w)
{
    pd_wifi_get_status(w);
    if (!w->ap_active || !w->ap_ip[0]) {
        return false;
    }
    struct sockaddr_storage addr;
    socklen_t alen = sizeof(addr);
    if (getsockname(httpd_req_to_sockfd(req), (struct sockaddr *)&addr, &alen) != 0) {
        return false;
    }
    const uint8_t *ip = NULL;
    if (addr.ss_family == AF_INET) {
        ip = (const uint8_t *)&((struct sockaddr_in *)&addr)->sin_addr.s_addr;
#if CONFIG_LWIP_IPV6
    } else if (addr.ss_family == AF_INET6) {
        // The server listens dual-stack: IPv4 connections show up as ::ffff:a.b.c.d.
        const uint8_t *a = (const uint8_t *)&((struct sockaddr_in6 *)&addr)->sin6_addr;
        static const uint8_t v4mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF};
        if (memcmp(a, v4mapped, sizeof(v4mapped)) == 0) {
            ip = a + 12;
        }
#endif
    }
    if (!ip) {
        return false;
    }
    char local[16];
    snprintf(local, sizeof(local), "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
    return strcmp(local, w->ap_ip) == 0;
}

/** True if the Host header names the stick (AP address, hostname, hostname.local) or is absent. */
static bool host_is_ours(httpd_req_t *req, const pd_wifi_status_t *w)
{
    char host[72];
    const esp_err_t err = httpd_req_get_hdr_value_str(req, "Host", host, sizeof(host));
    if (err == ESP_ERR_NOT_FOUND) {
        return true;   // HTTP/1.0 without Host: nothing to correct
    }
    if (err != ESP_OK) {
        return false;   // longer than any of our names
    }
    char *colon = strchr(host, ':');
    if (colon) {
        *colon = '\0';
    }
    size_t n = strlen(host);
    if (n > 0 && host[n - 1] == '.') {
        host[--n] = '\0';   // fully qualified form "name."
    }
    if (strcasecmp(host, w->ap_ip) == 0 || (w->hostname[0] && strcasecmp(host, w->hostname) == 0)) {
        return true;
    }
    const size_t hl = strlen(w->hostname);
    return hl > 0 && n == hl + 6 && strncasecmp(host, w->hostname, hl) == 0 && strcasecmp(host + hl, ".local") == 0;
}

/** 302 to the setup page. "?captive=1" lets the page know it may be in the OS sign-in sheet. */
static esp_err_t send_portal_redirect(httpd_req_t *req, const pd_wifi_status_t *w)
{
    char loc[40];
    snprintf(loc, sizeof(loc), "http://%s/?captive=1", w->ap_ip);
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", loc);
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    if (req->content_len > 0) {
        httpd_resp_set_hdr(req, "Connection", "close");   // the body is not read
    }
    const esp_err_t err = httpd_resp_send(req, NULL, 0);
    return err == ESP_OK && req->content_len == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t web_static_not_found(httpd_req_t *req, httpd_err_code_t error)
{
    pd_wifi_status_t w;
    if (via_setup_ap(req, &w) && !host_is_ours(req, &w)) {
        return send_portal_redirect(req, &w);
    }
    // Same as the HTTP server's default handling of this error.
    httpd_resp_send_err(req, error, NULL);
    return ESP_FAIL;
}

// ============================================================== routes

static esp_err_t h_index(httpd_req_t *req)
{
    pd_wifi_status_t w;
    if (via_setup_ap(req, &w) && !host_is_ours(req, &w)) {
        return send_portal_redirect(req, &w);   // e.g. http://example.com/ typed while on the AP
    }
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
    httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
    return httpd_resp_send(req, index_html_gz_start, index_html_gz_end - index_html_gz_start);
}

static esp_err_t send_asset(httpd_req_t *req, const char *type, const char *start, const char *end)
{
    httpd_resp_set_type(req, type);
    httpd_resp_set_hdr(req, "Cache-Control", "max-age=86400");
    return httpd_resp_send(req, start, end - start);
}

static esp_err_t h_manifest(httpd_req_t *req)
{
    return send_asset(req, "application/manifest+json", manifest_start, manifest_end);
}

static esp_err_t h_icon180(httpd_req_t *req)
{
    return send_asset(req, "image/png", icon180_start, icon180_end);
}

static esp_err_t h_icon192(httpd_req_t *req)
{
    return send_asset(req, "image/png", icon192_start, icon192_end);
}

static esp_err_t h_icon512(httpd_req_t *req)
{
    return send_asset(req, "image/png", icon512_start, icon512_end);
}

// Everything here is reachable without login: the page shell and what "Add to Home Screen" needs.
static const web_route_t s_routes[] = {
    {"/", HTTP_GET, h_index, WEB_ROLE_NONE},
    {"/index.html", HTTP_GET, h_index, WEB_ROLE_NONE},
    {"/manifest.webmanifest", HTTP_GET, h_manifest, WEB_ROLE_NONE},
    {"/icon-180.png", HTTP_GET, h_icon180, WEB_ROLE_NONE},
    {"/icon-192.png", HTTP_GET, h_icon192, WEB_ROLE_NONE},
    {"/icon-512.png", HTTP_GET, h_icon512, WEB_ROLE_NONE},
};

esp_err_t web_static_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
