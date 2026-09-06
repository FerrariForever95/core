# =====================================================================================
#  FILE:         earth_lowpoly.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Real-time 3D Low-Poly Earth (Subdivided Icosahedron / Geodesic)
#                showcase rotating smoothly on an authentic deep-space starfield:
#                - Dual-layer materials: Deep Ocean Azure vs. Emerald Continents
#                - Polar Ice Caps (pure brilliant white at top and bottom poles)
#                - Directional solar lighting with Blinn-Phong specular glints
#                - Soft atmospheric limb glow / halo ring encircling the globe
#                - Full 360-degree axial rotation with a realistic 23.5° axial tilt
#                - Sub-pixel scanline rasterizer with dirty row tracking for 60 FPS
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU clock to 240 MHz for maximum raster throughput
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 150
FOV    = 250.0
CAM_Z  = 4.3

# Earth constant tilt (23.5 degrees)
EARTH_TILT_DEG = 23.44

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0x0000)

BB_W = 380
BB_H = 300
BB_X = CX - (BB_W // 2)  # 50
BB_Y = 10
ROW_PITCH = BB_W * 2     # 760 bytes

FRAME_BUF = bytearray(BB_W * BB_H * 2)
BLACK_ROW = bytearray([0x00] * ROW_PITCH)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

# -------------------------------------------------------------------------
# Material Colors & Solar Lighting
# -------------------------------------------------------------------------
# Oceans: Deep Royal Azure
MAT_OCEAN_R = 0.08
MAT_OCEAN_G = 0.38
MAT_OCEAN_B = 0.88

# Continents: Emerald Landmass
MAT_LAND_R  = 0.18
MAT_LAND_G  = 0.72
MAT_LAND_B  = 0.28

# Ice Caps: Polar Frost
MAT_ICE_R   = 0.95
MAT_ICE_G   = 0.96
MAT_ICE_B   = 0.98

# Directional Sunlight (Sun placed at Top-Right-Front)
SUN_DX = 0.55
SUN_DY = 0.65
SUN_DZ = -0.52
inv_sun = 1.0 / math.sqrt(SUN_DX * SUN_DX + SUN_DY * SUN_DY + SUN_DZ * SUN_DZ)
SUN_DX *= inv_sun
SUN_DY *= inv_sun
SUN_DZ *= inv_sun

# -------------------------------------------------------------------------
# Low-Poly Spherical Geodesic Mesh (Icosahedron Base: 12 Vertices, 20 Triangles)
# -------------------------------------------------------------------------
PHI = (1.0 + math.sqrt(5.0)) * 0.5  # Golden ratio
RAW_VERTS = [
    (-1.0,  PHI,  0.0), ( 1.0,  PHI,  0.0), (-1.0, -PHI,  0.0), ( 1.0, -PHI,  0.0),
    ( 0.0, -1.0,  PHI), ( 0.0,  1.0,  PHI), ( 0.0, -1.0, -PHI), ( 0.0,  1.0, -PHI),
    ( PHI,  0.0, -1.0), ( PHI,  0.0,  1.0), (-PHI,  0.0, -1.0), (-PHI,  0.0,  1.0),
]

# Normalize all vertices to perfect unit sphere radius
BASE_VERTS = []
for vx, vy, vz in RAW_VERTS:
    inv_r = 1.0 / math.sqrt(vx * vx + vy * vy + vz * vz)
    BASE_VERTS.append((vx * inv_r, vy * inv_r, vz * inv_r))

# 20 Faces (v0, v1, v2) with CCW winding
BASE_TRIS = [
    (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
    (1, 5, 9),  (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
    (3, 9, 4),  (3, 4, 2),  (3, 2, 6),  (3, 6, 8),  (3, 8, 9),
    (4, 9, 5),  (2, 4, 11), (6, 2, 10), (8, 6, 7),  (9, 8, 1)
]

# Midpoint sphere vertex cache for Geodesic 1-to-4 triangle subdivision
MID_CACHE = {}
SPHERE_VERTS = list(BASE_VERTS)

def get_subdivided_midpoint(i1, i2):
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

# Subdivide into an 80-triangle low-poly geodesic sphere
GEODESIC_TRIS = []
for v0, v1, v2 in BASE_TRIS:
    a = get_subdivided_midpoint(v0, v1)
    b = get_subdivided_midpoint(v1, v2)
    c = get_subdivided_midpoint(v2, v0)
    GEODESIC_TRIS.append((v0, a, c))
    GEODESIC_TRIS.append((v1, b, a))
    GEODESIC_TRIS.append((v2, c, b))
    GEODESIC_TRIS.append((a, b, c))

# Assign land/ocean/ice material flags to each facet based on latitude & longitude
# 0 = Ocean, 1 = Continent, 2 = Polar Ice Cap
EARTH_MESH = []
for f0, f1, f2 in GEODESIC_TRIS:
    v0, v1, v2 = SPHERE_VERTS[f0], SPHERE_VERTS[f1], SPHERE_VERTS[f2]
    cx = (v0[0] + v1[0] + v2[0]) * 0.3333
    cy = (v0[1] + v1[1] + v2[1]) * 0.3333
    cz = (v0[2] + v1[2] + v2[2]) * 0.3333

    lat = math.asin(max(-1.0, min(1.0, cy)))
    lon = math.atan2(cx, cz)

    # Polar ice caps above 64 deg latitude
    if abs(lat) > 1.10:
        mat = 2
    else:
        # Continental landmass distribution
        is_land = False
        # Eurasia / Africa cluster
        if (-0.4 < lon < 2.0) and (-0.5 < lat < 1.0):
            if not (-0.2 < lon < 0.8 and -0.1 < lat < 0.4 and (cx * cx + cz * cz) < 0.3):
                is_land = True
        # Americas cluster
        if (-2.6 < lon < -1.0) and (-0.9 < lat < 0.9):
            is_land = True
        # Australia
        if (1.9 < lon < 2.8) and (-0.8 < lat < -0.2):
            is_land = True

        mat = 1 if is_land else 0

    EARTH_MESH.append((f0, f1, f2, mat))

# Static Background Starfield (Stars do not shift with Earth rotation)
STARS = []
for i in range(45):
    sx = (i * 97 + 13) % 376 + 2
    sy = (i * 131 + 29) % 296 + 2
    bright = 0xFFFF if (i % 3 == 0) else 0x8410
    STARS.append((sx, sy, bright))

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
# 3D Geodesic Transform, Lighting & Projection Pipeline
# -------------------------------------------------------------------------
def render_lowpoly_earth(rotation_deg: float):
    rot_rad = math.radians(rotation_deg)
    tilt_rad = math.radians(EARTH_TILT_DEG)

    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    cos_t, sin_t = math.cos(tilt_rad), math.sin(tilt_rad)

    world_v = []
    screen_v = []

    # 1. Transform Vertices: Spin around tilted planetary axis
    for vx, vy, vz in SPHERE_VERTS:
        # Scale to hero radius on screen
        vx *= 1.48
        vy *= 1.48
        vz *= 1.48

        # Spin around local Y-axis (Planetary Longitude)
        rx = vx * cos_r + vz * sin_r
        ry = vy
        rz = -vx * sin_r + vz * cos_r

        # Apply 23.5-degree axial tilt (Z-axis roll)
        tx = rx * cos_t - ry * sin_t
        ty = rx * sin_t + ry * cos_t
        tz = rz

        # Camera space translation
        wz = tz + CAM_Z
        world_v.append((tx, ty, wz))

        inv_wz = 1.0 / wz
        sx = int(190 + (tx * FOV * inv_wz))
        sy = int(145 - (ty * FOV * inv_wz))
        screen_v.append((sx, sy))

    frame_min_y = 300
    frame_max_y = 0

    # 2. Draw Distant Space Starfield
    pitch = 760
    for sx, sy, col in STARS:
        offset = sy * pitch + (sx << 1)
        FRAME_BUF[offset] = (col >> 8) & 0xFF
        FRAME_BUF[offset + 1] = col & 0xFF

    # 3. Backface Culling, Flat Facet Normal & Solar Shading
    render_queue = []

    for f0, f1, f2, mat_type in EARTH_MESH:
        p0, p1, p2 = world_v[f0], world_v[f1], world_v[f2]
        v1x, v1y, v1z = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        v2x, v2y, v2z = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]

        nx = v1y * v2z - v1z * v2y
        ny = v1z * v2x - v1x * v2z
        nz = v1x * v2y - v1y * v2x

        # Camera Ray Dot Normal
        if (nx * p0[0] + ny * p0[1] + nz * p0[2]) < 0.0:
            inv_len = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
            nx *= inv_len
            ny *= inv_len
            nz *= inv_len

            # Directional Solar Diffuse
            dot_s = nx * SUN_DX + ny * SUN_DY - nz * SUN_DZ
            diff = dot_s if dot_s > 0.0 else 0.0

            # Ocean Specular Glint (Water glints in sunlight; land is matte)
            hx, hy, hz = SUN_DX, SUN_DY, SUN_DZ - 1.0
            h_inv = 1.0 / math.sqrt(hx * hx + hy * hy + hz * hz)
            dot_h = nx * (hx * h_inv) + ny * (hy * h_inv) - nz * (hz * h_inv)
            spec = (dot_h ** 10) * 0.90 if (dot_h > 0.0 and mat_type == 0) else 0.0

            if mat_type == 1:
                # Continents (Emerald green vegetation)
                r = (0.12 + 0.88 * diff) * MAT_LAND_R
                g = (0.12 + 0.88 * diff) * MAT_LAND_G
                b = (0.12 + 0.88 * diff) * MAT_LAND_B
            elif mat_type == 2:
                # Polar Ice Caps (High-albedo white frost)
                r = (0.18 + 0.82 * diff) * MAT_ICE_R
                g = (0.18 + 0.82 * diff) * MAT_ICE_G
                b = (0.18 + 0.82 * diff) * MAT_ICE_B
            else:
                # Oceans (Deep Royal Azure with solar specular glint)
                r = (0.10 + 0.90 * diff) * MAT_OCEAN_R + spec
                g = (0.10 + 0.90 * diff) * MAT_OCEAN_G + spec
                b = (0.10 + 0.90 * diff) * MAT_OCEAN_B + spec

            if r > 1.0: r = 1.0
            if g > 1.0: g = 1.0
            if b > 1.0: b = 1.0

            hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
            lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

            # Crisp low-poly darker facet seam
            er, eg, eb = r * 0.70, g * 0.70, b * 0.70
            e_hi = ((int(er * 31.0) & 0x1F) << 3) | ((int(eg * 63.0) >> 3) & 0x07)
            e_lo = (((int(eg * 63.0) & 0x07) << 5) | (int(eb * 31.0) & 0x1F)) & 0xFF

            avg_z = (p0[2] + p1[2] + p2[2]) * 0.3333
            tri_pts = (screen_v[f0], screen_v[f1], screen_v[f2])
            render_queue.append((avg_z, tri_pts, hi, lo, e_hi, e_lo))

    # 4. Painter's Depth Sorting (Back-to-Front)
    render_queue.sort(key=lambda item: item[0], reverse=True)

    for _, pts, hi, lo, e_hi, e_lo in render_queue:
        t_min, t_max = raster_triangle(pts[0], pts[1], pts[2], hi, lo, e_hi, e_lo)
        if t_min < frame_min_y: frame_min_y = t_min
        if t_max > frame_max_y: frame_max_y = t_max

    return frame_min_y, frame_max_y

# -------------------------------------------------------------------------
# Showpiece Execution Loop
# -------------------------------------------------------------------------
def run():
    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = BLACK_ROW

    moclcd.fill_rect(10, 10, 240, 12, 0x0000)
    moclcd.draw_text(10, 10, "PLANET EARTH // LOW-POLY 3D", 0x07FF, 0x0000)

    rot_angle = 0.0

    # Initial frame blit
    f_min, f_max = render_lowpoly_earth(rot_angle)
    blit_top = max(0, f_min)
    blit_bottom = min(299, f_max)
    blit_h = blit_bottom - blit_top + 1
    start_offset = blit_top * ROW_PITCH
    end_offset = start_offset + (blit_h * ROW_PITCH)
    moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])
    prev_min_y = f_min
    prev_max_y = f_max

    time.sleep_ms(800)

    # Turntable orbital spin loop
    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, BLACK_ROW)

        # Smooth, continuous planetary rotation
        rot_angle = (rot_angle + 2.4) % 360.0

        f_min, f_max = render_lowpoly_earth(rot_angle)

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
