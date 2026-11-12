// Captive-portal DNS responder of the setup access point (private to pd_wifi).
//
// Answers every A query on <ap-ip>:53 with the access point's own address, so that the
// connectivity checks of phones and laptops land on the web server, which redirects them to the
// setup page. Other query types get an empty answer (NOERROR, no records). Runs as a raw lwIP
// UDP PCB in the TCP/IP thread: no task, no socket, no buffer while idle.
#pragma once

#include <stdint.h>
#include "esp_err.h"

/** Start answering on ip (IPv4, network byte order) port 53. No-op if already running. */
esp_err_t pd_dns_start(uint32_t ip);

/** Stop answering. No-op if not running. */
void pd_dns_stop(void);
