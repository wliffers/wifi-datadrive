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
#include "drive.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>
#include "board.h"
#include "diskio_impl.h"
#include "diskio_wl.h"
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
#define TAIL_SIZE          1024             // bytes kept for the ZIP end-of-central-directory check
#define FAT_ALLOC_UNIT     2048             // with 512 B sectors -> FAT16 on this volume size

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
static tinyusb_msc_storage_handle_t s_storage;
static drive_status_t s_st;
static int s_open_downloads;
static int64_t s_replug_until_ms;           // ignore DETACHED caused by our own re-plug

// Upload session (only one at a time)
static int s_up_fd = -1;
static bool s_up_overwrite;
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

static void lock(void)   { xSemaphoreTake(s_lock, portMAX_DELAY); }
static void unlock(void) { xSemaphoreGive(s_lock); }

const char *drive_result_str(drive_result_t r)
{
    switch (r) {
    case DRIVE_OK:               return "OK";
    case DRIVE_ERR_BUSY:         return "Upload oder Download läuft gerade";
    case DRIVE_ERR_HOST_ACTIVE:  return "Das Laufwerk ist am USB-Host eingebunden. Am Host zuerst auswerfen oder Umschalten bestätigen.";
    case DRIVE_ERR_HOST_WRITING: return "Der USB-Host schreibt gerade. Bitte warten, bis der Kopiervorgang fertig ist.";
    case DRIVE_ERR_WRONG_OWNER:  return "Nicht möglich, solange das Laufwerk dem USB-Host bereitgestellt ist";
    case DRIVE_ERR_NO_FS:        return "Kein gültiges Dateisystem - bitte formatieren";
    case DRIVE_ERR_BAD_NAME:     return "Ungültiger Dateiname";
    case DRIVE_ERR_BAD_TYPE:     return "Dateityp nicht erlaubt";
    case DRIVE_ERR_EXISTS:       return "Datei existiert bereits";
    case DRIVE_ERR_NOT_FOUND:    return "Datei nicht gefunden";
    case DRIVE_ERR_NO_SPACE:     return "Nicht genug freier Speicher";
    case DRIVE_ERR_INVALID_FILE: return "Datei ist beschädigt oder kein gültiges 3MF (ZIP)";
    case DRIVE_ERR_INCOMPLETE:   return "Upload unvollständig";
    case DRIVE_ERR_IO:           return "Lese-/Schreibfehler";
    default:                     return "Unbekannter Fehler";
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
    memcpy(resp->vendor_id, "PrintDrv", 8);
    memcpy(resp->product_id, "WiFi PrintDrive ", 16);
    memcpy(resp->product_rev, "0.1 ", 4);
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
    set_host_io(false);                         // refuse all new host I/O
    // Two barrier rounds: the first lets a WRITE(10) callback that already passed the
    // gate finish (and queue its deferred sector write), the second drains that write.
    if (!usb_dev_barrier(2000) || !usb_dev_barrier(2000)) {
        ESP_LOGE(TAG, "USB barrier timeout - host writes may still be pending");
        set_host_io(true);
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
    BYTE pdrv = 0xFF;
    ESP_RETURN_ON_ERROR(ff_diskio_get_drive(&pdrv), TAG, "no free FatFs drive");
    ESP_RETURN_ON_ERROR(ff_diskio_register_wl_partition(pdrv, s_wl), TAG, "diskio register");
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
        ESP_LOGW(TAG, "formatting storage (FAT, %d byte clusters)", FAT_ALLOC_UNIT);
        const MKFS_PARM opt = {.fmt = FM_FAT, .n_fat = 2, .align = 0, .n_root = 0, .au_size = FAT_ALLOC_UNIT};
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
            snprintf(new_label, sizeof(new_label), "%sPRINTDRIVE", drv);
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
    ff_diskio_clear_pdrv_wl(s_wl);
    free(work);
    free(fs);
    return ret;
}

// ============================================================== tasks / events

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

esp_err_t drive_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_events = xQueueCreate(8, sizeof(drive_event_t));
    ESP_RETURN_ON_FALSE(s_lock && s_events, ESP_ERR_NO_MEM, TAG, "alloc");

    uint8_t v = HOST_WRITABLE_DEFAULT;
    nvs_get_flag("host_wr", &v);
    s_st.host_writable = v != 0;

    const esp_partition_t *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
                                                           ESP_PARTITION_SUBTYPE_DATA_FAT, "storage");
    ESP_RETURN_ON_FALSE(part, ESP_ERR_NOT_FOUND, TAG, "partition 'storage' not found");
    ESP_RETURN_ON_ERROR(wl_mount(part, &s_wl), TAG, "wl_mount");
    s_st.sector_size = wl_sector_size(s_wl);
    ESP_LOGI(TAG, "storage partition @0x%lx, %lu bytes raw, WL sector %lu, WL size %u",
             (unsigned long)part->address, (unsigned long)part->size,
             (unsigned long)s_st.sector_size, (unsigned)wl_size(s_wl));

    // Format if requested from the web UI, or on the very first boot.
    uint8_t fmt_req = 0, fmt_done = 0;
    nvs_get_flag("fmt_req", &fmt_req);
    nvs_get_flag("fmt_done", &fmt_done);
    bool valid = false;
    ESP_RETURN_ON_ERROR(probe_and_format(fmt_req != 0, &valid), TAG, "probe");
    if (!valid && !fmt_done) {
        ESP_LOGW(TAG, "first boot: creating file system");
        ESP_RETURN_ON_ERROR(probe_and_format(true, &valid), TAG, "format");
    }
    if (valid) {
        nvs_set_flag("fmt_done", 1);
    }
    if (fmt_req) {
        nvs_set_flag("fmt_req", 0);
    }
    s_st.needs_format = !valid;

    // MSC driver with automatic ownership switching OFF - we decide (see header).
    const tinyusb_msc_driver_config_t drv_cfg = {
        .user_flags.auto_mount_off = 1,
        .callback = NULL,
    };
    ESP_RETURN_ON_ERROR(tinyusb_msc_install_driver(&drv_cfg), TAG, "msc driver");

    set_host_io(false);
    const tinyusb_msc_storage_config_t st_cfg = {
        .medium.wl_handle = s_wl,
        .fat_fs = {
            .base_path = (char *)DRIVE_BASE_PATH,
            .config.max_files = 4,
            .do_not_format = true,       // never format silently; see probe_and_format()
            .format_flags = 0,
        },
        // Start owned by the app if the FS is valid; otherwise keep it unmounted.
        .mount_point = valid ? TINYUSB_MSC_STORAGE_MOUNT_APP : TINYUSB_MSC_STORAGE_MOUNT_USB,
    };
    ESP_RETURN_ON_ERROR(tinyusb_msc_new_storage_spiflash(&st_cfg, &s_storage), TAG, "msc storage");

    lock();
    if (valid) {
        uint64_t total = 0, free_b = 0;
        s_st.owner = DRIVE_OWNER_APP;
        s_st.fs_ok = esp_vfs_fat_info(DRIVE_BASE_PATH, &total, &free_b) == ESP_OK;
        cleanup_temp_file();
        refresh_fs_info();
    } else {
        // No usable FS: keep it away from the host too, until formatted via the web UI.
        s_st.owner = DRIVE_OWNER_APP;
        s_st.fs_ok = false;
    }
    unlock();

    ESP_RETURN_ON_ERROR(usb_dev_install(on_usb_event), TAG, "usb");
    xTaskCreate(drive_task, "drive", 4096, NULL, 5, NULL);
    return ESP_OK;
}

void drive_get_status(drive_status_t *out)
{
    lock();
    *out = s_st;
    unlock();
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
    lock();
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
    if (is_3mf(name)) {
        return DRIVE_OK;
    }
#if CONFIG_PD_ALLOW_GCODE
    if (has_suffix_ci(name, ".gcode")) {
        return DRIVE_OK;
    }
#endif
    return DRIVE_ERR_BAD_TYPE;
}

static void make_path(char *out, size_t len, const char *name)
{
    snprintf(out, len, "%s/%s", DRIVE_BASE_PATH, name);
}

static drive_result_t require_app_fs(void)
{
    if (s_st.owner != DRIVE_OWNER_APP) {
        return DRIVE_ERR_WRONG_OWNER;
    }
    return s_st.fs_ok ? DRIVE_OK : DRIVE_ERR_NO_FS;
}

// ---- file operations

drive_result_t drive_list(drive_file_t *files, size_t max, size_t *count)
{
    *count = 0;
    lock();
    drive_result_t r = require_app_fs();
    if (r == DRIVE_OK) {
        DIR *d = opendir(DRIVE_BASE_PATH);
        if (!d) {
            r = DRIVE_ERR_IO;
        } else {
            struct dirent *e;
            char path[sizeof(DRIVE_BASE_PATH) + 1 + 256];
            while ((e = readdir(d)) != NULL && *count < max) {
                if (e->d_type != DT_REG || e->d_name[0] == '.') {
                    continue;
                }
                struct stat st;
                make_path(path, sizeof(path), e->d_name);
                strlcpy(files[*count].name, e->d_name, sizeof(files[*count].name));
                files[*count].size = stat(path, &st) == 0 ? (uint32_t)st.st_size : 0;
                (*count)++;
            }
            closedir(d);
        }
    }
    unlock();
    return r;
}

drive_result_t drive_delete(const char *name)
{
    if (!name || strlen(name) > DRIVE_FILE_NAME_MAX || strchr(name, '/') || strchr(name, '\\') ||
        name[0] == '.' || name[0] == '\0') {
        return DRIVE_ERR_BAD_NAME;
    }
    lock();
    drive_result_t r = require_app_fs();
    if (r == DRIVE_OK) {
        char path[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_FILE_NAME_MAX + 1];
        make_path(path, sizeof(path), name);
        if (unlink(path) != 0) {
            r = errno == ENOENT ? DRIVE_ERR_NOT_FOUND : DRIVE_ERR_IO;
        } else {
            ESP_LOGI(TAG, "deleted '%s'", name);
            refresh_fs_info();
        }
    }
    unlock();
    return r;
}

drive_result_t drive_format(void)
{
    lock();
    if (s_st.owner != DRIVE_OWNER_APP) {
        unlock();
        return DRIVE_ERR_WRONG_OWNER;
    }
    if (s_st.upload_active || s_open_downloads > 0) {
        unlock();
        return DRIVE_ERR_BUSY;
    }
    // Formatting happens on the next boot, before USB/MSC is started (one code path).
    if (nvs_set_flag("fmt_req", 1) != ESP_OK) {
        unlock();
        return DRIVE_ERR_IO;
    }
    ESP_LOGW(TAG, "format requested - restarting");
    unlock();
    vTaskDelay(pdMS_TO_TICKS(300));
    esp_restart();
    return DRIVE_OK;
}

drive_result_t drive_download_open(const char *name, int *fd, uint32_t *size)
{
    if (!name || name[0] == '.' || strchr(name, '/') || strchr(name, '\\') || strlen(name) > DRIVE_FILE_NAME_MAX) {
        return DRIVE_ERR_BAD_NAME;
    }
    lock();
    drive_result_t r = require_app_fs();
    if (r == DRIVE_OK) {
        char path[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_FILE_NAME_MAX + 1];
        make_path(path, sizeof(path), name);
        struct stat st;
        *fd = open(path, O_RDONLY);
        if (*fd < 0 || fstat(*fd, &st) != 0) {
            if (*fd >= 0) {
                close(*fd);
            }
            r = DRIVE_ERR_NOT_FOUND;
        } else {
            *size = (uint32_t)st.st_size;
            s_open_downloads++;
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

drive_result_t drive_upload_begin(const char *name, uint32_t size, bool overwrite)
{
    drive_result_t r = drive_check_name(name);
    if (r != DRIVE_OK) {
        return r;
    }
    if (size == 0) {
        return DRIVE_ERR_INVALID_FILE;
    }
    lock();
    if ((r = require_app_fs()) != DRIVE_OK) {
        goto out;
    }
    if (s_st.upload_active) {
        r = DRIVE_ERR_BUSY;
        goto out;
    }
    char path[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_FILE_NAME_MAX + 1];
    make_path(path, sizeof(path), name);
    struct stat st;
    if (stat(path, &st) == 0 && !overwrite) {
        r = DRIVE_ERR_EXISTS;
        goto out;
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
    s_st.upload_active = true;
    strlcpy(s_st.upload_name, name, sizeof(s_st.upload_name));
    s_st.upload_received = 0;
    s_st.upload_total = size;
    ESP_LOGI(TAG, "upload start '%s' (%lu bytes)", name, (unsigned long)size);
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

drive_result_t drive_upload_finish(void)
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
        char path[sizeof(DRIVE_BASE_PATH) + 1 + DRIVE_FILE_NAME_MAX + 1];
        make_path(path, sizeof(path), s_st.upload_name);
        struct stat st;
        const bool exists = stat(path, &st) == 0;
        if (exists && !s_up_overwrite) {
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
            r = DRIVE_ERR_IO;
        }
    }
    if (r == DRIVE_OK) {
        ESP_LOGI(TAG, "upload ok '%s'", s_st.upload_name);
    } else {
        ESP_LOGW(TAG, "upload failed '%s': %s", s_st.upload_name, drive_result_str(r));
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
    lock();
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
