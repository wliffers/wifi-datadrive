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
//
// Optional encryption (drive_crypt_*): an encrypted drive is LOCKED after every power-up.
// Locked, neither owner has a file system (host: "no medium", app: DRIVE_ERR_LOCKED);
// unlocking leads into the normal states above. A drive whose key header is damaged or
// unreadable is blocked the same way (DRIVE_CRYPT_DAMAGED, DRIVE_ERR_CRYPT_DAMAGED) and is
// never taken for an unencrypted one.
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

// drive_list() / drive_list_page(): every entry in the packed buffer is a header of DRIVE_LIST_HDR
// bytes - uint32_t size, uint32_t mtime (native byte order, copy with memcpy), uint8_t type
// (DRIVE_ENTRY_*) - followed by the NUL-terminated name.
#define DRIVE_LIST_HDR        9
#define DRIVE_LIST_ENTRY_MAX  (DRIVE_LIST_HDR + DRIVE_FILE_NAME_MAX + 1)   // largest packed entry
#define DRIVE_ENTRY_FILE      0
#define DRIVE_ENTRY_DIR       1

typedef enum {
    DRIVE_OWNER_NONE = 0,             // not initialised / failed
    DRIVE_OWNER_APP,
    DRIVE_OWNER_USB,
} drive_owner_t;

// Drive encryption (components/pd_crypt). While LOCKED the owner is APP with fs_ok = false:
// the USB host sees "no medium", every file operation returns DRIVE_ERR_LOCKED.
typedef enum {
    DRIVE_CRYPT_OFF = 0,              // medium is not encrypted
    DRIVE_CRYPT_LOCKED,               // encrypted, password needed (state after every power-up)
    DRIVE_CRYPT_UNLOCKED,             // encrypted, works like an unencrypted drive
    DRIVE_CRYPT_DAMAGED,              // key header present but invalid, or unreadable: blocked like
                                      // LOCKED (DRIVE_ERR_CRYPT_DAMAGED); only drive_crypt_force_format()
} drive_crypt_state_t;

#define DRIVE_CRYPT_PW_MIN_CHARS  12      // drive password: at least 12 characters ...
#define DRIVE_CRYPT_PW_MAX_BYTES  128     // ... and at most 128 bytes (UTF-8)
#define DRIVE_CRYPT_RECOVERY_LEN  40      // buffer for the recovery key "XXXX-XXXX-...-XXXX" + NUL

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
    drive_crypt_state_t crypt;        // encryption state (live, also during long operations)
} drive_status_t;

/** Details of the drive encryption, see drive_crypt_get_info(). */
typedef struct {
    drive_crypt_state_t state;
    bool available;                   // encryption possible here (self-test passed, medium large enough)
    bool hw_fast_path;                // AES peripheral driven directly (else slower mbedtls path)
    uint32_t retry_after_s;           // > 0: unlock/disable/change are refused for this many seconds
    uint32_t failed_attempts;         // wrong secrets in a row since boot (RAM only)
    uint32_t kdf_iterations;          // PBKDF2 iterations of the password (encrypted only)
    uint64_t volume_bytes;            // size the file system / host sees (0 while locked)
} drive_crypt_info_t;

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
    // Drive encryption
    DRIVE_ERR_LOCKED,                 // drive is encrypted and locked: unlock first
    DRIVE_ERR_AUTH,                   // wrong drive password / recovery key
    DRIVE_ERR_RETRY_LATER,            // too many wrong attempts: wait retry_after_s
    DRIVE_ERR_WEAK_PASSWORD,          // new password < DRIVE_CRYPT_PW_MIN_CHARS chars or > MAX_BYTES
    DRIVE_ERR_NOT_ENCRYPTED,          // operation needs an encrypted drive
    DRIVE_ERR_ALREADY_ENCRYPTED,      // setup on a drive that is already encrypted
    DRIVE_ERR_CRYPT_UNAVAILABLE,      // encryption not possible (self-test failed, medium too small
                                      // or not erasable)
    DRIVE_ERR_CRYPT_DAMAGED,          // key header damaged / unreadable (or fails authentication)
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
 * Folders have size and mtime 0. mtime is unix seconds (pd_time_from_fat()), 0 = unknown.
 * DRIVE_ERR_NOT_FOUND if the folder does not exist. Entries that do not fit are skipped and set
 * info->truncated (a later, shorter name may still fit). Heap: about 1.1 KiB during the call.
 */
drive_result_t drive_list(const char *dir, char *buf, size_t buf_len, drive_list_info_t *info);

/**
 * One page of a listing, for answers that are sent over the network: fills buf like drive_list(),
 * but starting at the visible entry number start_index (0 = first; hidden names are not counted)
 * and only as far as whole entries fit. The drive lock is held only while the folder is read -
 * send the page afterwards, then ask for the next one with start_index = *next_index while *more
 * is true. There is nothing to close in between: every page is a complete, independent read.
 *  - *next_index: visible entry number after the last one written (or skipped, see below).
 *  - *more: true if at least one more entry follows (it did not fit into this page).
 *  - info: count / used of THIS page; info->truncated only if an entry could not fit even into
 *    the empty buffer (never with buf_len >= DRIVE_LIST_ENTRY_MAX) - it is then skipped.
 * Each page re-opens the folder and skips start_index entries. If the folder changes between
 * pages, entries may be missed or repeated (by as many as were added/removed before the current
 * position); nothing else happens. Results as drive_list(), NOT_FOUND also if the folder was
 * removed between pages. *next_index > start_index whenever *more is true.
 */
drive_result_t drive_list_page(const char *dir, uint32_t start_index, char *buf, size_t buf_len,
                               drive_list_info_t *info, uint32_t *next_index, bool *more);

/** Called by drive_list_each() for every visible entry (folders: size and mtime 0). name is only
 *  valid during the call. Return false to stop the walk early. */
typedef bool (*drive_list_cb_t)(void *ctx, const char *name, bool is_dir, uint32_t size, uint32_t mtime);

/**
 * Listing without a buffer: cb is called for every visible entry, in directory order, while the
 * folder is read. Same checks and results as drive_list() (BAD_NAME, LOCKED, CRYPT_DAMAGED,
 * WRONG_OWNER, NO_FS, NOT_FOUND, IO); DRIVE_OK also when cb stopped the walk.
 * cb runs with the drive lock held: it must be quick and must NOT block - no network I/O (use
 * drive_list_page() for that), no drive_* calls. Heap: about 1.1 KiB during the call.
 */
drive_result_t drive_list_each(const char *dir, drive_list_cb_t cb, void *ctx);

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

/**
 * Move (copy = false) or copy (copy = true) `from` to `to`, replacing an existing target (file or
 * folder, WebDAV "Overwrite: T"). Everything happens under ONE acquisition of the drive lock, as a
 * long operation (status snapshot, BUSY for owner changes / lock / format), so neither the USB
 * host nor the auto-lock ever sees an intermediate state. The old target is renamed to a hidden
 * temporary name next to it and only removed after the new object took its place; a copy is made
 * under a hidden temporary name first. On failure the old target is restored. Without an existing
 * target the result is a plain drive_move()/drive_copy(). *replaced is set if a target was
 * replaced. Results as drive_move()/drive_copy(), plus: BAD_NAME also when the target is a folder
 * that contains the source, or for a copy onto the source itself under another spelling (case,
 * 8.3 alias; a move onto itself is a plain rename, as drive_move()); EXISTS when the
 * target is a hidden entry (reached through an alias); IO for a read-only target file. Copy needs
 * the full size of the source as free space (the old target is removed last).
 */
drive_result_t drive_replace(const char *from, const char *to, bool copy, bool *replaced);

/**
 * Re-create an empty FAT file system (APP only, erases all files).
 * Not encrypted: the device restarts and formats at boot (does not return on success).
 * Encrypted and unlocked: formatted immediately, returns DRIVE_OK (no restart - after a restart
 * the drive would be locked). Locked: DRIVE_ERR_LOCKED.
 */
drive_result_t drive_format(void);

/** Validate a file name (characters, length, allowed extension). */
drive_result_t drive_check_name(const char *name);

/** Open a file for download (APP only). IS_DIR, NOT_FOUND. Caller must call drive_download_close(). */
drive_result_t drive_download_open(const char *path, int *fd, uint32_t *size);
void drive_download_close(int fd);

// ---- ZIP download: walk a folder tree in a fixed depth-first order (see web_zip.c) ----

typedef struct drive_zip drive_zip_t;

typedef struct {
    const char *name;                 // path in the archive: "<folder>/sub/file" (no folder prefix
                                      // for the root); valid until the next drive_zip_* call
    bool is_dir;
    uint32_t size;                    // files only
    uint16_t dos_date, dos_time;      // FAT time stamp (local time), 0 = unknown
} drive_zip_entry_t;

/**
 * Start a walk of the folder `dir` (existing path, "" = root; APP only). Until drive_zip_close()
 * it counts as an open download: owner changes, format, lock and folder operations get BUSY.
 * base_out receives the folder's real name ("" for the root). BAD_NAME, NOT_FOUND (also for a
 * file), WRONG_OWNER, NO_FS, LOCKED, IO (no memory: about 3 KiB + one FatFs file object).
 */
drive_result_t drive_zip_open(const char *dir, drive_zip_t **out, char *base_out, size_t base_len);
/** Restart the walk: the folder itself comes first (not for the root), then its contents. */
void drive_zip_rewind(drive_zip_t *z);
/**
 * Next entry, a folder before its contents; hidden names are skipped as in drive_list(). *end is
 * set after the last one. BAD_NAME: nested deeper than 16 levels or path longer than 1.5 KB; IO.
 */
drive_result_t drive_zip_next(drive_zip_t *z, drive_zip_entry_t *e, bool *end);
/** Read the file returned last by drive_zip_next() (opened on the first call). *got = 0 at its end. */
drive_result_t drive_zip_read(drive_zip_t *z, void *buf, size_t len, size_t *got);
void drive_zip_close(drive_zip_t *z);

// ---- Upload: written to a temp file in the root and renamed only after validation ----

/**
 * Checks of an upload that do not depend on the drive (path, file type, size): BAD_NAME,
 * BAD_TYPE, TOO_LARGE, INVALID_FILE (size 0). drive_upload_begin() repeats them.
 */
drive_result_t drive_upload_validate(const char *path, uint64_t size);

/**
 * path: complete target path including the file name. NOT_FOUND (folder missing), IS_DIR.
 * Always starts fresh: a kept interrupted upload (see drive_upload_suspend()) is dropped.
 */
drive_result_t drive_upload_begin(const char *path, uint32_t size, bool overwrite);
drive_result_t drive_upload_write(const void *data, size_t len);
/** mtime: modification time to store for the file (unix seconds; utime() converts to local FAT
 *  time with the time zone), 0 = now. */
drive_result_t drive_upload_finish(uint32_t mtime);
/** Drop the running upload and its temp file. */
void drive_upload_abort(void);

/**
 * All checks of drive_upload_begin() without touching anything (for /api/upload/check): a kept
 * interrupted upload stays. Free space counts the kept part as available (it is either
 * continued or dropped by the next upload).
 */
drive_result_t drive_upload_check(const char *path, uint64_t size, bool overwrite);

// Resuming (RAM only, at most one interrupted upload). Dropped by a fresh upload (any target),
// by handing the drive to the USB host, lock / format / encryption changes, and by a restart.

/** The connection broke: keep the received bytes (closed = on the medium) for resuming. Without
 *  any bytes, or if closing fails, the same as drive_upload_abort(). */
void drive_upload_suspend(void);

/**
 * Continue an interrupted upload of exactly path and size at offset. offset must equal the bytes
 * held (*held, 0 if none); otherwise *mismatch is set (result DRIVE_ERR_INCOMPLETE, nothing
 * changes). offset 0 with nothing held is a fresh drive_upload_begin(). Then write the remaining
 * size - offset bytes and finish as usual; the content check covers the whole file.
 */
drive_result_t drive_upload_resume(const char *path, uint32_t size, bool overwrite, uint32_t offset,
                                   uint32_t *held, bool *mismatch);

/** *offset = bytes held for exactly path and size (0 if none). BAD_NAME for an invalid path. */
drive_result_t drive_upload_resume_query(const char *path, uint64_t size, uint32_t *offset);

/** The kept interrupted upload for the status (false if none). Does not block during long operations. */
bool drive_upload_resumable(char *path, size_t path_len, uint32_t *size, uint32_t *offset);

/**
 * Prepare a restart: refuse (unless force) while an upload runs or the host wrote
 * recently; otherwise block host I/O, drain pending host writes and drop an open upload.
 * Host I/O stays blocked afterwards - the caller is expected to restart.
 */
drive_result_t drive_prepare_restart(bool force);

/** Restart into ROM download mode (USB is detached first). */
void drive_restart_to_download(void) __attribute__((noreturn));

// ---- Drive encryption (AES-256-XTS, see components/pd_crypt/include/pd_crypt.h) ----
//
// All functions block the caller. Checking a secret takes one key derivation, about 2 s
// (CONFIG_PD_CRYPT_KDF_TARGET_MS; twice that if a damaged header copy has to be tried as well).
// Setup additionally ERASES the whole medium: about 2 min on the internal flash, typically
// 10 s - 2 min on a 64 GB card (card-dependent, can be longer), then formats (10-30 s on a big
// card). Meanwhile drive_get_status() returns the state from the start and other drive
// operations get DRIVE_ERR_BUSY. Common results: DRIVE_ERR_BUSY (long folder operation, upload or
// download running), DRIVE_ERR_NOT_ENCRYPTED, DRIVE_ERR_CRYPT_DAMAGED, DRIVE_ERR_IO.
// Wrong secrets: DRIVE_ERR_AUTH, after the second one in a row also a wait (5 s, doubling up to
// 300 s, RAM only); during the wait DRIVE_ERR_RETRY_LATER. *retry_after_s (may be NULL) is set
// in both cases (or see drive_crypt_get_info()). The password and the recovery key are accepted
// wherever "secret" is asked.

/** Encryption state and details. Never blocks (no lock taken). */
void drive_crypt_get_info(drive_crypt_info_t *out);

/**
 * Encrypt the drive: ERASES and FORMATS it (all files are lost) and leaves it unlocked and
 * mounted for the app. Needs owner APP (else DRIVE_ERR_WRONG_OWNER) and an unencrypted drive
 * (else DRIVE_ERR_ALREADY_ENCRYPTED / DRIVE_ERR_CRYPT_DAMAGED). DRIVE_ERR_WEAK_PASSWORD,
 * DRIVE_ERR_CRYPT_UNAVAILABLE (also: card refuses ERASE and is too large to overwrite).
 * recovery_out (>= DRIVE_CRYPT_RECOVERY_LEN bytes) receives the recovery key. IMPORTANT: whenever
 * recovery_out is non-empty after the call the drive IS encrypted - show the key even if the
 * result is not DRIVE_OK (e.g. DRIVE_ERR_IO when only the formatting failed: the drive is then
 * unlocked with needs_format set). A failed setup that left the drive unencrypted may already
 * have erased it (needs_format set). If the key got lost: drive_crypt_new_recovery_key().
 */
drive_result_t drive_crypt_setup(const char *password, char *recovery_out, size_t recovery_len);

/**
 * Unlock with the password or the recovery key. Afterwards the drive behaves as after a change
 * of owner: mounted for the app, and if a USB host is connected (CONFIG_PD_PROVIDE_ON_ATTACH)
 * provided to it with the usual media-change signalling / soft re-plug. Already unlocked:
 * DRIVE_OK. A volume without a readable FAT unlocks with needs_format set (drive_format()).
 * DRIVE_ERR_CRYPT_DAMAGED: the secret was right but the header fails its authentication.
 */
drive_result_t drive_crypt_unlock(const char *secret, uint32_t *retry_after_s);

/**
 * Lock: keys are wiped, the host sees "no medium", file operations get DRIVE_ERR_LOCKED.
 * If the USB host holds the drive, the rules of drive_set_owner(APP, force) apply:
 * DRIVE_ERR_HOST_WRITING (never forced), DRIVE_ERR_HOST_ACTIVE unless force. Already locked: OK.
 */
drive_result_t drive_crypt_lock(bool force);

/**
 * Change the password (unlocked only, else DRIVE_ERR_LOCKED). old_secret may be the current
 * password or the recovery key (= "password forgotten": unlock with the recovery key, then set a
 * new password with it). Files are not touched; works for either owner, but not while the host
 * is writing (DRIVE_ERR_HOST_WRITING). DRIVE_ERR_WEAK_PASSWORD for the new one.
 */
drive_result_t drive_crypt_change_password(const char *old_secret, const char *new_password,
                                           uint32_t *retry_after_s);

/**
 * New recovery key (unlocked only; preconditions as drive_crypt_change_password()). password may
 * be the password or the current recovery key. The old recovery key stops working. recovery_out
 * (>= DRIVE_CRYPT_RECOVERY_LEN bytes) receives the new one, the only time it is available.
 * Wrong secret: DRIVE_ERR_AUTH / DRIVE_ERR_RETRY_LATER (wait: drive_crypt_get_info()).
 */
drive_result_t drive_crypt_new_recovery_key(const char *password, char *recovery_out, size_t recovery_len);

/**
 * Remove the encryption: checks the secret first, then erases the key header and FORMATS the
 * drive plain (all files are lost). Works locked or unlocked; when unlocked it needs owner APP
 * (DRIVE_ERR_WRONG_OWNER). DRIVE_ERR_IO after a correct secret means only the formatting
 * failed: the drive is then plain with needs_format set.
 */
drive_result_t drive_crypt_disable(const char *secret, uint32_t *retry_after_s);

/**
 * "Erase and format anyway" (ADMIN ONLY, confirm in the UI): for a DAMAGED drive, or a LOCKED one
 * whose password and recovery key are lost. Overwrites the key header without a secret and
 * formats the drive plain - all data is lost for good. Needs no upload/download running.
 * DRIVE_ERR_ALREADY_ENCRYPTED when unlocked (use drive_crypt_disable() or drive_format()),
 * DRIVE_ERR_NOT_ENCRYPTED when not encrypted, DRIVE_ERR_IO if the medium cannot be written.
 */
drive_result_t drive_crypt_force_format(void);

// ---- Auto-lock of an unlocked encrypted drive (settings in NVS, timer in the drive task) ----

#define DRIVE_AUTOLOCK_MINUTES_MAX  1440

/** Current auto-lock settings: minutes of inactivity (0 = off), lock when the USB host goes away. */
void drive_autolock_get(uint16_t *minutes, bool *on_detach);

/**
 * Store and apply the auto-lock settings; the inactivity period restarts. minutes 0 (off) ..
 * DRIVE_AUTOLOCK_MINUTES_MAX, else ESP_ERR_INVALID_ARG. The timer runs only while the file manager
 * owns the drive (never in USB mode; switching back starts the period anew). Inactivity = no local
 * file operation, no upload/download. on_detach (also in USB mode): host not
 * configured for 10 s, or suspended for 5 min without READ/WRITE. Full rules in drive.c
 * ("auto-lock"). The lock is never forced past DRIVE_ERR_BUSY or DRIVE_ERR_HOST_WRITING (retried
 * every few seconds instead).
 */
esp_err_t drive_autolock_set(uint16_t minutes, bool on_detach);

/**
 * Seconds until the inactivity timer would lock the drive if nothing happens, or -1 if no
 * countdown runs (timer off, drive not encrypted or locked, drive in USB mode, upload/download/long
 * operation running). 0 once the time is up; *waiting (may be NULL) is then true if the last lock
 * attempt was refused (busy). Cheap, takes no
 * lock and does not count as activity (status polls).
 */
int32_t drive_autolock_remaining_s(bool *waiting);

/** "Still working": restart the inactivity period now. Returns drive_autolock_remaining_s(). */
int32_t drive_autolock_touch(void);

#ifdef __cplusplus
}
#endif
