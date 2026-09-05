import math
import moclcd

WIDTH = 480
HEIGHT = 320
CX = 240
CY = 160
FOV = 250.0
CAM_Z = 3.6

moclcd.init(pclk=20_000_000, width=WIDTH, height=HEIGHT, madctl=0x28)
moclcd.reset()
moclcd.panel_init()
moclcd.backlight(True)
moclcd.fill_screen(0x0000)

LX, LY, LZ = 0.57735, -0.57735, -0.57735

CUBE_VERTS = [
    (-1.0, -1.0, -1.0),
    ( 1.0, -1.0, -1.0),
    ( 1.0,  1.0, -1.0),
    (-1.0,  1.0, -1.0),
    (-1.0, -1.0,  1.0),
    ( 1.0, -1.0,  1.0),
    ( 1.0,  1.0,  1.0),
    (-1.0,  1.0,  1.0)
]

CUBE_FACES = [
    (0, 1, 2, 3),
    (5, 4, 7, 6),
    (4, 0, 3, 7),
    (1, 5, 6, 2),
    (3, 2, 6, 7),
    (4, 5, 1, 0)
]

BB_W = 240
BB_H = 240
BB_X = CX - 120
BB_Y = CY - 120

FRAME_BUF = bytearray(BB_W * BB_H * 2)

EDGE_MIN = [0] * BB_H
EDGE_MAX = [0] * BB_H

TV_X = [0.0] * 8
TV_Y = [0.0] * 8
TV_Z = [0.0] * 8
SV_X = [0] * 8
SV_Y = [0] * 8

@micropython.native
def clear_fb(buf):
    for i in range(len(buf)):
        buf[i] = 0

@micropython.native
def raster_edge(x0: int, y0: int, x1: int, y1: int, e_min, e_max):
    if y0 == y1:
        return
    if y0 > y1:
        x0, x1 = x1, x0
        y0, y1 = y1, y0

    dx = x1 - x0
    dy = y1 - y0
    step = (dx << 16) // dy
    curr = x0 << 16

    for y in range(y0, y1):
        if 0 <= y < 240:
            px = curr >> 16
            if px < e_min[y]:
                e_min[y] = px
            if px > e_max[y]:
                e_max[y] = px
        curr += step

@micropython.native
def fill_spans(min_y: int, max_y: int, hi: int, lo: int, buf, e_min, e_max):
    row_pitch = 480
    for y in range(min_y, max_y + 1):
        xs = e_min[y]
        xe = e_max[y]
        if xs > xe:
            continue
        if xs < 0:
            xs = 0
        if xe >= 240:
            xe = 239

        offset = y * row_pitch + (xs << 1)
        cnt = xe - xs + 1
        for _ in range(cnt):
            buf[offset] = hi
            buf[offset + 1] = lo
            offset += 2

def raster_quad(i0, i1, i2, i3, hi, lo):
    pts = ((SV_X[i0], SV_Y[i0]), (SV_X[i1], SV_Y[i1]), (SV_X[i2], SV_Y[i2]), (SV_X[i3], SV_Y[i3]))
    min_y = 240
    max_y = -1
    for p in pts:
        y = p[1]
        if y < min_y: min_y = y
        if y > max_y: max_y = y

    if min_y < 0: min_y = 0
    if max_y >= 240: max_y = 239
    if min_y > max_y: return

    for y in range(min_y, max_y + 1):
        EDGE_MIN[y] = 9999
        EDGE_MAX[y] = -9999

    for i in range(4):
        p0 = pts[i]
        p1 = pts[(i + 1) & 3]
        raster_edge(p0[0], p0[1], p1[0], p1[1], EDGE_MIN, EDGE_MAX)

    fill_spans(min_y, max_y, hi, lo, FRAME_BUF, EDGE_MIN, EDGE_MAX)

def run():
    ax = 0.0
    ay = 0.0
    az = 0.0

    while True:
        clear_fb(FRAME_BUF)

        cx, sx = math.cos(ax), math.sin(ax)
        cy, sy = math.cos(ay), math.sin(ay)
        cz, sz = math.cos(az), math.sin(az)

        for i in range(8):
            vx, vy, vz = CUBE_VERTS[i]
            y1 = vy * cx - vz * sx
            z1 = vy * sx + vz * cx
            x2 = vx * cy + z1 * sy
            z2 = -vx * sy + z1 * cy
            x3 = x2 * cz - y1 * sz
            y3 = x2 * sz + y1 * cz
            z3 = z2 + CAM_Z

            TV_X[i] = x3
            TV_Y[i] = y3
            TV_Z[i] = z3

            inv_z = 1.0 / z3
            SV_X[i] = int((CX + (x3 * FOV * inv_z)) - BB_X)
            SV_Y[i] = int((CY - (y3 * FOV * inv_z)) - BB_Y)

        faces = []
        for f in CUBE_FACES:
            i0, i1, i2, i3 = f
            x0, y0, z0 = TV_X[i0], TV_Y[i0], TV_Z[i0]
            e1x, e1y, e1z = TV_X[i1] - x0, TV_Y[i1] - y0, TV_Z[i1] - z0
            e2x, e2y, e2z = TV_X[i2] - x0, TV_Y[i2] - y0, TV_Z[i2] - z0

            nx = e1y * e2z - e1z * e2y
            ny = e1z * e2x - e1x * e2z
            nz = e1x * e2y - e1y * e2x

            if (nx * x0 + ny * y0 + nz * z0) < 0.0:
                inv_l = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
                nx *= inv_l
                ny *= inv_l
                nz *= inv_l

                dot_nl = nx * LX + ny * LY + nz * LZ
                diff = dot_nl if dot_nl > 0.0 else 0.0

                spec = 0.0
                if dot_nl > 0.0:
                    rz = 2.0 * dot_nl * nz - LZ
                    if rz < 0.0:
                        spec = (-rz) ** 16

                r = 0.15 * 0.18 + 0.15 * diff * 0.82 + spec * 0.95
                g = 0.75 * 0.18 + 0.75 * diff * 0.82 + spec * 0.95
                b = 1.00 * 0.18 + 1.00 * diff * 0.82 + spec * 0.95

                if r > 1.0: r = 1.0
                if g > 1.0: g = 1.0
                if b > 1.0: b = 1.0

                c = ((int(r * 31.0) & 0x1F) << 11) | ((int(g * 63.0) & 0x3F) << 5) | (int(b * 31.0) & 0x1F)
                hi = (c >> 8) & 0xFF
                lo = c & 0xFF

                avg_z = z0 + TV_Z[i1] + TV_Z[i2] + TV_Z[i3]
                faces.append((avg_z, i0, i1, i2, i3, hi, lo))

        faces.sort(key=lambda item: item[0], reverse=True)

        for item in faces:
            raster_quad(item[1], item[2], item[3], item[4], item[5], item[6])

        moclcd.blit(BB_X, BB_Y, BB_W, BB_H, FRAME_BUF)

        ax += 0.22
        ay += 0.31
        az += 0.14

run()
