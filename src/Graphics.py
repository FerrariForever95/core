"""
Graphics.py -- Complete Orientation-Aware UI Widget Stack for Zeno `lcd` + `touch`
(ILI9488 8-bit parallel panel, 4-wire resistive touch, ESP32-S3). MicroPython.

Supports automatic orientation switching (Portrait 320x480 & Landscape 480x320)
across rotations 0, 1, 2, and 3.
"""

import time
import math
import json
import micropython
import lcd
import touch
from machine import Pin

pin = Pin(38, Pin.OUT)                # backlight enable

# ---------------------------------------------------------------------
# Globals & Dynamic Orientation State
# ---------------------------------------------------------------------

WIDTH = 480
HEIGHT = 320
ROTATION = 1                          # 0=Portrait, 1=Landscape, 2=Portrait Inv, 3=Landscape Inv

# Design reference dimensions (if your UI layouts were written for 480x320 or 320x480)
REF_WIDTH = 480
REF_HEIGHT = 320
AUTO_ADAPT_LAYOUT = True              # automatically scale/reflow widgets when rotation changes

active_screen = None
_registered_widgets = []

FONT_8X8 = "8x8"
FONT_MONO12 = lcd.FONT_MONO12
FONT_MONO16 = lcd.FONT_MONO16
FONT_SANS16 = lcd.FONT_SANS16
FONT_SANS24B = lcd.FONT_SANS24B
FONT_DIGITS48 = lcd.FONT_DIGITS48

DEFAULT_FONT = FONT_8X8
TRANSPARENT = -1

# ---------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------

def color565(r, g, b):
    return (r & 0xf8) << 8 | (g & 0xfc) << 3 | b >> 3


def blend(c0, c1, alpha):
    """alpha 0..255: 0 -> c0, 255 -> c1."""
    return lcd.blend(c0, c1, alpha)


# Base Grayscale
WHITE         = color565(255, 255, 255)
SNOW          = color565(255, 250, 250)
IVORY         = color565(255, 255, 240)
SILVER        = color565(192, 192, 192)
GRAY          = color565(128, 128, 128)
DARK_GRAY     = color565(60, 60, 60)
LIGHT_GRAY    = color565(200, 200, 200)
CHARCOAL      = color565(34, 34, 34)
BLACK         = color565(0, 0, 0)

# Reds & Pinks
RED           = color565(255, 59, 48)
DARK_RED      = color565(139, 0, 0)
CRIMSON       = color565(220, 20, 60)
CORAL         = color565(255, 127, 80)
SALMON        = color565(250, 128, 114)
PINK          = color565(255, 192, 203)
HOT_PINK      = color565(255, 105, 180)
DEEP_PINK     = color565(255, 20, 147)

# Oranges & Browns
ORANGE        = color565(255, 149, 0)
DARK_ORANGE   = color565(255, 140, 0)
TOMATO        = color565(255, 99, 71)
GOLD          = color565(255, 215, 0)
BROWN         = color565(165, 42, 42)
SADDLE_BROWN  = color565(139, 69, 19)
CHOCOLATE     = color565(210, 105, 30)
PERU          = color565(205, 133, 63)
SANDY_BROWN   = color565(244, 164, 96)

# Yellows
YELLOW        = color565(255, 204, 0)
LIGHT_YELLOW  = color565(255, 255, 224)
LEMON_CHIFFON = color565(255, 250, 205)
KHAKI         = color565(240, 230, 140)
GOLDENROD     = color565(218, 165, 32)

# Greens
GREEN         = color565(52, 199, 89)
DARK_GREEN    = color565(0, 100, 0)
LIME          = color565(0, 255, 0)
LIME_GREEN    = color565(50, 205, 50)
FOREST_GREEN  = color565(34, 139, 34)
PALE_GREEN    = color565(152, 251, 152)
SPRING_GREEN  = color565(0, 255, 127)
SEA_GREEN     = color565(46, 139, 87)
OLIVE         = color565(128, 128, 0)
DARK_OLIVE    = color565(85, 107, 47)

# Cyans & Blues
CYAN          = color565(0, 255, 255)
LIGHT_CYAN    = color565(224, 255, 255)
DARK_CYAN     = color565(0, 139, 139)
AQUAMARINE    = color565(127, 255, 212)
TURQUOISE     = color565(64, 224, 208)
BLUE          = color565(0, 122, 255)
DARK_BLUE     = color565(0, 0, 139)
NAVY          = color565(0, 0, 128)
MEDIUM_BLUE   = color565(0, 0, 205)
SKY_BLUE      = color565(135, 206, 235)
DEEP_SKY_BLUE = color565(0, 191, 255)
DODGER_BLUE   = color565(30, 144, 255)
CORNFLOWER    = color565(100, 149, 237)
ROYAL_BLUE    = color565(65, 105, 225)

# Purples, Violets & Magentas
PURPLE        = color565(175, 82, 222)
MAGENTA       = color565(255, 0, 255)
INDIGO        = color565(75, 0, 130)
VIOLET        = color565(238, 130, 238)
PLUM          = color565(221, 160, 221)
ORCHID        = color565(218, 112, 214)
MEDIUM_PURPLE = color565(147, 112, 219)
DARK_MAGENTA  = color565(139, 0, 139)
THISTLE       = color565(216, 191, 216)

# UI System Palettes
SURFACE       = color565(28, 28, 30)
SURFACE_ALT   = color565(44, 44, 46)
BORDER        = color565(70, 70, 72)
TEXT_MUTED    = color565(150, 150, 155)
ACCENT_BLUE   = color565(10, 132, 255)
ACCENT_GREEN  = color565(48, 209, 88)
ACCENT_RED    = color565(255, 69, 58)
ACCENT_ORANGE = color565(255, 159, 10)
ACCENT_YELLOW = color565(255, 214, 10)
ACCENT_PURPLE = color565(191, 90, 242)

background = BLACK


def lighten(c, amount=60):
    return lcd.blend(c, WHITE, amount)


def darken(c, amount=60):
    return lcd.blend(c, BLACK, amount)


def set_background(color):
    global background
    background = color


def set_default_font(font):
    global DEFAULT_FONT
    DEFAULT_FONT = font


def log_error(msg):
    print("[UI ERROR]", msg)


def log_warn(msg):
    print("[UI WARN]", msg)


_touch_ok = False
_touch_last = None
_touch_t = None
_cal_loaded = False


# ---------------------------------------------------------------------
# Bounds & Orientation Helpers
# ---------------------------------------------------------------------

def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _clip(x, y, w, h):
    x0 = x if x > 0 else 0
    y0 = y if y > 0 else 0
    x1 = x + w
    y1 = y + h
    if x1 > WIDTH:
        x1 = WIDTH
    if y1 > HEIGHT:
        y1 = HEIGHT
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def set_reference_size(w, h):
    """Set the reference resolution your UI coordinates were designed for
    (e.g., set_reference_size(320, 480) if designed in Portrait, or (480, 320) for Landscape).
    Widgets will automatically scale from (REF_WIDTH, REF_HEIGHT) to (WIDTH, HEIGHT)."""
    global REF_WIDTH, REF_HEIGHT
    REF_WIDTH = max(1, int(w))
    REF_HEIGHT = max(1, int(h))


def _resolve_coord(val, total, ref_total):
    if isinstance(val, float) and 0.0 <= val <= 1.0:
        return int(round(val * total))
    if val < 0:
        return max(0, total + int(val))
    if AUTO_ADAPT_LAYOUT and total != ref_total and val > ref_total:
        return int(round(val * total / ref_total))
    return int(val)


# ---------------------------------------------------------------------
# Display Setup & Dynamic Rotation
# ---------------------------------------------------------------------

def init_display(rotation=1):
    """Bring up the panel and touch in any orientation (0=Portrait, 1=Landscape, 2=Inv Portrait, 3=Inv Landscape).
    Returns (WIDTH, HEIGHT)."""
    global WIDTH, HEIGHT, ROTATION, REF_WIDTH, REF_HEIGHT, _touch_ok, _touch_last, _touch_t

    ROTATION = rotation & 3
    lcd.init(rotation=ROTATION)
    pin.on()
    WIDTH, HEIGHT = lcd.width(), lcd.height()
    REF_WIDTH, REF_HEIGHT = WIDTH, HEIGHT

    _touch_ok = False
    _touch_last = None
    _touch_t = None
    try:
        touch.init()
        _touch_ok = True
    except OSError as e:
        log_warn("touch init failed: {}".format(e))
    if _touch_ok:
        _load_touch_cal()
        if not _cal_loaded:
            log_warn("touch is not calibrated -- run calibrate_touch() once")
    return WIDTH, HEIGHT


def set_rotation(rotation, redraw=True):
    """Dynamically change display rotation at runtime (0, 1, 2, 3) and reflow all UI widgets."""
    global WIDTH, HEIGHT, ROTATION, _touch_last, _touch_t
    old_w, old_h = WIDTH, HEIGHT
    ROTATION = rotation & 3
    lcd.init(rotation=ROTATION)
    WIDTH, HEIGHT = lcd.width(), lcd.height()
    _touch_last = None
    _touch_t = None

    if _touch_ok:
        _load_touch_cal()

    if old_w != WIDTH or old_h != HEIGHT:
        for w in _registered_widgets:
            w._relayout(old_w, old_h, WIDTH, HEIGHT)

    if redraw:
        clear()
        if active_screen is not None:
            active_screen.draw()
        else:
            for w in _registered_widgets:
                if w.visible and w.parent is None:
                    w.draw()
    return WIDTH, HEIGHT


# ---------------------------------------------------------------------
# Touch & Orientation-Independent Calibration
# ---------------------------------------------------------------------

TOUCH_MIN_INTERVAL_MS = 5
CAL_FILE = "/touch_cal.json"


def _map_cal_point(mx, my, from_rot, to_rot, from_w, from_h):
    """Transform screen calibration target (mx, my) from `from_rot` to `to_rot`."""
    if from_rot == to_rot:
        return mx, my
    pw = min(from_w, from_h)
    ph = max(from_w, from_h)
    # Convert to Rotation 0 (Portrait 320x480)
    if from_rot == 0:
        x0, y0 = mx, my
    elif from_rot == 1:
        x0, y0 = pw - 1 - my, mx
    elif from_rot == 2:
        x0, y0 = pw - 1 - mx, ph - 1 - my
    else:
        x0, y0 = my, ph - 1 - mx

    # Convert from Rotation 0 to `to_rot`
    if to_rot == 0:
        return x0, y0
    elif to_rot == 1:
        return y0, pw - 1 - x0
    elif to_rot == 2:
        return pw - 1 - x0, ph - 1 - y0
    else:
        return ph - 1 - y0, x0


def get_touch():
    """Debounced, calibrated (x, y) clamped to current screen bounds, or None."""
    global _touch_last, _touch_t
    if not _touch_ok:
        return None
    now = time.ticks_ms()
    if _touch_t is None or time.ticks_diff(now, _touch_t) >= TOUCH_MIN_INTERVAL_MS:
        _touch_t = now
        pt = touch.read()
        if pt is not None:
            _touch_last = (_clamp(pt[0], 0, WIDTH - 1), _clamp(pt[1], 0, HEIGHT - 1))
        else:
            _touch_last = None
    return _touch_last


def touch_raw():
    try:
        return touch.raw()
    except OSError:
        return (0, 0, 0, False)


def _cal_read():
    try:
        with open(CAL_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cal_write(d):
    with open(CAL_FILE, "w") as f:
        json.dump(d, f)


def _load_touch_cal():
    """Load and solve touch calibration for current ROTATION, automatically rotating
    saved Portrait or Landscape points if needed."""
    global _cal_loaded
    _cal_loaded = False
    d = _cal_read()
    rot_key = str(ROTATION)
    cal_map = d.get("cal", {})
    pts = None
    src_rot = ROTATION
    src_w, src_h = WIDTH, HEIGHT

    if rot_key in cal_map:
        pts = cal_map[rot_key]
    elif "points" in d:
        pts = d["points"]
        src_rot = d.get("rotation", 0 if d["points"][2][2] < d["points"][2][3] else 1)
        src_w = 480 if (src_rot & 1) else 320
        src_h = 320 if (src_rot & 1) else 480
    elif cal_map:
        k0 = list(cal_map.keys())[0]
        pts = cal_map[k0]
        src_rot = int(k0)
        src_w = 480 if (src_rot & 1) else 320
        src_h = 320 if (src_rot & 1) else 480

    if pts:
        try:
            touch.calib_begin()
            for p in pts:
                tx, ty = _map_cal_point(p[2], p[3], src_rot, ROTATION, src_w, src_h)
                touch.calib_add_point(p[0], p[1], tx, ty)
            touch.calib_solve()
            _cal_loaded = True
        except (ValueError, OSError, TypeError) as e:
            log_warn("saved touch calibration rejected: {}".format(e))
    elif touch.calib_active():
        _cal_loaded = True


def _sample_mark(n=8, timeout_ms=30000):
    t0 = time.ticks_ms()
    pts = []
    while True:
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            raise OSError("touch calibration timed out")
        rx, ry, _z, ok = touch_raw()
        if ok:
            if pts and (abs(rx - pts[-1][0]) > 150 or abs(ry - pts[-1][1]) > 150):
                pts = [(rx, ry)]
            else:
                pts.append((rx, ry))
            if len(pts) >= n:
                break
        else:
            pts = []
        time.sleep_ms(5)
    xs = sorted([p[0] for p in pts])
    ys = sorted([p[1] for p in pts])
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _wait_release(timeout_ms=10000):
    t0 = time.ticks_ms()
    quiet = 0
    while quiet < 15:
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            raise OSError("touch did not release")
        if touch_raw()[3]:
            quiet = 0
        else:
            quiet += 1
        time.sleep_ms(5)


def calibrate_touch(inset=24):
    global _cal_loaded
    if not _touch_ok:
        raise RuntimeError("touch is not running")
    inset = _clamp(inset, 8, min(WIDTH, HEIGHT) // 4)
    marks = ((inset, inset), (WIDTH - 1 - inset, inset), (WIDTH // 2, HEIGHT // 2),
             (inset, HEIGHT - 1 - inset), (WIDTH - 1 - inset, HEIGHT - 1 - inset))
    pairs = []
    for i in range(len(marks)):
        mx, my = marks[i]
        clear(BLACK)
        draw_line(mx - 12, my, mx + 12, my, WHITE)
        draw_line(mx, my - 12, mx, my + 12, WHITE)
        draw_text_centered(WIDTH // 2, HEIGHT // 2 + 24,
                           "Touch the cross ({}/{})".format(i + 1, len(marks)), WHITE, BLACK)
        _wait_release()
        rx, ry = _sample_mark()
        pairs.append([int(rx), int(ry), mx, my])
        _wait_release()

    touch.calib_begin()
    for p in pairs:
        touch.calib_add_point(p[0], p[1], p[2], p[3])
    rms, worst = touch.calib_solve()
    try:
        touch.calib_save()
    except OSError as e:
        log_warn("NVS save failed: {}".format(e))
    d = _cal_read()
    cal_map = d.get("cal", {})
    cal_map[str(ROTATION)] = pairs
    _cal_write({"points": pairs, "rotation": ROTATION, "cal": cal_map})
    _cal_loaded = True
    clear()
    return rms, worst


def _point_in_rect(px, py, x, y, w, h, margin=0):
    return (x - margin <= px <= x + w + margin and
            y - margin <= py <= y + h + margin)


# ---------------------------------------------------------------------
# Drawing Primitives
# ---------------------------------------------------------------------

_capturing = False
_capture_ops = []


def begin_capture():
    global _capturing, _capture_ops
    _capturing = True
    _capture_ops = []


def end_capture():
    global _capturing
    _capturing = False
    return _capture_ops


def fill_rect(x, y, w, h, color):
    x, y, w, h = int(x), int(y), int(w), int(h)
    c = _clip(x, y, w, h)
    if c is not None:
        lcd.fill_rect(c[0], c[1], c[2], c[3], color)
    if _capturing:
        _capture_ops.append(('rect', x, y, w, h, color))


def fill_screen(color):
    fill_rect(0, 0, WIDTH, HEIGHT, color)


def clear(color=None):
    fill_rect(0, 0, WIDTH, HEIGHT, background if color is None else color)


def draw_hline(x, y, w, color):
    x, y, w = int(x), int(y), int(w)
    if 0 <= y < HEIGHT:
        c = _clip(x, y, w, 1)
        if c is not None:
            lcd.fill_rect(c[0], c[1], c[2], 1, color)


def draw_vline(x, y, h, color):
    x, y, h = int(x), int(y), int(h)
    if 0 <= x < WIDTH:
        c = _clip(x, y, 1, h)
        if c is not None:
            lcd.fill_rect(c[0], c[1], 1, c[3], color)


def set_pixel(x, y, color):
    x, y = int(x), int(y)
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        lcd.pixel(x, y, color)
    if _capturing:
        _capture_ops.append(('pixel', x, y, color))


draw_pixel = set_pixel


def draw_line(x0, y0, x1, y1, color, width=1):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    if not ((x0 < 0 and x1 < 0) or (y0 < 0 and y1 < 0) or
            (x0 >= WIDTH and x1 >= WIDTH) or (y0 >= HEIGHT and y1 >= HEIGHT)):
        lcd.line(x0, y0, x1, y1, color, max(1, int(width)))
    if _capturing:
        _capture_ops.append(('line', x0, y0, x1, y1, color, width))


def draw_rect(x, y, w, h, color, fill=False):
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        return
    if fill:
        fill_rect(x, y, w, h, color)
        return
    if x >= 0 and y >= 0 and x + w <= WIDTH and y + h <= HEIGHT:
        lcd.rect(x, y, w, h, color)
    else:
        draw_hline(x, y, w, color)
        draw_hline(x, y + h - 1, w, color)
        draw_vline(x, y, h, color)
        draw_vline(x + w - 1, y, h, color)
    if _capturing:
        _capture_ops.append(('rect_outline', x, y, w, h, color))


def fill_circle(cx, cy, r, color):
    cx, cy, r = int(cx), int(cy), int(r)
    if r >= 1 and not (cx + r < 0 or cy + r < 0 or cx - r >= WIDTH or cy - r >= HEIGHT):
        lcd.circle(cx, cy, r, color, True)
    if _capturing:
        _capture_ops.append(('circle', cx, cy, r, color, True))


def draw_circle(cx, cy, r, color):
    cx, cy, r = int(cx), int(cy), int(r)
    if r >= 1 and not (cx + r < 0 or cy + r < 0 or cx - r >= WIDTH or cy - r >= HEIGHT):
        lcd.circle(cx, cy, r, color)
    if _capturing:
        _capture_ops.append(('circle', cx, cy, r, color, False))


def draw_ellipse(cx, cy, rx, ry, color, fill=False):
    cx, cy, rx, ry = int(cx), int(cy), int(rx), int(ry)
    if rx >= 1 and ry >= 1 and not (cx + rx < 0 or cy + ry < 0 or cx - rx >= WIDTH or cy - ry >= HEIGHT):
        lcd.ellipse(cx, cy, rx, ry, color, fill)
    if _capturing:
        _capture_ops.append(('ellipse', cx, cy, rx, ry, color, fill))


def draw_rounded_rect(x, y, w, h, r, color, filled=True):
    x, y, w, h, r = int(x), int(y), int(w), int(h), int(r)
    if w > 0 and h > 0:
        r = _clamp(r, 0, min(w, h) // 2)
        if _clip(x, y, w, h) is not None:
            lcd.round_rect(x, y, w, h, r, color, filled)
    if _capturing:
        _capture_ops.append(('rrect', x, y, w, h, r, color, filled))


def draw_triangle(x0, y0, x1, y1, x2, y2, color, filled=False):
    x0, y0, x1, y1, x2, y2 = int(x0), int(y0), int(x1), int(y1), int(x2), int(y2)
    if not ((x0 < 0 and x1 < 0 and x2 < 0) or (y0 < 0 and y1 < 0 and y2 < 0) or
            (x0 >= WIDTH and x1 >= WIDTH and x2 >= WIDTH) or
            (y0 >= HEIGHT and y1 >= HEIGHT and y2 >= HEIGHT)):
        lcd.triangle(x0, y0, x1, y1, x2, y2, color, filled)
    if _capturing:
        _capture_ops.append(('tri', x0, y0, x1, y1, x2, y2, color, filled))


def draw_polygon(points, color, filled=False):
    flat = []
    if points and not isinstance(points[0], int):
        for p in points:
            flat.append(int(p[0]))
            flat.append(int(p[1]))
    else:
        flat = [int(v) for v in points]
    if len(flat) > 128:
        flat = flat[:128]
    if len(flat) & 1:
        flat = flat[:-1]
    if len(flat) >= 4:
        lcd.polygon(flat, color, filled)
    if _capturing:
        _capture_ops.append(('poly', tuple(flat), color, filled))


def draw_ring(cx, cy, r_in, r_out, color, start=0, end=360):
    cx, cy, r_in, r_out = int(cx), int(cy), max(0, int(r_in)), int(r_out)
    if r_out <= r_in or r_out < 1:
        return
    if end - start >= 360:
        start, end = 0, 360
    if not (cx + r_out < 0 or cy + r_out < 0 or cx - r_out >= WIDTH or cy - r_out >= HEIGHT):
        lcd.ring(cx, cy, r_in, r_out, color, int(start), int(end))
    if _capturing:
        _capture_ops.append(('ring', cx, cy, r_in, r_out, color, start, end))


def draw_arc(cx, cy, r, start, end, color, thick=1):
    draw_ring(cx, cy, max(1, int(r) - max(1, int(thick)) + 1), int(r), color, start, end)


def draw_pie(cx, cy, r, start, end, color):
    draw_ring(cx, cy, 0, int(r), color, start, end)


def draw_bezier(x0, y0, x1, y1, x2, y2, x3, y3, color):
    lcd.bezier(int(x0), int(y0), int(x1), int(y1), int(x2), int(y2), int(x3), int(y3), color)


def draw_qbezier(x0, y0, x1, y1, x2, y2, color):
    lcd.qbezier(int(x0), int(y0), int(x1), int(y1), int(x2), int(y2), color)


def draw_gradient_rect(x, y, w, h, c0, c1, vertical=True):
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w > 0 and h > 0 and _clip(x, y, w, h) is not None:
        lcd.gradient(x, y, w, h, c0, c1, vertical)
    if _capturing:
        _capture_ops.append(('grad', x, y, w, h, c0, c1, vertical))


def blit(x, y, w, h, buf):
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        return
    if x >= 0 and y >= 0 and x + w <= WIDTH and y + h <= HEIGHT:
        lcd.blit(x, y, w, h, buf)
        return
    vx0 = max(0, -x)
    vx1 = min(w, WIDTH - x)
    vy0 = max(0, -y)
    vy1 = min(h, HEIGHT - y)
    if vx1 <= vx0 or vy1 <= vy0:
        return
    mv = memoryview(buf)
    stride = w * 2
    cw = vx1 - vx0
    for r in range(vy0, vy1):
        o = r * stride + vx0 * 2
        lcd.blit(x + vx0, y + r, cw, 1, mv[o:o + cw * 2])


def draw_bitmap(x, y, w, h, bits, fg, bg=None, scale=1):
    x, y, w, h = int(x), int(y), int(w), int(h)
    scale = max(1, int(scale))
    if w <= 0 or h <= 0:
        return
    if _clip(x, y, w * scale, h * scale) is None:
        return
    lcd.blit_mono(x, y, w, h, bits, fg, bg, scale)


# ---------------------------------------------------------------------
# Text API
# ---------------------------------------------------------------------

def _build_font():
    import framebuf
    buf = bytearray(8)
    fb = framebuf.FrameBuffer(buf, 8, 8, framebuf.MONO_HLSB)
    out = bytearray(95 * 8)
    for i in range(95):
        fb.fill(0)
        fb.text(chr(32 + i), 0, 0, 1)
        out[i * 8:i * 8 + 8] = buf
    return bytes(out)


_FM = memoryview(_build_font())


def _res_font(font):
    return DEFAULT_FONT if font is None else font


def text_size(text, font=None, scale=1):
    text = text if isinstance(text, str) else str(text)
    font = _res_font(font)
    scale = max(1, int(scale))
    if font == FONT_8X8:
        lines = text.split("\n")
        w = 0
        for ln in lines:
            if len(ln) > w:
                w = len(ln)
        return w * 8 * scale, len(lines) * 8 * scale
    return lcd.text_size(text, font, scale)


def _bitmap_text(x, y, text, fg, bg, scale):
    cx = x
    step = 8 * scale
    for ch in text:
        o = ord(ch)
        if o == 10:
            cx = x
            y += step
            continue
        if y >= HEIGHT:
            break
        if cx >= WIDTH:
            continue
        if cx + step > 0 and y + step > 0:
            i = o - 32 if 32 <= o < 127 else 31
            lcd.blit_mono(cx, y, 8, 8, _FM[i * 8:i * 8 + 8], fg, bg, scale)
        cx += step
    return cx


def _text(x, y, text, fg, bg, font, scale):
    if font == FONT_8X8:
        return _bitmap_text(x, y, text, fg, bg, scale)
    return lcd.text(x, y, text, fg, bg, font, scale)


def draw_text(x, y, text, fg, bg=None, font=None, scale=1):
    x, y = int(x), int(y)
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return x
    if bg is None:
        bg = background
    elif bg == TRANSPARENT:
        bg = None
    font = _res_font(font)
    scale = max(1, int(scale))
    if _capturing:
        _capture_ops.append(('text', x, y, text, fg, bg, font, scale))
    if x >= WIDTH or y >= HEIGHT:
        return x
    if y < 0 and y + text_size(text, font, scale)[1] <= 0:
        return x
    return _text(x, y, text, fg, bg, font, scale)


def draw_text_transparent(x, y, text, fg, font=None, scale=1):
    return draw_text(x, y, text, fg, TRANSPARENT, font, scale)


def draw_text_centered(cx, y, text, fg, bg=None, scale=1, font=None):
    text = text if isinstance(text, str) else str(text)
    w, _h = text_size(text, font, scale)
    return draw_text(max(0, int(cx) - w // 2), int(y), text, fg, bg, font, scale)


def draw_text_right(rx, y, text, fg, bg=None, scale=1, font=None):
    text = text if isinstance(text, str) else str(text)
    w, _h = text_size(text, font, scale)
    return draw_text(max(0, int(rx) - w), int(y), text, fg, bg, font, scale)


def draw_text_in_rect(x, y, w, h, text, fg, bg=None, font=None, scale=1):
    tw, th = text_size(text, font, scale)
    tx = int(x) + max(0, (int(w) - tw) // 2)
    ty = int(y) + max(0, (int(h) - th) // 2)
    return draw_text(tx, ty, text, fg, bg, font, scale)


def load_font(path):
    with open(path, "rb") as f:
        data = f.read()
    lcd.font_info(data)
    return data


# ---------------------------------------------------------------------
# BMP Loading
# ---------------------------------------------------------------------

@micropython.viper
def _row24(src: ptr8, out: ptr8, o: int, n: int, xfp: int, step: int):
    for k in range(n):
        i = (xfp >> 16) * 3
        v = ((int(src[i + 2]) & 0xF8) << 8) | ((int(src[i + 1]) & 0xFC) << 3) | (int(src[i]) >> 3)
        out[o] = v >> 8
        out[o + 1] = v & 0xFF
        o += 2
        xfp += step


@micropython.viper
def _row32(src: ptr8, out: ptr8, o: int, n: int, xfp: int, step: int):
    for k in range(n):
        i = (xfp >> 16) * 4
        v = ((int(src[i + 2]) & 0xF8) << 8) | ((int(src[i + 1]) & 0xFC) << 3) | (int(src[i]) >> 3)
        out[o] = v >> 8
        out[o + 1] = v & 0xFF
        o += 2
        xfp += step


def _row16(src, out, o, n, xfp, step, is565):
    for _ in range(n):
        i = (xfp >> 16) * 2
        lo = src[i]
        hi = src[i + 1]
        if is565:
            out[o] = hi
            out[o + 1] = lo
        else:
            v = (hi << 8) | lo
            v = ((v & 0x7FE0) << 1) | (v & 0x1F)
            out[o] = v >> 8
            out[o + 1] = v & 0xFF
        o += 2
        xfp += step


def _row8(src, out, o, n, xfp, step, pal):
    for _ in range(n):
        j = src[xfp >> 16] * 2
        out[o] = pal[j]
        out[o + 1] = pal[j + 1]
        o += 2
        xfp += step


def _le(b):
    return int.from_bytes(b, "little")


def _s32(b):
    v = int.from_bytes(b, "little")
    return v - 0x100000000 if v & 0x80000000 else v


def _bmp_native_size(path):
    with open(path, "rb") as f:
        h = f.read(26)
        if len(h) < 26 or h[0:2] != b"BM":
            raise ValueError("not a BMP file")
        return _s32(h[18:22]), abs(_s32(h[22:26]))


def _bmp_parse(f):
    h = f.read(54)
    if len(h) < 54 or h[0:2] != b"BM":
        raise ValueError("not a BMP file")
    hdr = _le(h[14:18])
    if hdr < 40:
        raise ValueError("unsupported BMP header")
    data_off = _le(h[10:14])
    bw = _s32(h[18:22])
    bh = _s32(h[22:26])
    bpp = _le(h[28:30])
    comp = _le(h[30:34])
    ncol = _le(h[46:50])
    topdown = bh < 0
    bh = abs(bh)
    if bw <= 0 or bh == 0:
        raise ValueError("bad BMP size")
    if comp not in (0, 3):
        raise ValueError("compressed BMP not supported")
    is565 = False
    pal = None
    if bpp == 16:
        if comp == 3:
            f.seek(54)
            is565 = _le(f.read(4)) == 0xF800
    elif bpp == 8:
        f.seek(14 + hdr)
        raw = f.read((ncol or 256) * 4)
        pal = bytearray(512)
        for i in range(min(256, len(raw) // 4)):
            b = raw[i * 4]
            g = raw[i * 4 + 1]
            r = raw[i * 4 + 2]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            pal[i * 2] = v >> 8
            pal[i * 2 + 1] = v & 0xFF
    elif bpp not in (24, 32):
        raise ValueError("unsupported BMP bit depth {}".format(bpp))
    return bw, bh, topdown, bpp, data_off, pal, is565


def draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None):
    x, y = int(x), int(y)
    with open(path, "rb") as f:
        bw, bh, topdown, bpp, data_off, pal, is565 = _bmp_parse(f)

        if w and h:
            dw, dh = int(w), int(h)
        elif w:
            dw = int(w)
            dh = bh * dw // bw
        elif h:
            dh = int(h)
            dw = bw * dh // bh
        else:
            dw, dh = bw, bh
        if max_w and dw > max_w:
            dh = dh * int(max_w) // dw
            dw = int(max_w)
        if max_h and dh > max_h:
            dw = dw * int(max_h) // dh
            dh = int(max_h)
        dw = max(1, dw)
        dh = max(1, dh)

        if _capturing:
            _capture_ops.append(('bmp', path, x, y, dw, dh))

        vx0 = max(0, -x)
        vx1 = min(dw, WIDTH - x)
        vy0 = max(0, -y)
        vy1 = min(dh, HEIGHT - y)
        if vx1 <= vx0 or vy1 <= vy0:
            return dw, dh
        vw = vx1 - vx0

        stride = ((bw * bpp + 31) // 32) * 4
        xstep = (bw << 16) // dw
        xfp0 = vx0 * xstep
        rps = max(1, 8192 // (vw * 2))
        strip = bytearray(vw * 2 * rps)
        src = bytearray(stride)
        last_sy = -1
        r = 0
        top = y + vy0

        for dy in range(vy0, vy1):
            sy = dy * bh // dh
            if sy != last_sy:
                f.seek(data_off + (sy if topdown else bh - 1 - sy) * stride)
                f.readinto(src)
                last_sy = sy
            o = r * vw * 2
            if bpp == 24:
                _row24(src, strip, o, vw, xfp0, xstep)
            elif bpp == 32:
                _row32(src, strip, o, vw, xfp0, xstep)
            elif bpp == 16:
                _row16(src, strip, o, vw, xfp0, xstep, is565)
            else:
                _row8(src, strip, o, vw, xfp0, xstep, pal)
            r += 1
            if r == rps or dy == vy1 - 1:
                lcd.blit(x + vx0, top, vw, r,
                         strip if r == rps else memoryview(strip)[:r * vw * 2])
                top += r
                r = 0
        return dw, dh


def draw_logo(path, box_x=0, box_y=0, box_w=None, box_h=None,
              caption=None, caption_color=WHITE, bg=None, font=None):
    if box_w is None:
        box_w = WIDTH
    if box_h is None:
        box_h = HEIGHT
    box_w = max(1, min(int(box_w), WIDTH - int(box_x)))
    box_h = max(1, min(int(box_h), HEIGHT - int(box_y)))
    if bg is not None:
        fill_rect(box_x, box_y, box_w, box_h, bg)

    caption_h = (text_size(caption, font)[1] + 4) if caption else 0
    avail_h = max(1, box_h - caption_h)
    native_w, native_h = _bmp_native_size(path)
    scale = min(box_w / native_w, avail_h / native_h, 1.0)
    out_w = max(1, int(native_w * scale))
    out_h = max(1, int(native_h * scale))
    lx = int(box_x) + (box_w - out_w) // 2
    ly = int(box_y) + (avail_h - out_h) // 2
    draw_bmp(path, lx, ly, out_w, out_h)
    if caption:
        draw_text_centered(int(box_x) + box_w // 2, int(box_y) + avail_h + 2,
                           caption, caption_color, bg, 1, font)
    return lx, ly, out_w, out_h


# ---------------------------------------------------------------------
# Screen & Widget Animations
# ---------------------------------------------------------------------

def window_close_animation(duration=0.4, fps=60, color=None, ease=True):
    if color is None:
        color = WHITE
    cx = WIDTH // 2
    cy = HEIGHT // 2
    fill_rect(0, 0, WIDTH, HEIGHT, color)
    frames = max(1, int(duration * fps))
    delay = duration / frames
    prev_x0, prev_y0 = 0, 0
    prev_x1, prev_y1 = WIDTH - 1, HEIGHT - 1

    for i in range(frames + 1):
        t = i / frames
        if ease:
            t = t * t * (3 - 2 * t)
        t = 1 - t
        w = max(1, int(WIDTH * t))
        h = max(1, int(HEIGHT * t))
        x0 = cx - w // 2
        y0 = cy - h // 2
        x1 = x0 + w - 1
        y1 = y0 + h - 1
        if y0 > prev_y0:
            fill_rect(prev_x0, prev_y0, prev_x1 - prev_x0 + 1, y0 - prev_y0, background)
        if y1 < prev_y1:
            fill_rect(prev_x0, y1 + 1, prev_x1 - prev_x0 + 1, prev_y1 - y1, background)
        if x0 > prev_x0:
            fill_rect(prev_x0, y0, x0 - prev_x0, y1 - y0 + 1, background)
        if x1 < prev_x1:
            fill_rect(x1 + 1, y0, prev_x1 - x1, y1 - y0 + 1, background)
        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1
        time.sleep(delay)


def window_open_animation(duration=0.4, fps=60, color=None, ease=True):
    if color is None:
        color = WHITE
    clear()
    frames = max(1, int(duration * fps))
    delay = duration / frames
    cx = WIDTH // 2
    cy = HEIGHT // 2
    prev_x0 = prev_y0 = prev_x1 = prev_y1 = cx

    for i in range(frames + 1):
        t = i / frames
        if ease:
            t = 1 - (1 - t) ** 3
        w = max(1, int(WIDTH * t))
        h = max(1, int(HEIGHT * t))
        x0 = cx - w // 2
        y0 = cy - h // 2
        x1 = x0 + w - 1
        y1 = y0 + h - 1
        if y0 < prev_y0:
            fill_rect(x0, y0, w, prev_y0 - y0, color)
        if y1 > prev_y1:
            fill_rect(x0, prev_y1 + 1, w, y1 - prev_y1, color)
        if x0 < prev_x0:
            fill_rect(x0, prev_y0, prev_x0 - x0, prev_y1 - prev_y0 + 1, color)
        if x1 > prev_x1:
            fill_rect(prev_x1 + 1, prev_y0, x1 - prev_x1, prev_y1 - prev_y0 + 1, color)
        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1
        time.sleep(delay)


def _m(v, o, s):
    return int(o + (v - o) * s)


def replay_ops(ops, scale, ox, oy, text_reveal=0.92):
    """Redraw captured ops scaled about (ox, oy). Text appears once scale >= text_reveal."""
    for op in ops:
        k = op[0]
        if k == 'rect':
            fill_rect(_m(op[1], ox, scale), _m(op[2], oy, scale),
                      max(1, int(op[3] * scale)), max(1, int(op[4] * scale)), op[5])
        elif k == 'rect_outline':
            draw_rect(_m(op[1], ox, scale), _m(op[2], oy, scale),
                      max(1, int(op[3] * scale)), max(1, int(op[4] * scale)), op[5])
        elif k == 'rrect':
            draw_rounded_rect(_m(op[1], ox, scale), _m(op[2], oy, scale),
                              max(1, int(op[3] * scale)), max(1, int(op[4] * scale)),
                              max(0, int(op[5] * scale)), op[6], op[7])
        elif k == 'circle':
            r = max(1, int(op[3] * scale))
            if op[5]:
                fill_circle(_m(op[1], ox, scale), _m(op[2], oy, scale), r, op[4])
            else:
                draw_circle(_m(op[1], ox, scale), _m(op[2], oy, scale), r, op[4])
        elif k == 'ellipse':
            draw_ellipse(_m(op[1], ox, scale), _m(op[2], oy, scale),
                         max(1, int(op[3] * scale)), max(1, int(op[4] * scale)),
                         op[5], op[6])
        elif k == 'line':
            draw_line(_m(op[1], ox, scale), _m(op[2], oy, scale),
                      _m(op[3], ox, scale), _m(op[4], oy, scale),
                      op[5], max(1, int(op[6] * scale)))
        elif k == 'tri':
            draw_triangle(_m(op[1], ox, scale), _m(op[2], oy, scale),
                          _m(op[3], ox, scale), _m(op[4], oy, scale),
                          _m(op[5], ox, scale), _m(op[6], oy, scale),
                          op[7], op[8])
        elif k == 'poly':
            pts = op[1]
            scaled = []
            for idx in range(0, len(pts), 2):
                scaled.append(_m(pts[idx], ox, scale))
                scaled.append(_m(pts[idx + 1], oy, scale))
            draw_polygon(scaled, op[2], op[3])
        elif k == 'ring':
            draw_ring(_m(op[1], ox, scale), _m(op[2], oy, scale),
                      max(0, int(op[3] * scale)), max(1, int(op[4] * scale)),
                      op[5], op[6], op[7])
        elif k == 'grad':
            draw_gradient_rect(_m(op[1], ox, scale), _m(op[2], oy, scale),
                               max(1, int(op[3] * scale)), max(1, int(op[4] * scale)),
                               op[5], op[6], op[7])
        elif k == 'bmp':
            draw_bmp(op[1], _m(op[2], ox, scale), _m(op[3], oy, scale),
                     max(1, int(op[4] * scale)), max(1, int(op[5] * scale)))
        elif k == 'text':
            if scale >= text_reveal:
                draw_text(_m(op[1], ox, scale), _m(op[2], oy, scale),
                          op[3], op[4], op[5], op[6], max(1, int(op[7] * scale)))
        elif k == 'pixel':
            set_pixel(_m(op[1], ox, scale), _m(op[2], oy, scale), op[3])


def animate_widget_in(widget, duration=0.2, steps=6):
    """Capture a widget's drawing ops and zoom it in smoothly about its center."""
    begin_capture()
    widget.draw()
    ops = end_capture()
    ox = widget.x + widget.w // 2
    oy = widget.y + widget.h // 2
    delay = duration / max(1, steps)
    for i in range(1, steps + 1):
        s = i / steps
        fill_rect(widget.x, widget.y, widget.w, widget.h, background)
        replay_ops(ops, s, ox, oy)
        time.sleep(delay)


# ---------------------------------------------------------------------
# Orientation-Aware Base Widget & Complete UI Component Stack
# ---------------------------------------------------------------------

class UIWidget:
    """Base class for all UI widgets. Automatically adapts coordinates and sizes
    when the display switches between Portrait (320x480) and Landscape (480x320)."""

    def __init__(self, x, y, w, h):
        self._raw_x = x
        self._raw_y = y
        self._raw_w = w
        self._raw_h = h
        self._created_w = WIDTH
        self._created_h = HEIGHT

        self.x = _resolve_coord(x, WIDTH, REF_WIDTH)
        self.y = _resolve_coord(y, HEIGHT, REF_HEIGHT)
        self.w = max(1, _resolve_coord(w, WIDTH, REF_WIDTH))
        self.h = max(1, _resolve_coord(h, HEIGHT, REF_HEIGHT))
        self._clamp_bounds()

        self.visible = True
        self.enabled = True
        self.pressed = False
        self.parent = None

        if self not in _registered_widgets:
            _registered_widgets.append(self)

    def _clamp_bounds(self):
        self.w = _clamp(int(self.w), 1, WIDTH)
        self.h = _clamp(int(self.h), 1, HEIGHT)
        self.x = _clamp(int(self.x), 0, max(0, WIDTH - self.w))
        self.y = _clamp(int(self.y), 0, max(0, HEIGHT - self.h))

    def _relayout(self, old_w, old_h, new_w, new_h):
        """Proportional reflow when rotation changes between Portrait and Landscape."""
        if old_w <= 0 or old_h <= 0:
            return
        self.x = int(round(self.x * new_w / old_w))
        self.y = int(round(self.y * new_h / old_h))
        self.w = max(8, int(round(self.w * new_w / old_w)))
        self.h = max(8, int(round(self.h * new_h / old_h)))
        self._clamp_bounds()

    def contains(self, px, py, margin=2):
        return _point_in_rect(px, py, self.x, self.y, self.w, self.h, margin)

    def clear_area(self, bg_color=None):
        fill_rect(self.x, self.y, self.w, self.h, background if bg_color is None else bg_color)

    def show(self, redraw=True):
        self.visible = True
        if redraw:
            self.draw()

    def hide(self, redraw=True):
        self.visible = False
        if redraw:
            self.clear_area()

    def draw(self):
        pass

    def update(self, pt):
        return self.handle_touch(pt)

    def handle_touch(self, pt):
        return False


class UILabel(UIWidget):
    def __init__(self, x, y, text="", color=WHITE, bg=TRANSPARENT,
                 font=None, scale=1, align="left", w=0, h=0, fg=None):
        if fg is not None:
            color = fg
        tw, th = text_size(text, font, scale)
        super().__init__(x, y, w if w > 0 else max(1, tw), h if h > 0 else max(1, th))
        self.text = str(text)
        self.color = color
        self.bg = bg
        self.font = font
        self.scale = max(1, int(scale))
        self.align = align
        self._auto_size = (w <= 0 or h <= 0)

    def set_text(self, text, redraw=True):
        text = str(text)
        if text == self.text:
            return
        if redraw and self.visible:
            self.clear_area(background if self.bg == TRANSPARENT or self.bg is None else self.bg)
        self.text = text
        if self._auto_size:
            tw, th = text_size(self.text, self.font, self.scale)
            self.w = _clamp(tw, 1, WIDTH - self.x)
            self.h = _clamp(th, 1, HEIGHT - self.y)
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        if self.bg != TRANSPARENT and self.bg is not None:
            fill_rect(self.x, self.y, self.w, self.h, self.bg)
        if not self._auto_size:
            if self.align == "center":
                draw_text_in_rect(self.x, self.y, self.w, self.h, self.text, self.color, self.bg, self.font, self.scale)
            elif self.align == "right":
                _, th = text_size(self.text, self.font, self.scale)
                draw_text_right(self.x + self.w, self.y + max(0, (self.h - th) // 2),
                                self.text, self.color, self.bg, self.scale, self.font)
            else:
                _, th = text_size(self.text, self.font, self.scale)
                draw_text(self.x, self.y + max(0, (self.h - th) // 2),
                          self.text, self.color, self.bg, self.font, self.scale)
        else:
            if self.align == "center":
                draw_text_centered(self.x, self.y, self.text, self.color, self.bg, self.scale, self.font)
            elif self.align == "right":
                draw_text_right(self.x, self.y, self.text, self.color, self.bg, self.scale, self.font)
            else:
                draw_text(self.x, self.y, self.text, self.color, self.bg, self.font, self.scale)


class UIButton(UIWidget):
    def __init__(self, x, y, w, h, text="", action=None,
                 color=ACCENT_BLUE, text_color=WHITE, pressed_color=None,
                 border_color=None, radius=8, font=None, scale=1,
                 bg=None, fg=None, label=None):
        super().__init__(x, y, w, h)
        if label is not None:
            text = label
        if bg is not None:
            color = bg
        if fg is not None:
            text_color = fg
        self.text = str(text)
        self.action = action
        self.color = color
        self.text_color = text_color
        self.pressed_color = pressed_color if pressed_color is not None else darken(color, 70)
        self.border_color = border_color
        self.radius = radius
        self.font = font
        self.scale = max(1, int(scale))

    def set_text(self, text, redraw=True):
        self.text = str(text)
        if redraw and self.visible:
            self.draw()

    set_label = set_text

    def draw(self):
        if not self.visible:
            return
        c = DARK_GRAY if not self.enabled else (self.pressed_color if self.pressed else self.color)
        tc = TEXT_MUTED if not self.enabled else self.text_color
        r = _clamp(self.radius, 0, min(self.w, self.h) // 2)
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, c, filled=True)
        if self.border_color is not None:
            draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.border_color, filled=False)
        if self.text:
            draw_text_in_rect(self.x, self.y, self.w, self.h, self.text, tc, c, self.font, self.scale)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            inside = self.contains(pt[0], pt[1])
            if inside and not self.pressed:
                self.pressed = True
                self.draw()
                return True
            elif not inside and self.pressed:
                self.pressed = False
                self.draw()
        else:
            if self.pressed:
                self.pressed = False
                self.draw()
                if self.action:
                    try:
                        self.action()
                    except Exception as e:
                        log_error("UIButton action failed: {}".format(e))
                return True
        return self.pressed


class UIIconButton(UIButton):
    """Button that renders a 1bpp bitmap icon or BMP file with optional caption."""

    def __init__(self, x, y, w, h, icon_bits=None, icon_w=0, icon_h=0,
                 bmp_path=None, text="", action=None, color=SURFACE_ALT,
                 fg=WHITE, radius=8, font=None, scale=1):
        super().__init__(x, y, w, h, text=text, action=action, color=color,
                         text_color=fg, radius=radius, font=font, scale=scale)
        self.icon_bits = icon_bits
        self.icon_w = icon_w
        self.icon_h = icon_h
        self.bmp_path = bmp_path

    def draw(self):
        if not self.visible:
            return
        c = DARK_GRAY if not self.enabled else (self.pressed_color if self.pressed else self.color)
        tc = TEXT_MUTED if not self.enabled else self.text_color
        r = _clamp(self.radius, 0, min(self.w, self.h) // 2)
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, c, filled=True)
        if self.bmp_path:
            draw_logo(self.bmp_path, self.x + 4, self.y + 4, self.w - 8, self.h - 8,
                      caption=self.text if self.text else None, caption_color=tc, bg=None, font=self.font)
        elif self.icon_bits and self.icon_w > 0 and self.icon_h > 0:
            ix = self.x + (self.w - self.icon_w) // 2
            iy = self.y + (self.h - self.icon_h) // 2 - (6 if self.text else 0)
            draw_bitmap(ix, iy, self.icon_w, self.icon_h, self.icon_bits, tc, c, 1)
            if self.text:
                draw_text_centered(self.x + self.w // 2, self.y + self.h - 14, self.text, tc, c, 1, self.font)
        elif self.text:
            draw_text_in_rect(self.x, self.y, self.w, self.h, self.text, tc, c, self.font, self.scale)


class UIToggle(UIWidget):
    """Toggle switch widget (also aliased as UISwitch)."""

    def __init__(self, x, y, w=54, h=28, value=False, onchange=None, action=None,
                 on_color=ACCENT_GREEN, off_color=SURFACE_ALT, knob_color=WHITE):
        super().__init__(x, y, w, h)
        self.value = bool(value)
        self.onchange = onchange or action
        self.on_color = on_color
        self.off_color = off_color
        self.knob_color = knob_color

    def set_value(self, v, redraw=True):
        v = bool(v)
        if v != self.value:
            self.value = v
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        r = min(self.w, self.h) // 2
        col = self.on_color if self.value else self.off_color
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, col, filled=True)
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, BORDER, filled=False)
        kr = max(2, r - 3)
        kx = (self.x + self.w - r) if self.value else (self.x + r)
        ky = self.y + self.h // 2
        fill_circle(kx, ky, kr, self.knob_color)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            if self.contains(pt[0], pt[1], margin=4):
                self.pressed = True
                return True
        else:
            if self.pressed:
                self.pressed = False
                self.value = not self.value
                self.draw()
                if self.onchange:
                    try:
                        self.onchange(self.value)
                    except TypeError:
                        self.onchange()
                    except Exception as e:
                        log_error("UIToggle error: {}".format(e))
                return True
        return False


UISwitch = UIToggle


class UICheckbox(UIWidget):
    def __init__(self, x, y, label="", value=False, onchange=None, action=None,
                 size=22, color=ACCENT_BLUE, text_color=WHITE, font=None, scale=1):
        tw, th = text_size(label, font, scale)
        super().__init__(x, y, size + (8 + tw if label else 0), max(size, th))
        self.box_size = size
        self.label = str(label)
        self.value = bool(value)
        self.onchange = onchange or action
        self.color = color
        self.text_color = text_color
        self.font = font
        self.scale = max(1, int(scale))

    def set_value(self, val, redraw=True):
        self.value = bool(val)
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        self.clear_area()
        bs = min(self.box_size, self.h)
        by = self.y + (self.h - bs) // 2
        r = max(3, bs // 5)
        draw_rounded_rect(self.x, by, bs, bs, r, self.color if self.value else SURFACE, filled=True)
        draw_rounded_rect(self.x, by, bs, bs, r, self.color if self.value else BORDER, filled=False)
        if self.value:
            draw_line(self.x + bs // 4, by + bs // 2, self.x + bs * 2 // 5, by + bs * 3 // 4, WHITE, 2)
            draw_line(self.x + bs * 2 // 5, by + bs * 3 // 4, self.x + bs * 3 // 4, by + bs // 4, WHITE, 2)
        if self.label:
            _, th = text_size(self.label, self.font, self.scale)
            draw_text(self.x + bs + 8, self.y + max(0, (self.h - th) // 2),
                      self.label, self.text_color, background, self.font, self.scale)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            if self.contains(pt[0], pt[1], margin=4):
                self.pressed = True
                return True
        else:
            if self.pressed:
                self.pressed = False
                self.value = not self.value
                self.draw()
                if self.onchange:
                    try:
                        self.onchange(self.value)
                    except TypeError:
                        self.onchange()
                    except Exception as e:
                        log_error("UICheckbox error: {}".format(e))
                return True
        return False


class UIRadioGroup(UIWidget):
    """Group of mutually exclusive radio options."""

    def __init__(self, x, y, options, selected=0, onchange=None,
                 spacing=28, color=ACCENT_BLUE, text_color=WHITE, font=None, scale=1):
        self.options = [str(o) for o in options]
        self.spacing = max(20, int(spacing))
        max_tw = 0
        for o in self.options:
            tw, _ = text_size(o, font, scale)
            if tw > max_tw:
                max_tw = tw
        total_w = 26 + max_tw
        total_h = max(22, len(self.options) * self.spacing)
        super().__init__(x, y, total_w, total_h)
        self.selected = _clamp(selected, 0, max(0, len(self.options) - 1))
        self.onchange = onchange
        self.color = color
        self.text_color = text_color
        self.font = font
        self.scale = max(1, int(scale))

    def draw(self):
        if not self.visible:
            return
        self.clear_area()
        r = 9
        for i, opt in enumerate(self.options):
            cy = self.y + i * self.spacing + self.spacing // 2
            cx = self.x + r + 2
            fill_circle(cx, cy, r, SURFACE)
            draw_circle(cx, cy, r, self.color if i == self.selected else BORDER)
            if i == self.selected:
                fill_circle(cx, cy, r - 4, self.color)
            _, th = text_size(opt, self.font, self.scale)
            draw_text(self.x + 2 * r + 8, cy - th // 2, opt, self.text_color, background, self.font, self.scale)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled or not self.options:
            return False
        if pt is not None:
            if self.contains(pt[0], pt[1]):
                idx = (pt[1] - self.y) // self.spacing
                idx = _clamp(idx, 0, len(self.options) - 1)
                if idx != self.selected:
                    self.selected = idx
                    self.draw()
                    if self.onchange:
                        try:
                            self.onchange(self.selected, self.options[self.selected])
                        except TypeError:
                            self.onchange(self.selected)
                return True
        return False


class UISlider(UIWidget):
    def __init__(self, x, y, w, h=28, min_val=0, max_val=100, value=50,
                 onchange=None, color=ACCENT_BLUE, track_color=SURFACE_ALT,
                 knob_color=WHITE, step=1):
        super().__init__(x, y, w, h)
        self.min_val = min_val
        self.max_val = max_val if max_val > min_val else min_val + 1
        self.step = step
        self.value = _clamp(value, self.min_val, self.max_val)
        self.onchange = onchange
        self.color = color
        self.track_color = track_color
        self.knob_color = knob_color

    def set_value(self, val, redraw=True):
        val = _clamp(val, self.min_val, self.max_val)
        if val != self.value:
            self.value = val
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        self.clear_area()
        t = (self.value - self.min_val) / (self.max_val - self.min_val)
        if self.w >= self.h:
            kr = max(6, self.h // 2 - 2)
            th = max(4, self.h // 3)
            ty = self.y + (self.h - th) // 2
            tx0 = self.x + kr
            tw = max(1, self.w - 2 * kr)
            draw_rounded_rect(tx0, ty, tw, th, th // 2, self.track_color, filled=True)
            fw = int(tw * t)
            if fw > 0:
                draw_rounded_rect(tx0, ty, fw, th, th // 2, self.color, filled=True)
            kx = tx0 + fw
            ky = self.y + self.h // 2
            fill_circle(kx, ky, kr, self.knob_color)
            draw_circle(kx, ky, kr, BORDER)
        else:
            kr = max(6, self.w // 2 - 2)
            tw = max(4, self.w // 3)
            tx = self.x + (self.w - tw) // 2
            ty0 = self.y + kr
            th = max(1, self.h - 2 * kr)
            draw_rounded_rect(tx, ty0, tw, th, tw // 2, self.track_color, filled=True)
            fh = int(th * t)
            ky = ty0 + th - fh
            if fh > 0:
                draw_rounded_rect(tx, ky, tw, fh, tw // 2, self.color, filled=True)
            kx = self.x + self.w // 2
            fill_circle(kx, ky, kr, self.knob_color)
            draw_circle(kx, ky, kr, BORDER)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            if self.pressed or self.contains(pt[0], pt[1], margin=6):
                self.pressed = True
                if self.w >= self.h:
                    kr = max(6, self.h // 2 - 2)
                    ratio = _clamp((pt[0] - (self.x + kr)) / max(1, self.w - 2 * kr), 0.0, 1.0)
                else:
                    kr = max(6, self.w // 2 - 2)
                    ratio = 1.0 - _clamp((pt[1] - (self.y + kr)) / max(1, self.h - 2 * kr), 0.0, 1.0)
                raw = self.min_val + ratio * (self.max_val - self.min_val)
                if self.step:
                    raw = round((raw - self.min_val) / self.step) * self.step + self.min_val
                if isinstance(self.min_val, int) and isinstance(self.max_val, int) and isinstance(self.step, int):
                    raw = int(round(raw))
                nv = _clamp(raw, self.min_val, self.max_val)
                if nv != self.value:
                    self.value = nv
                    self.draw()
                    if self.onchange:
                        try:
                            self.onchange(self.value)
                        except Exception as e:
                            log_error("UISlider error: {}".format(e))
                return True
        else:
            self.pressed = False
        return False


class UIProgressBar(UIWidget):
    def __init__(self, x, y, w, h=20, value=0, min_val=0, max_val=100,
                 color=ACCENT_GREEN, bg=SURFACE_ALT, border_color=BORDER,
                 radius=6, show_percent=False, font=None):
        super().__init__(x, y, w, h)
        self.min_val = min_val
        self.max_val = max_val if max_val > min_val else min_val + 1
        self.value = _clamp(value, self.min_val, self.max_val)
        self.color = color
        self.bg = bg
        self.border_color = border_color
        self.radius = radius
        self.show_percent = show_percent
        self.font = font

    def set_value(self, val, redraw=True):
        nv = _clamp(val, self.min_val, self.max_val)
        if nv != self.value:
            self.value = nv
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        r = _clamp(self.radius, 0, min(self.w, self.h) // 2)
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.bg, filled=True)
        ratio = _clamp((self.value - self.min_val) / (self.max_val - self.min_val), 0.0, 1.0)
        fw = int(self.w * ratio)
        if fw > 0:
            draw_rounded_rect(self.x, self.y, fw, self.h, min(r, fw // 2), self.color, filled=True)
        if self.border_color is not None:
            draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.border_color, filled=False)
        if self.show_percent:
            pct = "{}%".format(int(round(ratio * 100)))
            draw_text_in_rect(self.x, self.y, self.w, self.h, pct, WHITE, TRANSPARENT, self.font, 1)


class UIGauge(UIWidget):
    def __init__(self, cx, cy, r=48, value=0, min_val=0, max_val=100,
                 thickness=10, color=ACCENT_BLUE, bg=SURFACE_ALT,
                 text_color=WHITE, label="", unit="", font=None):
        super().__init__(cx - r, cy - r, r * 2, r * 2)
        self.min_val = min_val
        self.max_val = max_val if max_val > min_val else min_val + 1
        self.value = _clamp(value, self.min_val, self.max_val)
        self.thickness = max(2, int(thickness))
        self.color = color
        self.bg = bg
        self.text_color = text_color
        self.label = str(label)
        self.unit = str(unit)
        self.font = font

    def set_value(self, val, redraw=True):
        nv = _clamp(val, self.min_val, self.max_val)
        if nv != self.value:
            self.value = nv
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        r_out = min(self.w, self.h) // 2
        cx = self.x + self.w // 2
        cy = self.y + self.h // 2
        thick = min(self.thickness, max(2, r_out // 2))
        r_in = max(1, r_out - thick)

        draw_ring(cx, cy, r_in, r_out, self.bg, 135, 405)
        ratio = _clamp((self.value - self.min_val) / (self.max_val - self.min_val), 0.0, 1.0)
        sweep = int(270 * ratio)
        if sweep > 0:
            draw_ring(cx, cy, r_in, r_out, self.color, 135, 135 + sweep)

        fill_circle(cx, cy, max(1, r_in - 2), background)
        val_str = "{}{}".format(self.value, self.unit)
        _, th = text_size(val_str, self.font, 1)
        draw_text_centered(cx, cy - th // 2 - (6 if self.label else 0), val_str, self.text_color, background, 1, self.font)
        if self.label:
            draw_text_centered(cx, cy + th // 2, self.label, TEXT_MUTED, background, 1, FONT_8X8)


class UINumberBox(UIWidget):
    """[- / value / +] numeric stepper widget."""

    def __init__(self, x, y, w=120, h=36, value=0, min_val=0, max_val=100, step=1,
                 onchange=None, color=SURFACE_ALT, btn_color=ACCENT_BLUE, text_color=WHITE, font=None):
        super().__init__(x, y, w, h)
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.value = _clamp(value, min_val, max_val)
        self.onchange = onchange
        self.color = color
        self.btn_color = btn_color
        self.text_color = text_color
        self.font = font
        self._active_side = 0

    def set_value(self, val, redraw=True):
        nv = _clamp(val, self.min_val, self.max_val)
        if nv != self.value:
            self.value = nv
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        bw = min(self.h, self.w // 3)
        draw_rounded_rect(self.x, self.y, self.w, self.h, 6, self.color, filled=True)
        draw_rounded_rect(self.x, self.y, bw, self.h, 6,
                           darken(self.btn_color, 60) if self._active_side == -1 else self.btn_color, filled=True)
        draw_rounded_rect(self.x + self.w - bw, self.y, bw, self.h, 6,
                          darken(self.btn_color, 60) if self._active_side == 1 else self.btn_color, filled=True)
        draw_text_in_rect(self.x, self.y, bw, self.h, "-", WHITE, TRANSPARENT, self.font, 1)
        draw_text_in_rect(self.x + self.w - bw, self.y, bw, self.h, "+", WHITE, TRANSPARENT, self.font, 1)
        draw_text_in_rect(self.x + bw, self.y, self.w - 2 * bw, self.h,
                          str(self.value), self.text_color, self.color, self.font, 1)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        bw = min(self.h, self.w // 3)
        if pt is not None:
            if self.contains(pt[0], pt[1]):
                self.pressed = True
                if pt[0] < self.x + bw:
                    self._active_side = -1
                elif pt[0] > self.x + self.w - bw:
                    self._active_side = 1
                else:
                    self._active_side = 0
                self.draw()
                return True
        else:
            if self.pressed:
                self.pressed = False
                side = self._active_side
                self._active_side = 0
                if side != 0:
                    nv = _clamp(self.value + side * self.step, self.min_val, self.max_val)
                    if nv != self.value:
                        self.value = nv
                        if self.onchange:
                            try:
                                self.onchange(self.value)
                            except Exception as e:
                                log_error("UINumberBox error: {}".format(e))
                self.draw()
                return True
        return False


class UIList(UIWidget):
    """Scrollable/selectable list of items."""

    def __init__(self, x, y, w, h, items=None, onselect=None,
                 item_h=30, bg=SURFACE, fg=WHITE, sel_bg=ACCENT_BLUE,
                 border=BORDER, font=None):
        super().__init__(x, y, w, h)
        self.items = list(items) if items else []
        self.onselect = onselect
        self.item_h = max(18, int(item_h))
        self.bg = bg
        self.fg = fg
        self.sel_bg = sel_bg
        self.border = border
        self.font = font
        self.selected = -1
        self.scroll = 0
        self._touch_y0 = None

    def set_items(self, items, redraw=True):
        self.items = list(items)
        self.selected = -1
        self.scroll = 0
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        if self.border is not None:
            draw_rect(self.x, self.y, self.w, self.h, self.border, fill=False)
        vis_rows = max(1, self.h // self.item_h)
        for i in range(vis_rows):
            idx = self.scroll + i
            if idx >= len(self.items):
                break
            ry = self.y + i * self.item_h
            sel = (idx == self.selected)
            rbg = self.sel_bg if sel else self.bg
            if sel:
                fill_rect(self.x + 1, ry + 1, self.w - 2, self.item_h - 1, rbg)
            txt = str(self.items[idx])
            _, th = text_size(txt, self.font, 1)
            draw_text(self.x + 8, ry + max(0, (self.item_h - th) // 2), txt, self.fg, rbg, self.font, 1)
            if i < vis_rows - 1:
                draw_hline(self.x + 4, ry + self.item_h - 1, self.w - 8, BORDER)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            if self.contains(pt[0], pt[1]):
                self.pressed = True
                row = (pt[1] - self.y) // self.item_h
                idx = self.scroll + row
                if 0 <= idx < len(self.items) and idx != self.selected:
                    self.selected = idx
                    self.draw()
                return True
        else:
            if self.pressed:
                self.pressed = False
                if 0 <= self.selected < len(self.items) and self.onselect:
                    try:
                        self.onselect(self.selected, self.items[self.selected])
                    except TypeError:
                        self.onselect(self.selected)
                    except Exception as e:
                        log_error("UIList error: {}".format(e))
                return True
        return False


class UIDropdown(UIWidget):
    """Collapsible dropdown selector."""

    def __init__(self, x, y, w, h=34, options=None, selected=0, onchange=None,
                 bg=SURFACE_ALT, fg=WHITE, accent=ACCENT_BLUE, font=None):
        super().__init__(x, y, w, h)
        self.options = [str(o) for o in (options or ["Option 1"])]
        self.selected = _clamp(selected, 0, len(self.options) - 1)
        self.onchange = onchange
        self.bg = bg
        self.fg = fg
        self.accent = accent
        self.font = font
        self.expanded = False
        self._base_h = self.h

    def draw(self):
        if not self.visible:
            return
        draw_rounded_rect(self.x, self.y, self.w, self._base_h, 6, self.bg, filled=True)
        draw_rounded_rect(self.x, self.y, self.w, self._base_h, 6, BORDER, filled=False)
        cur = self.options[self.selected] if self.options else ""
        _, th = text_size(cur, self.font, 1)
        draw_text(self.x + 10, self.y + max(0, (self._base_h - th) // 2), cur, self.fg, self.bg, self.font, 1)
        ax = self.x + self.w - 18
        ay = self.y + self._base_h // 2
        draw_triangle(ax - 5, ay - 3, ax + 5, ay - 3, ax, ay + 4, self.fg, filled=True)

        if self.expanded:
            vis = min(len(self.options), max(1, (HEIGHT - (self.y + self._base_h)) // self._base_h))
            dh = vis * self._base_h
            dy = self.y + self._base_h
            fill_rect(self.x, dy, self.w, dh, SURFACE)
            draw_rect(self.x, dy, self.w, dh, self.accent, fill=False)
            for i in range(vis):
                ry = dy + i * self._base_h
                bgc = self.accent if i == self.selected else SURFACE
                if i == self.selected:
                    fill_rect(self.x + 1, ry + 1, self.w - 2, self._base_h - 1, bgc)
                draw_text(self.x + 10, ry + max(0, (self._base_h - th) // 2),
                          self.options[i], self.fg, bgc, self.font, 1)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            vis = min(len(self.options), max(1, (HEIGHT - (self.y + self._base_h)) // self._base_h))
            full_h = self._base_h * (1 + (vis if self.expanded else 0))
            if _point_in_rect(pt[0], pt[1], self.x, self.y, self.w, full_h):
                self.pressed = True
                self._last_pt = pt
                return True
            elif self.expanded:
                self.expanded = False
                if active_screen:
                    active_screen.draw()
                else:
                    self.draw()
        else:
            if self.pressed:
                self.pressed = False
                pt0 = getattr(self, "_last_pt", None)
                if pt0 is not None:
                    if pt0[1] < self.y + self._base_h:
                        self.expanded = not self.expanded
                        if not self.expanded and active_screen:
                            active_screen.draw()
                        else:
                            self.draw()
                    elif self.expanded:
                        idx = (pt0[1] - (self.y + self._base_h)) // self._base_h
                        if 0 <= idx < len(self.options):
                            self.selected = idx
                            if self.onchange:
                                try:
                                    self.onchange(self.selected, self.options[self.selected])
                                except TypeError:
                                    self.onchange(self.selected)
                        self.expanded = False
                        if active_screen:
                            active_screen.draw()
                        else:
                            self.draw()
                return True
        return False


class UITabs(UIWidget):
    """Segmented tab bar at top or bottom of screen."""

    def __init__(self, x, y, w, h=36, tabs=None, selected=0, onchange=None,
                 bg=SURFACE, active_bg=ACCENT_BLUE, fg=WHITE, font=None):
        super().__init__(x, y, w, h)
        self.tabs = [str(t) for t in (tabs or ["Tab 1", "Tab 2"])]
        self.selected = _clamp(selected, 0, max(0, len(self.tabs) - 1))
        self.onchange = onchange
        self.bg = bg
        self.active_bg = active_bg
        self.fg = fg
        self.font = font

    def draw(self):
        if not self.visible or not self.tabs:
            return
        draw_rounded_rect(self.x, self.y, self.w, self.h, 6, self.bg, filled=True)
        n = len(self.tabs)
        tw = self.w // n
        for i, title in enumerate(self.tabs):
            tx = self.x + i * tw
            cw = (self.w - i * tw) if i == n - 1 else tw
            if i == self.selected:
                draw_rounded_rect(tx + 2, self.y + 2, cw - 4, self.h - 4, 5, self.active_bg, filled=True)
            draw_text_in_rect(tx, self.y, cw, self.h, title,
                              self.fg if i == self.selected else TEXT_MUTED,
                              self.active_bg if i == self.selected else self.bg, self.font, 1)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled or not self.tabs:
            return False
        if pt is not None and self.contains(pt[0], pt[1]):
            self.pressed = True
            idx = _clamp((pt[0] - self.x) * len(self.tabs) // max(1, self.w), 0, len(self.tabs) - 1)
            if idx != self.selected:
                self.selected = idx
                self.draw()
                if self.onchange:
                    try:
                        self.onchange(self.selected, self.tabs[self.selected])
                    except TypeError:
                        self.onchange(self.selected)
            return True
        else:
            self.pressed = False
        return False


class UIChart(UIWidget):
    """Real-time line or bar chart widget."""

    def __init__(self, x, y, w, h, data=None, min_val=0, max_val=100,
                 mode="line", color=ACCENT_GREEN, bg=SURFACE, grid_color=BORDER):
        super().__init__(x, y, w, h)
        self.data = list(data) if data else []
        self.min_val = min_val
        self.max_val = max_val if max_val > min_val else min_val + 1
        self.mode = mode
        self.color = color
        self.bg = bg
        self.grid_color = grid_color

    def push(self, val, max_points=32, redraw=True):
        self.data.append(val)
        if len(self.data) > max_points:
            self.data.pop(0)
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        draw_rect(self.x, self.y, self.w, self.h, self.grid_color, fill=False)
        for g in range(1, 4):
            gy = self.y + (self.h * g) // 4
            draw_hline(self.x + 1, gy, self.w - 2, DARK_GRAY)
        if not self.data:
            return
        n = len(self.data)
        span = self.max_val - self.min_val
        if self.mode == "bar":
            bw = max(2, (self.w - 8) // max(1, n))
            for i, v in enumerate(self.data):
                ratio = _clamp((v - self.min_val) / span, 0.0, 1.0)
                bh = int((self.h - 6) * ratio)
                bx = self.x + 4 + i * bw
                by = self.y + self.h - 3 - bh
                if bh > 0:
                    fill_rect(bx, by, max(1, bw - 2), bh, self.color)
        else:
            if n == 1:
                ratio = _clamp((self.data[0] - self.min_val) / span, 0.0, 1.0)
                py = self.y + self.h - 3 - int((self.h - 6) * ratio)
                fill_circle(self.x + self.w // 2, py, 3, self.color)
            else:
                prev_x = prev_y = None
                for i, v in enumerate(self.data):
                    ratio = _clamp((v - self.min_val) / span, 0.0, 1.0)
                    px = self.x + 4 + i * (self.w - 8) // (n - 1)
                    py = self.y + self.h - 3 - int((self.h - 6) * ratio)
                    if prev_x is not None:
                        draw_line(prev_x, prev_y, px, py, self.color, 2)
                    prev_x, prev_y = px, py


class UICard(UIWidget):
    """Panel / container widget that groups child widgets."""

    def __init__(self, x, y, w, h, title=None, bg=SURFACE, border=BORDER,
                 title_color=WHITE, radius=10, font=None):
        super().__init__(x, y, w, h)
        self.title = title
        self.bg = bg
        self.border = border
        self.title_color = title_color
        self.radius = radius
        self.font = font
        self.children = []

    def add(self, widget):
        widget.parent = self
        if widget not in self.children:
            self.children.append(widget)
        return widget

    def draw(self):
        if not self.visible:
            return
        r = _clamp(self.radius, 0, min(self.w, self.h) // 2)
        draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.bg, filled=True)
        if self.border is not None:
            draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.border, filled=False)
        if self.title:
            draw_text(self.x + 10, self.y + 8, self.title, self.title_color, self.bg, self.font, 1)
            _, th = text_size(self.title, self.font, 1)
            draw_hline(self.x + 6, self.y + th + 12, max(1, self.w - 12), BORDER)
        for c in self.children:
            if c.visible:
                c.draw()

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        for c in reversed(self.children):
            if c.handle_touch(pt):
                return True
        return False


UIPanel = UICard


class UIHeader(UIWidget):
    """Full-width top navigation/status header that automatically spans WIDTH."""

    def __init__(self, title="", height=32, bg=SURFACE, fg=WHITE, right_text="", font=None):
        super().__init__(0, 0, WIDTH, height)
        self.title = str(title)
        self.right_text = str(right_text)
        self.bg = bg
        self.fg = fg
        self.font = font

    def _relayout(self, old_w, old_h, new_w, new_h):
        self.x = 0
        self.y = 0
        self.w = new_w

    def draw(self):
        if not self.visible:
            return
        fill_rect(0, 0, WIDTH, self.h, self.bg)
        draw_hline(0, self.h - 1, WIDTH, BORDER)
        _, th = text_size(self.title, self.font, 1)
        ty = max(0, (self.h - th) // 2)
        draw_text(10, ty, self.title, self.fg, self.bg, self.font, 1)
        if self.right_text:
            draw_text_right(WIDTH - 10, ty, self.right_text, TEXT_MUTED, self.bg, 1, self.font)


class UIModal(UICard):
    """Centered dialog modal with OK/Cancel actions."""

    def __init__(self, title="Confirm", message="", w=240, h=140,
                 on_ok=None, on_cancel=None, font=None):
        w = min(w, WIDTH - 24)
        h = min(h, HEIGHT - 24)
        super().__init__((WIDTH - w) // 2, (HEIGHT - h) // 2, w, h,
                         title=title, bg=SURFACE_ALT, border=WHITE, radius=10, font=font)
        self.message = str(message)
        self.on_ok = on_ok
        self.on_cancel = on_cancel
        bw = (w - 30) // 2
        by = self.y + self.h - 42
        self.btn_ok = UIButton(self.x + 10, by, bw, 32, "OK",
                               action=self._do_ok, color=ACCENT_BLUE, font=font)
        self.btn_cancel = UIButton(self.x + 20 + bw, by, bw, 32, "Cancel",
                                   action=self._do_cancel, color=DARK_GRAY, font=font)
        self.add(self.btn_ok)
        self.add(self.btn_cancel)

    def _do_ok(self):
        self.hide()
        if self.on_ok:
            self.on_ok()

    def _do_cancel(self):
        self.hide()
        if self.on_cancel:
            self.on_cancel()

    def draw(self):
        if not self.visible:
            return
        super().draw()
        if self.message:
            draw_text_in_rect(self.x + 10, self.y + 30, self.w - 20, self.h - 76,
                              self.message, WHITE, self.bg, self.font, 1)


class UIScreen:
    """Screen manager grouping widgets and handling automatic orientation updates."""

    def __init__(self, bg=BLACK, widgets=None):
        self.bg = bg
        self.widgets = list(widgets) if widgets else []

    def add(self, widget):
        if widget not in self.widgets:
            self.widgets.append(widget)
        return widget

    def show(self):
        global active_screen
        active_screen = self
        set_background(self.bg)
        self.draw()

    def draw(self):
        clear(self.bg)
        for w in self.widgets:
            if w.visible:
                w.draw()

    def poll(self):
        return poll(self.widgets)


Screen = UIScreen


def set_screen(screen):
    screen.show()


def poll(widgets=None):
    """Sample touch once and dispatch to widgets (top-most first)."""
    if widgets is None:
        widgets = active_screen.widgets if active_screen is not None else _registered_widgets
    pt = get_touch()
    handled = False
    for w in reversed(widgets):
        if w.handle_touch(pt):
            handled = True
            if pt is not None:
                break
    return handled
