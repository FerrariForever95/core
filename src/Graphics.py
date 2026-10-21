"""
Graphics.py -- Complete Orientation-Aware UI Widget Stack for Zeno `lcd` + `touch`
(ILI9488 8-bit parallel panel, 4-wire resistive touch, ESP32-S3). MicroPython.

Supports automatic orientation switching (Portrait 320x480 & Landscape 480x320)
across rotations 0, 1, 2, and 3.

RULE: every class takes the lcd object as its FIRST argument, so one lcd
object is used for everything:

    import lcd
    import Graphics as g
    g.init_display(1)
    txt = g.UIText(lcd, 10, 10, "Hello")
    btn = g.UIButton(lcd, 10, 40, 100, 36, "OK", action=on_ok)
    win = g.UIWindow(lcd, title="Demo")
    kb  = g.VirtualKeyboard(lcd)
    box = g.UIInputTextBox(lcd, 10, 100, 200, 24, kb)
    g.DialogBox(lcd, title="Hi", message="Sure?").show()
    g.UIToast(lcd, "Saved").show()

Each instance keeps it as `self.lcd`, and the drawing primitives are routed
through it (see bind_lcd()).
"""

import time
import math
import json
import micropython
import lcd
import touch
import bmp
import png
import jpeg
from machine import Pin

pin = Pin(38, Pin.OUT)                # backlight enable

# ---------------------------------------------------------------------
# Globals & Dynamic Orientation State
# ---------------------------------------------------------------------

WIDTH = 480
HEIGHT = 320
ROTATION = 1                        # 0=Portrait, 1=Landscape, 2=Portrait Inv, 3=Landscape Inv

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
# lcd object binding
# ---------------------------------------------------------------------

def bind_lcd(obj):
    """Route all drawing primitives through this lcd object.
    Every class constructor calls this with the lcd it was given."""
    global lcd
    if obj is None or isinstance(obj, (int, float, str, bool, list, tuple, bytes, bytearray)):
        raise TypeError("first argument must be the lcd object, e.g. UIButton(lcd, x, y, ...)")
    lcd = obj
    return obj


def get_lcd():
    return lcd


# ---------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------

def color565(r, g, b):
    return (r & 0xf8) << 8 | (g & 0xfc) << 3 | b >> 3


def blend(c0, c1, alpha):
    """alpha 0..255: 0 -> c0, 255 -> c1."""
    return lcd.blend(c0, c1, alpha)

def draw_transparent_image(path, x, y, w=None, h=None, max_w=None, max_h=None, transparent_color=0x0000):
    """Draws any image (BMP, PNG, JPEG) with a transparent color key (default black 0x0000).
    Skips matching pixels so backgrounds and UI gradients show through seamlessly."""
    x, y = int(x), int(y)
    decoder = _get_image_decoder(path)
    
    fit_w = max_w if max_w is not None else w
    fit_h = max_h if max_h is not None else h
    
    try:
        img = decoder.open(path, fit=(fit_w, fit_h) if (fit_w or fit_h) else None)
        dw, dh = img.width, img.height
        
        if _capturing:
            _capture_ops.append(('image_transparent', path, x, y, dw, dh))
            
        buf = memoryview(img.buf)
        for row in range(dh):
            py = y + row
            if py < 0 or py >= HEIGHT:
                continue
            row_offset = row * dw * 2
            start_col = None
            run_pixels = bytearray()
            
            for col in range(dw):
                px = x + col
                if px < 0 or px >= WIDTH:
                    continue
                idx = row_offset + (col * 2)
                lo = buf[idx]
                hi = buf[idx + 1]
                color = (hi << 8) | lo
                
                if color == transparent_color:
                    # Flush run on transparent pixel
                    if start_col is not None and len(run_pixels) > 0:
                        lcd.blit(start_col, py, len(run_pixels) // 2, 1, run_pixels)
                        start_col = None
                        run_pixels = bytearray()
                else:
                    # Accumulate opaque pixel
                    if start_col is None:
                        start_col = px
                    run_pixels.append(lo)
                    run_pixels.append(hi)
                    
            # Flush final run of the row
            if start_col is not None and len(run_pixels) > 0:
                lcd.blit(start_col, py, len(run_pixels) // 2, 1, run_pixels)
        return dw, dh
    except Exception as e:
        log_error("Failed to draw transparent image {}: {}".format(path, e))
        return 0, 0
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

def init_display(rotation=1, lcd_obj=None):
    """Bring up the panel and touch in any orientation (0=Portrait, 1=Landscape, 2=Inv Portrait, 3=Inv Landscape).
    Optionally pass lcd_obj to bind a specific lcd object first.
    Returns (WIDTH, HEIGHT)."""
    global WIDTH, HEIGHT, ROTATION, REF_WIDTH, REF_HEIGHT, _touch_ok, _touch_last, _touch_t

    if lcd_obj is not None:
        bind_lcd(lcd_obj)
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
    """Debounced, calibrated (x, y) clamped to current screen bounds, or None.
    Filters out idle (0, 0) ADC noise when released."""
    global _touch_last, _touch_t
    if not _touch_ok:
        return None
    now = time.ticks_ms()
    if _touch_t is None or time.ticks_diff(now, _touch_t) >= TOUCH_MIN_INTERVAL_MS:
        _touch_t = now
        pt = touch.read()
        # Filter out None and raw idle/release noise at (0, 0)
        if pt is not None and not (pt[0] == 0 and pt[1] == 0):
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
# Native Image Loading (BMP / PNG / JPEG)
# ---------------------------------------------------------------------

def _get_image_decoder(path):
    ext = path.split(".")[-1].lower()
    if ext == "bmp":
        return bmp
    elif ext == "png":
        return png
    elif ext in ("jpg", "jpeg"):
        return jpeg
    else:
        # Fallback inspection by content sniffing if needed
        return bmp


def draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None):
    """Universal native image renderer supporting BMP, PNG, and JPEG formats."""
    x, y = int(x), int(y)
    
    # Choose appropriate native decoder module based on extension
    decoder = _get_image_decoder(path)
    
    # Fit constraints calculation
    fit_w = max_w if max_w is not None else w
    fit_h = max_h if max_h is not None else h
    
    try:
        # Open and fit using native modules
        img = decoder.open(path, fit=(fit_w, fit_h) if (fit_w or fit_h) else None)
        dw, dh = img.width, img.height
        
        if _capturing:
            _capture_ops.append(('image', path, x, y, dw, dh))
            
        lcd.blit(x, y, dw, dh, img.buf)
        return dw, dh
    except Exception as e:
        log_error("Failed to draw image {}: {}".format(path, e))
        return 0, 0


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
    
    decoder = _get_image_decoder(path)
    img = decoder.open(path)
    native_w, native_h = img.src_size if hasattr(img, "src_size") else (img.width, img.height)
    
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
        elif k == 'image':
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
# Every constructor: Class(lcd, ...)
# ---------------------------------------------------------------------

class UIWidget:
    """Base class for all UI widgets. Automatically adapts coordinates and sizes
    when the display switches between Portrait (320x480) and Landscape (480x320)."""

    def __init__(self, lcd, x, y, w, h):
        self.lcd = bind_lcd(lcd)
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
    def __init__(self, lcd, x, y, text="", color=WHITE, bg=TRANSPARENT,
                 font=None, scale=1, align="left", w=0, h=0, fg=None):
        bind_lcd(lcd)
        if fg is not None:
            color = fg
        tw, th = text_size(text, font, scale)
        super().__init__(lcd, x, y, w if w > 0 else max(1, tw), h if h > 0 else max(1, th))
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


class UIText(UILabel):
    """bg=None paints the current global background colour behind the text."""

    def __init__(self, lcd, x, y, text="", fg=WHITE, bg=None, font=None, scale=1):
        super().__init__(lcd, x, y, text, color=fg, bg=bg, font=font, scale=scale)


class UIButton(UIWidget):
    def __init__(self, lcd, x, y, w, h, text="", action=None,
                 color=ACCENT_BLUE, text_color=WHITE, pressed_color=None,
                 border_color=None, radius=8, font=None, scale=1,
                 bg=None, fg=None, label=None, margin=2):
        super().__init__(lcd, x, y, w, h)
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
        self.margin = margin

    def contains(self, px, py, margin=None):
        return _point_in_rect(px, py, self.x, self.y, self.w, self.h,
                            self.margin if margin is None else margin)

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
    """Button that renders a 1bpp bitmap icon, a BMP file, or plain text."""

    def __init__(self, lcd, x, y, w, h, icon_bits=None, icon_w=0, icon_h=0,
                 bmp_path=None, text="", action=None, color=None,
                 fg=None, radius=8, font=None, scale=1, label=None, bg=None):
        if isinstance(icon_bits, str):          # old positional form
            label = icon_bits
            icon_bits = None
            if bg is None and icon_w:
                bg = icon_w
            if fg is None and icon_h:
                fg = icon_h
            if action is None and callable(bmp_path):
                action = bmp_path
            icon_w = 0
            icon_h = 0
            bmp_path = None
        if label is not None:
            text = label
        if bg is not None:
            color = bg
        if color is None:
            color = SURFACE_ALT
        if fg is None:
            fg = WHITE
        super().__init__(lcd, x, y, w, h, text=text, action=action, color=color,
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


class UIBMPButton(UIWidget):
    """Image button with optional pressed image. Fires `action` on release.
    `margin` widens the touch area."""

    def __init__(self, lcd, x, y, w, h, bmp, *, bmp_pressed=None, margin=5, action=None):
        super().__init__(lcd, x, y, w, h)
        self.bmp = bmp
        self.bmp_pressed = bmp_pressed
        self.margin = margin
        self.action = action

    def contains(self, px, py, margin=None):
        return _point_in_rect(px, py, self.x, self.y, self.w, self.h,
                            self.margin if margin is None else margin)

    def draw(self):
        if not self.visible:
            return
        path = self.bmp_pressed if (self.pressed and self.bmp_pressed) else self.bmp
        try:
            draw_bmp(path, self.x, self.y, self.w, self.h)
        except Exception as e:
            log_error("UIBMPButton draw failed ({}): {}".format(path, e))

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is not None:
            inside = self.contains(pt[0], pt[1])
            if inside and not self.pressed:
                self.pressed = True
                if self.bmp_pressed:
                    self.draw()
            elif not inside and self.pressed:
                self.pressed = False
                if self.bmp_pressed:
                    self.draw()
            return self.pressed
        if self.pressed:
            self.pressed = False
            if self.bmp_pressed:
                self.draw()
            if self.action:
                try:
                    self.action()
                except Exception as e:
                    log_error("UIBMPButton action failed: {}".format(e))
            return True
        return False


class UIToggle(UIWidget):
    """Toggle switch widget (also aliased as UISwitch)."""

    def __init__(self, lcd, x, y, w=54, h=28, value=False, onchange=None, action=None,
                 on_color=ACCENT_GREEN, off_color=SURFACE_ALT, knob_color=WHITE):
        super().__init__(lcd, x, y, w, h)
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


class UIToggleSwitch(UIToggle):
    """Old UIToggleSwitch(x, y, w, h, state, on_color, off_color, knob, action)."""

    def __init__(self, lcd, x, y, w=50, h=26, state=False,
                 on_color=color565(0, 180, 0), off_color=color565(120, 120, 120),
                 knob=WHITE, action=None):
        super().__init__(lcd, x, y, w, h, value=state, action=action,
                         on_color=on_color, off_color=off_color, knob_color=knob)

    state = property(lambda self: self.value, lambda self, v: self.set_value(v))


class UICheckbox(UIWidget):
    """UICheckbox(lcd, x, y, label, value|checked, onchange|action)."""

    def __init__(self, lcd, x, y, label="", value=False, onchange=None, action=None,
                 size=22, color=ACCENT_BLUE, text_color=WHITE, font=None, scale=1,
                 checked=None):
        bind_lcd(lcd)
        if checked is not None:
            value = checked
        tw, th = text_size(label, font, scale)
        super().__init__(lcd, x, y, size + (8 + tw if label else 0), max(size, th))
        self.box_size = size
        self.label = str(label)
        self.value = bool(value)
        self.onchange = onchange or action
        self.color = color
        self.text_color = text_color
        self.font = font
        self.scale = max(1, int(scale))

    checked = property(lambda self: self.value, lambda self, v: self.set_value(v))

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


UICheckBox = UICheckbox


class UIRadioGroup(UIWidget):
    """Group of mutually exclusive radio options. onchange|action(idx, text)."""

    def __init__(self, lcd, x, y, options, selected=0, onchange=None,
                 spacing=28, color=ACCENT_BLUE, text_color=WHITE, font=None, scale=1,
                 action=None):
        bind_lcd(lcd)
        self.options = [str(o) for o in options]
        self.spacing = max(20, int(spacing))
        max_tw = 0
        for o in self.options:
            tw, _ = text_size(o, font, scale)
            if tw > max_tw:
                max_tw = tw
        total_w = 26 + max_tw
        total_h = max(22, len(self.options) * self.spacing)
        super().__init__(lcd, x, y, total_w, total_h)
        self.selected = _clamp(selected, 0, max(0, len(self.options) - 1))
        self.onchange = onchange or action
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
    def __init__(self, lcd, x, y, w, h=28, min_val=0, max_val=100, value=50,
                 onchange=None, color=ACCENT_BLUE, track_color=SURFACE_ALT,
                 knob_color=WHITE, step=1):
        super().__init__(lcd, x, y, w, h)
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


class UISliderClassic(UISlider):
    """Old UISlider(x, y, w, min_v, max_v, value, track, fill, knob, action)."""

    def __init__(self, lcd, x, y, w, min_v=0, max_v=100, value=0,
                 track=color565(80, 80, 80), fill=color565(0, 150, 255),
                 knob=WHITE, action=None, h=22):
        super().__init__(lcd, x, y, w, h, min_val=min_v, max_val=max_v, value=value,
                         onchange=action, color=fill, track_color=track,
                         knob_color=knob)


class UIProgressBar(UIWidget):
    """UIProgressBar(lcd, x, y, w, h, value, ...). `fg` is an alias of `color`;
    .set(val) is the old setter."""

    def __init__(self, lcd, x, y, w, h=20, value=0, min_val=0, max_val=100,
                 color=ACCENT_GREEN, bg=SURFACE_ALT, border_color=BORDER,
                 radius=6, show_percent=False, font=None, fg=None):
        super().__init__(lcd, x, y, w, h)
        if fg is not None:
            color = fg
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

    def set(self, val):
        self.set_value(val)

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
    def __init__(self, lcd, cx, cy, r=48, value=0, min_val=0, max_val=100,
                 thickness=10, color=ACCENT_BLUE, bg=SURFACE_ALT,
                 text_color=WHITE, label="", unit="", font=None):
        super().__init__(lcd, cx - r, cy - r, r * 2, r * 2)
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

    def __init__(self, lcd, x, y, w=120, h=36, value=0, min_val=0, max_val=100, step=1,
                 onchange=None, color=SURFACE_ALT, btn_color=ACCENT_BLUE, text_color=WHITE, font=None):
        super().__init__(lcd, x, y, w, h)
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


class UIStepper(UIWidget):
    """Old UIStepper(x, y, value, step, action). Fires action(value) on
    release (no blocking sleep). Optional min_val / max_val."""

    def __init__(self, lcd, x, y, value=0, step=1, action=None,
                 w=80, h=24, min_val=None, max_val=None,
                 color=color565(50, 50, 50), btn_color=color565(80, 80, 80),
                 text_color=WHITE, font=None):
        super().__init__(lcd, x, y, w, h)
        self.value = value
        self.step = step
        self.action = action
        self.min_val = min_val
        self.max_val = max_val
        self.color = color
        self.btn_color = btn_color
        self.text_color = text_color
        self.font = font
        self._side = 0

    def set_value(self, v, redraw=True):
        if self.min_val is not None and v < self.min_val:
            v = self.min_val
        if self.max_val is not None and v > self.max_val:
            v = self.max_val
        if v != self.value:
            self.value = v
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        bw = max(16, min(self.h, self.w // 3))
        fill_rect(self.x, self.y, self.w, self.h, self.color)
        lc = lighten(self.btn_color, 60) if self._side == -1 else self.btn_color
        rc = lighten(self.btn_color, 60) if self._side == 1 else self.btn_color
        fill_rect(self.x, self.y, bw, self.h, lc)
        fill_rect(self.x + self.w - bw, self.y, bw, self.h, rc)
        draw_text_in_rect(self.x, self.y, bw, self.h, "-", self.text_color, lc, self.font, 1)
        draw_text_in_rect(self.x + self.w - bw, self.y, bw, self.h, "+", self.text_color, rc, self.font, 1)
        draw_text_in_rect(self.x + bw, self.y, self.w - 2 * bw, self.h,
                          str(self.value), self.text_color, self.color, self.font, 1)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        bw = max(16, min(self.h, self.w // 3))
        if pt is not None:
            if self.contains(pt[0], pt[1], 0):
                side = -1 if pt[0] < self.x + bw else (1 if pt[0] >= self.x + self.w - bw else 0)
                if not self.pressed or side != self._side:
                    self.pressed = True
                    self._side = side
                    self.draw()
                return True
            if self.pressed:
                self.pressed = False
                self._side = 0
                self.draw()
            return False
        if self.pressed:
            self.pressed = False
            side = self._side
            self._side = 0
            if side != 0:
                self.set_value(self.value + side * self.step, redraw=False)
                self.draw()
                if self.action:
                    try:
                        self.action(self.value)
                    except Exception as e:
                        log_error("UIStepper action failed: {}".format(e))
            else:
                self.draw()
            return True
        return False


class UIList(UIWidget):
    """Scrollable/selectable list of items."""

    def __init__(self, lcd, x, y, w, h, items=None, onselect=None,
                 item_h=30, bg=SURFACE, fg=WHITE, sel_bg=ACCENT_BLUE,
                 border=BORDER, font=None):
        super().__init__(lcd, x, y, w, h)
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


class UIListView(UIWidget):
    """Old UIListView(x, y, w, h, items, item_h, bg, fg, sel, text_x,
    highlight, action). Drag vertically to scroll (pixel-smooth); a tap that
    doesn't move more than 6 px selects the row and fires action(idx, item)
    on release. Rows never draw outside the widget box."""

    DRAG_THRESHOLD = 6

    def __init__(self, lcd, x, y, w, h, items=None, item_h=24,
                 bg=color565(20, 20, 20), fg=WHITE, sel=color565(0, 120, 255),
                 text_x=6, highlight=False, action=None, font=None):
        super().__init__(lcd, x, y, w, h)
        self.items = list(items) if items else []
        self.item_h = max(12, int(item_h))
        self.bg = bg
        self.fg = fg
        self.sel = sel
        self.text_x = text_x
        self.highlight = highlight
        self.action = action
        self.font = font
        self.scroll = 0
        self.selected = -1
        self._y0 = None
        self._s0 = 0
        self._moved = False

    def set_items(self, items, redraw=True):
        self.items = list(items)
        self.selected = -1
        self.scroll = 0
        if redraw and self.visible:
            self.draw()

    def _max_scroll(self):
        return max(0, len(self.items) * self.item_h - self.h)

    def draw(self):
        if not self.visible:
            return
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        if not self.enabled or not self.items:
            return
        self.scroll = _clamp(self.scroll, 0, self._max_scroll())
        first = self.scroll // self.item_h
        offset = self.scroll % self.item_h
        rows = (self.h + offset + self.item_h - 1) // self.item_h
        bottom = self.y + self.h
        for i in range(rows):
            idx = first + i
            if idx >= len(self.items):
                break
            iy = self.y + i * self.item_h - offset
            top = iy if iy > self.y else self.y
            bot = iy + self.item_h
            if bot > bottom:
                bot = bottom
            if bot <= top:
                continue
            is_sel = self.highlight and idx == self.selected
            row_bg = self.sel if is_sel else self.bg
            if is_sel:
                fill_rect(self.x, top, self.w, bot - top, row_bg)
            txt = str(self.items[idx])
            _, th = text_size(txt, self.font, 1)
            ty = iy + (self.item_h - th) // 2
            if ty >= self.y and ty + th <= bottom:
                draw_text(self.x + self.text_x, ty, txt, self.fg, row_bg, self.font, 1)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled or not self.items:
            return False
        if pt is not None:
            if self._y0 is None:
                if not self.contains(pt[0], pt[1], 0):
                    return False
                self._y0 = pt[1]
                self._s0 = self.scroll
                self._moved = False
                return True
            dy = self._y0 - pt[1]
            if abs(dy) > self.DRAG_THRESHOLD:
                self._moved = True
            if self._moved:
                before = self.scroll
                self.scroll = _clamp(self._s0 + dy, 0, self._max_scroll())
                if self.scroll != before:
                    self.draw()
            return True
        if self._y0 is not None:
            y0 = self._y0
            moved = self._moved
            self._y0 = None
            if not moved:
                idx = (self.scroll + (y0 - self.y)) // self.item_h
                if 0 <= idx < len(self.items):
                    self.selected = idx
                    self.draw()
                    if self.action:
                        try:
                            self.action(idx, self.items[idx])
                        except TypeError:
                            self.action(idx)
                        except Exception as e:
                            log_error("UIListView action failed: {}".format(e))
            return True
        return False


class UIDropdown(UIWidget):
    """Collapsible dropdown selector."""

    def __init__(self, lcd, x, y, w, h=34, options=None, selected=0, onchange=None,
                 bg=SURFACE_ALT, fg=WHITE, accent=ACCENT_BLUE, font=None):
        super().__init__(lcd, x, y, w, h)
        self.options = [str(o) for o in (options or ["Option 1"])]
        self.selected = _clamp(selected, 0, len(self.options) - 1)
        self.onchange = onchange
        self.bg = bg
        self.fg = fg
        self.accent = accent
        self.font = font
        self.expanded = False
        self._base_h = self.h
        self._last_pt = None

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
                pt0 = self._last_pt
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
    """Segmented tab bar. onchange|action(idx, title); selected|active."""

    def __init__(self, lcd, x, y, w, h=36, tabs=None, selected=0, onchange=None,
                 bg=SURFACE, active_bg=ACCENT_BLUE, fg=WHITE, font=None,
                 active=None, action=None):
        super().__init__(lcd, x, y, w, h)
        if active is not None:
            selected = active
        self.tabs = [str(t) for t in (tabs or ["Tab 1", "Tab 2"])]
        self.selected = _clamp(selected, 0, max(0, len(self.tabs) - 1))
        self.onchange = onchange or action
        self.bg = bg
        self.active_bg = active_bg
        self.fg = fg
        self.font = font

    active = property(lambda self: self.selected,
                      lambda self, v: setattr(self, "selected", v))

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


UITabBar = UITabs


class UIChart(UIWidget):
    """Real-time line or bar chart widget."""

    def __init__(self, lcd, x, y, w, h, data=None, min_val=0, max_val=100,
                 mode="line", color=ACCENT_GREEN, bg=SURFACE, grid_color=BORDER):
        super().__init__(lcd, x, y, w, h)
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

    def __init__(self, lcd, x, y, w, h, title=None, bg=SURFACE, border=BORDER,
                 title_color=WHITE, radius=10, font=None):
        super().__init__(lcd, x, y, w, h)
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


class UIPanel(UICard):
    """Old UIPanel(x, y, w, h, title, bg, border, title_fg, title_bg):
    square-cornered panel with the old colour defaults and open() animation."""

    def __init__(self, lcd, x, y, w, h, title=None, bg=None, border=None,
                 title_fg=None, title_bg=None, radius=0, font=None):
        super().__init__(
            lcd, x, y, w, h, title=title,
            bg=bg if bg is not None else color565(30, 30, 30),
            border=border if border is not None else color565(80, 80, 80),
            title_color=title_fg if title_fg is not None else color565(200, 200, 200),
            radius=radius, font=font)
        self.title_bg = title_bg

    def open(self, steps=8, delay_ms=1):
        """Grow-in animation from the top-left corner, then full draw."""
        steps = max(1, int(steps))
        for i in range(1, steps + 1):
            cw = (self.w * i) // steps
            ch = (self.h * i) // steps
            fill_rect(self.x, self.y, cw, ch, self.bg)
            draw_rect(self.x, self.y, cw, ch, self.border)
            if delay_ms:
                time.sleep_ms(delay_ms)
        self.draw()


class UIHeader(UIWidget):
    """Full-width top navigation/status header that automatically spans WIDTH."""

    def __init__(self, lcd, title="", height=32, bg=SURFACE, fg=WHITE, right_text="", font=None):
        super().__init__(lcd, 0, 0, WIDTH, height)
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
    """Centered, non-blocking dialog modal with OK/Cancel actions."""

    def __init__(self, lcd, title="Confirm", message="", w=240, h=140,
                 on_ok=None, on_cancel=None, font=None):
        w = min(w, WIDTH - 24)
        h = min(h, HEIGHT - 24)
        super().__init__(lcd, (WIDTH - w) // 2, (HEIGHT - h) // 2, w, h,
                         title=title, bg=SURFACE_ALT, border=WHITE, radius=10, font=font)
        self.message = str(message)
        self.on_ok = on_ok
        self.on_cancel = on_cancel
        bw = (w - 30) // 2
        by = self.y + self.h - 42
        self.btn_ok = UIButton(lcd, self.x + 10, by, bw, 32, "OK",
                               action=self._do_ok, color=ACCENT_BLUE, font=font)
        self.btn_cancel = UIButton(lcd, self.x + 20 + bw, by, bw, 32, "Cancel",
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


class UIDivider(UIWidget):
    """Old UIDivider(x, y, w, color). vertical=True makes `length` the height."""

    def __init__(self, lcd, x, y, length, color=color565(100, 100, 100), vertical=False, thickness=1):
        if vertical:
            super().__init__(lcd, x, y, thickness, length)
        else:
            super().__init__(lcd, x, y, length, thickness)
        self.color = color

    def draw(self):
        if self.visible:
            fill_rect(self.x, self.y, self.w, self.h, self.color)


class UIStatusIndicator(UIWidget):
    """Coloured dot centred on (x, y). States: OK, WARN, ERR."""
    OK = 0
    WARN = 1
    ERR = 2

    def __init__(self, lcd, x, y, r=6, state=0):
        super().__init__(lcd, max(0, x - r), max(0, y - r), r * 2, r * 2)
        self.r = r
        self.state = state

    def set_state(self, state, redraw=True):
        if state != self.state:
            self.state = state
            if redraw and self.visible:
                self.draw()

    def draw(self):
        if not self.visible:
            return
        if self.state == self.OK:
            c = color565(0, 200, 0)
        elif self.state == self.WARN:
            c = color565(255, 165, 0)
        else:
            c = color565(200, 0, 0)
        fill_circle(self.x + self.w // 2, self.y + self.h // 2, self.r, c)


class UITextBoxView(UIWidget):
    """Wrapped, drag-scrollable text (8x8 font). Whole lines only are drawn,
    so partial rows never spill outside the box."""

    def __init__(self, lcd, x, y, w, h, text=None, fg=WHITE, bg=BLACK, padding=4, scale=1):
        super().__init__(lcd, x, y, w, h)
        self.fg = fg
        self.bg = bg
        self.padding = padding
        self.scale = max(1, int(scale))
        self.char_w = 8 * self.scale
        self.char_h = 8 * self.scale
        self.lines = []
        self.scroll_px = 0
        self._text = ""
        self._touch_active = False
        self._y0 = 0
        self._s0 = 0
        if text:
            self.set_text(text)

    def _rewrap(self):
        cols = max(1, (self.w - self.padding * 2) // self.char_w)
        self.lines = []
        for raw in self._text.split("\n"):
            while len(raw) > cols:
                self.lines.append(raw[:cols])
                raw = raw[cols:]
            if raw:
                self.lines.append(raw)

    def _relayout(self, old_w, old_h, new_w, new_h):
        UIWidget._relayout(self, old_w, old_h, new_w, new_h)
        self._rewrap()

    def set_text(self, text, redraw=False):
        self._text = str(text) if text else ""
        self.scroll_px = 0
        self._touch_active = False
        self._rewrap()
        if redraw and self.visible:
            self.draw()

    def _clamp_scroll(self):
        view_h = ((self.h - self.padding * 2) // self.char_h) * self.char_h
        max_scroll = max(0, len(self.lines) * self.char_h - view_h)
        if self.scroll_px < 0:
            self.scroll_px = 0
        elif self.scroll_px > max_scroll:
            self.scroll_px = max_scroll

    def draw(self):
        if not self.visible:
            return
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        if not self.enabled or not self.lines:
            return
        self._clamp_scroll()
        ch = self.char_h
        first = self.scroll_px // ch
        ty = self.y + self.padding - (self.scroll_px % ch)
        tx = self.x + self.padding
        end = min(len(self.lines), first + (self.h - self.padding * 2) // ch + 2)
        for i in range(first, end):
            if ty >= self.y and ty + ch <= self.y + self.h:
                draw_text(tx, ty, self.lines[i], self.fg, self.bg, FONT_8X8, self.scale)
            ty += ch

    def handle_touch(self, pt):
        if not self.visible or not self.enabled or not self.lines:
            return False
        if pt is not None:
            if not self._touch_active:
                if not self.contains(pt[0], pt[1], 0):
                    return False
                self._touch_active = True
                self._y0 = pt[1]
                self._s0 = self.scroll_px
                return True
            before = self.scroll_px
            self.scroll_px = self._s0 + (self._y0 - pt[1])
            self._clamp_scroll()
            if self.scroll_px != before:
                self.draw()
            return True
        if self._touch_active:
            self._touch_active = False
            return True
        return False


class IOSSlider(UIWidget):
    """Flicker-free knob slider (only the knob is erased/redrawn)."""
    KNOB = 16

    def __init__(self, lcd, x, y, w, min_v=0, max_v=100, value=0,
                 track_border=color565(40, 40, 40), track_fill=color565(90, 90, 90),
                 knob=color565(220, 220, 220), action=None):
        super().__init__(lcd, x, y, w, 18)
        self.min = min_v
        self.max = max_v if max_v > min_v else min_v + 1
        self.value = float(_clamp(value, self.min, self.max))
        self.track_border = track_border
        self.track_fill = track_fill
        self.knob_color = knob
        self.action = action
        self.dragging = False
        self.drag_offset = 0
        self._last_kx = self._kx_for(self.value)

    def _travel(self):
        return max(1, self.w - self.KNOB)

    def _kx_for(self, value):
        return self.x + int((value - self.min) / (self.max - self.min) * self._travel())

    def set_value(self, value, redraw=True):
        self.value = float(_clamp(value, self.min, self.max))
        self._last_kx = self._kx_for(self.value)
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        fill_rect(self.x - 2, self.y - 2, self.w + 4, self.h + 4, background)
        draw_rect(self.x, self.y, self.w, self.h, self.track_border)
        fill_rect(self.x + 1, self.y + 1, self.w - 2, self.h - 2, self.track_fill)
        draw_hline(self.x + 1, self.y + 1, self.w - 2, color565(140, 140, 140))
        self._last_kx = self._kx_for(self.value)
        self._draw_knob(self._last_kx)

    def _draw_knob(self, kx):
        ky = self.y + 1
        k = self.KNOB
        fill_rect(kx + 1, ky + 1, k, k, BLACK)
        fill_rect(kx, ky, k, k, self.knob_color)
        draw_hline(kx + 1, ky + 1, k - 2, WHITE)

    def _erase_knob(self, kx):
        x0 = max(self.x + 1, kx)
        x1 = min(self.x + self.w - 1, kx + self.KNOB + 2)
        if x1 > x0:
            fill_rect(x0, self.y + 1, x1 - x0, self.h - 2, self.track_fill)
            draw_hline(x0, self.y + 1, x1 - x0, color565(140, 140, 140))
        draw_rect(self.x, self.y, self.w, self.h, self.track_border)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is None:
            self.dragging = False
            return False
        tx, ty = pt
        if not self.dragging:
            if _point_in_rect(tx, ty, self._last_kx, self.y + 1, self.KNOB, self.KNOB, 6):
                self.dragging = True
                self.drag_offset = tx - self._last_kx
            else:
                return False
        new_x = _clamp(tx - self.drag_offset, self.x, self.x + self._travel())
        if new_x == self._last_kx:
            return True
        self._erase_knob(self._last_kx)
        self.value = self.min + (new_x - self.x) / self._travel() * (self.max - self.min)
        self._draw_knob(new_x)
        self._last_kx = new_x
        if self.action:
            try:
                self.action(self.value)
            except Exception as e:
                log_error("IOSSlider action failed: {}".format(e))
        return True


class UIScreen:
    """Screen manager grouping widgets and handling automatic orientation updates."""

    def __init__(self, lcd, bg=BLACK, widgets=None):
        self.lcd = bind_lcd(lcd)
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


def _unreg(*widgets):
    """Drop short-lived widgets from the global registry so they don't leak."""
    for w in widgets:
        try:
            _registered_widgets.remove(w)
        except ValueError:
            pass


# ---------------------------------------------------------------------
# UIWindow -- the old UIScreen (taskbar + title + exit button + background)
# ---------------------------------------------------------------------
class UIWindow(UIScreen):
    """background: None | RGB565 int | BMP path.  Exit button calls on_exit().
    Also carries the old UIScreen API: layer(), openscreen(), closescreen(),
    start_withoutexit(), check(), taskbar(), draw_gradient()."""

    def __init__(self, lcd, title=None, background=None, on_exit=None,
                 taskbar_color=color565(50, 50, 50), title_color=WHITE,
                 taskbar_height=35, exit_button=True, widgets=None):
        super().__init__(lcd, background if isinstance(background, int) else BLACK, widgets)
        self.wallpaper = background if isinstance(background, str) else None
        self.title = title
        self.on_exit = on_exit
        self.taskbar_color = taskbar_color
        self.title_color = title_color
        self.taskbar_height = taskbar_height
        self._grad = None
        self._grad_block = 8
        self._prev_bg = None
        self.exit_btn = None
        if exit_button:
            sz = max(16, taskbar_height - 5)
            self.exit_btn = UIButton(lcd, WIDTH - sz - 2, (taskbar_height - sz) // 2, sz, sz, "X",
                                     action=self._exit, color=color565(0, 0, 200), radius=4)

    def _exit(self):
        if self.on_exit:
            self.on_exit()

    def _draw_taskbar(self):
        h = self.taskbar_height
        fill_rect(0, 0, WIDTH, h, self.taskbar_color)
        if self.title:
            draw_text_centered(WIDTH // 2, (h - 8) // 2, self.title,
                               self.title_color, self.taskbar_color)
        if self.exit_btn is not None:
            self.exit_btn.y = (h - self.exit_btn.h) // 2
            self.exit_btn.draw()

    def taskbar(self, color, title, text_color=WHITE, height=35):
        """Old UIScreen.taskbar(color, text, text_color, height)."""
        self.taskbar_color = color
        self.title = title
        self.title_color = text_color
        self.taskbar_height = height
        self._draw_taskbar()

    def layer(self, x, y, width, height, color):
        """Old UIScreen.layer(): paint a filled rectangle."""
        fill_rect(x, y, width, height, color)

    def draw_gradient(self, c1, c2, angle=0, block_size=8):
        """0/180 vertical, 90/270 horizontal, 45/135 diagonal (blocky,
        `block_size` px). Kept as the window background and repainted by draw()."""
        angle = angle % 360
        self._grad = (c1, c2, angle)
        self._grad_block = max(2, int(block_size))
        self._paint_gradient()

    def _paint_gradient(self):
        c1, c2, a = self._grad
        if a in (45, 135):
            bs = self._grad_block
            total = WIDTH + HEIGHT
            for y in range(0, HEIGHT, bs):
                for x in range(0, WIDTH, bs):
                    t = (x + (HEIGHT - y)) / total if a == 45 else (x + y) / total
                    fill_rect(x, y, bs, bs, blend(c1, c2, int(_clamp(t, 0.0, 1.0) * 255)))
            return
        if a in (180, 270):
            c1, c2 = c2, c1
        draw_gradient_rect(0, 0, WIDTH, HEIGHT, c1, c2, a not in (90, 270))

    def draw(self):
        if self._grad is not None:
            self._paint_gradient()
        elif self.wallpaper:
            try:
                draw_bmp(self.wallpaper, 0, 0, WIDTH, HEIGHT)
            except Exception as e:
                log_error("UIWindow wallpaper failed: {}".format(e))
                clear(self.bg)
        else:
            clear(self.bg)
        self._draw_taskbar()
        for w in self.widgets:
            if w.visible:
                w.draw()

    def start(self, animate=True):
        self._prev_bg = background
        if animate:
            window_open_animation(0.4, 60, self.bg)
        self.show()

    def start_withoutexit(self, animate=False):
        """Kernel/home screen: taskbar but no exit button."""
        self.exit_btn = None
        self.start(animate=animate)

    def openscreen(self):
        window_open_animation(0.4, 60, self.bg)

    def closescreen(self):
        window_close_animation(0.4, 60)

    def close(self, animate=True):
        global active_screen
        if animate:
            window_close_animation(0.4, 60)
        if active_screen is self:
            active_screen = None
        if self._prev_bg is not None:
            set_background(self._prev_bg)

    def check(self):
        """Old UIScreen.check(): fire on_exit when the X is tapped."""
        if self.exit_btn is None:
            return False
        return bool(self.exit_btn.handle_touch(get_touch()))

    def poll(self):
        ws = self.widgets
        if self.exit_btn is not None:
            ws = ws + [self.exit_btn]
        return poll(ws)


# ---------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------
class Keyboard:
    DEBOUNCE_MS = 180

    def __init__(self, lcd, height=140, ok_action=None):
        self.lcd = bind_lcd(lcd)
        self._req_h = height
        self.height = height
        self.ok_action = ok_action

        # Android Material Dark Theme Palette (RGB565)
        self.COLOR_BG = 0x10A2       # Deep dark panel base
        self.COLOR_KEY = 0x3186      # Charcoal letter key
        self.COLOR_ACTION = 0x4208   # Modifier key (Space, Del, OK)
        self.COLOR_PRESS = 0x7BEF    # Tactile highlight on press
        self.COLOR_TEXT = 0xFFFF     # Crisp white text
        self.COLOR_BORDER = 0x0000   # Shadow / outline

        self.buffer = ""
        self.case_upper = False
        self.symbol_mode = False
        self.key_buttons = []
        self.last_touch_ms = 0

        self.update_dimensions()

    def update_dimensions(self):
        """Recalculates keyboard boundaries from the live WIDTH/HEIGHT (rotation-aware)."""
        self.width = WIDTH - 16
        self.x = 8
        self.height = min(self._req_h, HEIGHT - 60)
        self.y = HEIGHT - self.height - 8

    def _get_layout(self):
        if self.symbol_mode:
            return [
                ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
                ['-', '/', '\\', ':', ';', '(', ')', '_', '|', '+'],
                ['.', '@', '#', '$', '&', '%', 'ABC'],
                ['SPACE', 'DEL', 'OK'],
            ]

        layout = [
            ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P'],
            ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L'],
            ['Aa', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', 'DEL'],
            ['123', 'SPACE', 'OK'],
        ]

        if not self.case_upper:
            return [[k.lower() if len(k) == 1 and k.isalpha() else k for k in row] for row in layout]
        return layout

    def render(self):
        """Draws the compact keyboard panel and calculates precise key hitboxes dynamically."""
        self.update_dimensions()

        fill_rect(self.x, self.y, self.width, self.height, self.COLOR_BG)
        draw_rect(self.x, self.y, self.width, self.height, 0x4208)

        self.key_buttons = []
        layout = self._get_layout()
        rows = len(layout)

        row_h = (self.height - 10) // rows
        gap = 3

        for r_idx, row in enumerate(layout):
            ry = self.y + 5 + (r_idx * row_h)
            num_keys = len(row)

            if r_idx == 3:  # Bottom Row (Modifiers & Space)
                if self.symbol_mode:
                    spacew = self.width - 150 - (gap * 2)
                    self.key_buttons.append({'x': self.x + 5, 'y': ry, 'w': spacew, 'h': row_h - gap, 'label': 'SPACE'})
                    self.key_buttons.append({'x': self.x + 5 + spacew + gap, 'y': ry, 'w': 70, 'h': row_h - gap, 'label': 'DEL'})
                    self.key_buttons.append({'x': self.x + self.width - 75, 'y': ry, 'w': 70, 'h': row_h - gap, 'label': 'OK'})
                else:
                    self.key_buttons.append({'x': self.x + 5, 'y': ry, 'w': 65, 'h': row_h - gap, 'label': '123'})
                    spacew = self.width - 145 - (gap * 2)
                    self.key_buttons.append({'x': self.x + 75, 'y': ry, 'w': spacew, 'h': row_h - gap, 'label': 'SPACE'})
                    self.key_buttons.append({'x': self.x + self.width - 65, 'y': ry, 'w': 60, 'h': row_h - gap, 'label': 'OK'})
            else:
                total_gaps = (num_keys - 1) * gap
                avail_w = self.width - 10 - total_gaps
                k_w = avail_w // num_keys

                for k_idx, label in enumerate(row):
                    kw_custom = k_w + 10 if label in ["Aa", "DEL"] else k_w
                    kx = self.x + 5 + (k_idx * (k_w + gap))
                    self.key_buttons.append({'x': kx, 'y': ry, 'w': kw_custom, 'h': row_h - gap, 'label': label})

        for key in self.key_buttons:
            self._draw_key(key, pressed=False)

    def _draw_key(self, key, pressed=False):
        x, y, w, h, label = key['x'], key['y'], key['w'], key['h'], key['label']

        fill_col = self.COLOR_PRESS if pressed else (self.COLOR_ACTION if label in ["SPACE", "DEL", "OK", "123", "ABC", "Aa"] else self.COLOR_KEY)
        text_col = 0x0000 if pressed else self.COLOR_TEXT

        fill_rect(x, y, w, h, fill_col)
        draw_rect(x, y, w, h, self.COLOR_BORDER)

        tx = x + (w // 2) - (len(label) * 4)
        ty = y + (h // 2) - 4
        draw_text(tx, ty, label, text_col, fill_col, font=FONT_8X8)

    def check_touch(self):
        pt = get_touch()
        if not pt:
            return None

        now = time.ticks_ms()
        if self.last_touch_ms and time.ticks_diff(now, self.last_touch_ms) < self.DEBOUNCE_MS:
            return None

        tx, ty = pt

        for key in self.key_buttons:
            if key['x'] <= tx <= key['x'] + key['w'] and key['y'] <= ty <= key['y'] + key['h']:
                self._draw_key(key, pressed=True)
                time.sleep_ms(50)
                self._draw_key(key, pressed=False)

                lbl = key['label']
                self.last_touch_ms = now

                if lbl == "DEL":
                    if self.buffer:
                        self.buffer = self.buffer[:-1]
                elif lbl == "SPACE":
                    self.buffer += " "
                elif lbl == "OK":
                    if self.ok_action:
                        self.ok_action(self.buffer)
                    return self.buffer
                elif lbl == "Aa":
                    self.case_upper = not self.case_upper
                    self.render()
                elif lbl == "123":
                    self.symbol_mode = True
                    self.render()
                elif lbl == "ABC":
                    self.symbol_mode = False
                    self.render()
                else:
                    self.buffer += lbl

                while get_touch() is not None:
                    time.sleep_ms(10)

                return None
        return None

    def get_buffer(self, clear=False):
        out = self.buffer
        if clear:
            self.buffer = ""
        return out

    def clear_buffer(self):
        self.buffer = ""


class VitualKeyboard:
    DEBOUNCE_MS = 180

    def __init__(self, height=140, ok_action=None):
        self.height = height
        self.ok_action = ok_action

        # Android Material Dark Theme Palette (RGB565)
        self.COLOR_BG = 0x10A2       # Deep dark panel base
        self.COLOR_KEY = 0x3186      # Charcoal letter key
        self.COLOR_ACTION = 0x4208   # Modifier key (Space, Del, OK)
        self.COLOR_PRESS = 0x7BEF    # Tactile highlight on press
        self.COLOR_TEXT = 0xFFFF     # Crisp white text
        self.COLOR_BORDER = 0x0000   # Shadow / outline

        self.buffer = ""
        self.case_upper = False
        self.symbol_mode = False
        self.key_buttons = []
        self.last_touch_ms = 0

        self.update_dimensions()

    def update_dimensions(self):
        """Recalculates screen boundaries dynamically on rotation or resize."""
        disp_w = width if 'width' in globals() else 480
        disp_h = height if 'height' in globals() else 320

        self.width = disp_w - 16
        self.x = 8
        self.height = min(self.height, disp_h - 60)
        self.y = disp_h - self.height - 8

    def _get_layout(self):
        if self.symbol_mode:
            return [
                ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
                ['-', '/', '\\', ':', ';', '(', ')', '_', '|', '+'],
                ['.', '@', '#', '$', '&', '%', 'ABC'],
                ['SPACE', 'DEL', 'OK'],
            ]

        layout = [
            ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P'],
            ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L'],
            ['Aa', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', 'DEL'],
            ['123', 'SPACE', 'OK'],
        ]

        if not self.case_upper:
            return [[k.lower() if len(k) == 1 and k.isalpha() else k for k in row] for row in layout]
        return layout

    def render(self):
        """Draws the compact keyboard panel and calculates precise key hitboxes dynamically."""
        self.update_dimensions()

        fill_rect(self.x, self.y, self.width, self.height, self.COLOR_BG)
        draw_rect(self.x, self.y, self.width, self.height, 0x4208)
        
        self.key_buttons = []
        layout = self._get_layout()
        rows = len(layout)
        
        row_h = (self.height - 10) // rows
        gap = 3

        for r_idx, row in enumerate(layout):
            ry = self.y + 5 + (r_idx * row_h)
            num_keys = len(row)
            
            if r_idx == 3:  # Bottom Row (Modifiers & Space)
                if self.symbol_mode:
                    spacew = self.width - 150 - (gap * 2)
                    self.key_buttons.append({'x': self.x + 5, 'y': ry, 'w': spacew, 'h': row_h - gap, 'label': 'SPACE'})
                    self.key_buttons.append({'x': self.x + 5 + spacew + gap, 'y': ry, 'w': 70, 'h': row_h - gap, 'label': 'DEL'})
                    self.key_buttons.append({'x': self.x + self.width - 75, 'y': ry, 'w': 70, 'h': row_h - gap, 'label': 'OK'})
                else:
                    self.key_buttons.append({'x': self.x + 5, 'y': ry, 'w': 65, 'h': row_h - gap, 'label': '123'})
                    spacew = self.width - 145 - (gap * 2)
                    self.key_buttons.append({'x': self.x + 75, 'y': ry, 'w': spacew, 'h': row_h - gap, 'label': 'SPACE'})
                    self.key_buttons.append({'x': self.x + self.width - 65, 'y': ry, 'w': 60, 'h': row_h - gap, 'label': 'OK'})
            else:
                total_gaps = (num_keys - 1) * gap
                avail_w = self.width - 10 - total_gaps
                k_w = avail_w // num_keys
                
                for k_idx, label in enumerate(row):
                    kw_custom = k_w + 10 if label in ["Aa", "DEL"] else k_w
                    kx = self.x + 5 + (k_idx * (k_w + gap))
                    self.key_buttons.append({'x': kx, 'y': ry, 'w': kw_custom, 'h': row_h - gap, 'label': label})

        for key in self.key_buttons:
            self._draw_key(key, pressed=False)

    def _draw_key(self, key, pressed=False):
        x, y, w, h, label = key['x'], key['y'], key['w'], key['h'], key['label']
        
        fill_col = self.COLOR_PRESS if pressed else (self.COLOR_ACTION if label in ["SPACE", "DEL", "OK", "123", "ABC", "Aa"] else self.COLOR_KEY)
        text_col = 0x0000 if pressed else self.COLOR_TEXT
        
        fill_rect(x, y, w, h, fill_col)
        draw_rect(x, y, w, h, self.COLOR_BORDER)
        
        tx = x + (w // 2) - (len(label) * 4)
        ty = y + (h // 2) - 4
        draw_text(tx, ty, label, text_col, fill_col, font=FONT_8X8)

    def check_touch(self):
        touch = get_touch()
        if not touch:
            return None

        now = time.ticks_ms()
        if self.last_touch_ms and time.ticks_diff(now, self.last_touch_ms) < self.DEBOUNCE_MS:
            return None

        tx, ty = touch

        for key in self.key_buttons:
            if key['x'] <= tx <= key['x'] + key['w'] and key['y'] <= ty <= key['y'] + key['h']:
                self._draw_key(key, pressed=True)
                time.sleep_ms(50)
                self._draw_key(key, pressed=False)
                
                lbl = key['label']
                self.last_touch_ms = now
                
                if lbl == "DEL":
                    if self.buffer:
                        self.buffer = self.buffer[:-1]
                elif lbl == "SPACE":
                    self.buffer += " "
                elif lbl == "OK":
                    if self.ok_action:
                        self.ok_action(self.buffer)
                    return self.buffer
                elif lbl == "Aa":
                    self.case_upper = not self.case_upper
                    self.render()
                elif lbl == "123":
                    self.symbol_mode = True
                    self.render()
                elif lbl == "ABC":
                    self.symbol_mode = False
                    self.render()
                else:
                    self.buffer += lbl
                
                while get_touch() is not None:
                    time.sleep_ms(10)
                    
                return None
        return None

    def get_buffer(self, clear=False):
        out = self.buffer
        if clear:
            self.buffer = ""
        return out

    def clear_buffer(self):
        self.buffer = ""


class UIInputTextBox(UIWidget):
    """Single-line text box fed by a keyboard's .buffer. Tapping it opens the
    keyboard (VirtualKeyboard.open(), or Keyboard.render()). Put it in the list
    you pass to poll(): that also drives the caret blink and redraws on change."""

    def __init__(self, lcd, x, y, w, h, keyboard, fg=WHITE, bg=BLACK, padding=4, blink_ms=500):
        super().__init__(lcd, x, y, w, h)
        self.kb = keyboard
        self.fg = fg
        self.bg = bg
        self.padding = padding
        self.cols = max(1, (self.w - padding * 2) // 8)
        self.focused = False
        self._down = False
        self._last_buf = None
        self._caret_on = True
        self._last_blink = time.ticks_ms()
        self._blink_ms = blink_ms

    def _open_kb(self):
        opener = getattr(self.kb, "open", None)
        if opener:
            opener()
        else:
            self.kb.render()

    def draw(self, force=False):
        if not self.visible:
            return
        buf = self.kb.buffer
        now = time.ticks_ms()
        blink = False
        if self.focused and time.ticks_diff(now, self._last_blink) > self._blink_ms:
            self._caret_on = not self._caret_on
            self._last_blink = now
            blink = True
        if not force and buf == self._last_buf and not blink:
            return
        self._last_buf = buf
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        draw_rect(self.x, self.y, self.w, self.h, BORDER)
        shown = buf[-max(1, self.cols - 1):] if buf else ""
        text = shown + ("|" if (self.focused and self._caret_on) else "")
        if text:
            draw_text(self.x + self.padding, self.y + (self.h - 8) // 2,
                      text, self.fg, self.bg, FONT_8X8)

    def handle_touch(self, pt):
        if not self.visible or not self.enabled:
            return False
        if pt is None:
            self._down = False
            self.draw()
            return False
        if not self.contains(pt[0], pt[1], 0):
            return False
        if not self._down:
            self._down = True
            self.focused = True
            self._open_kb()
            self.draw(force=True)
        return True


class VKTouchCalibrator:
    """VKTouchCalibrator(lcd, vk, samples_per_key=5).run() asks you to tap every
    key on the ABC and 123 pages, averages the touches, prints a
    calibrated_keys = [...] block, and returns it as a list of dicts.
    (The current Keyboard hit-tests real key rectangles, so this is optional.)"""

    def __init__(self, lcd, vk, samples_per_key=5):
        self.lcd = bind_lcd(lcd)
        self.vk = vk
        self.samples = max(1, int(samples_per_key))

    def _msg(self, text):
        fill_rect(0, 0, WIDTH, 16, background)
        draw_text(2, 4, text, WHITE, background, FONT_8X8)

    def _wait_touch(self):
        while True:
            p = get_touch()
            if p:
                return p
            time.sleep_ms(10)

    def _wait_release(self):
        while get_touch() is not None:
            time.sleep_ms(10)

    def _unique_buttons(self):
        seen = set()
        out = []
        for b in self.vk.key_buttons:
            if b["label"] and b["label"] not in seen:
                seen.add(b["label"])
                out.append(b)
        return out

    def _calibrate_layout(self, name):
        results = []
        self._msg("Calibrating {} layout...".format(name))
        time.sleep_ms(400)
        for btn in self._unique_buttons():
            self.vk._draw_key(btn, pressed=True)
            self._msg("Touch '{}' x{}".format(btn["label"], self.samples))
            xs = []
            ys = []
            for _ in range(self.samples):
                p = self._wait_touch()
                xs.append(p[0])
                ys.append(p[1])
                self._wait_release()
                time.sleep_ms(80)
            self.vk._draw_key(btn, pressed=False)
            ax = sum(xs) // len(xs)
            ay = sum(ys) // len(ys)
            print("[VKTouchCalibrator] {} '{}' avg: ({}, {})".format(name, btn["label"], ax, ay))
            results.append({"key": btn["label"], "x": ax, "y": ay})
            time.sleep_ms(150)
        return results

    def run(self):
        vk = self.vk
        if hasattr(vk, "active"):
            vk.active = True
        self._msg("Starting VK calibration...")
        print("[VKTouchCalibrator] Starting calibration...")
        time.sleep_ms(500)

        vk.symbol_mode = False
        vk.case_upper = True
        vk.render()
        res_abc = self._calibrate_layout("ABC")

        vk.symbol_mode = True
        vk.render()
        res_sym = self._calibrate_layout("123/sym")

        final = []
        seen = set()
        for item in res_abc + res_sym:
            if item["key"] not in seen:
                seen.add(item["key"])
                final.append(item)

        print("\n[VKTouchCalibrator] Done. calibrated_keys block:\n")
        print("calibrated_keys = [")
        for it in final:
            print("    {{'key': {!r}, 'x': {}, 'y': {}}},".format(it["key"], it["x"], it["y"]))
        print("]\n")

        vk.symbol_mode = False
        self._msg("Calibration complete.")
        return final


# ---------------------------------------------------------------------
# DialogBox / UIToast / UIScreenAnimator / HTML
# ---------------------------------------------------------------------
class DialogBox:
    """show() blocks until a button is pressed and returns "yes", "no" or None (X).
    Callbacks run AFTER the dialog area has been cleared/redrawn.
    (UIModal is the non-blocking OK/Cancel version.)"""

    def __init__(self, lcd, *, title="Dialog", message="", btn_yes="Yes", btn_no="No",
                 on_yes=None, on_no=None, on_exit=None, w=260, h=140):
        self.lcd = bind_lcd(lcd)
        self.title = title
        self.message = message
        self.btn_yes = btn_yes
        self.btn_no = btn_no
        self.on_yes = on_yes
        self.on_no = on_no
        self.on_exit = on_exit
        self.w = w
        self.h = h
        self._result = None
        self._running = False

    def _yes(self):
        self._result = "yes"
        self._running = False

    def _no(self):
        self._result = "no"
        self._running = False

    def _exit(self):
        self._result = None
        self._running = False

    def show(self):
        if self._running:
            return None
        w = min(self.w, WIDTH - 24)
        h = min(self.h, HEIGHT - 24)
        x = (WIDTH - w) // 2
        y = (HEIGHT - h) // 2
        L = self.lcd
        card = UICard(L, x, y, w, h, title=self.title, bg=SURFACE_ALT, border=WHITE, radius=10)
        bw = (w - 30) // 2
        by = y + h - 42
        btn_x = UIButton(L, x + w - 28, y + 4, 22, 22, "X", action=self._exit,
                         color=ACCENT_BLUE, radius=4)
        btn_y = UIButton(L, x + 10, by, bw, 32, self.btn_yes, action=self._yes, color=ACCENT_BLUE)
        btn_n = UIButton(L, x + 20 + bw, by, bw, 32, self.btn_no, action=self._no, color=DARK_GRAY)
        card.add(btn_x)
        card.add(btn_y)
        card.add(btn_n)
        card.draw()
        if self.message:
            draw_text_in_rect(x + 10, y + 30, w - 20, h - 76, self.message, WHITE, SURFACE_ALT)

        self._result = None
        self._running = True
        try:
            while self._running:
                poll([card])
                time.sleep_ms(10)
        finally:
            self._running = False
            _unreg(card, btn_x, btn_y, btn_n)

        fill_rect(x, y, w, h, background)
        if active_screen is not None:
            active_screen.draw()

        cb = self.on_yes if self._result == "yes" else \
            self.on_no if self._result == "no" else self.on_exit
        if cb:
            cb()
        return self._result


class UIToast:
    """Bottom banner. show() blocks for `duration` seconds by default.
    On hide it calls on_hide(), else redraws active_screen, else paints the
    banner area with the background colour."""

    def __init__(self, lcd, text, duration=2, bg=color565(40, 40, 40), fg=WHITE, on_hide=None):
        self.lcd = bind_lcd(lcd)
        self.text = str(text)
        self.duration = duration
        self.bg = bg
        self.fg = fg
        self.on_hide = on_hide
        self._area = None

    def show(self, blocking=True):
        h = 26
        x = 10
        y = HEIGHT - h - 4
        w = WIDTH - 20
        self._area = (x, y, w, h)
        draw_rounded_rect(x, y, w, h, 6, self.bg, filled=True)
        draw_text(x + 8, y + (h - 8) // 2, self.text, self.fg, self.bg, FONT_8X8)
        if blocking:
            time.sleep(self.duration)
            self.hide()

    def hide(self):
        if self._area is None:
            return
        x, y, w, h = self._area
        self._area = None
        if self.on_hide:
            self.on_hide()
        elif active_screen is not None:
            active_screen.draw()
        else:
            fill_rect(x, y, w, h, background)


class UIScreenAnimator:
    """Timing only: call update() each frame and offset your drawing by .offset_y.
    If `ui` is given, ui.anim_offset_y is kept in sync (old behaviour)."""

    def __init__(self, lcd, ui=None):
        self.lcd = bind_lcd(lcd)
        self.ui = ui
        self.mode = None
        self.t0 = 0
        self.duration = 1
        self.running = False
        self.start_offset = 0
        self.end_offset = 0
        self.offset_y = 0

    def _begin(self, mode, duration, a, b):
        self.mode = mode
        self.duration = max(1, duration)
        self.start_offset = a
        self.end_offset = b
        self.t0 = time.ticks_ms()
        self.running = True

    def open(self, duration=220):
        self._begin("open", duration, 30, 0)

    def close(self, duration=160):
        self._begin("close", duration, 0, 30)

    def boot(self, duration=400):
        self._begin("boot", duration, 50, 0)

    def update(self):
        if not self.running:
            return False
        t = min(1.0, time.ticks_diff(time.ticks_ms(), self.t0) / self.duration)
        e = t ** 3 if self.mode == "close" else 1 - (1 - t) ** 3
        self.offset_y = int(self.start_offset + (self.end_offset - self.start_offset) * e)
        if t >= 1.0:
            self.running = False
            self.offset_y = 0
        if self.ui is not None:
            try:
                self.ui.anim_offset_y = self.offset_y
            except AttributeError:
                pass
        return self.running


class HTML:
    """Tiny viewer: h1 / h2 / p / br, word wrap, no regex.
    h1 is drawn at scale 2. Rendering stops at the bottom of the screen."""
    MARGIN = 6
    SCALE = {"h1": 2, "h2": 1, "p": 1}

    def __init__(self, lcd, title="HTML", on_exit=None, taskbar_height=35):
        self.lcd = bind_lcd(lcd)
        self.win = UIWindow(lcd, title=title, background=WHITE, on_exit=on_exit,
                            taskbar_color=color565(40, 40, 40), taskbar_height=taskbar_height)
        self.styles = {
            "body": {"color": BLACK, "bg": WHITE},
            "p": {"color": BLACK},
            "h1": {"color": BLACK},
        }
        self.cur = "p"
        self.x = self.MARGIN
        self.y = self.MARGIN
        self._full = False

    def open(self, path, animate=True):
        self.win.bg = self.styles["body"]["bg"]
        self.win.start(animate=animate)
        self.x = self.MARGIN
        self.y = self.win.taskbar_height + self.MARGIN
        self.cur = "p"
        self._full = False
        with open(path, "r") as f:
            html = f.read()
        self._parse(html)

    def poll(self):
        return self.win.poll()

    def _scale(self):
        return self.SCALE.get(self.cur, 1)

    def _parse(self, html):
        i = 0
        n = len(html)
        while i < n and not self._full:
            if html[i] == "<":
                j = html.find(">", i)
                if j == -1:
                    break
                self._tag(html[i + 1:j].strip().lower())
                i = j + 1
            else:
                j = html.find("<", i)
                if j == -1:
                    j = n
                self._text(html[i:j])
                i = j

    def _tag(self, tag):
        if tag.startswith("/"):
            self.cur = "p"
            self._newline()
            return
        parts = tag.split()
        name = parts[0].rstrip("/") if parts else ""
        if name in ("h1", "h2", "p"):
            self.cur = name
            self._newline()
        elif name == "br":
            self._newline()

    def _text(self, text):
        if self._full:
            return
        text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        style = self.styles.get(self.cur, self.styles["p"])
        fg = style["color"]
        bg = self.styles["body"]["bg"]
        sc = self._scale()
        for word in text.split(" "):
            if not word:
                continue
            w = (len(word) + 1) * 8 * sc
            if self.x + w >= WIDTH - self.MARGIN and self.x > self.MARGIN:
                self._newline()
                if self._full:
                    return
            draw_text(self.x, self.y, word + " ", fg, bg, FONT_8X8, sc)
            self.x += w

    def _newline(self):
        self.x = self.MARGIN
        self.y += 8 * self._scale() + 2
        if self.y + 8 > HEIGHT:
            self._full = True
