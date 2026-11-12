#include "display.h"

#include <string.h>
#include "board.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_st7735.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "display";

extern const uint8_t display_font5x7[];   // 256 glyphs x 5 columns, CP437 layout

static esp_lcd_panel_handle_t s_panel;
static uint16_t *s_fb;                    // byte-swapped RGB565, DISPLAY_W * DISPLAY_H
static SemaphoreHandle_t s_flush_done;

static bool on_color_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *edata, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_flush_done, &woken);
    return woken == pdTRUE;
}

void display_backlight(bool on)
{
    gpio_set_level(BOARD_LCD_PIN_BL, on ? BOARD_LCD_BL_ON_LEVEL : !BOARD_LCD_BL_ON_LEVEL);
}

esp_err_t display_init(void)
{
    if (s_panel) {
        return ESP_OK;
    }

    const gpio_config_t bl = {
        .pin_bit_mask = 1ULL << BOARD_LCD_PIN_BL,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&bl), TAG, "backlight gpio");
    display_backlight(false);

    s_flush_done = xSemaphoreCreateBinary();
    s_fb = heap_caps_calloc(DISPLAY_W * DISPLAY_H, sizeof(uint16_t), MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    ESP_RETURN_ON_FALSE(s_flush_done && s_fb, ESP_ERR_NO_MEM, TAG, "no memory for framebuffer");

    // Bus, IO and panel settings follow the vendor example examples/lcd/lcd.ino.
    const spi_bus_config_t bus = ST7735_PANEL_BUS_SPI_CONFIG(BOARD_LCD_PIN_SCLK, BOARD_LCD_PIN_MOSI,
                                                             DISPLAY_W * DISPLAY_H * sizeof(uint16_t));
    ESP_RETURN_ON_ERROR(spi_bus_initialize(BOARD_LCD_SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");

    esp_lcd_panel_io_handle_t io = NULL;
    const esp_lcd_panel_io_spi_config_t io_cfg = ST7735_PANEL_IO_SPI_CONFIG(BOARD_LCD_PIN_CS, BOARD_LCD_PIN_DC,
                                                                          on_color_trans_done, NULL);
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BOARD_LCD_SPI_HOST, &io_cfg, &io),
                        TAG, "panel io");

    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = BOARD_LCD_PIN_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR,
        .data_endian = LCD_RGB_DATA_ENDIAN_LITTLE,
        .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7735(io, &panel_cfg, &s_panel), TAG, "panel");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_invert_color(s_panel, true), TAG, "invert");
    // Landscape orientation exactly as in the vendor example.
    esp_lcd_panel_set_gap(s_panel, BOARD_LCD_GAP_X, BOARD_LCD_GAP_Y);
    esp_lcd_panel_swap_xy(s_panel, true);
    ESP_RETURN_ON_ERROR(esp_lcd_panel_mirror(s_panel, false, true), TAG, "mirror");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "disp on");

    display_fill(DISPLAY_BLACK);
    display_flush();
    display_backlight(true);
    ESP_LOGI(TAG, "LCD ready (%dx%d)", DISPLAY_W, DISPLAY_H);
    return ESP_OK;
}

static inline uint16_t swap16(uint16_t c)
{
    return (uint16_t)((c >> 8) | (c << 8));
}

void display_fill(uint16_t color)
{
    display_rect(0, 0, DISPLAY_W, DISPLAY_H, color);
}

void display_rect(int x, int y, int w, int h, uint16_t color)
{
    if (!s_fb) {
        return;
    }
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > DISPLAY_W) { w = DISPLAY_W - x; }
    if (y + h > DISPLAY_H) { h = DISPLAY_H - y; }
    if (w <= 0 || h <= 0) {
        return;
    }
    const uint16_t c = swap16(color);
    for (int row = y; row < y + h; row++) {
        uint16_t *p = &s_fb[row * DISPLAY_W + x];
        for (int i = 0; i < w; i++) {
            p[i] = c;
        }
    }
}

// Decode one UTF-8 code point and map it to a CP437 glyph index.
static uint8_t next_glyph(const char **s)
{
    const uint8_t *p = (const uint8_t *)*s;
    uint32_t cp;
    if (p[0] < 0x80) {
        cp = p[0];
        *s += 1;
    } else if ((p[0] & 0xE0) == 0xC0 && p[1]) {
        cp = ((p[0] & 0x1F) << 6) | (p[1] & 0x3F);
        *s += 2;
    } else if ((p[0] & 0xF0) == 0xE0 && p[1] && p[2]) {
        cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F);
        *s += 3;
    } else {
        *s += 1;
        return '?';
    }
    if (cp < 0x80) {
        return (uint8_t)cp;
    }
    switch (cp) {
    case 0xE4: return 0x84; // ä
    case 0xF6: return 0x94; // ö
    case 0xFC: return 0x81; // ü
    case 0xC4: return 0x8E; // Ä
    case 0xD6: return 0x99; // Ö
    case 0xDC: return 0x9A; // Ü
    case 0xDF: return 0xE1; // ß
    case 0xB0: return 0xF8; // °
    default:   return '?';
    }
}

int display_text_width(const char *utf8, int scale)
{
    int n = 0;
    while (*utf8) {
        next_glyph(&utf8);
        n++;
    }
    return n * DISPLAY_CHAR_W * scale;
}

int display_text(int x, int y, const char *utf8, uint16_t fg, uint16_t bg, int scale)
{
    if (!s_fb || !utf8) {
        return x;
    }
    if (scale < 1) {
        scale = 1;
    }
    const uint16_t cfg = swap16(fg), cbg = swap16(bg);
    while (*utf8 && x < DISPLAY_W) {
        const uint8_t *glyph = &display_font5x7[next_glyph(&utf8) * 5];
        for (int col = 0; col < DISPLAY_CHAR_W; col++) {
            const uint8_t bits = col < 5 ? glyph[col] : 0;   // 6th column = spacing
            for (int row = 0; row < DISPLAY_CHAR_H; row++) {
                const uint16_t c = (bits >> row) & 1 ? cfg : cbg;
                for (int dy = 0; dy < scale; dy++) {
                    const int py = y + row * scale + dy;
                    if (py < 0 || py >= DISPLAY_H) {
                        continue;
                    }
                    for (int dx = 0; dx < scale; dx++) {
                        const int px = x + col * scale + dx;
                        if (px >= 0 && px < DISPLAY_W) {
                            s_fb[py * DISPLAY_W + px] = c;
                        }
                    }
                }
            }
        }
        x += DISPLAY_CHAR_W * scale;
    }
    return x;
}

void display_blit(int x, int y, int w, int h, const uint16_t *rgb565)
{
    if (!s_fb || !rgb565) {
        return;
    }
    for (int row = 0; row < h; row++) {
        const int py = y + row;
        if (py < 0 || py >= DISPLAY_H) {
            continue;
        }
        for (int col = 0; col < w; col++) {
            const int px = x + col;
            if (px >= 0 && px < DISPLAY_W) {
                s_fb[py * DISPLAY_W + px] = swap16(rgb565[row * w + col]);
            }
        }
    }
}

#define FLUSH_RECT_MAX_PIXELS 2048   // 48 x 40 animation box = 1920

esp_err_t display_flush_rect(int x, int y, int w, int h)
{
    if (!s_panel) {
        return ESP_ERR_INVALID_STATE;
    }
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > DISPLAY_W) { w = DISPLAY_W - x; }
    if (y + h > DISPLAY_H) { h = DISPLAY_H - y; }
    if (w <= 0 || h <= 0) {
        return ESP_OK;
    }
    if (w * h > FLUSH_RECT_MAX_PIXELS) {
        return display_flush();
    }
    // The panel driver needs a tightly packed bitmap: copy the rectangle into a small DMA buffer.
    static uint16_t *blk;
    if (!blk) {
        blk = heap_caps_malloc(FLUSH_RECT_MAX_PIXELS * sizeof(uint16_t), MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!blk) {
            return display_flush();
        }
    }
    for (int row = 0; row < h; row++) {
        memcpy(&blk[row * w], &s_fb[(y + row) * DISPLAY_W + x], w * sizeof(uint16_t));
    }
    xSemaphoreTake(s_flush_done, 0);
    ESP_RETURN_ON_ERROR(esp_lcd_panel_draw_bitmap(s_panel, x, y, x + w, y + h, blk), TAG, "draw rect");
    if (xSemaphoreTake(s_flush_done, pdMS_TO_TICKS(200)) != pdTRUE) {
        ESP_LOGW(TAG, "flush timeout");
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

esp_err_t display_flush(void)
{
    if (!s_panel) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_flush_done, 0);   // clear a stale signal
    ESP_RETURN_ON_ERROR(esp_lcd_panel_draw_bitmap(s_panel, 0, 0, DISPLAY_W, DISPLAY_H, s_fb), TAG, "draw");
    // Wait so the caller may modify the framebuffer again right away.
    if (xSemaphoreTake(s_flush_done, pdMS_TO_TICKS(200)) != pdTRUE) {
        ESP_LOGW(TAG, "flush timeout");
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}
