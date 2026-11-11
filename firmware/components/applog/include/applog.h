// Keeps the most recent ESP_LOG output in a RAM ring buffer so it can be read via
// the web interface (useful when no serial console is available, e.g. at the printer).
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Hook into esp_log. Output still goes to the normal console as well. */
void applog_init(void);

/**
 * Copy the buffered log (oldest first) into out, NUL terminated.
 * Returns the number of bytes copied (without the terminator).
 */
size_t applog_read(char *out, size_t out_len);

/** Optional extra output (e.g. USB CDC). Called from the logging task; must not block or log. */
typedef void (*applog_sink_t)(const char *text, size_t len);
void applog_set_sink(applog_sink_t sink);

#ifdef __cplusplus
}
#endif
