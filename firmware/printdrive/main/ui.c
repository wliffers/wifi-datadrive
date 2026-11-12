// Status screen layout (160 x 80):
//   y  0..15  header bar, colour = state: BOOT / USB / BEREIT / UPLOAD / WLAN / FEHLER / GESPERRT
//             (all texts German or English, see s_txt; the language follows the web UI)
//   y 19      address line  (IP, or setup access point SSID)      } left column x 3..105,
//   y 29      second line   (hostname.local, or access point password) } max 17 characters
//   y 40      storage line  + bar at y 50 (x 3..105)                }
//   x 108..155, y 18..57: animation box (anim.c), nothing else is drawn there
//   y 59      host / upload line (full width)
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
#include "anim.h"
#include "auth.h"
#include "board.h"
#include "display.h"
#include "drive.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "pd_wifi.h"
#include "qrcode.h"
#include "sdkconfig.h"
#include "ui.h"

static const char *TAG = "ui";

// ---- display language (follows the web UI, NVS "pd_ui"/"lang") --------------------
// Width limits (5x7 font, 6 px per character): left column 17 characters, bottom lines 26,
// QR captions 12, titles at scale 2 at most 8 characters (the Wi-Fi tag sits at the right).
// Every English text is no longer than the German one where that matters, or within the limit.

typedef enum {
    T_STARTING, T_LOG_SERIAL,
    T_TITLE_ERROR, T_TITLE_LOCKED, T_TITLE_WLAN, T_TITLE_READY, T_TITLE_BUTTON, T_TAG_WLAN,
    T_QR_NO_WLAN, T_QR_CLICK_CLOSE, T_QR_ERROR, T_QR_ADDRESS, T_QR_CLICK, T_QR_CLOSE,
    T_QR_JOIN1, T_QR_JOIN2, T_QR_CLICK_ZU,
    T_HOLD_RELEASE, T_HOLD_FLASH, T_HOLD_KEEP,
    T_RESET_LOGIN, T_RESET_DONE, T_RESET_FAILED,
    T_N_KEY_DAMAGED, T_N_UNLOCK_FIRST, T_N_HOST_ACTIVE, T_N_UPLOAD_MODE, T_N_USB_PROVIDED,
    T_N_HOST_WRITING, T_N_TRANSFER, T_N_SWITCH_FAILED,
    T_FLASH_MODE, T_FLASH_EXIT1, T_FLASH_EXIT2,
    T_WLAN_CONNECTING, T_WLAN_NOT_SET,
    T_KEY_AREA, T_DAMAGED, T_UNLOCK1, T_UNLOCK2, T_NO_FS, T_FORMAT_WEB,
    T_HOST_NONE, T_HOST_EJECTED, T_HOST_NO_DRIVE, T_HOST_WRITING, T_HOST_READING, T_HOST_STANDBY,
    T_HOST_CONNECTED, T_HOST_READONLY,
    T_WEB_ERASE, T_ENCRYPTED, T_FILES,
    T_COUNT
} txt_t;

static const char *const s_txt[T_COUNT][2] = {
    [T_STARTING] = {"startet ...", "starting ..."},
    [T_LOG_SERIAL] = {"Log: serielle Konsole", "Log: serial console"},
    [T_TITLE_ERROR] = {"FEHLER", "ERROR"},
    [T_TITLE_LOCKED] = {"GESPERRT", "LOCKED"},
    [T_TITLE_WLAN] = {"WLAN", "WIFI"},
    [T_TITLE_READY] = {"BEREIT", "READY"},
    [T_TITLE_BUTTON] = {"TASTE", "BUTTON"},
    [T_TAG_WLAN] = {"WLAN", "WiFi"},
    [T_QR_NO_WLAN] = {"Kein WLAN", "No WiFi"},
    [T_QR_CLICK_CLOSE] = {"Klick = schließen", "Click = close"},
    [T_QR_ERROR] = {"QR-Fehler", "QR error"},
    [T_QR_ADDRESS] = {"Adresse", "Address"},
    [T_QR_CLICK] = {"Klick:", "Click:"},
    [T_QR_CLOSE] = {"schließen", "close"},
    [T_QR_JOIN1] = {"WLAN", "Join"},
    [T_QR_JOIN2] = {"beitreten", "WiFi"},
    [T_QR_CLICK_ZU] = {"Klick: zu", "Click: close"},
    [T_HOLD_RELEASE] = {"Loslassen:", "Release:"},
    [T_HOLD_FLASH] = {"Flash-Modus", "Flash mode"},
    [T_HOLD_KEEP] = {"Weiter halten:", "Keep holding:"},
    [T_RESET_LOGIN] = {"Anmeldung", "Login"},
    [T_RESET_DONE] = {"zurückgesetzt", "reset"},
    [T_RESET_FAILED] = {"Reset fehlgeschlagen", "Reset failed"},
    [T_N_KEY_DAMAGED] = {"Schlüsselbereich defekt", "Key area damaged"},
    [T_N_UNLOCK_FIRST] = {"Erst im Browser entsperren", "Unlock in browser first"},
    [T_N_HOST_ACTIVE] = {"Gerät aktiv! 2x = OK", "Host active! 2x = OK"},
    [T_N_UPLOAD_MODE] = {"Upload-Modus", "Upload mode"},
    [T_N_USB_PROVIDED] = {"USB bereitgestellt", "Drive on USB"},
    [T_N_HOST_WRITING] = {"Gerät schreibt - warten", "Host writing - wait"},
    [T_N_TRANSFER] = {"Up-/Download läuft", "Transfer running"},
    [T_N_SWITCH_FAILED] = {"Umschalten fehlgeschl.", "Switch failed"},
    [T_FLASH_MODE] = {"Download-Modus", "Download mode"},
    [T_FLASH_EXIT1] = {"Zum Beenden: aus-", "To exit: unplug"},
    [T_FLASH_EXIT2] = {"und einstecken", "and plug in again"},
    [T_WLAN_CONNECTING] = {"WLAN verbinde...", "WiFi connecting.."},
    [T_WLAN_NOT_SET] = {"WLAN nicht konfig.", "No WiFi set up"},
    [T_KEY_AREA] = {"Schlüsselbereich", "Key area"},
    [T_DAMAGED] = {"beschädigt", "damaged"},
    [T_UNLOCK1] = {"Im Browser", "Unlock in"},
    [T_UNLOCK2] = {"entsperren", "web browser"},
    [T_NO_FS] = {"Kein Dateisystem!", "No file system!"},
    [T_FORMAT_WEB] = {"Im Web formatieren", "Format in web UI"},
    [T_HOST_NONE] = {"Kein USB-Gerät", "No USB host"},
    [T_HOST_EJECTED] = {"Gerät: ausgeworfen", "Host: ejected"},
    [T_HOST_NO_DRIVE] = {"Gerät: kein Laufwerk", "Host: no drive"},
    [T_HOST_WRITING] = {"Gerät schreibt...", "Host writing..."},
    [T_HOST_READING] = {"Gerät liest...", "Host reading..."},
    [T_HOST_STANDBY] = {"Gerät: Standby", "Host: standby"},
    [T_HOST_CONNECTED] = {"Gerät: verbunden", "Host: connected"},
    [T_HOST_READONLY] = {"Gerät: nur lesen", "Host: read-only"},
    [T_WEB_ERASE] = {"Im Web: Löschen", "Web UI: erase"},
    [T_ENCRYPTED] = {"verschlüsselt", "encrypted"},
    [T_FILES] = {"Datei(en)", "file(s)"},
};

static volatile uint8_t s_lang;      // 0 = de (default), 1 = en

static const char *tr(txt_t id)
{
    return s_txt[id][s_lang ? 1 : 0];
}

static void load_lang(void)
{
    nvs_handle_t h;
    char v[4] = "";
    size_t len = sizeof(v);
    if (nvs_open("pd_ui", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_str(h, "lang", v, &len) != ESP_OK) {
            v[0] = 0;
        }
        nvs_close(h);
    }
    s_lang = strcmp(v, "en") == 0 ? 1 : 0;
}

const char *ui_get_lang(void)
{
    return s_lang ? "en" : "de";
}

esp_err_t ui_set_lang(const char *lang)
{
    uint8_t l;
    if (lang && strcmp(lang, "de") == 0) {
        l = 0;
    } else if (lang && strcmp(lang, "en") == 0) {
        l = 1;
    } else {
        return ESP_ERR_INVALID_ARG;
    }
    if (l == s_lang) {
        return ESP_OK;              // the web UI calls this on every switch: no needless NVS write
    }
    s_lang = l;                     // the LCD follows with its next redraw (250 ms)
    nvs_handle_t h;
    esp_err_t err = nvs_open("pd_ui", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "lang", lang);
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "display language not saved: %s", esp_err_to_name(err));
    }
    return err;
}

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

// Black text on light backgrounds, white on dark ones (RGB565 -> rough luminance).
static uint16_t header_fg(uint16_t bg)
{
    const int r = (bg >> 8) & 0xF8, g = (bg >> 3) & 0xFC, b = (bg << 3) & 0xF8;
    return (r * 3 + g * 6 + b) / 10 >= 120 ? DISPLAY_BLACK : DISPLAY_WHITE;
}

static void header(const char *title, uint16_t bg)
{
    display_rect(0, 0, DISPLAY_W, 16, bg);
    display_text(3, 1, title, header_fg(bg), bg, 2);
}

// Accent colour of the mascot per animation state (anim/manifest.json): header bar and LED.
static const uint16_t s_accent[ANIM_COUNT] = {
    [ANIM_NONE] = DISPLAY_GREY,
    [ANIM_READY] = DISPLAY_RGB(0x40, 0xE5, 0xD1),
    [ANIM_UPLOAD] = DISPLAY_RGB(0x4E, 0xA5, 0xFF),
    [ANIM_DOWNLOAD] = DISPLAY_RGB(0xB5, 0x8A, 0xFF),
    [ANIM_PROCESSING] = DISPLAY_RGB(0xFF, 0xB4, 0x54),
    [ANIM_SUCCESS] = DISPLAY_RGB(0x73, 0xE9, 0x9C),
    [ANIM_CONNECTING] = DISPLAY_RGB(0x40, 0xE5, 0xD1),
    [ANIM_DISCONNECTED] = DISPLAY_RGB(0xFF, 0xE0, 0x82),
    [ANIM_LOCKED] = DISPLAY_RGB(0xB5, 0x8A, 0xFF),
    [ANIM_ERROR] = DISPLAY_RGB(0xFF, 0x5D, 0x68),
};

esp_err_t ui_init(void)
{
    load_lang();
    esp_err_t err = display_init();
    if (err == ESP_OK) {
        display_fill(DISPLAY_BLACK);
        header("BOOT", DISPLAY_GREY);
        display_text(3, 24, "WiFi DataDrive", DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 36, tr(T_STARTING), DISPLAY_GREY, DISPLAY_BLACK, 1);
        display_flush();
    }
    board_led_set(40, 40, 40, 1);
    return err;
}

void ui_show_fatal(const char *msg)
{
    display_fill(DISPLAY_BLACK);
    header(tr(T_TITLE_ERROR), DISPLAY_RED);
    display_text(3, 24, msg, DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 60, tr(T_LOG_SERIAL), DISPLAY_GREY, DISPLAY_BLACK, 1);
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
        // Bounded: the access point name is "DataDrive-XXXX" and its password 10 characters.
        snprintf(text, len, "WIFI:T:WPA;S:%.40s;P:%.20s;;", ssid, pass);
        return 'w';
    }
    text[0] = 0;
    return 0;
}

// Draws the QR screen only when its content changed (the library logs every encoded text,
// which for the join code includes the password - so no re-encoding every 250 ms).
static void draw_qr_screen(const pd_wifi_status_t *w)
{
    static uint8_t drawn_lang;
    char text[96];
    const char kind = qr_content(w, text, sizeof(text));
    if (strcmp(text, s_qr_drawn) == 0 && drawn_lang == s_lang) {
        return;
    }
    strlcpy(s_qr_drawn, text, sizeof(s_qr_drawn));
    drawn_lang = s_lang;
    display_fill(DISPLAY_WHITE);
    if (!kind) {
        draw_black(10, 22, tr(T_QR_NO_WLAN), 2);
        draw_black(10, 46, tr(T_QR_CLICK_CLOSE), 1);
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
        draw_black(10, 30, tr(T_QR_ERROR), 2);
    }
    const int cx = DISPLAY_H + 4;
    if (kind == 'u') {
        draw_black(cx, 6, tr(T_QR_ADDRESS), 1);
        draw_wrapped(cx, 18, w->sta_ip);
        draw_black(cx, 58, tr(T_QR_CLICK), 1);
        draw_black(cx, 68, tr(T_QR_CLOSE), 1);
    } else {
        draw_black(cx, 6, tr(T_QR_JOIN1), 1);
        draw_black(cx, 16, tr(T_QR_JOIN2), 1);
        draw_wrapped(cx, 30, w->ap_ssid);
        draw_black(cx, 68, tr(T_QR_CLICK_ZU), 1);
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
    header(tr(T_TITLE_BUTTON), DISPLAY_ORANGE);
    display_text(3, 21, tr(T_HOLD_RELEASE), DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 31, tr(T_HOLD_FLASH), DISPLAY_YELLOW, DISPLAY_BLACK, 1);
    display_text(3, 44, tr(T_HOLD_KEEP), DISPLAY_WHITE, DISPLAY_BLACK, 1);
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
        display_text(3, 24, tr(T_RESET_LOGIN), DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 34, tr(T_RESET_DONE), DISPLAY_WHITE, DISPLAY_BLACK, 1);
        display_text(3, 52, "admin / admin", DISPLAY_YELLOW, DISPLAY_BLACK, 1);
    } else {
        header(tr(T_TITLE_ERROR), DISPLAY_RED);
        display_text(3, 30, tr(T_RESET_FAILED), DISPLAY_WHITE, DISPLAY_BLACK, 1);
    }
    display_flush();
}

// ---- button ---------------------------------------------------------------------

static void do_switch(int64_t t)
{
    drive_status_t d;
    drive_get_status(&d);
    drive_result_t r;
    if (d.crypt == DRIVE_CRYPT_DAMAGED) {
        s_confirm_until_ms = 0;
        ui_notify(tr(T_N_KEY_DAMAGED), true);
        return;
    }
    if (d.crypt == DRIVE_CRYPT_LOCKED) {
        s_confirm_until_ms = 0;
        ui_notify(tr(T_N_UNLOCK_FIRST), true);
        return;
    }
    if (d.owner == DRIVE_OWNER_USB) {
        const bool force = t < s_confirm_until_ms;
        r = drive_set_owner(DRIVE_OWNER_APP, force);
        if (r == DRIVE_ERR_HOST_ACTIVE) {
            s_confirm_until_ms = t + 4000;
            ui_notify(tr(T_N_HOST_ACTIVE), true);
            return;
        }
    } else {
        r = drive_set_owner(DRIVE_OWNER_USB, false);
    }
    s_confirm_until_ms = 0;
    if (r == DRIVE_OK) {
        ui_notify(tr(d.owner == DRIVE_OWNER_USB ? T_N_UPLOAD_MODE : T_N_USB_PROVIDED), false);
    } else if (r == DRIVE_ERR_HOST_WRITING) {
        ui_notify(tr(T_N_HOST_WRITING), true);
    } else if (r == DRIVE_ERR_BUSY) {
        ui_notify(tr(T_N_TRANSFER), true);
    } else {
        ui_notify(tr(T_N_SWITCH_FAILED), true);
    }
    ESP_LOGI(TAG, "button: switch result %d", r);
}

static void enter_flash_mode(void)
{
    display_fill(DISPLAY_BLACK);
    header("FLASH", DISPLAY_RED);
    display_text(3, 24, tr(T_FLASH_MODE), DISPLAY_WHITE, DISPLAY_BLACK, 1);
    display_text(3, 36, tr(T_FLASH_EXIT1), DISPLAY_GREY, DISPLAY_BLACK, 1);
    display_text(3, 46, tr(T_FLASH_EXIT2), DISPLAY_GREY, DISPLAY_BLACK, 1);
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

// Size text for the 104 px wide left column: the unit of the first number is dropped when both
// share it ("58.2/59.5GB").
static void fmt_storage(char *out, size_t len, const drive_status_t *d)
{
    char a[12], b[12];
    fmt_size(a, sizeof(a), d->free_bytes);
    fmt_size(b, sizeof(b), d->total_bytes);
    const size_t la = strlen(a), lb = strlen(b);
    if (la >= 2 && lb >= 2 && strcmp(a + la - 2, b + lb - 2) == 0) {
        a[la - 2] = 0;
    }
    snprintf(out, len, "%s %s/%s", d->on_sd ? "SD" : "Fl", a, b);
}

// Which animation belongs to the current state (same priority as the header title).
static anim_id_t pick_anim(const drive_status_t *d, const pd_wifi_status_t *w, int64_t t)
{
    static drive_crypt_state_t prev_crypt = DRIVE_CRYPT_OFF;
    static bool unlock_playing;
    if (prev_crypt == DRIVE_CRYPT_LOCKED && d->crypt != DRIVE_CRYPT_LOCKED) {
        unlock_playing = true;
    }
    prev_crypt = d->crypt;
    if (d->crypt == DRIVE_CRYPT_LOCKED) {
        unlock_playing = false;
        return ANIM_LOCKED;
    }
    if (unlock_playing) {
        if (!anim_finished(t)) {
            return ANIM_SUCCESS;
        }
        unlock_playing = false;
    }
    if (d->crypt == DRIVE_CRYPT_DAMAGED) {
        unlock_playing = false;
        return ANIM_ERROR;
    }
    if (d->owner == DRIVE_OWNER_NONE || !d->fs_ok) {
        return ANIM_ERROR;
    }
    if (d->upload_active) {
        return ANIM_UPLOAD;
    }
    if (anim_is_busy()) {
        return ANIM_PROCESSING;
    }
    if (d->owner == DRIVE_OWNER_USB) {
        const bool io = d->host_write_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS ||
                        d->host_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS;
        return io ? ANIM_DOWNLOAD : ANIM_READY;
    }
    if (!w->sta_connected) {
        return (w->ap_active || w->sta_configured) ? ANIM_CONNECTING : ANIM_DISCONNECTED;
    }
    return ANIM_READY;
}

static void render(const drive_status_t *d, const pd_wifi_status_t *w, bool draw)
{
    char line[DRIVE_NAME_MAX + 16];
    uint16_t colour;
    const char *title;
    const bool locked = d->crypt == DRIVE_CRYPT_LOCKED;
    const bool damaged = d->crypt == DRIVE_CRYPT_DAMAGED;

    const int64_t t = now_ms();
    const anim_id_t an = pick_anim(d, w, t);
    colour = s_accent[an];
    if (locked) {                          // before the error case: fs_ok is false while locked
        title = tr(T_TITLE_LOCKED);
    } else if (damaged || d->owner == DRIVE_OWNER_NONE || !d->fs_ok) {
        title = tr(T_TITLE_ERROR);
    } else if (d->upload_active) {
        title = "UPLOAD";
    } else if (d->owner == DRIVE_OWNER_USB) {
        title = "USB";
    } else if (!w->sta_connected && w->ap_active) {
        title = tr(T_TITLE_WLAN);
    } else {
        title = tr(T_TITLE_READY);
    }

    // LED in state colour, dim; kept up to date while an overlay owns the LCD
    const uint8_t r = (colour >> 8) & 0xF8, g = (colour >> 3) & 0xFC, bl = (colour << 3) & 0xF8;
    board_led_set(r, g, bl, 1);
    anim_set(an, t);
    if (!draw) {
        return;
    }
    anim_tick(t);

    display_fill(DISPLAY_BLACK);
    header(title, colour);
    // Wi-Fi indicator in the header
    const char *wtag = w->sta_connected ? tr(T_TAG_WLAN) : (w->ap_active ? "AP" : "----");
    display_text(DISPLAY_W - display_text_width(wtag, 1) - 3, 4, wtag,
                 header_fg(colour), colour, 1);

    // Address lines: left column only (x 3..105 = 17 characters), the animation box starts at x 108
    if (w->sta_connected) {
        display_text(3, 19, w->sta_ip, DISPLAY_YELLOW, DISPLAY_BLACK, 1);
        snprintf(line, sizeof(line), "%.11s.local", w->hostname);
        display_text(3, 29, line, DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else if (w->ap_active) {
        snprintf(line, sizeof(line), "%.17s", w->ap_ssid);
        display_text(3, 19, line, DISPLAY_CYAN, DISPLAY_BLACK, 1);
        // "PW " + password (10 characters normally); without the prefix if it would not fit
        snprintf(line, sizeof(line), strlen(w->ap_pass) <= 14 ? "PW %s" : "%.17s", w->ap_pass);
        display_text(3, 29, line, DISPLAY_CYAN, DISPLAY_BLACK, 1);
    } else {
        display_text(3, 19, tr(w->sta_configured ? T_WLAN_CONNECTING : T_WLAN_NOT_SET), DISPLAY_GREY,
                     DISPLAY_BLACK, 1);
    }

    // Storage
    if (damaged) {
        display_text(3, 40, tr(T_KEY_AREA), DISPLAY_RED, DISPLAY_BLACK, 1);
        display_text(3, 50, tr(T_DAMAGED), DISPLAY_RED, DISPLAY_BLACK, 1);
    } else if (locked) {
        display_text(3, 40, tr(T_UNLOCK1), DISPLAY_YELLOW, DISPLAY_BLACK, 1);
        display_text(3, 50, tr(T_UNLOCK2), DISPLAY_YELLOW, DISPLAY_BLACK, 1);
    } else if (d->needs_format) {
        display_text(3, 40, tr(T_NO_FS), DISPLAY_RED, DISPLAY_BLACK, 1);
        display_text(3, 50, tr(T_FORMAT_WEB), DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else if (d->total_bytes) {
        fmt_storage(line, sizeof(line), d);
        display_text(3, 40, line, DISPLAY_WHITE, DISPLAY_BLACK, 1);
        const int bar_w = 102;                                  // x 3..104
        const int used_w = (int)(bar_w * (d->total_bytes - d->free_bytes) / d->total_bytes);
        display_rect(3, 50, bar_w, 5, DISPLAY_GREY);
        display_rect(3, 50, used_w, 5, d->free_bytes < d->total_bytes / 10 ? DISPLAY_RED : DISPLAY_GREEN);
    }

    // Host / upload line (below the box, full width)
    if (d->upload_active && d->upload_total) {
        const int pct = (int)(100ULL * d->upload_received / d->upload_total);
        snprintf(line, sizeof(line), "%3d%% %s", pct, d->upload_name);
        display_text(3, 59, line, DISPLAY_ORANGE, DISPLAY_BLACK, 1);
        display_rect(3, 67, (DISPLAY_W - 6) * pct / 100, 2, DISPLAY_ORANGE);
    } else if (locked || damaged) {
        // nothing: the lines above already say it
    } else {
        const char *host;
        if (!d->usb_connected) {
            host = tr(T_HOST_NONE);
        } else if (d->owner != DRIVE_OWNER_USB) {
            host = tr(d->host_ejected ? T_HOST_EJECTED : T_HOST_NO_DRIVE);
        } else if (d->host_write_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = tr(T_HOST_WRITING);
        } else if (d->host_idle_ms < CONFIG_PD_HOST_WRITE_GUARD_MS) {
            host = tr(T_HOST_READING);
        } else if (d->usb_suspended) {
            host = tr(T_HOST_STANDBY);
        } else {
            host = tr(d->host_writable ? T_HOST_CONNECTED : T_HOST_READONLY);
        }
        display_text(3, 59, host, DISPLAY_WHITE, DISPLAY_BLACK, 1);
    }

    // Message line
    if (now_ms() < s_msg_until_ms) {
        display_text(3, 70, s_msg, s_msg_error ? DISPLAY_RED : DISPLAY_GREEN, DISPLAY_BLACK, 1);
    } else if (damaged) {
        display_text(3, 70, tr(T_WEB_ERASE), DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else if (locked) {
        display_text(3, 70, tr(T_ENCRYPTED), DISPLAY_GREY, DISPLAY_BLACK, 1);
    } else {
        snprintf(line, sizeof(line), "%lu %s", (unsigned long)d->file_count, tr(T_FILES));
        display_text(3, 70, line, DISPLAY_GREY, DISPLAY_BLACK, 1);
    }
    anim_draw();                 // after the text: the box is the last thing drawn
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
        } else if (s_overlay == OV_NONE && anim_tick(now_ms())) {
            // animation frame due between two full redraws: send only the box (3.8 kB)
            anim_draw();
            display_flush_rect(ANIM_X, ANIM_Y, ANIM_W, ANIM_H);
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void ui_start(void)
{
    xTaskCreate(ui_task, "ui", 6144, NULL, 3, NULL);
}
