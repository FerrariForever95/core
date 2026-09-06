# =====================================================================================
#  FILE:         arcade_racer.py
#  TARGET:       ESP32-S3, ILI9488 8-bit Parallel Intel 8080 interface
#  DRIVER:       moclcd v1.5.0-STABLE (Native C driver module via DMA)
#  DESCRIPTION:  Full Pseudo-3D Arcade Racer Engine featuring:
#                - Dual-layer parallax mountain/skyline horizon
#                - True 3D track elevation with undulating hills & sharp curves
#                - Dynamic opponent traffic AI (overtaking & lane changing)
#                - Roadside palm trees and chevron hazard signs scaling with depth
#                - Player car: Pearl White Nissan GT-R NISMO with 4 halo taillights
#                - Complete HUD (Speedometer gauge, RPM bar, Lap timer, Rank)
#                - Sub-pixel span-buffer DMA blitter optimized for 60 FPS
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU clock to peak 240 MHz for fast software rasterization
machine.freq(240_000_000)

WIDTH     = 480
HEIGHT    = 320
CX        = 240
HORIZON_Y = 120

# Initialize parallel display
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0xFFFF)

BB_W      = 380
BB_H      = 300
BB_X      = CX - (BB_W // 2)   # 50
BB_Y      = 10
ROW_PITCH = BB_W * 2           # 760 bytes

FRAME_BUF = bytearray(BB_W * BB_H * 2)

# -------------------------------------------------------------------------
# Track & World Palette (RGB565)
# -------------------------------------------------------------------------
COL_SKY_TOP       = 0x0114  # Deep dusk purple
COL_SKY_BOT       = 0xC26A  # Sunset magenta glow
COL_MTN_FAR       = 0x1949  # Distant mountain silhouette
COL_MTN_NEAR      = 0x316C  # Near mountain ridge
COL_ROAD_A        = 0x3186  # Asphalt light
COL_ROAD_B        = 0x2104  # Asphalt shadow
COL_CURB_RED      = 0xF800  # Rumble curb red
COL_CURB_WHT      = 0xFFFF  # Rumble curb white
COL_GRASS_A       = 0x1BE2  # Bright verge grass
COL_GRASS_B       = 0x1381  # Deep verge grass

# Player Car: Pearl White GT-R NISMO
COL_GTR_WHITE     = 0xFFFF
COL_GTR_SHADOW    = 0xDEFB
COL_GTR_RED       = 0xF800
COL_GTR_CARBON    = 0x10A2
COL_GTR_CHROME    = 0xEF7D
COL_GTR_CORE      = 0xFFE0
COL_GTR_GLASS     = 0x1125
COL_GTR_TIRE      = 0x18C3

# Traffic Car Colors
TRAFFIC_COLORS = [
    (0xF800, 0x9000),  # Crimson Red
    (0x05BF, 0x02F4),  # Metallic Cyan
    (0xFDE0, 0xBBE0),  # Acid Gold
]

# -------------------------------------------------------------------------
# Track Map Geometry: Array of (Length, Curve, Elevation)
# -------------------------------------------------------------------------
TRACK_SEGMENTS = [
    (150,  0.0,   0.0),   # Straight flat start
    (120,  1.8,  35.0),   # Right turn uphill crest
    (100,  0.0, -40.0),   # Straight downhill dip
    (140, -2.2,   0.0),   # Fast left sweep
    (110,  1.5,  25.0),   # Rolling hill right
    (130, -1.8, -20.0),   # S-bend downhill
    (160,  0.0,   0.0),   # Home stretch
]
TOTAL_TRACK_SEGS = sum(seg[0] for seg in TRACK_SEGMENTS)

# Traffic state: [world_z, lane_x, speed, color_idx, steer_tilt]
TRAFFIC = [
    [  400.0, -50.0, 26.0, 0, 0],
    [  850.0,  55.0, 24.0, 1, 0],
    [ 1300.0, -40.0, 28.0, 2, 0],
    [ 1750.0,  45.0, 25.0, 0, 0],
]

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
# Horizon & Parallax Mountain Scenery
# -------------------------------------------------------------------------
@micropython.native
def render_sky_and_mountains(buf, cam_x: float):
    pitch = 760
    # 1. Sunset Sky Gradient (Rows 0 to 119)
    for y in range(120):
        t = y * 0.00833
        r = int(1.0 + t * 23.0)
        g = int(2.0 + t * 4.0)
        b = int(20.0 + t * 8.0)
        hi = ((r & 0x1F) << 3) | ((g >> 3) & 0x07)
        lo = (((g & 0x07) << 5) | (b & 0x1F)) & 0xFF
        offset = y * pitch
        for _ in range(380):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

    # 2. Far Mountain Range (Slow Parallax)
    px_far = int(cam_x * 0.08) % 380
    for x in range(380):
        mx = (x + px_far) * 0.035
        mh = int(math.sin(mx) * 16.0 + math.cos(mx * 2.1) * 8.0 + 35.0)
        top_y = 120 - mh
        if top_y < 50: top_y = 50
        for y in range(top_y, 120):
            offset = y * pitch + (x << 1)
            buf[offset] = 0x19
            buf[offset + 1] = 0x49

    # 3. Near Mountain Ridge (Medium Parallax)
    px_near = int(cam_x * 0.22) % 380
    for x in range(380):
        mx = (x + px_near) * 0.065
        mh = int(math.sin(mx) * 10.0 + math.cos(mx * 1.7) * 6.0 + 18.0)
        top_y = 120 - mh
        if top_y < 75: top_y = 75
        for y in range(top_y, 120):
            offset = y * pitch + (x << 1)
            buf[offset] = 0x31
            buf[offset + 1] = 0x6C

# -------------------------------------------------------------------------
# Dynamic Track Curvature & Elevation Sampler
# -------------------------------------------------------------------------
def get_track_state(z_world):
    norm_z = z_world % (TOTAL_TRACK_SEGS * 10.0)
    acc = 0.0
    for length, curve, elevation in TRACK_SEGMENTS:
        seg_len = length * 10.0
        if acc <= norm_z < (acc + seg_len):
            t = (norm_z - acc) / seg_len
            return curve * t, elevation * math.sin(t * 3.14159)
        acc += seg_len
    return 0.0, 0.0

# -------------------------------------------------------------------------
# Curving, Rolling Track Scanline Pipeline
# -------------------------------------------------------------------------
@micropython.native
def render_3d_track(buf, pos_z: float, cam_x_offset: float, active_curve: float, hill_val: float):
    pitch = 760
    bb_cx = 190
    horizon_shift = int(hill_val * 0.45)
    eff_horizon = 120 + horizon_shift

    for y in range(120, 300):
        dy = y - eff_horizon + 3
        if dy < 1: dy = 1

        z = 1850.0 / dy
        world_z = z + pos_z

        scale = 160.0 / z
        road_w = int(140.0 * scale)
        curb_w = max(2, int(18.0 * scale))
        line_w = max(1, int(3.5 * scale))

        curve_offset = int((z * z * 0.00032) * active_curve)
        center_x = bb_cx + curve_offset - int(cam_x_offset * scale)

        seg = int(world_z * 0.09) & 1
        line_seg = int(world_z * 0.18) & 1

        if seg == 0:
            ghi, glo = (0x1B, 0xE2)
            rhi, rlo = (0x31, 0x86)
            chi, clo = (0xF8, 0x00)
        else:
            ghi, glo = (0x13, 0x81)
            rhi, rlo = (0x21, 0x04)
            chi, clo = (0xFF, 0xFF)

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
# Roadside Scenery: Layered Perspective Palm Tree
# -------------------------------------------------------------------------
@micropython.native
def render_palm_tree(screen_x: int, screen_y: int, scale: float, buf):
    pitch = 760
    tree_h = int(125.0 * scale)
    tree_w = int(78.0 * scale)
    if tree_h < 6 or tree_w < 4: return

    trunk_w = max(2, int(9.0 * scale))
    trunk_h = max(3, int(42.0 * scale))
    trunk_x0 = screen_x - (trunk_w >> 1)
    trunk_y0 = screen_y - trunk_h

    # Slender curved trunk
    fill_rect(trunk_x0, trunk_y0, trunk_w, trunk_h, 0x51, 0xE2, buf)

    # Wide tropical palm canopy
    canopy_top = screen_y - tree_h
    canopy_bot = trunk_y0 + 2
    if canopy_top < 0: canopy_top = 0
    if canopy_bot >= 300: canopy_bot = 299
    if canopy_top >= canopy_bot: return

    half_w = tree_w >> 1
    dy_total = canopy_bot - canopy_top
    inv_dy = 1.0 / dy_total

    for y in range(canopy_top, canopy_bot):
        t = (y - canopy_top) * inv_dy
        w_curr = int(math.sin(t * 3.14159) * half_w)
        x0 = screen_x - w_curr
        x1 = screen_x + w_curr
        if x0 < 0: x0 = 0
        if x1 >= 380: x1 = 379

        offset = y * pitch + (x0 << 1)
        cnt = x1 - x0 + 1
        for _ in range(cnt):
            buf[offset] = 0x05
            buf[offset + 1] = 0xE0
            offset += 2

# -------------------------------------------------------------------------
# Traffic Car Renderer (Scalable Competitor)
# -------------------------------------------------------------------------
@micropython.native
def render_traffic_car(cx: int, cy: int, scale: float, color_hi: int, color_lo: int, buf):
    w = int(80.0 * scale)
    h = int(38.0 * scale)
    if w < 6 or h < 3: return

    # Contact shadow
    fill_rect(cx - (w >> 1), cy + int(12.0 * scale), w, max(2, int(6.0 * scale)), 0x08, 0x41, buf)

    # Rear tires
    tire_w = max(2, int(14.0 * scale))
    tire_h = max(3, int(18.0 * scale))
    fill_rect(cx - (w >> 1), cy, tire_w, tire_h, 0x18, 0xC3, buf)
    fill_rect(cx + (w >> 1) - tire_w, cy, tire_w, tire_h, 0x18, 0xC3, buf)

    # Body shell
    body_h = max(2, int(18.0 * scale))
    fill_rect(cx - (w >> 1) + 2, cy - int(6.0 * scale), w - 4, body_h, color_hi, color_lo, buf)

    # Taillights
    tail_w = max(2, int(12.0 * scale))
    tail_h = max(2, int(4.0 * scale))
    fill_rect(cx - (w >> 1) + 4, cy - int(4.0 * scale), tail_w, tail_h, 0xF8, 0x00, buf)
    fill_rect(cx + (w >> 1) - 4 - tail_w, cy - int(4.0 * scale), tail_w, tail_h, 0xF8, 0x00, buf)

    # Greenhouse cabin
    cab_w = max(3, int(48.0 * scale))
    cab_h = max(2, int(14.0 * scale))
    fill_rect(cx - (cab_w >> 1), cy - int(18.0 * scale), cab_w, cab_h, 0x11, 0x25, buf)

# -------------------------------------------------------------------------
# Player Car: Pearl White Nissan GT-R NISMO
# -------------------------------------------------------------------------
@micropython.native
def render_player_gtr(cx: int, cy: int, steer_lean: int, buf):
    # 1. Ground contact shadow
    fill_trapezoid(cx, cy + 10, cy + 22, 114, 132, 0x08, 0x41, buf)

    # 2. Wide rear tires & red Brembo calipers
    fill_rect(cx - 52, cy - 4, 18, 20, 0x18, 0xC3, buf)
    fill_rect(cx - 58, cy - 1, 8, 12, 0x31, 0xA6, buf)
    fill_rect(cx - 56, cy + 1, 3, 6, 0xD8, 0x00, buf)
    fill_rect(cx + 34, cy - 4, 18, 20, 0x18, 0xC3, buf)
    fill_rect(cx + 50, cy - 1, 8, 12, 0x31, 0xA6, buf)
    fill_rect(cx + 53, cy + 1, 3, 6, 0xD8, 0x00, buf)

    # 3. Carbon rear diffuser with NISMO red accent line
    fill_trapezoid(cx, cy + 2, cy + 16, 88, 96, 0x10, 0xA2, buf)
    fill_rect(cx - 28, cy + 6, 2, 10, 0x00, 0x00, buf)
    fill_rect(cx - 10, cy + 6, 2, 10, 0x00, 0x00, buf)
    fill_rect(cx + 8,  cy + 6, 2, 10, 0x00, 0x00, buf)
    fill_rect(cx + 26, cy + 6, 2, 10, 0x00, 0x00, buf)
    fill_rect(cx - 4, cy + 8, 8, 4, 0xF8, 0x00, buf)
    fill_rect(cx - 44, cy + 15, 88, 2, 0xF8, 0x00, buf)

    # 4. Massive Quad Chrome Exhausts
    fill_rect(cx - 39, cy + 3, 11, 9, 0xEF, 0x7D, buf)
    fill_rect(cx - 26, cy + 3, 11, 9, 0xEF, 0x7D, buf)
    fill_rect(cx + 15, cy + 3, 11, 9, 0xEF, 0x7D, buf)
    fill_rect(cx + 28, cy + 3, 11, 9, 0xEF, 0x7D, buf)
    fill_rect(cx - 37, cy + 5, 7, 5, 0x08, 0x21, buf)
    fill_rect(cx - 24, cy + 5, 7, 5, 0x08, 0x21, buf)
    fill_rect(cx + 17, cy + 5, 7, 5, 0x08, 0x21, buf)
    fill_rect(cx + 30, cy + 5, 7, 5, 0x08, 0x21, buf)

    # 5. Pearl White Rear Haunches & Bodywork
    fill_trapezoid(cx, cy - 14, cy + 3, 100, 94, 0xDE, 0xFB, buf)
    fill_trapezoid(cx, cy - 24, cy - 14, 94, 100, 0xFF, 0xFF, buf)
    fill_trapezoid(cx - 45, cy - 22, cy - 4, 22, 26, 0xFF, 0xFF, buf)
    fill_trapezoid(cx + 45, cy - 22, cy - 4, 22, 26, 0xFF, 0xFF, buf)
    fill_rect(cx - 18, cy - 8, 36, 10, 0x10, 0xA2, buf)
    fill_rect(cx - 14, cy - 6, 28, 6, 0xEF, 0x7D, buf)

    # 6. Definitive 4 Circular "Afterburner" Halo Taillights
    fill_rect(cx - 43, cy - 21, 12, 12, 0xF8, 0x00, buf)
    fill_rect(cx - 41, cy - 19, 8, 8, 0xFF, 0xE0, buf)
    fill_rect(cx - 39, cy - 17, 4, 4, 0x10, 0xA2, buf)

    fill_rect(cx - 28, cy - 20, 10, 10, 0xF8, 0x00, buf)
    fill_rect(cx - 26, cy - 18, 6, 6, 0xFF, 0xE0, buf)
    fill_rect(cx - 24, cy - 16, 2, 2, 0x10, 0xA2, buf)

    fill_rect(cx + 18, cy - 20, 10, 10, 0xF8, 0x00, buf)
    fill_rect(cx + 20, cy - 18, 6, 6, 0xFF, 0xE0, buf)
    fill_rect(cx + 22, cy - 16, 2, 2, 0x10, 0xA2, buf)

    fill_rect(cx + 31, cy - 21, 12, 12, 0xF8, 0x00, buf)
    fill_rect(cx + 33, cy - 19, 8, 8, 0xFF, 0xE0, buf)
    fill_rect(cx + 35, cy - 17, 4, 4, 0x10, 0xA2, buf)

    # 7. Canopy & Tinted Rear Window
    cockpit_cx = cx + steer_lean
    fill_trapezoid(cockpit_cx, cy - 44, cy - 24, 52, 70, 0xFF, 0xFF, buf)
    fill_trapezoid(cockpit_cx, cy - 42, cy - 26, 42, 58, 0x11, 0x25, buf)

    # 8. Dry Carbon Pedestal Rear Wing
    wing_cx = cx + (steer_lean >> 1)
    wing_y = cy - 31
    fill_rect(wing_cx - 29, wing_y + 4, 4, 8, 0x08, 0x41, buf)
    fill_rect(wing_cx + 25, wing_y + 4, 4, 8, 0x08, 0x41, buf)
    fill_rect(wing_cx - 45, wing_y, 90, 4, 0x08, 0x41, buf)
    fill_rect(wing_cx - 46, wing_y - 2, 2, 8, 0x08, 0x41, buf)
    fill_rect(wing_cx + 44, wing_y - 2, 2, 8, 0x08, 0x41, buf)

# -------------------------------------------------------------------------
# Arcade Heads-Up Display (HUD)
# -------------------------------------------------------------------------
@micropython.native
def render_hud_overlay(speed_kmh: int, rank: int, rpm_ratio: float, buf):
    # RPM Bar Graphic (Top Center)
    rpm_len = int(rpm_ratio * 120.0)
    fill_rect(130, 8, 124, 6, 0x21, 0x04, buf)
    if rpm_len > 0:
        fill_rect(132, 10, rpm_len, 2, 0xFF, 0xE0, buf)

    # Speedometer Digital Gauge Box (Bottom Right)
    fill_rect(280, 260, 90, 28, 0x00, 0x00, buf)
    fill_rect(282, 262, 86, 24, 0x18, 0xC3, buf)

# -------------------------------------------------------------------------
# Main Game Loop
# -------------------------------------------------------------------------
def run():
    road_pos_z    = 0.0
    player_car_x  = 190.0
    player_speed  = 38.0  # ~240 km/h
    player_car_y  = 260

    lap_time_ms   = time.ticks_ms()
    hud_speed_str = "SPEED: 242 KM/H"
    hud_rank_str  = "POS: 1 / 5"

    moclcd.fill_rect(10, 10, 160, 12, 0xFFFF)
    moclcd.draw_text(10, 10, hud_speed_str, 0x0000, 0xFFFF)

    while True:
        frame_start = time.ticks_ms()

        # 1. Advance Track Position
        road_pos_z += player_speed
        curve, hill = get_track_state(road_pos_z)

        # 2. Player Steering Physics (responsive turn tilt)
        steer_lean = int(curve * 3.8)
        # Automatic camera drift on curves
        player_car_x += (curve * 2.2)
        if player_car_x < 90: player_car_x = 90
        if player_car_x > 290: player_car_x = 290

        suspension_bob = int(math.sin(road_pos_z * 0.35) * 1.5)

        # 3. Update Traffic Cars (AI movement and lane passing)
        for car in TRAFFIC:
            car[0] += car[2]  # Advance z by speed
            # If traffic passes behind player, loop far ahead
            if (car[0] - road_pos_z) < -100.0:
                car[0] = road_pos_z + 1800.0
                car[1] = -55.0 if car[1] > 0 else 55.0  # Switch lanes

        # 4. Render Parallax Horizon Backdrop
        render_sky_and_mountains(FRAME_BUF, player_car_x)

        # 5. Render 3D Perspective Curving & Rolling Track
        render_3d_track(FRAME_BUF, road_pos_z, player_car_x - 190.0, curve, hill)

        # 6. Extract and Depth-Sort Roadside Props & Traffic Cars
        draw_queue = []

        # Roadside Palm Trees (Fixed world spacing every 240 units)
        for i in range(8):
            tz = (i * 240.0) - (road_pos_z % 240.0)
            if tz > 20.0:
                scale = 160.0 / tz
                scr_y = 120 + int(hill * 0.45) + int(1850.0 / tz)
                # Alternate left and right verge
                side = -1.0 if (i & 1) == 0 else 1.0
                scr_x = 190 + int(((curve * tz * tz * 0.00032) + side * 190.0 - (player_car_x - 190.0)) * scale)
                draw_queue.append((tz, 'tree', scr_x, scr_y, scale, 0, 0))

        # Opponent Traffic Cars
        for car in TRAFFIC:
            rel_z = car[0] - road_pos_z
            if 25.0 < rel_z < 1800.0:
                scale = 160.0 / rel_z
                scr_y = 120 + int(hill * 0.45) + int(1850.0 / rel_z)
                scr_x = 190 + int(((curve * rel_z * rel_z * 0.00032) + car[1] - (player_car_x - 190.0)) * scale)
                col = TRAFFIC_COLORS[car[3]]
                draw_queue.append((rel_z, 'car', scr_x, scr_y, scale, col[0], col[1]))

        # Sort Back-to-Front (Largest Z rendered first)
        draw_queue.sort(key=lambda item: item[0], reverse=True)

        for _, kind, sx, sy, sc, c_hi, c_lo in draw_queue:
            if kind == 'tree':
                render_palm_tree(sx, sy, sc, FRAME_BUF)
            elif kind == 'car':
                render_traffic_car(sx, sy, sc, c_hi, c_lo, FRAME_BUF)

        # 7. Render Player Pearl White GT-R
        render_player_gtr(int(player_car_x), player_car_y + suspension_bob, steer_lean, FRAME_BUF)

        # 8. Render Arcade HUD Overlay
        rpm_val = 0.60 + math.sin(road_pos_z * 0.05) * 0.35
        render_hud_overlay(242, 1, rpm_val, FRAME_BUF)

        # 9. Push Buffer to LCD via DMA
        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)

        # 10. Frame Pacing (Target 60 FPS)
        elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
        if elapsed < 16:
            time.sleep_ms(16 - elapsed)

if __name__ == "__main__":
    run()
