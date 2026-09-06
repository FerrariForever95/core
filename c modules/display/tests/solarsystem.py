# =====================================================================================
#  FILE:         solar_system_3d.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Real-time 3D Solar System Simulation on ESP32-S3:
#                - Glowing central Sun radiating omnidirectional light & solar corona
#                - Strict radial order: Mercury, Venus, Earth, Mars, Jupiter, Saturn,
#                  Uranus, Neptune with relative scaling for MCU resolution
#                - Keplerian orbital velocities: inner planets orbit rapidly, outer slowly
#                - Smooth 3D analytical sphere shaders lit directly by the central Sun
#                - Saturn rendered with its distinct tilted analytical ring system
#                - 3D perspective camera tilted down to reveal coplanar orbits & depth
#                - Painter's depth-sorted rendering blitted directly via DMA
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU clock to 240 MHz for maximum software raster throughput
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 150
FOV    = 270.0
CAM_Z  = 4.6

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

# Camera viewing angle (Tilted downward by 32 degrees to reveal planar orbits)
CAM_TILT_DEG = 32.0
CAM_COS = math.cos(math.radians(CAM_TILT_DEG))
CAM_SIN = math.sin(math.radians(CAM_TILT_DEG))

# -------------------------------------------------------------------------
# Planetary Configuration: [OrbitRadius, RelRadius, OrbitalSpeed, BaseColor(R,G,B)]
# Radii & Distances scaled perceptually so all 8 planets remain visible on 480x320
# -------------------------------------------------------------------------
# Central Sun
SUN_RADIUS = 24

PLANETS = [
    # Mercury: Small, rocky slate grey, fast orbit
    {"name": "mercury", "dist": 0.45, "r": 0.050, "speed": 0.082, "col": (0.75, 0.72, 0.70), "angle": 0.8},
    # Venus: Bright golden-cream sulfuric atmosphere
    {"name": "venus",   "dist": 0.70, "r": 0.085, "speed": 0.058, "col": (0.95, 0.82, 0.52), "angle": 2.4},
    # Earth: Vibrant Azure & Emerald
    {"name": "earth",   "dist": 1.02, "r": 0.092, "speed": 0.044, "col": (0.15, 0.52, 0.98), "angle": 4.1},
    # Mars: Rusty iron crimson/orange
    {"name": "mars",    "dist": 1.30, "r": 0.065, "speed": 0.034, "col": (0.92, 0.35, 0.18), "angle": 1.2},
    # Jupiter: Massive gas giant, ochre & cream atmospheric bands
    {"name": "jupiter", "dist": 1.75, "r": 0.220, "speed": 0.020, "col": (0.88, 0.72, 0.50), "angle": 5.2},
    # Saturn: Giant with Golden-ochre core & distinctive ring system
    {"name": "saturn",  "dist": 2.25, "r": 0.185, "speed": 0.015, "col": (0.95, 0.84, 0.56), "angle": 3.0},
    # Uranus: Pale cyan/aquamarine ice giant
    {"name": "uranus",  "dist": 2.70, "r": 0.125, "speed": 0.010, "col": (0.42, 0.88, 0.85), "angle": 0.3},
    # Neptune: Deep electric cobalt blue ice giant
    {"name": "neptune", "dist": 3.10, "r": 0.120, "speed": 0.007, "col": (0.18, 0.38, 0.92), "angle": 4.7},
]

# Random Distant Starfield Background (60 Stars)
STARS = []
for i in range(60):
    sx = (i * 113 + 23) % (BB_W - 4) + 2
    sy = (i * 149 + 37) % (BB_H - 4) + 2
    col = 0xFFFF if (i % 3 == 0) else (0x9CD3 if (i % 2 == 0) else 0x4A49)
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
def draw_starfield(buf):
    pitch = 760
    for sx, sy, col in STARS:
        offset = sy * pitch + (sx << 1)
        buf[offset]     = (col >> 8) & 0xFF
        buf[offset + 1] = col & 0xFF

@micropython.native
def render_sun_core(buf, cx: int, cy: int, r_sun: int):
    pitch = 760
    r_corona = r_sun + 7
    r2_sun = r_sun * r_sun
    r2_corona = r_corona * r_corona

    y_min = cy - r_corona
    y_max = cy + r_corona
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - cy
        dy2 = dy * dy
        offset = y * pitch

        for x in range(cx - r_corona, cx + r_corona + 1):
            if 0 <= x < 380:
                dx = x - cx
                d2 = dx * dx + dy2

                if d2 <= r2_sun:
                    # White-hot incandescent solar plasma core
                    if d2 < (r2_sun * 0.45):
                        buf[offset + (x << 1)]     = 0xFF
                        buf[offset + (x << 1) + 1] = 0xFF
                    else:
                        # Golden yellow limb
                        buf[offset + (x << 1)]     = 0xFF
                        buf[offset + (x << 1) + 1] = 0xE0
                elif d2 <= r2_corona:
                    # Glowing radial amber corona rim
                    buf[offset + (x << 1)]     = 0xFC
                    buf[offset + (x << 1) + 1] = 0x00

# -------------------------------------------------------------------------
# Analytical Planet Sphere Rasterizer (Lit from Central Sun)
# -------------------------------------------------------------------------
@micropython.native
def render_planet_sphere(buf, sx: int, sy: int, sr: int,
                         lx: float, ly: float, lz: float,
                         base_r: float, base_g: float, base_b: float):
    pitch = 760
    if sr < 2:
        # Sub-pixel fallback: simple point
        if 0 <= sx < 380 and 0 <= sy < 300:
            hi = ((int(base_r * 31.0) & 0x1F) << 3) | ((int(base_g * 63.0) >> 3) & 0x07)
            lo = (((int(base_g * 63.0) & 0x07) << 5) | (int(base_b * 31.0) & 0x1F)) & 0xFF
            offset = sy * pitch + (sx << 1)
            buf[offset] = hi
            buf[offset + 1] = lo
        return sy, sy

    sr2 = sr * sr
    inv_sr = 1.0 / float(sr)

    y_min = sy - sr
    y_max = sy + sr
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - sy
        dy2 = dy * dy
        span_w2 = sr2 - dy2
        if span_w2 >= 0:
            hw = int(math.sqrt(span_w2))
            x0 = sx - hw
            x1 = sx + hw
            if x0 < 0: x0 = 0
            if x1 >= 380: x1 = 379

            ny = -float(dy) * inv_sr
            ny2 = ny * ny
            l_dot_y = ny * ly

            offset = y * pitch + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = float(x - sx)
                nx = dx * inv_sr
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    # Directional diffuse from Sun position
                    dot_l = nx * lx + l_dot_y + nz * lz
                    diff = dot_l if dot_l > 0.0 else 0.0

                    # Solar ambient baseline
                    amb = 0.12
                    r = (amb + diff * 1.55) * base_r
                    g = (amb + diff * 1.55) * base_g
                    b = (amb + diff * 1.55) * base_b

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    buf[offset]     = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
                    buf[offset + 1] = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

                offset += 2

    return y_min, y_max

# -------------------------------------------------------------------------
# Saturn's Analytical Tilted Ring Arc Rasterizer
# -------------------------------------------------------------------------
@micropython.native
def render_saturn_rings(buf, pcx: int, pcy: int, sr: int, is_front: int):
    pitch = 760
    r_in = int(sr * 1.35)
    r_out = int(sr * 2.25)
    tilt = 0.34
    inv_tilt = 1.0 / tilt

    max_dy = int(r_out * tilt) + 1
    y_start = pcy if is_front else (pcy - max_dy)
    y_end   = (pcy + max_dy + 1) if is_front else pcy

    if y_start < 0: y_start = 0
    if y_end > 300: y_end = 300

    r_in2 = float(r_in * r_in)
    r_out2 = float(r_out * r_out)

    for y in range(y_start, y_end):
        dy = float(y - pcy)
        plane_y2 = (dy * inv_tilt) * (dy * inv_tilt)
        offset = y * pitch

        for x in range(pcx - r_out, pcx + r_out + 1):
            if 0 <= x < 380:
                dx = float(x - pcx)
                r_plane2 = dx * dx + plane_y2

                if r_in2 <= r_plane2 <= r_out2:
                    # Main ring color with subtle brightness roll-off
                    buf[offset + (x << 1)]     = 0xDE
                    buf[offset + (x << 1) + 1] = 0x54

# -------------------------------------------------------------------------
# Scene Rendering Pipeline
# -------------------------------------------------------------------------
def render_solar_system():
    frame_min_y = 300
    frame_max_y = 0

    # 1. Distant space starfield
    draw_starfield(FRAME_BUF)

    # 2. Project Sun (Dead Center: 0, 0, 0)
    sun_wz = CAM_Z
    sun_sx = 190
    sun_sy = 150
    render_sun_core(FRAME_BUF, sun_sx, sun_sy, SUN_RADIUS)

    # 3. Transform & Project All 8 Planets
    render_queue = []

    for p in PLANETS:
        # Advance orbital angle along plane
        p["angle"] += p["speed"]

        # Planetary position in coplanar XZ orbital plane
        px = math.cos(p["angle"]) * (p["dist"] * 1.15)
        pz = math.sin(p["angle"]) * (p["dist"] * 1.15)
        py = 0.0

        # Tilt scene towards camera view
        cam_x = px
        cam_y = py * CAM_COS - pz * CAM_SIN
        cam_z = py * CAM_SIN + pz * CAM_COS + CAM_Z

        inv_wz = 1.0 / cam_z
        sx = int(190 + (cam_x * FOV * inv_wz))
        sy = int(150 - (cam_y * FOV * inv_wz))
        sr = max(1, int(p["r"] * FOV * inv_wz * 1.1))

        # Solar lighting unit vector from Sun (0,0,0) to Planet in camera space
        # Inverted so dot product points toward the light source
        lx = -cam_x
        ly = -cam_y
        lz = -(cam_z - CAM_Z)
        inv_len = 1.0 / math.sqrt(lx * lx + ly * ly + lz * lz)
        lx *= inv_len
        ly *= inv_len
        lz *= inv_len

        render_queue.append((cam_z, p["name"], sx, sy, sr, lx, ly, lz, p["col"]))

    # 4. Painter's Depth Sorting (Back-to-Front: Largest Z rendered first)
    render_queue.sort(key=lambda item: item[0], reverse=True)

    # 5. Render Orbiting Bodies
    for z, name, sx, sy, sr, lx, ly, lz, col in render_queue:
        if name == "saturn":
            # Back ring arc
            render_saturn_rings(FRAME_BUF, sx, sy, sr, 0)

        p_min, p_max = render_planet_sphere(FRAME_BUF, sx, sy, sr, lx, ly, lz, col[0], col[1], col[2])
        if p_min < frame_min_y: frame_min_y = p_min
        if p_max > frame_max_y: frame_max_y = p_max

        if name == "saturn":
            # Front ring arc sweeps across planet
            render_saturn_rings(FRAME_BUF, sx, sy, sr, 1)

    # Ensure Sun region is included in bounding update
    if (sun_sy - SUN_RADIUS - 8) < frame_min_y: frame_min_y = sun_sy - SUN_RADIUS - 8
    if (sun_sy + SUN_RADIUS + 8) > frame_max_y: frame_max_y = sun_sy + SUN_RADIUS + 8

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

    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, BLACK_ROW)

        f_min, f_max = render_solar_system()

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
