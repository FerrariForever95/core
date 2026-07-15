/*
 * lcd_min.c — minimal 8080 8-bit parallel bring-up module.
 *
 * Purpose: verify the physical bus (D0-D7, WR, DC, RESET, RD) is wired
 * and timed correctly BEFORE building any ILI9488-specific driver logic.
 * Exposes exactly four Python-callable functions:
 *
 *   lcd_min.init(data, dc, wr, rd=None, reset=None, pclk=10_000_000)
 *   lcd_min.reset()
 *   lcd_min.cmd(cmd, params=None)
 *   lcd_min.data(buf)
 *
 * Nothing else. No rotation, no gamma, no framebuffer helpers, no
 * object model — just enough to push bytes on the bus and confirm with
 * a logic analyzer / scope that CMD and DATA cycles look correct.
 *
 * CS is not handled here at all: per your hardware, CS is tied
 * permanently LOW, so it is simply never wired into esp_lcd
 * (cs_gpio_num = -1) and never toggled.
 */

#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "mphalport.h"

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"

#include <string.h>

/* ILI9488 command used to stream additional GRAM data without
 * resending 0x2C (RAMWR) / CASET / RASET. This is the controller's own
 * documented mechanism for "just send more pixel bytes" and works
 * identically on every bus type, unlike the lcd_cmd=-1 "skip command
 * phase" trick (which ESP-IDF's own docs mark as SPI/I2C-only, not
 * guaranteed for I80). */
#define LCD_CMD_RAMWRC 0x3C

/* ---- single-display module state (no object model, by design) ---- */
STATIC esp_lcd_i80_bus_handle_t  s_bus       = NULL;
STATIC esp_lcd_panel_io_handle_t s_io        = NULL;
STATIC mp_hal_pin_obj_t          s_reset_pin;
STATIC bool                      s_has_reset = false;

/* -------------------------------------------------------------------
 * lcd_min.init(data, dc, wr, rd=None, reset=None, pclk=10_000_000)
 *   data  : tuple of 8 Pin objects, (D0, D1, ... D7)
 *   dc    : Pin, D/C (RS) line
 *   wr    : Pin, WR line
 *   rd    : Pin, RD line (optional). Configured as output, driven and
 *           left HIGH. Never toggled again — esp_lcd's I80 bus is
 *           write-only, this just satisfies the controller's idle
 *           requirement.
 *   reset : Pin, RESET line (optional). Configured as output, idled
 *           HIGH (active-low reset).
 *   pclk  : write clock in Hz (start conservative, e.g. 5-10 MHz,
 *           while confirming signal integrity through level shifters)
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_data, ARG_dc, ARG_wr, ARG_rd, ARG_reset, ARG_pclk };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_data,  MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL}    },
        { MP_QSTR_dc,    MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL}    },
        { MP_QSTR_wr,    MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL}    },
        { MP_QSTR_rd,    MP_ARG_KW_ONLY | MP_ARG_OBJ,  {.u_obj = mp_const_none}  },
        { MP_QSTR_reset, MP_ARG_KW_ONLY | MP_ARG_OBJ,  {.u_obj = mp_const_none}  },
        { MP_QSTR_pclk,  MP_ARG_KW_ONLY | MP_ARG_INT,  {.u_int = 10000000}       },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    mp_obj_tuple_t *data_t = MP_OBJ_TO_PTR(args[ARG_data].u_obj);
    if (data_t->len != 8) {
        mp_raise_ValueError(MP_ERROR_TEXT("data= must be a tuple of exactly 8 pins (D0..D7)"));
    }

    esp_lcd_i80_bus_config_t bus_cfg = {
        .dc_gpio_num = mp_hal_get_pin_obj(args[ARG_dc].u_obj),
        .wr_gpio_num = mp_hal_get_pin_obj(args[ARG_wr].u_obj),
        .clk_src     = LCD_CLK_SRC_PLL160M,
        .data_gpio_nums = {
            mp_hal_get_pin_obj(data_t->items[0]),
            mp_hal_get_pin_obj(data_t->items[1]),
            mp_hal_get_pin_obj(data_t->items[2]),
            mp_hal_get_pin_obj(data_t->items[3]),
            mp_hal_get_pin_obj(data_t->items[4]),
            mp_hal_get_pin_obj(data_t->items[5]),
            mp_hal_get_pin_obj(data_t->items[6]),
            mp_hal_get_pin_obj(data_t->items[7]),
        },
        .bus_width          = 8,
        .max_transfer_bytes = 480 * 320 * 3,  /* worst case: full frame, 18bpp */
    };
    esp_err_t ret = esp_lcd_new_i80_bus(&bus_cfg, &s_bus);
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_lcd_new_i80_bus failed: %d"), ret);
    }

    esp_lcd_panel_io_i80_config_t io_cfg = {
        .cs_gpio_num       = -1,   /* CS is tied LOW in hardware; not driven by MCU */
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
    ret = esp_lcd_new_panel_io_i80(s_bus, &io_cfg, &s_io);
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_lcd_new_panel_io_i80 failed: %d"), ret);
    }

    if (args[ARG_rd].u_obj != mp_const_none) {
        mp_hal_pin_obj_t rd_pin = mp_hal_get_pin_obj(args[ARG_rd].u_obj);
        mp_hal_pin_output(rd_pin);
        mp_hal_pin_write(rd_pin, 1);   /* idle HIGH, never toggled */
    }

    if (args[ARG_reset].u_obj != mp_const_none) {
        s_reset_pin = mp_hal_get_pin_obj(args[ARG_reset].u_obj);
        mp_hal_pin_output(s_reset_pin);
        mp_hal_pin_write(s_reset_pin, 1);  /* idle HIGH (active-low reset) */
        s_has_reset = true;
    } else {
        s_has_reset = false;
    }

    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_KW(lcd_min_init_obj, 0, lcd_min_init);


/* -------------------------------------------------------------------
 * lcd_min.reset()
 *   RESX low for 20ms (datasheet minimum is 10us; 20ms gives margin
 *   and is trivial to see on a logic analyzer), then release and wait
 *   120ms before any command is sent, per ILI9488 reset timing.
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_reset(void)
{
    if (!s_has_reset) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("no reset pin configured in init()"));
    }
    mp_hal_pin_write(s_reset_pin, 0);
    mp_hal_delay_us(20 * 1000);
    mp_hal_pin_write(s_reset_pin, 1);
    mp_hal_delay_us(120 * 1000);
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(lcd_min_reset_obj, lcd_min_reset);


/* -------------------------------------------------------------------
 * lcd_min.cmd(cmd, params=None)
 *   One 8080 command-write cycle (DC=0, one byte) optionally followed
 *   by N data-write cycles (DC=1) carrying `params`. This is the
 *   primitive used for every register write: SWRESET, SLPOUT, MADCTL,
 *   COLMOD, CASET, RASET, RAMWR(with no data yet), DISPON, etc.
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_cmd(size_t n_args, const mp_obj_t *args_in)
{
    if (s_io == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("lcd_min.init() must be called first"));
    }
    int cmd = mp_obj_get_int(args_in[0]);

    const void *buf = NULL;
    size_t len = 0;
    mp_buffer_info_t bufinfo;
    if (n_args == 2 && args_in[1] != mp_const_none) {
        mp_get_buffer_raise(args_in[1], &bufinfo, MP_BUFFER_READ);
        buf = bufinfo.buf;
        len = bufinfo.len;
    }

    esp_err_t ret = esp_lcd_panel_io_tx_param(s_io, cmd, buf, len);
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("cmd 0x%02x failed: %d"), cmd, ret);
    }
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(lcd_min_cmd_obj, 1, 2, lcd_min_cmd);


/* -------------------------------------------------------------------
 * lcd_min.data(buf)
 *   Streams a raw block of pixel/GRAM bytes. Internally issues
 *   RAMWRC (0x3C, "Memory Write Continue") ahead of the buffer, which
 *   tells the ILI9488 to keep writing from wherever the last RAMWR
 *   (0x2C) / CASET / RASET window left off — so you can call this
 *   repeatedly for one big transfer without resending 0x2C each time.
 *   For the very first chunk of a write, send 0x2C via cmd() instead,
 *   e.g.: cmd(0x2C); data(first_chunk); data(next_chunk); ...
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_data(mp_obj_t buf_in)
{
    if (s_io == NULL) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("lcd_min.init() must be called first"));
    }
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(buf_in, &bufinfo, MP_BUFFER_READ);

    esp_err_t ret = esp_lcd_panel_io_tx_color(s_io, LCD_CMD_RAMWRC, bufinfo.buf, bufinfo.len);
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("data write failed: %d"), ret);
    }
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(lcd_min_data_obj, lcd_min_data);


/* ---- module table ---- */
STATIC const mp_rom_map_elem_t lcd_min_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_lcd_min)     },
    { MP_ROM_QSTR(MP_QSTR_init),     MP_ROM_PTR(&lcd_min_init_obj)   },
    { MP_ROM_QSTR(MP_QSTR_reset),    MP_ROM_PTR(&lcd_min_reset_obj)  },
    { MP_ROM_QSTR(MP_QSTR_cmd),      MP_ROM_PTR(&lcd_min_cmd_obj)    },
    { MP_ROM_QSTR(MP_QSTR_data),     MP_ROM_PTR(&lcd_min_data_obj)   },
};
STATIC MP_DEFINE_CONST_DICT(lcd_min_globals, lcd_min_globals_table);

const mp_obj_module_t mp_module_lcd_min = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&lcd_min_globals,
};

MP_REGISTER_MODULE(MP_QSTR_lcd_min, mp_module_lcd_min);
