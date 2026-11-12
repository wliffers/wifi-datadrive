#include "web_files.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "web_common.h"

static const char *TAG = "web";

#define LIST_BUF_SIZE (32 * 1024)                 // packed listing, enough for ~1000 typical names
#define PATH_BUF_SIZE (DRIVE_SRC_PATH_MAX + 1)    // any path parameter (checked by drive_check_path())
#define ITEM_BUF_SIZE (2 * PATH_BUF_SIZE + 96)    // one JSON entry or the head with "dir" (escaped)

// ------------------------------------------------------------------ parameters

typedef enum {
    PARAM_OK,
    PARAM_MISSING,
    PARAM_BAD,      // present, but too long or not decodable
} param_t;

/** Read a path parameter into out (PATH_BUF_SIZE bytes). */
static param_t path_param(httpd_req_t *req, const char *key, char *out)
{
    if (web_query_param(req, key, out, PATH_BUF_SIZE)) {
        return PARAM_OK;
    }
    // web_query_param() does not tell "missing" from "too long": look at the raw query.
    param_t res = PARAM_MISSING;
    char *query = malloc(CONFIG_HTTPD_MAX_URI_LEN + 1);
    if (query) {
        char v[2];
        if (httpd_req_get_url_query_str(req, query, CONFIG_HTTPD_MAX_URI_LEN + 1) == ESP_OK) {
            const esp_err_t e = httpd_query_key_value(query, key, v, sizeof(v));
            if (e == ESP_OK || e == ESP_ERR_HTTPD_RESULT_TRUNC) {
                res = PARAM_BAD;
            }
        }
        free(query);
    }
    return res;
}

static esp_err_t send_missing(httpd_req_t *req, const char *key)
{
    char msg[48];
    snprintf(msg, sizeof(msg), "%s fehlt oder ist ungültig", key);
    return web_send_error(req, "400 Bad Request", "BAD_REQUEST", msg);
}

/** Answer for a path parameter that is not PARAM_OK. */
static esp_err_t send_param_error(httpd_req_t *req, param_t p, const char *key)
{
    return p == PARAM_MISSING ? send_missing(req, key) : web_send_result(req, DRIVE_ERR_BAD_NAME);
}

/** Read and discard the rest of the request body so an error answer reaches the client.
 *  Returns false if the client is gone. */
static bool drain_body(httpd_req_t *req, size_t remaining)
{
    int timeouts = 0;
    while (remaining > 0) {
        const int n = httpd_req_recv(req, (char *)web_io_buf(),
                                     remaining < WEB_IO_BUF_SIZE ? remaining : WEB_IO_BUF_SIZE);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            return false;
        }
        remaining -= (size_t)n;
    }
    return true;
}

// ------------------------------------------------------------------ listing, download

static esp_err_t h_files(httpd_req_t *req)
{
    char *mem = malloc(LIST_BUF_SIZE + PATH_BUF_SIZE + ITEM_BUF_SIZE);
    if (!mem) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
    }
    char *const list = mem, *const dir = mem + LIST_BUF_SIZE, *const item = dir + PATH_BUF_SIZE;
    const param_t p = path_param(req, "dir", dir);
    if (p == PARAM_MISSING) {
        dir[0] = '\0';   // no dir: root folder
    }
    drive_list_info_t info;
    const drive_result_t r = p == PARAM_BAD ? DRIVE_ERR_BAD_NAME : drive_list(dir, list, LIST_BUF_SIZE, &info);
    if (r != DRIVE_OK) {
        free(mem);
        return web_send_result(req, r);
    }
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    item[0] = '\0';
    web_appendf(item, ITEM_BUF_SIZE, "{\"ok\":true,\"dir\":");
    web_json_str(item, ITEM_BUF_SIZE, dir);
    web_appendf(item, ITEM_BUF_SIZE, ",\"entries\":[");
    httpd_resp_sendstr_chunk(req, item);
    size_t pos = 0;
    for (uint32_t i = 0; i < info.count; i++) {
        uint32_t size, mtime;
        memcpy(&size, list + pos, sizeof(size));
        memcpy(&mtime, list + pos + 4, sizeof(mtime));
        const bool is_dir = list[pos + 8] == DRIVE_ENTRY_DIR;
        const char *name = list + pos + DRIVE_LIST_HDR;
        pos += DRIVE_LIST_HDR + strlen(name) + 1;
        item[0] = '\0';
        web_appendf(item, ITEM_BUF_SIZE, "%s{\"name\":", i ? "," : "");
        web_json_str(item, ITEM_BUF_SIZE, name);
        web_appendf(item, ITEM_BUF_SIZE, ",\"type\":\"%s\",\"size\":%lu,\"mtime\":%lu}", is_dir ? "dir" : "file",
                    (unsigned long)size, (unsigned long)mtime);
        httpd_resp_sendstr_chunk(req, item);
    }
    snprintf(item, ITEM_BUF_SIZE, "],\"truncated\":%s}", info.truncated ? "true" : "false");
    httpd_resp_sendstr_chunk(req, item);
    free(mem);
    return httpd_resp_sendstr_chunk(req, NULL);
}

static esp_err_t h_download(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    int fd = -1;
    uint32_t size = 0;
    const drive_result_t r = drive_download_open(path, &fd, &size);
    if (r != DRIVE_OK) {
        return web_send_result(req, r);
    }
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Content-Disposition", "attachment");
    uint8_t *const buf = web_io_buf();
    esp_err_t err = ESP_OK;
    ssize_t n;
    while ((n = read(fd, buf, WEB_IO_BUF_SIZE)) > 0) {
        if ((err = httpd_resp_send_chunk(req, (const char *)buf, n)) != ESP_OK) {
            break;
        }
    }
    drive_download_close(fd);
    if (err == ESP_OK && n < 0) {
        // Read error: do NOT send the terminating chunk, abort the connection instead so the
        // client sees a failed transfer rather than a silently truncated file.
        ESP_LOGE(TAG, "download '%s': read error, aborting", path);
        return ESP_FAIL;
    }
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, NULL, 0);
    }
    return err;
}

// ------------------------------------------------------------------ upload, push

/** Validate an upload before sending data: path, type, folder, existence, free space. */
static esp_err_t h_upload_check(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE], size_s[21];   // at most 20 digits, like the test server
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    if (!web_query_param(req, "size", size_s, sizeof(size_s)) || !size_s[0] ||
        strspn(size_s, "0123456789") != strlen(size_s)) {
        return send_missing(req, "size");
    }
    const unsigned long long size = strtoull(size_s, NULL, 10);   // overflow: ULLONG_MAX -> TOO_LARGE
    drive_result_t r = drive_upload_validate(path, size);
    if (r == DRIVE_OK) {
        // Begin + abort performs exactly the checks the real upload will do.
        r = drive_upload_begin(path, (uint32_t)size, web_query_flag(req, "overwrite"));
        if (r == DRIVE_OK) {
            drive_upload_abort();
        }
    }
    return web_send_result(req, r);
}

/**
 * Receive the request body as an upload to `path` (shared by upload and push). Returns ESP_FAIL
 * if the client is gone (nothing can be answered); otherwise ESP_OK with the result in *out,
 * which the caller still has to send. No upload session is left open in either case.
 */
static esp_err_t receive_upload(httpd_req_t *req, const char *path, bool overwrite, drive_result_t *out)
{
    uint8_t *const buf = web_io_buf();
    const size_t total = req->content_len;
    drive_result_t r = drive_upload_begin(path, (uint32_t)total, overwrite);
    if (r != DRIVE_OK) {
        *out = r;
        return drain_body(req, total) ? ESP_OK : ESP_FAIL;
    }

    const int64_t t0 = esp_timer_get_time();
    size_t remaining = total, fill = 0;
    int timeouts = 0;
    while (remaining > 0) {
        const size_t want = remaining < WEB_IO_BUF_SIZE - fill ? remaining : WEB_IO_BUF_SIZE - fill;
        const int n = httpd_req_recv(req, (char *)buf + fill, want);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            ESP_LOGW(TAG, "upload connection lost (%d)", n);
            drive_upload_abort();
            return ESP_FAIL;   // client is gone, nothing to answer
        }
        timeouts = 0;
        fill += (size_t)n;
        remaining -= (size_t)n;
        if (fill == WEB_IO_BUF_SIZE || remaining == 0) {
            r = drive_upload_write(buf, fill);
            fill = 0;
            if (r != DRIVE_OK) {
                drive_upload_abort();
                *out = r;
                return drain_body(req, remaining) ? ESP_OK : ESP_FAIL;
            }
        }
    }
    // Optional: original modification time of the file, and the client's clock (both local
    // time as seconds since 1970). The device has no clock of its own.
    char num[16];
    const uint32_t mtime =
        web_query_param(req, "mtime", num, sizeof(num)) ? (uint32_t)strtoul(num, NULL, 10) : 0;
    if (web_query_param(req, "now", num, sizeof(num)) && time(NULL) < 1700000000) {
        const struct timeval tv = {.tv_sec = (time_t)strtoul(num, NULL, 10)};
        if (tv.tv_sec > 1700000000) {
            settimeofday(&tv, NULL);
        }
    }
    r = drive_upload_finish(mtime);
    const int64_t dt_ms = (esp_timer_get_time() - t0) / 1000;
    ESP_LOGI(TAG, "upload '%s' %u bytes in %lld ms (%.1f KB/s): %s", path, (unsigned)total, dt_ms,
             dt_ms ? (double)total / dt_ms : 0.0, drive_result_str(r));
    *out = r;
    return ESP_OK;
}

static esp_err_t h_upload(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return drain_body(req, req->content_len) ? send_param_error(req, p, "path") : ESP_FAIL;
    }
    drive_result_t r;
    if (receive_upload(req, path, web_query_flag(req, "overwrite"), &r) != ESP_OK) {
        return ESP_FAIL;
    }
    return web_send_result(req, r);
}

/** Push answer: the usual result object plus "took_over" and "provided". */
static esp_err_t send_push(httpd_req_t *req, const char *status, const char *code, const char *msg, bool ok,
                           bool took_over, bool provided)
{
    char buf[400] = "";
    web_appendf(buf, sizeof(buf), "{\"ok\":%s,\"code\":\"%s\",\"error\":", ok ? "true" : "false", code);
    web_json_str(buf, sizeof(buf), msg);
    web_appendf(buf, sizeof(buf), ",\"took_over\":%s,\"provided\":%s}", took_over ? "true" : "false",
                provided ? "true" : "false");
    return web_send_json(req, status, buf);
}

static esp_err_t send_push_result(httpd_req_t *req, drive_result_t r, bool took_over, bool provided)
{
    return send_push(req, web_result_http_status(r), web_result_code(r), r == DRIVE_OK ? "" : drive_result_str(r),
                     r == DRIVE_OK, took_over, provided);
}

/**
 * One-step upload for scripts: validate, take the drive over from the USB host if needed
 * (rules of /api/mode?target=app), store through the normal upload session, then provide the
 * drive to the USB host again - also when storing failed - if this request took it over.
 */
static esp_err_t h_push(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE], v[8];
    const size_t total = req->content_len;
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        if (!drain_body(req, total)) {
            return ESP_FAIL;
        }
        return p == PARAM_MISSING
                   ? send_push(req, "400 Bad Request", "BAD_REQUEST", "path fehlt oder ist ungültig", false, false, false)
                   : send_push_result(req, DRIVE_ERR_BAD_NAME, false, false);
    }
    // overwrite defaults to 1 here (unlike /api/upload)
    const bool overwrite =
        !(web_query_param(req, "overwrite", v, sizeof(v)) && (strcmp(v, "0") == 0 || strcmp(v, "false") == 0));
    const bool force = web_query_flag(req, "force");

    // 1. Everything that does not need the drive, before taking it from the USB host.
    drive_result_t r = drive_upload_validate(path, total);
    bool took_over = false, provided = false;
    // 2. Take over. force only skips HOST_ACTIVE; HOST_WRITING and BUSY are never overridden.
    if (r == DRIVE_OK) {
        drive_status_t st;
        drive_get_status(&st);
        if (st.owner == DRIVE_OWNER_USB) {
            r = drive_set_owner(DRIVE_OWNER_APP, force);
            // do_mount_app() sets the owner before it knows whether the file system mounted
            // (NO_FS): decide from the real owner, so the drive is always given back.
            drive_get_status(&st);
            took_over = st.owner == DRIVE_OWNER_APP;
            if (took_over) {
                ESP_LOGI(TAG, "push: drive taken over from the USB host");
            }
        }
    }
    esp_err_t err = ESP_OK;
    if (r != DRIVE_OK) {
        err = drain_body(req, total) ? ESP_OK : ESP_FAIL;   // nothing taken over in this case
    } else {
        // 3. Store (no session is left open afterwards, whatever happened).
        err = receive_upload(req, path, overwrite, &r);
    }
    // 4. Give the drive back if this request took it.
    if (took_over) {
        const drive_result_t g = drive_set_owner(DRIVE_OWNER_USB, false);
        provided = g == DRIVE_OK;
        if (provided) {
            ESP_LOGI(TAG, "push: drive provided to the USB host again");
        } else {
            ESP_LOGW(TAG, "push: could not provide the drive again: %s", drive_result_str(g));
        }
    }
    if (err != ESP_OK) {
        return ESP_FAIL;
    }
    return send_push_result(req, r, took_over, provided);
}

// ------------------------------------------------------------------ folders and file management

static esp_err_t h_mkdir(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    return web_send_result(req, drive_mkdir(path));
}

static esp_err_t h_delete(httpd_req_t *req)
{
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    return web_send_result(req, drive_delete(path, web_query_flag(req, "recursive")));
}

/** Shared by move and copy: reads "from" and "to"; returns false after answering an error. */
static bool from_to_params(httpd_req_t *req, char *from, char *to, esp_err_t *ret)
{
    param_t p = path_param(req, "from", from);
    if (p != PARAM_OK) {
        *ret = send_param_error(req, p, "from");
        return false;
    }
    p = path_param(req, "to", to);
    if (p != PARAM_OK) {
        *ret = send_param_error(req, p, "to");
        return false;
    }
    return true;
}

static esp_err_t h_move(httpd_req_t *req)
{
    char from[PATH_BUF_SIZE], to[PATH_BUF_SIZE];
    esp_err_t ret = ESP_OK;
    if (!from_to_params(req, from, to, &ret)) {
        return ret;
    }
    return web_send_result(req, drive_move(from, to));
}

static esp_err_t h_copy(httpd_req_t *req)
{
    char from[PATH_BUF_SIZE], to[PATH_BUF_SIZE];
    esp_err_t ret = ESP_OK;
    if (!from_to_params(req, from, to, &ret)) {
        return ret;
    }
    // Blocks the (single) HTTP task until the copy is done; drive.c logs size and duration.
    return web_send_result(req, drive_copy(from, to));
}

static const web_route_t s_routes[] = {
    {"/api/files", HTTP_GET, h_files, WEB_ROLE_USER},
    {"/api/download", HTTP_GET, h_download, WEB_ROLE_USER},
    {"/api/upload/check", HTTP_POST, h_upload_check, WEB_ROLE_USER},
    {"/api/upload", HTTP_POST, h_upload, WEB_ROLE_USER},
    {"/api/push", HTTP_POST, h_push, WEB_ROLE_USER},
    {"/api/mkdir", HTTP_POST, h_mkdir, WEB_ROLE_USER},
    {"/api/delete", HTTP_POST, h_delete, WEB_ROLE_USER},
    {"/api/move", HTTP_POST, h_move, WEB_ROLE_USER},
    {"/api/copy", HTTP_POST, h_copy, WEB_ROLE_USER},
};

esp_err_t web_files_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
