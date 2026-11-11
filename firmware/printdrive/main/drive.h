// Storage ownership manager ("drive").
//
// The FAT file system on the internal flash has exactly ONE owner at any time:
//
//   DRIVE_OWNER_APP  - mounted locally at DRIVE_BASE_PATH. The web server may list,
//                      upload and delete files. The USB host sees "no medium"
//                      (like an empty card reader); every host READ/WRITE is refused.
//   DRIVE_OWNER_USB  - not mounted locally. Only the USB host accesses the sectors.
//                      All local file operations are refused.
//
// Switching is only done by drive_set_owner() under a mutex, never while an upload
// is in progress, and USB -> APP only when the host has ejected the drive, is gone,
// or the caller explicitly forces it while the host has been write-idle for a while.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DRIVE_BASE_PATH  "/data"
#define DRIVE_NAME_MAX   120          // max length of uploaded file names (bytes, UTF-8)
#define DRIVE_FILE_NAME_MAX 255      // max length of existing (e.g. host-created) names

typedef enum {
    DRIVE_OWNER_NONE = 0,             // not initialised / failed
    DRIVE_OWNER_APP,
    DRIVE_OWNER_USB,
} drive_owner_t;

typedef struct {
    drive_owner_t owner;
    bool fs_ok;                       // file system mounted and usable (APP) / was usable (USB)
    bool needs_format;                // no valid FAT found and not auto-formatted
    bool usb_connected;               // host has configured the device
    bool usb_suspended;
    bool host_ejected;                // host sent "eject" since the drive was last provided
    bool host_writable;               // host may write (setting, applied on next provide)
    uint32_t host_idle_ms;            // since last host READ/WRITE (UINT32_MAX = never)
    uint32_t host_write_idle_ms;      // since last host WRITE (UINT32_MAX = never)
    uint64_t total_bytes;             // last known file system size
    uint64_t free_bytes;              // last known free space
    uint32_t sector_size;
    bool on_sd;                       // medium is the microSD card (else internal flash)
    char medium_name[12];             // card name from CID, or "Flash"
    uint32_t file_count;              // last known number of files in the root folder
    // Upload in progress
    bool upload_active;
    char upload_name[DRIVE_NAME_MAX + 1];
    uint32_t upload_received;
    uint32_t upload_total;
} drive_status_t;

/** Result summary of drive_list(). */
typedef struct {
    uint32_t count;                   // files written to the buffer
    uint32_t dirs;                    // folders in the root directory (not listed)
    size_t used;                      // bytes used in the buffer
    bool truncated;                   // buffer was too small for all files
} drive_list_info_t;

typedef enum {
    DRIVE_OK = 0,
    DRIVE_ERR_BUSY,                   // upload in progress
    DRIVE_ERR_HOST_ACTIVE,            // host uses the drive; eject first or force
    DRIVE_ERR_HOST_WRITING,           // host wrote very recently; never forced
    DRIVE_ERR_WRONG_OWNER,            // operation needs the other owner
    DRIVE_ERR_NO_FS,                  // file system missing/broken
    DRIVE_ERR_BAD_NAME,
    DRIVE_ERR_BAD_TYPE,               // file extension not allowed
    DRIVE_ERR_EXISTS,
    DRIVE_ERR_NOT_FOUND,
    DRIVE_ERR_NO_SPACE,
    DRIVE_ERR_INVALID_FILE,           // content check failed (e.g. not a ZIP/3MF)
    DRIVE_ERR_INCOMPLETE,             // fewer bytes than announced
    DRIVE_ERR_TOO_LARGE,              // file >= 4 GiB (FAT limit)
    DRIVE_ERR_IO,
} drive_result_t;

/** Human readable (German) text for a result code. */
const char *drive_result_str(drive_result_t r);

/** Initialise flash storage, TinyUSB (MSC + optional CDC log) and the ownership state. */
esp_err_t drive_init(void);

void drive_get_status(drive_status_t *out);

/**
 * Request a new owner.
 *  - to USB: refused while an upload is active.
 *  - to APP: if the host is connected and did not eject, refused with
 *    DRIVE_ERR_HOST_ACTIVE unless force is set; even with force refused with
 *    DRIVE_ERR_HOST_WRITING if the host wrote within CONFIG_PD_HOST_WRITE_GUARD_MS.
 */
drive_result_t drive_set_owner(drive_owner_t target, bool force);

/** Allow or deny host writes. Stored in NVS; takes effect the next time the drive is provided. */
esp_err_t drive_set_host_writable(bool writable);

/**
 * List regular files in the root folder (APP only) into a packed buffer. Each entry is
 * 4 bytes size + 4 bytes mtime (both native uint32_t, copy with memcpy) followed by the
 * NUL-terminated name. mtime is seconds since 1970 in the writer's local time, 0 = unknown.
 */
drive_result_t drive_list(char *buf, size_t buf_len, drive_list_info_t *info);

/** Rename a file in the root folder (APP only). `to` must pass drive_check_name(). */
drive_result_t drive_rename(const char *name, const char *to);

drive_result_t drive_delete(const char *name);

/** Re-create an empty FAT file system (APP only, erases all files). */
drive_result_t drive_format(void);

/** Validate a file name (characters, length, allowed extension). */
drive_result_t drive_check_name(const char *name);

/** Open a file for download (APP only). Caller must call drive_download_close(). */
drive_result_t drive_download_open(const char *name, int *fd, uint32_t *size);
void drive_download_close(int fd);

// ---- Upload: written to a temp file and renamed only after validation ----
drive_result_t drive_upload_begin(const char *name, uint32_t size, bool overwrite);
drive_result_t drive_upload_write(const void *data, size_t len);
/** mtime: modification time to store for the file (local time as seconds since 1970), 0 = now. */
drive_result_t drive_upload_finish(uint32_t mtime);
void drive_upload_abort(void);

/**
 * Prepare a restart: refuse (unless force) while an upload runs or the host wrote
 * recently; otherwise block host I/O, drain pending host writes and drop an open upload.
 * Host I/O stays blocked afterwards - the caller is expected to restart.
 */
drive_result_t drive_prepare_restart(bool force);

/** Restart into ROM download mode (USB is detached first). */
void drive_restart_to_download(void) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif
