# =====================================================================================
#  FILE:         esp32_showcase.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Real-time 3D ESP32-S3 Dev Board Showcase on a pure white background:
#                - Matte Black FR-4 PCB body with 4 corner mounting holes
#                - Brushed silver RF Shielding Can with antenna trace zone
#                - Dual gold/silver 15-pin DIP headers on left and right rails
#                - Dual metal USB-C receptacles (UART + Native USB)
#                - Onboard tactile BOOT/RESET micro-switches and glowing status LED
#                - Full 360-degree turntable rotation with studio lighting
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
CAM_Z  = 4.4

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
WHITE_ROW = bytearray([0xFF] * ROW_PITCH)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

# -------------------------------------------------------------------------
# Board Color Palette (RGB565)
# -------------------------------------------------------------------------
COL_PCB_MATTE    = 0x18C3  # Matte Obsidian / Dark FR-4
COL_PCB_BORDER   = 0x0841  # Board edge chamfer
COL_METAL_CAN    = 0xCE79  # Brushed Silver RF Shielding Can
COL_CAN_SHADOW   = 0x8410  # Shield side profile
COL_HEADER_GOLD  = 0xFE40  # 2.54mm Gold-plated pin headers
COL_HEADER_BASE  = 0x1082  # Black plastic pin header runner
COL_USBC_SHELL   = 0xEF7D  # Polished stainless steel USB-C jacket
COL_USBC_PORT    = 0x0821  # Dark port cavity
COL_ANTENNA_PCB  = 0x0962  # Serpentine antenna top trace zone
COL_LED_BLUE     = 0x07FF  # Glowing onboard active LED
COL_BUTTON_SILV  = 0xDEFB  # Tactile switch casing

# Directional studio lighting vector
LIGHT_DX = 0.45
LIGHT_DY = 0.75
LIGHT_DZ = -0.48
inv_l = 1.0 / math.sqrt(LIGHT_DX * LIGHT_DX + LIGHT_DY * LIGHT_DY + LIGHT_DZ * LIGHT_DZ)
LIGHT_DX *= inv_l
LIGHT_DY *= inv_l
LIGHT_DZ *= inv_l

# -------------------------------------------------------------------------
# 3D Board Component Layout (Relative to PCB center)
# PCB Dimensions: Width = 1.6, Height = 2.4, Depth = 0.1
# -------------------------------------------------------------------------
# Vertices for the main PCB slab (Front: 0-3, Back: 4-7)
PCB_VERTS = [
    (-0.80,  1.20,  0.05),  # 0: Top-Left Front
    ( 0.80,  1.20,  0.05),  # 1: Top-Right Front
    ( 0.80, -1.20,  0.05),  # 2: Bot-Right Front
    (-0.80, -1.20,  0.05),  # 3: Bot-Left Front
    (-0.80,  1.20, -0.05),  # 4: Top-Left Back
    ( 0.80,  1.20, -0.05),  # 5: Top-Right Back
    ( 0.80, -1.20, -0.05),  # 6: Bot-Right Back
    (-0.80, -1.20, -0.05),  # 7: Bot-Left Back
]

# Triangles (v0, v1, v2, material: 0=PCB Face, 1=PCB Edge)
PCB_TRIS = [
    # Front Face
    (0, 3, 1, 0), (1, 3, 2, 0),
    # Back Face
    (4, 5, 7, 0), (5, 6, 7, 0),
    # Left Edge
    (4, 7, 0),    (0, 7, 3),
    # Right Edge
    (1, 2, 5),    (5, 2, 6),
    # Top Edge
    (4, 0, 5),    (5, 0, 1),
    # Bottom Edge
    (3, 7, 2),    (2, 7, 6),
]

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
def fill_circle_fast(cx: int, cy: int, r: int, hi: int, lo: int, buf):
    pitch = 760
    if r < 1: return
    r2 = r * r
    y0 = max(0, cy - r)
    y1 = min(299, cy + r)

    for y in range(y0, y1 + 1):
        dy = y - cy
        span = r2 - (dy * dy)
        if span >= 0:
            hw = int(math.sqrt(span))
            x0 = max(0, cx - hw)
            x1 = min(379, cx + hw)
            offset = y * pitch + (x0 << 1)
            for _ in range(x1 - x0 + 1):
                buf[offset] = hi
                buf[offset + 1] = lo
                offset += 2

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
# 3D Board Projection & Component Detailing
# -------------------------------------------------------------------------
def render_esp32_board(yaw_deg: float, pitch_deg: float):
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)

    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    cos_p, sin_p = math.cos(pitch), math.sin(pitch)

    world_v = []
    screen_v = []

    # Transform 8 primary vertices of the PCB substrate
    for vx, vy, vz in PCB_VERTS:
        # Rotate Yaw (around Y)
        rx = vx * cos_y + vz * sin_y
        ry = vy
        rz = -vx * sin_y + vz * cos_y

        # Rotate Pitch (around X)
        px = rx
        py = ry * cos_p - rz * sin_p
        pz = ry * sin_p + rz * cos_p

        wz = pz + CAM_Z
        world_v.append((px, py, wz))

        inv_wz = 1.0 / wz
        sx = int(190 + (px * FOV * inv_wz))
        sy = int(145 - (py * FOV * inv_wz))
        screen_v.append((sx, sy))

    frame_min_y = 300
    frame_max_y = 0

    # 1. Floor Drop Shadow
    shadow_w = int(70.0 * (0.8 + 0.2 * abs(cos_y)))
    fill_rect(190 - shadow_w, 260, shadow_w << 1, 16, 0xCE, 0x59, FRAME_BUF)

    # 2. Render Main PCB Slab
    render_queue = []
    for f in PCB_TRIS:
        p0, p1, p2 = world_v[f[0]], world_v[f[1]], world_v[f[2]]
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

            dot_l = nx * LIGHT_DX + ny * LIGHT_DY - nz * LIGHT_DZ
            diff = dot_l if dot_l > 0.0 else 0.0

            # Matte black solder-mask with lighting
            val = int(24 + 40 * diff)
            hi = ((val >> 3) << 3) | ((val >> 2) >> 3)
            lo = (((val >> 2) & 0x07) << 5) | (val >> 3)

            avg_z = (p0[2] + p1[2] + p2[2]) * 0.3333
            tri_pts = (screen_v[f[0]], screen_v[f[1]], screen_v[f[2]])
            render_queue.append((avg_z, tri_pts, hi, lo, 0x08, 0x41))

    render_queue.sort(key=lambda item: item[0], reverse=True)
    for _, pts, hi, lo, e_hi, e_lo in render_queue:
        t_min, t_max = raster_triangle(pts[0], pts[1], pts[2], hi, lo, e_hi, e_lo)
        if t_min < frame_min_y: frame_min_y = t_min
        if t_max > frame_max_y: frame_max_y = t_max

    # 3. Project Detail Components if Front Face is Visible
    front_facing = cos_y > 0.0
    if front_facing:
        def project_local_point(lx, ly, lz):
            rx = lx * cos_y + lz * sin_y
            ry = ly
            rz = -lx * sin_y + lz * cos_y
            px = rx
            py = ry * cos_p - rz * sin_p
            pz = ry * sin_p + rz * cos_p
            wz = pz + CAM_Z
            inv_w = 1.0 / wz
            return int(190 + (px * FOV * inv_w)), int(145 - (py * FOV * inv_w)), inv_w

        # --- PCB Serpentine Antenna Area (Top Strip) ---
        ax0, ay0, _ = project_local_point(-0.65,  1.10, 0.06)
        ax1, ay1, _ = project_local_point( 0.65,  0.88, 0.06)
        fill_rect(min(ax0, ax1), min(ay0, ay1), abs(ax1 - ax0), abs(ay1 - ay0), 0x09, 0x62, FRAME_BUF)

        # --- Silver Metal RF Shielding Can (ESP32-S3-WROOM) ---
        cx0, cy0, _ = project_local_point(-0.55,  0.75, 0.08)
        cx1, cy1, _ = project_local_point( 0.55, -0.10, 0.08)
        can_w = abs(cx1 - cx0)
        can_h = abs(cy1 - cy0)
        can_x = min(cx0, cx1)
        can_y = min(cy0, cy1)
        fill_rect(can_x, can_y, can_w, can_h, 0xCE, 0x79, FRAME_BUF)
        # Laser-etched logo band across the can
        fill_rect(can_x + 4, can_y + (can_h >> 1) - 4, can_w - 8, 8, 0x84, 0x10, FRAME_BUF)

        # --- Dual USB-C Metal Connectors (Bottom Edge) ---
        # UART Port (Left)
        u1x, u1y, _ = project_local_point(-0.45, -1.25, 0.08)
        fill_rect(u1x - 7, u1y - 8, 14, 10, 0xEF, 0x7D, FRAME_BUF)
        fill_rect(u1x - 4, u1y - 6, 8, 6, 0x08, 0x21, FRAME_BUF)

        # Native USB Port (Right)
        u2x, u2y, _ = project_local_point( 0.45, -1.25, 0.08)
        fill_rect(u2x - 7, u2y - 8, 14, 10, 0xEF, 0x7D, FRAME_BUF)
        fill_rect(u2x - 4, u2y - 6, 8, 6, 0x08, 0x21, FRAME_BUF)

        # --- Tactile Buttons (BOOT & RESET) ---
        b1x, b1y, _ = project_local_point(-0.45, -0.80, 0.07)
        fill_rect(b1x - 4, b1y - 4, 8, 8, 0xDE, 0xFB, FRAME_BUF)
        fill_rect(b1x - 2, b1y - 2, 4, 4, 0x10, 0x82, FRAME_BUF)

        b2x, b2y, _ = project_local_point( 0.45, -0.80, 0.07)
        fill_rect(b2x - 4, b2y - 4, 8, 8, 0xDE, 0xFB, FRAME_BUF)
        fill_rect(b2x - 2, b2y - 2, 4, 4, 0x10, 0x82, FRAME_BUF)

        # --- Glowing RGB / Status LED ---
        lx, ly, _ = project_local_point(-0.45, -0.55, 0.07)
        fill_circle_fast(lx, ly, 4, 0x07, 0xFF, FRAME_BUF)
        fill_rect(lx - 1, ly - 1, 2, 2, 0xFF, 0xFF, FRAME_BUF)

        # --- Dual 15-Pin DIP Headers (Left and Right Rails) ---
        for i in range(12):
            py_pos = 0.90 - (i * 0.16)
            # Left Header Pin (Gold Pad)
            hpx, hpy, _ = project_local_point(-0.72, py_pos, 0.07)
            fill_rect(hpx - 2, hpy - 2, 5, 5, 0xFE, 0x40, FRAME_BUF)
            fill_rect(hpx - 1, hpy - 1, 2, 2, 0x10, 0x82, FRAME_BUF)

            # Right Header Pin (Gold Pad)
            hpx2, hpy2, _ = project_local_point( 0.72, py_pos, 0.07)
            fill_rect(hpx2 - 2, hpy2 - 2, 5, 5, 0xFE, 0x40, FRAME_BUF)
            fill_rect(hpx2 - 1, hpy2 - 1, 2, 2, 0x10, 0x82, FRAME_BUF)

    return frame_min_y, frame_max_y

# -------------------------------------------------------------------------
# Main Showcase Loop
# -------------------------------------------------------------------------
def run():
    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        offset = y * ROW_PITCH
        FRAME_BUF[offset:offset + ROW_PITCH] = WHITE_ROW

    moclcd.fill_rect(10, 10, 240, 12, 0xFFFF)
    moclcd.draw_text(10, 10, "ESP32-S3 // HARDWARE 3D", 0x0000, 0xFFFF)

    yaw_angle = 0.0
    pitch_angle = -18.0  # Angled perspective tilt to show headers and USB-C

    # Initial frame blit
    f_min, f_max = render_esp32_board(yaw_angle, pitch_angle)
    blit_top = max(0, f_min)
    blit_bottom = min(299, f_max)
    blit_h = blit_bottom - blit_top + 1
    start_offset = blit_top * ROW_PITCH
    end_offset = start_offset + (blit_h * ROW_PITCH)
    moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])
    prev_min_y = f_min
    prev_max_y = f_max

    time.sleep_ms(800)

    # Turntable loop
    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_ROW)

        # Smooth clockwise rotation
        yaw_angle = (yaw_angle + 2.4) % 360.0

        f_min, f_max = render_esp32_board(yaw_angle, pitch_angle)

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
      
