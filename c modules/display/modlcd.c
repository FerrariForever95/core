/*
 * modlcd_nopool.c — 8080 8-bit parallel LCD module for MicroPython,
 * built on top of ESP-IDF's esp_lcd i80 driver.
 *
 * This is the NO-DMA-POOL baseline: a single reusable DMA-capable
 * scratch buffer (heap_caps_malloc + MALLOC_CAP_DMA), refilled and
 * resent per chunk via esp_lcd_panel_io_tx_color(). No pool of
 * buffers, no in-flight FIFO, no completion callback bookkeeping --
 * each tx_color() call is issued and the next chunk isn't prepared
 * until the previous one's contents are safe to overwrite.
 *
 * Note on correctness vs the DMA-pool version: esp_lcd_panel_io_tx_color()
 * is asynchronous -- it queues the transfer and returns once accepted
 * into the queue, it does not block until the hardware has actually
 * finished reading the buffer. With trans_queue_depth > 1, calling it
 * again immediately with the SAME buffer while an earlier queued
 * transfer using that buffer hasn't drained yet is a race: the DMA
 * engine could still be reading old bytes while this code overwrites
 * them with new ones (that's exactly what the pool + in-flight FIFO in
 * the other version exists to prevent). Since fill_rect() below always
 * fills the buffer with the same solid color before resending it, that
 * race is benign here specifically because every possible byte value
 * in the buffer is the same value -- there's no way to observe
 * "torn"/mixed content between old and new chunk. If you extend this
 * file to send varying content per chunk (e.g. a real blit of
 * non-uniform pixel data), that assumption stops holding and you'd see
 * visible tearing/corruption from reusing the buffer too soon; the
 * pool exists specifically to make that safe for varying-content
 * transfers, not solid fills.
 *
 * Same pin mapping as your working setup:
 * - RST: GPIO 12
 * - RS (DC): GPIO 13
 * - WR: GPIO 14
 * - RD: GPIO 41
 * - BL: GPIO 38
 * - D0-D7: GPIOs 16, 15, 11, 10, 9, 4, 18, 17
 *
 * API (subset -- just enough to bring the panel up and confirm it's
 * alive; extend the same way the fuller version does if this works):
 *   moclcdnp.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
 *   moclcdnp.reset()
 *   moclcdnp.panel_init()
 *   moclcdnp.backlight(on)
 *   moclcdnp.cmd(cmd, params=None)
 *   moclcdnp.data(buf)
 *   moclcdnp.fill_rect(x, y, w, h, color)
 *   moclcdnp.fill_screen(color)
 *   moclcdnp.draw_pixel(x, y, color)
 */

#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "mphalport.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_heap_caps.h"

#include <string.h>

#define LCD_CMD_CASET  0x2A
#define LCD_CMD_PASET  0x2B
#define LCD_CMD_RAMWR  0x2C
#define LCD_CMD_RAMWRC 0x3C

/* how many pixels we buffer per chunk (2 bytes/pixel -> 4KB chunks) */
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
static uint8_t                   s_madctl    = 0x28;
static uint8_t                  *s_fill_buf  = NULL; /* single reusable DMA-capable scratch buffer */

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
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("moclcdnp.init() must be called first"));
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
 * for a pixel stream. */
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

/* Stream `total_pixels` copies of `color`. Fills the single scratch
 * buffer once per chunk-size worth of pixels and resends it -- see the
 * file header note on why reusing one buffer is safe for a SOLID fill
 * specifically. No pool, no queue depth tuning here: whatever
 * trans_queue_depth was set at init() time, esp_lcd handles the actual
 * queuing/backpressure internally on tx_color(). */
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

static void do_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= s_width || y >= s_height) return;
    set_window((uint16_t)x, (uint16_t)y, (uint16_t)x, (uint16_t)y);
    stream_solid(1, color);
}

/* -------------------------------------------------------------------
 * moclcdnp.init(pclk=10_000_000, width=480, height=320, madctl=0x28)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
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
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcdnp_init_obj, 0, moclcdnp_init);

/* -------------------------------------------------------------------
 * moclcdnp.reset()
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_reset(void)
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
static MP_DEFINE_CONST_FUN_OBJ_0(moclcdnp_reset_obj, moclcdnp_reset);

/* -------------------------------------------------------------------
 * moclcdnp.panel_init()
 * Identical command sequence/timings to your working version.
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_panel_init(void)
{
    require_init();

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
static MP_DEFINE_CONST_FUN_OBJ_0(moclcdnp_panel_init_obj, moclcdnp_panel_init);

/* -------------------------------------------------------------------
 * moclcdnp.backlight(on)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_backlight(mp_obj_t on_in)
{
    mp_hal_pin_write(s_bl_pin, mp_obj_is_true(on_in) ? 1 : 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcdnp_backlight_obj, moclcdnp_backlight);

/* -------------------------------------------------------------------
 * moclcdnp.cmd(cmd, params=None)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_cmd(size_t n_args, const mp_obj_t *args_in)
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
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcdnp_cmd_obj, 1, 2, moclcdnp_cmd);

/* -------------------------------------------------------------------
 * moclcdnp.data(buf)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_data(mp_obj_t buf_in)
{
    require_init();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);
    io_check(esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, bufinfo.buf, bufinfo.len), "data write");
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcdnp_data_obj, moclcdnp_data);

/* -------------------------------------------------------------------
 * moclcdnp.fill_rect(x, y, w, h, color)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_fill_rect(size_t n_args, const mp_obj_t *args_in)
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
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcdnp_fill_rect_obj, 5, 5, moclcdnp_fill_rect);

/* -------------------------------------------------------------------
 * moclcdnp.fill_screen(color)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_fill_screen(mp_obj_t color_in)
{
    mp_obj_t args[5] = {
        mp_obj_new_int(0), mp_obj_new_int(0),
        mp_obj_new_int(s_width), mp_obj_new_int(s_height),
        color_in
    };
    return moclcdnp_fill_rect(5, args);
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcdnp_fill_screen_obj, moclcdnp_fill_screen);

/* -------------------------------------------------------------------
 * moclcdnp.draw_pixel(x, y, color)
 * ---------------------------------------------------------------- */
static mp_obj_t moclcdnp_draw_pixel(size_t n_args, const mp_obj_t *args_in)
{
    require_init();
    int x = mp_obj_get_int(args_in[0]);
    int y = mp_obj_get_int(args_in[1]);
    uint16_t color = (uint16_t)mp_obj_get_int(args_in[2]);
    do_draw_pixel(x, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcdnp_draw_pixel_obj, 3, 3, moclcdnp_draw_pixel);

/* ---- module table ---- */
static const mp_rom_map_elem_t moclcdnp_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),    MP_ROM_QSTR(MP_QSTR_moclcdnp)          },
    { MP_ROM_QSTR(MP_QSTR_init),        MP_ROM_PTR(&moclcdnp_init_obj)        },
    { MP_ROM_QSTR(MP_QSTR_reset),       MP_ROM_PTR(&moclcdnp_reset_obj)       },
    { MP_ROM_QSTR(MP_QSTR_panel_init),  MP_ROM_PTR(&moclcdnp_panel_init_obj)  },
    { MP_ROM_QSTR(MP_QSTR_backlight),   MP_ROM_PTR(&moclcdnp_backlight_obj)   },
    { MP_ROM_QSTR(MP_QSTR_cmd),         MP_ROM_PTR(&moclcdnp_cmd_obj)         },
    { MP_ROM_QSTR(MP_QSTR_data),        MP_ROM_PTR(&moclcdnp_data_obj)        },
    { MP_ROM_QSTR(MP_QSTR_fill_rect),   MP_ROM_PTR(&moclcdnp_fill_rect_obj)   },
    { MP_ROM_QSTR(MP_QSTR_fill_screen), MP_ROM_PTR(&moclcdnp_fill_screen_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_pixel),  MP_ROM_PTR(&moclcdnp_draw_pixel_obj)  },
};
static MP_DEFINE_CONST_DICT(moclcdnp_globals, moclcdnp_globals_table);

const mp_obj_module_t mp_module_moclcdnp = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcdnp_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcdnp, mp_module_moclcdnp);
