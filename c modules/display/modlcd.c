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

#include <string.h>

#define LCD_CMD_CASET  0x2A
#define LCD_CMD_PASET  0x2B
#define LCD_CMD_RAMWR  0x2C
#define LCD_CMD_RAMWRC 0x3C   /* continuation write, used for pixel streaming */

/* how many pixels we buffer per DMA chunk (2 bytes/pixel -> 4KB chunks) */
#define FILL_CHUNK_PIXELS  65536

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
