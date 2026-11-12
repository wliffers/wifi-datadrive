// LCD status screen, RGB status LED and BOOT button handling.
#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Show the BOOT screen right away (before the slow parts of the start-up). */
esp_err_t ui_init(void);

/** Show a fatal start-up error on the LCD. */
void ui_show_fatal(const char *msg);

/** Start the UI task (status screen, LED, button). */
void ui_start(void);

/** Short message shown in the bottom line for a few seconds. */
void ui_notify(const char *msg, bool error);

/** Display language: "de" (default) or "en". Loaded from NVS by ui_init(). */
const char *ui_get_lang(void);

/** Set the display language ("de" or "en", else ESP_ERR_INVALID_ARG) and store it in NVS.
 *  Applied with the next redraw. */
esp_err_t ui_set_lang(const char *lang);

#ifdef __cplusplus
}
#endif
