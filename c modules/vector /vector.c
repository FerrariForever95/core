/*
 * vector - fast buffer math for MicroPython on ESP32-S3 (PIE / AI vector unit)
 *
 * Fast paths use ESP-DSP kernels (PIE-accelerated on the S3) when the
 * esp-dsp component is available at build time. Otherwise a portable scalar
 * C fallback is compiled, so the module always builds and always works.
 *
 * Supported typecodes (array.array / memoryview / any buffer with typecode):
 *   'b' int8   'h' int16   'i' or 'l' int32   'f' float32
 *
 * API (all buffer-in / buffer-out, no allocation):
 *   vector.add(a, b, out, shift=0)
 *   vector.sub(a, b, out, shift=0)
 *   vector.mul(a, b, out, shift=0)
 *   vector.dot(a, b, shift=0)  -> int or float
 *   vector.info()              -> backend description string
 *
 * Integer ops saturate to the element type; `shift` is an arithmetic right
 * shift applied to the wide result before saturation (use it for fixed-point,
 * e.g. Q15 multiply: shift=15). `shift` is ignored for floats.
 * `out` may be the same object as `a` or `b` (in-place).
 */

#include <stdint.h>
#include <string.h>

#include "py/runtime.h"
#include "py/obj.h"
#include "py/objarray.h"

#if defined(__has_include)
#if __has_include("dsps_add.h") && __has_include("dsps_mul.h") && __has_include("dsps_dotprod.h")
#define VECTOR_HAVE_DSP 1
#include "dsps_add.h"
#include "dsps_mul.h"
#include "dsps_dotprod.h"
#endif
#endif

#ifndef VECTOR_HAVE_DSP
#define VECTOR_HAVE_DSP 0
#endif

#define CLAMP(r, lo, hi) ((r) < (lo) ? (lo) : ((r) > (hi) ? (hi) : (r)))

typedef void (*bin_fn_t)(const void *a, const void *b, void *o, size_t n, int sh);

enum { OP_ADD = 0, OP_SUB = 1, OP_MUL = 2 };
typedef enum { K_S8, K_S16, K_S32, K_F32 } kind_t;

/* ------------------------------------------------------------------ */
/* Scalar integer kernels (saturating)                                  */
/* ------------------------------------------------------------------ */

#define DEF_INT_OPS(SFX, T, WT, LO, HI)                                              \
    static void sc_add_##SFX(const void *pa, const void *pb, void *po, size_t n, int sh) { \
        const T *a = pa, *b = pb; T *o = po;                                         \
        for (size_t i = 0; i < n; i++) {                                             \
            WT r = (WT)a[i] + (WT)b[i];                                              \
            if (sh > 0) r >>= sh;                                                    \
            o[i] = (T)CLAMP(r, (WT)(LO), (WT)(HI));                                  \
        }                                                                            \
    }                                                                                \
    static void sc_sub_##SFX(const void *pa, const void *pb, void *po, size_t n, int sh) { \
        const T *a = pa, *b = pb; T *o = po;                                         \
        for (size_t i = 0; i < n; i++) {                                             \
            WT r = (WT)a[i] - (WT)b[i];                                              \
            if (sh > 0) r >>= sh;                                                    \
            o[i] = (T)CLAMP(r, (WT)(LO), (WT)(HI));                                  \
        }                                                                            \
    }                                                                                \
    static void sc_mul_##SFX(const void *pa, const void *pb, void *po, size_t n, int sh) { \
        const T *a = pa, *b = pb; T *o = po;                                         \
        for (size_t i = 0; i < n; i++) {                                             \
            WT r = (WT)a[i] * (WT)b[i];                                              \
            if (sh > 0) r >>= sh;                                                    \
            o[i] = (T)CLAMP(r, (WT)(LO), (WT)(HI));                                  \
        }                                                                            \
    }

DEF_INT_OPS(s8,  int8_t,  int32_t, INT8_MIN,  INT8_MAX)
DEF_INT_OPS(s16, int16_t, int32_t, INT16_MIN, INT16_MAX)
DEF_INT_OPS(s32, int32_t, int64_t, INT32_MIN, INT32_MAX)

/* ------------------------------------------------------------------ */
/* Scalar float kernels                                                 */
/* ------------------------------------------------------------------ */

static void sc_add_f32(const void *pa, const void *pb, void *po, size_t n, int sh) {
    (void)sh; const float *a = pa, *b = pb; float *o = po;
    for (size_t i = 0; i < n; i++) o[i] = a[i] + b[i];
}
static void sc_sub_f32(const void *pa, const void *pb, void *po, size_t n, int sh) {
    (void)sh; const float *a = pa, *b = pb; float *o = po;
    for (size_t i = 0; i < n; i++) o[i] = a[i] - b[i];
}
static void sc_mul_f32(const void *pa, const void *pb, void *po, size_t n, int sh) {
    (void)sh; const float *a = pa, *b = pb; float *o = po;
    for (size_t i = 0; i < n; i++) o[i] = a[i] * b[i];
}

/* ------------------------------------------------------------------ */
/* ESP-DSP (PIE-accelerated) fast paths                                 */
/* ------------------------------------------------------------------ */

#if VECTOR_HAVE_DSP
static void fast_add_s16(const void *a, const void *b, void *o, size_t n, int sh) {
    dsps_add_s16(a, b, o, (int)n, 1, 1, 1, sh > 0 ? sh : 0);
}
static void fast_mul_s16(const void *a, const void *b, void *o, size_t n, int sh) {
    dsps_mul_s16(a, b, o, (int)n, 1, 1, 1, sh > 0 ? sh : 0);
}
static void fast_add_f32(const void *a, const void *b, void *o, size_t n, int sh) {
    (void)sh; dsps_add_f32(a, b, o, (int)n, 1, 1, 1);
}
static void fast_mul_f32(const void *a, const void *b, void *o, size_t n, int sh) {
    (void)sh; dsps_mul_f32(a, b, o, (int)n, 1, 1, 1);
}
#define ADD_S16 fast_add_s16
#define MUL_S16 fast_mul_s16
#define ADD_F32 fast_add_f32
#define MUL_F32 fast_mul_f32
#else
#define ADD_S16 sc_add_s16
#define MUL_S16 sc_mul_s16
#define ADD_F32 sc_add_f32
#define MUL_F32 sc_mul_f32
#endif

/* dispatch tables: [kind][op] */
static const bin_fn_t op_table[4][3] = {
    [K_S8]  = { sc_add_s8,  sc_sub_s8,  sc_mul_s8  },
    [K_S16] = { ADD_S16,    sc_sub_s16, MUL_S16    },
    [K_S32] = { sc_add_s32, sc_sub_s32, sc_mul_s32 },
    [K_F32] = { ADD_F32,    sc_sub_f32, MUL_F32    },
};

/* ------------------------------------------------------------------ */
/* Helpers                                                              */
/* ------------------------------------------------------------------ */

static kind_t kind_of(int tc, size_t *esz) {
    switch (tc) {
        case 'b': *esz = 1; return K_S8;
        case 'h': *esz = 2; return K_S16;
        case 'i':
        case 'l': *esz = 4; return K_S32;
        case 'f': *esz = 4; return K_F32;
        default:
            mp_raise_TypeError(MP_ERROR_TEXT("vector: typecode must be b, h, i/l or f"));
    }
}

static kind_t check_pair(mp_buffer_info_t *a, mp_buffer_info_t *b, size_t *n) {
    size_t esz_a, esz_b;
    kind_t ka = kind_of(a->typecode, &esz_a);
    kind_t kb = kind_of(b->typecode, &esz_b);
    if (ka != kb) {
        mp_raise_TypeError(MP_ERROR_TEXT("vector: typecodes differ"));
    }
    size_t na = a->len / esz_a, nb = b->len / esz_b;
    if (na != nb) {
        mp_raise_ValueError(MP_ERROR_TEXT("vector: lengths differ"));
    }
    *n = na;
    return ka;
}

enum { ARG_a, ARG_b, ARG_out, ARG_shift };

static mp_obj_t vector_binop(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args, int op) {
    static const mp_arg_t allowed[] = {
        { MP_QSTR_a,     MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = mp_const_none} },
        { MP_QSTR_b,     MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = mp_const_none} },
        { MP_QSTR_out,   MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = mp_const_none} },
        { MP_QSTR_shift, MP_ARG_KW_ONLY | MP_ARG_INT,  {.u_int = 0} },
    };
    mp_arg_val_t v[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, v);

    mp_buffer_info_t a, b, o;
    mp_get_buffer_raise(v[ARG_a].u_obj, &a, MP_BUFFER_READ);
    mp_get_buffer_raise(v[ARG_b].u_obj, &b, MP_BUFFER_READ);
    mp_get_buffer_raise(v[ARG_out].u_obj, &o, MP_BUFFER_WRITE);

    size_t n;
    kind_t k = check_pair(&a, &b, &n);
    if (o.typecode != a.typecode && !((o.typecode == 'i' || o.typecode == 'l') && k == K_S32)) {
        mp_raise_TypeError(MP_ERROR_TEXT("vector: out typecode differs"));
    }
    size_t esz;
    kind_of(o.typecode, &esz);
    if (o.len / esz != n) {
        mp_raise_ValueError(MP_ERROR_TEXT("vector: out length differs"));
    }
    if (v[ARG_shift].u_int < 0 || v[ARG_shift].u_int > 62) {
        mp_raise_ValueError(MP_ERROR_TEXT("vector: shift out of range"));
    }
    if (n) {
        op_table[k][op](a.buf, b.buf, o.buf, n, v[ARG_shift].u_int);
    }
    return mp_const_none;
}

static mp_obj_t vector_add(size_t n, const mp_obj_t *p, mp_map_t *kw) { return vector_binop(n, p, kw, OP_ADD); }
static mp_obj_t vector_sub(size_t n, const mp_obj_t *p, mp_map_t *kw) { return vector_binop(n, p, kw, OP_SUB); }
static mp_obj_t vector_mul(size_t n, const mp_obj_t *p, mp_map_t *kw) { return vector_binop(n, p, kw, OP_MUL); }
static MP_DEFINE_CONST_FUN_OBJ_KW(vector_add_obj, 3, vector_add);
static MP_DEFINE_CONST_FUN_OBJ_KW(vector_sub_obj, 3, vector_sub);
static MP_DEFINE_CONST_FUN_OBJ_KW(vector_mul_obj, 3, vector_mul);

/* ------------------------------------------------------------------ */
/* dot product                                                          */
/* ------------------------------------------------------------------ */

static mp_obj_t vector_dot(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    static const mp_arg_t allowed[] = {
        { MP_QSTR_a,     MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = mp_const_none} },
        { MP_QSTR_b,     MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = mp_const_none} },
        { MP_QSTR_shift, MP_ARG_KW_ONLY | MP_ARG_INT,  {.u_int = 0} },
    };
    mp_arg_val_t v[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, v);

    mp_buffer_info_t a, b;
    mp_get_buffer_raise(v[0].u_obj, &a, MP_BUFFER_READ);
    mp_get_buffer_raise(v[1].u_obj, &b, MP_BUFFER_READ);
    int sh = v[2].u_int;
    if (sh < 0 || sh > 62) {
        mp_raise_ValueError(MP_ERROR_TEXT("vector: shift out of range"));
    }

    size_t n;
    kind_t k = check_pair(&a, &b, &n);

    switch (k) {
        case K_F32: {
            float acc = 0.0f;
            #if VECTOR_HAVE_DSP
            if (n) dsps_dotprod_f32(a.buf, b.buf, &acc, (int)n);
            #else
            const float *x = a.buf, *y = b.buf;
            for (size_t i = 0; i < n; i++) acc += x[i] * y[i];
            #endif
            return mp_obj_new_float(acc);
        }
        case K_S16: {
            /* Result is int16 (ESP-DSP semantics): pick `shift` so it fits. */
            #if VECTOR_HAVE_DSP
            int16_t r = 0;
            if (n) dsps_dotprod_s16(a.buf, b.buf, &r, (int)n, (int8_t)sh);
            return mp_obj_new_int(r);
            #else
            const int16_t *x = a.buf, *y = b.buf;
            int64_t acc = 0;
            for (size_t i = 0; i < n; i++) acc += (int32_t)x[i] * (int32_t)y[i];
            acc >>= sh;
            return mp_obj_new_int(CLAMP(acc, (int64_t)INT16_MIN, (int64_t)INT16_MAX));
            #endif
        }
        case K_S8: {
            const int8_t *x = a.buf, *y = b.buf;
            int64_t acc = 0;
            for (size_t i = 0; i < n; i++) acc += (int32_t)x[i] * (int32_t)y[i];
            return mp_obj_new_int_from_ll(acc >> sh);
        }
        case K_S32:
        default: {
            const int32_t *x = a.buf, *y = b.buf;
            int64_t acc = 0;
            for (size_t i = 0; i < n; i++) acc += (int64_t)x[i] * (int64_t)y[i];
            return mp_obj_new_int_from_ll(acc >> sh);
        }
    }
}
static MP_DEFINE_CONST_FUN_OBJ_KW(vector_dot_obj, 2, vector_dot);

/* ------------------------------------------------------------------ */
/* info                                                                 */
/* ------------------------------------------------------------------ */

static mp_obj_t vector_info(void) {
    #if VECTOR_HAVE_DSP
    const char *m = "esp-dsp (PIE accelerated: add/mul s16 f32, dot s16 f32); scalar: others";
    return mp_obj_new_str(m, strlen(m));
    #else
    const char *m = "scalar C fallback (esp-dsp not found at build time)";
    return mp_obj_new_str(m, strlen(m));
    #endif
}
static MP_DEFINE_CONST_FUN_OBJ_0(vector_info_obj, vector_info);

/* ------------------------------------------------------------------ */
/* module                                                               */
/* ------------------------------------------------------------------ */

static const mp_rom_map_elem_t vector_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_vector) },
    { MP_ROM_QSTR(MP_QSTR_add),  MP_ROM_PTR(&vector_add_obj) },
    { MP_ROM_QSTR(MP_QSTR_sub),  MP_ROM_PTR(&vector_sub_obj) },
    { MP_ROM_QSTR(MP_QSTR_mul),  MP_ROM_PTR(&vector_mul_obj) },
    { MP_ROM_QSTR(MP_QSTR_dot),  MP_ROM_PTR(&vector_dot_obj) },
    { MP_ROM_QSTR(MP_QSTR_info), MP_ROM_PTR(&vector_info_obj) },
};
static MP_DEFINE_CONST_DICT(vector_module_globals, vector_module_globals_table);

const mp_obj_module_t vector_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&vector_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_vector, vector_user_cmodule);
