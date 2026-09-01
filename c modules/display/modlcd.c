/*
 * moclcd.c — Robust 8080 8-bit parallel LCD driver for MicroPython.
 * Engineered for ESP32-S3 with glitch-free bus initialization & strict timing.
 *
 * Pin Map:
 * - RST: GPIO 12
 * - RS (DC): GPIO 13
 * - WR: GPIO 14
 * - RD: GPIO 41
 * - BL (Backlight): GPIO 38
 * - D0-D7: GPIOs 16, 15, 11, 10, 9, 4, 18, 17
 */

#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "mphalport.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_heap_caps.h"

#include <string.h>

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

/* 4096 pixels = 8192 bytes DMA scratch buffer allocated in internal SRAM */
#define FILL_CHUNK_PIXELS 4096

/* ---- Driver State ---- */
static esp_lcd_i80_bus_handle_t  s_bus       = NULL;
static esp_lcd_panel_io_handle_t s_io        = NULL;
static mp_hal_pin_obj_t          s_reset_pin = 12;
static mp_hal_pin_obj_t          s_bl_pin    = 38;
static mp_hal_pin_obj_t          s_rd_pin    = 41;
static mp_hal_pin_obj_t          s_wr_pin    = 14;
static mp_hal_pin_obj_t          s_dc_pin    = 13;
static bool                      s_has_reset = false;
static uint16_t                  s_width     = 480;
static uint16_t                  s_height    = 320;
static uint8_t                   s_madctl    = 0x28; /* Landscape default */
static uint8_t                  *s_fill_buf  = NULL;

/* -------------------------------------------------------------------
 * Internal Helpers
 * ---------------------------------------------------------------- */
static inline void io_check(esp_err_t ret, const char *what)
{
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("%s failed: %d"), what, ret);
    }
}

static inline void require_init(void)
{
    if (s_io == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("moclcd.init() must be called first"));
    }
}

static inline void lcd_cmd_raw(uint8_t cmd, const void *buf, size_t len)
{
    io_check(esp_lcd_panel_io_tx_param(s_io, cmd, buf, len), "cmd");
}

static void ensure_fill_buf(void)
{
    if (s_fill_buf == NULL) {
        /* Allocate fill buffer in internal DMA SRAM for peak transfer speed */
        s_fill_buf = heap_caps_malloc(FILL_CHUNK_PIXELS * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (s_fill_buf == NULL) {
            s_fill_buf = heap_caps_malloc(FILL_CHUNK_PIXELS * 2, MALLOC_CAP_DMA);
        }
        if (s_fill_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("no DMA memory for fill buffer"));
        }
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
 * MicroPython Module API
 * ---------------------------------------------------------------- */

/* moclcd.init(pclk=10_000_000, width=480, height=320, madctl=0x28) */
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

    /* Safe cleanup if re-initializing */
    if (s_io != NULL) {
        esp_lcd_panel_io_del(s_io);
        s_io = NULL;
    }
    if (s_bus != NULL) {
        esp_lcd_del_i80_bus(s_bus);
        s_bus = NULL;
    }
    if (s_fill_buf != NULL) {
        free(s_fill_buf);
        s_fill_buf = NULL;
    }

    s_width  = (uint16_t)args[ARG_width].u_int;
    s_height = (uint16_t)args[ARG_height].u_int;
    s_madctl = (uint8_t)args[ARG_madctl].u_int;

    /* Clamp control pins HIGH to prevent spurious writes while CS is grounded */
    mp_hal_pin_output(s_rd_pin);
    mp_hal_pin_write(s_rd_pin, 1);

    mp_hal_pin_output(s_wr_pin);
    mp_hal_pin_write(s_wr_pin, 1);

    mp_hal_pin_output(s_dc_pin);
    mp_hal_pin_write(s_dc_pin, 1);

    mp_hal_pin_output(s_bl_pin);
    mp_hal_pin_write(s_bl_pin, 0); /* Keep backlight off until display init settles */

    mp_hal_pin_output(s_reset_pin);
    mp_hal_pin_write(s_reset_pin, 1);
    s_has_reset = true;

    /* Hardware reset sequence executed while bus control lines are clamped HIGH */
    mp_hal_delay_ms(10);
    mp_hal_pin_write(s_reset_pin, 0);
    mp_hal_delay_ms(25);
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_ms(150); /* Mandatory recovery for controller internal boot */

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
        .cs_gpio_num       = -1, /* CS tied directly to GND */
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

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

/* moclcd.reset() */
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

/* moclcd.panel_init() */
static mp_obj_t moclcd_panel_init(void)
{
    require_init();

    /* 1. Flush any desynced multi-byte parser state with NOPs */
    lcd_cmd_raw(LCD_CMD_NOP, NULL, 0);
    lcd_cmd_raw(LCD_CMD_NOP, NULL, 0);
    mp_hal_delay_ms(10);

    /* 2. Software Reset */
    lcd_cmd_raw(LCD_CMD_SWRESET, NULL, 0);
    mp_hal_delay_ms(150); /* Mandatory: wait for internal registers to reset */

    /* 3. Sleep Out */
    lcd_cmd_raw(LCD_CMD_SLPOUT, NULL, 0);
    mp_hal_delay_ms(150); /* Mandatory: wait for DC-DC charge pump voltage to stabilize */

    /* 4. Interface Pixel Format (16-bit RGB565) */
    uint8_t colmod = 0x55;
    lcd_cmd_raw(LCD_CMD_COLMOD, &colmod, 1);
    mp_hal_delay_ms(10);

    /* 5. Memory Access Control (Orientation & Color Order) */
    uint8_t madctl = s_madctl;
    lcd_cmd_raw(LCD_CMD_MADCTL, &madctl, 1);
    mp_hal_delay_ms(10);

    /* 6. Default Full Column & Page Window */
    uint16_t x1 = s_width - 1;
    uint16_t y1 = s_height - 1;
    uint8_t caset[4] = { 0x00, 0x00, (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    lcd_cmd_raw(LCD_CMD_CASET, caset, sizeof(caset));

    uint8_t paset[4] = { 0x00, 0x00, (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    lcd_cmd_raw(LCD_CMD_PASET, paset, sizeof(paset));

    /* 7. Display ON */
    lcd_cmd_raw(LCD_CMD_DISPON, NULL, 0);
    mp_hal_delay_ms(120); /* Allow gate drivers to activate */

    /* 8. Enable Backlight once display pipeline is solid */
    mp_hal_pin_write(s_bl_pin, 1);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

/* moclcd.backlight(on) */
static mp_obj_t moclcd_backlight(mp_obj_t on_in)
{
    mp_hal_pin_write(s_bl_pin, mp_obj_is_true(on_in) ? 1 : 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_obj, moclcd_backlight);

/* moclcd.cmd(cmd, params=None) */
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

/* moclcd.data(buf) */
static mp_obj_t moclcd_data(mp_obj_t buf_in)
{
    require_init();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, bufinfo.buf, bufinfo.len), "data write");
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

/* moclcd.fill_rect(x, y, w, h, color) */
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

/* moclcd.fill_screen(color) */
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

/* moclcd.blit(x, y, w, h, buf) */
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

/* moclcd.draw_pixel(x, y, color) */
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

/* moclcd.draw_line(x0, y0, x1, y1, color) */
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

/* moclcd.draw_rect(x, y, w, h, color) */
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

/* moclcd.draw_circle(x0, y0, r, color) */
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

/* moclcd.fill_circle(x0, y0, r, color) */
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

/* ---- Module Globals & Registration ---- */
static const mp_rom_map_elem_t moclcd_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),    MP_ROM_QSTR(MP_QSTR_moclcd)          },
    { MP_ROM_QSTR(MP_QSTR_init),        MP_ROM_PTR(&moclcd_init_obj)        },
    { MP_ROM_QSTR(MP_QSTR_reset),       MP_ROM_PTR(&moclcd_reset_obj)       },
    { MP_ROM_QSTR(MP_QSTR_panel_init),  MP_ROM_PTR(&moclcd_panel_init_obj)  },
    { MP_ROM_QSTR(MP_QSTR_backlight),   MP_ROM_PTR(&moclcd_backlight_obj)   },
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
