#pragma once

/**
 * Optional HTTPS for the web UI: setting and key material.
 *
 * The device is its own small certificate authority:
 *  - a root CA (ECDSA P-256, valid until 2049) with X.509 name constraints, so that even a
 *    stolen CA key can only vouch for "<hostname>", "*.local" and private IPv4 addresses;
 *    this CA is what a user installs on iPhone/Windows to get rid of the warning,
 *  - a server certificate (ECDSA P-256, signed by the CA, SAN = <hostname>.local, <hostname>,
 *    the current IP and the setup-AP IP, EKU serverAuth, validity <= 825 days as Apple
 *    requires). It is renewed automatically at boot when it is about to expire (reference
 *    date: firmware build date or the clock, whichever is later); the CA stays the same.
 * Everything is stored in NVS (namespace "pd_tls"). The setting takes effect after a restart.
 *
 * All functions are called from app_main before the web server starts or from the web server
 * task (one request at a time); they are not thread-safe.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define PD_TLS_FP_LEN 96   // "AB:CD:...": 32 bytes as hex with colons + NUL

typedef struct {
    bool enabled;                    // stored setting
    bool active;                     // HTTPS server runs in this boot
    bool restart_pending;            // setting or certificate changed since boot
    char cert_sha256[PD_TLS_FP_LEN];    // server certificate ("" if none yet)
    char ca_sha256[PD_TLS_FP_LEN];      // root CA ("" if none yet)
    char cert_expires[11];           // "YYYY-MM-DD" ("" if none yet)
} pd_tls_info_t;

/** Read the setting. When HTTPS is enabled: create missing key material, renew an expiring
 *  server certificate and load the server credentials for pd_tls_server_credentials(). */
esp_err_t pd_tls_init(void);

/** Stored setting (takes effect after a restart). */
bool pd_tls_enabled(void);

/** HTTPS server is running in this boot (set by the web server). */
bool pd_tls_active(void);
void pd_tls_set_active(bool active);

/** Store the setting. Enabling creates the key material now if there is none, so the
 *  fingerprints can be shown before the restart. */
esp_err_t pd_tls_set_enabled(bool on);

/** New server key and certificate (current IP/hostname), same CA. Effective after a restart. */
esp_err_t pd_tls_renew_cert(void);

/** New CA and new server certificate (CA must be installed again on clients). Effective after
 *  a restart. */
esp_err_t pd_tls_regenerate(void);

/** Server certificate and private key (DER) loaded by pd_tls_init(). False if not loaded. The
 *  buffers stay valid until pd_tls_release_server_credentials(). */
bool pd_tls_server_credentials(const uint8_t **crt, size_t *crt_len, const uint8_t **key, size_t *key_len);

/** Wipe and free the loaded server credentials (the HTTPS server keeps its own copy). */
void pd_tls_release_server_credentials(void);

void pd_tls_get_info(pd_tls_info_t *out);

/** Malloc'ed copy of the CA certificate (DER); caller frees. ESP_ERR_NOT_FOUND if none. */
esp_err_t pd_tls_read_ca(uint8_t **der, size_t *len);
