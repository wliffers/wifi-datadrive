// Storage ownership manager - see drive.h for the concept.
//
// Safety mechanisms (in addition to esp_tinyusb's own mount/unmount logic):
//  1. auto_mount_off: esp_tinyusb never switches ownership on its own (its automatic
//     mode would unmount the FAT under an open upload file when a host connects).
//  2. Host I/O gate: READ(10)/WRITE(10)/TEST UNIT READY are wrapped (linker --wrap,
//     see CMakeLists.txt). While the app owns the medium, every host access is
//     refused with "medium not present" - even from hosts that ignore TEST UNIT READY.
//  3. Barrier: before the app mounts the FAT, all host writes already accepted by
//     TinyUSB (deferred into the TinyUSB task) are drained.
//  4. Media change: after giving the medium back to the host, the first TEST UNIT
//     READY reports UNIT ATTENTION "medium may have changed" so the host drops its
//     cached FAT; optionally the device additionally re-enumerates (soft re-plug).
//  5. Uploads go to a temp file and are renamed only after size + content checks.
//
// Encryption (components/pd_crypt) sits below all of this, directly above the medium: FatFs
// gets pd_crypt's disk driver and the USB host's sectors go through pd_crypt (hook in usb_dev.c,
// capacity in __wrap_tud_msc_capacity_cb below). The ownership logic is unchanged; "locked" is
// an extra state in which the app side has no file system and the host gate stays closed.
// Lock/unlock/setup/disable run under s_lock like an owner change.
//
// Folder operations (list, mkdir, delete, move, copy) call FatFs directly instead of going
// through the VFS: the VFS silently truncates paths longer than 255 bytes (fixed buffer in
// vfs_fat.c), and only FatFs can tell whether two paths name the same folder (case folding,
// short-name aliases). Uploads and downloads keep using the VFS (file descriptors).
#include "drive.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <utime.h>
#include "board.h"
#include "diskio_impl.h"
#include "diskio_wl.h"
#include "diskio_sdmmc.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "ff.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "pd_crypt.h"
#include "sdkconfig.h"
#include "tinyusb.h"
#include "tinyusb_msc.h"
#include "usb_dev.h"
#include "wear_levelling.h"

static const char *TAG = "drive";
static const char *NVS_NS = "pd_drive";

#define TMP_PATH           DRIVE_BASE_PATH "/.pd_upload.tmp"
#define OLD_PATH           DRIVE_BASE_PATH "/.pd_old.tmp"     // previous version during overwrite
#define FREE_RESERVE_BYTES (64 * 1024)      // keep room for FAT/dir growth
#define MTIME_MIN          347155200        // 1981-01-01: older FAT stamps mean "clock was not set"
#define TAIL_SIZE          1024             // bytes kept for the ZIP end-of-central-directory check
// Cluster size (Kconfig). 4096: every FatFs write covers whole 4 KiB flash sectors (fast, but
// FAT12 on this ~11 MB volume). 2048: FAT16, but each cluster write is a slow partial sector.
#define FAT_ALLOC_UNIT     (CONFIG_PD_FAT_CLUSTER_KB * 1024)
_Static_assert(CONFIG_PD_FAT_CLUSTER_KB == 2 || CONFIG_PD_FAT_CLUSTER_KB == 4, "PD_FAT_CLUSTER_KB must be 2 or 4");
#define FLASH_SECTOR_SECTORS 8             // 4096 B flash erase sector / 512 B FAT sector
#define SD_ALIGN_SECTORS   8192             // 4 MiB data-area alignment, like the SD formatter
#define FMT_REQ_FLASH      1                // NVS fmt_req values: which medium to format
#define FMT_REQ_SD         2

// Folder operations
#define VFS_REL_MAX        254              // longest relative path the VFS passes on untruncated
#define FAT_REL_OFF        3                // FatFs paths are "N:/" + relative path
#define TREE_DEPTH_MAX     16               // recursion levels of delete/copy (deeper -> DRIVE_ERR_IO)
#define TREE_PATH_SIZE     (FAT_REL_OFF + DRIVE_SRC_PATH_MAX + TREE_DEPTH_MAX * (DRIVE_FILE_NAME_MAX + 1) + 1)
#define DEL_BATCH_SIZE     2048             // names collected per round before deleting them
#define COPY_BUF_SIZE      8192
#define YIELD_INTERVAL_US  100000           // long operations let the idle tasks run (task watchdog)
#define STATUS_WAIT_MS     100              // drive_get_status(): retry interval while the lock is busy

// Boolean Kconfig options are undefined (not 0) when disabled.
#ifdef CONFIG_PD_PROVIDE_ON_ATTACH
#define PROVIDE_ON_ATTACH true
#else
#define PROVIDE_ON_ATTACH false
#endif
#ifdef CONFIG_PD_EJECT_TO_APP
#define EJECT_TO_APP true
#else
#define EJECT_TO_APP false
#endif
#ifdef CONFIG_PD_REPLUG_ON_PROVIDE
#define REPLUG_ON_PROVIDE true
#else
#define REPLUG_ON_PROVIDE false
#endif
#ifdef CONFIG_PD_HOST_WRITABLE_DEFAULT
#define HOST_WRITABLE_DEFAULT 1
#else
#define HOST_WRITABLE_DEFAULT 0
#endif

typedef enum {
    EV_USB_ATTACHED,
    EV_USB_DETACHED,
    EV_HOST_EJECT,
} drive_event_t;

static SemaphoreHandle_t s_lock;            // guards all state below + file system access
static QueueHandle_t s_events;
static wl_handle_t s_wl = WL_INVALID_HANDLE;
static sdmmc_card_t *s_card;              // non-NULL when the microSD card is the medium
static tinyusb_msc_storage_handle_t s_storage;
static drive_status_t s_st;
static int s_open_downloads;
static int64_t s_replug_until_ms;           // ignore DETACHED caused by our own re-plug
static bool s_crypt_failed;                 // pd_crypt could not even be attached: blocked like DAMAGED

// Status snapshot for drive_get_status() while a long folder operation holds s_lock
// (copy, recursive delete). Ownership cannot change during such an operation.
static volatile bool s_long_op;
static drive_status_t s_snap;
static portMUX_TYPE s_snap_mux = portMUX_INITIALIZER_UNLOCKED;

// Upload session (only one at a time)
static int s_up_fd = -1;
static bool s_up_overwrite;
static char s_up_path[DRIVE_PATH_MAX + 1];  // target path relative to the root
static uint8_t s_up_head[4];
static uint8_t s_up_tail[TAIL_SIZE];

// Host I/O gate - read from the TinyUSB task, written under s_lock.
static volatile bool s_host_io_enabled;
static volatile bool s_host_write_enabled;
static volatile bool s_media_changed;
static volatile bool s_host_ejected;
static volatile uint32_t s_last_host_read_ms;
static volatile uint32_t s_last_host_write_ms;
static volatile bool s_host_read_seen, s_host_write_seen;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/** Milliseconds since *last (UINT32_MAX if never). Reads *last before the clock so a
 *  concurrent host access can never make the result wrap to a huge value. */
static uint32_t idle_ms(bool seen, const volatile uint32_t *last)
{
    if (!seen) {
        return UINT32_MAX;
    }
    const uint32_t l = *last;
    const int32_t d = (int32_t)(now_ms() - l);
    return d < 0 ? 0 : (uint32_t)d;
}

/** DRIVE_OK, or why neither side may use the medium (locked / damaged encryption header). */
static drive_result_t crypt_blocked(void)
{
    switch (pd_crypt_state()) {
    case PD_CRYPT_PLAIN:
    case PD_CRYPT_UNLOCKED: return s_crypt_failed ? DRIVE_ERR_CRYPT_DAMAGED : DRIVE_OK;
    case PD_CRYPT_LOCKED:   return DRIVE_ERR_LOCKED;
    default:                return DRIVE_ERR_CRYPT_DAMAGED;   // DAMAGED, or attach failed (NONE)
    }
}

static drive_crypt_state_t crypt_state_public(void)
{
    if (s_crypt_failed) {
        return DRIVE_CRYPT_DAMAGED;
    }
    switch (pd_crypt_state()) {
    case PD_CRYPT_LOCKED:   return DRIVE_CRYPT_LOCKED;
    case PD_CRYPT_UNLOCKED: return DRIVE_CRYPT_UNLOCKED;
    case PD_CRYPT_PLAIN:    return DRIVE_CRYPT_OFF;
    default:                return DRIVE_CRYPT_DAMAGED;
    }
}

static void lock(void)   { xSemaphoreTake(s_lock, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_lock); }

/** Like lock(), but gives up (false) as soon as a long folder operation (copy, recursive
 *  delete) is running, so the caller (UI task, button) is not frozen. The owner cannot change
 *  during such an operation anyway. */
static bool lock_unless_long_op(void)
{
    if (s_long_op) {
        return false;
    }
    while (xSemaphoreTake(s_lock, pdMS_TO_TICKS(STATUS_WAIT_MS)) != pdTRUE) {
        if (s_long_op) {
            return false;
        }
    }
    return true;
}

const char *drive_result_str(drive_result_t r)
{
    // Shown to end users in the web UI: plain language, no internal terms.
    switch (r) {
    case DRIVE_OK:               return "OK";
    case DRIVE_ERR_BUSY:         return "Es läuft gerade eine Dateiübertragung. Bitte warten, bis sie abgeschlossen ist.";
    case DRIVE_ERR_HOST_ACTIVE:  return "Das USB-Gerät verwendet das Laufwerk noch. Bitte dort zuerst auswerfen oder das Trennen bestätigen.";
    case DRIVE_ERR_HOST_WRITING: return "Das USB-Gerät schreibt gerade auf das Laufwerk. Bitte warten, bis es fertig ist.";
    case DRIVE_ERR_WRONG_OWNER:  return "Das geht nur, solange die Dateiverwaltung aktiv ist.";
    case DRIVE_ERR_NO_FS:        return "Der Speicher ist nicht lesbar und muss formatiert werden.";
    case DRIVE_ERR_BAD_NAME:     return "Ungültiger Dateiname.";
    case DRIVE_ERR_BAD_TYPE:     return "Dieser Dateityp ist nicht erlaubt.";
    case DRIVE_ERR_EXISTS:       return "Eine Datei mit diesem Namen gibt es bereits.";
    case DRIVE_ERR_NOT_FOUND:    return "Die Datei wurde nicht gefunden.";
    case DRIVE_ERR_NO_SPACE:     return "Nicht genug freier Speicher.";
    case DRIVE_ERR_INVALID_FILE: return "Die Datei ist unvollständig oder beschädigt (keine gültige 3MF-Datei).";
    case DRIVE_ERR_INCOMPLETE:   return "Die Übertragung war unvollständig.";
    case DRIVE_ERR_TOO_LARGE:    return "Die Datei ist zu groß – möglich sind höchstens 4 GB pro Datei.";
    case DRIVE_ERR_IO:           return "Lese- oder Schreibfehler auf dem Speicher.";
    case DRIVE_ERR_DIR_NOT_EMPTY: return "Der Ordner ist nicht leer.";
    case DRIVE_ERR_IS_DIR:       return "Das ist ein Ordner, keine Datei.";
    case DRIVE_ERR_LOCKED:       return "Das Laufwerk ist verschlüsselt und gesperrt. Bitte zuerst mit dem Laufwerks-Passwort entsperren.";
    case DRIVE_ERR_AUTH:         return "Falsches Laufwerks-Passwort oder falscher Wiederherstellungsschlüssel.";
    case DRIVE_ERR_RETRY_LATER:  return "Zu viele Fehlversuche. Bitte etwas warten und es dann erneut versuchen.";
    case DRIVE_ERR_WEAK_PASSWORD: return "Das Laufwerks-Passwort muss mindestens 12 Zeichen lang sein.";
    case DRIVE_ERR_NOT_ENCRYPTED: return "Das Laufwerk ist nicht verschlüsselt.";
    case DRIVE_ERR_ALREADY_ENCRYPTED: return "Das Laufwerk ist bereits verschlüsselt.";
    case DRIVE_ERR_CRYPT_UNAVAILABLE: return "Verschlüsselung ist mit diesem Speicher nicht möglich (Selbsttest fehlgeschlagen, Speicher zu klein oder nicht löschbar).";
    case DRIVE_ERR_CRYPT_DAMAGED: return "Der Schlüsselbereich des verschlüsselten Laufwerks ist beschädigt oder nicht lesbar. Die Daten sind nicht zugänglich; das Laufwerk kann nur gelöscht und neu formatiert werden.";
    default:                     return "Unbekannter Fehler.";
    }
}

// ============================================================== TinyUSB MSC hooks
// tud_msc_* callbacks below are either wrapped (--wrap, real implementation in
// esp_tinyusb) or override TinyUSB weak defaults that esp_tinyusb does not define.

int32_t __real_tud_msc_read10_cb(uint8_t lun, uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize);
int32_t __real_tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize);
bool __real_tud_msc_test_unit_ready_cb(uint8_t lun);

int32_t __wrap_tud_msc_read10_cb(uint8_t lun, uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize)
{
    if (!s_host_io_enabled) {
        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3A, 0x00);   // medium not present
        return -1;
    }
    s_last_host_read_ms = now_ms();
    s_host_read_seen = true;
    return __real_tud_msc_read10_cb(lun, lba, offset, buffer, bufsize);
}

int32_t __wrap_tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize)
{
    if (!s_host_io_enabled) {
        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3A, 0x00);
        return -1;
    }
    if (!s_host_write_enabled) {
        tud_msc_set_sense(lun, SCSI_SENSE_DATA_PROTECT, 0x27, 0x00);   // write protected
        return -1;
    }
    s_last_host_write_ms = now_ms();
    s_host_write_seen = true;
    return __real_tud_msc_write10_cb(lun, lba, offset, buffer, bufsize);
}

bool __wrap_tud_msc_test_unit_ready_cb(uint8_t lun)
{
    if (!s_host_io_enabled) {
        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3A, 0x00);
        return false;
    }
    if (s_media_changed) {
        s_media_changed = false;
        // UNIT ATTENTION: "not ready to ready change, medium may have changed"
        tud_msc_set_sense(lun, SCSI_SENSE_UNIT_ATTENTION, 0x28, 0x00);
        return false;
    }
    return __real_tud_msc_test_unit_ready_cb(lun);
}

void __real_tud_msc_capacity_cb(uint8_t lun, uint32_t *block_count, uint16_t *block_size);

// esp_tinyusb reports the size it read once at start-up. An encrypted volume is smaller (header
// area) and its size changes with setup/disable; while locked 0 means "no medium" to TinyUSB.
void __wrap_tud_msc_capacity_cb(uint8_t lun, uint32_t *block_count, uint16_t *block_size)
{
    __real_tud_msc_capacity_cb(lun, block_count, block_size);
    if (pd_crypt_is_encrypted()) {
        *block_count = pd_crypt_volume_sectors();
    }
}

// Overrides TinyUSB weak default (not defined by esp_tinyusb).
bool tud_msc_is_writable_cb(uint8_t lun)
{
    (void)lun;
    return s_host_write_enabled;
}

// Overrides TinyUSB weak default: own SCSI INQUIRY identification.
uint32_t tud_msc_inquiry2_cb(uint8_t lun, scsi_inquiry_resp_t *resp, uint32_t bufsize)
{
    (void)lun;
    if (bufsize < sizeof(*resp)) {
        return 0;
    }
    memcpy(resp->vendor_id, "DataDrv ", 8);
    memcpy(resp->product_id, "WiFi DataDrive  ", 16);
    memcpy(resp->product_rev, "0.3 ", 4);
    return sizeof(*resp);
}

// Overrides TinyUSB weak default; called after every non READ/WRITE SCSI command.
void tud_msc_scsi_complete_cb(uint8_t lun, uint8_t const scsi_cmd[16])
{
    (void)lun;
    // START STOP UNIT (0x1B): byte 4 bit1 = LOEJ, bit0 = START. LOEJ && !START = eject.
    if (scsi_cmd[0] == 0x1B && (scsi_cmd[4] & 0x02) && !(scsi_cmd[4] & 0x01)) {
        if (!s_host_ejected) {
            s_host_ejected = true;
            const drive_event_t ev = EV_HOST_EJECT;
            xQueueSend(s_events, &ev, 0);
        }
    }
}

// ============================================================== helpers (call with s_lock held)

static void set_host_io(bool enabled)
{
    s_host_write_enabled = enabled && s_st.host_writable;
    s_host_io_enabled = enabled;
}

static void refresh_fs_info(void)
{
    if (s_st.owner != DRIVE_OWNER_APP || !s_st.fs_ok) {
        return;
    }
    uint64_t total = 0, free_b = 0;
    if (esp_vfs_fat_info(DRIVE_BASE_PATH, &total, &free_b) == ESP_OK) {
        s_st.total_bytes = total;
        s_st.free_bytes = free_b;
    }
    uint32_t count = 0;
    DIR *d = opendir(DRIVE_BASE_PATH);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            if (e->d_type == DT_REG && e->d_name[0] != '.') {
                count++;
            }
        }
        closedir(d);
    }
    s_st.file_count = count;
}

static void cleanup_temp_file(void)
{
    struct stat st;
    if (stat(TMP_PATH, &st) == 0) {
        ESP_LOGW(TAG, "removing stale upload temp file (%ld bytes)", (long)st.st_size);
        unlink(TMP_PATH);
    }
    if (stat(OLD_PATH, &st) == 0) {
        // Only left behind by a power loss in the middle of an overwrite.
        ESP_LOGW(TAG, "removing stale previous-version file (%ld bytes)", (long)st.st_size);
        unlink(OLD_PATH);
    }
}

static esp_err_t nvs_get_flag(const char *key, uint8_t *val)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err == ESP_OK) {
        err = nvs_get_u8(h, key, val);
        nvs_close(h);
    }
    return err;
}

static esp_err_t nvs_set_flag(const char *key, uint8_t val)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_u8(h, key, val);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    return err;
}

/** Switch to APP. Caller holds s_lock and has checked the preconditions. */
static drive_result_t do_mount_app(void)
{
    const bool host_had_it = s_st.owner == DRIVE_OWNER_USB;
    set_host_io(false);                         // refuse all new host I/O
    // Two barrier rounds: the first lets a WRITE(10) callback that already passed the
    // gate finish (and queue its deferred sector write), the second drains that write.
    if (!usb_dev_barrier(2000) || !usb_dev_barrier(2000)) {
        ESP_LOGE(TAG, "USB barrier timeout - host writes may still be pending");
        if (host_had_it) {
            set_host_io(true);                  // only give back what the host had
        }
        return DRIVE_ERR_IO;
    }
    tinyusb_msc_set_storage_mount_point(s_storage, TINYUSB_MSC_STORAGE_MOUNT_APP);
    tinyusb_msc_mount_point_t mp;
    tinyusb_msc_get_storage_mount_point(s_storage, &mp);
    uint64_t total = 0, free_b = 0;
    s_st.owner = DRIVE_OWNER_APP;
    s_st.fs_ok = (mp == TINYUSB_MSC_STORAGE_MOUNT_APP) &&
                 esp_vfs_fat_info(DRIVE_BASE_PATH, &total, &free_b) == ESP_OK;
    if (!s_st.fs_ok) {
        ESP_LOGE(TAG, "file system could not be mounted for the app");
        return DRIVE_ERR_NO_FS;
    }
    s_st.needs_format = false;
    cleanup_temp_file();
    refresh_fs_info();
    ESP_LOGI(TAG, "owner: APP (web upload possible), %llu of %llu bytes free",
             (unsigned long long)s_st.free_bytes, (unsigned long long)s_st.total_bytes);
    return DRIVE_OK;
}

/** Switch to USB. Caller holds s_lock and has checked the preconditions. */
static void do_mount_usb(bool replug)
{
    tinyusb_msc_set_storage_mount_point(s_storage, TINYUSB_MSC_STORAGE_MOUNT_USB);
    s_st.owner = DRIVE_OWNER_USB;
    s_host_ejected = false;
    s_host_read_seen = s_host_write_seen = false;
    s_media_changed = true;
    set_host_io(true);
    ESP_LOGI(TAG, "owner: USB host (%s)", s_st.host_writable ? "read/write" : "read-only");
    if (replug && usb_dev_connected()) {
        s_replug_until_ms = esp_timer_get_time() / 1000 + 4000;
        usb_dev_replug();
    }
}

// ============================================================== format at boot

/**
 * Probe the FAT on the wear-levelled partition and format it if requested.
 * Runs before esp_tinyusb takes over the WL handle. Uses MBR + FAT16 like a
 * normal small USB stick (esp_tinyusb's own formatter would create FAT12 here).
 */
static esp_err_t probe_and_format(bool do_format, bool *fs_valid)
{
    const pd_crypt_state_t cst = pd_crypt_state();
    if (s_crypt_failed || (cst != PD_CRYPT_PLAIN && cst != PD_CRYPT_UNLOCKED)) {
        return ESP_ERR_INVALID_STATE;            // locked / damaged: the volume cannot be used
    }
    BYTE pdrv = 0xFF;
    ESP_RETURN_ON_ERROR(ff_diskio_get_drive(&pdrv), TAG, "no free FatFs drive");
    if (s_card) {
        ff_diskio_register_sdmmc(pdrv, s_card);   // returns void
        ff_sdmmc_set_disk_status_check(pdrv, false);
    } else {
        ESP_RETURN_ON_ERROR(ff_diskio_register_wl_partition(pdrv, s_wl), TAG, "diskio register");
    }
    if (cst == PD_CRYPT_UNLOCKED) {
        pd_crypt_diskio_register(pdrv);           // the volume behind the encryption header
    }
    char drv[3] = {(char)('0' + pdrv), ':', 0};
    FATFS *fs = calloc(1, sizeof(FATFS));
    void *work = malloc(4096);
    esp_err_t ret = ESP_OK;
    if (!fs || !work) {
        ret = ESP_ERR_NO_MEM;
        goto out;
    }
    FRESULT fr = FR_OK;
    if (do_format) {
        MKFS_PARM opt;
        if (s_card) {
            // microSD: FAT32 like a normal USB stick, FatFs picks the cluster size.
            ESP_LOGW(TAG, "formatting microSD card (FAT32)");
            opt = (MKFS_PARM){.fmt = FM_FAT32, .n_fat = 2, .align = SD_ALIGN_SECTORS, .n_root = 0, .au_size = 0};
        } else {
            ESP_LOGW(TAG, "formatting internal flash (FAT, %d byte clusters)", (int)FAT_ALLOC_UNIT);
            // align = 8 sectors (4 KiB): data area starts on a flash erase-sector boundary, so
            // sequential cluster writes avoid the slow read-modify-erase path of wear levelling.
            opt = (MKFS_PARM){.fmt = FM_FAT, .n_fat = 2, .align = FLASH_SECTOR_SECTORS, .n_root = 0,
                              .au_size = FAT_ALLOC_UNIT};
        }
        fr = f_mkfs(drv, &opt, work, 4096);
        if (fr != FR_OK) {
            ESP_LOGE(TAG, "f_mkfs failed (%d)", fr);
            ret = ESP_FAIL;
            goto out;
        }
    }
    fr = f_mount(fs, drv, 1);
    *fs_valid = (fr == FR_OK);
    if (fr == FR_OK) {
        if (do_format) {
            char new_label[16];
            snprintf(new_label, sizeof(new_label), "%sDATADRIVE", drv);
            f_setlabel(new_label);   // best effort
        }
        char label[40] = "";   // UTF-8 label can be up to 34 bytes (FatFs docs)
        f_getlabel(drv, label, NULL);
        ESP_LOGI(TAG, "FAT ok (type %d: 1=FAT12 2=FAT16 3=FAT32, label '%s')", fs->fs_type, label);
        f_mount(NULL, drv, 0);
    } else {
        ESP_LOGW(TAG, "no valid FAT found (%d)", fr);
    }
out:
    ff_diskio_unregister(pdrv);
    if (!s_card) {
        ff_diskio_clear_pdrv_wl(s_wl);
    }
    free(work);
    free(fs);
    return ret;
}

// ============================================================== encryption helpers (s_lock held)

/** Status snapshot for drive_get_status() and BUSY for everybody else while we work. */
static void long_op_begin(void)
{
    taskENTER_CRITICAL(&s_snap_mux);
    s_snap = s_st;
    taskEXIT_CRITICAL(&s_snap_mux);
    s_long_op = true;
}

static void long_op_end(void)
{
    s_long_op = false;
}

/**
 * Take the medium away from both sides: host gate closed, host writes already accepted drained
 * (same double barrier as do_mount_app), FAT unmounted from the app. Afterwards owner APP
 * without file system. The caller has checked the owner-change rules.
 */
static drive_result_t release_medium(void)
{
    const bool host_had_it = s_st.owner == DRIVE_OWNER_USB;
    set_host_io(false);
    if (!usb_dev_barrier(2000) || !usb_dev_barrier(2000)) {
        ESP_LOGE(TAG, "USB barrier timeout - host writes may still be pending");
        if (host_had_it) {
            set_host_io(true);
        }
        return DRIVE_ERR_IO;
    }
    tinyusb_msc_set_storage_mount_point(s_storage, TINYUSB_MSC_STORAGE_MOUNT_USB);   // unmount (no-op if not mounted)
    s_st.owner = DRIVE_OWNER_APP;
    s_st.fs_ok = false;
    s_st.total_bytes = s_st.free_bytes = 0;
    s_st.file_count = 0;
    return DRIVE_OK;
}

/** New FAT on the released medium (through pd_crypt if encrypted), then mount it for the app. */
static drive_result_t format_and_mount(void)
{
    bool valid = false;
    if (probe_and_format(true, &valid) != ESP_OK || !valid) {
        s_st.needs_format = true;
        return DRIVE_ERR_IO;
    }
    if (!s_card) {
        nvs_set_flag("fmt_done", 1);
    }
    return do_mount_app();
}

static drive_result_t crypt_result(pd_crypt_err_t e)
{
    switch (e) {
    case PD_CRYPT_OK:              return DRIVE_OK;
    case PD_CRYPT_ERR_AUTH:        return DRIVE_ERR_AUTH;
    case PD_CRYPT_ERR_WAIT:        return DRIVE_ERR_RETRY_LATER;
    case PD_CRYPT_ERR_WEAK:        return DRIVE_ERR_WEAK_PASSWORD;
    case PD_CRYPT_ERR_STATE:       return pd_crypt_is_encrypted() ? DRIVE_ERR_ALREADY_ENCRYPTED : DRIVE_ERR_NOT_ENCRYPTED;
    case PD_CRYPT_ERR_UNSUPPORTED: return DRIVE_ERR_CRYPT_UNAVAILABLE;
    case PD_CRYPT_ERR_DAMAGED:     return DRIVE_ERR_CRYPT_DAMAGED;
    default:                       return DRIVE_ERR_IO;
    }
}

/** true (and *r set) if the USB host holds the drive and may not lose it now (drive_set_owner rules). */
static bool host_keeps_drive(bool force, drive_result_t *r)
{
    if (s_st.owner != DRIVE_OWNER_USB || !usb_dev_connected() || s_host_ejected) {
        return false;
    }
    if (s_host_write_enabled && idle_ms(s_host_write_seen, &s_last_host_write_ms) < CONFIG_PD_HOST_WRITE_GUARD_MS) {
        *r = DRIVE_ERR_HOST_WRITING;
        return true;
    }
    if (!force) {
        *r = DRIVE_ERR_HOST_ACTIVE;
        return true;
    }
    ESP_LOGW(TAG, "taking the drive from the host (confirmed by user)");
    return false;
}

// ============================================================== tasks / events

static void on_msc_event(tinyusb_msc_storage_handle_t handle, tinyusb_msc_event_t *event, void *arg)
{
    ESP_LOGD(TAG, "msc event %d (mount point %d)", event->id, event->mount_point);
    if (event->id == TINYUSB_MSC_EVENT_MOUNT_FAILED || event->id == TINYUSB_MSC_EVENT_FORMAT_FAILED) {
        ESP_LOGE(TAG, "msc event %d: mount/format failed", event->id);
    }
}

static void on_usb_event(bool attached)
{
    const drive_event_t ev = attached ? EV_USB_ATTACHED : EV_USB_DETACHED;
    xQueueSend(s_events, &ev, 0);
}

static void drive_task(void *arg)
{
    // Boot policy: if a host enumerates within a few seconds, provide the drive to it.
    for (int i = 0; i < CONFIG_PD_BOOT_WAIT_HOST_MS / 100 && !usb_dev_connected(); i++) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    lock();
    if (PROVIDE_ON_ATTACH && usb_dev_connected() && s_st.owner == DRIVE_OWNER_APP && s_st.fs_ok &&
        !s_st.upload_active) {
        ESP_LOGI(TAG, "host present at boot -> providing drive");
        do_mount_usb(false);
    } else if (!usb_dev_connected()) {
        ESP_LOGI(TAG, "no USB host at boot -> staying in upload mode");
    }
    unlock();

    drive_event_t ev;
    while (true) {
        if (xQueueReceive(s_events, &ev, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        lock();
        const bool busy = s_st.upload_active || s_open_downloads > 0;
        switch (ev) {
        case EV_USB_ATTACHED:
            ESP_LOGI(TAG, "USB host attached");
            if ((int64_t)(esp_timer_get_time() / 1000) < s_replug_until_ms) {
                // Re-enumeration after our own soft re-plug: the owner was decided before it.
                // Without this, a take-over within the re-plug second was undone here.
                break;
            }
            if (PROVIDE_ON_ATTACH && s_st.owner == DRIVE_OWNER_APP && s_st.fs_ok && !busy) {
                do_mount_usb(false);
            }
            break;
        case EV_USB_DETACHED:
            if ((int64_t)(esp_timer_get_time() / 1000) < s_replug_until_ms) {
                break;   // caused by our own soft re-plug
            }
            ESP_LOGW(TAG, "USB host detached");
            if (s_st.owner == DRIVE_OWNER_USB) {
                do_mount_app();   // host is gone, nobody else can write
            }
            break;
        case EV_HOST_EJECT:
            ESP_LOGI(TAG, "host ejected the drive");
            if (EJECT_TO_APP && s_st.owner == DRIVE_OWNER_USB) {
                do_mount_app();
            }
            break;
        }
        unlock();
    }
}

// ============================================================== public API

/** Try the microSD slot (SDMMC 4-bit, pins from the LILYGO repo). Returns true if a card works. */
static bool sd_try_init(void)
{
#if CONFIG_PD_USE_SDCARD
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 4;
    slot.clk = BOARD_SD_PIN_CLK;
    slot.cmd = BOARD_SD_PIN_CMD;
    slot.d0 = BOARD_SD_PIN_D0;
    slot.d1 = BOARD_SD_PIN_D1;
    slot.d2 = BOARD_SD_PIN_D2;
    slot.d3 = BOARD_SD_PIN_D3;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;   // in addition to any board pull-ups

    if (sdmmc_host_init() != ESP_OK) {
        return false;
    }
    if (sdmmc_host_init_slot(host.slot, &slot) != ESP_OK) {
        sdmmc_host_deinit();
        return false;
    }
    sdmmc_card_t *card = calloc(1, sizeof(sdmmc_card_t));
    esp_err_t err = card ? sdmmc_card_init(&host, card) : ESP_ERR_NO_MEM;
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "no usable microSD card (%s) - using internal flash", esp_err_to_name(err));
        free(card);
        sdmmc_host_deinit();
        return false;
    }
    s_card = card;
    ESP_LOGI(TAG, "microSD card '%s': %llu MB, %u kHz, %d-bit", card->cid.name,
             (unsigned long long)card->csd.capacity * card->csd.sector_size / (1024 * 1024),
             (unsigned)card->real_freq_khz, 1 << card->log_bus_width);
    return true;
#else
    return false;
#endif
}

esp_err_t drive_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_events = xQueueCreate(8, sizeof(drive_event_t));
    ESP_RETURN_ON_FALSE(s_lock && s_events, ESP_ERR_NO_MEM, TAG, "alloc");

    uint8_t v = HOST_WRITABLE_DEFAULT;
    nvs_get_flag("host_wr", &v);
    s_st.host_writable = v != 0;

    // Medium: microSD card if one works, otherwise the internal flash partition.
    if (sd_try_init()) {
        s_st.on_sd = true;
        s_st.sector_size = s_card->csd.sector_size;
        strlcpy(s_st.medium_name, s_card->cid.name, sizeof(s_st.medium_name));
    } else {
        const esp_partition_t *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
                                                               ESP_PARTITION_SUBTYPE_DATA_FAT, "storage");
        ESP_RETURN_ON_FALSE(part, ESP_ERR_NOT_FOUND, TAG, "partition 'storage' not found");
        ESP_RETURN_ON_ERROR(wl_mount(part, &s_wl), TAG, "wl_mount");
        s_st.sector_size = wl_sector_size(s_wl);
        strlcpy(s_st.medium_name, "Flash", sizeof(s_st.medium_name));
        ESP_LOGI(TAG, "storage partition @0x%lx, %lu bytes raw, WL sector %lu, WL size %u",
                 (unsigned long)part->address, (unsigned long)part->size,
                 (unsigned long)s_st.sector_size, (unsigned)wl_size(s_wl));
    }

    // Encryption header: decides whether the medium is plain or encrypted (= locked after boot).
    pd_crypt_medium_t cm;
    if (s_card) {
        pd_crypt_medium_sdmmc(s_card, &cm);
    } else {
        pd_crypt_medium_wl(s_wl, &cm);
    }
    // Fail closed: if the medium cannot be classified it is blocked, never taken as plain.
    const esp_err_t attach_err = pd_crypt_attach(&cm);
    const pd_crypt_state_t cst = pd_crypt_state();
    s_crypt_failed = cst == PD_CRYPT_NONE;
    if (attach_err != ESP_OK || cst == PD_CRYPT_DAMAGED || s_crypt_failed) {
        ESP_LOGE(TAG, "encryption header could not be evaluated (%s) - drive blocked",
                 esp_err_to_name(attach_err));
    }
    const bool locked = crypt_blocked() != DRIVE_OK;    // locked or damaged

    // Format only if requested from the web UI for THIS medium, or (internal flash only)
    // on the very first boot. A microSD card is never formatted automatically.
    // A locked / damaged medium cannot be probed or formatted; a pending request is dropped.
    uint8_t fmt_req = 0, fmt_done = 0;
    nvs_get_flag("fmt_req", &fmt_req);
    nvs_get_flag("fmt_done", &fmt_done);
    const uint8_t this_medium = s_card ? FMT_REQ_SD : FMT_REQ_FLASH;
    bool valid = false;
    if (locked) {
        ESP_LOGI(TAG, "drive is %s - not mounted", cst == PD_CRYPT_LOCKED ? "encrypted and locked" : "blocked");
    } else {
        ESP_RETURN_ON_ERROR(probe_and_format(fmt_req == this_medium, &valid), TAG, "probe");
        if (!valid && !fmt_done && !s_card) {
            ESP_LOGW(TAG, "first boot: creating file system");
            ESP_RETURN_ON_ERROR(probe_and_format(true, &valid), TAG, "format");
        }
        if (valid && !s_card) {
            nvs_set_flag("fmt_done", 1);
        }
    }
    if (fmt_req) {
        nvs_set_flag("fmt_req", 0);
    }
    s_st.needs_format = !valid && !locked;

    // MSC driver with automatic ownership switching OFF - we decide (see header).
    const tinyusb_msc_driver_config_t drv_cfg = {
        .user_flags.auto_mount_off = 1,
        .callback = on_msc_event,
    };
    ESP_RETURN_ON_ERROR(tinyusb_msc_install_driver(&drv_cfg), TAG, "msc driver");

    set_host_io(false);
    tinyusb_msc_storage_config_t st_cfg = {
        .fat_fs = {
            .base_path = (char *)DRIVE_BASE_PATH,
            .config.max_files = 4,
            .do_not_format = true,       // never format silently; see probe_and_format()
            .format_flags = 0,
        },
        // Start owned by the app if the FS is valid; otherwise keep it unmounted.
        .mount_point = valid ? TINYUSB_MSC_STORAGE_MOUNT_APP : TINYUSB_MSC_STORAGE_MOUNT_USB,
    };
    if (s_card) {
        st_cfg.medium.card = s_card;
        ESP_RETURN_ON_ERROR(tinyusb_msc_new_storage_sdmmc(&st_cfg, &s_storage), TAG, "msc storage");
    } else {
        st_cfg.medium.wl_handle = s_wl;
        ESP_RETURN_ON_ERROR(tinyusb_msc_new_storage_spiflash(&st_cfg, &s_storage), TAG, "msc storage");
    }

    lock();
    if (valid) {
        uint64_t total = 0, free_b = 0;
        s_st.owner = DRIVE_OWNER_APP;
        s_st.fs_ok = esp_vfs_fat_info(DRIVE_BASE_PATH, &total, &free_b) == ESP_OK;
        cleanup_temp_file();
        refresh_fs_info();
    } else {
        // No usable FS (or locked): keep it away from the host too, until formatted via the
        // web UI (or unlocked).
        s_st.owner = DRIVE_OWNER_APP;
        s_st.fs_ok = false;
    }
    unlock();

    esp_log_level_set("tusb_desc", ESP_LOG_ERROR);   // default-descriptor notes, MS OS string 0xEE
    ESP_RETURN_ON_ERROR(usb_dev_install(on_usb_event), TAG, "usb");
    xTaskCreate(drive_task, "drive", 4096, NULL, 5, NULL);
    return ESP_OK;
}

void drive_get_status(drive_status_t *out)
{
    // Blocks like a plain lock() - except while a long folder operation (copy, recursive
    // delete) holds the lock: then the state from its start is returned, so the display and
    // the button keep working. The owner cannot change while such an operation runs.
    bool locked;
    while (!(locked = xSemaphoreTake(s_lock, pdMS_TO_TICKS(STATUS_WAIT_MS)) == pdTRUE) && !s_long_op) {
    }
    if (locked) {
        *out = s_st;
        unlock();
    } else {
        taskENTER_CRITICAL(&s_snap_mux);
        *out = s_snap;
        taskEXIT_CRITICAL(&s_snap_mux);
    }
    out->crypt = crypt_state_public();
    out->usb_connected = usb_dev_connected();
    out->usb_suspended = usb_dev_suspended();
    out->host_ejected = s_host_ejected;
    out->host_write_idle_ms = idle_ms(s_host_write_seen, &s_last_host_write_ms);
    const uint32_t read_idle = idle_ms(s_host_read_seen, &s_last_host_read_ms);
    out->host_idle_ms = read_idle < out->host_write_idle_ms ? read_idle : out->host_write_idle_ms;
}

drive_result_t drive_set_owner(drive_owner_t target, bool force)
{
    drive_result_t r = DRIVE_OK;
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    if ((r = crypt_blocked()) != DRIVE_OK) {     // locked / damaged: neither side may have it
        goto out;
    }
    if (target == s_st.owner) {
        goto out;
    }
    if (s_st.upload_active || s_open_downloads > 0) {
        r = DRIVE_ERR_BUSY;
        goto out;
    }
    if (target == DRIVE_OWNER_USB) {
        // Allowed even without a readable FS: the host can then format the drive itself.
        // If the host ejected the drive it will not look at the medium again by
        // itself, so re-plug in that case (or always, if configured).
        do_mount_usb(REPLUG_ON_PROVIDE || s_host_ejected);
    } else if (target == DRIVE_OWNER_APP) {
        const bool host_holds_it = usb_dev_connected() && !s_host_ejected;
        if (host_holds_it) {
            const uint32_t write_idle = idle_ms(s_host_write_seen, &s_last_host_write_ms);
            if (s_host_write_enabled && write_idle < CONFIG_PD_HOST_WRITE_GUARD_MS) {
                r = DRIVE_ERR_HOST_WRITING;   // never taken away during a copy
                goto out;
            }
            if (!force) {
                r = DRIVE_ERR_HOST_ACTIVE;
                goto out;
            }
            ESP_LOGW(TAG, "taking the drive from the host (confirmed by user)");
        }
        r = do_mount_app();
    } else {
        r = DRIVE_ERR_WRONG_OWNER;
    }
out:
    unlock();
    return r;
}

esp_err_t drive_set_host_writable(bool writable)
{
    lock();
    s_st.host_writable = writable;
    unlock();
    return nvs_set_flag("host_wr", writable ? 1 : 0);
}

// ---- names

static bool has_suffix_ci(const char *s, const char *suffix)
{
    const size_t ls = strlen(s), lx = strlen(suffix);
    return ls > lx && strcasecmp(s + ls - lx, suffix) == 0;
}

static bool is_3mf(const char *name)
{
    return has_suffix_ci(name, ".3mf");
}

drive_result_t drive_check_name(const char *name)
{
    if (!name) {
        return DRIVE_ERR_BAD_NAME;
    }
    const size_t len = strlen(name);
    if (len == 0 || len > DRIVE_NAME_MAX || name[0] == '.' || name[0] == ' ' ||
        name[len - 1] == '.' || name[len - 1] == ' ') {
        return DRIVE_ERR_BAD_NAME;
    }
    for (const unsigned char *p = (const unsigned char *)name; *p; p++) {
        if (*p < 0x20 || *p == 0x7F || strchr("/\\:*?\"<>|", *p)) {
            return DRIVE_ERR_BAD_NAME;
        }
    }
#if CONFIG_PD_UPLOAD_ALL_TYPES
    return DRIVE_OK;
#else
    if (is_3mf(name)) {
        return DRIVE_OK;
    }
#if CONFIG_PD_ALLOW_GCODE
    if (has_suffix_ci(name, ".gcode")) {
        return DRIVE_OK;
    }
#endif
    return DRIVE_ERR_BAD_TYPE;
#endif
}

/** VFS path of a relative path (upload/download only; see VFS_REL_MAX). */
static void make_path(char *out, size_t len, const char *name)
{
    snprintf(out, len, "%s/%s", DRIVE_BASE_PATH, name);
}

static drive_result_t require_app_fs(void)
{
    const drive_result_t blocked = crypt_blocked();
    if (blocked != DRIVE_OK) {
        return blocked;
    }
    if (s_st.owner != DRIVE_OWNER_APP) {
        return DRIVE_ERR_WRONG_OWNER;
    }
    return s_st.fs_ok ? DRIVE_OK : DRIVE_ERR_NO_FS;
}

static const char *last_segment(const char *path)
{
    const char *s = strrchr(path, '/');
    return s ? s + 1 : path;
}

static int path_segments(const char *path)
{
    int n = path[0] ? 1 : 0;
    for (const char *p = path; *p; p++) {
        n += *p == '/';
    }
    return n;
}

// ---- path validation

/** Strict UTF-8: no overlong forms, no surrogates, nothing above U+10FFFF. */
static bool utf8_valid(const unsigned char *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        const unsigned char c = s[i];
        if (c < 0x80) {
            i++;
            continue;
        }
        size_t n;
        uint32_t cp, min;
        if ((c & 0xE0) == 0xC0) {
            n = 1, cp = c & 0x1F, min = 0x80;
        } else if ((c & 0xF0) == 0xE0) {
            n = 2, cp = c & 0x0F, min = 0x800;
        } else if ((c & 0xF8) == 0xF0) {
            n = 3, cp = c & 0x07, min = 0x10000;
        } else {
            return false;   // continuation byte without a lead byte, or 0xF8..0xFF
        }
        if (len - i <= n) {
            return false;   // sequence cut off
        }
        for (size_t k = 1; k <= n; k++) {
            if ((s[i + k] & 0xC0) != 0x80) {
                return false;
            }
            cp = cp << 6 | (s[i + k] & 0x3F);
        }
        if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
            return false;
        }
        i += n + 1;
    }
    return true;
}

/** One path segment of n bytes (not NUL-terminated). strict: rules for a new name. */
static bool segment_ok(const char *s, size_t n, bool strict)
{
    // n == 0 is an empty segment: leading '/', trailing '/' or "//".
    if (n == 0 || n > (strict ? DRIVE_NAME_MAX : DRIVE_FILE_NAME_MAX)) {
        return false;
    }
    // No leading dot: rules out "." and ".." (FatFs itself has no relative paths, but never rely
    // on that), hidden names and our own temp files (.pd_upload.tmp, .pd_old.tmp).
    if (s[0] == '.') {
        return false;
    }
    if (strict && (s[0] == ' ' || s[n - 1] == '.' || s[n - 1] == ' ')) {
        return false;
    }
    for (size_t i = 0; i < n; i++) {
        const unsigned char c = (unsigned char)s[i];
        // Also for existing names (FAT cannot store any of these, so no real name is refused):
        // a control character silently ENDS a path inside FatFs ("a\x01b" would mean "a"),
        // '\\' is a second separator, ':' selects a drive, '*' '?' are wildcards.
        if (c < 0x20 || c == 0x7F || strchr("\\:*?\"<>|", c)) {
            return false;
        }
    }
    return true;
}

drive_result_t drive_check_path(const char *path, bool for_new)
{
    // This is the only gate between paths from the network and the file system. Every public
    // file operation calls it before the path is combined with the volume prefix. Rules:
    //  - relative to the root: no leading '/', so "N:/" + path can never name another volume
    //    or an absolute location; no ':' at all (drive selector), no '\\' (second separator)
    //  - no empty segment (no leading/trailing '/', no "//"), no segment with a leading dot
    //    (".", "..", hidden files, temp files), no control characters (FatFs would cut the
    //    path there), valid UTF-8 only (no overlong encodings of '/' or '.')
    //  - bounded length and depth, so every buffer below has a fixed, checked size
    // Case variants and FAT short-name aliases are legal names; operations that must not treat
    // two paths as different (folder into itself) compare what FatFs resolves, not strings.
    if (!path) {
        return DRIVE_ERR_BAD_NAME;
    }
    const size_t max = for_new ? DRIVE_PATH_MAX : DRIVE_SRC_PATH_MAX;
    const size_t total = strnlen(path, max + 1);
    if (total == 0) {
        return for_new ? DRIVE_ERR_BAD_NAME : DRIVE_OK;   // root: exists, cannot be created
    }
    if (total > max || !utf8_valid((const unsigned char *)path, total)) {
        return DRIVE_ERR_BAD_NAME;
    }
    int depth = 0;
    const char *seg = path;
    while (true) {
        const char *end = strchr(seg, '/');
        const size_t n = end ? (size_t)(end - seg) : strlen(seg);
        if (++depth > DRIVE_PATH_DEPTH_MAX || !segment_ok(seg, n, for_new)) {
            return DRIVE_ERR_BAD_NAME;
        }
        if (!end) {
            return DRIVE_OK;
        }
        seg = end + 1;
    }
}

// ---- FatFs helpers (call with s_lock held while the app owns the drive)

/** FatFs drive prefix "N:" of our volume - the same lookup esp_tinyusb uses to unmount it. */
static bool fat_drive(char drv[3])
{
    const BYTE pdrv = s_card ? ff_diskio_get_pdrv_card(s_card) : ff_diskio_get_pdrv_wl(s_wl);
    if (pdrv >= FF_VOLUMES) {
        return false;
    }
    drv[0] = (char)('0' + pdrv);
    drv[1] = ':';
    drv[2] = '\0';
    return true;
}

/** "N:/" + rel (rel has passed drive_check_path()). Returns the length, 0 on error. */
static size_t fat_path(char *out, size_t size, const char *rel)
{
    char drv[3];
    if (!fat_drive(drv)) {
        return 0;
    }
    const int n = snprintf(out, size, "%s/%s", drv, rel);
    return (n > 0 && (size_t)n < size) ? (size_t)n : 0;
}

static drive_result_t fr_result(FRESULT fr)
{
    switch (fr) {
    case FR_OK:           return DRIVE_OK;
    case FR_NO_FILE:
    case FR_NO_PATH:      return DRIVE_ERR_NOT_FOUND;
    case FR_INVALID_NAME: return DRIVE_ERR_BAD_NAME;
    case FR_EXIST:        return DRIVE_ERR_EXISTS;
    default:              return DRIVE_ERR_IO;
    }
}

/** FAT date/time -> seconds since 1970 (local time), exactly like the VFS stat(); 0 = unknown. */
static uint32_t fat_mtime(WORD fdate, WORD ftime)
{
    struct tm tm = {
        .tm_mday = fdate & 0x1F,
        .tm_mon = ((fdate >> 5) & 0x0F) - 1,
        .tm_year = (fdate >> 9) + 80,
        .tm_hour = ftime >> 11,
        .tm_min = (ftime >> 5) & 0x3F,
        .tm_sec = (ftime & 0x1F) * 2,
        .tm_isdst = -1,
    };
    const time_t t = mktime(&tm);
    return t >= MTIME_MIN ? (uint32_t)t : 0;
}

/** Name the listing hides (leading dot, "System Volume Information"). */
static bool name_hidden(const char *n)
{
    return n[0] == '.' || strcasecmp(n, "System Volume Information") == 0;
}

/**
 * Central visibility check for EXISTING objects (buf = mutable FatFs path "N:/a/b/c", modified
 * temporarily). Every component is resolved with f_stat(); if the long name FatFs reports for
 * it is hidden (e.g. reached through an 8.3 alias like "SYSTEM~1" or "TRASHE~1") the result is
 * NOT_FOUND, exactly as if it did not exist. On DRIVE_OK *fno describes the last component
 * (untouched for the root). A missing component gives NOT_FOUND.
 */
static drive_result_t check_visible(char *buf, FILINFO *fno)
{
    char *seg = buf + FAT_REL_OFF;
    while (*seg) {
        char *slash = strchr(seg, '/');
        if (slash) {
            *slash = '\0';
        }
        const bool given_hidden = name_hidden(seg);   // cheap check of the given spelling
        const FRESULT fr = f_stat(buf, fno);
        if (slash) {
            *slash = '/';
        }
        if (fr != FR_OK) {
            return fr_result(fr);
        }
        if (given_hidden || name_hidden(fno->fname)) {   // the reported long name is the real test
            return DRIVE_ERR_NOT_FOUND;
        }
        if (!slash) {
            break;
        }
        seg = slash + 1;
    }
    return DRIVE_OK;
}

/** NOT_FOUND unless the parent folder of the FatFs path in buf exists and is visible (root always does). */
static drive_result_t check_parent(char *buf, FILINFO *fno)
{
    char *slash = strrchr(buf + FAT_REL_OFF, '/');
    if (!slash) {
        return DRIVE_OK;
    }
    *slash = '\0';
    const drive_result_t r = check_visible(buf, fno);
    *slash = '/';
    if (r == DRIVE_OK) {
        return (fno->fattrib & AM_DIR) ? DRIVE_OK : DRIVE_ERR_NOT_FOUND;
    }
    return r;
}

// ---- recursive folder operations
//
// Stack safety (HTTP task, 10 KiB): everything sizeable lives in one heap context - the path
// buffers that the recursive helpers extend and truncate in place, one FatFs directory object
// per level, the FILINFO scratch and the copy buffers. A recursive frame only holds a few
// scalars, and recursion stops at TREE_DEPTH_MAX levels (deeper trees -> DRIVE_ERR_IO).

typedef struct {
    char path[TREE_PATH_SIZE];          // FatFs path being worked on (source for copy)
    char path2[TREE_PATH_SIZE];         // copy/move target
    FF_DIR dirs[TREE_DEPTH_MAX];        // open directory of each recursion level
    FILINFO fno;                        // scratch, overwritten by every recursion level
    char sub[DRIVE_FILE_NAME_MAX + 1];  // delete: sub folder found in the current round
    char batch[DEL_BATCH_SIZE];         // delete: file names collected in the current round
    uint64_t bytes;                     // scan: sum of the file sizes
    int base_segs;                      // scan: segments of the target path
    int64_t last_yield_us;
    FIL *fsrc, *fdst;                   // copy only (FIL embeds a 4 KiB sector buffer)
    uint8_t *buf;
} tree_ctx_t;

static void tree_yield(tree_ctx_t *c)
{
    const int64_t now = esp_timer_get_time();
    if (now - c->last_yield_us >= YIELD_INTERVAL_US) {
        vTaskDelay(1);
        c->last_yield_us = esp_timer_get_time();
    }
}

/** Append "/name" to the path of length len. Returns the new length, 0 if it does not fit. */
static size_t path_push(char *buf, size_t len, const char *name)
{
    const size_t n = strlen(name);
    if (len + 1 + n + 1 > TREE_PATH_SIZE) {
        return 0;
    }
    buf[len] = '/';
    memcpy(buf + len + 1, name, n + 1);
    return len + 1 + n;
}

/**
 * Walk the folder c->path (length len): add up the file sizes in c->bytes and refuse with
 * BAD_NAME if an entry would end up deeper than DRIVE_PATH_DEPTH_MAX segments below a target
 * path of c->base_segs segments (the folder itself takes the target's place).
 */
static drive_result_t scan_tree(tree_ctx_t *c, size_t len, int depth)
{
    if (depth >= TREE_DEPTH_MAX) {
        return DRIVE_ERR_IO;
    }
    FF_DIR *d = &c->dirs[depth];
    if (f_opendir(d, c->path) != FR_OK) {
        return DRIVE_ERR_IO;
    }
    drive_result_t r = DRIVE_OK;
    while (r == DRIVE_OK) {
        tree_yield(c);
        if (f_readdir(d, &c->fno) != FR_OK) {
            r = DRIVE_ERR_IO;
        } else if (c->fno.fname[0] == '\0') {
            break;
        } else if (c->base_segs + depth + 1 > DRIVE_PATH_DEPTH_MAX) {
            r = DRIVE_ERR_BAD_NAME;
        } else if (c->fno.fattrib & AM_DIR) {
            const size_t nl = path_push(c->path, len, c->fno.fname);
            r = nl ? scan_tree(c, nl, depth + 1) : DRIVE_ERR_IO;
            c->path[len] = '\0';
        } else {
            c->bytes += c->fno.fsize;
        }
    }
    f_closedir(d);
    return r;
}

/**
 * Delete everything inside the folder c->path (length len), not the folder itself.
 * Entries are never deleted while the folder is being read: each round collects file names
 * (until the batch is full or a sub folder turns up), closes the folder, deletes the files,
 * then empties and removes the sub folder. Every round removes at least one entry.
 */
static drive_result_t delete_tree(tree_ctx_t *c, size_t len, int depth)
{
    if (depth >= TREE_DEPTH_MAX) {
        return DRIVE_ERR_IO;
    }
    FF_DIR *d = &c->dirs[depth];
    while (true) {
        if (f_opendir(d, c->path) != FR_OK) {
            return DRIVE_ERR_IO;
        }
        size_t used = 0;
        bool have_sub = false;
        FRESULT fr;
        while ((fr = f_readdir(d, &c->fno)) == FR_OK && c->fno.fname[0] != '\0') {
            if (c->fno.fattrib & AM_DIR) {
                strlcpy(c->sub, c->fno.fname, sizeof(c->sub));
                have_sub = true;
                break;
            }
            const size_t n = strlen(c->fno.fname) + 1;
            if (used + n > sizeof(c->batch)) {
                break;   // next round
            }
            memcpy(c->batch + used, c->fno.fname, n);
            used += n;
        }
        f_closedir(d);
        if (fr != FR_OK) {
            return DRIVE_ERR_IO;
        }
        if (used == 0 && !have_sub) {
            return DRIVE_OK;   // empty
        }
        for (size_t pos = 0; pos < used; pos += strlen(c->batch + pos) + 1) {
            tree_yield(c);
            if (!path_push(c->path, len, c->batch + pos)) {
                return DRIVE_ERR_IO;
            }
            fr = f_unlink(c->path);
            c->path[len] = '\0';
            if (fr != FR_OK) {
                return DRIVE_ERR_IO;   // e.g. a read-only file
            }
        }
        if (have_sub) {
            const size_t nl = path_push(c->path, len, c->sub);
            drive_result_t r = nl ? delete_tree(c, nl, depth + 1) : DRIVE_ERR_IO;
            if (r == DRIVE_OK && f_unlink(c->path) != FR_OK) {
                r = DRIVE_ERR_IO;
            }
            c->path[len] = '\0';
            if (r != DRIVE_OK) {
                return r;
            }
        }
    }
}

/** Copy the file c->path to the new file c->path2 (removed again on failure). */
static drive_result_t copy_file(tree_ctx_t *c)
{
    if (f_open(c->fsrc, c->path, FA_READ) != FR_OK) {
        return DRIVE_ERR_IO;
    }
    FRESULT fr = f_open(c->fdst, c->path2, FA_WRITE | FA_CREATE_NEW);
    if (fr != FR_OK) {
        f_close(c->fsrc);
        return fr == FR_EXIST ? DRIVE_ERR_EXISTS : (fr == FR_DENIED ? DRIVE_ERR_NO_SPACE : DRIVE_ERR_IO);
    }
    drive_result_t r = DRIVE_OK;
    while (true) {
        tree_yield(c);
        UINT got = 0, put = 0;
        if (f_read(c->fsrc, c->buf, COPY_BUF_SIZE, &got) != FR_OK) {
            r = DRIVE_ERR_IO;
            break;
        }
        if (got == 0) {
            break;
        }
        fr = f_write(c->fdst, c->buf, got, &put);
        if (fr != FR_OK || put != got) {
            r = (fr == FR_OK || fr == FR_DENIED) ? DRIVE_ERR_NO_SPACE : DRIVE_ERR_IO;   // put < got: disk full
            break;
        }
    }
    if (f_close(c->fdst) != FR_OK && r == DRIVE_OK) {
        r = DRIVE_ERR_IO;
    }
    f_close(c->fsrc);
    if (r != DRIVE_OK) {
        f_unlink(c->path2);
    } else if (f_stat(c->path, &c->fno) == FR_OK) {
        f_utime(c->path2, &c->fno);   // best effort: keep the modification time
    }
    return r;
}

/** Copy the contents of folder c->path (length slen) into the existing folder c->path2 (dlen). */
static drive_result_t copy_tree(tree_ctx_t *c, size_t slen, size_t dlen, int depth)
{
    if (depth >= TREE_DEPTH_MAX) {
        return DRIVE_ERR_IO;
    }
    FF_DIR *d = &c->dirs[depth];
    if (f_opendir(d, c->path) != FR_OK) {
        return DRIVE_ERR_IO;
    }
    drive_result_t r = DRIVE_OK;
    while (r == DRIVE_OK) {
        tree_yield(c);
        if (f_readdir(d, &c->fno) != FR_OK) {
            r = DRIVE_ERR_IO;
            break;
        }
        if (c->fno.fname[0] == '\0') {
            break;
        }
        const bool is_dir = (c->fno.fattrib & AM_DIR) != 0;
        const size_t snl = path_push(c->path, slen, c->fno.fname);
        const size_t dnl = snl ? path_push(c->path2, dlen, c->fno.fname) : 0;
        if (!dnl) {
            r = DRIVE_ERR_IO;
        } else if (is_dir) {
            const FRESULT fr = f_mkdir(c->path2);
            r = fr == FR_OK ? copy_tree(c, snl, dnl, depth + 1)
                            : (fr == FR_DENIED ? DRIVE_ERR_NO_SPACE : DRIVE_ERR_IO);
        } else {
            r = copy_file(c);
        }
        c->path[slen] = '\0';
        c->path2[dlen] = '\0';
    }
    f_closedir(d);
    return r;
}

/** Start of every folder operation (caller holds s_lock): owner, busy, heap context. */
static drive_result_t tree_begin(tree_ctx_t **out)
{
    *out = NULL;
    drive_result_t r = require_app_fs();
    if (r == DRIVE_OK && (s_st.upload_active || s_open_downloads > 0)) {
        r = DRIVE_ERR_BUSY;
    }
    if (r == DRIVE_OK) {
        *out = calloc(1, sizeof(tree_ctx_t));
        if (!*out) {
            ESP_LOGE(TAG, "no memory for a folder operation (%u bytes)", (unsigned)sizeof(tree_ctx_t));
            r = DRIVE_ERR_IO;
        }
    }
    if (r == DRIVE_OK) {
        (*out)->last_yield_us = esp_timer_get_time();
        taskENTER_CRITICAL(&s_snap_mux);
        s_snap = s_st;
        taskEXIT_CRITICAL(&s_snap_mux);
        s_long_op = true;
    }
    return r;
}

/** End of a folder operation (caller still holds s_lock). */
static void tree_end(tree_ctx_t *c)
{
    if (c) {
        free(c->buf);
        free(c->fdst);
        free(c->fsrc);
        free(c);
        refresh_fs_info();
        s_long_op = false;
    }
}

/**
 * Common checks of move and copy, in the order of the contract: NOT_FOUND (source), NOT_FOUND
 * (target folder), BAD_NAME (folder into itself or a descendant), BAD_NAME (too deep).
 * Afterwards c->path is the source, c->path2 the target, c->bytes the size of the source.
 */
static drive_result_t prepare_move_copy(tree_ctx_t *c, const char *from, const char *to, bool *is_dir)
{
    const size_t slen = fat_path(c->path, TREE_PATH_SIZE, from);
    if (!slen || !fat_path(c->path2, TREE_PATH_SIZE, to)) {
        return DRIVE_ERR_IO;
    }
    FRESULT fr;
    drive_result_t r = check_visible(c->path, &c->fno);
    if (r != DRIVE_OK) {
        return r;
    }
    *is_dir = (c->fno.fattrib & AM_DIR) != 0;
    c->bytes = c->fno.fsize;
    r = check_parent(c->path2, &c->fno);
    if (r != DRIVE_OK || !*is_dir) {
        return r;
    }
    // Folder into itself: compare what FatFs resolves, not strings - "A" and "a", "Ä" and "ä"
    // or a short-name alias like "MEINEP~1" all name the same folder. FatFs does not check this
    // itself; renaming a folder into its own sub folder would cut the tree off the volume.
    FF_DIR *d = &c->dirs[0];
    if (f_opendir(d, c->path) != FR_OK) {
        return DRIVE_ERR_IO;
    }
    const DWORD src_clust = d->obj.sclust;   // first cluster identifies the folder
    f_closedir(d);
    if (src_clust == 0) {
        return DRIVE_ERR_IO;                 // only the root has none, and it is never a source
    }
    for (char *p = strchr(c->path2 + FAT_REL_OFF, '/'); p; p = strchr(p + 1, '/')) {
        *p = '\0';                            // every proper prefix of the target path
        fr = f_opendir(d, c->path2);
        const DWORD clust = d->obj.sclust;
        if (fr == FR_OK) {
            f_closedir(d);
        }
        *p = '/';
        if (fr == FR_OK && clust == src_clust) {
            return DRIVE_ERR_BAD_NAME;
        }
    }
    c->bytes = 0;
    c->base_segs = path_segments(to);
    return scan_tree(c, slen, 0);
}

static drive_result_t check_src_dst(const char *from, const char *to)
{
    if (drive_check_path(from, false) != DRIVE_OK || from[0] == '\0' ||
        drive_check_path(to, true) != DRIVE_OK) {
        return DRIVE_ERR_BAD_NAME;
    }
    return DRIVE_OK;
}

// ---- file operations

drive_result_t drive_list(const char *dir, char *buf, size_t buf_len, drive_list_info_t *info)
{
    memset(info, 0, sizeof(*info));
    drive_result_t r = drive_check_path(dir, false);
    if (r != DRIVE_OK) {
        return r;
    }
    struct {
        char path[FAT_REL_OFF + DRIVE_SRC_PATH_MAX + 1];
        FF_DIR dir;
        FILINFO fno;
    } *c = NULL;
    lock();
    r = require_app_fs();
    if (r == DRIVE_OK && !(c = malloc(sizeof(*c)))) {
        r = DRIVE_ERR_IO;
    }
    if (r == DRIVE_OK && !fat_path(c->path, sizeof(c->path), dir)) {
        r = DRIVE_ERR_IO;
    }
    if (r == DRIVE_OK) {
        r = check_visible(c->path, &c->fno);          // hidden folders (also via alias): NOT_FOUND
    }
    if (r == DRIVE_OK) {
        r = fr_result(f_opendir(&c->dir, c->path));   // a file instead of a folder: NOT_FOUND
    }
    if (r == DRIVE_OK) {
        FRESULT fr;
        while ((fr = f_readdir(&c->dir, &c->fno)) == FR_OK && c->fno.fname[0] != '\0') {
            const char *name = c->fno.fname;
            if (name[0] == '.' || strcasecmp(name, "System Volume Information") == 0) {
                continue;
            }
            const size_t name_len = strlen(name) + 1;
            if (info->used + DRIVE_LIST_HDR + name_len > buf_len) {
                info->truncated = true;
                continue;   // a shorter name may still fit
            }
            const bool is_dir = (c->fno.fattrib & AM_DIR) != 0;
            const uint32_t size = is_dir ? 0 : (uint32_t)c->fno.fsize;
            const uint32_t mtime = is_dir ? 0 : fat_mtime(c->fno.fdate, c->fno.ftime);
            const uint8_t type = is_dir ? DRIVE_ENTRY_DIR : DRIVE_ENTRY_FILE;
            char *e = buf + info->used;
            memcpy(e, &size, sizeof(size));
            memcpy(e + 4, &mtime, sizeof(mtime));
            e[8] = (char)type;
            memcpy(e + DRIVE_LIST_HDR, name, name_len);
            info->used += DRIVE_LIST_HDR + name_len;
            info->count++;
        }
        f_closedir(&c->dir);
        if (fr != FR_OK) {
            r = DRIVE_ERR_IO;
        }
    }
    unlock();
    free(c);
    return r;
}

drive_result_t drive_mkdir(const char *path)
{
    drive_result_t r = drive_check_path(path, true);
    if (r != DRIVE_OK) {
        return r;
    }
    lock();
    tree_ctx_t *c;
    r = tree_begin(&c);
    if (r == DRIVE_OK && !fat_path(c->path, TREE_PATH_SIZE, path)) {
        r = DRIVE_ERR_IO;
    }
    if (r == DRIVE_OK) {
        r = check_parent(c->path, &c->fno);
    }
    if (r == DRIVE_OK) {
        const FRESULT fr = f_mkdir(c->path);
        r = fr == FR_DENIED ? DRIVE_ERR_NO_SPACE : fr_result(fr);
    }
    if (r == DRIVE_OK) {
        ESP_LOGI(TAG, "mkdir '%s'", path);
    }
    tree_end(c);
    unlock();
    return r;
}

drive_result_t drive_delete(const char *path, bool recursive)
{
    if (drive_check_path(path, false) != DRIVE_OK || path[0] == '\0') {
        return DRIVE_ERR_BAD_NAME;   // the root cannot be deleted
    }
    lock();
    tree_ctx_t *c;
    drive_result_t r = tree_begin(&c);
    size_t len = 0;
    if (r == DRIVE_OK && !(len = fat_path(c->path, TREE_PATH_SIZE, path))) {
        r = DRIVE_ERR_IO;
    }
    if (r == DRIVE_OK) {
        r = check_visible(c->path, &c->fno);
    }
    if (r == DRIVE_OK && (c->fno.fattrib & AM_DIR)) {
        FF_DIR *d = &c->dirs[0];
        bool empty = false;
        if (f_opendir(d, c->path) != FR_OK) {
            r = DRIVE_ERR_IO;
        } else {
            const FRESULT fr = f_readdir(d, &c->fno);   // hidden entries count as content, too
            empty = fr == FR_OK && c->fno.fname[0] == '\0';
            f_closedir(d);
            if (fr != FR_OK) {
                r = DRIVE_ERR_IO;
            }
        }
        if (r == DRIVE_OK && !empty) {
            r = recursive ? delete_tree(c, len, 0) : DRIVE_ERR_DIR_NOT_EMPTY;
        }
    }
    if (r == DRIVE_OK && f_unlink(c->path) != FR_OK) {
        r = DRIVE_ERR_IO;   // e.g. read-only file
    }
    if (r == DRIVE_OK) {
        ESP_LOGI(TAG, "deleted '%s'", path);
    } else if (c) {
        ESP_LOGW(TAG, "delete '%s': %s", path, drive_result_str(r));
    }
    tree_end(c);
    unlock();
    return r;
}

drive_result_t drive_move(const char *from, const char *to)
{
    drive_result_t r = check_src_dst(from, to);
    if (r != DRIVE_OK) {
        return r;
    }
    lock();
    tree_ctx_t *c;
    bool is_dir = false;
    r = tree_begin(&c);
    if (r == DRIVE_OK) {
        r = prepare_move_copy(c, from, to, &is_dir);
    }
#if !CONFIG_PD_UPLOAD_ALL_TYPES
    if (r == DRIVE_OK && !is_dir) {
        r = drive_check_name(last_segment(to));   // BAD_TYPE
    }
#endif
    if (r == DRIVE_OK) {
        // FatFs moves across folders of the volume, refuses an existing target (FR_EXIST) and
        // allows renaming an object onto itself (changing only the case).
        const FRESULT fr = f_rename(c->path, c->path2);
        r = fr == FR_DENIED ? DRIVE_ERR_NO_SPACE : fr_result(fr);
    }
    if (r == DRIVE_OK) {
        ESP_LOGI(TAG, "moved '%s' -> '%s'", from, to);
    }
    tree_end(c);
    unlock();
    return r;
}

drive_result_t drive_copy(const char *from, const char *to)
{
    drive_result_t r = check_src_dst(from, to);
    if (r != DRIVE_OK) {
        return r;
    }
    lock();
    tree_ctx_t *c;
    bool is_dir = false, created = false;
    r = tree_begin(&c);
    if (r == DRIVE_OK) {
        r = prepare_move_copy(c, from, to, &is_dir);
    }
    if (r == DRIVE_OK) {
        FILINFO *fno = &c->fno;
        const FRESULT fr = f_stat(c->path2, fno);
        r = fr == FR_OK ? DRIVE_ERR_EXISTS : (fr == FR_NO_FILE ? DRIVE_OK : fr_result(fr));
    }
    if (r == DRIVE_OK) {
        refresh_fs_info();
        if (c->bytes + FREE_RESERVE_BYTES > s_st.free_bytes) {
            r = DRIVE_ERR_NO_SPACE;
        }
    }
    if (r == DRIVE_OK) {
        c->fsrc = malloc(sizeof(FIL));
        c->fdst = malloc(sizeof(FIL));
        c->buf = malloc(COPY_BUF_SIZE);
        if (!c->fsrc || !c->fdst || !c->buf) {
            ESP_LOGE(TAG, "no memory for copy buffers");
            r = DRIVE_ERR_IO;
        }
    }
    if (r == DRIVE_OK) {
        const int64_t t0 = esp_timer_get_time();
        if (!is_dir) {
            r = copy_file(c);                     // removes its partial target itself
        } else {
            const FRESULT fr = f_mkdir(c->path2);
            created = fr == FR_OK;
            r = created ? copy_tree(c, strlen(c->path), strlen(c->path2), 0)
                        : (fr == FR_DENIED ? DRIVE_ERR_NO_SPACE : fr_result(fr));
        }
        if (r != DRIVE_OK && created) {
            // Remove the incomplete copy again (it did not exist before: EXISTS was checked).
            const size_t dlen = strlen(c->path2);
            memcpy(c->path, c->path2, dlen + 1);
            if (delete_tree(c, dlen, 0) != DRIVE_OK || f_unlink(c->path) != FR_OK) {
                ESP_LOGE(TAG, "could not remove the incomplete copy '%s'", to);
            }
        }
        ESP_LOGI(TAG, "copy '%s' -> '%s' (%llu bytes, %lld ms): %s", from, to, (unsigned long long)c->bytes,
                 (esp_timer_get_time() - t0) / 1000, drive_result_str(r));
    }
    tree_end(c);
    unlock();
    return r;
}

drive_result_t drive_format(void)
{
    lock();
    const drive_result_t blocked = crypt_blocked();
    if (blocked != DRIVE_OK) {
        unlock();
        return blocked;                          // damaged: drive_crypt_force_format()
    }
    if (s_st.owner != DRIVE_OWNER_APP) {
        unlock();
        return DRIVE_ERR_WRONG_OWNER;
    }
    if (s_st.upload_active || s_open_downloads > 0) {
        unlock();
        return DRIVE_ERR_BUSY;
    }
    if (pd_crypt_state() == PD_CRYPT_UNLOCKED) {
        // The boot-time path below cannot work here: after the restart the drive is locked.
        ESP_LOGW(TAG, "formatting the encrypted volume");
        long_op_begin();
        drive_result_t r = release_medium();
        if (r == DRIVE_OK) {
            r = format_and_mount();
        }
        long_op_end();
        unlock();
        return r;
    }
    // Formatting happens on the next boot, before USB/MSC is started (one code path).
    if (nvs_set_flag("fmt_req", s_card ? FMT_REQ_SD : FMT_REQ_FLASH) != ESP_OK) {
        unlock();
        return DRIVE_ERR_IO;
    }
    ESP_LOGW(TAG, "format requested - restarting");
    unlock();
    vTaskDelay(pdMS_TO_TICKS(300));
    esp_restart();
    return DRIVE_OK;
}

drive_result_t drive_download_open(const char *path, int *fd, uint32_t *size)
{
    *fd = -1;
    // The VFS would silently cut a longer path (and could open another file).
    if (drive_check_path(path, false) != DRIVE_OK || strlen(path) > VFS_REL_MAX) {
        return DRIVE_ERR_BAD_NAME;
    }
    lock();
    drive_result_t r = require_app_fs();
    if (r == DRIVE_OK && path[0] == '\0') {
        r = DRIVE_ERR_IS_DIR;
    }
    if (r == DRIVE_OK && path[0] != '\0') {
        struct {
            char path[FAT_REL_OFF + VFS_REL_MAX + 1];
            FILINFO fno;
        } *v = malloc(sizeof(*v));
        if (!v || !fat_path(v->path, sizeof(v->path), path)) {
            r = DRIVE_ERR_IO;
        } else {
            r = check_visible(v->path, &v->fno);
        }
        free(v);
    }
    if (r == DRIVE_OK) {
        char full[sizeof(DRIVE_BASE_PATH) + 1 + VFS_REL_MAX + 1];
        make_path(full, sizeof(full), path);
        struct stat st;
        if (stat(full, &st) != 0) {
            r = DRIVE_ERR_NOT_FOUND;
        } else if (S_ISDIR(st.st_mode)) {
            r = DRIVE_ERR_IS_DIR;
        } else {
            *fd = open(full, O_RDONLY);
            if (*fd < 0 || fstat(*fd, &st) != 0) {
                if (*fd >= 0) {
                    close(*fd);
                    *fd = -1;
                }
                r = DRIVE_ERR_NOT_FOUND;
            } else {
                *size = (uint32_t)st.st_size;
                s_open_downloads++;
            }
        }
    }
    unlock();
    return r;
}

void drive_download_close(int fd)
{
    lock();
    close(fd);
    if (s_open_downloads > 0) {
        s_open_downloads--;
    }
    unlock();
}

// ---- upload

drive_result_t drive_upload_validate(const char *path, uint64_t size)
{
    if (drive_check_path(path, true) != DRIVE_OK) {
        return DRIVE_ERR_BAD_NAME;
    }
    const drive_result_t r = drive_check_name(last_segment(path));   // BAD_TYPE
    if (r != DRIVE_OK) {
        return r;
    }
    if (size > UINT32_MAX) {
        return DRIVE_ERR_TOO_LARGE;
    }
    return size == 0 ? DRIVE_ERR_INVALID_FILE : DRIVE_OK;
}

drive_result_t drive_upload_begin(const char *path, uint32_t size, bool overwrite)
{
    drive_result_t r = drive_upload_validate(path, size);
    if (r != DRIVE_OK) {
        return r;
    }
    lock();
    if ((r = require_app_fs()) != DRIVE_OK) {
        goto out;
    }
    if (s_st.upload_active) {
        r = DRIVE_ERR_BUSY;
        goto out;
    }
    // New paths are at most DRIVE_PATH_MAX (< VFS_REL_MAX) bytes: safe for the VFS.
    char full[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_PATH_MAX + 1];
    make_path(full, sizeof(full), path);
    struct stat st;
    char *slash = strrchr(full + sizeof(DRIVE_BASE_PATH), '/');
    if (slash) {   // target folder must exist and be visible (the root always does)
        *slash = '\0';
        bool folder = stat(full, &st) == 0 && S_ISDIR(st.st_mode);
        if (folder) {
            struct {
                char path[FAT_REL_OFF + DRIVE_PATH_MAX + 1];
                FILINFO fno;
            } *v = malloc(sizeof(*v));
            if (!v) {
                *slash = '/';
                r = DRIVE_ERR_IO;
                goto out;
            }
            folder = fat_path(v->path, sizeof(v->path), full + sizeof(DRIVE_BASE_PATH)) &&
                     check_visible(v->path, &v->fno) == DRIVE_OK;
            free(v);
        }
        *slash = '/';
        if (!folder) {
            r = DRIVE_ERR_NOT_FOUND;
            goto out;
        }
    }
    if (stat(full, &st) == 0) {
        if (S_ISDIR(st.st_mode)) {
            r = DRIVE_ERR_IS_DIR;
            goto out;
        }
        if (!overwrite) {
            r = DRIVE_ERR_EXISTS;
            goto out;
        }
    }
    cleanup_temp_file();
    refresh_fs_info();
    // Old file is only removed after success, so the new one needs its full size.
    if ((uint64_t)size + FREE_RESERVE_BYTES > s_st.free_bytes) {
        r = DRIVE_ERR_NO_SPACE;
        goto out;
    }
    s_up_fd = open(TMP_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (s_up_fd < 0) {
        r = DRIVE_ERR_IO;
        goto out;
    }
    s_up_overwrite = overwrite;
    memset(s_up_head, 0, sizeof(s_up_head));
    memset(s_up_tail, 0, sizeof(s_up_tail));
    strlcpy(s_up_path, path, sizeof(s_up_path));
    s_st.upload_active = true;
    strlcpy(s_st.upload_name, last_segment(path), sizeof(s_st.upload_name));
    s_st.upload_received = 0;
    s_st.upload_total = size;
    ESP_LOGI(TAG, "upload start '%s' (%lu bytes)", path, (unsigned long)size);
out:
    unlock();
    return r;
}

drive_result_t drive_upload_write(const void *data, size_t len)
{
    drive_result_t r = DRIVE_OK;
    lock();
    if (!s_st.upload_active || s_up_fd < 0) {
        r = DRIVE_ERR_IO;
        goto out;
    }
    if ((uint64_t)s_st.upload_received + len > s_st.upload_total) {
        r = DRIVE_ERR_INVALID_FILE;   // more data than announced
        goto out;
    }
    // Remember first 4 bytes and the last TAIL_SIZE bytes for the content check.
    const uint8_t *p = data;
    for (size_t i = 0; i < len && s_st.upload_received + i < sizeof(s_up_head); i++) {
        s_up_head[s_st.upload_received + i] = p[i];
    }
    if (len >= TAIL_SIZE) {
        memcpy(s_up_tail, p + len - TAIL_SIZE, TAIL_SIZE);
    } else {
        memmove(s_up_tail, s_up_tail + len, TAIL_SIZE - len);
        memcpy(s_up_tail + TAIL_SIZE - len, p, len);
    }
    size_t done = 0;
    while (done < len) {
        const ssize_t n = write(s_up_fd, p + done, len - done);
        if (n <= 0) {
            r = (errno == ENOSPC) ? DRIVE_ERR_NO_SPACE : DRIVE_ERR_IO;
            ESP_LOGE(TAG, "upload write failed: errno %d", errno);
            goto out;
        }
        done += (size_t)n;
    }
    s_st.upload_received += len;
out:
    unlock();
    return r;
}

static bool zip_eocd_present(void)
{
    // ZIP end of central directory record "PK\5\6" lies within the last 22 + comment bytes.
    const uint32_t have = s_st.upload_received < TAIL_SIZE ? s_st.upload_received : TAIL_SIZE;
    for (int i = TAIL_SIZE - 22; i >= (int)(TAIL_SIZE - have); i--) {
        if (s_up_tail[i] == 'P' && s_up_tail[i + 1] == 'K' && s_up_tail[i + 2] == 5 && s_up_tail[i + 3] == 6) {
            return true;
        }
    }
    return false;
}

static void upload_reset_locked(void)
{
    if (s_up_fd >= 0) {
        close(s_up_fd);
        s_up_fd = -1;
    }
    unlink(TMP_PATH);
    s_st.upload_active = false;
    refresh_fs_info();
}

drive_result_t drive_upload_finish(uint32_t mtime)
{
    drive_result_t r = DRIVE_OK;
    lock();
    if (!s_st.upload_active || s_up_fd < 0) {
        unlock();
        return DRIVE_ERR_IO;
    }
    if (s_st.upload_received != s_st.upload_total) {
        r = DRIVE_ERR_INCOMPLETE;
    } else if (fsync(s_up_fd) != 0) {
        r = DRIVE_ERR_IO;
    }
    if (close(s_up_fd) != 0 && r == DRIVE_OK) {
        r = DRIVE_ERR_IO;
    }
    s_up_fd = -1;
    if (r == DRIVE_OK && is_3mf(s_st.upload_name)) {
        const bool zip_head = memcmp(s_up_head, "PK\x03\x04", 4) == 0;
        if (!zip_head || !zip_eocd_present()) {
            r = DRIVE_ERR_INVALID_FILE;
        }
    }
    if (r == DRIVE_OK) {
        // Target in its folder; the temp files stay in the root (rename moves across folders).
        char path[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_PATH_MAX + 1];
        make_path(path, sizeof(path), s_up_path);
        struct stat st;
        const bool exists = stat(path, &st) == 0;
        if (exists && S_ISDIR(st.st_mode)) {
            r = DRIVE_ERR_IS_DIR;   // never rename a folder away as "previous version"
        } else if (exists && !s_up_overwrite) {
            r = DRIVE_ERR_EXISTS;
        } else if (exists) {
            // Keep the previous version until the new file is in place.
            unlink(OLD_PATH);
            if (rename(path, OLD_PATH) != 0) {
                r = DRIVE_ERR_IO;
            } else if (rename(TMP_PATH, path) != 0) {
                rename(OLD_PATH, path);
                r = DRIVE_ERR_IO;
            } else {
                unlink(OLD_PATH);
            }
        } else if (rename(TMP_PATH, path) != 0) {
            r = errno == ENOENT ? DRIVE_ERR_NOT_FOUND : DRIVE_ERR_IO;
        }
        if (r == DRIVE_OK && mtime >= MTIME_MIN) {
            const struct utimbuf times = {.actime = mtime, .modtime = mtime};
            utime(path, &times);   // best effort: keep the file's original modification time
        }
    }
    if (r == DRIVE_OK) {
        ESP_LOGI(TAG, "upload ok '%s'", s_up_path);
    } else {
        ESP_LOGW(TAG, "upload failed '%s': %s", s_up_path, drive_result_str(r));
    }
    upload_reset_locked();
    unlock();
    return r;
}

void drive_upload_abort(void)
{
    lock();
    if (s_st.upload_active) {
        ESP_LOGW(TAG, "upload aborted '%s' after %lu bytes", s_st.upload_name,
                 (unsigned long)s_st.upload_received);
        upload_reset_locked();
    }
    unlock();
}

drive_result_t drive_prepare_restart(bool force)
{
    if (force) {
        lock();
    } else if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    if (!force && s_st.upload_active) {
        unlock();
        return DRIVE_ERR_BUSY;
    }
    if (!force && s_host_write_enabled &&
        idle_ms(s_host_write_seen, &s_last_host_write_ms) < CONFIG_PD_HOST_WRITE_GUARD_MS) {
        unlock();
        return DRIVE_ERR_HOST_WRITING;
    }
    set_host_io(false);
    usb_dev_barrier(2000);
    usb_dev_barrier(2000);
    if (s_st.upload_active) {
        ESP_LOGW(TAG, "restart: dropping upload '%s'", s_st.upload_name);
        upload_reset_locked();
    }
    unlock();
    ESP_LOGI(TAG, "ready for restart");
    return DRIVE_OK;
}

void drive_restart_to_download(void)
{
    drive_prepare_restart(true);
    board_restart_to_download_mode();   // USB shutdown handler detaches first
}

// ============================================================== encryption

void drive_crypt_get_info(drive_crypt_info_t *out)
{
    pd_crypt_info_t ci;
    pd_crypt_get_info(&ci);                     // lock-free
    memset(out, 0, sizeof(*out));
    out->state = crypt_state_public();
    out->available = ci.available && !s_crypt_failed;
    out->hw_fast_path = ci.hw_fast_path;
    out->retry_after_s = ci.retry_after_s;
    out->failed_attempts = ci.failed_attempts;
    out->kdf_iterations = ci.kdf_iterations;
    out->volume_bytes = (uint64_t)ci.volume_sectors * PD_CRYPT_SECTOR_SIZE;
}

/** Common checks of setup/disable/force: owner APP, nothing open. */
static drive_result_t crypt_require_idle_app(void)
{
    if (s_st.owner != DRIVE_OWNER_APP) {
        return DRIVE_ERR_WRONG_OWNER;
    }
    return (s_st.upload_active || s_open_downloads > 0) ? DRIVE_ERR_BUSY : DRIVE_OK;
}

/** Header-only changes (password, recovery key): unlocked, no upload, host not writing. */
static drive_result_t crypt_require_header_change(void)
{
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_UNLOCKED: break;
    case DRIVE_CRYPT_LOCKED:   return DRIVE_ERR_LOCKED;
    case DRIVE_CRYPT_DAMAGED:  return DRIVE_ERR_CRYPT_DAMAGED;
    default:                   return DRIVE_ERR_NOT_ENCRYPTED;
    }
    if (s_st.upload_active) {
        return DRIVE_ERR_BUSY;
    }
    if (s_st.owner == DRIVE_OWNER_USB && s_host_write_enabled &&
        idle_ms(s_host_write_seen, &s_last_host_write_ms) < CONFIG_PD_HOST_WRITE_GUARD_MS) {
        return DRIVE_ERR_HOST_WRITING;
    }
    return DRIVE_OK;
}

drive_result_t drive_crypt_setup(const char *password, char *recovery_out, size_t recovery_len)
{
    if (!recovery_out || recovery_len < DRIVE_CRYPT_RECOVERY_LEN) {
        return DRIVE_ERR_IO;                     // programming error
    }
    recovery_out[0] = '\0';
    if (pd_crypt_check_password(password) != PD_CRYPT_OK) {
        return DRIVE_ERR_WEAK_PASSWORD;
    }
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r;
    pd_crypt_info_t ci;
    pd_crypt_get_info(&ci);
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_OFF:     r = ci.available ? crypt_require_idle_app() : DRIVE_ERR_CRYPT_UNAVAILABLE; break;
    case DRIVE_CRYPT_DAMAGED: r = DRIVE_ERR_CRYPT_DAMAGED; break;
    default:                  r = DRIVE_ERR_ALREADY_ENCRYPTED; break;
    }
    if (r != DRIVE_OK) {
        unlock();
        return r;
    }
    ESP_LOGW(TAG, "setting up drive encryption (erases and formats the drive)");
    long_op_begin();
    r = release_medium();
    if (r == DRIVE_OK) {
        const pd_crypt_err_t e = pd_crypt_setup(password, recovery_out, recovery_len);
        if (e == PD_CRYPT_OK) {
            r = format_and_mount();
            if (r == DRIVE_ERR_NO_FS) {
                r = DRIVE_ERR_IO;
            }
        } else {
            r = crypt_result(e);
            if (pd_crypt_state() == PD_CRYPT_PLAIN) {
                // Not encrypted, but the medium may already be (partly) erased: mount what is
                // left, otherwise it needs formatting.
                if (do_mount_app() != DRIVE_OK) {
                    s_st.needs_format = true;
                }
            } else if (r == DRIVE_OK) {
                r = DRIVE_ERR_IO;                // header written but locked (no memory for the keys)
            }
        }
    }
    long_op_end();
    unlock();
    return r;
}

drive_result_t drive_crypt_unlock(const char *secret, uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r = DRIVE_OK;
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_LOCKED:   break;
    case DRIVE_CRYPT_UNLOCKED: r = DRIVE_OK; goto out;
    case DRIVE_CRYPT_DAMAGED:  r = DRIVE_ERR_CRYPT_DAMAGED; goto out;
    default:                   r = DRIVE_ERR_NOT_ENCRYPTED; goto out;
    }
    long_op_begin();
    const pd_crypt_err_t e = pd_crypt_unlock(secret, retry_after_s);
    if (e == PD_CRYPT_OK) {
        // Same path as an owner change: app first, then the host if one is attached.
        if (do_mount_app() == DRIVE_ERR_NO_FS) {
            s_st.needs_format = true;            // unlocked, but the volume has no file system
        }
        if (PROVIDE_ON_ATTACH && s_st.fs_ok && usb_dev_connected()) {
            ESP_LOGI(TAG, "unlocked with a host attached -> providing drive");
            do_mount_usb(REPLUG_ON_PROVIDE || s_host_ejected);
        }
    } else {
        r = crypt_result(e);
    }
    long_op_end();
out:
    unlock();
    return r;
}

drive_result_t drive_crypt_lock(bool force)
{
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r = DRIVE_OK;
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_UNLOCKED: break;
    case DRIVE_CRYPT_LOCKED:   goto out;
    case DRIVE_CRYPT_DAMAGED:  r = DRIVE_ERR_CRYPT_DAMAGED; goto out;
    default:                   r = DRIVE_ERR_NOT_ENCRYPTED; goto out;
    }
    if (s_st.upload_active || s_open_downloads > 0) {
        r = DRIVE_ERR_BUSY;
        goto out;
    }
    if (host_keeps_drive(force, &r)) {
        goto out;
    }
    r = release_medium();
    if (r == DRIVE_OK) {
        pd_crypt_lock();
        s_st.needs_format = false;
        ESP_LOGI(TAG, "drive locked");
    }
out:
    unlock();
    return r;
}

drive_result_t drive_crypt_change_password(const char *old_secret, const char *new_password,
                                           uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (pd_crypt_check_password(new_password) != PD_CRYPT_OK) {
        return DRIVE_ERR_WEAK_PASSWORD;
    }
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r = crypt_require_header_change();
    if (r == DRIVE_OK) {
        // Only the header sectors outside the volume change; host or app I/O may continue
        // (pd_crypt serialises the medium access).
        long_op_begin();
        r = crypt_result(pd_crypt_change_password(old_secret, new_password, retry_after_s));
        long_op_end();
    }
    unlock();
    return r;
}

drive_result_t drive_crypt_new_recovery_key(const char *password, char *recovery_out, size_t recovery_len)
{
    if (!recovery_out || recovery_len < DRIVE_CRYPT_RECOVERY_LEN) {
        return DRIVE_ERR_IO;                     // programming error
    }
    recovery_out[0] = '\0';
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r = crypt_require_header_change();
    if (r == DRIVE_OK) {
        long_op_begin();
        r = crypt_result(pd_crypt_new_recovery_key(password, recovery_out, recovery_len, NULL));
        long_op_end();
    }
    unlock();
    return r;
}

drive_result_t drive_crypt_disable(const char *secret, uint32_t *retry_after_s)
{
    if (retry_after_s) {
        *retry_after_s = 0;
    }
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r;
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_LOCKED:
    case DRIVE_CRYPT_UNLOCKED: r = crypt_require_idle_app(); break;
    case DRIVE_CRYPT_DAMAGED:  r = DRIVE_ERR_CRYPT_DAMAGED; break;
    default:                   r = DRIVE_ERR_NOT_ENCRYPTED; break;
    }
    if (r != DRIVE_OK) {
        unlock();
        return r;
    }
    long_op_begin();
    // Secret first: nothing is unmounted for a wrong one. Nobody uses the mounted volume
    // meanwhile (s_lock held; owner APP, so the host gate is closed). After the header is gone
    // the volume is only unmounted (no writes) and formatted plain.
    const pd_crypt_err_t e = pd_crypt_disable(secret, retry_after_s);
    if (e == PD_CRYPT_OK) {
        ESP_LOGW(TAG, "drive encryption removed - formatting the plain drive");
        r = release_medium();
        if (r == DRIVE_OK) {
            r = format_and_mount();
            if (r == DRIVE_ERR_NO_FS) {
                r = DRIVE_ERR_IO;
            }
        }
        if (r != DRIVE_OK) {
            s_st.needs_format = true;
        }
    } else {
        r = crypt_result(e);
    }
    long_op_end();
    unlock();
    return r;
}

drive_result_t drive_crypt_force_format(void)
{
    if (!lock_unless_long_op()) {
        return DRIVE_ERR_BUSY;
    }
    drive_result_t r = DRIVE_OK;
    switch (crypt_state_public()) {
    case DRIVE_CRYPT_LOCKED:
    case DRIVE_CRYPT_DAMAGED:  break;
    case DRIVE_CRYPT_UNLOCKED: r = DRIVE_ERR_ALREADY_ENCRYPTED; break;   // use disable or format
    default:                   r = DRIVE_ERR_NOT_ENCRYPTED; break;
    }
    if (r == DRIVE_OK && s_crypt_failed) {
        r = DRIVE_ERR_CRYPT_UNAVAILABLE;         // pd_crypt not attached: nothing can be done
    }
    if (r == DRIVE_OK) {
        r = crypt_require_idle_app();
    }
    if (r != DRIVE_OK) {
        unlock();
        return r;
    }
    ESP_LOGW(TAG, "erasing the encryption header without a secret and formatting (all data lost)");
    long_op_begin();
    r = release_medium();
    if (r == DRIVE_OK) {
        const esp_err_t err = pd_crypt_force_plain();
        if (pd_crypt_state() == PD_CRYPT_PLAIN) {
            r = format_and_mount();
            if (r == DRIVE_ERR_NO_FS) {
                r = DRIVE_ERR_IO;
            }
            if (r == DRIVE_OK && err != ESP_OK) {
                ESP_LOGW(TAG, "a header sector could not be overwritten (%s), but formatting worked",
                         esp_err_to_name(err));
            }
        } else {
            r = DRIVE_ERR_IO;
        }
        if (r != DRIVE_OK) {
            s_st.needs_format = pd_crypt_state() == PD_CRYPT_PLAIN;
        }
    }
    long_op_end();
    unlock();
    return r;
}
