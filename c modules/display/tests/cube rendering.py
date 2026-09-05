import math
import machine
import moclcd
import micropython
#wokring cube rendeirng code for 1.5.0 - stable , with shaders and background
# Lock CPU to 240 MHz for maximum math throughput
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 145      # Centered with room for floor shadow
FOV    = 240.0
CAM_Z  = 3.6

# -------------------------------------------------------------------------
# EXACT WORKING HARDWARE STARTUP SEQUENCE
# -------------------------------------------------------------------------
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)  # Visual boot confirmation
moclcd.fill_screen(0xFFFF)  # Studio White Background

# -------------------------------------------------------------------------
# Lighting Setup
# -------------------------------------------------------------------------
KX, KY, KZ = 0.57735, -0.57735, -0.57735
FX, FY, FZ = -0.4082, 0.8165, 0.4082

# Blinn-Phong Halfway vector with View vector (0, 0, -1)
HX, HY, HZ = 0.3714, -0.3714, -0.8510

CUBE_VERTS = [
    (-1.0, -1.0, -1.0),
    ( 1.0, -1.0, -1.0),
    ( 1.0,  1.0, -1.0),
    (-1.0,  1.0, -1.0),
    (-1.0, -1.0,  1.0),
    ( 1.0, -1.0,  1.0),
    ( 1.0,  1.0,  1.0),
    (-1.0,  1.0,  1.0)
]

CUBE_FACES = [
    (0, 1, 2, 3),  # Back
    (5, 4, 7, 6),  # Front
    (4, 0, 3, 7),  # Left
    (1, 5, 6, 2),  # Right
    (3, 2, 6, 7),  # Top
    (4, 5, 1, 0)   # Bottom
]

# Expanded bounding box: 300x300 prevents edge clipping
BB_W = 300
BB_H = 300
BB_X = CX - 150
BB_Y = CY - 140

# 300x300 RGB565 buffer (180,000 bytes)
FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * (BB_W * 2))

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

TV_X = [0.0] * 8
TV_Y = [0.0] * 8
TV_Z = [0.0] * 8
SV_X = [0] * 8
SV_Y = [0] * 8

SORT_KEYS = [0.0] * 6
SORT_IDXS = [0, 1, 2, 3, 4, 5]
FACE_COLORS = [0] * 6

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    pitch = 600  # 300 pixels * 2 bytes
    for y in range(y0, y1 + 1):
        idx = y * pitch
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
    row_pitch = 600
    for y in range(min_y, max_y + 1):
        xs = e_min[y]
        xe = e_max[y]
        if xs > xe:
            continue
        if xs < 0:
            xs = 0
        if xe >= 300:
            xe = 299

        offset = y * row_pitch + (xs << 1)
        cnt = xe - xs + 1
        for _ in range(cnt):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

@micropython.native
def draw_drop_shadow(cx: int, cy: int, rx: int, ry: int, buf):
    """Soft, dual-tier elliptical drop shadow on the white studio floor."""
    row_pitch = 600
    rx2 = rx * rx
    ry2 = ry * ry
    if rx2 <= 0 or ry2 <= 0:
        return

    # Shadow tints in RGB565
    inner_hi, inner_lo = 0xBD, 0xD7  # ~#B8B8B8 Soft Gray
    outer_hi, outer_lo = 0xDE, 0xFB  # ~#DEDEDE Ambient Rim Gray

    for dy in range(-ry, ry + 1):
        sy = cy + dy
        if 0 <= sy < 300:
            dy2 = dy * dy
            val = 1.0 - (dy2 / ry2)
            if val > 0:
                span_x = int(rx * math.sqrt(val))
                x_start = max(0, cx - span_x)
                x_end = min(299, cx + span_x)
                offset = sy * row_pitch + (x_start << 1)

                for px in range(x_start, x_end + 1):
                    dx = px - cx
                    dist_norm = (dx * dx) / rx2 + dy2 / ry2
                    if dist_norm < 0.45:
                        buf[offset] = inner_hi
                        buf[offset + 1] = inner_lo
                    elif dist_norm < 1.0:
                        buf[offset] = outer_hi
                        buf[offset + 1] = outer_lo
                    offset += 2

def raster_quad(i0, i1, i2, i3, hi, lo):
    pts = (
        (SV_X[i0], SV_Y[i0]),
        (SV_X[i1], SV_Y[i1]),
        (SV_X[i2], SV_Y[i2]),
        (SV_X[i3], SV_Y[i3])
    )
    
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

    # Pre-fill initial frame buffer with white
    for y in range(300):
        idx = y * 600
        FRAME_BUF[idx:idx + 600] = WHITE_CHUNK

    while True:
        # Clear only the rows dirtied during the previous frame back to white
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        cx, sx = math.cos(ax), math.sin(ax)
        cy, sy = math.cos(ay), math.sin(ay)
        cz, sz = math.cos(az), math.sin(az)

        min_proj_y = 999
        max_proj_y = -999
        avg_cam_x = 0.0

        # Transform and project vertices
        for i in range(8):
            vx, vy, vz = CUBE_VERTS[i]
            y1 = vy * cx - vz * sx
            z1 = vy * sx + vz * cx
            x2 = vx * cy + z1 * sy
            z2 = -vx * sy + z1 * cy
            x3 = x2 * cz - y1 * sz
            y3 = x2 * sz + y1 * cz
            z3 = z2 + CAM_Z

            TV_X[i] = x3
            TV_Y[i] = y3
            TV_Z[i] = z3
            avg_cam_x += x3

            inv_z = 1.0 / z3
            px = int((CX + (x3 * FOV * inv_z)) - BB_X)
            py = int((CY - (y3 * FOV * inv_z)) - BB_Y)

            SV_X[i] = px
            SV_Y[i] = py

            if py < min_proj_y: min_proj_y = py
            if py > max_proj_y: max_proj_y = py

        avg_cam_x *= 0.125

        # Floor shadow beneath the cube
        shadow_floor_y = 256
        shadow_cx = int(150 + (avg_cam_x * 40.0))
        shadow_rx = 78
        shadow_ry = 18

        draw_drop_shadow(shadow_cx, shadow_floor_y, shadow_rx, shadow_ry, FRAME_BUF)

        active_count = 0

        # Compute lighting and face normals
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

                # Deep Cobalt/Azure Material (bold contrast against white)
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

        # Rasterize visible faces
        frame_min_y = min_proj_y
        frame_max_y = max(max_proj_y, shadow_floor_y + shadow_ry)

        for i in range(active_count):
            f_idx = SORT_IDXS[i]
            i0, i1, i2, i3 = CUBE_FACES[f_idx]
            col = FACE_COLORS[f_idx]
            hi = (col >> 8) & 0xFF
            lo = col & 0xFF
            raster_quad(i0, i1, i2, i3, hi, lo)

        # Clamp vertical bounds
        frame_min_y = max(0, min(299, frame_min_y))
        frame_max_y = max(0, min(299, frame_max_y))

        # Union bounds with previous frame
        blit_top = min(frame_min_y, prev_min_y)
        blit_bottom = max(frame_max_y, prev_max_y)
        blit_h = blit_bottom - blit_top + 1

        # Push dirty scanline band via DMA
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
