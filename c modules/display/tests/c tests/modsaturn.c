// =====================================================================================
//  FILE:         modsaturn.c
//  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 Bus via DMA
//  DESCRIPTION:  Synchronous Native C Module for Analytical Saturn with Precession Wobble
//                - Uses 32-line streaming DMA chunks (Zero heap fragmentation / No MemoryError)
//                - Exposes Python API: saturn.start(fps=60), press Ctrl+C to exit
// =====================================================================================

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/mphal.h"

#include "esp_timer.h"
#include "esp_heap_caps.h"

// External hardware hooks pointing to original moclcd internal drivers
extern void moclcd_init_internal(void);
extern void moclcd_panel_init_internal(void);
extern void moclcd_backlight_internal(bool on);
extern void moclcd_fill_screen_internal(uint16_t color);
extern void moclcd_blit_internal(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const void *buf);

#define BB_W            380
#define BB_H            300
#define BB_X            (240 - (BB_W / 2))
#define BB_Y            10

#define CHUNK_H         32
#define CHUNK_ROWS      (BB_H / CHUNK_H) // 9 chunks total
#define CHUNK_SIZE      (BB_W * CHUNK_H * 2) // ~24 KB per chunk (easily fits internal DMA SRAM)

#define SPHERE_R        66
#define SPHERE_R2       (SPHERE_R * SPHERE_R)

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

static inline void render_stars_chunk(uint8_t *chunk_buf, int y_start, int y_end, float t) {
    int pitch = BB_W * 2;
    for (int i = 0; i < NUM_STARS; ++i) {
        if (s_stars[i].y >= y_start && s_stars[i].y < y_end) {
            float tw = sinf(t * s_stars[i].speed + s_stars[i].phase);
            uint16_t col;

            if (s_stars[i].tier == 0) col = (tw > -0.2f) ? 0xFFFF : 0xCE79;
            else if (s_stars[i].tier == 1) col = (tw > 0.0f) ? 0x9E7F : 0x52AA;
            else if (s_stars[i].tier == 2) col = (tw > 0.2f) ? 0x8410 : 0x4208;
            else col = (tw > 0.4f) ? 0x5ACB : 0x2104;

            int local_y = s_stars[i].y - y_start;
            size_t off = (size_t)local_y * pitch + ((size_t)s_stars[i].x << 1);
            chunk_buf[off]     = (uint8_t)(col >> 8);
            chunk_buf[off + 1] = (uint8_t)(col & 0xFF);
        }
    }
}

static void render_rings_arc_chunk(uint8_t *chunk_buf, int y_start, int y_end, int is_front, float tilt_y, float roll_x) {
    const int pcx = 190;
    const int pcy = 150;
    int pitch = BB_W * 2;

    const float inv_tilt = 1.0f / tilt_y;
    const int max_y_extent = (int)(176.0f * tilt_y) + 4;
    int arc_y0 = is_front ? pcy : (pcy - max_y_extent);
    int arc_y1 = is_front ? (pcy + max_y_extent + 1) : pcy;

    int c_min = (y_start > arc_y0) ? y_start : arc_y0;
    int c_max = (y_end - 1 < arc_y1) ? (y_end - 1) : arc_y1;
    if (c_min > c_max) return;

    const float cos_roll = cosf(roll_x);
    const float sin_roll = sinf(roll_x);

    for (int y = c_min; y <= c_max; ++y) {
        float dy = (float)(y - pcy);
        uint8_t *line_ptr = &chunk_buf[(y - y_start) * pitch];

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

static void render_smooth_saturn_sphere_chunk(uint8_t *chunk_buf, int y_start, int y_end, float tilt_y, float roll_x) {
    const int pcx = 190;
    const int pcy = 150;
    const float inv_sr = 1.0f / (float)SPHERE_R;
    int pitch = BB_W * 2;

    int y_min = (y_start > pcy - SPHERE_R) ? y_start : (pcy - SPHERE_R);
    int y_max = (y_end - 1 < pcy + SPHERE_R) ? (y_end - 1) : (pcy + SPHERE_R);
    if (y_min > y_max) return;

    const float cos_roll = cosf(roll_x);
    const float sin_roll = sinf(roll_x);

    for (int y = y_min; y <= y_max; ++y) {
        int dy = y - pcy;
        int span_w2 = SPHERE_R2 - (dy * dy);

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

            uint8_t *line_ptr = &chunk_buf[(y - y_start) * pitch];

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

// saturn.start([fps]) - Chunked streaming DMA loop with zero memory fragmentation
static mp_obj_t mod_saturn_start(size_t n_args, const mp_obj_t *args) {
    uint32_t target_fps = 60;
    if (n_args > 0) {
        target_fps = (uint32_t)mp_obj_get_int(args[0]);
        if (target_fps < 1) target_fps = 1;
        if (target_fps > 120) target_fps = 120;
    }
    uint32_t target_delay_ms = 1000 / target_fps;

    // Allocate lightweight 32-line chunk buffer (~24 KB) instead of 228 KB full buffer
    uint8_t *chunk_buf = (uint8_t *)heap_caps_malloc(CHUNK_SIZE, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!chunk_buf) {
        mp_raise_msg(&mp_type_MemoryError, MP_ERROR_TEXT("Failed to allocate chunk DMA buffer"));
    }

    moclcd_init_internal();
    moclcd_panel_init_internal();
    moclcd_backlight_internal(true);
    moclcd_fill_screen_internal(0x0000);

    init_starfield();

    float time_phase = 0.0f;

    while (true) {
        mp_handle_pending(true); // Clean Ctrl+C interruption back to REPL
        int64_t frame_start = esp_timer_get_time();

        time_phase += 0.045f;
        float tilt_val = 0.38f + sinf(time_phase) * 0.10f;
        float roll_val = cosf(time_phase) * 0.18f;

        // Render and stream frame sequentially in 32-line chunks via DMA
        for (int c = 0; c < CHUNK_ROWS; ++c) {
            int y_start = c * CHUNK_H;
            int y_end = y_start + CHUNK_H;

            memset(chunk_buf, 0x00, CHUNK_SIZE);

            render_stars_chunk(chunk_buf, y_start, y_end, time_phase);
            render_rings_arc_chunk(chunk_buf, y_start, y_end, 0, tilt_val, roll_val);
            render_smooth_saturn_sphere_chunk(chunk_buf, y_start, y_end, tilt_val, roll_val);
            render_rings_arc_chunk(chunk_buf, y_start, y_end, 1, tilt_val, roll_val);

            moclcd_blit_internal(BB_X, BB_Y + y_start, BB_W, CHUNK_H, chunk_buf);
        }

        int64_t elapsed_ms = (esp_timer_get_time() - frame_start) / 1000;
        if (elapsed_ms < target_delay_ms) {
            mp_hal_delay_ms(target_delay_ms - elapsed_ms);
        }
    }

    heap_caps_free(chunk_buf);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_saturn_start_obj, 0, 1, mod_saturn_start);

static const mp_rom_map_elem_t saturn_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_saturn) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&mod_saturn_start_obj) },
};
static MP_DEFINE_CONST_DICT(saturn_module_globals, saturn_module_globals_table);

const mp_obj_module_t saturn_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&saturn_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_saturn, saturn_user_cmodule);
