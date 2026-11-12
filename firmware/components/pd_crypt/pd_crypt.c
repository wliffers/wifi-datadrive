// Drive encryption layer - see pd_crypt.h for the concept and the medium layout.
//
// Header layout (one 512-byte sector, little endian; primary at sector 0, backup at phys-1):
//
//   off size  field
//     0    8  magic "PDCRYPT1"
//     8    2  version (2)
//    10    2  sector size (512)
//    12    4  flags (0)
//    16    8  generation (incremented by every header change)
//    24    4  physical sectors of the medium at setup (must match at attach)
//    28    4  data_start  (first physical sector of the volume)
//    32    4  data_sectors (volume size)
//    36    4  backup sector (= physical sectors - 1)
//    40   16  key check = HMAC-SHA256(MK, "pd_crypt mk check")[0..15]
//    56  136  slot 0: password          slot layout: salt[32], iterations u32, reserved u32,
//   192  136  slot 1: recovery key                   wrapped[64], tag[32]
//   328   32  header MAC = HMAC-SHA256(MK, "pd_crypt hdr" || bytes 0..55)
//   360  120  reserved (0)
//   480   32  SHA-256 of bytes 0..479 (integrity only; the MAC is checked after unlock)
//
// Slot (secret = password bytes (UTF-8) or the 20 bytes of the recovery key):
//   dk     = PBKDF2-HMAC-SHA256(secret, salt, iterations, 32)
//   K_enc  = HMAC(dk, "pd_crypt enc"),  K_mac = HMAC(dk, "pd_crypt mac")
//   wrapped = MK xor (HMAC(K_enc, 0x01) || HMAC(K_enc, 0x02))
//   tag    = HMAC(K_mac, "PDCRYPT1" || slot index (1 byte) || salt || iterations (le32) || wrapped)
// Every slot write uses a fresh random salt, so a K_enc never wraps two different keys.
// MK = Key1 (data, 32 bytes) || Key2 (tweak, 32 bytes) as in IEEE 1619 / mbedtls / OpenSSL.
// Tweak of volume sector n = n as a 128-bit little-endian number.
//
// Erase at setup (everything except the two header sectors), in chunks with the I/O lock
// released in between:
//   microSD: SD ERASE command (CMD32/33/38), 64 MiB per command. The card only marks blocks as
//            erased; typical cards need well under a second per command, i.e. roughly 10 s to
//            2 min for 64 GB (~950 commands). The SD spec allows cards to report much longer
//            erase timeouts (worst case tens of minutes for 64 GB); the driver waits blocking
//            (no busy loop), so the calling HTTP task blocks but the watchdog is not affected.
//            DISCARD is not used: after it the card may still return the old data.
//            If ERASE fails, the chunk is overwritten with zeros, up to
//            CONFIG_PD_CRYPT_OVERWRITE_MAX_MB; beyond that setup is refused (a full overwrite
//            of a large card would take hours).
//   flash:   wl_erase_range() in 256 KiB steps: ~2700 flash sectors of 4 KiB at ~45 ms, about
//            2 minutes for the 10.8 MB partition (spi_flash yields during erase).
//
// tools/pd_crypt_check.py re-implements the format independently (vectors, header, unlock).
#include "pd_crypt.h"

#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include "diskio_impl.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/aes.h"
#include "mbedtls/md.h"
#include "mbedtls/pkcs5.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include "sdkconfig.h"
#if CONFIG_PD_CRYPT_HW_DIRECT
#include "aes/esp_aes.h"
#include "hal/aes_hal.h"
#include "hal/aes_ll.h"
#define HW_DIRECT true
#else
#define HW_DIRECT false
#endif
#include "xts_vectors.h"

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "pd_crypt assumes a little-endian CPU (header layout, XTS tweak arithmetic)"
#endif

static const char *TAG = "pd_crypt";

#define SS                  PD_CRYPT_SECTOR_SIZE
#define MAGIC               "PDCRYPT1"
#define HDR_VERSION         2
#define MIN_VOLUME_SECTORS  2048        // refuse media below 1 MiB of volume
#define SLOT_PW             0
#define SLOT_REC            1
#define REC_BYTES           20          // 160-bit recovery key
#define REC_CHARS           32          // base32
#define REC_ITER            1000        // high-entropy secret: the KDF only needs to be a PRF
#define KDF_PROBE_ITER      1000
#define KDF_PROBES          5           // the fastest probe counts (preemption only slows down)
#define KDF_MIN_ITER        10000       // absolute floor (besides half a second's worth)
#define KDF_MAX_ITER        10000000
#define KDF_HDR_CAP_FACTOR  4           // a header may ask for at most 4x our own calibration
#define TWEAK_BATCH         16          // sectors per hardware lock hold
#define CROSS_SECTORS       20          // self-test crosses the batch boundary
#define WAIT_FREE_FAILS     2           // wrong attempts without a wait
#define WAIT_FIRST_S        5
#define WAIT_MAX_S          300
#define SD_ALIGN            8192        // 4 MiB: keeps drive.c's 4 MiB FAT alignment physical
#define SD_ERASE_CHUNK      131072      // 64 MiB per ERASE command
#define FLASH_ALIGN         8           // 4 KiB: one flash erase sector
#define FLASH_ERASE_CHUNK   512         // 256 KiB
#define SCRATCH_BYTES       (CONFIG_PD_CRYPT_SCRATCH_KB * 1024)
#define ZERO_CHUNK          16          // sectors per overwrite write

static const char B32[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";   // Crockford, no I L O U

typedef struct __attribute__((packed)) {
    uint8_t salt[32];
    uint32_t iterations;
    uint32_t reserved;
    uint8_t wrapped[64];
    uint8_t tag[32];
} slot_t;

typedef struct __attribute__((packed)) {
    char magic[8];
    uint16_t version;
    uint16_t sector_size;
    uint32_t flags;
    uint64_t generation;
    uint32_t phys_sectors;
    uint32_t data_start;
    uint32_t data_sectors;
    uint32_t backup_sector;
    uint8_t mk_check[16];
    slot_t slot[2];
    uint8_t hdr_mac[32];
    uint8_t reserved[120];
    uint8_t checksum[32];
} hdr_t;

#define HDR_MAC_COVER 56                // bytes 0..55: all fields before the slots

_Static_assert(sizeof(slot_t) == 136, "slot layout");
_Static_assert(sizeof(hdr_t) == SS, "header must be one sector");
_Static_assert(offsetof(hdr_t, slot) == HDR_MAC_COVER, "header layout");
_Static_assert(offsetof(hdr_t, hdr_mac) == 328, "header layout");
_Static_assert(offsetof(hdr_t, checksum) == 480, "header layout");
_Static_assert(SCRATCH_BYTES % SS == 0, "scratch must hold whole sectors");

typedef union {
    uint64_t q[2];
    uint32_t w[4];
    uint8_t b[16];
} blk_t;

typedef enum { OPEN_OK, OPEN_AUTH, OPEN_BAD } open_t;

// ---- state
static SemaphoreHandle_t s_op;              // serialises setup/unlock/lock/change/disable/...
static SemaphoreHandle_t s_io;              // raw I/O, keys, scratch, header copies
static pd_crypt_medium_t s_m;
static volatile pd_crypt_state_t s_state = PD_CRYPT_NONE;
static hdr_t s_hdr;                         // current header (encrypted states)
static hdr_t s_alt;                         // other valid copy if it differs from s_hdr
static bool s_alt_valid;
static bool s_need_sync;                    // copies differ / one invalid: rewrite after unlock
static uint8_t s_key[64];                   // master key while UNLOCKED
static uint8_t *s_scratch;                  // write buffer while UNLOCKED (DMA capable)
static uint32_t s_scratch_sectors;
static uint8_t s_sector_buf[SS] __attribute__((aligned(4)));   // header I/O (internal RAM)
static bool s_available;
static bool s_selftest_ok;
static bool s_hw;                           // use the direct AES peripheral path
static volatile uint32_t s_fails;
static volatile int64_t s_next_try_us;
static uint32_t s_kdf_iter;                 // calibration result (cached per boot)
static mbedtls_aes_xts_context s_xts_enc, s_xts_dec;   // mbedtls path only
static bool s_xts_ctx;
// Lock-free copies for pd_crypt_get_info() / pd_crypt_volume_sectors() (written under s_io).
static volatile uint32_t s_info_volume, s_info_data_start, s_info_iter;
static volatile uint64_t s_info_gen;

// ============================================================== media

static esp_err_t sd_read(void *ctx, uint32_t sector, void *buf, uint32_t count)
{
    return sdmmc_read_sectors((sdmmc_card_t *)ctx, buf, sector, count);
}

static esp_err_t sd_write(void *ctx, uint32_t sector, const void *buf, uint32_t count)
{
    return sdmmc_write_sectors((sdmmc_card_t *)ctx, buf, sector, count);
}

static esp_err_t sd_erase(void *ctx, uint32_t sector, uint32_t count)
{
    return sdmmc_erase_sectors((sdmmc_card_t *)ctx, sector, count, SDMMC_ERASE_ARG);
}

static esp_err_t wlm_read(void *ctx, uint32_t sector, void *buf, uint32_t count)
{
    return wl_read((wl_handle_t)(intptr_t)ctx, (size_t)sector * SS, buf, (size_t)count * SS);
}

static esp_err_t wlm_erase(void *ctx, uint32_t sector, uint32_t count)
{
    return wl_erase_range((wl_handle_t)(intptr_t)ctx, (size_t)sector * SS, (size_t)count * SS);
}

static esp_err_t wlm_write(void *ctx, uint32_t sector, const void *buf, uint32_t count)
{
    esp_err_t err = wlm_erase(ctx, sector, count);   // same sequence as esp_tinyusb / diskio_wl
    return err == ESP_OK ? wl_write((wl_handle_t)(intptr_t)ctx, (size_t)sector * SS, buf, (size_t)count * SS)
                         : err;
}

void pd_crypt_medium_sdmmc(sdmmc_card_t *card, pd_crypt_medium_t *out)
{
    *out = (pd_crypt_medium_t){
        .read = sd_read, .write = sd_write, .erase = sd_erase, .ctx = card,
        .sectors = card->csd.sector_size == SS ? (uint32_t)card->csd.capacity : 0,
        .align = SD_ALIGN, .erase_chunk = SD_ERASE_CHUNK,
    };
}

void pd_crypt_medium_wl(wl_handle_t wl, pd_crypt_medium_t *out)
{
    *out = (pd_crypt_medium_t){
        .read = wlm_read, .write = wlm_write, .erase = wlm_erase, .ctx = (void *)(intptr_t)wl,
        .sectors = wl_sector_size(wl) == SS ? (uint32_t)(wl_size(wl) / SS) : 0,
        .align = FLASH_ALIGN, .erase_chunk = FLASH_ERASE_CHUNK,
    };
}

/** Volume position for this medium: [align, end), end aligned, >= align sectors of tail. */
static bool layout(uint32_t *start, uint32_t *count)
{
    const uint32_t a = s_m.align;
    if (a == 0 || s_m.sectors < 3 * a) {
        return false;
    }
    const uint32_t end = (s_m.sectors - a) / a * a;
    *start = a;
    *count = end > a ? end - a : 0;
    return *count >= MIN_VOLUME_SECTORS;
}

static esp_err_t raw_read(uint32_t sector, void *buf, uint32_t count)
{
    if ((uint64_t)sector + count > s_m.sectors) {
        return ESP_ERR_INVALID_SIZE;
    }
    return s_m.read(s_m.ctx, sector, buf, count);
}

static esp_err_t raw_write(uint32_t sector, const void *buf, uint32_t count)
{
    if ((uint64_t)sector + count > s_m.sectors) {
        return ESP_ERR_INVALID_SIZE;
    }
    return s_m.write(s_m.ctx, sector, buf, count);
}

/** State change + lock-free info copies (s_io held, or no concurrent user at attach). */
static void set_state(pd_crypt_state_t st)
{
    const bool enc = st == PD_CRYPT_LOCKED || st == PD_CRYPT_UNLOCKED;
    s_info_volume = st == PD_CRYPT_PLAIN ? s_m.sectors : (st == PD_CRYPT_UNLOCKED ? s_hdr.data_sectors : 0);
    s_info_data_start = enc ? s_hdr.data_start : 0;
    s_info_iter = enc ? s_hdr.slot[SLOT_PW].iterations : 0;
    s_info_gen = enc ? s_hdr.generation : 0;
    s_state = st;
}

// ============================================================== AES-256-XTS

static inline void gf_double(blk_t *t)
{
    // Multiply the tweak by x in GF(2^128), polynomial x^128 + x^7 + x^2 + x + 1 (IEEE 1619).
    const uint64_t carry = t->q[1] >> 63;
    t->q[1] = (t->q[1] << 1) | (t->q[0] >> 63);
    t->q[0] = (t->q[0] << 1) ^ (carry * 0x87);
}

#if CONFIG_PD_CRYPT_HW_DIRECT
/**
 * XTS with the AES peripheral in "typical" (register) mode, one block at a time without DMA.
 * mbedtls' XTS calls esp_aes_crypt_ecb() per 16-byte block, which sets up a DMA transfer and
 * takes the lock for every block (~33 times per sector); this path takes the lock and loads
 * each key once per TWEAK_BATCH sectors. Checked against mbedtls and IEEE vectors at boot.
 */
static void xts_hw(const uint8_t key[64], bool enc, uint64_t sector, uint8_t *buf, uint32_t count)
{
    blk_t tw[TWEAK_BATCH];
    while (count) {
        const uint32_t n = count < TWEAK_BATCH ? count : TWEAK_BATCH;
        esp_aes_acquire_hardware();            // shared AES/SHA lock, clock on, peripheral reset
        aes_ll_dma_enable(false);              // typical mode
        aes_hal_setkey(key + 32, 32, ESP_AES_ENCRYPT);
        for (uint32_t i = 0; i < n; i++) {
            tw[i].q[0] = sector + i;
            tw[i].q[1] = 0;
            aes_hal_transform_block(tw[i].b, tw[i].b);
        }
        aes_hal_setkey(key, 32, enc ? ESP_AES_ENCRYPT : ESP_AES_DECRYPT);
        for (uint32_t i = 0; i < n; i++) {
            blk_t t = tw[i];
            uint8_t *p = buf + (size_t)i * SS;
            for (int j = 0; j < SS / 16; j++, p += 16) {
                blk_t x;
                memcpy(x.b, p, 16);             // buffers may be unaligned (FatFs user data)
                x.w[0] ^= t.w[0], x.w[1] ^= t.w[1], x.w[2] ^= t.w[2], x.w[3] ^= t.w[3];
                aes_hal_transform_block(x.b, x.b);
                x.w[0] ^= t.w[0], x.w[1] ^= t.w[1], x.w[2] ^= t.w[2], x.w[3] ^= t.w[3];
                memcpy(p, x.b, 16);
                gf_double(&t);
            }
        }
        esp_aes_release_hardware();
        sector += n;
        buf += (size_t)n * SS;
        count -= n;
    }
    mbedtls_platform_zeroize(tw, sizeof(tw));
}

/** Overwrite the key registers (called when the keys are wiped). */
static void hw_clear_key(void)
{
    static const uint8_t zero[32];
    esp_aes_acquire_hardware();
    aes_hal_setkey(zero, 32, ESP_AES_ENCRYPT);
    esp_aes_release_hardware();
}
#endif

static int xts_sw(mbedtls_aes_xts_context *ctx, bool enc, uint64_t sector, uint8_t *buf, uint32_t count)
{
    for (uint32_t i = 0; i < count; i++, sector++, buf += SS) {
        blk_t du = {.q = {sector, 0}};
        const int r = mbedtls_aes_crypt_xts(ctx, enc ? MBEDTLS_AES_ENCRYPT : MBEDTLS_AES_DECRYPT, SS,
                                            du.b, buf, buf);
        if (r != 0) {
            return r;
        }
    }
    return 0;
}

/** En-/decrypt volume sectors in place with the master key (s_io held, UNLOCKED). */
static esp_err_t xts_volume(bool enc, uint32_t sector, uint8_t *buf, uint32_t count)
{
#if CONFIG_PD_CRYPT_HW_DIRECT
    if (s_hw) {
        xts_hw(s_key, enc, sector, buf, count);
        return ESP_OK;
    }
#endif
    return xts_sw(enc ? &s_xts_enc : &s_xts_dec, enc, sector, buf, count) == 0 ? ESP_OK : ESP_FAIL;
}

// ============================================================== self-test

static void kat_plain(uint8_t *b)
{
    for (int i = 0; i < SS; i++) {
        b[i] = (uint8_t)i;
    }
}

/** One known-answer test in both directions (b: 2 sectors). hw: direct path, else mbedtls. */
static bool kat(bool hw, uint64_t unit, const uint8_t *ct, uint8_t *b)
{
    bool ok = false;
    kat_plain(b);
#if CONFIG_PD_CRYPT_HW_DIRECT
    if (hw) {
        xts_hw(XTS_VEC_KEY, true, unit, b, 1);
        ok = memcmp(b, ct, SS) == 0;
        xts_hw(XTS_VEC_KEY, false, unit, b, 1);
    }
#endif
    if (!hw) {
        mbedtls_aes_xts_context e, d;
        mbedtls_aes_xts_init(&e);
        mbedtls_aes_xts_init(&d);
        ok = mbedtls_aes_xts_setkey_enc(&e, XTS_VEC_KEY, 512) == 0 &&
             mbedtls_aes_xts_setkey_dec(&d, XTS_VEC_KEY, 512) == 0 &&
             xts_sw(&e, true, unit, b, 1) == 0 && memcmp(b, ct, SS) == 0 &&
             xts_sw(&d, false, unit, b, 1) == 0;
        mbedtls_aes_xts_free(&e);
        mbedtls_aes_xts_free(&d);
    }
    uint8_t *ref = b + SS;
    kat_plain(ref);
    return ok && memcmp(b, ref, SS) == 0;
}

#if CONFIG_PD_CRYPT_HW_DIRECT
/** Direct path == mbedtls on CROSS_SECTORS sectors (crosses the TWEAK_BATCH boundary). b: 2x that. */
static bool cross_check(uint8_t *b)
{
    const size_t len = (size_t)CROSS_SECTORS * SS;
    uint8_t key[64];
    esp_fill_random(key, sizeof(key));
    esp_fill_random(b, len);
    memcpy(b + len, b, len);
    const uint64_t sector = 0x123456789ULL;
    xts_hw(key, true, sector, b, CROSS_SECTORS);
    mbedtls_aes_xts_context e;
    mbedtls_aes_xts_init(&e);
    bool ok = mbedtls_aes_xts_setkey_enc(&e, key, 512) == 0 &&
              xts_sw(&e, true, sector, b + len, CROSS_SECTORS) == 0 && memcmp(b, b + len, len) == 0;
    mbedtls_aes_xts_free(&e);
    mbedtls_platform_zeroize(key, sizeof(key));
    return ok;
}

/** Crypto-only throughput of both paths (logged at boot for the speed measurement). */
static void measure_speed(uint8_t *b)
{
    const uint32_t n = 16, rounds = 8;
    uint8_t key[64];
    esp_fill_random(key, sizeof(key));
    memset(b, 0xA5, n * SS);
    int64_t t0 = esp_timer_get_time();
    for (uint32_t r = 0; r < rounds; r++) {
        xts_hw(key, true, r * n, b, n);
    }
    const int64_t t_enc = esp_timer_get_time() - t0;
    t0 = esp_timer_get_time();
    for (uint32_t r = 0; r < rounds; r++) {
        xts_hw(key, false, r * n, b, n);
    }
    const int64_t t_dec = esp_timer_get_time() - t0;
    mbedtls_aes_xts_context e;
    mbedtls_aes_xts_init(&e);
    mbedtls_aes_xts_setkey_enc(&e, key, 512);
    t0 = esp_timer_get_time();
    xts_sw(&e, true, 0, b, n);
    const int64_t t_sw = esp_timer_get_time() - t0;
    mbedtls_aes_xts_free(&e);
    mbedtls_platform_zeroize(key, sizeof(key));
    const uint64_t bytes = (uint64_t)n * SS * rounds;
    ESP_LOGI(TAG, "XTS speed (crypto only): direct enc %llu KB/s, dec %llu KB/s; mbedtls %llu KB/s",
             (unsigned long long)(t_enc ? bytes * 1000 / t_enc : 0),
             (unsigned long long)(t_dec ? bytes * 1000 / t_dec : 0),
             (unsigned long long)(t_sw ? (uint64_t)n * SS * 1000 / t_sw : 0));
}
#endif

/**
 * full: both vectors on both paths, cross-check, speed. Otherwise one known answer on the path
 * that will be used (always run, also with CONFIG_PD_CRYPT_SELFTEST off). Sets s_hw.
 */
static esp_err_t selftest(bool full)
{
    const size_t buf_len = (size_t)2 * CROSS_SECTORS * SS;
    uint8_t *b = heap_caps_malloc(buf_len, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!b) {
        return ESP_ERR_NO_MEM;
    }
    bool sw_ok, hw_ok = false;
    if (full) {
        sw_ok = kat(false, XTS_VEC10_UNIT, XTS_VEC10_CT, b) && kat(false, XTS_VEC14_UNIT, XTS_VEC14_CT, b);
#if CONFIG_PD_CRYPT_HW_DIRECT
        hw_ok = kat(true, XTS_VEC10_UNIT, XTS_VEC10_CT, b) && kat(true, XTS_VEC14_UNIT, XTS_VEC14_CT, b) &&
                cross_check(b);
        if (hw_ok) {
            measure_speed(b);
        }
#endif
    } else {
#if CONFIG_PD_CRYPT_HW_DIRECT
        hw_ok = kat(true, XTS_VEC10_UNIT, XTS_VEC10_CT, b);
#endif
        sw_ok = hw_ok || kat(false, XTS_VEC10_UNIT, XTS_VEC10_CT, b);
    }
    mbedtls_platform_zeroize(b, buf_len);
    free(b);
    ESP_LOGI(TAG, "self-test AES-256-XTS (IEEE P1619 vector 10%s): mbedtls %s, direct %s", full ? ", 14, cross" : "",
             sw_ok ? "OK" : "FAILED", HW_DIRECT ? (hw_ok ? "OK" : "FAILED") : "disabled");
    s_hw = hw_ok;
    s_selftest_ok = sw_ok || hw_ok;
    if (!s_selftest_ok) {
        ESP_LOGE(TAG, "self-test failed - encryption disabled");
        return ESP_FAIL;
    }
    if (!hw_ok && HW_DIRECT) {
        ESP_LOGW(TAG, "direct AES path failed its self-test - using mbedtls (slower)");
    }
    return ESP_OK;
}

esp_err_t pd_crypt_selftest(void)
{
    if (!s_op) {
        return selftest(true);
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    // s_hw must not change while a key is in use (it selects the XTS path).
    const esp_err_t err = s_state == PD_CRYPT_UNLOCKED ? ESP_ERR_INVALID_STATE : selftest(true);
    xSemaphoreGive(s_op);
    return err;
}

// ============================================================== keys and header

static int hmac256(const uint8_t *key, size_t klen, const void *msg, size_t mlen, uint8_t out[32])
{
    return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, klen, msg, mlen, out);
}

static bool ct_equal(const uint8_t *a, const uint8_t *b, size_t n)
{
    uint8_t d = 0;
    for (size_t i = 0; i < n; i++) {
        d |= a[i] ^ b[i];
    }
    return d == 0;
}

static int64_t kdf_probe(void)
{
    static const uint8_t salt[32];
    uint8_t out[32];
    const int64_t t0 = esp_timer_get_time();
    mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, (const uint8_t *)"calibration", 11, salt, sizeof(salt),
                                  KDF_PROBE_ITER, sizeof(out), out);
    return esp_timer_get_time() - t0;
}

/** PBKDF2 iterations for CONFIG_PD_CRYPT_KDF_TARGET_MS on this device (cached per boot). */
static uint32_t kdf_iterations(void)
{
    if (s_kdf_iter) {
        return s_kdf_iter;
    }
    int64_t best = INT64_MAX;
    for (int i = 0; i < KDF_PROBES; i++) {
        const int64_t dt = kdf_probe();
        best = dt > 0 && dt < best ? dt : best;
    }
    const uint64_t rate = (uint64_t)KDF_PROBE_ITER * 1000000 / (uint64_t)best;   // iterations per second
    uint64_t it = rate * CONFIG_PD_CRYPT_KDF_TARGET_MS / 1000;
    const uint64_t floor = rate / 2 > KDF_MIN_ITER ? rate / 2 : KDF_MIN_ITER;      // >= 0.5 s worth
    it = it < floor ? floor : (it > KDF_MAX_ITER ? KDF_MAX_ITER : it);
    s_kdf_iter = (uint32_t)((it + 999) / 1000 * 1000);
    ESP_LOGI(TAG, "KDF calibration: %llu iterations/s (best of %d probes) -> %lu iterations (~%d ms)",
             (unsigned long long)rate, KDF_PROBES, (unsigned long)s_kdf_iter, CONFIG_PD_CRYPT_KDF_TARGET_MS);
    return s_kdf_iter;
}

static int derive(const uint8_t *secret, size_t len, const uint8_t salt[32], uint32_t iter,
                  uint8_t k_enc[32], uint8_t k_mac[32])
{
    uint8_t dk[32];
    int r = mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, secret, len, salt, 32, iter, sizeof(dk), dk);
    if (r == 0) {
        r = hmac256(dk, sizeof(dk), "pd_crypt enc", 12, k_enc);
    }
    if (r == 0) {
        r = hmac256(dk, sizeof(dk), "pd_crypt mac", 12, k_mac);
    }
    mbedtls_platform_zeroize(dk, sizeof(dk));
    return r;
}

static int slot_tag(const uint8_t k_mac[32], int index, const slot_t *s, uint8_t tag[32])
{
    uint8_t msg[8 + 1 + 32 + 4 + 64];
    memcpy(msg, MAGIC, 8);
    msg[8] = (uint8_t)index;
    memcpy(msg + 9, s->salt, 32);
    const uint32_t it = s->iterations;
    memcpy(msg + 41, &it, 4);
    memcpy(msg + 45, s->wrapped, 64);
    return hmac256(k_mac, 32, msg, sizeof(msg), tag);
}

/** XOR the 64-byte keystream of k_enc into buf. */
static int slot_stream_xor(const uint8_t k_enc[32], uint8_t buf[64])
{
    uint8_t ks[64];
    const uint8_t one = 1, two = 2;
    int r = hmac256(k_enc, 32, &one, 1, ks);
    if (r == 0) {
        r = hmac256(k_enc, 32, &two, 1, ks + 32);
    }
    for (int i = 0; i < 64; i++) {
        buf[i] ^= ks[i];
    }
    mbedtls_platform_zeroize(ks, sizeof(ks));
    return r;
}

static bool slot_make(slot_t *s, int index, const uint8_t *secret, size_t len, uint32_t iter,
                      const uint8_t mk[64])
{
    uint8_t k_enc[32], k_mac[32];
    memset(s, 0, sizeof(*s));
    esp_fill_random(s->salt, sizeof(s->salt));
    s->iterations = iter;
    memcpy(s->wrapped, mk, 64);
    uint8_t tag[32] = {0};
    const bool ok = derive(secret, len, s->salt, iter, k_enc, k_mac) == 0 &&
                    slot_stream_xor(k_enc, s->wrapped) == 0 && slot_tag(k_mac, index, s, tag) == 0;
    memcpy(s->tag, tag, sizeof(tag));
    mbedtls_platform_zeroize(k_enc, sizeof(k_enc));
    mbedtls_platform_zeroize(k_mac, sizeof(k_mac));
    return ok;
}

static bool slot_open(const slot_t *s, int index, const uint8_t *secret, size_t len, uint8_t mk[64])
{
    const uint32_t iter = s->iterations;
    const uint32_t cap = KDF_HDR_CAP_FACTOR * kdf_iterations();
    if (iter < 1 || iter > (cap > REC_ITER ? cap : REC_ITER)) {
        // A crafted header could otherwise block the caller for minutes.
        ESP_LOGE(TAG, "slot %d asks for %lu KDF iterations (limit %lu) - refused", index, (unsigned long)iter,
                 (unsigned long)cap);
        return false;
    }
    uint8_t k_enc[32], k_mac[32], tag[32] = {0}, stored[32];
    bool ok = derive(secret, len, s->salt, iter, k_enc, k_mac) == 0 && slot_tag(k_mac, index, s, tag) == 0;
    memcpy(stored, s->tag, sizeof(stored));
    ok = ok && ct_equal(tag, stored, sizeof(tag));
    if (ok) {
        memcpy(mk, s->wrapped, 64);
        ok = slot_stream_xor(k_enc, mk) == 0;
    }
    mbedtls_platform_zeroize(k_enc, sizeof(k_enc));
    mbedtls_platform_zeroize(k_mac, sizeof(k_mac));
    return ok;
}

static void mk_check(const uint8_t mk[64], uint8_t out[16])
{
    uint8_t h[32];
    hmac256(mk, 64, "pd_crypt mk check", 17, h);
    memcpy(out, h, 16);
    mbedtls_platform_zeroize(h, sizeof(h));
}

/** HMAC(MK, "pd_crypt hdr" || header bytes 0..55): authenticates magic, version, geometry. */
static void hdr_mac(const hdr_t *h, const uint8_t mk[64], uint8_t out[32])
{
    uint8_t msg[12 + HDR_MAC_COVER];
    memcpy(msg, "pd_crypt hdr", 12);
    memcpy(msg + 12, h, HDR_MAC_COVER);
    hmac256(mk, 64, msg, sizeof(msg), out);
}

static void hdr_seal(hdr_t *h)
{
    uint8_t sum[32];
    mbedtls_sha256((const uint8_t *)h, offsetof(hdr_t, checksum), sum, 0);
    memcpy(h->checksum, sum, sizeof(sum));
}

/** MAC (with the master key) and checksum after a change of the header. */
static void hdr_finish(hdr_t *h, const uint8_t mk[64])
{
    uint8_t mac[32];
    hdr_mac(h, mk, mac);
    memcpy(h->hdr_mac, mac, sizeof(mac));
    hdr_seal(h);
}

static bool hdr_magic(const hdr_t *h)
{
    return memcmp(h->magic, MAGIC, 8) == 0;
}

/** Checksum, version and geometry (the MAC needs the key: checked by hdr_open). */
static bool hdr_valid(const hdr_t *h)
{
    uint8_t sum[32];
    if (!hdr_magic(h)) {
        return false;
    }
    mbedtls_sha256((const uint8_t *)h, offsetof(hdr_t, checksum), sum, 0);
    if (memcmp(sum, h->checksum, sizeof(sum)) != 0) {
        return false;
    }
    return h->version == HDR_VERSION && h->sector_size == SS && h->phys_sectors == s_m.sectors &&
           h->backup_sector == s_m.sectors - 1 && h->data_start >= 1 && h->data_sectors >= MIN_VOLUME_SECTORS &&
           (uint64_t)h->data_start + h->data_sectors <= (uint64_t)s_m.sectors - 1;
}

/** Write the header: backup first, then primary (s_io held; hdr_finish() done). */
static esp_err_t hdr_write_both(const hdr_t *h)
{
    memcpy(s_sector_buf, h, SS);
    esp_err_t err = raw_write(s_m.sectors - 1, s_sector_buf, 1);
    if (err == ESP_OK) {
        err = raw_write(0, s_sector_buf, 1);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "header write failed (%s)", esp_err_to_name(err));
    }
    return err;
}

/** Overwrite both header copies: backup first, then primary (s_io held). Tries both. */
static esp_err_t hdr_erase_both(void)
{
    memset(s_sector_buf, 0, SS);
    const esp_err_t eb = raw_write(s_m.sectors - 1, s_sector_buf, 1);
    const esp_err_t ep = raw_write(0, s_sector_buf, 1);
    return eb != ESP_OK ? eb : ep;
}

/**
 * Make [start, end) unreadable: medium erase per chunk, zero overwrite as fallback (bounded).
 * Releases s_io between chunks (pd_crypt_get_info and the USB task are never held up long).
 */
static esp_err_t wipe_range(uint32_t start, uint32_t end)
{
    const uint64_t ow_max = (uint64_t)CONFIG_PD_CRYPT_OVERWRITE_MAX_MB * (1024 * 1024 / SS);
    const uint32_t chunk = s_m.erase_chunk ? s_m.erase_chunk : 1024;
    uint64_t overwritten = 0;
    uint8_t *zero = NULL;
    esp_err_t err = ESP_OK;
    int last_pct = -1;
    const int64_t t0 = esp_timer_get_time();
    ESP_LOGW(TAG, "erasing sectors %lu..%lu (%llu MiB)", (unsigned long)start, (unsigned long)end - 1,
             (unsigned long long)((uint64_t)(end - start) * SS >> 20));
    for (uint32_t s = start; s < end && err == ESP_OK;) {
        const uint32_t n = end - s < chunk ? end - s : chunk;
        xSemaphoreTake(s_io, portMAX_DELAY);
        esp_err_t e = s_m.erase ? s_m.erase(s_m.ctx, s, n) : ESP_ERR_NOT_SUPPORTED;
        xSemaphoreGive(s_io);
        if (e != ESP_OK) {
            if (overwritten == 0) {
                ESP_LOGW(TAG, "erase not possible at %lu (%s) - overwriting with zeros", (unsigned long)s,
                         esp_err_to_name(e));
            }
            // A failing first chunk on a big medium: refuse before anything is destroyed.
            const uint64_t need = s == start ? (uint64_t)(end - start) : n;
            if (overwritten + need > ow_max) {
                ESP_LOGE(TAG, "medium cannot be erased and is too large to overwrite (> %d MB)",
                         CONFIG_PD_CRYPT_OVERWRITE_MAX_MB);
                err = ESP_ERR_NOT_SUPPORTED;
                break;
            }
            if (!zero && !(zero = heap_caps_calloc(ZERO_CHUNK, SS, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL))) {
                err = ESP_ERR_NO_MEM;
                break;
            }
            for (uint32_t k = 0; k < n && err == ESP_OK;) {
                const uint32_t m = n - k < ZERO_CHUNK ? n - k : ZERO_CHUNK;
                xSemaphoreTake(s_io, portMAX_DELAY);
                err = raw_write(s + k, zero, m);
                xSemaphoreGive(s_io);
                k += m;
            }
            overwritten += n;
        }
        s += n;
        const int pct = (int)((uint64_t)(s - start) * 10 / (end - start)) * 10;
        if (pct != last_pct) {
            last_pct = pct;
            ESP_LOGI(TAG, "erase %d %% (%lld s)", pct, (esp_timer_get_time() - t0) / 1000000);
        }
        vTaskDelay(1);
    }
    free(zero);
    return err;
}

// ---- recovery key text

static void rec_encode(const uint8_t rec[REC_BYTES], char *out)
{
    uint32_t acc = 0;
    int bits = 0, n = 0;
    for (int i = 0; i < REC_BYTES; i++) {
        acc = (acc << 8) | rec[i];
        bits += 8;
        while (bits >= 5) {
            if (n > 0 && n % 4 == 0) {
                *out++ = '-';
            }
            *out++ = B32[(acc >> (bits - 5)) & 31];
            bits -= 5;
            n++;
        }
    }
    *out = '\0';
    mbedtls_platform_zeroize(&acc, sizeof(acc));
}

/** Parse a recovery key (case-insensitive, '-' and spaces ignored, O->0, I/L->1). */
static bool rec_decode(const char *s, uint8_t rec[REC_BYTES])
{
    uint8_t v[REC_CHARS];
    int n = 0;
    bool ok = true;
    for (; *s && ok; s++) {
        char c = *s;
        if (c == '-' || c == ' ') {
            continue;
        }
        if (c >= 'a' && c <= 'z') {
            c = (char)(c - 'a' + 'A');
        }
        c = c == 'O' ? '0' : (c == 'I' || c == 'L') ? '1' : c;
        const char *p = strchr(B32, c);
        ok = p && n < REC_CHARS;
        if (ok) {
            v[n++] = (uint8_t)(p - B32);
        }
    }
    ok = ok && n == REC_CHARS;
    if (ok) {
        uint32_t acc = 0;
        int bits = 0, o = 0;
        for (int i = 0; i < REC_CHARS; i++) {
            acc = (acc << 5) | v[i];
            bits += 5;
            if (bits >= 8) {
                rec[o++] = (uint8_t)(acc >> (bits - 8));
                bits -= 8;
            }
        }
        mbedtls_platform_zeroize(&acc, sizeof(acc));
    }
    mbedtls_platform_zeroize(v, sizeof(v));
    return ok;
}

/**
 * Try a secret against a header: recovery key format -> recovery slot, then password slot.
 * OPEN_BAD: a slot accepted the secret but the key check or the header MAC failed.
 */
static open_t hdr_open(const hdr_t *h, const char *secret, uint8_t mk[64])
{
    uint8_t rec[REC_BYTES], chk[16], mac[32];
    const size_t len = strnlen(secret, PD_CRYPT_PW_MAX_BYTES + 1);
    bool ok = false;
    if (rec_decode(secret, rec)) {
        ok = slot_open(&h->slot[SLOT_REC], SLOT_REC, rec, REC_BYTES, mk);
    }
    mbedtls_platform_zeroize(rec, sizeof(rec));
    if (!ok && len >= 1 && len <= PD_CRYPT_PW_MAX_BYTES) {
        ok = slot_open(&h->slot[SLOT_PW], SLOT_PW, (const uint8_t *)secret, len, mk);
    }
    if (!ok) {
        mbedtls_platform_zeroize(mk, 64);
        return OPEN_AUTH;
    }
    mk_check(mk, chk);
    hdr_mac(h, mk, mac);
    uint8_t stored[32];
    memcpy(stored, h->hdr_mac, sizeof(stored));
    if (memcmp(chk, h->mk_check, sizeof(chk)) != 0 || !ct_equal(mac, stored, sizeof(mac))) {
        ESP_LOGE(TAG, "header authentication failed (damaged or manipulated header)");
        return OPEN_BAD;                    // mk stays valid for the caller (disable)
    }
    return OPEN_OK;
}

// ---- wrong-attempt delay (RAM only, reset by a reboot - the KDF time still applies)

static uint32_t retry_after(void)
{
    const int64_t d = s_next_try_us - esp_timer_get_time();
    return d > 0 ? (uint32_t)((d + 999999) / 1000000) : 0;
}

static void note_failure(void)
{
    s_fails++;
    uint32_t wait = 0;
    if (s_fails > WAIT_FREE_FAILS) {
        const uint32_t sh = s_fails - WAIT_FREE_FAILS - 1;
        wait = sh >= 6 ? WAIT_MAX_S : WAIT_FIRST_S << sh;
        wait = wait > WAIT_MAX_S ? WAIT_MAX_S : wait;
    }
    s_next_try_us = esp_timer_get_time() + (int64_t)wait * 1000000;
    ESP_LOGW(TAG, "wrong password or recovery key (%lu in a row, next try in %lu s)", (unsigned long)s_fails,
             (unsigned long)wait);
}

static void note_success(void)
{
    s_fails = 0;
    s_next_try_us = 0;
}

static pd_crypt_err_t fail_auth(uint32_t *retry_after_s)
{
    note_failure();
    if (retry_after_s) {
        *retry_after_s = retry_after();
    }
    return PD_CRYPT_ERR_AUTH;
}

static bool must_wait(uint32_t *retry_after_s)
{
    const uint32_t w = retry_after();
    if (retry_after_s) {
        *retry_after_s = w;
    }
    return w > 0;
}

/** Check a secret against the current header (and the alternative copy). */
static pd_crypt_err_t open_current(const char *secret, uint8_t mk[64], bool *used_alt, bool accept_bad,
                                   uint32_t *retry_after_s)
{
    *used_alt = false;
    open_t r = hdr_open(&s_hdr, secret, mk);
    if (r != OPEN_OK && s_alt_valid) {
        uint8_t mk2[64];
        const open_t r2 = hdr_open(&s_alt, secret, mk2);   // interrupted header update: other copy
        if (r2 == OPEN_OK || (r == OPEN_AUTH && r2 == OPEN_BAD)) {
            memcpy(mk, mk2, 64);
            *used_alt = true;
            r = r2;
        }
        mbedtls_platform_zeroize(mk2, sizeof(mk2));
    }
    if (r == OPEN_AUTH) {
        return fail_auth(retry_after_s);
    }
    note_success();                         // the secret itself was right
    if (r == OPEN_BAD && !accept_bad) {
        mbedtls_platform_zeroize(mk, 64);
        return PD_CRYPT_ERR_DAMAGED;
    }
    return PD_CRYPT_OK;
}

// ---- key install / wipe (s_io held)

static esp_err_t key_install(const uint8_t mk[64])
{
    memcpy(s_key, mk, sizeof(s_key));
    if (!s_hw) {
        mbedtls_aes_xts_init(&s_xts_enc);
        mbedtls_aes_xts_init(&s_xts_dec);
        s_xts_ctx = true;
        if (mbedtls_aes_xts_setkey_enc(&s_xts_enc, s_key, 512) != 0 ||
            mbedtls_aes_xts_setkey_dec(&s_xts_dec, s_key, 512) != 0) {
            return ESP_FAIL;
        }
    }
    for (uint32_t n = SCRATCH_BYTES / SS; n >= 1 && !s_scratch; n /= 2) {
        s_scratch = heap_caps_malloc((size_t)n * SS, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        s_scratch_sectors = s_scratch ? n : 0;
    }
    if (!s_scratch) {
        return ESP_ERR_NO_MEM;
    }
    if (s_scratch_sectors < SCRATCH_BYTES / SS) {
        ESP_LOGW(TAG, "write buffer reduced to %lu sectors (low memory)", (unsigned long)s_scratch_sectors);
    }
    return ESP_OK;
}

static void key_wipe(void)
{
    mbedtls_platform_zeroize(s_key, sizeof(s_key));
    if (s_xts_ctx) {
        mbedtls_aes_xts_free(&s_xts_enc);     // zeroizes the contexts
        mbedtls_aes_xts_free(&s_xts_dec);
        s_xts_ctx = false;
    }
    if (s_scratch) {
        mbedtls_platform_zeroize(s_scratch, (size_t)s_scratch_sectors * SS);
        free(s_scratch);
        s_scratch = NULL;
        s_scratch_sectors = 0;
    }
#if CONFIG_PD_CRYPT_HW_DIRECT
    hw_clear_key();
#endif
}

static void on_shutdown(void)
{
    // esp_restart(): RAM is not cleared by a software reset. Wipe even if an I/O holds the lock
    // (the chip resets right after); nothing may use the key afterwards.
    const bool got = s_io && xSemaphoreTake(s_io, pdMS_TO_TICKS(200)) == pdTRUE;
    if (s_state == PD_CRYPT_UNLOCKED) {
        s_state = PD_CRYPT_LOCKED;
    }
    mbedtls_platform_zeroize(s_key, sizeof(s_key));
    mbedtls_platform_zeroize(&s_xts_enc, sizeof(s_xts_enc));
    mbedtls_platform_zeroize(&s_xts_dec, sizeof(s_xts_dec));
    if (s_scratch) {
        mbedtls_platform_zeroize(s_scratch, (size_t)s_scratch_sectors * SS);
    }
    if (got) {
        xSemaphoreGive(s_io);
    }
}

// ============================================================== public API

esp_err_t pd_crypt_attach(const pd_crypt_medium_t *medium)
{
    if (!s_io) {
        s_io = xSemaphoreCreateMutex();
        s_op = xSemaphoreCreateMutex();
        ESP_RETURN_ON_FALSE(s_io && s_op, ESP_ERR_NO_MEM, TAG, "mutex");   // state stays NONE
        esp_register_shutdown_handler(on_shutdown);
    }
    s_m = *medium;
#if CONFIG_PD_CRYPT_SELFTEST
    selftest(true);
#else
    selftest(false);
#endif
    uint32_t ds, dn;
    s_available = s_selftest_ok && s_m.sectors && layout(&ds, &dn);
    s_alt_valid = s_need_sync = false;
    if (!s_m.sectors) {
        set_state(PD_CRYPT_PLAIN);          // sector size != 512: can never carry our header
        ESP_LOGW(TAG, "medium sector size is not %d bytes - encryption not available", SS);
        return ESP_OK;
    }

    hdr_t *p = malloc(2 * sizeof(hdr_t));   // not on the stack: runs in the small main task
    if (!p) {
        set_state(PD_CRYPT_DAMAGED);        // fail closed
        return ESP_ERR_NO_MEM;
    }
    hdr_t *b = p + 1;
    // Both copies are read independently; a read error only invalidates that copy.
    xSemaphoreTake(s_io, portMAX_DELAY);
    const esp_err_t p_err = raw_read(0, s_sector_buf, 1);
    memcpy(p, s_sector_buf, SS);
    const bool sig55aa = s_sector_buf[510] == 0x55 && s_sector_buf[511] == 0xAA;
    const esp_err_t b_err = raw_read(s_m.sectors - 1, s_sector_buf, 1);
    memcpy(b, s_sector_buf, SS);
    xSemaphoreGive(s_io);
    const bool p_magic = p_err == ESP_OK && hdr_magic(p), b_magic = b_err == ESP_OK && hdr_magic(b);
    const bool p_ok = p_magic && hdr_valid(p), b_ok = b_magic && hdr_valid(b);
    // Sector 0 is an MBR / boot sector (not our header): the medium was formatted elsewhere.
    const bool foreign = p_err == ESP_OK && !p_magic && sig55aa;
    pd_crypt_state_t st;
    const char *why;
    if (p_ok && b_ok) {
        const bool b_newer = b->generation > p->generation;
        s_hdr = b_newer ? *b : *p;
        s_alt = b_newer ? *p : *b;
        s_alt_valid = s_need_sync = memcmp(p, b, SS) != 0;
        st = PD_CRYPT_LOCKED, why = s_need_sync ? "both copies, they differ" : "both copies";
    } else if (p_ok) {
        s_hdr = *p;
        s_need_sync = true;
        st = PD_CRYPT_LOCKED, why = "primary only";
    } else if (b_ok && !foreign) {
        s_hdr = *b;                         // primary lost or unreadable (e.g. power fail)
        s_need_sync = true;
        st = PD_CRYPT_LOCKED, why = p_err == ESP_OK ? "backup only" : "backup only, sector 0 unreadable";
    } else if (foreign) {
        st = PD_CRYPT_PLAIN, why = b_magic ? "stale backup header ignored (medium re-formatted)" : "";
    } else if (p_magic || b_magic) {
        st = PD_CRYPT_DAMAGED, why = "header present, but no copy is valid";
    } else if (p_err != ESP_OK) {
        st = PD_CRYPT_DAMAGED, why = "sector 0 unreadable";
    } else {
        st = PD_CRYPT_PLAIN, why = "";
    }
    mbedtls_platform_zeroize(p, 2 * sizeof(hdr_t));
    free(p);
    xSemaphoreTake(s_io, portMAX_DELAY);
    set_state(st);
    xSemaphoreGive(s_io);
    if (st == PD_CRYPT_LOCKED) {
        ESP_LOGI(TAG, "medium is encrypted (header generation %llu, %s) -> locked",
                 (unsigned long long)s_hdr.generation, why);
    } else if (st == PD_CRYPT_DAMAGED) {
        ESP_LOGE(TAG, "encryption header damaged (%s; reads: primary %s, backup %s) - drive blocked", why,
                 esp_err_to_name(p_err), esp_err_to_name(b_err));
    } else {
        ESP_LOGI(TAG, "medium is not encrypted%s%s%s", s_available ? "" : " (encryption not available)",
                 why[0] ? ": " : "", why);
    }
    return ESP_OK;
}

pd_crypt_state_t pd_crypt_state(void)
{
    return s_state;
}

bool pd_crypt_is_encrypted(void)
{
    const pd_crypt_state_t st = s_state;
    return st == PD_CRYPT_LOCKED || st == PD_CRYPT_UNLOCKED || st == PD_CRYPT_DAMAGED;
}

uint32_t pd_crypt_volume_sectors(void)
{
    return s_info_volume;
}

void pd_crypt_get_info(pd_crypt_info_t *out)
{
    // Lock-free: setup holds the locks for minutes while erasing.
    memset(out, 0, sizeof(*out));
    out->state = s_state;
    out->available = s_available;
    out->hw_fast_path = s_hw;
    out->volume_sectors = s_info_volume;
    out->data_start = s_info_data_start;
    out->kdf_iterations = s_info_iter;
    out->generation = s_info_gen;
    out->failed_attempts = s_fails;
    out->retry_after_s = retry_after();
}

esp_err_t pd_crypt_read(uint32_t sector, void *buf, uint32_t count)
{
    if (count == 0) {
        return ESP_OK;
    }
    esp_err_t err;
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (s_state != PD_CRYPT_UNLOCKED) {
        err = ESP_ERR_INVALID_STATE;
    } else if ((uint64_t)sector + count > s_hdr.data_sectors) {
        err = ESP_ERR_INVALID_SIZE;
    } else {
        err = raw_read(s_hdr.data_start + sector, buf, count);
        if (err == ESP_OK) {
            err = xts_volume(false, sector, buf, count);
        }
    }
    xSemaphoreGive(s_io);
    return err;
}

esp_err_t pd_crypt_write(uint32_t sector, const void *buf, uint32_t count)
{
    if (count == 0) {
        return ESP_OK;
    }
    esp_err_t err = ESP_OK;
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (s_state != PD_CRYPT_UNLOCKED || !s_scratch) {
        err = ESP_ERR_INVALID_STATE;
    } else if ((uint64_t)sector + count > s_hdr.data_sectors) {
        err = ESP_ERR_INVALID_SIZE;
    } else {
        const uint8_t *src = buf;
        while (count && err == ESP_OK) {
            const uint32_t n = count < s_scratch_sectors ? count : s_scratch_sectors;
            memcpy(s_scratch, src, (size_t)n * SS);
            err = xts_volume(true, sector, s_scratch, n);
            if (err == ESP_OK) {
                err = raw_write(s_hdr.data_start + sector, s_scratch, n);
            }
            src += (size_t)n * SS;
            sector += n;
            count -= n;
        }
    }
    xSemaphoreGive(s_io);
    return err;
}

/** TRIM of volume sectors (FatFs CTRL_TRIM): erased at their physical place. */
static esp_err_t volume_trim(uint32_t sector, uint32_t count)
{
    esp_err_t err;
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (s_state != PD_CRYPT_UNLOCKED || !s_m.erase) {
        err = ESP_ERR_INVALID_STATE;
    } else if (count == 0 || (uint64_t)sector + count > s_hdr.data_sectors) {
        err = ESP_ERR_INVALID_SIZE;
    } else {
        err = s_m.erase(s_m.ctx, s_hdr.data_start + sector, count);
    }
    xSemaphoreGive(s_io);
    return err;
}

pd_crypt_err_t pd_crypt_check_password(const char *pw)
{
    if (!pw) {
        return PD_CRYPT_ERR_WEAK;
    }
    const size_t len = strnlen(pw, PD_CRYPT_PW_MAX_BYTES + 1);
    size_t chars = 0;
    for (size_t i = 0; i < len; i++) {
        chars += ((uint8_t)pw[i] & 0xC0) != 0x80;   // count UTF-8 lead bytes = characters
    }
    return (len > PD_CRYPT_PW_MAX_BYTES || chars < PD_CRYPT_PW_MIN_CHARS) ? PD_CRYPT_ERR_WEAK : PD_CRYPT_OK;
}

pd_crypt_err_t pd_crypt_setup(const char *password, char *recovery_out, size_t recovery_len)
{
    if (!recovery_out || recovery_len < PD_CRYPT_RECOVERY_LEN) {
        return PD_CRYPT_ERR_NO_MEM;
    }
    recovery_out[0] = '\0';
    if (pd_crypt_check_password(password) != PD_CRYPT_OK) {
        return PD_CRYPT_ERR_WEAK;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    pd_crypt_err_t res = PD_CRYPT_OK;
    hdr_t *h = NULL;
    uint8_t mk[64], rec[REC_BYTES];
    uint32_t ds = 0, dn = 0;
    if (s_state != PD_CRYPT_PLAIN) {
        res = PD_CRYPT_ERR_STATE;
        goto out;
    }
    if (!s_available || !layout(&ds, &dn)) {
        res = PD_CRYPT_ERR_UNSUPPORTED;
        goto out;
    }
    h = calloc(1, sizeof(hdr_t));
    if (!h) {
        res = PD_CRYPT_ERR_NO_MEM;
        goto out;
    }
    const uint32_t iter = kdf_iterations();
    esp_fill_random(mk, sizeof(mk));
    esp_fill_random(rec, sizeof(rec));
    memcpy(h->magic, MAGIC, 8);
    h->version = HDR_VERSION;
    h->sector_size = SS;
    h->generation = 1;
    h->phys_sectors = s_m.sectors;
    h->data_start = ds;
    h->data_sectors = dn;
    h->backup_sector = s_m.sectors - 1;
    mk_check(mk, h->mk_check);
    int64_t t0 = esp_timer_get_time();
    if (!slot_make(&h->slot[SLOT_PW], SLOT_PW, (const uint8_t *)password, strlen(password), iter, mk) ||
        !slot_make(&h->slot[SLOT_REC], SLOT_REC, rec, REC_BYTES, REC_ITER, mk)) {
        res = PD_CRYPT_ERR_NO_MEM;
        goto out;
    }
    hdr_finish(h, mk);
    ESP_LOGI(TAG, "setup: key slots derived in %lld ms", (esp_timer_get_time() - t0) / 1000);

    // Old (unencrypted) content must not survive: erase everything except the header sectors.
    t0 = esp_timer_get_time();
    esp_err_t err = wipe_range(1, s_m.sectors - 1);
    ESP_LOGI(TAG, "setup: medium erased in %lld s (%s)", (esp_timer_get_time() - t0) / 1000000, esp_err_to_name(err));
    if (err != ESP_OK) {
        res = err == ESP_ERR_NOT_SUPPORTED ? PD_CRYPT_ERR_UNSUPPORTED
            : err == ESP_ERR_NO_MEM ? PD_CRYPT_ERR_NO_MEM : PD_CRYPT_ERR_IO;
        goto out;
    }
    xSemaphoreTake(s_io, portMAX_DELAY);
    err = hdr_write_both(h);
    if (err == ESP_OK) {
        s_hdr = *h;
        s_alt_valid = s_need_sync = false;
        err = key_install(mk);
        if (err == ESP_OK) {
            set_state(PD_CRYPT_UNLOCKED);
        } else {
            key_wipe();
            set_state(PD_CRYPT_LOCKED);     // header is written: the drive is encrypted, but locked
        }
    }
    xSemaphoreGive(s_io);
    if (err != ESP_OK) {
        res = err == ESP_ERR_NO_MEM ? PD_CRYPT_ERR_NO_MEM : PD_CRYPT_ERR_IO;
        if (s_state != PD_CRYPT_LOCKED) {
            goto out;
        }
    }
    rec_encode(rec, recovery_out);          // also when locked after a memory failure
    note_success();
    ESP_LOGW(TAG, "drive encryption set up: %lu sectors at %lu, %lu KDF iterations",
             (unsigned long)dn, (unsigned long)ds, (unsigned long)iter);
out:
    mbedtls_platform_zeroize(mk, sizeof(mk));
    mbedtls_platform_zeroize(rec, sizeof(rec));
    if (h) {
        mbedtls_platform_zeroize(h, sizeof(*h));
        free(h);
    }
    xSemaphoreGive(s_op);
    return res;
}

pd_crypt_err_t pd_crypt_unlock(const char *secret, uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (!secret) {
        return PD_CRYPT_ERR_AUTH;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    pd_crypt_err_t res = PD_CRYPT_OK;
    uint8_t mk[64];
    bool use_alt = false;
    if (s_state == PD_CRYPT_UNLOCKED) {
        goto out;
    }
    if (s_state != PD_CRYPT_LOCKED) {
        res = s_state == PD_CRYPT_DAMAGED ? PD_CRYPT_ERR_DAMAGED : PD_CRYPT_ERR_STATE;
        goto out;
    }
    if (!s_selftest_ok) {
        res = PD_CRYPT_ERR_UNSUPPORTED;
        goto out;
    }
    if (must_wait(retry_after_s)) {
        res = PD_CRYPT_ERR_WAIT;
        goto out;
    }
    const int64_t t0 = esp_timer_get_time();
    res = open_current(secret, mk, &use_alt, false, retry_after_s);
    if (res != PD_CRYPT_OK) {
        goto out;
    }
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (use_alt) {
        s_hdr = s_alt;
    }
    if (s_need_sync || use_alt) {
        // Make both copies identical again (best effort; failures only cost redundancy).
        if (hdr_write_both(&s_hdr) == ESP_OK) {
            s_need_sync = s_alt_valid = false;
            ESP_LOGI(TAG, "header copies synchronised");
        }
    }
    const esp_err_t err = key_install(mk);
    if (err == ESP_OK) {
        set_state(PD_CRYPT_UNLOCKED);
    } else {
        key_wipe();
        set_state(PD_CRYPT_LOCKED);
        res = PD_CRYPT_ERR_NO_MEM;
    }
    xSemaphoreGive(s_io);
    if (res == PD_CRYPT_OK) {
        ESP_LOGI(TAG, "unlocked (%lld ms)", (esp_timer_get_time() - t0) / 1000);
    }
out:
    mbedtls_platform_zeroize(mk, sizeof(mk));
    xSemaphoreGive(s_op);
    return res;
}

void pd_crypt_lock(void)
{
    if (!s_op) {
        return;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (s_state == PD_CRYPT_UNLOCKED) {
        key_wipe();
        set_state(PD_CRYPT_LOCKED);
        ESP_LOGI(TAG, "locked, keys wiped");
    }
    xSemaphoreGive(s_io);
    xSemaphoreGive(s_op);
}

/** Common part of change_password / new_recovery_key: verify, re-wrap one slot, write. */
static pd_crypt_err_t rewrap_slot(int index, const char *secret, const uint8_t *new_secret, size_t new_len,
                                  uint32_t iter, uint32_t *retry_after_s)
{
    pd_crypt_err_t res;
    uint8_t mk[64];
    hdr_t *h = NULL;
    bool alt = false;
    if (s_state != PD_CRYPT_UNLOCKED) {
        return PD_CRYPT_ERR_STATE;
    }
    if (must_wait(retry_after_s)) {
        return PD_CRYPT_ERR_WAIT;
    }
    res = open_current(secret, mk, &alt, false, retry_after_s);
    if (res != PD_CRYPT_OK) {
        goto out;
    }
    if (!ct_equal(mk, s_key, sizeof(mk))) {   // s_key only changes under s_op, which we hold
        ESP_LOGE(TAG, "header key differs from the key in use");
        res = PD_CRYPT_ERR_DAMAGED;
        goto out;
    }
    h = malloc(sizeof(hdr_t));
    if (!h) {
        res = PD_CRYPT_ERR_NO_MEM;
        goto out;
    }
    *h = alt ? s_alt : s_hdr;
    if (!slot_make(&h->slot[index], index, new_secret, new_len, iter, mk)) {
        res = PD_CRYPT_ERR_NO_MEM;
        goto out;
    }
    uint64_t gen = s_hdr.generation;
    if (s_alt_valid && s_alt.generation > gen) {
        gen = s_alt.generation;
    }
    h->generation = gen + 1;
    hdr_finish(h, mk);
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (hdr_write_both(h) == ESP_OK) {
        s_hdr = *h;
        s_alt_valid = s_need_sync = false;
        set_state(PD_CRYPT_UNLOCKED);       // refresh info (iterations, generation)
        ESP_LOGI(TAG, "%s changed (header generation %llu)", index == SLOT_PW ? "password" : "recovery key",
                 (unsigned long long)h->generation);
    } else {
        res = PD_CRYPT_ERR_IO;
        // The backup may already hold the new slot: keep it as alternative for unlock.
        s_alt = *h;
        s_alt_valid = s_need_sync = true;
    }
    xSemaphoreGive(s_io);
out:
    mbedtls_platform_zeroize(mk, sizeof(mk));
    if (h) {
        mbedtls_platform_zeroize(h, sizeof(*h));
        free(h);
    }
    return res;
}

pd_crypt_err_t pd_crypt_change_password(const char *old_secret, const char *new_password, uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (pd_crypt_check_password(new_password) != PD_CRYPT_OK) {
        return PD_CRYPT_ERR_WEAK;
    }
    if (!old_secret) {
        return PD_CRYPT_ERR_AUTH;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    const pd_crypt_err_t res = rewrap_slot(SLOT_PW, old_secret, (const uint8_t *)new_password,
                                           strlen(new_password), kdf_iterations(), retry_after_s);
    xSemaphoreGive(s_op);
    return res;
}

pd_crypt_err_t pd_crypt_new_recovery_key(const char *secret, char *recovery_out, size_t recovery_len,
                                         uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (!recovery_out || recovery_len < PD_CRYPT_RECOVERY_LEN) {
        return PD_CRYPT_ERR_NO_MEM;
    }
    recovery_out[0] = '\0';
    if (!secret) {
        return PD_CRYPT_ERR_AUTH;
    }
    uint8_t rec[REC_BYTES];
    esp_fill_random(rec, sizeof(rec));
    xSemaphoreTake(s_op, portMAX_DELAY);
    const pd_crypt_err_t res = rewrap_slot(SLOT_REC, secret, rec, REC_BYTES, REC_ITER, retry_after_s);
    xSemaphoreGive(s_op);
    if (res == PD_CRYPT_OK) {
        rec_encode(rec, recovery_out);
    }
    mbedtls_platform_zeroize(rec, sizeof(rec));
    return res;
}

pd_crypt_err_t pd_crypt_disable(const char *secret, uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (!secret) {
        return PD_CRYPT_ERR_AUTH;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    pd_crypt_err_t res = PD_CRYPT_OK;
    uint8_t mk[64];
    bool alt;
    if (s_state != PD_CRYPT_LOCKED && s_state != PD_CRYPT_UNLOCKED) {
        res = s_state == PD_CRYPT_DAMAGED ? PD_CRYPT_ERR_DAMAGED : PD_CRYPT_ERR_STATE;
        goto out;
    }
    if (must_wait(retry_after_s)) {
        res = PD_CRYPT_ERR_WAIT;
        goto out;
    }
    // A correct secret is enough here even if the header MAC fails: the data is thrown away.
    res = open_current(secret, mk, &alt, true, retry_after_s);
    if (res != PD_CRYPT_OK) {
        goto out;
    }
    xSemaphoreTake(s_io, portMAX_DELAY);
    if (hdr_erase_both() == ESP_OK) {
        key_wipe();
        memset(&s_hdr, 0, sizeof(s_hdr));
        memset(&s_alt, 0, sizeof(s_alt));
        s_alt_valid = s_need_sync = false;
        set_state(PD_CRYPT_PLAIN);
        ESP_LOGW(TAG, "drive encryption removed (header erased)");
    } else {
        res = PD_CRYPT_ERR_IO;
    }
    xSemaphoreGive(s_io);
out:
    mbedtls_platform_zeroize(mk, sizeof(mk));
    xSemaphoreGive(s_op);
    return res;
}

esp_err_t pd_crypt_force_plain(void)
{
    if (!s_op) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_op, portMAX_DELAY);
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (s_state == PD_CRYPT_LOCKED || s_state == PD_CRYPT_DAMAGED) {
        xSemaphoreTake(s_io, portMAX_DELAY);
        key_wipe();
        err = hdr_erase_both();
        memset(&s_hdr, 0, sizeof(s_hdr));
        memset(&s_alt, 0, sizeof(s_alt));
        s_alt_valid = s_need_sync = false;
        set_state(PD_CRYPT_PLAIN);          // even after a write error: the caller formats (or fails)
        xSemaphoreGive(s_io);
        ESP_LOGW(TAG, "encryption header force-erased (%s) - all data lost", esp_err_to_name(err));
    }
    xSemaphoreGive(s_op);
    return err;
}

// ============================================================== FatFs disk driver

static DSTATUS dio_status(unsigned char pdrv)
{
    (void)pdrv;
    return s_state == PD_CRYPT_UNLOCKED ? 0 : STA_NOINIT;
}

static DRESULT dio_read(unsigned char pdrv, unsigned char *buff, uint32_t sector, unsigned count)
{
    (void)pdrv;
    const esp_err_t err = pd_crypt_read(sector, buff, count);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "volume read %lu+%u failed (%s)", (unsigned long)sector, count, esp_err_to_name(err));
    }
    return err == ESP_OK ? RES_OK : RES_ERROR;
}

static DRESULT dio_write(unsigned char pdrv, const unsigned char *buff, uint32_t sector, unsigned count)
{
    (void)pdrv;
    const esp_err_t err = pd_crypt_write(sector, buff, count);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "volume write %lu+%u failed (%s)", (unsigned long)sector, count, esp_err_to_name(err));
    }
    return err == ESP_OK ? RES_OK : RES_ERROR;
}

static DRESULT dio_ioctl(unsigned char pdrv, unsigned char cmd, void *buff)
{
    (void)pdrv;
    switch (cmd) {
    case CTRL_SYNC:
        return RES_OK;
    case GET_SECTOR_COUNT:
        *(DWORD *)buff = pd_crypt_volume_sectors();
        return RES_OK;
    case GET_SECTOR_SIZE:
        *(WORD *)buff = SS;
        return RES_OK;
    case CTRL_TRIM: {
        // Only issued by FatFs with CONFIG_FATFS_USE_TRIM (start, end inclusive, volume sectors).
        const LBA_t *r = buff;
        if (r[1] < r[0]) {
            return RES_PARERR;
        }
        return volume_trim((uint32_t)r[0], (uint32_t)(r[1] - r[0] + 1)) == ESP_OK ? RES_OK : RES_ERROR;
    }
    default:
        return RES_PARERR;
    }
}

void pd_crypt_diskio_register(uint8_t pdrv)
{
    static const ff_diskio_impl_t impl = {
        .init = dio_status,
        .status = dio_status,
        .read = dio_read,
        .write = dio_write,
        .ioctl = dio_ioctl,
    };
    ff_diskio_register(pdrv, &impl);
}
