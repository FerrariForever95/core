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
FOV    = 240.0
CAM_Z  = 3.8

moclcd.init()
moclcd.panel_init()
moclcd.backlight(1)
moclcd.fill_screen(0xF800)
moclcd.fill_screen(0xFFFF)

VIRTUAL_FLOOR_Y = -1.45
SPHERE_RADIUS   = 0.70

BB_W = 280
BB_H = 300
BB_X = CX - 140
BB_Y = CY - 150
ROW_PITCH = 560

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * ROW_PITCH)

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    pitch = 560
    for y in range(y0, y1 + 1):
        idx = y * pitch
        buf[idx:idx + 560] = white_row

@micropython.native
def render_shadow_disk(cx: int, cy: int, rx: int, ry: int, density: int, buf):
    if rx <= 0 or ry <= 0:
        return cy, cy

    c_val = 206 - (density * 18)
    if c_val < 96:
        c_val = 96
    hi = (c_val & 0xF8) | (c_val >> 5)
    lo = ((c_val & 0x1C) << 3) | (c_val >> 3)

    y_min = cy - ry
    y_max = cy + ry
    if y_min < 0:
        y_min = 0
    if y_max >= 300:
        y_max = 299

    ry2 = ry * ry
    inv_ry2 = 1.0 / ry2
    for y in range(y_min, y_max + 1):
        dy = y - cy
        span_norm = 1.0 - (dy * dy * inv_ry2)
        if span_norm > 0.0:
            half_w = int(rx * math.sqrt(span_norm))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0:
                x0 = 0
            if x1 >= 280:
                x1 = 279

            offset = y * 560 + (x0 << 1)
            count = x1 - x0 + 1
            for _ in range(count):
                buf[offset] = hi
                buf[offset + 1] = lo
                offset += 2

    return y_min, y_max

@micropython.native
def render_sphere_squash(cx: int, cy: int, r_screen: int, sx: float, sy: float, buf):
    rx = int(r_screen * sx)
    ry = int(r_screen * sy)
    if rx < 1:
        rx = 1
    if ry < 1:
        ry = 1

    y_min = cy - ry
    y_max = cy + ry
    if y_min < 0:
        y_min = 0
    if y_max >= 300:
        y_max = 299

    inv_rx = 1.0 / rx
    inv_ry = 1.0 / ry
    ry2 = ry * ry
    inv_ry2 = 1.0 / ry2

    for y in range(y_min, y_max + 1):
        dy = y - cy
        dy2 = dy * dy
        norm_y = 1.0 - (dy2 * inv_ry2)
        if norm_y >= 0.0:
            half_w = int(rx * math.sqrt(norm_y))
            x0 = cx - half_w
            x1 = cx + half_w
            if x0 < 0:
                x0 = 0
            if x1 >= 280:
                x1 = 279

            ny_base = -dy * inv_ry
            ny2 = ny_base * ny_base
            dot_k_y = ny_base * 0.57735
            dot_f_y = ny_base * -0.8165
            dot_h_y = ny_base * 0.3714

            offset = y * 560 + (x0 << 1)
            for x in range(x0, x1 + 1):
                dx = x - cx
                nx = dx * inv_rx
                nz2 = 1.0 - (nx * nx + ny2)
                if nz2 > 0.0:
                    nz = math.sqrt(nz2)

                    dot_k = nx * 0.57735 + dot_k_y - nz * 0.57735
                    diff_k = dot_k if dot_k > 0.0 else 0.0

                    dot_f = nx * -0.4082 + dot_f_y + nz * 0.4082
                    diff_f = dot_f if dot_f > 0.0 else 0.0

                    dot_h = nx * 0.3714 + dot_h_y - nz * 0.8510
                    spec = 0.0
                    if dot_h > 0.0:
                        h2 = dot_h * dot_h
                        h4 = h2 * h2
                        spec = h4 * h4

                    r = 0.10 + 0.38 * diff_k + 0.10 * diff_f + spec * 1.35
                    g = 0.35 + 1.05 * diff_k + 0.25 * diff_f + spec * 1.35
                    b = 0.72 + 1.25 * diff_k + 0.50 * diff_f + spec * 1.45

                    if r > 1.0:
                        r = 1.0
                    if g > 1.0:
                        g = 1.0
                    if b > 1.0:
                        b = 1.0

                    ir = int(r * 31.0) & 0x1F
                    ig = int(g * 63.0) & 0x3F
                    ib = int(b * 31.0) & 0x1F

                    buf[offset] = (ir << 3) | (ig >> 3)
                    buf[offset + 1] = ((ig & 0x07) << 5) | ib
                offset += 2

    return y_min, y_max

def run():
    pos_y = 1.10
    vel_y = 0.0
    gravity = -0.012
    restitution = -0.93
    floor_contact_y = VIRTUAL_FLOOR_Y + SPHERE_RADIUS

    squash_x = 1.0
    squash_y = 1.0

    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        idx = y * 560
        FRAME_BUF[idx:idx + 560] = WHITE_CHUNK

    fps_counter = 0
    t_last_fps = time.ticks_ms()
    fps_display_str = "FPS: --"

    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff

    moclcd.fill_rect(10, 10, 75, 12, 0xFFFF)
    moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)

    inv_z = 1.0 / CAM_Z
    sphere_cx = int(CX - BB_X)
    fov_inv_z = FOV * inv_z
    shadow_cy = int((CY - (VIRTUAL_FLOOR_Y * fov_inv_z)) - BB_Y)
    sphere_r = int(SPHERE_RADIUS * fov_inv_z)

    while True:
        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        vel_y += gravity
        pos_y += vel_y

        if pos_y <= floor_contact_y:
            pos_y = floor_contact_y
            impact_energy = abs(vel_y)
            vel_y *= restitution
            if abs(vel_y) < 0.015:
                vel_y = 0.17

            squash_factor = impact_energy * 2.2
            if squash_factor > 0.38:
                squash_factor = 0.38
            squash_y = 1.0 - squash_factor
            squash_x = 1.0 + (squash_factor * 0.5)
        else:
            squash_x += (1.0 - squash_x) * 0.22
            squash_y += (1.0 - squash_y) * 0.22

        altitude = pos_y - floor_contact_y
        shadow_rx = int(sphere_r * (0.82 + altitude * 0.38) * squash_x)
        shadow_ry = int(shadow_rx * 0.30)
        dens_calc = int(6.0 - altitude * 2.4)
        if dens_calc < 1:
            shadow_density = 1
        elif dens_calc > 6:
            shadow_density = 6
        else:
            shadow_density = dens_calc

        s_min_y, s_max_y = render_shadow_disk(sphere_cx, shadow_cy, shadow_rx, shadow_ry, shadow_density, FRAME_BUF)

        sphere_cy = int((CY - (pos_y * fov_inv_z)) - BB_Y)
        sp_min_y, sp_max_y = render_sphere_squash(sphere_cx, sphere_cy, sphere_r, squash_x, squash_y, FRAME_BUF)

        frame_min_y = s_min_y if s_min_y < sp_min_y else sp_min_y
        frame_max_y = s_max_y if s_max_y > sp_max_y else sp_max_y

        if frame_min_y < 0:
            frame_min_y = 0
        if frame_max_y >= 300:
            frame_max_y = 299

        blit_top = frame_min_y if frame_min_y < prev_min_y else prev_min_y
        blit_bottom = frame_max_y if frame_max_y > prev_max_y else prev_max_y
        blit_h = blit_bottom - blit_top + 1

        start_offset = blit_top * 560
        end_offset = start_offset + (blit_h * 560)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = frame_min_y
        prev_max_y = frame_max_y

        fps_counter += 1
        if fps_counter >= 20:
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
