#include "applog.h"

#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#define APPLOG_SIZE 8192

static char s_buf[APPLOG_SIZE];
static size_t s_head;      // next write position
static bool s_wrapped;
static vprintf_like_t s_prev;
static volatile applog_sink_t s_sink;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static int applog_vprintf(const char *fmt, va_list args)
{
    char line[256];
    va_list copy;
    va_copy(copy, args);
    int n = vsnprintf(line, sizeof(line), fmt, copy);
    va_end(copy);
    if (n > 0) {
        size_t len = (size_t)n < sizeof(line) ? (size_t)n : sizeof(line) - 1;
        taskENTER_CRITICAL(&s_mux);
        for (size_t i = 0; i < len; i++) {
            s_buf[s_head++] = line[i];
            if (s_head == APPLOG_SIZE) {
                s_head = 0;
                s_wrapped = true;
            }
        }
        taskEXIT_CRITICAL(&s_mux);
        applog_sink_t sink = s_sink;
        if (sink) {
            sink(line, len);
        }
    }
    return s_prev ? s_prev(fmt, args) : n;
}

void applog_set_sink(applog_sink_t sink)
{
    s_sink = sink;
}

void applog_init(void)
{
    if (!s_prev) {
        s_prev = esp_log_set_vprintf(applog_vprintf);
    }
}

size_t applog_read(char *out, size_t out_len)
{
    if (!out || out_len == 0) {
        return 0;
    }
    size_t n = 0;
    taskENTER_CRITICAL(&s_mux);
    const size_t start = s_wrapped ? s_head : 0;
    const size_t avail = s_wrapped ? APPLOG_SIZE : s_head;
    // If out is too small, keep the newest part.
    const size_t skip = avail > out_len - 1 ? avail - (out_len - 1) : 0;
    for (size_t i = skip; i < avail; i++) {
        out[n++] = s_buf[(start + i) % APPLOG_SIZE];
    }
    taskEXIT_CRITICAL(&s_mux);
    out[n] = '\0';
    return n;
}
