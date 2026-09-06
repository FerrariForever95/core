import math
import time
import machine
import moclcd
import micropython

machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 124  # Horizon line

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

# Palette
COL_HORIZON_FOG = 0x2965
COL_ROAD_DARK   = 0x2124
COL_ROAD_LIGHT  = 0x31A6
COL_CURB_RED    = 0xD882
COL_CURB_WHT    = 0xEF7D
COL_GRASS_1     = 0x1301
COL_GRASS_2     = 0x1B62
COL_POLE        = 0x94B2

@micropython.native
def fill_rect_fast(x0: int, y0: int, w: int, h: int, hi: int, lo: int, buf):
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
def render_track(buf, pos_z: float, curve_val: float):
    pitch = 760
    bb_cx = 190

    # 1. Sky & Twilight Gradient
    for y in range(120):
        t = y * 0.00833
        r = int(12.0 + t * 24.0)
        g = int(18.0 + t * 28.0)
        b = int(45.0 + t * 35.0)
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

        # Perspective projection
        scale = 160.0 / z
        road_w = int(145.0 * scale)
        curb_w = max(2, int(18.0 * scale))
        line_w = max(1, int(3.5 * scale))

        # Quadratic curve offset
        curve_offset = int((z * z * 0.00035) * curve_val)
        center_x = bb_cx + curve_offset

        seg = int(world_z * 0.09) & 1
        line_seg = int(world_z * 0.18) & 1

        if seg == 0:
            ghi, glo = (0x1B, 0x62)
            rhi, rlo = (0x31, 0xA6)
            chi, clo = (0xD8, 0x82)
        else:
            ghi, glo = (0x13, 0x01)
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

@micropython.native
def render_car_model(cx: int, cy: int, steer_lean: int, buf):
    # Contact shadow with soft edge
    fill_rect_fast(cx - 52, cy + 12, 104, 10, 0x08, 0x41, buf)

    # Wide rear racing tires with camber
    fill_rect_fast(cx - 50, cy - 6, 16, 22, 0x18, 0xC3, buf)
    fill_rect_fast(cx + 34, cy - 6, 16, 22, 0x18, 0xC3, buf)

    # Carbon fiber lower diffuser with strakes
    fill_rect_fast(cx - 44, cy + 2, 88, 14, 0x10, 0x82, buf)
    fill_rect_fast(cx - 16, cy + 8, 4, 8, 0x00, 0x00, buf)
    fill_rect_fast(cx + 12, cy + 8, 4, 8, 0x00, 0x00, buf)

    # Titanium Quad Exhausts
    fill_rect_fast(cx - 30, cy + 6, 8, 6, 0x94, 0xB2, buf)
    fill_rect_fast(cx + 22, cy + 6, 8, 6, 0x94, 0xB2, buf)

    # Main Body Shell (Electric Cyan with Shaded Crease)
    fill_rect_fast(cx - 46, cy - 20, 92, 22, 0x03, 0x7F, buf)
    fill_rect_fast(cx - 46, cy - 10, 92, 4,  0x02, 0x16, buf)

    # Continuous Blade Taillight (Bright Red with Core White Accent)
    fill_rect_fast(cx - 42, cy - 16, 84, 4, 0xF8, 0x00, buf)
    fill_rect_fast(cx - 38, cy - 15, 76, 2, 0xFE, 0x66, buf)

    # Sloped Fastback Roof & Rear Tinted Window
    roof_x = cx - 28 + steer_lean
    fill_rect_fast(roof_x, cy - 40, 56, 20, 0x02, 0x16, buf)
    fill_rect_fast(roof_x + 4, cy - 38, 48, 16, 0x10, 0x82, buf)

    # High-Downforce GT Wing with Dual Carbon Endplates
    wing_x = cx - 44 + steer_lean
    fill_rect_fast(wing_x, cy - 28, 88, 4, 0x08, 0x41, buf)
    fill_rect_fast(wing_x - 2, cy - 32, 4, 10, 0x00, 0x00, buf)
    fill_rect_fast(wing_x + 86, cy - 32, 4, 10, 0x00, 0x00, buf)
    fill_rect_fast(wing_x + 18, cy - 24, 4, 6, 0x00, 0x00, buf)
    fill_rect_fast(wing_x + 66, cy - 24, 4, 6, 0x00, 0x00, buf)

def run():
    road_z = 0.0
    curve_phase = 0.0
    car_x = 190
    car_y = 262

    while True:
        road_z += 32.0
        curve_phase += 0.02
        curve = math.sin(curve_phase) * 1.6

        steer_lean = int(curve * 3.5)
        car_draw_x = int(car_x + math.sin(curve_phase * 1.5) * 25.0)
        bob = int(math.sin(road_z * 0.3) * 1.5)

        render_track(FRAME_BUF, road_z, curve)
        render_car_model(car_draw_x, car_y + bob, steer_lean, FRAME_BUF)

        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)
        time.sleep_ms(12)

if __name__ == "__main__":
    run()
