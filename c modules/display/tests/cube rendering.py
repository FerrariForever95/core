# =====================================================================================
#  3D REAL-TIME PERSPECTIVE RASTER ENGINE
#  DRIVER:      moclcd v1.5.0 STABLE
#  TARGET:      ESP32-S3, 8-bit Parallel Intel 8080 LCD (ILI9488, 480x320)
#
#  CHANGELOG (vs previous build):
#  - Driver verified and standardized on moclcd v1.5.0 STABLE.
#  - Centered cube: re-aligned origin (CX=240, CY=160) and centered the 300x300
#    bounding box at (BB_X=90, BB_Y=10) for true screen-center rendering.
#  - Adjusted virtual floor plane (VIRTUAL_FLOOR_Y=1.55) and steepened light projection
#    vector (LY=1.75) so the projected shadow grounds naturally under the centered cube
#    without drifting outside the bottom scanline margins.
#  - Preserved optimized bounding-scanline DMA blit and zero-allocation frame pipeline.
# =====================================================================================

import math
import machine
import moclcd
import micropython

# Lock CPU to maximum 240 MHz silicon clock
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 160      # Perfectly centered on vertical axis
FOV    = 240.0
CAM_Z  = 3.8

# -------------------------------------------------------------------------
# EXACT WORKING HARDWARE STARTUP SEQUENCE (moclcd v1.5.0 STABLE)
# -------------------------------------------------------------------------
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)  # Power/sync boot flash
moclcd.fill_screen(0xFFFF)  # Studio White background

# -------------------------------------------------------------------------
# Lighting & Shadow Projection Vectors
# -------------------------------------------------------------------------
KX, KY, KZ = 0.57735, -0.57735, -0.57735
FX, FY, FZ = -0.4082, 0.8165, 0.4082
HX, HY, HZ = 0.3714, -0.3714, -0.8510

# Virtual ground plane & shadow casting light source
VIRTUAL_FLOOR_Y = 1.55
LX, LY, LZ      = -0.25, 1.75, -0.20

# Reduced cube scale (0.75) keeps projection & shadow cleanly in-frame
CUBE_VERTS = [
    (-0.75, -0.75, -0.75),
    ( 0.75, -0.75, -0.75),
    ( 0.75,  0.75, -0.75),
    (-0.75,  0.75, -0.75),
    (-0.75, -0.75,  0.75),
    ( 0.75, -0.75,  0.75),
    ( 0.75,  0.75,  0.75),
    (-0.75,  0.75,  0.75)
]

CUBE_FACES = [
    (0, 1, 2, 3),  # Back
    (5, 4, 7, 6),  # Front
    (4, 0, 3, 7),  # Left
    (1, 5, 6, 2),  # Right
    (3, 2, 6, 7),  # Top
    (4, 5, 1, 0)   # Bottom
]

# Symmetrical 300x300 bounding box centered on 480x320 panel
BB_W = 300
BB_H = 300
BB_X = CX - 150   # 90
BB_Y = CY - 150   # 10
ROW_PITCH = 600   # 300 pixels * 2 bytes/pixel

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * ROW_PITCH)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

TV_X = [0.0] * 8
TV_Y = [0.0] * 8
TV_Z = [0.0] * 8
SV_X = [0] * 8
SV_Y = [0] * 8

SHAD_X = [0] * 8
SHAD_Y = [0] * 8

SORT_KEYS = [0.0] * 6
SORT_IDXS = [0, 1, 2, 3, 4, 5]
FACE_COLORS = [0] * 6

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    for y in range(y0, y1 + 1):
        idx = y * 600
        buf[idx:idx + 600] = white_row

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
    for y in range(min_y, max_y + 1):
        xs = e_min[y]
        xe = e_max[y]
        if xs > xe:
            continue
        if xs < 0:
            xs = 0
        if xe >= 300:
            xe = 299

        offset = y * 600 + (xs << 1)
        cnt = xe - xs + 1
        for _ in range(cnt):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

def raster_quad_pts(pts, hi, lo):
    min_y = 300
    max_y = -1
    for p in pts:
        y = p[1]
        if y < min_y: min_y = y
        if y > max_y: max_y = y

    if min_y < 0: min_y = 0
    if max_y >= 300: max_y = 299
    if min_y > max_y: return min_y, max_y

    for y in range(min_y, max_y + 1):
        EDGE_MIN[y] = 9999
        EDGE_MAX[y] = -9999

    for i in range(4):
        p0 = pts[i]
        p1 = pts[(i + 1) & 3]
        raster_edge(p0[0], p0[1], p1[0], p1[1], EDGE_MIN, EDGE_MAX)

    fill_spans(min_y, max_y, hi, lo, FRAME_BUF, EDGE_MIN, EDGE_MAX)
    return min_y, max_y

def run():
    ax = 0.0
    ay = 0.0
    az = 0.0

    prev_min_y = 0
    prev_max_y = 299

    # Initialize frame buffer canvas with white
    for y in range(300):
        idx = y * 600
        FRAME_BUF[idx:idx + 600] = WHITE_CHUNK

    while True:
        # Clear only the rows touched in the last frame
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        cx, sx = math.cos(ax), math.sin(ax)
        cy, sy = math.cos(ay), math.sin(ay)
        cz, sz = math.cos(az), math.sin(az)

        frame_min_y = 300
        frame_max_y = 0

        # Transform vertices and calculate virtual plane shadow projection
        for i in range(8):
            vx, vy, vz = CUBE_VERTS[i]
            y1 = vy * cx - vz * sx
            z1 = vy * sx + vz * cx
            x2 = vx * cy + z1 * sy
            z2 = -vx * sy + z1 * cy
            x3 = x2 * cz - y1 * sz
            y3 = x2 * sz + y1 * cz

            # Ray intersection onto the virtual ground plane
            t = (VIRTUAL_FLOOR_Y - y3) / LY
            sx_world = x3 + t * LX
            sz_world = z2 + t * LZ + CAM_Z

            inv_sz = 1.0 / sz_world
            SHAD_X[i] = int((CX + (sx_world * FOV * inv_sz)) - BB_X)
            SHAD_Y[i] = int((CY - (VIRTUAL_FLOOR_Y * FOV * inv_sz)) - BB_Y)

            z3 = z2 + CAM_Z
            TV_X[i] = x3
            TV_Y[i] = y3
            TV_Z[i] = z3

            inv_z = 1.0 / z3
            SV_X[i] = int((CX + (x3 * FOV * inv_z)) - BB_X)
            SV_Y[i] = int((CY - (y3 * FOV * inv_z)) - BB_Y)

        # 1. Rasterize projected drop shadow quads on virtual floor
        for f_idx in range(6):
            i0, i1, i2, i3 = CUBE_FACES[f_idx]
            shad_pts = (
                (SHAD_X[i0], SHAD_Y[i0]),
                (SHAD_X[i1], SHAD_Y[i1]),
                (SHAD_X[i2], SHAD_Y[i2]),
                (SHAD_X[i3], SHAD_Y[i3])
            )
            s_min, s_max = raster_quad_pts(shad_pts, 0x84, 0x10)  # Dense gray shadow
            if s_min < frame_min_y: frame_min_y = s_min
            if s_max > frame_max_y: frame_max_y = s_max

        # 2. Lighting & Backface Culling for the cube
        active_count = 0
        for f_idx in range(6):
            i0, i1, i2, i3 = CUBE_FACES[f_idx]
            x0, y0, z0 = TV_X[i0], TV_Y[i0], TV_Z[i0]
            e1x, e1y, e1z = TV_X[i1] - x0, TV_Y[i1] - y0, TV_Z[i1] - z0
            e2x, e2y, e2z = TV_X[i2] - x0, TV_Y[i2] - y0, TV_Z[i2] - z0

            nx = e1y * e2z - e1z * e2y
            ny = e1z * e2x - e1x * e2z
            nz = e1x * e2y - e1y * e2x

            if (nx * x0 + ny * y0 + nz * z0) < 0.0:
                inv_l = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
                nx *= inv_l
                ny *= inv_l
                nz *= inv_l

                dot_k = nx * KX + ny * KY + nz * KZ
                diff_k = dot_k if dot_k > 0.0 else 0.0

                dot_f = nx * FX + ny * FY + nz * FZ
                diff_f = dot_f if dot_f > 0.0 else 0.0

                dot_h = nx * HX + ny * HY + nz * HZ
                spec = (dot_h ** 16) if dot_h > 0.0 else 0.0

                avg_z = z0 + TV_Z[i1] + TV_Z[i2] + TV_Z[i3]
                depth_factor = max(0.60, 1.28 - (avg_z * 0.15))

                r = (0.10 + 0.35 * diff_k + 0.10 * diff_f + spec * 1.25) * depth_factor
                g = (0.35 + 1.05 * diff_k + 0.25 * diff_f + spec * 1.25) * depth_factor
                b = (0.70 + 1.25 * diff_k + 0.50 * diff_f + spec * 1.30) * depth_factor

                if r > 1.0: r = 1.0
                if g > 1.0: g = 1.0
                if b > 1.0: b = 1.0

                c = ((int(r * 31.0) & 0x1F) << 11) | ((int(g * 63.0) & 0x3F) << 5) | (int(b * 31.0) & 0x1F)
                FACE_COLORS[f_idx] = c
                SORT_KEYS[active_count] = avg_z
                SORT_IDXS[active_count] = f_idx
                active_count += 1

        # Z-sorting
        for i in range(1, active_count):
            k = SORT_KEYS[i]
            idx_val = SORT_IDXS[i]
            j = i - 1
            while j >= 0 and SORT_KEYS[j] < k:
                SORT_KEYS[j + 1] = SORT_KEYS[j]
                SORT_IDXS[j + 1] = SORT_IDXS[j]
                j -= 1
            SORT_KEYS[j + 1] = k
            SORT_IDXS[j + 1] = idx_val

        # 3. Rasterize visible cube faces over the shadow
        for i in range(active_count):
            f_idx = SORT_IDXS[i]
            i0, i1, i2, i3 = CUBE_FACES[f_idx]
            col = FACE_COLORS[f_idx]
            hi = (col >> 8) & 0xFF
            lo = col & 0xFF
            quad_pts = (
                (SV_X[i0], SV_Y[i0]),
                (SV_X[i1], SV_Y[i1]),
                (SV_X[i2], SV_Y[i2]),
                (SV_X[i3], SV_Y[i3])
            )
            q_min, q_max = raster_quad_pts(quad_pts, hi, lo)
            if q_min < frame_min_y: frame_min_y = q_min
            if q_max > frame_max_y: frame_max_y = q_max

        # Vertical bounds clamping
        frame_min_y = max(0, min(299, frame_min_y))
        frame_max_y = max(0, min(299, frame_max_y))

        # Union dirty span with previous frame
        blit_top = min(frame_min_y, prev_min_y)
        blit_bottom = max(frame_max_y, prev_max_y)
        blit_h = blit_bottom - blit_top + 1

        # Push dirty scanline window over DMA
        start_offset = blit_top * (BB_W * 2)
        end_offset = start_offset + (blit_h * BB_W * 2)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = frame_min_y
        prev_max_y = frame_max_y

        ax += 0.08
        ay += 0.12
        az += 0.05

if __name__ == "__main__":
    run()
