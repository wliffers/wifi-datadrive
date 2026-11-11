// Board support for the LILYGO T-Dongle-S3 (LCD variant, 16 MB flash, no PSRAM).
//
// All pin numbers below are taken from the official LILYGO repository
// (https://github.com/Xinyuan-LilyGO/T-Dongle-S3):
//   - docs/en/t-dongle-s3/REAMDE.MD  (pin map, LED type, button)
//   - examples/lcd/lcd.ino           (LCD SPI pins, backlight polarity, gap/orientation)
// Do not change them without checking the schematic of your board revision.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// ---- LCD: ST7735, 0.96", 160x80, SPI ----
#define BOARD_LCD_SPI_HOST      SPI2_HOST
#define BOARD_LCD_PIN_MOSI      3
#define BOARD_LCD_PIN_SCLK      5
#define BOARD_LCD_PIN_CS        4
#define BOARD_LCD_PIN_DC        2
#define BOARD_LCD_PIN_RST       1
#define BOARD_LCD_PIN_BL        38
#define BOARD_LCD_BL_ON_LEVEL   0       // backlight is active LOW
#define BOARD_LCD_WIDTH         160     // landscape
#define BOARD_LCD_HEIGHT        80
#define BOARD_LCD_GAP_X         1       // controller RAM offset in landscape mode
#define BOARD_LCD_GAP_Y         26

// ---- RGB LED: APA102 (colour order BGR) ----
#define BOARD_LED_PIN_DATA      40
#define BOARD_LED_PIN_CLK       39

// ---- BOOT button (also download-mode strap) ----
#define BOARD_BUTTON_PIN        0       // LOW when pressed

// ---- microSD (SDMMC 4-bit) - NOT used before phase 5 ----
#define BOARD_SD_PIN_D0         14
#define BOARD_SD_PIN_D1         17
#define BOARD_SD_PIN_D2         21
#define BOARD_SD_PIN_D3         18
#define BOARD_SD_PIN_CLK        12
#define BOARD_SD_PIN_CMD        16

/** Configure button and LED GPIOs and switch the RGB LED off. */
esp_err_t board_init(void);

/** true while the BOOT button is held down. */
bool board_button_pressed(void);

/**
 * Set the APA102 RGB LED. brightness: 0..31 (APA102 global brightness).
 * Not thread safe - call from one task only.
 */
void board_led_set(uint8_t r, uint8_t g, uint8_t b, uint8_t brightness);

/** Size of the attached SPI flash in bytes (read from the flash chip). */
uint32_t board_flash_size(void);

/**
 * Restart into the ROM serial download mode (for flashing without pressing BOOT).
 * Shutdown handlers registered with esp_register_shutdown_handler() run first.
 * If the ROM does not come up in download mode, unplugging and re-plugging the
 * dongle always returns to normal boot.
 */
void board_restart_to_download_mode(void) __attribute__((noreturn));

#ifdef __cplusplus
}
#endif
