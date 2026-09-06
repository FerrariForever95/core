import math
import time
import machine
import moclcd
import micropython

# Lock CPU to peak 240 MHz clock
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 130  # Horizon line

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

# Colors in RGB565 format
COL_SKY_TOP    = 0x1127  # Dark Midnight Blue
COL_SKY_HORIZ  = 0x3A54  # Hazy Dusk Horizon
COL_GRASS_A    = 0x1BE2  # Forest Green
COL_GRASS_B    = 0x1381  # Deep Shadow Green
COL_ROAD_A     = 0x3186  # Dark Asphalt
COL_ROAD_B     = 0x2104  # Shadow Asphalt
COL_CURB_RED   = 0xD8A2  # Bright Red Curb
COL_CURB_WHT   = 0xFFFF  # White Curb
COL_LINE_WHT   = 0xFFFF  # White Lane Divider

# Car Colors
COL_CAR_BODY   = 0x0A9F  # Electric Cyan
COL_CAR_SHADOW = 0x0841  # Dark Shaded Body
COL_CAR_GLASS  = 0x18C5  # Tinted Obsidian Glass
COL_CAR_TAIL   = 0xF800  # Glowing Red Tail Light
COL_CAR_EXH    = 0xD69A  # Chrome Exhaust
COL_CAR_TIRE   = 0x18C3  # Matte Charcoal Rubber

# Tree Colors
COL_TRUNK      = 0x51E2  # Deep Bark Brown
COL_LEAF_DARK  = 0x0A82  # Forest Pine
COL_LEAF_LGT   = 0x1BE4  # Sunlit Foliage

# Road Geometry Parameters
ROAD_WORLD_WIDTH = 220.0
LANE_LINE_WIDTH  = 5.0
CURB_WIDTH       = 14.0

# Active roadside trees: list of [x_world, z_world]
# Negative X = Left side of road, Positive X = Right side of road
NUM_TREES = 8
TREES_X = [ -180.0,  180.0, -210.0,  200.0, -190.0,  220.0, -200.0,  190.0 ]
TREES_Z = [   40.0,   75.0,  110.0,  145.0,  180.0,  215.0,  250.0,  285.0 ]

# -------------------------------------------------------------------------
# Low-Level Rasterizers
# -------------------------------------------------------------------------
@micropython.native
def fill_rect_buf(x0: int, y0: int, w: int, h: int, hi: int, lo: int, buf):
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
def render_road_scanlines(buf, road_pos_z: float, cam_x_offset: float):
    pitch = 760
    bb_cx = 190

    # 1. Sky & Distant Horizon backdrop (Rows 0 to 119)
    for y in range(120):
        # Sky vertical gradient
        r = 16 + (y * 12) // 120
        g = 24 + (y * 24) // 120
        b = 50 + (y * 40) // 120
        hi = ((r & 0x1F) << 3) | ((g >> 3) & 0x07)
        lo = (((g & 0x07) << 5) | (b & 0x1F)) & 0xFF

        offset = y * pitch
        for _ in range(380):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

    # 2. Perspective Road Projection (Rows 120 to 299)
    for y in range(120, 300):
        dy = y - 118  # Vertical distance below vanishing horizon
        # Perspective depth (closer scanlines have smaller z)
        z = 1800.0 / dy
        world_z = z + road_pos_z

        # Perspective scale factor
        scale = 160.0 / z
        road_half_w = int(140.0 * scale)
        curb_w = int(16.0 * scale)
        lane_w = int(3.5 * scale)
        if lane_w < 1: lane_w = 1

        # Center of road on screen with subtle steering camera offset
        center_x = bb_cx + int(cam_x_offset * scale)

        # Alternating stripe frequency
        seg = int(world_z * 0.08) & 1
        line_seg = int(world_z * 0.16) & 1

        if seg == 0:
            grass_hi, grass_lo = (0x1B, 0xE2)
            road_hi, road_lo   = (0x31, 0x86)
            curb_hi, curb_lo   = (0xD8, 0xA2)
        else:
            grass_hi, grass_lo = (0x13, 0x81)
            road_hi, road_lo   = (0x21, 0x04)
            curb_hi, curb_lo   = (0xFF, 0xFF)

        offset = y * pitch

        # Road boundaries
        road_left  = center_x - road_half_w
        road_right = center_x + road_half_w
        curb_l0    = road_left - curb_w
        curb_r1    = road_right + curb_w

        line_left  = center_x - lane_w
        line_right = center_x + lane_w

        for x in range(380):
            if x < curb_l0 or x > curb_r1:
                # Roadside grass
                buf[offset] = grass_hi
                buf[offset + 1] = grass_lo
            elif x < road_left or x > road_right:
                # Red / White rumble curb
                buf[offset] = curb_hi
                buf[offset + 1] = curb_lo
            else:
                # Asphalt surface with white dashed center line
                if line_seg == 0 and (line_left <= x <= line_right):
                    buf[offset] = 0xFF
                    buf[offset + 1] = 0xFF
                else:
                    buf[offset] = road_hi
                    buf[offset + 1] = road_lo
            offset += 2

@micropython.native
def render_tree(screen_x: int, screen_y: int, scale: float, buf):
    pitch = 760
    tree_h = int(110.0 * scale)
    tree_w = int(64.0 * scale)
    if tree_h < 4 or tree_w < 3: return

    half_w = tree_w >> 1
    trunk_w = max(2, int(10.0 * scale))
    trunk_h = max(2, int(28.0 * scale))
    trunk_x0 = screen_x - (trunk_w >> 1)
    trunk_y0 = screen_y - trunk_h

    # Render Tree Trunk
    fill_rect_buf(trunk_x0, trunk_y0, trunk_w, trunk_h, 0x51, 0xE2, buf)

    # Render Layered Triangular Conical Foliage
    crown_y1 = trunk_y0
    crown_y0 = screen_y - tree_h
    if crown_y0 < 0: crown_y0 = 0
    if crown_y1 >= 300: crown_y1 = 299
    if crown_y0 >= crown_y1: return

    foliage_h = crown_y1 - crown_y0
    inv_h = 1.0 / foliage_h

    for y in range(crown_y0, crown_y1):
        ratio = (y - crown_y0) * inv_h
        layer_w = int(ratio * half_w)
        x0 = screen_x - layer_w
        x1 = screen_x + layer_w
        if x0 < 0: x0 = 0
        if x1 >= 380: x1 = 379

        offset = y * pitch + (x0 << 1)
        cnt = x1 - x0 + 1

        # Shaded left/right canopy for 3D depth
        half_cnt = cnt >> 1
        for _ in range(half_cnt):
            buf[offset] = 0x0A
            buf[offset + 1] = 0x82  # Shadow foliage
            offset += 2
        for _ in range(cnt - half_cnt):
            buf[offset] = 0x1B
            buf[offset + 1] = 0xE4  # Lit foliage
            offset += 2

@micropython.native
def render_car(car_x: int, car_y: int, steer_tilt: int, buf):
    # Dynamic Rear-View Sports Coupe (Centered around car_x, car_y)
    # Ground Contact Shadow
    fill_rect_buf(car_x - 48, car_y + 8, 96, 12, 0x10, 0x82, buf)

    # Rear Tires (Left and Right)
    fill_rect_buf(car_x - 46, car_y - 8, 14, 20, 0x18, 0xC3, buf)
    fill_rect_buf(car_x + 32, car_y - 8, 14, 20, 0x18, 0xC3, buf)

    # Lower Bumper / Carbon Diffuser
    fill_rect_buf(car_x - 42, car_y - 4, 84, 16, 0x10, 0x82, buf)

    # Dual Chrome Exhaust Tips
    fill_rect_buf(car_x - 22, car_y + 4, 8, 5, 0xD6, 0x9A, buf)
    fill_rect_buf(car_x + 14, car_y + 4, 8, 5, 0xD6, 0x9A, buf)

    # Main Sports Car Body (Lower Shell)
    fill_rect_buf(car_x - 44, car_y - 24, 88, 22, 0x0A, 0x9F, buf)
    fill_rect_buf(car_x - 44, car_y - 24, 88, 4, 0x08, 0x41, buf) # Body shadow crease

    # Glowing Neon Taillights
    fill_rect_buf(car_x - 40, car_y - 20, 18, 5, 0xF8, 0x00, buf)
    fill_rect_buf(car_x + 22, car_y - 20, 18, 5, 0xF8, 0x00, buf)

    # Tapered Cockpit & Tinted Rear Window
    roof_x = car_x - 28 + steer_tilt
    fill_rect_buf(roof_x, car_y - 44, 56, 20, 0x0A, 0x9F, buf)       # Outer frame
    fill_rect_buf(roof_x + 4, car_y - 41, 48, 15, 0x18, 0xC5, buf)   # Tinted Glass

    # Rear Deck Wing / Spoiler
    wing_x = car_x - 36 + steer_tilt
    fill_rect_buf(wing_x, car_y - 30, 72, 4, 0x08, 0x41, buf)
    fill_rect_buf(wing_x + 10, car_y - 26, 4, 4, 0x10, 0x82, buf)
    fill_rect_buf(wing_x + 58, car_y - 26, 4, 4, 0x10, 0x82, buf)

# -------------------------------------------------------------------------
# Main Game Loop
# -------------------------------------------------------------------------
def run(target_fps=60):
    target_frame_ms = 1000 // target_fps

    road_z = 0.0
    speed = 28.0   # World units per frame

    car_base_x = 190
    car_base_y = 265
    steer_phase = 0.0

    fps_counter = 0
    t_last_fps = time.ticks_ms()
    fps_display_str = "SPEED: 220 KM/H | 60 FPS"

    moclcd.fill_rect(10, 10, 190, 12, 0xFFFF)
    moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)

    while True:
        frame_start = time.ticks_ms()

        # Update road streaming position
        road_z += speed

        # Car steering oscillation & suspension bounce
        steer_phase += 0.05
        car_offset_x = math.sin(steer_phase) * 36.0
        steer_tilt = int(math.cos(steer_phase) * 3.0)
        suspension_bob = int(math.sin(road_z * 0.25) * 1.5)

        current_car_x = int(car_base_x + car_offset_x)
        current_car_y = car_base_y + suspension_bob

        # 1. Render road perspective scanlines
        render_road_scanlines(FRAME_BUF, road_z, -car_offset_x * 0.45)

        # 2. Update and render roadside trees (sorted by Z, back-to-front)
        tree_draw_list = []
        for i in range(NUM_TREES):
            # Advance tree relative to moving car
            tz = (TREES_Z[i] - (road_z % 280.0))
            if tz < 20.0:
                tz += 280.0

            scale = 160.0 / tz
            tx = 190 + int((TREES_X[i] - car_offset_x * 0.45) * scale)
            ty = 118 + int(1800.0 / tz)

            tree_draw_list.append((tz, tx, ty, scale))

        # Sort trees back-to-front
        tree_draw_list.sort(key=lambda item: item[0], reverse=True)
        for tz, tx, ty, scale in tree_draw_list:
            render_tree(tx, ty, scale, FRAME_BUF)

        # 3. Render chase-cam player car
        render_car(current_car_x, current_car_y, steer_tilt, FRAME_BUF)

        # 4. DMA Full Window Blit
        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)

        # Frame rate limiter
        elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
        if elapsed < target_frame_ms:
            time.sleep_ms(target_frame_ms - elapsed)

        fps_counter += 1
        if fps_counter >= 20:
            now = time.ticks_ms()
            dt = time.ticks_diff(now, t_last_fps)
            if dt > 0:
                actual_fps = (fps_counter * 1000.0) / dt
                fps_display_str = "SPEED: 220 KM/H | {:.1f} FPS".format(actual_fps)
                moclcd.fill_rect(10, 10, 190, 10, 0xFFFF)
                moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)
            t_last_fps = now
            fps_counter = 0

if __name__ == "__main__":
    run()
