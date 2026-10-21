# main.py -- Zeno OS boot sequence, driven by /LOGS/bootcfg.json
#
# Honoured BootConfig keys:
#   BOOT_MODE        NORMAL | SAFE (SAFE/REPL = no tuning/network/kernel, stay at REPL)
#   LOGGER_STATUS    ENABLED | DISABLED   (DISABLED = nothing written to the log file)
#   LOG_REPL         ENABLED | DISABLED   (print boot logs to the REPL)
#   SHOW_UI          true | false         (false = headless: no display, no login)
#   BOOT_CMD         "text"               (live boot log on screen, not on REPL)
#   LOG_SHOW_STATUS  1 | 0                (same as BOOT_CMD="text"; also accepts key "log.show_status")
#   WIFI_AUTOCONNECT true | false
#   OPT_LEVEL        0..3
#   MODE             PERFORMANCE | anything else = normal power
#   KERNEL_PATH      file executed as the kernel (falls back to zeno.loc)
import gc, machine, time, os, sys
from sys import path

SAFE_MODE_PIN = None        # e.g. 0 to hold BOOT low at power-on -> stay at REPL
path.append("/bin")
path.append("/bin/Graphics")
path.append("/bin/ZenCMD")
path.append("/SYSTEM32/OS")

DEFAULTS = {
    "BOOT_MODE": "NORMAL",
    "LOGGER_STATUS": "ENABLED",
    "LOG_REPL": "ENABLED",
    "SHOW_UI": True,
    "BOOT_CMD": "",
    "LOG_SHOW_STATUS": 0,
    "WIFI_AUTOCONNECT": True,
    "OPT_LEVEL": 0,
    "MODE": "PERFORMANCE",
    "KERNEL_PATH": None,
}

log = None
S = None          # Services module
zeno = None
cfg = {}
pm_power = None
gfx = None
lcd = None

live_boot_logs = []                       # every boot line, in order
_policy = {"repl": True, "screen": False}  # where _note() sends output
_scr = {"y": 10, "color": None}
MAX_CHARS = 56                             # fits a 480x320 panel at 8x8 font


# ---------------------------------------------------------------- helpers
def _cfg(key):
    if key in cfg:
        return cfg[key]
    return DEFAULTS.get(key)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(v)


def _safe_mode():
    if SAFE_MODE_PIN is None:
        return False
    try:
        return machine.Pin(SAFE_MODE_PIN, machine.Pin.IN, machine.Pin.PULL_UP).value() == 0
    except Exception:
        return False


def _screen_line(msg):
    """Draw one green log line on the display, wrapping and scrolling."""
    if gfx is None:
        return
    try:
        while True:
            if _scr["y"] + 10 > gfx.HEIGHT - 10:
                gfx.clear(gfx.BLACK)
                _scr["y"] = 10
            gfx.draw_text(6, _scr["y"], msg[:MAX_CHARS], _scr["color"], gfx.BLACK,
                          font=gfx.FONT_8X8, scale=1)
            _scr["y"] += 12
            msg = msg[MAX_CHARS:]
            if not msg:
                break
            msg = "  " + msg
    except Exception:
        pass


def _note(msg, error=False):
    line = "[BOOT] {}".format(msg)
    live_boot_logs.append(line)
    if _policy["repl"]:
        print(line)
    if _policy["screen"]:
        _screen_line(line)
    try:
        if log:
            (log.error if error else log.debug)(msg, source="BOOT")
    except Exception:
        pass


def _halt(msg):
    """Fatal path: always tell the REPL, whatever LOG_REPL says."""
    _policy["repl"] = True
    _policy["screen"] = False
    _note(msg, error=True)


def _stage(name, fn):
    """Run one boot stage; never raises. Returns True on success."""
    _note("Starting stage: {}".format(name))
    try:
        fn()
        _note("stage '{}' ok".format(name))
        return True
    except Exception as e:
        _note("stage '{}' FAILED: {}".format(name, e), error=True)
        return False


# ---------------------------------------------------------------- stages
def st_zeno():
    global zeno
    import zeno as _z
    zeno = _z


def st_services():
    global S, cfg
    import Services as _S           # also loads /LOGS/bootcfg.json
    S = _S
    cfg = dict(S.bootcfg.config)


def _noop_write(self, text):
    return ("logging disabled", False)


def st_logger():
    global log
    if str(_cfg("LOGGER_STATUS")).upper() == "ENABLED":
        log = S.Logger(boot=True)
    else:
        # Disable file logging for every Logger instance (kernel included)
        S.Logger._write = _noop_write
        log = S.Logger(boot=False)


def st_display():
    global gfx, lcd
    import lcd as _lcd
    from Graphics import Graphics as _g
    lcd, gfx = _lcd, _g
    gfx.init_display(getattr(zeno, "rotation", 1), lcd_obj=lcd)
    if zeno is not None:
        zeno.ui = lcd
    time.sleep(0.2)
    gfx.pin.on()
    gfx.clear(gfx.BLACK)
    _scr["color"] = gfx.color565(78, 212, 38)
    _scr["y"] = 10


def st_tuning():
    global pm_power
    import micropython
    micropython.opt_level(int(_cfg("OPT_LEVEL") or 0))
    pm_power = S.PowerManagement(log)
    mode = str(_cfg("MODE")).upper()
    pm_power.baseline_set("high" if mode == "PERFORMANCE" else "normal")


def st_network():
    if not _truthy(_cfg("WIFI_AUTOCONNECT")):
        _note("WiFi autoconnect disabled in config.")
        return
    if not getattr(zeno, "wifi_enabled", True):
        _note("WiFi disabled by zeno profile.")
        return
    _note("Initializing WifiManager auto_connect...")
    S.WifiManager(log).auto_connect()


def st_login():
    """Password screen. Display must already be initialised."""
    # Don't let boot notes draw over the login UI
    was_screen = _policy["screen"]
    _policy["screen"] = False

    # If logs weren't streaming live, replay them now in green
    if not was_screen:
        _scr["y"] = 10
        for line in live_boot_logs:
            _screen_line(line)
            time.sleep_ms(70)
    time.sleep_ms(800)
    gfx.clear(gfx.BLACK)

    expected_password = getattr(zeno, "password", "1234")

    try:
        lw, lh = 64, 64
        gfx.draw_bmp("/SYSTEM32/RESOURCES/user.bmp", (gfx.WIDTH - lw) // 2, 25, lw, lh)
    except Exception as e:
        _note("user.bmp not found, using text fallback: {}".format(e))
        gfx.draw_text_centered(gfx.WIDTH // 2, 40, "Zeno OS", gfx.WHITE, gfx.BLACK, scale=2)

    gfx.draw_text_centered(gfx.WIDTH // 2, 95, "Enter Password", gfx.TEXT_MUTED, gfx.BLACK, scale=1)

    authenticated = False

    def on_ok(pwd):
        nonlocal authenticated
        if pwd == expected_password:
            authenticated = True
        else:
            _note("incorrect password attempt", error=True)
            gfx.UIToast(lcd, "Incorrect Password", duration=2, bg=gfx.ACCENT_RED).show()
            kb.clear_buffer()
            input_box.draw(force=True)

    kb = gfx.VitualKeyboard(height=135, ok_action=on_ok)
    box_w = min(280, gfx.WIDTH - 40)
    input_box = gfx.UIInputTextBox(lcd, (gfx.WIDTH - box_w) // 2, 115, box_w, 36, kb)
    input_box.draw(force=True)
    kb.render()

    _note("showing login screen, awaiting password")

    while not authenticated:
        gfx.poll([input_box])
        kb.check_touch()
        input_box.draw()
        if kb.buffer == expected_password and not getattr(kb, "active", True):
            authenticated = True
        time.sleep_ms(15)

    try:
        kb.close()
    except Exception:
        pass

    gfx.clear(gfx.BLACK)
    _scr["y"] = 10
    _policy["screen"] = was_screen      # resume live log streaming for kernel stage
    _note("authentication successful")


def st_kernel():
    path = _cfg("KERNEL_PATH") or getattr(zeno, "loc", None)
    if not path:
        raise RuntimeError("no kernel path (KERNEL_PATH / zeno.loc)")
    os.stat(path)

    _note("loading kernel from {}".format(path))
    with open(path) as f:
        src = f.read()
    gc.collect()

    try:
        exec(compile(src, path, "exec"), {"__name__": "__main__", "__file__": path})
    except Exception as e:
        if _policy["repl"]:
            print("[BOOT] KERNEL LOAD FAILED:")
            sys.print_exception(e)
        _note("kernel load failed: {}: {}".format(type(e).__name__, e), error=True)
        raise e
    _note("kernel running")


# ---------------------------------------------------------------- boot
def boot():
    _note("Zeno OS starting (Secure Login)")
    if not (_stage("zeno", st_zeno) and _stage("services", st_services)):
        _halt("core unavailable -- staying at REPL")
        return

    _stage("logger", st_logger)

    mode = str(_cfg("BOOT_MODE")).upper()
    if _safe_mode() or mode in ("SAFE", "REPL"):
        _halt("SAFE MODE ({}): no tuning/network/kernel -- staying at REPL".format(mode))
        return

    ui = _truthy(_cfg("SHOW_UI"))
    want_screen = ui and (
        _truthy(_cfg("LOG_SHOW_STATUS"))
        or _truthy(cfg.get("log.show_status", 0))
        or str(_cfg("BOOT_CMD")).strip().lower() == "text"
    )
    repl_on = _truthy(_cfg("LOG_REPL"))

    if ui:
        if not _stage("display", st_display):
            _halt("display init failed -- boot halted, staying at REPL")
            return
        if want_screen:
            for line in live_boot_logs:     # catch up on lines logged before the display was ready
                _screen_line(line)
            _policy["screen"] = True
    # logs go to the screen *instead of* the REPL when screen streaming is on
    _policy["repl"] = repl_on and not _policy["screen"]

    _stage("tuning", st_tuning)
    _stage("network", st_network)

    if ui:
        if not _stage("login", st_login):
            _halt("login failed -- boot halted, staying at REPL")
            return
    else:
        _note("SHOW_UI disabled: headless boot, login skipped")

    if not _stage("kernel", st_kernel):
        _halt("kernel failed -- staying at REPL")
        return

    if log:
        log.boot_complete()
    gc.collect()
    _note("free heap: {} bytes".format(gc.mem_free()))


boot()
