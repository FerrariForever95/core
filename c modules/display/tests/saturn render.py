# =====================================================================================
#  FILE:         saturn_3d.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Real-time 3D Saturn with Dual Concentric Ring System & Cassini Division:
#                - 80-facet low-poly gas sphere with atmospheric banding (Butterscotch / Gold)
#                - Concentric planar rings (Inner C-Ring, B-Ring, Cassini Gap, Outer A-Ring)
#                - Full depth-sorted occlusion: back ring segment behind the planet,
#                  front ring segment sweeping across the gas giant's equator
#                - Dramatic directional solar lighting with planet shadow cast on rings
#                - Characteristic 26.7° planetary tilt with smooth axial rotation
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
CY     = 155
FOV    = 250.0
CAM_Z  = 4.5

# Saturn's axial inclination
SATURN_TILT_DEG = 26.73

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
# Material & Solar Parameters
# -------------------------------------------------------------------------
# Gas Giant Atmosphere Bands
MAT_EQUATOR_R = 0.95
MAT_EQUATOR_G = 0.82
MAT_EQUATOR_B = 0.52

MAT_TEMPER_R  = 0.82
MAT_TEMPER_G  = 0.68
MAT_TEMPER_B  = 0.40

MAT_POLAR_R   = 0.60
MAT_POLAR_G   = 0.55
MAT_POLAR_B   = 0.38

# Ring System Radiance
MAT_RING_B_R  = 0.92  # Dense, reflective Main B-Ring
MAT_RING_B_G  = 0.84
MAT_RING_B_B  = 0.60

MAT_RING_A_R  = 0.72  # Outer A-Ring
MAT_RING_A_G  = 0.64
MAT_RING_A_B  = 0.48

# Directional Sunlight (Top-Right-Front)
SUN_DX = 0.65
SUN_DY = 0.55
SUN_DZ = -0.52
inv_sun = 1.0 / math.sqrt(SUN_DX * SUN_DX + SUN_DY * SUN_DY + SUN_DZ * SUN_DZ)
SUN_DX *= inv_sun
SUN_DY *= inv_sun
SUN_DZ *= inv_sun

# -------------------------------------------------------------------------
# Planet Sphere Construction (80-Facet Geodesic)
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

GEODESIC_TRIS = []
for v0, v1, v2 in BASE_TRIS:
    a = get_midpoint(v0, v1)
    b = get_midpoint(v1, v2)
    c = get_midpoint(v2, v0)
    GEODESIC_TRIS.append((v0, a, c))
    GEODESIC_TRIS.append((v1, b, a))
    GEODESIC_TRIS.append((v2, c, b))
    GEODESIC_TRIS.append((a, b, c))

# Assign atmospheric bands based on latitude
SATURN_SPHERE = []
for f0, f1, f2 in GEODESIC_TRIS:
    v0, v1, v2 = SPHERE_VERTS[f0], SPHERE_VERTS[f1], SPHERE_VERTS[f2]
    cy_mid = (v0[1] + v1[1] + v2[1]) * 0.3333
    lat = abs(cy_mid)
    if lat < 0.32:
        band = 0  # Bright Creamy Equator
    elif lat < 0.72:
        band = 1  # Golden Ochre Temperate Band
    else:
        band = 2  # Darker Polar Storm Cap
    SATURN_SPHERE.append((f0, f1, f2, band))

# -------------------------------------------------------------------------
# Saturn's Concentric Rings Construction (Planar Disks with Cassini Division)
# -------------------------------------------------------------------------
# Ring Radii: Inner B = 1.35, Outer B = 1.85, Inner A = 1.98, Outer A = 2.30
RING_SECTORS = 24
RING_VERTS = []
RING_FACES = []

for i in range(RING_SECTORS):
    angle = (2.0 * math.pi * i) / RING_SECTORS
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    # 4 concentric ring vertices per sector on the equatorial XZ plane
    RING_VERTS.append((cos_a * 1.35, 0.0, sin_a * 1.35))  # 0: Inner B
    RING_VERTS.append((cos_a * 1.85, 0.0, sin_a * 1.85))  # 1: Outer B
    RING_VERTS.append((cos_a * 1.98, 0.0, sin_a * 1.98))  # 2: Inner A (after Cassini Gap)
    RING_VERTS.append((cos_a * 2.30, 0.0, sin_a * 2.30))  # 3: Outer A

for i in range(RING_SECTORS):
    cur = i * 4
    nxt = ((i + 1) % RING_SECTORS) * 4

    # Main B-Ring Quads (Split into 2 Triangles, Material: 3)
    RING_FACES.append((cur + 0, nxt + 0, cur + 1, 3))
    RING_FACES.append((nxt + 0, nxt + 1, cur + 1, 3))

    # Outer A-Ring Quads (Split into 2 Triangles, Material: 4)
    # The 1.85 -> 1.98 gap is skipped to render the empty Cassini Division!
    RING_FACES.append((cur + 2, nxt + 2, cur + 3, 4))
    RING_FACES.append((nxt + 2, nxt + 3, cur + 3, 4))

# Starfield
STARS = []
for i in range(50):
    sx = (i * 113 + 19) % 376 + 2
    sy = (i * 149 + 37) % 296 + 2
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
# 3D Saturn Pipeline
# -------------------------------------------------------------------------
def render_saturn_scene(rot_deg: float):
    rot_rad = math.radians(rot_deg)
    tilt_rad = math.radians(SATURN_TILT_DEG)

    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
    cos_t, sin_t = math.cos(tilt_rad), math.sin(tilt_rad)

    # 1. Transform Planet Sphere Vertices
    sphere_world = []
    sphere_screen = []
    for vx, vy, vz in SPHERE_VERTS:
        vx *= 1.15
        vy *= 1.15
        vz *= 1.15

        rx = vx * cos_r + vz * sin_r
        ry = vy
        rz = -vx * sin_r + vz * cos_r

        tx = rx * cos_t - ry * sin_t
        ty = rx * sin_t + ry * cos_t
        tz = rz

        wz = tz + CAM_Z
        sphere_world.append((tx, ty, wz))
        inv_wz = 1.0 / wz
        sx = int(190 + (tx * FOV * inv_wz))
        sy = int(150 - (ty * FOV * inv_wz))
        sphere_screen.append((sx, sy))

    # 2. Transform Planetary Ring Vertices
    ring_world = []
    ring_screen = []
    for vx, vy, vz in RING_VERTS:
        vx *= 1.15
        vy *= 1.15
        vz *= 1.15

        rx = vx * cos_r + vz * sin_r
        ry = vy
        rz = -vx * sin_r + vz * cos_r

        tx = rx * cos_t - ry * sin_t
        ty = rx * sin_t + ry * cos_t
        tz = rz

        wz = tz + CAM_Z
        ring_world.append((tx, ty, wz))
        inv_wz = 1.0 / wz
        sx = int(190 + (tx * FOV * inv_wz))
        sy = int(150 - (ty * FOV * inv_wz))
        ring_screen.append((sx, sy))

    frame_min_y = 300
    frame_max_y = 0

    # Draw Deep Space Stars
    pitch = 760
    for sx, sy, col in STARS:
        offset = sy * pitch + (sx << 1)
        FRAME_BUF[offset] = (col >> 8) & 0xFF
        FRAME_BUF[offset + 1] = col & 0xFF

    render_queue = []

    # 3. Process Gas Giant Sphere Triangles
    for f0, f1, f2, band in SATURN_SPHERE:
        p0, p1, p2 = sphere_world[f0], sphere_world[f1], sphere_world[f2]
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

            dot_s = nx * SUN_DX + ny * SUN_DY - nz * SUN_DZ
            diff = (dot_s * 1.35) if dot_s > 0.0 else 0.0
            amb = 0.10

            if band == 0:
                r = (amb + diff) * MAT_EQUATOR_R
                g = (amb + diff) * MAT_EQUATOR_G
                b = (amb + diff) * MAT_EQUATOR_B
            elif band == 1:
                r = (amb + diff) * MAT_TEMPER_R
                g = (amb + diff) * MAT_TEMPER_G
                b = (amb + diff) * MAT_TEMPER_B
            else:
                r = (amb + diff) * MAT_POLAR_R
                g = (amb + diff) * MAT_POLAR_G
                b = (amb + diff) * MAT_POLAR_B

            if r > 1.0: r = 1.0
            if g > 1.0: g = 1.0
            if b > 1.0: b = 1.0

            hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
            lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

            er, eg, eb = r * 0.74, g * 0.74, b * 0.74
            e_hi = ((int(er * 31.0) & 0x1F) << 3) | ((int(eg * 63.0) >> 3) & 0x07)
            e_lo = (((int(eg * 63.0) & 0x07) << 5) | (int(eb * 31.0) & 0x1F)) & 0xFF

            avg_z = (p0[2] + p1[2] + p2[2]) * 0.3333
            tri_pts = (sphere_screen[f0], sphere_screen[f1], sphere_screen[f2])
            render_queue.append((avg_z, tri_pts, hi, lo, e_hi, e_lo))

    # 4. Process Rings (Both sides visible: double-sided lighting)
    # Ring normal is aligned with Saturn's tilted rotational axis
    r_nx = -sin_t
    r_ny = cos_t
    r_nz = 0.0

    dot_ring = r_nx * SUN_DX + r_ny * SUN_DY - r_nz * SUN_DZ
    ring_diff = abs(dot_ring) * 1.40

    for f0, f1, f2, mat_type in RING_FACES:
        p0, p1, p2 = ring_world[f0], ring_world[f1], ring_world[f2]
        avg_x = (p0[0] + p1[0] + p2[0]) * 0.3333
        avg_y = (p0[1] + p1[1] + p2[1]) * 0.3333
        avg_z = (p0[2] + p1[2] + p2[2]) * 0.3333

        # Approximate planet shadow cast onto rings
        # (Points behind the planet sphere in the negative light ray direction)
        in_shadow = False
        if avg_z > CAM_Z and (avg_x * avg_x + avg_y * avg_y) < 1.4:
            in_shadow = True

        amb = 0.08
        diff = 0.0 if in_shadow else ring_diff

        if mat_type == 3:
            # Main B-Ring (Bright, Dense)
            r = (amb + diff) * MAT_RING_B_R
            g = (amb + diff) * MAT_RING_B_G
            b = (amb + diff) * MAT_RING_B_B
        else:
            # Outer A-Ring
            r = (amb + diff) * MAT_RING_A_R
            g = (amb + diff) * MAT_RING_A_G
            b = (amb + diff) * MAT_RING_A_B

        if r > 1.0: r = 1.0
        if g > 1.0: g = 1.0
        if b > 1.0: b = 1.0

        hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
        lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

        er, eg, eb = r * 0.82, g * 0.82, b * 0.82
        e_hi = ((int(er * 31.0) & 0x1F) << 3) | ((int(eg * 63.0) >> 3) & 0x07)
        e_lo = (((int(eg * 63.0) & 0x07) << 5) | (int(eb * 31.0) & 0x1F)) & 0xFF

        tri_pts = (ring_screen[f0], ring_screen[f1], ring_screen[f2])
        render_queue.append((avg_z, tri_pts, hi, lo, e_hi, e_lo))

    # 5. Full Scene Painter's Sort (Back Ring -> Planet -> Front Ring)
    render_queue.sort(key=lambda item: item[0], reverse=True)

    for _, pts, hi, lo, e_hi, e_lo in render_queue:
        t_min, t_max = raster_triangle(pts[0], pts[1], pts[2], hi, lo, e_hi, e_lo)
        if t_min < frame_min_y: frame_min_y = t_min
        if t_max > frame_max_y: frame_max_y = t_max

    return frame_min_y, frame_max_y

# -------------------------------------------------------------------------
# Execution Loop
# -------------------------------------------------------------------------
def run():
    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = BLACK_ROW

    rot_angle = 0.0

    # Initial frame blit
    f_min, f_max = render_saturn_scene(rot_angle)
    blit_top = max(0, f_min)
    blit_bottom = min(299, f_max)
    blit_h = blit_bottom - blit_top + 1
    start_offset = blit_top * ROW_PITCH
    end_offset = start_offset + (blit_h * ROW_PITCH)
    moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])
    prev_min_y = f_min
    prev_max_y = f_max

    time.sleep_ms(400)

    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, BLACK_ROW)

        rot_angle = (rot_angle + 2.0) % 360.0

        f_min, f_max = render_saturn_scene(rot_angle)

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
