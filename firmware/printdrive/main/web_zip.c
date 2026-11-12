// Folder download as ZIP archive: GET /api/zip?path=<folder>.
//
// Format: method "stored" (no compression: 3MF files are ZIPs already and the CPU is small).
// Every file entry has general purpose bit 3 set and is followed by a data descriptor with the
// CRC-32 computed while reading (sizes and CRC are zero in the local header), so the archive is
// streamed with chunked transfer encoding and never needs seeking. Folder entries end with '/'.
// Names are UTF-8 with the language-encoding flag (bit 11). ZIP64 is used where the classic
// fields do not suffice: per file for a size of 0xFFFFFFFF (the FAT maximum), per central record
// for a local header offset >= 4 GiB, and for the end records when the archive or the central
// directory passes 4 GiB or there are 65535 entries or more.
//
// Memory: the central directory is not kept. It needs, besides what the walk delivers again
// (names, sizes, dates), only the CRC of every file and the local header offsets. The offsets
// follow from the names and sizes, so a third walk over the tree regenerates the central
// directory; only the CRCs (4 bytes per file) are kept in RAM, in blocks of at most 4 KiB, for at
// most ZIP_MAX_FILES files (16 KiB worst case, allocated after counting, before the HTTP header).
// The tree cannot change between the walks: the walk counts as an open download (owner change,
// format, lock and folder operations get BUSY) and every file operation runs in the single HTTP
// server task. A mismatch is detected anyway (sizes read, final offset) and aborts the transfer.
//
// Errors before the HTTP header (counting, memory) get a normal JSON error answer. After it the
// only way to signal a failure is to abort the connection without the terminating chunk.
#include "web_zip.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "web_common.h"
#include "web_crypt.h"

static const char *TAG = "zip";

#define ZIP_MAX_FILES   4096                       // files per archive (folders are not limited)
#define ZIP_CRC_BLOCK   1024                       // CRCs per heap block (4 KiB)
#define ZIP_CRC_BLOCKS  (ZIP_MAX_FILES / ZIP_CRC_BLOCK)
#define PATH_BUF_SIZE   (DRIVE_SRC_PATH_MAX + 1)
#define DISP_SIZE       (64 + 4 * (DRIVE_FILE_NAME_MAX + 1))   // quoted fallback + RFC 5987 form
#define YIELD_US        100000                     // counting walk lets the idle task run

#define MAX16           0xFFFFu
#define MAX32           0xFFFFFFFFu
#define FLAG_DESCRIPTOR 0x0008u                    // CRC and sizes follow the data
#define FLAG_UTF8       0x0800u                    // names are UTF-8
#define VER_CLASSIC     20                         // 2.0: folders, stored files
#define VER_ZIP64       45                         // 4.5: ZIP64
#define MADE_BY         VER_ZIP64                  // host system 0 (MS-DOS/FAT), spec 4.5
#define ATTR_DIR        0x10u                      // MS-DOS attributes (external attributes)
#define ATTR_ARCHIVE    0x20u
#define DOS_DATE_1980   0x0021u                    // 1980-01-01: FAT/ZIP "no date"

typedef struct {
    httpd_req_t *req;
    uint8_t *buf;                                  // web_io_buf(): output collected into big chunks
    size_t fill;
    uint64_t pos;                                  // archive bytes produced so far
    bool failed;                                   // sending failed: client gone
    uint32_t *crc[ZIP_CRC_BLOCKS];
    uint32_t files;                                // number of files (counting walk)
    uint32_t entries;                              // files and folders (counting walk)
    char path[PATH_BUF_SIZE];
    char base[DRIVE_FILE_NAME_MAX + 1];
    char disp[DISP_SIZE];                          // Content-Disposition, must live until sent
} zip_ctx_t;

// ------------------------------------------------------------------ output

static void le16(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void le32(uint8_t *p, uint32_t v)
{
    le16(p, v & 0xFFFF);
    le16(p + 2, v >> 16);
}

static void le64(uint8_t *p, uint64_t v)
{
    le32(p, (uint32_t)v);
    le32(p + 4, (uint32_t)(v >> 32));
}

static bool zflush(zip_ctx_t *c)
{
    if (c->fill > 0 && !c->failed &&
        httpd_resp_send_chunk(c->req, (const char *)c->buf, (ssize_t)c->fill) != ESP_OK) {
        c->failed = true;
    }
    c->fill = 0;
    return !c->failed;
}

static bool zput(zip_ctx_t *c, const void *data, size_t len)
{
    const uint8_t *p = data;
    while (len > 0 && !c->failed) {
        const size_t n = len < WEB_IO_BUF_SIZE - c->fill ? len : WEB_IO_BUF_SIZE - c->fill;
        memcpy(c->buf + c->fill, p, n);
        c->fill += n;
        c->pos += n;
        p += n;
        len -= n;
        if (c->fill == WEB_IO_BUF_SIZE) {
            zflush(c);
        }
    }
    return !c->failed;
}

// ------------------------------------------------------------------ records

static bool file_zip64(const drive_zip_entry_t *e)
{
    return !e->is_dir && e->size >= MAX32;
}

static size_t name_len(const drive_zip_entry_t *e)
{
    return strlen(e->name) + (e->is_dir ? 1 : 0);   // folders end with '/'
}

static uint32_t dos_date(const drive_zip_entry_t *e)
{
    return e->dos_date ? e->dos_date : DOS_DATE_1980;
}

/** Bytes of one entry from its local header to the end of its data descriptor. */
static uint64_t local_record_size(const drive_zip_entry_t *e)
{
    const bool z64 = file_zip64(e);
    uint64_t n = 30 + name_len(e) + (z64 ? 20 : 0);
    if (!e->is_dir) {
        n += (uint64_t)e->size + (z64 ? 24 : 16);
    }
    return n;
}

static bool put_name(zip_ctx_t *c, const drive_zip_entry_t *e)
{
    return zput(c, e->name, strlen(e->name)) && (!e->is_dir || zput(c, "/", 1));
}

static bool put_local_header(zip_ctx_t *c, const drive_zip_entry_t *e)
{
    const bool z64 = file_zip64(e);
    uint8_t h[30 + 20];
    le32(h, 0x04034b50);
    le16(h + 4, z64 ? VER_ZIP64 : VER_CLASSIC);
    le16(h + 6, FLAG_UTF8 | (e->is_dir ? 0 : FLAG_DESCRIPTOR));
    le16(h + 8, 0);                                 // stored
    le16(h + 10, e->dos_date ? e->dos_time : 0);
    le16(h + 12, dos_date(e));
    le32(h + 14, 0);                                // CRC: in the data descriptor
    le32(h + 18, z64 ? MAX32 : 0);                  // sizes: in the data descriptor
    le32(h + 22, z64 ? MAX32 : 0);
    le16(h + 26, (uint32_t)name_len(e));
    le16(h + 28, z64 ? 20 : 0);
    if (!zput(c, h, 30) || !put_name(c, e)) {
        return false;
    }
    if (!z64) {
        return true;
    }
    le16(h, 0x0001);                                // ZIP64 extended information: both sizes
    le16(h + 2, 16);
    le64(h + 4, e->size);
    le64(h + 12, e->size);
    return zput(c, h, 20);
}

/** File data with CRC-32, then the data descriptor. false: read error, size changed or client gone. */
static bool put_file_data(zip_ctx_t *c, drive_zip_t *z, const drive_zip_entry_t *e, uint32_t *crc_out)
{
    // Keep reads 4-byte aligned (the SD card driver bounces unaligned buffers sector by sector).
    if ((c->fill & 3) != 0 && !zflush(c)) {
        return false;
    }
    uint32_t crc = 0;
    uint64_t done = 0;
    while (true) {
        size_t got = 0;
        uint8_t *dst = c->buf + c->fill;
        const drive_result_t r = drive_zip_read(z, dst, WEB_IO_BUF_SIZE - c->fill, &got);
        if (r != DRIVE_OK) {
            ESP_LOGE(TAG, "read error in '%s': %s", e->name, drive_result_str(r));
            return false;
        }
        if (got == 0) {
            break;
        }
        crc = esp_rom_crc32_le(crc, dst, (uint32_t)got);
        c->fill += got;
        c->pos += got;
        done += got;
        if (done > e->size) {
            break;
        }
        if (c->fill == WEB_IO_BUF_SIZE && !zflush(c)) {
            return false;
        }
    }
    if (done != e->size) {
        ESP_LOGE(TAG, "'%s' changed while zipping (%llu instead of %lu bytes)", e->name,
                 (unsigned long long)done, (unsigned long)e->size);
        return false;
    }
    uint8_t d[24];
    size_t n = 16;
    le32(d, 0x08074b50);
    le32(d + 4, crc);
    if (file_zip64(e)) {
        le64(d + 8, e->size);
        le64(d + 16, e->size);
        n = 24;
    } else {
        le32(d + 8, e->size);
        le32(d + 12, e->size);
    }
    *crc_out = crc;
    return zput(c, d, n);
}

static bool put_central_header(zip_ctx_t *c, const drive_zip_entry_t *e, uint32_t crc, uint64_t offset)
{
    const bool z64_size = file_zip64(e), z64_off = offset >= MAX32;
    uint8_t x[4 + 24];
    size_t xl = 0;
    if (z64_size || z64_off) {                      // only the fields that are 0xFFFFFFFF below
        xl = 4;
        if (z64_size) {
            le64(x + xl, e->size);
            le64(x + xl + 8, e->size);
            xl += 16;
        }
        if (z64_off) {
            le64(x + xl, offset);
            xl += 8;
        }
        le16(x, 0x0001);
        le16(x + 2, (uint32_t)(xl - 4));
    }
    uint8_t h[46];
    le32(h, 0x02014b50);
    le16(h + 4, MADE_BY);
    le16(h + 6, xl ? VER_ZIP64 : VER_CLASSIC);
    le16(h + 8, FLAG_UTF8 | (e->is_dir ? 0 : FLAG_DESCRIPTOR));
    le16(h + 10, 0);
    le16(h + 12, e->dos_date ? e->dos_time : 0);
    le16(h + 14, dos_date(e));
    le32(h + 16, e->is_dir ? 0 : crc);
    le32(h + 20, z64_size ? MAX32 : e->size);
    le32(h + 24, z64_size ? MAX32 : e->size);
    le16(h + 28, (uint32_t)name_len(e));
    le16(h + 30, (uint32_t)xl);
    le16(h + 32, 0);                                // comment
    le16(h + 34, 0);                                // disk
    le16(h + 36, 0);                                // internal attributes
    le32(h + 38, e->is_dir ? ATTR_DIR : ATTR_ARCHIVE);
    le32(h + 42, z64_off ? MAX32 : (uint32_t)offset);
    return zput(c, h, 46) && put_name(c, e) && (xl == 0 || zput(c, x, xl));
}

static bool put_end(zip_ctx_t *c, uint64_t entries, uint64_t cd_off)
{
    const uint64_t cd_size = c->pos - cd_off;
    if (entries >= MAX16 || cd_size >= MAX32 || cd_off >= MAX32) {
        uint8_t r[56 + 20];
        const uint64_t eocd64 = c->pos;
        le32(r, 0x06064b50);                        // ZIP64 end of central directory record
        le64(r + 4, 44);
        le16(r + 12, MADE_BY);
        le16(r + 14, VER_ZIP64);
        le32(r + 16, 0);
        le32(r + 20, 0);
        le64(r + 24, entries);
        le64(r + 32, entries);
        le64(r + 40, cd_size);
        le64(r + 48, cd_off);
        le32(r + 56, 0x07064b50);                   // ZIP64 end of central directory locator
        le32(r + 60, 0);
        le64(r + 64, eocd64);
        le32(r + 72, 1);
        if (!zput(c, r, sizeof(r))) {
            return false;
        }
    }
    uint8_t e[22];
    le32(e, 0x06054b50);
    le16(e + 4, 0);
    le16(e + 6, 0);
    le16(e + 8, entries >= MAX16 ? MAX16 : (uint32_t)entries);
    le16(e + 10, entries >= MAX16 ? MAX16 : (uint32_t)entries);
    le32(e + 12, cd_size >= MAX32 ? MAX32 : (uint32_t)cd_size);
    le32(e + 16, cd_off >= MAX32 ? MAX32 : (uint32_t)cd_off);
    le16(e + 20, 0);
    return zput(c, e, sizeof(e));
}

// ------------------------------------------------------------------ request

/** True if the query has a "path" key (web_query_param() also fails for a value too long). */
static bool path_present(httpd_req_t *req)
{
    char *q = (char *)web_io_buf();   // free at this point
    char v[2];
    if (!q || httpd_req_get_url_query_str(req, q, WEB_IO_BUF_SIZE) != ESP_OK) {
        return false;
    }
    const esp_err_t e = httpd_query_key_value(q, "path", v, sizeof(v));
    return e == ESP_OK || e == ESP_ERR_HTTPD_RESULT_TRUNC;
}

static void disp_putc(char *out, size_t len, size_t *pos, char ch)
{
    if (*pos + 1 < len) {
        out[(*pos)++] = ch;
        out[*pos] = '\0';
    }
}

static void disp_puts(char *out, size_t len, size_t *pos, const char *s)
{
    while (*s) {
        disp_putc(out, len, pos, *s++);
    }
}

/** attachment; filename="<name>.zip" plus filename*=UTF-8''... (RFC 5987) for other characters. */
static void make_disposition(char *out, size_t len, const char *base)
{
    const char *name = base[0] ? base : "DataDrive";
    bool plain = true;
    for (const unsigned char *p = (const unsigned char *)name; *p; p++) {
        if (*p < 0x20 || *p >= 0x7F || *p == '"' || *p == '\\') {
            plain = false;
        }
    }
    size_t pos = 0;
    out[0] = '\0';
    disp_puts(out, len, &pos, "attachment; filename=\"");
    for (const unsigned char *p = (const unsigned char *)name; *p; p++) {
        if (*p >= 0x80 && *p < 0xC0) {
            continue;                               // UTF-8 continuation byte: one '_' per character
        }
        const bool ok = *p >= 0x20 && *p < 0x7F && *p != '"' && *p != '\\';
        disp_putc(out, len, &pos, ok ? (char)*p : '_');
    }
    disp_puts(out, len, &pos, ".zip\"");
    if (plain) {
        return;
    }
    disp_puts(out, len, &pos, "; filename*=UTF-8''");
    static const char hex[] = "0123456789ABCDEF";
    for (const unsigned char *p = (const unsigned char *)name; *p; p++) {
        const bool attr_char = (*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
                               (*p >= '0' && *p <= '9') || strchr("!#$&+-.^_`|~", *p) != NULL;
        if (attr_char) {
            disp_putc(out, len, &pos, (char)*p);
        } else {
            disp_putc(out, len, &pos, '%');
            disp_putc(out, len, &pos, hex[*p >> 4]);
            disp_putc(out, len, &pos, hex[*p & 15]);
        }
    }
    disp_puts(out, len, &pos, ".zip");
}

/** Counting walk: entries, files, and every path within the walk's limits. */
static drive_result_t count_tree(zip_ctx_t *c, drive_zip_t *z)
{
    drive_zip_rewind(z);
    int64_t last_yield = esp_timer_get_time();
    while (true) {
        drive_zip_entry_t e;
        bool end = false;
        const drive_result_t r = drive_zip_next(z, &e, &end);
        if (r != DRIVE_OK || end) {
            return r;
        }
        c->entries++;
        c->files += e.is_dir ? 0 : 1;
        if (esp_timer_get_time() - last_yield >= YIELD_US) {
            vTaskDelay(1);
            last_yield = esp_timer_get_time();
        }
    }
}

/** Second and third walk: entries with data, then the central directory and the end records. */
static bool stream_archive(zip_ctx_t *c, drive_zip_t *z)
{
    uint32_t file_idx = 0, entries = 0;
    drive_zip_entry_t e;
    bool end = false;
    drive_zip_rewind(z);
    while (true) {
        if (drive_zip_next(z, &e, &end) != DRIVE_OK) {
            return false;
        }
        if (end) {
            break;
        }
        if (!put_local_header(c, &e)) {
            return false;
        }
        if (!e.is_dir) {
            if (file_idx >= c->files) {
                ESP_LOGE(TAG, "more files than counted");
                return false;
            }
            uint32_t crc = 0;
            if (!put_file_data(c, z, &e, &crc)) {
                return false;
            }
            c->crc[file_idx / ZIP_CRC_BLOCK][file_idx % ZIP_CRC_BLOCK] = crc;
            file_idx++;
        }
        entries++;
    }

    const uint64_t cd_off = c->pos;
    uint64_t offset = 0;
    uint32_t cd_files = 0, cd_entries = 0;
    drive_zip_rewind(z);
    while (true) {
        if (drive_zip_next(z, &e, &end) != DRIVE_OK) {
            return false;
        }
        if (end) {
            break;
        }
        uint32_t crc = 0;
        if (!e.is_dir) {
            if (cd_files >= file_idx) {
                ESP_LOGE(TAG, "central directory: more files than written");
                return false;
            }
            crc = c->crc[cd_files / ZIP_CRC_BLOCK][cd_files % ZIP_CRC_BLOCK];
            cd_files++;
        }
        if (!put_central_header(c, &e, crc, offset)) {
            return false;
        }
        offset += local_record_size(&e);
        cd_entries++;
    }
    if (offset != cd_off || cd_files != file_idx || cd_entries != entries) {
        ESP_LOGE(TAG, "tree changed while zipping (offset %llu/%llu)", (unsigned long long)offset,
                 (unsigned long long)cd_off);
        return false;
    }
    return put_end(c, entries, cd_off) && zflush(c);
}

static esp_err_t zip_run(httpd_req_t *req, zip_ctx_t *c)
{
    if (!web_query_param(req, "path", c->path, sizeof(c->path))) {
        if (path_present(req)) {
            return web_send_result(req, DRIVE_ERR_BAD_NAME);   // too long or not decodable
        }
        c->path[0] = '\0';                                     // no path: the root folder
    }
    drive_zip_t *z = NULL;
    drive_result_t r = drive_zip_open(c->path, &z, c->base, sizeof(c->base));
    if (r != DRIVE_OK) {
        return web_send_result(req, r);
    }
    const int64_t t0 = esp_timer_get_time();
    esp_err_t ret = ESP_OK;
    r = count_tree(c, z);
    if (r != DRIVE_OK) {
        ret = web_send_result(req, r);
        goto out;
    }
    if (c->files > ZIP_MAX_FILES) {
        char msg[96];
        snprintf(msg, sizeof(msg), "Der Ordner enthält zu viele Dateien für ein ZIP (höchstens %d).", ZIP_MAX_FILES);
        ret = web_send_error(req, "413 Payload Too Large", "TOO_MANY_FILES", msg);
        goto out;
    }
    for (uint32_t i = 0; i * ZIP_CRC_BLOCK < c->files; i++) {
        const uint32_t n = c->files - i * ZIP_CRC_BLOCK;
        c->crc[i] = malloc((n < ZIP_CRC_BLOCK ? n : ZIP_CRC_BLOCK) * sizeof(uint32_t));
        if (!c->crc[i]) {
            ret = web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
            goto out;
        }
    }

    make_disposition(c->disp, sizeof(c->disp), c->base);
    httpd_resp_set_type(req, "application/zip");
    httpd_resp_set_hdr(req, "Content-Disposition", c->disp);
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    c->req = req;
    c->buf = web_io_buf();
    if (!stream_archive(c, z)) {
        // Header already sent: abort without the terminating chunk, so the client sees a
        // failed transfer instead of a silently broken archive.
        ESP_LOGE(TAG, "'%s': aborted after %llu bytes", c->path, (unsigned long long)c->pos);
        ret = ESP_FAIL;
        goto out;
    }
    ret = httpd_resp_send_chunk(req, NULL, 0);
    const int64_t dt_ms = (esp_timer_get_time() - t0) / 1000;
    ESP_LOGI(TAG, "'%s': %lu entries (%lu files), %llu bytes in %lld ms", c->path, (unsigned long)c->entries,
             (unsigned long)c->files, (unsigned long long)c->pos, dt_ms);
out:
    drive_zip_close(z);
    return ret;
}

esp_err_t web_zip_get(httpd_req_t *req)
{
    const drive_result_t blk = web_crypt_block();
    if (blk != DRIVE_OK) {
        return web_send_result(req, blk);
    }
    zip_ctx_t *c = calloc(1, sizeof(*c));
    if (!c) {
        return web_send_error(req, "500 Internal Server Error", "NO_MEM", "Zu wenig Arbeitsspeicher.");
    }
    const esp_err_t ret = zip_run(req, c);
    for (int i = 0; i < ZIP_CRC_BLOCKS; i++) {
        free(c->crc[i]);
    }
    free(c);
    return ret;
}
