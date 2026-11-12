// Minimal framebuffer display layer for the T-Dongle-S3 ST7735 LCD (160x80).
// No LVGL: a 5x7 bitmap font and a few primitives are enough for a status screen.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DISPLAY_W 160
#define DISPLAY_H 80

/** RGB565 colour from 8-bit components. */
#define DISPLAY_RGB(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))

#define DISPLAY_BLACK   DISPLAY_RGB(0, 0, 0)
#define DISPLAY_WHITE   DISPLAY_RGB(255, 255, 255)
#define DISPLAY_GREY    DISPLAY_RGB(110, 110, 110)
#define DISPLAY_RED     DISPLAY_RGB(220, 30, 30)
#define DISPLAY_GREEN   DISPLAY_RGB(30, 170, 60)
#define DISPLAY_BLUE    DISPLAY_RGB(30, 90, 220)
#define DISPLAY_ORANGE  DISPLAY_RGB(240, 140, 0)
#define DISPLAY_YELLOW  DISPLAY_RGB(240, 220, 0)
#define DISPLAY_CYAN    DISPLAY_RGB(0, 190, 210)

/** Character cell size at scale 1 (5x7 glyph + 1px spacing). */
#define DISPLAY_CHAR_W 6
#define DISPLAY_CHAR_H 8

/** Initialise SPI bus, panel and backlight. Safe to call once. */
esp_err_t display_init(void);

void display_backlight(bool on);

/** Drawing goes into a RAM framebuffer; call display_flush() to send it. */
void display_fill(uint16_t color);
void display_rect(int x, int y, int w, int h, uint16_t color);

/**
 * Draw UTF-8 text (ASCII plus German umlauts). Background cells are filled with bg.
 * Text is clipped at the right screen edge. Returns the x position after the text.
 */
int display_text(int x, int y, const char *utf8, uint16_t fg, uint16_t bg, int scale);

/** Width in pixels that display_text() would use. */
int display_text_width(const char *utf8, int scale);

/** Copy a w x h block of native RGB565 pixels (row-major, e.g. DISPLAY_RGB()) into the framebuffer, clipped. */
void display_blit(int x, int y, int w, int h, const uint16_t *rgb565);

/** Transfer only a rectangle of the framebuffer (at most 2048 pixels, else the whole frame). */
esp_err_t display_flush_rect(int x, int y, int w, int h);

/** Transfer the framebuffer to the panel (blocks until the DMA transfer is done). */
esp_err_t display_flush(void);

#ifdef __cplusplus
}
#endif
