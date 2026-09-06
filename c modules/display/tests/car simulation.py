# =====================================================================================
#  FILE:         highway_gtr.py
#  MODULE:       highway (MicroPython pseudo-3D perspective racer demo)
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  High-speed arcade perspective highway engine featuring a Nissan GT-R
#                (R35) chase-cam model with dynamic road curvature scanlines, dual-tone
#                rumble strips, dashed lane dividers, and suspension physics.
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU to peak 240 MHz clock for deterministic software rasterization
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 124

# Initialize ILI9488 panel via moclcd parallel bus
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0xFFFF)

BB_W = 380
BB_H = 300
BB_X = CX - (BB_W // 2)
BB_Y = 10
ROW_PITCH = BB_W * 2

FRAME_BUF = bytearray(BB_W * BB_H * 2)

# -------------------------------------------------------------------------
# Nissan GT-R (R35) Signature Palette: Bayside Blue & Carbon
# -------------------------------------------------------------------------
COL_GTR_BLUE     = 0x1A7F  # Bayside Blue metallic
COL_GTR_SHADOW   = 0x0974  # Deep under-bumper blue
COL_CARBON_DIFF  = 0x10A2  # Carbon rear diffuser
COL_CHROME_TIPS  = 0xCE79  # Large dual quad exhaust tips
COL_TAIL_OUTER   = 0xF800  # Iconic dual ring outer red
COL_TAIL_CORE    = 0xFFE0  # Concentric glowing halo core
COL_GLASS_TINT   = 0x1125  # Dark tinted rear windshield
COL_WING_CARBON  = 0x0841  # R35 trunk deck spoiler

# -------------------------------------------------------------------------
# Low-Level Rasterizers
# -------------------------------------------------------------------------
@micropython.native
def fill_rect(x0: int, y0: int, w: int, h: int, hi: int, lo: int, buf):
    pitch = 760
    x1 = x0 + w
    y1 = y0 + h
    if x0 < 0: x0 = 0
    if x1 > 380: x1 = 380
    if y0 < 0: y0 = 0
    if y1 > 300: y1 = 300
    if x0 >= x1 or y0 >= y1: return

    for y in range(y0, y1):
        offset = y * pitch + (x0 << 1)
        cnt = x1 - x0
        for _ in range(cnt):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

@micropython.native
def fill_trapezoid(cx: int, y0: int, y1: int, w_top: int, w_bot: int, hi: int, lo: int, buf):
    pitch = 760
    if y0 >= y1 or y1 <= 0 or y0 >= 300: return
    dy = y1 - y0
    inv_dy = 1.0 / dy

    for y in range(y0, y1):
        if 0 <= y < 300:
            t = (y - y0) * inv_dy
            half_w = int((w_top * (1.0 - t) + w_bot * t) * 0.5)
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 > 380: x1 = 380
            if x0 < x1:
                offset = y * pitch + (x0 << 1)
                cnt = x1 - x0
                for _ in range(cnt):
                    buf[offset] = hi
                    buf[offset + 1] = lo
                    offset += 2

# -------------------------------------------------------------------------
# Dynamic Curving Track
# -------------------------------------------------------------------------
@micropython.native
def render_track(buf, pos_z: float, curve_val: float):
    pitch = 760
    bb_cx = 190

    for y in range(120):
        t = y * 0.00833
        r = int(12.0 + t * 18.0)
        g = int(16.0 + t * 24.0)
        b = int(40.0 + t * 38.0)
        hi = ((r & 0x1F) << 3) | ((g >> 3) & 0x07)
        lo = (((g & 0x07) << 5) | (b & 0x1F)) & 0xFF
        offset = y * pitch
        for _ in range(380):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

    for y in range(120, 300):
        dy = y - 118
        z = 1800.0 / dy
        world_z = z + pos_z

        scale = 160.0 / z
        road_w = int(145.0 * scale)
        curb_w = max(2, int(18.0 * scale))
        line_w = max(1, int(3.5 * scale))

        curve_offset = int((z * z * 0.00035) * curve_val)
        center_x = bb_cx + curve_offset

        seg = int(world_z * 0.09) & 1
        line_seg = int(world_z * 0.18) & 1

        if seg == 0:
            ghi, glo = (0x13, 0x41)
            rhi, rlo = (0x31, 0xA6)
            chi, clo = (0xD8, 0x82)
        else:
            ghi, glo = (0x0B, 0x01)
            rhi, rlo = (0x21, 0x24)
            chi, clo = (0xEF, 0x7D)

        offset = y * pitch
        rl = center_x - road_w
        rr = center_x + road_w
        cl = rl - curb_w
        cr = rr + curb_w
        ll = center_x - line_w
        lr = center_x + line_w

        for x in range(380):
            if x < cl or x > cr:
                buf[offset] = ghi
                buf[offset + 1] = glo
            elif x < rl or x > rr:
                buf[offset] = chi
                buf[offset + 1] = clo
            else:
                if line_seg == 0 and (ll <= x <= lr):
                    buf[offset] = 0xFF
                    buf[offset + 1] = 0xFF
                else:
                    buf[offset] = rhi
                    buf[offset + 1] = rlo
            offset += 2

# -------------------------------------------------------------------------
# Nissan GT-R (R35) Rear Profile Renderer
# -------------------------------------------------------------------------
@micropython.native
def render_gtr(cx: int, cy: int, steer_lean: int, buf):
    # 1. Ground contact shadow
    fill_trapezoid(cx, cy + 10, cy + 22, 108, 126, 0x08, 0x41, buf)

    # 2. Wide rear tires
    fill_rect(cx - 50, cy - 4, 16, 20, 0x18, 0xC3, buf)
    fill_rect(cx + 34, cy - 4, 16, 20, 0x18, 0xC3, buf)

    # 3. Carbon rear diffuser with center rear fog light
    fill_trapezoid(cx, cy + 2, cy + 16, 86, 94, 0x10, 0xA2, buf)
    fill_rect(cx - 4, cy + 8, 8, 4, 0xF8, 0x00, buf)

    # 4. Massive Dual Quad Exhaust Tips (GT-R signature feature)
    fill_rect(cx - 38, cy + 4, 10, 8, 0xCE, 0x79, buf)
    fill_rect(cx - 26, cy + 4, 10, 8, 0xCE, 0x79, buf)
    fill_rect(cx + 16, cy + 4, 10, 8, 0xCE, 0x79, buf)
    fill_rect(cx + 28, cy + 4, 10, 8, 0xCE, 0x79, buf)
    # Dark exhaust bores
    fill_rect(cx - 36, cy + 6, 6, 4, 0x08, 0x41, buf)
    fill_rect(cx - 24, cy + 6, 6, 4, 0x08, 0x41, buf)
    fill_rect(cx + 18, cy + 6, 6, 4, 0x08, 0x41, buf)
    fill_rect(cx + 30, cy + 6, 6, 4, 0x08, 0x41, buf)

    # 5. Broad, Muscular R35 Rear Bumper and Fenders
    fill_trapezoid(cx, cy - 14, cy + 4, 98, 92, 0x09, 0x74, buf)
    fill_trapezoid(cx, cy - 24, cy - 14, 92, 98, 0x1A, 0x7F, buf)
    # Inset license plate recess
    fill_rect(cx - 18, cy - 8, 36, 10, 0x10, 0xA2, buf)

    # 6. Iconic 4 Circular Halo Taillights (Outer Large, Inner Small)
    # Left Outer Ring
    fill_rect(cx - 40, cy - 20, 10, 10, 0xF8, 0x00, buf)
    fill_rect(cx - 38, cy - 18, 6, 6, 0xFF, 0xE0, buf)
    # Left Inner Ring
    fill_rect(cx - 26, cy - 19, 8, 8, 0xF8, 0x00, buf)
    fill_rect(cx - 24, cy - 17, 4, 4, 0xFF, 0xE0, buf)
    # Right Inner Ring
    fill_rect(cx + 18, cy - 19, 8, 8, 0xF8, 0x00, buf)
    fill_rect(cx + 20, cy - 17, 4, 4, 0xFF, 0xE0, buf)
    # Right Outer Ring
    fill_rect(cx + 30, cy - 20, 10, 10, 0xF8, 0x00, buf)
    fill_rect(cx + 32, cy - 18, 6, 6, 0xFF, 0xE0, buf)

    # 7. Angular R35 Greenhouse Canopy & Tinted Glass
    cockpit_cx = cx + steer_lean
    fill_trapezoid(cockpit_cx, cy - 44, cy - 24, 52, 70, 0x1A, 0x7F, buf)
    fill_trapezoid(cockpit_cx, cy - 42, cy - 26, 42, 58, 0x11, 0x25, buf)

    # 8. Factory Trunk-Mounted Pedestal Spoiler
    wing_cx = cx + (steer_lean >> 1)
    wing_y = cy - 30
    # Left and right mounting uprights
    fill_rect(wing_cx - 28, wing_y + 4, 4, 8, 0x08, 0x41, buf)
    fill_rect(wing_cx + 24, wing_y + 4, 4, 8, 0x08, 0x41, buf)
    # Aerofoil main plane
    fill_rect(wing_cx - 42, wing_y, 84, 4, 0x08, 0x41, buf)

# -------------------------------------------------------------------------
# Main Game Loop
# -------------------------------------------------------------------------
def run():
    road_z = 0.0
    curve_phase = 0.0
    car_x = 190
    car_y = 260

    while True:
        road_z += 34.0
        curve_phase += 0.022
        curve = math.sin(curve_phase) * 1.7

        steer_lean = int(curve * 3.6)
        car_draw_x = int(car_x + math.sin(curve_phase * 1.4) * 26.0)
        suspension_bob = int(math.sin(road_z * 0.35) * 1.5)

        render_track(FRAME_BUF, road_z, curve)
        render_gtr(car_draw_x, car_y + suspension_bob, steer_lean, FRAME_BUF)

        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)
        time.sleep_ms(12)

if __name__ == "__main__":
    run()
