/*
 * moclcd.c — Higher-level 8080 8-bit parallel LCD module for MicroPython,
 * built on top of ESP-IDF's esp_lcd i80 driver.
 *  v2.3 beta
 * Hardware Pin Mapping:
 * - RST: GPIO 12
 * - RS (DC): GPIO 13
 * - WR: GPIO 14
 * - RD: GPIO 41
 * - BL (Backlight): GPIO 38
 * - D0-D7: GPIOs 16, 15, 11, 10, 9, 4, 18, 17
 */
V2.3 
#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "mphalport.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_heap_caps.h"
#include "driver/ledc.h"
#include "extmod/font_petme128_8x8.h"
#include "py/mperrno.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define LCD_CMD_NOP     0x00
#define LCD_CMD_SWRESET 0x01
#define LCD_CMD_SLPOUT  0x11
#define LCD_CMD_DISPON  0x29
#define LCD_CMD_CASET   0x2A
#define LCD_CMD_PASET   0x2B
#define LCD_CMD_RAMWR   0x2C
#define LCD_CMD_RAMWRC  0x3C
#define LCD_CMD_MADCTL  0x36
#define LCD_CMD_COLMOD  0x3A

/* 4096 pixels = 8192 bytes per DMA buffer (Internal SRAM for maximum speed) */
#define FILL_CHUNK_PIXELS 4096
#define FILL_CHUNK_BYTES  (FILL_CHUNK_PIXELS * 2)

/* Sized safely against hardware trans_queue_depth */
#define LCD_TRANS_QUEUE_DEPTH 10
#define LCD_DMA_POOL_SLOTS    4

typedef struct {
    uint8_t *buf;
    volatile bool in_flight;
} lcd_dma_slot_t;

static lcd_dma_slot_t s_dma_pool[LCD_DMA_POOL_SLOTS];
static bool           s_dma_pool_inited = false;

/* ---- Module & Hardware State ---- */
static esp_lcd_i80_bus_handle_t  s_bus           = NULL;
static esp_lcd_panel_io_handle_t s_io            = NULL;
static mp_hal_pin_obj_t          s_reset_pin     = 12;
static mp_hal_pin_obj_t          s_bl_pin        = 38;
static mp_hal_pin_obj_t          s_rd_pin        = 41;
static mp_hal_pin_obj_t          s_wr_pin        = 14;
static mp_hal_pin_obj_t          s_dc_pin        = 13;
static bool                      s_has_reset     = false;
static uint16_t                  s_width         = 480;
static uint16_t                  s_height        = 320;
static uint8_t                   s_madctl        = 0x28;
static bool                      s_bl_pwm_inited = false;
static uint32_t                  s_bl_duty_max   = 255;

static bool s_initialized       = false;
static bool s_panel_initialized = false;

#define FONT_CHAR_W      8
#define FONT_CHAR_H      8
#define FONT_FIRST_CHAR  32
#define FONT_LAST_CHAR   127

/* -------------------------------------------------------------------
 * Helpers
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

static bool IRAM_ATTR lcd_color_trans_done_cb(esp_lcd_panel_io_handle_t panel_io,
                                              esp_lcd_panel_io_event_data_t *edata,
                                              void *user_ctx)
{
    lcd_dma_slot_t *slot = (lcd_dma_slot_t *)user_ctx;
    if (slot != NULL) {
        slot->in_flight = false;
    }
    return false;
}

static void lcd_dma_pool_init(void)
{
    if (s_dma_pool_inited) return;
    for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
        s_dma_pool[i].buf = heap_caps_malloc(FILL_CHUNK_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (s_dma_pool[i].buf == NULL) {
            s_dma_pool[i].buf = heap_caps_malloc(FILL_CHUNK_BYTES, MALLOC_CAP_DMA);
        }
        s_dma_pool[i].in_flight = false;
        if (s_dma_pool[i].buf == NULL) {
            for (int j = 0; j < i; j++) {
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
    s_dma_pool_inited = false;
}

static void lcd_dma_wait_idle(void)
{
    if (!s_dma_pool_inited) return;
    bool busy;
    do {
        busy = false;
        for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
            if (s_dma_pool[i].in_flight) {
                busy = true;
                break;
            }
        }
        if (busy) {
            MICROPY_EVENT_POLL_HOOK
        }
    } while (busy);
}

static lcd_dma_slot_t *lcd_dma_acquire(void)
{
    lcd_dma_pool_init();
    for (;;) {
        for (int i = 0; i < LCD_DMA_POOL_SLOTS; i++) {
            if (!s_dma_pool[i].in_flight) {
                return &s_dma_pool[i];
            }
        }
        MICROPY_EVENT_POLL_HOOK
    }
}

static void lcd_dma_submit(lcd_dma_slot_t *slot, uint8_t cmd, size_t len)
{
    slot->in_flight = true;
    esp_err_t ret = esp_lcd_panel_io_tx_color(s_io, cmd, slot->buf, len);
    if (ret != ESP_OK) {
        slot->in_flight = false;
        io_check(ret, "dma submit");
    }
}

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

static void stream_solid(uint32_t total_pixels, uint16_t color)
{
    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);

    uint32_t remaining = total_pixels;
    while (remaining > 0) {
        uint32_t n = remaining < FILL_CHUNK_PIXELS ? remaining : FILL_CHUNK_PIXELS;
        lcd_dma_slot_t *slot = lcd_dma_acquire();
        for (uint32_t i = 0; i < n; i++) {
            slot->buf[2 * i]     = hi;
            slot->buf[2 * i + 1] = lo;
        }
        lcd_dma_submit(slot, LCD_CMD_RAMWRC, (size_t)n * 2);
        remaining -= n;
    }
}

static void stream_from_buffer(const uint8_t *src, size_t total_bytes)
{
    size_t remaining = total_bytes;
    const uint8_t *p = src;
    while (remaining > 0) {
        size_t n = remaining < FILL_CHUNK_BYTES ? remaining : FILL_CHUNK_BYTES;
        lcd_dma_slot_t *slot = lcd_dma_acquire();
        memcpy(slot->buf, p, n);
        lcd_dma_submit(slot, LCD_CMD_RAMWRC, n);
        p += n;
        remaining -= n;
    }
}

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

typedef struct {
    int x0, y, x1;
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

static void draw_glyph(int x, int y, char c, uint16_t fg, uint16_t bg, bool bg_transparent)
{
    if (c < FONT_FIRST_CHAR || c > FONT_LAST_CHAR) c = ' ';
    const uint8_t *glyph = &font_petme128_8x8[(c - FONT_FIRST_CHAR) * 8];

    if (bg_transparent) {
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
        for (int col = 0; col < FONT_CHAR_W; col++) {
            uint8_t line = glyph[col];
            for (int row = 0; row < FONT_CHAR_H; row++) {
                do_draw_pixel(x + col, y + row, ((line >> row) & 1) ? fg : bg);
            }
        }
        return;
    }

    uint8_t fg_hi = (uint8_t)(fg >> 8), fg_lo = (uint8_t)(fg & 0xFF);
    uint8_t bg_hi = (uint8_t)(bg >> 8), bg_lo = (uint8_t)(bg & 0xFF);

    lcd_dma_slot_t *slot = lcd_dma_acquire();
    for (int row = 0; row < FONT_CHAR_H; row++) {
        for (int col = 0; col < FONT_CHAR_W; col++) {
            bool on = (glyph[col] >> row) & 1;
            int p = (row * FONT_CHAR_W + col) * 2;
            slot->buf[p]     = on ? fg_hi : bg_hi;
            slot->buf[p + 1] = on ? fg_lo : bg_lo;
        }
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + FONT_CHAR_W - 1), (uint16_t)(y + FONT_CHAR_H - 1));
    lcd_dma_submit(slot, LCD_CMD_RAMWRC, FONT_CHAR_W * FONT_CHAR_H * 2);
}

/* -------------------------------------------------------------------
 * API Definitions
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_pclk, ARG_width, ARG_height, ARG_madctl };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_pclk,   MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 10000000} },
        { MP_QSTR_width,  MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 480} },
        { MP_QSTR_height, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 320} },
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
        mp_raise_ValueError(MP_ERROR_TEXT("invalid pclk"));
    }

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

    /* Clamp control pins HIGH to prevent spurious writes while CS is grounded */
    mp_hal_pin_output(s_rd_pin);
    mp_hal_pin_write(s_rd_pin, 1);

    mp_hal_pin_output(s_wr_pin);
    mp_hal_pin_write(s_wr_pin, 1);

    mp_hal_pin_output(s_dc_pin);
    mp_hal_pin_write(s_dc_pin, 1);

    mp_hal_pin_output(s_bl_pin);
    mp_hal_pin_write(s_bl_pin, 0);

    mp_hal_pin_output(s_reset_pin);
    mp_hal_pin_write(s_reset_pin, 1);
    s_has_reset = true;

    /* Pre-bus reset sequence */
    mp_hal_delay_ms(10);
    mp_hal_pin_write(s_reset_pin, 0);
    mp_hal_delay_ms(25);
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_ms(150);

    int data_gpios[8] = { 16, 15, 11, 10, 9, 4, 18, 17 };

    esp_lcd_i80_bus_config_t bus_cfg = {
        .dc_gpio_num = s_dc_pin,
        .wr_gpio_num = s_wr_pin,
        .clk_src     = LCD_CLK_SRC_PLL160M,
        .data_gpio_nums = {
            data_gpios[0], data_gpios[1], data_gpios[2], data_gpios[3],
            data_gpios[4], data_gpios[5], data_gpios[6], data_gpios[7],
        },
        .bus_width          = 8,
        .max_transfer_bytes = (size_t)s_width * (size_t)s_height * 2,
    };
    io_check(esp_lcd_new_i80_bus(&bus_cfg, &s_bus), "esp_lcd_new_i80_bus");

    esp_lcd_panel_io_i80_config_t io_cfg = {
        .cs_gpio_num       = -1,
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
    };
    io_check(esp_lcd_new_panel_io_i80(s_bus, &io_cfg, &s_io), "esp_lcd_new_panel_io_i80");

    lcd_dma_pool_init();
    s_initialized = true;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

static mp_obj_t moclcd_deinit(void)
{
    if (!s_initialized) return mp_const_none;
    lcd_dma_wait_idle();
    lcd_dma_pool_deinit();
    if (s_io)  { esp_lcd_panel_io_del(s_io);  s_io  = NULL; }
    if (s_bus) { esp_lcd_del_i80_bus(s_bus);  s_bus = NULL; }
    s_initialized       = false;
    s_panel_initialized = false;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_deinit_obj, moclcd_deinit);

static mp_obj_t moclcd_reset(void)
{
    if (!s_has_reset) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("no reset pin configured"));
    }
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_ms(10);
    mp_hal_pin_write(s_reset_pin, 0);
    mp_hal_delay_ms(25);
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_ms(150);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_reset_obj, moclcd_reset);

static mp_obj_t moclcd_panel_init(void)
{
    require_init();

    /* NOPs flush any desynced multi-byte parser register state */
    lcd_cmd_raw(LCD_CMD_NOP, NULL, 0);
    lcd_cmd_raw(LCD_CMD_NOP, NULL, 0);
    mp_hal_delay_ms(10);

    lcd_cmd_raw(LCD_CMD_SWRESET, NULL, 0);
    mp_hal_delay_ms(150);

    lcd_cmd_raw(LCD_CMD_SLPOUT, NULL, 0);
    mp_hal_delay_ms(150); /* Mandatory charge pump stabilization */

    uint8_t colmod = 0x55; /* RGB565 */
    lcd_cmd_raw(LCD_CMD_COLMOD, &colmod, 1);
    mp_hal_delay_ms(10);

    uint8_t madctl = s_madctl;
    lcd_cmd_raw(LCD_CMD_MADCTL, &madctl, 1);
    mp_hal_delay_ms(10);

    uint16_t x1 = s_width - 1;
    uint16_t y1 = s_height - 1;
    uint8_t caset[4] = { 0x00, 0x00, (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    lcd_cmd_raw(LCD_CMD_CASET, caset, sizeof(caset));

    uint8_t paset[4] = { 0x00, 0x00, (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    lcd_cmd_raw(LCD_CMD_PASET, paset, sizeof(paset));

    lcd_cmd_raw(LCD_CMD_DISPON, NULL, 0);
    mp_hal_delay_ms(120);

    mp_hal_pin_write(s_bl_pin, 1);
    s_panel_initialized = true;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

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

    if (freq_hz <= 0) mp_raise_ValueError(MP_ERROR_TEXT("invalid freq_hz"));
    if (res_bits <= 0 || res_bits > 20) mp_raise_ValueError(MP_ERROR_TEXT("invalid resolution_bits"));

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
        .duty       = (1u << res_bits) - 1,
        .hpoint     = 0,
    };
    io_check(ledc_channel_config(&ch_cfg), "ledc_channel_config");

    s_bl_duty_max   = (1u << res_bits) - 1;
    s_bl_pwm_inited = true;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_backlight_init_obj, 0, moclcd_backlight_init);

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

static mp_obj_t moclcd_cmd(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int cmd = mp_obj_get_int(args_in[0]);
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

static mp_obj_t moclcd_data(mp_obj_t buf_in)
{
    require_init();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);
    stream_from_buffer((const uint8_t *)bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    if (w <= 0 || h <= 0 || x < 0 || y < 0 ||
        x + w > s_width || y + h > s_height) {
        mp_raise_ValueError(MP_ERROR_TEXT("fill_rect out of bounds"));
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + w - 1), (uint16_t)(y + h - 1));
    stream_solid((uint32_t)w * (uint32_t)h, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_rect_obj, 5, 5, moclcd_fill_rect);

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

static mp_obj_t moclcd_blit(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);

    if (w <= 0 || h <= 0 || x < 0 || y < 0 ||
        x + w > s_width || y + h > s_height) {
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
    for (;;) {
        do_draw_pixel(x, y, color);
        if (x == x1 && y == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x += sx; }
        if (e2 <= dx) { err += dx; y += sy; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_line_obj, 5, 5, moclcd_draw_line);

static mp_obj_t moclcd_draw_rect(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    if (w <= 0 || h <= 0) return mp_const_none;
    do_fill_rect_clip(x, y, w, 1, color);
    do_fill_rect_clip(x, y + h - 1, w, 1, color);
    do_fill_rect_clip(x, y, 1, h, color);
    do_fill_rect_clip(x + w - 1, y, 1, h, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_rect_obj, 5, 5, moclcd_draw_rect);

static mp_obj_t moclcd_draw_circle(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x0 = mp_obj_get_int(args_in[0]);
    int y0 = mp_obj_get_int(args_in[1]);
    int r  = mp_obj_get_int(args_in[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[3]);

    if (r < 0) return mp_const_none;

    int f = 1 - r;
    int ddF_x = 1;
    int ddF_y = -2 * r;
    int x = 0;
    int y = r;

    do_draw_pixel(x0, y0 + r, color);
    do_draw_pixel(x0, y0 - r, color);
    do_draw_pixel(x0 + r, y0, color);
    do_draw_pixel(x0 - r, y0, color);

    while (x < y) {
        if (f >= 0) { y--; ddF_y += 2; f += ddF_y; }
        x++;
        ddF_x += 2;
        f += ddF_x;

        do_draw_pixel(x0 + x, y0 + y, color);
        do_draw_pixel(x0 - x, y0 + y, color);
        do_draw_pixel(x0 + x, y0 - y, color);
        do_draw_pixel(x0 - x, y0 - y, color);
        do_draw_pixel(x0 + y, y0 + x, color);
        do_draw_pixel(x0 - y, y0 + x, color);
        do_draw_pixel(x0 + y, y0 - x, color);
        do_draw_pixel(x0 - y, y0 - x, color);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_circle_obj, 4, 4, moclcd_draw_circle);

static mp_obj_t moclcd_fill_circle(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x0 = mp_obj_get_int(args_in[0]);
    int y0 = mp_obj_get_int(args_in[1]);
    int r  = mp_obj_get_int(args_in[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[3]);

    if (r < 0) return mp_const_none;

    do_fill_rect_clip(x0, y0 - r, 1, 2 * r + 1, color);

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

    for (size_t i = 0; i < len; i++) {
        draw_glyph(x + (int)i * FONT_CHAR_W, y, text[i], fg, bg, bg_transparent);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text8x8_obj, 4, 5, moclcd_draw_text8x8);

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

    FILE *f = fopen(path, "rb");
    if (!f) mp_raise_OSError(MP_ENOENT);

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

    if (bpp != 24 || compression != 0 || bmp_w <= 0 || bmp_h_raw == 0) {
        fclose(f);
        mp_raise_ValueError(MP_ERROR_TEXT("unsupported BMP format"));
    }

    bool top_down = bmp_h_raw < 0;
    int32_t bmp_h = top_down ? -bmp_h_raw : bmp_h_raw;
    int row_size = ((bmp_w * 3 + 3) / 4) * 4;

    int out_w = want_w > 0 ? want_w : (int)bmp_w;
    int out_h = want_h > 0 ? want_h : (int)bmp_h;
    if (max_w > 0 && out_w > max_w) out_w = max_w;
    if (max_h > 0 && out_h > max_h) out_h = max_h;

    int dx = x, dy = y, dw = out_w, dh = out_h;
    if (!clip_rect(&dx, &dy, &dw, &dh)) {
        fclose(f);
        return mp_const_none;
    }

    int skip_out_rows = dy - y;
    int skip_out_cols = dx - x;
    bool scaling = (out_w != (int)bmp_w) || (out_h != (int)bmp_h);

    uint8_t *row_buf = heap_caps_malloc(row_size, MALLOC_CAP_DEFAULT);
    uint8_t *out_row = heap_caps_malloc((size_t)dw * 2, MALLOC_CAP_DEFAULT);
    if (!row_buf || !out_row) {
        if (row_buf) heap_caps_free(row_buf);
        if (out_row) heap_caps_free(out_row);
        fclose(f);
        mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no memory for BMP decode"));
    }

    set_window((uint16_t)dx, (uint16_t)dy, (uint16_t)(dx + dw - 1), (uint16_t)(dy + dh - 1));

    int last_src_row = -1;
    for (int out_row_idx = 0; out_row_idx < dh; out_row_idx++) {
        int dest_row = out_row_idx + skip_out_rows;
        int src_row = scaling ? (int)(((int64_t)dest_row * bmp_h) / out_h) : dest_row;
        if (src_row >= (int)bmp_h) src_row = (int)bmp_h - 1;

        int file_row = top_down ? src_row : ((int)bmp_h - 1 - src_row);
        if (file_row != last_src_row) {
            fseek(f, (long)(data_offset + (uint32_t)file_row * (uint32_t)row_size), SEEK_SET);
            if (fread(row_buf, 1, row_size, f) != (size_t)row_size) break;
            last_src_row = file_row;
        }

        int p = 0;
        for (int out_col = 0; out_col < dw; out_col++) {
            int dest_col = out_col + skip_out_cols;
            int src_col = scaling ? (int)(((int64_t)dest_col * bmp_w) / out_w) : dest_col;
            if (src_col >= (int)bmp_w) src_col = (int)bmp_w - 1;

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

/* ---- Module Globals Table ---- */
static const mp_rom_map_elem_t moclcd_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),        MP_ROM_QSTR(MP_QSTR_moclcd)            },
    { MP_ROM_QSTR(MP_QSTR_init),            MP_ROM_PTR(&moclcd_init_obj)          },
    { MP_ROM_QSTR(MP_QSTR_deinit),          MP_ROM_PTR(&moclcd_deinit_obj)        },
    { MP_ROM_QSTR(MP_QSTR_reset),           MP_ROM_PTR(&moclcd_reset_obj)         },
    { MP_ROM_QSTR(MP_QSTR_panel_init),      MP_ROM_PTR(&moclcd_panel_init_obj)    },
    { MP_ROM_QSTR(MP_QSTR_backlight),       MP_ROM_PTR(&moclcd_backlight_obj)     },
    { MP_ROM_QSTR(MP_QSTR_backlight_init),  MP_ROM_PTR(&moclcd_backlight_init_obj)},
    { MP_ROM_QSTR(MP_QSTR_backlight_set),   MP_ROM_PTR(&moclcd_backlight_set_obj) },
    { MP_ROM_QSTR(MP_QSTR_cmd),             MP_ROM_PTR(&moclcd_cmd_obj)           },
    { MP_ROM_QSTR(MP_QSTR_data),            MP_ROM_PTR(&moclcd_data_obj)          },
    { MP_ROM_QSTR(MP_QSTR_fill_rect),       MP_ROM_PTR(&moclcd_fill_rect_obj)     },
    { MP_ROM_QSTR(MP_QSTR_fill_screen),     MP_ROM_PTR(&moclcd_fill_screen_obj)   },
    { MP_ROM_QSTR(MP_QSTR_blit),            MP_ROM_PTR(&moclcd_blit_obj)          },
    { MP_ROM_QSTR(MP_QSTR_draw_pixel),      MP_ROM_PTR(&moclcd_draw_pixel_obj)    },
    { MP_ROM_QSTR(MP_QSTR_draw_line),       MP_ROM_PTR(&moclcd_draw_line_obj)     },
    { MP_ROM_QSTR(MP_QSTR_draw_rect),       MP_ROM_PTR(&moclcd_draw_rect_obj)     },
    { MP_ROM_QSTR(MP_QSTR_draw_circle),     MP_ROM_PTR(&moclcd_draw_circle_obj)   },
    { MP_ROM_QSTR(MP_QSTR_fill_circle),     MP_ROM_PTR(&moclcd_fill_circle_obj)   },
    { MP_ROM_QSTR(MP_QSTR_draw_text8x8),    MP_ROM_PTR(&moclcd_draw_text8x8_obj)  },
    { MP_ROM_QSTR(MP_QSTR_draw_bmp),        MP_ROM_PTR(&moclcd_draw_bmp_obj)      },
};
static MP_DEFINE_CONST_DICT(moclcd_globals, moclcd_globals_table);

const mp_obj_module_t mp_module_moclcd = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, mp_module_moclcd);
