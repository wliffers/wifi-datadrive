// USB device layer: TinyUSB install, descriptors, optional CDC log port,
// "1200 baud touch" to enter download mode, soft re-plug.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** attached = host configured the device (true) or device was detached (false). */
typedef void (*usb_dev_event_cb_t)(bool attached);

/** Install TinyUSB. The MSC driver/storage must already be created. */
esp_err_t usb_dev_install(usb_dev_event_cb_t cb);

bool usb_dev_connected(void);
bool usb_dev_suspended(void);

/** Simulate unplug/replug so the host re-enumerates (like re-inserting a USB stick). */
void usb_dev_replug(void);

/**
 * Wait until the TinyUSB task has processed all work queued so far
 * (including deferred MSC sector writes). Returns false on timeout.
 */
bool usb_dev_barrier(uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif
