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
#include "anim.h"
#include "pd_time.h"
#include "web_crypt.h"
#include "web_zip.h"

static const char *TAG = "web";

#define LIST_BUF_SIZE (32 * 1024)                 // listing limit ("truncated"), see list_entry(); no buffer
#define PATH_BUF_SIZE (DRIVE_SRC_PATH_MAX + 1)    // any path parameter (checked by drive_check_path())
#define ITEM_BUF_SIZE (2 * PATH_BUF_SIZE + 96)    // one JSON entry or the head with "dir" (escaped)
#define LIST_PAGE_SIZE 4096                       // packed entries read per drive_list_page() call
#define LIST_OUT_SIZE (WEB_IO_BUF_SIZE - PATH_BUF_SIZE - ITEM_BUF_SIZE - LIST_PAGE_SIZE)
_Static_assert(LIST_PAGE_SIZE >= DRIVE_LIST_ENTRY_MAX, "a page must hold the longest entry");
_Static_assert(LIST_OUT_SIZE >= 4 * ITEM_BUF_SIZE, "listing output part too small");

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

/**
 * Streamed listing in the shared I/O buffer (no buffer of its own): the folder is read in pages
 * (drive_list_page(), LIST_PAGE_SIZE of packed entries); the drive lock is held only while a page
 * is read and NEVER while sending, so a slow client cannot stall the drive task, owner changes,
 * lock or the button. The JSON is collected in the output part (LIST_OUT_SIZE) and only sent when
 * that part is full, so a folder of up to ~90 typical entries goes out in one piece after it was
 * read completely - every error can still be answered normally. Larger folders are sent in parts;
 * an error after the first part can only abort the connection (like a failed download). If the
 * folder changes between two pages, entries may be missed or repeated (see drive_list_page()).
 */
typedef struct {
    httpd_req_t *req;
    char *item;            // one JSON entry (ITEM_BUF_SIZE)
    char *out;             // answer not sent yet
    size_t len, cap;
    size_t used;           // LIST_BUF_SIZE accounting of the former packed list
    uint32_t count;
    bool truncated;
    bool sent;             // something went out already: an error can no longer be answered
    esp_err_t err;         // send error
} list_ctx_t;

static esp_err_t list_flush(list_ctx_t *c)
{
    if (!c->sent) {
        httpd_resp_set_type(c->req, "application/json");
        httpd_resp_set_hdr(c->req, "Cache-Control", "no-store");
        c->sent = true;
    }
    const esp_err_t err = c->len ? httpd_resp_send_chunk(c->req, c->out, c->len) : ESP_OK;
    c->len = 0;
    return err;
}

/** Append n bytes (n <= ITEM_BUF_SIZE < cap) to the answer, sending the collected part first if needed. */
static bool list_put(list_ctx_t *c, const char *s, size_t n)
{
    if (c->len + n > c->cap && (c->err = list_flush(c)) != ESP_OK) {
        return false;
    }
    memcpy(c->out + c->len, s, n);
    c->len += n;
    return true;
}

/** One entry into the answer. false only if sending failed (c->err). */
static bool list_entry(list_ctx_t *c, const char *name, bool is_dir, uint32_t size, uint32_t mtime)
{
    // Same limit as the former 32 KiB packed list (DRIVE_LIST_HDR + name + NUL per entry), so the
    // entries and "truncated" stay exactly as before.
    const size_t need = DRIVE_LIST_HDR + strlen(name) + 1;
    if (c->used + need > LIST_BUF_SIZE) {
        c->truncated = true;
        return true;   // a shorter name may still fit
    }
    c->used += need;
    char *const item = c->item;
    item[0] = '\0';
    web_appendf(item, ITEM_BUF_SIZE, "%s{\"name\":", c->count ? "," : "");
    web_json_str(item, ITEM_BUF_SIZE, name);
    web_appendf(item, ITEM_BUF_SIZE, ",\"type\":\"%s\",\"size\":%lu,\"mtime\":%lu}", is_dir ? "dir" : "file",
                (unsigned long)size, (unsigned long)mtime);
    c->count++;
    return list_put(c, item, strlen(item));
}

static esp_err_t h_files(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char *const mem = (char *)web_io_buf();   // free while this handler runs (one request at a time)
    if (!mem) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
    }
    char *const dir = mem, *const item = dir + PATH_BUF_SIZE, *const page = item + ITEM_BUF_SIZE;
    list_ctx_t c = {
        .req = req,
        .item = item,
        .out = page + LIST_PAGE_SIZE,
        .cap = LIST_OUT_SIZE,
    };
    const param_t p = path_param(req, "dir", dir);
    if (p == PARAM_MISSING) {
        dir[0] = '\0';   // no dir: root folder
    }
    if (p == PARAM_BAD) {
        return web_send_result(req, DRIVE_ERR_BAD_NAME);
    }
    item[0] = '\0';
    web_appendf(item, ITEM_BUF_SIZE, "{\"ok\":true,\"dir\":");
    web_json_str(item, ITEM_BUF_SIZE, dir);
    web_appendf(item, ITEM_BUF_SIZE, ",\"entries\":[");
    list_put(&c, item, strlen(item));   // nothing collected yet: always fits, never sends
    drive_result_t r = DRIVE_OK;
    uint32_t index = 0;
    bool more = true;
    while (more) {
        drive_list_info_t info;
        uint32_t next = index;
        r = drive_list_page(dir, index, page, LIST_PAGE_SIZE, &info, &next, &more);   // lock only in here
        if (r != DRIVE_OK) {
            break;
        }
        c.truncated |= info.truncated;   // never with LIST_PAGE_SIZE >= DRIVE_LIST_ENTRY_MAX
        // The drive lock is released: sending may block as long as the client wants.
        size_t pos = 0;
        for (uint32_t i = 0; i < info.count; i++) {
            const char *const e = page + pos;
            uint32_t size, mtime;
            memcpy(&size, e, sizeof(size));
            memcpy(&mtime, e + 4, sizeof(mtime));
            const char *const name = e + DRIVE_LIST_HDR;
            pos += DRIVE_LIST_HDR + strlen(name) + 1;
            if (!list_entry(&c, name, e[8] == DRIVE_ENTRY_DIR, size, mtime)) {
                return ESP_FAIL;   // client gone while sending
            }
        }
        if (more && next <= index) {
            r = DRIVE_ERR_IO;      // no progress (cannot happen): never loop forever
            break;
        }
        if (more && c.used + DRIVE_LIST_HDR + 2 > LIST_BUF_SIZE) {
            c.truncated = true;    // not even a one-character name fits any more: stop reading
            break;
        }
        index = next;
    }
    if (r != DRIVE_OK) {
        if (!c.sent) {
            return web_send_result(req, r);
        }
        ESP_LOGE(TAG, "listing '%s' failed after the first part (%s): aborting", dir, drive_result_str(r));
        return ESP_FAIL;   // no terminating chunk: the client sees a broken answer, not a short list
    }
    snprintf(item, ITEM_BUF_SIZE, "],\"truncated\":%s}", c.truncated ? "true" : "false");
    if (!list_put(&c, item, strlen(item))) {
        return ESP_FAIL;
    }
    if (!c.sent) {
        // Complete answer in one piece (Content-Length instead of chunks: fewer TLS records).
        httpd_resp_set_type(req, "application/json");
        httpd_resp_set_hdr(req, "Cache-Control", "no-store");
        return httpd_resp_send(req, c.out, c.len);
    }
    if (list_flush(&c) != ESP_OK) {
        return ESP_FAIL;
    }
    return httpd_resp_send_chunk(req, NULL, 0);
}

static esp_err_t h_download(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
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

/** Decimal number of at most 20 digits (like the test server); overflow gives ULLONG_MAX. */
static bool parse_num(const char *s, unsigned long long *out)
{
    if (!s[0] || strspn(s, "0123456789") != strlen(s)) {
        return false;
    }
    *out = strtoull(s, NULL, 10);   // overflow: ULLONG_MAX -> TOO_LARGE
    return true;
}

static bool size_param(httpd_req_t *req, unsigned long long *size)
{
    char size_s[21];
    return web_query_param(req, "size", size_s, sizeof(size_s)) && parse_num(size_s, size);
}

/** Validate an upload before sending data: path, type, folder, existence, free space. */
static esp_err_t h_upload_check(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    unsigned long long size;
    if (!size_param(req, &size)) {
        return send_missing(req, "size");
    }
    // The checks of the real upload, without touching a kept interrupted upload.
    return web_send_result(req, drive_upload_check(path, size, web_query_flag(req, "overwrite")));
}

/** GET /api/upload/resume?path=&size=: bytes held for exactly this path and size (0 = none). */
static esp_err_t h_upload_resume(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    unsigned long long size;
    if (!size_param(req, &size)) {
        return send_missing(req, "size");
    }
    uint32_t offset = 0;
    const drive_result_t r = drive_upload_resume_query(path, size, &offset);
    if (r != DRIVE_OK) {
        return web_send_result(req, r);
    }
    char buf[48];
    snprintf(buf, sizeof(buf), "{\"ok\":true,\"offset\":%lu}", (unsigned long)offset);
    return web_send_json(req, "200 OK", buf);
}

static esp_err_t send_resume_mismatch(httpd_req_t *req, uint32_t held)
{
    char buf[160];
    snprintf(buf, sizeof(buf),
             "{\"ok\":false,\"code\":\"RESUME_MISMATCH\",\"error\":\"Der Upload kann an dieser Stelle nicht "
             "fortgesetzt werden.\",\"offset\":%lu}",
             (unsigned long)held);
    return web_send_json(req, "409 Conflict", buf);
}

/**
 * Receive the request body into the upload session opened by the caller and finish it (shared
 * by upload and push). Returns ESP_FAIL if the client is gone (nothing can be answered);
 * otherwise ESP_OK with the result in *out, which the caller still has to send. No upload
 * session is left open in either case. keep_partial: a broken connection keeps the received
 * bytes for resuming (drive_upload_suspend()) instead of dropping them.
 */
static esp_err_t receive_body(httpd_req_t *req, const char *path, bool keep_partial, drive_result_t *out)
{
    uint8_t *const buf = web_io_buf();
    const size_t total = req->content_len;
    drive_result_t r;

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
            // Store what already arrived, so the kept part ends exactly where the client resumes.
            if (keep_partial && (fill == 0 || drive_upload_write(buf, fill) == DRIVE_OK)) {
                drive_upload_suspend();
            } else {
                drive_upload_abort();
            }
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
    // Optional: original modification time of the file, and the client's clock (both real unix
    // seconds). The clock is only taken while it is still unknown (pd_time decides).
    char num[16];
    const uint32_t mtime =
        web_query_param(req, "mtime", num, sizeof(num)) ? (uint32_t)strtoul(num, NULL, 10) : 0;
    if (web_query_param(req, "now", num, sizeof(num))) {
        pd_time_set_client((uint32_t)strtoul(num, NULL, 10));
    }
    r = drive_upload_finish(mtime);
    const int64_t dt_ms = (esp_timer_get_time() - t0) / 1000;
    ESP_LOGI(TAG, "upload '%s' %u bytes in %lld ms (%.1f KB/s): %s", path, (unsigned)total, dt_ms,
             dt_ms ? (double)total / dt_ms : 0.0, drive_result_str(r));
    *out = r;
    return ESP_OK;
}

/** Fresh upload of the whole body to `path`; same contract as receive_body(). */
static esp_err_t receive_upload(httpd_req_t *req, const char *path, bool overwrite, bool keep_partial,
                                drive_result_t *out)
{
    const drive_result_t r = drive_upload_begin(path, (uint32_t)req->content_len, overwrite);
    if (r != DRIVE_OK) {
        *out = r;
        return drain_body(req, req->content_len) ? ESP_OK : ESP_FAIL;
    }
    return receive_body(req, path, keep_partial, out);
}

/**
 * POST /api/upload. Without offset: fresh upload of the whole body. With offset (and size = total
 * size): the body is the rest from offset on and is appended to the interrupted upload of exactly
 * this path and size, if the server holds exactly offset bytes of it; else 409 RESUME_MISMATCH
 * with the server's "offset". A broken connection keeps the received part in both cases.
 */
static esp_err_t h_upload(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return drain_body(req, req->content_len) ? web_send_result(req, blk) : ESP_FAIL;
    }
    char path[PATH_BUF_SIZE];
    unsigned long long offset = 0, size = 0;
    const param_t po = path_param(req, "offset", path);   // path buffer used before the path is read
    const bool resume = po != PARAM_MISSING;
    if (resume && (po != PARAM_OK || !parse_num(path, &offset))) {
        return drain_body(req, req->content_len) ? send_missing(req, "offset") : ESP_FAIL;
    }
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return drain_body(req, req->content_len) ? send_param_error(req, p, "path") : ESP_FAIL;
    }
    const bool overwrite = web_query_flag(req, "overwrite");
    drive_result_t r;
    if (!resume) {
        if (receive_upload(req, path, overwrite, true, &r) != ESP_OK) {
            return ESP_FAIL;
        }
        return web_send_result(req, r);
    }

    if (!size_param(req, &size)) {
        return drain_body(req, req->content_len) ? send_missing(req, "size") : ESP_FAIL;
    }
    uint32_t held = 0;
    bool mismatch = false;
    r = drive_upload_validate(path, size);   // BAD_NAME, BAD_TYPE, TOO_LARGE, INVALID_FILE
    if (r == DRIVE_OK && offset > size) {
        drive_upload_resume_query(path, size, &held);
        mismatch = true;                     // can never match what the server holds
    } else if (r == DRIVE_OK && (unsigned long long)req->content_len != size - offset) {
        return drain_body(req, req->content_len)
                   ? web_send_error(req, "400 Bad Request", "BAD_REQUEST",
                                    "Content-Length muss size minus offset sein")
                   : ESP_FAIL;
    } else if (r == DRIVE_OK) {
        r = drive_upload_resume(path, (uint32_t)size, overwrite, (uint32_t)offset, &held, &mismatch);
    }
    if (mismatch || r != DRIVE_OK) {
        if (!drain_body(req, req->content_len)) {
            return ESP_FAIL;
        }
        return mismatch ? send_resume_mismatch(req, held) : web_send_result(req, r);
    }
    if (receive_body(req, path, true, &r) != ESP_OK) {
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
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return drain_body(req, req->content_len) ? send_push_result(req, blk, false, false) : ESP_FAIL;
    }
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
        // 3. Store (no session is left open afterwards, whatever happened; push keeps no partial
        //    upload: the drive is handed back to the USB host right after).
        err = receive_upload(req, path, overwrite, false, &r);
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
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    return web_send_result(req, drive_mkdir(path));
}

static esp_err_t h_delete(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char path[PATH_BUF_SIZE];
    const param_t p = path_param(req, "path", path);
    if (p != PARAM_OK) {
        return send_param_error(req, p, "path");
    }
    const bool recursive = web_query_flag(req, "recursive");
    if (recursive) {
        anim_set_busy(true);
    }
    const drive_result_t r = drive_delete(path, recursive);
    if (recursive) {
        anim_set_busy(false);
    }
    return web_send_result(req, r);
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
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char from[PATH_BUF_SIZE], to[PATH_BUF_SIZE];
    esp_err_t ret = ESP_OK;
    if (!from_to_params(req, from, to, &ret)) {
        return ret;
    }
    return web_send_result(req, drive_move(from, to));
}

static esp_err_t h_copy(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    char from[PATH_BUF_SIZE], to[PATH_BUF_SIZE];
    esp_err_t ret = ESP_OK;
    if (!from_to_params(req, from, to, &ret)) {
        return ret;
    }
    // Blocks the (single) HTTP task until the copy is done; drive.c logs size and duration.
    anim_set_busy(true);
    const drive_result_t r = drive_copy(from, to);
    anim_set_busy(false);
    return web_send_result(req, r);
}

static const web_route_t s_routes[] = {
    {"/api/files", HTTP_GET, h_files, WEB_ROLE_USER},
    {"/api/download", HTTP_GET, h_download, WEB_ROLE_USER},
    {"/api/upload/check", HTTP_POST, h_upload_check, WEB_ROLE_USER},
    {"/api/upload/resume", HTTP_GET, h_upload_resume, WEB_ROLE_USER},
    {"/api/zip", HTTP_GET, web_zip_get, WEB_ROLE_USER},
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
