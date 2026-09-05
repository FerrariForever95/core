/*
 * =====================================================================================
 *  FILE:         modsphere.c
 *  MODULE:       sphere (MicroPython native C module)
 *  TARGET:       ESP32-S3, ILI9488 8-bit Parallel i80 (moclcd v1.5.0 STABLE)
 *  DESCRIPTION:  Native C implementation of the low-bounce, squashing 3D sphere
 *                with dynamic ground shadow and corner FPS overlay.
 *
 *  USAGE IN MICROPYTHON:
 *      import sphere
 *      sphere.start()         # Runs continuous render loop
 *      sphere.start(300)      # Runs 300 benchmark frames and returns
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

/* -------------------------------------------------------------------------
 * Low-level moclcd driver external linkage
 * ------------------------------------------------------------------------- */
extern void moclcd_init_internal(void);
extern void moclcd_panel_init_internal(void);
extern void moclcd_backlight_internal(bool on);
extern void moclcd_fill_screen_internal(uint16_t color);
extern void moclcd_fill_rect_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
extern void moclcd_blit_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const void *buf);
extern void moclcd_draw_text_internal(uint16_t x, uint16_t y, const char *str, uint16_t fg, uint16_t bg);

/* -------------------------------------------------------------------------
 * Display and Scene Layout
 * ------------------------------------------------------------------------- */
#define SCREEN_W           480
#define SCREEN_H           320
#define CENTER_X           240
#define CENTER_Y           150
#define FOV_SCALE          240.0f
#define CAMERA_Z           3.8f

#define BB_W               280
#define BB_H               300
#define BB_X               (CENTER_X - 140)   /* 100 */
#define BB_Y               (CENTER_Y - 150)   /* 0   */
#define ROW_PITCH_BYTES    (BB_W * 2)         /* 560 */

#define COLOR_BOOT         0xF800
#define COLOR_WHITE        0xFFFF
#define COLOR_BLACK        0x0000

#define VIRTUAL_FLOOR_Y    (-1.45f)
#define SPHERE_RADIUS      (0.70f)

static uint8_t *s_frame_buf = NULL;

/* -------------------------------------------------------------------------
 * Dirty Buffer Clear
 * ------------------------------------------------------------------------- */
static inline void clear_dirty_rows(uint8_t *buf, int y0, int y1)
{
    if (y0 > y1) return;
    size_t offset = (size_t)y0 * ROW_PITCH_BYTES;
    size_t length = (size_t)(y1 - y0 + 1) * ROW_PITCH_BYTES;
    memset(buf + offset, 0xFF, length);
}

/* -------------------------------------------------------------------------
 * Dynamic Floor Shadow Renderer
 * ------------------------------------------------------------------------- */
static void render_shadow_disk(int cx, int cy, int rx, int ry, int density, uint8_t *buf, int *out_min, int *out_max)
{
    if (rx <= 0 || ry <= 0) {
        *out_min = cy;
        *out_max = cy;
        return;
    }

    int c_val = 206 - (density * 18);
    if (c_val < 96) c_val = 96;

    uint8_t hi = (uint8_t)((c_val & 0xF8) | (c_val >> 5));
    uint8_t lo = (uint8_t)(((c_val & 0x1C) << 3) | (c_val >> 3));

    int y_min = cy - ry;
    int y_max = cy + ry;
    if (y_min < 0) y_min = 0;
    if (y_max >= BB_H) y_max = BB_H - 1;

    float inv_ry2 = 1.0f / (float)(ry * ry);

    for (int y = y_min; y <= y_max; y++) {
        int dy = y - cy;
        float span_norm = 1.0f - ((float)(dy * dy) * inv_ry2);
        if (span_norm > 0.0f) {
            int half_w = (int)((float)rx * sqrtf(span_norm));
            int x0 = cx - half_w;
            int x1 = cx + half_w;
            if (x0 < 0) x0 = 0;
            if (x1 >= BB_W) x1 = BB_W - 1;

            uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (x0 << 1);
            int count = x1 - x0 + 1;
            while (count--) {
                *p++ = hi;
                *p++ = lo;
            }
        }
    }

    *out_min = y_min;
    *out_max = y_max;
}

/* -------------------------------------------------------------------------
 * Analytical Shaded Sphere Renderer with Squash Deformation
 * ------------------------------------------------------------------------- */
static void render_sphere_squash(int cx, int cy, int r_screen, float sx, float sy, uint8_t *buf, int *out_min, int *out_max)
{
    int rx = (int)((float)r_screen * sx);
    int ry = (int)((float)r_screen * sy);
    if (rx < 1) rx = 1;
    if (ry < 1) ry = 1;

    int y_min = cy - ry;
    int y_max = cy + ry;
    if (y_min < 0) y_min = 0;
    if (y_max >= BB_H) y_max = BB_H - 1;

    float inv_rx = 1.0f / (float)rx;
    float inv_ry = 1.0f / (float)ry;
    float inv_ry2 = 1.0f / (float)(ry * ry);

    for (int y = y_min; y <= y_max; y++) {
        int dy = y - cy;
        int dy2 = dy * dy;
        float norm_y = 1.0f - ((float)dy2 * inv_ry2);

        if (norm_y >= 0.0f) {
            int half_w = (int)((float)rx * sqrtf(norm_y));
            int x0 = cx - half_w;
            int x1 = cx + half_w;
            if (x0 < 0) x0 = 0;
            if (x1 >= BB_W) x1 = BB_W - 1;

            float ny_base = -(float)dy * inv_ry;
            float ny2 = ny_base * ny_base;
            float dot_k_y = ny_base * 0.57735f;
            float dot_f_y = ny_base * -0.8165f;
            float dot_h_y = ny_base * 0.3714f;

            uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (x0 << 1);

            for (int x = x0; x <= x1; x++) {
                float dx = (float)(x - cx);
                float nx = dx * inv_rx;
                float nz2 = 1.0f - (nx * nx + ny2);

                if (nz2 > 0.0f) {
                    float nz = sqrtf(nz2);

                    float dot_k = nx * 0.57735f + dot_k_y - nz * 0.57735f;
                    float diff_k = (dot_k > 0.0f) ? dot_k : 0.0f;

                    float dot_f = nx * -0.4082f + dot_f_y + nz * 0.4082f;
                    float diff_f = (dot_f > 0.0f) ? dot_f : 0.0f;

                    float dot_h = nx * 0.3714f + dot_h_y - nz * 0.8510f;
                    float spec = 0.0f;
                    if (dot_h > 0.0f) {
                        float h2 = dot_h * dot_h;
                        float h4 = h2 * h2;
                        spec = h4 * h4;
                    }

                    float r = 0.10f + 0.38f * diff_k + 0.10f * diff_f + spec * 1.35f;
                    float g = 0.35f + 1.05f * diff_k + 0.25f * diff_f + spec * 1.35f;
                    float b = 0.72f + 1.25f * diff_k + 0.50f * diff_f + spec * 1.45f;

                    if (r > 1.0f) r = 1.0f;
                    if (g > 1.0f) g = 1.0f;
                    if (b > 1.0f) b = 1.0f;

                    uint16_t ir = (uint16_t)(r * 31.0f) & 0x1F;
                    uint16_t ig = (uint16_t)(g * 63.0f) & 0x3F;
                    uint16_t ib = (uint16_t)(b * 31.0f) & 0x1F;

                    *p++ = (uint8_t)((ir << 3) | (ig >> 3));
                    *p++ = (uint8_t)(((ig & 0x07) << 5) | ib);
                } else {
                    p += 2;
                }
            }
        }
    }

    *out_min = y_min;
    *out_max = y_max;
}

/* -------------------------------------------------------------------------
 * Execution Loop: sphere.start()
 * ------------------------------------------------------------------------- */
static mp_obj_t sphere_start(size_t n_args, const mp_obj_t *args)
{
    int max_frames = (n_args > 0) ? mp_obj_get_int(args[0]) : -1;

    if (s_frame_buf == NULL) {
        s_frame_buf = (uint8_t *)heap_caps_malloc(BB_W * BB_H * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (s_frame_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("sphere: failed to allocate DMA frame buffer"));
        }
    }

    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(COLOR_BOOT);
    moclcd_fill_screen_internal(COLOR_WHITE);

    memset(s_frame_buf, 0xFF, BB_W * BB_H * 2);

    float pos_y = 0.25f;
    float vel_y = 0.0f;
    const float gravity = -0.016f;
    const float restitution = -0.72f;
    const float floor_contact_y = VIRTUAL_FLOOR_Y + SPHERE_RADIUS;

    float squash_x = 1.0f;
    float squash_y = 1.0f;

    int prev_min_y = 0;
    int prev_max_y = BB_H - 1;

    const float inv_z = 1.0f / CAMERA_Z;
    const int sphere_cx = CENTER_X - BB_X;
    const float fov_inv_z = FOV_SCALE * inv_z;
    const int shadow_cy = (int)((float)CENTER_Y - (VIRTUAL_FLOOR_Y * fov_inv_z)) - BB_Y;
    const int sphere_r = (int)(SPHERE_RADIUS * fov_inv_z);

    int frame_count = 0;
    int fps_frame_count = 0;
    int64_t t_last_fps = esp_timer_get_time();
    char fps_str[16] = "FPS: --";

    moclcd_fill_rect_internal(10, 10, 75, 12, COLOR_WHITE);
    moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);

    while (max_frames < 0 || frame_count < max_frames) {
        clear_dirty_rows(s_frame_buf, prev_min_y, prev_max_y);

        /* Physics update */
        vel_y += gravity;
        pos_y += vel_y;

        if (pos_y <= floor_contact_y) {
            pos_y = floor_contact_y;
            float impact_energy = fabsf(vel_y);
            vel_y *= restitution;

            if (fabsf(vel_y) < 0.04f) {
                vel_y = 0.11f;
            }

            float squash_factor = impact_energy * 1.5f;
            if (squash_factor > 0.25f) squash_factor = 0.25f;
            squash_y = 1.0f - squash_factor;
            squash_x = 1.0f + (squash_factor * 0.5f);
        } else {
            squash_x += (1.0f - squash_x) * 0.30f;
            squash_y += (1.0f - squash_y) * 0.30f;
        }

        /* Shadow parameters */
        float altitude = pos_y - floor_contact_y;
        int shadow_rx = (int)((float)sphere_r * (0.82f + altitude * 0.38f) * squash_x);
        int shadow_ry = (int)((float)shadow_rx * 0.30f);
        int dens_calc = (int)(6.0f - altitude * 3.5f);
        int shadow_density = (dens_calc < 1) ? 1 : ((dens_calc > 6) ? 6 : dens_calc);

        int s_min_y, s_max_y;
        render_shadow_disk(sphere_cx, shadow_cy, shadow_rx, shadow_ry, shadow_density, s_frame_buf, &s_min_y, &s_max_y);

        /* Sphere position and render */
        int sphere_cy = (int)((float)CENTER_Y - (pos_y * fov_inv_z)) - BB_Y;
        int sp_min_y, sp_max_y;
        render_sphere_squash(sphere_cx, sphere_cy, sphere_r, squash_x, squash_y, s_frame_buf, &sp_min_y, &sp_max_y);

        int frame_min_y = (s_min_y < sp_min_y) ? s_min_y : sp_min_y;
        int frame_max_y = (s_max_y > sp_max_y) ? s_max_y : sp_max_y;

        if (frame_min_y < 0) frame_min_y = 0;
        if (frame_max_y >= BB_H) frame_max_y = BB_H - 1;

        int blit_top = (frame_min_y < prev_min_y) ? frame_min_y : prev_min_y;
        int blit_bottom = (frame_max_y > prev_max_y) ? frame_max_y : prev_max_y;
        int blit_h = blit_bottom - blit_top + 1;

        size_t start_offset = (size_t)blit_top * ROW_PITCH_BYTES;
        moclcd_blit_internal(BB_X, BB_Y + blit_top, BB_W, blit_h, s_frame_buf + start_offset);

        prev_min_y = frame_min_y;
        prev_max_y = frame_max_y;

        /* Corner FPS counter */
        fps_frame_count++;
        if (fps_frame_count >= 20) {
            int64_t now = esp_timer_get_time();
            int64_t dt = now - t_last_fps;
            if (dt > 0) {
                float fps = (fps_frame_count * 1000000.0f) / (float)dt;
                snprintf(fps_str, sizeof(fps_str), "FPS: %.1f", fps);
                moclcd_fill_rect_internal(10, 10, 75, 10, COLOR_WHITE);
                moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);
            }
            t_last_fps = now;
            fps_frame_count = 0;
        }

        frame_count++;
    }

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(sphere_start_obj, 0, 1, sphere_start);

/* -------------------------------------------------------------------------
 * MicroPython Module Definition
 * ------------------------------------------------------------------------- */
static const mp_rom_map_elem_t sphere_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_sphere) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&sphere_start_obj) },
};
static MP_DEFINE_CONST_DICT(sphere_module_globals, sphere_module_globals_table);

const mp_obj_module_t sphere_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&sphere_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_sphere, sphere_user_cmodule);
