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

#ifdef __cplusplus
}
#endif
