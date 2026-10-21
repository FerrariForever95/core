# kernel.py -- Zeno OS kernel (Standalone Graphics-API Edition with Ubuntu Gradient Theme).
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
ROTATION      = getattr(zeno, "rotation", 1)      # 0/2 portrait, 1/3 landscape
APP_PATH      = "/SYSTEM32/APPS"
LOGO_PATH     = "/SYSTEM32/APPS/LOGO"
POWER_BMP     = "/SYSTEM32/RESOURCES/power.png"   # Supports .png (transparent) or .bmp/.jpg

TASKBAR_H     = 35
TASKBAR_RGB   = (44, 0, 30)      # Deep Ubuntu dark aubergine taskbar
POWER_SIZE    = 31               # icon tile in the taskbar
LOGO_SIZE     = 50               # app logo; tile adds 4px inset each side
TILE          = LOGO_SIZE + 8
PADDING       = 10
TOUCH_MARGIN  = PADDING // 2        # extra touch area, kept < half the gap so tiles never overlap

# ---------------------------------------------------------------------------
# UBUNTU GRADIENT THEME WALLPAPER
# ---------------------------------------------------------------------------
ubuntu_gradient_wallpaper = gfx.UIGradientWallpaper(
    lcd, 
    [
        G.color565(44, 0, 30),    # Deep Aubergine Purple
        G.color565(89, 43, 76),   # Mid Violet-Aubergine
        G.color565(227, 85, 27)   # Classic Ubuntu Warm Orange
    ], 
    angle=135                     # Diagonal gradient flow
)

# ---------------------------------------------------------------------------
# GRAPHICS WIDGET & HELPERS
# ---------------------------------------------------------------------------
class AppIcon(UIIconButton):
    """Icon tile with a slightly larger touch area than its drawn size."""
    def contains(self, px, py, margin=2):
        return G.UIWidget.contains(self, px, py, TOUCH_MARGIN)

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
    """Renders the multi-stop Ubuntu gradient wallpaper."""
    try:
        ubuntu_gradient_wallpaper.draw()
    except Exception as e:
        log.error("gradient wallpaper draw failed: {}".format(e), source="KERNEL")
        G.clear(G.color565(44, 0, 30))


def scan_apps():
    try:
        apps = os.listdir(APP_PATH)
        logo_files = set(os.listdir(LOGO_PATH))
    except Exception:
        return []
    
    valid_apps = []
    for f in apps:
        if f.endswith(".py"):
            base = f[:-3]
            if (base + ".bmp") in logo_files or (base + ".png") in logo_files or (base + ".jpg") in logo_files:
                valid_apps.append(base)
    return sorted(valid_apps)


def get_logo_path(name):
    """Checks for .png first (for transparency), falls back to .jpg and .bmp."""
    for ext in (".png", ".jpg", ".jpeg", ".bmp"):
        p = LOGO_PATH + "/" + name + ext
        try:
            os.stat(p)
            return p
        except Exception:
            continue
    return LOGO_PATH + "/" + name + ".bmp"


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
            image_path=get_logo_path(name),
            action=lambda n=name: launch_app(n),
            color=G.color565(60, 20, 50), radius=6))
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
    
    globals()["APP_EXIT"] = AppExit
    
    print("[KERNEL] Launching app:", name)
    log.debug("launching app '{}'".format(name), source="KERNEL")
    
    try:
        with open(path) as f:
            code = f.read()
        
        exec(code, globals())
        
    except AppExit:
        print("[KERNEL] App exited cleanly via APP_EXIT:", name)
        log.debug("app '{}' exited normally".format(name), source="KERNEL")
        
    except Exception as e:
        buf = uio.StringIO()
        sys.print_exception(e, buf)
        err_output = buf.getvalue()
        
        print("[APP ERROR CRASH]", name)
        print(err_output)
        
        log.error("app '{}' crashed: {}".format(name, e), source="KERNEL")
        show_error_dialog("Error: " + name, str(e))
        
    finally:
        redraw_home()

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
    G.set_background(G.color565(44, 0, 30))

    bar = G.color565(*TASKBAR_RGB)
    header = G.UIHeader(lcd, "Zeno OS", TASKBAR_H, bg=bar, fg=G.WHITE)
    
    power_x = G.WIDTH - POWER_SIZE - 4
    power_y = (TASKBAR_H - POWER_SIZE) // 2
    
    btn_power = UIIconButton(
        lcd,
        power_x, power_y, POWER_SIZE, POWER_SIZE,
        image_path=POWER_BMP, action=show_power, color=bar, radius=6)

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
