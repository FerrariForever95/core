/*
 * =====================================================================================
 *  FILE:         modhighway.c
 *  MODULE:       highway (MicroPython native C module)
 *  TARGET:       ESP32-S3, ILI9488 8-bit Parallel i80 (moclcd v1.5.0-STABLE)
 *  DESCRIPTION:  High-speed pseudo-3D perspective highway engine in native C.
 *                Features an authentic Nissan GT-R (R35) chase-cam model with
 *                curving track scanlines, multi-tone rumble curbs, lane divider
 *                projections, suspension bounce, quad exhausts, iconic circular halo
 *                taillights, and multi-tier DMA buffer allocation fallback.
 *
 *  USAGE:
 *      import highway
 *      highway.start()       # Runs at default 72 FPS cap
 *      highway.start(30)     # Sets dynamic frame rate cap to 30 FPS
 *      highway.start(60)     # Sets dynamic frame rate cap to 60 FPS
 * =====================================================================================
 */

#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdbool.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mphal.h"

#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_rom_sys.h"

/* -------------------------------------------------------------------------
 * Low-level moclcd driver linkage (non-static symbols in modlcd.c)
 * ------------------------------------------------------------------------- */
extern void moclcd_init_internal(void);
extern void moclcd_panel_init_internal(void);
extern void moclcd_backlight_internal(bool on);
extern void moclcd_fill_screen_internal(uint16_t color);
extern void moclcd_fill_rect_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
extern void moclcd_blit_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const void *buf);
extern void moclcd_draw_text_internal(uint16_t x, uint16_t y, const char *str, uint16_t fg, uint16_t bg);

/* -------------------------------------------------------------------------
 * Display and Projection Geometry
 * ------------------------------------------------------------------------- */
#define SCREEN_W              480
#define SCREEN_H              320
#define CENTER_X              240
#define HORIZON_Y             124

#define BB_W                  380
#define BB_H                  300
#define BB_X                  (CENTER_X - (BB_W / 2))  /* 50 */
#define BB_Y                  10
#define ROW_PITCH_BYTES       (BB_W * 2)               /* 760 */

#define COLOR_BOOT            0xF800
#define COLOR_WHITE           0xFFFF
#define COLOR_BLACK           0x0000

#define DEFAULT_FPS           72

/* -------------------------------------------------------------------------
 * Nissan GT-R (R35) Signature Color Palette (RGB565)
 * ------------------------------------------------------------------------- */
#define COL_GTR_BLUE          0x1A7F
#define COL_GTR_SHADOW        0x0974
#define COL_CARBON_DIFF       0x10A2
#define COL_CHROME_TIPS       0xCE79
#define COL_TAIL_OUTER        0xF800
#define COL_TAIL_CORE         0xFFE0
#define COL_GLASS_TINT        0x1125
#define COL_WING_CARBON       0x0841

static uint8_t *s_frame_buf = NULL;

/* -------------------------------------------------------------------------
 * Low-Level Rasterizers
 * ------------------------------------------------------------------------- */
static inline void fill_rect_buf(int x0, int y0, int w, int h, uint8_t hi, uint8_t lo, uint8_t *buf)
{
    int x1 = x0 + w;
    int y1 = y0 + h;
    if (x0 < 0) x0 = 0;
    if (x1 > BB_W) x1 = BB_W;
    if (y0 < 0) y0 = 0;
    if (y1 > BB_H) y1 = BB_H;
    if (x0 >= x1 || y0 >= y1) return;

    for (int y = y0; y < y1; y++) {
        uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (x0 << 1);
        int cnt = x1 - x0;
        while (cnt--) {
            *p++ = hi;
            *p++ = lo;
        }
    }
}

static inline void fill_trapezoid_buf(int cx, int y0, int y1, int w_top, int w_bot, uint8_t hi, uint8_t lo, uint8_t *buf)
{
    if (y0 >= y1 || y1 <= 0 || y0 >= BB_H) return;
    int dy = y1 - y0;
    float inv_dy = 1.0f / (float)dy;

    for (int y = y0; y < y1; y++) {
        if (y >= 0 && y < BB_H) {
            float t = (float)(y - y0) * inv_dy;
            int half_w = (int)(((float)w_top * (1.0f - t) + (float)w_bot * t) * 0.5f);
            int x0 = cx - half_w;
            int x1 = cx + half_w;
            if (x0 < 0) x0 = 0;
            if (x1 > BB_W) x1 = BB_W;
            if (x0 < x1) {
                uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (x0 << 1);
                int cnt = x1 - x0;
                while (cnt--) {
                    *p++ = hi;
                    *p++ = lo;
                }
            }
        }
    }
}

/* -------------------------------------------------------------------------
 * Curving Highway Scanline Generator
 * ------------------------------------------------------------------------- */
static void render_track(uint8_t *buf, float pos_z, float curve_val)
{
    const int bb_cx = BB_W / 2;

    /* 1. Sky Gradient */
    for (int y = 0; y < 120; y++) {
        float t = (float)y * 0.00833f;
        int r = (int)(12.0f + t * 18.0f);
        int g = (int)(16.0f + t * 24.0f);
        int b = (int)(40.0f + t * 38.0f);
        uint8_t hi = (uint8_t)(((r & 0x1F) << 3) | ((g >> 3) & 0x07));
        uint8_t lo = (uint8_t)((((g & 0x07) << 5) | (b & 0x1F)) & 0xFF);

        uint8_t *p = buf + (y * ROW_PITCH_BYTES);
        int cnt = BB_W;
        while (cnt--) {
            *p++ = hi;
            *p++ = lo;
        }
    }

    /* 2. Track Surface Scanlines */
    for (int y = 120; y < BB_H; y++) {
        int dy = y - 118;
        float z = 1800.0f / (float)dy;
        float world_z = z + pos_z;

        float scale = 160.0f / z;
        int road_w = (int)(145.0f * scale);
        int curb_w = (int)(18.0f * scale);
        if (curb_w < 2) curb_w = 2;
        int line_w = (int)(3.5f * scale);
        if (line_w < 1) line_w = 1;

        int curve_offset = (int)((z * z * 0.00035f) * curve_val);
        int center_x = bb_cx + curve_offset;

        int seg = (int)(world_z * 0.09f) & 1;
        int line_seg = (int)(world_z * 0.18f) & 1;

        uint8_t ghi, glo, rhi, rlo, chi, clo;
        if (seg == 0) {
            ghi = 0x13; glo = 0x41;
            rhi = 0x31; rlo = 0xA6;
            chi = 0xD8; clo = 0x82;
        } else {
            ghi = 0x0B; glo = 0x01;
            rhi = 0x21; rlo = 0x24;
            chi = 0xEF; clo = 0x7D;
        }

        int rl = center_x - road_w;
        int rr = center_x + road_w;
        int cl = rl - curb_w;
        int cr = rr + curb_w;
        int ll = center_x - line_w;
        int lr = center_x + line_w;

        uint8_t *p = buf + (y * ROW_PITCH_BYTES);
        for (int x = 0; x < BB_W; x++) {
            if (x < cl || x > cr) {
                *p++ = ghi;
                *p++ = glo;
            } else if (x < rl || x > rr) {
                *p++ = chi;
                *p++ = clo;
            } else {
                if (line_seg == 0 && (x >= ll && x <= lr)) {
                    *p++ = 0xFF;
                    *p++ = 0xFF;
                } else {
                    *p++ = rhi;
                    *p++ = rlo;
                }
            }
        }
    }
}

/* -------------------------------------------------------------------------
 * Nissan GT-R (R35) Rear Profile Rasterizer
 * ------------------------------------------------------------------------- */
static void render_gtr(int cx, int cy, int steer_lean, uint8_t *buf)
{
    /* 1. Ground contact shadow */
    fill_trapezoid_buf(cx, cy + 10, cy + 22, 108, 126, 0x08, 0x41, buf);

    /* 2. Wide rear tires */
    fill_rect_buf(cx - 50, cy - 4, 16, 20, 0x18, 0xC3, buf);
    fill_rect_buf(cx + 34, cy - 4, 16, 20, 0x18, 0xC3, buf);

    /* 3. Carbon rear diffuser with center rear fog light */
    fill_trapezoid_buf(cx, cy + 2, cy + 16, 86, 94, 0x10, 0xA2, buf);
    fill_rect_buf(cx - 4, cy + 8, 8, 4, 0xF8, 0x00, buf);

    /* 4. Massive Dual Quad Exhaust Tips */
    fill_rect_buf(cx - 38, cy + 4, 10, 8, 0xCE, 0x79, buf);
    fill_rect_buf(cx - 26, cy + 4, 10, 8, 0xCE, 0x79, buf);
    fill_rect_buf(cx + 16, cy + 4, 10, 8, 0xCE, 0x79, buf);
    fill_rect_buf(cx + 28, cy + 4, 10, 8, 0xCE, 0x79, buf);
    /* Dark exhaust bores */
    fill_rect_buf(cx - 36, cy + 6, 6, 4, 0x08, 0x41, buf);
    fill_rect_buf(cx - 24, cy + 6, 6, 4, 0x08, 0x41, buf);
    fill_rect_buf(cx + 18, cy + 6, 6, 4, 0x08, 0x41, buf);
    fill_rect_buf(cx + 30, cy + 6, 6, 4, 0x08, 0x41, buf);

    /* 5. Broad R35 Rear Bumper and Fenders */
    fill_trapezoid_buf(cx, cy - 14, cy + 4, 98, 92, 0x09, 0x74, buf);
    fill_trapezoid_buf(cx, cy - 24, cy - 14, 92, 98, 0x1A, 0x7F, buf);
    /* Inset license plate recess */
    fill_rect_buf(cx - 18, cy - 8, 36, 10, 0x10, 0xA2, buf);

    /* 6. Iconic 4 Circular Halo Taillights (Outer Large, Inner Small) */
    /* Left Outer Ring */
    fill_rect_buf(cx - 40, cy - 20, 10, 10, 0xF8, 0x00, buf);
    fill_rect_buf(cx - 38, cy - 18, 6, 6, 0xFF, 0xE0, buf);
    /* Left Inner Ring */
    fill_rect_buf(cx - 26, cy - 19, 8, 8, 0xF8, 0x00, buf);
    fill_rect_buf(cx - 24, cy - 17, 4, 4, 0xFF, 0xE0, buf);
    /* Right Inner Ring */
    fill_rect_buf(cx + 18, cy - 19, 8, 8, 0xF8, 0x00, buf);
    fill_rect_buf(cx + 20, cy - 17, 4, 4, 0xFF, 0xE0, buf);
    /* Right Outer Ring */
    fill_rect_buf(cx + 30, cy - 20, 10, 10, 0xF8, 0x00, buf);
    fill_rect_buf(cx + 32, cy - 18, 6, 6, 0xFF, 0xE0, buf);

    /* 7. Angular R35 Greenhouse Canopy & Tinted Glass */
    int cockpit_cx = cx + steer_lean;
    fill_trapezoid_buf(cockpit_cx, cy - 44, cy - 24, 52, 70, 0x1A, 0x7F, buf);
    fill_trapezoid_buf(cockpit_cx, cy - 42, cy - 26, 42, 58, 0x11, 0x25, buf);

    /* 8. Factory Trunk-Mounted Pedestal Spoiler */
    int wing_cx = cx + (steer_lean >> 1);
    int wing_y = cy - 30;
    fill_rect_buf(wing_cx - 28, wing_y + 4, 4, 8, 0x08, 0x41, buf);
    fill_rect_buf(wing_cx + 24, wing_y + 4, 4, 8, 0x08, 0x41, buf);
    fill_rect_buf(wing_cx - 42, wing_y, 84, 4, 0x08, 0x41, buf);
}

/* -------------------------------------------------------------------------
 * Execution Loop: highway.start(target_fps=72)
 * ------------------------------------------------------------------------- */
static mp_obj_t highway_start(size_t n_args, const mp_obj_t *args)
{
    int target_fps = DEFAULT_FPS;
    if (n_args > 0) {
        target_fps = mp_obj_get_int(args[0]);
        if (target_fps < 1) target_fps = 1;
        if (target_fps > 120) target_fps = 120;
    }

    uint32_t target_frame_time_us = (uint32_t)(1000000 / target_fps);

    if (s_frame_buf == NULL) {
        size_t buf_size = (size_t)BB_W * BB_H * 2;
        
        /* 1. High-speed Internal DMA SRAM */
        s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);

        /* 2. Fallback to PSRAM (SPIRAM) with 64-byte alignment */
        if (s_frame_buf == NULL) {
            s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_SPIRAM);
        }

        /* 3. Generic DMA heap fallback */
        if (s_frame_buf == NULL) {
            s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
        }

        if (s_frame_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("highway: failed to allocate DMA frame buffer in SRAM or PSRAM"));
        }
    }

    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(COLOR_BOOT);
    moclcd_fill_screen_internal(COLOR_WHITE);

    float road_z = 0.0f;
    float curve_phase = 0.0f;
    const int car_base_x = BB_W / 2;
    const int car_base_y = 260;

    int fps_frame_count = 0;
    int64_t t_last_fps = esp_timer_get_time();
    char fps_str[40];
    snprintf(fps_str, sizeof(fps_str), "FPS: -- | Capped to %d", target_fps);

    moclcd_fill_rect_internal(10, 10, 160, 12, COLOR_WHITE);
    moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);

    while (true) {
        int64_t frame_start = esp_timer_get_time();

        /* Catch Ctrl+C cleanly to return to REPL */
        mp_handle_pending(true);

        road_z += 34.0f;
        curve_phase += 0.022f;
        float curve = sinf(curve_phase) * 1.7f;

        int steer_lean = (int)(curve * 3.6f);
        int car_draw_x = car_base_x + (int)(sinf(curve_phase * 1.4f) * 26.0f);
        int suspension_bob = (int)(sinf(road_z * 0.35f) * 1.5f);

        render_track(s_frame_buf, road_z, curve);
        render_gtr(car_draw_x, car_base_y + suspension_bob, steer_lean, s_frame_buf);

        /* Push entire viewport to LCD display via DMA */
        moclcd_blit_internal(BB_X, BB_Y, BB_W, BB_H, s_frame_buf);

        /* Dynamic Target Frame Rate Limiter */
        int64_t elapsed_us = esp_timer_get_time() - frame_start;
        if (elapsed_us < (int64_t)target_frame_time_us) {
            esp_rom_delay_us((uint32_t)((int64_t)target_frame_time_us - elapsed_us));
        }

        fps_frame_count++;
        if (fps_frame_count >= 20) {
            int64_t now = esp_timer_get_time();
            int64_t dt = now - t_last_fps;
            if (dt > 0) {
                float fps = (fps_frame_count * 1000000.0f) / (float)dt;
                snprintf(fps_str, sizeof(fps_str), "FPS: %.1f | Capped to %d", fps, target_fps);
                moclcd_fill_rect_internal(10, 10, 160, 10, COLOR_WHITE);
                moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);
            }
            t_last_fps = now;
            fps_frame_count = 0;
        }
    }

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(highway_start_obj, 0, 1, highway_start);

/* -------------------------------------------------------------------------
 * MicroPython Module Registration
 * ------------------------------------------------------------------------- */
static const mp_rom_map_elem_t highway_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_highway) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&highway_start_obj) },
};
static MP_DEFINE_CONST_DICT(highway_module_globals, highway_module_globals_table);

const mp_obj_module_t highway_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&highway_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_highway, highway_user_cmodule);
