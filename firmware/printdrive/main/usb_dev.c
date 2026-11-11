#include "usb_dev.h"

#include <stdlib.h>
#include <string.h>
#include "applog.h"
#include "board.h"
#include "drive.h"
#include "device/usbd_pvt.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#if CONFIG_TINYUSB_CDC_ENABLED
#include "tinyusb_cdc_acm.h"
#endif

static const char *TAG = "usb_dev";

static usb_dev_event_cb_t s_cb;
static SemaphoreHandle_t s_barrier;

// ---------------------------------------------------------------- descriptors
// Default device descriptor and strings come from esp_tinyusb (strings set in
// sdkconfig.defaults). Only the configuration descriptor is our own so that we can
// declare 500 mA (Wi-Fi peaks are well above the default 100 mA) and no remote wakeup.
#define EP_CDC_NOTIF 0x81
#define EP_CDC_OUT   0x02
#define EP_CDC_IN    0x82
#define EP_MSC_OUT   0x03
#define EP_MSC_IN    0x83

#if CONFIG_TINYUSB_CDC_ENABLED
enum { ITF_CDC = 0, ITF_CDC_DATA, ITF_MSC, ITF_TOTAL };
#define STRID_CDC 4
#define STRID_MSC 5
#define CFG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN + TUD_MSC_DESC_LEN)
#else
enum { ITF_MSC = 0, ITF_TOTAL };
#define STRID_MSC 4
#define CFG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_MSC_DESC_LEN)
#endif

static const uint8_t s_fs_config[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_TOTAL, 0, CFG_TOTAL_LEN, 0, 500),
#if CONFIG_TINYUSB_CDC_ENABLED
    TUD_CDC_DESCRIPTOR(ITF_CDC, STRID_CDC, EP_CDC_NOTIF, 8, EP_CDC_OUT, EP_CDC_IN, 64),
#endif
    TUD_MSC_DESCRIPTOR(ITF_MSC, STRID_MSC, EP_MSC_OUT, EP_MSC_IN, 64),
};

// ---------------------------------------------------------------- events
static void on_tusb_event(tinyusb_event_t *event, void *arg)
{
    if (!s_cb) {
        return;
    }
    if (event->id == TINYUSB_EVENT_ATTACHED) {
        s_cb(true);
    } else if (event->id == TINYUSB_EVENT_DETACHED) {
        s_cb(false);
    }
}

// Barrier with sequence numbers: a late completion of an earlier (timed out) barrier
// can never be mistaken for the current one. Only called from one task at a time
// (drive.c holds its lock), deferred functions run in FIFO order in the TinyUSB task.
static volatile uint32_t s_barrier_done;
static uint32_t s_barrier_seq;

static void barrier_fn(void *param)
{
    s_barrier_done = (uint32_t)(uintptr_t)param;
    xSemaphoreGive(s_barrier);
}

bool usb_dev_barrier(uint32_t timeout_ms)
{
    const uint32_t my = ++s_barrier_seq;
    const TickType_t start = xTaskGetTickCount(), limit = pdMS_TO_TICKS(timeout_ms);
    usbd_defer_func(barrier_fn, (void *)(uintptr_t)my, false);
    while (s_barrier_done != my) {
        const TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= limit || xSemaphoreTake(s_barrier, limit - elapsed) != pdTRUE) {
            return s_barrier_done == my;
        }
    }
    return true;
}

bool usb_dev_connected(void)
{
    return tud_mounted();
}

bool usb_dev_suspended(void)
{
    return tud_suspended();
}

void usb_dev_replug(void)
{
    ESP_LOGI(TAG, "soft re-plug");
    tud_disconnect();
    vTaskDelay(pdMS_TO_TICKS(600));
    tud_connect();
}

static void usb_shutdown_handler(void)
{
    // Let the host see a clean disconnect before the chip resets.
    tud_disconnect();
    esp_rom_delay_us(50 * 1000);
}

// ---------------------------------------------------------------- CDC log port
#if CONFIG_TINYUSB_CDC_ENABLED
static StreamBufferHandle_t s_log_stream;
static SemaphoreHandle_t s_log_mutex;       // stream buffers allow only one writer at a time
static volatile uint32_t s_line_baud;
static volatile bool s_touch_requested;
static volatile bool s_send_backlog;

static void log_sink(const char *s, size_t n)
{
    // Lines from the CDC driver itself (e.g. "flush failed") would feed back forever.
    if (!s_log_stream || strstr(s, "tusb_cdc_acm") != NULL) {
        return;
    }
    if (xSemaphoreTake(s_log_mutex, 0) == pdTRUE) {
        xStreamBufferSend(s_log_stream, s, n, 0);   // drop if full, never block
        xSemaphoreGive(s_log_mutex);
    }
}

static void on_line_coding(int itf, cdcacm_event_t *event)
{
    s_line_baud = event->line_coding_changed_data.p_line_coding->bit_rate;
}

static void on_line_state(int itf, cdcacm_event_t *event)
{
    // Arduino-style "1200 baud touch": open at 1200 baud, then drop DTR.
    if (!event->line_state_changed_data.dtr && s_line_baud == 1200) {
        s_touch_requested = true;
    } else if (event->line_state_changed_data.dtr) {
        s_send_backlog = true;   // a terminal was opened: send the buffered boot log first
    }
}

static void on_rx(int itf, cdcacm_event_t *event)
{
    uint8_t buf[64];
    size_t n = 0;
    tinyusb_cdcacm_read(itf, buf, sizeof(buf), &n);   // input is ignored
}

static void log_task(void *arg)
{
    char buf[256];
    while (true) {
        if (s_touch_requested) {
            s_touch_requested = false;
            const drive_result_t r = drive_prepare_restart(false);
            if (r == DRIVE_OK) {
                ESP_LOGW(TAG, "1200 baud touch -> download mode");
                vTaskDelay(pdMS_TO_TICKS(100));
                board_restart_to_download_mode();
            }
            ESP_LOGW(TAG, "1200 baud touch ignored: %s", drive_result_str(r));
        }
        if (s_send_backlog && tud_cdc_n_connected(TINYUSB_CDC_ACM_0)) {
            s_send_backlog = false;
            char *backlog = malloc(8193);
            if (backlog) {
                const size_t len = applog_read(backlog, 8193);
                xStreamBufferReset(s_log_stream);   // its content is part of the backlog
                for (size_t off = 0; off < len; off += 256) {
                    const size_t chunk = len - off < 256 ? len - off : 256;
                    tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, (const uint8_t *)backlog + off, chunk);
                    tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, pdMS_TO_TICKS(50));
                }
                free(backlog);
            }
        }
        const size_t n = xStreamBufferReceive(s_log_stream, buf, sizeof(buf), pdMS_TO_TICKS(200));
        if (n && tud_cdc_n_connected(TINYUSB_CDC_ACM_0)) {
            tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, (const uint8_t *)buf, n);
            tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, pdMS_TO_TICKS(20));
        }
    }
}

static esp_err_t cdc_init(void)
{
    const tinyusb_config_cdcacm_t acm = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = on_rx,
        .callback_line_state_changed = on_line_state,
        .callback_line_coding_changed = on_line_coding,
    };
    ESP_RETURN_ON_ERROR(tinyusb_cdcacm_init(&acm), TAG, "cdc init");
    s_log_stream = xStreamBufferCreate(4096, 1);
    s_log_mutex = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_log_stream && s_log_mutex, ESP_ERR_NO_MEM, TAG, "log stream");
    applog_set_sink(log_sink);
    xTaskCreate(log_task, "usb_log", 3072, NULL, 2, NULL);
    return ESP_OK;
}
#endif

// ---------------------------------------------------------------- install
esp_err_t usb_dev_install(usb_dev_event_cb_t cb)
{
    s_cb = cb;
    s_barrier = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_barrier, ESP_ERR_NO_MEM, TAG, "barrier");

    tinyusb_config_t cfg = TINYUSB_DEFAULT_CONFIG(on_tusb_event, NULL);
    cfg.descriptor.full_speed_config = s_fs_config;
    ESP_RETURN_ON_ERROR(tinyusb_driver_install(&cfg), TAG, "tinyusb install");

#if CONFIG_TINYUSB_CDC_ENABLED
    ESP_RETURN_ON_ERROR(cdc_init(), TAG, "cdc");
#endif
    esp_register_shutdown_handler(usb_shutdown_handler);
#if CONFIG_TINYUSB_CDC_ENABLED
    ESP_LOGI(TAG, "TinyUSB installed (MSC + CDC log)");
#else
    ESP_LOGI(TAG, "TinyUSB installed (MSC only)");
#endif
    return ESP_OK;
}
