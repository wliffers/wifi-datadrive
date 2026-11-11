// Status screen layout (160 x 80):
//   y  0..15  header bar, colour = state: BOOT / USB / BEREIT / UPLOAD / WLAN / FEHLER
//   y 19      address line  (IP, or setup access point SSID)
//   y 29      second line   (hostname.local, or access point password)
//   y 40      storage line  + bar at y 50
//   y 58      host / upload line
//   y 70      message line
//
// BOOT button: short press = toggle USB <-> upload mode (second press within 4 s
// confirms taking the drive from a host that has not ejected it);
// hold 5 s = restart into download (flash) mode.
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "board.h"
#include "display.h"
#include "drive.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pd_wifi.h"
#include "sdkconfig.h"
#include "ui.h"

static const char *TAG = "ui";

static char s_msg[32];
static bool s_msg_error;
static int64_t s_msg_until_ms;
static int64_t s_confirm_until_ms;     // pending "press again to force"

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

void ui_notify(const char *msg, bool error)
{
    strlcpy(s_msg, msg, sizeof(s_msg));
    s_msg_error = error;
    s_msg_until_ms = now_ms() + 6000;
}

static void header(const char *title, uint16_t bg)
{
    display_rect(0, 0, DISPLAY_W, 16, bg);
    display_text(3, 1, title, DISPLAY_WHITE, bg, 2);
}

esp_err_t ui_init(void)
{
    esp_err_t err = display_init();
    if (err == ESP_OK) {
        display_fill(DISPLAY_BLACK);
        header("BOOT", DISPLAY_GREY);
        display_text(3, 24, "WiFi PrintDrive", DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 36, "startet ...", DISPLAY_GREY, DISPLAY_BLACK, 1);
        display_flush();
    }
    board_led_set(40, 40, 40, 1);
    return err;
}

void ui_show_fatal(const char *msg)
{
    display_fill(DISPLAY_BLACK);
    header("FEHLER", DISPLAY_RED);
    display_text(3, 24, msg, DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 60, "Log: serielle Konsole", DISPLAY_GREY, DISPLAY_BLACK, 1);
    display_flush();
    board_led_set(255, 0, 0, 4);
}

static void fmt_size(char *out, size_t len, uint64_t bytes)
{
    if (bytes >= 1024ULL * 1024 * 1024) {
        snprintf(out, len, "%.1fGB", bytes / (1024.0 * 1024.0 * 1024.0));
    } else if (bytes >= 1024 * 1024) {
        snprintf(out, len, "%.1fMB", bytes / (1024.0 * 1024.0));
    } else {
        snprintf(out, len, "%lukB", (unsigned long)(bytes / 1024));
    }
}

static void handle_button(void)
{
    static int64_t pressed_since = 0;
    static bool long_done = false;
    const int64_t t = now_ms();

    if (board_button_pressed()) {
        if (pressed_since == 0) {
            pressed_since = t;
        } else if (!long_done && t - pressed_since > 5000) {
            long_done = true;
            display_fill(DISPLAY_BLACK);
            header("FLASH", DISPLAY_RED);
            display_text(3, 24, "Download-Modus", DISPLAY_WHITE, DISPLAY_BLACK, 1);
            display_text(3, 36, "Zum Beenden: aus-", DISPLAY_GREY, DISPLAY_BLACK, 1);
            display_text(3, 46, "und einstecken", DISPLAY_GREY, DISPLAY_BLACK, 1);
            display_flush();
            vTaskDelay(pdMS_TO_TICKS(300));
            drive_restart_to_download();
        }
        return;
    }
    if (pressed_since == 0) {
        return;
    }
    const int64_t held = t - pressed_since;
    pressed_since = 0;
    if (long_done || held < 50) {
        long_done = false;
        return;
    }

    drive_status_t d;
    drive_get_status(&d);
    drive_result_t r;
    if (d.owner == DRIVE_OWNER_USB) {
        const bool force = t < s_confirm_until_ms;
        r = drive_set_owner(DRIVE_OWNER_APP, force);
        if (r == DRIVE_ERR_HOST_ACTIVE) {
            s_confirm_until_ms = t + 4000;
            ui_notify("Host aktiv! Nochmal=OK", true);
            return;
        }
    } else {
        r = drive_set_owner(DRIVE_OWNER_USB, false);
    }
    s_confirm_until_ms = 0;
    if (r == DRIVE_OK) {
        ui_notify(d.owner == DRIVE_OWNER_USB ? "Upload-Modus" : "USB bereitgestellt", false);
    } else if (r == DRIVE_ERR_HOST_WRITING) {
        ui_notify("Host schreibt - warten", true);
    } else if (r == DRIVE_ERR_BUSY) {
        ui_notify("Up-/Download läuft", true);
    } else {
        ui_notify("Umschalten fehlgeschl.", true);
    }
    ESP_LOGI(TAG, "button: switch result %d", r);
}

static void render(const drive_status_t *d, const pd_wifi_status_t *w)
{
    char line[DRIVE_NAME_MAX + 16], a[12], b[12];
    uint16_t colour;
    const char *title;

    if (d->owner == DRIVE_OWNER_NONE || !d->fs_ok) {
        title = "FEHLER";
        colour = DISPLAY_RED;
    } else if (d->upload_active) {
        title = "UPLOAD";
        colour = DISPLAY_ORANGE;
    } else if (d->owner == DRIVE_OWNER_USB) {
        title = "USB";
        colour = DISPLAY_GREEN;
    } else if (!w->sta_connected && w->ap_active) {
        title = "WLAN";
        colour = DISPLAY_CYAN;
    } else {
        title = "BEREIT";
        colour = DISPLAY_BLUE;
    }

    display_fill(DISPLAY_BLACK);
    header(title, colour);
    // Wi-Fi indicator in the header
    const char *wtag = w->sta_connected ? "WLAN" : (w->ap_active ? "AP" : "----");
    display_text(DISPLAY_W - display_text_width(wtag, 1) - 3, 4, wtag, DISPLAY_WHITE, colour, 1);

    // Address lines
    if (w->sta_connected) {
        display_text(3, 19, w->sta_ip, DISPLAY_YELLOW, DISPLAY_BLACK, 1);
        snprintf(line, sizeof(line), "%s.local", w->hostname);
        display_text(3, 29, line, DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else if (w->ap_active) {
        display_text(3, 19, w->ap_ssid, DISPLAY_CYAN, DISPLAY_BLACK, 1);
        snprintf(line, sizeof(line), "PW %s", w->ap_pass);
        display_text(3, 29, line, DISPLAY_CYAN, DISPLAY_BLACK, 1);
    } else {
        display_text(3, 19, w->sta_configured ? "WLAN verbinde..." : "WLAN nicht konfig.", DISPLAY_GREY,
                     DISPLAY_BLACK, 1);
    }

    // Storage
    if (d->needs_format) {
        display_text(3, 40, "Kein Dateisystem!", DISPLAY_RED, DISPLAY_BLACK, 1);
        display_text(3, 50, "Im Web formatieren", DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else if (d->total_bytes) {
        fmt_size(a, sizeof(a), d->free_bytes);
        fmt_size(b, sizeof(b), d->total_bytes);
        snprintf(line, sizeof(line), "%s %s / %s", d->on_sd ? "SD" : "Frei", a, b);
        display_text(3, 40, line, DISPLAY_WHITE, DISPLAY_BLACK, 1);
        const int used_w = (int)((DISPLAY_W - 6) * (d->total_bytes - d->free_bytes) / d->total_bytes);
        display_rect(3, 50, DISPLAY_W - 6, 5, DISPLAY_GREY);
        display_rect(3, 50, used_w, 5, d->free_bytes < d->total_bytes / 10 ? DISPLAY_RED : DISPLAY_GREEN);
    }

    // Host / upload line
    if (d->upload_active && d->upload_total) {
        const int pct = (int)(100ULL * d->upload_received / d->upload_total);
        snprintf(line, sizeof(line), "%3d%% %s", pct, d->upload_name);
        display_text(3, 59, line, DISPLAY_ORANGE, DISPLAY_BLACK, 1);
        display_rect(3, 67, (DISPLAY_W - 6) * pct / 100, 2, DISPLAY_ORANGE);
    } else {
        const char *host;
        if (!d->usb_connected) {
            host = "Kein USB-Host";
        } else if (d->owner != DRIVE_OWNER_USB) {
            host = d->host_ejected ? "Host: ausgeworfen" : "Host: kein Medium";
        } else if (d->host_write_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = "Host schreibt...";
        } else if (d->host_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = "Host liest...";
        } else if (d->usb_suspended) {
            host = "Host: Standby";
        } else {
            host = d->host_writable ? "Host: verbunden" : "Host: nur lesen";
        }
        display_text(3, 59, host, DISPLAY_WHITE, DISPLAY_BLACK, 1);
    }

    // Message line
    if (now_ms() < s_msg_until_ms) {
        display_text(3, 70, s_msg, s_msg_error ? DISPLAY_RED : DISPLAY_GREEN, DISPLAY_BLACK, 1);
    } else {
        snprintf(line, sizeof(line), "%lu Datei(en)", (unsigned long)d->file_count);
        display_text(3, 70, line, DISPLAY_GREY, DISPLAY_BLACK, 1);
    }
    display_flush();

    // LED in state colour, dim
    const uint8_t r = (colour >> 8) & 0xF8, g = (colour >> 3) & 0xFC, bl = (colour << 3) & 0xF8;
    board_led_set(r, g, bl, 1);
}

static void ui_task(void *arg)
{
    drive_status_t d;
    pd_wifi_status_t w;
    int tick = 0;
    while (true) {
        handle_button();
        if (++tick >= 5) {   // redraw every 250 ms, button polled every 50 ms
            tick = 0;
            drive_get_status(&d);
            pd_wifi_get_status(&w);
            render(&d, &w);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

void ui_start(void)
{
    xTaskCreate(ui_task, "ui", 4096, NULL, 3, NULL);
}
