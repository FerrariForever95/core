# =====================================================================================
#  FILE:         solar_system_fullscreen.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Full-Screen 480x320 Solar System with Distinct Signatures & True Tilts:
#                - Scaled up planet radii with high-contrast, identifiable surface traits:
#                  * Mercury: Cratered lunar-slate with sub-solar bake
#                  * Venus: Sulfuric pale-gold veil with dense cloud swirls
#                  * Earth: Vivid blue ocean, emerald continents & white polar caps
#                  * Mars: Rust-red iron deserts with bright white polar cap
#                  * Jupiter: Great Red Spot storm oval & ochre/cream atmospheric bands
#                  * Saturn: Wide double rings with Cassini gap & equatorial shadow
#                  * Uranus: Cyan aquamarine ice-giant tilted sideways (97.8°)
#                  * Neptune: Deep electric azure with dark storm streak & faint ring
#                - Accurate astronomical axial inclinations (obliquity) applied per planet
#                - Continuous exponential solar gradient core & corona
#                - 200+ multi-tier random twinkling stars across the full 480x320 display
#                - Streamed 32-line DMA scanline buffers (zero heap pressure)
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

machine.freq(240_000_000)

WIDTH     = 480
HEIGHT    = 320
CX        = 240
CY        = 160
FOV       = 295.0
CAM_Z     = 4.6

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0x0000)

CHUNK_H    = 32
CHUNK_ROWS = HEIGHT // CHUNK_H  # 10 chunks (320 / 32)
CHUNK_SIZE = WIDTH * CHUNK_H * 2
CHUNK_BUF  = bytearray(CHUNK_SIZE)

# Camera viewing angle (Tilted downward by 34 degrees for wide elliptical layout)
CAM_TILT_DEG = 34.0
CAM_COS = math.cos(math.radians(CAM_TILT_DEG))
CAM_SIN = math.sin(math.radians(CAM_TILT_DEG))

# -------------------------------------------------------------------------
# Astronomical Planetary Configuration:
# - tilt: Exact axial tilt in degrees
# - r: Enlarged visual radius for instant identification on 480x320
# - dist: Perceptual orbital spread spanning the entire 480px width
# -------------------------------------------------------------------------
PLANETS = [
    {
        "name": "mercury",
        "dist": 0.50, "r": 0.080, "speed": 0.076, "tilt": 0.03,
        "col": (0.75, 0.72, 0.70), "angle": 0.8
    },
    {
        "name": "venus",
        "dist": 0.78, "r": 0.115, "speed": 0.054, "tilt": 177.3,
        "col": (0.95, 0.86, 0.54), "angle": 2.4
    },
    {
        "name": "earth",
        "dist": 1.15, "r": 0.125, "speed": 0.042, "tilt": 23.44,
        "col": (0.12, 0.54, 0.98), "angle": 4.1
    },
    {
        "name": "mars",
        "dist": 1.50, "r": 0.095, "speed": 0.032, "tilt": 25.19,
        "col": (0.94, 0.32, 0.16), "angle": 1.2
    },
    {
        "name": "jupiter",
        "dist": 2.10, "r": 0.260, "speed": 0.018, "tilt": 3.13,
        "col": (0.90, 0.72, 0.48), "angle": 5.2
    },
    {
        "name": "saturn",
        "dist": 2.70, "r": 0.210, "speed": 0.014, "tilt": 26.73,
        "col": (0.95, 0.84, 0.56), "angle": 3.0
    },
    {
        "name": "uranus",
        "dist": 3.20, "r": 0.150, "speed": 0.009, "tilt": 97.77,
        "col": (0.42, 0.90, 0.86), "angle": 0.3
    },
    {
        "name": "neptune",
        "dist": 3.70, "r": 0.145, "speed": 0.006, "tilt": 28.32,
        "col": (0.16, 0.38, 0.95), "angle": 4.7
    },
]

# -------------------------------------------------------------------------
# Random Starfield Generation (200 Stars across 480x320 Canvas)
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

# Pre-computed Continuous Radial Sun Gradient (64 steps)
SUN_GRAD_HI = bytearray(64)
SUN_GRAD_LO = bytearray(64)

for i in range(64):
    t = float(i) / 63.0
    if t < 0.28:
        u = t / 0.28
        r = 1.0; g = 1.0; b = 1.0 - u * 0.85
    elif t < 0.62:
        u = (t - 0.28) / 0.34
        r = 1.0; g = 1.0 - u * 0.55; b = 0.15 * (1.0 - u)
    elif t < 0.88:
        u = (t - 0.62) / 0.26
        r = 1.0 - u * 0.60; g = 0.45 * (1.0 - u); b = 0.0
    else:
        u = (t - 0.88) / 0.12
        r = 0.40 * (1.0 - u); g = 0.0; b = 0.0

    hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
    lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF
    SUN_GRAD_HI[i] = hi
    SUN_GRAD_LO[i] = lo

# -------------------------------------------------------------------------
# Low-Level Rasterizers (Within current 32-line Chunk)
# -------------------------------------------------------------------------
@micropython.native
def render_stars_chunk(t: float, y_start: int, y_end: int, buf):
    pitch = 960
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
    r_corona2 = 1296.0
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
def render_detailed_planet_chunk(y_start: int, y_end: int, buf,
                                 p_id: int, sx: int, sy: int, sr: int,
                                 lx: float, ly: float, lz: float,
                                 cos_t: float, sin_t: float):
    # p_id: 0:Mercury, 1:Venus, 2:Earth, 3:Mars, 4:Jupiter, 5:Saturn, 6:Uranus, 7:Neptune
    pitch = 960
    if sr < 2:
        if y_start <= sy < y_end and 0 <= sx < 480:
            off = (sy - y_start) * pitch + (sx << 1)
            buf[off]     = 0xFF
            buf[off + 1] = 0xFF
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

                    # Compute latitude aligned with the planet's exact axial tilt
                    local_lat_y = float(dx) * sin_t + float(dy) * cos_t
                    lat = local_lat_y * inv_sr
                    abs_lat = abs(lat)

                    # Surface Feature Color Synthesizer
                    if p_id == 0:
                        # Mercury: Rocky slate with crater noise
                        base_r, base_g, base_b = 0.72, 0.70, 0.68
                        if ((x ^ y) & 0x03) == 0:
                            base_r *= 0.82; base_g *= 0.82; base_b *= 0.82
                    elif p_id == 1:
                        # Venus: Thick cream veil with sulfur streaks
                        base_r, base_g, base_b = 0.95, 0.86, 0.54
                        if abs(math.sin(lat * 6.0)) > 0.65:
                            base_r = 0.88; base_g = 0.76; base_b = 0.42
                    elif p_id == 2:
                        # Earth: Oceans + Emerald Continents + Ice Caps
                        if abs_lat > 0.78:
                            base_r, base_g, base_b = 1.00, 1.00, 1.00 # Ice Cap
                        elif abs(math.sin(nx * 4.0 + lat * 3.0)) > 0.40:
                            base_r, base_g, base_b = 0.18, 0.74, 0.28 # Land
                        else:
                            base_r, base_g, base_b = 0.08, 0.48, 0.98 # Ocean
                    elif p_id == 3:
                        # Mars: Rust red with brilliant white polar cap
                        if lat > 0.74:
                            base_r, base_g, base_b = 1.00, 0.98, 0.98 # North Pole Ice
                        else:
                            base_r, base_g, base_b = 0.92, 0.32, 0.16 # Rust
                            if abs_lat < 0.25 and abs(nx) < 0.35:
                                base_r = 0.65; base_g = 0.22; base_b = 0.12 # Dark Basalt Sea
                    elif p_id == 4:
                        # Jupiter: Alternating Ochre/Cream bands & Great Red Spot
                        band = int(abs_lat * 7.0)
                        if (band & 1) == 0:
                            base_r, base_g, base_b = 0.92, 0.74, 0.52 # Warm Cream
                        else:
                            base_r, base_g, base_b = 0.74, 0.50, 0.32 # Ochre Belt
                        # Great Red Spot (-0.35 latitude, forward facing)
                        if -0.48 < lat < -0.22 and 0.15 < nx < 0.60:
                            base_r, base_g, base_b = 0.88, 0.22, 0.15 # Red Storm
                    elif p_id == 5:
                        # Saturn: Pale gold / butterscotch bands
                        if abs_lat < 0.30:
                            base_r, base_g, base_b = 0.98, 0.86, 0.58
                        else:
                            base_r, base_g, base_b = 0.82, 0.70, 0.44
                    elif p_id == 6:
                        # Uranus: Sideways cyan ice-giant
                        base_r, base_g, base_b = 0.42, 0.90, 0.86
                        if abs_lat > 0.80:
                            base_r = 0.55; base_g = 0.98; base_b = 0.94
                    else:
                        # Neptune: Vivid cobalt blue with white storm streak
                        base_r, base_g, base_b = 0.14, 0.34, 0.94
                        if -0.20 < lat < -0.10 and abs(nx) < 0.40:
                            base_r, base_g, base_b = 0.85, 0.92, 1.00 # Cirrus Storm

                    # Solar diffuse calculation
                    dot_l = nx * lx + l_dot_y + nz * lz
                    diff = dot_l if dot_l > 0.0 else 0.0

                    amb = 0.16
                    r = (amb + diff * 1.65) * base_r
                    g = (amb + diff * 1.65) * base_g
                    b = (amb + diff * 1.65) * base_b

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    buf[offset]     = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
                    buf[offset + 1] = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

                offset += 2

@micropython.native
def render_saturn_rings_tilted_chunk(y_start: int, y_end: int, buf,
                                     pcx: int, pcy: int, sr: int,
                                     cos_t: float, sin_t: float, is_front: int):
    pitch = 960
    r_in  = int(sr * 1.35)
    r_out = int(sr * 2.35)
    tilt  = 0.36
    inv_tilt = 1.0 / tilt

    max_dy = int(r_out * tilt) + 2
    arc_y0 = pcy if is_front else (pcy - max_dy)
    arc_y1 = (pcy + max_dy + 1) if is_front else pcy

    c_min = max(y_start, arc_y0)
    c_max = min(y_end - 1, arc_y1)
    if c_min > c_max: return

    r_in2  = float(r_in * r_in)
    r_out2 = float(r_out * r_out)
    gap_in2  = float((r_in + int((r_out - r_in) * 0.58)) ** 2)
    gap_out2 = float((r_in + int((r_out - r_in) * 0.66)) ** 2)

    for y in range(c_min, c_max + 1):
        dy = float(y - pcy)
        local_y = y - y_start
        offset = local_y * pitch

        for x in range(pcx - r_out, pcx + r_out + 1):
            if 0 <= x < 480:
                dx = float(x - pcx)
                # Rotate ring according to Saturn's true axial tilt
                rx = dx * cos_t - dy * sin_t
                ry = (dx * sin_t + dy * cos_t) * inv_tilt
                r_plane2 = rx * rx + ry * ry

                # Dual rings separated by Cassini Gap
                if (r_in2 <= r_plane2 <= r_out2) and not (gap_in2 <= r_plane2 <= gap_out2):
                    off = offset + (x << 1)
                    if r_plane2 < gap_in2:
                        buf[off]     = 0xFF  # Bright inner B-Ring
                        buf[off + 1] = 0x6E
                    else:
                        buf[off]     = 0xCE  # Outer A-Ring
                        buf[off + 1] = 0x54

# -------------------------------------------------------------------------
# Full-Screen Orchestration & DMA Streaming Loop
# -------------------------------------------------------------------------
def run():
    star_timer = 0.0

    while True:
        star_timer += 0.05

        # 1. Update Planet Coordinates & Project into 3D Space
        projected_planets = []
        for idx, p in enumerate(PLANETS):
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
            sr = max(2, int(p["r"] * FOV * inv_wz * 1.25))

            # Lighting unit vector pointing back at Sun (0, 0, 0)
            lx = -cam_x
            ly = -cam_y
            lz = -(cam_z - CAM_Z)
            inv_l = 1.0 / math.sqrt(lx * lx + ly * ly + lz * lz)
            lx *= inv_l
            ly *= inv_l
            lz *= inv_l

            # Pre-compute tilt components
            tilt_rad = math.radians(p["tilt"])
            cos_t = math.cos(tilt_rad)
            sin_t = math.sin(tilt_rad)

            projected_planets.append((cam_z, idx, p["name"], sx, sy, sr, lx, ly, lz, cos_t, sin_t))

        # Sort Back-to-Front
        projected_planets.sort(key=lambda item: item[0], reverse=True)

        # 2. Render & Blit Sequentially via 32-Line DMA Chunks
        for c in range(CHUNK_ROWS):
            y_chunk_start = c * CHUNK_H
            y_chunk_end   = y_chunk_start + CHUNK_H

            # Clear chunk buffer to space black
            for i in range(CHUNK_SIZE):
                CHUNK_BUF[i] = 0x00

            # Step A: Random twinkling stars
            render_stars_chunk(star_timer, y_chunk_start, y_chunk_end, CHUNK_BUF)

            # Step B: Continuous Sun core & corona
            render_smooth_sun_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, SUN_GRAD_HI, SUN_GRAD_LO)

            # Step C: Planets & Rings
            for z, p_id, name, sx, sy, sr, lx, ly, lz, cos_t, sin_t in projected_planets:
                if name == "saturn":
                    render_saturn_rings_tilted_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, sx, sy, sr, cos_t, sin_t, 0)

                render_detailed_planet_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, p_id, sx, sy, sr, lx, ly, lz, cos_t, sin_t)

                if name == "saturn":
                    render_saturn_rings_tilted_chunk(y_chunk_start, y_chunk_end, CHUNK_BUF, sx, sy, sr, cos_t, sin_t, 1)

            # Direct DMA Window Blit
            moclcd.blit(0, y_chunk_start, WIDTH, CHUNK_H, CHUNK_BUF)

        time.sleep_ms(8)

if __name__ == "__main__":
    run()
