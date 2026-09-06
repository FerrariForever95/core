// =====================================================================================
//  FILE:         modsolarsys.c
//  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 Bus via DMA
//  DESCRIPTION:  Full-Screen (480x320) Native C Solar System Engine:
//                - Continuous radial exponential Sun gradient
//                - Multi-tier twinkling procedural starfield (200 stars)
//                - 8 Planets with individual radii, speeds, and correct axial tilts
//                - Depth-sorted rendering with accurate ring occlusion for Saturn
//                - Exposes Python API: solarsystem.start(), solarsystem.stop()
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

extern void moclcd_init(void);
extern void moclcd_panel_init(void);
extern void moclcd_backlight(uint8_t state);
extern void moclcd_fill_screen(uint16_t color);
extern void moclcd_blit(int16_t x, int16_t y, int16_t w, int16_t h, const uint8_t *buf);

#define WIDTH     480
#define HEIGHT    320
#define CX        240
#define CY        160
#define CHUNK_H   32
#define CHUNK_ROWS (HEIGHT / CHUNK_H)
#define CHUNK_SIZE (WIDTH * CHUNK_H * 2)

#define NUM_STARS 200

typedef struct {
    int16_t x;
    int16_t sy;
    uint8_t tier;
    float phase;
    float speed;
} star_node_t;

typedef struct {
    const char *name;
    float dist;
    float r;
    float speed;
    float tilt;
    float col_r, col_g, col_b;
    float angle;
} planet_cfg_t;

static planet_cfg_t s_planets[8] = {
    {"mercury", 0.50f, 0.080f, 0.076f, 0.03f,   0.75f, 0.72f, 0.70f, 0.8f},
    {"venus",   0.78f, 0.115f, 0.054f, 177.3f,  0.95f, 0.86f, 0.54f, 2.4f},
    {"earth",   1.15f, 0.125f, 0.042f, 23.44f,  0.12f, 0.54f, 0.98f, 4.1f},
    {"mars",    1.50f, 0.095f, 0.032f, 25.19f,  0.94f, 0.32f, 0.16f, 1.2f},
    {"jupiter", 2.10f, 0.260f, 0.018f, 3.13f,   0.90f, 0.72f, 0.48f, 5.2f},
    {"saturn",  2.70f, 0.210f, 0.014f, 26.73f,  0.95f, 0.84f, 0.56f, 3.0f},
    {"uranus",  3.20f, 0.150f, 0.009f, 97.77f,  0.42f, 0.90f, 0.86f, 0.3f},
    {"neptune", 3.70f, 0.145f, 0.006f, 28.32f,  0.16f, 0.38f, 0.95f, 4.7f}
};

static star_node_t s_stars[NUM_STARS];
static uint8_t *s_chunk_buf = NULL;
static TaskHandle_t s_ss_task_handle = NULL;
static volatile bool s_ss_running = false;

static uint32_t s_rng_seed = 0x6B18D3C1;
static inline uint32_t lcg_rand(void) {
    s_rng_seed = (s_rng_seed * 1664525u + 1013904223u);
    return s_rng_seed;
}

static void init_starfield(void) {
    s_rng_seed = 0x6B18D3C1;
    for (int i = 0; i < NUM_STARS; ++i) {
        s_stars[i].x = (int16_t)((lcg_rand() % (WIDTH - 8)) + 4);
        s_stars[i].sy = (int16_t)((lcg_rand() % (HEIGHT - 8)) + 4);
        s_stars[i].tier = (uint8_t)(i % 4);
        s_stars[i].phase = (float)(lcg_rand() % 628) * 0.01f;
        s_stars[i].speed = 0.06f + (float)(lcg_rand() % 120) * 0.001f;
    }
}

static inline void render_stars_chunk(float t, int y_start, int y_end, uint8_t *buf) {
    int pitch = WIDTH * 2;
    for (int i = 0; i < NUM_STARS; ++i) {
        if (s_stars[i].sy >= y_start && s_stars[i].sy < y_end) {
            float tw = sinf(t * s_stars[i].speed + s_stars[i].phase);
            uint16_t col;
            if (s_stars[i].tier == 0) col = (tw > -0.2f) ? 0xFFFF : 0xCE79;
            else if (s_stars[i].tier == 1) col = (tw > 0.0f) ? 0x9E7F : 0x52AA;
            else if (s_stars[i].tier == 2) col = 0x8410;
            else col = 0x5ACB;

            int local_y = s_stars[i].sy - y_start;
            size_t off = (size_t)local_y * pitch + ((size_t)s_stars[i].x << 1);
            buf[off]     = (uint8_t)(col >> 8);
            buf[off + 1] = (uint8_t)(col & 0xFF);
        }
    }
}

static void render_smooth_sun_chunk(int y_start, int y_end, uint8_t *buf) {
    int pitch = WIDTH * 2;
    int r_corona = 36;
    float r_corona2 = 1296.0f;
    float inv_r = 1.0f / 36.0f;

    int y_min = (y_start > CY - r_corona) ? y_start : (CY - r_corona);
    int y_max = (y_end - 1 < CY + r_corona) ? (y_end - 1) : (CY + r_corona);
    if (y_min > y_max) return;

    for (int y = y_min; y <= y_max; ++y) {
        int dy = y - CY;
        float dy2 = (float)(dy * dy);
        int local_y = y - y_start;
        uint8_t *line_ptr = &buf[local_y * pitch];

        for (int x = CX - r_corona; x <= CX + r_corona; ++x) {
            if (x >= 0 && x < WIDTH) {
                float dx = (float)(x - CX);
                float d2 = dx * dx + dy2;
                if (d2 < r_corona2) {
                    float dist = sqrtf(d2);
                    float t = dist * inv_r;
                    if (t > 1.0f) t = 1.0f;

                    float r, g, b;
                    if (t < 0.28f) {
                        float u = t / 0.28f;
                        r = 1.0f; g = 1.0f; b = 1.0f - u * 0.85f;
                    } else if (t < 0.62f) {
                        float u = (t - 0.28f) / 0.34f;
                        r = 1.0f; g = 1.0f - u * 0.55f; b = 0.15f * (1.0f - u);
                    } else {
                        r = 0.40f * (1.0f - t); g = 0.0f; b = 0.0f;
                    }

                    uint16_t r_int = (uint16_t)(r * 31.0f);
                    uint16_t g_int = (uint16_t)(g * 63.0f);
                    uint16_t b_int = (uint16_t)(b * 31.0f);
                    uint16_t col = (r_int << 11) | (g_int << 5) | b_int;

                    size_t off = (size_t)x << 1;
                    line_ptr[off]     = (uint8_t)(col >> 8);
                    line_ptr[off + 1] = (uint8_t)(col & 0xFF);
                }
            }
        }
    }
}

static void render_detailed_planet_chunk(int y_start, int y_end, uint8_t *buf, int p_id, int sx, int sy, int sr, float lx, float ly, float lz, float cos_t, float sin_t) {
    int pitch = WIDTH * 2;
    if (sr < 2) {
        if (sy >= y_start && sy < y_end && sx >= 0 && sx < WIDTH) {
            size_t off = (size_t)(sy - y_start) * pitch + ((size_t)sx << 1);
            buf[off] = 0xFF; buf[off + 1] = 0xFF;
        }
        return;
    }

    int sr2 = sr * sr;
    float inv_sr = 1.0f / (float)sr;

    int c_min = (y_start > sy - sr) ? y_start : (sy - sr);
    int c_max = (y_end - 1 < sy + sr) ? (y_end - 1) : (sy + sr);
    if (c_min > c_max) return;

    for (int y = c_min; y <= c_max; ++y) {
        int dy = y - sy;
        int span_w2 = sr2 - (dy * dy);
        if (span_w2 >= 0) {
            int hw = (int)sqrtf((float)span_w2);
            int x0 = (0 > sx - hw) ? 0 : (sx - hw);
            int x1 = (WIDTH - 1 < sx + hw) ? (WIDTH - 1) : (sx + hw);

            float ny = -(float)dy * inv_sr;
            float ny2 = ny * ny;
            float l_dot_y = ny * ly;

            uint8_t *line_ptr = &buf[(y - y_start) * pitch];

            for (int x = x0; x <= x1; ++x) {
                float dx = (float)(x - sx);
                float nx = dx * inv_sr;
                float nz2 = 1.0f - (nx * nx + ny2);

                if (nz2 > 0.0f) {
                    float nz = sqrtf(nz2);
                    float local_lat_y = dx * sin_t + (float)dy * cos_t;
                    float lat = local_lat_y * inv_sr;
                    float abs_lat = fabsf(lat);

                    float br = 0.5f, bg = 0.5f, bb = 0.5f;
                    if (p_id == 2) { // Earth
                        if (abs_lat > 0.78f) { br = 1.f; bg = 1.f; bb = 1.f; }
                        else if (fabsf(sinf(nx * 4.0f + lat * 3.0f)) > 0.40f) { br = 0.18f; bg = 0.74f; bb = 0.28f; }
                        else { br = 0.08f; bg = 0.48f; bb = 0.98f; }
                    } else if (p_id == 4) { // Jupiter
                        if (((int)(abs_lat * 7.0f) & 1) == 0) { br = 0.92f; bg = 0.74f; bb = 0.52f; }
                        else { br = 0.74f; bg = 0.50f; bb = 0.32f; }
                    } else {
                        br = s_planets[p_id].col_r;
                        bg = s_planets[p_id].col_g;
                        bb = s_planets[p_id].col_b;
                    }

                    float dot_l = nx * lx + l_dot_y + nz * lz;
                    float diff = (dot_l > 0.0f) ? dot_l : 0.0f;
                    float amb = 0.16f;

                    float r = (amb + diff * 1.65f) * br;
                    float g = (amb + diff * 1.65f) * bg;
                    float b = (amb + diff * 1.65f) * bb;

                    if (r > 1.f) r = 1.f;
                    if (g > 1.f) g = 1.f;
                    if (b > 1.f) b = 1.f;

                    uint16_t r_int = (uint16_t)(r * 31.0f);
                    uint16_t g_int = (uint16_t)(g * 63.0f);
                    uint16_t b_int = (uint16_t)(b * 31.0f);
                    uint16_t col = (r_int << 11) | (g_int << 5) | b_int;

                    size_t off = (size_t)x << 1;
                    line_ptr[off]     = (uint8_t)(col >> 8);
                    line_ptr[off + 1] = (uint8_t)(col & 0xFF);
                }
            }
        }
    }
}

static void render_saturn_rings_unified_chunk(int y_start, int y_end, uint8_t *buf, int pcx, int pcy, int sr, float cos_t, float sin_t) {
    int pitch = WIDTH * 2;
    int r_in = (int)(sr * 1.35f);
    int r_out = (int)(sr * 2.35f);
    float tilt = 0.36f;
    float inv_tilt = 1.0f / tilt;

    int max_dy = (int)((float)r_out * tilt) + 2;
    int c_min = (y_start > pcy - max_dy) ? y_start : (pcy - max_dy);
    int c_max = (y_end - 1 < pcy + max_dy) ? (y_end - 1) : (pcy + max_dy);
    if (c_min > c_max) return;

    float r_in2 = (float)(r_in * r_in);
    float r_out2 = (float)(r_out * r_out);
    float gap_in2 = (float)((r_in + (int)((float)(r_out - r_in) * 0.58f)) * (r_in + (int)((float)(r_out - r_in) * 0.58f)));
    float gap_out2 = (float)((r_in + (int)((float)(r_out - r_in) * 0.66f)) * (r_in + (int)((float)(r_out - r_in) * 0.66f)));
    float sr2 = (float)(sr * sr);

    for (int y = c_min; y <= c_max; ++y) {
        float dy = (float)(y - pcy);
        uint8_t *line_ptr = &buf[(y - y_start) * pitch];

        for (int x = pcx - r_out; x <= pcx + r_out; ++x) {
            if (x >= 0 && x < WIDTH) {
                float dx = (float)(x - pcx);
                float d_sphere2 = dx * dx + dy * dy;
                if (d_sphere2 < sr2) {
                    float nz_depth = sqrtf(sr2 - d_sphere2);
                    float ry_test = (dx * sin_t + dy * cos_t) * inv_tilt;
                    float ring_z = -ry_test * sin_t;
                    if (ring_z < nz_depth) continue;
                }

                float rx = dx * cos_t - dy * sin_t;
                float ry = (dx * sin_t + dy * cos_t) * inv_tilt;
                float r_plane2 = rx * rx + ry * ry;

                if (r_plane2 >= r_in2 && r_plane2 <= r_out2 && !(r_plane2 >= gap_in2 && r_plane2 <= gap_out2)) {
                    uint16_t col = (r_plane2 < gap_in2) ? 0xFF6E : 0xCE54;
                    size_t off = (size_t)x << 1;
                    line_ptr[off]     = (uint8_t)(col >> 8);
                    line_ptr[off + 1] = (uint8_t)(col & 0xFF);
                }
            }
        }
    }
}

typedef struct {
    float z;
    int idx;
    int sx, sy, sr;
    float lx, ly, lz;
    float cos_t, sin_t;
} projected_planet_t;

static int compare_planets(const void *a, const void *b) {
    const projected_planet_t *pa = (const projected_planet_t *)a;
    const projected_planet_t *pb = (const projected_planet_t *)b;
    if (pa->z < pb->z) return 1;
    if (pa->z > pb->z) return -1;
    return 0;
}

static void solarsystem_render_task(void *pvParameters) {
    if (!s_chunk_buf) {
        s_chunk_buf = (uint8_t * )heap_caps_malloc(CHUNK_SIZE, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_chunk_buf) {
            s_ss_running = false;
            vTaskDelete(NULL);
            return;
        }
    }

    moclcd_init();
    moclcd_panel_init();
    moclcd_backlight(1);
    moclcd_fill_screen(0x0000);

    init_starfield();

    float star_timer = 0.0f;
    float cam_cos = cosf(34.0f * 3.14159f / 180.0f);
    float cam_sin = sinf(34.0f * 3.14159f / 180.0f);

    while (s_ss_running) {
        star_timer += 0.05f;

        projected_planet_t proj[8];
        for (int i = 0; i < 8; ++i) {
            s_planets[i].angle += s_planets[i].speed;

            float px = cosf(s_planets[i].angle) * (s_planets[i].dist * 1.15f);
            float pz = sinf(s_planets[i].angle) * (s_planets[i].dist * 1.15f);
            float py = 0.0f;

            float cam_x = px;
            float cam_y = py * cam_cos - pz * cam_sin;
            float cam_z = py * cam_sin + pz * cam_cos + 4.6f;

            float inv_wz = 1.0f / cam_z;
            proj[i].z = cam_z;
            proj[i].idx = i;
            proj[i].sx = (int)((float)CX + (cam_x * 295.0f * inv_wz));
            proj[i].sy = (int)((float)CY - (cam_y * 295.0f * inv_wz));
            proj[i].sr = (int)(s_planets[i].r * 295.0f * inv_wz * 1.25f);
            if (proj[i].sr < 2) proj[i].sr = 2;

            float lx = -cam_x, ly = -cam_y, lz = -(cam_z - 4.6f);
            float inv_l = 1.0f / sqrtf(lx * lx + ly * ly + lz * lz);
            proj[i].lx = lx * inv_l; proj[i].ly = ly * inv_l; proj[i].lz = lz * inv_l;

            float tilt_rad = s_planets[i].tilt * 3.14159f / 180.0f;
            proj[i].cos_t = cosf(tilt_rad);
            proj[i].sin_t = sinf(tilt_rad);
        }

        qsort(proj, 8, sizeof(projected_planet_t), compare_planets);

        for (int c = 0; c < CHUNK_ROWS; ++c) {
            int y_start = c * CHUNK_H;
            int y_end = y_start + CHUNK_H;

            memset(s_chunk_buf, 0x00, CHUNK_SIZE);

            render_stars_chunk(star_timer, y_start, y_end, s_chunk_buf);
            render_smooth_sun_chunk(y_start, y_end, s_chunk_buf);

            for (int i = 0; i < 8; ++i) {
                int p_id = proj[i].idx;
                if (p_id == 5) { // Saturn
                    render_detailed_planet_chunk(y_start, y_end, s_chunk_buf, p_id, proj[i].sx, proj[i].sy, proj[i].sr, proj[i].lx, proj[i].ly, proj[i].lz, proj[i].cos_t, proj[i].sin_t);
                    render_saturn_rings_unified_chunk(y_start, y_end, s_chunk_buf, proj[i].sx, proj[i].sy, proj[i].sr, proj[i].cos_t, proj[i].sin_t);
                } else {
                    render_detailed_planet_chunk(y_start, y_end, s_chunk_buf, p_id, proj[i].sx, proj[i].sy, proj[i].sr, proj[i].lx, proj[i].ly, proj[i].lz, proj[i].cos_t, proj[i].sin_t);
                }
            }

            moclcd_blit(0, y_start, WIDTH, CHUNK_H, s_chunk_buf);
        }

        vTaskDelay(pdMS_TO_TICKS(8));
    }

    if (s_chunk_buf) {
        heap_caps_free(s_chunk_buf);
        s_chunk_buf = NULL;
    }
    s_ss_task_handle = NULL;
    vTaskDelete(NULL);
}

// -------------------------------------------------------------------------
// MicroPython C-Module Interface
// -------------------------------------------------------------------------
static mp_obj_t mod_solarsystem_start(void) {
    if (s_ss_running) return mp_const_none;
    s_ss_running = true;

    BaseType_t res = xTaskCreatePinnedToCore(solarsystem_render_task, "ss_task", 8192, NULL, 5, &s_ss_task_handle, 1);
    if (res != pdPASS) {
        s_ss_running = false;
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("Failed to start solar system task"));
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_solarsystem_start_obj, mod_solarsystem_start);

static mp_obj_t mod_solarsystem_stop(void) {
    if (s_ss_running) {
        s_ss_running = false;
        while (s_ss_task_handle != NULL) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_solarsystem_stop_obj, mod_solarsystem_stop);

static const mp_rom_map_elem_t solarsystem_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_solarsystem) },
    { MP_ROM_QSTR(MP_QSTR_start),    MP_ROM_PTR(&mod_solarsystem_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),     MP_ROM_PTR(&mod_solarsystem_stop_obj) },
};
static MP_DEFINE_CONST_DICT(solarsystem_globals, solarsystem_globals_table);

const mp_obj_module_t solarsystem_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&solarsystem_globals,
};

MP_REGISTER_MODULE(MP_QSTR_solarsystem, solarsystem_user_cmodule);
