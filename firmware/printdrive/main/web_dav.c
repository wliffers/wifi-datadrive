#include "web_dav.h"

/*
 * WebDAV at /dav/ (RFC 4918 class 1 plus a minimal class 2): the drive as a network drive for
 * Windows Explorer, macOS Finder and phone file apps.
 *
 * Drive access: only through drive.h, with the rules of the web file manager - path check
 * (drive_check_path() inside every drive function), file types and the atomic upload through
 * drive_upload_*. Only while the file manager owns the drive. A WebDAV request never takes the
 * drive over from the USB host: clients send long series of requests (PROPFIND, LOCK, PUT,
 * PROPPATCH, UNLOCK, PROPFIND ...), so a take-over per request would hand the medium back and
 * forth to the printer (a media change each time, possibly during a print), and taking it over
 * while the host has not ejected needs an explicit confirmation (force) that a WebDAV client
 * cannot give. Instead such requests get 503 with a short German text and Retry-After.
 * Locked or damaged encrypted drive: 423, as the other file handlers (web_crypt_block()).
 *
 * Authentication: HTTP Basic only (web_dav_authorize() -> auth_identify_basic_only()). WebDAV
 * clients cannot send the CSRF header X-PrintDrive, so the dispatcher exempts these routes from
 * that check. This is safe:
 *  - A cookie never authorizes a WebDAV request, not even together with a Basic header: only the
 *    Basic credentials of the request itself are checked. A cross-site page cannot add an
 *    "Authorization" header (it is not CORS-safelisted, so the browser sends a preflight OPTIONS
 *    first, and our OPTIONS answer never contains Access-Control-Allow-* - the preflight fails
 *    and the real request is never sent).
 *  - All writing methods (PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK, PROPPATCH, PROPFIND) are
 *    not "simple" methods, so a cross-site page can only send them after that failing preflight.
 *  - POST is not registered under /dav (httpd answers 405), and GET/HEAD only read.
 *  - Credentials a browser cached after a 401 from /dav/ are NOT limited to /dav/: browsers send
 *    cached Basic credentials with requests to every path of the host, cross-site ones included,
 *    and auth.c accepts Basic on the API routes as well. This stays harmless only because of two
 *    invariants of the whole web server: no GET route anywhere changes state, and every API POST
 *    needs the X-PrintDrive header (which a cross-site page cannot set without a preflight). A
 *    forged simple request can then at most read, and the page cannot see the answer.
 *
 * Memory: no permanent buffers beyond the placeholder table (about 1 KiB). Listings are read page
 * by page (drive_list_page()) into the shared 16 KiB I/O buffer of the web server (web_io_buf());
 * the drive lock is only held while a page is read, never while it is sent. Answers are streamed
 * in 1400-byte pieces from the stack. No malloc here; the drive layer allocates about 1.3 KiB
 * briefly per page.
 */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>
#include "anim.h"
#include "auth.h"
#include "drive.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs.h"
#include "web_common.h"
#include "web_crypt.h"

static const char *TAG = "dav";

#define DAV_REALM      "DataDrive"
#define DAV_URI        "/dav/?*"              // wildcard template: "/dav", "/dav/" and "/dav/..."
#define PATH_BUF       (DRIVE_SRC_PATH_MAX + 1)
#define OUT_BUF        1400                   // one streamed piece of an XML answer (stack)
#define HDR_BUF        1025                   // Destination header (the whole header block is 1 KiB)
#define LOCK_BODY_BUF  1024
#define DRAIN_LIMIT    (1024 * 1024)          // refused uploads up to this size are read and dropped
#define PH_MAX         4
#define PH_LIFETIME_US (5LL * 60 * 1000000)
#define NVS_NS         "pd_dav"
#define NVS_KEY        "on"

static const char ALLOW[] = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK";
static const char XML_HEAD[] = "<?xml version=\"1.0\" encoding=\"utf-8\"?>";

static const char MSG_NOT_FOUND[] = "Nicht gefunden.";
static const char MSG_BAD_PATH[] = "Ungültiger Pfad.";
static const char MSG_JUNK[] = "Diese Systemdatei wird auf dem DataDrive nicht gespeichert.";
static const char MSG_USB[] =
    "Das Laufwerk ist gerade dem USB-Gerät (Drucker oder PC) zugeordnet. Bitte in der Weboberfläche "
    "„Dateiverwaltung aktivieren“ wählen und es dann erneut versuchen.";
static const char MSG_CRYPT[] = "Das Laufwerk ist verschlüsselt und gesperrt. Bitte zuerst in der Weboberfläche entsperren.";
static const char MSG_OFF[] = "WebDAV ist in den Einstellungen des DataDrive ausgeschaltet.";
static const char MSG_DEFAULT_PW[] = "Bitte zuerst in der Weboberfläche das Passwort ändern.";
static const char MSG_PARENT[] = "Der übergeordnete Ordner fehlt.";
static const char MSG_CHUNKED[] =
    "Hochladen ohne Content-Length (chunked) wird nicht unterstützt. Bitte ein anderes WebDAV-Programm "
    "oder die Weboberfläche verwenden.";
static const char COLLECTION_HTML[] =
    "<!doctype html><meta charset=\"utf-8\"><title>DataDrive WebDAV</title>"
    "<p>Dies ist die WebDAV-Freigabe des DataDrive. Bitte als Netzlaufwerk verbinden oder die "
    "<a href=\"/\">Weboberfläche</a> verwenden.</p>";

static bool s_enabled = true;

bool web_dav_enabled(void)
{
    return s_enabled;
}

// ============================================================== paths

typedef enum {
    P_DAV,          // below /dav: drive path in out ("" = root folder)
    P_SERVER_ROOT,  // exactly "/" (Windows probes the server root)
    P_OTHER,
    P_BAD,          // malformed percent-encoding or too long
} path_kind_t;

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/** Percent-decode n bytes of a URL path into out. No '+' rule (in a path '+' is a plus sign).
 *  Leading and trailing '/' are removed. False if malformed, %00 or longer than out_len - 1.
 *  Everything else (empty segments, dot names, control characters, UTF-8) is checked by
 *  drive_check_path() inside the drive layer. */
static bool decode_path(const char *in, size_t n, char *out, size_t out_len)
{
    size_t o = 0;
    for (size_t i = 0; i < n; i++) {
        char c = in[i];
        if (c == '%') {
            if (i + 2 >= n) {
                return false;
            }
            const int h = hexval(in[i + 1]), l = hexval(in[i + 2]);
            if (h < 0 || l < 0 || (h == 0 && l == 0)) {
                return false;
            }
            c = (char)(h * 16 + l);
            i += 2;
        }
        if (o + 1 >= out_len) {
            return false;
        }
        out[o++] = c;
    }
    out[o] = '\0';
    size_t lead = 0;
    while (out[lead] == '/') {
        lead++;
    }
    if (lead) {
        memmove(out, out + lead, o - lead + 1);
        o -= lead;
    }
    while (o > 0 && out[o - 1] == '/') {
        out[--o] = '\0';
    }
    return true;
}

static path_kind_t parse_path(const char *uri, char *out, size_t out_len)
{
    const size_t n = strcspn(uri, "?#");
    if (n == 1 && uri[0] == '/') {
        out[0] = '\0';
        return P_SERVER_ROOT;
    }
    if (n >= 4 && strncmp(uri, "/dav", 4) == 0 && (n == 4 || uri[4] == '/')) {
        return decode_path(uri + 4, n - 4, out, out_len) ? P_DAV : P_BAD;
    }
    return P_OTHER;
}

/** Destination header: absolute URL ("http://host/dav/x") or absolute path ("/dav/x"). The host
 *  is not compared: the request reached us, and only paths below /dav are accepted. */
static path_kind_t parse_destination(const char *dst, char *out, size_t out_len)
{
    const char *scheme = strstr(dst, "://");
    if (scheme) {
        dst = strchr(scheme + 3, '/');
        if (!dst) {
            return P_BAD;
        }
    }
    return parse_path(dst, out, out_len);
}

/** Probes and metadata files of Windows and macOS: desktop.ini, Thumbs.db, ehthumbs.db (last
 *  segment) and every name with a leading dot (._*, .DS_Store, .Trashes, ...; the drive layer
 *  refuses and hides those anyway). Reads answer 404 at once, writes 403. */
static bool junk_name(const char *seg)
{
    return seg[0] == '.' || strcasecmp(seg, "desktop.ini") == 0 || strcasecmp(seg, "thumbs.db") == 0 ||
           strcasecmp(seg, "ehthumbs.db") == 0;
}

/** 8.3 alias of the drive layer's upload temp files in the root (".pd_upload.tmp" is stored as
 *  "PD_UPL~1.TMP", ".pd_old.tmp" as "PD_OLD~1.TMP"; with more numbers "PD_UP~12.TMP" etc.): every
 *  "PD_*~*.TMP". Through such a name a request would reach the hidden temp file, or an upload
 *  would end with its own temp file renamed onto the requested name. */
static bool temp_alias(const char *seg)
{
    const size_t n = strlen(seg);
    return n >= 8 && n <= 12 && strncasecmp(seg, "PD_", 3) == 0 && strchr(seg, '~') &&
           strcasecmp(seg + n - 4, ".TMP") == 0;
}

static bool junk_path(const char *path)
{
    const char *seg = path;
    while (*seg) {
        const char *slash = strchr(seg, '/');
        if (!slash) {
            return junk_name(seg) || (seg == path && temp_alias(seg));
        }
        if (seg[0] == '.') {
            return true;
        }
        seg = slash + 1;
    }
    return false;
}

/** True if path lies inside folder dir (case-insensitive, like FAT). */
static bool is_below(const char *path, const char *dir)
{
    const size_t n = strlen(dir);
    return n > 0 && strncasecmp(path, dir, n) == 0 && path[n] == '/';
}

// ============================================================== simple answers

static esp_err_t send_text(httpd_req_t *req, const char *status, const char *msg)
{
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, msg);
}

static esp_err_t send_empty(httpd_req_t *req, const char *status)
{
    httpd_resp_set_status(req, status);
    return httpd_resp_send(req, NULL, 0);
}

static esp_err_t send_401(httpd_req_t *req)
{
    httpd_resp_set_hdr(req, "WWW-Authenticate", "Basic realm=\"" DAV_REALM "\", charset=\"UTF-8\"");
    return send_text(req, "401 Unauthorized", "Anmeldung erforderlich (Benutzer und Passwort der Weboberfläche).");
}

/** Drive result -> WebDAV status and a short German text. */
static esp_err_t send_result(httpd_req_t *req, drive_result_t r)
{
    const char *status;
    const char *msg = drive_result_str(r);
    switch (r) {
    case DRIVE_ERR_NOT_FOUND:      status = "404 Not Found"; break;
    case DRIVE_ERR_BAD_NAME:
    case DRIVE_ERR_INCOMPLETE:     status = "400 Bad Request"; break;
    case DRIVE_ERR_BAD_TYPE:       status = "415 Unsupported Media Type"; break;
    case DRIVE_ERR_TOO_LARGE:      status = "413 Payload Too Large"; break;
    case DRIVE_ERR_NO_SPACE:       status = "507 Insufficient Storage"; break;
    case DRIVE_ERR_INVALID_FILE:   status = "422 Unprocessable Entity"; break;
    case DRIVE_ERR_EXISTS:
    case DRIVE_ERR_DIR_NOT_EMPTY:  status = "409 Conflict"; break;
    case DRIVE_ERR_IS_DIR:         status = "405 Method Not Allowed"; break;
    case DRIVE_ERR_LOCKED:         status = "423 Locked"; msg = MSG_CRYPT; break;
    case DRIVE_ERR_CRYPT_DAMAGED:  status = "423 Locked"; break;
    case DRIVE_ERR_WRONG_OWNER:
    case DRIVE_ERR_HOST_ACTIVE:
    case DRIVE_ERR_HOST_WRITING:
        status = "503 Service Unavailable";
        msg = MSG_USB;
        httpd_resp_set_hdr(req, "Retry-After", "30");
        break;
    case DRIVE_ERR_BUSY:
    case DRIVE_ERR_NO_FS:
    case DRIVE_ERR_CRYPT_UNAVAILABLE:
        status = "503 Service Unavailable";
        httpd_resp_set_hdr(req, "Retry-After", "10");
        break;
    default:                       status = "500 Internal Server Error"; break;
    }
    return send_text(req, status, msg);
}

/** Read and drop the rest of the request body, 16 KiB at a time. False if the client is gone. */
static bool drain(httpd_req_t *req)
{
    int timeouts = 0;
    while (true) {
        const int n = httpd_req_recv(req, (char *)web_io_buf(), WEB_IO_BUF_SIZE);
        if (n == 0) {
            return true;
        }
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n < 0) {
            return false;
        }
        timeouts = 0;
    }
}

/** Refusal of an upload whose body has not been read: small bodies are dropped so that the
 *  client gets the answer; larger ones are not worth it - answer and close the connection. */
static esp_err_t refuse_upload(httpd_req_t *req, drive_result_t r, const char *status, const char *msg)
{
    const bool close = req->content_len > DRAIN_LIMIT;
    if (!close && !drain(req)) {
        return ESP_FAIL;
    }
    if (close) {
        httpd_resp_set_hdr(req, "Connection", "close");
    }
    if (status) {
        send_text(req, status, msg);
    } else {
        send_result(req, r);
    }
    return close ? ESP_FAIL : ESP_OK;
}

/** Read the whole (small) request body into buf and terminate it. ESP_ERR_INVALID_SIZE if it
 *  does not fit (nothing read), ESP_FAIL if the connection broke. */
static esp_err_t read_body(httpd_req_t *req, char *buf, size_t cap)
{
    if (req->content_len >= cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    size_t got = 0;
    int timeouts = 0;
    while (got < req->content_len) {
        const int n = httpd_req_recv(req, buf + got, req->content_len - got);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            return ESP_FAIL;
        }
        got += (size_t)n;
    }
    buf[got] = '\0';
    return ESP_OK;
}

/** Send all bytes on the connection (used for GET/HEAD, whose header we write ourselves). */
static esp_err_t send_all(httpd_req_t *req, const char *p, size_t len)
{
    int timeouts = 0;
    while (len > 0) {
        const int n = httpd_send(req, p, len);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts <= 3) {
            continue;
        }
        if (n <= 0) {
            return ESP_FAIL;
        }
        timeouts = 0;
        p += n;
        len -= (size_t)n;
    }
    return ESP_OK;
}

/** Header of a file answer with its real Content-Length. httpd_resp_send() needs the whole body
 *  in RAM and httpd_resp_send_chunk() has no length; macOS Finder needs Content-Length on file
 *  answers, and HEAD must report the size without a body. */
static esp_err_t send_file_head(httpd_req_t *req, uint32_t size, uint32_t mtime)
{
    // ETag: the same value as getetag in PROPFIND; Last-Modified only with a known time (as
    // getlastmodified).
    char lm[64] = "";
    if (mtime != 0) {
        const time_t t = (time_t)mtime;
        struct tm tm;
        gmtime_r(&t, &tm);
        strftime(lm, sizeof(lm), "Last-Modified: %a, %d %b %Y %H:%M:%S GMT\r\n", &tm);
    }
    char h[320];
    const int n = snprintf(h, sizeof(h),
                           "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %lu\r\n"
                           "ETag: \"%lx-%lx\"\r\n%sAccept-Ranges: none\r\nCache-Control: no-cache\r\n\r\n",
                           (unsigned long)size, (unsigned long)size, (unsigned long)mtime, lm);
    return send_all(req, h, (size_t)n);
}

// ============================================================== streamed XML

typedef struct {
    httpd_req_t *req;
    esp_err_t err;
    size_t n;
    char buf[OUT_BUF];
} out_t;

static void out_init(out_t *o, httpd_req_t *req, const char *status)
{
    o->req = req;
    o->err = ESP_OK;
    o->n = 0;
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/xml; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
}

static void out_flush(out_t *o)
{
    if (o->n > 0 && o->err == ESP_OK) {
        o->err = httpd_resp_send_chunk(o->req, o->buf, (ssize_t)o->n);
    }
    o->n = 0;
}

static void out_mem(out_t *o, const char *s, size_t len)
{
    while (len > 0) {
        if (o->n == OUT_BUF) {
            out_flush(o);
        }
        const size_t k = len < OUT_BUF - o->n ? len : OUT_BUF - o->n;
        memcpy(o->buf + o->n, s, k);
        o->n += k;
        s += k;
        len -= k;
    }
}

static void out_str(out_t *o, const char *s)
{
    out_mem(o, s, strlen(s));
}

static void out_fmt(out_t *o, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void out_fmt(out_t *o, const char *fmt, ...)
{
    char tmp[320];   // longest use: a file's size/type/etag properties (about 170 bytes)
    va_list ap;
    va_start(ap, fmt);
    const int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n > 0) {
        out_mem(o, tmp, (size_t)n < sizeof(tmp) ? (size_t)n : sizeof(tmp) - 1);
    }
}

static bool unreserved(unsigned char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '.' ||
           c == '_' || c == '~' || c == '/';
}

/** Percent-encode a path (UTF-8 bytes, spaces, umlauts, '&', '%', '#', ...); '/' stays. The
 *  result only contains characters that need no XML escaping. */
static void out_enc(out_t *o, const char *s)
{
    static const char hex[] = "0123456789ABCDEF";
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (unreserved(*p)) {
            out_mem(o, (const char *)p, 1);
        } else {
            const char e[3] = {'%', hex[*p >> 4], hex[*p & 15]};
            out_mem(o, e, 3);
        }
    }
}

/** End of the answer: last piece and the terminating chunk. */
static esp_err_t out_end(out_t *o)
{
    out_flush(o);
    if (o->err == ESP_OK) {
        o->err = httpd_resp_send_chunk(o->req, NULL, 0);
    }
    return o->err;
}

// ============================================================== drive helpers

typedef struct {
    bool is_dir;
    uint32_t size;
    uint32_t mtime;   // seconds since 1970, 0 = unknown (folders)
} entry_t;

/** Owner, file system and encryption state, before any drive operation. */
static drive_result_t drive_ready(drive_status_t *st)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return blk;
    }
    drive_get_status(st);
    if (st->owner != DRIVE_OWNER_APP) {
        return DRIVE_ERR_WRONG_OWNER;
    }
    return st->fs_ok ? DRIVE_OK : DRIVE_ERR_NO_FS;
}

/** The only directory iteration this module uses: one page of drive_list_page() into the shared
 *  I/O buffer (packed entries, see DRIVE_LIST_HDR), starting at visible entry number start. The
 *  drive lock is released when it returns, so the page may be sent over the network. */
static drive_result_t list_page(const char *dir, uint32_t start, drive_list_info_t *info, uint32_t *next, bool *more)
{
    return drive_list_page(dir, start, (char *)web_io_buf(), WEB_IO_BUF_SIZE, info, next, more);
}

/** Next entry of the listing in the I/O buffer; returns its name. */
static const char *list_next(size_t *pos, entry_t *e)
{
    const char *b = (const char *)web_io_buf() + *pos;
    memcpy(&e->size, b, sizeof(e->size));
    memcpy(&e->mtime, b + 4, sizeof(e->mtime));
    e->is_dir = b[8] == DRIVE_ENTRY_DIR;
    const char *name = b + DRIVE_LIST_HDR;
    *pos += DRIVE_LIST_HDR + strlen(name) + 1;
    return name;
}

/** Type, size and time of an existing path ("" = root) by listing its parent folder page by page
 *  (path is modified temporarily). Uses the I/O buffer. Note: the name is compared as given
 *  (case-insensitive ASCII); an 8.3 alias or a different case of a non-ASCII letter is not found
 *  here even though the drive layer resolves it. */
static drive_result_t lookup(char *path, entry_t *e)
{
    memset(e, 0, sizeof(*e));
    if (path[0] == '\0') {
        e->is_dir = true;
        return DRIVE_OK;
    }
    char *slash = strrchr(path, '/');
    const char *name = slash ? slash + 1 : path;
    uint32_t start = 0;
    bool more = true;
    drive_result_t r = DRIVE_OK;
    while (more && r == DRIVE_OK) {
        drive_list_info_t info;
        uint32_t next = 0;
        if (slash) {
            *slash = '\0';
        }
        r = list_page(slash ? path : "", start, &info, &next, &more);
        if (slash) {
            *slash = '/';
        }
        if (r != DRIVE_OK) {
            break;   // parent missing (or not a folder): NOT_FOUND
        }
        size_t pos = 0;
        for (uint32_t i = 0; i < info.count; i++) {
            entry_t x;
            if (strcasecmp(list_next(&pos, &x), name) == 0) {
                *e = x;
                return DRIVE_OK;
            }
        }
        if (more && next <= start) {
            r = DRIVE_ERR_IO;   // cannot happen (see drive_list_page()): never loop forever
        }
        start = next;
    }
    return r == DRIVE_OK ? DRIVE_ERR_NOT_FOUND : r;
}

// ============================================================== placeholders
//
// Windows creates a file with a 0-byte PUT (or a LOCK on a new name) and only then sends the
// content, and looks at the file in between (PROPFIND, PROPPATCH, LOCK). The drive layer never
// stores empty files (upload rules), so such a name is kept here in RAM and reported as an empty
// file until the real PUT arrives, it is deleted or moved, or 5 minutes have passed. A real file
// of the same name always wins.
//
// "Shadow" placeholder: a 0-byte PUT onto an EXISTING file (a client truncating it before it
// writes the new content). The drive cannot store the empty file, and deleting the real one would
// lose it for good if the content never follows, so the real file stays on the medium untouched
// and WebDAV reports it as empty (PROPFIND, GET, HEAD) while the shadow lives - but only as long
// as the real file is still the one that was shadowed (same size and time); a file changed in
// between by the web interface wins. The following PUT replaces the real file and ends the shadow;
// if nothing follows, the shadow expires and the old content is visible again. Elsewhere (web
// interface, printer) the real file is never hidden.

typedef struct {
    bool used;
    bool shadow;          // shadows the real file real_size/real_mtime
    int64_t t_us;
    uint32_t mtime;
    uint32_t real_size;
    uint32_t real_mtime;
    char path[DRIVE_PATH_MAX + 1];
} placeholder_t;

static placeholder_t s_ph[PH_MAX];

static void ph_expire(void)
{
    const int64_t now = esp_timer_get_time();
    for (int i = 0; i < PH_MAX; i++) {
        if (s_ph[i].used && now - s_ph[i].t_us > PH_LIFETIME_US) {
            s_ph[i].used = false;
        }
    }
}

static placeholder_t *ph_find(const char *path)
{
    ph_expire();
    for (int i = 0; i < PH_MAX; i++) {
        if (s_ph[i].used && strcasecmp(s_ph[i].path, path) == 0) {
            return &s_ph[i];
        }
    }
    return NULL;
}

/** Add (or renew) the placeholder of path; real != NULL: a shadow of that existing file. */
static void ph_add(const char *path, const entry_t *real)
{
    if (strlen(path) > DRIVE_PATH_MAX) {
        return;   // cannot happen: the name passed the rules for new paths
    }
    placeholder_t *p = ph_find(path);
    for (int i = 0; !p && i < PH_MAX; i++) {
        if (!s_ph[i].used) {
            p = &s_ph[i];
        }
    }
    if (!p) {   // all in use: replace the oldest
        p = &s_ph[0];
        for (int i = 1; i < PH_MAX; i++) {
            if (s_ph[i].t_us < p->t_us) {
                p = &s_ph[i];
            }
        }
    }
    const time_t now = time(NULL);
    p->used = true;
    p->t_us = esp_timer_get_time();
    p->mtime = now > 1700000000 ? (uint32_t)now : 0;   // only with a set clock
    p->shadow = real != NULL;
    p->real_size = real ? real->size : 0;
    p->real_mtime = real ? real->mtime : 0;
    strlcpy(p->path, path, sizeof(p->path));
}

/** The real file e that exists under the name of placeholder p, as WebDAV reports it: the empty
 *  file of a shadow that still covers exactly this file. Otherwise the real file wins and the
 *  placeholder is dropped (false). */
static bool ph_covers(placeholder_t *p, entry_t *e)
{
    if (p->shadow && !e->is_dir && e->size == p->real_size && e->mtime == p->real_mtime) {
        *e = (entry_t){.is_dir = false, .size = 0, .mtime = p->mtime};
        return true;
    }
    p->used = false;
    return false;
}

/** Remove the placeholder of path and all below it (folder deleted or moved). */
static void ph_remove_tree(const char *path)
{
    for (int i = 0; i < PH_MAX; i++) {
        if (s_ph[i].used && (strcasecmp(s_ph[i].path, path) == 0 || is_below(s_ph[i].path, path))) {
            s_ph[i].used = false;
        }
    }
}

/** Name of placeholder p if it lies directly in folder dir, else NULL. */
static const char *ph_name_in(const placeholder_t *p, const char *dir)
{
    const size_t n = strlen(dir);
    const char *rest = p->path;
    if (n > 0) {
        if (!is_below(p->path, dir)) {
            return NULL;
        }
        rest = p->path + n + 1;
    }
    return strchr(rest, '/') ? NULL : rest;
}

// ============================================================== PROPFIND

static void out_time_props(out_t *o, uint32_t mtime)
{
    if (mtime == 0) {
        return;
    }
    // FAT times are converted by the drive layer with the system time zone: gmtime gives UTC,
    // which is what getlastmodified (RFC 1123, GMT) and creationdate (RFC 3339) need.
    const time_t t = (time_t)mtime;
    struct tm tm;
    gmtime_r(&t, &tm);
    char buf[40];
    strftime(buf, sizeof(buf), "%a, %d %b %Y %H:%M:%S GMT", &tm);
    out_fmt(o, "<D:getlastmodified>%s</D:getlastmodified>", buf);
    strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
    out_fmt(o, "<D:creationdate>%s</D:creationdate>", buf);
}

/** One <D:response>. dir/name: the resource is dir + "/" + name, or dir itself (name NULL).
 *  self: the requested resource (gets the lock and quota properties). No displayname: Windows
 *  would show it instead of the name from the href. */
static void out_response(out_t *o, const char *dir, const char *name, const entry_t *e, const drive_status_t *st)
{
    out_str(o, "<D:response><D:href>/dav/");
    out_enc(o, dir);
    if (name) {
        if (dir[0]) {
            out_str(o, "/");
        }
        out_enc(o, name);
    }
    if (e->is_dir && (dir[0] || name)) {
        out_str(o, "/");
    }
    out_str(o, "</D:href><D:propstat><D:prop>");
    if (e->is_dir) {
        out_str(o, "<D:resourcetype><D:collection/></D:resourcetype>");
    } else {
        out_fmt(o,
                "<D:resourcetype/><D:getcontentlength>%lu</D:getcontentlength>"
                "<D:getcontenttype>application/octet-stream</D:getcontenttype>"
                "<D:getetag>\"%lx-%lx\"</D:getetag>",
                (unsigned long)e->size, (unsigned long)e->size, (unsigned long)e->mtime);
    }
    out_time_props(o, e->mtime);
    if (st) {
        out_str(o,
                "<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/>"
                "</D:locktype></D:lockentry><D:lockentry><D:lockscope><D:shared/></D:lockscope><D:locktype>"
                "<D:write/></D:locktype></D:lockentry></D:supportedlock><D:lockdiscovery/>");
        if (e->is_dir) {
            // RFC 4331: Windows shows the free space of the mapped drive, Finder asks for it too.
            out_fmt(o, "<D:quota-available-bytes>%llu</D:quota-available-bytes><D:quota-used-bytes>%llu</D:quota-used-bytes>",
                    (unsigned long long)st->free_bytes,
                    (unsigned long long)(st->total_bytes > st->free_bytes ? st->total_bytes - st->free_bytes : 0));
        }
    }
    out_str(o, "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>");
}

static void out_plain_collection(out_t *o, const char *href)
{
    out_fmt(o,
            "<D:response><D:href>%s</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>",
            href);
}

static esp_err_t h_propfind(httpd_req_t *req)
{
    char path[PATH_BUF];
    const path_kind_t kind = parse_path(req->uri, path, sizeof(path));
    // Request headers are gone once the answer has started: read Depth first. Missing means
    // "infinity" (RFC 4918 9.1), which is refused like an explicit one.
    char d[12] = "";
    int depth = -1;
    if (httpd_req_get_hdr_value_len(req, "Depth") < sizeof(d) &&
        httpd_req_get_hdr_value_str(req, "Depth", d, sizeof(d)) == ESP_OK) {
        depth = strcmp(d, "0") == 0 ? 0 : (strcmp(d, "1") == 0 ? 1 : -1);
    }
    if (depth < 0) {
        httpd_resp_set_status(req, "403 Forbidden");
        httpd_resp_set_type(req, "application/xml; charset=utf-8");
        return httpd_resp_sendstr(req, "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                                       "<D:error xmlns:D=\"DAV:\"><D:propfind-finite-depth/></D:error>");
    }
    // The request body (which properties) is ignored: the answer always has the same set.
    // httpd drops the unread body after the handler.
    out_t o;
    if (kind == P_SERVER_ROOT) {
        // Windows looks at the server root before it maps http://host/dav: one folder "dav".
        out_init(&o, req, "207 Multi-Status");
        out_str(&o, XML_HEAD);
        out_str(&o, "<D:multistatus xmlns:D=\"DAV:\">");
        out_plain_collection(&o, "/");
        if (depth == 1) {
            out_plain_collection(&o, "/dav/");
        }
        out_str(&o, "</D:multistatus>");
        return out_end(&o);
    }
    if (kind != P_DAV) {
        return send_text(req, kind == P_BAD ? "400 Bad Request" : "404 Not Found",
                         kind == P_BAD ? MSG_BAD_PATH : MSG_NOT_FOUND);
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (junk_path(path)) {
        return send_text(req, "404 Not Found", MSG_NOT_FOUND);
    }
    entry_t self = {.is_dir = true};
    drive_list_info_t info = {0};
    uint32_t start = 0, next = 0;
    bool listed = false, more = false;
    r = DRIVE_OK;
    if (depth == 1) {
        // First page: a folder gives its entries; NOT_FOUND for files and missing paths.
        r = list_page(path, start, &info, &next, &more);
        listed = r == DRIVE_OK;
    }
    if (path[0] != '\0' && !listed && (depth == 0 || r == DRIVE_ERR_NOT_FOUND)) {
        r = lookup(path, &self);
        placeholder_t *ph = ph_find(path);
        if (r == DRIVE_ERR_NOT_FOUND && ph) {
            self = (entry_t){.is_dir = false, .size = 0, .mtime = ph->mtime};
            r = DRIVE_OK;
        } else if (r == DRIVE_OK && ph) {
            ph_covers(ph, &self);
        }
    }
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }

    out_init(&o, req, "207 Multi-Status");
    out_str(&o, XML_HEAD);
    out_str(&o, "<D:multistatus xmlns:D=\"DAV:\">");
    out_response(&o, path, NULL, &self, &st);
    if (listed) {
        // Page by page: the drive lock is only held while a page is read, never while it is sent.
        ph_expire();
        bool covered[PH_MAX] = {false};   // shadow placeholders already reported with their real file
        while (o.err == ESP_OK) {
            size_t pos = 0;
            for (uint32_t i = 0; i < info.count; i++) {
                entry_t e;
                const char *name = list_next(&pos, &e);
                if (junk_name(name)) {
                    continue;
                }
                for (int k = 0; k < PH_MAX; k++) {   // a real file replaces its placeholder (or is shadowed)
                    const char *pn = s_ph[k].used ? ph_name_in(&s_ph[k], path) : NULL;
                    if (pn && strcasecmp(pn, name) == 0 && ph_covers(&s_ph[k], &e)) {
                        covered[k] = true;
                    }
                }
                out_response(&o, path, name, &e, NULL);
            }
            if (!more) {
                break;
            }
            if (next <= start) {
                r = DRIVE_ERR_IO;   // cannot happen (see drive_list_page()): never loop forever
                break;
            }
            start = next;
            r = list_page(path, start, &info, &next, &more);
            if (r != DRIVE_OK) {
                break;
            }
        }
        if (o.err != ESP_OK) {
            return o.err;   // client gone
        }
        if (r != DRIVE_OK) {
            // The 207 has started: a shortened listing would look complete to the client. Close the
            // connection without the end of the answer instead, so the request visibly fails.
            ESP_LOGW(TAG, "PROPFIND '%s': listing broke off after entry %lu: %s", path, (unsigned long)start,
                     drive_result_str(r));
            return ESP_FAIL;
        }
        for (int k = 0; k < PH_MAX; k++) {
            const char *pn = s_ph[k].used && !covered[k] ? ph_name_in(&s_ph[k], path) : NULL;
            if (pn) {
                const entry_t e = {.is_dir = false, .size = 0, .mtime = s_ph[k].mtime};
                out_response(&o, path, pn, &e, NULL);
            }
        }
    }
    out_str(&o, "</D:multistatus>");
    return out_end(&o);
}

// ============================================================== GET, HEAD

static esp_err_t get_or_head(httpd_req_t *req, bool head)
{
    char path[PATH_BUF];
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (junk_path(path)) {
        return send_text(req, "404 Not Found", MSG_NOT_FOUND);
    }
    // Time (Last-Modified, ETag as getetag) from the listing of the parent folder - before the file
    // is opened: lookup() uses the I/O buffer, and the drive layer reports no time on open.
    entry_t e = {0};
    const bool known = path[0] && lookup(path, &e) == DRIVE_OK && !e.is_dir;
    placeholder_t *ph = path[0] ? ph_find(path) : NULL;
    if (known && ph && ph_covers(ph, &e)) {
        return send_file_head(req, 0, e.mtime);   // shadowed by a 0-byte PUT: reported empty
    }
    int fd = -1;
    uint32_t size = 0;
    r = path[0] ? drive_download_open(path, &fd, &size) : DRIVE_ERR_IS_DIR;
    if (r == DRIVE_ERR_IS_DIR) {
        // A folder in a browser (after the 401 login): short hint instead of a listing.
        httpd_resp_set_type(req, "text/html; charset=utf-8");
        httpd_resp_set_hdr(req, "Cache-Control", "no-store");
        return head ? httpd_resp_send(req, NULL, 0) : httpd_resp_sendstr(req, COLLECTION_HTML);
    }
    if (r == DRIVE_ERR_NOT_FOUND && (ph = ph_find(path)) != NULL) {
        return send_file_head(req, 0, ph->mtime);
    }
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    esp_err_t err = send_file_head(req, size, known && e.size == size ? e.mtime : 0);
    uint32_t left = head ? 0 : size;
    uint8_t *const buf = web_io_buf();
    while (err == ESP_OK && left > 0) {
        const ssize_t n = read(fd, buf, left < WEB_IO_BUF_SIZE ? left : WEB_IO_BUF_SIZE);
        if (n <= 0) {
            // Content-Length is already out: closing the connection makes the client see a failed
            // transfer instead of a silently short file.
            ESP_LOGE(TAG, "GET '%s': read error, aborting", path);
            err = ESP_FAIL;
            break;
        }
        err = send_all(req, (const char *)buf, (size_t)n);
        left -= (uint32_t)n;
    }
    drive_download_close(fd);
    return err;
}

static esp_err_t h_get(httpd_req_t *req)
{
    return get_or_head(req, false);
}

static esp_err_t h_head(httpd_req_t *req)
{
    return get_or_head(req, true);
}

// ============================================================== PUT

static esp_err_t h_put(httpd_req_t *req)
{
    char path[PATH_BUF];
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return refuse_upload(req, DRIVE_ERR_BAD_NAME, "400 Bad Request", MSG_BAD_PATH);
    }
    // Chunked body (macOS Finder): esp_http_server has no decoder for request bodies - content_len
    // would be 0 and the chunks would be parsed as the next request. Any Transfer-Encoding other
    // than exactly "identity" (also "identity, chunked") is refused, and so is a PUT without
    // Content-Length (its body length is unknown; only an explicit "Content-Length: 0" is an empty
    // file). The connection is closed: the body that follows cannot be told apart from a request.
    const size_t te_len = httpd_req_get_hdr_value_len(req, "Transfer-Encoding");
    bool length_ok = httpd_req_get_hdr_value_len(req, "Content-Length") > 0;
    if (te_len > 0) {
        char te[16] = "";
        length_ok = length_ok && te_len < sizeof(te) &&
                    httpd_req_get_hdr_value_str(req, "Transfer-Encoding", te, sizeof(te)) == ESP_OK &&
                    strcasecmp(te, "identity") == 0;
    }
    if (!length_ok) {
        httpd_resp_set_hdr(req, "Connection", "close");
        send_text(req, "411 Length Required", MSG_CHUNKED);
        return ESP_FAIL;
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return refuse_upload(req, r, NULL, NULL);
    }
    if (path[0] == '\0') {
        return refuse_upload(req, DRIVE_ERR_IS_DIR, NULL, NULL);
    }
    if (junk_path(path)) {
        ESP_LOGD(TAG, "PUT '%s' refused (system file)", path);
        return refuse_upload(req, DRIVE_ERR_BAD_NAME, "403 Forbidden", MSG_JUNK);
    }
    const size_t total = req->content_len;
    if (total == 0) {
        // Windows creates the file empty first. drive_upload_check() performs the checks of a real
        // upload (name, type, folder, existence, space) without touching anything - in particular a
        // kept interrupted upload of the web interface survives.
        r = drive_upload_check(path, 1, false);
        if (r == DRIVE_OK) {
            ph_add(path, NULL);
            return send_empty(req, "201 Created");
        }
        if (r == DRIVE_ERR_EXISTS) {
            // An existing file truncated before the new content follows (Windows does this when
            // it overwrites a file): a shadow placeholder, see above. The real file stays.
            entry_t e = {0};
            r = drive_upload_check(path, 1, true);
            if (r == DRIVE_OK && (lookup(path, &e) != DRIVE_OK || e.is_dir)) {
                // Found by the drive layer only under another spelling (8.3 alias, case of a
                // non-ASCII letter): the real file cannot be told apart reliably - refuse rather
                // than claim an empty file.
                return send_text(req, "409 Conflict",
                                 "Die vorhandene Datei kann unter diesem Namen nicht geleert werden.");
            }
            if (r == DRIVE_OK) {
                ph_add(path, &e);
                return send_empty(req, "204 No Content");
            }
        }
        return r == DRIVE_ERR_NOT_FOUND ? send_text(req, "409 Conflict", MSG_PARENT) : send_result(req, r);
    }
    bool existed = false;
    r = drive_upload_begin(path, (uint32_t)total, false);
    if (r == DRIVE_ERR_EXISTS) {
        existed = true;
        r = drive_upload_begin(path, (uint32_t)total, true);   // PUT replaces (atomic: old file kept until done)
    }
    if (r != DRIVE_OK) {
        return r == DRIVE_ERR_NOT_FOUND ? refuse_upload(req, r, "409 Conflict", MSG_PARENT)
                                        : refuse_upload(req, r, NULL, NULL);
    }
    uint8_t *const buf = web_io_buf();
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
            ESP_LOGW(TAG, "PUT '%s': connection lost (%d)", path, n);
            drive_upload_abort();
            return ESP_FAIL;
        }
        timeouts = 0;
        fill += (size_t)n;
        remaining -= (size_t)n;
        if (fill == WEB_IO_BUF_SIZE || remaining == 0) {
            r = drive_upload_write(buf, fill);
            fill = 0;
            if (r != DRIVE_OK) {
                drive_upload_abort();
                if (remaining > DRAIN_LIMIT) {   // the rest is not worth reading: answer and close
                    httpd_resp_set_hdr(req, "Connection", "close");
                    send_result(req, r);
                    return ESP_FAIL;
                }
                return drain(req) ? send_result(req, r) : ESP_FAIL;
            }
        }
    }
    r = drive_upload_finish(0);   // 0 = now (system clock)
    const int64_t dt_ms = (esp_timer_get_time() - t0) / 1000;
    ESP_LOGI(TAG, "PUT '%s' %u bytes in %lld ms: %s", path, (unsigned)total, dt_ms, drive_result_str(r));
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    ph_remove_tree(path);
    return send_empty(req, existed ? "204 No Content" : "201 Created");
}

// ============================================================== DELETE, MKCOL

static esp_err_t h_delete(httpd_req_t *req)
{
    char path[PATH_BUF];
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (path[0] == '\0') {
        return send_text(req, "403 Forbidden", "Der Hauptordner kann nicht gelöscht werden.");
    }
    if (junk_path(path)) {
        return send_text(req, "404 Not Found", MSG_NOT_FOUND);
    }
    const bool had_ph = ph_find(path) != NULL;
    ph_remove_tree(path);
    anim_set_busy(true);
    r = drive_delete(path, true);   // DELETE on a collection is always recursive (RFC 4918 9.6.1)
    anim_set_busy(false);
    if (r == DRIVE_OK || (r == DRIVE_ERR_NOT_FOUND && had_ph)) {
        return send_empty(req, "204 No Content");
    }
    return send_result(req, r);
}

static esp_err_t h_mkcol(httpd_req_t *req)
{
    char path[PATH_BUF];
    if (req->content_len > 0) {
        return refuse_upload(req, DRIVE_ERR_BAD_TYPE, "415 Unsupported Media Type", "MKCOL ohne Inhalt senden.");
    }
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (path[0] == '\0') {
        return send_text(req, "405 Method Not Allowed", "Den Ordner gibt es schon.");
    }
    if (junk_path(path)) {
        return send_text(req, "403 Forbidden", MSG_JUNK);
    }
    r = drive_mkdir(path);
    switch (r) {
    case DRIVE_OK:
        ph_remove_tree(path);
        return send_empty(req, "201 Created");
    case DRIVE_ERR_EXISTS:
        return send_text(req, "405 Method Not Allowed", "Den Namen gibt es schon.");
    case DRIVE_ERR_NOT_FOUND:
        return send_text(req, "409 Conflict", MSG_PARENT);
    default:
        return send_result(req, r);
    }
}

// ============================================================== MOVE, COPY

/*
 * Overwrite: T onto an existing target (RFC 4918 9.8.4, 9.9.3): drive_replace() does the whole
 * replacement under one acquisition of the drive lock (old target renamed to a hidden temporary
 * name, removed only after the new object took its place, restored on failure), so the printer or
 * the auto-lock can never get the drive in between. It refuses with BAD_NAME when the target is
 * the source under another spelling (case, umlaut case, 8.3 alias) or a folder containing it.
 *
 * Placeholders: a real file always moves/copies as itself (a shadow at the source ends with a
 * MOVE). A source that only exists as a placeholder moves/copies as a placeholder, but never onto
 * an existing real file or folder: replacing real data by an empty file that the drive cannot
 * store (and that expires after 5 minutes) would delete it for good - 409 with Overwrite: T,
 * 412 with Overwrite: F.
 */
static esp_err_t move_copy(httpd_req_t *req, bool copy)
{
    char src[PATH_BUF], dst[PATH_BUF];
    if (parse_path(req->uri, src, sizeof(src)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    // Headers first (they are gone once the answer has started).
    bool overwrite = true;
    {
        char raw[HDR_BUF];
        const size_t dlen = httpd_req_get_hdr_value_len(req, "Destination");
        if (dlen == 0 || dlen >= sizeof(raw) ||
            httpd_req_get_hdr_value_str(req, "Destination", raw, sizeof(raw)) != ESP_OK) {
            return send_text(req, "400 Bad Request", "Destination fehlt oder ist zu lang.");
        }
        const path_kind_t k = parse_destination(raw, dst, sizeof(dst));
        if (k != P_DAV) {
            return k == P_BAD ? send_text(req, "400 Bad Request", MSG_BAD_PATH)
                              : send_text(req, "502 Bad Gateway", "Das Ziel liegt nicht unter /dav/.");
        }
        char ov[4] = "";
        if (httpd_req_get_hdr_value_len(req, "Overwrite") < sizeof(ov) &&
            httpd_req_get_hdr_value_str(req, "Overwrite", ov, sizeof(ov)) == ESP_OK) {
            overwrite = !(ov[0] == 'F' || ov[0] == 'f');
        }
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (src[0] == '\0' || dst[0] == '\0') {
        return send_text(req, "403 Forbidden", "Der Hauptordner kann nicht verschoben oder kopiert werden.");
    }
    if (junk_path(src)) {
        return send_text(req, "404 Not Found", MSG_NOT_FOUND);
    }
    if (junk_path(dst)) {
        return send_text(req, "403 Forbidden", MSG_JUNK);
    }
    if (strcmp(src, dst) == 0) {
        return send_text(req, "403 Forbidden", "Quelle und Ziel sind gleich.");
    }
    // A folder into itself, or onto a folder that contains the source: refused BEFORE an
    // existing target could be deleted for Overwrite.
    if (is_below(dst, src) || is_below(src, dst)) {
        return send_text(req, "403 Forbidden", "Ein Ordner kann nicht in sich selbst oder seinen Inhalt.");
    }

    if (!overwrite && ph_find(dst)) {
        return send_text(req, "412 Precondition Failed", "Das Ziel gibt es schon (Overwrite: F).");
    }

    anim_set_busy(copy);   // display: copying blocks the HTTP task until it is done
    r = copy ? drive_copy(src, dst) : drive_move(src, dst);
    bool replaced = false;
    if (r == DRIVE_ERR_EXISTS && overwrite) {
        // EXISTS comes after the source, name, type and depth checks of the drive layer: the source
        // exists, so BAD_NAME from drive_replace() can only mean "target is the source itself or
        // contains it".
        anim_set_busy(true);
        r = drive_replace(src, dst, copy, &replaced);
        anim_set_busy(false);
        if (r == DRIVE_ERR_BAD_NAME) {
            return send_text(req, "403 Forbidden", "Quelle und Ziel sind dieselbe Datei, oder das Ziel enthält die Quelle.");
        }
    }
    anim_set_busy(false);
    if (r == DRIVE_ERR_EXISTS) {
        return send_text(req, "412 Precondition Failed", "Das Ziel gibt es schon.");
    }
    if (r == DRIVE_ERR_NOT_FOUND && ph_find(src)) {
        // The source is an empty file that only exists as a placeholder: checks of a new file at
        // the target, nothing is touched (a kept interrupted upload survives).
        const drive_result_t v = drive_upload_check(dst, 1, false);
        if (v == DRIVE_ERR_EXISTS || v == DRIVE_ERR_IS_DIR) {
            return overwrite ? send_text(req, "409 Conflict",
                                         "Eine leere Datei kann eine vorhandene Datei oder einen Ordner nicht ersetzen.")
                             : send_text(req, "412 Precondition Failed", "Das Ziel gibt es schon (Overwrite: F).");
        }
        if (v != DRIVE_OK) {
            return v == DRIVE_ERR_NOT_FOUND ? send_text(req, "409 Conflict", MSG_PARENT) : send_result(req, v);
        }
        replaced = ph_find(dst) != NULL;   // a placeholder replaces a placeholder (Overwrite: T, see above)
        if (!copy) {
            ph_remove_tree(src);
        }
        ph_add(dst, NULL);
        return send_empty(req, replaced ? "204 No Content" : "201 Created");
    }
    if (r == DRIVE_ERR_NOT_FOUND) {
        entry_t e;   // source there: the target folder is missing (409), else 404
        return lookup(src, &e) == DRIVE_OK ? send_text(req, "409 Conflict", MSG_PARENT) : send_result(req, r);
    }
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (!copy) {
        ph_remove_tree(src);
    }
    replaced = replaced || ph_find(dst) != NULL;   // a placeholder at the target counts as replaced
    ph_remove_tree(dst);
    return send_empty(req, replaced ? "204 No Content" : "201 Created");
}

static esp_err_t h_move(httpd_req_t *req)
{
    return move_copy(req, false);
}

static esp_err_t h_copy(httpd_req_t *req)
{
    return move_copy(req, true);
}

// ============================================================== LOCK, UNLOCK (minimal class 2)
//
// Locks are not stored or enforced: the server has one writer at a time anyway (one upload
// session, one HTTP task). LOCK hands out a fresh token, refresh echoes the token from If:,
// UNLOCK always succeeds. Windows and Finder only allow writing on servers that support LOCK.

/** Length of s percent-encoded (see out_enc()). */
static size_t enc_len(const char *s)
{
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        n += unreserved(*p) ? 1 : 3;
    }
    return n;
}

static void enc_append(char *buf, size_t len, const char *s)
{
    static const char hex[] = "0123456789ABCDEF";
    size_t n = strlen(buf);
    for (const unsigned char *p = (const unsigned char *)s; *p && n + 4 < len; p++) {
        if (unreserved(*p)) {
            buf[n++] = (char)*p;
        } else {
            buf[n++] = '%';
            buf[n++] = hex[*p >> 4];
            buf[n++] = hex[*p & 15];
        }
    }
    buf[n] = '\0';
}

static esp_err_t h_lock(httpd_req_t *req)
{
    char path[PATH_BUF];
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    // Headers first: If (token of a refresh) and Depth. The token is echoed into the XML answer:
    // only our own form (hex digits and '-' after "opaquelocktoken:") is taken over; anything else
    // gets a fresh token below.
    static const char TOKEN_PREFIX[] = "opaquelocktoken:";
    char token[80] = "";
    bool have_if_token = false;
    {
        char ifh[160];
        if (httpd_req_get_hdr_value_len(req, "If") < sizeof(ifh) &&
            httpd_req_get_hdr_value_str(req, "If", ifh, sizeof(ifh)) == ESP_OK) {
            const char *t = strstr(ifh, TOKEN_PREFIX);
            if (t) {
                have_if_token = true;
                const size_t pl = sizeof(TOKEN_PREFIX) - 1;
                const size_t n = strcspn(t, ">) ");
                bool ok = n > pl && n < sizeof(token);
                for (size_t i = pl; ok && i < n; i++) {
                    ok = hexval(t[i]) >= 0 || t[i] == '-';
                }
                if (ok) {
                    memcpy(token, t, n);
                    token[n] = '\0';
                }
            }
        }
    }
    bool depth0 = false;
    {
        char d[12];
        depth0 = httpd_req_get_hdr_value_len(req, "Depth") < sizeof(d) &&
                 httpd_req_get_hdr_value_str(req, "Depth", d, sizeof(d)) == ESP_OK && strcmp(d, "0") == 0;
    }
    const bool refresh = req->content_len == 0;   // no body: refresh of an existing lock (RFC 4918 9.10.2)
    bool shared = false;
    if (refresh && !have_if_token) {
        return send_text(req, "400 Bad Request", "LOCK ohne Inhalt braucht ein If mit Lock-Token.");
    }
    if (!refresh) {
        char *body = (char *)web_io_buf();
        const esp_err_t e = read_body(req, body, WEB_IO_BUF_SIZE);
        if (e == ESP_ERR_INVALID_SIZE) {
            return send_text(req, "413 Payload Too Large", "Anfrage zu groß.");
        }
        if (e != ESP_OK) {
            return ESP_FAIL;
        }
        const char *scope = strstr(body, "lockscope");
        shared = scope && strstr(scope, "shared") && (!strstr(scope, "exclusive") ||
                                                     strstr(scope, "shared") < strstr(scope, "exclusive"));
        token[0] = '\0';   // a new lock always gets a new token
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (junk_path(path)) {
        return send_text(req, "403 Forbidden", MSG_JUNK);
    }
    const char *status = "200 OK";
    if (path[0] != '\0' && !refresh && !ph_find(path)) {
        // LOCK on a new name creates an empty file (RFC 4918 9.10.4): a placeholder here.
        // drive_upload_check(): the checks of a new file without touching anything (Windows sends
        // such a LOCK for every file it creates; a kept interrupted upload must survive it).
        r = drive_upload_check(path, 1, false);
        if (r == DRIVE_OK) {
            ph_add(path, NULL);
            status = "201 Created";
        } else if (r != DRIVE_ERR_EXISTS && r != DRIVE_ERR_IS_DIR) {
            entry_t e;   // e.g. an existing name that the rules for new names refuse
            if (lookup(path, &e) != DRIVE_OK) {
                return r == DRIVE_ERR_NOT_FOUND ? send_text(req, "409 Conflict", MSG_PARENT) : send_result(req, r);
            }
        }
    }
    if (!token[0]) {
        snprintf(token, sizeof(token), "opaquelocktoken:%08lx-%04lx-4%03lx-a%03lx-%08lx%04lx",
                 (unsigned long)esp_random(), (unsigned long)(esp_random() & 0xFFFF),
                 (unsigned long)(esp_random() & 0xFFF), (unsigned long)(esp_random() & 0xFFF),
                 (unsigned long)esp_random(), (unsigned long)(esp_random() & 0xFFFF));
    }
    char body[LOCK_BODY_BUF] = "";
    web_appendf(body, sizeof(body),
                "%s<D:prop xmlns:D=\"DAV:\"><D:lockdiscovery><D:activelock><D:locktype><D:write/></D:locktype>"
                "<D:lockscope><D:%s/></D:lockscope><D:depth>%s</D:depth><D:timeout>Second-3600</D:timeout>"
                "<D:locktoken><D:href>%s</D:href></D:locktoken>",
                XML_HEAD, shared ? "shared" : "exclusive", depth0 ? "0" : "infinity", token);
    if (strlen(body) + enc_len(path) + 120 < sizeof(body)) {   // lockroot (RFC 4918) if it fits
        web_appendf(body, sizeof(body), "<D:lockroot><D:href>/dav/");
        enc_append(body, sizeof(body), path);
        web_appendf(body, sizeof(body), "</D:href></D:lockroot>");
    }
    web_appendf(body, sizeof(body), "</D:activelock></D:lockdiscovery></D:prop>");
    char lock_hdr[sizeof(token) + 2];
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/xml; charset=utf-8");
    if (!refresh) {
        snprintf(lock_hdr, sizeof(lock_hdr), "<%s>", token);
        httpd_resp_set_hdr(req, "Lock-Token", lock_hdr);
    }
    return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t h_unlock(httpd_req_t *req)
{
    // Windows sometimes sends the token without the angle brackets: any non-empty one is accepted.
    if (httpd_req_get_hdr_value_len(req, "Lock-Token") == 0) {
        return send_text(req, "400 Bad Request", "Lock-Token fehlt.");
    }
    return send_empty(req, "204 No Content");
}

// ============================================================== PROPPATCH (accepted, nothing stored)

/** Namespace URI of prefix (pl bytes; pl == 0: default namespace) from the first declaration at
 *  or after from (and before limit, if given). Enough for the flat updates clients send. */
static bool find_ns(const char *from, const char *limit, const char *prefix, size_t pl, const char **uri,
                    size_t *ul)
{
    for (const char *p = strstr(from, "xmlns"); p && (!limit || p < limit); p = strstr(p + 5, "xmlns")) {
        const char *q = p + 5;
        if (pl > 0) {
            if (*q != ':' || strncmp(q + 1, prefix, pl) != 0) {
                continue;
            }
            q += 1 + pl;
        }
        while (*q == ' ') {
            q++;
        }
        if (*q != '=') {
            continue;
        }
        q++;
        while (*q == ' ') {
            q++;
        }
        const char quote = *q;
        if (quote != '"' && quote != '\'') {
            continue;
        }
        const char *end = strchr(q + 1, quote);
        if (!end) {
            return false;
        }
        *uri = q + 1;
        *ul = (size_t)(end - (q + 1));
        return true;
    }
    return false;
}

static bool xml_name_ok(const char *s, size_t n)
{
    if (n == 0 || n > 100) {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        const char c = s[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_' ||
              (c == '.' && i > 0))) {
            return false;
        }
    }
    return !(s[0] >= '0' && s[0] <= '9') && s[0] != '-';
}

/** Echo one property name of the request as <x:name xmlns:x="ns"/>. */
static void out_prop_name(out_t *o, const char *body, const char *tag, const char *tag_end, const char *prefix,
                          size_t pl, const char *local, size_t ll)
{
    if (!xml_name_ok(local, ll) || (pl > 0 && !xml_name_ok(prefix, pl))) {
        return;
    }
    const char *ns = NULL;
    size_t nl = 0;
    if (pl == 0) {
        if (!find_ns(tag, tag_end, "", 0, &ns, &nl)) {
            find_ns(body, NULL, "", 0, &ns, &nl);
        }
    } else if (!find_ns(body, NULL, prefix, pl, &ns, &nl)) {
        return;   // undeclared prefix: not well-formed, skip
    }
    if (ns && nl == 4 && strncmp(ns, "DAV:", 4) == 0) {
        out_str(o, "<D:");
        out_mem(o, local, ll);
        out_str(o, "/>");
        return;
    }
    out_str(o, "<x:");
    out_mem(o, local, ll);
    out_str(o, " xmlns:x=\"");
    for (size_t i = 0; ns && i < nl; i++) {
        if (ns[i] == '"') {
            out_str(o, "&quot;");
        } else if (ns[i] != '<') {
            out_mem(o, ns + i, 1);
        }
    }
    out_str(o, "\"/>");
}

/** All properties named in <prop> elements of a propertyupdate (set and remove). */
static void out_proppatch_props(out_t *o, const char *body)
{
    int depth = 0, prop_depth = -1;
    const char *p = body;
    while ((p = strchr(p, '<')) != NULL) {
        if (p[1] == '?') {
            p = strstr(p, "?>");
            if (!p) {
                break;
            }
            p += 2;
            continue;
        }
        if (p[1] == '!') {
            const char *end = strncmp(p, "<!--", 4) == 0      ? strstr(p + 4, "-->")
                              : strncmp(p, "<![CDATA[", 9) == 0 ? strstr(p + 9, "]]>")
                                                                : strchr(p, '>');
            if (!end) {
                break;
            }
            p = end + 1;
            continue;
        }
        const char *gt = strchr(p, '>');
        if (!gt) {
            break;
        }
        if (p[1] == '/') {
            depth--;
            if (prop_depth >= 0 && depth == prop_depth) {
                prop_depth = -1;
            }
            p = gt + 1;
            continue;
        }
        const bool empty = gt[-1] == '/';
        const char *name = p + 1;
        const size_t nl = strcspn(name, " \t\r\n/>");
        const char *colon = memchr(name, ':', nl);
        const char *local = colon ? colon + 1 : name;
        const size_t ll = nl - (size_t)(local - name);
        if (prop_depth >= 0 && depth == prop_depth + 1) {
            out_prop_name(o, body, p, gt, name, colon ? (size_t)(colon - name) : 0, local, ll);
        } else if (prop_depth < 0 && !empty && ll == 4 && strncmp(local, "prop", 4) == 0) {
            prop_depth = depth;
        }
        if (!empty) {
            depth++;
        }
        p = gt + 1;
    }
}

static esp_err_t h_proppatch(httpd_req_t *req)
{
    char path[PATH_BUF];
    if (parse_path(req->uri, path, sizeof(path)) != P_DAV) {
        return send_text(req, "400 Bad Request", MSG_BAD_PATH);
    }
    drive_status_t st;
    drive_result_t r = drive_ready(&st);
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    if (junk_path(path)) {
        return send_text(req, "404 Not Found", MSG_NOT_FOUND);
    }
    entry_t e;
    r = lookup(path, &e);   // uses the I/O buffer: before the body is read into it
    if (r == DRIVE_ERR_NOT_FOUND && ph_find(path)) {
        e = (entry_t){0};
        r = DRIVE_OK;
    }
    if (r != DRIVE_OK) {
        return send_result(req, r);
    }
    char *body = (char *)web_io_buf();
    const esp_err_t be = read_body(req, body, WEB_IO_BUF_SIZE);
    if (be == ESP_ERR_INVALID_SIZE) {
        return send_text(req, "413 Payload Too Large", "Anfrage zu groß.");
    }
    if (be != ESP_OK) {
        return ESP_FAIL;
    }
    // Windows sets Win32CreationTime/LastAccessTime/LastModifiedTime/FileAttributes: all are
    // confirmed with 200 and nothing is changed (the drive keeps the time of the upload).
    out_t o;
    out_init(&o, req, "207 Multi-Status");
    out_str(&o, XML_HEAD);
    out_str(&o, "<D:multistatus xmlns:D=\"DAV:\"><D:response><D:href>/dav/");
    out_enc(&o, path);
    if (e.is_dir && path[0]) {
        out_str(&o, "/");
    }
    out_str(&o, "</D:href><D:propstat><D:prop>");
    out_proppatch_props(&o, body);
    out_str(&o, "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>");
    return out_end(&o);
}

// ============================================================== OPTIONS, authorization, setting

static esp_err_t h_options(httpd_req_t *req)
{
    if (s_enabled) {
        httpd_resp_set_hdr(req, "DAV", "1, 2");
        httpd_resp_set_hdr(req, "MS-Author-Via", "DAV");
        httpd_resp_set_hdr(req, "Allow", ALLOW);
    } else {
        httpd_resp_set_hdr(req, "Allow", "OPTIONS, GET, HEAD, POST");
    }
    // Never any Access-Control-Allow-* header: a CORS preflight to this server always fails.
    return send_empty(req, "200 OK");
}

bool web_dav_authorize(httpd_req_t *req, web_role_t min_role)
{
    if (min_role == WEB_ROLE_NONE) {
        return true;   // OPTIONS; without the DAV headers while WebDAV is switched off
    }
    if (!s_enabled) {
        send_text(req, "403 Forbidden", MSG_OFF);
        return false;
    }
    // Basic only: a session cookie is never looked at, not even next to a Basic header (CSRF, see
    // the head comment). auth_identify_basic_only() checks the credentials of this request with
    // the failure counting, lock-out and cache of auth.c; missing, other scheme or wrong: 401.
    const auth_result_t r = auth_identify_basic_only(req);
    if (r == AUTH_ERR_UNAUTHORIZED) {
        send_401(req);   // the client asks for (other) credentials
        return false;
    }
    if (r != AUTH_OK) {
        auth_send_error(req, r);   // 429 with Retry-After (lock-out), 503 (user database unreadable)
        return false;
    }
    if (auth_current_role(req) < min_role) {
        auth_send_error(req, AUTH_ERR_FORBIDDEN_ROLE);
        return false;
    }
    if (auth_current_default_password(req)) {
        send_text(req, "403 Forbidden", MSG_DEFAULT_PW);   // forced password change (contract 2.4)
        return false;
    }
    return true;
}

static void load_setting(void)
{
    nvs_handle_t h;
    uint8_t v = 1;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_u8(h, NVS_KEY, &v) == ESP_OK) {
            s_enabled = v != 0;
        }
        nvs_close(h);
    }
}

static esp_err_t save_setting(bool on)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_u8(h, NVS_KEY, on ? 1 : 0);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

/** POST /api/settings/webdav?value=0|1 (admin): switch WebDAV on or off (stored, immediate). */
static esp_err_t h_settings_webdav(httpd_req_t *req)
{
    char v[8];
    if (!web_query_param(req, "value", v, sizeof(v)) ||
        (strcmp(v, "0") != 0 && strcmp(v, "1") != 0 && strcmp(v, "true") != 0 && strcmp(v, "false") != 0)) {
        return web_send_error(req, "400 Bad Request", "BAD_REQUEST", "value muss 0 oder 1 sein");
    }
    const bool on = strcmp(v, "1") == 0 || strcmp(v, "true") == 0;
    if (save_setting(on) != ESP_OK) {
        return web_send_error(req, "500 Internal Server Error", "IO", "Die Einstellung konnte nicht gespeichert werden.");
    }
    s_enabled = on;
    ESP_LOGI(TAG, "WebDAV %s", on ? "on" : "off");
    return web_send_json(req, "200 OK", on ? "{\"ok\":true,\"webdav\":true}" : "{\"ok\":true,\"webdav\":false}");
}

// ============================================================== routes

// Wildcard templates need uri_match_fn = httpd_uri_match_wildcard (web_server.c). On "/" only
// OPTIONS and PROPFIND: Windows sends both to the server root before it maps http://host/dav.
static const web_route_t s_dav_routes[] = {
    {"/", HTTP_OPTIONS, h_options, WEB_ROLE_NONE},
    {"/", HTTP_PROPFIND, h_propfind, WEB_ROLE_USER},
    {DAV_URI, HTTP_OPTIONS, h_options, WEB_ROLE_NONE},
    {DAV_URI, HTTP_PROPFIND, h_propfind, WEB_ROLE_USER},
    {DAV_URI, HTTP_GET, h_get, WEB_ROLE_USER},
    {DAV_URI, HTTP_HEAD, h_head, WEB_ROLE_USER},
    {DAV_URI, HTTP_PUT, h_put, WEB_ROLE_USER},
    {DAV_URI, HTTP_DELETE, h_delete, WEB_ROLE_USER},
    {DAV_URI, HTTP_MKCOL, h_mkcol, WEB_ROLE_USER},
    {DAV_URI, HTTP_MOVE, h_move, WEB_ROLE_USER},
    {DAV_URI, HTTP_COPY, h_copy, WEB_ROLE_USER},
    {DAV_URI, HTTP_LOCK, h_lock, WEB_ROLE_USER},
    {DAV_URI, HTTP_UNLOCK, h_unlock, WEB_ROLE_USER},
    {DAV_URI, HTTP_PROPPATCH, h_proppatch, WEB_ROLE_USER},
};

// Normal API route: dispatcher with session/Basic and the CSRF header.
static const web_route_t s_settings_routes[] = {
    {"/api/settings/webdav", HTTP_POST, h_settings_webdav, WEB_ROLE_ADMIN},
};

bool web_dav_route(const web_route_t *route)
{
    for (size_t i = 0; i < sizeof(s_dav_routes) / sizeof(s_dav_routes[0]); i++) {
        if (route == &s_dav_routes[i]) {
            return true;
        }
    }
    return false;
}

esp_err_t web_dav_register(httpd_handle_t server)
{
    load_setting();
    esp_err_t err = web_register_routes(server, s_dav_routes, sizeof(s_dav_routes) / sizeof(s_dav_routes[0]));
    if (err == ESP_OK) {
        err = web_register_routes(server, s_settings_routes, sizeof(s_settings_routes) / sizeof(s_settings_routes[0]));
    }
    return err;
}
