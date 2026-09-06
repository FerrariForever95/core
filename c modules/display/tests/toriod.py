import math
import time
import machine
import moclcd
import micropython

# Lock silicon clock to maximum performance
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 145
FOV    = 240.0
CAM_Z  = 4.2

# Hardware initialization
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0xFFFF)

VIRTUAL_FLOOR_Y = -1.50
INV_LIGHT_Y = 1.0 / -1.75
LIGHT_DIR_X = -0.25
LIGHT_DIR_Z = -0.20

# High-density stress window: 420 x 300 (840 bytes/row, 252 KB frame buffer)
BB_W = 420
BB_H = 300
BB_X = CX - 210
BB_Y = CY - 145
ROW_PITCH = BB_W * 2

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * ROW_PITCH)

# Torus mesh topology (16 segments along ring, 8 tube slices = 128 vertices, 128 quads)
SEGS_U = 16
SEGS_V = 8
TOTAL_VERTS = SEGS_U * SEGS_V
TOTAL_FACES = TOTAL_VERTS

R_OUTER = 1.15
r_TUBE1 = 0.16

R_INNER = 0.78
r_TUBE2 = 0.13

# Precompute unit parametric donut coordinates
TORUS1_U = [0.0] * TOTAL_VERTS
TORUS1_V = [0.0] * TOTAL_VERTS
TORUS1_W = [0.0] * TOTAL_VERTS

TORUS2_U = [0.0] * TOTAL_VERTS
TORUS2_V = [0.0] * TOTAL_VERTS
TORUS2_W = [0.0] * TOTAL_VERTS

idx = 0
for u_step in range(SEGS_U):
    u = u_step * (2.0 * math.pi / SEGS_U)
    cos_u = math.cos(u)
    sin_u = math.sin(u)
    for v_step in range(SEGS_V):
        v = v_step * (2.0 * math.pi / SEGS_V)
        cos_v = math.cos(v)
        sin_v = math.sin(v)

        # Outer ring
        TORUS1_U[idx] = (R_OUTER + r_TUBE1 * cos_v) * cos_u
        TORUS1_V[idx] = (R_OUTER + r_TUBE1 * cos_v) * sin_u
        TORUS1_W[idx] = r_TUBE1 * sin_v

        # Inner ring
        TORUS2_U[idx] = (R_INNER + r_TUBE2 * cos_v) * cos_u
        TORUS2_V[idx] = (R_INNER + r_TUBE2 * cos_v) * sin_u
        TORUS2_W[idx] = r_TUBE2 * sin_v

        idx += 1

# Precompute quad indices
F_V0 = [0] * TOTAL_FACES
F_V1 = [0] * TOTAL_FACES
F_V2 = [0] * TOTAL_FACES
F_V3 = [0] * TOTAL_FACES

f_idx = 0
for u_step in range(SEGS_U):
    next_u = (u_step + 1) % SEGS_U
    for v_step in range(SEGS_V):
        next_v = (v_step + 1) % SEGS_V
        F_V0[f_idx] = u_step * SEGS_V + v_step
        F_V1[f_idx] = next_u * SEGS_V + v_step
        F_V2[f_idx] = next_u * SEGS_V + next_v
        F_V3[f_idx] = u_step * SEGS_V + next_v
        f_idx += 1

# Transformation & raster scratch buffers
TV1_X = [0.0] * TOTAL_VERTS
TV1_Y = [0.0] * TOTAL_VERTS
TV1_Z = [0.0] * TOTAL_VERTS
SV1_X = [0] * TOTAL_VERTS
SV1_Y = [0] * TOTAL_VERTS
SH1_X = [0] * TOTAL_VERTS
SH1_Y = [0] * TOTAL_VERTS

TV2_X = [0.0] * TOTAL_VERTS
TV2_Y = [0.0] * TOTAL_VERTS
TV2_Z = [0.0] * TOTAL_VERTS
SV2_X = [0] * TOTAL_VERTS
SV2_Y = [0] * TOTAL_VERTS
SH2_X = [0] * TOTAL_VERTS
SH2_Y = [0] * TOTAL_VERTS

SORT_KEYS = [0.0] * (TOTAL_FACES * 2)
SORT_IDXS = [0] * (TOTAL_FACES * 2)
FACE_COLS = [0] * (TOTAL_FACES * 2)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    pitch = 840
    for y in range(y0, y1 + 1):
        idx = y * pitch
        buf[idx:idx + 840] = white_row

@micropython.native
def raster_edge(x0: int, y0: int, x1: int, y1: int, e_min, e_max):
    if y0 == y1:
        return
    if y0 > y1:
        x0, x1 = x1, x0
        y0, y1 = y1, y0

    dx = x1 - x0
    dy = y1 - y0
    step = (dx << 16) // dy
    curr = x0 << 16

    for y in range(y0, y1):
        if 0 <= y < 300:
            px = curr >> 16
            if px < e_min[y]:
                e_min[y] = px
            if px > e_max[y]:
                e_max[y] = px
        curr += step

@micropython.native
def fill_spans(min_y: int, max_y: int, hi: int, lo: int, buf, e_min, e_max):
    pitch = 840
    for y in range(min_y, max_y + 1):
        xs = e_min[y]
        xe = e_max[y]
        if xs > xe:
            continue
        if xs < 0:
            xs = 0
        if xe >= 420:
            xe = 419

        offset = y * pitch + (xs << 1)
        cnt = xe - xs + 1
        for _ in range(cnt):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

def raster_quad_pts(p0, p1, p2, p3, hi: int, lo: int):
    y0 = p0[1]
    y1 = p1[1]
    y2 = p2[1]
    y3 = p3[1]

    min_y = y0
    if y1 < min_y: min_y = y1
    if y2 < min_y: min_y = y2
    if y3 < min_y: min_y = y3

    max_y = y0
    if y1 > max_y: max_y = y1
    if y2 > max_y: max_y = y2
    if y3 > max_y: max_y = y3

    if min_y < 0: min_y = 0
    if max_y >= 300: max_y = 299
    if min_y > max_y: return 300, 0

    for y in range(min_y, max_y + 1):
        EDGE_MIN[y] = 9999
        EDGE_MAX[y] = -9999

    raster_edge(p0[0], y0, p1[0], y1, EDGE_MIN, EDGE_MAX)
    raster_edge(p1[0], y1, p2[0], y2, EDGE_MIN, EDGE_MAX)
    raster_edge(p2[0], y2, p3[0], y3, EDGE_MIN, EDGE_MAX)
    raster_edge(p3[0], y3, p0[0], y0, EDGE_MIN, EDGE_MAX)

    fill_spans(min_y, max_y, hi, lo, FRAME_BUF, EDGE_MIN, EDGE_MAX)
    return min_y, max_y

@micropython.native
def render_core_sphere(cx: int, cy: int, r_screen: int, buf):
    pitch = 840
    r2 = r_screen * r_screen
    inv_r = 1.0 / r_screen

    y_min = cy - r_screen
    y_max = cy + r_screen
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - cy
        dy2 = dy * dy
        span_w2 = r2 - dy2
        if span_w2 >= 0:
            half_w = int(math.sqrt(span_w2))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 >= 420: x1 = 419

            ny_base = -dy * inv_r
            ny2 = ny_base * ny_base
            dot_k_y = ny_base * 0.57735
            dot_f_y = ny_base * -0.8165
            dot_h_y = ny_base * 0.3714

            offset = y * pitch + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = x - cx
                nx = dx * inv_r
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    dot_k = nx * 0.57735 + dot_k_y - nz * 0.57735
                    diff_k = dot_k if dot_k > 0.0 else 0.0

                    dot_f = nx * -0.4082 + dot_f_y + nz * 0.4082
                    diff_f = dot_f if dot_f > 0.0 else 0.0

                    dot_h = nx * 0.3714 + dot_h_y - nz * 0.8510
                    spec = 0.0
                    if dot_h > 0.0:
                        h2 = dot_h * dot_h
                        h4 = h2 * h2
                        spec = h4 * h4

                    # Deep Chrome Gold Core
                    r = 0.20 + 0.95 * diff_k + 0.30 * diff_f + spec * 1.60
                    g = 0.16 + 0.75 * diff_k + 0.22 * diff_f + spec * 1.60
                    b = 0.05 + 0.20 * diff_k + 0.08 * diff_f + spec * 1.60

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    ir = int(r * 31.0) & 0x1F
                    ig = int(g * 63.0) & 0x3F
                    ib = int(b * 31.0) & 0x1F

                    buf[offset] = (ir << 3) | (ig >> 3)
                    buf[offset + 1] = ((ig & 0x07) << 5) | ib
                offset += 2

    return y_min, y_max

def run():
    rot1_x = 0.0
    rot1_y = 0.0
    rot2_y = 0.0
    rot2_z = 0.0

    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        idx = y * ROW_PITCH
        FRAME_BUF[idx:idx + ROW_PITCH] = WHITE_CHUNK

    fps_counter = 0
    t_last_fps = time.ticks_ms()
    fps_display_str = "FPS: -- | Capped to 72"

    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff

    moclcd.fill_rect(10, 10, 160, 12, 0xFFFF)
    moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)

    TARGET_FRAME_MS = 14

    while True:
        frame_start = ticks_ms()

        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        # -----------------------------------------------------------------
        # 1. Outer Ring Rotation Matrix (Primary Gimbal Axis)
        # -----------------------------------------------------------------
        cx1, sx1 = math.cos(rot1_x), math.sin(rot1_x)
        cy1, sy1 = math.cos(rot1_y), math.sin(rot1_y)

        r1_00 = cy1
        r1_01 = sx1 * sy1
        r1_02 = cx1 * sy1

        r1_10 = 0.0
        r1_11 = cx1
        r1_12 = -sx1

        r1_20 = -sy1
        r1_21 = sx1 * cy1
        r1_22 = cx1 * cy1

        # -----------------------------------------------------------------
        # 2. Inner Ring Rotation Matrix (Secondary Precessing Axis)
        # -----------------------------------------------------------------
        cy2, sy2 = math.cos(rot2_y), math.sin(rot2_y)
        cz2, sz2 = math.cos(rot2_z), math.sin(rot2_z)

        r2_00 = cy2 * cz2
        r2_01 = -sz2
        r2_02 = sy2 * cz2

        r2_10 = cy2 * sz2
        r2_11 = cz2
        r2_12 = sy2 * sz2

        r2_20 = -sy2
        r2_21 = 0.0
        r2_22 = cy2

        frame_min_y = 300
        frame_max_y = 0

        # Transform Outer Torus Vertices & Project Shadows
        for i in range(TOTAL_VERTS):
            u, v, w = TORUS1_U[i], TORUS1_V[i], TORUS1_W[i]
            x = r1_00 * u + r1_01 * v + r1_02 * w
            y = r1_10 * u + r1_11 * v + r1_12 * w
            z = r1_20 * u + r1_21 * v + r1_22 * w

            # Virtual floor shadow ray
            t = (VIRTUAL_FLOOR_Y - y) * INV_LIGHT_Y
            sx_w = x + t * LIGHT_DIR_X
            sz_w = z + t * LIGHT_DIR_Z + CAM_Z

            inv_sz = 1.0 / sz_w
            SH1_X[i] = int((CX + (sx_w * FOV * inv_sz)) - BB_X)
            SH1_Y[i] = int((CY - (VIRTUAL_FLOOR_Y * FOV * inv_sz)) - BB_Y)

            cam_z = z + CAM_Z
            TV1_X[i] = x
            TV1_Y[i] = y
            TV1_Z[i] = cam_z

            inv_z = 1.0 / cam_z
            SV1_X[i] = int((CX + (x * FOV * inv_z)) - BB_X)
            SV1_Y[i] = int((CY - (y * FOV * inv_z)) - BB_Y)

        # Transform Inner Torus Vertices & Project Shadows
        for i in range(TOTAL_VERTS):
            u, v, w = TORUS2_U[i], TORUS2_V[i], TORUS2_W[i]
            x = r2_00 * u + r2_01 * v + r2_02 * w
            y = r2_10 * u + r2_11 * v + r2_12 * w
            z = r2_20 * u + r2_21 * v + r2_22 * w

            t = (VIRTUAL_FLOOR_Y - y) * INV_LIGHT_Y
            sx_w = x + t * LIGHT_DIR_X
            sz_w = z + t * LIGHT_DIR_Z + CAM_Z

            inv_sz = 1.0 / sz_w
            SH2_X[i] = int((CX + (sx_w * FOV * inv_sz)) - BB_X)
            SH2_Y[i] = int((CY - (VIRTUAL_FLOOR_Y * FOV * inv_sz)) - BB_Y)

            cam_z = z + CAM_Z
            TV2_X[i] = x
            TV2_Y[i] = y
            TV2_Z[i] = cam_z

            inv_z = 1.0 / cam_z
            SV2_X[i] = int((CX + (x * FOV * inv_z)) - BB_X)
            SV2_Y[i] = int((CY - (y * FOV * inv_z)) - BB_Y)

        # -----------------------------------------------------------------
        # 3. Rasterize Shadow Meshes on Floor (Dual Hollow Ring Shadows)
        # -----------------------------------------------------------------
        for f in range(TOTAL_FACES):
            i0, i1, i2, i3 = F_V0[f], F_V1[f], F_V2[f], F_V3[f]
            s1_min, s1_max = raster_quad_pts(
                (SH1_X[i0], SH1_Y[i0]), (SH1_X[i1], SH1_Y[i1]),
                (SH1_X[i2], SH1_Y[i2]), (SH1_X[i3], SH1_Y[i3]),
                0xAD, 0x55
            )
            if s1_min < frame_min_y: frame_min_y = s1_min
            if s1_max > frame_max_y: frame_max_y = s1_max

            s2_min, s2_max = raster_quad_pts(
                (SH2_X[i0], SH2_Y[i0]), (SH2_X[i1], SH2_Y[i1]),
                (SH2_X[i2], SH2_Y[i2]), (SH2_X[i3], SH2_Y[i3]),
                0x94, 0x92
            )
            if s2_min < frame_min_y: frame_min_y = s2_min
            if s2_max > frame_max_y: frame_max_y = s2_max

        # -----------------------------------------------------------------
        # 4. Face Normals, Backface Cull & Dual Lighting
        # -----------------------------------------------------------------
        active_faces = 0

        # Process Outer Ring Faces (Electric Cyan / Cobalt)
        for f in range(TOTAL_FACES):
            i0, i1, i2, i3 = F_V0[f], F_V1[f], F_V2[f], F_V3[f]
            x0, y0, z0 = TV1_X[i0], TV1_Y[i0], TV1_Z[i0]
            e1x, e1y, e1z = TV1_X[i1] - x0, TV1_Y[i1] - y0, TV1_Z[i1] - z0
            e2x, e2y, e2z = TV1_X[i2] - x0, TV1_Y[i2] - y0, TV1_Z[i2] - z0

            nx = e1y * e2z - e1z * e2y
            ny = e1z * e2x - e1x * e2z
            nz = e1x * e2y - e1y * e2x

            if (nx * x0 + ny * y0 + nz * z0) < 0.0:
                inv_l = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
                nx *= inv_l
                ny *= inv_l
                nz *= inv_l

                dot_k = nx * 0.57735 + ny * 0.57735 - nz * 0.57735
                diff_k = dot_k if dot_k > 0.0 else 0.0
                dot_f = nx * -0.4082 + ny * -0.8165 + nz * 0.4082
                diff_f = dot_f if dot_f > 0.0 else 0.0

                dot_h = nx * 0.3714 + ny * 0.3714 - nz * 0.8510
                spec = (dot_h ** 8) if dot_h > 0.0 else 0.0

                r = 0.08 + 0.35 * diff_k + 0.10 * diff_f + spec * 1.30
                g = 0.35 + 0.85 * diff_k + 0.25 * diff_f + spec * 1.30
                b = 0.80 + 1.10 * diff_k + 0.40 * diff_f + spec * 1.40

                if r > 1.0: r = 1.0
                if g > 1.0: g = 1.0
                if b > 1.0: b = 1.0

                c = ((int(r * 31.0) & 0x1F) << 11) | ((int(g * 63.0) & 0x3F) << 5) | (int(b * 31.0) & 0x1F)
                FACE_COLS[active_faces] = c
                SORT_KEYS[active_faces] = z0 + TV1_Z[i1] + TV1_Z[i2] + TV1_Z[i3]
                SORT_IDXS[active_faces] = f
                active_faces += 1

        # Process Inner Ring Faces (Ruby Crimson)
        for f in range(TOTAL_FACES):
            i0, i1, i2, i3 = F_V0[f], F_V1[f], F_V2[f], F_V3[f]
            x0, y0, z0 = TV2_X[i0], TV2_Y[i0], TV2_Z[i0]
            e1x, e1y, e1z = TV2_X[i1] - x0, TV2_Y[i1] - y0, TV2_Z[i1] - z0
            e2x, e2y, e2z = TV2_X[i2] - x0, TV2_Y[i2] - y0, TV2_Z[i2] - z0

            nx = e1y * e2z - e1z * e2y
            ny = e1z * e2x - e1x * e2z
            nz = e1x * e2y - e1y * e2x

            if (nx * x0 + ny * y0 + nz * z0) < 0.0:
                inv_l = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
                nx *= inv_l
                ny *= inv_l
                nz *= inv_l

                dot_k = nx * 0.57735 + ny * 0.57735 - nz * 0.57735
                diff_k = dot_k if dot_k > 0.0 else 0.0
                dot_f = nx * -0.4082 + ny * -0.8165 + nz * 0.4082
                diff_f = dot_f if dot_f > 0.0 else 0.0

                dot_h = nx * 0.3714 + ny * 0.3714 - nz * 0.8510
                spec = (dot_h ** 8) if dot_h > 0.0 else 0.0

                r = 0.85 + 1.15 * diff_k + 0.30 * diff_f + spec * 1.40
                g = 0.10 + 0.25 * diff_k + 0.10 * diff_f + spec * 1.30
                b = 0.20 + 0.30 * diff_k + 0.15 * diff_f + spec * 1.30

                if r > 1.0: r = 1.0
                if g > 1.0: g = 1.0
                if b > 1.0: b = 1.0

                c = ((int(r * 31.0) & 0x1F) << 11) | ((int(g * 63.0) & 0x3F) << 5) | (int(b * 31.0) & 0x1F)
                FACE_COLS[active_faces] = c
                SORT_KEYS[active_faces] = z0 + TV2_Z[i1] + TV2_Z[i2] + TV2_Z[i3]
                # Encode inner ring flag in MSB of index
                SORT_IDXS[active_faces] = f | 0x8000
                active_faces += 1

        # Painter's Algorithm Depth Sort (In-Place Insertion Sort)
        for i in range(1, active_faces):
            k = SORT_KEYS[i]
            idx_v = SORT_IDXS[i]
            col_v = FACE_COLS[i]
            j = i - 1
            while j >= 0 and SORT_KEYS[j] < k:
                SORT_KEYS[j + 1] = SORT_KEYS[j]
                SORT_IDXS[j + 1] = SORT_IDXS[j]
                FACE_COLS[j + 1] = FACE_COLS[j]
                j -= 1
            SORT_KEYS[j + 1] = k
            SORT_IDXS[j + 1] = idx_v
            FACE_COLS[j + 1] = col_v

        # -----------------------------------------------------------------
        # 5. Interleaved Depth-Sorted Rasterization
        # -----------------------------------------------------------------
        # Center core sphere renders when depth crosses CAM_Z
        core_rendered = False
        core_scr_r = int(0.38 * FOV * (1.0 / CAM_Z))
        core_cx = CX - BB_X
        core_cy = CY - BB_Y

        for i in range(active_faces):
            avg_z = SORT_KEYS[i] * 0.25
            if not core_rendered and avg_z <= CAM_Z:
                sp_min, sp_max = render_core_sphere(core_cx, core_cy, core_scr_r, FRAME_BUF)
                if sp_min < frame_min_y: frame_min_y = sp_min
                if sp_max > frame_max_y: frame_max_y = sp_max
                core_rendered = True

            raw_idx = SORT_IDXS[i]
            col = FACE_COLS[i]
            hi = (col >> 8) & 0xFF
            lo = col & 0xFF

            if raw_idx & 0x8000:
                f = raw_idx & 0x7FFF
                i0, i1, i2, i3 = F_V0[f], F_V1[f], F_V2[f], F_V3[f]
                q_min, q_max = raster_quad_pts(
                    (SV2_X[i0], SV2_Y[i0]), (SV2_X[i1], SV2_Y[i1]),
                    (SV2_X[i2], SV2_Y[i2]), (SV2_X[i3], SV2_Y[i3]),
                    hi, lo
                )
            else:
                f = raw_idx
                i0, i1, i2, i3 = F_V0[f], F_V1[f], F_V2[f], F_V3[f]
                q_min, q_max = raster_quad_pts(
                    (SV1_X[i0], SV1_Y[i0]), (SV1_X[i1], SV1_Y[i1]),
                    (SV1_X[i2], SV1_Y[i2]), (SV1_X[i3], SV1_Y[i3]),
                    hi, lo
                )

            if q_min < frame_min_y: frame_min_y = q_min
            if q_max > frame_max_y: frame_max_y = q_max

        if not core_rendered:
            sp_min, sp_max = render_core_sphere(core_cx, core_cy, core_scr_r, FRAME_BUF)
            if sp_min < frame_min_y: frame_min_y = sp_min
            if sp_max > frame_max_y: frame_max_y = sp_max

        # Vertical screen clamps
        if frame_min_y < 0: frame_min_y = 0
        if frame_max_y >= 300: frame_max_y = 299

        blit_top = frame_min_y if frame_min_y < prev_min_y else prev_min_y
        blit_bottom = frame_max_y if frame_max_y > prev_max_y else prev_max_y
        blit_h = blit_bottom - blit_top + 1

        start_offset = blit_top * ROW_PITCH
        end_offset = start_offset + (blit_h * ROW_PITCH)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = frame_min_y
        prev_max_y = frame_max_y

        # Advance gyroscopic precession angles
        rot1_x += 0.052
        rot1_y += 0.078
        rot2_y += 0.091
        rot2_z += 0.063

        # Frame limiter to 72 FPS
        elapsed = ticks_diff(ticks_ms(), frame_start)
        if elapsed < TARGET_FRAME_MS:
            time.sleep_ms(TARGET_FRAME_MS - elapsed)

        fps_counter += 1
        if fps_counter >= 15:
            now = ticks_ms()
            dt = ticks_diff(now, t_last_fps)
            if dt > 0:
                fps = (fps_counter * 1000.0) / dt
                fps_display_str = "FPS: {:.1f} | Capped to 72".format(fps)
                moclcd.fill_rect(10, 10, 160, 10, 0xFFFF)
                moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)
            t_last_fps = now
            fps_counter = 0

if __name__ == "__main__":
    run()
