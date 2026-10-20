# kernel.py -- Zeno OS kernel (Standalone Graphics-API Edition with Error Modal Dialogs).
import os, sys, gc, uio, time
import zeno
import Services as S

# ---------------------------------------------------------------------------
# GRAPHICS IMPORT & LCD BINDING FROM ZENO.UI
# ---------------------------------------------------------------------------
sys.path.append("/bin/Graphics")
from Graphics import Graphics as gfx
UIIconButton = gfx.UIIconButton
G = gfx

# Retrieve lcd from zeno.ui (initialized during boot sequence)
lcd = getattr(zeno, "ui", None)
if lcd is None:
    import lcd as _lcd
    lcd = _lcd
    zeno.ui = lcd

# Ensure graphics stack knows about the shared lcd instance
G.bind_lcd(lcd)

# ---------------------------------------------------------------------------
# KERNEL AUTH (flags other modules read)
# ---------------------------------------------------------------------------
zeno.boot_cap = None
zeno.authorized = True

# ---------------------------------------------------------------------------
# LOGGER -- initialised at kernel load
# ---------------------------------------------------------------------------
log = S.Logger()
log.debug("kernel loading (standalone)", source="KERNEL")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
ROTATION      = getattr(zeno, "rotation", 1)     # 0/2 portrait, 1/3 landscape
APP_PATH      = "/SYSTEM32/APPS"
LOGO_PATH     = "/SYSTEM32/APPS/LOGO"
POWER_BMP     = "/SYSTEM32/RESOURCES/power.bmp"

TASKBAR_H     = 35
TASKBAR_RGB   = (125, 125, 125)
POWER_SIZE    = 31                 # icon tile in the taskbar
LOGO_SIZE     = 50                 # app logo; tile adds 4px inset each side
TILE          = LOGO_SIZE + 8
PADDING       = 10
TOUCH_MARGIN  = PADDING // 2       # extra touch area, kept < half the gap so tiles never overlap

# ---------------------------------------------------------------------------
# GRAPHICS WIDGET & HELPERS
# ---------------------------------------------------------------------------
class AppIcon(UIIconButton):
    """Icon tile with a slightly larger touch area than its drawn size."""
    def contains(self, px, py, margin=2):
        return G.UIWidget.contains(self, px, py, TOUCH_MARGIN)

    def draw(self):
        if not self.visible:
            return
        c = G.DARK_GRAY if not self.enabled else (self.pressed_color if self.pressed else self.color)
        tc = G.TEXT_MUTED if not self.enabled else self.text_color
        r = G._clamp(self.radius, 0, min(self.w, self.h) // 2)
        
        G.draw_rounded_rect(self.x, self.y, self.w, self.h, r, c, filled=True)
        if self.border_color is not None:
            G.draw_rounded_rect(self.x, self.y, self.w, self.h, r, self.border_color, filled=False)
            
        if self.bmp_path:
            inset = 4
            iw = self.w - (inset * 2)
            ih = self.h - (inset * 2)
            try:
                G.draw_logo(self.bmp_path, self.x + inset, self.y + inset, iw, ih,
                            caption=None, caption_color=tc, bg=None, font=self.font)
            except Exception as e:
                log.error("AppIcon logo draw failed: {}".format(e), source="KERNEL")

header = None
btn_power = None
dlg = None
active_modal = None
home_widgets = []
_icons = []
_app_names = None
running = True

def _unregister(w):
    try:
        G._registered_widgets.remove(w)
    except Exception:
        pass


def _safe_draw(w):
    try:
        w.draw()
    except Exception as e:
        log.error("draw failed ({}): {}".format(type(w).__name__, e), source="KERNEL")


def draw_wallpaper():
    wp = getattr(zeno, "wallpaper", None)
    if wp:
        try:
            G.draw_bmp(wp, 0, 0, G.WIDTH, G.HEIGHT)
            return
        except Exception as e:
            log.error("wallpaper failed: {}".format(e), source="KERNEL")
    G.clear(G.BLACK)


def scan_apps():
    try:
        apps = os.listdir(APP_PATH)
        logos = set(os.listdir(LOGO_PATH))
    except Exception:
        return []
    return sorted(f[:-3] for f in apps if f.endswith(".py") and (f[:-3] + ".bmp") in logos)


def build_icons(names):
    global _icons, _app_names
    for b in _icons:
        _unregister(b)
    _icons = []
    sw, sh = G.WIDTH, G.HEIGHT
    x = PADDING
    y = TASKBAR_H + PADDING
    for name in names:
        if x + TILE > sw:
            x = PADDING
            y += TILE + PADDING
        if y + TILE > sh:
            break
        _icons.append(AppIcon(
            lcd,
            x, y, TILE, TILE,
            bmp_path=LOGO_PATH + "/" + name + ".bmp",
            action=lambda n=name: launch_app(n),
            color=G.SURFACE_ALT, radius=0))
        x += TILE + PADDING
    _app_names = names


def redraw_home():
    global active_modal
    active_modal = None
    names = scan_apps()
    if names != _app_names:
        build_icons(names)
    draw_wallpaper()
    _safe_draw(header)
    _safe_draw(btn_power)
    for b in _icons:
        _safe_draw(b)
    home_widgets[:] = [btn_power] + _icons

# ---------------------------------------------------------------------------
# APP SYSTEM & ERROR DIALOG HANDLING
# ---------------------------------------------------------------------------
class AppExit(Exception):
    pass


def show_error_dialog(title, err_message):
    global active_modal
    err_w = min(360, G.WIDTH - 40)
    err_h = min(160, G.HEIGHT - 40)
    active_modal = G.UIModal(
        lcd,
        title=title,
        message=err_message[:120],  # truncated to fit modal nicely
        w=err_w,
        h=err_h,
        on_ok=redraw_home,
        on_cancel=redraw_home
    )
    active_modal.btn_ok.set_text("OK", redraw=False)
    active_modal.btn_cancel.set_text("Close", redraw=False)
    active_modal.show()


def launch_app(name):
    path = APP_PATH + "/" + name + ".py"
    app_dir = APP_PATH if 'APP_PATH' in globals() else "/SYSTEM32/APPS"
    gc.collect()
    log.debug("launching app '{}' (non-isolated)".format(name), source="KERNEL")
    
    # Expose app control flags in the shared global scope
    globals()["APP_EXIT"] = AppExit
    globals()["__file__"] = path
    globals()["__name__"] = "__main__"

    old_path = list(sys.path)
    old_cwd = os.getcwd()
    for p in [app_dir, "/bin", "/"]:
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        os.chdir(app_dir)
        with open(path) as f:
            exec(compile(f.read(), path, "exec"), globals())
        redraw_home()
    except AppExit:
        redraw_home()
    except Exception as e:
        buf = uio.StringIO()
        sys.print_exception(e, buf)
        err_str = "{} - {}".format(type(e).__name__, e)
        print("[APP ERROR]", name)
        print(buf.getvalue())
        log.error("app '{}' crashed: {}".format(name, e), source="KERNEL")
        show_error_dialog("Error: " + name, err_str)
    finally:
        try:
            os.chdir(old_cwd)
        except Exception:
            os.chdir("/")
        sys.path = old_path
# ---------------------------------------------------------------------------
# POWER DIALOG
# ---------------------------------------------------------------------------
def _shutdown():
    global running
    log.debug("shutdown confirmed - exiting to REPL", source="KERNEL")
    try:
        G.window_close_animation(0.4)
        G.clear(G.BLACK)
        G.pin.off()                   # backlight disable
    except Exception as e:
        log.error("shutdown display step failed: {}".format(e), source="KERNEL")
    running = False


def show_power():
    global active_modal
    active_modal = dlg
    dlg.show()

# ---------------------------------------------------------------------------
# UI START
# ---------------------------------------------------------------------------
def start_ui():
    global header, btn_power, dlg
    
    G.pin.on()
    G.set_background(G.BLACK)

    bar = G.color565(*TASKBAR_RGB)
    header = G.UIHeader(lcd, "Home", TASKBAR_H, bg=bar, fg=G.WHITE)
    btn_power = UIIconButton(
        lcd,
        G.WIDTH - POWER_SIZE - 4, (TASKBAR_H - POWER_SIZE) // 2, POWER_SIZE, POWER_SIZE,
        bmp_path=POWER_BMP, action=show_power, color=bar, radius=6)

    dlg = G.UIModal(lcd, "Power", "Shutdown?", on_ok=_shutdown, on_cancel=redraw_home)
    dlg.btn_ok.set_text("Yes", redraw=False)
    dlg.btn_cancel.set_text("No", redraw=False)
    dlg.visible = False

    redraw_home()

# ---------------------------------------------------------------------------
# BRING-UP & STANDALONE EVENT LOOP
# ---------------------------------------------------------------------------
start_ui()
log.debug("kernel ready (standalone ui loop running)", source="KERNEL")

while running:
    current_modal = active_modal if (active_modal and active_modal.visible) else None
    G.poll([current_modal] if current_modal else home_widgets)
    time.sleep_ms(15)

log.debug("kernel exited cleanly to REPL", source="KERNEL")
