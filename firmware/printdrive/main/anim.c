#include "anim.h"

#include "display.h"

// One animation: up to 16 palette entries (RGB565), frames of ANIM_W x ANIM_H palette indices,
// either packed 4 bit (two pixels per byte, high nibble first) or run-length coded
// (byte = index << 4 | (run - 1), runs of 1..16 pixels). off[] = start of each frame in data.
typedef struct {
    const uint16_t *pal;
    const uint8_t *data;
    const uint16_t *off;
    uint8_t frames;
    uint8_t rle;
    uint16_t ms;
    uint8_t loop;
} anim_def_t;

#include "anim_data.inc"      // generated: static const anim_def_t anim_defs[ANIM_COUNT]

static anim_id_t s_id;
static int64_t s_start_ms;
static int s_frame;           // frame due
static int s_drawn = -1;      // frame in the framebuffer
static bool s_busy;
static uint16_t s_buf[ANIM_W * ANIM_H];

bool anim_available(anim_id_t id)
{
    return id > ANIM_NONE && id < ANIM_COUNT && anim_defs[id].frames > 0;
}

void anim_set(anim_id_t id, int64_t now_ms)
{
    if (id == s_id) {
        return;
    }
    s_id = id;
    s_start_ms = now_ms;
    s_frame = 0;
    s_drawn = -1;
}

static int frame_at(int64_t now_ms)
{
    const anim_def_t *a = &anim_defs[s_id];
    const int64_t n = (now_ms - s_start_ms) / a->ms;
    if (a->loop) {
        return (int)(n % a->frames);
    }
    return n >= a->frames ? a->frames - 1 : (int)n;
}

bool anim_tick(int64_t now_ms)
{
    if (!anim_available(s_id)) {
        return false;
    }
    s_frame = frame_at(now_ms);
    return s_frame != s_drawn;
}

bool anim_finished(int64_t now_ms)
{
    if (!anim_available(s_id)) {
        return true;
    }
    const anim_def_t *a = &anim_defs[s_id];
    return !a->loop && now_ms - s_start_ms >= (int64_t)a->frames * a->ms;
}

void anim_draw(void)
{
    if (!anim_available(s_id)) {
        return;
    }
    const anim_def_t *a = &anim_defs[s_id];
    const uint8_t *p = a->data + a->off[s_frame];
    uint16_t *out = s_buf;
    if (a->rle) {
        for (int n = 0; n < ANIM_W * ANIM_H;) {
            const uint16_t c = a->pal[*p >> 4];
            for (int run = (*p & 15) + 1; run > 0 && n < ANIM_W * ANIM_H; run--, n++) {
                *out++ = c;
            }
            p++;
        }
    } else {
        for (int n = 0; n < ANIM_W * ANIM_H; n += 2, p++) {
            *out++ = a->pal[*p >> 4];
            *out++ = a->pal[*p & 15];
        }
    }
    display_blit(ANIM_X, ANIM_Y, ANIM_W, ANIM_H, s_buf);
    s_drawn = s_frame;
}

void anim_set_busy(bool busy)
{
    s_busy = busy;
}

bool anim_is_busy(void)
{
    return s_busy;
}
