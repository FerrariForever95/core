/*
 * =====================================================================================
 *  FILE:        modlcd.c
 *  MODULE:      moclcd  (MicroPython native C module)
 *  TARGET:      ESP32-S3, ESP-IDF esp_lcd i80 (Intel 8080) parallel bus, 8-bit data
 *  PANEL:       ILI9488, 320 x 480, MIPI DCS Rev.1 command set
 *
 *  VERSION:     1.5.0-STABLE
 *  STAGE:       1 of 2 (display output with C-linkage exports for modcube.c)
 * =====================================================================================
 */

#include <string.h>
#include <stdlib.h>
#include <stdbool.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objstr.h"
#include "py/objarray.h"
#include "py/binary.h"
#include "py/mphal.h"

#include "driver/gpio.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_sys.h"

static const char *TAG = "moclcd";

#define MOCLCD_VERSION_STRING   "1.5.0-STABLE"
#define MOCLCD_STATUS_STRING    "STABLE"

/* Hardware Pin Definitions */
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

/* Commands */
#define ILI9488_CMD_CASET   0x2A
#define ILI9488_CMD_PASET   0x2B
#define ILI9488_CMD_RAMWR   0x2C
#define ILI9488_CMD_RAMWRC  0x3C
#define ILI9488_CMD_RAMRD   0x2E
#define ILI9488_CMD_MADCTL  0x36
#define ILI9488_CMD_COLMOD  0x3A
#define ILI9488_CMD_SLPOUT  0x11
#define ILI9488_CMD_SLPIN   0x10
#define ILI9488_CMD_DISPON  0x29
#define ILI9488_CMD_DISPOFF 0x28
#define ILI9488_CMD_SWRESET 0x01
#define ILI9488_CMD_INVON   0x21
#define ILI9488_CMD_INVOFF  0x20
#define ILI9488_CMD_RDDID   0x04
#define ILI9488_CMD_RDID4   0xD3

typedef struct {
    esp_lcd_i80_bus_handle_t   i80_bus;
    esp_lcd_panel_io_handle_t  io;
    bool                       initialized;
    uint16_t                   width;
    uint16_t                   height;
    uint32_t                   pclk_hz;
    uint8_t                    madctl;
    bool                       bl_state;
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

static const int k_data_pins_fwd[8] = {
    LCD_PIN_NUM_D0, LCD_PIN_NUM_D1, LCD_PIN_NUM_D2, LCD_PIN_NUM_D3,
    LCD_PIN_NUM_D4, LCD_PIN_NUM_D5, LCD_PIN_NUM_D6, LCD_PIN_NUM_D7,
};

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
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: cmd 0x%02X failed (err=%d)"), cmd, (int)err);
    }
}

static void moclcd_send_pixels(const void *data, size_t len_bytes)
{
    esp_err_t err = esp_lcd_panel_io_tx_color(s_lcd.io, ILI9488_CMD_RAMWRC, data, len_bytes);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: pixel stream failed (err=%d)"), (int)err);
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

typedef struct {
    uint8_t cmd;
    uint8_t len;
    const uint8_t *params;
    uint16_t delay_ms;
} moclcd_init_cmd_t;

static const uint8_t k_p_none[]      = {0x00};
static const uint8_t k_p_colmod55[]  = {0x55};
static const uint8_t k_p_pwctr1[]    = {0x10, 0x10};
static const uint8_t k_p_pwctr2[]    = {0x41};
static const uint8_t k_p_vmctr1[]    = {0x00, 0x22, 0x80, 0x40};
static const uint8_t k_p_madctl[]    = {0x68};
static const uint8_t k_p_ifmode[]    = {0x00};
static const uint8_t k_p_frmctr1[]   = {0xB0, 0x11};
static const uint8_t k_p_invtr[]     = {0x02};
static const uint8_t k_p_dfc[]       = {0x02, 0x02, 0x3B};
static const uint8_t k_p_etmod[]     = {0xC6};
static const uint8_t k_p_adjctl3[]   = {0xA9, 0x51, 0x2C, 0x82};

static const moclcd_init_cmd_t k_ili9488_init_seq[] = {
    { ILI9488_CMD_SWRESET, 0, k_p_none,      150 },
    { ILI9488_CMD_DISPOFF, 0, k_p_none,      0   },
    { ILI9488_CMD_COLMOD,  1, k_p_colmod55,  0   },
    { 0xC0,                2, k_p_pwctr1,    0   },
    { 0xC1,                1, k_p_pwctr2,    0   },
    { 0xC5,                4, k_p_vmctr1,    0   },
    { ILI9488_CMD_MADCTL,  1, k_p_madctl,    0   },
    { 0xB0,                1, k_p_ifmode,    0   },
    { 0xB1,                2, k_p_frmctr1,   0   },
    { 0xB4,                1, k_p_invtr,     0   },
    { 0xB6,                3, k_p_dfc,       0   },
    { 0xB7,                1, k_p_etmod,     0   },
    { ILI9488_CMD_COLMOD,  1, k_p_colmod55,  0   },
    { 0xF7,                4, k_p_adjctl3,   0   },
    { ILI9488_CMD_SLPOUT,  0, k_p_none,      150 },
    { ILI9488_CMD_DISPON,  0, k_p_none,      0   },
};
#define ILI9488_INIT_SEQ_COUNT (sizeof(k_ili9488_init_seq)/sizeof(k_ili9488_init_seq[0]))

static void moclcd_bus_deinit_if_needed(void)
{
    if (s_lcd.io) {
        esp_lcd_panel_io_del(s_lcd.io);
        s_lcd.io = NULL;
    }
    if (s_lcd.i80_bus) {
        esp_lcd_del_i80_bus(s_lcd.i80_bus);
        s_lcd.i80_bus = NULL;
    }
}

static void moclcd_bus_init(uint32_t pclk_hz)
{
    moclcd_bus_deinit_if_needed();

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
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: esp_lcd_new_i80_bus failed (err=%d)"), (int)err);
    }

    esp_lcd_panel_io_i80_config_t io_config = {
        .cs_gpio_num = LCD_PIN_NUM_CS,
        .pclk_hz = pclk_hz,
        .trans_queue_depth = 10,
        .dc_levels = {
            .dc_idle_level = 0,
            .dc_cmd_level = 0,
            .dc_dummy_level = 0,
            .dc_data_level = 1,
        },
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .flags = {
            .swap_color_bytes = 0,
        },
    };

    err = esp_lcd_new_panel_io_i80(s_lcd.i80_bus, &io_config, &s_lcd.io);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("moclcd: esp_lcd_new_panel_io_i80 failed (err=%d)"), (int)err);
    }
}

static void moclcd_bus_pins_safe_idle(void)
{
    uint64_t data_mask = 0;
    for (int i = 0; i < 8; i++) {
        data_mask |= (1ULL << k_data_pins_fwd[i]);
    }

    gpio_config_t io_conf = {
        .pin_bit_mask = data_mask
                       | (1ULL << LCD_PIN_NUM_DC)
                       | (1ULL << LCD_PIN_NUM_WR)
                       | (1ULL << LCD_PIN_NUM_RD)
                       | (1ULL << LCD_PIN_NUM_RST)
                       | (1ULL << LCD_PIN_NUM_BL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    for (int i = 0; i < 8; i++) {
        gpio_set_level(k_data_pins_fwd[i], 0);
    }
    gpio_set_level(LCD_PIN_NUM_DC, 1);
    gpio_set_level(LCD_PIN_NUM_WR, 1);
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    gpio_set_level(LCD_PIN_NUM_RST, 0);
    gpio_set_level(LCD_PIN_NUM_BL, 0);
}

static void moclcd_gpio_init(void)
{
    moclcd_bus_pins_safe_idle();
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
    gpio_set_level(LCD_PIN_NUM_RST, 0);
}

static void moclcd_panel_init_seq(uint8_t madctl)
{
    for (size_t i = 0; i < ILI9488_INIT_SEQ_COUNT; i++) {
        const moclcd_init_cmd_t *c = &k_ili9488_init_seq[i];
        if (c->cmd == ILI9488_CMD_MADCTL) {
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

static void moclcd_set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    uint8_t caset[4] = { (uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF),
                         (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF) };
    uint8_t paset[4] = { (uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF),
                         (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF) };
    moclcd_send_cmd(ILI9488_CMD_CASET, caset, 4);
    moclcd_send_cmd(ILI9488_CMD_PASET, paset, 4);
    moclcd_send_cmd(ILI9488_CMD_RAMWR, NULL, 0);
}

#define MOCLCD_FILL_CHUNK_PIXELS  1024
static void moclcd_fill_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color)
{
    moclcd_set_window(x0, y0, x1, y1);

    uint32_t total_pixels = (uint32_t)(x1 - x0 + 1) * (uint32_t)(y1 - y0 + 1);
    static uint16_t chunk_buf[MOCLCD_FILL_CHUNK_PIXELS];
    uint16_t be_color = __builtin_bswap16(color);
    for (uint32_t i = 0; i < MOCLCD_FILL_CHUNK_PIXELS; i++) {
        chunk_buf[i] = be_color;
    }

    while (total_pixels > 0) {
        uint32_t n = total_pixels > MOCLCD_FILL_CHUNK_PIXELS ? MOCLCD_FILL_CHUNK_PIXELS : total_pixels;
        moclcd_send_pixels(chunk_buf, n * 2);
        total_pixels -= n;
    }
}

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
    {0x00,0x41,0x36,0x08,0x00}, {0x08,0x08,0x2A,0x1C,0x08}
};

/* ===================================================================================
 *  EXPORTED C-LINKAGE INTERFACES (Used by modcube.c)
 * =================================================================================== */
void moclcd_init_internal(void)
{
    if (s_lcd.initialized) return;
    s_lcd.pclk_hz = 10000000;
    s_lcd.width   = 480;
    s_lcd.height  = 320;
    s_lcd.madctl  = 0x28;

    moclcd_gpio_init();
    moclcd_bus_init(s_lcd.pclk_hz);
    moclcd_hw_reset();
    s_lcd.initialized = true;
}

void moclcd_panel_init_internal(void)
{
    moclcd_panel_init_seq(s_lcd.madctl);
}

void moclcd_backlight_internal(bool on)
{
    s_lcd.bl_state = on;
    gpio_set_level(LCD_PIN_NUM_BL, on ? 1 : 0);
}

void moclcd_fill_screen_internal(uint16_t color)
{
    moclcd_fill_window(0, 0, s_lcd.width - 1, s_lcd.height - 1, color);
}

void moclcd_fill_rect_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color)
{
    if (w == 0 || h == 0) return;
    moclcd_fill_window(x, y, x + w - 1, y + h - 1, color);
}

void moclcd_blit_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const void *buf)
{
    if (w == 0 || h == 0) return;
    moclcd_set_window(x, y, x + w - 1, y + h - 1);
    moclcd_send_pixels(buf, (size_t)w * h * 2);
}

void moclcd_draw_text_internal(uint16_t x, uint16_t y, const char *str, uint16_t fg, uint16_t bg)
{
    uint16_t be_fg = __builtin_bswap16(fg);
    uint16_t be_bg = __builtin_bswap16(bg);
    uint16_t cur_x = x;

    for (const char *p = str; *p; p++) {
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
        moclcd_send_pixels(buf, sizeof(buf));
        cur_x += MOCLCD_FONT_W + 1;
    }
}

/* ===================================================================================
 *  MICROPYTHON WRAPPERS
 * =================================================================================== */
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

    uint32_t pclk_hz = (uint32_t)args[ARG_pclk].u_int;
    if (pclk_hz == 0 || pclk_hz > 20000000) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: pclk out of safe range (<=20MHz)"));
    }

    s_lcd.pclk_hz = pclk_hz;
    s_lcd.width   = (uint16_t)args[ARG_width].u_int;
    s_lcd.height  = (uint16_t)args[ARG_height].u_int;
    s_lcd.madctl  = (uint8_t)args[ARG_madctl].u_int;

    moclcd_gpio_init();
    moclcd_bus_init(s_lcd.pclk_hz);
    moclcd_hw_reset();

    s_lcd.initialized = true;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(moclcd_init_obj, 0, moclcd_init);

static mp_obj_t moclcd_reset(void)
{
    moclcd_bus_pins_safe_idle();
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LCD_PIN_NUM_RST) | (1ULL << LCD_PIN_NUM_RD) | (1ULL << LCD_PIN_NUM_BL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    moclcd_hw_reset();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_reset_obj, moclcd_reset);

static mp_obj_t moclcd_panel_init(void)
{
    moclcd_check_ready();
    moclcd_panel_init_internal();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(moclcd_panel_init_obj, moclcd_panel_init);

static mp_obj_t moclcd_backlight(mp_obj_t level_obj)
{
    int level = mp_obj_get_int(level_obj);
    moclcd_backlight_internal(level != 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_backlight_obj, moclcd_backlight);

static mp_obj_t moclcd_invert_display(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    bool enable = mp_obj_is_true(enable_obj);
    moclcd_send_cmd(enable ? ILI9488_CMD_INVON : ILI9488_CMD_INVOFF, NULL, 0);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_invert_display_obj, moclcd_invert_display);

static mp_obj_t moclcd_sleep(mp_obj_t enable_obj)
{
    moclcd_check_ready();
    bool enable = mp_obj_is_true(enable_obj);
    moclcd_send_cmd(enable ? ILI9488_CMD_SLPIN : ILI9488_CMD_SLPOUT, NULL, 0);
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
    moclcd_send_pixels(bufinfo.buf, bufinfo.len);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_data_obj, moclcd_data);

static void moclcd_manual_send_cmd_only(uint8_t cmd)
{
    gpio_config_t out_conf = {
        .pin_bit_mask = 0,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    for (int i = 0; i < 8; i++) out_conf.pin_bit_mask |= (1ULL << k_data_pins_fwd[i]);
    out_conf.pin_bit_mask |= (1ULL << LCD_PIN_NUM_DC) | (1ULL << LCD_PIN_NUM_WR);
    gpio_config(&out_conf);

    gpio_set_level(LCD_PIN_NUM_DC, 0);
    for (int i = 0; i < 8; i++) {
        gpio_set_level(k_data_pins_fwd[i], (cmd >> i) & 0x1);
    }
    gpio_set_level(LCD_PIN_NUM_WR, 0);
    esp_rom_delay_us(1);
    gpio_set_level(LCD_PIN_NUM_WR, 1);
    esp_rom_delay_us(1);
    gpio_set_level(LCD_PIN_NUM_DC, 1);
}

static uint8_t moclcd_read_byte_bitbang(void)
{
    gpio_set_level(LCD_PIN_NUM_RD, 0);
    esp_rom_delay_us(1);
    uint8_t val = 0;
    for (int i = 0; i < 8; i++) {
        val |= (gpio_get_level(k_data_pins_fwd[i]) & 0x1) << i;
    }
    gpio_set_level(LCD_PIN_NUM_RD, 1);
    esp_rom_delay_us(1);
    return val;
}

static mp_obj_t moclcd_read_reg(size_t n_args, const mp_obj_t *args)
{
    moclcd_check_ready();
    uint8_t command = (uint8_t)mp_obj_get_int(args[0]);
    mp_int_t length = (n_args > 1) ? mp_obj_get_int(args[1]) : 1;
    if (length < 1 || length > 8) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: read_reg length must be 1..8"));
    }

    moclcd_bus_deinit_if_needed();
    moclcd_manual_send_cmd_only(command);

    uint64_t data_mask = 0;
    for (int i = 0; i < 8; i++) data_mask |= (1ULL << k_data_pins_fwd[i]);
    gpio_config_t in_conf = {
        .pin_bit_mask = data_mask,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&in_conf);
    gpio_set_level(LCD_PIN_NUM_RD, 1);

    uint8_t result[8] = {0};
    for (mp_int_t i = 0; i < length; i++) {
        result[i] = moclcd_read_byte_bitbang();
    }

    moclcd_bus_init(s_lcd.pclk_hz);
    return mp_obj_new_bytes(result, length);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_read_reg_obj, 1, 2, moclcd_read_reg);

static mp_obj_t moclcd_set_window_py(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    moclcd_set_window((uint16_t)mp_obj_get_int(args[0]), (uint16_t)mp_obj_get_int(args[1]),
                      (uint16_t)mp_obj_get_int(args[2]), (uint16_t)mp_obj_get_int(args[3]));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_set_window_obj, 4, 4, moclcd_set_window_py);

static mp_obj_t moclcd_fill_rect(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    moclcd_fill_rect_internal((uint16_t)mp_obj_get_int(args[0]),
                              (uint16_t)mp_obj_get_int(args[1]),
                              (uint16_t)mp_obj_get_int(args[2]),
                              (uint16_t)mp_obj_get_int(args[3]),
                              (uint16_t)mp_obj_get_int(args[4]));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_fill_rect_obj, 5, 5, moclcd_fill_rect);

static mp_obj_t moclcd_fill_screen(mp_obj_t color_o)
{
    moclcd_check_ready();
    moclcd_fill_screen_internal((uint16_t)mp_obj_get_int(color_o));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(moclcd_fill_screen_obj, moclcd_fill_screen);

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
    if (bufinfo.len < ((size_t)w * h * 2)) {
        mp_raise_ValueError(MP_ERROR_TEXT("moclcd: blit buffer too small for w*h*2"));
    }
    moclcd_blit_internal(x, y, w, h, bufinfo.buf);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_blit_obj, 5, 5, moclcd_blit);

static mp_obj_t moclcd_draw_text(size_t n_args, const mp_obj_t *args)
{
    (void)n_args;
    moclcd_check_ready();
    moclcd_draw_text_internal((uint16_t)mp_obj_get_int(args[0]),
                              (uint16_t)mp_obj_get_int(args[1]),
                              mp_obj_str_get_str(args[2]),
                              (uint16_t)mp_obj_get_int(args[3]),
                              (uint16_t)mp_obj_get_int(args[4]));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(moclcd_draw_text_obj, 5, 5, moclcd_draw_text);

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
    { MP_ROM_QSTR(MP_QSTR_fill_rect),       MP_ROM_PTR(&moclcd_fill_rect_obj) },
    { MP_ROM_QSTR(MP_QSTR_fill_screen),     MP_ROM_PTR(&moclcd_fill_screen_obj) },
    { MP_ROM_QSTR(MP_QSTR_blit),            MP_ROM_PTR(&moclcd_blit_obj) },
    { MP_ROM_QSTR(MP_QSTR_draw_text),       MP_ROM_PTR(&moclcd_draw_text_obj) },
};
static MP_DEFINE_CONST_DICT(moclcd_module_globals, moclcd_module_globals_table);

const mp_obj_module_t moclcd_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&moclcd_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_moclcd, moclcd_user_cmodule);
