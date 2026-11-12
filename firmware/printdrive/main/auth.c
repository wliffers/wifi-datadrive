#include "auth.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"
#include "mbedtls/base64.h"
#include "mbedtls/md.h"
#include "mbedtls/pkcs5.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "sdkconfig.h"

// Authentication (API contract v3, section 2). Security notes:
// - Passwords and session tokens are never logged or stored in plain text. Buffers that held
//   them are wiped with mbedtls_platform_zeroize().
// - Hashes, tokens and cached Basic digests are compared in constant time.
// - Every header is length-checked before it is copied; oversize input is rejected.
// - Fail closed: if a stored user database exists but cannot be read or validated, every login
//   and every protected request is refused (503 AUTH_UNAVAILABLE) until the BOOT button reset
//   (auth_reset_users) writes a fresh one. Only a missing database (first start) creates
//   admin/admin. There is no code path that grants access without a verified password or session.
// - Lock-out per peer address (5 failures -> 30 s, doubling up to 15 min), reset only by a
//   verified password from the same peer, plus a global cap against distributed guessing.
// - Trusted devices carry the epoch of their user; any password/role change, deletion or a
//   failed device removal gives the user a new epoch, so stale NVS copies are never accepted.

static const char *TAG = "auth";

#define NVS_NS          "pd_auth"
#define NVS_KEY_USERS   "users"
#define DB_MAGIC        0x31554450u   // "PDU1"
#define DB_VERSION_1    1             // firmware up to 0.3.0 stage 2: no epoch (migrated at load)
#define DB_VERSION      2
#define SALT_LEN        16
#define HASH_LEN        32
#define PBKDF2_ROUNDS   2000
#define TOKEN_LEN       32            // bytes (256 bit)

#define SESSIONS_MAX    8
#define SESSION_IDLE_US (12LL * 3600 * 1000000)

// Lock-out per peer.
#define PEERS_MAX           16
#define PEER_LOCK_FAILS     5                         // consecutive failures -> lock-out
#define PEER_LOCK_BASE_US   (30LL * 1000000)          // first lock-out, doubled on each further one
#define PEER_LOCK_MAX_US    (15LL * 60 * 1000000)
#define RECENT_SUCCESS_US   (12LL * 3600 * 1000000)   // exempt from the global cap
// Global cap: more than GLOBAL_FAILS failures within GLOBAL_WINDOW_US -> password checks of
// peers without a recent success are refused for GLOBAL_LOCK_US.
#define GLOBAL_FAILS        30
#define GLOBAL_WINDOW_US    (60LL * 1000000)
#define GLOBAL_LOCK_US      (30LL * 1000000)

// Trusted devices: flash wear limits for creating device records.
#define DEVICES_PER_USER    4
#define DEV_WRITE_GAP_US    (10LL * 1000000)
#define DEV_WRITES_PER_HOUR 20
#define DEV_WRITE_WINDOW_US (3600LL * 1000000)

// Longest legal Basic header: "Basic " + base64(24 + 1 + 64 bytes) = 6 + 120 chars. Allow some
// slack for extra spaces; anything longer cannot be valid and is rejected without decoding.
#define AUTH_HDR_MAX    200
// Cookie headers may carry other cookies of the same host; the HTTP server limits a header
// line to CONFIG_HTTPD_MAX_REQ_HDR_LEN anyway.
#define COOKIE_HDR_MAX  1024

// Stored role values (independent of the web_role_t enum numbering).
#define ROLE_V_USER     1
#define ROLE_V_ADMIN    2
#define FLAG_DEFAULT_PW 0x01          // password is "admin" and the name is "admin"

// ------------------------------------------------------------------ persisted format

typedef struct {
    char name[AUTH_NAME_MAX + 1];
    uint8_t role;                     // ROLE_V_*
    uint8_t flags;                    // FLAG_*
    uint8_t salt[SALT_LEN];
    uint8_t hash[HASH_LEN];
} __attribute__((packed)) user_rec_v1_t;

typedef struct {
    uint32_t magic;
    uint8_t version;                  // DB_VERSION_1
    uint8_t count;
    uint8_t reserved[2];
    user_rec_v1_t users[AUTH_USERS_MAX];
} __attribute__((packed)) user_db_v1_t;

typedef struct {
    char name[AUTH_NAME_MAX + 1];
    uint8_t role;                     // ROLE_V_*
    uint8_t flags;                    // FLAG_*
    uint8_t salt[SALT_LEN];
    uint8_t hash[HASH_LEN];
    uint32_t epoch;                   // random, never 0; new on password/role change and re-creation
} __attribute__((packed)) user_rec_t;

typedef struct {
    uint32_t magic;
    uint8_t version;                  // DB_VERSION
    uint8_t count;
    uint8_t reserved[2];
    user_rec_t users[AUTH_USERS_MAX];
} __attribute__((packed)) user_db_t;

_Static_assert(sizeof(user_db_v1_t) != sizeof(user_db_t), "layouts must be distinguishable by size");

// Trusted devices (contract 2.2a): persistent sessions. Only SHA-256(token) is stored.
// Version 1 records (no epoch) are discarded at load.
#define NVS_KEY_DEVICES "devices"
#define DEV_MAGIC       0x31445044u   // "PDD1"
#define DEV_VERSION     2

typedef struct {
    uint8_t used;
    uint8_t hash[32];                 // SHA-256 of the session token
    char user[AUTH_NAME_MAX + 1];
    char label[AUTH_DEVICE_LABEL_MAX + 1];
    uint32_t seq;                     // creation order: the lowest is the oldest
    uint32_t epoch;                   // must equal the user's epoch, else the record is invalid
} __attribute__((packed)) device_rec_t;

typedef struct {
    uint32_t magic;
    uint8_t version;
    uint8_t reserved[3];
    uint32_t next_seq;                // monotonically increasing, persisted
    device_rec_t dev[AUTH_DEVICES_MAX];
} __attribute__((packed)) device_db_t;

// ------------------------------------------------------------------ RAM state (s_mutex)

typedef struct {
    bool used;
    uint8_t token[TOKEN_LEN];
    char user[AUTH_NAME_MAX + 1];
    int64_t created_us;
    int64_t last_us;
} session_t;

typedef struct {
    bool valid;
    uint8_t digest[32];               // SHA-256 of the last verified Authorization header value
} basic_cache_t;

/** Failure bookkeeping of one peer address (IPv4 as IPv4-mapped IPv6). */
typedef struct {
    bool used;
    bool has_success;                 // a password was verified from this peer (success_us valid)
    uint8_t addr[16];
    uint8_t fails;                    // consecutive failures since the last lock-out / success
    uint8_t lockouts;                 // lock-outs since the last success (doubles the duration)
    int64_t lock_until_us;            // locked while now < lock_until_us
    int64_t last_seen_us;             // LRU replacement
    int64_t success_us;
} peer_t;

static SemaphoreHandle_t s_mutex;
static StaticSemaphore_t s_mutex_buf;
static bool s_ready;
static volatile bool s_storage_ok;    // false: stored user database unreadable -> refuse everything

static user_db_t s_db;
static union {
    user_db_t v2;                     // rollback copy while saving
    user_db_v1_t v1;                  // load buffer for the migration
} s_bk;
static basic_cache_t s_basic_cache[AUTH_USERS_MAX];   // parallel to s_db.users
static session_t s_sessions[SESSIONS_MAX];
static device_db_t s_dev;
static uint8_t s_dummy_salt[SALT_LEN];   // for unknown users: same work as a real check

static peer_t s_peers[PEERS_MAX];
static int64_t s_glob_fail_us[GLOBAL_FAILS];   // ring of the last failure times
static int s_glob_pos;
static uint32_t s_glob_count;                  // failures noted (saturating)
static int64_t s_glob_lock_until_us;           // 0 = not locked

static int64_t s_dev_write_us[DEV_WRITES_PER_HOUR];   // ring of the last device-record writes
static int s_dev_write_pos;
static uint32_t s_dev_write_count;                    // writes noted (saturating)

// Identity of the request being handled. Only used by the HTTP server task, which handles one
// request at a time; reset at the start of every auth_authorize()/auth_identify()/auth_login().
static struct {
    char user[AUTH_NAME_MAX + 1];
    web_role_t role;
    bool default_pw;                  // still the delivery password: forced change pending
    int session;                      // slot in s_sessions, -1 for HTTP Basic / device / none
    uint8_t token[TOKEN_LEN];         // token of that slot (guards against slot reuse)
    int device;                       // slot in s_dev.dev for a trusted device, else -1
    uint8_t dev_hash[32];             // hash of that slot (guards against slot reuse)
    uint32_t retry_s;                 // seconds for Retry-After when AUTH_ERR_LOCKED was returned
} s_cur = {.session = -1, .device = -1};

static void lock(void)
{
    xSemaphoreTake(s_mutex, portMAX_DELAY);
}

static void unlock(void)
{
    xSemaphoreGive(s_mutex);
}

// ================================================================== result texts

const char *auth_result_code(auth_result_t r)
{
    switch (r) {
    case AUTH_OK:                 return "OK";
    case AUTH_ERR_BAD_REQUEST:    return "BAD_REQUEST";
    case AUTH_ERR_BAD_USER:       return "BAD_USER";
    case AUTH_ERR_BAD_PASSWORD:
    case AUTH_ERR_DEFAULT_PASSWORD: return "BAD_PASSWORD";
    case AUTH_ERR_UNAUTHORIZED:   return "UNAUTHORIZED";
    case AUTH_ERR_BAD_LOGIN:      return "BAD_LOGIN";
    case AUTH_ERR_FORBIDDEN_ROLE: return "FORBIDDEN_ROLE";
    case AUTH_ERR_MUST_CHANGE_PASSWORD: return "MUST_CHANGE_PASSWORD";
    case AUTH_ERR_NOT_FOUND:      return "NOT_FOUND";
    case AUTH_ERR_EXISTS:         return "EXISTS";
    case AUTH_ERR_LAST_ADMIN:     return "LAST_ADMIN";
    case AUTH_ERR_USER_LIMIT:     return "USER_LIMIT";
    case AUTH_ERR_LOCKED:         return "LOCKED";
    case AUTH_ERR_UNAVAILABLE:    return "AUTH_UNAVAILABLE";
    default:                      return "IO";
    }
}

const char *auth_result_str(auth_result_t r)
{
    // Shown to end users in the web UI: plain language, no internal terms.
    switch (r) {
    case AUTH_OK:                 return "OK";
    case AUTH_ERR_BAD_REQUEST:    return "Parameter fehlt oder ist ungültig";
    case AUTH_ERR_BAD_USER:       return "Ungültiger Benutzername. Erlaubt sind 1 bis 24 Zeichen: Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich.";
    case AUTH_ERR_BAD_PASSWORD:   return "Das Passwort muss zwischen 5 und 64 Zeichen lang sein.";
    case AUTH_ERR_DEFAULT_PASSWORD: return "Bitte ein anderes Passwort als das Auslieferungspasswort wählen.";
    case AUTH_ERR_UNAUTHORIZED:   return "Bitte zuerst anmelden.";
    case AUTH_ERR_BAD_LOGIN:      return "Benutzername oder Passwort ist falsch.";
    case AUTH_ERR_FORBIDDEN_ROLE: return "Dafür sind Administratorrechte nötig.";
    case AUTH_ERR_MUST_CHANGE_PASSWORD: return "Bitte zuerst das Passwort ändern.";
    case AUTH_ERR_NOT_FOUND:      return "Diesen Benutzer gibt es nicht.";
    case AUTH_ERR_EXISTS:         return "Diesen Benutzer gibt es schon.";
    case AUTH_ERR_LAST_ADMIN:     return "Der letzte Administrator kann nicht entfernt werden.";
    case AUTH_ERR_USER_LIMIT:     return "Es sind bereits 8 Benutzer angelegt. Bitte zuerst einen löschen.";
    // The first lock-out lasts 30 s, further ones longer (Retry-After header has the seconds).
    case AUTH_ERR_LOCKED:         return "Zu viele Fehlversuche. Bitte etwas warten und dann erneut versuchen.";
    case AUTH_ERR_UNAVAILABLE:    return "Die Benutzerverwaltung ist nicht lesbar. Bitte die Taste am Gerät 10 Sekunden halten, um die Anmeldung zurückzusetzen.";
    default:                      return "Die Benutzerdaten konnten nicht gespeichert werden.";
    }
}

const char *auth_result_http_status(auth_result_t r)
{
    switch (r) {
    case AUTH_OK:                 return "200 OK";
    case AUTH_ERR_BAD_REQUEST:
    case AUTH_ERR_BAD_USER:
    case AUTH_ERR_BAD_PASSWORD:
    case AUTH_ERR_DEFAULT_PASSWORD: return "400 Bad Request";
    case AUTH_ERR_UNAUTHORIZED:
    case AUTH_ERR_BAD_LOGIN:      return "401 Unauthorized";
    case AUTH_ERR_FORBIDDEN_ROLE:
    case AUTH_ERR_MUST_CHANGE_PASSWORD: return "403 Forbidden";
    case AUTH_ERR_NOT_FOUND:      return "404 Not Found";
    case AUTH_ERR_EXISTS:
    case AUTH_ERR_LAST_ADMIN:
    case AUTH_ERR_USER_LIMIT:     return "409 Conflict";
    case AUTH_ERR_LOCKED:         return "429 Too Many Requests";
    case AUTH_ERR_UNAVAILABLE:    return "503 Service Unavailable";
    default:                      return "500 Internal Server Error";
    }
}

esp_err_t auth_send_error(httpd_req_t *req, auth_result_t r)
{
    // httpd keeps only the pointer until the response is sent (inside web_send_error), and the
    // HTTP server task handles one request at a time: a static buffer is sufficient.
    static char retry[12];
    if (r == AUTH_ERR_LOCKED && s_cur.retry_s > 0) {
        snprintf(retry, sizeof(retry), "%u", (unsigned)s_cur.retry_s);
        httpd_resp_set_hdr(req, "Retry-After", retry);
    }
    return web_send_error(req, auth_result_http_status(r), auth_result_code(r), auth_result_str(r));
}

bool auth_storage_ok(void)
{
    return s_storage_ok;
}

// ================================================================== small helpers

static bool ct_equal(const uint8_t *a, const uint8_t *b, size_t n)
{
    volatile uint8_t d = 0;
    for (size_t i = 0; i < n; i++) {
        d |= a[i] ^ b[i];
    }
    return d == 0;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool hex_decode(const char *s, size_t n, uint8_t *out)
{
    for (size_t i = 0; i < n / 2; i++) {
        const int h = hexval(s[2 * i]), l = hexval(s[2 * i + 1]);
        if (h < 0 || l < 0) {
            return false;
        }
        out[i] = (uint8_t)(h * 16 + l);
    }
    return true;
}

static void hex_encode(const uint8_t *in, size_t n, char *out)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[in[i] >> 4];
        out[2 * i + 1] = digits[in[i] & 0x0f];
    }
    out[2 * n] = '\0';
}

bool auth_valid_name(const char *name)
{
    if (name == NULL) {
        return false;
    }
    size_t n = 0;
    for (const char *p = name; *p; p++, n++) {
        const char c = *p;
        if (n >= AUTH_NAME_MAX ||
            !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' ||
              c == '_' || c == '-')) {
            return false;
        }
    }
    return n >= 1;
}

static bool valid_password(const char *pass)
{
    if (pass == NULL) {
        return false;
    }
    const size_t n = strnlen(pass, AUTH_PASS_MAX + 1);
    return n >= AUTH_PASS_MIN && n <= AUTH_PASS_MAX;
}

static web_role_t role_from_v(uint8_t v)
{
    return v == ROLE_V_ADMIN ? WEB_ROLE_ADMIN : WEB_ROLE_USER;
}

static uint8_t role_to_v(web_role_t r)
{
    return r == WEB_ROLE_ADMIN ? ROLE_V_ADMIN : ROLE_V_USER;
}

static bool pbkdf2(const char *pass, size_t plen, const uint8_t salt[SALT_LEN], uint8_t out[HASH_LEN])
{
    const int rc = mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, (const unsigned char *)pass, plen, salt,
                                                 SALT_LEN, PBKDF2_ROUNDS, HASH_LEN, out);
    if (rc != 0) {
        ESP_LOGE(TAG, "PBKDF2 failed (-0x%04x)", (unsigned)-rc);
        mbedtls_platform_zeroize(out, HASH_LEN);
        return false;
    }
    return true;
}

/** A random epoch that is neither 0 (= "none") nor old. */
static uint32_t new_epoch(uint32_t old)
{
    uint32_t e;
    do {
        e = esp_random();
    } while (e == 0 || e == old);
    return e;
}

// ================================================================== peer address

/** Peer address of the request as 16 bytes (IPv4 as ::ffff:a.b.c.d). All zero if unknown:
 *  all such requests then share one bucket (fail closed rather than unlimited). */
static void peer_addr(httpd_req_t *req, uint8_t out[16])
{
    memset(out, 0, 16);
    struct sockaddr_storage ss;
    socklen_t sl = sizeof(ss);
    memset(&ss, 0, sizeof(ss));
    const int fd = httpd_req_to_sockfd(req);
    if (fd < 0 || getpeername(fd, (struct sockaddr *)&ss, &sl) != 0) {
        return;
    }
    if (ss.ss_family == AF_INET && sl >= sizeof(struct sockaddr_in)) {
        struct sockaddr_in a;
        memcpy(&a, &ss, sizeof(a));
        out[10] = 0xff;
        out[11] = 0xff;
        memcpy(out + 12, &a.sin_addr.s_addr, 4);
    }
#if CONFIG_LWIP_IPV6
    else if (ss.ss_family == AF_INET6 && sl >= sizeof(struct sockaddr_in6)) {
        struct sockaddr_in6 a;
        memcpy(&a, &ss, sizeof(a));
        memcpy(out, a.sin6_addr.s6_addr, 16);
    }
#endif
}

/** Printable form of a peer address for the log (not secret). */
static void peer_str(const uint8_t a[16], char out[48])
{
    static const uint8_t mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
    if (memcmp(a, mapped, sizeof(mapped)) == 0) {
        snprintf(out, 48, "%u.%u.%u.%u", a[12], a[13], a[14], a[15]);
        return;
    }
    size_t n = 0;
    out[0] = '\0';
    for (int i = 0; i < 16 && n < 40; i += 2) {
        n += (size_t)snprintf(out + n, 48 - n, "%s%x", i ? ":" : "", (unsigned)((a[i] << 8) | a[i + 1]));
    }
}

// ================================================================== lock-out (s_mutex held)

static bool peer_recent_success(const peer_t *p, int64_t now)
{
    return p->has_success && now - p->success_us < RECENT_SUCCESS_US;
}

/** Entry of this address; a new one replaces (in this order) a free slot, the least recently
 *  used unlocked entry, the least recently used locked entry, and only then the least recently
 *  used entry with a recent success (those are exempt from the global cap). Never NULL. */
static peer_t *peer_get(const uint8_t addr[16], int64_t now)
{
    for (int i = 0; i < PEERS_MAX; i++) {
        if (s_peers[i].used && memcmp(s_peers[i].addr, addr, 16) == 0) {
            s_peers[i].last_seen_us = now;
            return &s_peers[i];
        }
    }
    int best = -1, best_rank = 4;
    for (int i = 0; i < PEERS_MAX; i++) {
        const peer_t *p = &s_peers[i];
        const int rank = !p->used ? 0 : peer_recent_success(p, now) ? 3 : now < p->lock_until_us ? 2 : 1;
        if (rank < best_rank || (rank == best_rank && p->last_seen_us < s_peers[best].last_seen_us)) {
            best = i;
            best_rank = rank;
        }
    }
    peer_t *p = &s_peers[best];
    memset(p, 0, sizeof(*p));
    p->used = true;
    memcpy(p->addr, addr, 16);
    p->last_seen_us = now;
    return p;
}

/** 0 if p may have a password checked now, else the seconds until it may (>= 1). */
static uint32_t peer_blocked_s(const peer_t *p, int64_t now)
{
    int64_t until = 0;
    if (now < p->lock_until_us) {
        until = p->lock_until_us;
    }
    if (now < s_glob_lock_until_us && !peer_recent_success(p, now) && s_glob_lock_until_us > until) {
        until = s_glob_lock_until_us;
    }
    return until == 0 ? 0 : (uint32_t)((until - now + 999999) / 1000000);
}

static void peer_success(peer_t *p, int64_t now)
{
    p->fails = 0;
    p->lockouts = 0;
    p->lock_until_us = 0;
    p->has_success = true;
    p->success_us = now;
}

static void peer_failure(peer_t *p, int64_t now)
{
    char who[48];
    if (++p->fails >= PEER_LOCK_FAILS) {
        int64_t dur = PEER_LOCK_BASE_US;
        for (int i = 0; i < p->lockouts && dur < PEER_LOCK_MAX_US; i++) {
            dur *= 2;
        }
        if (dur > PEER_LOCK_MAX_US) {
            dur = PEER_LOCK_MAX_US;
        }
        p->fails = 0;
        if (p->lockouts < UINT8_MAX) {
            p->lockouts++;
        }
        p->lock_until_us = now + dur;
        peer_str(p->addr, who);
        ESP_LOGW(TAG, "%d failed password checks from %s - locked for %lld s", PEER_LOCK_FAILS, who,
                 (long long)(dur / 1000000));
    }
    // Global cap: the failure GLOBAL_FAILS entries ago is overwritten; if it lies within the
    // window, this is failure number GLOBAL_FAILS + 1 within it.
    const int64_t oldest = s_glob_fail_us[s_glob_pos];
    const bool full = s_glob_count >= GLOBAL_FAILS;
    s_glob_fail_us[s_glob_pos] = now;
    s_glob_pos = (s_glob_pos + 1) % GLOBAL_FAILS;
    if (s_glob_count < UINT32_MAX) {
        s_glob_count++;
    }
    if (full && now - oldest < GLOBAL_WINDOW_US) {
        if (now >= s_glob_lock_until_us) {
            ESP_LOGW(TAG, "more than %d failed password checks per minute - password checks restricted for 30 s",
                     GLOBAL_FAILS);
        }
        s_glob_lock_until_us = now + GLOBAL_LOCK_US;
    }
}

static void lockout_reset_all(void)
{
    memset(s_peers, 0, sizeof(s_peers));
    memset(s_glob_fail_us, 0, sizeof(s_glob_fail_us));
    s_glob_pos = 0;
    s_glob_count = 0;
    s_glob_lock_until_us = 0;
}

// ================================================================== user database (s_mutex held)

static int find_user(const char *name)
{
    if (name == NULL || name[0] == '\0') {
        return -1;
    }
    for (int i = 0; i < s_db.count; i++) {
        if (strcasecmp(s_db.users[i].name, name) == 0) {
            return i;
        }
    }
    return -1;
}

static int admin_count(void)
{
    int n = 0;
    for (int i = 0; i < s_db.count; i++) {
        n += s_db.users[i].role == ROLE_V_ADMIN;
    }
    return n;
}

/** New salt + hash for u (u->name must be set). False if hashing failed (u unchanged then).
 *  Does not touch the epoch (callers decide). */
static bool rec_set_password(user_rec_t *u, const char *pass)
{
    uint8_t salt[SALT_LEN], hash[HASH_LEN];
    esp_fill_random(salt, sizeof(salt));
    if (!pbkdf2(pass, strlen(pass), salt, hash)) {
        return false;
    }
    memcpy(u->salt, salt, SALT_LEN);
    memcpy(u->hash, hash, HASH_LEN);
    mbedtls_platform_zeroize(hash, sizeof(hash));
    const bool is_default = strcasecmp(u->name, "admin") == 0 && strcmp(pass, "admin") == 0;
    u->flags = is_default ? (u->flags | FLAG_DEFAULT_PW) : (u->flags & ~FLAG_DEFAULT_PW);
    return true;
}

/** db := only admin/admin. If hashing fails the record keeps a zero hash, which no password
 *  matches (fail closed: nobody can log in until the next reset). */
static void db_make_default(user_db_t *db)
{
    memset(db, 0, sizeof(*db));
    db->magic = DB_MAGIC;
    db->version = DB_VERSION;
    db->count = 1;
    user_rec_t *u = &db->users[0];
    strlcpy(u->name, "admin", sizeof(u->name));
    u->role = ROLE_V_ADMIN;
    u->epoch = new_epoch(0);
    if (!rec_set_password(u, "admin")) {
        ESP_LOGE(TAG, "default account could not be hashed - login impossible");
    }
}

static bool db_valid(const user_db_t *db)
{
    if (db->magic != DB_MAGIC || db->version != DB_VERSION || db->count < 1 || db->count > AUTH_USERS_MAX) {
        return false;
    }
    bool admin = false;
    for (int i = 0; i < db->count; i++) {
        const user_rec_t *u = &db->users[i];
        if (memchr(u->name, '\0', sizeof(u->name)) == NULL || !auth_valid_name(u->name) ||
            (u->role != ROLE_V_USER && u->role != ROLE_V_ADMIN)) {
            return false;
        }
        admin |= u->role == ROLE_V_ADMIN;
        for (int j = 0; j < i; j++) {
            if (strcasecmp(db->users[j].name, u->name) == 0) {
                return false;
            }
        }
    }
    return admin;
}

/** Load the stored users into s_db. ESP_OK (*migrated: converted from version 1 or epochs
 *  assigned, must be saved), ESP_ERR_NVS_NOT_FOUND (nothing stored yet) or another error
 *  (stored data unreadable/corrupt: s_db is left empty). Uses s_bk as buffer. */
static esp_err_t db_load(bool *migrated)
{
    *migrated = false;
    memset(&s_db, 0, sizeof(s_db));
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        return err;   // ESP_ERR_NVS_NOT_FOUND: namespace does not exist yet
    }
    size_t len = 0;
    err = nvs_get_blob(h, NVS_KEY_USERS, NULL, &len);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        // Trusted devices without users: the user record was lost, not "never created" (the
        // devices are only ever written after the users). Do not fall back to admin/admin.
        size_t dlen = 0;
        if (nvs_get_blob(h, NVS_KEY_DEVICES, NULL, &dlen) == ESP_OK) {
            err = ESP_ERR_INVALID_STATE;
        }
        nvs_close(h);
        return err;
    }
    if (err == ESP_OK && len == sizeof(user_db_t)) {
        err = nvs_get_blob(h, NVS_KEY_USERS, &s_bk.v2, &len);
        if (err == ESP_OK && (len != sizeof(user_db_t) || !db_valid(&s_bk.v2))) {
            err = ESP_ERR_INVALID_STATE;
        }
        if (err == ESP_OK) {
            memcpy(&s_db, &s_bk.v2, sizeof(s_db));
        }
    } else if (err == ESP_OK && len == sizeof(user_db_v1_t)) {
        // Migration from version 1 (no epochs): same fields, epoch 0 -> new epochs below.
        err = nvs_get_blob(h, NVS_KEY_USERS, &s_bk.v1, &len);
        if (err == ESP_OK && (len != sizeof(user_db_v1_t) || s_bk.v1.magic != DB_MAGIC ||
                              s_bk.v1.version != DB_VERSION_1 || s_bk.v1.count > AUTH_USERS_MAX)) {
            err = ESP_ERR_INVALID_STATE;
        }
        if (err == ESP_OK) {
            s_db.magic = DB_MAGIC;
            s_db.version = DB_VERSION;
            s_db.count = s_bk.v1.count;
            for (int i = 0; i < s_bk.v1.count; i++) {
                const user_rec_v1_t *o = &s_bk.v1.users[i];
                user_rec_t *u = &s_db.users[i];
                memcpy(u->name, o->name, sizeof(u->name));
                u->role = o->role;
                u->flags = o->flags;
                memcpy(u->salt, o->salt, SALT_LEN);
                memcpy(u->hash, o->hash, HASH_LEN);
                u->epoch = 0;
            }
            if (!db_valid(&s_db)) {
                err = ESP_ERR_INVALID_STATE;
            } else {
                ESP_LOGW(TAG, "user database converted from version 1 (trusted devices must log in again)");
            }
        }
    } else if (err == ESP_OK) {
        err = ESP_ERR_INVALID_SIZE;
    }
    nvs_close(h);
    mbedtls_platform_zeroize(&s_bk, sizeof(s_bk));
    if (err != ESP_OK) {
        mbedtls_platform_zeroize(&s_db, sizeof(s_db));
        return err == ESP_ERR_NVS_NOT_FOUND ? ESP_ERR_INVALID_STATE : err;   // vanished meanwhile
    }
    for (int i = 0; i < s_db.count; i++) {
        if (s_db.users[i].epoch == 0) {
            s_db.users[i].epoch = new_epoch(0);
            *migrated = true;
        }
    }
    return ESP_OK;
}

static esp_err_t db_save(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, NVS_KEY_USERS, &s_db, sizeof(s_db));
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "saving users failed: %s", esp_err_to_name(err));
    }
    return err;
}

/** Save s_db; on failure restore s_bk.v2 (which the caller filled before changing s_db). */
static auth_result_t db_commit(void)
{
    if (db_save() != ESP_OK) {
        memcpy(&s_db, &s_bk.v2, sizeof(s_db));
        return AUTH_ERR_IO;
    }
    return AUTH_OK;
}

/** Give user idx a new epoch and save it. AUTH_ERR_IO: nothing changed. */
static auth_result_t user_bump_epoch(int idx)
{
    memcpy(&s_bk.v2, &s_db, sizeof(s_db));
    s_db.users[idx].epoch = new_epoch(s_db.users[idx].epoch);
    return db_commit();
}

// ================================================================== sessions (s_mutex held)

static void session_wipe(int i)
{
    mbedtls_platform_zeroize(&s_sessions[i], sizeof(s_sessions[i]));
}

static void sessions_wipe_all(void)
{
    for (int i = 0; i < SESSIONS_MAX; i++) {
        session_wipe(i);
    }
}

/** End all sessions of name except slot keep (-1: none kept). */
static void sessions_end_user(const char *name, int keep)
{
    for (int i = 0; i < SESSIONS_MAX; i++) {
        if (s_sessions[i].used && i != keep && strcasecmp(s_sessions[i].user, name) == 0) {
            session_wipe(i);
        }
    }
}

/** Slot of a valid session with this token (refreshes it), else -1. Expired sessions and
 *  sessions of users that no longer exist are removed on the way. */
static int session_find(const uint8_t token[TOKEN_LEN], int64_t now)
{
    int found = -1;
    for (int i = 0; i < SESSIONS_MAX; i++) {   // no early exit: constant work per lookup
        if (s_sessions[i].used && ct_equal(s_sessions[i].token, token, TOKEN_LEN)) {
            found = i;
        }
    }
    if (found < 0) {
        return -1;
    }
    session_t *s = &s_sessions[found];
    if (now - s->last_us > SESSION_IDLE_US || find_user(s->user) < 0) {
        session_wipe(found);
        return -1;
    }
    s->last_us = now;
    return found;
}

/** New session for name: a free slot, else an expired one, else the oldest is displaced. */
static int session_create(const char *name, int64_t now, uint8_t token[TOKEN_LEN])
{
    int slot = -1;
    for (int i = 0; i < SESSIONS_MAX && slot < 0; i++) {
        if (!s_sessions[i].used) {
            slot = i;
        }
    }
    for (int i = 0; i < SESSIONS_MAX && slot < 0; i++) {
        if (now - s_sessions[i].last_us > SESSION_IDLE_US) {
            slot = i;
        }
    }
    if (slot < 0) {
        slot = 0;
        for (int i = 1; i < SESSIONS_MAX; i++) {
            if (s_sessions[i].created_us < s_sessions[slot].created_us) {
                slot = i;
            }
        }
    }
    session_wipe(slot);
    esp_fill_random(token, TOKEN_LEN);
    session_t *s = &s_sessions[slot];
    s->used = true;
    memcpy(s->token, token, TOKEN_LEN);
    strlcpy(s->user, name, sizeof(s->user));
    s->created_us = now;
    s->last_us = now;
    return slot;
}

// ================================================================== trusted devices (s_mutex held)
// NVS is written only when a device is created or removed (flash wear), never on lookup.
// Creating is rate-limited; removing never is (a removal that is not saved could come back).

static void devices_make_empty(device_db_t *d)
{
    memset(d, 0, sizeof(*d));
    d->magic = DEV_MAGIC;
    d->version = DEV_VERSION;
    d->next_seq = 1;
}

/** True if a new device record may be written now (flash wear limit). */
static bool dev_write_allowed(int64_t now)
{
    if (s_dev_write_count == 0) {
        return true;
    }
    const int last = (s_dev_write_pos + DEV_WRITES_PER_HOUR - 1) % DEV_WRITES_PER_HOUR;
    if (now - s_dev_write_us[last] < DEV_WRITE_GAP_US) {
        return false;
    }
    // Slot s_dev_write_pos holds the write DEV_WRITES_PER_HOUR writes ago (once the ring is full).
    return s_dev_write_count < DEV_WRITES_PER_HOUR || now - s_dev_write_us[s_dev_write_pos] >= DEV_WRITE_WINDOW_US;
}

static esp_err_t devices_save(void)
{
    s_dev_write_us[s_dev_write_pos] = esp_timer_get_time();   // every write counts for the budget
    s_dev_write_pos = (s_dev_write_pos + 1) % DEV_WRITES_PER_HOUR;
    if (s_dev_write_count < UINT32_MAX) {
        s_dev_write_count++;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, NVS_KEY_DEVICES, &s_dev, sizeof(s_dev));
        if (err == ESP_OK) {
            err = nvs_commit(h);
        }
        nvs_close(h);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "saving trusted devices failed: %s", esp_err_to_name(err));
    }
    return err;
}

/** True if slot i is a usable device: used, its user exists and the epochs match. */
static bool dev_live(int i)
{
    const device_rec_t *d = &s_dev.dev[i];
    if (!d->used) {
        return false;
    }
    const int idx = find_user(d->user);
    return idx >= 0 && d->epoch != 0 && d->epoch == s_db.users[idx].epoch;
}

/** Load the devices into s_dev (empty on any problem: fail closed). Needs s_db loaded. Records
 *  that are malformed, of an old layout, of a user that no longer exists or of an old epoch of
 *  their user are removed from NVS as well. */
static void devices_load(void)
{
    devices_make_empty(&s_dev);
    nvs_handle_t h;
    size_t len = 0;
    bool purge = false;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (err == ESP_OK) {
        err = nvs_get_blob(h, NVS_KEY_DEVICES, NULL, &len);
        if (err == ESP_OK && len == sizeof(s_dev)) {
            err = nvs_get_blob(h, NVS_KEY_DEVICES, &s_dev, &len);
        } else if (err == ESP_OK) {
            ESP_LOGW(TAG, "trusted devices of an older layout discarded");
            purge = true;
        }
        nvs_close(h);
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        devices_make_empty(&s_dev);
        ESP_LOGI(TAG, "no trusted devices stored");
        return;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "trusted devices unreadable (%s) - none active", esp_err_to_name(err));
        purge = true;
    } else if (!purge && (len != sizeof(s_dev) || s_dev.magic != DEV_MAGIC || s_dev.version != DEV_VERSION)) {
        ESP_LOGE(TAG, "trusted devices corrupt - none active");
        purge = true;
    }
    if (purge) {
        devices_make_empty(&s_dev);
        devices_save();   // a failure is logged; the stale data stays invalid either way
        return;
    }
    int n = 0;
    for (int i = 0; i < AUTH_DEVICES_MAX; i++) {
        device_rec_t *d = &s_dev.dev[i];
        if (!d->used && d->epoch == 0) {
            continue;
        }
        if (d->used != 1 || memchr(d->user, '\0', sizeof(d->user)) == NULL ||
            memchr(d->label, '\0', sizeof(d->label)) == NULL || !dev_live(i)) {
            mbedtls_platform_zeroize(d, sizeof(*d));
            purge = true;
            continue;
        }
        if (d->seq >= s_dev.next_seq) {
            s_dev.next_seq = d->seq + 1;
        }
        n++;
    }
    if (purge) {
        ESP_LOGW(TAG, "invalid or revoked trusted devices removed");
        devices_save();
    }
    ESP_LOGI(TAG, "%d trusted device(s) loaded", n);
}

/** Slot of the trusted device with this token, else -1 (constant work per lookup). */
static int device_find(const uint8_t token[TOKEN_LEN])
{
    uint8_t h[32];
    if (mbedtls_sha256(token, TOKEN_LEN, h, 0) != 0) {
        return -1;
    }
    int found = -1;
    for (int i = 0; i < AUTH_DEVICES_MAX; i++) {
        if (s_dev.dev[i].used && ct_equal(s_dev.dev[i].hash, h, sizeof(h))) {
            found = i;
        }
    }
    if (found >= 0 && !dev_live(found)) {
        found = -1;   // user vanished or epoch changed (the record goes with the next save/load)
    }
    return found;
}

static void device_id(int i, char out[9])
{
    hex_encode(s_dev.dev[i].hash, 4, out);
}

/** Remove (RAM only) all devices of name except slot keep (-1: none). True if anything was removed. */
static bool devices_remove_user(const char *name, int keep)
{
    bool changed = false;
    for (int i = 0; i < AUTH_DEVICES_MAX; i++) {
        if (s_dev.dev[i].used && i != keep && strcasecmp(s_dev.dev[i].user, name) == 0) {
            mbedtls_platform_zeroize(&s_dev.dev[i], sizeof(device_rec_t));
            changed = true;
        }
    }
    return changed;
}

static bool current_device_is(int dev);

/** Remove device slot i from RAM and NVS. If NVS cannot be updated, the owner gets a new epoch,
 *  so the stale stored copy can never be accepted again (that also revokes the owner's other
 *  devices, except the caller's own one until the next restart). False if neither worked. */
static bool device_remove(int i)
{
    char user[AUTH_NAME_MAX + 1];
    strlcpy(user, s_dev.dev[i].user, sizeof(user));
    mbedtls_platform_zeroize(&s_dev.dev[i], sizeof(device_rec_t));
    if (devices_save() == ESP_OK) {
        return true;
    }
    const int idx = find_user(user);
    if (idx < 0) {
        return true;   // no such user: the stored record is purged at the next load
    }
    if (user_bump_epoch(idx) != AUTH_OK) {
        ESP_LOGE(TAG, "trusted device could not be revoked in flash");
        return false;
    }
    for (int k = 0; k < AUTH_DEVICES_MAX; k++) {
        if (s_dev.dev[k].used && strcasecmp(s_dev.dev[k].user, user) == 0) {
            if (current_device_is(k)) {
                s_dev.dev[k].epoch = s_db.users[idx].epoch;   // RAM only: valid until restart
            } else {
                mbedtls_platform_zeroize(&s_dev.dev[k], sizeof(device_rec_t));
            }
        }
    }
    ESP_LOGW(TAG, "trusted device removal not saved - all stored devices of that user revoked");
    return true;
}

/** Create a trusted device for user uidx with a fresh token. Returns the slot or -1 (nothing
 *  changed then: write limit reached, no slot the user may take, or saving failed).
 *  Slot choice: the user's device with the same label; else, at DEVICES_PER_USER devices, the
 *  user's oldest; else a free slot; else the user's own oldest. Other users' devices are never
 *  displaced. */
static int device_create(int uidx, const char *label, uint8_t token[TOKEN_LEN], int64_t now)
{
    if (!dev_write_allowed(now)) {
        ESP_LOGW(TAG, "trusted device not saved (write limit) - volatile session instead");
        return -1;
    }
    const char *name = s_db.users[uidx].name;
    int same_label = -1, oldest_own = -1, free_slot = -1, own = 0;
    for (int i = 0; i < AUTH_DEVICES_MAX; i++) {
        const device_rec_t *d = &s_dev.dev[i];
        if (!dev_live(i)) {
            if (free_slot < 0) {
                free_slot = i;   // unused or stale record
            }
            continue;
        }
        if (strcasecmp(d->user, name) != 0) {
            continue;
        }
        own++;
        if (same_label < 0 && strcmp(d->label, label) == 0) {
            same_label = i;
        }
        if (oldest_own < 0 || d->seq < s_dev.dev[oldest_own].seq) {
            oldest_own = i;
        }
    }
    int slot = same_label;
    if (slot < 0) {
        slot = own >= DEVICES_PER_USER ? oldest_own : free_slot >= 0 ? free_slot : oldest_own;
    }
    if (slot < 0) {
        ESP_LOGW(TAG, "no free trusted-device slot - volatile session instead");
        return -1;
    }
    device_rec_t old = s_dev.dev[slot];
    const uint32_t old_seq = s_dev.next_seq;
    device_rec_t *d = &s_dev.dev[slot];
    memset(d, 0, sizeof(*d));
    // The 8-hex-digit id must be unique among the stored devices: draw again on a collision.
    for (bool unique = false; !unique;) {
        esp_fill_random(token, TOKEN_LEN);
        if (mbedtls_sha256(token, TOKEN_LEN, d->hash, 0) != 0) {
            s_dev.dev[slot] = old;
            mbedtls_platform_zeroize(&old, sizeof(old));
            return -1;
        }
        unique = true;
        for (int i = 0; i < AUTH_DEVICES_MAX; i++) {
            if (i != slot && s_dev.dev[i].used && memcmp(s_dev.dev[i].hash, d->hash, 4) == 0) {
                unique = false;
            }
        }
    }
    d->used = 1;
    strlcpy(d->user, name, sizeof(d->user));
    strlcpy(d->label, label, sizeof(d->label));
    d->seq = s_dev.next_seq++;
    d->epoch = s_db.users[uidx].epoch;
    if (devices_save() != ESP_OK) {
        s_dev.dev[slot] = old;
        s_dev.next_seq = old_seq;
        mbedtls_platform_zeroize(&old, sizeof(old));
        return -1;
    }
    mbedtls_platform_zeroize(&old, sizeof(old));
    return slot;
}

// ================================================================== password checks (s_mutex held)

/** Constant work for known and unknown users (a dummy salt is hashed for unknown ones). */
static bool verify_password(int idx, const char *pass)
{
    const size_t plen = strnlen(pass, AUTH_PASS_MAX + 1);
    if (plen < AUTH_PASS_MIN || plen > AUTH_PASS_MAX) {
        return false;   // such a password can never have been set
    }
    uint8_t h[HASH_LEN];
    const uint8_t *salt = idx >= 0 ? s_db.users[idx].salt : s_dummy_salt;
    const bool hashed = pbkdf2(pass, plen, salt, h);
    const bool ok = hashed && idx >= 0 && ct_equal(h, s_db.users[idx].hash, HASH_LEN);
    mbedtls_platform_zeroize(h, sizeof(h));
    return ok;
}

/** Password check for the peer addr, honouring its lock-out and the global cap. AUTH_OK (and
 *  *idx = user), AUTH_ERR_BAD_LOGIN (counted) or AUTH_ERR_LOCKED (not checked, s_cur.retry_s set). */
static auth_result_t check_credentials(const uint8_t addr[16], const char *name, const char *pass, int64_t now,
                                       int *idx_out)
{
    *idx_out = -1;
    peer_t *p = peer_get(addr, now);
    const uint32_t blocked = peer_blocked_s(p, now);
    if (blocked) {
        s_cur.retry_s = blocked;
        return AUTH_ERR_LOCKED;
    }
    const int idx = auth_valid_name(name) ? find_user(name) : -1;
    if (verify_password(idx, pass)) {
        peer_success(p, now);
        *idx_out = idx;
        return AUTH_OK;
    }
    peer_failure(p, now);
    return AUTH_ERR_BAD_LOGIN;
}

// ================================================================== request identity

static void clear_current(void)
{
    mbedtls_platform_zeroize(&s_cur, sizeof(s_cur));
    s_cur.role = WEB_ROLE_NONE;
    s_cur.session = -1;
    s_cur.device = -1;
}

/** s_mutex held. slot: RAM session or -1; dev: trusted device or -1 (Basic: both -1). */
static void set_current(int idx, int slot, int dev)
{
    strlcpy(s_cur.user, s_db.users[idx].name, sizeof(s_cur.user));
    s_cur.role = role_from_v(s_db.users[idx].role);
    s_cur.default_pw = (s_db.users[idx].flags & FLAG_DEFAULT_PW) != 0;
    s_cur.session = slot;
    if (slot >= 0) {
        memcpy(s_cur.token, s_sessions[slot].token, TOKEN_LEN);
    }
    s_cur.device = dev;
    if (dev >= 0) {
        memcpy(s_cur.dev_hash, s_dev.dev[dev].hash, sizeof(s_cur.dev_hash));
    }
}

/** True if the caller's trusted device is still slot dev (s_mutex held). */
static bool current_device_is(int dev)
{
    return dev >= 0 && s_cur.device == dev && s_dev.dev[dev].used &&
           ct_equal(s_dev.dev[dev].hash, s_cur.dev_hash, sizeof(s_cur.dev_hash));
}

/** Token from the first "dd_session" cookie, if it is exactly 64 hex characters. */
static bool read_session_cookie(httpd_req_t *req, uint8_t token[TOKEN_LEN])
{
    const size_t len = httpd_req_get_hdr_value_len(req, "Cookie");
    if (len == 0 || len > COOKIE_HDR_MAX) {
        return false;
    }
    char buf[COOKIE_HDR_MAX + 1];
    if (httpd_req_get_hdr_value_str(req, "Cookie", buf, sizeof(buf)) != ESP_OK) {
        return false;
    }
    const size_t name_len = strlen(AUTH_COOKIE_NAME);
    bool ok = false;
    const char *p = buf;
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == ';') {
            p++;
        }
        const char *key = p;
        while (*p && *p != '=' && *p != ';') {
            p++;
        }
        size_t key_len = (size_t)(p - key);
        while (key_len && (key[key_len - 1] == ' ' || key[key_len - 1] == '\t')) {
            key_len--;
        }
        if (*p != '=') {
            continue;   // pair without value: p is at ';' or at the end
        }
        const char *val = ++p;
        while (*p && *p != ';') {
            p++;
        }
        size_t val_len = (size_t)(p - val);
        if (key_len == name_len && memcmp(key, AUTH_COOKIE_NAME, name_len) == 0) {
            while (val_len && (*val == ' ' || *val == '\t')) {
                val++;
                val_len--;
            }
            while (val_len && (val[val_len - 1] == ' ' || val[val_len - 1] == '\t')) {
                val_len--;
            }
            ok = val_len == AUTH_TOKEN_HEX && hex_decode(val, AUTH_TOKEN_HEX, token);
            break;   // the first dd_session cookie decides
        }
    }
    mbedtls_platform_zeroize(buf, sizeof(buf));
    return ok;
}

typedef enum { BASIC_ABSENT, BASIC_OK, BASIC_FAIL, BASIC_LOCKED } basic_result_t;

/** HTTP Basic. s_mutex held. Sets the request identity on success. The peer's lock-out applies
 *  to cache hits as well (otherwise the cache would answer guesses during a lock-out); a cache
 *  hit never resets the failure counter, only a real password check does. */
static basic_result_t try_basic(httpd_req_t *req, const uint8_t addr[16], int64_t now)
{
    const size_t len = httpd_req_get_hdr_value_len(req, "Authorization");
    if (len == 0) {
        return BASIC_ABSENT;
    }
    char hdr[AUTH_HDR_MAX + 1];
    unsigned char raw[AUTH_HDR_MAX];
    uint8_t digest[32];
    basic_result_t res = BASIC_FAIL;
    size_t olen = 0;
    int hit = -1;

    if (len > AUTH_HDR_MAX) {
        // Too long to be a valid Basic header. Only look at the scheme (for the lock-out answer).
        char scheme[8] = "";
        httpd_req_get_hdr_value_str(req, "Authorization", scheme, sizeof(scheme));   // truncates
        if (strncasecmp(scheme, "Basic ", 6) != 0) {
            return BASIC_ABSENT;
        }
        const uint32_t blocked = peer_blocked_s(peer_get(addr, now), now);
        s_cur.retry_s = blocked;
        return blocked ? BASIC_LOCKED : BASIC_FAIL;
    }
    if (httpd_req_get_hdr_value_str(req, "Authorization", hdr, sizeof(hdr)) != ESP_OK) {
        return BASIC_FAIL;
    }
    if (strncasecmp(hdr, "Basic ", 6) != 0) {
        res = BASIC_ABSENT;   // another scheme: treated like no credentials
        goto out;
    }
    peer_t *p = peer_get(addr, now);
    const uint32_t blocked = peer_blocked_s(p, now);
    if (blocked) {
        s_cur.retry_s = blocked;
        res = BASIC_LOCKED;
        goto out;
    }

    // Cache: identical header as the last one verified for a user -> no PBKDF2.
    if (mbedtls_sha256((const unsigned char *)hdr, strlen(hdr), digest, 0) != 0) {
        goto out;
    }
    for (int i = 0; i < s_db.count; i++) {   // no early exit
        if (s_basic_cache[i].valid && ct_equal(s_basic_cache[i].digest, digest, sizeof(digest))) {
            hit = i;
        }
    }
    if (hit >= 0) {
        // Keeps a polling script exempt from the global cap; the failure counter stays as it is.
        p->has_success = true;
        p->success_us = now;
        set_current(hit, -1, -1);
        res = BASIC_OK;
        goto out;
    }

    // Decode "Basic <base64(name:password)>".
    char *b64 = hdr + 6;
    while (*b64 == ' ') {
        b64++;
    }
    size_t b64_len = strlen(b64);
    while (b64_len && b64[b64_len - 1] == ' ') {
        b64_len--;
    }
    if (b64_len == 0 ||
        mbedtls_base64_decode(raw, sizeof(raw) - 1, &olen, (const unsigned char *)b64, b64_len) != 0 ||
        olen == 0 || memchr(raw, '\0', olen) != NULL) {
        goto out;   // malformed: refused, not counted as a password failure
    }
    raw[olen] = '\0';
    char *colon = strchr((char *)raw, ':');
    const char *pass = "";
    if (colon) {
        *colon = '\0';
        pass = colon + 1;
    }
    int idx = -1;
    const auth_result_t r = check_credentials(addr, (const char *)raw, pass, now, &idx);
    if (r == AUTH_OK) {
        s_basic_cache[idx].valid = true;
        memcpy(s_basic_cache[idx].digest, digest, sizeof(digest));
        set_current(idx, -1, -1);
        res = BASIC_OK;
    } else if (r == AUTH_ERR_LOCKED) {
        res = BASIC_LOCKED;
    }

out:
    mbedtls_platform_zeroize(hdr, sizeof(hdr));
    mbedtls_platform_zeroize(raw, sizeof(raw));
    mbedtls_platform_zeroize(digest, sizeof(digest));
    return res;
}

/** Cookie first, then (if with_basic) HTTP Basic. Sets the request identity on success. */
static auth_result_t identify(httpd_req_t *req, bool with_basic)
{
    uint8_t token[TOKEN_LEN];
    uint8_t addr[16] = {0};
    const bool have_cookie = read_session_cookie(req, token);
    if (with_basic) {
        peer_addr(req, addr);
    }
    auth_result_t r = AUTH_ERR_UNAUTHORIZED;

    lock();
    const int64_t now = esp_timer_get_time();
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else {
        if (have_cookie) {
            const int slot = session_find(token, now);
            if (slot >= 0) {
                set_current(find_user(s_sessions[slot].user), slot, -1);
                r = AUTH_OK;
            } else {
                const int dev = device_find(token);   // trusted device: no time limit
                if (dev >= 0) {
                    set_current(find_user(s_dev.dev[dev].user), -1, dev);
                    r = AUTH_OK;
                }
            }
        }
        if (r != AUTH_OK && with_basic) {
            switch (try_basic(req, addr, now)) {
            case BASIC_OK:     r = AUTH_OK; break;
            case BASIC_LOCKED: r = AUTH_ERR_LOCKED; break;
            default:           break;
            }
        }
    }
    unlock();
    mbedtls_platform_zeroize(token, sizeof(token));
    return r;
}

// ================================================================== public: dispatcher

/** Forced password change (contract 2.4): the protected routes an identity with the delivery
 *  password may still use. auth_authorize() only receives the minimum role, so the route is
 *  recognised by its path: req->uri up to the '?' must equal an entry exactly (the HTTP server
 *  has already matched the URI to a registered route, so no prefix/encoding tricks apply).
 *  GET /api/session and POST /api/login are WEB_ROLE_NONE routes and never restricted.
 *  /api/users/password is limited to the caller's own account by auth_user_set_password(). */
static bool exempt_from_forced_change(httpd_req_t *req)
{
    static const struct {
        httpd_method_t method;
        const char *path;
    } allow[] = {
        {HTTP_POST, "/api/logout"},
        {HTTP_POST, "/api/users/password"},
    };
    const char *q = strchr(req->uri, '?');
    const size_t len = q ? (size_t)(q - req->uri) : strlen(req->uri);
    for (size_t i = 0; i < sizeof(allow) / sizeof(allow[0]); i++) {
        if (req->method == (int)allow[i].method && strlen(allow[i].path) == len &&
            memcmp(req->uri, allow[i].path, len) == 0) {
            return true;
        }
    }
    return false;
}

bool auth_authorize(httpd_req_t *req, web_role_t min_role)
{
    clear_current();
    if (min_role == WEB_ROLE_NONE) {
        if (s_ready) {
            identify(req, false);   // only for information; never refuses
        }
        return true;
    }
    if (!s_ready) {
        auth_send_error(req, AUTH_ERR_UNAUTHORIZED);   // fail closed
        return false;
    }
    const auth_result_t r = identify(req, true);   // AUTH_ERR_UNAVAILABLE if the database is unreadable
    if (r != AUTH_OK) {
        auth_send_error(req, r);
        return false;
    }
    if (s_cur.role < min_role) {
        auth_send_error(req, AUTH_ERR_FORBIDDEN_ROLE);
        return false;
    }
    if (s_cur.default_pw && !exempt_from_forced_change(req)) {
        auth_send_error(req, AUTH_ERR_MUST_CHANGE_PASSWORD);   // session and Basic alike
        return false;
    }
    return true;
}

auth_result_t auth_identify(httpd_req_t *req)
{
    clear_current();
    return s_ready ? identify(req, true) : AUTH_ERR_UNAUTHORIZED;
}

const char *auth_current_user(httpd_req_t *req)
{
    (void)req;
    return s_cur.user;
}

web_role_t auth_current_role(httpd_req_t *req)
{
    (void)req;
    return s_cur.role;
}

bool auth_current_default_password(httpd_req_t *req)
{
    (void)req;
    if (!s_ready || s_cur.user[0] == '\0') {
        return false;
    }
    lock();
    const int idx = find_user(s_cur.user);
    const bool def = idx >= 0 && (s_db.users[idx].flags & FLAG_DEFAULT_PW);
    unlock();
    return def;
}

// ================================================================== public: endpoints

/** Device label: printable ASCII and complete UTF-8 sequences (no control characters, no
 *  overlong/invalid sequences), spaces trimmed, cut at a character boundary to fit. */
static void sanitize_label(const char *in, char out[AUTH_DEVICE_LABEL_MAX + 1])
{
    size_t n = 0;
    const unsigned char *p = (const unsigned char *)(in ? in : "");
    while (*p) {
        size_t len = 0;
        if (*p >= 0x20 && *p < 0x7f) {
            len = 1;
        } else if (*p >= 0xc2 && *p <= 0xdf) {
            len = 2;
        } else if (*p >= 0xe0 && *p <= 0xef) {
            len = 3;
        } else if (*p >= 0xf0 && *p <= 0xf4) {
            len = 4;
        }
        bool ok = len > 0;
        for (size_t k = 1; ok && k < len; k++) {
            ok = (p[k] & 0xc0) == 0x80;
        }
        if (ok && len >= 3) {
            // Reject overlong forms, UTF-16 surrogates and code points above U+10FFFF.
            ok = !(p[0] == 0xe0 && p[1] < 0xa0) && !(p[0] == 0xed && p[1] >= 0xa0) &&
                 !(p[0] == 0xf0 && p[1] < 0x90) && !(p[0] == 0xf4 && p[1] >= 0x90);
        }
        if (ok && len == 2 && p[0] == 0xc2 && p[1] < 0xa0) {
            ok = false;   // C1 control characters U+0080..U+009F
        }
        if (!ok) {
            p++;          // drop this byte
            continue;
        }
        if (n + len > AUTH_DEVICE_LABEL_MAX) {
            break;
        }
        if (!(n == 0 && *p == ' ')) {   // no leading spaces
            memcpy(out + n, p, len);
            n += len;
        }
        p += len;
    }
    while (n && out[n - 1] == ' ') {
        n--;
    }
    out[n] = '\0';
    if (n == 0) {
        strlcpy(out, "Unbekanntes Gerät", AUTH_DEVICE_LABEL_MAX + 1);
    }
}

auth_result_t auth_login(httpd_req_t *req, const char *user, const char *pass, const char *device_label,
                         char token_hex[AUTH_TOKEN_HEX + 1], bool *persistent)
{
    clear_current();
    token_hex[0] = '\0';
    *persistent = false;
    if (!s_ready) {
        return AUTH_ERR_BAD_LOGIN;
    }
    uint8_t addr[16];
    peer_addr(req, addr);
    lock();
    if (!s_storage_ok) {
        unlock();
        return AUTH_ERR_UNAVAILABLE;
    }
    const int64_t now = esp_timer_get_time();
    int idx = -1;
    const auth_result_t r = check_credentials(addr, user, pass, now, &idx);
    if (r != AUTH_OK) {
        unlock();
        if (r == AUTH_ERR_BAD_LOGIN) {
            char who[48];
            peer_str(addr, who);
            ESP_LOGW(TAG, "login failed from %s", who);
        }
        return r;
    }
    uint8_t token[TOKEN_LEN];
    int dev = -1;
    if (device_label != NULL) {
        char label[AUTH_DEVICE_LABEL_MAX + 1];
        sanitize_label(device_label, label);
        dev = device_create(idx, label, token, now);
        if (dev < 0) {
            ESP_LOGW(TAG, "trusted device not created - volatile session instead");
        }
    }
    int slot = -1;
    if (dev >= 0) {
        set_current(idx, -1, dev);
        *persistent = true;
    } else {
        slot = session_create(s_db.users[idx].name, now, token);
        set_current(idx, slot, -1);
    }
    hex_encode(token, TOKEN_LEN, token_hex);
    mbedtls_platform_zeroize(token, sizeof(token));
    unlock();
    if (dev >= 0) {
        ESP_LOGI(TAG, "login '%s' (trusted device %d)", s_cur.user, dev);
    } else {
        ESP_LOGI(TAG, "login '%s' (session slot %d)", s_cur.user, slot);
    }
    return AUTH_OK;
}

void auth_logout(httpd_req_t *req)
{
    (void)req;
    if (!s_ready || (s_cur.session < 0 && s_cur.device < 0)) {
        return;
    }
    lock();
    const int slot = s_cur.session;
    if (slot >= 0 && s_sessions[slot].used && ct_equal(s_sessions[slot].token, s_cur.token, TOKEN_LEN)) {
        session_wipe(slot);
    }
    if (current_device_is(s_cur.device)) {
        const int dev = s_cur.device;
        s_cur.device = -1;   // not "current" any more: device_remove must not keep it alive
        device_remove(dev);  // removed in RAM in any case; if not saved, the user's epoch changes
    }
    unlock();
    ESP_LOGI(TAG, "logout '%s'", s_cur.user);
    s_cur.session = -1;
    s_cur.device = -1;
    mbedtls_platform_zeroize(s_cur.token, sizeof(s_cur.token));
}

size_t auth_list_devices(auth_device_info_t *out, size_t max)
{
    if (!s_ready || s_cur.user[0] == '\0') {
        return 0;
    }
    lock();
    size_t n = 0;
    for (int i = 0; i < AUTH_DEVICES_MAX && n < max && s_storage_ok; i++) {
        const device_rec_t *d = &s_dev.dev[i];
        if (!dev_live(i) || (s_cur.role != WEB_ROLE_ADMIN && strcasecmp(d->user, s_cur.user) != 0)) {
            continue;
        }
        device_id(i, out[n].id);
        strlcpy(out[n].user, d->user, sizeof(out[n].user));
        strlcpy(out[n].label, d->label, sizeof(out[n].label));
        out[n].current = current_device_is(i);
        n++;
    }
    unlock();
    return n;
}

auth_result_t auth_device_delete(const char *id)
{
    if (!s_ready || s_cur.user[0] == '\0') {
        return AUTH_ERR_UNAUTHORIZED;
    }
    if (id == NULL || strlen(id) != 8) {
        return AUTH_ERR_NOT_FOUND;
    }
    lock();
    int found = -1;
    for (int i = 0; i < AUTH_DEVICES_MAX && found < 0; i++) {
        char did[9];
        if (dev_live(i)) {
            device_id(i, did);
            if (strcasecmp(did, id) == 0) {
                found = i;
            }
        }
    }
    auth_result_t r = AUTH_OK;
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else if (found < 0) {
        r = AUTH_ERR_NOT_FOUND;
    } else if (s_cur.role != WEB_ROLE_ADMIN && strcasecmp(s_dev.dev[found].user, s_cur.user) != 0) {
        r = AUTH_ERR_FORBIDDEN_ROLE;
    } else {
        ESP_LOGI(TAG, "trusted device %d of '%s' removed", found, s_dev.dev[found].user);
        if (current_device_is(found)) {
            // The response still reports {"ok":true}; the identity of this request stays.
            s_cur.device = -1;   // deleting the own device: it must not be kept alive
        }
        if (!device_remove(found)) {
            r = AUTH_ERR_IO;   // removed in RAM anyway: it stops working now
        }
    }
    unlock();
    return r;
}

size_t auth_list_users(auth_user_info_t *out, size_t max)
{
    if (!s_ready) {
        return 0;
    }
    lock();
    size_t n = 0;
    for (int i = 0; i < s_db.count && n < max && s_storage_ok; i++, n++) {
        strlcpy(out[n].name, s_db.users[i].name, sizeof(out[n].name));
        out[n].role = role_from_v(s_db.users[i].role);
    }
    unlock();
    return n;
}

auth_result_t auth_user_add(const char *name, const char *pass, web_role_t role)
{
    if (role != WEB_ROLE_USER && role != WEB_ROLE_ADMIN) {
        return AUTH_ERR_BAD_REQUEST;
    }
    if (!auth_valid_name(name)) {
        return AUTH_ERR_BAD_USER;
    }
    if (!valid_password(pass)) {
        return AUTH_ERR_BAD_PASSWORD;
    }
    if (strcmp(pass, "admin") == 0) {
        return AUTH_ERR_DEFAULT_PASSWORD;   // the delivery password is only ever set by a reset
    }
    if (!s_ready) {
        return AUTH_ERR_IO;
    }
    lock();
    auth_result_t r = AUTH_OK;
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else if (find_user(name) >= 0) {
        r = AUTH_ERR_EXISTS;
    } else if (s_db.count >= AUTH_USERS_MAX) {
        r = AUTH_ERR_USER_LIMIT;
    } else {
        memcpy(&s_bk.v2, &s_db, sizeof(s_db));
        const int idx = s_db.count;
        user_rec_t *u = &s_db.users[idx];
        memset(u, 0, sizeof(*u));
        strlcpy(u->name, name, sizeof(u->name));
        u->role = role_to_v(role);
        u->epoch = new_epoch(0);   // a re-created name never matches devices of the old user
        if (!rec_set_password(u, pass)) {
            memcpy(&s_db, &s_bk.v2, sizeof(s_db));
            r = AUTH_ERR_IO;
        } else {
            s_db.count++;
            r = db_commit();
            if (r == AUTH_OK) {
                s_basic_cache[idx].valid = false;
                ESP_LOGI(TAG, "user '%s' added (%s)", name, role == WEB_ROLE_ADMIN ? "admin" : "user");
            }
        }
    }
    unlock();
    return r;
}

auth_result_t auth_user_delete(const char *name)
{
    if (!s_ready) {
        return AUTH_ERR_IO;
    }
    lock();
    auth_result_t r = AUTH_OK;
    const int idx = find_user(name);
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else if (idx < 0) {
        r = AUTH_ERR_NOT_FOUND;
    } else if (s_db.users[idx].role == ROLE_V_ADMIN && admin_count() <= 1) {
        r = AUTH_ERR_LAST_ADMIN;
    } else {
        char removed[AUTH_NAME_MAX + 1];
        strlcpy(removed, s_db.users[idx].name, sizeof(removed));
        memcpy(&s_bk.v2, &s_db, sizeof(s_db));
        const int tail = s_db.count - idx - 1;
        memmove(&s_db.users[idx], &s_db.users[idx + 1], (size_t)tail * sizeof(user_rec_t));
        s_db.count--;
        mbedtls_platform_zeroize(&s_db.users[s_db.count], sizeof(user_rec_t));
        r = db_commit();
        if (r == AUTH_OK) {
            memmove(&s_basic_cache[idx], &s_basic_cache[idx + 1], (size_t)tail * sizeof(basic_cache_t));
            mbedtls_platform_zeroize(&s_basic_cache[s_db.count], sizeof(basic_cache_t));
            sessions_end_user(removed, -1);
            if (devices_remove_user(removed, -1)) {
                devices_save();   // on failure: records of a missing user are purged at load
            }
            ESP_LOGI(TAG, "user '%s' deleted", removed);
        }
    }
    unlock();
    return r;
}

auth_result_t auth_user_set_role(const char *name, web_role_t role)
{
    if (role != WEB_ROLE_USER && role != WEB_ROLE_ADMIN) {
        return AUTH_ERR_BAD_REQUEST;
    }
    if (!s_ready) {
        return AUTH_ERR_IO;
    }
    lock();
    auth_result_t r = AUTH_OK;
    const int idx = find_user(name);
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else if (idx < 0) {
        r = AUTH_ERR_NOT_FOUND;
    } else if (s_db.users[idx].role == ROLE_V_ADMIN && role == WEB_ROLE_USER && admin_count() <= 1) {
        r = AUTH_ERR_LAST_ADMIN;
    } else if (s_db.users[idx].role != role_to_v(role)) {
        memcpy(&s_bk.v2, &s_db, sizeof(s_db));
        s_db.users[idx].role = role_to_v(role);
        s_db.users[idx].epoch = new_epoch(s_db.users[idx].epoch);   // same commit as the role
        r = db_commit();
        if (r == AUTH_OK) {
            s_basic_cache[idx].valid = false;
            if (devices_remove_user(s_db.users[idx].name, -1)) {
                devices_save();   // on failure: the stored records have the old epoch
            }
            ESP_LOGI(TAG, "user '%s' is now %s", s_db.users[idx].name, role == WEB_ROLE_ADMIN ? "admin" : "user");
        }
    }
    unlock();
    return r;
}

auth_result_t auth_user_set_password(httpd_req_t *req, const char *name, const char *pass, const char *old)
{
    if (!s_ready || s_cur.user[0] == '\0') {
        return AUTH_ERR_UNAUTHORIZED;
    }
    uint8_t addr[16];
    peer_addr(req, addr);
    lock();
    const int64_t now = esp_timer_get_time();
    auth_result_t r = AUTH_OK;
    int idx = -1;
    const bool own = strcasecmp(name, s_cur.user) == 0;
    if (!s_storage_ok) {
        r = AUTH_ERR_UNAVAILABLE;
    } else if (own) {
        r = check_credentials(addr, s_cur.user, old ? old : "", now, &idx);   // BAD_LOGIN / LOCKED
    } else if (s_cur.default_pw) {
        r = AUTH_ERR_MUST_CHANGE_PASSWORD;   // forced change: only the own account first
    } else if (s_cur.role != WEB_ROLE_ADMIN) {
        r = AUTH_ERR_FORBIDDEN_ROLE;   // before the existence check: no user enumeration
    } else if ((idx = find_user(name)) < 0) {
        r = AUTH_ERR_NOT_FOUND;
    }
    if (r == AUTH_OK && !valid_password(pass)) {
        r = AUTH_ERR_BAD_PASSWORD;
    } else if (r == AUTH_OK && strcmp(pass, "admin") == 0) {
        r = AUTH_ERR_DEFAULT_PASSWORD;   // never back to the delivery password
    }
    if (r == AUTH_OK) {
        memcpy(&s_bk.v2, &s_db, sizeof(s_db));
        if (!rec_set_password(&s_db.users[idx], pass)) {
            memcpy(&s_db, &s_bk.v2, sizeof(s_db));
            r = AUTH_ERR_IO;
        } else {
            s_db.users[idx].epoch = new_epoch(s_db.users[idx].epoch);   // same commit as the hash
            r = db_commit();
        }
    }
    if (r == AUTH_OK) {
        s_basic_cache[idx].valid = false;
        // Keep the caller's own session (if it still is the one we authenticated with).
        int keep = -1;
        if (own && s_cur.session >= 0 && s_sessions[s_cur.session].used &&
            ct_equal(s_sessions[s_cur.session].token, s_cur.token, TOKEN_LEN)) {
            keep = s_cur.session;
        }
        sessions_end_user(s_db.users[idx].name, keep);
        // Trusted devices of the user are removed - except the caller's own device on an own
        // change, which is re-issued with the new epoch so that (like the own RAM session) it
        // continues without a new login. If that save fails, the stored copy keeps the old
        // epoch: the device works until the next restart and must log in again then.
        const int keep_dev = own && current_device_is(s_cur.device) ? s_cur.device : -1;
        bool changed = devices_remove_user(s_db.users[idx].name, keep_dev);
        if (keep_dev >= 0) {
            s_dev.dev[keep_dev].epoch = s_db.users[idx].epoch;
            changed = true;
        }
        if (changed) {
            devices_save();
        }
        if (own) {
            s_cur.default_pw = false;   // the session goes on, now unrestricted
        }
        ESP_LOGI(TAG, "password of '%s' changed", s_db.users[idx].name);
    }
    unlock();
    return r;
}

// ================================================================== lifecycle

esp_err_t auth_init(void)
{
    if (s_mutex == NULL) {
        s_mutex = xSemaphoreCreateMutexStatic(&s_mutex_buf);
    }
    lock();
    sessions_wipe_all();
    memset(s_basic_cache, 0, sizeof(s_basic_cache));
    lockout_reset_all();
    esp_fill_random(s_dummy_salt, sizeof(s_dummy_salt));

    // One timed hash at start-up: shows the real cost of a password check in the log.
    {
        uint8_t h[HASH_LEN];
        const int64_t t0 = esp_timer_get_time();
        pbkdf2("benchmark", 9, s_dummy_salt, h);
        ESP_LOGI(TAG, "PBKDF2-HMAC-SHA256 (%d rounds) takes %lld ms", PBKDF2_ROUNDS,
                 (long long)((esp_timer_get_time() - t0) / 1000));
    }

    devices_make_empty(&s_dev);
    bool migrated = false;
    esp_err_t err = db_load(&migrated);
    if (err == ESP_OK) {
        s_storage_ok = true;
        ESP_LOGI(TAG, "%d user(s) loaded", s_db.count);
        if (migrated) {
            err = db_save();   // on failure the users stay valid in RAM; the next change retries
            if (err == ESP_OK) {
                ESP_LOGI(TAG, "user database saved in the new format");
            }
        }
        devices_load();   // drops devices of old layouts or epochs (also from NVS)
    } else if (err == ESP_ERR_NVS_NOT_FOUND) {
        s_storage_ok = true;
        db_make_default(&s_db);
        err = db_save();
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "no users stored - created admin/admin");
        } else {
            ESP_LOGE(TAG, "no users stored and saving failed - admin/admin in RAM only");
        }
    } else {
        // A database exists but cannot be read or validated: never fall back to a known
        // password. Every login and protected request is refused until the BOOT button reset.
        s_storage_ok = false;
        mbedtls_platform_zeroize(&s_db, sizeof(s_db));
        ESP_LOGE(TAG, "user database unreadable (%s) - all logins refused until the button reset",
                 esp_err_to_name(err));
        if (err == ESP_OK) {
            err = ESP_FAIL;
        }
    }
    s_ready = true;
    unlock();
    return err;
}

esp_err_t auth_reset_users(void)
{
    if (s_mutex == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    lock();
    sessions_wipe_all();
    memset(s_basic_cache, 0, sizeof(s_basic_cache));
    lockout_reset_all();
    db_make_default(&s_db);   // new epochs: no stored device can match any more
    devices_make_empty(&s_dev);
    esp_err_t err = db_save();
    const esp_err_t dev_err = devices_save();
    if (err == ESP_OK) {
        err = dev_err;
    }
    // The owner pressed the button: admin/admin applies (with the forced change) even if the
    // database could not be written; this also repairs an unreadable database.
    s_storage_ok = true;
    s_ready = true;
    unlock();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "users reset to admin/admin, all sessions and trusted devices ended");
    } else {
        ESP_LOGE(TAG, "users reset to admin/admin in RAM only (saving failed)");
    }
    return err;
}
