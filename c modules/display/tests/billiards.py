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
SPHERE_RADIUS   = 0.44

BB_W = 380
BB_H = 300
BB_X = CX - 190
BB_Y = CY - 150
ROW_PITCH = 760

FRAME_BUF = bytearray(BB_W * BB_H * 2)
WHITE_CHUNK = bytearray([0xFF] * ROW_PITCH)

# Preallocated scratch arrays
PX = [ -0.75,  0.00,  0.75 ]
PY = [  0.60,  0.95,  0.30 ]
PZ = [  0.15, -0.20,  0.05 ]

VX = [  0.016, -0.012, -0.014 ]
VY = [  0.000,  0.000,  0.000 ]
VZ = [  0.011,  0.014, -0.018 ]

SQUASH_X = [ 1.0, 1.0, 1.0 ]
SQUASH_Y = [ 1.0, 1.0, 1.0 ]

# Base materials: 0: Ruby Red, 1: Cyan Teal, 2: Deep Gold
MAT_R = [ 0.90, 0.10, 0.92 ]
MAT_G = [ 0.15, 0.85, 0.70 ]
MAT_B = [ 0.20, 0.95, 0.12 ]

SCR_X = [0] * 3
SCR_Y = [0] * 3
SCR_R = [0] * 3
SHAD_CY = [0] * 3
SHAD_RX = [0] * 3
SHAD_RY = [0] * 3
SHAD_DENS = [0] * 3

SORT_ORDER = [0, 1, 2]

@micropython.native
def clear_dirty_rows(buf, y0: int, y1: int, white_row):
    pitch = 760
    for y in range(y0, y1 + 1):
        idx = y * pitch
        buf[idx:idx + 760] = white_row

@micropython.native
def render_shadow_disk(cx: int, cy: int, rx: int, ry: int, density: int, buf):
    pitch = 760
    if rx <= 0 or ry <= 0:
        return cy, cy

    c_val = 210 - (density * 16)
    if c_val < 90:
        c_val = 90
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
            if x1 >= 380:
                x1 = 379

            offset = y * pitch + (x0 << 1)
            count = x1 - x0 + 1
            for _ in range(count):
                curr_hi = buf[offset]
                curr_lo = buf[offset + 1]
                # Multiplicative shadow blending for overlapping ground footprints
                if curr_hi == 0xFF and curr_lo == 0xFF:
                    buf[offset] = hi
                    buf[offset + 1] = lo
                else:
                    # Darken overlapping intersection
                    buf[offset] = 0x63
                    buf[offset + 1] = 0x2C
                offset += 2

    return y_min, y_max

@micropython.native
def render_sphere_squash(cx: int, cy: int, r_screen: int, sx: float, sy: float,
                         base_r: float, base_g: float, base_b: float, buf):
    pitch = 760
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
    inv_ry2 = 1.0 / (ry * ry)

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
            if x1 >= 380:
                x1 = 379

            ny_base = -dy * inv_ry
            ny2 = ny_base * ny_base
            dot_k_y = ny_base * 0.57735
            dot_f_y = ny_base * -0.8165
            dot_h_y = ny_base * 0.3714

            offset = y * pitch + (x0 << 1)
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

                    r = (0.12 + 0.90 * diff_k + 0.20 * diff_f) * base_r + spec * 1.40
                    g = (0.12 + 0.90 * diff_k + 0.20 * diff_f) * base_g + spec * 1.40
                    b = (0.12 + 0.90 * diff_k + 0.20 * diff_f) * base_b + spec * 1.40

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
    gravity = -0.013
    floor_contact_y = VIRTUAL_FLOOR_Y + SPHERE_RADIUS
    two_r = SPHERE_RADIUS * 2.0
    two_r_sq = two_r * two_r

    BOUND_X = 1.70
    BOUND_Z = 0.70

    prev_min_y = 0
    prev_max_y = 299

    for y in range(300):
        idx = y * ROW_PITCH
        FRAME_BUF[idx:idx + ROW_PITCH] = WHITE_CHUNK

    fps_counter = 0
    t_last_fps = time.ticks_ms()
    fps_display_str = "FPS: -- | Capped to 72"

    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff

    moclcd.fill_rect(10, 10, 160, 12, 0xFFFF)
    moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)

    TARGET_FRAME_MS = 14

    while True:
        frame_start = ticks_ms()

        clear_dirty_rows(FRAME_BUF, prev_min_y, prev_max_y, WHITE_CHUNK)

        # -------------------------------------------------------------
        # 1. Multi-Body Physics & Momentum Transfer
        # -------------------------------------------------------------
        for i in range(3):
            VY[i] += gravity
            PX[i] += VX[i]
            PY[i] += VY[i]
            PZ[i] += VZ[i]

            # Floor Collision with Squash Response
            if PY[i] <= floor_contact_y:
                PY[i] = floor_contact_y
                impact_energy = abs(VY[i])
                VY[i] *= -0.86
                if abs(VY[i]) < 0.04:
                    VY[i] = 0.16

                squash_factor = impact_energy * 1.5
                if squash_factor > 0.32:
                    squash_factor = 0.32
                SQUASH_Y[i] = 1.0 - squash_factor
                SQUASH_X[i] = 1.0 + (squash_factor * 0.5)
            else:
                SQUASH_X[i] += (1.0 - SQUASH_X[i]) * 0.22
                SQUASH_Y[i] += (1.0 - SQUASH_Y[i]) * 0.22

            # Virtual Bounding Walls (X and Z boundaries)
            if PX[i] < -BOUND_X:
                PX[i] = -BOUND_X
                VX[i] = -VX[i] * 0.95
            elif PX[i] > BOUND_X:
                PX[i] = BOUND_X
                VX[i] = -VX[i] * 0.95

            if PZ[i] < -BOUND_Z:
                PZ[i] = -BOUND_Z
                VZ[i] = -VZ[i] * 0.95
            elif PZ[i] > BOUND_Z:
                PZ[i] = BOUND_Z
                VZ[i] = -VZ[i] * 0.95

        # Inter-Sphere Elastic Collisions
        for i in range(2):
            for j in range(i + 1, 3):
                dx = PX[j] - PX[i]
                dy = PY[j] - PY[i]
                dz = PZ[j] - PZ[i]
                dist_sq = dx * dx + dy * dy + dz * dz

                if dist_sq < two_r_sq and dist_sq > 0.00001:
                    dist = math.sqrt(dist_sq)
                    inv_d = 1.0 / dist
                    nx = dx * inv_d
                    ny = dy * inv_d
                    nz = dz * inv_d

                    # Positional separation to prevent penetration sticking
                    overlap = 0.5 * (two_r - dist)
                    PX[i] -= nx * overlap
                    PY[i] -= ny * overlap
                    PZ[i] -= nz * overlap
                    PX[j] += nx * overlap
                    PY[j] += ny * overlap
                    PZ[j] += nz * overlap

                    # Elastic velocity projection along collision normal
                    k_vel = (VX[i] - VX[j]) * nx + (VY[i] - VY[j]) * ny + (VZ[i] - VZ[j]) * nz
                    if k_vel > 0.0:
                        impulse = k_vel * 0.92
                        VX[i] -= impulse * nx
                        VY[i] -= impulse * ny
                        VZ[i] -= impulse * nz
                        VX[j] += impulse * nx
                        VY[j] += impulse * ny
                        VZ[j] += impulse * nz

        # -------------------------------------------------------------
        # 2. Perspective Projection & Depth Sorting
        # -------------------------------------------------------------
        for i in range(3):
            eff_z = CAM_Z + PZ[i]
            inv_z = 1.0 / eff_z
            fov_inv_z = FOV * inv_z

            SCR_X[i] = int((CX + (PX[i] * fov_inv_z)) - BB_X)
            SCR_Y[i] = int((CY - (PY[i] * fov_inv_z)) - BB_Y)
            SCR_R[i] = int(SPHERE_RADIUS * fov_inv_z)

            altitude = PY[i] - floor_contact_y
            SHAD_CY[i] = int((CY - (VIRTUAL_FLOOR_Y * fov_inv_z)) - BB_Y)
            SHAD_RX[i] = int(SCR_R[i] * (0.80 + altitude * 0.38) * SQUASH_X[i])
            SHAD_RY[i] = int(SHAD_RX[i] * 0.30)
            dens = int(6.0 - altitude * 2.4)
            if dens < 1:
                SHAD_DENS[i] = 1
            elif dens > 6:
                SHAD_DENS[i] = 6
            else:
                SHAD_DENS[i] = dens

        # Sort spheres back-to-front by PZ (Painter's Algorithm)
        SORT_ORDER[0], SORT_ORDER[1], SORT_ORDER[2] = 0, 1, 2
        if PZ[SORT_ORDER[0]] > PZ[SORT_ORDER[1]]:
            SORT_ORDER[0], SORT_ORDER[1] = SORT_ORDER[1], SORT_ORDER[0]
        if PZ[SORT_ORDER[1]] > PZ[SORT_ORDER[2]]:
            SORT_ORDER[1], SORT_ORDER[2] = SORT_ORDER[2], SORT_ORDER[1]
        if PZ[SORT_ORDER[0]] > PZ[SORT_ORDER[1]]:
            SORT_ORDER[0], SORT_ORDER[1] = SORT_ORDER[1], SORT_ORDER[0]

        # -------------------------------------------------------------
        # 3. Multi-Shadow Ground Rasterization
        # -------------------------------------------------------------
        frame_min_y = 300
        frame_max_y = 0

        for i in range(3):
            s_min, s_max = render_shadow_disk(
                SCR_X[i], SHAD_CY[i], SHAD_RX[i], SHAD_RY[i], SHAD_DENS[i], FRAME_BUF
            )
            if s_min < frame_min_y:
                frame_min_y = s_min
            if s_max > frame_max_y:
                frame_max_y = s_max

        # -------------------------------------------------------------
        # 4. Multi-Sphere Rasterization (Back-to-Front)
        # -------------------------------------------------------------
        for idx in range(3):
            i = SORT_ORDER[idx]
            sp_min, sp_max = render_sphere_squash(
                SCR_X[i], SCR_Y[i], SCR_R[i], SQUASH_X[i], SQUASH_Y[i],
                MAT_R[i], MAT_G[i], MAT_B[i], FRAME_BUF
            )
            if sp_min < frame_min_y:
                frame_min_y = sp_min
            if sp_max > frame_max_y:
                frame_max_y = sp_max

        if frame_min_y < 0:
            frame_min_y = 0
        if frame_max_y >= 300:
            frame_max_y = 299

        blit_top = frame_min_y if frame_min_y < prev_min_y else prev_min_y
        blit_bottom = frame_max_y if frame_max_y > prev_max_y else prev_max_y
        blit_h = blit_bottom - blit_top + 1

        start_offset = blit_top * ROW_PITCH
        end_offset = start_offset + (blit_h * ROW_PITCH)
        moclcd.blit(BB_X, BB_Y + blit_top, BB_W, blit_h, FRAME_BUF[start_offset:end_offset])

        prev_min_y = frame_min_y
        prev_max_y = frame_max_y

        # Frame rate regulation (Capped to 72 FPS)
        elapsed = ticks_diff(ticks_ms(), frame_start)
        if elapsed < TARGET_FRAME_MS:
            time.sleep_ms(TARGET_FRAME_MS - elapsed)

        fps_counter += 1
        if fps_counter >= 20:
            now = ticks_ms()
            dt = ticks_diff(now, t_last_fps)
            if dt > 0:
                fps = (fps_counter * 1000.0) / dt
                fps_display_str = "FPS: {:.1f} | Capped to 72".format(fps)
                moclcd.fill_rect(10, 10, 160, 10, 0xFFFF)
                moclcd.draw_text(10, 10, fps_display_str, 0x0000, 0xFFFF)
            t_last_fps = now
            fps_counter = 0

if __name__ == "__main__":
    run()
