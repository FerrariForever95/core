"""
zeno_gfx.py — UI widget stack rebuilt directly on top of moclcd.

No "ui"/"ui.tft" wrapper object anymore. Every widget talks to the panel
through the module-level functions below (fill_rect, draw_text8x8,
draw_bmp, get_touch, ...), which in turn call moclcd directly.

Defaults to LANDSCAPE (480x320). Call init_display() once at boot; it
sets WIDTH/HEIGHT and runs moclcd.init()/reset()/panel_init() for you.

Touch: moclcd has no touch input, so touch is decoupled via
set_touch_handler(fn) — fn() should return (x, y) or None, same as the
old ui.get_touch(). Wire your touch driver in once at boot:

    import zeno_gfx as gfx
    gfx.init_display()                 # landscape 480x320
    gfx.set_touch_handler(my_touch.read)

Text and images: moclcd has no font/BMP support, so:
  - draw_text8x8() renders through MicroPython's built-in `framebuf`
    8x8 font into a scratch buffer, then blits it (with a byte swap,
    since framebuf packs RGB565 low-byte-first and moclcd wants
    high-byte-first).
  - draw_bmp() is a minimal loader for uncompressed 24-bit BMP files.
"""

import time
import urandom
import math
import framebuf

import moclcd

# -------------------------------------------------------------------
# Display setup / globals
# -------------------------------------------------------------------

WIDTH = 480
HEIGHT = 320

active_screen = None


def init_display(pclk=10_000_000, width=480, height=320, madctl=0x28):
    """Bring up the panel in landscape by default and sync WIDTH/HEIGHT.
    Pass width=320, height=480, madctl=0x48 for portrait instead."""
    global WIDTH, HEIGHT
    WIDTH, HEIGHT = width, height
    moclcd.init(pclk=pclk, width=width, height=height, madctl=madctl)
    moclcd.reset()
    time.sleep_ms(20)
    moclcd.panel_init()


# -------------------------------------------------------------------
# Colors
# -------------------------------------------------------------------

def color565(r, g, b):
    """Return RGB565 color value."""
    return (r & 0xf8) << 8 | (g & 0xfc) << 3 | b >> 3


WHITE      = color565(255, 255, 255)
BLACK      = color565(0, 0, 0)
GRAY       = color565(128, 128, 128)
DARK_GRAY  = color565(60, 60, 60)
LIGHT_GRAY = color565(200, 200, 200)

background = BLACK  # module-wide default background, override with set_background()


def set_background(color):
    global background
    background = color


def log_error(msg):
    print("[UI ERROR]", msg)


def log_warn(msg):
    print("[UI WARN]", msg)


# -------------------------------------------------------------------
# Touch (moclcd has none — plug your driver in here)
# -------------------------------------------------------------------

_touch_fn = None


def set_touch_handler(fn):
    """fn() should return (x, y) or None, exactly like the old ui.get_touch()."""
    global _touch_fn
    _touch_fn = fn


def get_touch():
    if _touch_fn is None:
        return None
    return _touch_fn()


# -------------------------------------------------------------------
# Safe drawing primitives (moclcd.fill_rect()/blit() raise on
# out-of-bounds; these clip silently instead, matching what the old
# ui.tft wrapper did)
# -------------------------------------------------------------------

def fill_rect(x, y, w, h, color):
    if w <= 0 or h <= 0:
        return
    if x < 0:
        w += x
        x = 0
    if y < 0:
        h += y
        y = 0
    if x + w > WIDTH:
        w = WIDTH - x
    if y + h > HEIGHT:
        h = HEIGHT - y
    if w <= 0 or h <= 0 or x >= WIDTH or y >= HEIGHT:
        return
    moclcd.fill_rect(x, y, w, h, color)


def fill_screen(color):
    fill_rect(0, 0, WIDTH, HEIGHT, color)


def clear(color=None):
    fill_rect(0, 0, WIDTH, HEIGHT, background if color is None else color)


def draw_hline(x, y, w, color):
    fill_rect(x, y, w, 1, color)


def draw_vline(x, y, h, color):
    fill_rect(x, y, 1, h, color)


def blit(x, y, w, h, buf):
    """Safe blit: skips (rather than raises) if it doesn't fully fit."""
    if x < 0 or y < 0 or x + w > WIDTH or y + h > HEIGHT:
        return
    moclcd.blit(x, y, w, h, buf)


# draw_circle / fill_circle / draw_line / draw_rect (outline) already
# clip silently inside moclcd itself, so those are used directly as
# moclcd.draw_circle(...), moclcd.fill_circle(...), moclcd.draw_line(...),
# moclcd.draw_rect(...) throughout this file.


def draw_text8x8(x, y, text, fg, bg=None):
    """Render text using MicroPython's built-in framebuf 8x8 font."""
    if not text:
        return
    if bg is None:
        bg = background

    w = len(text) * 8
    h = 8
    buf = bytearray(w * h * 2)
    fb = framebuf.FrameBuffer(buf, w, h, framebuf.RGB565)
    fb.fill(bg)
    fb.text(text, 0, 0, fg)

    # framebuf packs RGB565 low-byte-first; moclcd wants high-byte-first
    for i in range(0, len(buf), 2):
        buf[i], buf[i + 1] = buf[i + 1], buf[i]

    blit(x, y, w, h, buf)


def draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None):
    """Minimal loader: uncompressed 24-bit BMP only (no palette, no RLE)."""
    with open(path, "rb") as f:
        header = f.read(54)
        if header[0:2] != b"BM":
            raise ValueError("not a BMP file")

        data_offset = int.from_bytes(header[10:14], "little")
        bmp_w = int.from_bytes(header[18:22], "little")
        bmp_h_raw = int.from_bytes(header[22:26], "little", signed=True)
        bpp = int.from_bytes(header[28:30], "little")
        compression = int.from_bytes(header[30:34], "little")

        if bpp != 24 or compression != 0:
            raise ValueError("only uncompressed 24-bit BMP is supported")

        top_down = bmp_h_raw < 0
        bmp_h = abs(bmp_h_raw)

        row_size = ((bmp_w * 3 + 3) // 4) * 4  # rows padded to 4 bytes

        out_w = w or bmp_w
        out_h = h or bmp_h
        if max_w:
            out_w = min(out_w, max_w)
        if max_h:
            out_h = min(out_h, max_h)

        buf = bytearray(out_w * out_h * 2)
        row_buf = bytearray(row_size)

        for row in range(out_h):
            src_row = row if top_down else (bmp_h - 1 - row)
            f.seek(data_offset + src_row * row_size)
            f.readinto(row_buf)
            p = row * out_w * 2
            for col in range(out_w):
                b = row_buf[col * 3]
                g = row_buf[col * 3 + 1]
                r = row_buf[col * 3 + 2]
                c = color565(r, g, b)
                buf[p] = c >> 8       # MSB first, matching moclcd
                buf[p + 1] = c & 0xFF
                p += 2

    blit(x, y, out_w, out_h, buf)


# -------------------------------------------------------------------
# Screen transition animations
# -------------------------------------------------------------------

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


# =============================
# UIButton
# =============================
class UIButton:
    def __init__(self, x, y, w, h, label,
                 color=color565(0, 0, 255),
                 text_color=WHITE,
                 margin=5, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = str(label)
        self.color = color
        self.text_color = text_color
        self.margin = margin
        self.action = action

    def draw(self):
        try:
            fill_rect(self.x, self.y, self.w, self.h, self.color)

            char_width = 8
            char_height = 8
            text_width = len(self.label) * char_width
            text_height = char_height

            text_x = self.x + (self.w - text_width) // 2
            text_y = self.y + (self.h - text_height) // 2

            if self.label:
                draw_text8x8(text_x, text_y, self.label, self.text_color, self.color)

        except Exception as e:
            print("BTN DRAW ERROR!")
            print("Label:", self.label)
            print("Pos:", self.x, self.y)
            print("Size:", self.w, self.h)
            print("Error:", e)
            log_error("UIButton draw failed ({}): {}".format(self.label, e))
            try:
                fill_rect(self.x, self.y, self.w, self.h, color565(255, 0, 0))
            except Exception:
                pass

    def get_touch(self):
        p = get_touch()
        if p:
            tx, ty = p
            inside = (
                self.x - self.margin <= tx <= self.x + self.w + self.margin and
                self.y - self.margin <= ty <= self.y + self.h + self.margin
            )
            if inside and self.action:
                self.action()
            return inside
        return False


class UIText:
    def __init__(self, x, y, text, fg=WHITE, bg=None):
        self.x = x
        self.y = y
        self.text = text
        self.fg = fg
        self.bg = bg

    def draw(self):
        draw_text8x8(self.x, self.y, str(self.text), self.fg, self.bg if self.bg else background)


class UIBMPButton:
    def __init__(self, x, y, w, h, bmp, *, bmp_pressed=None, margin=5, action=None):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.bmp = bmp
        self.bmp_pressed = bmp_pressed
        self.margin = margin
        self.action = action
        self._pressed = False

    def draw(self):
        try:
            path = self.bmp_pressed if (self._pressed and self.bmp_pressed) else self.bmp
            draw_bmp(path, self.x, self.y, self.w, self.h)
        except Exception as e:
            print("BMP BUTTON DRAW ERROR")
            print("Pos:", self.x, self.y)
            print("Size:", self.w, self.h)
            print("Error:", e)
            log_error("UIBMPButton draw failed: {}".format(e))

    def get_touch(self):
        p = get_touch()
        if not p:
            self._pressed = False
            return False

        tx, ty = p
        inside = (
            self.x - self.margin <= tx <= self.x + self.w + self.margin and
            self.y - self.margin <= ty <= self.y + self.h + self.margin
        )

        if inside:
            if not self._pressed:
                self._pressed = True
                if self.bmp_pressed:
                    self.draw()
            if self.action:
                self.action()
            return True

        self._pressed = False
        return False


class UIScreen:
    def __init__(self,
                 fg=WHITE,
                 background=None,
                 on_exit=None,
                 taskbarcolor=color565(50, 50, 50),
                 taskbar_text=None,
                 taskbar_text_color=WHITE,
                 taskbar_height=35,
                 *args, **kwargs):

        self.fg = fg
        self.background = background
        self.on_exit = on_exit
        self.exit_args = args
        self.exit_kwargs = kwargs

        self.buttons_enabled = True
        self.exit_box = None

        self.taskbarcolor = taskbarcolor
        self.taskbar_text = taskbar_text
        self.taskbar_text_color = taskbar_text_color
        self.taskbar_height = taskbar_height

    def layer(self, x, y, width, height, color):
        fill_rect(x, y, width, height, color)

    def _draw_background(self):
        if self.background is None:
            return
        if isinstance(self.background, int):
            fill_rect(0, 0, WIDTH, HEIGHT, self.background)
        elif isinstance(self.background, str):
            draw_bmp(self.background, 0, 0, max_w=WIDTH, max_h=HEIGHT)

    def openscreen(self):
        window_open_animation(duration=0.4, fps=60, color=self.background, ease=True)

    def closescreen(self):
        window_close_animation(duration=0.4, fps=60, color=None, ease=True)

    def start(self):
        global active_screen

        window_open_animation(duration=0.4, fps=60, color=self.background, ease=True)

        self._draw_background()

        h = self.taskbar_height
        fill_rect(0, 0, WIDTH, h, self.taskbarcolor)

        btn = 30
        x0 = WIDTH - btn - 2
        y0 = 2
        self.exit_box = (x0, y0, btn, btn)

        fill_rect(x0, y0, btn, btn, color565(0, 0, 200))
        moclcd.draw_line(x0 + 5, y0 + 5, x0 + btn - 5, y0 + btn - 5, WHITE)
        moclcd.draw_line(x0 + btn - 5, y0 + 5, x0 + 5, y0 + btn - 5, WHITE)

        if self.taskbar_text:
            tw = len(self.taskbar_text) * 8
            draw_text8x8((WIDTH - tw) // 2, (h - 8) // 2,
                         self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)

        active_screen = self

    def taskbar(self, taskbarcolor, taskbar_text, taskbar_text_color, taskbar_height=35):
        self.taskbar_text = taskbar_text
        self.taskbarcolor = taskbarcolor
        self.taskbar_text_color = taskbar_text_color
        self.taskbar_height = taskbar_height

        fill_rect(0, 0, WIDTH, self.taskbar_height, self.taskbarcolor)
        if self.taskbar_text:
            text_w = len(self.taskbar_text) * 8
            x_center = (WIDTH - text_w) // 2
            y_center = (self.taskbar_height - 8) // 2
            draw_text8x8(x_center, y_center, self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)

    def draw_gradient(self, color1, color2, angle=0, block_size=1):
        """Linear/diagonal gradient. Supported angles: 0,45,90,135,180,270."""
        w, h = WIDTH, HEIGHT

        def unpack_rgb565(c):
            r = ((c >> 11) & 0x1F) << 3
            g = ((c >> 5) & 0x3F) << 2
            b = (c & 0x1F) << 3
            return r, g, b

        def pack_rgb565(r, g, b):
            r = int(max(0, min(255, r)))
            g = int(max(0, min(255, g)))
            b = int(max(0, min(255, b)))
            return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

        r1, g1, b1 = unpack_rgb565(color1)
        r2, g2, b2 = unpack_rgb565(color2)

        diff = abs((r1 + g1 + b1) - (r2 + g2 + b2)) / 765
        gamma = 2.0 + diff * 0.3

        r1_g, g1_g, b1_g = [(x / 255) ** gamma for x in (r1, g1, b1)]
        r2_g, g2_g, b2_g = [(x / 255) ** gamma for x in (r2, g2, b2)]

        def cubic_blend(a, b, t):
            t2 = t * t
            t3 = t2 * t
            return a + (b - a) * (3 * t2 - 2 * t3)

        if angle in (0, 90, 180, 270):
            vertical = angle in (0, 180)
            steps = h if vertical else w

            for i in range(0, steps, block_size):
                t = i / steps
                if angle in (180, 270):
                    t = 1 - t

                r = pow(cubic_blend(r1_g, r2_g, t), 1 / gamma)
                g = pow(cubic_blend(g1_g, g2_g, t), 1 / gamma)
                b = pow(cubic_blend(b1_g, b2_g, t), 1 / gamma)

                r += (urandom.getrandbits(2) - 2) / 512
                g += (urandom.getrandbits(2) - 2) / 512
                b += (urandom.getrandbits(2) - 2) / 512

                color = pack_rgb565(r * 255, g * 255, b * 255)
                if vertical:
                    fill_rect(0, i, w, block_size, color)
                else:
                    fill_rect(i, 0, block_size, h, color)
            return

        if angle in (45, 135):
            for y in range(0, h, block_size):
                for x in range(0, w, block_size):
                    if angle == 45:
                        t = (x + (h - y)) / (w + h)
                    else:
                        t = (x + y) / (w + h)
                    t = max(0.0, min(1.0, t))

                    r = pow(cubic_blend(r1_g, r2_g, t), 1 / gamma)
                    g = pow(cubic_blend(g1_g, g2_g, t), 1 / gamma)
                    b = pow(cubic_blend(b1_g, b2_g, t), 1 / gamma)

                    r += (urandom.getrandbits(2) - 2) / 512
                    g += (urandom.getrandbits(2) - 2) / 512
                    b += (urandom.getrandbits(2) - 2) / 512

                    color = pack_rgb565(r * 255, g * 255, b * 255)
                    fill_rect(x, y, block_size, block_size, color)
            return

        log_warn("Unsupported gradient angle. Use 0, 45, 90, 135, 180, 270.")

    def start_withoutexit(self):
        global active_screen
        self.exit_box = None

        self._draw_background()

        h = self.taskbar_height
        fill_rect(0, 0, WIDTH, h, self.taskbarcolor)

        if self.taskbar_text:
            tw = len(self.taskbar_text) * 8
            draw_text8x8((WIDTH - tw) // 2, (h - 8) // 2,
                         self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)

        active_screen = self

    def check(self):
        if not self.exit_box or not self.buttons_enabled:
            return False

        p = get_touch()
        if not p:
            return False

        tx, ty = p
        x0, y0, w, h = self.exit_box

        if x0 <= tx <= x0 + w and y0 <= ty <= y0 + h:
            if self.on_exit:
                self.on_exit(*self.exit_args, **self.exit_kwargs)
            return True
        return False


class UITextBoxView:
    def __init__(self, x, y, w, h, text=None, fg=WHITE, bg=BLACK, padding=4):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.fg = fg
        self.bg = bg
        self.padding = padding

        self.char_w = 8
        self.char_h = 8

        self.cols = max(1, (w - padding * 2) // self.char_w)
        self.rows = max(1, (h - padding * 2) // self.char_h)

        self.scroll_px = 0

        self._touch_active = False
        self._y0 = 0
        self._s0_px = 0

        self.enabled = True
        self.lines = []

        if text:
            self.set_text(text)

    def _inside(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def set_text(self, text):
        self.lines = []
        self.scroll_px = 0
        self._touch_active = False

        if not text:
            return

        cols = self.cols
        append = self.lines.append

        for raw in str(text).split("\n"):
            while len(raw) > cols:
                append(raw[:cols])
                raw = raw[cols:]
            if raw:
                append(raw)

    def draw(self):
        fill_rect(self.x, self.y, self.w, self.h, self.bg)

        if not self.enabled or not self.lines:
            return

        total_h = len(self.lines) * self.char_h
        view_h = self.rows * self.char_h
        max_scroll = max(0, total_h - view_h)

        if self.scroll_px < 0:
            self.scroll_px = 0
        elif self.scroll_px > max_scroll:
            self.scroll_px = max_scroll

        first = self.scroll_px // self.char_h
        offset = self.scroll_px % self.char_h

        ty = self.y + self.padding - offset
        x = self.x + self.padding

        end = min(first + self.rows + 1, len(self.lines))

        for i in range(first, end):
            line = self.lines[i]
            if line:
                draw_text8x8(x, ty, line, self.fg, self.bg)
            ty += self.char_h

    def handle_touch(self):
        if not self.enabled or not self.lines:
            return False

        p = get_touch()

        if p and not self._touch_active:
            tx, ty = p
            if not self._inside(tx, ty):
                return False
            self._touch_active = True
            self._y0 = ty
            self._s0_px = self.scroll_px
            return True

        if p and self._touch_active:
            _, ty = p
            self.scroll_px = self._s0_px + (self._y0 - ty)
            self.draw()
            return True

        if not p and self._touch_active:
            self._touch_active = False
            return True

        return False


class DialogBox:
    def __init__(self, *, title="Dialog", message="",
                 btn_yes="Yes", btn_no="No",
                 on_yes=None, on_no=None, on_exit=None):
        self.on_yes = on_yes
        self.on_no = on_no
        self.on_exit = on_exit
        self.btn_yes = btn_yes
        self.btn_no = btn_no
        self.title = title
        self.message = message

        self._result = None
        self._running = False

    def _yes(self):
        self._result = "yes"
        self._running = False
        if self.on_yes:
            self.on_yes()

    def _no(self):
        self._result = "no"
        self._running = False
        if self.on_no:
            self.on_no()

    def _exit(self):
        self._result = None
        self._running = False
        if self.on_exit:
            self.on_exit()

    def show(self):
        if self._running:
            return None

        W = 156
        H = 85
        X = (WIDTH - W) // 2
        Y = (HEIGHT - H) // 2

        self.x, self.y, self.w, self.h = X, Y, W, H

        self.texts = []
        self.buttons = []

        fill_rect(X, Y, W, H, WHITE)
        fill_rect(X, Y, W, 17, color565(80, 80, 80))

        self.texts.append(UIText(X + 7, Y + 6, self.title, fg=WHITE, bg=color565(80, 80, 80)))
        self.texts.append(UIText(X + 10, Y + 39, self.message, fg=BLACK, bg=WHITE))

        self.buttons.append(UIButton(
            X + W - 16, Y + 2, 13, 13, label="X",
            color=color565(0, 0, 255), text_color=WHITE, margin=3, action=self._exit
        ))
        self.buttons.append(UIButton(
            X + 18, Y + H - 28, 54, 21, label=self.btn_yes,
            color=color565(212, 212, 212), text_color=BLACK, margin=5, action=self._yes
        ))
        self.buttons.append(UIButton(
            X + W - 18 - 51, Y + H - 28, 51, 21, label=self.btn_no,
            color=color565(212, 212, 212), text_color=BLACK, margin=5, action=self._no
        ))

        for t in self.texts:
            t.draw()
        for b in self.buttons:
            b.draw()

        self._running = True
        while self._running:
            for b in self.buttons:
                b.get_touch()
            time.sleep(0.02)

        return self._result


class UIToggleSwitch:
    def __init__(self, x, y, w=50, h=26, state=False,
                 on_color=color565(0, 180, 0),
                 off_color=color565(120, 120, 120),
                 knob=WHITE, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.state = state
        self.on_color = on_color
        self.off_color = off_color
        self.knob = knob
        self.action = action

    def draw(self):
        r = self.h // 2
        bg = self.on_color if self.state else self.off_color

        fill_rect(self.x, self.y, self.w, self.h, background)

        moclcd.fill_circle(self.x + r, self.y + r, r, bg)
        moclcd.fill_circle(self.x + self.w - r - 1, self.y + r, r, bg)
        fill_rect(self.x + r, self.y, self.w - 2 * r, self.h, bg)

        kx = self.x + self.w - r - 1 if self.state else self.x + r
        moclcd.fill_circle(kx, self.y + r, r - 2, self.knob)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y <= ty <= self.y + self.h:
            self.state = not self.state
            self.draw()
            if self.action:
                self.action(self.state)
            time.sleep(0.15)
            return True
        return False


class UISlider:
    def __init__(self, x, y, w, min_v=0, max_v=100, value=0,
                 track=color565(80, 80, 80),
                 fill=color565(0, 150, 255),
                 knob=WHITE, action=None):
        self.x, self.y, self.w = x, y, w
        self.h = 10
        self.min = min_v
        self.max = max_v
        self.value = value
        self.track = track
        self.fill = fill
        self.knob = knob
        self.action = action

    def draw(self):
        fill_rect(self.x - 8, self.y - 8, self.w + 16, self.h + 16, background)
        fill_rect(self.x, self.y, self.w, self.h, self.track)

        pos = int((self.value - self.min) * self.w / (self.max - self.min))
        fill_rect(self.x, self.y, pos, self.h, self.fill)

        moclcd.fill_circle(self.x + pos, self.y + self.h // 2, 7, self.knob)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y - 6 <= ty <= self.y + self.h + 6:
            rel = max(0, min(self.w, tx - self.x))
            self.value = self.min + int(rel * (self.max - self.min) / self.w)
            self.draw()
            if self.action:
                self.action(self.value)
            return True
        return False


class UIPanel:
    def __init__(self, x, y, w, h, title=None, bg=None, border=None,
                 title_fg=None, title_bg=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.title = title

        self.bg = bg if bg is not None else color565(30, 30, 30)
        self.border = border if border is not None else color565(80, 80, 80)
        self.title_fg = title_fg if title_fg is not None else color565(200, 200, 200)
        self.title_bg = title_bg if title_bg is not None else self.bg

    def draw(self):
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        moclcd.draw_rect(self.x, self.y, self.w, self.h, self.border)

        if self.title:
            draw_text8x8(self.x + 6, self.y + 6, self.title, self.title_fg, self.title_bg)

    def open(self, steps=8, delay_ms=1):
        tw = self.w
        th = self.h

        for i in range(1, steps + 1):
            cw = (tw * i) // steps
            ch = (th * i) // steps

            fill_rect(self.x, self.y, cw, ch, self.bg)
            moclcd.draw_rect(self.x, self.y, cw, ch, self.border)

            if delay_ms:
                time.sleep_ms(delay_ms)

        self.draw()


class UIProgressBar:
    def __init__(self, x, y, w, h=12, value=0,
                 bg=color565(50, 50, 50), fg=color565(0, 200, 0)):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.value = value
        self.bg = bg
        self.fg = fg

    def set(self, val):
        self.value = max(0, min(100, val))
        self.draw()

    def draw(self):
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        fw = int(self.w * self.value / 100)
        if fw:
            fill_rect(self.x, self.y, fw, self.h, self.fg)


class UIStatusIndicator:
    OK = 0
    WARN = 1
    ERR = 2

    def __init__(self, x, y, r=6, state=0):
        self.x, self.y, self.r = x, y, r
        self.state = state

    def draw(self):
        if self.state == self.OK:
            c = color565(0, 200, 0)
        elif self.state == self.WARN:
            c = color565(255, 165, 0)
        else:
            c = color565(200, 0, 0)
        moclcd.fill_circle(self.x, self.y, self.r, c)


class UIToast:
    def __init__(self, text, duration=2):
        self.text = text
        self.duration = duration

    def show(self):
        h = 26
        y = HEIGHT - h - 4
        fill_rect(10, y, WIDTH - 20, h, color565(40, 40, 40))
        draw_text8x8(16, y + 9, self.text, WHITE)
        time.sleep(self.duration)
        clear()


class UIListView:
    def __init__(self, x, y, w, h, items, item_h=24,
                 bg=color565(20, 20, 20), fg=WHITE,
                 sel=color565(0, 120, 255), text_x=6,
                 highlight=False, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.items = items
        self.item_h = item_h
        self.bg = bg
        self.fg = fg
        self.sel = sel
        self.text_x = text_x
        self.highlight = highlight
        self.action = action

        self.scroll = 0
        self.selected = -1
        self.enabled = True

        self._y0 = None
        self._s0 = 0
        self._moved = False

    def _inside(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def draw(self):
        fill_rect(self.x, self.y, self.w, self.h, self.bg)

        if not self.enabled:
            return

        content_h = len(self.items) * self.item_h
        max_scroll = max(0, content_h - self.h)

        if self.scroll < 0:
            self.scroll = 0
        elif self.scroll > max_scroll:
            self.scroll = max_scroll

        first = self.scroll // self.item_h
        offset = self.scroll % self.item_h
        visible = (self.h + self.item_h - 1) // self.item_h

        for i in range(visible):
            idx = first + i
            if idx >= len(self.items):
                break

            iy = self.y + i * self.item_h - offset
            if iy + self.item_h <= self.y or iy >= self.y + self.h:
                continue

            row_bg = self.sel if (self.highlight and idx == self.selected) else self.bg
            fill_rect(self.x, iy, self.w, self.item_h, row_bg)

            ty = iy + (self.item_h - 8) // 2
            if self.y <= ty < self.y + self.h:
                draw_text8x8(self.x + self.text_x, ty, str(self.items[idx]), self.fg, row_bg)

    def handle_touch(self):
        if not self.enabled:
            return False

        p = get_touch()

        if p and self._y0 is None:
            tx, ty = p
            if not self._inside(tx, ty):
                return False
            self._y0 = ty
            self._s0 = self.scroll
            self._moved = False
            return True

        if p and self._y0 is not None:
            _, ty = p
            dy = self._y0 - ty
            if abs(dy) > 6:
                self._moved = True
            self.scroll = self._s0 + dy
            self.draw()
            return True

        if not p and self._y0 is not None:
            ty0 = self._y0
            moved = self._moved
            self._y0 = None

            if not moved:
                rel_y = ty0 - self.y
                if 0 <= rel_y < self.h:
                    idx = (self.scroll + rel_y) // self.item_h
                    if 0 <= idx < len(self.items):
                        self.selected = idx
                        self.draw()
                        if self.action:
                            self.action(idx, self.items[idx])
            return True

        return False


class UIInputTextBox:
    def __init__(self, x, y, w, h, keyboard, fg, bg, padding=4, blink_ms=500):
        self.kb = keyboard

        self.x, self.y, self.w, self.h = x, y, w, h
        self.fg = fg
        self.bg = bg
        self.padding = padding

        self.char_w = 8
        self.char_h = 8
        self.cols = max(1, (w - padding * 2) // self.char_w)

        self._last_buf = None
        self._caret_on = True
        self._last_blink = time.ticks_ms()
        self._blink_ms = blink_ms

        self.enabled = True

        fill_rect(self.x, self.y, self.w, self.h, self.bg)

    def _inside(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def draw(self, force=False):
        buf = self.kb.buffer

        if not buf:
            return

        now = time.ticks_ms()
        blink = False

        if time.ticks_diff(now, self._last_blink) > self._blink_ms:
            self._caret_on = not self._caret_on
            self._last_blink = now
            blink = True

        if not force and buf == self._last_buf and not blink:
            return

        self._last_buf = buf

        fill_rect(self.x, self.y, self.w, self.h, self.bg)

        visible = buf[-self.cols:]
        text = visible + ("|" if self._caret_on else "")

        if not text.strip("|"):
            return

        draw_text8x8(self.x + self.padding, self.y + (self.h - self.char_h) // 2,
                     text, self.fg, self.bg)

    def handle_touch(self):
        if not self.enabled:
            return False

        p = get_touch()
        if not p:
            return False

        tx, ty = p
        if not self._inside(tx, ty):
            return False

        self.kb.open()
        return True


class UIIconButton:
    def __init__(self, x, y, w, h, label, bg=color565(60, 60, 60), fg=WHITE, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = label
        self.bg = bg
        self.fg = fg
        self.action = action

    def draw(self):
        fill_rect(self.x, self.y, self.w, self.h, self.bg)
        tw = len(self.label) * 8
        draw_text8x8(self.x + (self.w - tw) // 2, self.y + (self.h - 8) // 2,
                     self.label, self.fg, self.bg)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y <= ty <= self.y + self.h:
            if self.action:
                self.action()
            time.sleep(0.15)
            return True
        return False


class UICheckBox:
    def __init__(self, x, y, label, checked=False, action=None):
        self.x, self.y = x, y
        self.label = label
        self.checked = checked
        self.action = action
        self.size = 18

    def draw(self):
        fill_rect(self.x, self.y, self.size, self.size, background)
        moclcd.draw_rect(self.x, self.y, self.size, self.size, color565(200, 200, 200))
        if self.checked:
            moclcd.draw_line(self.x + 3, self.y + 9, self.x + 7, self.y + 14, color565(0, 200, 0))
            moclcd.draw_line(self.x + 7, self.y + 14, self.x + 15, self.y + 3, color565(0, 200, 0))
        draw_text8x8(self.x + self.size + 6, self.y + 5, self.label, WHITE, background)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.size and self.y <= ty <= self.y + self.size:
            self.checked = not self.checked
            self.draw()
            if self.action:
                self.action(self.checked)
            time.sleep(0.15)
            return True
        return False


class UIRadioGroup:
    def __init__(self, x, y, options, selected=0, action=None):
        self.x, self.y = x, y
        self.options = options
        self.selected = selected
        self.action = action
        self.r = 6
        self.spacing = 22

    def draw(self):
        for i, opt in enumerate(self.options):
            cy = self.y + i * self.spacing
            moclcd.draw_circle(self.x, cy, self.r, color565(200, 200, 200))
            if i == self.selected:
                moclcd.fill_circle(self.x, cy, self.r - 2, color565(0, 180, 255))
            draw_text8x8(self.x + 14, cy - 4, opt, WHITE, background)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        for i in range(len(self.options)):
            cy = self.y + i * self.spacing
            if abs(tx - self.x) <= self.r + 4 and abs(ty - cy) <= self.r + 4:
                self.selected = i
                self.draw()
                if self.action:
                    self.action(i, self.options[i])
                time.sleep(0.15)
                return True
        return False


class UITabBar:
    def __init__(self, x, y, w, h, tabs, active=0, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.tabs = tabs
        self.active = active
        self.action = action

    def draw(self):
        tabw = self.w // len(self.tabs)
        for i, t in enumerate(self.tabs):
            bg = color565(0, 120, 255) if i == self.active else color565(80, 80, 80)
            fill_rect(self.x + i * tabw, self.y, tabw, self.h, bg)
            tw = len(t) * 8
            draw_text8x8(self.x + i * tabw + (tabw - tw) // 2,
                         self.y + (self.h - 8) // 2, t, WHITE, bg)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y <= ty <= self.y + self.h:
            idx = (tx - self.x) // (self.w // len(self.tabs))
            if idx != self.active and idx < len(self.tabs):
                self.active = idx
                self.draw()
                if self.action:
                    self.action(idx, self.tabs[idx])
                time.sleep(0.15)
            return True
        return False


class UIStepper:
    def __init__(self, x, y, value=0, step=1, action=None):
        self.x, self.y = x, y
        self.value = value
        self.step = step
        self.action = action

    def draw(self):
        fill_rect(self.x, self.y, 80, 24, color565(50, 50, 50))
        draw_text8x8(self.x + 6, self.y + 8, "-", WHITE, color565(50, 50, 50))
        draw_text8x8(self.x + 32, self.y + 8, str(self.value), WHITE, color565(50, 50, 50))
        draw_text8x8(self.x + 64, self.y + 8, "+", WHITE, color565(50, 50, 50))

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + 24 and self.y <= ty <= self.y + 24:
            self.value -= self.step
        elif self.x + 56 <= tx <= self.x + 80 and self.y <= ty <= self.y + 24:
            self.value += self.step
        else:
            return False
        self.draw()
        if self.action:
            self.action(self.value)
        time.sleep(0.15)
        return True


class UIDivider:
    def __init__(self, x, y, w, color=color565(100, 100, 100)):
        self.x, self.y, self.w = x, y, w
        self.color = color

    def draw(self):
        draw_hline(self.x, self.y, self.w, self.color)


class UIScreenAnimator:
    """Vertical offset controller for screen transitions. No redraw logic
    here — read .offset_y each frame and apply it yourself."""

    def __init__(self):
        self.mode = None
        self.t0 = 0
        self.duration = 0
        self.running = False
        self.start_offset = 0
        self.end_offset = 0
        self.offset_y = 0

    def _now(self):
        return time.ticks_ms()

    def _progress(self):
        return min(1.0, time.ticks_diff(self._now(), self.t0) / self.duration)

    def _ease_out(self, t):
        return 1 - (1 - t) ** 3

    def _ease_in(self, t):
        return t ** 3

    def open(self, duration=220):
        self.mode = "open"
        self.duration = duration
        self.start_offset = 30
        self.end_offset = 0
        self.t0 = self._now()
        self.running = True

    def close(self, duration=160):
        self.mode = "close"
        self.duration = duration
        self.start_offset = 0
        self.end_offset = 30
        self.t0 = self._now()
        self.running = True

    def boot(self, duration=400):
        self.mode = "boot"
        self.duration = duration
        self.start_offset = 50
        self.end_offset = 0
        self.t0 = self._now()
        self.running = True

    def update(self):
        if not self.running:
            return False

        t = self._progress()

        if self.mode == "open":
            e = self._ease_out(t)
        elif self.mode == "close":
            e = self._ease_in(t)
        else:
            e = self._ease_out(t)

        self.offset_y = int(self.start_offset + (self.end_offset - self.start_offset) * e)

        if t >= 1.0:
            self.running = False
            self.offset_y = 0
            return False

        return True


class VirtualKeyboard:
    """NOTE: calibrated_keys below were tuned for a different screen
    resolution/orientation. After switching to landscape, re-run
    VKTouchCalibrator to get fresh coordinates before relying on touch."""

    TOUCH_RADIUS = 12
    DEBOUNCE_MS = 200

    calibrated_keys = [
        {'key': 'Q', 'x': 0, 'y': 132},
        {'key': 'W', 'x': 21, 'y': 136},
        {'key': 'E', 'x': 54, 'y': 136},
        {'key': 'R', 'x': 91, 'y': 136},
        {'key': 'T', 'x': 126, 'y': 135},
        {'key': 'Y', 'x': 161, 'y': 133},
        {'key': 'U', 'x': 197, 'y': 134},
        {'key': 'I', 'x': 236, 'y': 136},
        {'key': 'O', 'x': 271, 'y': 135},
        {'key': 'P', 'x': 303, 'y': 135},
        {'key': 'A', 'x': 0, 'y': 169},
        {'key': 'S', 'x': 25, 'y': 170},
        {'key': 'D', 'x': 62, 'y': 171},
        {'key': 'F', 'x': 102, 'y': 171},
        {'key': 'G', 'x': 142, 'y': 171},
        {'key': 'H', 'x': 180, 'y': 171},
        {'key': 'J', 'x': 218, 'y': 173},
        {'key': 'K', 'x': 257, 'y': 171},
        {'key': 'L', 'x': 295, 'y': 171},
        {'key': 'Aa', 'x': 0, 'y': 205},
        {'key': 'Z', 'x': 28, 'y': 206},
        {'key': 'X', 'x': 64, 'y': 207},
        {'key': 'C', 'x': 102, 'y': 205},
        {'key': 'V', 'x': 140, 'y': 208},
        {'key': 'B', 'x': 178, 'y': 208},
        {'key': 'N', 'x': 217, 'y': 207},
        {'key': 'M', 'x': 257, 'y': 208},
        {'key': 'DEL', 'x': 292, 'y': 208},
        {'key': '123', 'x': 29, 'y': 238},
        {'key': 'SPACE', 'x': 147, 'y': 238},
        {'key': 'OK', 'x': 256, 'y': 238},
        {'key': '1', 'x': 0, 'y': 132},
        {'key': '2', 'x': 20, 'y': 132},
        {'key': '3', 'x': 53, 'y': 134},
        {'key': '4', 'x': 88, 'y': 134},
        {'key': '5', 'x': 126, 'y': 134},
        {'key': '6', 'x': 161, 'y': 132},
        {'key': '7', 'x': 196, 'y': 133},
        {'key': '8', 'x': 236, 'y': 131},
        {'key': '9', 'x': 269, 'y': 134},
        {'key': '0', 'x': 304, 'y': 132},
        {'key': '-', 'x': 0, 'y': 166},
        {'key': '/', 'x': 25, 'y': 168},
        {'key': '\\', 'x': 59, 'y': 169},
        {'key': ':', 'x': 91, 'y': 169},
        {'key': ';', 'x': 123, 'y': 169},
        {'key': '(', 'x': 164, 'y': 170},
        {'key': ')', 'x': 198, 'y': 172},
        {'key': '_', 'x': 233, 'y': 173},
        {'key': '|', 'x': 270, 'y': 167},
        {'key': '+', 'x': 303, 'y': 171},
        {'key': '.', 'x': 3, 'y': 205},
        {'key': '@', 'x': 48, 'y': 203},
        {'key': '#', 'x': 80, 'y': 203},
        {'key': '$', 'x': 134, 'y': 204},
        {'key': '&', 'x': 194, 'y': 206},
        {'key': '%', 'x': 243, 'y': 204},
        {'key': 'ABC', 'x': 289, 'y': 207},
    ]

    def __init__(self, width=None, height=120,
                 text_color=None, key_color=None, border_color=None,
                 bg_color=None, ok_action=None):

        self.width = width or WIDTH
        self.height = height

        self.text_color = text_color or WHITE
        self.key_color = key_color or GRAY
        self.border_color = border_color or DARK_GRAY
        self.bg_color = bg_color or GRAY

        self.buffer = ""
        self.case_upper = False
        self.symbol_mode = False
        self.ok_action = ok_action

        self.x = 0
        self.y = HEIGHT - height

        self.key_buttons = []
        self.last_touch_ms = 0

        self.active = True

        self._draw_keys()

    def close(self):
        self.active = False
        fill_rect(self.x, self.y, self.width, self.height, BLACK)

    def open(self):
        self.active = True
        self._draw_keys()

    def _canon(self, label):
        if len(label) == 1 and label.isalpha():
            return label.upper()
        return label

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
            lower = []
            for row in layout:
                new_row = []
                for k in row:
                    new_row.append(k.lower() if (len(k) == 1 and k.isalpha()) else k)
                lower.append(new_row)
            return lower

        return layout

    def _draw_keys(self):
        fill_rect(self.x, self.y, self.width, self.height, self.bg_color)
        self.key_buttons = []

        layout = self._get_layout()
        rows = len(layout)
        rh = self.height // rows

        for r, row in enumerate(layout):
            cols = len(row)
            kw = self.width // cols

            for c, label in enumerate(row):
                kx = self.x + c * kw
                ky = self.y + r * rh
                w, h = kw - 2, rh - 2
                self.key_buttons.append({'x': kx, 'y': ky, 'w': w, 'h': h, 'label': label})
                self._draw_key(kx, ky, w, h, label)

    def _draw_key(self, x, y, w, h, label):
        fill_rect(x, y, w, h, self.key_color)
        moclcd.draw_rect(x, y, w, h, self.border_color)
        tx = x + (w // 2) - (len(label) * 4)
        ty = y + (h // 2) - 4
        draw_text8x8(tx, ty, label, self.text_color, self.key_color)

    def _highlight_key(self, key_btn):
        x, y, w, h = key_btn['x'], key_btn['y'], key_btn['w'], key_btn['h']
        fill_rect(x, y, w, h, LIGHT_GRAY)
        time.sleep(0.05)
        self._draw_key(x, y, w, h, key_btn['label'])

    def check_touch(self):
        if not self.active:
            return

        touch = get_touch()
        if not touch:
            return

        now = time.ticks_ms()
        if self.last_touch_ms and time.ticks_diff(now, self.last_touch_ms) < self.DEBOUNCE_MS:
            return

        tx, ty = touch
        layout = self._get_layout()

        active_canon = []
        for r in layout:
            for lbl in r:
                c = self._canon(lbl)
                if c not in active_canon:
                    active_canon.append(c)

        best = None
        best_d2 = 999999

        for entry in self.calibrated_keys:
            if self._canon(entry['key']) not in active_canon:
                continue
            dx = tx - entry['x']
            dy = ty - entry['y']
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best = entry

        if not best or best_d2 > (self.TOUCH_RADIUS * self.TOUCH_RADIUS):
            return

        target = self._canon(best['key'])
        key_btn = None
        logical_label = best['key']

        for btn in self.key_buttons:
            if self._canon(btn['label']) == target:
                key_btn = btn
                logical_label = btn['label']
                break

        self._key_pressed(logical_label, key_btn)
        self.last_touch_ms = now

    def _key_pressed(self, k, key_btn=None):
        if key_btn:
            self._highlight_key(key_btn)

        if k == "DEL":
            if self.buffer:
                self.buffer = self.buffer[:-1]
        elif k == "SPACE":
            self.buffer += " "
        elif k == "OK":
            if self.ok_action:
                self.ok_action(self.buffer)
        elif k == "Aa":
            self.case_upper = not self.case_upper
            self._draw_keys()
        elif k == "123":
            self.symbol_mode = True
            self._draw_keys()
        elif k == "ABC":
            self.symbol_mode = False
            self._draw_keys()
        else:
            if len(k) == 1:
                if k.isalpha():
                    self.buffer += k.upper() if self.case_upper else k.lower()
                else:
                    self.buffer += k

    def get_buffer(self, clear=False):
        out = self.buffer
        if clear:
            self.buffer = ""
        return out

    def clear_buffer(self):
        self.buffer = ""


class VKTouchCalibrator:
    """Touch calibrator for VirtualKeyboard. Run this after switching
    resolution/orientation — the shipped calibrated_keys won't match."""

    def __init__(self, vk, samples_per_key=5):
        self.vk = vk
        self.samples_per_key = samples_per_key

        self.bg = background
        self.highlight = LIGHT_GRAY
        self.border = WHITE
        self.text = WHITE

    def _msg(self, text):
        fill_rect(0, 0, WIDTH, 16, self.bg)
        draw_text8x8(2, 4, text, self.text, self.bg)

    def _wait_for_touch(self):
        while True:
            p = get_touch()
            if p:
                return p
            time.sleep_ms(10)

    def _wait_for_release(self):
        while True:
            p = get_touch()
            if not p:
                return
            time.sleep_ms(10)

    def _highlight_button(self, btn):
        x, y, w, h = btn["x"], btn["y"], btn["w"], btn["h"]
        fill_rect(x, y, w, h, self.highlight)
        moclcd.draw_rect(x, y, w, h, self.border)

    def _redraw_button(self, btn):
        self.vk._draw_key(btn["x"], btn["y"], btn["w"], btn["h"], btn["label"])

    def _find_button_by_label(self, label):
        for btn in self.vk.key_buttons:
            if btn["label"] == label:
                return btn
        return None

    def _collect_unique_labels(self):
        labels = []
        seen = set()
        for btn in self.vk.key_buttons:
            lab = btn["label"]
            if not lab:
                continue
            if lab not in seen:
                seen.add(lab)
                labels.append(lab)
        return labels

    def _calibrate_current_layout(self, layout_name):
        results = []

        labels = self._collect_unique_labels()
        self._msg("Calibrating {} layout...".format(layout_name))
        time.sleep_ms(400)

        for key in labels:
            btn = self._find_button_by_label(key)
            if not btn:
                continue

            self._highlight_button(btn)
            self._msg("Touch '{}' {}x".format(key, self.samples_per_key))

            xs = []
            ys = []

            for i in range(self.samples_per_key):
                p = self._wait_for_touch()
                xs.append(p[0])
                ys.append(p[1])

                self._wait_for_release()
                time.sleep_ms(80)

            self._redraw_button(btn)

            avg_x = sum(xs) // len(xs)
            avg_y = sum(ys) // len(ys)

            print("[VKTouchCalibrator] {} '{}' avg: ({}, {})".format(layout_name, key, avg_x, avg_y))

            results.append({"key": key, "x": avg_x, "y": avg_y})
            time.sleep_ms(150)

        return results

    def run(self):
        fill_rect(0, 0, WIDTH, 16, self.bg)

        print("[VKTouchCalibrator] Starting calibration...")
        self._msg("Starting VK calibration...")
        time.sleep_ms(500)

        self.vk.symbol_mode = False
        self.vk.case_upper = True
        self.vk._draw_keys()
        res_abc = self._calibrate_current_layout("ABC")

        self.vk.symbol_mode = True
        self.vk.case_upper = True
        self.vk._draw_keys()
        res_sym = self._calibrate_current_layout("123/sym")

        all_res = res_abc + res_sym

        final = []
        seen = set()
        for item in all_res:
            k = item["key"]
            if k in seen:
                continue
            seen.add(k)
            final.append(item)

        print("\n[VKTouchCalibrator] Done. Full calibrated_keys block:\n")
        print("calibrated_keys = [")
        for item in final:
            print("    {{'key': {!r}, 'x': {}, 'y': {}}},".format(item["key"], item["x"], item["y"]))
        print("]\n")
        print("[VKTouchCalibrator] Paste into VirtualKeyboard.calibrated_keys if you want to use it")

        self._msg("Calibration complete.")
        return final


class HTML:
    TASKBAR_H = 35
    MARGIN = 6

    FONT_MAP = {'h1': 8, 'h2': 8, 'p': 8}

    def __init__(self, title="HTML"):
        self.screen = UIScreen(taskbar_text=title, taskbarcolor=color565(40, 40, 40))

        self.styles = {
            'body': {'color': BLACK, 'bg': WHITE},
            'p': {'color': BLACK},
            'h1': {'color': BLACK},
        }

        self.x = self.MARGIN
        self.y = self.TASKBAR_H + self.MARGIN
        self.cur = 'p'

    def open(self, path):
        self.screen.start()
        self._clear_body()
        html = self._read(path)
        self._parse(html)

    def _read(self, path):
        with open(path, "r") as f:
            return f.read()

    def _clear_body(self):
        bg = self.styles['body']['bg']
        fill_rect(0, self.TASKBAR_H, WIDTH, HEIGHT - self.TASKBAR_H, bg)

    def _parse(self, html):
        i = 0
        ln = len(html)

        while i < ln:
            if html[i] == "<":
                j = html.find(">", i)
                if j == -1:
                    break
                tag = html[i + 1:j].strip().lower()
                self._tag(tag)
                i = j + 1
            else:
                j = html.find("<", i)
                if j == -1:
                    j = ln
                text = html[i:j]
                self._text(text)
                i = j

    def _tag(self, tag):
        if tag.startswith("/"):
            self.cur = 'p'
            self._newline()
            return

        if tag == "h1":
            self.cur = "h1"
            self._newline()
        elif tag == "p":
            self.cur = "p"
            self._newline()
        elif tag == "br":
            self._newline()

    def _text(self, text):
        if not text.strip():
            return

        fg = self.styles[self.cur]['color']
        bg = self.styles['body']['bg']

        for word in text.split(" "):
            w = (len(word) + 1) * 8
            if self.x + w >= WIDTH - self.MARGIN:
                self._newline()

            draw_text8x8(self.x, self.y, word + " ", fg, bg)
            self.x += w

    def _newline(self):
        self.x = self.MARGIN
        self.y += 10
        if self.y >= HEIGHT - 10:
            self.y = self.TASKBAR_H + self.MARGIN


class IOSSlider:
    def __init__(self, x, y, w, min_v=0, max_v=100, value=0,
                 track_border=color565(40, 40, 40),
                 track_fill=color565(90, 90, 90),
                 knob=color565(220, 220, 220),
                 action=None):

        self.x, self.y, self.w = x, y, w
        self.h = 18
        self.knob_size = 16

        self.min = min_v
        self.max = max_v
        self.value = float(value)

        self.track_border = track_border
        self.track_fill = track_fill
        self.knob_color = knob
        self.action = action

        self.dragging = False
        self.drag_offset = 0

        self._last_kx = None

    def draw(self):
        fill_rect(self.x - 2, self.y - 2, self.w + 4, self.h + 4, background)

        moclcd.draw_rect(self.x, self.y, self.w, self.h, self.track_border)
        fill_rect(self.x + 1, self.y + 1, self.w - 2, self.h - 2, self.track_fill)

        draw_hline(self.x + 1, self.y + 1, self.w - 2, color565(140, 140, 140))

        travel = self.w - self.knob_size
        kx = self.x + int((self.value - self.min) / (self.max - self.min) * travel)

        self._draw_knob(kx)
        self._last_kx = kx

    def _draw_knob(self, kx):
        ky = self.y + 1

        fill_rect(kx + 1, ky + 1, self.knob_size, self.knob_size, BLACK)
        fill_rect(kx, ky, self.knob_size, self.knob_size, self.knob_color)
        draw_hline(kx + 1, ky + 1, self.knob_size - 2, WHITE)

    def _erase_knob(self, kx):
        fill_rect(kx, self.y + 1, self.knob_size + 2, self.knob_size + 2, self.track_fill)
        draw_hline(kx, self.y + 1, self.knob_size + 2, color565(140, 140, 140))

    def handle_touch(self):
        p = get_touch()

        if not p:
            self.dragging = False
            return False

        tx, ty = p

        travel = self.w - self.knob_size
        ky = self.y + 1

        if self._last_kx is None:
            return False

        if not self.dragging:
            if (self._last_kx <= tx <= self._last_kx + self.knob_size and
                    ky <= ty <= ky + self.knob_size):
                self.dragging = True
                self.drag_offset = tx - self._last_kx
            else:
                return False

        new_x = tx - self.drag_offset
        new_x = max(self.x, min(self.x + travel, new_x))

        if new_x == self._last_kx:
            return True

        self._erase_knob(self._last_kx)

        ratio = (new_x - self.x) / travel
        self.value = self.min + ratio * (self.max - self.min)

        self._draw_knob(new_x)
        self._last_kx = new_x

        if self.action:
            self.action(self.value)

        return True
