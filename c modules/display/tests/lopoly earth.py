# =====================================================================================
#  FILE:         earth_cinematic.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Cinematic 3D Low-Poly Earth with Strong Directional Sunlight:
#                - Clean display (no HUD/debug text overlays)
#                - Accurate continental geometry (Africa, Europe, Americas, Asia,
#                  India subcontinent, and Polar Ice Caps)
#                - High-contrast solar terminator (dramatic day-to-night division)
#                - Ocean specular sun-glint and night-side atmospheric dropoff
#                - Deep-space starfield background
#                - Smooth 23.5-degree axial spin loop
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 160
FOV    = 270.0
CAM_Z  = 4.2

EARTH_TILT_DEG = 23.44

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0x0000)

BB_W = 380
BB_H = 300
BB_X = CX - (BB_W // 2)
BB_Y = 10
ROW_PITCH = BB_W * 2

FRAME_BUF = bytearray(BB_W * BB_H * 2)
BLACK_ROW = bytearray([0x00] * ROW_PITCH)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

# -------------------------------------------------------------------------
# Material & Solar Radiance Parameters
# -------------------------------------------------------------------------
# Oceans: Deep Azure Blue
MAT_OCEAN_R = 0.05
MAT_OCEAN_G = 0.35
MAT_OCEAN_B = 0.95

# Continents: Saturated Emerald / Savannah
MAT_LAND_R  = 0.15
MAT_LAND_G  = 0.78
MAT_LAND_B  = 0.22

# Polar Ice: Brilliant White
MAT_ICE_R   = 0.98
MAT_ICE_G   = 0.98
MAT_ICE_B   = 1.00

# Strong Directional Sunlight Vector (Sun from Top-Right-Front)
SUN_DX = 0.65
SUN_DY = 0.55
SUN_DZ = -0.52
inv_sun = 1.0 / math.sqrt(SUN_DX * SUN_DX + SUN_DY * SUN_DY + SUN_DZ * SUN_DZ)
SUN_DX *= inv_sun
SUN_DY *= inv_sun
SUN_DZ *= inv_sun

# -------------------------------------------------------------------------
# Geodesic Mesh Construction (Subdivided Icosahedron)
# -------------------------------------------------------------------------
PHI = (1.0 + math.sqrt(5.0)) * 0.5
RAW_VERTS = [
    (-1.0,  PHI,  0.0), ( 1.0,  PHI,  0.0), (-1.0, -PHI,  0.0), ( 1.0, -PHI,  0.0),
    ( 0.0, -1.0,  PHI), ( 0.0,  1.0,  PHI), ( 0.0, -1.0, -PHI), ( 0.0,  1.0, -PHI),
    ( PHI,  0.0, -1.0), ( PHI,  0.0,  1.0), (-PHI,  0.0, -1.0), (-PHI,  0.0,  1.0),
]

BASE_VERTS = []
for vx, vy, vz in RAW_VERTS:
    inv_r = 1.0 / math.sqrt(vx * vx + vy * vy + vz * vz)
    BASE_VERTS.append((vx * inv_r, vy * inv_r, vz * inv_r))

BASE_TRIS = [
    (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
    (1, 5, 9),  (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
    (3, 9, 4),  (3, 4, 2),  (3, 2, 6),  (3, 6, 8),  (3, 8, 9),
    (4, 9, 5),  (2, 4, 11), (6, 2, 10), (8, 6, 7),  (9, 8, 1)
]

MID_CACHE = {}
SPHERE_VERTS = list(BASE_VERTS)

def get_midpoint(i1, i2):
    key = (min(i1, i2), max(i1, i2))
    if key in MID_CACHE:
        return MID_CACHE[key]
    v1 = SPHERE_VERTS[i1]
    v2 = SPHERE_VERTS[i2]
    mx = (v1[0] + v2[0]) * 0.5
    my = (v1[1] + v2[1]) * 0.5
    mz = (v1[2] + v2[2]) * 0.5
    inv_len = 1.0 / math.sqrt(mx * mx + my * my + mz * mz)
    idx = len(SPHERE_VERTS)
    SPHERE_VERTS.append((mx * inv_len, my * inv_len, mz * inv_len))
    MID_CACHE[key] = idx
    return idx

# 80-facet geodesic sphere
GEODESIC_TRIS = []
for v0, v1, v2 in BASE_TRIS:
    a = get_midpoint(v0, v1)
    b = get_midpoint(v1, v2)
    c = get_midpoint(v2, v0)
    GEODESIC_TRIS.append((v0, a, c))
    GEODESIC_TRIS.append((v1, b, a))
    GEODESIC_TRIS.append((v2, c, b))
    GEODESIC_TRIS.append((a, b, c))

# Exact Continental Geo-Masking
# 0 = Ocean, 1 = Continent, 2 = Polar Ice Cap
EARTH_MESH = []
for f0, f1, f2 in GEODESIC_TRIS:
    v0, v1, v2 = SPHERE_VERTS[f0], SPHERE_VERTS[f1], SPHERE_VERTS[f2]
    cx = (v0[0] + v1[0] + v2[0]) * 0.3333
    cy = (v0[1] + v1[1] + v2[1]) * 0.3333
    cz = (v0[2] + v1[2] + v2[2]) * 0.3333

    lat = math.degrees(math.asin(max(-1.0, min(1.0, cy))))
    lon = math.degrees(math.atan2(cx, cz))

    if abs(lat) > 63.0:
        mat = 2  # Polar Ice
    else:
        is_land = False
        # Africa & Middle East
        if (-20.0 <= lon <= 55.0) and (-35.0 <= lat <= 38.0):
            is_land = True
        # Europe
        elif (-10.0 <= lon <= 45.0) and (36.0 <= lat <= 65.0):
            is_land = True
        # Asia & India subcontinent
        elif (45.0 <= lon <= 145.0) and (5.0 <= lat <= 65.0):
            is_land = True
        # North America
        elif (-165.0 <= lon <= -55.0) and (15.0 <= lat <= 65.0):
            is_land = True
        # South America
        elif (-85.0 <= lon <= -35.0) and (-55.0 <= lat <= 12.0):
            is_land = True
        # Australia
        elif (112.0 <= lon <= 155.0) and (-42.0 <= lat <= -10.0):
            is_land = True

        mat = 1 if is_land else 0

    EARTH_MESH.append((f0, f1, f2, mat))

# Deep Space Starfield
STARS = []
for i in range(55):
    sx = (i * 109 + 17) % 376 + 2
    sy = (i * 137 + 31) % 296 + 2
    col = 0xFFFF if (i % 3 == 0) else 0x7BEF
    STARS.append((sx, sy, col))

# -------------------------------------------------------------------------
# Low-Level Rasterizers
# -------------------------------------------------------------------------
@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, black_row):
    pitch = 760
    for y in range(y0, y1 + 1):
        offset = y * pitch
        buf[offset:offset + 760] = black_row

@micropython.native
def raster_edge(x0: int, y0: int, x1: int, y1: int, e_min, e_max):
    if y0 == y1: return
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
            if px < e_min[y]: e_min[y] = px
            if px > e_max[y]: e_max[y] = px
        curr += step

@micropython.native
def fill_spans_shaded(min_y: int, max_y: int, hi: int, lo: int, edge_hi: int, edge_lo: int, buf, e_min, e_max):
    pitch = 760
    for y in range(min_y, max_y + 1):
        xs = e_min[y]
        xe = e_max[y]
        if xs > xe: continue
        if xs < 0: xs = 0
        if xe >= 380: xe = 379

        offset = y * pitch + (xs << 1)
        cnt = xe - xs + 1

        if cnt == 1:
            buf[offset] = edge_hi
            buf[offset + 1] = edge_lo
        elif cnt > 1:
            buf[offset] = edge_hi
            buf[offset + 1] = edge_lo
            offset += 2
            for _ in range(cnt - 2):
                buf[offset] = hi
                buf[offset + 1] = lo
                offset += 2
            buf[offset] = edge_hi
            buf[offset + 1] = edge_lo

def raster_triangle(p0, p1, p2, hi: int, lo: int, edge_hi: int, edge_lo: int):
    y0, y1, y2 = p0[1], p1[1], p2[1]
    min_y = min(y0, y1, y2)
    max_y = max(y0, y1, y2)

    if min_y < 0: min_y = 0
    if max_y >= 300: max_y = 299
    if min_y > max_y: return 300, -1

    for y in range(min_y, max_y + 1):
        EDGE_MIN[y] = 9999
        EDGE_MAX[y] = -9999

    raster_edge(p0[0], y0, p1[0], y1, EDGE_MIN, EDGE_MAX)
    raster_edge(p1[0], y1, p2[0], y2, EDGE_MIN, EDGE_MAX)
    raster_edge(p2[0], y2, p0[0], y0, EDGE_MIN, EDGE_MAX)

    fill_spans_shaded(min_y, max_y, hi, lo, edge_hi, edge_lo, FRAME_BUF, EDGE_MIN, EDGE_MAX)
    return min_y, max_y

# -------------------------------------------------------------------------
# 3D Solar Pipeline
# -------------------------------------------------------------------------
def render_earth_scene(rot_deg: float):
    rot_rad = math.radians(rot_deg)
    tilt_rad = math.radians(EARTH_TILT_DEG)

    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    cos_t, sin_t = math.cos(tilt_rad), math.sin(tilt_rad)

    world_v = []
    screen_v = []

    for vx, vy, vz in SPHERE_VERTS:
        vx *= 1.55
        vy *= 1.55
        vz *= 1.55

        # Axial rotation (longitude)
        rx = vx * cos_r + vz * sin_r
        ry = vy
        rz = -vx * sin_r + vz * cos_r

        # 23.5-degree earth axis tilt
        tx = rx * cos_t - ry * sin_t
        ty = rx * sin_t + ry * cos_t
        tz = rz

        wz = tz + CAM_Z
        world_v.append((tx, ty, wz))

        inv_wz = 1.0 / wz
        sx = int(190 + (tx * FOV * inv_wz))
        sy = int(150 - (ty * FOV * inv_wz))
        screen_v.append((sx, sy))

    frame_min_y = 300
    frame_max_y = 0

    # 1. Starfield
    pitch = 760
    for sx, sy, col in STARS:
        offset = sy * pitch + (sx << 1)
        FRAME_BUF[offset] = (col >> 8) & 0xFF
        FRAME_BUF[offset + 1] = col & 0xFF

    # 2. Backface Culling & High-Contrast Solar Shading
    render_queue = []

    for f0, f1, f2, mat_type in EARTH_MESH:
        p0, p1, p2 = world_v[f0], world_v[f1], world_v[f2]
        v1x, v1y, v1z = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        v2x, v2y, v2z = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]

        nx = v1y * v2z - v1z * v2y
        ny = v1z * v2x - v1x * v2z
        nz = v1x * v2y - v1y * v2x

        if (nx * p0[0] + ny * p0[1] + nz * p0[2]) < 0.0:
            inv_len = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
            nx *= inv_len
            ny *= inv_len
            nz *= inv_len

            # Direct sunlight dot product
            dot_s = nx * SUN_DX + ny * SUN_DY - nz * SUN_DZ
            # Sharp solar terminator falloff (dark night side, intense sunlit day side)
            diff = dot_s if dot_s > 0.0 else 0.0

            # Ocean Sun Glint
            hx, hy, hz = SUN_DX, SUN_DY, SUN_DZ - 1.0
            h_inv = 1.0 / math.sqrt(hx * hx + hy * hy + hz * hz)
            dot_h = nx * (hx * h_inv) + ny * (hy * h_inv) - nz * (hz * h_inv)
            spec = (dot_h ** 12) * 1.25 if (dot_h > 0.0 and mat_type == 0 and diff > 0.05) else 0.0

            # Atmospheric ambient floor on night side
            amb = 0.06

            if mat_type == 1:
                # Land Continents
                r = (amb + 0.94 * diff) * MAT_LAND_R
                g = (amb + 0.94 * diff) * MAT_LAND_G
                b = (amb + 0.94 * diff) * MAT_LAND_B
            elif mat_type == 2:
                # Polar Ice Caps
                r = (amb + 0.94 * diff) * MAT_ICE_R
                g = (amb + 0.94 * diff) * MAT_ICE_G
                b = (amb + 0.94 * diff) * MAT_ICE_B
            else:
                # Oceans + Solar Glint
                r = (amb + 0.94 * diff) * MAT_OCEAN_R + spec
                g = (amb + 0.94 * diff) * MAT_OCEAN_G + spec
                b = (amb + 0.94 * diff) * MAT_OCEAN_B + spec

            if r > 1.0: r = 1.0
            if g > 1.0: g = 1.0
            if b > 1.0: b = 1.0

            hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
            lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

            # Subtle facet seam border
            er, eg, eb = r * 0.72, g * 0.72, b * 0.72
            e_hi = ((int(er * 31.0) & 0x1F) << 3) | ((int(eg * 63.0) >> 3) & 0x07)
            e_lo = (((int(eg * 63.0) & 0x07) << 5) | (int(eb * 31.0) & 0x1F)) & 0xFF

            avg_z = (p0[2] + p1[2] + p2[2]) * 0.3333
            tri_pts = (screen_v[f0], screen_v[f1], screen_v[f2])
            render_queue.append((avg_z, tri_pts, hi, lo, e_hi, e_lo))

    # 3. Painter's Sort
    render_queue.sort(key=lambda item: item[0], reverse=True)

    for _, pts, hi, lo, e_hi, e_lo in render_queue:
        t_min, t_max = raster_triangle(pts[0], pts[1], pts[2], hi, lo, e_hi, e_lo)
        if t_min < frame_min_y: frame_min_y = t_min
        if t_max > frame_max_y: frame_max_y = t_max

    return frame_min_y, frame_max_y

# -------------------------------------------------------------------------
# Main Execution Loop
# -------------------------------------------------------------------------
def run():
    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = BLACK_ROW

    rot_angle = 0.0

    # Initial frame blit
    f_min, f_max = render_earth_scene(rot_angle)
    blit_top = max(0, f_min)
    blit_bottom = min(299, f_max)
    blit_h = blit_bottom - blit_top + 1
    start_offset = blit_top * ROW_PITCH
    end_offset = start_offset + (blit_h * ROW_PITCH)
    moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])
    prev_min_y = f_min
    prev_max_y = f_max

    time.sleep_ms(600)

    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, BLACK_ROW)

        rot_angle = (rot_angle + 2.2) % 360.0

        f_min, f_max = render_earth_scene(rot_angle)

        blit_top = min(f_min, prev_min_y)
        blit_bottom = max(f_max, prev_max_y)
        if blit_top < 0: blit_top = 0
        if blit_bottom >= 300: blit_bottom = 299
        blit_h = blit_bottom - blit_top + 1

        start_offset = blit_top * ROW_PITCH
        end_offset = start_offset + (blit_h * ROW_PITCH)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = f_min
        prev_max_y = f_max

        time.sleep_ms(14)

if __name__ == "__main__":
    run()
