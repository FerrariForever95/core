# =====================================================================================
#  FILE:         doraemon_3d.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Real-time 3D Doraemon Head & Bell Showcase on Pure White Studio Floor:
#                - Electric Cyan-Blue Head Sphere & White Muzzle Mask
#                - Expressive 3D Eyes with Dark Pupils & Specular Catchlights
#                - Red Nose Sphere, Whiskers, Smile, and Collar Strip
#                - Golden Bell Pendant with Sound-Slit Aperture
#                - Dynamic Elliptical Contact Shadow on Floor
#                - Smooth 360-degree Clockwise Turntable Animation
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
CY     = 145
FOV    = 240.0
CAM_Z  = 4.2

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0xFFFF)

BB_W = 380
BB_H = 300
BB_X = CX - (BB_W // 2)  # 50
BB_Y = 10
ROW_PITCH = BB_W * 2     # 760 bytes

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_ROW = bytearray([0xFF] * ROW_PITCH)

# -------------------------------------------------------------------------
# Material Colors (RGB565)
# -------------------------------------------------------------------------
COL_BLUE_BASE   = (0.10, 0.58, 0.95)  # Doraemon Cyan-Blue
COL_WHITE_BASE  = (0.96, 0.96, 0.98)  # White Face Mask / Eyes
COL_RED_BASE    = (0.92, 0.12, 0.15)  # Nose & Collar
COL_GOLD_BASE   = (0.96, 0.78, 0.12)  # Bell Pendant
COL_BLACK_565   = 0x1082
COL_WHITE_565   = 0xFFFF
COL_SHADOW_565  = 0xB596

# Studio Light Vector (Top-Right-Front)
LX, LY, LZ = 0.50, 0.75, -0.42
l_inv = 1.0 / math.sqrt(LX * LX + LY * LY + LZ * LZ)
LX *= l_inv
LY *= l_inv
LZ *= l_inv

# -------------------------------------------------------------------------
# Low-Level Rasterizers
# -------------------------------------------------------------------------
@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    pitch = 760
    for y in range(y0, y1 + 1):
        offset = y * pitch
        buf[offset:offset + 760] = white_row

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
def render_shadow_disk(cx: int, cy: int, rx: int, ry: int, buf):
    pitch = 760
    if rx <= 0 or ry <= 0: return cy, cy

    y_min = cy - ry
    y_max = cy + ry
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    inv_ry2 = 1.0 / float(ry * ry)
    for y in range(y_min, y_max + 1):
        dy = y - cy
        span = 1.0 - (float(dy * dy) * inv_ry2)
        if span > 0.0:
            half_w = int(float(rx) * math.sqrt(span))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 >= 380: x1 = 379

            offset = y * pitch + (x0 << 1)
            cnt = x1 - x0 + 1
            for _ in range(cnt):
                buf[offset] = 0xB5
                buf[offset + 1] = 0x96
                offset += 2

    return y_min, y_max

@micropython.native
def render_lit_sphere(cx: int, cy: int, r_screen: int,
                      base_r: float, base_g: float, base_b: float,
                      spec_power: float, buf):
    pitch = 760
    if r_screen < 2: return cy, cy

    y_min = cy - r_screen
    y_max = cy + r_screen
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    inv_r = 1.0 / float(r_screen)
    r2 = r_screen * r_screen

    # Specular Halfway Vector
    hx = 0.50
    hy = 0.75
    hz = -0.42 - 1.0
    h_inv = 1.0 / math.sqrt(hx * hx + hy * hy + hz * hz)
    hx *= h_inv
    hy *= h_inv
    hz *= h_inv

    for y in range(y_min, y_max + 1):
        dy = y - cy
        dy2 = dy * dy
        span_w2 = r2 - dy2
        if span_w2 >= 0:
            half_w = int(math.sqrt(span_w2))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 >= 380: x1 = 379

            ny = -float(dy) * inv_r
            ny2 = ny * ny
            l_dot_y = ny * 0.75
            h_dot_y = ny * hy

            offset = y * pitch + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = float(x - cx)
                nx = dx * inv_r
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    # Directional diffuse
                    dot_l = nx * 0.50 + l_dot_y - nz * (-0.42)
                    diff = dot_l if dot_l > 0.0 else 0.0

                    # Specular highlight
                    dot_h = nx * hx + h_dot_y - nz * hz
                    spec = (dot_h ** 12) * spec_power if dot_h > 0.0 else 0.0

                    r = (0.24 + 0.76 * diff) * base_r + spec
                    g = (0.24 + 0.76 * diff) * base_g + spec
                    b = (0.24 + 0.76 * diff) * base_b + spec

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    buf[offset] = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
                    buf[offset + 1] = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF
                offset += 2

    return y_min, y_max

# -------------------------------------------------------------------------
# Scene Graph Pipeline
# -------------------------------------------------------------------------
def render_doraemon(yaw_deg: float):
    yaw = math.radians(yaw_deg)
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)

    frame_min_y = 300
    frame_max_y = 0

    # 1. Base Pedestal Contact Shadow
    s_min, s_max = render_shadow_disk(190, 260, 95, 24, FRAME_BUF)
    if s_min < frame_min_y: frame_min_y = s_min
    if s_max > frame_max_y: frame_max_y = s_max

    # 2. Primitive List [World X, Y, Z, Radius, R, G, B, SpecPower, Type]
    # Local offsets relative to head center (0, 0.1, 0)
    primitives = [
        # Main Cyan Head Sphere
        [ 0.00,  0.15,  0.00, 1.00, COL_BLUE_BASE[0],  COL_BLUE_BASE[1],  COL_BLUE_BASE[2],  0.8, 'head'],
        # White Muzzle Face Mask (Protrudes forward)
        [ 0.00, -0.05,  0.32, 0.76, COL_WHITE_BASE[0], COL_WHITE_BASE[1], COL_WHITE_BASE[2], 0.4, 'muzzle'],
        # Red Collar Torus Approximation
        [ 0.00, -0.74,  0.08, 0.72, COL_RED_BASE[0],   COL_RED_BASE[1],   COL_RED_BASE[2],   0.5, 'collar'],
        # Red Nose Sphere
        [ 0.00,  0.12,  0.92, 0.15, COL_RED_BASE[0],   COL_RED_BASE[1],   COL_RED_BASE[2],   1.2, 'nose'],
        # Left Eye (White Sclera)
        [-0.20,  0.42,  0.78, 0.22, COL_WHITE_BASE[0], COL_WHITE_BASE[1], COL_WHITE_BASE[2], 0.7, 'l_eye'],
        # Right Eye (White Sclera)
        [ 0.20,  0.42,  0.78, 0.22, COL_WHITE_BASE[0], COL_WHITE_BASE[1], COL_WHITE_BASE[2], 0.7, 'r_eye'],
        # Golden Bell Pendant
        [ 0.00, -0.92,  0.64, 0.17, COL_GOLD_BASE[0],  COL_GOLD_BASE[1],  COL_GOLD_BASE[2],  1.4, 'bell'],
    ]

    transformed = []
    for px, py, pz, r, cr, cg, cb, spec, ptype in primitives:
        # Rotate Clockwise around Y
        rx = px * cos_y + pz * sin_y
        ry = py
        rz = -px * sin_y + pz * cos_y

        wz = rz + CAM_Z
        inv_wz = 1.0 / wz
        sx = int(190 + (rx * FOV * inv_wz))
        sy = int(145 - (ry * FOV * inv_wz))
        sr = int(r * FOV * inv_wz)

        transformed.append((wz, sx, sy, sr, cr, cg, cb, spec, ptype, rz))

    # Painter's Algorithm: Sort Back-to-Front
    transformed.sort(key=lambda item: item[0], reverse=True)

    # 3. Render Primitives in Depth Order
    for wz, sx, sy, sr, cr, cg, cb, spec, ptype, rz in transformed:
        p_min, p_max = render_lit_sphere(sx, sy, sr, cr, cg, cb, spec, FRAME_BUF)
        if p_min < frame_min_y: frame_min_y = p_min
        if p_max > frame_max_y: frame_max_y = p_max

        # Facial Overlays (Only visible when facing camera)
        facing = math.cos(yaw)
        if facing > 0.10:
            if ptype == 'l_eye':
                # Black Pupil & Specular Catchlight
                fill_rect(sx - 3, sy - 2, 7, 9, 0x10, 0x82, FRAME_BUF)
                fill_rect(sx - 2, sy - 1, 3, 3, 0xFF, 0xFF, FRAME_BUF)
            elif ptype == 'r_eye':
                fill_rect(sx - 4, sy - 2, 7, 9, 0x10, 0x82, FRAME_BUF)
                fill_rect(sx - 3, sy - 1, 3, 3, 0xFF, 0xFF, FRAME_BUF)
            elif ptype == 'bell':
                # Sound-slit horizontal groove & ball eyelet
                fill_rect(sx - sr + 2, sy, (sr << 1) - 4, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(sx - 2, sy + 3, 4, 4, 0x10, 0x82, FRAME_BUF)

    # 4. Whiskers & Philtrum Seam Line (Drawn on forward face)
    if math.cos(yaw) > 0.25:
        # Find projected nose and muzzle positions
        for item in transformed:
            if item[8] == 'nose':
                nx, ny = item[1], item[2]
                # Philtrum vertical seam down from nose
                fill_rect(nx - 1, ny + 10, 2, 28, 0x10, 0x82, FRAME_BUF)
                # Smile curve arc
                fill_rect(nx - 24, ny + 38, 48, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx - 28, ny + 34, 4, 4, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx + 24, ny + 34, 4, 4, 0x10, 0x82, FRAME_BUF)

                # Whiskers (3 left, 3 right)
                dx_tilt = int(sin_y * 12.0)
                # Left whiskers
                fill_rect(nx - 52 - dx_tilt, ny + 14, 24, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx - 54 - dx_tilt, ny + 22, 26, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx - 50 - dx_tilt, ny + 30, 22, 2, 0x10, 0x82, FRAME_BUF)
                # Right whiskers
                fill_rect(nx + 28 - dx_tilt, ny + 14, 24, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx + 28 - dx_tilt, ny + 22, 26, 2, 0x10, 0x82, FRAME_BUF)
                fill_rect(nx + 28 - dx_tilt, ny + 30, 22, 2, 0x10, 0x82, FRAME_BUF)

    return frame_min_y, frame_max_y

# -------------------------------------------------------------------------
# Main Turntable Loop
# -------------------------------------------------------------------------
def run():
    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = WHITE_ROW

    moclcd.fill_rect(10, 10, 240, 12, 0xFFFF)
    moclcd.draw_text(10, 10, "DORAEMON 3D // STUDIO 360", 0x0000, 0xFFFF)

    yaw_angle = 0.0

    # Initial frame
    f_min, f_max = render_doraemon(yaw_angle)
    blit_top = max(0, f_min)
    blit_bottom = min(299, f_max)
    blit_h = blit_bottom - blit_top + 1
    start_offset = blit_top * ROW_PITCH
    end_offset = start_offset + (blit_h * ROW_PITCH)
    moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])
    prev_min_y = f_min
    prev_max_y = f_max

    time.sleep_ms(1000)

    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_ROW)

        # Smooth clockwise turntable spin
        yaw_angle = (yaw_angle + 2.6) % 360.0

        f_min, f_max = render_doraemon(yaw_angle)

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
