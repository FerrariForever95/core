import math
import time
import machine
import moclcd
import micropython

machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 124

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
# Hypercar Color Palette (Track Spec Carbon & Flare Orange / Jet Black)
# -------------------------------------------------------------------------
COL_BODY_BASE    = 0xFBE0  # Vivid Flame Orange / Sunset Gold
COL_BODY_SHADOW  = 0xB9A0  # Shaded Underbody Orange
COL_BODY_HL      = 0xFDC0  # Specular Edge Highlight
COL_CARBON_DARK  = 0x1082  # Weave Base Carbon
COL_CARBON_LGT   = 0x2124  # Exposed Satin Carbon
COL_EXHAUST_GLOW = 0x54BE  # Burnt Blue Titanium Lips
COL_DIFFUSER     = 0x0841  # Pure Matte Black Aerodynamics
COL_TAIL_CORE    = 0xFFE0  # White-Hot LED Core
COL_TAIL_RED     = 0xF800  # Intense Cherry LED Brake Bar
COL_TAIL_BLOOM   = 0x9000  # Ambient Red Reflector Edge
COL_GLASS_TINT   = 0x1185  # Deep Smoked Polycarbonate
COL_ENGINE_MESH  = 0x18C3  # Rear Deck Cooling Grille

# -------------------------------------------------------------------------
# Low-Level Rasterizers (Trapezoids & Solid Spans)
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
# Dynamic Track Scanline Renderer
# -------------------------------------------------------------------------
@micropython.native
def render_track(buf, pos_z: float, curve_val: float):
    pitch = 760
    bb_cx = 190

    # 1. Sky Gradient with Horizon Haze
    for y in range(120):
        t = y * 0.00833
        r = int(10.0 + t * 20.0)
        g = int(14.0 + t * 24.0)
        b = int(38.0 + t * 40.0)
        hi = ((r & 0x1F) << 3) | ((g >> 3) & 0x07)
        lo = (((g & 0x07) << 5) | (b & 0x1F)) & 0xFF
        offset = y * pitch
        for _ in range(380):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

    # 2. Curving Highway Scanlines
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
# Upgraded GT3 / Hypercar Model
# -------------------------------------------------------------------------
@micropython.native
def render_hypercar(cx: int, cy: int, steer_lean: int, buf):
    # 1. Broad Aerodynamic Ground Contact Shadow
    fill_trapezoid(cx, cy + 10, cy + 22, 114, 134, 0x08, 0x41, buf)

    # 2. Racing Slicks (Rear Wheels with Camber Angle)
    fill_trapezoid(cx - 48, cy - 8, cy + 18, 18, 22, 0x18, 0xC3, buf)
    fill_trapezoid(cx + 48, cy - 8, cy + 18, 18, 22, 0x18, 0xC3, buf)
    # Inner Wheel Well Shadows
    fill_rect(cx - 56, cy - 4, 8, 18, 0x00, 0x00, buf)
    fill_rect(cx + 48, cy - 4, 8, 18, 0x00, 0x00, buf)

    # 3. Race Rear Diffuser (Faceted Strakes & Negative Venting)
    fill_trapezoid(cx, cy + 2, cy + 18, 86, 96, 0x10, 0x82, buf)
    # 4 Vertical Diffuser Strakes
    fill_rect(cx - 30, cy + 6, 3, 13, 0x00, 0x00, buf)
    fill_rect(cx - 10, cy + 6, 3, 13, 0x00, 0x00, buf)
    fill_rect(cx + 8,  cy + 6, 3, 13, 0x00, 0x00, buf)
    fill_rect(cx + 28, cy + 6, 3, 13, 0x00, 0x00, buf)

    # Central FIA Rain Light
    fill_rect(cx - 4, cy + 10, 8, 5, 0xFE, 0x00, buf)

    # Quad Inset Titanium Exhaust Outlets
    fill_rect(cx - 24, cy + 4, 9, 6, 0x21, 0x24, buf)
    fill_rect(cx - 22, cy + 5, 5, 4, 0x54, 0xBE, buf)
    fill_rect(cx + 15, cy + 4, 9, 6, 0x21, 0x24, buf)
    fill_rect(cx + 17, cy + 5, 5, 4, 0x54, 0xBE, buf)

    # 4. Muscular Rear Haunches & Lower Bumper
    fill_trapezoid(cx, cy - 14, cy + 4, 98, 92, 0xB9, 0xA0, buf)
    fill_trapezoid(cx - 44, cy - 20, cy - 4, 24, 28, 0xFB, 0xE0, buf)
    fill_trapezoid(cx + 44, cy - 20, cy - 4, 24, 28, 0xFB, 0xE0, buf)
    fill_rect(cx - 32, cy - 12, 64, 14, 0x10, 0x82, buf)

    # 5. Continuous Sleek Blade Taillights
    fill_rect(cx - 44, cy - 17, 88, 5, 0x90, 0x00, buf)
    fill_rect(cx - 42, cy - 16, 84, 3, 0xF8, 0x00, buf)
    fill_rect(cx - 38, cy - 15, 76, 1, 0xFF, 0xE0, buf)
    fill_rect(cx - 44, cy - 14, 3, 5, 0xF8, 0x00, buf)
    fill_rect(cx + 41, cy - 14, 3, 5, 0xF8, 0x00, buf)

    # 6. Mid-Engine Deck, Slotted Cooling Louvres & Roofline
    roof_offset = steer_lean
    cockpit_cx = cx + roof_offset

    fill_trapezoid(cockpit_cx, cy - 32, cy - 18, 62, 78, 0xFB, 0xE0, buf)
    fill_rect(cockpit_cx - 20, cy - 28, 40, 2, 0x10, 0x82, buf)
    fill_rect(cockpit_cx - 18, cy - 24, 36, 2, 0x10, 0x82, buf)
    fill_rect(cockpit_cx - 16, cy - 20, 32, 2, 0x10, 0x82, buf)

    fill_trapezoid(cockpit_cx, cy - 48, cy - 32, 42, 62, 0xFB, 0xE0, buf)
    fill_trapezoid(cockpit_cx, cy - 45, cy - 33, 34, 52, 0x11, 0x85, buf)
    fill_rect(cockpit_cx - 6, cy - 50, 12, 3, 0x10, 0x82, buf)

    # 7. High-Downforce Swan-Neck GT Rear Wing
    wing_cx = cx + (roof_offset >> 1)
    wing_y = cy - 36

    fill_rect(wing_cx - 18, wing_y + 4, 3, 14, 0x10, 0x82, buf)
    fill_rect(wing_cx + 15, wing_y + 4, 3, 14, 0x10, 0x82, buf)

    fill_trapezoid(wing_cx, wing_y, wing_y + 5, 96, 92, 0x21, 0x24, buf)
    # Fixed: passed hi/lo bytes (0xFD, 0xC0) instead of single 16-bit integer
    fill_rect(wing_cx - 46, wing_y + 1, 92, 2, 0xFD, 0xC0, buf)

    fill_trapezoid(wing_cx - 48, wing_y - 4, wing_y + 9, 5, 7, 0xFB, 0xE0, buf)
    fill_trapezoid(wing_cx + 47, wing_y - 4, wing_y + 9, 5, 7, 0xFB, 0xE0, buf)

# -------------------------------------------------------------------------
# Loop Runner
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

        steer_lean = int(curve * 4.2)
        car_draw_x = int(car_x + math.sin(curve_phase * 1.4) * 28.0)
        suspension_bob = int(math.sin(road_z * 0.35) * 1.5)

        render_track(FRAME_BUF, road_z, curve)
        render_hypercar(car_draw_x, car_y + suspension_bob, steer_lean, FRAME_BUF)

        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)
        time.sleep_ms(12)

if __name__ == "__main__":
    run()
