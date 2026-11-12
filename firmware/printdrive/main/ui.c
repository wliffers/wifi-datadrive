// Status screen layout (160 x 80):
//   y  0..15  header bar, colour = state: BOOT / USB / BEREIT / UPLOAD / WLAN / FEHLER
//   y 19      address line  (IP, or setup access point SSID)
//   y 29      second line   (hostname.local, or access point password)
//   y 40      storage line  + bar at y 50
//   y 58      host / upload line
//   y 70      message line
//
// BOOT button (polled every 50 ms):
//   single click  = QR code screen for 20 s (another click hides it);
//   double click  = toggle USB <-> upload mode (a second double click within 4 s confirms
//                   taking the drive from a host that has not ejected it);
//   hold 5..10 s  = LCD countdown; release = restart into download (flash) mode;
//   hold 10 s     = reset the users to admin/admin (release then does nothing).
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "auth.h"
#include "board.h"
#include "display.h"
#include "drive.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pd_wifi.h"
#include "qrcode.h"
#include "sdkconfig.h"
#include "ui.h"

static const char *TAG = "ui";

static char s_msg[32];
static bool s_msg_error;
static int64_t s_msg_until_ms;
static int64_t s_confirm_until_ms;     // pending "press again to force"

#define POLL_MS          50
#define DOUBLE_MS        400     // max gap between release and the second press
#define HOLD_FLASH_MS    5000    // from here the LCD shows the countdown
#define HOLD_RESET_MS    10000
#define RESET_SHOW_MS    3000
#define QR_SHOW_MS       20000

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
        display_text(3, 24, "WiFi DataDrive", DISPLAY_WHITE, DISPLAY_BLACK, 1);
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

// ---- QR screen ----------------------------------------------------------------

#define CAPTION_CHARS    12      // caption column is 76 px wide, 6 px per character

typedef enum { OV_NONE, OV_QR, OV_HOLD, OV_RESET } overlay_t;

static overlay_t s_overlay;          // screen that replaces the status screen, if any
static int64_t s_overlay_until_ms;   // end of OV_QR / OV_RESET
static char s_qr_drawn[96];          // content of the QR currently on screen ("" = none yet)
static bool s_reset_ok;

static void draw_black(int x, int y, const char *t, int scale)
{
    display_text(x, y, t, DISPLAY_BLACK, DISPLAY_WHITE, scale);
}

// Caption text wrapped at the column width, preferably after '.', '-' or ' '.
static void draw_wrapped(int x, int y, const char *t)
{
    char buf[CAPTION_CHARS + 1];
    for (int lines = 0; *t && lines < 4; lines++, y += 10) {
        size_t n = strlen(t);
        if (n > CAPTION_CHARS) {
            n = CAPTION_CHARS;
            for (size_t i = CAPTION_CHARS; i > 3; i--) {
                if (t[i - 1] == '.' || t[i - 1] == '-' || t[i - 1] == ' ') {
                    n = i;
                    break;
                }
            }
        }
        memcpy(buf, t, n);
        buf[n] = 0;
        draw_black(x, y, buf, 1);
        t += n;
    }
}

// Wi-Fi QR syntax: the characters \ ; , " : must be escaped with a backslash.
static void wifi_escape(char *out, size_t len, const char *in)
{
    size_t o = 0;
    for (; *in && o + 2 < len; in++) {
        if (strchr("\\;,\":", *in)) {
            out[o++] = '\\';
        }
        out[o++] = *in;
    }
    out[o] = 0;
}

typedef struct {
    int size, scale;
} qr_info_t;

// Called by esp_qrcode_generate() with the finished symbol. Draws black modules into the
// white 80x80 area at the left; runs of dark modules in a row become one rectangle.
static void qr_display(esp_qrcode_handle_t qr, void *user)
{
    qr_info_t *info = user;
    const int n = esp_qrcode_get_size(qr);
    // Largest integer scale; quiet zone of 2 modules if possible, else 1, else none.
    int scale = 0;
    for (int qz = 2; qz >= 0 && scale == 0; qz--) {
        scale = DISPLAY_H / (n + 2 * qz);
    }
    if (scale < 1) {
        scale = 1;
    }
    info->size = n;
    info->scale = scale;
    const int off = (DISPLAY_H - n * scale) / 2;
    for (int y = 0; y < n; y++) {
        for (int x = 0; x < n;) {
            if (!esp_qrcode_get_module(qr, x, y)) {
                x++;
                continue;
            }
            int run = 1;
            while (x + run < n && esp_qrcode_get_module(qr, x + run, y)) {
                run++;
            }
            display_rect(off + x * scale, off + y * scale, run * scale, scale, DISPLAY_BLACK);
            x += run;
        }
    }
}

// What the QR code shows: station address wins over the setup access point's join code.
static char qr_content(const pd_wifi_status_t *w, char *text, size_t len)
{
    if (w->sta_connected && w->sta_ip[0]) {
        snprintf(text, len, "http://%s", w->sta_ip);
        return 'u';
    }
    if (w->ap_active) {
        char ssid[70], pass[40];
        wifi_escape(ssid, sizeof(ssid), w->ap_ssid);
        wifi_escape(pass, sizeof(pass), w->ap_pass);
        snprintf(text, len, "WIFI:T:WPA;S:%s;P:%s;;", ssid, pass);
        return 'w';
    }
    text[0] = 0;
    return 0;
}

// Draws the QR screen only when its content changed (the library logs every encoded text,
// which for the join code includes the password - so no re-encoding every 250 ms).
static void draw_qr_screen(const pd_wifi_status_t *w)
{
    char text[96];
    const char kind = qr_content(w, text, sizeof(text));
    if (strcmp(text, s_qr_drawn) == 0) {
        return;
    }
    strlcpy(s_qr_drawn, text, sizeof(s_qr_drawn));
    display_fill(DISPLAY_WHITE);
    if (!kind) {
        draw_black(10, 22, "Kein WLAN", 2);
        draw_black(10, 46, "Klick = schließen", 1);
        display_flush();
        return;
    }
    // ECC low: http://192.168.x.y (up to 22 bytes) fits version 2, the join code version 3-4.
    qr_info_t info = { 0 };
    esp_qrcode_config_t cfg = ESP_QRCODE_CONFIG_DEFAULT();
    cfg.display_func_with_cb = qr_display;
    cfg.user_data = &info;
    cfg.max_qrcode_version = 6;
    cfg.qrcode_ecc_level = ESP_QRCODE_ECC_LOW;
    if (esp_qrcode_generate(&cfg, text) != ESP_OK) {
        draw_black(10, 30, "QR-Fehler", 2);
    }
    const int cx = DISPLAY_H + 4;
    if (kind == 'u') {
        draw_black(cx, 6, "Adresse", 1);
        draw_wrapped(cx, 18, w->sta_ip);
        draw_black(cx, 58, "Klick:", 1);
        draw_black(cx, 68, "schließen", 1);
    } else {
        draw_black(cx, 6, "WLAN", 1);
        draw_black(cx, 16, "beitreten", 1);
        draw_wrapped(cx, 30, w->ap_ssid);
        draw_black(cx, 68, "Klick: zu", 1);
    }
    display_flush();
    ESP_LOGI(TAG, "QR %c: %d modules, scale %d px", kind, info.size, info.scale);
}

static void show_qr(void)
{
    s_overlay = OV_QR;
    s_overlay_until_ms = now_ms() + QR_SHOW_MS;
    s_qr_drawn[0] = '!';   // never equals real content: forces the first draw
    s_qr_drawn[1] = 0;
}

// ---- hold screens ---------------------------------------------------------------

static void draw_hold_screen(int64_t held)
{
    char line[32];
    const int left = (int)((HOLD_RESET_MS - held + 999) / 1000);
    display_fill(DISPLAY_BLACK);
    header("TASTE", DISPLAY_ORANGE);
    display_text(3, 21, "Loslassen:", DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 31, "Flash-Modus", DISPLAY_YELLOW, DISPLAY_BLACK, 1);
    display_text(3, 44, "Weiter halten:", DISPLAY_WHITE, DISPLAY_BLACK, 1);
    snprintf(line, sizeof(line), "Reset in %d s", left);
    display_text(3, 54, line, DISPLAY_RED, DISPLAY_BLACK, 1);
    // progress bar for the 5 s .. 10 s window
    display_rect(3, 68, DISPLAY_W - 6, 6, DISPLAY_GREY);
    display_rect(3, 68, (int)((DISPLAY_W - 6) * (held - HOLD_FLASH_MS) / (HOLD_RESET_MS - HOLD_FLASH_MS)), 6,
                 DISPLAY_RED);
    display_flush();
}

static void draw_reset_screen(void)
{
    display_fill(DISPLAY_BLACK);
    if (s_reset_ok) {
        header("RESET", DISPLAY_GREEN);
        display_text(3, 24, "Anmeldung", DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 34, "zurückgesetzt", DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 52, "admin / admin", DISPLAY_YELLOW, DISPLAY_BLACK, 1);
    } else {
        header("FEHLER", DISPLAY_RED);
        display_text(3, 30, "Reset fehlgeschlagen", DISPLAY_WHITE, DISPLAY_BLACK, 1);
    }
    display_flush();
}

// ---- button ---------------------------------------------------------------------

static void do_switch(int64_t t)
{
    drive_status_t d;
    drive_get_status(&d);
    drive_result_t r;
    if (d.owner == DRIVE_OWNER_USB) {
        const bool force = t < s_confirm_until_ms;
        r = drive_set_owner(DRIVE_OWNER_APP, force);
        if (r == DRIVE_ERR_HOST_ACTIVE) {
            s_confirm_until_ms = t + 4000;
            ui_notify("Gerät aktiv! 2x = OK", true);
            return;
        }
    } else {
        r = drive_set_owner(DRIVE_OWNER_USB, false);
    }
    s_confirm_until_ms = 0;
    if (r == DRIVE_OK) {
        ui_notify(d.owner == DRIVE_OWNER_USB ? "Upload-Modus" : "USB bereitgestellt", false);
    } else if (r == DRIVE_ERR_HOST_WRITING) {
        ui_notify("Gerät schreibt - warten", true);
    } else if (r == DRIVE_ERR_BUSY) {
        ui_notify("Up-/Download läuft", true);
    } else {
        ui_notify("Umschalten fehlgeschl.", true);
    }
    ESP_LOGI(TAG, "button: switch result %d", r);
}

static void enter_flash_mode(void)
{
    display_fill(DISPLAY_BLACK);
    header("FLASH", DISPLAY_RED);
    display_text(3, 24, "Download-Modus", DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 36, "Zum Beenden: aus-", DISPLAY_GREY, DISPLAY_BLACK, 1);
    display_text(3, 46, "und einstecken", DISPLAY_GREY, DISPLAY_BLACK, 1);
    display_flush();
    vTaskDelay(pdMS_TO_TICKS(300));
    drive_restart_to_download();
}

// Called every 50 ms. Click classification:
//   short press (< 5 s) released -> wait DOUBLE_MS for a second press;
//   second press started within DOUBLE_MS of the first release -> double click;
//   otherwise the window expires -> single click.
static void handle_button(void)
{
    // Debounce: a level is accepted once two consecutive polls (50 ms apart) agree.
    static bool raw_prev, stable;
    static int64_t pressed_at, released_at;
    static bool click_pending;      // short press waiting for a possible second one
    static bool reset_done;         // 10 s reached: the release must do nothing
    static int shown_left = -1;     // countdown value currently on the LCD
    const int64_t t = now_ms();
    const bool raw = board_button_pressed();
    const bool changed = (raw == raw_prev) && (raw != stable);
    raw_prev = raw;

    if (changed) {
        stable = raw;
        if (stable) {
            pressed_at = t;
            reset_done = false;
            shown_left = -1;
        } else if (!reset_done) {
            if (t - pressed_at >= HOLD_FLASH_MS) {
                enter_flash_mode();
            } else if (click_pending && pressed_at - released_at <= DOUBLE_MS) {
                click_pending = false;
                s_overlay = OV_NONE;      // a double click also closes the QR screen
                do_switch(t);
            } else {
                click_pending = true;
                released_at = t;
            }
        }
    }

    if (stable) {
        const int64_t held = t - pressed_at;
        if (!reset_done && held >= HOLD_RESET_MS) {
            reset_done = true;
            click_pending = false;
            s_reset_ok = (auth_reset_users() == ESP_OK);
            ESP_LOGW(TAG, "button: user reset %s", s_reset_ok ? "done" : "FAILED");
            s_overlay = OV_RESET;
            s_overlay_until_ms = t + RESET_SHOW_MS;
            draw_reset_screen();
        } else if (!reset_done && held >= HOLD_FLASH_MS) {
            click_pending = false;
            const int left = (int)((HOLD_RESET_MS - held + 999) / 1000);
            if (s_overlay != OV_HOLD || left != shown_left || held % 500 < POLL_MS) {
                shown_left = left;
                s_overlay = OV_HOLD;
                draw_hold_screen(held);
            }
        }
        return;
    }

    // Single click: acted on only after the double-click window has passed, button up.
    if (click_pending && t - released_at > DOUBLE_MS) {
        click_pending = false;
        if (s_overlay == OV_QR) {
            s_overlay = OV_NONE;
        } else if (s_overlay == OV_NONE) {
            show_qr();
        }
    }
}

static void render(const drive_status_t *d, const pd_wifi_status_t *w, bool draw)
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

    // LED in state colour, dim; kept up to date while an overlay owns the LCD
    const uint8_t r = (colour >> 8) & 0xF8, g = (colour >> 3) & 0xFC, bl = (colour << 3) & 0xF8;
    board_led_set(r, g, bl, 1);
    if (!draw) {
        return;
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
            host = "Kein USB-Gerät";
        } else if (d->owner != DRIVE_OWNER_USB) {
            host = d->host_ejected ? "Gerät: ausgeworfen" : "Gerät: kein Laufwerk";
        } else if (d->host_write_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = "Gerät schreibt...";
        } else if (d->host_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = "Gerät liest...";
        } else if (d->usb_suspended) {
            host = "Gerät: Standby";
        } else {
            host = d->host_writable ? "Gerät: verbunden" : "Gerät: nur lesen";
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
}

static void ui_task(void *arg)
{
    drive_status_t d;
    pd_wifi_status_t w;
    int tick = 0;
    while (true) {
        handle_button();
        if ((s_overlay == OV_QR || s_overlay == OV_RESET) && now_ms() >= s_overlay_until_ms) {
            s_overlay = OV_NONE;
        }
        if (++tick >= 5) {   // redraw every 250 ms, button polled every 50 ms
            tick = 0;
            drive_get_status(&d);
            pd_wifi_get_status(&w);
            render(&d, &w, s_overlay == OV_NONE);
            if (s_overlay == OV_QR) {
                draw_qr_screen(&w);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void ui_start(void)
{
    xTaskCreate(ui_task, "ui", 6144, NULL, 3, NULL);
}
