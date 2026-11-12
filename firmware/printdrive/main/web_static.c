#include "web_static.h"

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

static esp_err_t h_index(httpd_req_t *req)
{
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
