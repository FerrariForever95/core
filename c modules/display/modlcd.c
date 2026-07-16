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
 * - Fills stream through a small DMA-capable buffer (heap_caps_malloc
 *   with MALLOC_CAP_DMA) that's resent in chunks. Because
 *   trans_queue_depth is 10, several chunks can be in flight on the DMA
 *   engine at once instead of the CPU/Python loop stalling on each line
 *   like the original demo script did.
 *
 * API:
 *   moclcd.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
 *                                             -- defaults to landscape;
 *                                                pass width=320, height=480,
 *                                                madctl=0x48 for portrait
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

#define LCD_CMD_CASET  0x2A
#define LCD_CMD_PASET  0x2B
#define LCD_CMD_RAMWR  0x2C
#define LCD_CMD_RAMWRC 0x3C   /* continuation write, used for pixel streaming */

/* how many pixels we buffer per DMA chunk (2 bytes/pixel -> 4KB chunks) */
#define FILL_CHUNK_PIXELS 2048

/* ---- module state ---- */
static esp_lcd_i80_bus_handle_t  s_bus       = NULL;
static esp_lcd_panel_io_handle_t s_io        = NULL;
static mp_hal_pin_obj_t          s_reset_pin = 12;
static mp_hal_pin_obj_t          s_bl_pin    = 38;
static mp_hal_pin_obj_t          s_rd_pin    = 41;
static bool                      s_has_reset = false;
static uint16_t                  s_width     = 480;
static uint16_t                  s_height    = 320;
static uint8_t                   s_madctl    = 0x28; /* landscape (MV set); 0x48=portrait, 0x88/0xE8=other rotations */
static uint8_t                  *s_fill_buf  = NULL; /* FILL_CHUNK_PIXELS*2 bytes, DMA capable */
static bool                      s_bl_pwm_inited = false;
static uint32_t                  s_bl_duty_max   = 255; /* set by backlight_init() from resolution_bits */
static uint8_t                  *s_glyph_buf = NULL;    /* 8*8*2 bytes, DMA capable, reused per glyph */

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
    if (s_io == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("moclcd.init() must be called first"));
    }
}

static void lcd_cmd_raw(uint8_t cmd, const void *buf, size_t len)
{
    io_check(esp_lcd_panel_io_tx_param(s_io, cmd, buf, len), "cmd");
}

static void ensure_fill_buf(void)
{
    if (s_fill_buf == NULL) {
        s_fill_buf = heap_caps_malloc(FILL_CHUNK_PIXELS * 2, MALLOC_CAP_DMA);
        if (s_fill_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no DMA memory for fill buffer"));
        }
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
 * chunked, DMA-pipelined path. */
static void stream_solid(uint32_t total_pixels, uint16_t color)
{
    ensure_fill_buf();

    uint32_t chunk = total_pixels < FILL_CHUNK_PIXELS ? total_pixels : FILL_CHUNK_PIXELS;
    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);
    for (uint32_t i = 0; i < chunk; i++) {
        s_fill_buf[2 * i]     = hi;
        s_fill_buf[2 * i + 1] = lo;
    }

    uint32_t remaining = total_pixels;
    while (remaining > 0) {
        uint32_t n = remaining < FILL_CHUNK_PIXELS ? remaining : FILL_CHUNK_PIXELS;
        io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, s_fill_buf, n * 2), "fill");
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
 * text rendering -- font_petme128_8x8 is column-major: 8 bytes/char,
 * byte i is column i, bit j of that byte is row j. Same table and
 * bit layout MicroPython's framebuf.text() uses internally.
 * ---------------------------------------------------------------- */
static void ensure_glyph_buf(void)
{
    if (s_glyph_buf == NULL) {
        s_glyph_buf = heap_caps_malloc(FONT_CHAR_W * FONT_CHAR_H * 2, MALLOC_CAP_DMA);
        if (s_glyph_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no DMA memory for glyph buffer"));
        }
    }
}

/* Draws one 8x8 glyph at (x,y). If bg_transparent, only foreground
 * pixels are plotted (one address-window per lit pixel -- slower, but
 * leaves whatever's already behind the glyph untouched). Otherwise the
 * whole 8x8 cell (fg+bg) is built in a small buffer and sent as a
 * single DMA transfer when it fully fits on-panel. */
static void draw_glyph(int x, int y, char c, uint16_t fg, uint16_t bg, bool bg_transparent)
{
    if (c < FONT_FIRST_CHAR || c > FONT_LAST_CHAR) c = ' ';
    const uint8_t *glyph = &font_petme128_8x8[(c - FONT_FIRST_CHAR) * 8];

    if (bg_transparent) {
        for (int col = 0; col < FONT_CHAR_W; col++) {
            uint8_t line = glyph[col];
            for (int row = 0; row < FONT_CHAR_H; row++) {
                if ((line >> row) & 1) {
                    do_draw_pixel(x + col, y + row, fg);
                }
            }
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

    ensure_glyph_buf();
    uint8_t fg_hi = (uint8_t)(fg >> 8), fg_lo = (uint8_t)(fg & 0xFF);
    uint8_t bg_hi = (uint8_t)(bg >> 8), bg_lo = (uint8_t)(bg & 0xFF);

    for (int row = 0; row < FONT_CHAR_H; row++) {
        for (int col = 0; col < FONT_CHAR_W; col++) {
            bool on = (glyph[col] >> row) & 1;
            int p = (row * FONT_CHAR_W + col) * 2;
            s_glyph_buf[p]     = on ? fg_hi : bg_hi;
            s_glyph_buf[p + 1] = on ? fg_lo : bg_lo;
        }
    }

    set_window((uint16_t)x, (uint16_t)y, (uint16_t)(x + FONT_CHAR_W - 1), (uint16_t)(y + FONT_CHAR_H - 1));
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, s_glyph_buf, FONT_CHAR_W * FONT_CHAR_H * 2), "text");
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

    for (size_t i = 0; i < len; i++) {
        draw_glyph(x + (int)i * FONT_CHAR_W, y, text[i], fg, bg, bg_transparent);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text8x8_obj, 4, 5, moclcd_draw_text8x8);

/* -------------------------------------------------------------------
 * moclcd.draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None)
 * Minimal loader: uncompressed 24-bit BMP only (no palette, no RLE).
 * w/h/max_w/max_h of 0 (the default) are treated as "unset", same as
 * the Python version's None. The whole converted image is built in
 * one DMA-capable buffer and sent as a single transfer, same approach
 * as blit().
 *
 * Note: this uses the C library's fopen()/fread(), so `path` must be
 * reachable through the ESP-IDF VFS (e.g. internal flash or SD mounted
 * via esp_vfs) -- the same filesystem MicroPython's own open() sees.
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

    bool top_down = bmp_h_raw < 0;
    int32_t bmp_h = top_down ? -bmp_h_raw : bmp_h_raw;
    int row_size = ((bmp_w * 3 + 3) / 4) * 4; /* rows padded to 4 bytes */

    int out_w = want_w > 0 ? want_w : (int)bmp_w;
    int out_h = want_h > 0 ? want_h : (int)bmp_h;
    if (max_w > 0 && out_w > max_w) out_w = max_w;
    if (max_h > 0 && out_h > max_h) out_h = max_h;

    if (out_w <= 0 || out_h <= 0) {
        fclose(f);
        return mp_const_none;
    }

    int dx = x, dy = y, dw = out_w, dh = out_h;
    if (!clip_rect(&dx, &dy, &dw, &dh)) {
        fclose(f);
        return mp_const_none;
    }

    int skip_rows = dy - y; /* how many source rows/cols the top/left clip ate */
    int skip_cols = dx - x;

    uint8_t *row_buf = heap_caps_malloc(row_size, MALLOC_CAP_DEFAULT);
    uint8_t *img = heap_caps_malloc((size_t)dw * (size_t)dh * 2, MALLOC_CAP_DMA);
    if (!row_buf || !img) {
        if (row_buf) heap_caps_free(row_buf);
        if (img) heap_caps_free(img);
        fclose(f);
        mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no memory for bmp load"));
    }

    for (int row = 0; row < dh; row++) {
        int dest_row = row + skip_rows;
        int src_row = top_down ? dest_row : ((int)bmp_h - 1 - dest_row);

        fseek(f, (long)(data_offset + (uint32_t)src_row * (uint32_t)row_size), SEEK_SET);
        if (fread(row_buf, 1, row_size, f) != (size_t)row_size) {
            break; /* short read / EOF: stop rather than send garbage rows */
        }

        int p = row * dw * 2;
        for (int col = 0; col < dw; col++) {
            int src_col = col + skip_cols;
            uint8_t b = row_buf[src_col * 3 + 0];
            uint8_t g = row_buf[src_col * 3 + 1];
            uint8_t r = row_buf[src_col * 3 + 2];
            uint16_t c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
            img[p++] = (uint8_t)(c >> 8);
            img[p++] = (uint8_t)(c & 0xFF);
        }
    }

    set_window((uint16_t)dx, (uint16_t)dy, (uint16_t)(dx + dw - 1), (uint16_t)(dy + dh - 1));
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, img, (size_t)dw * (size_t)dh * 2), "bmp");

    heap_caps_free(row_buf);
    heap_caps_free(img);
    fclose(f);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_draw_bmp_obj, 3, moclcd_draw_bmp);

/* -------------------------------------------------------------------
 * moclcd.init(pclk=10_000_000, width=320, height=480)
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

    s_width  = (uint16_t)args[ARG_width].u_int;
    s_height = (uint16_t)args[ARG_height].u_int;
    s_madctl = (uint8_t)args[ARG_madctl].u_int;

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
        /* generous ceiling so a full-frame blit() can go out in one shot;
           fill_rect() still chunks itself for pipelining regardless */
        .max_transfer_bytes = (size_t)s_width * (size_t)s_height * 2,
    };
    io_check(esp_lcd_new_i80_bus(&bus_cfg, &s_bus), "esp_lcd_new_i80_bus");

    esp_lcd_panel_io_i80_config_t io_cfg = {
        .cs_gpio_num       = -1, /* CS tied LOW in hardware */
        .pclk_hz           = (uint32_t)args[ARG_pclk].u_int,
        .trans_queue_depth = 10,
        .dc_levels = {
            .dc_idle_level  = 0,
            .dc_cmd_level   = 0,
            .dc_dummy_level = 0,
            .dc_data_level  = 1,
        },
        .lcd_cmd_bits   = 8,
        .lcd_param_bits = 8,
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

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

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

    int res_bits = args[ARG_res_bits].u_int;

    ledc_timer_config_t timer_cfg = {
        .speed_mode      = LEDC_LOW_SPEED_MODE,
        .duty_resolution = (ledc_timer_bit_t)res_bits,
        .timer_num       = LEDC_TIMER_0,
        .freq_hz         = (uint32_t)args[ARG_freq].u_int,
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
 * moclcd.data(buf) -- raw passthrough, still available for one-off writes
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_data(mp_obj_t buf_in)
{
    require_init();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, bufinfo.buf, bufinfo.len), "data write");
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

/* -------------------------------------------------------------------
 * moclcd.fill_rect(x, y, w, h, color)
 *
 * Sets the address window once, fills a small DMA-capable scratch
 * buffer with the target color, then resends that same buffer in
 * chunks via esp_lcd_panel_io_tx_color(). Because the content never
 * changes, the buffer can be safely queued again even while an earlier
 * chunk is still draining out over DMA, so up to trans_queue_depth
 * chunks stay in flight at once instead of the CPU waiting on each one.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args_in)
{
    require_init();

    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[4]);

    if (x < 0 || y < 0 || w <= 0 || h <= 0 ||
        x + w > s_width || y + h > s_height) {
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
 * pixel) into the window in one DMA-backed transfer. Useful for
 * sprites, images, or a full framebuffer flush.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_blit(size_t n_args, const mp_obj_t *args_in)
{
    require_init();

    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    int w = mp_obj_get_int(args_in[2]);
    int h = mp_obj_get_int(args_in[3]);

    if (x < 0 || y < 0 || w <= 0 || h <= 0 ||
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
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, bufinfo.buf, bufinfo.len), "blit");

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
 * Horizontal/vertical lines take a fast path through fill_rect's
 * chunked DMA stream (a "line" one pixel thick). Diagonals fall back
 * to a pixel-by-pixel Bresenham walk, since each pixel needs its own
 * address window on this bus.
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
 * Midpoint circle algorithm, 8-way symmetry, pixel-by-pixel (each
 * pixel needs its own address window on this bus, same as draw_line's
 * diagonal case).
 * ---------------------------------------------------------------- */
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

/* -------------------------------------------------------------------
 * moclcd.fill_circle(x0, y0, r, color)
 * Midpoint circle algorithm filled via vertical spans (same approach
 * Adafruit_GFX uses) -- each span goes through the DMA fill path
 * instead of being plotted pixel by pixel, so a filled circle is much
 * cheaper than the same shape built out of draw_pixel() calls.
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
};
static MP_DEFINE_CONST_DICT(moclcd_globals, moclcd_globals_table);

const mp_obj_module_t mp_module_moclcd = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, mp_module_moclcd);
