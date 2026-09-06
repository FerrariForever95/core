// =====================================================================================
//  FILE:         modsaturn.c
//  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 Bus via DMA
//  DESCRIPTION:  Complete MicroPython Native C Module for Analytical Saturn
//                with Precession Wobble & Random Twinkling Starfield.
//                - Zero-heap allocation render loop in internal DMA SRAM
//                - Direct DMA window blits via moclcd bindings
//                - Exposes Python API: saturn.start(fps=60), saturn.stop()
// =====================================================================================

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mphal.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

// External hardware hooks provided by native moclcd driver
extern void moclcd_init(void);
extern void moclcd_panel_init(void);
extern void moclcd_backlight(uint8_t state);
extern void moclcd_fill_screen(uint16_t color);
extern void moclcd_blit(int16_t x, int16_t y, int16_t w, int16_t h, const uint8_t *buf);

#define LCD_WIDTH       480
#define LCD_HEIGHT      320
#define CX              240
#define CY              150

#define BB_W            380
#define BB_H            300
#define BB_X            (CX - (BB_W / 2))  // 50
#define BB_Y            10
#define ROW_PITCH       (BB_W * 2)         // 760 bytes

#define SPHERE_R        66
#define SPHERE_R2       (SPHERE_R * SPHERE_R)

// Ring Boundaries (Equatorial Plane Radius Squared)
// B-Ring: 86 to 134 -> 7396 to 17956
// Cassini Gap: 134 to 143
// A-Ring: 143 to 176 -> 20449 to 30976
#define RING_B_IN2      7396.0f
#define RING_B_OUT2     17956.0f
#define RING_A_IN2      20449.0f
#define RING_A_OUT2     30976.0f

#define NUM_STARS       128

typedef struct {
    int16_t x;
    int16_t y;
    uint8_t tier;
    float phase;
    float speed;
} star_t;

static star_t s_stars[NUM_STARS];
static uint8_t *s_frame_buf = NULL;
static TaskHandle_t s_saturn_task_handle = NULL;
static volatile bool s_running = false;
static uint32_t s_target_delay_ms = 16;

static inline uint16_t rgb565(float r, float g, float b) {
    if (r < 0.0f) r = 0.0f; else if (r > 1.0f) r = 1.0f;
    if (g < 0.0f) g = 0.0f; else if (g > 1.0f) g = 1.0f;
    if (b < 0.0f) b = 0.0f; else if (b > 1.0f) b = 1.0f;

    uint16_t r_int = (uint16_t)(r * 31.0f);
    uint16_t g_int = (uint16_t)(g * 63.0f);
    uint16_t b_int = (uint16_t)(b * 31.0f);
    return (r_int << 11) | (g_int << 5) | b_int;
}

static uint32_t s_rng_seed = 0x1337BEEF;
static inline uint32_t lcg_rand(void) {
    s_rng_seed = (s_rng_seed * 1664525u + 1013904223u);
    return s_rng_seed;
}

static void init_starfield(void) {
    s_rng_seed = 0x1337BEEF;
    for (int i = 0; i < NUM_STARS; ++i) {
        s_stars[i].x = (int16_t)((lcg_rand() % (BB_W - 8)) + 4);
        s_stars[i].y = (int16_t)((lcg_rand() % (BB_H - 8)) + 4);
        s_stars[i].tier = (uint8_t)(i % 4);
        s_stars[i].phase = (float)(lcg_rand() % 628) * 0.01f;
        s_stars[i].speed = 0.08f + (float)(lcg_rand() % 100) * 0.001f;
    }
}

static inline void clear_dirty_rows(int y0, int y1) {
    if (y0 < 0) y0 = 0;
    if (y1 >= BB_H) y1 = BB_H - 1;
    if (y0 > y1) return;

    size_t offset = (size_t)y0 * ROW_PITCH;
    size_t length = (size_t)(y1 - y0 + 1) * ROW_PITCH;
    memset(&s_frame_buf[offset], 0x00, length);
}

static inline void render_twinkling_stars(float t) {
    for (int i = 0; i < NUM_STARS; ++i) {
        float tw = sinf(t * s_stars[i].speed + s_stars[i].phase);
        uint16_t col;

        if (s_stars[i].tier == 0) {
            col = (tw > -0.2f) ? 0xFFFF : 0xCE79;
        } else if (s_stars[i].tier == 1) {
            col = (tw > 0.0f) ? 0x9E7F : 0x52AA;
        } else if (s_stars[i].tier == 2) {
            col = (tw > 0.2f) ? 0x8410 : 0x4208;
        } else {
            col = (tw > 0.4f) ? 0x5ACB : 0x2104;
        }

        size_t off = ((size_t)s_stars[i].y * ROW_PITCH) + ((size_t)s_stars[i].x << 1);
        s_frame_buf[off]     = (uint8_t)(col >> 8);
        s_frame_buf[off + 1] = (uint8_t)(col & 0xFF);
    }
}

static void render_rings_arc(int is_front, float tilt_y, float roll_x) {
    const int pcx = 190;
    const int pcy = 150;

    const float inv_tilt = 1.0f / tilt_y;
    const int max_y_extent = (int)(176.0f * tilt_y) + 4;
    int y_start = is_front ? pcy : (pcy - max_y_extent);
    int y_end   = is_front ? (pcy + max_y_extent + 1) : pcy;

    if (y_start < 0) y_start = 0;
    if (y_end > BB_H) y_end = BB_H;

    const float cos_roll = cosf(roll_x);
    const float sin_roll = sinf(roll_x);

    for (int y = y_start; y < y_end; ++y) {
        float dy = (float)(y - pcy);
        uint8_t *line_ptr = &s_frame_buf[y * ROW_PITCH];

        for (int x = 0; x < BB_W; ++x) {
            float dx = (float)(x - pcx);

            float rx = dx * cos_roll - dy * sin_roll;
            float ry = (dx * sin_roll + dy * cos_roll) * inv_tilt;
            float r_plane2 = rx * rx + ry * ry;

            float ring_base = 0.0f;
            if (r_plane2 >= RING_B_IN2 && r_plane2 <= RING_B_OUT2) {
                ring_base = 1.00f;
            } else if (r_plane2 >= RING_A_IN2 && r_plane2 <= RING_A_OUT2) {
                ring_base = 0.80f;
            }

            if (ring_base > 0.0f) {
                size_t offset = (size_t)x << 1;

                if (!is_front && (dx * dx + dy * dy < 4356.0f)) {
                    line_ptr[offset]     = 0x08;
                    line_ptr[offset + 1] = 0x41;
                } else {
                    float sun_term = (rx * 0.0058f * 0.62f + 0.72f);
                    float sun_factor = 0.35f + (sun_term > 0.0f ? sun_term : 0.0f) * 1.80f;

                    uint16_t col = rgb565(
                        ring_base * sun_factor,
                        ring_base * 0.90f * sun_factor,
                        ring_base * 0.60f * sun_factor
                    );
                    line_ptr[offset]     = (uint8_t)(col >> 8);
                    line_ptr[offset + 1] = (uint8_t)(col & 0xFF);
                }
            }
        }
    }
}

static void render_smooth_saturn_sphere(float tilt_y, float roll_x) {
    const int pcx = 190;
    const int pcy = 150;
    const float inv_sr = 1.0f / (float)SPHERE_R;

    int y_min = pcy - SPHERE_R;
    int y_max = pcy + SPHERE_R;
    if (y_min < 0) y_min = 0;
    if (y_max >= BB_H) y_max = BB_H - 1;

    const float cos_roll = cosf(roll_x);
    const float sin_roll = sinf(roll_x);

    for (int y = y_min; y <= y_max; ++y) {
        int dy = y - pcy;
        int dy2 = dy * dy;
        int span_w2 = SPHERE_R2 - dy2;

        if (span_w2 >= 0) {
            int hw = (int)sqrtf((float)span_w2);
            int x0 = pcx - hw;
            int x1 = pcx + hw;
            if (x0 < 0) x0 = 0;
            if (x1 >= BB_W) x1 = BB_W - 1;

            float ny = -(float)dy * inv_sr;
            float ny2 = ny * ny;
            float l_dot_y = ny * 0.62f;
            float h_dot_y = ny * 0.52f;

            uint8_t *line_ptr = &s_frame_buf[y * ROW_PITCH];

            for (int x = x0; x <= x1; ++x) {
                float dx = (float)(x - pcx);
                float nx = dx * inv_sr;
                float nz2 = 1.0f - (nx * nx + ny2);

                if (nz2 > 0.0f) {
                    float nz = sqrtf(nz2);

                    float lat_y = dx * sin_roll + (float)dy * cos_roll;
                    float lat = fabsf(lat_y * inv_sr);

                    float base_r, base_g, base_b;
                    if (lat < 0.28f) {
                        base_r = 1.00f; base_g = 0.88f; base_b = 0.58f;
                    } else if (lat < 0.68f) {
                        base_r = 0.90f; base_g = 0.74f; base_b = 0.44f;
                    } else {
                        base_r = 0.70f; base_g = 0.60f; base_b = 0.38f;
                    }

                    float dot_s = nx * 0.62f + l_dot_y - nz * (-0.48f);
                    float diff = (dot_s > 0.0f) ? (dot_s * 1.85f) : 0.0f;

                    float dot_h = nx * 0.52f + h_dot_y - nz * (-0.68f);
                    float spec = 0.0f;
                    if (dot_h > 0.0f && diff > 0.10f) {
                        spec = powf(dot_h, 14.0f) * 1.60f;
                    }

                    float amb = 0.12f;
                    uint16_t col = rgb565(
                        (amb + diff) * base_r + spec,
                        (amb + diff) * base_g + spec,
                        (amb + diff) * base_b + spec
                    );

                    size_t offset = (size_t)x << 1;
                    line_ptr[offset]     = (uint8_t)(col >> 8);
                    line_ptr[offset + 1] = (uint8_t)(col & 0xFF);
                }
            }
        }
    }
}

static void saturn_render_task(void *pvParameters) {
    if (!s_frame_buf) {
        s_frame_buf = (uint8_t *)heap_caps_malloc(BB_W * BB_H * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_frame_buf) {
            s_running = false;
            vTaskDelete(NULL);
            return;
        }
    }
    memset(s_frame_buf, 0x00, BB_W * BB_H * 2);

    moclcd_init();
    moclcd_panel_init();
    moclcd_backlight(1);
    moclcd_fill_screen(0x0000);

    init_starfield();

    float time_phase = 0.0f;
    int prev_min_y = 0;
    int prev_max_y = BB_H - 1;

    while (s_running) {
        int64_t t_start = esp_timer_get_time();

        time_phase += 0.045f;
        float tilt_val = 0.38f + sinf(time_phase) * 0.10f;
        float roll_val = cosf(time_phase) * 0.18f;

        clear_dirty_rows(prev_min_y, prev_max_y);
        render_twinkling_stars(time_phase);

        render_rings_arc(0, tilt_val, roll_val);
        render_smooth_saturn_sphere(tilt_val, roll_val);
        render_rings_arc(1, tilt_val, roll_val);

        int extent = (int)(176.0f * tilt_val) + 6;
        int f_min = 150 - extent;
        int f_max = 150 + extent;
        if (f_min < 0) f_min = 0;
        if (f_max >= BB_H) f_max = BB_H - 1;

        int blit_top = (f_min < prev_min_y) ? f_min : prev_min_y;
        int blit_bottom = (f_max > prev_max_y) ? f_max : prev_max_y;
        if (blit_top < 0) blit_top = 0;
        if (blit_bottom >= BB_H) blit_bottom = BB_H - 1;
        int blit_h = blit_bottom - blit_top + 1;

        size_t start_offset = (size_t)blit_top * ROW_PITCH;
        moclcd_blit(BB_X, BB_Y + blit_top, BB_W, blit_h, &s_frame_buf[start_offset]);

        prev_min_y = f_min;
        prev_max_y = f_max;

        int64_t elapsed_ms = (esp_timer_get_time() - t_start) / 1000;
        if (elapsed_ms < s_target_delay_ms) {
            vTaskDelay(pdMS_TO_TICKS(s_target_delay_ms - elapsed_ms));
        } else {
            vTaskDelay(1);
        }
    }

    if (s_frame_buf) {
        heap_caps_free(s_frame_buf);
        s_frame_buf = NULL;
    }
    s_saturn_task_handle = NULL;
    vTaskDelete(NULL);
}

// -------------------------------------------------------------------------
// MicroPython C-Module Interface
// -------------------------------------------------------------------------

// saturn.start([fps])
STATIC mp_obj_t mod_saturn_start(size_t n_args, const mp_obj_t *args) {
    if (s_running) {
        return mp_const_none;
    }

    uint32_t target_fps = 60;
    if (n_args > 0) {
        target_fps = (uint32_t)mp_obj_get_int(args[0]);
        if (target_fps < 1) target_fps = 1;
        if (target_fps > 120) target_fps = 120;
    }
    s_target_delay_ms = 1000 / target_fps;
    s_running = true;

    // Pin task directly to Core 1 to avoid contending with MicroPython on Core 0
    BaseType_t res = xTaskCreatePinnedToCore(
        saturn_render_task,
        "saturn_task",
        8192,
        NULL,
        5,
        &s_saturn_task_handle,
        1
    );

    if (res != pdPASS) {
        s_running = false;
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("Failed to create saturn worker task"));
    }

    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_saturn_start_obj, 0, 1, mod_saturn_start);

// saturn.stop()
STATIC mp_obj_t mod_saturn_stop(void) {
    if (s_running) {
        s_running = false;
        while (s_saturn_task_handle != NULL) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(mod_saturn_stop_obj, mod_saturn_stop);

// Module globals dictionary
STATIC const mp_rom_map_elem_t saturn_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_saturn) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&mod_saturn_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),     MP_ROM_PTR(&mod_saturn_stop_obj) },
};
STATIC MP_DEFINE_CONST_DICT(saturn_module_globals, saturn_module_globals_table);

// Module definition
const mp_obj_module_t saturn_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&saturn_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_saturn, saturn_user_cmodule);
