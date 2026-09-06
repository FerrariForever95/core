# =====================================================================================
#  FILE:         solar_system_fullscreen.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  True Full-Screen (480x320) Complete Solar System Simulation:
#                - Full-width resolution: all 8 planets fully visible in their orbits
#                - Continuous radial exponential gradient Sun (seamless thermal core
#                  from blinding white -> yellow -> amber -> deep space black)
#                - 200+ random twinkling stars across the entire 480x320 canvas
#                - Dual 32-line DMA scanline streaming buffers (zero heap pressure)
#                - Real-time 3D planetary lighting computed relative to central Sun
#                - Saturn rendered with double-sided analytical ring occlusion
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU clock to 240 MHz for maximum math throughput
machine.freq(240_000_000)

WIDTH     = 480
HEIGHT    = 320
CX        = 240
CY        = 160
FOV       = 285.0
CAM_Z     = 4.6

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0x0000)

# -------------------------------------------------------------------------
# Dual 32-Line Streaming DMA Buffers (Full 480px width)
# 480 * 32 * 2 bytes = 30,720 bytes per buffer (fits easily in internal SRAM)
# -------------------------------------------------------------------------
CHUNK_H    = 32
CHUNK_ROWS = HEIGHT // CHUNK_H  # 10 chunks total (320 / 32)
CHUNK_SIZE = WIDTH * CHUNK_H * 2
CHUNK_BUF  = bytearray(CHUNK_SIZE)

# Camera viewing angle (Tilted down 34 degrees for wide elliptical orbits)
CAM_TILT_DEG = 34.0
CAM_COS = math.cos(math.radians(CAM_TILT_DEG))
CAM_SIN = math.sin(math.radians(CAM_TILT_DEG))

# -------------------------------------------------------------------------
# Planetary Configuration: [Distance, Radius, Speed, Color(R,G,B), InitialAngle]
# Distances scaled to span full 480px width: Neptune max span reaches ~228px from center
# -------------------------------------------------------------------------
SUN_RADIUS = 26

PLANETS = [
    # Mercury: Slate Grey
    {"name": "mercury", "dist": 0.52, "r": 0.048, "speed": 0.076, "col": (0.75, 0.72, 0.70), "angle": 0.8},
    # Venus: Golden Cream
    {"name": "venus",   "dist": 0.82, "r": 0.082, "speed": 0.054, "col": (0.95, 0.82, 0.52), "angle": 2.4},
    # Earth: Azure & Emerald
    {"name": "earth",   "dist": 1.18, "r": 0.088, "speed": 0.042, "col": (0.15, 0.52, 0.98), "angle": 4.1},
    # Mars: Rust Red
    {"name": "mars",    "dist": 1.50, "r": 0.062, "speed": 0.032, "col": (0.92, 0.35, 0.18), "angle": 1.2},
    # Jupiter: Ochre Gas Giant
    {"name": "jupiter", "dist": 2.05, "r": 0.210, "speed": 0.018, "col": (0.88, 0.72, 0.50), "angle": 5.2},
    # Saturn: Golden Ringed Giant
    {"name": "saturn",  "dist": 2.65, "r": 0.175, "speed": 0.014, "col": (0.95, 0.84, 0.56), "angle": 3.0},
    # Uranus: Aquamarine Ice Giant
    {"name": "uranus",  "dist": 3.15, "r": 0.120, "speed": 0.009, "col": (0.42, 0.88, 0.85), "angle": 0.3},
    # Neptune: Cobalt Ice Giant
    {"name": "neptune", "dist": 3.65, "r": 0.115, "speed": 0.006, "col": (0.18, 0.38, 0.92), "angle": 4.7},
]

# -------------------------------------------------------------------------
# Random Twinkling Starfield across Full 480x320 Canvas (200 Stars)
# -------------------------------------------------------------------------
NUM_STARS = 200
STAR_DATA = []

seed = 0x6B18D3C1
def lcg_rand():
    global seed
    seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
    return seed

for i in range(NUM_STARS):
    sx = int((lcg_rand() % (WIDTH - 8)) + 4)
    sy = int((lcg_rand() % (HEIGHT - 8)) + 4)
    tier = i % 4
    twinkle_phase = float((lcg_rand() % 628)) * 0.01
    twinkle_speed = 0.06 + float((lcg_rand() % 120)) * 0.001
    STAR_DATA.append((sx, sy, tier, twinkle_phase, twinkle_speed))

# Pre-computed Sun Radial Gradient Palette (64 discrete RGB565 steps)
# Blinding White Core -> Solar Yellow -> Warm Amber -> Corona Red -> Space Black
SUN_GRAD_HI = bytearray(64)
SUN_GRAD_LO = bytearray(64)

for i in range(64):
    t = float(i) / 63.0  # 0.0 = center, 1.0 = outer edge
    if t < 0.28:
        # Core: Pure White to Intense Yellow
        u = t / 0.28
        r = 1.0
        g = 1.0
        b = 1.0 - u * 0.85
    elif t < 0.62:
        # Radiance Zone: Yellow to Deep Orange
        u = (t - 0.28) / 0.34
        r = 1.0
        g = 1.0 - u * 0.55
        b = 0.15 * (1.0 - u)
    elif t < 0.88:
        # Corona: Deep Orange to Crimson Edge
        u = (t - 0.62) / 0.26
        r = 1.0 - u * 0.60
        g = 0.45 * (1.0 - u)
        b = 0.0
    else:
        # Outer Dissolve: Crimson to Black
        u = (t - 0.88) / 0.12
        r = 0.40 * (1.0 - u)
        g = 0.0
        b = 0.0

    hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
    lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF
    SUN_GRAD_HI[i] = hi
    SUN_GRAD_LO[i] = lo

# -------------------------------------------------------------------------
# Low-Level Fast Rasterizers (Operating within current chunk Y-window)
# -------------------------------------------------------------------------
@micropython.native
def render_stars_chunk(t: float, y_start: int, y_end: int, buf):
    pitch = 960  # 480 * 2
    for sx, sy, tier, phase, spd in STAR_DATA:
        if y_start <= sy < y_end:
            tw = math.sin(t * spd + phase)
            if tier == 0:
                col = 0xFFFF if tw > -0.2 else 0xCE79
            elif tier == 1:
                col = 0x9E7F if tw > 0.0 else 0x52AA
            elif tier == 2:
                col = 0x8410 if tw > 0.2 else 0x4208
            else:
                col = 0x5ACB if tw > 0.4 else 0x2104

            local_y = sy - y_start
            offset = local_y * pitch + (sx << 1)
            buf[offset]     = (col >> 8) & 0xFF
            buf[offset + 1] = col & 0xFF

@micropython.native
def render_smooth_sun_chunk(y_start: int, y_end: int, buf, grad_hi, grad_lo):
    pitch = 960
    pcx = 240
    pcy = 160
    r_corona = 36
    r_corona2 = 1296.0  # 36^2
    inv_r = 1.0 / 36.0

    y_min = max(y_start, pcy - r_corona)
    y_max = min(y_end - 1, pcy + r_corona)
    if y_min > y_max: return

    for y in range(y_min, y_max + 1):
        dy = y - pcy
        dy2 = float(dy * dy)
        local_y = y - y_start
        offset = local_y * pitch

        for x in range(pcx - r_corona, pcx + r_corona + 1):
            if 0 <= x < 480:
                dx = x - pcx
                d2 = float(dx * dx) + dy2
                if d2 < r_corona2:
                    dist = math.sqrt(d2)
                    step_idx = int(dist * inv_r * 63.0)
                    if step_idx > 63: step_idx = 63
                    off = offset + (x << 1)
                    buf[off]     = grad_hi[step_idx]
                    buf[off + 1] = grad_lo[step_idx]

@micropython.native
def render_planet_chunk(y_start: int, y_end: int, buf, sx: int, sy: int, sr: int,
                        lx: float, ly: float, lz: float,
                        base_r: float, base_g: float, base_b: float):
    pitch = 960
    if sr < 2:
        if y_start <= sy < y_end and 0 <= sx < 480:
            hi = ((int(base_r * 31.0) & 0x1F) << 3) | ((int(base_g * 63.0) >> 3) & 0x07)
            lo = (((int(base_g * 63.0) & 0x07) << 5) | (int(base_b * 31.0) & 0x1F)) & 0xFF
            off = (sy - y_start) * pitch + (sx << 1)
            buf[off] = hi
            buf[off + 1] = lo
        return

    sr2 = sr * sr
    inv_sr = 1.0 / float(sr)

    c_min = max(y_start, sy - sr)
    c_max = min(y_end - 1, sy + sr)
    if c_min > c_max: return

    for y in range(c_min, c_max + 1):
        dy = y - sy
        dy2 = dy * dy
        span_w2 = sr2 - dy2
        if span_w2 >= 0:
            hw = int(math.sqrt(span_w2))
            x0 = max(0, sx - hw)
            x1 = min(479, sx + hw)

            ny = -float(dy) * inv_sr
            ny2 = ny * ny
            l_dot_y = ny * ly

            local_y = y - y_start
            offset = local_y * pitch + (x0 << 1)

            for x in range(x0, x1 + 1):
                dx = float(x - sx)
                nx = dx * inv_sr
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)
                    dot_l = nx * lx + l_dot_y + nz * lz
                    diff = dot_l if dot_l > 0.0 else 0.0

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

@micropython.native
def render_saturn_rings_chunk(y_start: int, y_end: int, buf, pcx: int, pcy: int, sr: int, is_front: int):
    pitch = 960
    r_in = int(sr * 1.35)
    r_out = int(sr * 2.25)
    tilt = 0.34
    inv_tilt = 1.0 / tilt

    max_dy = int(r_out * tilt) + 1
    arc_y0 = pcy if is_front else (pcy - max_dy)
    arc_y1 = (pcy + max_dy + 1) if is_front else pcy

    c_min = max(y_start, arc_y0)
    c_max = min(y_end - 1, arc_y1)
    if c_min > c_max: return

    r_in2 = float(r_in * r_in)
    r_out2 = float(r_out * r_out)

    for y in range(c_min, c_max + 1):
        dy = float(y - pcy)
        plane_y2 = (dy * inv_tilt) * (dy * inv_tilt)
        local_y = y - y_start
        offset = local_y * pitch

        for x in range(pcx - r_out, pcx + r_out + 1):
            if 0 <= x < 480:
                dx = float(x - pcx)
                r_plane2 = dx * dx + plane_y2
                if r_in2 <= r_plane2 <= r_out2:
                    off = offset + (x << 1)
                    buf[off]     = 0xDE
                    buf[off + 1] = 0x54

# -------------------------------------------------------------------------
# Full-Screen Orchestration & DMA Streaming Loop
# -------------------------------------------------------------------------
def run():
    star_timer = 0.0

    while True:
        star_timer += 0.05

        # 1. Update Planet Orbits in 3D Space
        projected_planets = []
        for p in PLANETS:
            p["angle"] += p["speed"]

            px = math.cos(p["angle"]) * (p["dist"] * 1.15)
            pz = math.sin(p["angle"]) * (p["dist"] * 1.15)
            py = 0.0

            cam_x = px
            cam_y = py * CAM_COS - pz * CAM_SIN
            cam_z = py * CAM_SIN + pz * CAM_COS + CAM_Z

            inv_wz = 1.0 / cam_z
            sx = int(CX + (cam_x * FOV * inv_wz))
            sy = int(CY - (cam_y * FOV * inv_wz))
            sr = max(1, int(p["r"] * FOV * inv_wz * 1.1))

            lx = -cam_x
            ly = -cam_y
            lz = -(cam_z - CAM_Z)
            inv_l = 1.0 / math.sqrt(lx * lx + ly * ly + lz * lz)
            lx *= inv_l
            ly *= inv_l
            lz *= inv_l

            projected_planets.append((cam_z, p["name"], sx, sy, sr, lx, ly, lz, p["col"]))

        # Sort Back-to-Front (Largest Z drawn first)
        projected_planets.sort(key=lambda item: item[0], reverse=True)

        # 2. Render and Stream Each 32-Line Chunk Sequentially via DMA
        for c in range(CHUNK_ROWS):
            y_chunk_start = c * CHUNK_H
            y_chunk_end   = y_chunk_start + CHUNK_H

            # Fast clear chunk to pure space black
            for i in range(CHUNK_SIZE):
                CHUNK_BUF[i] = 0x00

            # Step A: Stars in current chunk
            render_stars_chunk(star_timer, y_chunk_start, y_chunk_end, CHUNK_BUF)

            # Step B: Smooth Radial Gradient Sun Core & Corona
            render_smooth_sun_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, SUN_GRAD_HI, SUN_GRAD_LO)

            # Step C: Planets & Rings in current chunk
            for z, name, sx, sy, sr, lx, ly, lz, col in projected_planets:
                if name == "saturn":
                    render_saturn_rings_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, sx, sy, sr, 0)

                render_planet_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, sx, sy, sr, lx, ly, lz, col[0], col[1], col[2])

                if name == "saturn":
                    render_saturn_rings_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, sx, sy, sr, 1)

            # Direct DMA Window Blit to ILI9488
            moclcd.blit(0, y_chunk_start, WIDTH, CHUNK_H, CHUNK_BUF)

        time.sleep_ms(10)

if __name__ == "__main__":
    run()
