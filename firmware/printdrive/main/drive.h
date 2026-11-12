// Storage ownership manager ("drive").
//
// The FAT file system on the internal flash has exactly ONE owner at any time:
//
//   DRIVE_OWNER_APP  - mounted locally at DRIVE_BASE_PATH. The web server may list,
//                      upload, download and manage files and folders. The USB host
//                      sees "no medium" (like an empty card reader); every host
//                      READ/WRITE is refused.
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
#define DRIVE_NAME_MAX   120          // max length of one new name segment (bytes, UTF-8)
#define DRIVE_FILE_NAME_MAX 255      // max length of existing (e.g. host-created) names

// Paths (API v3, section 3.1): relative to the root, '/'-separated, no leading or trailing
// '/', "" = root folder. See drive_check_path().
#define DRIVE_PATH_MAX        240     // max total length of a path that creates something (bytes)
#define DRIVE_PATH_DEPTH_MAX  8       // max number of segments of any path
#define DRIVE_SRC_PATH_MAX    765     // max total length of an existing path accepted as input

// drive_list(): every entry in the packed buffer is a header of DRIVE_LIST_HDR bytes -
// uint32_t size, uint32_t mtime (native byte order, copy with memcpy), uint8_t type
// (DRIVE_ENTRY_*) - followed by the NUL-terminated name.
#define DRIVE_LIST_HDR        9
#define DRIVE_ENTRY_FILE      0
#define DRIVE_ENTRY_DIR       1

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
    uint32_t count;                   // entries (files and folders) written to the buffer
    size_t used;                      // bytes used in the buffer
    bool truncated;                   // buffer was too small for all entries
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
    DRIVE_ERR_DIR_NOT_EMPTY,          // folder not empty and recursive delete not requested
    DRIVE_ERR_IS_DIR,                 // a file was expected, the path names a folder
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
 * Validate a path from the network (API v3, 3.1). The single gate against path traversal:
 * every file operation below calls it before the path touches the file system.
 *  - for_new = true:  path of something that is created (mkdir, upload, move/copy target):
 *                     every segment follows the name rules of drive_check_name() (without the
 *                     type check), at most DRIVE_PATH_DEPTH_MAX segments, DRIVE_PATH_MAX bytes.
 *                     The empty path (root) is refused.
 *  - for_new = false: path of something that exists (list, download, delete, move/copy
 *                     source): segments up to DRIVE_FILE_NAME_MAX bytes, no leading dot, no
 *                     characters FAT cannot store; the empty path (root) is accepted.
 * Returns DRIVE_OK or DRIVE_ERR_BAD_NAME.
 */
drive_result_t drive_check_path(const char *path, bool for_new);

/**
 * List the folder `dir` (existing path, "" = root; APP only) into a packed buffer, see
 * DRIVE_LIST_HDR. Names with a leading dot and "System Volume Information" are skipped.
 * Folders have size and mtime 0. mtime is seconds since 1970 in the writer's local time,
 * 0 = unknown. DRIVE_ERR_NOT_FOUND if the folder does not exist.
 */
drive_result_t drive_list(const char *dir, char *buf, size_t buf_len, drive_list_info_t *info);

/** Create exactly one folder (the parent must exist). NOT_FOUND, EXISTS. */
drive_result_t drive_mkdir(const char *path);

/** Delete a file or folder. A non-empty folder needs recursive, else DIR_NOT_EMPTY. */
drive_result_t drive_delete(const char *path, bool recursive);

/**
 * Rename and/or move a file or folder; `to` is the complete new path. EXISTS (changing only
 * the case is allowed), NOT_FOUND (source or target folder missing), BAD_NAME (folder into
 * itself or a descendant, or the result would be deeper than DRIVE_PATH_DEPTH_MAX).
 */
drive_result_t drive_move(const char *from, const char *to);

/**
 * Copy a file or a folder (recursively). EXISTS, NOT_FOUND, BAD_NAME (as drive_move()),
 * NO_SPACE (checked first: sum of the file sizes + reserve). A partial copy is removed again
 * if anything fails. Blocks all other file operations while it runs.
 */
drive_result_t drive_copy(const char *from, const char *to);

/** Re-create an empty FAT file system (APP only, erases all files). */
drive_result_t drive_format(void);

/** Validate a file name (characters, length, allowed extension). */
drive_result_t drive_check_name(const char *name);

/** Open a file for download (APP only). IS_DIR, NOT_FOUND. Caller must call drive_download_close(). */
drive_result_t drive_download_open(const char *path, int *fd, uint32_t *size);
void drive_download_close(int fd);

// ---- Upload: written to a temp file in the root and renamed only after validation ----

/**
 * Checks of an upload that do not depend on the drive (path, file type, size): BAD_NAME,
 * BAD_TYPE, TOO_LARGE, INVALID_FILE (size 0). drive_upload_begin() repeats them.
 */
drive_result_t drive_upload_validate(const char *path, uint64_t size);

/** path: complete target path including the file name. NOT_FOUND (folder missing), IS_DIR. */
drive_result_t drive_upload_begin(const char *path, uint32_t size, bool overwrite);
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
