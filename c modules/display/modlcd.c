/*
 * lcd_min.c — minimal 8080 8-bit parallel bring-up module.
 * Updated with your specific pin mappings:
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

#include <string.h>

#define LCD_CMD_RAMWRC 0x3C

/* ---- single-display module state ---- */
STATIC esp_lcd_i80_bus_handle_t  s_bus       = NULL;
STATIC esp_lcd_panel_io_handle_t s_io        = NULL;
STATIC mp_hal_pin_obj_t          s_reset_pin;
STATIC bool                      s_has_reset = false;

/* -------------------------------------------------------------------
 * lcd_min.init(pclk=10_000_000)
 * Configures your specific pin mapping automatically using 
 * ESP-IDF's hardware 8080 parallel peripheral.
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_init(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    enum { ARG_pclk };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_pclk, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = 10000000} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    /* --- Your Exact Data Pins (D0 through D7) --- */
    int data_gpios[8] = { 16, 15, 11, 10, 9, 4, 18, 17 };

    esp_lcd_i80_bus_config_t bus_cfg = {
        .dc_gpio_num = 13, /* RS */
        .wr_gpio_num = 14, /* WR */
        .clk_src     = LCD_CLK_SRC_PLL160M,
        .data_gpio_nums = {
            data_gpios[0],
            data_gpios[1],
            data_gpios[2],
            data_gpios[3],
            data_gpios[4],
            data_gpios[5],
            data_gpios[6],
            data_gpios[7],
        },
        .bus_width          = 8,
        .max_transfer_bytes = 480 * 320 * 3,
    };
    esp_err_t ret = esp_lcd_new_i80_bus(&bus_cfg, &s_bus);
    if (ret != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("esp_lcd_new_i80_bus failed: %d"), ret);
    }

    esp_lcd_panel_io_i80_config_t io_cfg = {
        .cs_gpio_num       = -1,   /* CS tied LOW in hardware */
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

    /* --- Configure RD Pin (GPIO 41) --- */
    mp_hal_pin_obj_t rd_pin = 41;
    mp_hal_pin_output(rd_pin);
    mp_hal_pin_write(rd_pin, 1);   /* idle HIGH */

    /* --- Configure Backlight Pin (GPIO 38) --- */
    mp_hal_pin_obj_t bl_pin = 38;
    mp_hal_pin_output(bl_pin);
    mp_hal_pin_write(bl_pin, 1);   /* Backlight ON */

    /* --- Configure Reset Pin (GPIO 12) --- */
    s_reset_pin = 12;
    mp_hal_pin_output(s_reset_pin);
    mp_hal_pin_write(s_reset_pin, 1);  
    s_has_reset = true;

    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_KW(lcd_min_init_obj, 0, lcd_min_init);

/* -------------------------------------------------------------------
 * lcd_min.reset()
 * ---------------------------------------------------------------- */
STATIC mp_obj_t lcd_min_reset(void)
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
STATIC MP_DEFINE_CONST_FUN_OBJ_0(lcd_min_reset_obj, lcd_min_reset);

/* -------------------------------------------------------------------
 * lcd_min.cmd(cmd, params=None)
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
