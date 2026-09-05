# =====================================================================================
#  3D REAL-TIME BOUNCING SPHERE WITH REALISTIC LIGHTING & DYNAMIC SHADOW
#  DRIVER:      moclcd v1.5.0 STABLE
#  TARGET:      ESP32-S3, 8-bit Parallel Intel 8080 LCD (ILI9488, 480x320)
#
#  FEATURES:
#  - Driver retained on moclcd v1.5.0 STABLE startup sequence.
#  - Crisp Studio White background (0xFFFF) with corner FPS readout.
#  - Analytical ray-sphere shader: Smooth per-pixel 3D spherical curvature with
#    dual-source Blinn-Phong shading (diffuse key, rim fill, specular glint).
#  - Physical gravity and restitution: Sphere bounces vertically from top to bottom
#    onto the virtual floor plane (VIRTUAL_FLOOR_Y = -1.45).
#  - Dynamic Ground Shadow: Projects onto the floor plane directly beneath the sphere.
#    As the sphere falls closer to the floor, the shadow shrinks, tightens, and
#    darkens; as it bounces upward, the shadow expands and softens.
# =====================================================================================

import math
import time
import machine
import moclcd
import micropython

# Lock CPU to peak 240 MHz silicon clock
machine.freq(240_000_000)

WIDTH  = 480
HEIGHT = 320
CX     = 240
CY     = 150
FOV    = 240.0
CAM_Z  = 3.8

# -------------------------------------------------------------------------
# EXACT WORKING HARDWARE STARTUP SEQUENCE (moclcd v1.5.0 STABLE)
# -------------------------------------------------------------------------
moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)  # Hardware boot confirmation
moclcd.fill_screen(0xFFFF)  # Studio White canvas

# -------------------------------------------------------------------------
# Lighting & Projection Constants
# -------------------------------------------------------------------------
KX, KY, KZ = 0.57735, 0.57735, -0.57735
FX, FY, FZ = -0.4082, -0.8165, 0.4082
HX, HY, HZ = 0.3714, 0.3714, -0.8510

VIRTUAL_FLOOR_Y = -1.45
SPHERE_RADIUS   = 0.70

# Bounding box dimensions (centered on 480x320 screen)
BB_W = 280
BB_H = 300
BB_X = CX - 140  # 100
BB_Y = CY - 150  # 0
ROW_PITCH = 560  # 280 pixels * 2 bytes

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * ROW_PITCH)

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    for y in range(y0, y1 + 1):
        idx = y * 560
        buf[idx:idx + 560] = white_row

@micropython.native
def render_shadow_disk(cx: int, cy: int, rx: int, ry: int, density: int, buf):
    """Renders a soft, perspective-projected elliptical shadow onto the virtual floor."""
    if rx <= 0 or ry <= 0:
        return cy, cy

    rx2 = rx * rx
    ry2 = ry * ry

    # Shadow color in RGB565 (darker when closer, softer when higher)
    c_val = max(0x60, 0xCE - (density * 18))
    hi = ((c_val & 0xF8) | (c_val >> 5)) & 0xFF
    lo = (((c_val & 0x1C) << 3) | (c_val >> 3)) & 0xFF

    y_min = cy - ry
    y_max = cy + ry
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - cy
        dy2 = dy * dy
        span_norm = 1.0 - (dy2 / ry2)
        if span_norm > 0.0:
            half_w = int(rx * math.sqrt(span_norm))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 >= 280: x1 = 279

            offset = y * 560 + (x0 << 1)
            count = x1 - x0 + 1
            for _ in range(count):
                buf[offset] = hi
                buf[offset + 1] = lo
                offset += 2

    return y_min, y_max

@micropython.native
def render_sphere(cx: int, cy: int, r_screen: int, buf):
    """Analytically renders a 3D smooth lit sphere directly into the framebuffer."""
    r2 = r_screen * r_screen
    inv_r = 1.0 / r_screen

    y_min = cy - r_screen
    y_max = cy + r_screen
    if y_min < 0: y_min = 0
    if y_max >= 300: y_max = 299

    for y in range(y_min, y_max + 1):
        dy = y - cy
        # Flip dy so positive dy in world calculation points upward
        ny_base = -dy * inv_r
        dy2 = dy * dy
        span_w2 = r2 - dy2
        if span_w2 >= 0:
            half_w = int(math.sqrt(span_w2))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0: x0 = 0
            if x1 >= 280: x1 = 279

            offset = y * 560 + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = x - cx
                nx = dx * inv_r
                ny = ny_base
                nz2 = 1.0 - (nx * nx + ny * ny)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    # 1. Diffuse Key Light
                    dot_k = nx * 0.57735 + ny * 0.57735 - nz * 0.57735
                    diff_k = dot_k if dot_k > 0.0 else 0.0

                    # 2. Cool Rim Fill Light
                    dot_f = nx * -0.4082 + ny * -0.8165 + nz * 0.4082
                    diff_f = dot_f if dot_f > 0.0 else 0.0

                    # 3. Blinn-Phong Specular Highlight: ((h^2)^2)^2
                    dot_h = nx * 0.3714 + ny * 0.3714 - nz * 0.8510
                    if dot_h > 0.0:
                        h2 = dot_h * dot_h
                        h4 = h2 * h2
                        spec = h4 * h4
                    else:
                        spec = 0.0

                    # Azure Material Shading
                    r = 0.10 + 0.38 * diff_k + 0.10 * diff_f + spec * 1.35
                    g = 0.35 + 1.05 * diff_k + 0.25 * diff_f + spec * 1.35
                    b = 0.72 + 1.25 * diff_k + 0.50 * diff_f + spec * 1.45

                    if r > 1.0: r = 1.0
                    if g > 1.0: g = 1.0
                    if b > 1.0: b = 1.0

                    hi = ((int(r * 31.0) & 0x1F) << 3) | ((int(g * 63.0) >> 3) & 0x07)
                    lo = (((int(g * 63.0) & 0x07) << 5) | (int(b * 31.0) & 0x1F)) & 0xFF

                    buf[offset] = hi
                    buf[offset + 1] = lo
                offset += 2

    return y_min, y_max

def run():
    # Physics parameters
    pos_y = 1.10          # Starting height (world units)
    vel_y = 0.0           # Initial vertical velocity
    gravity = -0.011      # Downward acceleration
    restitution = -0.92   # Bounciness coefficient
    floor_contact_y = VIRTUAL_FLOOR_Y + SPHERE_RADIUS  # Bottom collision point

    prev_min_y = 0
    prev_max_y = 299

    # Pre-fill canvas with white
    for y in range(300):
        idx = y * 560
        FRAME_BUF[idx:idx + 560] = WHITE_CHUNK

    # FPS diagnostics
    fps_counter = 0
    t_last_fps = time.ticks_ms()
    fps_display_str = "FPS: --"

    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff

    # Initial FPS label
    moclcd.fill_rect(10, 10, 75, 12, 0xFFFF)
    moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)

    while True:
        # Clear only scanlines dirtied in the prior frame
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        # -----------------------------------------------------------------
        # 1. Physics Step (Gravity & Floor Bounce)
        # -----------------------------------------------------------------
        vel_y += gravity
        pos_y += vel_y

        if pos_y <= floor_contact_y:
            pos_y = floor_contact_y
            vel_y *= restitution
            # Prevent microscopic jitter when settling
            if abs(vel_y) < 0.015:
                vel_y = 0.16

        # -----------------------------------------------------------------
        # 2. Perspective Projection & Dynamic Shadow Calculations
        # -----------------------------------------------------------------
        inv_z = 1.0 / CAM_Z
        sphere_cx = int((CX) - BB_X)                           # 140 (center of bounding box)
        sphere_cy = int((CY - (pos_y * FOV * inv_z)) - BB_Y)   # Screen Y
        sphere_r  = int(SPHERE_RADIUS * FOV * inv_z)           # Screen pixel radius (~44px)

        # Distance above ground plane determines shadow scale and density
        altitude = pos_y - floor_contact_y
        shadow_cy = int((CY - (VIRTUAL_FLOOR_Y * FOV * inv_z)) - BB_Y)

        # As sphere approaches floor: shadow shrinks and becomes darker
        shadow_rx = int(sphere_r * (0.85 + altitude * 0.35))
        shadow_ry = int(shadow_rx * 0.32)
        shadow_density = max(1, min(6, int(6.0 - altitude * 2.2)))

        # -----------------------------------------------------------------
        # 3. Render Virtual Floor Shadow First
        # -----------------------------------------------------------------
        s_min_y, s_max_y = render_shadow_disk(sphere_cx, shadow_cy, shadow_rx, shadow_ry, shadow_density, FRAME_BUF)

        # -----------------------------------------------------------------
        # 4. Render 3D Sphere Over Shadow
        # -----------------------------------------------------------------
        sp_min_y, sp_max_y = render_sphere(sphere_cx, sphere_cy, sphere_r, FRAME_BUF)

        # -----------------------------------------------------------------
        # 5. Dynamic Dirty Window DMA Blit
        # -----------------------------------------------------------------
        frame_min_y = min(s_min_y, sp_min_y)
        frame_max_y = max(s_max_y, sp_max_y)

        frame_min_y = max(0, min(299, frame_min_y))
        frame_max_y = max(0, min(299, frame_max_y))

        blit_top = min(frame_min_y, prev_min_y)
        blit_bottom = max(frame_max_y, prev_max_y)
        blit_h = blit_bottom - blit_top + 1

        start_offset = blit_top * (BB_W * 2)
        end_offset = start_offset + (blit_h * BB_W * 2)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = frame_min_y
        prev_max_y = frame_max_y

        # -----------------------------------------------------------------
        # 6. Real-Time FPS Overlay
        # -----------------------------------------------------------------
        fps_counter += 1
        if fps_counter >= 12:
            now = ticks_ms()
            dt = ticks_diff(now, t_last_fps)
            if dt > 0:
                fps = (fps_counter * 1000.0) / dt
                fps_display_str = "FPS: {:.1f}".format(fps)
                moclcd.fill_rect(10, 10, 75, 10, 0xFFFF)
                moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)
            t_last_fps = now
            fps_counter = 0

if __name__ == "__main__":
    run()
