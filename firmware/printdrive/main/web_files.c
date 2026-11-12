#include "web_files.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "web_common.h"

static const char *TAG = "web";

#define LIST_BUF_SIZE (32 * 1024)   // packed file list, enough for ~1000 typical names

static esp_err_t h_files(httpd_req_t *req)
{
    char *list = malloc(LIST_BUF_SIZE);
    if (!list) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
    }
    drive_list_info_t info;
    const drive_result_t r = drive_list(list, LIST_BUF_SIZE, &info);
    if (r != DRIVE_OK) {
        free(list);
        return web_send_result(req, r);
    }
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_sendstr_chunk(req, "{\"ok\":true,\"files\":[");
    char item[DRIVE_FILE_NAME_MAX * 2 + 96];
    size_t pos = 0;
    for (uint32_t i = 0; i < info.count; i++) {
        uint32_t size, mtime;
        memcpy(&size, list + pos, sizeof(size));
        memcpy(&mtime, list + pos + sizeof(size), sizeof(mtime));
        const char *name = list + pos + 2 * sizeof(uint32_t);
        pos += 2 * sizeof(uint32_t) + strlen(name) + 1;
        item[0] = '\0';
        web_appendf(item, sizeof(item), "%s{\"name\":", i ? "," : "");
        web_json_str(item, sizeof(item), name);
        web_appendf(item, sizeof(item), ",\"size\":%lu,\"mtime\":%lu}", (unsigned long)size,
                    (unsigned long)mtime);
        httpd_resp_sendstr_chunk(req, item);
    }
    snprintf(item, sizeof(item), "],\"dirs\":%lu,\"truncated\":%s}", (unsigned long)info.dirs,
             info.truncated ? "true" : "false");
    httpd_resp_sendstr_chunk(req, item);
    free(list);
    return httpd_resp_sendstr_chunk(req, NULL);
}

/** Validate an upload before sending data: name, type, existence, free space. */
static esp_err_t h_upload_check(httpd_req_t *req)
{
    char name[DRIVE_NAME_MAX + 1], size_s[16];
    if (!web_query_param(req, "name", name, sizeof(name)) ||
        !web_query_param(req, "size", size_s, sizeof(size_s))) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    drive_result_t r = drive_check_name(name);
    if (r == DRIVE_OK) {
        // Begin + abort performs exactly the checks the real upload will do.
        const unsigned long long size = strtoull(size_s, NULL, 10);
        r = size > UINT32_MAX ? DRIVE_ERR_TOO_LARGE
                              : drive_upload_begin(name, (uint32_t)size, web_query_flag(req, "overwrite"));
        if (r == DRIVE_OK) {
            drive_upload_abort();
        }
    }
    return web_send_result(req, r);
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

static esp_err_t h_upload(httpd_req_t *req)
{
    uint8_t *const buf = web_io_buf();
    char name[DRIVE_NAME_MAX + 1];
    if (!web_query_param(req, "name", name, sizeof(name))) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    const size_t total = req->content_len;
    drive_result_t r = drive_upload_begin(name, (uint32_t)total, web_query_flag(req, "overwrite"));
    if (r != DRIVE_OK) {
        return drain_body(req, total) ? web_send_result(req, r) : ESP_FAIL;
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
                return drain_body(req, remaining) ? web_send_result(req, r) : ESP_FAIL;
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
    ESP_LOGI(TAG, "upload '%s' %u bytes in %lld ms (%.1f KB/s): %s", name, (unsigned)total, dt_ms,
             dt_ms ? (double)total / dt_ms : 0.0, drive_result_str(r));
    return web_send_result(req, r);
}

static esp_err_t h_delete(httpd_req_t *req)
{
    char name[DRIVE_FILE_NAME_MAX + 1];
    if (!web_query_param(req, "name", name, sizeof(name))) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    return web_send_result(req, drive_delete(name));
}

static esp_err_t h_rename(httpd_req_t *req)
{
    char name[DRIVE_FILE_NAME_MAX + 1], to[DRIVE_NAME_MAX + 1];
    if (!web_query_param(req, "name", name, sizeof(name)) || !web_query_param(req, "to", to, sizeof(to))) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    return web_send_result(req, drive_rename(name, to));
}

static esp_err_t h_download(httpd_req_t *req)
{
    char name[DRIVE_FILE_NAME_MAX + 1];
    if (!web_query_param(req, "name", name, sizeof(name))) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    int fd = -1;
    uint32_t size = 0;
    const drive_result_t r = drive_download_open(name, &fd, &size);
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
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, NULL, 0);
    }
    return err;
}

static const web_route_t s_routes[] = {
    {"/api/files", HTTP_GET, h_files, WEB_ROLE_USER},
    {"/api/download", HTTP_GET, h_download, WEB_ROLE_USER},
    {"/api/upload/check", HTTP_POST, h_upload_check, WEB_ROLE_USER},
    {"/api/upload", HTTP_POST, h_upload, WEB_ROLE_USER},
    {"/api/delete", HTTP_POST, h_delete, WEB_ROLE_USER},
    {"/api/rename", HTTP_POST, h_rename, WEB_ROLE_USER},
};

esp_err_t web_files_register(httpd_handle_t server)
{
    return web_register_routes(server, s_routes, sizeof(s_routes) / sizeof(s_routes[0]));
}
