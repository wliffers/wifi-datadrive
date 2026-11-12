// Drive encryption layer: AES-256-XTS per 512-byte sector between the storage medium
// (microSD card or wear-levelled flash) and everything above it (FatFs, USB MSC).
//
// Medium layout when encrypted (all sectors 512 bytes, numbers are physical sectors;
// A = alignment, 8192 sectors = 4 MiB on a microSD card, 8 sectors = 4 KiB on the flash):
//
//   0                      primary header (see pd_crypt.c, "Header layout")
//   1 .. A-1               reserved (erased at setup), keeps the volume aligned
//   A .. A+data_sectors-1  encrypted volume. Volume sector n is stored at physical sector
//                          A + n, encrypted with tweak n.
//   .. phys-2              reserved tail, at least A sectors, the volume ends on an A boundary
//   phys-1                 backup header
//
// Keys: a random 512-bit master key (two AES-256 keys for XTS) encrypts the data. The header
// stores it twice, wrapped by a key derived from the password (PBKDF2-HMAC-SHA256, iteration
// count calibrated so that one derivation takes CONFIG_PD_CRYPT_KDF_TARGET_MS, default 2 s) and
// by a key derived from the recovery key. The geometry fields are authenticated with the master
// key. Changing the password only rewrites the header. Header updates are power-fail safe:
// the backup copy is written first, then the primary; each copy carries a SHA-256 checksum
// and a generation number.
//
// Setup erases the whole medium (SD ERASE command / flash erase) before the header is written,
// so nothing of an earlier unencrypted file system stays readable.
//
// Thread safety: all functions may be called from any task. Raw medium I/O, key changes and
// header writes are serialised internally; pd_crypt_state(), pd_crypt_get_info() and
// pd_crypt_volume_sectors() never wait. The caller (drive.c) makes sure nothing above uses the
// volume while it is locked, set up, disabled or force-erased.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "sdmmc_cmd.h"
#include "wear_levelling.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PD_CRYPT_SECTOR_SIZE   512
#define PD_CRYPT_PW_MIN_CHARS  12       // minimum password length (Unicode characters)
#define PD_CRYPT_PW_MAX_BYTES  128      // maximum password length (bytes, UTF-8)
// Recovery key: 32 Crockford base32 characters in 8 groups of 4, e.g.
// "7K3M-Q0TZ-...". Buffer size including the terminating NUL:
#define PD_CRYPT_RECOVERY_LEN  40

typedef enum {
    PD_CRYPT_NONE = 0,                  // not attached (init failed)
    PD_CRYPT_PLAIN,                     // medium is not encrypted
    PD_CRYPT_LOCKED,                    // encrypted, keys not in RAM: no volume access
    PD_CRYPT_UNLOCKED,                  // encrypted, volume accessible
    PD_CRYPT_DAMAGED,                   // header present but no copy valid, or sector 0 unreadable:
                                        // blocked like LOCKED; only pd_crypt_force_plain() helps
} pd_crypt_state_t;

typedef enum {
    PD_CRYPT_OK = 0,
    PD_CRYPT_ERR_STATE,                 // wrong state for the operation (e.g. setup when encrypted)
    PD_CRYPT_ERR_AUTH,                  // wrong password / recovery key
    PD_CRYPT_ERR_WAIT,                  // too many wrong attempts: retry after retry_after_s
    PD_CRYPT_ERR_WEAK,                  // new password shorter than PD_CRYPT_PW_MIN_CHARS or too long
    PD_CRYPT_ERR_IO,                    // medium read/write failed
    PD_CRYPT_ERR_NO_MEM,
    PD_CRYPT_ERR_UNSUPPORTED,           // medium too small / not erasable / self-test failed
    PD_CRYPT_ERR_DAMAGED,               // secret correct, but the header fails its authentication
} pd_crypt_err_t;

/** Raw access to the physical medium (sectors of PD_CRYPT_SECTOR_SIZE bytes). */
typedef struct {
    esp_err_t (*read)(void *ctx, uint32_t sector, void *buf, uint32_t count);
    esp_err_t (*write)(void *ctx, uint32_t sector, const void *buf, uint32_t count);
    /** Make sectors unreadable (SD ERASE / flash erase). NULL or error: pd_crypt overwrites. */
    esp_err_t (*erase)(void *ctx, uint32_t sector, uint32_t count);
    void *ctx;
    uint32_t sectors;                   // physical size in sectors (0: unsuitable, never encrypted)
    uint32_t align;                     // volume start / end alignment (= data_start)
    uint32_t erase_chunk;               // sectors per erase call (progress, lock release)
} pd_crypt_medium_t;

typedef struct {
    pd_crypt_state_t state;
    bool available;                     // setup possible (self-test passed, medium suitable)
    bool hw_fast_path;                  // AES peripheral driven directly (else mbedtls per block)
    uint32_t volume_sectors;            // what the file system / USB host see (0 locked/damaged)
    uint32_t data_start;                // see layout above (encrypted only)
    uint32_t kdf_iterations;            // PBKDF2 iterations of the password slot (encrypted only)
    uint32_t failed_attempts;           // wrong secrets since the last success (RAM only)
    uint32_t retry_after_s;             // > 0: next attempt is refused for this many seconds
    uint64_t generation;                // header generation (encrypted only)
} pd_crypt_info_t;

/** Medium descriptors for the two supported media (alignment chosen accordingly). */
void pd_crypt_medium_sdmmc(sdmmc_card_t *card, pd_crypt_medium_t *out);
void pd_crypt_medium_wl(wl_handle_t wl, pd_crypt_medium_t *out);

/**
 * Attach the medium at boot: runs the self-test, reads both header copies independently and
 * sets the state to PLAIN, LOCKED or DAMAGED. Never writes to the medium. Fails closed: if the
 * medium cannot be classified, the state is DAMAGED (or NONE if not even that is possible,
 * which the caller must also treat as blocked).
 */
esp_err_t pd_crypt_attach(const pd_crypt_medium_t *medium);

pd_crypt_state_t pd_crypt_state(void);
/** Not PLAIN/NONE: the raw medium must not be used directly (LOCKED, UNLOCKED, DAMAGED). */
bool pd_crypt_is_encrypted(void);
void pd_crypt_get_info(pd_crypt_info_t *out);

/** Size of the volume above this layer: physical size (PLAIN), data_sectors (UNLOCKED), else 0. */
uint32_t pd_crypt_volume_sectors(void);

/**
 * Sector I/O on the encrypted volume (sector 0 = first sector the file system / host sees).
 * UNLOCKED only (else ESP_ERR_INVALID_STATE; PLAIN media are accessed by the normal drivers).
 * read decrypts in place in buf; write never modifies buf (it encrypts via an internal buffer).
 */
esp_err_t pd_crypt_read(uint32_t sector, void *buf, uint32_t count);
esp_err_t pd_crypt_write(uint32_t sector, const void *buf, uint32_t count);

/** Register a FatFs disk driver for drive number pdrv that uses pd_crypt_read/write (+ TRIM). */
void pd_crypt_diskio_register(uint8_t pdrv);

/** Password policy check (length only): PD_CRYPT_OK or PD_CRYPT_ERR_WEAK. */
pd_crypt_err_t pd_crypt_check_password(const char *password);

/**
 * PLAIN -> UNLOCKED. Erases the medium (minutes, see pd_crypt.c), writes a new header; the
 * caller formats the new volume. recovery_out (>= PD_CRYPT_RECOVERY_LEN bytes) receives the
 * recovery key; it is shown once and never stored on the device.
 */
pd_crypt_err_t pd_crypt_setup(const char *password, char *recovery_out, size_t recovery_len);

/**
 * LOCKED -> UNLOCKED with the password or the recovery key (detected by its format; a secret in
 * recovery key format that does not match is also tried as a password). Takes about one KDF
 * time. Wrong attempts: PD_CRYPT_ERR_AUTH, then an increasing wait (PD_CRYPT_ERR_WAIT);
 * *retry_after_s (may be NULL) tells how long. Already UNLOCKED: PD_CRYPT_OK.
 */
pd_crypt_err_t pd_crypt_unlock(const char *secret, uint32_t *retry_after_s);

/** UNLOCKED -> LOCKED: keys and buffers are wiped. The caller must have stopped all volume I/O. */
void pd_crypt_lock(void);

/**
 * Re-wrap the master key with a new password (UNLOCKED only). old_secret may be the current
 * password or the recovery key. The data is not touched.
 */
pd_crypt_err_t pd_crypt_change_password(const char *old_secret, const char *new_password,
                                        uint32_t *retry_after_s);

/**
 * New recovery key (UNLOCKED only, secret = password or current recovery key): slot 1 is
 * re-wrapped, the old recovery key stops working. recovery_out as for pd_crypt_setup().
 */
pd_crypt_err_t pd_crypt_new_recovery_key(const char *secret, char *recovery_out, size_t recovery_len,
                                         uint32_t *retry_after_s);

/**
 * LOCKED/UNLOCKED -> PLAIN after checking the password or recovery key: both header copies are
 * erased (the data becomes unreadable for good). The caller formats the plain medium.
 */
pd_crypt_err_t pd_crypt_disable(const char *secret, uint32_t *retry_after_s);

/**
 * LOCKED/DAMAGED -> PLAIN without a secret ("erase and format anyway"; all data is lost):
 * both header sectors are overwritten. The caller formats the plain medium.
 */
esp_err_t pd_crypt_force_plain(void);

/**
 * Run the full AES-256-XTS known-answer tests again (IEEE P1619/D16 vectors 10 and 14, direct
 * path against mbedtls across a batch boundary). Refused (ESP_ERR_INVALID_STATE) while UNLOCKED.
 */
esp_err_t pd_crypt_selftest(void);

#ifdef __cplusplus
}
#endif
