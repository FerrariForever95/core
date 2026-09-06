/*
 * =====================================================================================
 *  FILE:         modgyro.c
 *  MODULE:       gyro (MicroPython native C module)
 *  TARGET:       ESP32-S3, ILI9488 8-bit Parallel i80 (moclcd v1.5.0-STABLE)
 *  DESCRIPTION:  High-stress 3D Gyroscopic Engine: Dual Precessing Concentric
 *                Torus Rings, Central Chrome Core Sphere, Dynamic Negative-Hole
 *                Floor Shadows, Interleaved Painter's Depth Sorting, Fixed 72 FPS
 *                Frame Pacing, and Clean Ctrl+C REPL Breakout.
 *
 *  USAGE:
 *      import gyro
 *      gyro.start()       # Continuous rendering loop; Ctrl+C returns to REPL
 *      gyro.start(500)    # Runs for 500 benchmark frames or until Ctrl+C
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
 * Scene & Screen Topology
 * ------------------------------------------------------------------------- */
#define SCREEN_W              480
#define SCREEN_H              320
#define CENTER_X              240
#define CENTER_Y              145
#define FOV_SCALE             240.0f
#define CAMERA_Z              4.2f

#define BB_W                  420
#define BB_H                  300
#define BB_X                  (CENTER_X - 210)  /* 30  */
#define BB_Y                  (CENTER_Y - 145)  /* 0   */
#define ROW_PITCH_BYTES       (BB_W * 2)        /* 840 */

#define COLOR_BOOT            0xF800
#define COLOR_WHITE           0xFFFF
#define COLOR_BLACK           0x0000

#define VIRTUAL_FLOOR_Y       (-1.50f)
#define LIGHT_DIR_X           (-0.25f)
#define LIGHT_DIR_Y           (-1.75f)
#define LIGHT_DIR_Z           (-0.20f)
#define INV_LIGHT_DIR_Y       (1.0f / LIGHT_DIR_Y)

#define TARGET_FRAME_TIME_US  13888             /* 72 FPS */

/* Torus Mesh Resolution: 16 Ring Slices x 8 Tube Slices = 128 Verts / Quads */
#define SEGS_U                16
#define SEGS_V                8
#define TOTAL_VERTS           (SEGS_U * SEGS_V)
#define TOTAL_FACES           TOTAL_VERTS

#define R_OUTER               1.15f
#define R_TUBE1               0.16f

#define R_INNER               0.78f
#define R_TUBE2               0.13f

#define CORE_SPHERE_RADIUS    0.38f

static uint8_t *s_frame_buf = NULL;
static int s_edge_min[BB_H];
static int s_edge_max[BB_H];

/* Parametric Mesh Coordinates */
static float s_torus1_u[TOTAL_VERTS];
static float s_torus1_v[TOTAL_VERTS];
static float s_torus1_w[TOTAL_VERTS];

static float s_torus2_u[TOTAL_VERTS];
static float s_torus2_v[TOTAL_VERTS];
static float s_torus2_w[TOTAL_VERTS];

static int s_face_v0[TOTAL_FACES];
static int s_face_v1[TOTAL_FACES];
static int s_face_v2[TOTAL_FACES];
static int s_face_v3[TOTAL_FACES];

static bool s_mesh_initialized = false;

static void init_torus_meshes(void)
{
    if (s_mesh_initialized) return;

    int idx = 0;
    const float two_pi = 6.28318530718f;

    for (int u_step = 0; u_step < SEGS_U; u_step++) {
        float u = (float)u_step * (two_pi / (float)SEGS_U);
        float cos_u = cosf(u);
        float sin_u = sinf(u);

        for (int v_step = 0; v_step < SEGS_V; v_step++) {
            float v = (float)v_step * (two_pi / (float)SEGS_V);
            float cos_v = cosf(v);
            float sin_v = sinf(v);

            /* Outer Ring Mesh */
            s_torus1_u[idx] = (R_OUTER + R_TUBE1 * cos_v) * cos_u;
            s_torus1_v[idx] = (R_OUTER + R_TUBE1 * cos_v) * sin_u;
            s_torus1_w[idx] = R_TUBE1 * sin_v;

            /* Inner Ring Mesh */
            s_torus2_u[idx] = (R_INNER + R_TUBE2 * cos_v) * cos_u;
            s_torus2_v[idx] = (R_INNER + R_TUBE2 * cos_v) * sin_u;
            s_torus2_w[idx] = R_TUBE2 * sin_v;

            idx++;
        }
    }

    int f_idx = 0;
    for (int u_step = 0; u_step < SEGS_U; u_step++) {
        int next_u = (u_step + 1) % SEGS_U;
        for (int v_step = 0; v_step < SEGS_V; v_step++) {
            int next_v = (v_step + 1) % SEGS_V;
            s_face_v0[f_idx] = u_step * SEGS_V + v_step;
            s_face_v1[f_idx] = next_u * SEGS_V + v_step;
            s_face_v2[f_idx] = next_u * SEGS_V + next_v;
            s_face_v3[f_idx] = u_step * SEGS_V + next_v;
            f_idx++;
        }
    }

    s_mesh_initialized = true;
}

static inline void clear_dirty_rows(uint8_t *buf, int y0, int y1)
{
    if (y0 > y1) return;
    size_t offset = (size_t)y0 * ROW_PITCH_BYTES;
    size_t length = (size_t)(y1 - y0 + 1) * ROW_PITCH_BYTES;
    memset(buf + offset, 0xFF, length);
}

static inline void raster_edge(int x0, int y0, int x1, int y1)
{
    if (y0 == y1) return;
    if (y0 > y1) {
        int tx = x0; x0 = x1; x1 = tx;
        int ty = y0; y0 = y1; y1 = ty;
    }

    int dx = x1 - x0;
    int dy = y1 - y0;
    int step = (dx << 16) / dy;
    int curr = x0 << 16;

    for (int y = y0; y < y1; y++) {
        if (y >= 0 && y < BB_H) {
            int px = curr >> 16;
            if (px < s_edge_min[y]) s_edge_min[y] = px;
            if (px > s_edge_max[y]) s_edge_max[y] = px;
        }
        curr += step;
    }
}

static inline void fill_spans(int min_y, int max_y, uint8_t hi, uint8_t lo, uint8_t *buf)
{
    for (int y = min_y; y <= max_y; y++) {
        int xs = s_edge_min[y];
        int xe = s_edge_max[y];
        if (xs > xe) continue;
        if (xs < 0) xs = 0;
        if (xe >= BB_W) xe = BB_W - 1;

        uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (xs << 1);
        int cnt = xe - xs + 1;
        while (cnt--) {
            *p++ = hi;
            *p++ = lo;
        }
    }
}

static void raster_quad(int x0, int y0, int x1, int y1, int x2, int y2, int x3, int y3,
                        uint16_t color, int *out_min, int *out_max)
{
    int min_y = y0;
    if (y1 < min_y) min_y = y1;
    if (y2 < min_y) min_y = y2;
    if (y3 < min_y) min_y = y3;

    int max_y = y0;
    if (y1 > max_y) max_y = y1;
    if (y2 > max_y) max_y = y2;
    if (y3 > max_y) max_y = y3;

    if (min_y < 0) min_y = 0;
    if (max_y >= BB_H) max_y = BB_H - 1;

    if (min_y > max_y) {
        *out_min = BB_H;
        *out_max = -1;
        return;
    }

    for (int y = min_y; y <= max_y; y++) {
        s_edge_min[y] = 9999;
        s_edge_max[y] = -9999;
    }

    raster_edge(x0, y0, x1, y1);
    raster_edge(x1, y1, x2, y2);
    raster_edge(x2, y2, x3, y3);
    raster_edge(x3, y3, x0, y0);

    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);
    fill_spans(min_y, max_y, hi, lo, s_frame_buf);

    *out_min = min_y;
    *out_max = max_y;
}

static void render_core_sphere(int cx, int cy, int r_screen, uint8_t *buf, int *out_min, int *out_max)
{
    int r2 = r_screen * r_screen;
    float inv_r = 1.0f / (float)r_screen;

    int y_min = cy - r_screen;
    int y_max = cy + r_screen;
    if (y_min < 0) y_min = 0;
    if (y_max >= BB_H) y_max = BB_H - 1;

    for (int y = y_min; y <= y_max; y++) {
        int dy = y - cy;
        int dy2 = dy * dy;
        int span_w2 = r2 - dy2;

        if (span_w2 >= 0) {
            int half_w = (int)sqrtf((float)span_w2);
            int x0 = cx - half_w;
            int x1 = cx + half_w;
            if (x0 < 0) x0 = 0;
            if (x1 >= BB_W) x1 = BB_W - 1;

            float ny_base = -(float)dy * inv_r;
            float ny2 = ny_base * ny_base;
            float dot_k_y = ny_base * 0.57735f;
            float dot_f_y = ny_base * -0.8165f;
            float dot_h_y = ny_base * 0.3714f;

            uint8_t *p = buf + (y * ROW_PITCH_BYTES) + (x0 << 1);

            for (int x = x0; x <= x1; x++) {
                float dx = (float)(x - cx);
                float nx = dx * inv_r;
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

                    /* Deep Polished Chrome Gold Core */
                    float r = 0.20f + 0.95f * diff_k + 0.30f * diff_f + spec * 1.60f;
                    float g = 0.16f + 0.75f * diff_k + 0.22f * diff_f + spec * 1.60f;
                    float b = 0.05f + 0.20f * diff_k + 0.08f * diff_f + spec * 1.60f;

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
 * Execution Loop: gyro.start()
 * ------------------------------------------------------------------------- */
static mp_obj_t gyro_start(size_t n_args, const mp_obj_t *args)
{
    int max_frames = (n_args > 0) ? mp_obj_get_int(args[0]) : -1;

    init_torus_meshes();

    if (s_frame_buf == NULL) {
        s_frame_buf = (uint8_t *)heap_caps_malloc(BB_W * BB_H * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (s_frame_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("gyro: failed to allocate DMA frame buffer"));
        }
    }

    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(COLOR_BOOT);
    moclcd_fill_screen_internal(COLOR_WHITE);

    memset(s_frame_buf, 0xFF, BB_W * BB_H * 2);

    float rot1_x = 0.0f, rot1_y = 0.0f;
    float rot2_y = 0.0f, rot2_z = 0.0f;

    int prev_min_y = 0;
    int prev_max_y = BB_H - 1;

    int frame_count = 0;
    int fps_frame_count = 0;
    int64_t t_last_fps = esp_timer_get_time();
    char fps_str[36] = "FPS: -- | Capped to 72";

    moclcd_fill_rect_internal(10, 10, 160, 12, COLOR_WHITE);
    moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);

    /* Scratch arrays for transformation */
    float tv1_x[TOTAL_VERTS], tv1_y[TOTAL_VERTS], tv1_z[TOTAL_VERTS];
    int sv1_x[TOTAL_VERTS], sv1_y[TOTAL_VERTS];
    int sh1_x[TOTAL_VERTS], sh1_y[TOTAL_VERTS];

    float tv2_x[TOTAL_VERTS], tv2_y[TOTAL_VERTS], tv2_z[TOTAL_VERTS];
    int sv2_x[TOTAL_VERTS], sv2_y[TOTAL_VERTS];
    int sh2_x[TOTAL_VERTS], sh2_y[TOTAL_VERTS];

    float sort_keys[TOTAL_FACES * 2];
    int sort_idxs[TOTAL_FACES * 2];
    uint16_t face_cols[TOTAL_FACES * 2];

    const int core_scr_r = (int)(CORE_SPHERE_RADIUS * FOV_SCALE * (1.0f / CAMERA_Z));
    const int core_cx = CENTER_X - BB_X;
    const int core_cy = CENTER_Y - BB_Y;

    while (max_frames < 0 || frame_count < max_frames) {
        int64_t frame_start = esp_timer_get_time();

        /* MicroPython interrupt handler (Ctrl+C REPL break) */
        mp_handle_pending(true);

        clear_dirty_rows(s_frame_buf, prev_min_y, prev_max_y);

        /* 1. Outer Torus 3x3 Matrix */
        float cx1 = cosf(rot1_x), sx1 = sinf(rot1_x);
        float cy1 = cosf(rot1_y), sy1 = sinf(rot1_y);

        float r1_00 = cy1;
        float r1_01 = sx1 * sy1;
        float r1_02 = cx1 * sy1;

        float r1_10 = 0.0f;
        float r1_11 = cx1;
        float r1_12 = -sx1;

        float r1_20 = -sy1;
        float r1_21 = sx1 * cy1;
        float r1_22 = cx1 * cy1;

        /* 2. Inner Torus 3x3 Matrix */
        float cy2 = cosf(rot2_y), sy2 = sinf(rot2_y);
        float cz2 = cosf(rot2_z), sz2 = sinf(rot2_z);

        float r2_00 = cy2 * cz2;
        float r2_01 = -sz2;
        float r2_02 = sy2 * cz2;

        float r2_10 = cy2 * sz2;
        float r2_11 = cz2;
        float r2_12 = sy2 * sz2;

        float r2_20 = -sy2;
        float r2_21 = 0.0f;
        float r2_22 = cy2;

        int frame_min_y = BB_H;
        int frame_max_y = 0;

        /* Transform Outer Ring Vertices */
        for (int i = 0; i < TOTAL_VERTS; i++) {
            float u = s_torus1_u[i], v = s_torus1_v[i], w = s_torus1_w[i];
            float x = r1_00 * u + r1_01 * v + r1_02 * w;
            float y = r1_10 * u + r1_11 * v + r1_12 * w;
            float z = r1_20 * u + r1_21 * v + r1_22 * w;

            float t = (VIRTUAL_FLOOR_Y - y) * INV_LIGHT_DIR_Y;
            float sx_w = x + t * LIGHT_DIR_X;
            float sz_w = z + t * LIGHT_DIR_Z + CAMERA_Z;

            float inv_sz = 1.0f / sz_w;
            sh1_x[i] = (int)((CENTER_X + (sx_w * FOV_SCALE * inv_sz)) - BB_X);
            sh1_y[i] = (int)((CENTER_Y - (VIRTUAL_FLOOR_Y * FOV_SCALE * inv_sz)) - BB_Y);

            float cam_z = z + CAMERA_Z;
            tv1_x[i] = x;
            tv1_y[i] = y;
            tv1_z[i] = cam_z;

            float inv_z = 1.0f / cam_z;
            sv1_x[i] = (int)((CENTER_X + (x * FOV_SCALE * inv_z)) - BB_X);
            sv1_y[i] = (int)((CENTER_Y - (y * FOV_SCALE * inv_z)) - BB_Y);
        }

        /* Transform Inner Ring Vertices */
        for (int i = 0; i < TOTAL_VERTS; i++) {
            float u = s_torus2_u[i], v = s_torus2_v[i], w = s_torus2_w[i];
            float x = r2_00 * u + r2_01 * v + r2_02 * w;
            float y = r2_10 * u + r2_11 * v + r2_12 * w;
            float z = r2_20 * u + r2_21 * v + r2_22 * w;

            float t = (VIRTUAL_FLOOR_Y - y) * INV_LIGHT_DIR_Y;
            float sx_w = x + t * LIGHT_DIR_X;
            float sz_w = z + t * LIGHT_DIR_Z + CAMERA_Z;

            float inv_sz = 1.0f / sz_w;
            sh2_x[i] = (int)((CENTER_X + (sx_w * FOV_SCALE * inv_sz)) - BB_X);
            sh2_y[i] = (int)((CENTER_Y - (VIRTUAL_FLOOR_Y * FOV_SCALE * inv_sz)) - BB_Y);

            float cam_z = z + CAMERA_Z;
            tv2_x[i] = x;
            tv2_y[i] = y;
            tv2_z[i] = cam_z;

            float inv_z = 1.0f / cam_z;
            sv2_x[i] = (int)((CENTER_X + (x * FOV_SCALE * inv_z)) - BB_X);
            sv2_y[i] = (int)((CENTER_Y - (y * FOV_SCALE * inv_z)) - BB_Y);
        }

        /* 3. Cast Dual Hollow Shadow Rings onto Virtual Floor */
        for (int f = 0; f < TOTAL_FACES; f++) {
            int i0 = s_face_v0[f], i1 = s_face_v1[f], i2 = s_face_v2[f], i3 = s_face_v3[f];
            int s1_min, s1_max;
            raster_quad(sh1_x[i0], sh1_y[i0], sh1_x[i1], sh1_y[i1],
                        sh1_x[i2], sh1_y[i2], sh1_x[i3], sh1_y[i3],
                        0xAD55, &s1_min, &s1_max);
            if (s1_min < frame_min_y) frame_min_y = s1_min;
            if (s1_max > frame_max_y) frame_max_y = s1_max;

            int s2_min, s2_max;
            raster_quad(sh2_x[i0], sh2_y[i0], sh2_x[i1], sh2_y[i1],
                        sh2_x[i2], sh2_y[i2], sh2_x[i3], sh2_y[i3],
                        0x9492, &s2_min, &s2_max);
            if (s2_min < frame_min_y) frame_min_y = s2_min;
            if (s2_max > frame_max_y) frame_max_y = s2_max;
        }

        /* 4. Normals, Backface Culling, and Blinn-Phong Shading */
        int active_faces = 0;

        /* Outer Torus (Electric Cyan / Cobalt) */
        for (int f = 0; f < TOTAL_FACES; f++) {
            int i0 = s_face_v0[f], i1 = s_face_v1[f], i2 = s_face_v2[f];
            float x0 = tv1_x[i0], y0 = tv1_y[i0], z0 = tv1_z[i0];
            float e1x = tv1_x[i1] - x0, e1y = tv1_y[i1] - y0, e1z = tv1_z[i1] - z0;
            float e2x = tv1_x[i2] - x0, e2y = tv1_y[i2] - y0, e2z = tv1_z[i2] - z0;

            float nx = e1y * e2z - e1z * e2y;
            float ny = e1z * e2x - e1x * e2z;
            float nz = e1x * e2y - e1y * e2x;

            if ((nx * x0 + ny * y0 + nz * z0) < 0.0f) {
                float inv_l = 1.0f / sqrtf(nx * nx + ny * ny + nz * nz);
                nx *= inv_l; ny *= inv_l; nz *= inv_l;

                float dot_k = nx * 0.57735f + ny * 0.57735f - nz * 0.57735f;
                float diff_k = (dot_k > 0.0f) ? dot_k : 0.0f;
                float dot_f = nx * -0.4082f + ny * -0.8165f + nz * 0.4082f;
                float diff_f = (dot_f > 0.0f) ? dot_f : 0.0f;

                float dot_h = nx * 0.3714f + ny * 0.3714f - nz * 0.8510f;
                float spec = 0.0f;
                if (dot_h > 0.0f) {
                    float h2 = dot_h * dot_h;
                    float h4 = h2 * h2;
                    spec = h4 * h4;
                }

                float r = 0.08f + 0.35f * diff_k + 0.10f * diff_f + spec * 1.30f;
                float g = 0.35f + 0.85f * diff_k + 0.25f * diff_f + spec * 1.30f;
                float b = 0.80f + 1.10f * diff_k + 0.40f * diff_f + spec * 1.40f;

                if (r > 1.0f) r = 1.0f;
                if (g > 1.0f) g = 1.0f;
                if (b > 1.0f) b = 1.0f;

                uint16_t c = ((uint16_t)(r * 31.0f) << 11) |
                             ((uint16_t)(g * 63.0f) << 5)  |
                             ((uint16_t)(b * 31.0f));

                face_cols[active_faces] = c;
                sort_keys[active_faces] = z0 + tv1_z[i1] + tv1_z[i2] + tv1_z[s_face_v3[f]];
                sort_idxs[active_faces] = f;
                active_faces++;
            }
        }

        /* Inner Torus (Ruby Crimson) */
        for (int f = 0; f < TOTAL_FACES; f++) {
            int i0 = s_face_v0[f], i1 = s_face_v1[f], i2 = s_face_v2[f];
            float x0 = tv2_x[i0], y0 = tv2_y[i0], z0 = tv2_z[i0];
            float e1x = tv2_x[i1] - x0, e1y = tv2_y[i1] - y0, e1z = tv2_z[i1] - z0;
            float e2x = tv2_x[i2] - x0, e2y = tv2_y[i2] - y0, e2z = tv2_z[i2] - z0;

            float nx = e1y * e2z - e1z * e2y;
            float ny = e1z * e2x - e1x * e2z;
            float nz = e1x * e2y - e1y * e2x;

            if ((nx * x0 + ny * y0 + nz * z0) < 0.0f) {
                float inv_l = 1.0f / sqrtf(nx * nx + ny * ny + nz * nz);
                nx *= inv_l; ny *= inv_l; nz *= inv_l;

                float dot_k = nx * 0.57735f + ny * 0.57735f - nz * 0.57735f;
                float diff_k = (dot_k > 0.0f) ? dot_k : 0.0f;
                float dot_f = nx * -0.4082f + ny * -0.8165f + nz * 0.4082f;
                float diff_f = (dot_f > 0.0f) ? dot_f : 0.0f;

                float dot_h = nx * 0.3714f + ny * 0.3714f - nz * 0.8510f;
                float spec = 0.0f;
                if (dot_h > 0.0f) {
                    float h2 = dot_h * dot_h;
                    float h4 = h2 * h2;
                    spec = h4 * h4;
                }

                float r = 0.85f + 1.15f * diff_k + 0.30f * diff_f + spec * 1.40f;
                float g = 0.10f + 0.25f * diff_k + 0.10f * diff_f + spec * 1.30f;
                float b = 0.20f + 0.30f * diff_k + 0.15f * diff_f + spec * 1.30f;

                if (r > 1.0f) r = 1.0f;
                if (g > 1.0f) g = 1.0f;
                if (b > 1.0f) b = 1.0f;

                uint16_t c = ((uint16_t)(r * 31.0f) << 11) |
                             ((uint16_t)(g * 63.0f) << 5)  |
                             ((uint16_t)(b * 31.0f));

                face_cols[active_faces] = c;
                sort_keys[active_faces] = z0 + tv2_z[i1] + tv2_z[i2] + tv2_z[s_face_v3[f]];
                /* Flag MSB for inner ring */
                sort_idxs[active_faces] = f | 0x8000;
                active_faces++;
            }
        }

        /* Insertion Sort (Back-to-Front Depth) */
        for (int i = 1; i < active_faces; i++) {
            float k = sort_keys[i];
            int idx_v = sort_idxs[i];
            uint16_t col_v = face_cols[i];
            int j = i - 1;
            while (j >= 0 && sort_keys[j] < k) {
                sort_keys[j + 1] = sort_keys[j];
                sort_idxs[j + 1] = sort_idxs[j];
                face_cols[j + 1] = face_cols[j];
                j--;
            }
            sort_keys[j + 1] = k;
            sort_idxs[j + 1] = idx_v;
            face_cols[j + 1] = col_v;
        }

        /* 5. Interleaved Depth-Sorted Rasterization (Rings + Core Sphere) */
        bool core_rendered = false;

        for (int i = 0; i < active_faces; i++) {
            float avg_z = sort_keys[i] * 0.25f;

            if (!core_rendered && avg_z <= CAMERA_Z) {
                int sp_min, sp_max;
                render_core_sphere(core_cx, core_cy, core_scr_r, s_frame_buf, &sp_min, &sp_max);
                if (sp_min < frame_min_y) frame_min_y = sp_min;
                if (sp_max > frame_max_y) frame_max_y = sp_max;
                core_rendered = true;
            }

            int raw_idx = sort_idxs[i];
            uint16_t col = face_cols[i];
            int q_min, q_max;

            if (raw_idx & 0x8000) {
                int f = raw_idx & 0x7FFF;
                int i0 = s_face_v0[f], i1 = s_face_v1[f], i2 = s_face_v2[f], i3 = s_face_v3[f];
                raster_quad(sv2_x[i0], sv2_y[i0], sv2_x[i1], sv2_y[i1],
                            sv2_x[i2], sv2_y[i2], sv2_x[i3], sv2_y[i3],
                            col, &q_min, &q_max);
            } else {
                int f = raw_idx;
                int i0 = s_face_v0[f], i1 = s_face_v1[f], i2 = s_face_v2[f], i3 = s_face_v3[f];
                raster_quad(sv1_x[i0], sv1_y[i0], sv1_x[i1], sv1_y[i1],
                            sv1_x[i2], sv1_y[i2], sv1_x[i3], sv1_y[i3],
                            col, &q_min, &q_max);
            }

            if (q_min < frame_min_y) frame_min_y = q_min;
            if (q_max > frame_max_y) frame_max_y = q_max;
        }

        if (!core_rendered) {
            int sp_min, sp_max;
            render_core_sphere(core_cx, core_cy, core_scr_r, s_frame_buf, &sp_min, &sp_max);
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

        /* Advance gyroscopic rotation angles */
        rot1_x += 0.052f;
        rot1_y += 0.078f;
        rot2_y += 0.091f;
        rot2_z += 0.063f;

        /* Hardware 72 FPS limiter */
        int64_t elapsed_us = esp_timer_get_time() - frame_start;
        if (elapsed_us < TARGET_FRAME_TIME_US) {
            esp_rom_delay_us((uint32_t)(TARGET_FRAME_TIME_US - elapsed_us));
        }

        fps_frame_count++;
        if (fps_frame_count >= 15) {
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
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(gyro_start_obj, 0, 1, gyro_start);

/* -------------------------------------------------------------------------
 * MicroPython Module Registration
 * ------------------------------------------------------------------------- */
static const mp_rom_map_elem_t gyro_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_gyro) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&gyro_start_obj) },
};
static MP_DEFINE_CONST_DICT(gyro_module_globals, gyro_module_globals_table);

const mp_obj_module_t gyro_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&gyro_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_gyro, gyro_user_cmodule);
