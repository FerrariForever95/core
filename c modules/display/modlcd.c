/*
 * =====================================================================================
 *  FILE:         modlcd.c
 *  MODULE:       moclcd (MicroPython native C module)
 *  TARGET:       ESP32-S3, ESP-IDF esp_lcd i80 (Intel 8080) parallel bus, 8-bit data
 *  PANEL:        ILI9488, 320 x 480
 *
 *  VERSION:      1.0.1-dev
 *  BUILD STATUS: DEV/UNVERIFIED
 *  STAGE:        1 of 2 (Display Output Only)
 *
 *  -------------------------------------------------------------------------------
 *  PIN MAP (Hardware Fixed)
 *  -------------------------------------------------------------------------------
 *      D0            -> GPIO 16
 *      D1            -> GPIO 15
 *      D2            -> GPIO 11
 *      D3            -> GPIO 10
 *      D4            -> GPIO 9
 *      D5            -> GPIO 4
 *      D6  (YM)      -> GPIO 18
 *      D7  (XP)      -> GPIO 17
 *      RS / DC (XM)  -> GPIO 13
 *      WR            -> GPIO 14
 *      RD            -> GPIO 41
 *      RST           -> GPIO 12
 *      BL            -> GPIO 38
 *      CS  (YP)      -> Hardwired to GND on PCB (cs_gpio_num = -1)
 *
 *  -------------------------------------------------------------------------------
 *  CHANGELOG (v1.0.1-dev)
 *  -------------------------------------------------------------------------------
 *  - Maintained 16-bit RGB565 (2 bytes/pixel, MSB first) with COLMOD 0x55.
 *  - Fixed esp_lcd_panel_io_tx_color call: replaced invalid '-1' command with
 *    0x2C (RAMWR) on first transmission and 0x3C (RAMWRC) on continued streams.
 *  - Removed premature RAMWR (0x2C) from set_window() to avoid command collision.
 *  - Added dual NOP (0x00) flushes prior to SWRESET (0x01) to protect grounded CS.
 *  - Implemented all required drawing, streaming, and font rendering primitives.
 * =====================================================================================
 */

#include <string.h>
#include <stdlib.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "py/binary.h"
#include "py/mphal.h"

#include "driver/gpio.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_types.h"
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"

#define MOCLCD_VERSION_STRING   "1.0.1-dev"
#define MOCLCD_STATUS_STRING    "DEV/UNVERIFIED"

#define LCD_PIN_NUM_D0      16
#define LCD_PIN_NUM_D1      15
#define LCD_PIN_NUM_D2      11
#define LCD_PIN_NUM_D3      10
#define LCD_PIN_NUM_D4      9
#define LCD_PIN_NUM_D5      4
#define LCD_PIN_NUM_D6      18
#define LCD_PIN_NUM_D7      17
#define LCD_PIN_NUM_DC      13
#define LCD_PIN_NUM_WR      14
#define LCD_PIN_NUM_RD      41
#define LCD_PIN_NUM_RST     12
#define LCD_PIN_NUM_BL      38
#define LCD_PIN_NUM_CS      (-1)

#define LCD_CMD_NOP         0x00
#define LCD_CMD_SWRESET     0x01
#define LCD_CMD_SLPIN       0x10
#define LCD_CMD_SLPOUT      0x11
#define LCD_CMD_INVOFF      0x20
#define LCD_CMD_INVON       0x21
#define LCD_CMD_DISPOFF     0x28
#define LCD_CMD_DISPON      0x29
#define LCD_CMD_CASET       0x2A
#define LCD_CMD_PASET       0x2B
#define LCD_CMD_RAMWR       0x2C
#define LCD_CMD_MADCTL      0x36
#define LCD_CMD_COLMOD      0x3A
#define LCD_CMD_RAMWRC      0x3C

#define FILL_CHUNK_PIXELS   1024

typedef struct {
    esp_lcd_i80_bus_handle_t  i80_bus;
    esp_lcd_panel_io_handle_t io;
    bool                      initialized;
    uint16_t                  width;
    uint16_t                  height;
    uint32_t                  pclk_hz;
    uint8_t                   madctl;
    bool                      bl_state;
} moclcd_state_t;

static moclcd_state_t s_lcd = {
    .i80_bus = NULL,
    .io = NULL,
    .initialized = false,
    .width = 480,
    .height = 320,
    .pclk_hz = 10000000,
    .madctl = 0x28,
    .bl_state = false,
};

static const int k_data_pins[8] = {
    LCD_PIN_NUM_D0, LCD_PIN_NUM_D1, LCD_PIN_NUM_D2, LCD_PIN_NUM_D3,
    LCD_PIN_NUM_D4, LCD_PIN_NUM_D5, LCD_PIN_NUM_D6, LCD_PIN_NUM_D7,
};

/* -------------------------------------------------------------------
 * Internal Bus Operations
 * ---------------------------------------------------------------- */
static inline void moclcd_check_ready(void)
{
    if (!s_lcd.initialized) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("moclcd: call init() first"));
    }
}

static void moclcd_send_cmd(uint8_t cmd, const uint8_t *params, size_t len)
{
    esp_err_t err = esp_lcd_panel_io_tx_param(s_lcd.io, cmd, params, len);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError, MP_ERROR_TEXT("moclcd: cmd 0x%02X failed (%d)"), cmd, (int)err);
    }
}

static void moclcd_set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    uint8_t caset[4] = { (uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF), (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    uint8_t paset[4] = { (uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF), (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    moclcd_send_cmd(LCD_CMD_CASET, caset, 4);
    moclcd_send_cmd(LCD_CMD_PASET, paset, 4);
}

static void moclcd_stream_pixels(const void *data, size_t len_bytes, bool is_first_chunk)
{
    uint8_t cmd = is_first_chunk ? LCD_CMD_RAMWR : LCD_CMD_RAMWRC;
    esp_err_t err = esp_lcd_panel_io_tx_color(s_lcd.io, cmd, data, len_bytes);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError, MP_ERROR_TEXT("moclcd: pixel stream failed (%d)"), (int)err);
    }
}

static void moclcd_fill_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color)
{
    moclcd_set_window(x0, y0, x1, y1);

    uint32_t total_pixels = (uint32_t)(x1 - x0 + 1) * (uint32_t)(y1 - y0 + 1);
    static uint16_t chunk_buf[FILL_CHUNK_PIXELS];
    uint16_t be_color = __builtin_bswap16(color);

    for (uint32_t i = 0; i < FILL_CHUNK_PIXELS; i++) {
        chunk_buf[i] = be_color;
    }

    bool first = true;
    while (total_pixels > 0) {
        uint32_t n = total_pixels > FILL_CHUNK_PIXELS ? FILL_CHUNK_PIXELS : total_pixels;
        moclcd_stream_pixels(chunk_buf, n * 2, first);
        first = false;
        total_pixels -= n;
    }
}

static void moclcd_hw_reset(void)
{
    gpio_set_level(LCD_PIN_NUM_RST, 1);
    mp_hal_delay_ms(50);
    gpio_set_level(LCD_PIN_NUM_RST, 0);
    mp_hal_delay_ms(100);
    gpio_set_level(LCD_PIN_NUM_RST, 1);
    mp_hal_delay_ms(100);
}

/* -------------------------------------------------------------------
 * MCUFRIEND_kbv Verbatim Register Sequence
 * ---------------------------------------------------------------- */
typedef struct {
    uint8_t cmd;
    uint8_t len;
    const uint8_t *params;
    uint16_t delay_ms;
} moclcd_init_cmd_t;

static const uint8_t k_p_colmod55[] = {0x55};
static const uint8_t k_p_pwctr1[]   = {0x10, 0x10};
static const uint8_t k_p_pwctr2[]   = {0x41};
static const uint8_t k_p_vmctr1[]   = {0x00, 0x22, 0x80, 0x40};
static const uint8_t k_p_ifmode[]   = {0x00};
static const uint8_t k_p_frmctr1[]  = {0xB0, 0x11};
static const uint8_t k_p_invtr[]    = {0x02};
static const uint8_t k_p_dfc[]      = {0x02, 0x02, 0x3B};
static const uint8_t k_p_etmod[]    = {0xC6};
static const uint8_t k_p_adjctl3[]  = {0xA9, 0x51, 0x2C, 0x82};

static const moclcd_init_cmd_t k_mcufriend_init_seq[] = {
    { LCD_CMD_NOP,     0, NULL,          10  },
    { LCD_CMD_NOP,     0, NULL,          10  },
    { LCD_CMD_SWRESET, 0, NULL,          150 },
    { LCD_CMD_DISPOFF, 0, NULL,          0   },
    { LCD_CMD_COLMOD,  1, k_p_colmod55,  0   },
    { 0xC0,            2, k_p_pwctr1,    0   },
    { 0xC1,            1, k_p_pwctr2,    0   },
    { 0xC5,            4, k_p_vmctr1,    0   },
    { LCD_CMD_MADCTL,  0, NULL,          0   },
    { 0xB0,            1, k_p_ifmode,    0   },
    { 0xB1,            2, k_p_frmctr1,   0   },
    { 0xB4,            1, k_p_invtr,     0   },
    { 0xB6,            3, k_p_dfc,       0   },
    { 0xB7,            1, k_p_etmod,     0   },
    { LCD_CMD_COLMOD,  1, k_p_colmod55,  0   },
    { 0xF7,            4, k_p_adjctl3,   0   },
    { LCD_CMD_SLPOUT,  0, NULL,          150 },
    { LCD_CMD_DISPON,  0, NULL,          0   },
};
#define INIT_SEQ_COUNT (sizeof(k_mcufriend_init_seq) / sizeof(k_mcufriend_init_seq[0]))

static void moclcd_panel_init_seq(uint8_t madctl)
{
    for (size_t i = 0; i < INIT_SEQ_COUNT; i++) {
        const moclcd_init_cmd_t *c = &k_mcufriend_init_seq[i];
        if (c->cmd == LCD_CMD_MADCTL) {
            uint8_t p = madctl;
            moclcd_send_cmd(c->cmd, &p, 1);
        } else {
            moclcd_send_cmd(c->cmd, c->len ? c->params : NULL, c->len);
        }
        if (c->delay_ms) {
            mp_hal_delay_ms(c->delay_ms);
        }
    }
}

/* -------------------------------------------------------------------
 * Bitbang Read Register Handling
 * ---------------------------------------------------------------- */
static void moclcd_data_pins_as_input(void)
{
    uint64_t mask = 0;
    for (int i = 0; i < 8; i++) mask |= (1ULL << k_data_pins[i]);
    gpio_config_t io_conf = {
        .pin_bit_mask = mask,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
}

static uint8_t moclcd_read_byte_bitbang(void)
{
    gpio_set_level(LCD_PIN_NUM_RD, 0);
    esp_rom_delay_us(1);
    uint8_t val = 0;
    for (int i = 0; i < 8; i++) {
        val |= (gpio_get_level(k_data_pins[i]) & 0x1) << i;
    }
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    esp_rom_delay_us(1);
    return val;
}

/* -------------------------------------------------------------------
 * MicroPython C Module Bindings
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_pclk, ARG_width, ARG_height, ARG_madctl };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_pclk,   MP_ARG_INT, {.u_int = 10000000} },
        { MP_QSTR_width,  MP_ARG_INT, {.u_int = 480} },
        { MP_QSTR_height, MP_ARG_INT, {.u_int = 320} },
        { MP_QSTR_madctl, MP_ARG_INT, {.u_int = 0x28} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    if (s_lcd.io) {
        esp_lcd_panel_io_del(s_lcd.io);
        s_lcd.io = NULL;
    }
    if (s_lcd.i80_bus) {
        esp_lcd_del_i80_bus(s_lcd.i80_bus);
        s_lcd.i80_bus = NULL;
    }

    s_lcd.pclk_hz = (uint32_t)args[ARG_pclk].u_int;
    s_lcd.width   = (uint16_t)args[ARG_width].u_int;
    s_lcd.height  = (uint16_t)args[ARG_height].u_int;
    s_lcd.madctl  = (uint8_t)args[ARG_madctl].u_int;

    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LCD_PIN_NUM_RST) | (1ULL << LCD_PIN_NUM_BL) | (1ULL << LCD_PIN_NUM_RD),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    gpio_set_level(LCD_PIN_NUM_BL, 0);
    gpio_set_level(LCD_PIN_NUM_RST, 1);

    esp_lcd_i80_bus_config_t bus_config = {
        .dc_gpio_num = LCD_PIN_NUM_DC,
        .wr_gpio_num = LCD_PIN_NUM_WR,
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .data_gpio_nums = {
            LCD_PIN_NUM_D0, LCD_PIN_NUM_D1, LCD_PIN_NUM_D2, LCD_PIN_NUM_D3,
            LCD_PIN_NUM_D4, LCD_PIN_NUM_D5, LCD_PIN_NUM_D6, LCD_PIN_NUM_D7,
        },
        .bus_width = 8,
        .max_transfer_bytes = 480 * 320 * 2,
        .psram_trans_align = 64,
        .sram_trans_align = 4,
    };
    esp_err_t err = esp_lcd_new_i80_bus(&bus_config, &s_lcd.i80_bus);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError, MP_ERROR_TEXT("bus init failed: %d"), (int)err);
    }

    esp_lcd_panel_io_i80_config_t io_config = {
        .cs_gpio_num = LCD_PIN_NUM_CS,
        .pclk_hz = s_lcd.pclk_hz,
        .trans_queue_depth = 10,
        .dc_levels = {
            .dc_idle_level = 0,
            .dc_cmd_level = 0,
            .dc_dummy_level = 0,
            .dc_data_level = 1,
        },
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .flags = { .swap_color_bytes = 0 },
    };
    err = esp_lcd_new_panel_io_i80(s_lcd.i80_bus, &io_config, &s_lcd.io);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError, MP_ERROR_TEXT("panel io init failed: %d"), (int)err);
    }

    s_lcd.initialized = true;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

static mp_obj_t moclcd_reset(void)
{
    moclcd_hw_reset();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_reset_obj, moclcd_reset);

static mp_obj_t moclcd_panel_init(void)
{
    moclcd_check_ready();
    moclcd_panel_init_seq(s_lcd.madctl);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

static mp_obj_t moclcd_backlight(mp_obj_t level_obj)
{
    s_lcd.bl_state = (mp_obj_get_int(level_obj) != 0);
    gpio_set_level(LCD_PIN_NUM_BL, s_lcd.bl_state ? 1 : 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_obj, moclcd_backlight);

static mp_obj_t moclcd_invert_display(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    moclcd_send_cmd(mp_obj_is_true(enable_obj) ? LCD_CMD_INVON : LCD_CMD_INVOFF, NULL, 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_invert_display_obj, moclcd_invert_display);

static mp_obj_t moclcd_sleep(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    moclcd_send_cmd(mp_obj_is_true(enable_obj) ? LCD_CMD_SLPIN : LCD_CMD_SLPOUT, NULL, 0);
    mp_hal_delay_ms(150);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_sleep_obj, moclcd_sleep);

static mp_obj_t moclcd_cmd(size_t n_args, const mp_obj_t *args)
{
    moclcd_check_ready();
    uint8_t command = (uint8_t)mp_obj_get_int(args[0]);
    if (n_args < 2 || args[1] == mp_const_none) {
        moclcd_send_cmd(command, NULL, 0);
        return mp_const_none;
    }
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[1], &bufinfo, MP_BUFFER_READ);
    moclcd_send_cmd(command, (const uint8_t *)bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_cmd_obj, 1, 2, moclcd_cmd);

static mp_obj_t moclcd_data(mp_obj_t buffer_obj)
{
    moclcd_check_ready();
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buffer_obj, &bufinfo, MP_BUFFER_READ);
    moclcd_stream_pixels(bufinfo.buf, bufinfo.len, false);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

static mp_obj_t moclcd_read_reg(size_t n_args, const mp_obj_t *args)
{
    moclcd_check_ready();
    uint8_t command = (uint8_t)mp_obj_get_int(args[0]);
    mp_int_t length = (n_args > 1) ? mp_obj_get_int(args[1]) : 1;
    if (length < 1 || length > 8) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: read length must be 1..8"));
    }

    moclcd_send_cmd(command, NULL, 0);
    moclcd_data_pins_as_input();
    gpio_set_level(LCD_PIN_NUM_RD, 1);

    uint8_t result[8] = {0};
    for (mp_int_t i = 0; i < length; i++) {
        result[i] = moclcd_read_byte_bitbang();
    }

    uint64_t mask = 0;
    for (int i = 0; i < 8; i++) mask |= (1ULL << k_data_pins[i]);
    gpio_config_t io_conf = {
        .pin_bit_mask = mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    return mp_obj_new_bytes(result, length);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_read_reg_obj, 1, 2, moclcd_read_reg);

static mp_obj_t moclcd_set_window_py(mp_obj_t x0_o, mp_obj_t y0_o, mp_obj_t x1_o, mp_obj_t y1_o)
{
    moclcd_check_ready();
    moclcd_set_window((uint16_t)mp_obj_get_int(x0_o), (uint16_t)mp_obj_get_int(y0_o),
                      (uint16_t)mp_obj_get_int(x1_o), (uint16_t)mp_obj_get_int(y1_o));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_4(moclcd_set_window_obj, moclcd_set_window_py);

/* -------------------------------------------------------------------
 * Primitives
 * ---------------------------------------------------------------- */
static mp_obj_t moclcd_pixel(mp_obj_t x_o, mp_obj_t y_o, mp_obj_t color_o)
{
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(x_o);
    uint16_t y = (uint16_t)mp_obj_get_int(y_o);
    uint16_t color = (uint16_t)mp_obj_get_int(color_o);
    moclcd_fill_window(x, y, x, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(moclcd_pixel_obj, moclcd_pixel);

static mp_obj_t moclcd_hline(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);
    if (w == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_hline_obj, 4, 4, moclcd_hline);

static mp_obj_t moclcd_vline(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);
    if (h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x, y + h - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_vline_obj, 4, 4, moclcd_vline);

static mp_obj_t moclcd_draw_rect(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);
    if (w == 0 || h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y, color);
    moclcd_fill_window(x, y + h - 1, x + w - 1, y + h - 1, color);
    moclcd_fill_window(x, y, x, y + h - 1, color);
    moclcd_fill_window(x + w - 1, y, x + w - 1, y + h - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_rect_obj, 5, 5, moclcd_draw_rect);

static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);
    if (w == 0 || h == 0) return mp_const_none;
    moclcd_fill_window(x, y, x + w - 1, y + h - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_rect_obj, 5, 5, moclcd_fill_rect);

static mp_obj_t moclcd_fill_screen(mp_obj_t color_o)
{
    moclcd_check_ready();
    uint16_t color = (uint16_t)mp_obj_get_int(color_o);
    moclcd_fill_window(0, 0, s_lcd.width - 1, s_lcd.height - 1, color);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_fill_screen_obj, moclcd_fill_screen);

static mp_obj_t moclcd_draw_line(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int x1 = mp_obj_get_int(args[2]);
    int y1 = mp_obj_get_int(args[3]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[4]);

    if (y0 == y1) {
        int x_lo = x0 < x1 ? x0 : x1;
        int w = (x0 < x1 ? x1 - x0 : x0 - x1) + 1;
        moclcd_fill_window((uint16_t)x_lo, (uint16_t)y0, (uint16_t)(x_lo + w - 1), (uint16_t)y0, color);
        return mp_const_none;
    }
    if (x0 == x1) {
        int y_lo = y0 < y1 ? y0 : y1;
        int h = (y0 < y1 ? y1 - y0 : y0 - y1) + 1;
        moclcd_fill_window((uint16_t)x0, (uint16_t)y_lo, (uint16_t)x0, (uint16_t)(y_lo + h - 1), color);
        return mp_const_none;
    }

    int dx = abs(x1 - x0), sx = x0 < x1 ? 1 : -1;
    int dy = -abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
    int err = dx + dy;
    while (1) {
        moclcd_fill_window((uint16_t)x0, (uint16_t)y0, (uint16_t)x0, (uint16_t)y0, color);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_line_obj, 5, 5, moclcd_draw_line);

static mp_obj_t moclcd_draw_circle(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int r  = mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);

    int x = r, y = 0, err = 0;
    while (x >= y) {
        moclcd_fill_window((uint16_t)(x0 + x), (uint16_t)(y0 + y), (uint16_t)(x0 + x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 + y), (uint16_t)(y0 + x), (uint16_t)(x0 + y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 + x), (uint16_t)(x0 - y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 + y), (uint16_t)(x0 - x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 - y), (uint16_t)(x0 - x), (uint16_t)(y0 - y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 - x), (uint16_t)(x0 - y), (uint16_t)(y0 - x), color);
        moclcd_fill_window((uint16_t)(x0 + y), (uint16_t)(y0 - x), (uint16_t)(x0 + y), (uint16_t)(y0 - x), color);
        moclcd_fill_window((uint16_t)(x0 + x), (uint16_t)(y0 - y), (uint16_t)(x0 + x), (uint16_t)(y0 - y), color);
        if (err <= 0) { y += 1; err += 2 * y + 1; }
        if (err > 0)  { x -= 1; err -= 2 * x + 1; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_circle_obj, 4, 4, moclcd_draw_circle);

static mp_obj_t moclcd_fill_circle(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    int x0 = mp_obj_get_int(args[0]);
    int y0 = mp_obj_get_int(args[1]);
    int r  = mp_obj_get_int(args[2]);
    uint16_t color = (uint16_t)mp_obj_get_int(args[3]);

    int x = r, y = 0, err = 0;
    while (x >= y) {
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 + y), (uint16_t)(x0 + x), (uint16_t)(y0 + y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 + x), (uint16_t)(x0 + y), (uint16_t)(y0 + x), color);
        moclcd_fill_window((uint16_t)(x0 - x), (uint16_t)(y0 - y), (uint16_t)(x0 + x), (uint16_t)(y0 - y), color);
        moclcd_fill_window((uint16_t)(x0 - y), (uint16_t)(y0 - x), (uint16_t)(x0 + y), (uint16_t)(y0 - x), color);
        if (err <= 0) { y += 1; err += 2 * y + 1; }
        if (err > 0)  { x -= 1; err -= 2 * x + 1; }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_circle_obj, 4, 4, moclcd_fill_circle);

static mp_obj_t moclcd_blit(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);

    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[4], &bufinfo, MP_BUFFER_READ);

    size_t expected = (size_t)w * (size_t)h * 2;
    if (bufinfo.len < expected) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: blit buffer too small"));
    }

    moclcd_set_window(x, y, x + w - 1, y + h - 1);
    moclcd_stream_pixels(bufinfo.buf, expected, true);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_blit_obj, 5, 5, moclcd_blit);

static mp_obj_t moclcd_draw_stream(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    uint16_t w = (uint16_t)mp_obj_get_int(args[2]);
    uint16_t h = (uint16_t)mp_obj_get_int(args[3]);
    mp_obj_t source = args[4];

    moclcd_set_window(x, y, x + w - 1, y + h - 1);

    mp_obj_t iterable = mp_getiter(source, NULL);
    mp_obj_t item;
    bool first = true;
    while ((item = mp_iternext(iterable)) != MP_OBJ_STOP_ITERATION) {
        mp_buffer_info_t bufinfo;
        mp_get_buffer_raise(item, &bufinfo, MP_BUFFER_READ);
        moclcd_stream_pixels(bufinfo.buf, bufinfo.len, first);
        first = false;
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_stream_obj, 5, 5, moclcd_draw_stream);

static mp_obj_t moclcd_fill_buffer(mp_obj_t buffer_obj, mp_obj_t color_obj)
{
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buffer_obj, &bufinfo, MP_BUFFER_RW);
    uint16_t color = (uint16_t)mp_obj_get_int(color_obj);
    uint16_t be_color = __builtin_bswap16(color);

    size_t n_pixels = bufinfo.len / 2;
    uint16_t *p = (uint16_t *)bufinfo.buf;
    for (size_t i = 0; i < n_pixels; i++) {
        p[i] = be_color;
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(moclcd_fill_buffer_obj, moclcd_fill_buffer);

/* -------------------------------------------------------------------
 * Font 5x7 Glyph Engine
 * ---------------------------------------------------------------- */
#define MOCLCD_FONT_W 5
#define MOCLCD_FONT_H 7
#define MOCLCD_FONT_FIRST 32
#define MOCLCD_FONT_LAST  126

static const uint8_t k_font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, {0x00,0x00,0x5F,0x00,0x00}, {0x00,0x07,0x00,0x07,0x00},
    {0x14,0x7F,0x14,0x7F,0x14}, {0x24,0x2A,0x7F,0x2A,0x12}, {0x23,0x13,0x08,0x64,0x62},
    {0x36,0x49,0x56,0x20,0x50}, {0x00,0x08,0x07,0x03,0x00}, {0x00,0x1C,0x22,0x41,0x00},
    {0x00,0x41,0x22,0x1C,0x00}, {0x2A,0x1C,0x7F,0x1C,0x2A}, {0x08,0x08,0x3E,0x08,0x08},
    {0x00,0x80,0x70,0x30,0x00}, {0x08,0x08,0x08,0x08,0x08}, {0x00,0x00,0x60,0x60,0x00},
    {0x20,0x10,0x08,0x04,0x02}, {0x3E,0x51,0x49,0x45,0x3E}, {0x00,0x42,0x7F,0x40,0x00},
    {0x72,0x49,0x49,0x49,0x46}, {0x21,0x41,0x49,0x4D,0x33}, {0x18,0x14,0x12,0x7F,0x10},
    {0x27,0x45,0x45,0x45,0x39}, {0x3C,0x4A,0x49,0x49,0x30}, {0x01,0x71,0x09,0x05,0x03},
    {0x36,0x49,0x49,0x49,0x36}, {0x06,0x49,0x49,0x29,0x1E}, {0x00,0x36,0x36,0x00,0x00},
    {0x00,0x56,0x36,0x00,0x00}, {0x08,0x14,0x22,0x41,0x00}, {0x14,0x14,0x14,0x14,0x14},
    {0x00,0x41,0x22,0x14,0x08}, {0x02,0x01,0x59,0x09,0x06}, {0x3E,0x41,0x5D,0x59,0x4E},
    {0x7C,0x12,0x11,0x12,0x7C}, {0x7F,0x49,0x49,0x49,0x36}, {0x3E,0x41,0x41,0x41,0x22},
    {0x7F,0x41,0x41,0x41,0x3E}, {0x7F,0x49,0x49,0x49,0x41}, {0x7F,0x09,0x09,0x09,0x01},
    {0x3E,0x41,0x41,0x51,0x73}, {0x7F,0x08,0x08,0x08,0x7F}, {0x00,0x41,0x7F,0x41,0x00},
    {0x20,0x40,0x41,0x3F,0x01}, {0x7F,0x08,0x14,0x22,0x41}, {0x7F,0x40,0x40,0x40,0x40},
    {0x7F,0x02,0x1C,0x02,0x7F}, {0x7F,0x04,0x08,0x10,0x7F}, {0x3E,0x41,0x41,0x41,0x3E},
    {0x7F,0x09,0x09,0x09,0x06}, {0x3E,0x41,0x51,0x21,0x5E}, {0x7F,0x09,0x19,0x29,0x46},
    {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7F,0x01,0x01}, {0x3F,0x40,0x40,0x40,0x3F},
    {0x1F,0x20,0x40,0x20,0x1F}, {0x7F,0x20,0x18,0x20,0x7F}, {0x63,0x14,0x08,0x14,0x63},
    {0x03,0x04,0x78,0x04,0x03}, {0x61,0x51,0x49,0x45,0x43}, {0x00,0x00,0x7F,0x41,0x41},
    {0x02,0x04,0x08,0x10,0x20}, {0x41,0x41,0x7F,0x00,0x00}, {0x04,0x02,0x01,0x02,0x04},
    {0x40,0x40,0x40,0x40,0x40}, {0x00,0x01,0x02,0x04,0x00}, {0x20,0x54,0x54,0x54,0x78},
    {0x7F,0x48,0x44,0x44,0x38}, {0x38,0x44,0x44,0x44,0x20}, {0x38,0x44,0x44,0x48,0x7F},
    {0x38,0x54,0x54,0x54,0x18}, {0x08,0x7E,0x09,0x01,0x02}, {0x0C,0x52,0x52,0x52,0x3E},
    {0x7F,0x08,0x04,0x04,0x78}, {0x00,0x44,0x7D,0x40,0x00}, {0x20,0x40,0x44,0x3D,0x00},
    {0x7F,0x10,0x28,0x44,0x00}, {0x00,0x41,0x7F,0x40,0x00}, {0x7C,0x04,0x18,0x04,0x78},
    {0x7C,0x08,0x04,0x04,0x78}, {0x38,0x44,0x44,0x44,0x38}, {0x7C,0x14,0x14,0x14,0x08},
    {0x08,0x14,0x14,0x18,0x7C}, {0x7C,0x08,0x04,0x04,0x08}, {0x48,0x54,0x54,0x54,0x20},
    {0x04,0x3F,0x44,0x40,0x20}, {0x3C,0x40,0x40,0x20,0x7C}, {0x1C,0x20,0x40,0x20,0x1C},
    {0x3C,0x40,0x30,0x40,0x3C}, {0x44,0x28,0x10,0x28,0x44}, {0x0C,0x50,0x50,0x50,0x3C},
    {0x44,0x64,0x54,0x4C,0x44}, {0x00,0x08,0x36,0x41,0x00}, {0x00,0x00,0x7F,0x00,0x00},
    {0x00,0x41,0x36,0x08,0x00}, {0x08,0x08,0x2A,0x1C,0x08},
};

static mp_obj_t moclcd_draw_char(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);

    const char *s = mp_obj_str_get_str(args[2]);
    char ch = s[0] ? s[0] : ' ';
    uint16_t fg = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t bg = (uint16_t)mp_obj_get_int(args[4]);

    int idx = (ch >= MOCLCD_FONT_FIRST && ch <= MOCLCD_FONT_LAST) ? (ch - MOCLCD_FONT_FIRST) : 0;
    const uint8_t *glyph = k_font5x7[idx];

    uint16_t be_fg = __builtin_bswap16(fg);
    uint16_t be_bg = __builtin_bswap16(bg);
    uint16_t buf[MOCLCD_FONT_W * MOCLCD_FONT_H];

    for (int col = 0; col < MOCLCD_FONT_W; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < MOCLCD_FONT_H; row++) {
            buf[row * MOCLCD_FONT_W + col] = (bits & (1 << row)) ? be_fg : be_bg;
        }
    }

    moclcd_set_window(x, y, x + MOCLCD_FONT_W - 1, y + MOCLCD_FONT_H - 1);
    moclcd_stream_pixels(buf, sizeof(buf), true);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_char_obj, 5, 5, moclcd_draw_char);

static mp_obj_t moclcd_draw_text(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    uint16_t x = (uint16_t)mp_obj_get_int(args[0]);
    uint16_t y = (uint16_t)mp_obj_get_int(args[1]);
    const char *s = mp_obj_str_get_str(args[2]);
    uint16_t fg = (uint16_t)mp_obj_get_int(args[3]);
    uint16_t bg = (uint16_t)mp_obj_get_int(args[4]);

    uint16_t be_fg = __builtin_bswap16(fg);
    uint16_t be_bg = __builtin_bswap16(bg);
    uint16_t cur_x = x;

    for (const char *p = s; *p; p++) {
        char ch = *p;
        int idx = (ch >= MOCLCD_FONT_FIRST && ch <= MOCLCD_FONT_LAST) ? (ch - MOCLCD_FONT_FIRST) : 0;
        const uint8_t *glyph = k_font5x7[idx];
        uint16_t buf[MOCLCD_FONT_W * MOCLCD_FONT_H];
        for (int col = 0; col < MOCLCD_FONT_W; col++) {
            uint8_t bits = glyph[col];
            for (int row = 0; row < MOCLCD_FONT_H; row++) {
                buf[row * MOCLCD_FONT_W + col] = (bits & (1 << row)) ? be_fg : be_bg;
            }
        }
        moclcd_set_window(cur_x, y, cur_x + MOCLCD_FONT_W - 1, y + MOCLCD_FONT_H - 1);
        moclcd_stream_pixels(buf, sizeof(buf), true);
        cur_x += MOCLCD_FONT_W + 1;
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text_obj, 5, 5, moclcd_draw_text);

/* -------------------------------------------------------------------
 * Module Globals
 * ---------------------------------------------------------------- */
static MP_DEFINE_STR_OBJ(moclcd_version_obj, MOCLCD_VERSION_STRING);
static MP_DEFINE_STR_OBJ(moclcd_build_status_obj, MOCLCD_STATUS_STRING);

static const mp_rom_map_elem_t moclcd_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),        MP_ROM_QSTR(MP_QSTR_moclcd) },
    { MP_ROM_QSTR(MP_QSTR_version),         MP_ROM_PTR(&moclcd_version_obj) },
    { MP_ROM_QSTR(MP_QSTR_build_status),    MP_ROM_PTR(&moclcd_build_status_obj) },

    { MP_ROM_QSTR(MP_QSTR_init),            MP_ROM_PTR(&moclcd_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_reset),           MP_ROM_PTR(&moclcd_reset_obj) },
    { MP_ROM_QSTR(MP_QSTR_panel_init),      MP_ROM_PTR(&moclcd_panel_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_backlight),       MP_ROM_PTR(&moclcd_backlight_obj) },
    { MP_ROM_QSTR(MP_QSTR_invert_display),  MP_ROM_PTR(&moclcd_invert_display_obj) },
    { MP_ROM_QSTR(MP_QSTR_sleep),           MP_ROM_PTR(&moclcd_sleep_obj) },

    { MP_ROM_QSTR(MP_QSTR_cmd),             MP_ROM_PTR(&moclcd_cmd_obj) },
    { MP_ROM_QSTR(MP_QSTR_data),            MP_ROM_PTR(&moclcd_data_obj) },
    { MP_ROM_QSTR(MP_QSTR_read_reg),        MP_ROM_PTR(&moclcd_read_reg_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_window),      MP_ROM_PTR(&moclcd_set_window_obj) },

    { MP_ROM_QSTR(MP_QSTR_pixel),           MP_ROM_PTR(&moclcd_pixel_obj) },
    { MP_ROM_QSTR(MP_QSTR_hline),           MP_ROM_PTR(&moclcd_hline_obj) },
    { MP_ROM_QSTR(MP_QSTR_vline),           MP_ROM_PTR(&moclcd_vline_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_line),       MP_ROM_PTR(&moclcd_draw_line_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_rect),       MP_ROM_PTR(&moclcd_draw_rect_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_rect),       MP_ROM_PTR(&moclcd_fill_rect_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_screen),     MP_ROM_PTR(&moclcd_fill_screen_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_circle),     MP_ROM_PTR(&moclcd_draw_circle_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_circle),     MP_ROM_PTR(&moclcd_fill_circle_obj) },

    { MP_ROM_QSTR(MP_QSTR_blit),            MP_ROM_PTR(&moclcd_blit_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_stream),     MP_ROM_PTR(&moclcd_draw_stream_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_buffer),     MP_ROM_PTR(&moclcd_fill_buffer_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_char),       MP_ROM_PTR(&moclcd_draw_char_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_text),       MP_ROM_PTR(&moclcd_draw_text_obj) },
};
static MP_DEFINE_CONST_DICT(moclcd_module_globals, moclcd_module_globals_table);

const mp_obj_module_t moclcd_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, moclcd_user_cmodule);
