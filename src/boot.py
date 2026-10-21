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
#
# Logging: every _note() goes to the logger file AND to the REPL/screen sink
# chosen by config, with the same text, so the three views always agree.
# Notes made before the logger exists are buffered and written once it is up.
# Passwords are never logged.
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
um = None         # usermanager instance (also exposed as zeno.usr)

live_boot_logs = []                        # every boot line, in order
_pending = []                              # (level, src, msg) made before the logger existed
_results = {}                              # stage name -> True/False
_policy = {"repl": True, "screen": False}  # where _note() sends output
_scr = {"y": 10, "color": None}
_t_boot = time.ticks_ms()
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


def _to_logger(level, src, msg):
    """Write to the logger file; buffer if the logger isn't up yet."""
    if log is None:
        if len(_pending) < 80:
            _pending.append((level, src, msg))
        return
    try:
        getattr(log, level)(msg, source=src)
    except Exception:
        pass


def _flush_pending():
    for level, src, msg in _pending:
        _to_logger(level, src, msg)
    del _pending[:]


def _note(msg, error=False, warn=False, src="BOOT"):
    level = "error" if error else ("warning" if warn else "debug")
    line = "[{}] {}".format(src, msg)
    live_boot_logs.append(line)
    if _policy["repl"]:
        print(line)
    if _policy["screen"]:
        _screen_line(line)
    _to_logger(level, src, msg)


def _halt(msg):
    """Fatal path: always tell the REPL, whatever LOG_REPL says."""
    _policy["repl"] = True
    _policy["screen"] = False
    _note(msg, error=True)


def _stage(name, fn):
    """Run one boot stage; never raises. Returns True on success."""
    _note("stage '{}' starting".format(name))
    t0 = time.ticks_ms()
    ok = False
    try:
        fn()
        ok = True
        _note("stage '{}' ok ({} ms)".format(name, time.ticks_diff(time.ticks_ms(), t0)))
    except Exception as e:
        _note("stage '{}' FAILED after {} ms: {}: {}".format(
            name, time.ticks_diff(time.ticks_ms(), t0), type(e).__name__, e), error=True)
    _results[name] = ok
    return ok


def _reset_cause():
    try:
        rc = machine.reset_cause()
        for n in ("PWRON_RESET", "HARD_RESET", "WDT_RESET", "DEEPSLEEP_RESET", "SOFT_RESET"):
            if getattr(machine, n, None) == rc:
                return n
        return str(rc)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- stages
def st_zeno():
    global zeno
    import zeno as _z
    zeno = _z
    # never log the password
    _note("profile loaded: user={} wifi_enabled={} rotation={}".format(
        getattr(zeno, "user", "?"), getattr(zeno, "wifi_enabled", True),
        getattr(zeno, "rotation", 1)), src="ZENO")


def st_services():
    global S, cfg
    import Services as _S           # also loads /LOGS/bootcfg.json
    S = _S
    cfg = dict(S.bootcfg.config)
    _note("bootcfg loaded: " + ", ".join(
        "{}={}".format(k, cfg[k]) for k in sorted(cfg)), src="CONFIG")
    _note("heap after services: {} bytes free".format(gc.mem_free()), src="CONFIG")


def _noop_write(self, text):
    return ("logging disabled", False)


def st_logger():
    global log
    if str(_cfg("LOGGER_STATUS")).upper() == "ENABLED":
        log = S.Logger(boot=True)
        _flush_pending()             # write everything logged before the logger existed
        _note("logger enabled, file={}".format(log.log_file_user), src="LOGGER")
    else:
        # Disable file logging for every Logger instance (kernel included)
        S.Logger._write = _noop_write
        log = S.Logger(boot=False)
        del _pending[:]
        _note("logger DISABLED by config: nothing will be written to the log file", warn=True, src="LOGGER")


def st_users():
    """Start the user-management service (mounts zfs, repairs OS/users, exposes zeno.usr)."""
    global um
    import zfs, json
    try:
        zfs.mount()
        _note("zfs mounted", src="USERS")
    except Exception as e:
        _note("zfs.mount skipped: {}".format(e), src="USERS")     # usually 'already mounted'

    um = S.usermanager()
    with S.SystemPrivilege(S._SYSTEM_TOKEN, "boot-users"):
        for p in ("OS", "OS/users"):
            if not zfs.exists(p):
                zfs.mkdir(p)
                _note("created missing {}".format(p), warn=True, src="USERS")
        had_main = zfs.exists("OS/users/userinfo.json")
        info = um.__read__()          # creates/heals OS/users/userinfo.json from zeno.py
        if had_main:
            _note("main user record present", src="USERS")
        else:
            _note("main user record (re)created from zeno.py", warn=True, src="USERS")
        d = "OS/users/" + info["user"]
        if not zfs.exists(d):         # per-user folder so authenticate_user/create_user work
            zfs.mkdir(d)
            try:
                zfs.mkdir(d + "/home")
            except Exception:
                pass
            um.d.write(d + "/userinfo.json", json.dumps(
                {"user": info["user"], "password": info["password"], "root": False}))
            _note("created per-user folder for '{}'".format(info["user"]), warn=True, src="USERS")
        try:
            count = len(zfs.listdir("OS/users"))
        except Exception:
            count = -1
    S.usermanager._session_root = False
    if zeno is not None:
        zeno.usr = um
    _note("usermanager ready: main user='{}', {} entries in OS/users, Super Mode off".format(
        info["user"], count), src="USERS")


def _expected_password():
    """Password to check at login: usermanager first, zeno.py only as a fallback."""
    if um is not None:
        try:
            with S.SystemPrivilege(S._SYSTEM_TOKEN, "login"):
                pw = str(um.__password__())
            _note("login credential source: usermanager", src="LOGIN")
            return pw
        except Exception as e:
            _note("usermanager password read failed: {}".format(e), error=True, src="LOGIN")
    pw = getattr(zeno, "password", None)
    if pw is None:
        raise RuntimeError("no password source (usermanager and zeno.py both unavailable)")
    _note("login credential source: zeno.py fallback (usermanager unavailable)", warn=True, src="LOGIN")
    return str(pw)


def _mark_login():
    """Record a successful login so the guardian's security_scan stays quiet."""
    zeno.authorized = True
    zeno.boot_cap = None              # boot capability consumed
    zeno.boot_time = time.time()
    if um is not None:
        zeno.usr = um
    S.usermanager._session_root = False   # logged in, but not in Super Mode
    _note("login recorded: user='{}' authorized=True boot_cap consumed".format(
        getattr(zeno, "user", "?")), src="LOGIN")


def st_display():
    global gfx, lcd
    import lcd as _lcd
    from Graphics import Graphics as _g
    lcd, gfx = _lcd, _g
    rotation = getattr(zeno, "rotation", 1)
    gfx.init_display(rotation, lcd_obj=lcd)
    if zeno is not None:
        zeno.ui = lcd
        _note("display manager active: zeno.ui bound to {}".format(
            getattr(lcd, "__name__", "lcd")), src="DISPLAY")
    time.sleep(0.2)
    gfx.pin.on()
    gfx.clear(gfx.BLACK)
    _scr["color"] = gfx.color565(78, 212, 38)
    _scr["y"] = 10
    _note("display ready: {}x{} rotation={}".format(gfx.WIDTH, gfx.HEIGHT, rotation), src="DISPLAY")


def st_tuning():
    global pm_power
    import micropython
    level = int(_cfg("OPT_LEVEL") or 0)
    micropython.opt_level(level)
    pm_power = S.PowerManagement(log)
    mode = str(_cfg("MODE")).upper()
    baseline = "high" if mode == "PERFORMANCE" else "normal"
    pm_power.baseline_set(baseline)
    _note("tuning: opt_level={} mode={} power_baseline={}".format(level, mode, baseline), src="TUNING")


def st_network():
    if not _truthy(_cfg("WIFI_AUTOCONNECT")):
        _note("WiFi autoconnect disabled in config.", src="NET")
        return
    if not getattr(zeno, "wifi_enabled", True):
        _note("WiFi disabled by zeno profile.", src="NET")
        return
    _note("initializing WifiManager auto_connect...", src="NET")
    result = S.WifiManager(log).auto_connect()
    _note("auto_connect result: {}".format(result), src="NET")


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
            time.sleep_ms(40)
    time.sleep_ms(800)
    gfx.clear(gfx.BLACK)

    user = getattr(zeno, "user", "?")
    expected_password = _expected_password()
    _note("preparing login screen for user '{}'".format(user), src="LOGIN")

    try:
        lw, lh = 64, 64
        gfx.draw_bmp("/SYSTEM32/RESOURCES/user.bmp", (gfx.WIDTH - lw) // 2, 25, lw, lh)
    except Exception as e:
        _note("user.bmp not found, using text fallback: {}".format(e), warn=True, src="LOGIN")
        gfx.draw_text_centered(gfx.WIDTH // 2, 40, "Zeno OS", gfx.WHITE, gfx.BLACK, scale=2)

    gfx.draw_text_centered(gfx.WIDTH // 2, 95, "Enter Password", gfx.TEXT_MUTED, gfx.BLACK, scale=1)

    authenticated = False
    attempts = 0

    def on_ok(pwd):
        nonlocal authenticated, attempts
        if pwd == expected_password:
            authenticated = True
        else:
            attempts += 1
            _note("incorrect password attempt #{} for user '{}'".format(attempts, user),
                  warn=True, src="LOGIN")
            gfx.UIToast(lcd, "Incorrect Password", duration=2, bg=gfx.ACCENT_RED).show()
            kb.clear_buffer()
            input_box.draw(force=True)

    kb = gfx.VitualKeyboard(height=135, ok_action=on_ok)
    box_w = min(280, gfx.WIDTH - 40)
    input_box = gfx.UIInputTextBox(lcd, (gfx.WIDTH - box_w) // 2, 115, box_w, 36, kb)
    input_box.draw(force=True)
    kb.render()

    t_prompt = time.ticks_ms()
    _note("LOGIN PROMPTED: user '{}' at {} ms since boot, awaiting password".format(
        user, time.ticks_diff(t_prompt, _t_boot)), src="LOGIN")

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
    _note("login accepted for '{}' after {} ms and {} failed attempt(s)".format(
        user, time.ticks_diff(time.ticks_ms(), t_prompt), attempts), src="LOGIN")
    _mark_login()


def st_kernel():
    path = _cfg("KERNEL_PATH") or getattr(zeno, "loc", None)
    if not path:
        raise RuntimeError("no kernel path (KERNEL_PATH / zeno.loc)")
    st = os.stat(path)

    _note("loading kernel from {} ({} bytes)".format(path, st[6]), src="KERNEL")
    with open(path) as f:
        src = f.read()
    gc.collect()
    _note("kernel source read, heap {} bytes free".format(gc.mem_free()), src="KERNEL")

    try:
        code = compile(src, path, "exec")
        _note("kernel compiled, handing control to kernel", src="KERNEL")
        exec(code, {"__name__": "__main__", "__file__": path})
    except Exception as e:
        if _policy["repl"]:
            print("[KERNEL] LOAD FAILED:")
            sys.print_exception(e)
        _note("kernel load failed: {}: {}".format(type(e).__name__, e), error=True, src="KERNEL")
        raise e
    _note("kernel returned control to boot", src="KERNEL")


# ---------------------------------------------------------------- boot
def boot():
    _note("Zeno OS starting (Secure Login)")
    _note("reset cause: {}, heap {} bytes free".format(_reset_cause(), gc.mem_free()))
    if not (_stage("zeno", st_zeno) and _stage("services", st_services)):
        _halt("core unavailable -- staying at REPL")
        return

    _stage("logger", st_logger)

    mode = str(_cfg("BOOT_MODE")).upper()
    _note("boot mode: {}".format(mode))
    if _safe_mode() or mode in ("SAFE", "REPL"):
        _halt("SAFE MODE ({}): no tuning/network/kernel -- staying at REPL".format(mode))
        return

    _stage("users", st_users)        # usermanager must be up before the login screen

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
    _note("log sinks: file={} repl={} screen={}".format(
        log is not None and str(_cfg("LOGGER_STATUS")).upper() == "ENABLED",
        _policy["repl"], _policy["screen"]), src="LOGGER")

    _stage("tuning", st_tuning)
    _stage("network", st_network)

    _note("pre-login summary: " + " ".join(
        "{}={}".format(k, "ok" if v else "FAILED") for k, v in _results.items()))

    if ui:
        if not _stage("login", st_login):
            _halt("login failed -- boot halted, staying at REPL")
            return
    else:
        _note("SHOW_UI disabled: headless boot, login skipped (zeno.authorized stays False)",
              warn=True, src="LOGIN")

    if not _stage("kernel", st_kernel):
        _halt("kernel failed -- staying at REPL")
        return

    if log:
        log.boot_complete()
    gc.collect()
    _note("boot complete in {} ms, free heap: {} bytes".format(
        time.ticks_diff(time.ticks_ms(), _t_boot), gc.mem_free()))


boot()
