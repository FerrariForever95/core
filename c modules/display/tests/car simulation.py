# =====================================================================================
#  FILE:         highway_gt3rs.py
#  MODULE:       highway (MicroPython pseudo-3D perspective racer demo)
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Porsche 911 GT3 RS (992-generation) chase-cam highway engine:
#                - High-visibility Lizard Green / Acid Green livery with Weissach accents
#                - Iconic 911 teardrop greenhouse canopy tapering inward
#                - Enormous rear wheel-arch flared haunches with wide track slicks
#                - Full-width razor-thin continuous LED light bar
#                - Centrally mounted dual titanium exhaust barrels
#                - Massive swan-neck top-hung multi-element GT rear wing with DRS ram
#                - Functional rear bumper aero side vents & deep carbon under-diffuser
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU clock to peak 240 MHz for fast software rasterization
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 124

# Display bus initialization
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
# Porsche 911 GT3 RS Signature Palette (Lizard Green & Weissach Carbon)
# -------------------------------------------------------------------------
COL_GT3_GREEN    = 0x7FE0  # Electric Acid/Lizard Green (pops instantly off asphalt)
COL_GT3_SHADOW   = 0x3CA0  # Shaded lower fender green
COL_CARBON_DARK  = 0x1082  # Raw carbon weave / rear bumper cutouts
COL_CARBON_MED   = 0x2124  # Satin wing aerofoil & diffuser
COL_LIGHTBAR_RED = 0xF800  # Intense continuous 911 rear red light blade
COL_LIGHTBAR_HL  = 0xFFE0  # White-hot LED center core
COL_EXHAUST_OUT  = 0xCE79  # Ceramic/Titanium exhaust outer rim
COL_EXHAUST_IN   = 0x0841  # Dark exhaust nozzle bore
COL_GLASS_TINT   = 0x1146  # Smoked rear engine-cover glass
COL_TIRE_RUBBER  = 0x18C3  # Michelin Cup 2 R rear rubber
COL_TIRE_RIM     = 0xFE40  # Neodyme Gold / Satin Aurum lightweight wheel face

# -------------------------------------------------------------------------
# Low-Level Fast Rasterizers
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
# Curving Highway Track Scanner
# -------------------------------------------------------------------------
@micropython.native
def render_track(buf, pos_z: float, curve_val: float):
    pitch = 760
    bb_cx = 190

    # Sky gradient
    for y in range(120):
        t = y * 0.00833
        r = int(10.0 + t * 18.0)
        g = int(14.0 + t * 24.0)
        b = int(38.0 + t * 40.0)
        hi = ((r & 0x1F) << 3) | ((g >> 3) & 0x07)
        lo = (((g & 0x07) << 5) | (b & 0x1F)) & 0xFF
        offset = y * pitch
        for _ in range(380):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

    # Perspective road scanlines
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
# Porsche 911 GT3 RS (992) Specific Rear Model
# -------------------------------------------------------------------------
@micropython.native
def render_gt3rs(cx: int, cy: int, steer_lean: int, flame_burst: int, buf):
    # 1. Broad ground shadow
    fill_trapezoid(cx, cy + 10, cy + 22, 116, 136, 0x08, 0x41, buf)

    # 2. Extreme-Width 335-Section Rear Tires (Camber Angle & Gold Wheels)
    fill_trapezoid(cx - 52, cy - 6, cy + 18, 20, 24, 0x18, 0xC3, buf)
    fill_trapezoid(cx + 52, cy - 6, cy + 18, 20, 24, 0x18, 0xC3, buf)
    # Visible Wheel Faces (Weissach Aurum Gold Center)
    fill_rect(cx - 58, cy - 2, 8, 12, 0xFE, 0x40, buf)
    fill_rect(cx + 50, cy - 2, 8, 12, 0xFE, 0x40, buf)

    # 3. Aggressive Rear Diffuser with 4 Vertical Aero Tunnels
    fill_trapezoid(cx, cy + 2, cy + 17, 84, 96, 0x10, 0x82, buf)
    fill_rect(cx - 26, cy + 6, 3, 11, 0x00, 0x00, buf)
    fill_rect(cx - 10, cy + 6, 3, 11, 0x00, 0x00, buf)
    fill_rect(cx + 7,  cy + 6, 3, 11, 0x00, 0x00, buf)
    fill_rect(cx + 23, cy + 6, 3, 11, 0x00, 0x00, buf)

    # 4. Signature 911 Center-Exit Dual Exhaust Tips
    fill_rect(cx - 11, cy + 3, 10, 8, 0xCE, 0x79, buf)
    fill_rect(cx + 1,  cy + 3, 10, 8, 0xCE, 0x79, buf)
    fill_rect(cx - 9,  cy + 5, 6,  4, 0x08, 0x41, buf)
    fill_rect(cx + 3,  cy + 5, 6,  4, 0x08, 0x41, buf)

    # Exhaust Flame Overrun Spit
    if flame_burst:
        fill_rect(cx - 10, cy + 8, 8, 7, 0x54, 0xBE, buf) # Cyan flame root
        fill_rect(cx + 2,  cy + 8, 8, 7, 0x54, 0xBE, buf)
        fill_rect(cx - 8,  cy + 10, 4, 8, 0xFF, 0xE0, buf) # White core
        fill_rect(cx + 4,  cy + 10, 4, 8, 0xFF, 0xE0, buf)

    # 5. Iconic Wide Muscular 911 Rear Hips / Haunches (Bulging Fenders)
    fill_trapezoid(cx, cy - 14, cy + 4, 104, 98, 0x3C, 0xA0, buf)
    fill_trapezoid(cx - 48, cy - 22, cy - 4, 22, 28, 0x7F, 0xE0, buf) # Left Haunch
    fill_trapezoid(cx + 48, cy - 22, cy - 4, 22, 28, 0x7F, 0xE0, buf) # Right Haunch
    fill_trapezoid(cx, cy - 22, cy - 14, 98, 104, 0x7F, 0xE0, buf)   # Mid Bumper

    # Fender Wheel-Arch Pressure Relief Louvres (Black Side Aero Cutouts)
    fill_rect(cx - 48, cy - 14, 4, 12, 0x10, 0x82, buf)
    fill_rect(cx + 44, cy - 14, 4, 12, 0x10, 0x82, buf)

    # 6. Full-Width Seamless 911 Razor LED Light Strip
    fill_rect(cx - 46, cy - 18, 92, 4, 0xF8, 0x00, buf)
    fill_rect(cx - 42, cy - 17, 84, 2, 0xFF, 0xE0, buf) # White-hot core blade
    # PORSCHE script dark recess area
    fill_rect(cx - 20, cy - 13, 40, 5, 0x10, 0x82, buf)

    # 7. Distinctive 911 Teardrop Canopy & Sloping Rear Window
    cockpit_cx = cx + steer_lean
    # Tapered Rear Engine Deck Cover
    fill_trapezoid(cockpit_cx, cy - 36, cy - 22, 48, 72, 0x7F, 0xE0, buf)
    # Sloping Rear Windshield (Teardrop shape)
    fill_trapezoid(cockpit_cx, cy - 48, cy - 34, 34, 46, 0x11, 0x46, buf)
    # Double-Bubble Lightweight Roof Profile
    fill_trapezoid(cockpit_cx, cy - 51, cy - 48, 30, 34, 0x10, 0x82, buf)

    # 8. Towering Top-Hung Swan-Neck GT Rear Wing (Higher than the roofline)
    wing_cx = cx + (steer_lean >> 1)
    wing_y = cy - 54

    # Top-Hung Curved Carbon Pylons
    fill_rect(wing_cx - 20, wing_y + 4, 4, 28, 0x10, 0x82, buf)
    fill_rect(wing_cx + 16, wing_y + 4, 4, 28, 0x10, 0x82, buf)
    # Center DRS Hydraulic Ram Actuator
    fill_rect(wing_cx - 2, wing_y - 2, 4, 8, 0xCE, 0x79, buf)

    # Dual-Element Main Aerofoil Blade
    fill_trapezoid(wing_cx, wing_y, wing_y + 6, 106, 102, 0x21, 0x24, buf)
    fill_rect(wing_cx - 50, wing_y + 1, 100, 2, 0x7F, 0xE0, buf) # Green leading-edge accent
    fill_rect(wing_cx - 48, wing_y - 4, 96, 3, 0x10, 0x82, buf)   # Active DRS Upper Flap

    # Massive Vertical Wing Endplates with RS Cutouts
    fill_trapezoid(wing_cx - 54, wing_y - 8, wing_y + 14, 5, 7, 0x7F, 0xE0, buf)
    fill_trapezoid(wing_cx + 49, wing_y - 8, wing_y + 14, 5, 7, 0x7F, 0xE0, buf)
    fill_rect(wing_cx - 53, wing_y - 4, 2, 14, 0x10, 0x82, buf)
    fill_rect(wing_cx + 51, wing_y - 4, 2, 14, 0x10, 0x82, buf)

# -------------------------------------------------------------------------
# Main Game Loop
# -------------------------------------------------------------------------
def run():
    road_z = 0.0
    curve_phase = 0.0
    car_x = 190
    car_y = 260
    flame_counter = 0

    while True:
        road_z += 35.0
        curve_phase += 0.022
        curve = math.sin(curve_phase) * 1.75

        # Rear-engine chassis dynamics: sharp steering rotation
        steer_lean = int(curve * 4.0)
        car_draw_x = int(car_x + math.sin(curve_phase * 1.4) * 28.0)
        suspension_bob = int(math.sin(road_z * 0.40) * 1.2)

        # Trigger flame pop on hard steering transitions
        flame_counter = (flame_counter + 1) % 18
        flame_burst = 1 if flame_counter > 15 else 0

        render_track(FRAME_BUF, road_z, curve)
        render_gt3rs(car_draw_x, car_y + suspension_bob, steer_lean, flame_burst, FRAME_BUF)

        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)
        time.sleep_ms(12)

if __name__ == "__main__":
    run()
