"""
zeno_gfx.py — UI widget stack built directly on top of moclcd.

Every widget talks to the panel through the module-level functions below
(fill_rect, draw_text8x8, draw_bmp, get_touch, ...), which in turn call
moclcd directly. No extra wrapper object required.

Defaults to LANDSCAPE (480x320). Call init_display() once at boot; it
sets WIDTH/HEIGHT and runs moclcd.init()/reset()/panel_init() for you.

Touch: moclcd has no touch input, so touch is decoupled via
set_touch_handler(fn) — fn() should return (x, y) or None.

    import zeno_gfx as gfx
    gfx.init_display()                 # landscape 480x320
    gfx.set_touch_handler(my_touch.read)

Text and images: moclcd.c implements both natively in C (font_petme128_8x8
for text, a minimal uncompressed-24-bit-BMP loader for images), each
sent to the panel as DMA transfers. draw_text8x8() and draw_bmp() here
are thin wrappers over moclcd.draw_text8x8()/moclcd.draw_bmp() — no
Python-side pixel loops or framebuf scratch buffers involved.
  - draw_text8x8(x, y, text, fg, bg=None) draws opaque by default
    (bg falls back to the module `background`); pass bg=None via
    draw_text8x8_transparent() for the native per-pixel transparent mode.
  - draw_bmp() forwards straight to moclcd.draw_bmp(); only uncompressed
    24-bit BMP is supported (raises ValueError otherwise).
  - draw_logo() wraps draw_bmp() for a common "brand mark" use case
    (centering, scaling to fit a box, optional caption underneath).

Notifications: NotificationBadge draws a small dot/count marker that
can be pinned to the corner of any widget (button, icon, tab, avatar)
to indicate unread/pending state — the classic "red dot" pattern.
Toast() gives a non-blocking, stackable banner for transient messages;
call Toast.update() once per frame from your main loop.

Framebuffer readback: moclcd is a write-only DMA panel with no
readback bus, so reading "what's currently on screen" is backed by an
opt-in shadow copy moclcd.c keeps in sync with every draw call.
  - enable_framebuffer_mirror() turns mirroring on (~300KB RAM at
    480x320); call once near startup if you'll need this at all.
  - read_framebuffer(dest=None, x=0, y=0, w=None, h=None) forwards the
    current frame (or a sub-rect) into `dest` -- your own pre-allocated
    buffer/variable -- or allocates and returns one if dest is None.
  - screenshot_to_file(path, ...) is read_framebuffer() + a raw write.

Faster screen animations: window_open_animation()/window_close_
animation() and their content-aware _live() "genie" counterparts now
batch each frame's rects through moclcd.blit_fast() (one C call per
frame instead of one Python->C call per rect), so opening/closing
screens is noticeably snappier, especially for busy screens with many
widgets. replay_ops_fast() is the batched version of replay_ops() used
internally by the _live() variants.

Multi-font text: moclcd's built-in font (id 0) is the fixed 8x8
font_petme128_8x8 table. register_font(font_id, glyph_data, char_w,
char_h, first_char, last_char) registers an additional font (ids 1-7)
from a column-major glyph table (same byte layout as
font_petme128_8x8), and draw_text(x, y, text, fg, bg=None, font=font_id)
draws with it -- useful for a bigger/bold heading font alongside the
default body text.

Filesystem note for draw_bmp()/draw_logo(): these go through moclcd's
native BMP loader, which opens files via fopen() -> MicroPython's VFS,
same as Python's own open(). Calling them before the filesystem is
mounted (os.mount(), or the board's default flash mount) will raise —
moclcd now detects that case and explains it directly instead of a
bare "file not found". See init_display()'s docstring for details.
"""

import time
import urandom
import array
import moclcd

# ---------------------------------------------------------------------
# Display setup / globals
# ---------------------------------------------------------------------

WIDTH = 480
HEIGHT = 320

active_screen = None


def brightness(value):
    moclcd.backlight_set(value)


def init_display(pclk=20_000_000, width=480, height=320, madctl=0x28):
    """Bring up the panel in landscape by default and sync WIDTH/HEIGHT.
    Pass width=320, height=480, madctl=0x48 for portrait instead.

    NOTE on draw_bmp()/draw_logo(): those go through moclcd's native
    BMP loader, which opens files via C's fopen() -> MicroPython's VFS.
    That means the filesystem must already be mounted (os.mount(), or
    the board's default flash mount) before any draw_bmp() call, or
    every path will look like it doesn't exist. init_display() itself
    doesn't touch the filesystem, so it's always safe to call from
    boot.py; just don't call draw_bmp()/draw_logo() until after the FS
    is mounted (normally that just means "not before main.py runs").
    """
    global WIDTH, HEIGHT
    WIDTH, HEIGHT = width, height
    moclcd.backlight(False)
    moclcd.init(pclk=pclk, width=width, height=height, madctl=madctl)
    moclcd.backlight(True)
    moclcd.reset()
    time.sleep_ms(20)
    moclcd.panel_init()


# ---------------------------------------------------------------------
# Framebuffer readback
#
# moclcd is a write-only DMA panel with no readback bus, so "reading
# the current frame" is backed by an opt-in shadow copy that moclcd.c
# keeps in sync with every draw call (see mirror_enable() in
# modlcd.c). Turn it on once (costs ~300KB RAM at 480x320) if your app
# needs to grab pixels back -- e.g. for a screenshot-to-file feature,
# a "restore what was here" pattern for popups/toasts, or feeding a
# captured region back into blit() elsewhere on screen.
# ---------------------------------------------------------------------

_mirroring = False


def enable_framebuffer_mirror():
    """Start mirroring every draw call into an internal shadow
    framebuffer so read_framebuffer() has something to read. Call this
    once near startup (after init_display()) if you'll need frame
    capture at all -- it costs extra RAM and a little CPU per draw
    call, so it's off by default."""
    global _mirroring
    moclcd.mirror_enable()
    _mirroring = True


def disable_framebuffer_mirror(free_memory=False):
    """Stop mirroring. Pass free_memory=True to also release the
    shadow buffer's RAM (mirror_free()) instead of just pausing it."""
    global _mirroring
    if free_memory:
        moclcd.mirror_free()
    else:
        moclcd.mirror_disable()
    _mirroring = False


def read_framebuffer(dest=None, x=0, y=0, w=None, h=None):
    """Read the current frame (or a sub-rectangle of it) out of the
    shadow framebuffer and forward it into `dest` -- a pre-allocated
    writable buffer such as a bytearray, or a variable you already
    hold a reference to. If `dest` is omitted, a correctly-sized
    bytearray is allocated and returned for you.

    Requires enable_framebuffer_mirror() to have been called first.
    Result is RGB565, MSB-first per pixel -- the same layout blit()
    expects, so you can feed a captured region straight back in:

        buf = gfx.read_framebuffer(x=10, y=10, w=64, h=64)
        ... draw a popup over that area ...
        gfx.blit(10, 10, 64, 64, buf)   # restore what was there
    """
    if not _mirroring:
        raise OSError("read_framebuffer: call enable_framebuffer_mirror() first")

    rw = w if w is not None else WIDTH
    rh = h if h is not None else HEIGHT

    if dest is None:
        dest = bytearray(rw * rh * 2)

    moclcd.read_framebuffer(dest, x=x, y=y, w=rw, h=rh)
    return dest


def screenshot_to_file(path, x=0, y=0, w=None, h=None):
    """Convenience: read_framebuffer() then write the raw RGB565 bytes
    straight to `path`. Requires enable_framebuffer_mirror() to have
    been called first. Requires the filesystem to already be mounted
    (same caveat as draw_bmp() -- see init_display()'s docstring)."""
    buf = read_framebuffer(x=x, y=y, w=w, h=h)
    with open(path, "wb") as f:
        f.write(buf)
    return buf


# ---------------------------------------------------------------------
# Multi-font support
#
# moclcd's built-in font (id 0) is the fixed 8x8 font_petme128_8x8
# table. Additional fonts can be registered at runtime with
# register_font() -- pass a column-major glyph table (same byte layout
# as font_petme128_8x8: char_w bytes per glyph, bit j of byte i is row
# j of column i) exported from wherever you convert a .bdf/.ttf/image
# font offline. Once registered, draw text with that font id via
# draw_text(..., font=font_id).
# ---------------------------------------------------------------------

_registered_fonts = {}  # font_id -> (char_w, char_h, first_char, last_char)


def register_font(font_id, glyph_data, char_w, char_h, first_char=32, last_char=127):
    """Register a font (id 1..7; id 0 is always the built-in 8x8 font
    and can't be overwritten). glyph_data is a bytes-like column-major
    bitmap table covering (last_char - first_char + 1) characters,
    char_w bytes each. Once registered, use draw_text(..., font=font_id)."""
    moclcd.register_font(font_id, glyph_data, char_w, char_h, first_char, last_char)
    _registered_fonts[font_id] = (char_w, char_h, first_char, last_char)


def unregister_font(font_id):
    moclcd.unregister_font(font_id)
    _registered_fonts.pop(font_id, None)


def font_metrics(font_id=0):
    """Returns (char_w, char_h, first_char, last_char) for a font id."""
    return moclcd.font_metrics(font_id)


def draw_text(x, y, text, fg, bg=None, font=0):
    """Like draw_text8x8() but with a `font` id selecting which
    registered font to draw with (0 = built-in 8x8). bg=None means
    the module default `background` is used as an opaque fill, same
    convention as draw_text8x8(); pass bg=False for moclcd's native
    transparent mode instead (only foreground pixels are plotted)."""
    if not text:
        return
    actual_bg = background if bg is None else (None if bg is False else bg)
    moclcd.draw_text(x, y, text, fg, bg=actual_bg, font=font)
    if _capturing:
        _capture_ops.append(('text_font', x, y, text, fg, actual_bg, font))


# ---------------------------------------------------------------------
# Colors — an iOS-ish default palette, small and consistent
# ---------------------------------------------------------------------

def color565(r, g, b):
    """Return RGB565 color value."""
    return (r & 0xf8) << 8 | (g & 0xfc) << 3 | b >> 3


WHITE      = color565(255, 255, 255)
BLACK      = color565(0, 0, 0)
GRAY       = color565(128, 128, 128)
DARK_GRAY  = color565(60, 60, 60)
LIGHT_GRAY = color565(200, 200, 200)

BLUE       = color565(0, 122, 255)
GREEN      = color565(52, 199, 89)
RED        = color565(255, 59, 48)
ORANGE     = color565(255, 149, 0)
YELLOW     = color565(255, 204, 0)
PURPLE     = color565(175, 82, 222)

SURFACE       = color565(28, 28, 30)   # dark card/panel background
SURFACE_ALT   = color565(44, 44, 46)   # slightly lighter surface
BORDER        = color565(70, 70, 72)
TEXT_MUTED    = color565(150, 150, 155)

background = BLACK  # module-wide default background, override with set_background()


def set_background(color):
    global background
    background = color


def log_error(msg):
    print("[UI ERROR]", msg)


def log_warn(msg):
    print("[UI WARN]", msg)


# ---------------------------------------------------------------------
# Touch (moclcd has none — plug your driver in here)
# ---------------------------------------------------------------------

_touch_fn = None


def set_touch_handler(fn):
    """fn() should return (x, y) or None."""
    global _touch_fn
    _touch_fn = fn


def get_touch():
    if _touch_fn is None:
        return None
    return _touch_fn()


def _point_in_rect(px, py, x, y, w, h, margin=0):
    return (x - margin <= px <= x + w + margin and
            y - margin <= py <= y + h + margin)


# ---------------------------------------------------------------------
# Safe drawing primitives (moclcd.fill_rect()/blit() raise on
# out-of-bounds; these clip silently instead)
# ---------------------------------------------------------------------

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
    if _capturing:
        _capture_ops.append(('rect', x, y, w, h, color))


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


def draw_rounded_rect(x, y, w, h, r, color, filled=True):
    """Rounded rectangle built from a center cross plus four corner
    circles. Falls back to a plain rect if r is too big/small."""
    r = max(0, min(r, w // 2, h // 2))
    if r == 0:
        if filled:
            fill_rect(x, y, w, h, color)
        else:
            draw_rect(x, y, w, h, color)
        return

    if filled:
        fill_rect(x + r, y, w - 2 * r, h, color)
        fill_rect(x, y + r, w, h - 2 * r, color)
        fill_circle(x + r, y + r, r, color)
        fill_circle(x + w - r - 1, y + r, r, color)
        fill_circle(x + r, y + h - r - 1, r, color)
        fill_circle(x + w - r - 1, y + h - r - 1, r, color)
    else:
        draw_hline(x + r, y, w - 2 * r, color)
        draw_hline(x + r, y + h - 1, w - 2 * r, color)
        draw_vline(x, y + r, h - 2 * r, color)
        draw_vline(x + w - 1, y + r, h - 2 * r, color)
        draw_circle(x + r, y + r, r, color)
        draw_circle(x + w - r - 1, y + r, r, color)
        draw_circle(x + r, y + h - r - 1, r, color)
        draw_circle(x + w - r - 1, y + h - r - 1, r, color)


# ---------------------------------------------------------------------
# Capture-aware wrappers for circle/line/outline-rect primitives.
#
# These exist for two reasons: they clip-safe wrap moclcd's raw calls
# (like fill_rect/blit already did), and -- the new part -- they log
# themselves into the module-level "op list" when capture is active
# (see begin_capture()/end_capture() below). Every widget in this file
# has been switched from calling moclcd.fill_circle/draw_circle/
# draw_line/draw_rect directly to calling these instead, so that a
# UIScreen.snapshot() faithfully records everything drawn on it.
# ---------------------------------------------------------------------

_capturing = False
_capture_ops = []


def begin_capture():
    """Start recording every fill_rect/draw_rect/fill_circle/draw_circle/
    draw_line/draw_text8x8/draw_bmp call as a lightweight op list,
    instead of (or in addition to) sending it to the panel. Each call
    still draws normally -- capture just also logs it -- so you can
    call this right before a normal draw pass and get a replayable
    snapshot "for free"."""
    global _capturing, _capture_ops
    _capturing = True
    _capture_ops = []


def end_capture():
    """Stop recording and return the captured op list."""
    global _capturing
    _capturing = False
    return _capture_ops


def fill_circle(cx, cy, r, color):
    moclcd.fill_circle(cx, cy, r, color)
    if _capturing:
        _capture_ops.append(('circle', cx, cy, r, color, True))


def draw_circle(cx, cy, r, color):
    moclcd.draw_circle(cx, cy, r, color)
    if _capturing:
        _capture_ops.append(('circle', cx, cy, r, color, False))


def draw_line(x0, y0, x1, y1, color):
    moclcd.draw_line(x0, y0, x1, y1, color)
    if _capturing:
        _capture_ops.append(('line', x0, y0, x1, y1, color))


def draw_rect(x, y, w, h, color):
    moclcd.draw_rect(x, y, w, h, color)
    if _capturing:
        _capture_ops.append(('rect_outline', x, y, w, h, color))


def draw_text8x8(x, y, text, fg, bg=None):
    """Render text using moclcd's native 8x8 font blitter.

    moclcd.draw_text8x8() is implemented in C (font_petme128_8x8, the
    same table MicroPython's framebuf.text() uses) and sends each glyph
    as a single DMA transfer instead of building the string in a Python
    framebuf and manually byte-swapping it — this is the fast path.

    bg semantics: zeno_gfx keeps its own default of an OPAQUE background
    (the module-wide `background`, or whatever is passed), matching the
    old behavior most app code was written against. Pass bg=None
    explicitly if you want moclcd's native transparent mode (only
    foreground pixels are plotted, one address-window per lit pixel —
    slower, but leaves whatever's already drawn behind the text alone).
    """
    if not text:
        return
    if bg is None:
        bg = background
    moclcd.draw_text8x8(x, y, text, fg, bg)
    if _capturing:
        _capture_ops.append(('text', x, y, text, fg, bg))


def draw_text8x8_transparent(x, y, text, fg):
    """Explicit transparent-background variant (only lit pixels drawn).
    Slower than draw_text8x8() since each pixel needs its own address
    window, but doesn't disturb whatever is already behind the glyphs."""
    if not text:
        return
    moclcd.draw_text8x8(x, y, text, fg, None)


def draw_text_centered(cx, y, text, fg, bg=None, scale=1):
    """Draw text8x8 horizontally centered on cx. scale repeats each
    char cell to approximate larger text without a new font."""
    if not text:
        return
    w = len(text) * 8 * scale
    x = cx - w // 2
    if scale == 1:
        draw_text8x8(x, y, text, fg, bg)
        return
    # scaled variant: render to a small buffer then blit stretched
    # (kept simple: just draw at native size repeated on a grid)
    for i, ch in enumerate(text):
        cxp = x + i * 8 * scale
        draw_text8x8(cxp, y, ch, fg, bg)


def _bmp_native_size(path):
    """Peek just the BMP header (26 bytes is enough) for native pixel
    dimensions, without decoding pixel data. Used by draw_logo() to
    compute a fit-to-box scale before handing the real draw off to the
    native loader."""
    with open(path, "rb") as f:
        header = f.read(26)
        if header[0:2] != b"BM":
            raise ValueError("not a BMP file")
        bmp_w = int.from_bytes(header[18:22], "little")
        bmp_h = abs(int.from_bytes(header[22:26], "little"))
        return bmp_w, bmp_h


def draw_bmp(path, x, y, w=None, h=None, max_w=None, max_h=None):
    """Draw an uncompressed 24-bit BMP via moclcd's native loader.

    moclcd.draw_bmp() is implemented in C: it parses the header, decodes
    straight into a DMA-capable buffer, and sends the whole image as a
    single transfer — no per-pixel Python loop, no intermediate
    bytearray copy through this layer. This wrapper just translates
    Python's None ("unset") into the 0 the C function expects for
    w/h/max_w/max_h.

    Raises the same errors as before (ValueError for a bad/unsupported
    BMP, OSError if the path doesn't exist) since moclcd.draw_bmp()
    raises those natively.
    """
    moclcd.draw_bmp(
        path, x, y,
        w=w or 0, h=h or 0,
        max_w=max_w or 0, max_h=max_h or 0,
    )
    if _capturing:
        rw, rh = w, h
        if not rw or not rh:
            try:
                nw, nh = _bmp_native_size(path)
                rw, rh = rw or nw, rh or nh
            except Exception:
                rw, rh = rw or 1, rh or 1
        _capture_ops.append(('bmp', path, x, y, rw, rh))


def draw_logo(path, box_x=0, box_y=0, box_w=WIDTH, box_h=HEIGHT,
              caption=None, caption_color=WHITE, bg=None):
    """Draw a BMP logo centered within (box_x, box_y, box_w, box_h),
    scaled down (not up) to fit, with an optional caption underneath.

    Useful for splash screens / about screens / branded headers:

        gfx.draw_logo("logo.bmp", box_h=200, caption="MyDevice v1.0")
    """
    if bg is not None:
        fill_rect(box_x, box_y, box_w, box_h, bg)

    # Peek at native size first without allocating full pixel buffer twice:
    # draw_bmp already supports max_w/max_h clamping, reuse that for "fit".
    caption_h = 12 if caption else 0
    avail_h = box_h - caption_h

    native_w, native_h = _bmp_native_size(path)

    scale = min(box_w / native_w, avail_h / native_h, 1.0)
    out_w = max(1, int(native_w * scale))
    out_h = max(1, int(native_h * scale))

    lx = box_x + (box_w - out_w) // 2
    ly = box_y + (avail_h - out_h) // 2

    draw_bmp(path, lx, ly, out_w, out_h)

    if caption:
        draw_text_centered(box_x + box_w // 2, box_y + avail_h + 2,
                            caption, caption_color, bg)

    return lx, ly, out_w, out_h


# ---------------------------------------------------------------------
# Screen transition animations
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# blit_fast batching helper: builds int32 arrays for a batch of rects
# and sends them to moclcd in one C call instead of one Python call
# per rect. Cuts per-frame MicroPython call/bounds-check overhead out
# of the animation hot loop -- fill_rect() clips+validates in Python
# and moclcd.fill_rect() re-validates in C on *every* call; blit_fast
# skips straight to the batched C-side clip+stream path used by
# do_fill_rect_clip() in modlcd.c.
# ---------------------------------------------------------------------

def _flush_batch(xs, ys, ws, hs, cs):
    n = len(xs)
    if n == 0:
        return
    moclcd.blit_fast(
        array.array('i', xs), array.array('i', ys),
        array.array('i', ws), array.array('i', hs),
        array.array('i', cs), n,
    )
    if _capturing:
        for i in range(n):
            _capture_ops.append(('rect', xs[i], ys[i], ws[i], hs[i], cs[i]))
    xs.clear(); ys.clear(); ws.clear(); hs.clear(); cs.clear()


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

        xs, ys, ws, hs, cs = [], [], [], [], []
        if y0 > prev_y0:
            xs.append(prev_x0); ys.append(prev_y0); ws.append(prev_x1 - prev_x0 + 1); hs.append(y0 - prev_y0); cs.append(background)
        if y1 < prev_y1:
            xs.append(prev_x0); ys.append(y1 + 1); ws.append(prev_x1 - prev_x0 + 1); hs.append(prev_y1 - y1); cs.append(background)
        if x0 > prev_x0:
            xs.append(prev_x0); ys.append(y0); ws.append(x0 - prev_x0); hs.append(y1 - y0 + 1); cs.append(background)
        if x1 < prev_x1:
            xs.append(x1 + 1); ys.append(y0); ws.append(prev_x1 - x1); hs.append(y1 - y0 + 1); cs.append(background)
        _flush_batch(xs, ys, ws, hs, cs)

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

        xs, ys, ws, hs, cs = [], [], [], [], []
        if y0 < prev_y0:
            xs.append(x0); ys.append(y0); ws.append(w); hs.append(prev_y0 - y0); cs.append(color)
        if y1 > prev_y1:
            xs.append(x0); ys.append(prev_y1 + 1); ws.append(w); hs.append(y1 - prev_y1); cs.append(color)
        if x0 < prev_x0:
            xs.append(x0); ys.append(prev_y0); ws.append(prev_x0 - x0); hs.append(prev_y1 - prev_y0 + 1); cs.append(color)
        if x1 > prev_x1:
            xs.append(prev_x1 + 1); ys.append(prev_y0); ws.append(x1 - prev_x1); hs.append(prev_y1 - prev_y0 + 1); cs.append(color)
        _flush_batch(xs, ys, ws, hs, cs)

        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1
        time.sleep(delay)


# ---------------------------------------------------------------------
# "Genie" animations: shrink/grow the screen's ACTUAL content
# (taskbar, buttons, text, icons) instead of a flat color block.
#
# moclcd has no framebuffer readback (it's a write-only DMA panel), so
# there's no cheap way to "grab the real pixels and scale them" the
# way a desktop compositor would -- that needs a full-screen RAM copy
# (480x320x2 bytes = ~300KB), which most boards don't have spare.
#
# Instead these work off a *command list* captured by begin_capture()/
# end_capture() (or UIScreen.snapshot()): every fill_rect/circle/line/
# text/bmp call made while capturing is logged as a small tuple. That
# list gets replayed every frame with each shape's position/size
# scaled toward an origin point -- rectangles, circles, lines and BMPs
# all scale correctly since they're just numbers. Text can't be
# scaled (moclcd's font is a fixed 8x8 bitmap glyph table), so text
# ops are simply skipped until the animation is almost finished, then
# they pop in at the very end -- the same trick a lot of embedded UIs
# use for genie/zoom effects on non-scalable fonts.
# ---------------------------------------------------------------------

def replay_ops(ops, scale, origin_x, origin_y, text_reveal=0.92):
    """Redraw a captured op list scaled toward (origin_x, origin_y).
    scale=1.0 reproduces the screen as captured; scale=0 collapses
    everything onto the origin point. Text is only drawn once `scale`
    passes `text_reveal` (see module docstring above for why)."""
    for op in ops:
        kind = op[0]

        if kind == 'rect':
            _, x, y, w, h, color = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            fill_rect(int(nx), int(ny), max(1, int(w * scale)), max(1, int(h * scale)), color)

        elif kind == 'rect_outline':
            _, x, y, w, h, color = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            draw_rect(int(nx), int(ny), max(1, int(w * scale)), max(1, int(h * scale)), color)

        elif kind == 'circle':
            _, cx, cy, r, color, filled = op
            ncx = origin_x + (cx - origin_x) * scale
            ncy = origin_y + (cy - origin_y) * scale
            nr = max(1, int(r * scale))
            if filled:
                fill_circle(int(ncx), int(ncy), nr, color)
            else:
                draw_circle(int(ncx), int(ncy), nr, color)

        elif kind == 'line':
            _, x0, y0, x1, y1, color = op
            draw_line(
                int(origin_x + (x0 - origin_x) * scale), int(origin_y + (y0 - origin_y) * scale),
                int(origin_x + (x1 - origin_x) * scale), int(origin_y + (y1 - origin_y) * scale),
                color,
            )

        elif kind == 'bmp':
            _, path, x, y, w, h = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            try:
                draw_bmp(path, int(nx), int(ny), max(1, int(w * scale)), max(1, int(h * scale)))
            except Exception:
                pass

        elif kind == 'text':
            if scale < text_reveal:
                continue
            _, x, y, text, fg, bg = op
            draw_text8x8(int(origin_x + (x - origin_x) * scale), int(origin_y + (y - origin_y) * scale),
                         text, fg, bg)

        elif kind == 'text_font':
            if scale < text_reveal:
                continue
            _, x, y, text, fg, bg, font = op
            moclcd.draw_text(int(origin_x + (x - origin_x) * scale), int(origin_y + (y - origin_y) * scale),
                              text, fg, bg=bg, font=font)


def replay_ops_fast(ops, scale, origin_x, origin_y, text_reveal=0.92):
    """Same as replay_ops(), but batches every plain filled-rect op
    ('rect') in the list into as few moclcd.blit_fast() calls as
    possible instead of one moclcd.fill_rect() call per rect. A genie
    animation frame with a taskbar + several buttons can easily be
    10-30 'rect' ops; this turns that into a single C round trip per
    frame for the fills, while circles/lines/outlines/bmps/text (which
    aren't flat fills) still go through their normal per-op calls."""
    xs, ys, ws, hs, cs = [], [], [], [], []

    def flush():
        _flush_batch(xs, ys, ws, hs, cs)

    for op in ops:
        kind = op[0]

        if kind == 'rect':
            _, x, y, w, h, color = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            xs.append(int(nx)); ys.append(int(ny))
            ws.append(max(1, int(w * scale))); hs.append(max(1, int(h * scale)))
            cs.append(color)
            continue

        # any non-'rect' op breaks the run of batchable fills -- flush
        # what's queued so ordering/z-order stays correct, then handle
        # this op the normal way.
        flush()

        if kind == 'rect_outline':
            _, x, y, w, h, color = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            draw_rect(int(nx), int(ny), max(1, int(w * scale)), max(1, int(h * scale)), color)

        elif kind == 'circle':
            _, cx, cy, r, color, filled = op
            ncx = origin_x + (cx - origin_x) * scale
            ncy = origin_y + (cy - origin_y) * scale
            nr = max(1, int(r * scale))
            if filled:
                fill_circle(int(ncx), int(ncy), nr, color)
            else:
                draw_circle(int(ncx), int(ncy), nr, color)

        elif kind == 'line':
            _, x0, y0, x1, y1, color = op
            draw_line(
                int(origin_x + (x0 - origin_x) * scale), int(origin_y + (y0 - origin_y) * scale),
                int(origin_x + (x1 - origin_x) * scale), int(origin_y + (y1 - origin_y) * scale),
                color,
            )

        elif kind == 'bmp':
            _, path, x, y, w, h = op
            nx = origin_x + (x - origin_x) * scale
            ny = origin_y + (y - origin_y) * scale
            try:
                draw_bmp(path, int(nx), int(ny), max(1, int(w * scale)), max(1, int(h * scale)))
            except Exception:
                pass

        elif kind == 'text':
            if scale >= text_reveal:
                _, x, y, text, fg, bg = op
                draw_text8x8(int(origin_x + (x - origin_x) * scale), int(origin_y + (y - origin_y) * scale),
                             text, fg, bg)

        elif kind == 'text_font':
            if scale >= text_reveal:
                _, x, y, text, fg, bg, font = op
                moclcd.draw_text(int(origin_x + (x - origin_x) * scale), int(origin_y + (y - origin_y) * scale),
                                  text, fg, bg=bg, font=font)

    flush()


def _ops_bbox(ops):
    """Bounding box (x0, y0, x1, y1) covering every primitive in a
    captured op list. Falls back to the full screen if ops is empty."""
    if not ops:
        return 0, 0, WIDTH, HEIGHT

    x0 = y0 = 1 << 30
    x1 = y1 = -(1 << 30)

    for op in ops:
        kind = op[0]
        if kind == 'rect' or kind == 'rect_outline':
            _, x, y, w, h, color = op
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + w), max(y1, y + h)
        elif kind == 'bmp':
            _, path, x, y, w, h = op
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + w), max(y1, y + h)
        elif kind == 'circle':
            _, cx, cy, r, color, filled = op
            x0, y0 = min(x0, cx - r), min(y0, cy - r)
            x1, y1 = max(x1, cx + r), max(y1, cy + r)
        elif kind == 'line':
            _, lx0, ly0, lx1, ly1, color = op
            x0, y0 = min(x0, lx0, lx1), min(y0, ly0, ly1)
            x1, y1 = max(x1, lx0, lx1), max(y1, ly0, ly1)
        elif kind == 'text':
            _, x, y, text, fg, bg = op
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + len(text) * 8), max(y1, y + 8)
        elif kind == 'text_font':
            _, x, y, text, fg, bg, font = op
            cw, ch, _f, _l = moclcd.font_metrics(font)
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + len(text) * cw), max(y1, y + ch)

    if x1 < x0 or y1 < y0:
        return 0, 0, WIDTH, HEIGHT
    return x0, y0, x1, y1


def window_close_animation_live(ops, duration=0.4, fps=60, origin=None, ease=True, bg=None):
    """Content-aware close: shrinks the screen's actual captured
    content (from begin_capture()/end_capture() or UIScreen.snapshot())
    toward `origin` instead of a flat color block.

    origin defaults to screen center; pass a widget's center (e.g. a
    taskbar icon) to genie the window into that widget instead.
    """
    if bg is None:
        bg = background

    ox, oy = origin if origin else (WIDTH // 2, HEIGHT // 2)
    bx0, by0, bx1, by1 = _ops_bbox(ops)

    frames = max(1, int(duration * fps))
    delay = duration / frames

    prev_x0, prev_y0, prev_x1, prev_y1 = bx0, by0, bx1, by1

    for i in range(frames + 1):
        t = i / frames
        if ease:
            t = t * t * (3 - 2 * t)
        t = 1 - t  # 1 -> 0

        nx0 = ox + (bx0 - ox) * t
        ny0 = oy + (by0 - oy) * t
        nx1 = ox + (bx1 - ox) * t
        ny1 = oy + (by1 - oy) * t
        x0, x1 = sorted((int(nx0), int(nx1)))
        y0, y1 = sorted((int(ny0), int(ny1)))

        # vacate whatever the previous (bigger) frame occupied that
        # this (smaller) frame no longer covers
        xs, ys, ws, hs, cs = [], [], [], [], []
        if y0 > prev_y0:
            xs.append(prev_x0); ys.append(prev_y0); ws.append(prev_x1 - prev_x0); hs.append(y0 - prev_y0); cs.append(bg)
        if y1 < prev_y1:
            xs.append(prev_x0); ys.append(y1); ws.append(prev_x1 - prev_x0); hs.append(prev_y1 - y1); cs.append(bg)
        if x0 > prev_x0:
            xs.append(prev_x0); ys.append(y0); ws.append(x0 - prev_x0); hs.append(y1 - y0); cs.append(bg)
        if x1 < prev_x1:
            xs.append(x1); ys.append(y0); ws.append(prev_x1 - x1); hs.append(y1 - y0); cs.append(bg)
        _flush_batch(xs, ys, ws, hs, cs)

        replay_ops_fast(ops, t, ox, oy)

        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1
        time.sleep(delay)

    fill_rect(prev_x0, prev_y0, max(1, prev_x1 - prev_x0), max(1, prev_y1 - prev_y0), bg)


def window_open_animation_live(ops, duration=0.4, fps=60, origin=None, ease=True, bg=None):
    """Content-aware open: grows the screen's actual content in from
    `origin` (default screen center) instead of a flat color block.
    Call this with the op list from UIScreen.snapshot() / end_capture()."""
    if bg is None:
        bg = background

    ox, oy = origin if origin else (WIDTH // 2, HEIGHT // 2)

    clear(bg)

    frames = max(1, int(duration * fps))
    delay = duration / frames

    for i in range(frames + 1):
        t = i / frames
        if ease:
            t = 1 - (1 - t) ** 3
        replay_ops_fast(ops, t, ox, oy)
        time.sleep(delay)


# ---------------------------------------------------------------------
# Base class: shared touch-rect handling so widgets stop reimplementing
# "is this point inside me + debounce" from scratch each time.
# ---------------------------------------------------------------------

class _Touchable:
    """Mixin providing a standard rectangular hit-test + simple
    press/release edge detection. Widgets set self.x/y/w/h and can
    call self._tap() from handle_touch()/get_touch() to get a bool
    that is True exactly once per press (not held down)."""

    _press_active = False

    def _hit(self, margin=0):
        p = get_touch()
        if not p:
            self._press_active = False
            return None
        tx, ty = p
        if _point_in_rect(tx, ty, self.x, self.y, self.w, self.h, margin):
            return (tx, ty)
        self._press_active = False
        return None

    def _tap(self, margin=0):
        """Edge-triggered: True only on the frame touch first lands inside."""
        pt = self._hit(margin)
        if pt is None:
            return False
        if self._press_active:
            return False
        self._press_active = True
        return True


# =============================
# UIButton
# =============================
class UIButton(_Touchable):
    def __init__(self, x, y, w, h, label,
                 color=BLUE, text_color=WHITE,
                 radius=6, margin=5, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = str(label)
        self.color = color
        self.text_color = text_color
        self.radius = radius
        self.margin = margin
        self.action = action
        self.badge = None  # optional NotificationBadge attached to this button

    def draw(self):
        try:
            draw_rounded_rect(self.x, self.y, self.w, self.h, self.radius, self.color)

            text_w = len(self.label) * 8
            text_x = self.x + (self.w - text_w) // 2
            text_y = self.y + (self.h - 8) // 2

            if self.label:
                draw_text8x8(text_x, text_y, self.label, self.text_color, self.color)

            if self.badge:
                self.badge.draw_at_corner(self.x, self.y, self.w, self.h)

        except Exception as e:
            log_error("UIButton draw failed ({}): {}".format(self.label, e))
            try:
                fill_rect(self.x, self.y, self.w, self.h, RED)
            except Exception:
                pass

    def get_touch(self):
        pt = self._hit(self.margin)
        if pt and self.action and not self._press_active:
            self._press_active = True
            self.action()
            return True
        return pt is not None


class UIText:
    def __init__(self, x, y, text, fg=WHITE, bg=None):
        self.x = x
        self.y = y
        self.text = text
        self.fg = fg
        self.bg = bg

    def draw(self):
        draw_text8x8(self.x, self.y, str(self.text), self.fg, self.bg if self.bg else background)


class UIBMPButton(_Touchable):
    def __init__(self, x, y, w, h, bmp, *, bmp_pressed=None, margin=5, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.bmp = bmp
        self.bmp_pressed = bmp_pressed
        self.margin = margin
        self.action = action
        self.badge = None
        self._pressed = False

    def draw(self):
        try:
            path = self.bmp_pressed if (self._pressed and self.bmp_pressed) else self.bmp
            draw_bmp(path, self.x, self.y, self.w, self.h)
            if self.badge:
                self.badge.draw_at_corner(self.x, self.y, self.w, self.h)
        except Exception as e:
            log_error("UIBMPButton draw failed: {}".format(e))

    def get_touch(self):
        pt = self._hit(self.margin)
        if not pt:
            self._pressed = False
            return False

        if not self._pressed:
            self._pressed = True
            if self.bmp_pressed:
                self.draw()
        if self.action:
            self.action()
        return True


class UIScreen:
    def __init__(self,
                 fg=WHITE,
                 background=None,
                 on_exit=None,
                 taskbarcolor=SURFACE_ALT,
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
        self._ops = []

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

    def closescreen(self, widgets=None, genie=True, origin=None):
        """Animate the screen away. By default this is the new "genie"
        effect: it re-snapshots the real content (taskbar, exit box,
        and whatever's in `widgets`) so it reflects current state
        (e.g. a toggle you just flipped) and shrinks *that* toward
        `origin` (default: screen center). Pass genie=False for the
        old flat-color shrink."""
        if genie:
            ops = self.snapshot(widgets)
            window_close_animation_live(ops, duration=0.4, fps=60, origin=origin, bg=self.background)
        else:
            window_close_animation(duration=0.4, fps=60, color=None, ease=True)

    def _draw_chrome(self):
        """Draws background + taskbar + exit box. Shared by snapshot()
        (records it) and the non-genie start() path (draws it plain)."""
        self._draw_background()

        h = self.taskbar_height
        fill_rect(0, 0, WIDTH, h, self.taskbarcolor)

        btn = 30
        x0 = WIDTH - btn - 2
        y0 = 2
        self.exit_box = (x0, y0, btn, btn)

        draw_rounded_rect(x0, y0, btn, btn, 6, BLUE)
        draw_line(x0 + 8, y0 + 8, x0 + btn - 8, y0 + btn - 8, WHITE)
        draw_line(x0 + btn - 8, y0 + 8, x0 + 8, y0 + btn - 8, WHITE)

        if self.taskbar_text:
            draw_text_centered(WIDTH // 2, (h - 8) // 2,
                                self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)

    def snapshot(self, widgets=None):
        """Record the screen's current visual content -- background,
        taskbar, exit box, plus any extra widgets passed in -- as a
        lightweight op list for window_open_animation_live() /
        window_close_animation_live(). Actually draws everything once
        while recording, so call it right before an animated
        open/close (after your widgets reflect whatever state they're
        currently in). `widgets` is any list of objects with .draw().
        """
        begin_capture()
        self._draw_chrome()
        if widgets:
            for w in widgets:
                w.draw()
        self._ops = end_capture()
        return self._ops

    def start(self, widgets=None, genie=True, origin=None):
        """Bring the screen up. By default this is the "genie" open:
        it draws (and records) the taskbar/exit box/background plus
        any widgets you pass in, then grows that real content in from
        `origin` (default: screen center) instead of a flat color
        block. Pass genie=False for the old flat-color grow, drawn
        the plain way afterward.
        """
        global active_screen

        if genie:
            ops = self.snapshot(widgets)
            window_open_animation_live(ops, duration=0.4, fps=60, origin=origin, bg=self.background)
        else:
            window_open_animation(duration=0.4, fps=60, color=self.background, ease=True)
            self._draw_chrome()
            if widgets:
                for w in widgets:
                    w.draw()

        active_screen = self

    def taskbar(self, taskbarcolor, taskbar_text, taskbar_text_color, taskbar_height=35):
        self.taskbar_text = taskbar_text
        self.taskbarcolor = taskbarcolor
        self.taskbar_text_color = taskbar_text_color
        self.taskbar_height = taskbar_height

        fill_rect(0, 0, WIDTH, self.taskbar_height, self.taskbarcolor)
        if self.taskbar_text:
            draw_text_centered(WIDTH // 2, (self.taskbar_height - 8) // 2,
                                self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)

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
            draw_text_centered(WIDTH // 2, (h - 8) // 2,
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

        W = 190
        H = 96
        X = (WIDTH - W) // 2
        Y = (HEIGHT - H) // 2

        self.x, self.y, self.w, self.h = X, Y, W, H

        self.texts = []
        self.buttons = []

        draw_rounded_rect(X, Y, W, H, 10, SURFACE)
        draw_rounded_rect(X, Y, W, 22, 10, SURFACE_ALT)
        fill_rect(X, Y + 14, W, 8, SURFACE_ALT)  # square off bottom of header radius

        self.texts.append(UIText(X + 10, Y + 7, self.title, fg=WHITE, bg=SURFACE_ALT))
        self.texts.append(UIText(X + 10, Y + 44, self.message, fg=TEXT_MUTED, bg=SURFACE))

        self.buttons.append(UIButton(
            X + W - 20, Y + 3, 15, 15, label="X",
            color=RED, text_color=WHITE, radius=3, margin=3, action=self._exit
        ))
        self.buttons.append(UIButton(
            X + 14, Y + H - 32, 74, 24, label=self.btn_yes,
            color=GREEN, text_color=WHITE, margin=5, action=self._yes
        ))
        self.buttons.append(UIButton(
            X + W - 14 - 74, Y + H - 32, 74, 24, label=self.btn_no,
            color=SURFACE_ALT, text_color=WHITE, margin=5, action=self._no
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


class UIToggleSwitch(_Touchable):
    def __init__(self, x, y, w=50, h=26, state=False,
                 on_color=GREEN, off_color=color565(120, 120, 120),
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

        fill_circle(self.x + r, self.y + r, r, bg)
        fill_circle(self.x + self.w - r - 1, self.y + r, r, bg)
        fill_rect(self.x + r, self.y, self.w - 2 * r, self.h, bg)

        kx = self.x + self.w - r - 1 if self.state else self.x + r
        fill_circle(kx, self.y + r, r - 2, self.knob)

    def handle_touch(self):
        if not self._tap():
            return False
        self.state = not self.state
        self.draw()
        if self.action:
            self.action(self.state)
        return True


class UISlider(_Touchable):
    """iOS-style slider: bordered track, filled progress, draggable knob."""

    def __init__(self, x, y, w, min_v=0, max_v=100, value=0,
                 track_border=DARK_GRAY,
                 track_fill=SURFACE_ALT,
                 fill=BLUE,
                 knob=WHITE,
                 action=None):
        self.x, self.y, self.w = x, y, w
        self.h = 18
        self.knob_size = 16

        self.min = min_v
        self.max = max_v
        self.value = float(value)

        self.track_border = track_border
        self.track_fill = track_fill
        self.fill = fill
        self.knob_color = knob
        self.action = action

        self.dragging = False
        self.drag_offset = 0
        self._last_kx = None

    def draw(self):
        fill_rect(self.x - 2, self.y - 2, self.w + 4, self.h + 4, background)

        draw_rect(self.x, self.y, self.w, self.h, self.track_border)
        fill_rect(self.x + 1, self.y + 1, self.w - 2, self.h - 2, self.track_fill)

        travel = self.w - self.knob_size
        ratio = (self.value - self.min) / (self.max - self.min) if self.max != self.min else 0
        kx = self.x + int(ratio * travel)

        if kx > self.x + 1:
            fill_rect(self.x + 1, self.y + 1, kx - self.x, self.h - 2, self.fill)

        self._draw_knob(kx)
        self._last_kx = kx

    def _draw_knob(self, kx):
        ky = self.y + 1
        draw_rounded_rect(kx, ky, self.knob_size, self.knob_size, self.knob_size // 2, BLACK)
        draw_rounded_rect(kx + 1, ky + 1, self.knob_size - 2, self.knob_size - 2,
                           (self.knob_size - 2) // 2, self.knob_color)

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
            if _point_in_rect(tx, ty, self._last_kx, ky, self.knob_size, self.knob_size, 6):
                self.dragging = True
                self.drag_offset = tx - self._last_kx
            else:
                return False

        new_x = tx - self.drag_offset
        new_x = max(self.x, min(self.x + travel, new_x))
        if new_x == self._last_kx:
            return True

        ratio = (new_x - self.x) / travel if travel else 0
        self.value = self.min + ratio * (self.max - self.min)
        self.draw()

        if self.action:
            self.action(self.value)

        return True


class UIPanel:
    def __init__(self, x, y, w, h, title=None, bg=None, border=None,
                 title_fg=None, title_bg=None, radius=8):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.title = title
        self.radius = radius

        self.bg = bg if bg is not None else SURFACE
        self.border = border if border is not None else BORDER
        self.title_fg = title_fg if title_fg is not None else color565(200, 200, 200)
        self.title_bg = title_bg if title_bg is not None else self.bg

    def draw(self):
        draw_rounded_rect(self.x, self.y, self.w, self.h, self.radius, self.bg)
        draw_rounded_rect(self.x, self.y, self.w, self.h, self.radius, self.border, filled=False)

        if self.title:
            draw_text8x8(self.x + 8, self.y + 8, self.title, self.title_fg, self.title_bg)

    def open(self, steps=8, delay_ms=1):
        tw = self.w
        th = self.h

        for i in range(1, steps + 1):
            cw = (tw * i) // steps
            ch = (th * i) // steps
            draw_rounded_rect(self.x, self.y, cw, ch, min(self.radius, cw // 2, ch // 2), self.bg)
            draw_rounded_rect(self.x, self.y, cw, ch, min(self.radius, cw // 2, ch // 2), self.border, filled=False)
            if delay_ms:
                time.sleep_ms(delay_ms)

        self.draw()


# Alias: a "Card" is just a panel with a slightly larger default radius
# and no border — separated for semantic clarity in app code.
class UICard(UIPanel):
    def __init__(self, x, y, w, h, title=None, bg=None, radius=12):
        super().__init__(x, y, w, h, title=title, bg=bg, border=None, radius=radius)

    def draw(self):
        draw_rounded_rect(self.x, self.y, self.w, self.h, self.radius, self.bg)
        if self.title:
            draw_text8x8(self.x + 8, self.y + 8, self.title, self.title_fg, self.bg)


class UIProgressBar:
    def __init__(self, x, y, w, h=12, value=0, bg=SURFACE_ALT, fg=BLUE, radius=4):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.value = value
        self.bg = bg
        self.fg = fg
        self.radius = radius

    def set(self, val):
        self.value = max(0, min(100, val))
        self.draw()

    def draw(self):
        draw_rounded_rect(self.x, self.y, self.w, self.h, self.radius, self.bg)
        fw = int(self.w * self.value / 100)
        if fw >= self.h:
            draw_rounded_rect(self.x, self.y, fw, self.h, self.radius, self.fg)
        elif fw > 0:
            fill_rect(self.x, self.y, fw, self.h, self.fg)


class UISpinner:
    """Simple loading spinner: N dots around a circle, one step per
    call to draw(). Call draw() repeatedly (e.g. once per loop tick)
    to animate — it advances its own phase internally."""

    def __init__(self, cx, cy, radius=14, dots=8, color=BLUE, bg=None):
        self.cx, self.cy, self.radius = cx, cy, radius
        self.dots = dots
        self.color = color
        self.bg = bg
        self._phase = 0

    def draw(self):
        box = self.radius + 4
        fill_rect(self.cx - box, self.cy - box, box * 2, box * 2,
                  self.bg if self.bg is not None else background)

        import math
        for i in range(self.dots):
            angle = (2 * math.pi * i / self.dots) + (2 * math.pi * self._phase / self.dots)
            dx = int(self.cx + self.radius * math.cos(angle))
            dy = int(self.cy + self.radius * math.sin(angle))
            # fade dot size with position so it reads as motion
            fade = (i / self.dots)
            r = 1 + int(3 * fade)
            fill_circle(dx, dy, r, self.color)

        self._phase = (self._phase + 1) % self.dots

    def stop(self):
        box = self.radius + 4
        fill_rect(self.cx - box, self.cy - box, box * 2, box * 2,
                  self.bg if self.bg is not None else background)


class UIStatusIndicator:
    OK = 0
    WARN = 1
    ERR = 2

    def __init__(self, x, y, r=6, state=0):
        self.x, self.y, self.r = x, y, r
        self.state = state

    def draw(self):
        c = {self.OK: GREEN, self.WARN: ORANGE, self.ERR: RED}.get(self.state, RED)
        fill_circle(self.x, self.y, self.r, c)


# ---------------------------------------------------------------------
# NotificationBadge — the "red dot" / count marker.
# Two ways to use it:
#   1. Attach to a widget: btn.badge = NotificationBadge(count=3);
#      the widget's own draw() will call badge.draw_at_corner(...).
#   2. Draw manually anywhere: NotificationBadge(count=3).draw(x, y).
# ---------------------------------------------------------------------

class NotificationBadge:
    def __init__(self, count=None, color=RED, text_color=WHITE, max_count=99, min_radius=5):
        """count=None (or 0) draws a plain dot. count>0 draws a pill
        with the number (capped at max_count, shown as '99+')."""
        self.count = count
        self.color = color
        self.text_color = text_color
        self.max_count = max_count
        self.min_radius = min_radius
        self.visible = True

    def set(self, count):
        self.count = count

    def clear(self):
        self.count = None

    def draw(self, x, y):
        """Draw the badge centered at (x, y)."""
        if not self.visible:
            return

        if not self.count:
            fill_circle(x, y, self.min_radius, self.color)
            draw_circle(x, y, self.min_radius, BLACK)
            return

        label = str(self.count) if self.count <= self.max_count else "{}+".format(self.max_count)
        w = max(self.min_radius * 2, len(label) * 6 + 8)
        h = self.min_radius * 2 + 2
        bx = x - w // 2
        by = y - h // 2

        draw_rounded_rect(bx, by, w, h, h // 2, self.color)
        draw_rounded_rect(bx, by, w, h, h // 2, BLACK, filled=False)
        draw_text_centered(x, by + (h - 8) // 2, label, self.text_color, self.color)

    def draw_at_corner(self, wx, wy, ww, wh, corner="top-right", inset=2):
        """Convenience for pinning to a widget's bounding box."""
        if corner == "top-right":
            x, y = wx + ww - inset, wy + inset
        elif corner == "top-left":
            x, y = wx + inset, wy + inset
        elif corner == "bottom-right":
            x, y = wx + ww - inset, wy + wh - inset
        else:  # bottom-left
            x, y = wx + inset, wy + wh - inset
        self.draw(x, y)


class UIAvatar:
    """Circular avatar: either an initials badge or a BMP image,
    with an optional NotificationBadge pinned to the corner."""

    def __init__(self, x, y, r=20, image=None, initials=None,
                 bg=SURFACE_ALT, fg=WHITE, badge=None):
        self.x, self.y, self.r = x, y, r
        self.image = image
        self.initials = (initials or "")[:2].upper()
        self.bg = bg
        self.fg = fg
        self.badge = badge

    def draw(self):
        if self.image:
            try:
                draw_bmp(self.image, self.x - self.r, self.y - self.r,
                         self.r * 2, self.r * 2)
            except Exception as e:
                log_error("UIAvatar image failed: {}".format(e))
                fill_circle(self.x, self.y, self.r, self.bg)
        else:
            fill_circle(self.x, self.y, self.r, self.bg)
            if self.initials:
                draw_text_centered(self.x, self.y - 4, self.initials, self.fg, self.bg)

        if self.badge:
            self.badge.draw_at_corner(self.x - self.r, self.y - self.r,
                                       self.r * 2, self.r * 2)


class UIBadgeLabel:
    """Small pill/tag label, e.g. status chips: "NEW", "BETA", "3 left"."""

    def __init__(self, x, y, text, color=BLUE, text_color=WHITE, padding=6):
        self.x, self.y = x, y
        self.text = text
        self.color = color
        self.text_color = text_color
        self.padding = padding

    def draw(self):
        w = len(self.text) * 8 + self.padding * 2
        h = 16
        draw_rounded_rect(self.x, self.y, w, h, h // 2, self.color)
        draw_text8x8(self.x + self.padding, self.y + 4, self.text, self.text_color, self.color)
        return w, h


# ---------------------------------------------------------------------
# Toast — non-blocking, stackable notification banners.
# Call Toast.push(...) to enqueue, Toast.update() once per loop tick
# from your main event loop to render/expire it. No more sleep()
# blocking the whole UI thread like the old version did.
# ---------------------------------------------------------------------

class Toast:
    INFO = BLUE
    SUCCESS = GREEN
    WARNING = ORANGE
    ERROR = RED

    _queue = []
    _current = None
    _shown_at = 0
    _duration_ms = 2000
    _height = 30

    @classmethod
    def push(cls, text, kind=None, duration_ms=2000):
        cls._queue.append((text, kind or cls.INFO, duration_ms))

    @classmethod
    def update(cls):
        """Call once per frame. Draws the active toast, advances the
        queue, and clears the area when a toast expires."""
        now = time.ticks_ms()

        if cls._current is None:
            if not cls._queue:
                return
            cls._current = cls._queue.pop(0)
            cls._shown_at = now
            cls._draw(cls._current)
            return

        text, kind, duration = cls._current
        if time.ticks_diff(now, cls._shown_at) >= duration:
            cls._clear()
            cls._current = None

    @classmethod
    def _draw(cls, toast):
        text, kind, duration = toast
        y = HEIGHT - cls._height - 6
        x = 10
        w = WIDTH - 20

        draw_rounded_rect(x, y, w, cls._height, 8, SURFACE)
        fill_rect(x + 2, y + cls._height // 2 - 8, 6, 16, kind)  # accent bar
        draw_text8x8(x + 16, y + (cls._height - 8) // 2, text, WHITE, SURFACE)

    @classmethod
    def _clear(cls):
        y = HEIGHT - cls._height - 6
        fill_rect(10, y, WIDTH - 20, cls._height, background)


class UIListView:
    def __init__(self, x, y, w, h, items, item_h=24,
                 bg=SURFACE, fg=WHITE,
                 sel=BLUE, text_x=6,
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


class UIIconButton(_Touchable):
    def __init__(self, x, y, w, h, label, bg=SURFACE_ALT, fg=WHITE, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = label
        self.bg = bg
        self.fg = fg
        self.action = action
        self.badge = None

    def draw(self):
        draw_rounded_rect(self.x, self.y, self.w, self.h, 6, self.bg)
        draw_text_centered(self.x + self.w // 2, self.y + (self.h - 8) // 2,
                            self.label, self.fg, self.bg)
        if self.badge:
            self.badge.draw_at_corner(self.x, self.y, self.w, self.h)

    def handle_touch(self):
        if not self._tap():
            return False
        if self.action:
            self.action()
        return True


class UICheckBox(_Touchable):
    def __init__(self, x, y, label, checked=False, action=None):
        self.x, self.y = x, y
        self.label = label
        self.checked = checked
        self.action = action
        self.size = 18
        self.w = self.h = self.size

    def draw(self):
        fill_rect(self.x, self.y, self.size, self.size, background)
        draw_rounded_rect(self.x, self.y, self.size, self.size, 4, LIGHT_GRAY, filled=False)
        if self.checked:
            draw_rounded_rect(self.x, self.y, self.size, self.size, 4, GREEN)
            draw_line(self.x + 3, self.y + 9, self.x + 7, self.y + 14, WHITE)
            draw_line(self.x + 7, self.y + 14, self.x + 15, self.y + 3, WHITE)
        draw_text8x8(self.x + self.size + 6, self.y + 5, self.label, WHITE, background)

    def handle_touch(self):
        if not self._tap():
            return False
        self.checked = not self.checked
        self.draw()
        if self.action:
            self.action(self.checked)
        return True


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
            draw_circle(self.x, cy, self.r, LIGHT_GRAY)
            if i == self.selected:
                fill_circle(self.x, cy, self.r - 2, BLUE)
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
    def __init__(self, x, y, w, h, tabs, active=0, action=None, badges=None):
        """badges: optional dict {tab_index: NotificationBadge}."""
        self.x, self.y, self.w, self.h = x, y, w, h
        self.tabs = tabs
        self.active = active
        self.action = action
        self.badges = badges or {}

    def draw(self):
        tabw = self.w // len(self.tabs)
        for i, t in enumerate(self.tabs):
            bg = BLUE if i == self.active else SURFACE_ALT
            tx = self.x + i * tabw
            fill_rect(tx, self.y, tabw, self.h, bg)
            draw_text_centered(tx + tabw // 2, self.y + (self.h - 8) // 2, t, WHITE, bg)
            if i in self.badges:
                self.badges[i].draw_at_corner(tx, self.y, tabw, self.h, corner="top-right")

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
    def __init__(self, x, y, value=0, step=1, action=None, min_v=None, max_v=None):
        self.x, self.y = x, y
        self.value = value
        self.step = step
        self.action = action
        self.min_v = min_v
        self.max_v = max_v

    def draw(self):
        draw_rounded_rect(self.x, self.y, 80, 24, 6, SURFACE_ALT)
        draw_text8x8(self.x + 8, self.y + 8, "-", WHITE, SURFACE_ALT)
        draw_text_centered(self.x + 40, self.y + 8, str(self.value), WHITE, SURFACE_ALT)
        draw_text8x8(self.x + 65, self.y + 8, "+", WHITE, SURFACE_ALT)

    def handle_touch(self):
        p = get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + 24 and self.y <= ty <= self.y + 24:
            new_v = self.value - self.step
        elif self.x + 56 <= tx <= self.x + 80 and self.y <= ty <= self.y + 24:
            new_v = self.value + self.step
        else:
            return False

        if self.min_v is not None:
            new_v = max(self.min_v, new_v)
        if self.max_v is not None:
            new_v = min(self.max_v, new_v)

        self.value = new_v
        self.draw()
        if self.action:
            self.action(self.value)
        time.sleep(0.15)
        return True


class UIDivider:
    def __init__(self, x, y, w, color=BORDER):
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
    """NOTE: calibrated_keys below were tuned for a specific screen
    resolution/orientation. After switching resolutions, re-run
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
        draw_rounded_rect(x, y, w, h, 4, self.key_color)
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
