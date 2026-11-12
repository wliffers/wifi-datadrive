#include "tls.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/asn1.h"
#include "mbedtls/ecp.h"
#include "mbedtls/oid.h"
#include "mbedtls/pk.h"
#include "mbedtls/platform.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include "mbedtls/x509_crt.h"
#include "nvs.h"
#include "pd_wifi.h"
#include "sdkconfig.h"

static const char *TAG = "tls";

#define NVS_NS        "pd_tls"
#define KEY_ENABLED   "https"
#define KEY_CA_KEY    "ca_key"
#define KEY_CA_CRT    "ca_crt"
#define KEY_SRV_KEY   "srv_key"
#define KEY_SRV_CRT   "srv_crt"
#define KEY_SRV_EXP   "srv_exp"   // u32: last valid day of the server certificate (days since 1970)

#define SRV_VALID_DAYS 820   // Apple: TLS server certificates must not be valid longer than 825 days
#define SRV_RENEW_DAYS 60    // renew at boot when the certificate expires within this many days
#define CA_NOT_AFTER   "20491231235959"   // last date still encoded as UTCTime

#define KEY_DER_MAX 200      // P-256 private key DER is about 121 bytes
#define CRT_DER_MAX 1024     // our certificates are about 450..650 bytes

static bool s_enabled;
static bool s_active;
static bool s_changed;       // key material regenerated since boot
static pd_tls_info_t s_info;
static uint8_t *s_srv_crt, *s_srv_key;
static size_t s_srv_crt_len, s_srv_key_len;

// ------------------------------------------------------------------ helpers

static int rng(void *ctx, unsigned char *out, size_t len)
{
    (void)ctx;
    esp_fill_random(out, len);   // hardware RNG (true random while Wi-Fi/RF is on)
    return 0;
}

/** Days since 1970-01-01 for a civil date (H. Hinnant's algorithm). */
static int32_t days_from_civil(int y, int m, int d)
{
    y -= m <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const int yoe = y - era * 400;
    const int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static void civil_from_days(int32_t z, int *y, int *m, int *d)
{
    z += 719468;
    const int era = (z >= 0 ? z : z - 146096) / 146097;
    const int doe = z - era * 146097;
    const int yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const int mp = (5 * doy + 2) / 153;
    *d = doy - (153 * mp + 2) / 5 + 1;
    *m = mp < 10 ? mp + 3 : mp - 9;
    *y = yoe + era * 400 + (*m <= 2);
}

/** Firmware build date ("Nov 12 2026" in the app description) as a day number. */
static int32_t build_day(void)
{
    static const char months[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    const char *date = esp_app_get_description()->date;
    int m = 1;
    for (int i = 0; i < 12; i++) {
        if (strncmp(date, months + 3 * i, 3) == 0) {
            m = i + 1;
        }
    }
    const int d = atoi(date + 4), y = atoi(date + 7);
    return (y >= 2024 && d >= 1 && d <= 31) ? days_from_civil(y, m, d) : days_from_civil(2026, 1, 1);
}

/** "Today" as well as the device knows it: the clock if it was set by a browser (plausible
 *  range only), else the firmware build date. The device has no RTC. */
static int32_t ref_day(void)
{
    const int32_t b = build_day();
    const time_t now = time(NULL);
    const int32_t n = (int32_t)(now / 86400);
    return (n > b && n < b + 20 * 365) ? n : b;
}

static void x509_time(char out[15], int32_t day, bool end_of_day)
{
    int y, m, d;
    civil_from_days(day, &y, &m, &d);
    snprintf(out, 15, "%04d%02d%02d%s", y, m, d, end_of_day ? "235959" : "000000");
}

static void fingerprint(const uint8_t *der, size_t len, char out[PD_TLS_FP_LEN])
{
    uint8_t h[32];
    mbedtls_sha256(der, len, h, 0);
    for (int i = 0; i < 32; i++) {
        snprintf(out + 3 * i, 4, i < 31 ? "%02X:" : "%02X", h[i]);
    }
}

/** Parse "a.b.c.d" and accept it only if it is in 10/8, 172.16/12 or 192.168/16 (the ranges
 *  the CA's name constraints permit). */
static bool private_ip(const char *s, uint8_t ip[4])
{
    // Hand-written: sscanf would pull ~13 KB of newlib scanf code into the image.
    if (!s) {
        return false;
    }
    for (int i = 0; i < 4; i++) {
        unsigned v = 0;
        int digits = 0;
        while (*s >= '0' && *s <= '9' && digits < 4) {
            v = v * 10 + (unsigned)(*s++ - '0');
            digits++;
        }
        if (digits == 0 || v > 255 || *s != (i < 3 ? '.' : '\0')) {
            return false;
        }
        ip[i] = (uint8_t)v;
        s++;
    }
    return ip[0] == 10 || (ip[0] == 172 && (ip[1] & 0xF0) == 16) || (ip[0] == 192 && ip[1] == 168);
}

static bool valid_hostname(const char *h)
{
    const size_t n = strlen(h);
    if (n == 0 || n > 31) {
        return false;
    }
    for (const char *p = h; *p; p++) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '-')) {
            return false;
        }
    }
    return true;
}

static void ca_name(char *out, size_t len)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, len, "CN=DataDrive %02X%02X%02X Root CA,O=WiFi DataDrive", mac[3], mac[4], mac[5]);
}

/**
 * NameConstraints (RFC 5280 4.2.1.10), permitted subtrees only: dNSName "local" (any *.local
 * name), dNSName <hostname>, and the private IPv4 ranges. With this, the CA cannot be abused
 * for public web sites even if its key is read out of the flash.
 */
static size_t name_constraints(uint8_t *out, size_t size, const char *host)
{
    static const uint8_t ip_subtrees[] = {
        0x30, 0x0A, 0x87, 0x08, 10, 0, 0, 0, 0xFF, 0x00, 0x00, 0x00,     // 10.0.0.0/8
        0x30, 0x0A, 0x87, 0x08, 172, 16, 0, 0, 0xFF, 0xF0, 0x00, 0x00,   // 172.16.0.0/12
        0x30, 0x0A, 0x87, 0x08, 192, 168, 0, 0, 0xFF, 0xFF, 0x00, 0x00,  // 192.168.0.0/16
    };
    uint8_t sub[128];
    size_t n = 0;
    const char *names[2] = {"local", host};
    for (int i = 0; i < 2; i++) {
        const size_t l = names[i] ? strlen(names[i]) : 0;
        if (l == 0 || l > 40) {
            continue;
        }
        sub[n++] = 0x30;              // GeneralSubtree
        sub[n++] = (uint8_t)(l + 2);
        sub[n++] = 0x82;              // [2] dNSName
        sub[n++] = (uint8_t)l;
        memcpy(sub + n, names[i], l);
        n += l;
    }
    memcpy(sub + n, ip_subtrees, sizeof(ip_subtrees));
    n += sizeof(ip_subtrees);
    if (n + 4 > size || n + 2 > 127) {
        return 0;
    }
    out[0] = 0x30;                    // NameConstraints
    out[1] = (uint8_t)(n + 2);
    out[2] = 0xA0;                    // [0] permittedSubtrees
    out[3] = (uint8_t)n;
    memcpy(out + 4, sub, n);
    return n + 4;
}

typedef struct {
    mbedtls_pk_context *subject_key;
    mbedtls_pk_context *issuer_key;
    const char *subject;
    const char *issuer;
    int32_t from_day;
    int32_t to_day;         // only used for the server certificate
    bool ca;
    const mbedtls_x509_san_list *san;
    const uint8_t *nc;
    size_t nc_len;
} cert_params_t;

/** Write a certificate as DER at the end of buf; der and len point into buf. 0 or mbedtls error. */
static int write_cert(const cert_params_t *p, uint8_t *buf, size_t size, const uint8_t **der, size_t *len)
{
    static const char oid_name_constraints[] = MBEDTLS_OID_NAME_CONSTRAINTS;
    mbedtls_x509write_cert c;
    mbedtls_x509write_crt_init(&c);
    mbedtls_x509write_crt_set_version(&c, MBEDTLS_X509_CRT_VERSION_3);
    mbedtls_x509write_crt_set_md_alg(&c, MBEDTLS_MD_SHA256);
    mbedtls_x509write_crt_set_subject_key(&c, p->subject_key);
    mbedtls_x509write_crt_set_issuer_key(&c, p->issuer_key);

    uint8_t serial[16];
    esp_fill_random(serial, sizeof(serial));
    serial[0] = (serial[0] & 0x7F) | 0x40;   // positive, full length
    char nb[15], na[15];
    x509_time(nb, p->from_day, false);
    if (p->ca) {
        strlcpy(na, CA_NOT_AFTER, sizeof(na));
    } else {
        x509_time(na, p->to_day, true);
    }

    int r;
    if ((r = mbedtls_x509write_crt_set_serial_raw(&c, serial, sizeof(serial))) != 0 ||
        (r = mbedtls_x509write_crt_set_subject_name(&c, p->subject)) != 0 ||
        (r = mbedtls_x509write_crt_set_issuer_name(&c, p->issuer)) != 0 ||
        (r = mbedtls_x509write_crt_set_validity(&c, nb, na)) != 0 ||
        (r = mbedtls_x509write_crt_set_basic_constraints(&c, p->ca, p->ca ? 0 : -1)) != 0 ||
        (r = mbedtls_x509write_crt_set_subject_key_identifier(&c)) != 0 ||
        (r = mbedtls_x509write_crt_set_key_usage(&c, p->ca ? (MBEDTLS_X509_KU_KEY_CERT_SIGN | MBEDTLS_X509_KU_CRL_SIGN)
                                                         : MBEDTLS_X509_KU_DIGITAL_SIGNATURE)) != 0) {
        goto out;
    }
    if (p->ca) {
        r = mbedtls_x509write_crt_set_extension(&c, oid_name_constraints, MBEDTLS_OID_SIZE(MBEDTLS_OID_NAME_CONSTRAINTS),
                                                1, p->nc, p->nc_len);
    } else {
        const mbedtls_asn1_sequence eku = {
            .buf = {.tag = MBEDTLS_ASN1_OID, .len = MBEDTLS_OID_SIZE(MBEDTLS_OID_SERVER_AUTH),
                    .p = (unsigned char *)MBEDTLS_OID_SERVER_AUTH},
            .next = NULL,
        };
        if ((r = mbedtls_x509write_crt_set_authority_key_identifier(&c)) == 0 &&
            (r = mbedtls_x509write_crt_set_ext_key_usage(&c, &eku)) == 0) {
            r = mbedtls_x509write_crt_set_subject_alternative_name(&c, p->san);
        }
    }
    if (r != 0) {
        goto out;
    }
    r = mbedtls_x509write_crt_der(&c, buf, size, rng, NULL);
    if (r > 0) {
        *der = buf + size - r;
        *len = (size_t)r;
        r = 0;
    } else if (r == 0) {
        r = -1;
    }
out:
    mbedtls_x509write_crt_free(&c);
    return r;
}

static int gen_key(mbedtls_pk_context *pk)
{
    const int r = mbedtls_pk_setup(pk, mbedtls_pk_info_from_type(MBEDTLS_PK_ECKEY));
    return r != 0 ? r : mbedtls_ecp_gen_key(MBEDTLS_ECP_DP_SECP256R1, mbedtls_pk_ec(*pk), rng, NULL);
}

/** Read a blob into a new heap buffer. */
static esp_err_t get_blob(nvs_handle_t h, const char *key, uint8_t **out, size_t *len)
{
    *out = NULL;
    *len = 0;
    esp_err_t err = nvs_get_blob(h, key, NULL, len);
    if (err != ESP_OK) {
        return err;
    }
    if (*len == 0 || *len > CRT_DER_MAX) {
        return ESP_ERR_INVALID_SIZE;
    }
    *out = malloc(*len);
    if (!*out) {
        return ESP_ERR_NO_MEM;
    }
    err = nvs_get_blob(h, key, *out, len);
    if (err != ESP_OK) {
        free(*out);
        *out = NULL;
    }
    return err;
}

static void free_secret(uint8_t *p, size_t len)
{
    if (p) {
        mbedtls_platform_zeroize(p, len);
        free(p);
    }
}

/** Refresh fingerprints and expiry date from NVS (no key material is kept). */
static void load_info(void)
{
    s_info.cert_sha256[0] = s_info.ca_sha256[0] = s_info.cert_expires[0] = '\0';
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    uint8_t *der;
    size_t len;
    if (get_blob(h, KEY_SRV_CRT, &der, &len) == ESP_OK) {
        fingerprint(der, len, s_info.cert_sha256);
        free(der);
    }
    if (get_blob(h, KEY_CA_CRT, &der, &len) == ESP_OK) {
        fingerprint(der, len, s_info.ca_sha256);
        free(der);
    }
    uint32_t exp;
    if (nvs_get_u32(h, KEY_SRV_EXP, &exp) == ESP_OK) {
        int y, m, d;
        civil_from_days((int32_t)exp, &y, &m, &d);
        snprintf(s_info.cert_expires, sizeof(s_info.cert_expires), "%04d-%02d-%02d", y, m, d);
    }
    nvs_close(h);
}

typedef enum {
    MAKE_IF_NEEDED,   // create what is missing, renew an expiring server certificate
    MAKE_SERVER,      // new server key + certificate, keep the CA
    MAKE_ALL,         // new CA and new server key + certificate
} make_mode_t;

/**
 * Make sure CA and server certificate exist (see make_mode_t). Key generation and signing
 * take about a second; temporary heap use is a few KB.
 */
static esp_err_t ensure_material(make_mode_t mode)
{
    typedef struct {
        uint8_t ca_key[KEY_DER_MAX];
        uint8_t srv_key[KEY_DER_MAX];
        uint8_t crt[CRT_DER_MAX];
        uint8_t nc[128];
        char ca_name[80];
        char srv_name[96];
        char dns_local[48];
        char dns_host[32];
        uint8_t ip[2][4];
        mbedtls_x509_san_list san[4];
    } work_t;

    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NS, NVS_READWRITE, &h), TAG, "nvs_open");
    work_t *w = calloc(1, sizeof(work_t));
    if (!w) {
        nvs_close(h);
        return ESP_ERR_NO_MEM;
    }
    mbedtls_pk_context ca_pk, srv_pk;
    mbedtls_pk_init(&ca_pk);
    mbedtls_pk_init(&srv_pk);
    esp_err_t err = ESP_OK;
    int r = 0;
    bool new_ca = false;
    const int32_t today = ref_day();

    pd_wifi_status_t ws;
    pd_wifi_get_status(&ws);
    const char *host = valid_hostname(ws.hostname) ? ws.hostname : CONFIG_PD_WIFI_HOSTNAME;
    ca_name(w->ca_name, sizeof(w->ca_name));

    // ---- CA: load, or create
    uint8_t *der = NULL;
    size_t len = 0;
    size_t crt_len = 0;
    bool have_ca = mode != MAKE_ALL && get_blob(h, KEY_CA_KEY, &der, &len) == ESP_OK &&
                   nvs_get_blob(h, KEY_CA_CRT, NULL, &crt_len) == ESP_OK && crt_len > 0;
    if (have_ca) {
        have_ca = mbedtls_pk_parse_key(&ca_pk, der, len, NULL, 0, rng, NULL) == 0;
    }
    free_secret(der, len);
    if (!have_ca) {
        ESP_LOGI(TAG, "creating CA");
        mbedtls_pk_free(&ca_pk);
        mbedtls_pk_init(&ca_pk);
        const size_t nc_len = name_constraints(w->nc, sizeof(w->nc), host);
        const cert_params_t p = {
            .subject_key = &ca_pk, .issuer_key = &ca_pk, .subject = w->ca_name, .issuer = w->ca_name,
            .from_day = today - 2, .ca = true, .nc = w->nc, .nc_len = nc_len,
        };
        const uint8_t *crt;
        if ((r = nc_len == 0 ? -1 : 0) != 0 || (r = gen_key(&ca_pk)) != 0 ||
            (r = write_cert(&p, w->crt, sizeof(w->crt), &crt, &crt_len)) != 0) {
            goto fail;
        }
        const int kl = mbedtls_pk_write_key_der(&ca_pk, w->ca_key, sizeof(w->ca_key));
        if (kl <= 0) {
            r = kl ? kl : -1;
            goto fail;
        }
        // Drop the old server certificate first: an interrupted write then only leaves a CA
        // without server certificate, which the next start completes.
        nvs_erase_key(h, KEY_SRV_CRT);
        nvs_erase_key(h, KEY_SRV_KEY);
        nvs_erase_key(h, KEY_SRV_EXP);
        if ((err = nvs_set_blob(h, KEY_CA_KEY, w->ca_key + sizeof(w->ca_key) - kl, kl)) != ESP_OK ||
            (err = nvs_set_blob(h, KEY_CA_CRT, crt, crt_len)) != ESP_OK || (err = nvs_commit(h)) != ESP_OK) {
            goto fail;
        }
        new_ca = true;
    }

    // ---- server certificate: keep, or create
    uint32_t exp = 0;
    size_t srv_len = 0;
    const bool have_srv = nvs_get_u32(h, KEY_SRV_EXP, &exp) == ESP_OK &&
                          nvs_get_blob(h, KEY_SRV_CRT, NULL, &srv_len) == ESP_OK && srv_len > 0 &&
                          nvs_get_blob(h, KEY_SRV_KEY, NULL, &len) == ESP_OK && len > 0;
    if (mode == MAKE_IF_NEEDED && !new_ca && have_srv && (int32_t)exp >= today + SRV_RENEW_DAYS) {
        goto done;
    }
    ESP_LOGI(TAG, "creating server certificate");

    // SAN: <host>.local, <host>, current station IP and setup-AP IP (private ranges only).
    size_t n = 0;
    snprintf(w->dns_local, sizeof(w->dns_local), "%s.local", host);
    strlcpy(w->dns_host, host, sizeof(w->dns_host));
    const char *dns[2] = {w->dns_local, w->dns_host};
    for (int i = 0; i < 2; i++) {
        w->san[n].node.type = MBEDTLS_X509_SAN_DNS_NAME;
        w->san[n].node.san.unstructured_name.p = (unsigned char *)dns[i];
        w->san[n].node.san.unstructured_name.len = strlen(dns[i]);
        n++;
    }
    const char *ips[2] = {ws.sta_connected ? ws.sta_ip : NULL, ws.ap_ip[0] ? ws.ap_ip : "192.168.4.1"};
    for (int i = 0; i < 2; i++) {
        if (private_ip(ips[i], w->ip[i]) && !(i == 1 && n == 3 && memcmp(w->ip[0], w->ip[1], 4) == 0)) {
            w->san[n].node.type = MBEDTLS_X509_SAN_IP_ADDRESS;
            w->san[n].node.san.unstructured_name.p = w->ip[i];
            w->san[n].node.san.unstructured_name.len = 4;
            n++;
        }
    }
    for (size_t i = 0; i + 1 < n; i++) {
        w->san[i].next = &w->san[i + 1];
    }
    snprintf(w->srv_name, sizeof(w->srv_name), "CN=%s.local,O=WiFi DataDrive", host);
    const cert_params_t p = {
        .subject_key = &srv_pk, .issuer_key = &ca_pk, .subject = w->srv_name, .issuer = w->ca_name,
        .from_day = today - 2, .to_day = today - 2 + SRV_VALID_DAYS, .ca = false, .san = &w->san[0],
    };
    const uint8_t *crt;
    if ((r = gen_key(&srv_pk)) != 0 || (r = write_cert(&p, w->crt, sizeof(w->crt), &crt, &crt_len)) != 0) {
        goto fail;
    }
    const int kl = mbedtls_pk_write_key_der(&srv_pk, w->srv_key, sizeof(w->srv_key));
    if (kl <= 0) {
        r = kl ? kl : -1;
        goto fail;
    }
    exp = (uint32_t)p.to_day;
    if ((err = nvs_set_blob(h, KEY_SRV_KEY, w->srv_key + sizeof(w->srv_key) - kl, kl)) != ESP_OK ||
        (err = nvs_set_blob(h, KEY_SRV_CRT, crt, crt_len)) != ESP_OK ||
        (err = nvs_set_u32(h, KEY_SRV_EXP, exp)) != ESP_OK || (err = nvs_commit(h)) != ESP_OK) {
        goto fail;
    }
    s_changed = true;
    ESP_LOGI(TAG, "server certificate for %s.local, %u IP(s), %u bytes", host, (unsigned)(n - 2), (unsigned)crt_len);
    goto done;

fail:
    if (r != 0) {
        ESP_LOGE(TAG, "certificate generation failed: -0x%04x", (unsigned)-r);
        err = ESP_FAIL;
    } else {
        ESP_LOGE(TAG, "storing certificate failed: %s", esp_err_to_name(err));
    }
done:
    mbedtls_pk_free(&ca_pk);
    mbedtls_pk_free(&srv_pk);
    mbedtls_platform_zeroize(w, sizeof(*w));
    free(w);
    nvs_close(h);
    load_info();
    return err;
}

// ------------------------------------------------------------------ public API

esp_err_t pd_tls_init(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, KEY_ENABLED, &v);
        nvs_close(h);
    }
    s_enabled = v != 0;
    if (!s_enabled) {
        load_info();
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(ensure_material(MAKE_IF_NEEDED), TAG, "key material");
    s_changed = false;   // created/renewed before the server starts: in effect now

    ESP_RETURN_ON_ERROR(nvs_open(NVS_NS, NVS_READONLY, &h), TAG, "nvs_open");
    esp_err_t err = get_blob(h, KEY_SRV_CRT, &s_srv_crt, &s_srv_crt_len);
    if (err == ESP_OK) {
        err = get_blob(h, KEY_SRV_KEY, &s_srv_key, &s_srv_key_len);
    }
    nvs_close(h);
    if (err != ESP_OK) {
        pd_tls_release_server_credentials();
        ESP_LOGE(TAG, "loading server certificate failed: %s", esp_err_to_name(err));
    }
    return err;
}

bool pd_tls_enabled(void)
{
    return s_enabled;
}

bool pd_tls_active(void)
{
    return s_active;
}

void pd_tls_set_active(bool active)
{
    s_active = active;
}

esp_err_t pd_tls_set_enabled(bool on)
{
    if (on && (s_info.cert_sha256[0] == '\0' || s_info.ca_sha256[0] == '\0')) {
        ESP_RETURN_ON_ERROR(ensure_material(MAKE_IF_NEEDED), TAG, "key material");
    }
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NS, NVS_READWRITE, &h), TAG, "nvs_open");
    esp_err_t err = nvs_set_u8(h, KEY_ENABLED, on ? 1 : 0);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        s_enabled = on;
        ESP_LOGI(TAG, "HTTPS %s (after restart)", on ? "enabled" : "disabled");
    }
    return err;
}

esp_err_t pd_tls_renew_cert(void)
{
    return ensure_material(MAKE_SERVER);
}

esp_err_t pd_tls_regenerate(void)
{
    return ensure_material(MAKE_ALL);
}

bool pd_tls_server_credentials(const uint8_t **crt, size_t *crt_len, const uint8_t **key, size_t *key_len)
{
    if (!s_srv_crt || !s_srv_key) {
        return false;
    }
    *crt = s_srv_crt;
    *crt_len = s_srv_crt_len;
    *key = s_srv_key;
    *key_len = s_srv_key_len;
    return true;
}

void pd_tls_release_server_credentials(void)
{
    free_secret(s_srv_key, s_srv_key_len);
    free(s_srv_crt);
    s_srv_key = s_srv_crt = NULL;
    s_srv_key_len = s_srv_crt_len = 0;
}

void pd_tls_get_info(pd_tls_info_t *out)
{
    *out = s_info;
    out->enabled = s_enabled;
    out->active = s_active;
    out->restart_pending = s_enabled != s_active || (s_active && s_changed);
}

esp_err_t pd_tls_read_ca(uint8_t **der, size_t *len)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        return err == ESP_ERR_NVS_NOT_FOUND ? ESP_ERR_NOT_FOUND : err;
    }
    err = get_blob(h, KEY_CA_CRT, der, len);
    nvs_close(h);
    return err == ESP_ERR_NVS_NOT_FOUND ? ESP_ERR_NOT_FOUND : err;
}

// ------------------------------------------------------------------ reserved TLS record buffers
//
// Measured with 0.4.0 (dynamic mbedTLS buffers): every incoming 16 KiB record needed a fresh
// 16749-byte block, which a heap fragmented by Wi-Fi/lwIP buffers often did not have - uploads
// failed at once. Now the record buffers are static per connection (CONFIG_MBEDTLS_DYNAMIC_BUFFER
// off) and come from two blocks reserved at boot. mbedTLS's allocator is only replaced while the
// HTTPS server runs; without it mbedTLS uses calloc()/free() (CONFIG_MBEDTLS_CUSTOM_MEM_ALLOC),
// which on this board without PSRAM is the same internal RAM as CONFIG_MBEDTLS_INTERNAL_MEM_ALLOC.

#if defined(CONFIG_MBEDTLS_CUSTOM_MEM_ALLOC) && defined(CONFIG_PD_HTTPS_OPTION)

#ifdef CONFIG_MBEDTLS_ASYMMETRIC_CONTENT_LEN
#define POOL_IN_CONTENT  CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN
#define POOL_OUT_CONTENT CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN
#else
#define POOL_IN_CONTENT  CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN
#define POOL_OUT_CONTENT CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN
#endif
// mbedTLS adds 333 bytes to the content length (13 header + 16 IV + 48 MAC + 256 CBC padding);
// the dynamic-buffer variant (should it be switched on again) adds a small header on top.
#define POOL_SLACK 512

typedef struct {
    uint8_t *mem;
    size_t min;      // serves requests of more than min ...
    size_t cap;      // ... and at most cap bytes
    bool used;
} pool_slot_t;

static pool_slot_t s_pool[2];   // [0] input record buffer, [1] output record buffer
static portMUX_TYPE s_pool_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_pool_misses;

/** mbedTLS calloc (any task: TLS server, Wi-Fi supplicant, pd_crypt). */
static void *pool_calloc(size_t n, size_t size)
{
    if (n != 0 && size > SIZE_MAX / n) {
        return NULL;
    }
    const size_t len = n * size;
    bool tls_sized = false;
    for (size_t i = 0; i < sizeof(s_pool) / sizeof(s_pool[0]); i++) {
        pool_slot_t *s = &s_pool[i];
        if (!s->mem || len <= s->min || len > s->cap) {
            continue;
        }
        tls_sized = true;
        bool take = false;
        taskENTER_CRITICAL(&s_pool_mux);
        if (!s->used) {
            s->used = true;
            take = true;
        }
        taskEXIT_CRITICAL(&s_pool_mux);
        if (take) {
            memset(s->mem, 0, len);
            return s->mem;
        }
    }
    if (tls_sized) {
        s_pool_misses++;
    }
    return heap_caps_calloc(n, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

static void pool_free(void *p)
{
    if (!p) {
        return;
    }
    for (size_t i = 0; i < sizeof(s_pool) / sizeof(s_pool[0]); i++) {
        if (p == s_pool[i].mem) {
            taskENTER_CRITICAL(&s_pool_mux);
            s_pool[i].used = false;
            taskEXIT_CRITICAL(&s_pool_mux);
            return;
        }
    }
    heap_caps_free(p);   // also everything allocated with calloc() before the hook was installed
}

esp_err_t pd_tls_reserve_buffers(void)
{
    if (s_pool[0].mem) {
        return ESP_OK;
    }
    const size_t cap_in = POOL_IN_CONTENT + POOL_SLACK, cap_out = POOL_OUT_CONTENT + POOL_SLACK;
    uint8_t *in = heap_caps_malloc(cap_in, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *out = in ? heap_caps_malloc(cap_out, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) : NULL;
    if (!in || !out) {
        heap_caps_free(in);
        return ESP_ERR_NO_MEM;
    }
    s_pool[0] = (pool_slot_t){.mem = in, .min = POOL_IN_CONTENT, .cap = cap_in};
    s_pool[1] = (pool_slot_t){.mem = out, .min = POOL_OUT_CONTENT, .cap = cap_out};
    mbedtls_platform_set_calloc_free(pool_calloc, pool_free);
    ESP_LOGI(TAG, "TLS record buffers reserved (%u + %u bytes)", (unsigned)cap_in, (unsigned)cap_out);
    return ESP_OK;
}

void pd_tls_release_buffers(void)
{
    if (!s_pool[0].mem || s_pool[0].used || s_pool[1].used) {
        return;
    }
    mbedtls_platform_set_calloc_free(calloc, free);
    heap_caps_free(s_pool[0].mem);
    heap_caps_free(s_pool[1].mem);
    memset(s_pool, 0, sizeof(s_pool));
}

uint32_t pd_tls_buffer_misses(void)
{
    return s_pool_misses;
}

#else   // no custom mbedTLS allocator configured, or HTTPS compiled out

esp_err_t pd_tls_reserve_buffers(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

void pd_tls_release_buffers(void)
{
}

uint32_t pd_tls_buffer_misses(void)
{
    return 0;
}

#endif
