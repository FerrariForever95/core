# =====================================================================================
#  FILE:         saturn_analytic.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  High-Resolution Smooth Analytical Sphere Saturn with Orbital Rings:
#                - Perfectly smooth mathematical sphere (no polygonal facet edges)
#                - Top-down 35° camera pitch angle matching your schematic perspective
#                - Ultra-wide concentric rings with distinct Cassini division
#                - High-intensity directional sunlight with Blinn-Phong specular flare
#                - Exact depth occlusion: rear rings go behind the sphere, front rings
#                  cross directly in front of the lower hemisphere
#                - Sharp planet shadow cast across the rear rings
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
CY     = 150

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

# -------------------------------------------------------------------------
# Dimensions & Perspective Settings (Matches user diagram)
# -------------------------------------------------------------------------
SPHERE_R     = 68        # Planet radius in pixels
RING_IN_B    = 88        # Main inner ring radius
RING_OUT_B   = 136       # Main outer ring radius
RING_IN_A    = 145       # Outer ring start (Cassini gap: 136..145)
RING_OUT_A   = 178       # Outer ring boundary

# Camera tilt angle from top (flattens rings into broad ellipses)
TILT_FACTOR  = 0.38      # Vertical compression of rings (Y radius / X radius)

# High-Intensity Sunlight Vector (from Top-Right-Front)
SUN_LX = 0.62
SUN_LY = 0.62
SUN_LZ = -0.48
inv_l = 1.0 / math.sqrt(SUN_LX * SUN_LX + SUN_LY * SUN_LY + SUN_LZ * SUN_LZ)
SUN_LX *= inv_l
SUN_LY *= inv_l
SUN_LZ *= inv_l

# Blinn-Phong Specular Halfway Vector
HX = SUN_LX
HY = SUN_LY
HZ = SUN_LZ - 1.0
inv_h = 1.0 / math.sqrt(HX * HX + HY * HY + HZ * HZ)
HX *= inv_h
HY *= inv_h
HZ *= inv_h

# -------------------------------------------------------------------------
# Starfield (Deep Space)
# -------------------------------------------------------------------------
STARS = []
for i in range(55):
    sx = (i * 113 + 19) % 376 + 2
    sy = (i * 149 + 37) % 296 + 2
    col = 0xFFFF if (i % 3 == 0) else 0x7BEF
    STARS.append((sx, sy, col))

# -------------------------------------------------------------------------
# Low-Level Fast Rasterizers
# -------------------------------------------------------------------------
@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, black_row):
    pitch = 760
    for y in range(y0, y1 + 1):
        offset = y * pitch
        buf[offset:offset + 760] = black_row

@micropython.native
def render_starfield(buf):
    pitch = 760
    for sx, sy, col in STARS:
        offset = sy * pitch + (sx << 1)
        buf[offset] = (col >> 8) & 0xFF
        buf[offset + 1] = col & 0xFF

# -------------------------------------------------------------------------
# Analytical Ring Section Rasterizer (Back or Front Arc)
# -------------------------------------------------------------------------
@micropython.native
def render_rings_arc(is_front: int, buf):
    pitch = 760
    pcx = 190
    pcy = 150

    # Vertical bounds of the projected ring system
    max_y_extent = int(178.0 * 0.38) + 2
    y_start = pcy if is_front else (pcy - max_y_extent)
    y_end   = (pcy + max_y_extent + 1) if is_front else pcy

    if y_start < 0: y_start = 0
    if y_end > 300: y_end = 300

    inv_tilt = 1.0 / 0.38
    inv_sun_lx = 0.62
    inv_sun_ly = 0.62

    for y in range(y_start, y_end):
        dy = y - pcy
        # De-project vertical pixel coordinate to equatorial plane distance
        plane_y = float(dy) * inv_tilt
        plane_y2 = plane_y * plane_y

        offset = y * pitch
        for x in range(380):
            dx = float(x - pcx)
            r_plane2 = dx * dx + plane_y2

            # Check if current pixel lies within Ring B or Ring A
            # B-Ring: 88^2 (7744) to 136^2 (18496)
            # A-Ring: 145^2 (21025) to 178^2 (31684)
            in_ring = 0
            ring_base = 0.0

            if 7744.0 <= r_plane2 <= 18496.0:
                in_ring = 1
                ring_base = 0.98  # Blinding Gold-White B-Ring
            elif 21025.0 <= r_plane2 <= 31684.0:
                in_ring = 1
                ring_base = 0.78  # Bright A-Ring

            if in_ring:
                # Check for Planet Shadow cast onto rear ring arc
                if (not is_front) and (dx * dx + float(dy * dy) < 4624.0):
                    # Inside shadow cylinder
                    buf[offset] = 0x08
                    buf[offset + 1] = 0x41
                else:
                    # High-intensity forward scatter lighting
                    norm_x = dx * 0.0056
                    sun_factor = 0.30 + max(0.0, (norm_x * inv_sun_lx + 0.70)) * 1.85
                    val_r = ring_base * sun_factor
                    val_g = ring_base * 0.90 * sun_factor
                    val_b = ring_base * 0.62 * sun_factor

                    if val_r > 1.0: val_r = 1.0
                    if val_g > 1.0: val_g = 1.0
                    if val_b > 1.0: val_b = 1.0

                    buf[offset] = ((int(val_r * 31.0) & 0x1F) << 3) | ((int(val_g * 63.0) >> 3) & 0x07)
                    buf[offset + 1] = (((int(val_g * 63.0) & 0x07) << 5) | (int(val_b * 31.0) & 0x1F)) & 0xFF

            offset += 2

# -------------------------------------------------------------------------
# Analytical Smooth Sphere Rasterizer
# -------------------------------------------------------------------------
@micropython.native
def render_smooth_saturn_sphere(buf):
    pitch = 760
    pcx = 190
    pcy = 150
    sr = 68
    sr2 = sr * sr
    inv_sr = 1.0 / float(sr)

    y_min = pcy - sr
    y_max = pcy + sr
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - pcy
        dy2 = dy * dy
        span_w2 = sr2 - dy2
        if span_w2 >= 0:
            hw = int(math.sqrt(span_w2))
            x0 = pcx - hw
            x1 = pcx + hw
            if x0 < 0: x0 = 0
            if x1 >= 380: x1 = 379

            ny = -float(dy) * inv_sr
            ny2 = ny * ny
            l_dot_y = ny * 0.62
            h_dot_y = ny * 0.52

            # Atmospheric latitude banding
            lat = abs(float(dy) * inv_sr)
            if lat < 0.28:
                base_r, base_g, base_b = 1.00, 0.88, 0.56  # Equatorial cream
            elif lat < 0.68:
                base_r, base_g, base_b = 0.90, 0.74, 0.42  # Ochre band
            else:
                base_r, base_g, base_b = 0.70, 0.60, 0.38  # Polar storm cap

            offset = y * pitch + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = float(x - pcx)
                nx = dx * inv_sr
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    # Intense direct sunlight diffuse (1.8x gain)
                    dot_s = nx * 0.62 + l_dot_y - nz * (-0.48)
                    diff = (dot_s * 1.80) if dot_s > 0.0 else 0.0

                    # Sun Glint specular flare
                    dot_h = nx * 0.52 + h_dot_y - nz * (-0.68)
                    spec = (dot_h ** 14) * 1.50 if (dot_h > 0.0 and diff > 0.10) else 0.0

                    amb = 0.12
                    r = (amb + diff) * base_r + spec
                    g = (amb + diff) * base_g + spec
                    b = (amb + diff) * base_b + spec

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    buf[offset] = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
                    buf[offset + 1] = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

                offset += 2

# -------------------------------------------------------------------------
# Execution Loop
# -------------------------------------------------------------------------
def run():
    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = BLACK_ROW

    render_starfield(FRAME_BUF)

    # 1. Back Ring Arc (Occluded behind the planet sphere)
    render_rings_arc(0, FRAME_BUF)

    # 2. Smooth Analytical Gas Giant Sphere
    render_smooth_saturn_sphere(FRAME_BUF)

    # 3. Front Ring Arc (Sweeps across the lower half in front of the sphere)
    render_rings_arc(1, FRAME_BUF)

    # Blit full frame buffer to LCD display via DMA
    moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)

    # Static showcase loop
    while True:
        time.sleep_ms(200)

if __name__ == "__main__":
    run()
