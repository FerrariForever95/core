import time
import urandom
import math
WIDTH  = 320
HEIGHT = 240
def window_close_animation(ui, duration=0.4, fps=60, color=None, ease=True):

    if color is None:
        color = ui.white

    WIDTH = ui.tft.width
    HEIGHT = ui.tft.height

    cx = WIDTH // 2
    cy = HEIGHT // 2

    # Start fully filled
    ui.tft.fill_rectangle(0, 0, WIDTH, HEIGHT, color)

    frames = max(1, int(duration * fps))
    delay = duration / frames

    prev_x0 = 0
    prev_y0 = 0
    prev_x1 = WIDTH - 1
    prev_y1 = HEIGHT - 1

    for i in range(frames + 1):

        t = i / frames
        if ease:
            t = t * t * (3 - 2 * t)  # smoothstep

        # Reverse progression
        t = 1 - t

        w = max(1, int(WIDTH * t))
        h = max(1, int(HEIGHT * t))

        x0 = cx - w // 2
        y0 = cy - h // 2
        x1 = x0 + w - 1
        y1 = y0 + h - 1

        # --- Top strip clear ---
        if y0 > prev_y0:
            ui.tft.fill_rectangle(prev_x0, prev_y0,
                                  prev_x1 - prev_x0 + 1,
                                  y0 - prev_y0,
                                  ui.background)

        # --- Bottom strip clear ---
        if y1 < prev_y1:
            ui.tft.fill_rectangle(prev_x0, y1 + 1,
                                  prev_x1 - prev_x0 + 1,
                                  prev_y1 - y1,
                                  ui.background)

        # --- Left strip clear ---
        if x0 > prev_x0:
            ui.tft.fill_rectangle(prev_x0, y0,
                                  x0 - prev_x0,
                                  y1 - y0 + 1,
                                  ui.background)

        # --- Right strip clear ---
        if x1 < prev_x1:
            ui.tft.fill_rectangle(x1 + 1, y0,
                                  prev_x1 - x1,
                                  y1 - y0 + 1,
                                  ui.background)

        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1

        time.sleep(delay)

def window_open_animation(ui, duration=0.4, fps=60, color=None, ease=True):

    if color is None:
        color = ui.white

    WIDTH = ui.tft.width
    HEIGHT = ui.tft.height

    ui.clear(ui.background)

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

        # --- Top strip ---
        if y0 < prev_y0:
            ui.tft.fill_rectangle(x0, y0, w, prev_y0 - y0, color)

        # --- Bottom strip ---
        if y1 > prev_y1:
            ui.tft.fill_rectangle(x0, prev_y1 + 1, w, y1 - prev_y1, color)

        # --- Left strip ---
        if x0 < prev_x0:
            ui.tft.fill_rectangle(x0, prev_y0, prev_x0 - x0, prev_y1 - prev_y0 + 1, color)

        # --- Right strip ---
        if x1 > prev_x1:
            ui.tft.fill_rectangle(prev_x1 + 1, prev_y0, x1 - prev_x1, prev_y1 - prev_y0 + 1, color)

        prev_x0, prev_y0, prev_x1, prev_y1 = x0, y0, x1, y1

        time.sleep(delay)

def color565(r, g, b):
    """Return RGB565 color value.

    Args:
        r (int): Red value.
        g (int): Green value.
        b (int): Blue value.
    """
    return (r & 0xf8) << 8 | (g & 0xfc) << 3 | b >> 3
# =============================
# UIButton
# =============================
class UIButton:
    def __init__(self, x, y, w, h, label,
                 color=color565(0, 0, 255),
                 text_color=color565(255, 255, 255),
                 margin=5, action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.label = str(label)
        self.color = color
        self.text_color = text_color
        self.margin = margin
        self.action = action

    def draw(self, ui):
        try:
            # ------ DRAW RECT ------
            ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.color)

            # ------ TEXT CALC ------
            char_width = 8
            char_height = 8
            text_width = len(self.label) * char_width
            text_height = char_height

            # Centering
            text_x = self.x + (self.w - text_width) // 2
            text_y = self.y + (self.h - text_height) // 2

            # ------ DRAW TEXT ------
            if self.label:
                ui.tft.draw_text8x8(
                text_x,
                text_y,
                self.label,
                self.text_color,
                self.color
            )

        except Exception as e:
            # 🔥 Detailed crash report — extremely helpful
            print("BTN DRAW ERROR!")
            print("Label:", self.label)
            print("Pos:", self.x, self.y)
            print("Size:", self.w, self.h)
            print("Error:", e)

            # You may also log using your Logger:
            try:
                ui.log.error(
                    message=f"UIButton draw failed ({self.label}): {e}",
                    source="UI"
                )
            except:
                pass

            # Fail gracefully instead of crashing UI
            # Draw a fallback red box
            try:
                ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, color565(255, 0, 0))
            except:
                pass
    def get_touch(self, ui):
        p = ui.get_touch()
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
    def __init__(self, x, y, text, fg=color565(255, 255, 255), bg=None):
        self.x = x
        self.y = y
        self.text = text
        self.fg = fg
        self.bg = bg

    def draw(self, ui):
        ui.tft.draw_text8x8(
            self.x,
            self.y,
            str(self.text),
            self.fg,
            self.bg if self.bg else ui.background
        )
class UIBMPButton:
    def __init__(
        self,
        x,
        y,
        w,
        h,
        bmp,
        *,
        bmp_pressed=None,
        margin=5,
        action=None
    ):
        self.x = x
        self.y = y
        self.w = w
        self.h = h

        self.bmp = bmp
        self.bmp_pressed = bmp_pressed
        self.margin = margin
        self.action = action

        self._pressed = False

    # -------------------------------------------------
    # Draw
    # -------------------------------------------------
    def draw(self, ui):
        try:
            if self._pressed and self.bmp_pressed:
                ui.tft.draw_bmp(
                    self.bmp_pressed,
                    self.x,
                    self.y,
                    self.w,
                    self.h
                )
            else:
                ui.tft.draw_bmp(
                    self.bmp,
                    self.x,
                    self.y,
                    self.w,
                    self.h
                )

        except Exception as e:
            print("BMP BUTTON DRAW ERROR")
            print("Pos:", self.x, self.y)
            print("Size:", self.w, self.h)
            print("Error:", e)

            try:
                ui.log.error(
                    message=f"UIBMPButton draw failed: {e}",
                    source="UI"
                )
            except:
                pass

    # -------------------------------------------------
    # Touch → trigger action (UIButton-compatible)
    # -------------------------------------------------
    def get_touch(self, ui):
        p = ui.get_touch()
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
                    self.draw(ui)

            if self.action:
                self.action()

            return True

        self._pressed = False
        return False

class UIScreen:
    def __init__(self, ui,
                 fg=color565(255, 255, 255),
                 background=None,
                 on_exit=None,
                 taskbarcolor=color565(50, 50, 50),
                 taskbar_text=None,
                 taskbar_text_color=color565(255, 255, 255),
                 taskbar_height=35,
                 *args, **kwargs):

        self.ui = ui
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

    # -------------------------------------------------
    # INTERNAL BACKGROUND (COLOR OR BMP)
    # -------------------------------------------------
    def layer(self,x,y,width,height,color):
        if not self.ui:
            return
        self.ui.tft.fill_rectangle(x, y,width,height, color)
    def _draw_background(self, ui):
        if not ui or not ui.tft or self.background is None:
            return

        if isinstance(self.background, int):
            ui.tft.fill_rectangle(
                0, 0,
                ui.tft.width,
                ui.tft.height,
                self.background
            )
        elif isinstance(self.background, str):
            ui.tft.draw_bmp(
                self.background,
                0, 0,
                max_w=ui.tft.width,
                max_h=ui.tft.height
            )
    def openscreen(self):
        window_open_animation(self.ui, duration=0.4, fps=60, color=self.background, ease=True)
    def closescreen(self):
        window_close_animation(self.ui, duration=0.4, fps=60, color=None, ease=True)
        
    def start(self, ui):
        window_open_animation(self.ui, duration=0.4, fps=60, color=self.background, ease=True)
        self.ui = ui

        # background (if any)
        self._draw_background(ui)

        # taskbar
        h = self.taskbar_height
        ui.tft.fill_rectangle(0, 0, ui.tft.width, h, self.taskbarcolor)

        # exit button
        btn = 30
        x0 = ui.tft.width - btn - 2
        y0 = 2
        self.exit_box = (x0, y0, btn, btn)

        ui.tft.fill_rectangle(x0, y0, btn, btn, color565(0, 0, 200))
        ui.tft.draw_line(x0+5, y0+5, x0+btn-5, y0+btn-5, color565(255,255,255))
        ui.tft.draw_line(x0+btn-5, y0+5, x0+5, y0+btn-5, color565(255,255,255))

        # title
        if self.taskbar_text:
            tw = len(self.taskbar_text) * 8
            ui.tft.draw_text8x8(
                (ui.tft.width - tw)//2,
                (h - 8)//2,
                self.taskbar_text,
                self.taskbar_text_color,
                self.taskbarcolor
            )

        ui.active_screen = self
    def taskbar(self,ui,taskbarcolor,taskbar_text,taskbar_text_color,taskbar_height=35):
        self.taskbar_text=taskbar_text
        self.taskbarcolor=taskbarcolor
        self.taskbar_text_color=taskbar_text_color 
        self.taskbar_height = taskbar_height
        self.ui=ui
        self.ui.tft.fill_rectangle(0, 0, ui.tft.width,self. taskbar_height, self.taskbarcolor)
        if self.taskbar_text:
            text_w = len(self.taskbar_text) * 8  # assuming 8x8 font size
            x_center = (ui.tft.width - text_w) // 2
            y_center = (self.taskbar_height - 8) // 2
            self.ui.tft.draw_text8x8(x_center, y_center, self.taskbar_text, self.taskbar_text_color, self.taskbarcolor)
    def draw_gradient(self,ui, color1, color2, angle=0, block_size=1):
        """
        Ultra-smooth linear and diagonal gradient with cubic interpolation and dithering.
        Supported angles: 0, 45, 90, 135, 180, 270.
        """
        self.ui=ui
        if not ui or not ui.tft:
            return

        w, h = ui.tft.width, ui.tft.height

        # --- Helper: unpack RGB565 to 8-bit RGB ---
        def unpack_rgb565(c):
            r = ((c >> 11) & 0x1F) << 3
            g = ((c >> 5) & 0x3F) << 2
            b = (c & 0x1F) << 3
            return r, g, b

        # --- Helper: pack RGB888 to RGB565 ---
        def pack_rgb565(r, g, b):
            r = int(max(0, min(255, r)))
            g = int(max(0, min(255, g)))
            b = int(max(0, min(255, b)))
            return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

        # --- Extract RGB components ---
        r1, g1, b1 = unpack_rgb565(color1)
        r2, g2, b2 = unpack_rgb565(color2)

        # --- Adaptive gamma curve ---
        diff = abs((r1 + g1 + b1) - (r2 + g2 + b2)) / 765
        gamma = 2.0 + diff * 0.3

        r1_g, g1_g, b1_g = [(x / 255) ** gamma for x in (r1, g1, b1)]
        r2_g, g2_g, b2_g = [(x / 255) ** gamma for x in (r2, g2, b2)]

        # --- Cubic blend for smoothness ---
        def cubic_blend(a, b, t):
            t2 = t * t
            t3 = t2 * t
            return a + (b - a) * (3 * t2 - 2 * t3)

        # --- Linear directions ---
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

                # subtle dithering
                r += (urandom.getrandbits(2) - 2) / 512
                g += (urandom.getrandbits(2) - 2) / 512
                b += (urandom.getrandbits(2) - 2) / 512

                color = pack_rgb565(r * 255, g * 255, b * 255)
                if vertical:
                    self.ui.tft.fill_rectangle(0, i, w, block_size, color)
                else:
                    self.ui.tft.fill_rectangle(i, 0, block_size, h, color)
            return

        # --- Diagonal (45°, 135°) directions ---
        if angle in (45, 135):
            for y in range(0, h, block_size):
                for x in range(0, w, block_size):
                    if angle == 45:
                        t = (x + (h - y)) / (w + h)
                    else:  # 135°
                        t = (x + y) / (w + h)
                    t = max(0.0, min(1.0, t))

                    r = pow(cubic_blend(r1_g, r2_g, t), 1 / gamma)
                    g = pow(cubic_blend(g1_g, g2_g, t), 1 / gamma)
                    b = pow(cubic_blend(b1_g, b2_g, t), 1 / gamma)

                    # dither
                    r += (urandom.getrandbits(2) - 2) / 512
                    g += (urandom.getrandbits(2) - 2) / 512
                    b += (urandom.getrandbits(2) - 2) / 512

                    color = pack_rgb565(r * 255, g * 255, b * 255)
                    ui.tft.fill_rectangle(x, y, block_size, block_size, color)
            return

        self.log.warning(message=" Unsupported angle. Use 0, 45, 90, 135, 180, 270.",source="UI")       
    # -------------------------------------------------
    # START WITHOUT EXIT (KERNEL / HOME)
    # -------------------------------------------------
    def start_withoutexit(self, ui):
        self.ui = ui
        self.exit_box = None

        # IMPORTANT: do NOT wipe wallpaper unless background is set
        self._draw_background(ui)

        h = self.taskbar_height
        ui.tft.fill_rectangle(0, 0, ui.tft.width, h, self.taskbarcolor)

        if self.taskbar_text:
            tw = len(self.taskbar_text) * 8
            ui.tft.draw_text8x8(
                (ui.tft.width - tw)//2,
                (h - 8)//2,
                self.taskbar_text,
                self.taskbar_text_color,
                self.taskbarcolor
            )

        ui.active_screen = self

    # -------------------------------------------------
    # TOUCH
    # -------------------------------------------------
    def check(self, ui):
        if not self.exit_box or not self.buttons_enabled:
            return False

        p = ui.get_touch()
        if not p:
            return False

        tx, ty = p
        x0, y0, w, h = self.exit_box

        if x0 <= tx <= x0+w and y0 <= ty <= y0+h:
            if self.on_exit:
                self.on_exit(*self.exit_args, **self.exit_kwargs)
            return True
        return False

class UITextBoxView:
    def __init__(self, x, y, w, h,
                 text=None,
                 fg=color565(255, 255, 255),
                 bg=color565(0, 0, 0),
                 padding=4):

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

        # scroll (pixels)
        self.scroll_px = 0

        # touch state
        self._touch_active = False
        self._y0 = 0
        self._s0_px = 0

        self.enabled = True
        self.lines = []

        if text:
            self.set_text(text)

    # -------------------------------------------------
    def _inside(self, x, y):
        return (
            self.x <= x < self.x + self.w and
            self.y <= y < self.y + self.h
        )

    # -------------------------------------------------
    def set_text(self, text):
        # HARD RESET — text owns scroll
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

    # -------------------------------------------------
    def draw(self, ui):
        tft = ui.tft
        tft.fill_rectangle(self.x, self.y, self.w, self.h, self.bg)

        if not self.enabled or not self.lines:
            return

        total_h = len(self.lines) * self.char_h
        view_h = self.rows * self.char_h
        max_scroll = total_h - view_h
        if max_scroll < 0:
            max_scroll = 0

        if self.scroll_px < 0:
            self.scroll_px = 0
        elif self.scroll_px > max_scroll:
            self.scroll_px = max_scroll

        first = self.scroll_px // self.char_h
        offset = self.scroll_px % self.char_h

        ty = self.y + self.padding - offset
        x = self.x + self.padding

        end = first + self.rows + 1
        if end > len(self.lines):
            end = len(self.lines)

        for i in range(first, end):
            line = self.lines[i]
            if line:
                tft.draw_text8x8(x, ty, line, self.fg, self.bg)
            ty += self.char_h

    # -------------------------------------------------
    def handle_touch(self, ui):
        if not self.enabled or not self.lines:
            return False

        p = ui.get_touch()

        # --- TOUCH START ---
        if p and not self._touch_active:
            tx, ty = p
            if not self._inside(tx, ty):
                return False

            self._touch_active = True
            self._y0 = ty
            self._s0_px = self.scroll_px
            return True

        # --- DRAG ---
        if p and self._touch_active:
            _, ty = p
            self.scroll_px = self._s0_px + (self._y0 - ty)
            self.draw(ui)
            return True

        # --- RELEASE ---
        if not p and self._touch_active:
            self._touch_active = False
            return True

        return False


class DialogBox:
    def __init__(
        self,
        ui,
        *,
        title="Dialog",
        message="",
        btn_yes="Yes",
        btn_no="No",
        on_yes=None,
        on_no=None,
        on_exit=None
    ):
        self.ui = ui
        self.on_yes = on_yes
        self.on_no = on_no
        self.on_exit = on_exit
        self.btn_yes = btn_yes
        self.btn_no = btn_no
        self.title = title
        self.message = message

        self._result = None
        self._running = False

    # ------------------------------------------------
    # Internal actions
    # ------------------------------------------------
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

    # ------------------------------------------------
    # Public API
    # ------------------------------------------------
    def show(self):
        if self._running:
            return None

        ui = self.ui

        W = 156
        H = 85
        X = (ui.tft.width - W) // 2
        Y = (ui.tft.height - H) // 2

        self.x, self.y, self.w, self.h = X, Y, W, H

        self.texts = []
        self.buttons = []

        ui.tft.fill_rectangle(X, Y, W, H, color565(255, 255, 255))
        ui.tft.fill_rectangle(X, Y, W, 17, color565(80, 80, 80))

        self.texts.append(
            UIText(X + 7, Y + 6, self.title,
                   fg=color565(255, 255, 255),
                   bg=color565(80, 80, 80))
        )

        self.texts.append(
            UIText(X + 10, Y + 39, self.message,
                   fg=color565(0, 0, 0),
                   bg=color565(255, 255, 255))
        )

        self.buttons.append(
            UIButton(
                X + W - 16, Y + 2, 13, 13,
                label="X",
                color=color565(0, 0, 255),
                text_color=color565(255, 255, 255),
                margin=3,
                action=self._exit
            )
        )

        self.buttons.append(
            UIButton(
                X + 18, Y + H - 28, 54, 21,
                label=self.btn_yes,
                color=color565(212, 212, 212),
                text_color=color565(0, 0, 0),
                margin=5,
                action=self._yes
            )
        )

        self.buttons.append(
            UIButton(
                X + W - 18 - 51, Y + H - 28, 51, 21,
                label=self.btn_no,
                color=color565(212, 212, 212),
                text_color=color565(0, 0, 0),
                margin=5,
                action=self._no
            )
        )

        for t in self.texts:
            t.draw(ui)
        for b in self.buttons:
            b.draw(ui)

        self._running = True
        while self._running:
            for b in self.buttons:
                b.get_touch(ui)
            time.sleep(0.02)

        return self._result
class UIToggleSwitch:
    def __init__(self, x, y, w=50, h=26, state=False,
                 on_color=color565(0,180,0),
                 off_color=color565(120,120,120),
                 knob=color565(255,255,255),
                 action=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.state = state
        self.on_color = on_color
        self.off_color = off_color
        self.knob = knob
        self.action = action

    def draw(self, ui):
        r = self.h // 2
        bg = self.on_color if self.state else self.off_color

        ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, ui.background)

        ui.tft.fill_circle(self.x + r, self.y + r, r, bg)
        ui.tft.fill_circle(self.x + self.w - r - 1, self.y + r, r, bg)
        ui.tft.fill_rectangle(self.x + r, self.y, self.w - 2*r, self.h, bg)

        kx = self.x + self.w - r - 1 if self.state else self.x + r
        ui.tft.fill_circle(kx, self.y + r, r - 2, self.knob)

    def handle_touch(self, ui):
        p = ui.get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y <= ty <= self.y + self.h:
            self.state = not self.state
            self.draw(ui)
            if self.action:
                self.action(self.state)
            time.sleep(0.15)
            return True
        return False
class UISlider:
    def __init__(self, x, y, w,
                 min_v=0, max_v=100, value=0,
                 track=color565(80,80,80),
                 fill=color565(0,150,255),
                 knob=color565(255,255,255),
                 action=None):
        self.x, self.y, self.w = x, y, w
        self.h = 10
        self.min = min_v
        self.max = max_v
        self.value = value
        self.track = track
        self.fill = fill
        self.knob = knob
        self.action = action

    def draw(self, ui):
        ui.tft.fill_rectangle(
            self.x - 8, self.y - 8,
            self.w + 16, self.h + 16,
            ui.background
        )

        ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.track)

        pos = int((self.value - self.min) * self.w / (self.max - self.min))
        ui.tft.fill_rectangle(self.x, self.y, pos, self.h, self.fill)

        ui.tft.fill_circle(self.x + pos, self.y + self.h // 2, 7, self.knob)

    def handle_touch(self, ui):
        p = ui.get_touch()
        if not p:
            return False
        tx, ty = p
        if self.x <= tx <= self.x + self.w and self.y - 6 <= ty <= self.y + self.h + 6:
            rel = max(0, min(self.w, tx - self.x))
            self.value = self.min + int(rel * (self.max - self.min) / self.w)
            self.draw(ui)
            if self.action:
                self.action(self.value)
            return True
        return False
class UIPanel:
    def __init__(self,
                 x, y, w, h,
                 title=None,
                 bg=None,
                 border=None,
                 title_fg=None,
                 title_bg=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.title = title

        # colors (defaults are neutral)
        self.bg = bg if bg is not None else color565(30, 30, 30)
        self.border = border if border is not None else color565(80, 80, 80)
        self.title_fg = title_fg if title_fg is not None else color565(200, 200, 200)
        self.title_bg = title_bg if title_bg is not None else self.bg

    # --------------------------
    # Draw
    # --------------------------
    def draw(self, ui):
        ui.tft.fill_rectangle(
            self.x, self.y, self.w, self.h, self.bg
        )

        ui.tft.draw_rectangle(
            self.x, self.y, self.w, self.h, self.border
        )

        if self.title:
            ui.tft.draw_text8x8(
                self.x + 6,
                self.y + 6,
                self.title,
                self.title_fg,
                self.title_bg
            )

    # --------------------------
    # Opening animation
    # --------------------------
    def open(self, ui, steps=8, delay_ms=1):
        tw = self.w
        th = self.h

        for i in range(1, steps + 1):
            cw = (tw * i) // steps
            ch = (th * i) // steps

            ui.tft.fill_rectangle(
                self.x, self.y, cw, ch, self.bg
            )

            ui.tft.draw_rectangle(
                self.x, self.y, cw, ch, self.border
            )

            if delay_ms:
                import time
                time.sleep_ms(delay_ms)

        self.draw(ui)

class UIProgressBar:
    def __init__(self, x, y, w, h=12,
                 value=0,
                 bg=color565(50,50,50),
                 fg=color565(0,200,0)):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.value = value
        self.bg = bg
        self.fg = fg

    def set(self, ui, val):
        self.value = max(0, min(100, val))
        self.draw(ui)

    def draw(self, ui):
        ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.bg)
        fw = int(self.w * self.value / 100)
        if fw:
            ui.tft.fill_rectangle(self.x, self.y, fw, self.h, self.fg)

class UIStatusIndicator:
    OK = 0
    WARN = 1
    ERR = 2

    def __init__(self, x, y, r=6, state=0):
        self.x, self.y, self.r = x, y, r
        self.state = state

    def draw(self, ui):
        if self.state == self.OK:
            c = color565(0,200,0)
        elif self.state == self.WARN:
            c = color565(255,165,0)
        else:
            c = color565(200,0,0)
        ui.tft.fill_circle(self.x, self.y, self.r, c)
class UIToast:
    def __init__(self, text, duration=2):
        self.text = text
        self.duration = duration

    def show(self, ui):
        h = 26
        y = ui.tft.height - h - 4
        ui.tft.fill_rectangle(10, y, ui.tft.width - 20, h, color565(40,40,40))
        ui.tft.draw_text8x8(16, y + 9, self.text, color565(255,255,255))
        time.sleep(self.duration)
        ui.clear()
class UIListView:
    def __init__(self, x, y, w, h, items,
                 item_h=24,
                 bg=color565(20, 20, 20),
                 fg=color565(255, 255, 255),
                 sel=color565(0, 120, 255),
                 text_x=6,
                 highlight=False,
                 action=None):

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

    # -------------------------------------------------
    def _inside(self, x, y):
        return (
            self.x <= x < self.x + self.w and
            self.y <= y < self.y + self.h
        )

    # -------------------------------------------------
    def draw(self, ui):
        ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.bg)

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
            ui.tft.fill_rectangle(self.x, iy, self.w, self.item_h, row_bg)

            ty = iy + (self.item_h - 8) // 2
            if self.y <= ty < self.y + self.h:
                ui.tft.draw_text8x8(
                    self.x + self.text_x,
                    ty,
                    str(self.items[idx]),
                    self.fg,
                    row_bg
                )

    # -------------------------------------------------
    def handle_touch(self, ui):
        if not self.enabled:
            return False

        p = ui.get_touch()

        # ---- touch start ----
        if p and self._y0 is None:
            tx, ty = p
            if not self._inside(tx, ty):
                return False

            self._y0 = ty
            self._s0 = self.scroll
            self._moved = False
            return True

        # ---- drag ----
        if p and self._y0 is not None:
            _, ty = p
            dy = self._y0 - ty

            if abs(dy) > 6:
                self._moved = True

            self.scroll = self._s0 + dy
            self.draw(ui)
            return True

        # ---- release ----
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
                        self.draw(ui)
                        if self.action:
                            self.action(idx, self.items[idx])

            return True

        return False
# 
class UIInputTextBox:
    def __init__(self, x, y, w, h,
                 ui,
                 keyboard,
                 fg,
                 bg,
                 padding=4,
                 blink_ms=500):

        self.ui = ui
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

        # draw background once
        self.ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.bg)

    # -------------------------------------------------
    # HIT TEST
    # -------------------------------------------------
    def _inside(self, x, y):
        return (
            self.x <= x < self.x + self.w and
            self.y <= y < self.y + self.h
        )

    # -------------------------------------------------
    # DRAW (STRICT: NEVER draws empty text)
    # -------------------------------------------------
    def draw(self, force=False):
        buf = self.kb.buffer

        if not buf:
            return  # ABSOLUTE RULE: never draw ""

        now = time.ticks_ms()
        blink = False

        if time.ticks_diff(now, self._last_blink) > self._blink_ms:
            self._caret_on = not self._caret_on
            self._last_blink = now
            blink = True

        if not force and buf == self._last_buf and not blink:
            return

        self._last_buf = buf

        # clear background
        self.ui.tft.fill_rectangle(self.x, self.y, self.w, self.h, self.bg)

        visible = buf[-self.cols:]
        text = visible + ("|" if self._caret_on else "")

        # SAFETY: still guard
        if not text.strip("|"):
            return

        self.ui.tft.draw_text8x8(
            self.x + self.padding,
            self.y + (self.h - self.char_h) // 2,
            text,
            self.fg,
            self.bg
        )

    # -------------------------------------------------
    # TOUCH
    # -------------------------------------------------
    def handle_touch(self):
        if not self.enabled:
            return False

        p = self.ui.get_touch()
        if not p:
            return False

        tx, ty = p
        if not self._inside(tx, ty):
            return False

        # activate keyboard
        self.kb.open()
        return True

class UIIconButton:
    def __init__(self, x, y, w, h, label,
                 bg=color565(60,60,60),
                 fg=color565(255,255,255),
                 action=None):
        self.x,self.y,self.w,self.h=x,y,w,h
        self.label=label
        self.bg=bg
        self.fg=fg
        self.action=action

    def draw(self,ui):
        ui.tft.fill_rectangle(self.x,self.y,self.w,self.h,self.bg)
        tw=len(self.label)*8
        ui.tft.draw_text8x8(
            self.x+(self.w-tw)//2,
            self.y+(self.h-8)//2,
            self.label,
            self.fg,
            self.bg
        )

    def handle_touch(self,ui):
        p=ui.get_touch()
        if not p: return False
        tx,ty=p
        if self.x<=tx<=self.x+self.w and self.y<=ty<=self.y+self.h:
            if self.action: self.action()
            time.sleep(0.15)
            return True
        return False


# -----------------------------
# Checkbox
# -----------------------------
class UICheckBox:
    def __init__(self,x,y,label,checked=False,action=None):
        self.x,self.y=x,y
        self.label=label
        self.checked=checked
        self.action=action
        self.size=18

    def draw(self,ui):
        ui.tft.fill_rectangle(self.x,self.y,self.size,self.size,ui.background)
        ui.tft.draw_rectangle(self.x,self.y,self.size,self.size,color565(200,200,200))
        if self.checked:
            ui.tft.draw_line(self.x+3,self.y+9,self.x+7,self.y+14,color565(0,200,0))
            ui.tft.draw_line(self.x+7,self.y+14,self.x+15,self.y+3,color565(0,200,0))
        ui.tft.draw_text8x8(
            self.x+self.size+6,
            self.y+5,
            self.label,
            color565(255,255,255),
            ui.background
        )

    def handle_touch(self,ui):
        p=ui.get_touch()
        if not p: return False
        tx,ty=p
        if self.x<=tx<=self.x+self.size and self.y<=ty<=self.y+self.size:
            self.checked=not self.checked
            self.draw(ui)
            if self.action: self.action(self.checked)
            time.sleep(0.15)
            return True
        return False


# -----------------------------
# Radio Button Group
# -----------------------------
class UIRadioGroup:
    def __init__(self,x,y,options,selected=0,action=None):
        self.x,self.y=x,y
        self.options=options
        self.selected=selected
        self.action=action
        self.r=6
        self.spacing=22

    def draw(self,ui):
        for i,opt in enumerate(self.options):
            cy=self.y+i*self.spacing
            ui.tft.draw_circle(self.x,cy,self.r,color565(200,200,200))
            if i==self.selected:
                ui.tft.fill_circle(self.x,cy,self.r-2,color565(0,180,255))
            ui.tft.draw_text8x8(
                self.x+14,
                cy-4,
                opt,
                color565(255,255,255),
                ui.background
            )

    def handle_touch(self,ui):
        p=ui.get_touch()
        if not p: return False
        tx,ty=p
        for i in range(len(self.options)):
            cy=self.y+i*self.spacing
            if abs(tx-self.x)<=self.r+4 and abs(ty-cy)<=self.r+4:
                self.selected=i
                self.draw(ui)
                if self.action: self.action(i,self.options[i])
                time.sleep(0.15)
                return True
        return False


# -----------------------------
# Tab Bar
# -----------------------------
class UITabBar:
    def __init__(self,x,y,w,h,tabs,active=0,action=None):
        self.x,self.y,self.w,self.h=x,y,w,h
        self.tabs=tabs
        self.active=active
        self.action=action

    def draw(self,ui):
        tabw=self.w//len(self.tabs)
        for i,t in enumerate(self.tabs):
            bg=color565(0,120,255) if i==self.active else color565(80,80,80)
            ui.tft.fill_rectangle(self.x+i*tabw,self.y,tabw,self.h,bg)
            tw=len(t)*8
            ui.tft.draw_text8x8(
                self.x+i*tabw+(tabw-tw)//2,
                self.y+(self.h-8)//2,
                t,
                color565(255,255,255),
                bg
            )

    def handle_touch(self,ui):
        p=ui.get_touch()
        if not p: return False
        tx,ty=p
        if self.x<=tx<=self.x+self.w and self.y<=ty<=self.y+self.h:
            idx=(tx-self.x)//(self.w//len(self.tabs))
            if idx!=self.active and idx<len(self.tabs):
                self.active=idx
                self.draw(ui)
                if self.action: self.action(idx,self.tabs[idx])
                time.sleep(0.15)
            return True
        return False


# -----------------------------
# Numeric Stepper (+ / -)
# -----------------------------
class UIStepper:
    def __init__(self,x,y,value=0,step=1,action=None):
        self.x,self.y=x,y
        self.value=value
        self.step=step
        self.action=action

    def draw(self,ui):
        ui.tft.fill_rectangle(self.x,self.y,80,24,color565(50,50,50))
        ui.tft.draw_text8x8(self.x+6,self.y+8,"-",color565(255,255,255),color565(50,50,50))
        ui.tft.draw_text8x8(self.x+32,self.y+8,str(self.value),color565(255,255,255),color565(50,50,50))
        ui.tft.draw_text8x8(self.x+64,self.y+8,"+",color565(255,255,255),color565(50,50,50))

    def handle_touch(self,ui):
        p=ui.get_touch()
        if not p: return False
        tx,ty=p
        if self.x<=tx<=self.x+24 and self.y<=ty<=self.y+24:
            self.value-=self.step
        elif self.x+56<=tx<=self.x+80 and self.y<=ty<=self.y+24:
            self.value+=self.step
        else:
            return False
        self.draw(ui)
        if self.action: self.action(self.value)
        time.sleep(0.15)
        return True


# -----------------------------
# Divider
# -----------------------------
class UIDivider:
    def __init__(self,x,y,w,color=color565(100,100,100)):
        self.x,self.y,self.w=x,y,w
        self.color=color

    def draw(self,ui):
        ui.tft.draw_hline(self.x,self.y,self.w,self.color)
class UIScreenAnimator:
    """
    Lightweight screen animator for Zeno OS.
    Uses vertical slide + easing.
    NO redraw logic inside – only offset control.
    """

    def __init__(self, ui):
        self.ui = ui
        self.mode = None
        self.t0 = 0
        self.duration = 0
        self.running = False
        self.start_offset = 0
        self.end_offset = 0

    # ---------- timing ----------
    def _now(self):
        return time.ticks_ms()

    def _progress(self):
        return min(1.0,
            time.ticks_diff(self._now(), self.t0) / self.duration
        )

    # ---------- easing ----------
    def _ease_out(self, t):
        return 1 - (1 - t) ** 3

    def _ease_in(self, t):
        return t ** 3

    # ---------- animations ----------
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

    # ---------- frame update ----------
    def update(self):
        if not self.running:
            return False

        t = self._progress()

        if self.mode == "open":
            e = self._ease_out(t)
        elif self.mode == "close":
            e = self._ease_in(t)
        else:  # boot
            e = self._ease_out(t)

        offset = int(
            self.start_offset +
            (self.end_offset - self.start_offset) * e
        )

        self.ui.anim_offset_y = offset

        if t >= 1.0:
            self.running = False
            self.ui.anim_offset_y = 0
            return False

        return True


class VirtualKeyboard:
    TOUCH_RADIUS = 12
    DEBOUNCE_MS = 200  # 0.2s

    # === FULL CALIBRATION BLOCK (ABC + 123/SYMBOLS) ===
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

        # 123 / symbols page:
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

    def __init__(self, ui, width=320, height=120,
                 text_color=None, key_color=None, border_color=None,
                 bg_color=None, ok_action=None):

        self.ui = ui
        self.width = width
        self.height = height

        self.text_color = text_color or ui.white
        self.key_color = key_color or ui.gray
        self.border_color = border_color or ui.dark_gray
        self.bg_color = bg_color or ui.gray

        self.buffer = ""
        self.case_upper = False
        self.symbol_mode = False
        self.ok_action = ok_action

        self.x = 0
        self.y = ui.tft.height - height

        self.key_buttons = []
        self.last_touch_ms = 0

        self.active = True   # NEW: keyboard enabled/disabled

        self._draw_keys()

    # -------- enable/disable --------
    def close(self):
        """Hide keyboard and disable input."""
        self.active = False
        self.ui.tft.fill_rectangle(self.x, self.y, self.width, self.height, self.ui.black)

    def open(self):
        """Show keyboard again."""
        self.active = True
        self._draw_keys()

    # -------- helpers --------
    def _canon(self, label):
        if (len(label) == 1) and label.isalpha():
            return label.upper()
        return label

    # -------- layout --------
    def _get_layout(self):
        if self.symbol_mode:
            return [
                ['1','2','3','4','5','6','7','8','9','0'],
                ['-','/','\\',':',';','(',')','_','|','+'],
                ['.','@','#','$','&','%','ABC'],
                ['SPACE','DEL','OK'],
            ]

        layout = [
            ['Q','W','E','R','T','Y','U','I','O','P'],
            ['A','S','D','F','G','H','J','K','L'],
            ['Aa','Z','X','C','V','B','N','M','DEL'],
            ['123','SPACE','OK'],
        ]

        if not self.case_upper:
            lower = []
            for row in layout:
                new_row = []
                for k in row:
                    new_row.append(k.lower() if (len(k)==1 and k.isalpha()) else k)
                lower.append(new_row)
            return lower

        return layout

    # -------- draw keyboard --------
    def _draw_keys(self):
        self.ui.tft.fill_rectangle(self.x, self.y, self.width, self.height, self.bg_color)
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
        self.ui.tft.fill_rectangle(x, y, w, h, self.key_color)
        self.ui.tft.draw_rectangle(x, y, w, h, self.border_color)
        tx = x + (w // 2) - (len(label) * 4)
        ty = y + (h // 2) - 4
        self.ui.tft.draw_text8x8(tx, ty, label, self.text_color, self.key_color)

    def _highlight_key(self, key_btn):
        x, y, w, h = key_btn['x'], key_btn['y'], key_btn['w'], key_btn['h']
        self.ui.tft.fill_rectangle(x, y, w, h, self.ui.light_gray)
        time.sleep(0.05)
        self._draw_key(x, y, w, h, key_btn['label'])

    # -------- touch handling --------
    def check_touch(self,ui):
        if not self.active:     # NEW: disable input completely
            return

        touch = self.ui.get_touch()
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
            d2 = dx*dx + dy*dy
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

    # -------- logic --------
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

    # -------- buffer --------
    def get_buffer(self, clear=False):
        out = self.buffer
        if clear:
            self.buffer = ""
        return out

    def clear_buffer(self):
        self.buffer = ""

class VKTouchCalibrator:
    """
    Touch calibrator for VirtualKeyboard.

    - Calibrates ABC layout (whatever keys are drawn there)
    - Calibrates 123/symbol layout (whatever keys are drawn there)
    - Takes multiple samples per key and averages them
    - Prints a final calibrated_keys[] block to paste into VirtualKeyboard
    """

    def __init__(self, ui, vk, samples_per_key=5):
        """
        ui              : HUIModule instance (with ui.tft and ui.get_touch())
        vk              : VirtualKeyboard instance (already created)
        samples_per_key : how many touches per key to average
        """
        self.ui = ui
        self.tft = ui.tft
        self.vk = vk
        self.samples_per_key = samples_per_key

        # colors (fallbacks if not defined on ui)
        self.bg = getattr(ui, "background", 0)
        self.highlight = getattr(ui, "light_gray", 0x7BEF)  # mid gray-ish
        self.border = getattr(ui, "white", 0xFFFF)
        self.text = getattr(ui, "white", 0xFFFF)

    # ------------------ small UI helpers ------------------

    def _msg(self, text):
        """Draw a small status message at top of screen."""
        # simple banner area at top
        self.tft.fill_rectangle(0, 0, self.tft.width, 16, self.bg)
        self.tft.draw_text8x8(2, 4, text, self.text, self.bg)

    def _wait_for_touch(self):
        """Block until a touch is detected, return (x, y)."""
        while True:
            p = self.ui.get_touch()
            if p:
                return p
            time.sleep_ms(10)

    def _wait_for_release(self):
        """Block until touch is released (no touch)."""
        while True:
            p = self.ui.get_touch()
            if not p:
                return
            time.sleep_ms(10)

    def _highlight_button(self, btn):
        """Visual highlight around a specific key button."""
        x = btn["x"]
        y = btn["y"]
        w = btn["w"]
        h = btn["h"]
        # fill with highlight color + border
        self.tft.fill_rectangle(x, y, w, h, self.highlight)
        self.tft.draw_rectangle(x, y, w, h, self.border)

    def _redraw_button(self, btn):
        """Ask keyboard to redraw this button normally."""
        self.vk._draw_key(btn["x"], btn["y"], btn["w"], btn["h"], btn["label"])

    def _find_button_by_label(self, label):
        """Return key_buttons entry with given label or None."""
        for btn in self.vk.key_buttons:
            if btn["label"] == label:
                return btn
        return None

    def _collect_unique_labels(self):
        """Collect unique non-empty labels from current vk.key_buttons."""
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

    # ------------------ core calibration logic ------------------

    def _calibrate_current_layout(self, layout_name):
        """
        Calibrate all keys in the currently drawn layout.

        layout_name: string just used for logs ("ABC" or "123/sym").
        """
        results = []

        labels = self._collect_unique_labels()
        self._msg("Calibrating {} layout...".format(layout_name))
        time.sleep_ms(400)

        for key in labels:
            btn = self._find_button_by_label(key)
            if not btn:
                continue

            # highlight selected key
            self._highlight_button(btn)
            self._msg("Touch '{}' {}x".format(key, self.samples_per_key))

            xs = []
            ys = []

            for i in range(self.samples_per_key):
                # wait for touch
                p = self._wait_for_touch()
                xs.append(p[0])
                ys.append(p[1])

                # simple tiny visual feedback pause
                self._wait_for_release()
                time.sleep_ms(80)

            # restore button drawing
            self._redraw_button(btn)

            # compute average
            avg_x = sum(xs) // len(xs)
            avg_y = sum(ys) // len(ys)

            print("[VKTouchCalibrator] {} '{}' avg: ({}, {})"
                  .format(layout_name, key, avg_x, avg_y))

            results.append({
                "key": key,
                "x": avg_x,
                "y": avg_y
            })

            time.sleep_ms(150)

        return results

    def run(self):
        """
        Run full calibration:

        1) ABC layout (symbols off)
        2) 123/symbol layout (symbols on)

        Prints final calibrated_keys[] block.
        Returns the list of dicts.
        """
        # clear banner area once
        self.tft.fill_rectangle(0, 0, self.tft.width, 16, self.bg)

        print("[VKTouchCalibrator] Starting calibration...")
        self._msg("Starting VK calibration...")
        time.sleep_ms(500)

        # --- layout 1: ABC ---
        self.vk.symbol_mode = False
        self.vk.case_upper = True
        self.vk._draw_keys()
        res_abc = self._calibrate_current_layout("ABC")

        # --- layout 2: 123 / symbols ---
        self.vk.symbol_mode = True
        self.vk.case_upper = True
        self.vk._draw_keys()
        res_sym = self._calibrate_current_layout("123/sym")

        all_res = res_abc + res_sym

        # Deduplicate per key name (keep first occurrence)
        final = []
        seen = set()
        for item in all_res:
            k = item["key"]
            if k in seen:
                continue
            seen.add(k)
            final.append(item)

        # Pretty-print final block
        print("\n[VKTouchCalibrator] Done. Full calibrated_keys block:\n")
        print("calibrated_keys = [")
        for item in final:
            print("    {{'key': {!r}, 'x': {}, 'y': {}}},".format(
                item["key"], item["x"], item["y"]
            ))
        print("]\n")
        print("[VKTouchCalibrator] Paste into VirtualKeyboard.calibrated_keys if you want to use it")

        self._msg("Calibration complete.")
        return final

class HTML:
    TASKBAR_H = 35
    MARGIN = 6

    FONT_MAP = {
        'h1': 8,
        'h2': 8,
        'p': 8,
    }

    def __init__(self, ui, title="HTML"):
        self.ui = ui
        self.screen = UIScreen(
            ui,
            taskbar_text=title,
            taskbarcolor=color565(40, 40, 40)
        )

        self.styles = {
            'body': {
                'color': color565(0, 0, 0),
                'bg': color565(255, 255, 255)
            },
            'p': {
                'color': color565(0, 0, 0)
            },
            'h1': {
                'color': color565(0, 0, 0)
            }
        }

        self.x = self.MARGIN
        self.y = self.TASKBAR_H + self.MARGIN
        self.cur = 'p'

    # --------------------------
    # Public
    # --------------------------
    def open(self, path):
        self.screen.start(self.ui)
        self._clear_body()
        html = self._read(path)
        self._parse(html)

    # --------------------------
    # Core
    # --------------------------
    def _read(self, path):
        with open(path, "r") as f:
            return f.read()

    def _clear_body(self):
        bg = self.styles['body']['bg']
        self.ui.tft.fill_rectangle(
            0,
            self.TASKBAR_H,
            self.ui.tft.width,
            self.ui.tft.height - self.TASKBAR_H,
            bg
        )

    # --------------------------
    # Parser (NO regex)
    # --------------------------
    def _parse(self, html):
        i = 0
        ln = len(html)

        while i < ln:
            if html[i] == "<":
                j = html.find(">", i)
                if j == -1:
                    break
                tag = html[i+1:j].strip().lower()
                self._tag(tag)
                i = j + 1
            else:
                j = html.find("<", i)
                if j == -1:
                    j = ln
                text = html[i:j]
                self._text(text)
                i = j

    # --------------------------
    # Tags
    # --------------------------
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

    # --------------------------
    # Text
    # --------------------------
    def _text(self, text):
        if not text.strip():
            return

        fg = self.styles[self.cur]['color']
        bg = self.styles['body']['bg']

        for word in text.split(" "):
            w = (len(word) + 1) * 8
            if self.x + w >= self.ui.tft.width - self.MARGIN:
                self._newline()

            self.ui.tft.draw_text8x8(
                self.x,
                self.y,
                word + " ",
                fg,
                bg
            )
            self.x += w

    # --------------------------
    # Utils
    # --------------------------
    def _newline(self):
        self.x = self.MARGIN
        self.y += 10
        if self.y >= self.ui.tft.height - 10:
            self.y = self.TASKBAR_H + self.MARGIN
class IOSSlider:
    def __init__(self, x, y, w,
                 min_v=0, max_v=100, value=0,
                 track_border=color565(40,40,40),
                 track_fill=color565(90,90,90),
                 knob=color565(220,220,220),
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


    # ---------- STATIC DRAW (ONCE) ----------
    def draw(self, ui):
        # background clear only around slider
        ui.tft.fill_rectangle(
            self.x-2, self.y-2,
            self.w+4, self.h+4,
            ui.background
        )

        # track border
        ui.tft.draw_rectangle(
            self.x, self.y,
            self.w, self.h,
            self.track_border
        )

        # track fill
        ui.tft.fill_rectangle(
            self.x+1, self.y+1,
            self.w-2, self.h-2,
            self.track_fill
        )

        # top highlight
        ui.tft.draw_hline(
            self.x+1, self.y+1,
            self.w-2,
            color565(140,140,140)
        )

        # initial knob
        travel = self.w - self.knob_size
        kx = self.x + int(
            (self.value - self.min) / (self.max - self.min) * travel
        )

        self._draw_knob(ui, kx)
        self._last_kx = kx


    # ---------- KNOB DRAW ----------
    def _draw_knob(self, ui, kx):
        ky = self.y + 1

        # shadow
        ui.tft.fill_rectangle(
            kx+1, ky+1,
            self.knob_size, self.knob_size,
            color565(0,0,0)
        )

        # body
        ui.tft.fill_rectangle(
            kx, ky,
            self.knob_size, self.knob_size,
            self.knob_color
        )

        # highlight
        ui.tft.draw_hline(
            kx+1, ky+1,
            self.knob_size-2,
            color565(255,255,255)
        )


    # ---------- ERASE OLD KNOB ----------
    def _erase_knob(self, ui, kx):
        ui.tft.fill_rectangle(
            kx, self.y+1,
            self.knob_size+2, self.knob_size+2,
            self.track_fill
        )

        # restore top highlight
        ui.tft.draw_hline(
            kx, self.y+1,
            self.knob_size+2,
            color565(140,140,140)
        )


    # ---------- TOUCH ----------
    def handle_touch(self, ui):
        p = ui.get_touch()

        if not p:
            self.dragging = False
            return False

        tx, ty = p

        travel = self.w - self.knob_size
        ky = self.y + 1

        # current knob x
        if self._last_kx is None:
            return False

        # latch only if touching knob
        if not self.dragging:
            if (self._last_kx <= tx <= self._last_kx + self.knob_size and
                ky <= ty <= ky + self.knob_size):
                self.dragging = True
                self.drag_offset = tx - self._last_kx
            else:
                return False

        # new position
        new_x = tx - self.drag_offset
        new_x = max(self.x, min(self.x + travel, new_x))

        if new_x == self._last_kx:
            return True

        # erase old
        self._erase_knob(ui, self._last_kx)

        # update value
        ratio = (new_x - self.x) / travel
        self.value = self.min + ratio * (self.max - self.min)

        # draw new
        self._draw_knob(ui, new_x)
        self._last_kx = new_x

        if self.action:
            self.action(self.value)

        return True
