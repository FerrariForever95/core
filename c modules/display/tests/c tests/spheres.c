/*
 * =====================================================================================
 *  FILE:         modbilliards.c
 *  MODULE:       billiards (MicroPython native C module)
 *  TARGET:       ESP32-S3, ILI9488 8-bit Parallel i80 (moclcd v1.5.0-STABLE)
 *  DESCRIPTION:  High-performance Multi-Body 3D Billiard Spheres simulation in C.
 *                Features 3 Glossy Colored Spheres (Ruby Red, Cyan Teal, Liquid Gold)
 *                with 3D elastic collisions, momentum transfer, dynamic squash/stretch,
 *                additive shadow blending, back-to-front depth sorting, and a 72 FPS cap.
 *                Includes multi-tier DMA buffer allocation fallback (Internal SRAM -> 
 *                PSRAM / SPIRAM -> General 8-bit heap) to prevent allocation crashes.
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
#define CENTER_Y              150
#define FOV_SCALE             240.0f
#define CAMERA_Z              3.8f

#define BB_W                  380
#define BB_H                  300
#define BB_X                  (CENTER_X - 190)   /* 50 */
#define BB_Y                  (CENTER_Y - 150)   /* 0  */
#define ROW_PITCH_BYTES       (BB_W * 2)         /* 760 */

#define COLOR_BOOT            0xF800
#define COLOR_WHITE           0xFFFF
#define COLOR_BLACK           0x0000

#define VIRTUAL_FLOOR_Y       (-1.45f)
#define SPHERE_RADIUS         (0.44f)
#define NUM_SPHERES           3

/* Target 72 FPS: 13,888 microseconds per frame */
#define TARGET_FRAME_TIME_US  13888

static uint8_t *s_frame_buf = NULL;

/* -------------------------------------------------------------------------
 * Memory Clear Helper
 * ------------------------------------------------------------------------- */
static inline void clear_dirty_rows(uint8_t *buf, int y0, int y1)
{
    if (y0 > y1) return;
    size_t offset = (size_t)y0 * ROW_PITCH_BYTES;
    size_t length = (size_t)(y1 - y0 + 1) * ROW_PITCH_BYTES;
    memset(buf + offset, 0xFF, length);
}

/* -------------------------------------------------------------------------
 * Multi-Shadow Ground Footprint (with Additive Overlap Darkening)
 * ------------------------------------------------------------------------- */
static void render_shadow_disk(int cx, int cy, int rx, int ry, int density, uint8_t *buf, int *out_min, int *out_max)
{
    if (rx <= 0 || ry <= 0) {
        *out_min = cy;
        *out_max = cy;
        return;
    }

    int c_val = 210 - (density * 16);
    if (c_val < 90) c_val = 90;

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
                uint8_t curr_hi = *p;
                uint8_t curr_lo = *(p + 1);

                if (curr_hi == 0xFF && curr_lo == 0xFF) {
                    *p++ = hi;
                    *p++ = lo;
                } else {
                    /* Darken overlapping shadow intersections */
                    *p++ = 0x63;
                    *p++ = 0x2C;
                }
            }
        }
    }

    *out_min = y_min;
    *out_max = y_max;
}

/* -------------------------------------------------------------------------
 * Analytical 3D Sphere Renderer with Colored Base & Squash Deformation
 * ------------------------------------------------------------------------- */
static void render_sphere_squash(int cx, int cy, int r_screen, float sx, float sy,
                                 float base_r, float base_g, float base_b,
                                 uint8_t *buf, int *out_min, int *out_max)
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

                    float r = (0.12f + 0.90f * diff_k + 0.20f * diff_f) * base_r + spec * 1.40f;
                    float g = (0.12f + 0.90f * diff_k + 0.20f * diff_f) * base_g + spec * 1.40f;
                    float b = (0.12f + 0.90f * diff_k + 0.20f * diff_f) * base_b + spec * 1.40f;

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
 * Execution Loop: billiards.start()
 * ------------------------------------------------------------------------- */
static mp_obj_t billiards_start(size_t n_args, const mp_obj_t *args)
{
    int max_frames = (n_args > 0) ? mp_obj_get_int(args[0]) : -1;

    if (s_frame_buf == NULL) {
        size_t buf_size = (size_t)BB_W * BB_H * 2;
        
        /* Step 1: Attempt allocation in high-speed Internal DMA SRAM */
        s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);

        /* Step 2: Fallback to PSRAM (SPIRAM) with 64-byte alignment */
        if (s_frame_buf == NULL) {
            s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_SPIRAM);
        }

        /* Step 3: Generic DMA heap fallback */
        if (s_frame_buf == NULL) {
            s_frame_buf = (uint8_t *)heap_caps_aligned_alloc(64, buf_size, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
        }

        if (s_frame_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("billiards: failed to allocate DMA frame buffer in SRAM or PSRAM"));
        }
    }

    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(COLOR_BOOT);
    moclcd_fill_screen_internal(COLOR_WHITE);

    memset(s_frame_buf, 0xFF, BB_W * BB_H * 2);

    /* Physics & Position arrays */
    float px[NUM_SPHERES] = { -0.75f,  0.00f,  0.75f };
    float py[NUM_SPHERES] = {  0.60f,  0.95f,  0.30f };
    float pz[NUM_SPHERES] = {  0.15f, -0.20f,  0.05f };

    float vx[NUM_SPHERES] = {  0.016f, -0.012f, -0.014f };
    float vy[NUM_SPHERES] = {  0.000f,  0.000f,  0.000f };
    float vz[NUM_SPHERES] = {  0.011f,  0.014f, -0.018f };

    float squash_x[NUM_SPHERES] = { 1.0f, 1.0f, 1.0f };
    float squash_y[NUM_SPHERES] = { 1.0f, 1.0f, 1.0f };

    /* Materials: Ruby Red, Cyan Teal, Liquid Gold */
    const float mat_r[NUM_SPHERES] = { 0.90f, 0.10f, 0.92f };
    const float mat_g[NUM_SPHERES] = { 0.15f, 0.85f, 0.70f };
    const float mat_b[NUM_SPHERES] = { 0.20f, 0.95f, 0.12f };

    const float gravity = -0.013f;
    const float floor_contact_y = VIRTUAL_FLOOR_Y + SPHERE_RADIUS;
    const float two_r = SPHERE_RADIUS * 2.0f;
    const float two_r_sq = two_r * two_r;
    const float bound_x = 1.70f;
    const float bound_z = 0.70f;

    int prev_min_y = 0;
    int prev_max_y = BB_H - 1;

    int frame_count = 0;
    int fps_frame_count = 0;
    int64_t t_last_fps = esp_timer_get_time();
    char fps_str[36] = "FPS: -- | Capped to 72";

    moclcd_fill_rect_internal(10, 10, 160, 12, COLOR_WHITE);
    moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);

    while (max_frames < 0 || frame_count < max_frames) {
        int64_t frame_start = esp_timer_get_time();

        /* Trap Ctrl+C (KeyboardInterrupt) cleanly to return to REPL */
        mp_handle_pending(true);

        clear_dirty_rows(s_frame_buf, prev_min_y, prev_max_y);

        /* -------------------------------------------------------------
         * 1. Multi-Body Physics & Momentum Exchange
         * ------------------------------------------------------------- */
        for (int i = 0; i < NUM_SPHERES; i++) {
            vy[i] += gravity;
            px[i] += vx[i];
            py[i] += vy[i];
            pz[i] += vz[i];

            /* Floor bounce & squash reaction */
            if (py[i] <= floor_contact_y) {
                py[i] = floor_contact_y;
                float impact_energy = fabsf(vy[i]);
                vy[i] *= -0.86f;

                if (fabsf(vy[i]) < 0.04f) {
                    vy[i] = 0.16f;
                }

                float squash_factor = impact_energy * 1.5f;
                if (squash_factor > 0.32f) squash_factor = 0.32f;
                squash_y[i] = 1.0f - squash_factor;
                squash_x[i] = 1.0f + (squash_factor * 0.5f);
            } else {
                squash_x[i] += (1.0f - squash_x[i]) * 0.22f;
                squash_y[i] += (1.0f - squash_y[i]) * 0.22f;
            }

            /* Boundary walls (X and Z) */
            if (px[i] < -bound_x) {
                px[i] = -bound_x;
                vx[i] = -vx[i] * 0.95f;
            } else if (px[i] > bound_x) {
                px[i] = bound_x;
                vx[i] = -vx[i] * 0.95f;
            }

            if (pz[i] < -bound_z) {
                pz[i] = -bound_z;
                vz[i] = -vz[i] * 0.95f;
            } else if (pz[i] > bound_z) {
                pz[i] = bound_z;
                vz[i] = -vz[i] * 0.95f;
            }
        }

        /* Full 3D Elastic Pairwise Collisions */
        for (int i = 0; i < NUM_SPHERES - 1; i++) {
            for (int j = i + 1; j < NUM_SPHERES; j++) {
                float dx = px[j] - px[i];
                float dy = py[j] - py[i];
                float dz = pz[j] - pz[i];
                float dist_sq = dx * dx + dy * dy + dz * dz;

                if (dist_sq < two_r_sq && dist_sq > 0.00001f) {
                    float dist = sqrtf(dist_sq);
                    float inv_d = 1.0f / dist;
                    float nx = dx * inv_d;
                    float ny = dy * inv_d;
                    float nz = dz * inv_d;

                    /* Positional separation (prevents intersection sticking) */
                    float overlap = 0.5f * (two_r - dist);
                    px[i] -= nx * overlap;
                    py[i] -= ny * overlap;
                    pz[i] -= nz * overlap;
                    px[j] += nx * overlap;
                    py[j] += ny * overlap;
                    pz[j] += nz * overlap;

                    /* Relative velocity dot normal */
                    float k_vel = (vx[i] - vx[j]) * nx + (vy[i] - vy[j]) * ny + (vz[i] - vz[j]) * nz;
                    if (k_vel > 0.0f) {
                        float impulse = k_vel * 0.92f;
                        vx[i] -= impulse * nx;
                        vy[i] -= impulse * ny;
                        vz[i] -= impulse * nz;
                        vx[j] += impulse * nx;
                        vy[j] += impulse * ny;
                        vz[j] += impulse * nz;
                    }
                }
            }
        }

        /* -------------------------------------------------------------
         * 2. Perspective Projection & Dynamic Shadow Calculations
         * ------------------------------------------------------------- */
        int scr_x[NUM_SPHERES], scr_y[NUM_SPHERES], scr_r[NUM_SPHERES];
        int shad_cy[NUM_SPHERES], shad_rx[NUM_SPHERES], shad_ry[NUM_SPHERES], shad_dens[NUM_SPHERES];

        for (int i = 0; i < NUM_SPHERES; i++) {
            float eff_z = CAMERA_Z + pz[i];
            float inv_z = 1.0f / eff_z;
            float fov_inv_z = FOV_SCALE * inv_z;

            scr_x[i] = (int)((CENTER_X + (px[i] * fov_inv_z)) - BB_X);
            scr_y[i] = (int)((CENTER_Y - (py[i] * fov_inv_z)) - BB_Y);
            scr_r[i] = (int)(SPHERE_RADIUS * fov_inv_z);

            float altitude = py[i] - floor_contact_y;
            shad_cy[i] = (int)((CENTER_Y - (VIRTUAL_FLOOR_Y * fov_inv_z)) - BB_Y);
            shad_rx[i] = (int)((float)scr_r[i] * (0.80f + altitude * 0.38f) * squash_x[i]);
            shad_ry[i] = (int)((float)shad_rx[i] * 0.30f);

            int dens = (int)(6.0f - altitude * 2.4f);
            shad_dens[i] = (dens < 1) ? 1 : ((dens > 6) ? 6 : dens);
        }

        /* Painter's Depth Sorting: back-to-front (largest PZ rendered first) */
        int sort_order[NUM_SPHERES] = { 0, 1, 2 };
        if (pz[sort_order[0]] > pz[sort_order[1]]) {
            int t = sort_order[0]; sort_order[0] = sort_order[1]; sort_order[1] = t;
        }
        if (pz[sort_order[1]] > pz[sort_order[2]]) {
            int t = sort_order[1]; sort_order[1] = sort_order[2]; sort_order[2] = t;
        }
        if (pz[sort_order[0]] > pz[sort_order[1]]) {
            int t = sort_order[0]; sort_order[0] = sort_order[1]; sort_order[1] = t;
        }

        /* -------------------------------------------------------------
         * 3. Multi-Shadow Ground Footprints
         * ------------------------------------------------------------- */
        int frame_min_y = BB_H;
        int frame_max_y = 0;

        for (int i = 0; i < NUM_SPHERES; i++) {
            int s_min, s_max;
            render_shadow_disk(scr_x[i], shad_cy[i], shad_rx[i], shad_ry[i], shad_dens[i], s_frame_buf, &s_min, &s_max);
            if (s_min < frame_min_y) frame_min_y = s_min;
            if (s_max > frame_max_y) frame_max_y = s_max;
        }

        /* -------------------------------------------------------------
         * 4. Multi-Sphere Rasterization (Sorted Depth Order)
         * ------------------------------------------------------------- */
        for (int idx = 0; idx < NUM_SPHERES; idx++) {
            int i = sort_order[idx];
            int sp_min, sp_max;
            render_sphere_squash(scr_x[i], scr_y[i], scr_r[i], squash_x[i], squash_y[i],
                                 mat_r[i], mat_g[i], mat_b[i], s_frame_buf, &sp_min, &sp_max);
            if (sp_min < frame_min_y) frame_min_y = sp_min;
            if (sp_max > frame_max_y) frame_max_y = sp_max;
        }

        if (frame_min_y < 0) frame_min_y = 0;
        if (frame_max_y >= BB_H) frame_max_y = BB_H - 1;

        int blit_top = (frame_min_y < prev_min_y) ? frame_min_y : prev_min_y;
        int blit_bottom = (frame_max_y > prev_max_y) ? frame_max_y : prev_max_y;
        int blit_h = blit_bottom - blit_top + 1;

        size_t start_offset = (size_t)blit_top * ROW_PITCH_BYTES;
        moclcd_blit_internal(BB_X, BB_Y + blit_top, BB_W, blit_h, s_frame_buf + start_offset);

        prev_min_y = frame_min_y;
        prev_max_y = frame_max_y;

        /* Precision 72 FPS Frame Rate Limiter */
        int64_t elapsed_us = esp_timer_get_time() - frame_start;
        if (elapsed_us < TARGET_FRAME_TIME_US) {
            esp_rom_delay_us((uint32_t)(TARGET_FRAME_TIME_US - elapsed_us));
        }

        fps_frame_count++;
        if (fps_frame_count >= 20) {
            int64_t now = esp_timer_get_time();
            int64_t dt = now - t_last_fps;
            if (dt > 0) {
                float fps = (fps_frame_count * 1000000.0f) / (float)dt;
                snprintf(fps_str, sizeof(fps_str), "FPS: %.1f | Capped to 72", fps);
                moclcd_fill_rect_internal(10, 10, 160, 10, COLOR_WHITE);
                moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);
            }
            t_last_fps = now;
            fps_frame_count = 0;
        }

        frame_count++;
    }

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(billiards_start_obj, 0, 1, billiards_start);

/* -------------------------------------------------------------------------
 * MicroPython Module Registration
 * ------------------------------------------------------------------------- */
static const mp_rom_map_elem_t billiards_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_billiards) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&billiards_start_obj) },
};
static MP_DEFINE_CONST_DICT(billiards_module_globals, billiards_module_globals_table);

const mp_obj_module_t billiards_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&billiards_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_billiards, billiards_user_cmodule);
