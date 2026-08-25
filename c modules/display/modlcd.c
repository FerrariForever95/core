/*
 * moclcd.c — higher-level 8080 8-bit parallel LCD module for MicroPython,
 * built on top of ESP-IDF's esp_lcd i80 driver.
 *
 * Same known-good pin mapping and init sequence as lcd_min.c:
 * - RST: GPIO 12
 * - RS (DC): GPIO 13
 * - WR: GPIO 14
 * - RD: GPIO 41
 * - BL (Backlight): GPIO 38
 * - D0-D7: GPIOs 16, 15, 11, 10, 9, 4, 18, 17
 *
 * What's new vs lcd_min.c:
 * - panel_init() runs the exact working command sequence once (no more
 *   doing it by hand in Python).
 * - fill_rect() / fill_screen() / blit() replace the manual per-line
 *   data() calls from Python.
 * - All DMA-driven pixel streaming (fills, blit, glyphs, bmp) now goes
 *   through a small pool of DMA-capable buffers (see "DMA transfer
 *   layer" below) instead of reusing a single scratch buffer while
 *   transfers may still be in flight. Because trans_queue_depth allows
 *   several transactions to be queued on the I80 engine at once, a
 *   buffer must not be touched again until the hardware has actually
 *   finished reading it -- that's what the pool + completion callback
 *   guarantee.
 *
 * API (unchanged from previous revision):
 *   moclcd.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
 *                                             -- defaults to landscape;
 *                                                pass width=320, height=480,
 *                                                madctl=0x48 for portrait
 *   moclcd.deinit()                          -- NEW: releases bus/IO/DMA
 *                                                pool; safe to call init()
 *                                                again afterwards
 *   moclcd.reset()
 *   moclcd.panel_init()
 *   moclcd.backlight(on)                     -- digital on/off; drives PWM duty
 *                                                to max/0 instead if backlight_init()
 *                                                was called
 *   moclcd.backlight_init(freq_hz=5000, resolution_bits=8)
 *                                             -- sets up LEDC PWM on the BL pin
 *   moclcd.backlight_set(level)              -- level is 0.0-1.0 brightness fraction,
 *                                                requires backlight_init() first
 *   moclcd.cmd(cmd, params=None)     -- raw passthrough, still available
 *   moclcd.data(buf)                 -- raw passthrough, still available
 *   moclcd.fill_rect(x, y, w, h, color)      -- raises ValueError if out of bounds
 *   moclcd.fill_screen(color)
 *   moclcd.blit(x, y, w, h, buf)             -- buf is raw RGB565 bytes, MSB first
 *   moclcd.draw_pixel(x, y, color)           -- clipped silently if off-panel
 *   moclcd.draw_line(x0, y0, x1, y1, color)  -- clipped silently if off-panel
 *   moclcd.draw_rect(x, y, w, h, color)      -- outline; clipped silently if off-panel
 *   moclcd.draw_circle(x0, y0, r, color)     -- outline; clipped silently if off-panel
 *   moclcd.fill_circle(x0, y0, r, color)     -- filled; clipped silently if off-panel
 *   moclcd.draw_text8x8(x, y, text, fg, bg=None)
 *   moclcd.draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None)
 *                                             -- w/h now really scale the
 *                                                image (nearest-neighbour)
 *                                                when they differ from the
 *                                                BMP's native size; max_w/
 *                                                max_h additionally clamp
 *                                                the *output* size.
 */

#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "mphalport.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_heap_caps.h"
#include "driver/ledc.h"
#include "extmod/font_petme128_8x8.h"   /* same 8x8 font MicroPython's framebuf.text() uses */
#include "py/mperrno.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define LCD_CMD_CASET  0x2A
#define LCD_CMD_PASET  0x2B
#define LCD_CMD_RAMWR  0x2C
#define LCD_CMD_RAMWRC 0x3C   /* continuation write, used for pixel streaming */

/* how many pixels one DMA pool buffer holds (2 bytes/pixel -> 4KB chunks) */
#define FILL_CHUNK_PIXELS 2048
#define FILL_CHUNK_BYTES  (FILL_CHUNK_PIXELS * 2)

/* Number of buffers in the DMA pool. Sized against trans_queue_depth so
 * the hardware can have that many transactions queued while we still
 * have a spare buffer or two to prepare the next chunk into. */
#define LCD_TRANS_QUEUE_DEPTH 10
#define LCD_DMA_POOL_SLOTS    (LCD_TRANS_QUEUE_DEPTH + 2)

/* -------------------------------------------------------------------
 * DMA transfer layer
 *
 *   lcd_dma_acquire()  -- block (busy-poll, no RTOS blocking primitives
 *                          needed since this all runs on the MicroPython
 *                          task) until a free buffer is available, mark
 *                          it in-flight, return it.
 *   lcd_dma_submit()   -- hand a buffer + address window to the I80
 *                          queue via esp_lcd_panel_io_tx_color(). The
 *                          buffer stays marked in-flight; ownership does
 *                          NOT return to the caller.
 *   lcd_dma_release()  -- called from the esp_lcd "color trans done"
 *                          callback (interrupt/task context supplied by
 *                          esp_lcd) once the hardware has actually
 *                          finished reading a buffer. Marks the oldest
 *                          still-in-flight slot free again.
 *   lcd_dma_wait_idle()-- block until every pool slot is free again.
 *                          Used before deinit() and anywhere the driver
 *                          needs a hard sync point (e.g. before reusing
 *                          host-side memory that isn't itself pool
 *                          memory).
 *
 * This is the single path fill_rect(), fill_screen(), blit(),
 * draw_text8x8() and draw_bmp() all funnel through -- no more ad-hoc
 * heap_caps_malloc()/free() around individual tx_color() calls.
 *
 * IMPORTANT implementation note: esp_lcd_panel_io_i80_config_t's
 * on_color_trans_done callback is registered once per panel IO, and
 * its user_ctx is fixed at esp_lcd_new_panel_io_i80() time -- it is
 * NOT a per-transaction context, so the callback cannot be told
 * directly "slot #N just finished". Instead we rely on the I80 driver
 * completing transactions strictly in submission (FIFO) order on a
 * single queue: we keep our own FIFO of which pool slot each submitted
 * transaction used, and on each completion callback we pop the oldest
 * outstanding entry and mark that slot free. This matches how the
 * underlying hardware queue actually behaves and avoids needing a
 * per-transaction user_ctx.
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t  *buf;        /* FILL_CHUNK_BYTES, MALLOC_CAP_DMA */
    volatile bool in_flight;
} lcd_dma_slot_t;

static lcd_dma_slot_t s_dma_pool[LCD_DMA_POOL_SLOTS];
static bool           s_dma_pool_inited = false;

/* FIFO of slot indices for transactions currently queued/in flight on
 * the I80 driver, in submission order. Sized one larger than the pool
 * so it can never be mistaken for empty when full. Only ever touched
 * with interrupts effectively serialized against the single
 * MicroPython task that calls lcd_dma_submit(), except for the pop in
 * the completion callback -- guarded by disabling interrupts briefly
 * since the callback may run from an ISR context depending on esp_lcd
 * configuration. */
static volatile int s_inflight_fifo[LCD_DMA_POOL_SLOTS + 1];
static volatile int s_inflight_head = 0; /* next to pop (oldest) */
static volatile int s_inflight_tail = 0; /* next free slot to push */

static inline int fifo_next(int i) { return (i + 1) % (LCD_DMA_POOL_SLOTS + 1); }

/* ---- module / driver state ---- */
static esp_lcd_i80_bus_handle_t  s_bus       = NULL;
static esp_lcd_panel_io_handle_t s_io        = NULL;
static mp_hal_pin_obj_t          s_reset_pin = 12;
static mp_hal_pin_obj_t          s_bl_pin    = 38;
static mp_hal_pin_obj_t          s_rd_pin    = 41;
static bool                      s_has_reset = false;
static uint16_t                  s_width     = 480;
static uint16_t                  s_height    = 320;
static uint8_t                   s_madctl    = 0x28; /* landscape (MV set); 0x48=portrait, 0x88/0xE8=other rotations */
static bool                      s_bl_pwm_inited = false;
static uint32_t                  s_bl_duty_max   = 255; /* set by backlight_init() from resolution_bits */

/* explicit lifecycle stages, per requirement #9 */
static bool s_initialized        = false; /* init() succeeded: bus+io live */
static bool s_panel_initialized  = false; /* panel_init() succeeded */

#define FONT_CHAR_W     8
#define FONT_CHAR_H     8
#define FONT_FIRST_CHAR 32
#define FONT_LAST_CHAR  127

/* -------------------------------------------------------------------
 * helpers
 * ---------------------------------------------------------------- */
static void io_check(esp_err_t ret, const char *what)
{
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("%s failed: %d"), what, ret);
    }
}

static void require_init(void)
{
    if (!s_initialized || s_io == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("moclcd.init() must be called first"));
    }
}

static void lcd_cmd_raw(uint8_t cmd, const void *buf, size_t len)
{
    io_check(esp_lcd_panel_io_tx_param(s_io, cmd, buf, len), "cmd");
}

/* esp_lcd invokes this once a queued color transaction has actually
 * been shifted out over the bus and the buffer is safe to reuse. This
 * is the only place any pool slot is marked free again -- callers
 * never assume tx_color() has synchronously consumed the buffer.
 *
 * The I80 driver completes transactions in the order they were
 * submitted (single hardware queue), so each callback corresponds to
 * the oldest entry in our in-flight FIFO -- see the design note above
 * s_dma_pool for why we can't get the slot directly via user_ctx. */
static bool IRAM_ATTR lcd_color_trans_done_cb(esp_lcd_panel_io_handle_t panel_io,
                                               esp_lcd_panel_io_event_data_t *edata,
                                               void *user_ctx)
{
    if (s_inflight_head != s_inflight_tail) {
        int slot_idx = s_inflight_fifo[s_inflight_head];
        s_inflight_head = fifo_next(s_inflight_head);
        s_dma_pool[slot_idx].in_flight = false;
    }
    return false; /* no high-priority task wakeup needed */
}

static void lcd_dma_pool_init(void)
{
    if (s_dma_pool_inited) return;
    for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
        s_dma_pool[i].buf = heap_caps_malloc(FILL_CHUNK_BYTES, MALLOC_CAP_DMA);
        s_dma_pool[i].in_flight = false;
        if (s_dma_pool[i].buf == NULL) {
            /* unwind what we did allocate before raising */
            for (int j = 0; j <= i; j++) {
                if (s_dma_pool[j].buf) {
                    heap_caps_free(s_dma_pool[j].buf);
                    s_dma_pool[j].buf = NULL;
                }
            }
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no DMA memory for transfer pool"));
        }
    }
    s_dma_pool_inited = true;
}

static void lcd_dma_pool_deinit(void)
{
    if (!s_dma_pool_inited) return;
    for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
        if (s_dma_pool[i].buf) {
            heap_caps_free(s_dma_pool[i].buf);
            s_dma_pool[i].buf = NULL;
        }
        s_dma_pool[i].in_flight = false;
    }
    s_inflight_head = 0;
    s_inflight_tail = 0;
    s_dma_pool_inited = false;
}

/* Block until every in-flight slot has been released by the completion
 * callback. Used by deinit() so we never free pool memory (or the
 * panel IO) while the DMA engine might still touch it. */
static void lcd_dma_wait_idle(void)
{
    if (!s_dma_pool_inited) return;
    bool busy;
    do {
        busy = false;
        for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
            if (s_dma_pool[i].in_flight) { busy = true; break; }
        }
        if (busy) {
            MICROPY_EVENT_POLL_HOOK
        }
    } while (busy);
}

/* Acquire a free pool slot, blocking (yielding to other MicroPython/
 * event-loop work via MICROPY_EVENT_POLL_HOOK) until the completion
 * callback frees one up. Never returns a slot that DMA might still be
 * reading. Returns the slot's index (used to push into the in-flight
 * FIFO at submit time) via *out_idx. */
static lcd_dma_slot_t *lcd_dma_acquire(int *out_idx)
{
    lcd_dma_pool_init();
    for (;;) {
        for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
            if (!s_dma_pool[i].in_flight) {
                *out_idx = i;
                return &s_dma_pool[i];
            }
        }
        MICROPY_EVENT_POLL_HOOK
    }
}

/* Submit `len` bytes from an acquired slot's buffer as one async color
 * transfer. The slot stays marked in-flight; it is only released again
 * by the completion callback popping it off the in-flight FIFO, never
 * here. `cmd` is normally LCD_CMD_RAMWRC. */
static void lcd_dma_submit(lcd_dma_slot_t *slot, int slot_idx, uint8_t cmd, size_t len)
{
    slot->in_flight = true;

    /* Record this slot in the in-flight FIFO *before* submitting, so
       there's no window where the transaction could complete (and the
       callback run) before we know which slot it corresponds to. The
       pool is sized LCD_TRANS_QUEUE_DEPTH+2 and the FIFO array is one
       larger than the pool, so this can never overflow. */
    s_inflight_fifo[s_inflight_tail] = slot_idx;
    s_inflight_tail = fifo_next(s_inflight_tail);

    esp_err_t ret = esp_lcd_panel_io_tx_color(s_io, cmd, slot->buf, len);
    if (ret != ESP_OK) {
        /* submission itself failed synchronously -- the hardware never
           saw the buffer, so undo the FIFO push and free the slot
           ourselves rather than waiting on a callback that will never
           fire. Since this is the most recently pushed (tail) entry,
           it's safe to just roll the tail back. */
        s_inflight_tail = (s_inflight_tail - 1 + (LCD_DMA_POOL_SLOTS + 1)) % (LCD_DMA_POOL_SLOTS + 1);
        slot->in_flight = false;
        io_check(ret, "dma submit");
    }
}

/* CASET / PASET / RAMWR — sets the address window and arms the panel
 * for a pixel stream, exactly like begin_write() did in the Python demo. */
static void set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    uint8_t caset[4] = { (uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF),
                         (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    uint8_t paset[4] = { (uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF),
                         (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    lcd_cmd_raw(LCD_CMD_CASET, caset, sizeof(caset));
    lcd_cmd_raw(LCD_CMD_PASET, paset, sizeof(paset));
    lcd_cmd_raw(LCD_CMD_RAMWR, NULL, 0);
}

/* stream `total_pixels` copies of `color` right after the address
 * window has been armed via set_window(). Shared by fill_rect() and by
 * the line/rect/circle primitives below so they all get the same
 * chunked, DMA-pipelined path via the pool.
 *
 * Each chunk gets its own freshly-acquired slot -- we do NOT fill one
 * buffer once and resubmit it, because a solid-color chunk is cheap to
 * refill and this keeps the ownership rule uniform (a slot, once
 * submitted, is never touched again until its callback fires). */
static void stream_solid(uint32_t total_pixels, uint16_t color)
{
    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);

    uint32_t remaining = total_pixels;
    while (remaining > 0) {
        uint32_t n = remaining < FILL_CHUNK_PIXELS ? remaining : FILL_CHUNK_PIXELS;

        int idx;
        lcd_dma_slot_t *slot = lcd_dma_acquire(&idx);
        for (uint32_t i = 0; i < n; i++) {
            slot->buf[2 * i]     = hi;
            slot->buf[2 * i + 1] = lo;
        }
        lcd_dma_submit(slot, idx, LCD_CMD_RAMWRC, (size_t)n * 2);

        remaining -= n;
    }
}

/* Stream raw RGB565 bytes from an arbitrary source buffer (which may
 * be MicroPython-owned memory that must not be handed to DMA directly)
 * by copying it through the pool in chunks. Used by blit(). Copies add
 * a small amount of CPU work but keep every DMA-visible buffer owned
 * by us for its entire in-flight lifetime, which is required since we
 * cannot pin/borrow the Python object across an async transfer. */
static void stream_from_buffer(const uint8_t *src, size_t total_bytes)
{
    size_t remaining = total_bytes;
    const uint8_t *p = src;
    while (remaining > 0) {
        size_t n = remaining < FILL_CHUNK_BYTES ? remaining : FILL_CHUNK_BYTES;

        int idx;
        lcd_dma_slot_t *slot = lcd_dma_acquire(&idx);
        memcpy(slot->buf, p, n);
        lcd_dma_submit(slot, idx, LCD_CMD_RAMWRC, n);

        p += n;
        remaining -= n;
    }
}

/* Clip a rectangle to the panel bounds in place. Returns false if the
 * result is empty (nothing to draw), unlike the strict fill_rect()
 * below which raises on out-of-bounds. Shapes like circles and lines
 * routinely have parts that fall off the edge, so the primitives that
 * build on this clip silently instead of erroring. */
static bool clip_rect(int *x, int *y, int *w, int *h)
{
    if (*x < 0) { *w += *x; *x = 0; }
    if (*y < 0) { *h += *y; *y = 0; }
    if (*x + *w > s_width)  *w = (int)s_width  - *x;
    if (*y + *h > s_height) *h = (int)s_height - *y;
    return (*w > 0 && *h > 0 && *x < s_width && *y < s_height);
}

static void do_fill_rect_clip(int x, int y, int w, int h, uint16_t color)
{
    if (!clip_rect(&x, &y, &w, &h)) return;
    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + w - 1), (uint16_t)(y + h - 1));
    stream_solid((uint32_t)w * (uint32_t)h, color);
}

static void do_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= s_width || y >= s_height) return;
    set_window((uint16_t)x, (uint16_t)y, (uint16_t)x, (uint16_t)y);
    stream_solid(1, color);
}

/* -------------------------------------------------------------------
 * span helpers -- group contiguous pixels from Bresenham walks
 * (draw_line diagonals, draw_circle outlines) into horizontal runs so
 * they go out as one address-window + DMA burst instead of one
 * transaction per pixel. Kept intentionally simple: a "span" here is a
 * run of consecutive x values at a fixed y, flushed with do_fill_rect_clip.
 * ---------------------------------------------------------------- */
typedef struct {
    int x0, y, x1; /* inclusive x0..x1 at row y; open == (x1 < x0) */
    bool open;
} span_t;

static void span_reset(span_t *s)
{
    s->x0 = s->y = s->x1 = 0;
    s->open = false;
}

static void span_flush(span_t *s, uint16_t color)
{
    if (s->open) {
        do_fill_rect_clip(s->x0, s->y, s->x1 - s->x0 + 1, 1, color);
        s->open = false;
    }
}

/* Feed one more pixel into the span accumulator. Pixels must be fed in
 * increasing-x order within a row for coalescing to trigger; if x/y
 * don't extend the current run, the run is flushed and a new one is
 * started. Non-contiguous callers (e.g. circle's multiple symmetric
 * points) still work correctly -- they just won't coalesce with each
 * other, which is fine since they aren't adjacent anyway. */
static void span_feed(span_t *s, int x, int y, uint16_t color)
{
    if (s->open && y == s->y && x == s->x1 + 1) {
        s->x1 = x;
        return;
    }
    span_flush(s, color);
    s->x0 = s->x1 = x;
    s->y  = y;
    s->open = true;
}

/* -------------------------------------------------------------------
 * text rendering -- font_petme128_8x8 is column-major: 8 bytes/char,
 * byte i is column i, bit j of that byte is row j. Same table and
 * bit layout MicroPython's framebuf.text() uses internally.
 *
 * Glyphs now draw through the shared DMA pool (lcd_dma_acquire /
 * lcd_dma_submit) instead of one long-lived reusable buffer, so a
 * glyph is never overwritten while a previous character's transfer is
 * still in flight -- yet we still avoid a malloc/free per character
 * since pool slots are preallocated and just get recycled.
 * ---------------------------------------------------------------- */

/* Draws one 8x8 glyph at (x,y). If bg_transparent, only foreground
 * pixels are plotted (one address-window per lit pixel run -- slower,
 * but leaves whatever's already behind the glyph untouched). Otherwise
 * the whole 8x8 cell (fg+bg) is built into a pool slot and sent as a
 * single DMA transfer when it fully fits on-panel. */
static void draw_glyph(int x, int y, char c, uint16_t fg, uint16_t bg, bool bg_transparent)
{
    if (c < FONT_FIRST_CHAR || c > FONT_LAST_CHAR) c = ' ';
    const uint8_t *glyph = &font_petme128_8x8[(c - FONT_FIRST_CHAR) * 8];

    if (bg_transparent) {
        /* plot each row's lit run as a span so contiguous lit pixels in
           a row still coalesce into one transfer instead of one per pixel */
        for (int row = 0; row < FONT_CHAR_H; row++) {
            span_t s;
            span_reset(&s);
            for (int col = 0; col < FONT_CHAR_W; col++) {
                if ((glyph[col] >> row) & 1) {
                    span_feed(&s, x + col, y + row, fg);
                }
            }
            span_flush(&s, fg);
        }
        return;
    }

    int cx = x, cy = y, cw = FONT_CHAR_W, ch = FONT_CHAR_H;
    if (!clip_rect(&cx, &cy, &cw, &ch)) return;

    if (cw != FONT_CHAR_W || ch != FONT_CHAR_H) {
        /* clipped by a screen edge: fall back to per-pixel so we don't
           send pixels that belong to a different part of the screen */
        for (int col = 0; col < FONT_CHAR_W; col++) {
            uint8_t line = glyph[col];
            for (int row = 0; row < FONT_CHAR_H; row++) {
                do_draw_pixel(x + col, y + row, ((line >> row) & 1) ? fg : bg);
            }
        }
        return;
    }

    /* Full 8x8 cell fits FILL_CHUNK_BYTES easily (128 bytes), so one
       pool slot is always enough for one glyph. */
    uint8_t fg_hi = (uint8_t)(fg >> 8), fg_lo = (uint8_t)(fg & 0xFF);
    uint8_t bg_hi = (uint8_t)(bg >> 8), bg_lo = (uint8_t)(bg & 0xFF);

    int idx;
    lcd_dma_slot_t *slot = lcd_dma_acquire(&idx);
    for (int row = 0; row < FONT_CHAR_H; row++) {
        for (int col = 0; col < FONT_CHAR_W; col++) {
            bool on = (glyph[col] >> row) & 1;
            int p = (row * FONT_CHAR_W + col) * 2;
            slot->buf[p]     = on ? fg_hi : bg_hi;
            slot->buf[p + 1] = on ? fg_lo : bg_lo;
        }
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + FONT_CHAR_W - 1), (uint16_t)(y + FONT_CHAR_H - 1));
    lcd_dma_submit(slot, idx, LCD_CMD_RAMWRC, FONT_CHAR_W * FONT_CHAR_H * 2);
}

/* -------------------------------------------------------------------
 * moclcd.draw_text8x8(x, y, text, fg, bg=None)
 * bg omitted/None -> transparent background (only fg pixels drawn).
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_text8x8(size_t n_args, const mp_obj_t *args_in)
{
    require_init();

    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);

    size_t len;
    const char *text = mp_obj_str_get_data(args_in[2], &len);

    uint16_t fg = (uint16_t)mp_obj_get_int(args_in[3]);

    bool bg_transparent = (n_args < 5) || (args_in[4] == mp_const_none);
    uint16_t bg = bg_transparent ? 0 : (uint16_t)mp_obj_get_int(args_in[4]);

    /* guard against pathological x + len*8 overflow on 32-bit int */
    if (len > (size_t)(INT32_MAX / FONT_CHAR_W)) {
        mp_raise_ValueError(MP_ERROR_TEXT("text too long"));
    }

    for (size_t i = 0; i < len; i++) {
        draw_glyph(x + (int)i * FONT_CHAR_W, y, text[i], fg, bg, bg_transparent);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text8x8_obj, 4, 5, moclcd_draw_text8x8);

/* -------------------------------------------------------------------
 * moclcd.draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None)
 *
 * Minimal loader: uncompressed 24-bit BMP only (no palette, no RLE).
 * w/h/max_w/max_h of 0 (the default) are treated as "unset", same as
 * the Python version's None.
 *
 * Semantics (made internally consistent):
 *   - w/h, if given, are the OUTPUT size in pixels. If they differ from
 *     the BMP's native width/height, the image is nearest-neighbour
 *     scaled to fit -- this actually happens now, it isn't just
 *     accepted and ignored.
 *   - max_w/max_h, if given, additionally clamp the output size (after
 *     w/h are applied) so e.g. a caller can say "draw at up to 100x100"
 *     without knowing the source image's dimensions.
 *   - The result is then clipped to the panel and to (x, y).
 *
 * DMA safety: the image is streamed through the shared DMA pool one
 * row-chunk at a time (via stream_from_buffer on a per-row scratch
 * buffer), rather than being malloc'd whole, submitted, and freed
 * immediately after -- the old code could free `img` while esp_lcd
 * still had it queued. Row buffers used for *decoding* (row_buf, and
 * the small per-output-row RGB565 buffer) are plain heap memory that
 * is never itself hard to the DMA engine; only pool slots are, and
 * lcd_dma_submit()/the completion callback own their lifetime.
 *
 * Continues to support: uncompressed 24-bit BMP, bottom-up and
 * top-down row order, RGB888->RGB565 conversion, clipping. Anything
 * else (compression, non-24bpp) is rejected with ValueError.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_bmp(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    require_init();

    enum { ARG_path, ARG_x, ARG_y, ARG_w, ARG_h, ARG_max_w, ARG_max_h };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_path,  MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_rom_obj = MP_ROM_NONE} },
        { MP_QSTR_x,     MP_ARG_REQUIRED | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_y,     MP_ARG_REQUIRED | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_w,     MP_ARG_KW_ONLY  | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_h,     MP_ARG_KW_ONLY  | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_max_w, MP_ARG_KW_ONLY  | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_max_h, MP_ARG_KW_ONLY  | MP_ARG_INT, {.u_int = 0} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    const char *path = mp_obj_str_get_str(args[ARG_path].u_obj);
    int x = args[ARG_x].u_int;
    int y = args[ARG_y].u_int;
    int want_w = args[ARG_w].u_int;
    int want_h = args[ARG_h].u_int;
    int max_w  = args[ARG_max_w].u_int;
    int max_h  = args[ARG_max_h].u_int;

    if (want_w < 0 || want_h < 0 || max_w < 0 || max_h < 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("negative bmp dimension"));
    }

    FILE *f = fopen(path, "rb");
    if (!f) {
        mp_raise_OSError(MP_ENOENT);
    }

    uint8_t header[54];
    if (fread(header, 1, 54, f) != 54 || header[0] != 'B' || header[1] != 'M') {
        fclose(f);
        mp_raise_ValueError(MP_ERROR_TEXT("not a BMP file"));
    }

    uint32_t data_offset = (uint32_t)header[10] | ((uint32_t)header[11] << 8) |
                           ((uint32_t)header[12] << 16) | ((uint32_t)header[13] << 24);
    int32_t bmp_w = (int32_t)((uint32_t)header[18] | ((uint32_t)header[19] << 8) |
                              ((uint32_t)header[20] << 16) | ((uint32_t)header[21] << 24));
    int32_t bmp_h_raw = (int32_t)((uint32_t)header[22] | ((uint32_t)header[23] << 8) |
                                  ((uint32_t)header[24] << 16) | ((uint32_t)header[25] << 24));
    uint16_t bpp = (uint16_t)header[28] | ((uint16_t)header[29] << 8);
    uint32_t compression = (uint32_t)header[30] | ((uint32_t)header[31] << 8) |
                           ((uint32_t)header[32] << 16) | ((uint32_t)header[33] << 24);

    if (bpp != 24 || compression != 0) {
        fclose(f);
        mp_raise_ValueError(MP_ERROR_TEXT("only uncompressed 24-bit BMP is supported"));
    }

    if (bmp_w <= 0 || bmp_w > 8192 || bmp_h_raw == 0 ||
        (bmp_h_raw > 8192) || (bmp_h_raw < -8192)) {
        fclose(f);
        mp_raise_ValueError(MP_ERROR_TEXT("invalid or unsupported bmp dimensions"));
    }

    bool top_down = bmp_h_raw < 0;
    int32_t bmp_h = top_down ? -bmp_h_raw : bmp_h_raw;
    /* row_size padded to 4 bytes; bmp_w bounded above so this can't overflow int */
    int row_size = ((bmp_w * 3 + 3) / 4) * 4;

    /* --- resolve output size (this is where real scaling now happens) --- */
    int out_w = want_w > 0 ? want_w : (int)bmp_w;
    int out_h = want_h > 0 ? want_h : (int)bmp_h;
    if (max_w > 0 && out_w > max_w) out_w = max_w;
    if (max_h > 0 && out_h > max_h) out_h = max_h;

    if (out_w <= 0 || out_h <= 0) {
        fclose(f);
        return mp_const_none;
    }
    /* sane ceiling: never try to materialize an absurd output size */
    if (out_w > 4096 || out_h > 4096) {
        fclose(f);
        mp_raise_ValueError(MP_ERROR_TEXT("bmp output size too large"));
    }

    bool scaling = (out_w != (int)bmp_w) || (out_h != (int)bmp_h);

    /* --- clip destination rect to panel + honor (x, y) --- */
    int dx = x, dy = y, dw = out_w, dh = out_h;
    if (!clip_rect(&dx, &dy, &dw, &dh)) {
        fclose(f);
        return mp_const_none;
    }
    int skip_out_rows = dy - y; /* how many *output* rows/cols the top/left clip ate */
    int skip_out_cols = dx - x;

    uint8_t *row_buf = heap_caps_malloc(row_size, MALLOC_CAP_DEFAULT);
    /* one converted output row at a time -- small, fixed, no full-image
       allocation, and never handed to DMA directly (it's copied into
       pool slots by stream_from_buffer). */
    uint8_t *out_row = heap_caps_malloc((size_t)dw * 2, MALLOC_CAP_DEFAULT);
    if (!row_buf || !out_row) {
        if (row_buf) heap_caps_free(row_buf);
        if (out_row) heap_caps_free(out_row);
        fclose(f);
        mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no memory for bmp load"));
    }

    /* For scaling we need random access to source rows, and each output
       row may reuse or skip source rows depending on the scale factor.
       We still only ever hold ONE source row (row_buf) and ONE output
       row (out_row) in memory -- no whole-image buffer -- by re-seeking
       per output row. This costs some re-read I/O under upscaling, but
       keeps memory bounded regardless of image size, which matters more
       on this target than raw decode throughput. */
    set_window((uint16_t)dx, (uint16_t)dy, (uint16_t)(dx + dw - 1), (uint16_t)(dy + dh - 1));

    int last_src_row = -1;
    for (int out_row_idx = 0; out_row_idx < dh; out_row_idx++) {
        int dest_row = out_row_idx + skip_out_rows; /* row within full out_h image */

        int src_row;
        if (scaling) {
            src_row = (int)(((int64_t)dest_row * bmp_h) / out_h);
            if (src_row >= (int)bmp_h) src_row = (int)bmp_h - 1;
        } else {
            src_row = dest_row;
        }

        int file_row = top_down ? src_row : ((int)bmp_h - 1 - src_row);

        if (file_row != last_src_row) {
            fseek(f, (long)(data_offset + (uint32_t)file_row * (uint32_t)row_size), SEEK_SET);
            if (fread(row_buf, 1, row_size, f) != (size_t)row_size) {
                break; /* short read / EOF: stop rather than send garbage rows */
            }
            last_src_row = file_row;
        }

        int p = 0;
        for (int out_col = 0; out_col < dw; out_col++) {
            int dest_col = out_col + skip_out_cols;
            int src_col;
            if (scaling) {
                src_col = (int)(((int64_t)dest_col * bmp_w) / out_w);
                if (src_col >= (int)bmp_w) src_col = (int)bmp_w - 1;
            } else {
                src_col = dest_col;
            }
            uint8_t b = row_buf[src_col * 3 + 0];
            uint8_t g = row_buf[src_col * 3 + 1];
            uint8_t r = row_buf[src_col * 3 + 2];
            uint16_t c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
            out_row[p++] = (uint8_t)(c >> 8);
            out_row[p++] = (uint8_t)(c & 0xFF);
        }

        stream_from_buffer(out_row, (size_t)dw * 2);
    }

    heap_caps_free(row_buf);
    heap_caps_free(out_row);
    fclose(f);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_draw_bmp_obj, 3, moclcd_draw_bmp);

/* -------------------------------------------------------------------
 * moclcd.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_pclk, ARG_width, ARG_height, ARG_madctl };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_pclk,   MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 10000000} },
        { MP_QSTR_width,  MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 480} },
        { MP_QSTR_height, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 320} },
        /* 0x28 = landscape (MV set). 0x48 = portrait (original orientation).
           0x88 / 0xE8 = the other two 90-degree rotations. If the image
           comes up mirrored or upside down in landscape, try 0xE8. */
        { MP_QSTR_madctl, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 0x28} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    int width  = args[ARG_width].u_int;
    int height = args[ARG_height].u_int;
    int pclk   = args[ARG_pclk].u_int;
    int madctl = args[ARG_madctl].u_int;

    if (width <= 0 || height <= 0 || width > 2000 || height > 2000) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid width/height"));
    }
    if (pclk <= 0 || pclk > 40000000) {
        /* ILI9488 8080 timing on this wiring has been validated up to
           ~20-25MHz in practice; 40MHz is a hard ceiling to reject
           obviously-bogus values, not a recommendation to run that
           fast -- see driver notes for PCLK guidance. */
        mp_raise_ValueError(MP_ERROR_TEXT("invalid pclk"));
    }
    if ((madctl & ~0xFF) != 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid madctl"));
    }

    /* calling init() again: cleanly tear down any previous bus/IO/pool
       first so resources never leak across repeated init() calls */
    if (s_initialized) {
        lcd_dma_wait_idle();
        lcd_dma_pool_deinit();
        if (s_io)  { esp_lcd_panel_io_del(s_io);  s_io  = NULL; }
        if (s_bus) { esp_lcd_del_i80_bus(s_bus);  s_bus = NULL; }
        s_initialized       = false;
        s_panel_initialized = false;
    }

    s_width  = (uint16_t)width;
    s_height = (uint16_t)height;
    s_madctl = (uint8_t)madctl;

    /* --- Your exact data pins (D0 through D7) --- */
    int data_gpios[8] = { 16, 15, 11, 10, 9, 4, 18, 17 };

    esp_lcd_i80_bus_config_t bus_cfg = {
        .dc_gpio_num = 13, /* RS */
        .wr_gpio_num = 14, /* WR */
        .clk_src     = LCD_CLK_SRC_PLL160M,
        .data_gpio_nums = {
            data_gpios[0], data_gpios[1], data_gpios[2], data_gpios[3],
            data_gpios[4], data_gpios[5], data_gpios[6], data_gpios[7],
        },
        .bus_width          = 8,
        /* Pool chunks (FILL_CHUNK_BYTES) are the largest single transfer
           we ever submit now, so max_transfer_bytes only needs to cover
           one chunk plus headroom -- not a whole frame -- since the DMA
           transfer layer always chunks large operations itself. */
        .max_transfer_bytes = FILL_CHUNK_BYTES,
    };
    io_check(esp_lcd_new_i80_bus(&bus_cfg, &s_bus), "esp_lcd_new_i80_bus");

    esp_lcd_panel_io_i80_config_t io_cfg = {
        .cs_gpio_num       = -1, /* CS tied LOW in hardware */
        .pclk_hz           = (uint32_t)pclk,
        .trans_queue_depth = LCD_TRANS_QUEUE_DEPTH,
        .dc_levels = {
            .dc_idle_level  = 0,
            .dc_cmd_level   = 0,
            .dc_dummy_level = 0,
            .dc_data_level  = 1,
        },
        .lcd_cmd_bits   = 8,
        .lcd_param_bits = 8,
        .on_color_trans_done = lcd_color_trans_done_cb,
        /* user_ctx is per-transaction, not per-config, so it can't be
           set here -- see lcd_dma_submit()/the callback for how the
           right slot is identified instead. */
    };
    io_check(esp_lcd_new_panel_io_i80(s_bus, &io_cfg, &s_io), "esp_lcd_new_panel_io_i80");

    /* --- RD pin, idle HIGH --- */
    mp_hal_pin_output(s_rd_pin);
    mp_hal_pin_write(s_rd_pin, 1);

    /* --- Backlight, ON --- */
    mp_hal_pin_output(s_bl_pin);
    mp_hal_pin_write(s_bl_pin, 1);

    /* --- Reset pin, idle HIGH --- */
    mp_hal_pin_output(s_reset_pin);
    mp_hal_pin_write(s_reset_pin, 1);
    s_has_reset = true;

    lcd_dma_pool_init();

    s_initialized = true;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

/* -------------------------------------------------------------------
 * moclcd.deinit()
 * Waits for any in-flight DMA to complete, then tears down the panel
 * IO, the I80 bus, and the DMA pool, and resets lifecycle state so
 * init() can be called again cleanly. Safe to call multiple times or
 * when never initialized.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_deinit(void)
{
    if (!s_initialized) {
        return mp_const_none;
    }

    lcd_dma_wait_idle();
    lcd_dma_pool_deinit();

    if (s_io)  { esp_lcd_panel_io_del(s_io);  s_io  = NULL; }
    if (s_bus) { esp_lcd_del_i80_bus(s_bus);  s_bus = NULL; }

    s_initialized       = false;
    s_panel_initialized = false;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_deinit_obj, moclcd_deinit);

/* -------------------------------------------------------------------
 * moclcd.reset()
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_reset(void)
{
    if (!s_has_reset) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("no reset pin configured"));
    }
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_us(1000 * 1000);
    mp_hal_pin_write(s_reset_pin, 0);
    mp_hal_delay_us(1000 * 1000);
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_us(150 * 1000);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_reset_obj, moclcd_reset);

/* -------------------------------------------------------------------
 * moclcd.panel_init()
 * Runs the exact working 0x01 / 0x11 / 0x3A / 0x36 / 0x2A / 0x2B / 0x29
 * sequence from the Python script, sized to width/height from init().
 * Unchanged from the known-good sequence/timings.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_panel_init(void)
{
    require_init();

    /* matches time.sleep_ms(20) between reset() and the 0x01 command in
       the working Python script -- in raw C there's no interpreter
       overhead to give you this gap for free, so it's made explicit */
    mp_hal_delay_us(20 * 1000);

    lcd_cmd_raw(0x01, NULL, 0);              /* software reset */
    mp_hal_delay_us(150 * 1000);

    lcd_cmd_raw(0x11, NULL, 0);              /* sleep out */
    mp_hal_delay_us(150 * 1000);

    uint8_t colmod = 0x55;
    lcd_cmd_raw(0x3A, &colmod, 1);           /* 16bpp */
    mp_hal_delay_us(10 * 1000);

    uint8_t madctl = s_madctl;
    lcd_cmd_raw(0x36, &madctl, 1);
    mp_hal_delay_us(10 * 1000);

    uint16_t x1 = s_width - 1;
    uint16_t y1 = s_height - 1;
    uint8_t caset[4] = { 0x00, 0x00, (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    lcd_cmd_raw(0x2A, caset, sizeof(caset));

    uint8_t paset[4] = { 0x00, 0x00, (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    lcd_cmd_raw(0x2B, paset, sizeof(paset));

    lcd_cmd_raw(0x29, NULL, 0);              /* display on */
    mp_hal_delay_us(50 * 1000);

    s_panel_initialized = true;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

/* -------------------------------------------------------------------
 * moclcd.backlight_init(freq_hz=5000, resolution_bits=8)
 * Sets up an LEDC PWM channel on the backlight pin. Call once, before
 * using backlight_set() or expecting backlight() to dim rather than
 * just switch on/off.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_backlight_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_freq, ARG_res_bits };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_freq_hz,          MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 5000} },
        { MP_QSTR_resolution_bits,  MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 8} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    int freq_hz  = args[ARG_freq].u_int;
    int res_bits = args[ARG_res_bits].u_int;

    if (freq_hz <= 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid freq_hz"));
    }
    if (res_bits <= 0 || res_bits > 20) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid resolution_bits"));
    }

    ledc_timer_config_t timer_cfg = {
        .speed_mode      = LEDC_LOW_SPEED_MODE,
        .duty_resolution = (ledc_timer_bit_t)res_bits,
        .timer_num       = LEDC_TIMER_0,
        .freq_hz         = (uint32_t)freq_hz,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    io_check(ledc_timer_config(&timer_cfg), "ledc_timer_config");

    ledc_channel_config_t ch_cfg = {
        .gpio_num   = s_bl_pin,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel    = LEDC_CHANNEL_0,
        .intr_type  = LEDC_INTR_DISABLE,
        .timer_sel  = LEDC_TIMER_0,
        .duty       = 0,
        .hpoint     = 0,
    };
    io_check(ledc_channel_config(&ch_cfg), "ledc_channel_config");

    s_bl_duty_max   = (1u << res_bits) - 1;
    s_bl_pwm_inited = true;

    /* start fully on, matching the plain-GPIO backlight()'s prior default */
    io_check(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, s_bl_duty_max), "ledc_set_duty");
    io_check(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0), "ledc_update_duty");

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_backlight_init_obj, 0, moclcd_backlight_init);

/* -------------------------------------------------------------------
 * moclcd.backlight_set(level)
 * level is a 0.0-1.0 brightness fraction. Requires backlight_init().
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_backlight_set(mp_obj_t level_in)
{
    if (!s_bl_pwm_inited) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("moclcd.backlight_init() must be called first"));
    }
    mp_float_t level = mp_obj_get_float(level_in);
    if (level < 0) level = 0;
    if (level > 1) level = 1;

    uint32_t duty = (uint32_t)(level * s_bl_duty_max + 0.5f);
    io_check(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty), "ledc_set_duty");
    io_check(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0), "ledc_update_duty");

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_set_obj, moclcd_backlight_set);

/* -------------------------------------------------------------------
 * moclcd.backlight(on)
 * Plain on/off. If backlight_init() has been called, this drives the
 * PWM duty to max/0 instead of touching the pin directly (the pin is
 * now owned by the LEDC peripheral, not plain GPIO).
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_backlight(mp_obj_t on_in)
{
    bool on = mp_obj_is_true(on_in);

    if (s_bl_pwm_inited) {
        uint32_t duty = on ? s_bl_duty_max : 0;
        io_check(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty), "ledc_set_duty");
        io_check(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0), "ledc_update_duty");
    } else {
        mp_hal_pin_write(s_bl_pin, on ? 1 : 0);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_obj, moclcd_backlight);

/* -------------------------------------------------------------------
 * moclcd.cmd(cmd, params=None) -- raw passthrough, kept for flexibility
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_cmd(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int cmd = mp_obj_get_int(args_in[0]);
    if (cmd < 0 || cmd > 0xFF) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid cmd"));
    }

    const void *buf = NULL;
    size_t len = 0;
    mp_buffer_info_t bufinfo;
    if (n_args == 2 && args_in[1] != mp_const_none) {
        mp_get_buffer_raise(args_in[1], &bufinfo, MP_BUFFER_READ);
        buf = bufinfo.buf;
        len = bufinfo.len;
    }
    lcd_cmd_raw((uint8_t)cmd, buf, len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_cmd_obj, 1, 2, moclcd_cmd);

/* -------------------------------------------------------------------
 * moclcd.data(buf) -- raw passthrough, still available for one-off
 * writes. This is documented as synchronous-ish from the caller's
 * perspective in the original API, but esp_lcd_panel_io_tx_color() is
 * still async under the hood; to keep this call safe without changing
 * its signature (no length/ownership contract to preserve here since
 * callers already expect the buffer to be theirs afterwards), we copy
 * it through the DMA pool exactly like blit() does. This trades a memcpy
 * for correctness on a call that was never meant to be a hot path.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_data(mp_obj_t buf_in)
{
    require_init();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);
    stream_from_buffer((const uint8_t *)bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

/* -------------------------------------------------------------------
 * moclcd.fill_rect(x, y, w, h, color)
 *
 * Sets the address window once, then streams solid color through the
 * shared DMA pool in chunks via stream_solid(). Because pool slots are
 * only reused once their completion callback fires, up to
 * LCD_DMA_POOL_SLOTS chunks can be in flight/prepared at once instead
 * of the CPU waiting on each one -- without ever touching a buffer the
 * hardware might still be reading.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args_in)
{
    require_init();

    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    /* reject negative/zero and any overflow of x+w / y+h before it can
       wrap in signed arithmetic */
    if (w <= 0 || h <= 0 || x < 0 || y < 0 ||
        x > (int)s_width || y > (int)s_height ||
        w > (int)s_width || h > (int)s_height ||
        x > (int)s_width - w || y > (int)s_height - h) {
        mp_raise_ValueError(MP_ERROR_TEXT("fill_rect out of bounds"));
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + w - 1), (uint16_t)(y + h - 1));
    stream_solid((uint32_t)w * (uint32_t)h, color);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_rect_obj, 5, 5, moclcd_fill_rect);

/* -------------------------------------------------------------------
 * moclcd.fill_screen(color)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_fill_screen(mp_obj_t color_in)
{
    mp_obj_t args[5] = {
        mp_obj_new_int(0), mp_obj_new_int(0),
        mp_obj_new_int(s_width), mp_obj_new_int(s_height),
        color_in
    };
    return moclcd_fill_rect(5, args);
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_fill_screen_obj, moclcd_fill_screen);

/* -------------------------------------------------------------------
 * moclcd.blit(x, y, w, h, buf)
 * Pushes an arbitrary RGB565 pixel buffer (w*h*2 bytes, MSB first per
 * pixel) into the window. The MicroPython-owned buffer is never handed
 * directly to esp_lcd -- it's streamed into shared DMA-pool slots in
 * chunks (stream_from_buffer), each of which is only reused after its
 * own completion callback fires. This avoids both a full-frame extra
 * allocation AND the original bug where a Python buffer could be
 * garbage-collected or reused while DMA was still reading it.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_blit(size_t n_args, const mp_obj_t *args_in)
{
    require_init();

    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);

    if (w <= 0 || h <= 0 || x < 0 || y < 0 ||
        x > (int)s_width || y > (int)s_height ||
        w > (int)s_width || h > (int)s_height ||
        x > (int)s_width - w || y > (int)s_height - h) {
        mp_raise_ValueError(MP_ERROR_TEXT("blit out of bounds"));
    }

    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args_in[4], &bufinfo, MP_BUFFER_READ);

    size_t expected = (size_t)w * (size_t)h * 2;
    if (bufinfo.len != expected) {
        mp_raise_ValueError(MP_ERROR_TEXT("buffer size does not match w*h*2"));
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + w - 1), (uint16_t)(y + h - 1));
    stream_from_buffer((const uint8_t *)bufinfo.buf, bufinfo.len);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_blit_obj, 5, 5, moclcd_blit);

/* -------------------------------------------------------------------
 * moclcd.draw_pixel(x, y, color)
 * Silently clipped if off-panel (consistent with the primitives below,
 * unlike the strict fill_rect()/blit() calls above).
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_pixel(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[2]);
    do_draw_pixel(x, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_pixel_obj, 3, 3, moclcd_draw_pixel);

/* -------------------------------------------------------------------
 * moclcd.draw_line(x0, y0, x1, y1, color)
 * Horizontal/vertical lines take the fast path through fill_rect's
 * chunked DMA stream (a "line" one pixel thick). Diagonals walk
 * Bresenham but now coalesce each row's run of consecutive x's into a
 * span before flushing, so a typical diagonal line goes out as a
 * handful of address-window + DMA bursts instead of one per pixel.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_line(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x0 = mp_obj_get_int(args_in[0]);
    int y0 = mp_obj_get_int(args_in[1]);
    int x1 = mp_obj_get_int(args_in[2]);
    int y1 = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    if (y0 == y1) {
        int x = x0 < x1 ? x0 : x1;
        int w = (x0 < x1 ? x1 - x0 : x0 - x1) + 1;
        do_fill_rect_clip(x, y0, w, 1, color);
        return mp_const_none;
    }
    if (x0 == x1) {
        int y = y0 < y1 ? y0 : y1;
        int h = (y0 < y1 ? y1 - y0 : y0 - y1) + 1;
        do_fill_rect_clip(x0, y, 1, h, color);
        return mp_const_none;
    }

    int dx = x1 > x0 ? x1 - x0 : x0 - x1;
    int sx = x0 < x1 ? 1 : -1;
    int dy = y1 > y0 ? -(y1 - y0) : (y0 - y1);
    int sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;

    int x = x0, y = y0;
    span_t s;
    span_reset(&s);
    for (;;) {
        span_feed(&s, x, y, color);
        if (x == x1 && y == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x += sx; }
        if (e2 <= dx) { err += dx; y += sy; }
        /* if the walk just moved to a new row, or moved backwards in x
           (sx == -1), the run can no longer coalesce with the previous
           point going forward -- span_feed() already detects both cases
           (different y, or non-adjacent x) and flushes automatically. */
    }
    span_flush(&s, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_line_obj, 5, 5, moclcd_draw_line);

/* -------------------------------------------------------------------
 * moclcd.draw_rect(x, y, w, h, color)
 * Outline only (four 1px-thick edges via the DMA fill path). Use
 * fill_rect() for a solid rectangle.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_rect(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    if (w <= 0 || h <= 0) return mp_const_none;

    do_fill_rect_clip(x, y, w, 1, color);          /* top */
    do_fill_rect_clip(x, y + h - 1, w, 1, color);   /* bottom */
    do_fill_rect_clip(x, y, 1, h, color);           /* left */
    do_fill_rect_clip(x + w - 1, y, 1, h, color);   /* right */
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_rect_obj, 5, 5, moclcd_draw_rect);

/* -------------------------------------------------------------------
 * moclcd.draw_circle(x0, y0, r, color)
 * Midpoint circle algorithm, 8-way symmetry. Each of the (up to) 8
 * symmetric points per step is still its own span_feed() call, but
 * horizontally-adjacent points across steps (which happens near the
 * top/bottom/left/right of the circle where the octants are nearly
 * flat) now coalesce into one transfer via the same span mechanism
 * draw_line() uses, instead of always being one address-window per
 * pixel.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_draw_circle(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x0 = mp_obj_get_int(args_in[0]);
    int y0 = mp_obj_get_int(args_in[1]);
    int r  = mp_obj_get_int(args_in[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[3]);

    if (r < 0) return mp_const_none;

    if (r == 0) {
        do_draw_pixel(x0, y0, color);
        return mp_const_none;
    }

    int f = 1 - r;
    int ddF_x = 1;
    int ddF_y = -2 * r;
    int x = 0;
    int y = r;

    /* Each of the four "cardinal" points and each step's eight
       symmetric points are emitted in increasing-x order per row where
       possible so span_feed() can coalesce adjacent ones; points on
       different rows or non-adjacent x simply flush and start a new
       span, which is correct either way. */
    span_t s;
    span_reset(&s);

    span_feed(&s, x0, y0 + r, color);
    span_flush(&s, color);
    span_feed(&s, x0, y0 - r, color);
    span_flush(&s, color);
    span_feed(&s, x0 + r, y0, color);
    span_flush(&s, color);
    span_feed(&s, x0 - r, y0, color);
    span_flush(&s, color);

    while (x < y) {
        if (f >= 0) { y--; ddF_y += 2; f += ddF_y; }
        x++;
        ddF_x += 2;
        f += ddF_x;

        /* group the two points on each of the four affected rows so
           runs that happen to be adjacent (small radii, near 45
           degrees) still coalesce; for most steps these are two
           separate single-pixel spans, which is no worse than before */
        span_feed(&s, x0 - x, y0 + y, color);
        span_feed(&s, x0 + x, y0 + y, color);
        span_flush(&s, color);

        span_feed(&s, x0 - x, y0 - y, color);
        span_feed(&s, x0 + x, y0 - y, color);
        span_flush(&s, color);

        span_feed(&s, x0 - y, y0 + x, color);
        span_feed(&s, x0 + y, y0 + x, color);
        span_flush(&s, color);

        span_feed(&s, x0 - y, y0 - x, color);
        span_feed(&s, x0 + y, y0 - x, color);
        span_flush(&s, color);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_circle_obj, 4, 4, moclcd_draw_circle);

/* -------------------------------------------------------------------
 * moclcd.fill_circle(x0, y0, r, color)
 * Midpoint circle algorithm filled via vertical spans (same approach
 * Adafruit_GFX uses) -- each span goes through the DMA fill path
 * instead of being plotted pixel by pixel, so a filled circle is much
 * cheaper than the same shape built out of draw_pixel() calls.
 * Unchanged in approach; already using the efficient span/fill_rect path.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_fill_circle(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x0 = mp_obj_get_int(args_in[0]);
    int y0 = mp_obj_get_int(args_in[1]);
    int r  = mp_obj_get_int(args_in[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[3]);

    if (r < 0) return mp_const_none;

    do_fill_rect_clip(x0, y0 - r, 1, 2 * r + 1, color); /* central vertical span */

    int f = 1 - r;
    int ddF_x = 1;
    int ddF_y = -2 * r;
    int x = 0;
    int y = r;

    while (x < y) {
        if (f >= 0) { y--; ddF_y += 2; f += ddF_y; }
        x++;
        ddF_x += 2;
        f += ddF_x;

        do_fill_rect_clip(x0 + x, y0 - y, 1, 2 * y + 1, color);
        do_fill_rect_clip(x0 - x, y0 - y, 1, 2 * y + 1, color);
        do_fill_rect_clip(x0 + y, y0 - x, 1, 2 * x + 1, color);
        do_fill_rect_clip(x0 - y, y0 - x, 1, 2 * x + 1, color);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_circle_obj, 4, 4, moclcd_fill_circle);

/* ---- module table ---- */
static const mp_rom_map_elem_t moclcd_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),   MP_ROM_QSTR(MP_QSTR_moclcd)          },
    { MP_ROM_QSTR(MP_QSTR_init),        MP_ROM_PTR(&moclcd_init_obj)        },
    { MP_ROM_QSTR(MP_QSTR_deinit),      MP_ROM_PTR(&moclcd_deinit_obj)      },
    { MP_ROM_QSTR(MP_QSTR_reset),       MP_ROM_PTR(&moclcd_reset_obj)       },
    { MP_ROM_QSTR(MP_QSTR_panel_init),  MP_ROM_PTR(&moclcd_panel_init_obj)  },
    { MP_ROM_QSTR(MP_QSTR_backlight),   MP_ROM_PTR(&moclcd_backlight_obj)   },
    { MP_ROM_QSTR(MP_QSTR_backlight_init), MP_ROM_PTR(&moclcd_backlight_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_backlight_set),  MP_ROM_PTR(&moclcd_backlight_set_obj)  },
    { MP_ROM_QSTR(MP_QSTR_cmd),         MP_ROM_PTR(&moclcd_cmd_obj)         },
    { MP_ROM_QSTR(MP_QSTR_data),        MP_ROM_PTR(&moclcd_data_obj)        },
    { MP_ROM_QSTR(MP_QSTR_fill_rect),   MP_ROM_PTR(&moclcd_fill_rect_obj)   },
    { MP_ROM_QSTR(MP_QSTR_fill_screen), MP_ROM_PTR(&moclcd_fill_screen_obj) },
    { MP_ROM_QSTR(MP_QSTR_blit),        MP_ROM_PTR(&moclcd_blit_obj)        },
    { MP_ROM_QSTR(MP_QSTR_draw_pixel),  MP_ROM_PTR(&moclcd_draw_pixel_obj)  },
    { MP_ROM_QSTR(MP_QSTR_draw_line),   MP_ROM_PTR(&moclcd_draw_line_obj)   },
    { MP_ROM_QSTR(MP_QSTR_draw_rect),   MP_ROM_PTR(&moclcd_draw_rect_obj)   },
    { MP_ROM_QSTR(MP_QSTR_draw_circle), MP_ROM_PTR(&moclcd_draw_circle_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_circle), MP_ROM_PTR(&moclcd_fill_circle_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_text8x8),MP_ROM_PTR(&moclcd_draw_text8x8_obj)},
    { MP_ROM_QSTR(MP_QSTR_draw_bmp),    MP_ROM_PTR(&moclcd_draw_bmp_obj)    },
};
static MP_DEFINE_CONST_DICT(moclcd_globals, moclcd_globals_table);

const mp_obj_module_t mp_module_moclcd = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, mp_module_moclcd);
