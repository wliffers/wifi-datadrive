#include "pd_dns.h"

#include <stdbool.h>
#include <string.h>
#include "esp_netif.h"
#include "lwip/ip_addr.h"
#include "lwip/pbuf.h"
#include "lwip/udp.h"

#define DNS_PORT        53
#define DNS_HDR_LEN     12
#define DNS_MAX_QUERY   512   // classic DNS-over-UDP limit; longer datagrams are dropped
#define DNS_MAX_NAME    255   // RFC 1035: a name on the wire, including the final zero byte
#define DNS_ANSWER_LEN  16    // name pointer 2, type 2, class 2, TTL 4, length 2, IPv4 4
#define DNS_TTL_S       10    // short: clients must not keep these answers after leaving the AP
#define DNS_TYPE_A      1
#define DNS_TYPE_ANY    255
#define DNS_CLASS_IN    1

// Only used in the TCP/IP thread (start/stop run there via esp_netif_tcpip_exec(), the receive
// callback always does). s_ip is written before the call that hands over to that thread.
static struct udp_pcb *s_pcb;
static uint32_t s_ip;   // network byte order

static uint16_t get16(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

/** Offset just behind the (single) question, or -1 if it is malformed. Labels are walked with a
 *  bounds check each; compression pointers are refused (a question has nothing to point back
 *  to), so there is no recursion and no loop on hostile input. */
static int question_end(const uint8_t *m, size_t len)
{
    size_t pos = DNS_HDR_LEN;
    size_t name = 1;   // the final zero byte
    while (true) {
        if (pos >= len) {
            return -1;
        }
        const uint8_t l = m[pos];
        if (l == 0) {
            pos++;
            break;
        }
        if (l > 63) {
            return -1;   // 0xC0 pointer or reserved label type
        }
        name += (size_t)l + 1;
        if (name > DNS_MAX_NAME) {
            return -1;
        }
        pos += (size_t)l + 1;
    }
    if (pos + 4 > len) {   // type and class
        return -1;
    }
    return (int)(pos + 4);
}

static void on_query(void *arg, struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *addr, u16_t port)
{
    (void)arg;
    const u16_t len = p->tot_len;
    struct pbuf *r = NULL;
    if (len >= DNS_HDR_LEN && len <= DNS_MAX_QUERY) {
        // One contiguous buffer for the answer: the query is copied in and edited in place.
        r = pbuf_alloc(PBUF_TRANSPORT, (u16_t)(len + DNS_ANSWER_LEN), PBUF_RAM);
        if (r && pbuf_copy_partial(p, r->payload, len, 0) != len) {
            pbuf_free(r);
            r = NULL;
        }
    }
    pbuf_free(p);
    if (!r) {
        return;
    }

    uint8_t *m = r->payload;
    const bool is_query = (m[2] & 0x80) == 0;
    const bool opcode_query = (m[2] & 0x78) == 0;
    const int qend = (is_query && opcode_query && get16(m + 4) == 1) ? question_end(m, len) : -1;
    if (qend < 0) {
        pbuf_free(r);   // answers, other opcodes, several questions, malformed: dropped
        return;
    }
    const uint16_t qtype = get16(m + qend - 4);
    const uint16_t qclass = get16(m + qend - 2) & 0x7FFF;
    const bool answer = qclass == DNS_CLASS_IN && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY);

    m[2] = (uint8_t)(0x80 | 0x04 | (m[2] & 0x01));   // QR, AA, RD copied (opcode is 0, TC 0)
    m[3] = 0x80;                                       // RA, NOERROR
    put16(m + 4, 1);                                   // the question is echoed
    put16(m + 6, answer ? 1 : 0);                      // AAAA, HTTPS, ...: empty answer
    put16(m + 8, 0);
    put16(m + 10, 0);                                  // EDNS and other additional data dropped
    if (answer) {
        uint8_t *a = m + qend;
        put16(a, 0xC000 | DNS_HDR_LEN);   // pointer to the name in the question
        put16(a + 2, DNS_TYPE_A);
        put16(a + 4, DNS_CLASS_IN);
        put16(a + 6, 0);
        put16(a + 8, DNS_TTL_S);
        put16(a + 10, 4);
        memcpy(a + 12, &s_ip, 4);
    }
    pbuf_realloc(r, (u16_t)(qend + (answer ? DNS_ANSWER_LEN : 0)));
    udp_sendto(pcb, r, addr, port);
    pbuf_free(r);
}

static esp_err_t start_in_tcpip(void *ctx)
{
    (void)ctx;
    if (s_pcb) {
        return ESP_OK;
    }
    struct udp_pcb *pcb = udp_new_ip_type(IPADDR_TYPE_V4);
    if (!pcb) {
        return ESP_ERR_NO_MEM;
    }
    ip_addr_t local = IPADDR4_INIT(s_ip);   // only the access point's address, not the station's
    if (udp_bind(pcb, &local, DNS_PORT) != ERR_OK) {
        udp_remove(pcb);
        return ESP_FAIL;
    }
    udp_recv(pcb, on_query, NULL);
    s_pcb = pcb;
    return ESP_OK;
}

static esp_err_t stop_in_tcpip(void *ctx)
{
    (void)ctx;
    if (s_pcb) {
        udp_remove(s_pcb);
        s_pcb = NULL;
    }
    return ESP_OK;
}

esp_err_t pd_dns_start(uint32_t ip)
{
    s_ip = ip;
    return esp_netif_tcpip_exec(start_in_tcpip, NULL);
}

void pd_dns_stop(void)
{
    esp_netif_tcpip_exec(stop_in_tcpip, NULL);
}
