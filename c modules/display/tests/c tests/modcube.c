/*
 * =====================================================================================
 *  FILE:         modcube.c
 *  MODULE:       cube (MicroPython native C module)
 *  TARGET:       ESP32-S3, ESP-IDF esp_lcd i80 (Intel 8080) parallel bus, 8-bit data
 *  PANEL:        ILI9488, 480 x 320
 *
 *  DESCRIPTION:
 *  - Native C perspective rasterizer with directional lighting and floor shadow.
 *  - Direct hardware init using moclcd v1.5.0 STABLE sequence.
 *  - Exposes:
 *      cube.start() -> Launches the real-time 3D animation loop with corner FPS display.
 * =====================================================================================
 */

#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mphal.h"

#include "driver/gpio.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_types.h"
#include "esp_timer.h"

#define WIDTH              480
#define HEIGHT             320
#define CX                 240
#define CY                 150
#define FOV                240.0f
#define CAM_Z              3.8f

#define BB_W               300
#define BB_H               300
#define BB_X               (CX - 150)  /* 90 */
#define BB_Y               (CY - 150)  /* 0  */
#define ROW_PITCH_BYTES    (BB_W * 2)  /* 600 */

#define LCD_PIN_NUM_D0     16
#define LCD_PIN_NUM_D1     15
#define LCD_PIN_NUM_D2     11
#define LCD_PIN_NUM_D3     10
#define LCD_PIN_NUM_D4     9
#define LCD_PIN_NUM_D5     4
#define LCD_PIN_NUM_D6     18
#define LCD_PIN_NUM_D7     17
#define LCD_PIN_NUM_DC     13
#define LCD_PIN_NUM_WR     14
#define LCD_PIN_NUM_RD     41
#define LCD_PIN_NUM_RST    12
#define LCD_PIN_NUM_BL     38
#define LCD_PIN_NUM_CS     (-1)

#define LCD_CMD_CASET      0x2A
#define LCD_CMD_PASET      0x2B
#define LCD_CMD_RAMWR      0x2C
#define LCD_CMD_RAMWRC     0x3C

static esp_lcd_i80_bus_handle_t  s_bus = NULL;
static esp_lcd_panel_io_handle_t s_io  = NULL;

/* Framebuffer for dirty-band blits (300 x 300 x 2 = 180,000 bytes) */
static uint8_t s_frame_buf[BB_W * BB_H * 2];

/* Font 5x7 for corner FPS */
#define FONT_FIRST 32
#define FONT_LAST  126
static const uint8_t k_font5x7[][5] = {
    {0x00,0x00,0x00,0x00,0x00}, {0x00,0x00,0x5F,0x00,0x00}, {0x00,0x07,0x00,0x07,0x00},
    {0x14,0x7F,0x14,0x7F,0x14}, {0x24,0x2A,0x7F,0x2A,0x12}, {0x23,0x13,0x08,0x64,0x62},
    {0x36,0x49,0x56,0x20,0x50}, {0x00,0x08,0x07,0x03,0x00}, {0x00,0x1C,0x22,0x41,0x00},
    {0x00,0x41,0x22,0Here is the complete C implementation of the 3D rotating cube engine packaged as a native MicroPython module `cube` (`modcube.c`). It operates directly on top of your working `modlcd.c` driver and `esp_lcd` hardware state, executing the entire mathematical transform, Blinn-Phong lighting, virtual floor shadow projection, span rasterization, and DMA blitting at native C speeds.

### C Source File: `modcube.c`

```c
/*
 * =====================================================================================
 *  FILE:         modcube.c
 *  MODULE:       cube (MicroPython native C module)
 *  TARGET:       ESP32-S3, ILI9488 8-bit Parallel i80 (via moclcd driver)
 *  DESCRIPTION:  Native 3D Perspective Rasterizer Engine with Blinn-Phong Shading,
 *                Virtual Floor Shadow Projection, Dynamic Bounding-Box DMA Blit,
 *                and Corner FPS Overlay.
 *
 *  USAGE:
 *      import cube
 *      cube.start()          # Runs infinite real-time rotating cube demo
 *      cube.start(200)       # Runs exactly 200 frames and returns
 * =====================================================================================
 */

#include <math.h>
#include <string.h>
#include <stdlib.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mphal.h"

#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"

/* -------------------------------------------------------------------------
 * External LCD Driver Linkage (from modlcd.c)
 * ------------------------------------------------------------------------- */
extern void moclcd_init_internal(void);
extern void moclcd_panel_init_internal(void);
extern void moclcd_backlight_internal(bool on);
extern void moclcd_fill_screen_internal(uint16_t color);
extern void moclcd_fill_rect_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
extern void moclcd_blit_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const void *buf);
extern void moclcd_draw_text_internal(uint16_t x, uint16_t y, const char *str, uint16_t fg, uint16_t bg);

/* -------------------------------------------------------------------------
 * Screen & Projection Dimensions
 * ------------------------------------------------------------------------- */
#define SCREEN_W     480
#define SCREEN_H     320
#define CENTER_X     240
#define CENTER_Y     150
#define FOV_SCALE    240.0f
#define CAMERA_Z     3.8f

#define BB_W         300
#define BB_H         300
#define BB_X         (CENTER_X - (BB_W / 2))  /* 90 */
#define BB_Y         (CENTER_Y - (BB_H / 2))  /* 0 */
#define ROW_PITCH    (BB_W * 2)               /* 600 bytes */

#define COLOR_WHITE  0xFFFF
#define COLOR_BLACK  0x0000
#define COLOR_BOOT   0xF800
#define COLOR_SHADOW 0x8410

/* -------------------------------------------------------------------------
 * 3D Model & Lighting Constants
 * ------------------------------------------------------------------------- */
#define VIRTUAL_FLOOR_Y (-1.45f)
#define LIGHT_DIR_Y     (-1.75f)
#define INV_LIGHT_DIR_Y (1.0f / LIGHT_DIR_Y)
#define LIGHT_DIR_X     (-0.25f)
#define LIGHT_DIR_Z     (-0.20f)

static const float KEY_LIGHT[3]   = { 0.57735f,  0.57735f, -0.57735f };
static const float FILL_LIGHT[3]  = {-0.40820f, -0.81650f,  0.40820f };
static const float HALF_VECTOR[3] = { 0.37140f,  0.37140f, -0.85100f };

static const float CUBE_VERTS[8][3] = {
    {-0.75f, -0.75f, -0.75f},
    { 0.75f, -0.75f, -0.75f},
    { 0.75f,  0.75f, -0.75f},
    {-0.75f,  0.75f, -0.75f},
    {-0.75f, -0.75f,  0.75f},
    { 0.75f, -0.75f,  0.75f},
    { 0.75f,  0.75f,  0.75f},
    {-0.75f,  0.75f,  0.75f}
};

static const int CUBE_FACES[6][4] = {
    {0, 1, 2, 3},  /* Back   */
    {5, 4, 7, 6},  /* Front  */
    {4, 0, 3, 7},  /* Left   */
    {1, 5, 6, 2},  /* Right  */
    {3, 2, 6, 7},  /* Top    */
    {4, 5, 1, 0}   /* Bottom */
};

/* -------------------------------------------------------------------------
 * Preallocated Buffers (DMA & Cache-aligned in SRAM)
 * ------------------------------------------------------------------------- */
static uint8_t *s_frame_buf = NULL;
static int s_edge_min[BB_H];
static int s_edge_max[BB_H];

typedef struct {
    int x, y;
} point2d_t;

static inline void clear_dirty_rows(uint8_t *buf, int y0, int y1)
{
    if (y0 > y1) return;
    size_t offset = (size_t)y0 * ROW_PITCH;
    size_t length = (size_t)(y1 - y0 + 1) * ROW_PITCH;
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

        uint8_t *p = buf + (y * ROW_PITCH) + (xs << 1);
        int cnt = xe - xs + 1;
        while (cnt--) {
            *p++ = hi;
            *p++ = lo;
        }
    }
}

static void raster_quad(const point2d_t *pts, uint16_t color, int *out_min_y, int *out_max_y)
{
    int min_y = BB_H;
    int max_y = -1;

    for (int i = 0; i < 4; i++) {
        if (pts[i].y < min_y) min_y = pts[i].y;
        if (pts[i].y > max_y) max_y = pts[i].y;
    }

    if (min_y < 0) min_y = 0;
    if (max_y >= BB_H) max_y = BB_H - 1;
    if (min_y > max_y) {
        *out_min_y = BB_H;
        *out_max_y = -1;
        return;
    }

    for (int y = min_y; y <= max_y; y++) {
        s_edge_min[y] = 9999;
        s_edge_max[y] = -9999;
    }

    for (int i = 0; i < 4; i++) {
        int next = (i + 1) & 3;
        raster_edge(pts[i].x, pts[i].y, pts[next].x, pts[next].y);
    }

    uint8_t hi = (uint8_t)(color >> 8);
    uint8_t lo = (uint8_t)(color & 0xFF);
    fill_spans(min_y, max_y, hi, lo, s_frame_buf);

    *out_min_y = min_y;
    *out_max_y = max_y;
}

/* -------------------------------------------------------------------------
 * Cube Engine Main Execution Loop
 * ------------------------------------------------------------------------- */
static mp_obj_t cube_start(size_t n_args, const mp_obj_t *args)
{
    int max_frames = (n_args > 0) ? mp_obj_get_int(args[0]) : -1;

    if (s_frame_buf == NULL) {
        s_frame_buf = (uint8_t *)heap_caps_malloc(BB_W * BB_H * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (s_frame_buf == NULL) {
            mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("cube: failed to allocate DMA frame buffer"));
        }
    }

    /* Standardized Hardware Boot Sequence */
    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(COLOR_BOOT);
    moclcd_fill_screen_internal(COLOR_WHITE);

    memset(s_frame_buf, 0xFF, BB_W * BB_H * 2);

    float ax = 0.0f, ay = 0.0f, az = 0.0f;
    int prev_min_y = 0;
    int prev_max_y = BB_H - 1;

    int frame_count = 0;
    int fps_frame_count = 0;
    int64_t t_last_fps = esp_timer_get_time();
    char fps_str[16] = "FPS: --";

    moclcd_fill_rect_internal(10, 10, 75, 12, COLOR_WHITE);
    moclcd_draw_text_internal(10, 10, fps_str, COLOR_BLACK, COLOR_WHITE);

    while (max_frames < 0 || frame_count < max_frames) {
        clear_dirty_rows(s_frame_buf, prev_min_y, prev_max_y);

        /* 3x3 Combined Rotation Matrix */
        float cx = cosf(ax), sx = sinf(ax);
        float cy = cosf(ay), sy = sinf(ay);
        float cz = cosf(az), sz = sinf(az);

        float r00 = cy * cz;
        float r01 = sx * sy * cz - cx * sz;
        float r02 = cx * sy * cz + sx * sz;

        float r10 = cy * sz;
        float r11 = sx * sy * sz + cx * cz;
        float r12 = cx * sy * sz - sx * cz;

        float r20 = -sy;
        float r21 = sx * cy;
        float r22 = cx * cy;

        int frame_min_y = BB_H;
        int frame_max_y = 0;

        float tv_x[8], tv_y[8], tv_z[8];
        point2d_t sv[8];
        point2d_t shad[8];

        /* Vertex Transformation and Dual Perspective Projections */
        for (int i = 0; i < 8; i++) {
            float vx = CUBE_VERTS[i][0];
            float vy = CUBE_VERTS[i][1];
            float vz = CUBE_VERTS[i][2];

            float x3 = r00 * vx + r01 * vy + r02 * vz;
            float y3 = r10 * vx + r11 * vy + r12 * vz;
            float z3 = r20 * vx + r21 * vy + r22 * vz;

            /* Virtual Floor Ray Intersection */
            float t = (VIRTUAL_FLOOR_Y - y3) * INV_LIGHT_DIR_Y;
            float sx_world = x3 + t * LIGHT_DIR_X;
            float sz_world = z3 + t * LIGHT_DIR_Z + CAMERA_Z;

            float inv_sz = 1.0f / sz_world;
            shad[i].x = (int)((CENTER_X + (sx_world * FOV_SCALE * inv_sz)) - BB_X);
            shad[i].y = (int)((CENTER_Y - (VIRTUAL_FLOOR_Y * FOV_SCALE * inv_sz)) - BB_Y);

            float cam_z = z3 + CAMERA_Z;
            tv_x[i] = x3;
            tv_y[i] = y3;
            tv_z[i] = cam_z;

            float inv_z = 1.0f / cam_z;
            sv[i].x = (int)((CENTER_X + (x3 * FOV_SCALE * inv_z)) - BB_X);
            sv[i].y = (int)((CENTER_Y - (y3 * FOV_SCALE * inv_z)) - BB_Y);
        }

        /* 1. Cast Floor Shadows */
        for (int f = 0; f < 6; f++) {
            point2d_t s_pts[4] = {
                shad[CUBE_FACES[f][0]],
                shad[CUBE_FACES[f][1]],
                shad[CUBE_FACES[f][2]],
                shad[CUBE_FACES[f][3]]
            };
            int s_min, s_max;
            raster_quad(s_pts, COLOR_SHADOW, &s_min, &s_max);
            if (s_min < frame_min_y) frame_min_y = s_min;
            if (s_max > frame_max_y) frame_max_y = s_max;
        }

        /* 2. Face Culling, Lighting & Depth Sorting */
        float sort_keys[6];
        int sort_idxs[6];
        uint16_t face_colors[6];
        int active_count = 0;

        for (int f = 0; f < 6; f++) {
            int i0 = CUBE_FACES[f][0];
            int i1 = CUBE_FACES[f][1];
            int i2 = CUBE_FACES[f][2];
            int i3 = CUBE_FACES[f][3];

            float x0 = tv_x[i0], y0 = tv_y[i0], z0 = tv_z[i0];
            float e1x = tv_x[i1] - x0, e1y = tv_y[i1] - y0, e1z = tv_z[i1] - z0;
            float e2x = tv_x[i2] - x0, e2y = tv_y[i2] - y0, e2z = tv_z[i2] - z0;

            float nx = e1y * e2z - e1z * e2y;
            float ny = e1z * e2x - e1x * e2z;
            float nz = e1x * e2y - e1y * e2x;

            if ((nx * x0 + ny * y0 + nz * z0) < 0.0f) {
                float inv_len = 1.0f / sqrtf(nx * nx + ny * ny + nz * nz);
                nx *= inv_len;
                ny *= inv_len;
                nz *= inv_len;

                float dot_k = nx * KEY_LIGHT[0] + ny * KEY_LIGHT[1] + nz * KEY_LIGHT[2];
                float diff_k = (dot_k > 0.0f) ? dot_k : 0.0f;

                float dot_f = nx * FILL_LIGHT[0] + ny * FILL_LIGHT[1] + nz * FILL_LIGHT[2];
                float diff_f = (dot_f > 0.0f) ? dot_f : 0.0f;

                float dot_h = nx * HALF_VECTOR[0] + ny * HALF_VECTOR[1] + nz * HALF_VECTOR[2];
                float spec = 0.0f;
                if (dot_h > 0.0f) {
                    float h2 = dot_h * dot_h;
                    float h4 = h2 * h2;
                    spec = h4 * h4;
                }

                float avg_z = z0 + tv_z[i1] + tv_z[i2] + tv_z[i3];
                float depth = 1.25f - (avg_z * 0.14f);
                if (depth < 0.60f) depth = 0.60f;

                float r = (0.10f + 0.35f * diff_k + 0.10f * diff_f + spec * 1.25f) * depth;
                float g = (0.35f + 1.05f * diff_k + 0.25f * diff_f + spec * 1.25f) * depth;
                float b = (0.70f + 1.25f * diff_k + 0.50f * diff_f + spec * 1.30f) * depth;

                if (r > 1.0f) r = 1.0f;
                if (g > 1.0f) g = 1.0f;
                if b > 1.0f; b = 1.0f;

                uint16_t c = ((uint16_t)(r * 31.0f) << 11) |
                             ((uint16_t)(g * 63.0f) << 5)  |
                             ((uint16_t)(b * 31.0f));

                face_colors[f] = c;
                sort_keys[active_count] = avg_z;
                sort_idxs[active_count] = f;
                active_count++;
            }
        }

        /* In-Place Insertion Sort */
        for (int i = 1; i < active_count; i++) {
            float k = sort_keys[i];
            int idx_val = sort_idxs[i];
            int j = i - 1;
            while (j >= 0 && sort_keys[j] < k) {
                sort_keys[j + 1] = sort_keys[j];
                sort_idxs[j + 1] = sort_idxs[j];
                j--;
            }
            sort_keys[j + 1] = k;
            sort_idxs[j + 1] = idx_val;
        }

        /* 3. Rasterize Visible Faces Over Shadow */
        for (int i = 0; i < active_count; i++) {
            int f = sort_idxs[i];
            point2d_t q_pts[4] = {
                sv[CUBE_FACES[f][0]],
                sv[CUBE_FACES[f][1]],
                sv[CUBE_FACES[f][2]],
                sv[CUBE_FACES[f][3]]
            };
            int q_min, q_max;
            raster_quad(q_pts, face_colors[f], &q_min, &q_max);
            if (q_min < frame_min_y) frame_min_y = q_min;
            if (q_max > frame_max_y) frame_max_y = q_max;
        }

        if (frame_min_y < 0) frame_min_y = 0;
        if (frame_max_y >= BB_H) frame_max_y = BB_H - 1;

        int blit_top = (frame_min_y < prev_min_y) ? frame_min_y : prev_min_y;
        int blit_bottom = (frame_max_y > prev_max_y) ? frame_max_y : prev_max_y;
        int blit_h = blit_bottom - blit_top + 1;

        size_t start_offset = (size_t)blit_top * ROW_PITCH;
        moclcd_blit_internal(BB_X, BB_Y + blit_top, BB_W, blit_h, s_frame_buf + start_offset);

        prev_min_y = frame_min_y;
        prev_max_y = frame_max_y;

        ax += 0.08f;
        ay += 0.12f;
        az += 0.05f;

        /* 4. Top-Left FPS Readout */
        fps_frame_count++;
        if (fps_frame_count >= 15) {
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
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(cube_start_obj, 0, 1, cube_start);

/* -------------------------------------------------------------------------
 * MicroPython Module Registration
 * ------------------------------------------------------------------------- */
static const mp_rom_map_elem_t cube_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_cube) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&cube_start_obj) },
};
static MP_DEFINE_CONST_DICT(cube_module_globals, cube_module_globals_table);

const mp_obj_module_t cube_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&cube_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_cube, cube_user_cmodule);
