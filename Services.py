
import sys
if '/lib' not in sys.path:
    sys.path.append('/lib')
# sys.path.insert(0, '/lib/sinric pro')
import time
import os
import machine
import hashlib
import ucryptolib
import network
import gc
import pystone_lowmem
import json
import urequests
import usocket
import ntptime
import ssl
import micropython
import ubluetooth as bt
import zeno
import zfs
import ntptime
from machine import Pin, SPI, I2C, PWM, SoftSPI, RTC
from firmware import DS3231
from firmware import SDCard
from sinricpro import SinricPro
from sinricpro.devices.sinricpro_switch import SinricProSwitch

SD_SCK, SD_MOSI, SD_MISO, SD_CS = 40, 6, 5, 7
LOGS_DIR = "/LOGS"

if not zfs.info()["mounted"]:
    zfs.mount()
# =================================================

try:
    import machine
except ImportError:                      # pragma: no cover - desktop/testing
    machine = None

try:
    import sys as _sys
    _PLATFORM = _sys.platform
except Exception:
    _PLATFORM = "unknown"


# Known safe frequency tiers per platform, in Hz. Extend this as you add
# board support -- unknown platforms fall back to a single-level table
# built from whatever machine.freq() reports at boot, so set_level()
# degrades to a no-op instead of erroring out.
_PLATFORM_LEVELS = {
    "esp32":  {"low": 80_000_000,  "normal": 160_000_000, "high": 240_000_000, "turbo": 240_000_000},
    "rp2":    {"low": 48_000_000,  "normal": 125_000_000, "high": 200_000_000, "turbo": 250_000_000},
}

# ordering used to pick the "highest currently-requested" level
_LEVEL_ORDER = ("low", "normal", "high", "turbo")


class PowerManagement:
    def __init__(self, logger=None):
        self.logger = logger or (Logger() if "Logger" in globals() else None)
        self.source = "POWERMGR"

        self.levels = dict(_PLATFORM_LEVELS.get(_PLATFORM, {}))
        if not self.levels:
            # unknown platform -- build a single-level table around
            # whatever frequency we're actually running at right now
            base = self._raw_freq() or 0
            self.levels = {"low": base, "normal": base, "high": base, "turbo": base}

        self.baseline = "normal" if "normal" in self.levels else _LEVEL_ORDER[0]
        self._requests = {}     # reason -> level, e.g. {"pkg_download": "high"}

    def help(self):
        print("  power                          Show current frequency / active boosts")
        print("  power levels                   List available frequency tiers")
        print("  power set <level>               Manually pin CPU to a tier (low/normal/high/turbo)")
        print("  power boost <reason> [level]    Request a frequency tier, tracked by reason")
        print("  power release <reason>          Release a previous boost request")
        print("  power baseline <level>          Change the default idle tier")

    # =============================================
    # status / introspection
    # =============================================
    def status(self):
        current = self._raw_freq()
        print("\n[Power]")
        print("  Platform      : {}".format(_PLATFORM))
        print("  Current freq  : {}".format(self._fmt_hz(current)))
        print("  Baseline tier : {} ({})".format(self.baseline, self._fmt_hz(self.levels.get(self.baseline))))
        if self._requests:
            print("  Active boosts :")
            for reason, level in self._requests.items():
                print("    - {:<20} -> {}".format(reason, level))
        else:
            print("  Active boosts : none")
        return {"platform": _PLATFORM, "current_hz": current, "requests": dict(self._requests)}

    def levels_list(self):
        print("\n[Power] Available tiers ({}):".format(_PLATFORM))
        for name in _LEVEL_ORDER:
            if name in self.levels:
                print("  {:<8} {}".format(name, self._fmt_hz(self.levels[name])))
        return dict(self.levels)

    # =============================================
    # direct control
    # =============================================
    def set_level(self, level):
        """Pin the CPU to a named tier right now, ignoring any active
        boost requests. Mostly useful for manual/debug use -- normal code
        should prefer boost()/release() so it doesn't clobber other
        requesters."""
        return self._apply(level)

    def baseline_set(self, level):
        """Change the idle/default tier that release() falls back to."""
        if level not in self.levels:
            self._error("Unknown level '{}'".format(level))
            return False
        self.baseline = level
        if not self._requests:
            self._apply(level)
        return True

    # =============================================
    # reference-counted boost / release
    # =============================================
    def boost(self, reason, level="high"):
        """Request a frequency tier under the given reason. If multiple
        reasons are active at once, the highest requested tier wins.
        Call release(reason) with the same reason when done."""
        if level not in self.levels:
            self._error("Unknown level '{}'".format(level))
            return False
        self._requests[reason] = level
        return self._apply(self._highest_requested())

    def release(self, reason):
        """Drop a previous boost request. CPU frequency falls back to the
        next-highest remaining request, or the baseline tier if none are
        left."""
        if reason in self._requests:
            del self._requests[reason]
        target = self._highest_requested() or self.baseline
        return self._apply(target)

    def boosted(self, level="high", reason="context"):
        """Context manager: 'with power.boosted(): do_slow_thing()' boosts
        for the duration of the block and always releases afterwards, even
        if the block raises."""
        return _BoostContext(self, level, reason)

    # =============================================
    # simple demand-based helper
    # =============================================
    def auto_scale(self, load_percent, reason="auto"):
        """Optional convenience for callers that *do* have some notion of
        load (e.g. a queue depth, a request rate, time spent in a loop):
        boost when load is high, release when it drops back down."""
        if load_percent is None:
            return
        if load_percent >= 70:
            self.boost(reason, "high")
        elif load_percent >= 40:
            self.boost(reason, "normal")
        else:
            self.release(reason)

    # =============================================
    # internals
    # =============================================
    def _highest_requested(self):
        active = [lvl for lvl in self._requests.values() if lvl in self.levels]
        if not active:
            return None
        return max(active, key=lambda lvl: _LEVEL_ORDER.index(lvl))

    def _apply(self, level):
        if level not in self.levels:
            self._error("Unknown level '{}'".format(level))
            return False
        hz = self.levels[level]
        if not hz:
            self._error("No known frequency for level '{}' on platform '{}'".format(level, _PLATFORM))
            return False
        if machine is None:
            self._log("machine module unavailable -- would set {} ({})".format(level, self._fmt_hz(hz)))
            return True
        try:
            machine.freq(hz)
            self._log("CPU frequency set to {} ({})".format(level, self._fmt_hz(hz)))
            return True
        except Exception as e:
            self._error("Failed to set frequency: {}".format(e))
            return False

    def _raw_freq(self):
        if machine is None:
            return None
        try:
            return machine.freq()
        except Exception:
            return None

    def _fmt_hz(self, hz):
        if not hz:
            return "unknown"
        return "{:.0f} MHz".format(hz / 1_000_000)

    def _log(self, message):
        if self.logger:
            try:
                self.logger.debug(message, source=self.source)
            except Exception:
                pass

    def _error(self, message):
        if self.logger:
            try:
                self.logger.error(message, source=self.source)
            except Exception:
                pass
        print("[POWER] Error:", message)


class _BoostContext:
    """Backs PowerManagement.boosted() -- a plain class instead of
    @contextmanager so this has no dependency on the 'contextlib' module,
    which may not be present on every MicroPython build."""

    def __init__(self, power, level, reason):
        self.power = power
        self.level = level
        self.reason = reason

    def __enter__(self):
        self.power.boost(self.reason, self.level)
        return self.power

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.power.release(self.reason)
        return False  # never swallow exceptions
            
class CPU:
    def __init__(self, model="ESP32-S3N16R8"):
        self.model = model
        self.usage_pct = 0
        print(self.model)

    # -------------------------------------------------
    # SCHEDULER TAP (DO NOT BLOCK)
    # -------------------------------------------------
    def report_frame(self, busy_us, idle_us):
        total = busy_us + idle_us
        if total <= 0:
            return

        # integer math, cheap
        self.usage_pct = (busy_us * 100) // total

    def usage(self):
        return self.usage_pct

    # -------------------------------------------------
    # POWER / RESET CONTROL (AUTHORITY)
    # -------------------------------------------------
    def reboot(self):
        time.sleep_ms(50)
        machine.reset()

    def shutdown(self):
        time.sleep_ms(100)
        machine.deepsleep()

    def sleep_ms(self, ms):
        machine.lightsleep(ms)

    # -------------------------------------------------
    # CLOCK / FREQUENCY
    # -------------------------------------------------
    def set_freq(self, hz):
        machine.freq(hz)

    def get_freq(self):
        return machine.freq()

    # -------------------------------------------------
    # RESET / WAKE INFO
    # -------------------------------------------------
    def reset_cause(self):
        return machine.reset_cause()

    def wake_reason(self):
        return machine.wake_reason()

    # -------------------------------------------------
    # EMERGENCY
    # -------------------------------------------------
    def panic(self, reason=None):
        try:
            print("[CPU PANIC]", reason)
        except:
            pass
        time.sleep_ms(50)
        machine.reset()

    # -------------------------------------------------
    # CHIP HEALTH (OPTIONAL)
    # -------------------------------------------------
    def chip_temp(self):
        try:
            return esp.raw_temperature()
        except:
            return None

# =============================================================================
# Low-level ZFS text wrapper
# =============================================================================

class dewrapper:
    @staticmethod
    def read(path):
        return zfs.read(path).decode("utf-8")

    @staticmethod
    def write(path, text):
        zfs.write(path, text.encode("utf-8"))

    @staticmethod
    def lines(path):
        return [l.strip() for l in dewrapper.read(path).splitlines() if l.strip()]

    @staticmethod
    def records(path):
        return [line.split("###") for line in dewrapper.lines(path)]


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _makedirs(path):
    if not path:
        return
    cur = ""
    for p in path.split('/'):
        if not p:
            continue
        cur = cur + "/" + p if cur else p
        if not _exists(cur):
            try:
                os.mkdir(cur)
            except Exception:
                pass


# =============================================================================
# Exceptions
# =============================================================================

class PermissionError(Exception):
    pass


class FileNotFoundError(Exception):
    pass


class FileExistsError(Exception):
    pass


class NotADirectoryError(Exception):
    pass


# =============================================================================
# System privilege token -- lets trusted kernel code (PackageManager,
# boot sequencer, FileManager.refresh_tree) run root-gated operations
# independent of whether the interactive user is currently elevated.
# Not a hard security boundary (single-interpreter, no process isolation) --
# it's a seam between trusted and untrusted code paths, same tier as the
# rest of this permission model.
# =============================================================================

try:
    _SYSTEM_TOKEN = os.urandom(16)
except Exception:
    _SYSTEM_TOKEN = bytes(str(time.ticks_us()), "utf-8")


class SystemPrivilege:
    _active_depth = 0

    def __init__(self, token, reason="system operation"):
        if token != _SYSTEM_TOKEN:
            raise PermissionError("invalid system privilege token")
        self.reason = reason

    def __enter__(self):
        SystemPrivilege._active_depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        SystemPrivilege._active_depth -= 1
        return False

    @classmethod
    def active(cls):
        return cls._active_depth > 0

class usermanager:
    # Class-level, shared by every usermanager() instance in the process.
    # Always False on interpreter start -- normal mode by default on
    # every boot, regardless of what "root" says in userinfo.json.
    _session_root = False

    def __init__(self):
        self.path = "OS/users/userinfo.json"
        self.d = dewrapper()

    def _checkpath(self, path):
        return zfs.exists(path)

    def _require_system(self):
        # Internal-only gate. Anything below this line may only be
        # entered from inside a SystemPrivilege(_SYSTEM_TOKEN, ...)
        # block -- i.e. from trusted code in this same class, not from
        # apps, modules, or the shell.
        if not SystemPrivilege.active():
            raise PermissionError("access denied")

    def __read__(self):
        self._require_system()
        if not self._checkpath("OS"):
            zfs.mkdir("OS")
        if not self._checkpath("OS/users"):
            zfs.mkdir("OS/users")
        if not self._checkpath(self.path):
            self.d.write(self.path, json.dumps({
                "user": zeno.user, "password": zeno.password, "root": False
            }))

        data = self.d.read(self.path)
        if not data:
            zfs.delete(self.path)
            return self.__read__()
        try:
            parsed = json.loads(data)
        except Exception:
            zfs.delete(self.path)
            return self.__read__()

        # zeno.py is the source of truth for identity. If the stored
        # record belongs to a different user than the one zeno.py
        # currently declares, resync the record instead of silently
        # comparing against stale credentials.
        if parsed.get("user") != zeno.user:
            parsed = {"user": zeno.user, "password": zeno.password, "root": False}
            self.d.write(self.path, json.dumps(parsed))

        return parsed

    def _write(self, key, value):
        self._require_system()
        a = self.__read__()
        a[key] = value
        if self._checkpath(self.path):
            self.d.write(self.path, json.dumps(a))

    def __username__(self):
        self._require_system()
        return self.__read__()["user"]

    def __password__(self):
        self._require_system()
        return self.__read__()["password"]

    def userinfo(self):
        with SystemPrivilege(_SYSTEM_TOKEN, "userinfo"):
            info = dict(self.__read__())
        info.pop("password", None)
        return info

    def removeuser(self, name):
        with SystemPrivilege(_SYSTEM_TOKEN, "removeuser"):
            user = self.__username__()
            data = self.__read__()
            if name == user and data["root"] is True:
                print(f"{user} is deleted and default user name will be created")
                zfs.delete(self.path)
                self.__read__()
            else:
                print("user does't exists or not root")

    def is_session_root(self):
        """The only thing permission checks should ever call."""
        return usermanager._session_root or SystemPrivilege.active()

    def elevate(self, user, password):
        with SystemPrivilege(_SYSTEM_TOKEN, "elevate"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
            if ok and not self.__read__().get("root", False):
                self._write("root", True)
                print(f"{user} promoted to administrator")
        if ok:
            usermanager._session_root = True
            print("elevated user (this session)")
        else:
            print("user or password is wrong")

    def delevate(self, user, password):
        with SystemPrivilege(_SYSTEM_TOKEN, "delevate"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
        if ok:
            usermanager._session_root = False
            print("delevated user")
        else:
            print("user or password is wrong")

    def isrooted(self, user):
        """True only if elevated THIS session -- not just an admin
        account on paper."""
        with SystemPrivilege(_SYSTEM_TOKEN, "isrooted"):
            current = self.__username__()
        if user == current:
            return self.is_session_root()
        return False

    # ---------------- self-service identity changes ----------------

    def change_password(self, user, old_password, new_password):
        """Change the account password. Requires the current password
        (not just root) -- same as delevate/elevate."""
        with SystemPrivilege(_SYSTEM_TOKEN, "change_password"):
            ok = (user == self.__username__() and str(old_password) == str(self.__password__()))
            if ok:
                self._write("password", new_password)
        if ok:
            print("password changed")
            return True
        print("user or password is wrong")
        return False

    def change_username(self, user, password, new_username):
        """Change the stored username. Requires the current password.
        Note: this only updates userinfo.json -- if zeno.py's `user`
        field doesn't match afterward, __read__() will resync the
        record back to zeno.user on next access, undoing this. Update
        zeno.py's `user` value too if you want the change to persist."""
        with SystemPrivilege(_SYSTEM_TOKEN, "change_username"):
            ok = (user == self.__username__() and str(password) == str(self.__password__()))
            if ok:
                self._write("user", new_username)
        if ok:
            print(f"username changed to {new_username}")
            return True
        print("user or password is wrong")
        return False
    def rebuild(self, system_token=None):
        if system_token != _SYSTEM_TOKEN:
            raise PermissionError("invalid system privilege token")

        with SystemPrivilege(system_token, "rebuild"):
            if not self._checkpath("OS"):
                zfs.mkdir("OS")
            if not self._checkpath("OS/users"):
                zfs.mkdir("OS/users")

            if self._checkpath(self.path):
                zfs.delete(self.path)

            fresh = {"user": zeno.user, "password": zeno.password, "root": False}
            self.d.write(self.path, json.dumps(fresh))

            # verify it round-trips before declaring success
            try:
                check = json.loads(self.d.read(self.path))
            except Exception:
                raise ProcessError("rebuild: wrote userinfo.json but could not read it back")
            if check.get("user") != zeno.user:
                raise ProcessError("rebuild: verification failed after write")

        usermanager._session_root = False
        return True
    def current_user(self):
        """Public accessor -- safe for external callers (PackageManager,
        etc). Does not require system privilege."""
        with SystemPrivilege(_SYSTEM_TOKEN, "current_user"):
            return self.__username__()
# =============================================================================
# FileManager -- VFS facade. Apps must never touch .vfs files or `os`
# directly; everything goes through here.
# =============================================================================

SYS_DIR = "/.sys"

PROGRAM_EXT = (".py", ".mpy", ".zsh")
TEXT_EXT = (".txt", ".md", ".log")
BITMAP_EXT = (".bmp",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
AUDIO_EXT = (".mp3", ".wav", ".ogg")
VIDEO_EXT = (".mp4", ".avi", ".mov")
JSON_EXT = (".json",)
ARCHIVE_EXT = (".zip", ".tar", ".gz")

EXECUTABLE_EXT = (".py", ".mpy")     # auto-executable by default
ROOT_OWNED_EXT = (".py", ".mpy", ".zsh")

PERM_FULL, PERM_READ, PERM_READWRITE, PERM_NONE = 0, 1, 2, 3
PERM_READEXEC, PERM_WRITEEXEC, PERM_WRITE, PERM_EXEC = 4, 5, 6, 7

READ_PERMS = {PERM_FULL, PERM_READ, PERM_READWRITE, PERM_READEXEC}
WRITE_PERMS = {PERM_FULL, PERM_READWRITE, PERM_WRITEEXEC, PERM_WRITE}
EXEC_PERMS = {PERM_FULL, PERM_READEXEC, PERM_WRITEEXEC, PERM_EXEC}
VALID_PERMS = {0, 1, 2, 3, 4, 5, 6, 7}

S_IFDIR = 0x4000


def _os_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _os_isdir(path):
    try:
        return bool(os.stat(path)[0] & S_IFDIR)
    except OSError:
        return False


def _os_mkdir(path):
    if not _os_exists(path):
        os.mkdir(path)


def _os_stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return False, 0
    is_dir = bool(st[0] & S_IFDIR)
    return is_dir, (0 if is_dir else (st[6] if len(st) > 6 else 0))


def _os_rmnode(path):
    (os.rmdir if _os_isdir(path) else os.remove)(path)


def _os_rename(old, new):
    os.rename(old, new)


def _os_listdir(path):
    return os.listdir(path)


def _os_read(path):
    with open(path, "r") as f:
        return f.read()


def _os_write(path, data):
    with open(path, "w") as f:
        f.write(data)


def _norm(path):
    if not path:
        return "/"
    parts = [p for p in str(path).split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


def _parent(path):
    path = _norm(path)
    if path == "/":
        return "/"
    idx = path.rfind("/")
    return path[:idx] if idx > 0 else "/"


def _name(path):
    path = _norm(path)
    return "" if path == "/" else path.rsplit("/", 1)[-1]


def _join(parent, name):
    parent = _norm(parent)
    return name if parent == "/" else parent + "/" + name


def _ext(name):
    idx = name.rfind(".")
    return "" if idx <= 0 else name[idx:].lower()


def _mirror_dir_for(path):
    path = _norm(path)
    return SYS_DIR if path == "/" else SYS_DIR + path


def _meta_file_for(path):
    path = _norm(path)
    return SYS_DIR + "/root.vfs" if path == "/" else SYS_DIR + path + ".vfs"


def is_hidden(name):
    return bool(name) and name.startswith(".")


class FileManager:
    def __init__(self):
        self.um = usermanager()
        self._fd_table = {}
        self._next_fd = 3
        self._bootstrap()

    def _bootstrap(self):
        if not _os_exists(SYS_DIR):
            _os_mkdir(SYS_DIR)
        if not _os_exists(_meta_file_for("/")):
            self._rebuild_meta("/")

    def _current_user(self):
        return self.um.userinfo().get("user", "unknown")

    def _is_root(self):
        return self.um.is_session_root()

    def _check_permission(self, path, action):
        if self._is_root():
            return True
        perm = self.metadata(path).get("permission", PERM_NONE)
        allowed = {"read": READ_PERMS, "write": WRITE_PERMS, "exec": EXEC_PERMS}[action]
        if perm not in allowed:
            raise PermissionError(
                "%s: %s access denied for user '%s'" % (path, action, self._current_user())
            )
        return True

    def _filetype(self, name, is_dir):
        if is_dir:
            return "directory"
        ext = _ext(name)
        for exts, label in (
            (PROGRAM_EXT, "program"), (TEXT_EXT, "text"), (BITMAP_EXT, "bitmap"),
            (IMAGE_EXT, "image"), (AUDIO_EXT, "audio"), (VIDEO_EXT, "video"),
            (JSON_EXT, "json"), (ARCHIVE_EXT, "archive"),
        ):
            if ext in exts:
                return label
        return "unknown"

    def _default_owner(self, name):
        return "root" if _ext(name) in ROOT_OWNED_EXT else self._current_user()

    def _default_permission(self, name, is_dir):
        if is_dir:
            return PERM_FULL
        return PERM_READEXEC if _ext(name) in EXECUTABLE_EXT else PERM_READWRITE

    def _scan_entry(self, parent, name):
        is_dir, size = _os_stat(_join(parent, name))
        return {
            "owner": self._default_owner(name),
            "permission": self._default_permission(name, is_dir),
            "type": self._filetype(name, is_dir),
            "size": size,
        }

    def _ensure_dir_chain(self, path):
        path = _norm(path)
        if path == "/" or _os_exists(path):
            return
        self._ensure_dir_chain(_parent(path))
        _os_mkdir(path)

    def _rebuild_meta(self, dirpath):
        dirpath = _norm(dirpath)
        names = _os_listdir(dirpath) if _os_exists(dirpath) else []
        meta = {}
        for n in names:
            if dirpath == "/" and n == _name(SYS_DIR):
                continue
            meta[n] = self._scan_entry(dirpath, n)
        self._write_meta(dirpath, meta)
        return meta

    def _read_meta(self, dirpath):
        dirpath = _norm(dirpath)
        meta_file = _meta_file_for(dirpath)
        if not _os_exists(meta_file):
            return self._rebuild_meta(dirpath)
        raw = _os_read(meta_file)
        if not raw:
            return self._rebuild_meta(dirpath)
        try:
            return json.loads(raw)
        except Exception:
            return self._rebuild_meta(dirpath)

    def _write_meta(self, dirpath, data, _skip_ensure=False):
        dirpath = _norm(dirpath)
        meta_file = _meta_file_for(dirpath)
        if not _skip_ensure:
            container = SYS_DIR if dirpath == "/" else _mirror_dir_for(_parent(dirpath))
            self._ensure_dir_chain(container)
        _os_write(meta_file, json.dumps(data))

    def exists(self, path):
        return _os_exists(_norm(path))

    def metadata(self, path):
        path = _norm(path)
        if path == "/":
            return {"owner": "root", "permission": PERM_FULL, "type": "directory", "size": 0}
        if not self.exists(path):
            raise FileNotFoundError(path)
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        if name not in meta:
            meta = self._rebuild_meta(parent)
        return dict(meta.get(name) or self._scan_entry(parent, name))

    def listdir(self, path="/", show_hidden=False):
        path = _norm(path)
        if not self.exists(path):
            raise FileNotFoundError(path)
        if not _os_isdir(path) and path != "/":
            raise NotADirectoryError(path)
        self._check_permission(path, "read")

        meta = self._read_meta(path)
        real_names = _os_listdir(path)
        if path == "/":
            real_names = [n for n in real_names if n != _name(SYS_DIR)]

        changed = False
        for n in real_names:
            if n not in meta:
                meta[n] = self._scan_entry(path, n)
                changed = True
        for n in list(meta.keys()):
            if n not in real_names:
                del meta[n]
                changed = True
        if changed:
            self._write_meta(path, meta)

        names = real_names if show_hidden else [n for n in real_names if not is_hidden(n)]
        return sorted(names)

    def create(self, path, content="", owner=None, permission=None):
        path = _norm(path)
        parent, name = _parent(path), _name(path)
        if not self.exists(parent):
            raise FileNotFoundError(parent)
        if self.exists(path):
            raise FileExistsError(path)
        self._check_permission(parent, "write")

        owner = owner if owner is not None else self._default_owner(name)
        permission = permission if permission is not None else self._default_permission(name, False)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))

        _os_write(path, content)
        _, size = _os_stat(path)
        meta = self._read_meta(parent)
        meta[name] = {"owner": owner, "permission": permission, "type": self._filetype(name, False), "size": size}
        self._write_meta(parent, meta)
        return True

    def mkdir(self, path, owner=None, permission=None):
        path = _norm(path)
        if path == "/":
            raise FileExistsError("root already exists")
        parent, name = _parent(path), _name(path)
        if not self.exists(parent):
            raise FileNotFoundError(parent)
        if self.exists(path):
            raise FileExistsError(path)
        self._check_permission(parent, "write")

        owner = owner if owner is not None else self._default_owner(name)
        permission = permission if permission is not None else self._default_permission(name, True)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))

        _os_mkdir(path)
        meta = self._read_meta(parent)
        meta[name] = {"owner": owner, "permission": permission, "type": "directory", "size": 0}
        self._write_meta(parent, meta)

        self._ensure_dir_chain(_mirror_dir_for(path))
        self._write_meta(path, {}, _skip_ensure=True)
        return True

    def delete(self, path):
        path = _norm(path)
        if path == "/":
            raise PermissionError("cannot delete root")
        if not self.exists(path):
            raise FileNotFoundError(path)
        parent, name = _parent(path), _name(path)
        self._check_permission(parent, "write")

        if _os_isdir(path):
            for child in _os_listdir(path):
                self.delete(_join(path, child))
            _os_rmnode(path)
            if _os_exists(_meta_file_for(path)):
                _os_rmnode(_meta_file_for(path))
            if _os_exists(_mirror_dir_for(path)):
                _os_rmnode(_mirror_dir_for(path))
        else:
            _os_rmnode(path)

        meta = self._read_meta(parent)
        if name in meta:
            del meta[name]
            self._write_meta(parent, meta)
        return True

    def rename(self, path, new_name):
        path = _norm(path)
        parent, old_name = _parent(path), _name(path)
        if "/" in new_name:
            new_path = _norm(new_name)
            if _parent(new_path) != parent:
                raise ValueError("rename() cannot change directory, use move()")
            new_name_only = _name(new_path)
        else:
            new_name_only = new_name
            new_path = _join(parent, new_name_only)

        if not self.exists(path):
            raise FileNotFoundError(path)
        if self.exists(new_path):
            raise FileExistsError(new_path)
        self._check_permission(parent, "write")

        is_dir = _os_isdir(path)
        _os_rename(path, new_path)

        meta = self._read_meta(parent)
        entry = meta.pop(old_name, None) or self._scan_entry(parent, new_name_only)
        meta[new_name_only] = entry
        self._write_meta(parent, meta)

        if is_dir:
            if _os_exists(_meta_file_for(path)):
                _os_rename(_meta_file_for(path), _meta_file_for(new_path))
            if _os_exists(_mirror_dir_for(path)):
                _os_rename(_mirror_dir_for(path), _mirror_dir_for(new_path))
        return True

    def move(self, src, dst):
        src, dst = _norm(src), _norm(dst)
        if not self.exists(src):
            raise FileNotFoundError(src)
        if self.exists(dst):
            raise FileExistsError(dst)
        src_parent, src_name = _parent(src), _name(src)
        dst_parent, dst_name = _parent(dst), _name(dst)
        if not self.exists(dst_parent):
            raise FileNotFoundError(dst_parent)
        if src_parent == dst_parent:
            return self.rename(src, dst_name)

        self._check_permission(src_parent, "write")
        self._check_permission(dst_parent, "write")

        is_dir = _os_isdir(src)
        _os_rename(src, dst)

        src_meta = self._read_meta(src_parent)
        entry = src_meta.pop(src_name, None)
        self._write_meta(src_parent, src_meta)

        dst_meta = self._read_meta(dst_parent)
        dst_meta[dst_name] = entry or self._scan_entry(dst_parent, dst_name)
        self._write_meta(dst_parent, dst_meta)

        if is_dir:
            if _os_exists(_meta_file_for(src)):
                _os_rename(_meta_file_for(src), _meta_file_for(dst))
            if _os_exists(_mirror_dir_for(src)):
                _os_rename(_mirror_dir_for(src), _mirror_dir_for(dst))
        return True

    def copy(self, src, dst):
        src, dst = _norm(src), _norm(dst)
        if not self.exists(src):
            raise FileNotFoundError(src)
        if self.exists(dst):
            raise FileExistsError(dst)
        src_parent, dst_parent = _parent(src), _parent(dst)
        if not self.exists(dst_parent):
            raise FileNotFoundError(dst_parent)
        self._check_permission(src_parent, "read")
        self._check_permission(dst_parent, "write")

        src_entry = self._read_meta(src_parent).get(_name(src), {})
        if _os_isdir(src):
            self.mkdir(dst, owner=src_entry.get("owner"), permission=src_entry.get("permission"))
            for child in _os_listdir(src):
                self.copy(_join(src, child), _join(dst, child))
        else:
            self.create(dst, _os_read(src) or "", owner=src_entry.get("owner"), permission=src_entry.get("permission"))
        return True

    def chmod(self, path, permission):
        path = _norm(path)
        if permission not in VALID_PERMS:
            raise ValueError("invalid permission: %r" % (permission,))
        if path == "/":
            raise PermissionError("cannot chmod root")
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        entry = meta.get(name)
        if entry is None:
            if not self.exists(path):
                raise FileNotFoundError(path)
            entry = self._scan_entry(parent, name)
        if not self._is_root() and entry.get("owner") != self._current_user():
            raise PermissionError("only the owner or root may chmod '%s'" % path)
        entry["permission"] = permission
        meta[name] = entry
        self._write_meta(parent, meta)
        return True

    def chown(self, path, new_owner):
        path = _norm(path)
        if path == "/":
            raise PermissionError("cannot chown root")
        if not self._is_root():
            raise PermissionError("only root may chown")
        parent, name = _parent(path), _name(path)
        meta = self._read_meta(parent)
        entry = meta.get(name)
        if entry is None:
            if not self.exists(path):
                raise FileNotFoundError(path)
            entry = self._scan_entry(parent, name)
        entry["owner"] = new_owner
        meta[name] = entry
        self._write_meta(parent, meta)
        return True

    def refresh_tree(self, path="/", system_token=None):
        """Permission gate only -- delegates to _refresh_tree_impl().
        Pass system_token=_SYSTEM_TOKEN for trusted kernel callers that
        need this to work without an elevated interactive session."""
        if system_token is not None:
            if system_token != _SYSTEM_TOKEN:
                raise PermissionError("invalid system privilege token")
            with SystemPrivilege(system_token, "refresh_tree"):
                return self._refresh_tree_impl(path)
        if not self._is_root():
            raise PermissionError("only root may refresh the metadata tree")
        return self._refresh_tree_impl(path)

    def _refresh_tree_impl(self, path="/"):
        """No permission check of its own -- always go through refresh_tree()."""
        path = _norm(path)
        if not self.exists(path):
            raise FileNotFoundError(path)
        if path != "/" and not _os_isdir(path):
            raise NotADirectoryError(path)

        refreshed = 0
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                names = _os_listdir(current)
            except OSError:
                continue
            meta = {}
            for n in names:
                if current == "/" and n == _name(SYS_DIR):
                    continue
                entry = self._scan_entry(current, n)
                meta[n] = entry
                if entry["type"] == "directory":
                    stack.append(_join(current, n))
            self._write_meta(current, meta)
            refreshed += 1
        return refreshed

    def open(self, path, mode="r"):
        path = _norm(path)
        parent = _parent(path)
        wants_write = any(c in mode for c in "wa+")
        wants_read = "r" in mode or "+" in mode

        if not self.exists(path):
            if wants_write:
                self._check_permission(parent, "write")
                self.create(path, "")
            else:
                raise FileNotFoundError(path)

        if wants_read:
            self._check_permission(path, "read")
        if wants_write:
            self._check_permission(path, "write")

        handle = open(path, mode)
        fd = self._next_fd
        self._next_fd += 1
        self._fd_table[fd] = handle
        return fd

    def _handle(self, fd):
        handle = self._fd_table.get(fd)
        if handle is None:
            raise ValueError("invalid file descriptor: %r" % (fd,))
        return handle

    def read(self, fd, size=-1):
        return self._handle(fd).read(size)

    def write(self, fd, data):
        return self._handle(fd).write(data)

    def close(self, fd):
        handle = self._fd_table.pop(fd, None)
        if handle is None:
            raise ValueError("invalid file descriptor: %r" % (fd,))
        handle.close()
        return True


# =============================================================================
# Logger
# =============================================================================

class Logger:
    LEVELS = {0: "ERROR", 1: "WARNING", 2: "DEBUG"}

    def __init__(self, log_file_user="/LOGS/systemlog.txt", boot=False):
        self.i2c = I2C(0, scl=Pin(4), sda=Pin(5))
        self.rtc = DS3231(self.i2c)
        self.boot = boot
        self.log_file_user = log_file_user
        self._create_file(self.log_file_user)
        self._boot_marker = "[BOOT_START]"

        if self.boot:
            self._write(self._boot_marker)
            self.debug("Logger initialized. Boot starting...", source="BOOT")

    def _create_file(self, path):
        try:
            with open(path, "a"):
                pass
            return True
        except Exception as e:
            print("[Logger] File creation failed:", e)
            return False

    def _write(self, text):
        if debug_log_enabled:
            print(text)
        try:
            with open(self.log_file_user, "a") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def log(self, level, message, source="GENERAL"):
        self._write(f"[SRC:{source}] [{self.LEVELS.get(level, 'UNKNOWN')}] {message}")

    def error(self, message, source="GENERAL"):
        self.log(0, message, source)

    def warning(self, message, source="GENERAL"):
        self.log(1, message, source)

    def debug(self, message, source="GENERAL"):
        self.log(2, message, source)

    def boot_complete(self):
        self.debug("Boot sequence complete.", source="BOOT")
        self._write("_" * 40)

    def viewlogs(self, lines=None):
        try:
            with open(self.log_file_user, "r") as f:
                data = f.read()
        except Exception as e:
            print("[Logger] Failed to read logs:", e)
            return

        logs = data.strip().split("\n")
        last_boot_index = 0
        for i, line in enumerate(logs):
            if line.startswith("[BOOT_START]"):
                last_boot_index = i
        logs = logs[last_boot_index:]
        if lines:
            logs = logs[-lines:]
        print("\n".join(logs))

    def clear_logs(self):
        try:
            with open(self.log_file_user, "w") as f:
                f.write("")
            print("[Logger] Logs cleared successfully.")
            self.debug("Logs cleared successfully by system.", source="LOGGER")
        except Exception as e:
            print("[Logger] Failed to clear logs:", e)
            self.error(f"Failed to clear logs: {e}", source="LOGGER")

class Disk:
    def __init__(self, mount_point="/MemDisk"):
        """
        Initializes the Disk controller for hardware mounting, checking, info, and formatting.
        
        Args:
            logger: An instance of your custom Logger class.
            mount_point (str): Mount path for the SD card system.
        """
        self.log = zeno.log
        self.mount_point = mount_point
        self.spi = SPI(1, baudrate=20_000_000, polarity=0, phase=0,
                        sck=Pin(SD_SCK, Pin.OUT), mosi=Pin(SD_MOSI, Pin.OUT), miso=Pin(SD_MISO, Pin.OUT))
        self.sd = None
        self.cs = Pin(SD_CS, Pin.OUT)

    def check(self, retries=5, delay=0.2):
        for i in range(retries):
            try:
                os.listdir(self.mount_point)
                if i > 0:
                    self.log.debug("SD mount became available after {} retries".format(i + 1), source="DISK")
                return True
            except OSError:
                time.sleep(delay)
        self.log.error("SD card not accessible at '{}' after {} retries".format(self.mount_point, retries), source="DISK")
        return False

    def begin(self):
        try:
            self.sd = SDCard(self.spi, self.cs)
            os.mount(self.sd, self.mount_point)
            self.log.debug("SD card mounted at '{}'".format(self.mount_point), source="DISK")
            return True
        except Exception as e:
            self.sd = None
            self.log.error("SD init/mount failed for '{}': {}".format(self.mount_point, e), source="DISK")

        if self.check():
            self.log.warning("SD init failed, but '{}' appears to be accessible".format(self.mount_point), source="DISK")
            return True
        return False

    def unmount(self):
        try:
            os.umount(self.mount_point)
            self.log.debug("SD card unmounted from '{}'".format(self.mount_point), source="DISK")
            return True
        except Exception as e:
            self.log.error("Failed to unmount '{}': {}".format(self.mount_point, e), source="DISK")
            return False

    def format(self, filesystem=os.VfsFat):
        """
        Formats the storage device attached to the SD card.
        Requires the SD card to be initialized/present.
        """
        if not self.sd:
            self.log.error("Cannot format: SD card object not initialized", source="DISK")
            return False
        try:
            # Ensure it is unmounted before formatting if needed, or format the block device directly
            try:
                os.umount(self.mount_point)
            except Exception:
                pass
            
            filesystem.mkfs(self.sd)
            os.mount(self.sd, self.mount_point)
            self.log.debug("Disk formatted successfully and re-mounted at '{}'".format(self.mount_point), source="DISK")
            return True
        except Exception as e:
            self.log.error("Failed to format disk at '{}': {}".format(self.mount_point, e), source="DISK")
            return False

    def info(self, path=None):
        path = path or self.mount_point
        try:
            stats = os.statvfs(path)
            total_bytes = stats[2] * stats[0]
            free_bytes = stats[3] * stats[0]

            def convert(v):
                return "{:.2f} GB".format(v / 1024**3) if v >= 1024**3 else "{:.2f} MB".format(v / 1024**2)

            print("Path        :", path)
            print("Volume Name:", path.split("/")[-1])
            print("Total Size :", convert(total_bytes))
            print("Free Space :", convert(free_bytes))
            self.log.debug("Disk info for '{}': total={}, free={}".format(path, convert(total_bytes), convert(free_bytes)), source="DISK")
        except Exception as e:
            self.log.error("Cannot access '{}': {}".format(path, e), source="DISK")

class BootConfig:
    def __init__(self):
        self.default = {
            "BOOT_MODE": "NORMAL", "OPT_LEVEL": 0, "WIFI_AUTOCONNECT": True,
            "SHOW_UI": True, "KERNEL_PATH": "/SYSTEM32/Admin/ROM/kernel.py",
            "LOGGER_STATUS": "ENABLED", "LOG_REPL": "ENABLED", "MODE": "PERFORMANCE",
        }
        self.cfg_name = "bootcfg.json"
        self.cfg_dir = "/LOGS"
        self.cfg_path = self.cfg_dir + "/" + self.cfg_name
        self.config = {}

        try:
            files = os.listdir(self.cfg_dir)
        except Exception:
            os.mkdir(self.cfg_dir)
            files = []

        if self.cfg_name in files:
            try:
                with open(self.cfg_path, "r") as f:
                    self.config = json.loads(f.read())
            except Exception as e:
                print("[BOOTCFG] Load or parse failed:", e)
                self.config = dict(self.default)
                self.save()
        else:
            self.config = dict(self.default)
            self.save()

    def save(self):
        try:
            with open(self.cfg_path, "w") as f:
                json.dump(self.config, f)
        except Exception as e:
            print("[BOOTCFG] Save failed:", e)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save()

    def show(self):
        print("\n[Boot Configuration]")
        for k, v in self.config.items():
            print(" ", k, ":", v)
        print()




debug_log_enabled = False
bootcfg = BootConfig()
cfg = getattr(bootcfg, "config", {}) or {}


def cfg_get(cfg, *keys):
    for k in keys:
        if k in cfg:
            return cfg[k]


try:
    import _thread
    _HAVE_THREAD = True
except ImportError:
    _HAVE_THREAD = False

try:
    import urandom
    _HAVE_URANDOM = True
except ImportError:
    _HAVE_URANDOM = False
PERSIST_PATH = "/LOGS/proc_state.json"

#   1xxx  KERNEL    -- boot/UI/core system processes, owner=root
#   2xxx  USER      -- ordinary cooperative apps (loop/periodic/once)
#   3xxx  THREAD    -- real _thread-backed concurrent tasks
#   4xxx  DAEMON    -- root-owned background housekeeping (guardian, etc)
#   5xxx  NETWORK   -- networking / IO-bound tasks
#   9xxx  RESERVED  -- explicitly requested critical/system pids only
PID_TYPE_KERNEL   = 1
PID_TYPE_USER     = 2
PID_TYPE_THREAD   = 3
PID_TYPE_DAEMON   = 4
PID_TYPE_NETWORK  = 5
PID_TYPE_RESERVED = 9

PID_TYPE_NAMES = {
    PID_TYPE_KERNEL:   "KERNEL",
    PID_TYPE_USER:     "USER",
    PID_TYPE_THREAD:   "THREAD",
    PID_TYPE_DAEMON:   "DAEMON",
    PID_TYPE_NETWORK:  "NETWORK",
    PID_TYPE_RESERVED: "RESERVED",
}


def pid_type(pid):
    """Return the type digit encoded in a pid (its thousands place)."""
    return pid // 1000


def pid_type_name(pid):
    return PID_TYPE_NAMES.get(pid_type(pid), "UNKNOWN")

# ---------------- process states ----------------
NEW     = "NEW"
READY   = "READY"
RUNNING = "RUNNING"
BLOCKED = "BLOCKED"
ZOMBIE  = "ZOMBIE"     # finished/killed, exit info not yet reaped
DEAD    = "DEAD"       # reaped

# ---------------- signals ------------------------
SIGTERM = 15   # "please stop, next checkpoint"
SIGKILL = 9    # cooperative tasks: removed immediately, no cleanup call
SIGSTOP = 19   # pause (skip scheduling) without killing
SIGCONT = 18   # resume from SIGSTOP

MODE_LOOP     = "loop"      # runs every time it's scheduled
MODE_PERIODIC = "periodic"  # runs every `period` ms
MODE_ONCE     = "once"      # runs once then becomes ZOMBIE
MODE_THREAD   = "thread"    # runs on a real FreeRTOS thread via _thread

NICE_MIN, NICE_MAX = -20, 19


def _weight(nice):
    """Same idea as Linux's sched_prio_to_weight table, simplified:
    lower nice = more weight = scheduled more often. Weight halves
    every ~4 nice levels."""
    nice = max(NICE_MIN, min(NICE_MAX, nice))
    return max(1, 1024 >> ((nice + 20) // 4))


class ProcessError(Exception):
    pass


class PermissionDenied(ProcessError):
    pass


class Process:
    """PCB -- Process Control Block."""

    __slots__ = (
        "pid", "ppid", "owner", "name", "func", "mode", "period",
        "priority", "state", "vruntime", "pending_signal",
        "exit_code", "meta", "_last_run", "_thread_started",
    )

    def __init__(self, pid, ppid, owner, name, func, mode, period, priority):
        self.pid = pid
        self.ppid = ppid
        self.owner = owner          # username, or "root" for kernel/system procs
        self.name = name
        self.func = func
        self.mode = mode
        self.period = period
        self.priority = priority
        self.state = NEW
        self.vruntime = 0           # accumulated weighted runtime (us)
        self.pending_signal = None
        self.exit_code = None
        self.meta = {"samples": 0, "avg_us": None, "min_us": None,
                     "max_us": None, "last_us": None}
        self._last_run = 0
        self._thread_started = False

    def weight(self):
        return _weight(self.priority)

    def as_row(self):
        return "{:<5} {:<8} {:<8} {:<16} {:<9} {:<4} {:<8} {}".format(
            self.pid, pid_type_name(self.pid), self.owner, self.name,
            self.state, self.priority, self.mode,
            self.meta.get("avg_us") or "-"
        )


# =================================================
# Scheduler -- Zeno OS process scheduler
# =================================================
# Now actually drives PowerManagement instead of leaving it as an unused
# side service. Two independent signals feed it, both through the normal
# boost()/release() reference-counted request table (so PERFORMANCE /
# POWERSAVING policy in PowerManagement still pins the hardware at an
# extreme no matter what either signal below asks for -- only DYNAMIC
# policy actually lets them move the CPU):
#
#   1. FRAME-LEVEL LOAD -- once per scheduler frame, the whole-system
#      busy/idle ratio already computed by CPU.report_frame() is fed to
#      power.auto_scale(cpu.usage()). This is the "balance" in dynamic
#      mode: sustained high load across all tasks raises the tier,
#      sustained low load drops it back to baseline.
#
#   2. PER-TASK LOAD -- every cooperative task already tracks a running
#      avg_us (exponential moving average of its own execution time).
#      A task whose average crosses TASK_BOOST_THRESHOLD_US -- i.e. a
#      genuine "large loop" that keeps being expensive every time it's
#      scheduled -- gets its own named boost request that persists for
#      as long as it stays expensive. A "small function" that never
#      crosses the threshold never requests anything -- no boost, no
#      thrash, no wasted frequency switch. We wait for a few samples
#      (TASK_BOOST_MIN_SAMPLES) before acting either way, so a single
#      slow first-call doesn't flip the tier off one data point.
#
# Thread-mode tasks (mode=MODE_THREAD, e.g. daemons like the guardian)
# are intentionally left out of per-task boosting: they run once as a
# persistent thread rather than being re-scheduled per-tick, so a
# per-call average isn't meaningful for them, and pinning frequency high
# for a thread's entire lifetime (which may be "forever, mostly asleep")
# would fight the dynamic policy instead of serving it.

import time
import json
import os

try:
    import _thread
    _HAVE_THREAD = True
except ImportError:
    _HAVE_THREAD = False

try:
    import urandom
    _HAVE_URANDOM = True
except ImportError:
    _HAVE_URANDOM = False

PERSIST_PATH = "/LOGS/proc_state.json"

#   1xxx  KERNEL    -- boot/UI/core system processes, owner=root
#   2xxx  USER      -- ordinary cooperative apps (loop/periodic/once)
#   3xxx  THREAD    -- real _thread-backed concurrent tasks
#   4xxx  DAEMON    -- root-owned background housekeeping (guardian, etc)
#   5xxx  NETWORK   -- networking / IO-bound tasks
#   9xxx  RESERVED  -- explicitly requested critical/system pids only
PID_TYPE_KERNEL   = 1
PID_TYPE_USER     = 2
PID_TYPE_THREAD   = 3
PID_TYPE_DAEMON   = 4
PID_TYPE_NETWORK  = 5
PID_TYPE_RESERVED = 9

PID_TYPE_NAMES = {
    PID_TYPE_KERNEL:   "KERNEL",
    PID_TYPE_USER:     "USER",
    PID_TYPE_THREAD:   "THREAD",
    PID_TYPE_DAEMON:   "DAEMON",
    PID_TYPE_NETWORK:  "NETWORK",
    PID_TYPE_RESERVED: "RESERVED",
}


def pid_type(pid):
    """Return the type digit encoded in a pid (its thousands place)."""
    return pid // 1000


def pid_type_name(pid):
    return PID_TYPE_NAMES.get(pid_type(pid), "UNKNOWN")

# ---------------- process states ----------------
NEW     = "NEW"
READY   = "READY"
RUNNING = "RUNNING"
BLOCKED = "BLOCKED"
ZOMBIE  = "ZOMBIE"     # finished/killed, exit info not yet reaped
DEAD    = "DEAD"       # reaped

# ---------------- signals ------------------------
SIGTERM = 15   # "please stop, next checkpoint"
SIGKILL = 9    # cooperative tasks: removed immediately, no cleanup call
SIGSTOP = 19   # pause (skip scheduling) without killing
SIGCONT = 18   # resume from SIGSTOP

MODE_LOOP     = "loop"      # runs every time it's scheduled
MODE_PERIODIC = "periodic"  # runs every `period` ms
MODE_ONCE     = "once"      # runs once then becomes ZOMBIE
MODE_THREAD   = "thread"    # runs on a real FreeRTOS thread via _thread

NICE_MIN, NICE_MAX = -20, 19


def _weight(nice):
    """Same idea as Linux's sched_prio_to_weight table, simplified:
    lower nice = more weight = scheduled more often. Weight halves
    every ~4 nice levels."""
    nice = max(NICE_MIN, min(NICE_MAX, nice))
    return max(1, 1024 >> ((nice + 20) // 4))


class ProcessError(Exception):
    pass


class PermissionDenied(ProcessError):
    pass


class Process:
    """PCB -- Process Control Block."""

    __slots__ = (
        "pid", "ppid", "owner", "name", "func", "mode", "period",
        "priority", "state", "vruntime", "pending_signal",
        "exit_code", "meta", "_last_run", "_thread_started",
    )

    def __init__(self, pid, ppid, owner, name, func, mode, period, priority):
        self.pid = pid
        self.ppid = ppid
        self.owner = owner          # username, or "root" for kernel/system procs
        self.name = name
        self.func = func
        self.mode = mode
        self.period = period
        self.priority = priority
        self.state = NEW
        self.vruntime = 0           # accumulated weighted runtime (us)
        self.pending_signal = None
        self.exit_code = None
        self.meta = {"samples": 0, "avg_us": None, "min_us": None,
                     "max_us": None, "last_us": None}
        self._last_run = 0
        self._thread_started = False

    def weight(self):
        return _weight(self.priority)

    def as_row(self):
        return "{:<5} {:<8} {:<8} {:<16} {:<9} {:<4} {:<8} {}".format(
            self.pid, pid_type_name(self.pid), self.owner, self.name,
            self.state, self.priority, self.mode,
            self.meta.get("avg_us") or "-"
        )


class Scheduler:
    """Zeno OS process scheduler. One instance lives at zeno.sched."""

    # -------- power-scaling tuning --------
    # A task averaging at/above this many microseconds of CPU time per
    # scheduled run counts as a "large loop" and earns its own boost
    # request. Small/quick functions never reach this and never touch
    # frequency at all.
    TASK_BOOST_THRESHOLD_US = 8_000
    # Don't act on a task's load until it's run at least this many times
    # -- avoids one slow first call (cache miss, cold import, etc.)
    # deciding policy off a single sample.
    TASK_BOOST_MIN_SAMPLES = 3
    # Reason key used for the whole-system, frame-level auto_scale() call.
    FRAME_LOAD_REASON = "scheduler:frame"

    def __init__(self, logger=None, power=None):
        from Services import CPU
        self.cpu = CPU()
        self._current_user_fn = "root"

        self.table = {}          # pid -> Process
        self.running = False
        self._rand_seed = time.ticks_us() & 0xFFFFFFFF  # fallback PRNG state
        self._active_pid = None  # PID of the task currently executing

        # Execution tracing configuration
        self.trace_execution = True  # Set to True to display per-tick exec info

        # Names of pids we've already announced via "service started" logging,
        # so a periodic/looping task doesn't re-log every reschedule.
        self._announced = set()

        self._logger = logger  # optional Logger instance; falls back to print

        # PowerManagement wiring. `power` can be passed in explicitly
        # (e.g. a shared instance from Services); otherwise it's created
        # lazily on first use. `False` is used internally as a "already
        # tried and failed, don't retry every tick" sentinel.
        self.power = power
        self._boosted_tasks = set()   # reasons currently holding a per-task boost

        self._ensure_storage()
        self._load_state()

    # ---------------- logging ----------------
    def _get_logger(self):
        if self._logger is not None:
            return self._logger
        try:
            from Services import logger as _logger_accessor
            return _logger_accessor()
        except Exception:
            return None

    def _log_debug(self, msg, source="SCHED"):
        log = self._get_logger()
        if log is not None:
            try:
                log.debug(msg, source=source)
                return
            except Exception:
                pass
        print("[{}] {}".format(source, msg))

    def _log_error(self, msg, source="SCHED"):
        log = self._get_logger()
        if log is not None:
            try:
                log.error(msg, source=source)
                return
            except Exception:
                pass
        print("[{}][ERROR] {}".format(source, msg))

    def log_misuse(self, p, sig, reason):
        """Every rejected/blocked signal attempt on a process is recorded
        here -- centralized at the source (kill()) so nothing slips
        through, and daemons don't need to poll for this themselves."""
        self._log_error(
            "blocked signal {} on pid {} ({}, owner={}): {}".format(
                sig, p.pid, p.name, p.owner, reason
            ),
            source="SECSCAN",
        )

    # ---------------- power management ----------------
    def _get_power(self):
        """Lazily obtain (and cache) a PowerManagement instance. Returns
        None if it isn't available on this build, without raising or
        retrying every single call."""
        if self.power is False:
            return None
        if self.power is not None:
            return self.power
        try:
            self.power = PowerManagement(logger=self._get_logger())
        except Exception as e:
            self._log_error("PowerManagement unavailable: {}".format(e), source="POWER")
            self.power = False
            return None
        return self.power

    def set_power_policy(self, policy):
        """Passthrough: 'dynamic' (default, load-balanced) / 'performance'
        (pin max) / 'powersaving' (pin min)."""
        power = self._get_power()
        if power is None:
            self._log_error("cannot set power policy: PowerManagement unavailable", source="POWER")
            return False
        return power.policy_set(policy)

    def power_status(self):
        power = self._get_power()
        if power is None:
            print("[SCHED] PowerManagement unavailable.")
            return None
        return power.status()

    def _scale_for_frame_load(self):
        """Whole-system load signal, fed once per scheduler frame."""
        power = self._get_power()
        if power is None:
            return
        try:
            power.auto_scale(self.cpu.usage(), reason=self.FRAME_LOAD_REASON)
        except Exception as e:
            self._log_error("frame-level power scaling failed: {}".format(e), source="POWER")

    def _task_boost_reason(self, p):
        return "task:{}:{}".format(p.pid, p.name)

    def _scale_for_task(self, p):
        """Per-task load signal: only "large loop" tasks (sustained high
        avg_us across several runs) request a boost; quick tasks never
        touch frequency."""
        power = self._get_power()
        if power is None:
            return

        avg = p.meta.get("avg_us")
        samples = p.meta.get("samples", 0)
        if avg is None or samples < self.TASK_BOOST_MIN_SAMPLES:
            return  # not enough history to trust yet

        reason = self._task_boost_reason(p)
        is_large = avg >= self.TASK_BOOST_THRESHOLD_US
        already_boosted = reason in self._boosted_tasks

        if is_large and not already_boosted:
            if power.boost(reason, "high"):
                self._boosted_tasks.add(reason)
                self._log_debug(
                    "boosting CPU for large task '{}' (pid {}, avg {}us >= {}us)".format(
                        p.name, p.pid, avg, self.TASK_BOOST_THRESHOLD_US
                    ),
                    source="POWER",
                )
        elif not is_large and already_boosted:
            power.release(reason)
            self._boosted_tasks.discard(reason)
            self._log_debug(
                "releasing CPU boost for task '{}' (pid {}, avg {}us < {}us)".format(
                    p.name, p.pid, avg, self.TASK_BOOST_THRESHOLD_US
                ),
                source="POWER",
            )

    def _release_task_boost(self, p):
        """Always safe to call -- no-op if this task never held a boost.
        Called on every path a task can die on so a killed 'large loop'
        task can't leave its boost request stuck forever."""
        reason = self._task_boost_reason(p)
        if reason in self._boosted_tasks:
            self._boosted_tasks.discard(reason)
            power = self._get_power()
            if power is not None:
                power.release(reason)

    # ---------------- persistence ----------------
    def _ensure_storage(self):
        try:
            if "LOGS" not in os.listdir("/"):
                os.mkdir("/LOGS")
        except Exception:
            pass
        try:
            with open(PERSIST_PATH, "r"):
                pass
        except Exception:
            with open(PERSIST_PATH, "w") as f:
                f.write("{}")

    def _load_state(self):
        try:
            with open(PERSIST_PATH) as f:
                self._meta_store = json.loads(f.read())
        except Exception:
            self._meta_store = {}

    def _persist(self):
        out = {}
        for p in self.table.values():
            if p.meta["samples"] > 0:
                out[p.name] = p.meta
        try:
            with open(PERSIST_PATH, "w") as f:
                f.write(json.dumps(out))
        except Exception:
            pass

    # ---------------- ownership / permission ----------------
    def _caller_user(self):
        try:
            return self._current_user_fn()
        except Exception:
            return None

    def _can_signal(self, target, system_token=None):
        if system_token is not None:
            from Services import SystemPrivilege, _SYSTEM_TOKEN
            if system_token == _SYSTEM_TOKEN:
                return True
        caller = self._caller_user()
        if caller is None:
            return False
        if target.owner == caller:
            return True
        try:
            from Services import usermanager
            return usermanager().is_session_root()
        except Exception:
            return False

    # ---------------- pid allocation ----------------
    def _rand3(self):
        if _HAVE_URANDOM:
            return urandom.getrandbits(10) % 1000
        x = self._rand_seed
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17)
        x ^= (x << 5) & 0xFFFFFFFF
        self._rand_seed = x & 0xFFFFFFFF
        return x % 1000

    def _classify(self, mode, owner, ptype=None):
        if ptype is not None:
            if ptype not in PID_TYPE_NAMES:
                raise ValueError("invalid pid type: %r" % (ptype,))
            return ptype
        if mode == MODE_THREAD:
            return PID_TYPE_DAEMON if owner == "root" else PID_TYPE_THREAD
        if owner == "root":
            return PID_TYPE_KERNEL
        return PID_TYPE_USER

    def _alloc_pid(self, ptype):
        base = ptype * 1000
        for _ in range(2000):
            pid = base + self._rand3()
            if pid not in self.table:
                return pid
        raise ProcessError(
            "pid space exhausted for type {} ({})".format(ptype, PID_TYPE_NAMES.get(ptype))
        )

    # ---------------- syscalls ----------------
    def spawn(self, name, func, *, mode=MODE_LOOP, period=0, priority=0,
              owner=None, expected_us=None, ptype=None):
        if mode not in (MODE_LOOP, MODE_PERIODIC, MODE_ONCE, MODE_THREAD):
            raise ValueError("invalid mode: %r" % (mode,))
        if mode == MODE_THREAD and not _HAVE_THREAD:
            raise ProcessError("_thread not available on this build")

        owner = owner or self._caller_user() or "root"
        resolved_ptype = self._classify(mode, owner, ptype)

        # Daemon-series (4xxx) processes are critical background services
        # (guardian, etc). They must run as real threads so they can't be
        # starved or torn out by the cooperative runqueue -- enforce this
        # at spawn time rather than silently upgrading the mode, so a
        # misconfigured daemon fails loudly during development.
        if resolved_ptype == PID_TYPE_DAEMON and mode != MODE_THREAD:
            raise ValueError(
                "daemon-series (4xxx) processes must use mode=MODE_THREAD "
                "(got mode=%r for name=%r)" % (mode, name)
            )

        pid = self._alloc_pid(resolved_ptype)
        ppid = self._active_pid or 0

        p = Process(pid, ppid, owner, name, func, mode, period, priority)
        if name in self._meta_store:
            p.meta = self._meta_store[name]
        elif expected_us is not None:
            p.meta["avg_us"] = expected_us

        p.state = READY
        self.table[pid] = p

        # Announce once per pid -- "started guardian service" etc -- so the
        # log records what's running without spamming on every reschedule.
        if pid not in self._announced:
            self._announced.add(pid)
            self._log_debug(
                "started {} service -> pid={} type={} owner={} mode={} priority={}".format(
                    name, pid, pid_type_name(pid), owner, mode, priority
                ),
                source="SCHED",
            )

        if mode == MODE_THREAD:
            self._start_thread(p)

        return pid

    def kill(self, pid, sig=SIGTERM, system_token=None):
        p = self.table.get(pid)
        if p is None:
            raise ProcessError("no such pid: %d" % pid)

        # SIGKILL is never permitted on a daemon, regardless of caller --
        # daemons are thread-mode and must be stopped cooperatively via
        # SIGTERM + checkpoint(), never yanked with no cleanup mid-task.
        if pid_type(p.pid) == PID_TYPE_DAEMON and sig == SIGKILL:
            self.log_misuse(p, sig, reason="SIGKILL forbidden on daemon pid")
            raise PermissionDenied(
                "pid %d (%s) is a protected daemon; SIGKILL is not permitted, "
                "use SIGTERM for cooperative shutdown" % (pid, p.name)
            )

        if not self._can_signal(p, system_token=system_token):
            self.log_misuse(p, sig, reason="permission denied")
            raise PermissionDenied(
                "user cannot signal pid %d (owned by %s)" % (pid, p.owner)
            )
        if p.state in (ZOMBIE, DEAD):
            return True

        if sig == SIGKILL and p.mode != MODE_THREAD:
            p.state = ZOMBIE
            p.exit_code = -SIGKILL
            self._release_task_boost(p)
            self._persist()
            return True

        p.pending_signal = sig
        return True

    def should_die(self, pid):
        p = self.table.get(pid)
        return bool(p and p.pending_signal in (SIGTERM, SIGKILL))

    def checkpoint(self, pid):
        p = self.table.get(pid)
        if p and p.pending_signal in (SIGTERM, SIGKILL):
            p.state = ZOMBIE
            p.exit_code = -p.pending_signal
            self._release_task_boost(p)
            raise SystemExit

    def wait(self, pid, timeout_ms=None):
        start = time.ticks_ms()
        while True:
            p = self.table.get(pid)
            if p is None:
                return None
            if p.state in (ZOMBIE, DEAD):
                code = p.exit_code
                p.state = DEAD
                del self.table[pid]
                return code
            if timeout_ms is not None and time.ticks_diff(time.ticks_ms(), start) >= timeout_ms:
                return None
            time.sleep_ms(5)

    def nice(self, pid, priority, system_token=None):
        p = self.table.get(pid)
        if p is None:
            raise ProcessError("no such pid: %d" % pid)
        if not self._can_signal(p, system_token=system_token):
            raise PermissionDenied("user cannot renice pid %d" % pid)
        p.priority = max(NICE_MIN, min(NICE_MAX, priority))
        return True

    def getpid(self):
        return self._active_pid

    def list(self):
        print("{:<5} {:<8} {:<8} {:<16} {:<9} {:<4} {:<8} {}".format(
            "PID", "TYPE", "OWNER", "NAME", "STATE", "NI", "MODE", "AVG_US"
        ))
        for p in sorted(self.table.values(), key=lambda p: p.pid):
            print(p.as_row())
        return list(self.table.keys())

    # ---------------- thread-mode tasks ----------------
    def _start_thread(self, p):
        def runner():
            self._active_pid = p.pid
            p.state = RUNNING

            if self.trace_execution:
                print("[EXEC TRACE] Executing Thread Task -> PID: {}, Name: {}, User: {}".format(
                    p.pid, p.name, p.owner
                ))

            start = time.ticks_us()
            try:
                p.func(p.pid)
                p.exit_code = 0
            except SystemExit:
                pass
            except Exception as e:
                self._log_error("pid {} ({}) raised: {}".format(p.pid, p.name, e), source="SCHED")
                p.exit_code = -1

            elapsed = time.ticks_diff(time.ticks_us(), start)
            self._record_stat(p, elapsed)
            p.state = ZOMBIE
            self._release_task_boost(p)

            if self.trace_execution:
                print("[EXEC TRACE] Finished Thread Task -> PID: {}, Name: {}, Elapsed: {}us".format(
                    p.pid, p.name, elapsed
                ))

        p._thread_started = True
        _thread.start_new_thread(runner, ())

    # ---------------- cooperative runqueue (CFS-style) ----------------
    def _record_stat(self, p, elapsed_us):
        m = p.meta
        m["last_us"] = elapsed_us
        m["samples"] += 1
        m["min_us"] = elapsed_us if m["min_us"] is None else min(m["min_us"], elapsed_us)
        m["max_us"] = elapsed_us if m["max_us"] is None else max(m["max_us"], elapsed_us)
        m["avg_us"] = elapsed_us if m["avg_us"] is None else int(0.3 * elapsed_us + 0.7 * m["avg_us"])

    def _runnable_cooperative(self):
        now = time.ticks_ms()
        out = []
        for p in self.table.values():
            if p.mode == MODE_THREAD:
                continue
            if p.state in (ZOMBIE, DEAD, BLOCKED):
                continue
            if p.pending_signal == SIGSTOP:
                continue
            if p.mode == MODE_PERIODIC:
                if time.ticks_diff(now, p._last_run) < p.period:
                    continue
            out.append(p)
        return out

    def _pick_next(self, runnable):
        return min(runnable, key=lambda p: (p.vruntime, p.pid))

    def _run_one(self, p):
        if p.pending_signal in (SIGTERM, SIGKILL):
            p.state = ZOMBIE
            p.exit_code = -p.pending_signal
            self._release_task_boost(p)
            return

        self._active_pid = p.pid
        p.state = RUNNING

        if self.trace_execution:
            print("[EXEC TRACE] Executing Task -> PID: {}, Name: {}, User: {}".format(
                p.pid, p.name, p.owner
            ))

        start = time.ticks_us()
        try:
            p.func()
        except Exception as e:
            self._log_error("pid {} ({}) raised: {}".format(p.pid, p.name, e), source="SCHED")

        elapsed = max(1, time.ticks_diff(time.ticks_us(), start))
        self._active_pid = None

        self._record_stat(p, elapsed)
        p.vruntime += elapsed * 1024 // p.weight()
        p._last_run = time.ticks_ms()

        # Per-task power signal: only sustained "large loop" tasks get a
        # boost request out of this -- see _scale_for_task().
        self._scale_for_task(p)

        if self.trace_execution:
            print("[EXEC TRACE] Finished Task -> PID: {}, Name: {}, Elapsed: {}us".format(
                p.pid, p.name, elapsed
            ))

        if p.mode == MODE_ONCE:
            p.state = ZOMBIE
            p.exit_code = 0
            self._release_task_boost(p)
        else:
            p.state = READY

    def tick(self):
        runnable = self._runnable_cooperative()
        if not runnable:
            return False
        self._run_one(self._pick_next(runnable))
        self._reap_zombies()
        return True

    def _reap_zombies(self):
        pass

    def reap(self, pid):
        p = self.table.get(pid)
        if p and p.state == ZOMBIE:
            self._release_task_boost(p)
            del self.table[pid]
            self._announced.discard(pid)
            return True
        return False

    def start(self, frame_ms=16):
        self.running = True
        self._log_debug("scheduler main loop starting (frame_ms={})".format(frame_ms), source="SCHED")
        while self.running:
            frame_start = time.ticks_us()
            budget_us = frame_ms * 1000
            while time.ticks_diff(time.ticks_us(), frame_start) < budget_us:
                if not self.tick():
                    break
            busy = time.ticks_diff(time.ticks_us(), frame_start)
            idle = max(0, budget_us - busy)
            if idle > 0:
                time.sleep_us(idle)
            if hasattr(self.cpu, "report_frame"):
                self.cpu.report_frame(busy, idle)
            # Frame-level power signal: whole-system busy/idle ratio for
            # this frame drives the "balanced" dynamic-policy behavior.
            self._scale_for_frame_load()
            self._persist()

    def stop(self):
        self.running = False
        self._log_debug("scheduler main loop stopped", source="SCHED")

    def killall(self, system_token=None):
        """Best-effort: signal every non-daemon task to stop, skip daemons
        (they're protected and about to die via machine.reset() anyway).
        Never raises -- this runs on the boot-failure/reboot path and must
        not block a reset."""
        for pid, p in list(self.table.items()):
            if pid_type(p.pid) == PID_TYPE_DAEMON:
                continue
            try:
                self.kill(pid, SIGTERM, system_token=system_token)
            except Exception:
                pass
# =============================================================================
# system -- low-level system control, RAM/security housekeeping, guardian
# =============================================================================

class system:
    def __init__(self, opt_level=0, debug=False):
        self.opt_level_value = opt_level
        self.debug = debug
        self.log = Logger()
        self.path = "/SYSTEM32"
        self.cfg = BootConfig()
        self.sched = None

    def restart(self):
        try:
            self.log.debug("System restart requested", source="SYSTEM")
            machine.reset()
        except Exception as e:
            self.log.error("System restart failed: {}".format(e), source="SYSTEM")

    def optlevel(self, level):
        try:
            micropython.opt_level(int(level))
            self.opt_level_value = int(level)
            self.log.debug("System optimization level set to {}".format(level), source="SYSTEM")
        except Exception as e:
            self.log.error("Error configuring optimization level '{}': {}".format(level, e), source="SYSTEM")

    def info(self):
        try:
            print("Zeno Micro PC Version: V4.X alpha")
            print("CPU: ESP32-S3")
            print("CPU Frequency:", machine.freq() / 1_000_000, "MHz")
            print("CPU Cores:", 2)
            print("Installed RAM:", (gc.mem_free() + gc.mem_alloc()) / (1024 * 1024), "MB")
            print("Unique ID:", int.from_bytes(machine.unique_id(), 'big'))
            print("Installed internal ROM: 16 MB")
            print("Disk Info:")
            self.log.debug("System info displayed", source="SYSTEM")
        except Exception as e:
            self.log.error("Failed to gather base system info: {}".format(e), source="SYSTEM")
            return

        path = "/SYSTEM32"
        try:
            stats = os.statvfs(path)
            total_bytes = stats[2] * stats[0]
            free_bytes = stats[3] * stats[0]

            def convert(v):
                return "{:.2f} GB".format(v / 1024**3) if v >= 1024**3 else "{:.2f} MB".format(v / 1024**2)

            print("Path       :", path)
            print("Volume Name:", path.split('/')[-1])
            print("Total Size :", convert(total_bytes))
            print("Free Space :", convert(free_bytes))
        except Exception as e:
            self.log.error("Error accessing filesystem stats for '{}': {}".format(path, e), source="SYSTEM")

    def memconfig(self, percent=25):
        try:
            gc.collect()
            free, alloc = gc.mem_free(), gc.mem_alloc()
            threshold = alloc + free * percent // 100
            gc.threshold(threshold)
            micropython.alloc_emergency_exception_buf(100)
            self.log.debug(
                "Memory configuration updated: free={} alloc={} threshold_percent={} threshold={}"
                .format(free, alloc, percent, threshold), source="SYSTEM"
            )
        except Exception as e:
            self.log.error("Failed to configure memory/GC with percent {}: {}".format(percent, e), source="SYSTEM")

    def force_mem(self):
        try:
            before = gc.mem_free()
            gc.collect()
            after = gc.mem_free()
            self.log.debug("Forced GC executed: free memory {} -> {} bytes (delta {})".format(before, after, after - before), source="SYSTEM")
        except Exception as e:
            self.log.error("Forced garbage collection failed: {}".format(e), source="SYSTEM")

    def mem_usage(self):
        try:
            free, alloc = gc.mem_free(), gc.mem_alloc()
            total = free + alloc
            if total <= 0:
                self.log.error("Cannot compute memory usage: total memory reported as 0.", source="SYSTEM")
                return

            def fmt(v):
                if v >= 1024 * 1024:
                    return "{:.2f} MB".format(v / (1024 * 1024))
                if v >= 1024:
                    return "{:.2f} KB".format(v / 1024)
                return "{} B".format(v)

            print("[System] Memory usage:")
            print("  Total: {}".format(fmt(total)))
            print("  Used:  {} ({:.2f}%)".format(fmt(alloc), (alloc / total) * 100))
            print("  Free:  {} ({:.2f}%)".format(fmt(free), (free / total) * 100))
            self.log.debug("System memory information displayed", source="SYSTEM")
        except Exception as e:
            self.log.error("Failed to read memory usage: {}".format(e), source="SYSTEM")

    def perf_test(self):
        self.log.debug("Performing system hardware test", source="SYSTEM")
        try:
            pystone_lowmem.main(1000)
            self.log.debug("CPU benchmark (pystone_lowmem) completed.", source="SYSTEM")
        except Exception as e:
            self.log.error("CPU benchmark (pystone_lowmem) failed: {}".format(e), source="SYSTEM")

        try:
            start_ram = gc.mem_free() / (1024 * 1024)
            l = [0] * 100000
            mid_ram = gc.mem_free() / (1024 * 1024)
            del l
            gc.collect()
            end_ram = gc.mem_free() / (1024 * 1024)
            self.log.debug("RAM test: start={:.3f} MB, during alloc={:.3f} MB, after free={:.3f} MB".format(start_ram, mid_ram, end_ram), source="SYSTEM")
        except Exception as e:
            self.log.error("RAM performance test failed: {}".format(e), source="SYSTEM")

        try:
            start_flash = time.ticks_ms()
            tmp_path = "/tmp_test.bin"
            with open(tmp_path, "wb") as f:
                f.write(bytearray(1024 * 50))
            with open(tmp_path, "rb") as f:
                _ = f.read()
            try:
                os.remove(tmp_path)
            except Exception as e_rm:
                self.log.error("Flash test cleanup failed (could not remove '{}'): {}".format(tmp_path, e_rm), source="SYSTEM")
            self.log.debug("Flash test complete: 50KB write/read in {} ms".format(time.ticks_diff(time.ticks_ms(), start_flash)), source="SYSTEM")
        except Exception as e:
            self.log.error("Flash performance test failed: {}".format(e), source="SYSTEM")

    def mode(self, m):
        s = str(m).strip().upper()
        mode_map = {
            "PERF": ("PERFORMANCE", 0), "PERFORMANCE": ("PERFORMANCE", 0),
            "BAL": ("BALANCED", 3), "BALANCED": ("BALANCED", 3),
            "SAVE": ("POWERSAVING", 3), "POWERSAVE": ("POWERSAVING", 3), "POWERSAVING": ("POWERSAVING", 3),
        }
        selected = next((tup for key, tup in mode_map.items() if key in s), None)
        if not selected:
            self.log.error("Unknown mode requested: {}".format(m), "SYSTEM")
            return

        mode_name, optlevel_val = selected
        self.cfg.set("MODE", mode_name)
        self.optlevel(optlevel_val)
        self.log.debug("Mode set -> {} (optlevel {}) - rebooting".format(mode_name, optlevel_val), "SYSTEM")
        machine.reset()

    def ram_guard(self, warn_pct=80, crit_pct=92):
        """Threshold-gated: does nothing below warn_pct, only logs when
        gc actually reclaimed something meaningful or the critical
        threshold is hit -- avoids spamming /LOGS every interval."""
        free, alloc = gc.mem_free(), gc.mem_alloc()
        total = free + alloc
        if total <= 0:
            return 0.0
        used_pct = (alloc / total) * 100
        if used_pct < warn_pct:
            return used_pct

        before_free = free
        gc.collect()
        after_free = gc.mem_free()
        reclaimed = after_free - before_free
        new_used_pct = (gc.mem_alloc() / (after_free + gc.mem_alloc())) * 100

        if used_pct >= crit_pct:
            self.log.error("RAM critical: {:.1f}% used, collected {} bytes -> {:.1f}% used".format(used_pct, reclaimed, new_used_pct), source="RAMGUARD")
        elif reclaimed > 4096:
            self.log.debug("RAM warning: {:.1f}% used, collected {} bytes -> {:.1f}% used".format(used_pct, reclaimed, new_used_pct), source="RAMGUARD")
        return new_used_pct

    def security_scan(self):
        """Boring integrity checks only -- kernel auth flags, /.sys
        presence, runaway process count. Only ever logs on an actual finding."""
        findings = []
        try:
            if not getattr(zeno, "authorized", False):
                findings.append("zeno.authorized is False after boot")
            if getattr(zeno, "boot_cap", "unset") not in (None, "unset"):
                findings.append("zeno.boot_cap was not consumed at boot")
        except Exception as e:
            findings.append("kernel auth flag check failed: {}".format(e))

        try:
            os.stat("/.sys")
        except OSError:
            findings.append("/.sys metadata directory missing")

        if self.sched is not None and len(self.sched.table) > 64:
            findings.append("process count abnormally high: {}".format(len(self.sched.table)))

        for f in findings:
            self.log.error(f, source="SECSCAN")
        return findings

    def checkup(self, mem_warn_pct=80, mem_crit_pct=92):
        return {
            "mem_used_pct": self.ram_guard(mem_warn_pct, mem_crit_pct),
            "security_findings": self.security_scan(),
        }

    def start_guardian(self, sched, interval_ms=10_000):
        """Spawns the root-owned guardian thread process via the
        scheduler. Call once, right after zeno.sched exists at boot."""
        self.sched = sched

        def guardian(pid):
            self.log.debug("guardian started as pid {}".format(pid), source="GUARDIAN")
            while True:
                self.sched.checkpoint(pid)
                try:
                    self.checkup()
                except Exception as e:
                    self.log.error("checkup failed: {}".format(e), source="GUARDIAN")
                time.sleep_ms(interval_ms)

        pid = self.sched.spawn("guardian", guardian, mode="thread", owner="root", priority=10)
        self.log.debug("guardian spawned, pid={}".format(pid), source="SYSTEM")
        return pid

    def firmware_update(self):
        self._safe_update("firmware.py", "/LOGS/firmwarecopy.py", "firmwarestable.py")

    def boot_update(self):
        self._safe_update("boot.py", "/LOGS/bootcopy.py")

    def _safe_update(self, src_file, log_dest, stable_file=None):
        try:
            with open(src_file, "rb") as fsrc:
                data = fsrc.read()
        except Exception as e:
            self.log.error("Safe update failed while reading '{}': {}".format(src_file, e), source="SYSTEM")
            return

        try:
            try:
                os.mkdir("/LOGS")
            except OSError as e:
                if len(e.args) > 0 and e.args[0] != 17:
                    raise
        except Exception as e:
            self.log.error("Safe update failed while ensuring '/LOGS' directory: {}".format(e), source="SYSTEM")
            return

        try:
            with open(log_dest, "wb") as fdest:
                fdest.write(data)
        except Exception as e:
            self.log.error("Safe update failed while writing backup '{}' -> '{}': {}".format(src_file, log_dest, e), source="SYSTEM")
            return

        if stable_file:
            try:
                with open(stable_file, "wb") as fstable:
                    fstable.write(data)
            except Exception as e:
                self.log.error("Safe update failed while writing stable copy '{}' -> '{}': {}".format(src_file, stable_file, e), source="SYSTEM")
                return

        self.log.debug("{} backup complete (log='{}', stable='{}'). Restarting...".format(src_file, log_dest, stable_file), source="SYSTEM")
        try:
            for i in range(5, 0, -1):
                print(i)
                time.sleep(1)
            self.log.debug("System expecting restart after backup of '{}'.".format(src_file), source="SYSTEM")
            machine.reset()
        except Exception as e:
            self.log.error("Backup completed for '{}', but restart failed: {}".format(src_file, e), source="SYSTEM")


# =============================================================================
# Network
# =============================================================================

class Network:
    def __init__(self, ssid=None, password=None, timeout=15):
        self.ssid = ssid if ssid is not None else zeno.ssid
        self.password = str(password if password is not None else zeno.wifi_password)
        self.timeout = timeout
        self.wlan = network.WLAN(network.STA_IF)

    def connect(self):
        wlan = self.wlan
        wlan.active(False)
        time.sleep_ms(200)
        wlan.active(True)
        time.sleep_ms(200)
        try:
            wlan.config(pm=wlan.PM_NONE)
        except Exception as e:
            print("pm config failed (non-fatal):", e)

        print("MAC:", wlan.config('mac'))
        print("connecting to", self.ssid)
        wlan.connect(self.ssid, self.password)

        start = time.time()
        while not wlan.isconnected():
            status = wlan.status()
            print("status:", status, "elapsed:", time.time() - start)
            if status in (network.STAT_WRONG_PASSWORD, network.STAT_NO_AP_FOUND, network.STAT_CONNECT_FAIL):
                print("FATAL status, giving up")
                return False
            if time.time() - start > self.timeout:
                print("timeout, giving up")
                return False
            time.sleep_ms(500)

        print("CONNECTED:", wlan.ifconfig())
        return True

    def scan(self):
        self.wlan.active(True)
        results = self.wlan.scan()
        for n in results:
            print(n)
        return results

    def disconnect(self):
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        self.wlan.active(False)

    def isconnected(self):
        return self.wlan.isconnected()

    def ifconfig(self):
        return self.wlan.ifconfig()


# =============================================================================
# downloadhelper -- raw-socket HTTP(S) file download
# =============================================================================

class downloadhelper:
    def __init__(self):
        self.log = Logger()

    def download_file(self, url, save_dir="/", save_file=None):
        if not url:
            self.log.error("Download failed: empty URL provided", source="DOWNLOADSERV")
            return None

        url = url.strip()
        try:
            if url.startswith("http:/") and not url.startswith("http://"):
                url = url.replace("http:/", "http://", 1)
            elif url.startswith("https:/") and not url.startswith("https://"):
                url = url.replace("https:/", "https://", 1)
            elif not (url.startswith("http://") or url.startswith("https://")):
                url = "http://" + url
        except Exception as e:
            self.log.error("Failed to normalize URL '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None

        try:
            proto, _, hostport, *rest = url.split("/", 3)
            host = hostport.split(":")[0]
            port = 443 if proto == "https:" else 80
            path = "/" + (rest[0] if rest else "")
            if path == "/":
                path = "/index.html"
        except Exception as e:
            self.log.error("Invalid URL format '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None

        if not save_file:
            save_file = path.split("/")[-1] or "index.html"

        try:
            addr_info = usocket.getaddrinfo(host, port)
            if not addr_info:
                self.log.error("DNS resolution returned no results for host '{}'".format(host), source="DOWNLOADSERV")
                return None
            addr = addr_info[0][-1]
        except Exception as e:
            self.log.error("DNS resolution failed for host '{}': {}".format(host, e), source="DOWNLOADSERV")
            return None

        try:
            s = usocket.socket()
        except Exception as e:
            self.log.error("Failed to create socket: {}".format(e), source="DOWNLOADSERV")
            return None

        try:
            try:
                s.connect(addr)
            except Exception as e:
                self.log.error("Failed to connect to {}:{} -> {}".format(host, port, e), source="DOWNLOADSERV")
                return None

            if proto == "https:":
                try:
                    s = ssl.wrap_socket(s)
                except Exception as e:
                    self.log.error("SSL wrap failed for '{}': {}".format(url, e), source="DOWNLOADSERV")
                    return None

            try:
                s.send("GET {} HTTP/1.0\r\nHost: {}\r\n\r\n".format(path, host).encode())
            except Exception as e:
                self.log.error("Failed to send HTTP request to '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            try:
                status_line = s.readline()
            except Exception as e:
                self.log.error("Failed to read HTTP status from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            if not status_line:
                self.log.warning("No response from server '{}'".format(url), source="DOWNLOADSERV")
                return None

            try:
                parts = status_line.decode().split()
            except Exception as e:
                self.log.error("Failed to decode HTTP status line from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            if len(parts) < 2 or parts[1] != "200":
                self.log.error("HTTP error from '{}': {}".format(url, status_line.decode().strip()), source="DOWNLOADSERV")
                return None

            try:
                while True:
                    line = s.readline()
                    if not line or line == b"\r\n":
                        break
            except Exception as e:
                self.log.error("Failed while reading HTTP headers from '{}': {}".format(url, e), source="DOWNLOADSERV")
                return None

            try:
                if save_dir not in os.listdir("/"):
                    try:
                        os.mkdir(save_dir)
                    except Exception as e_mk:
                        self.log.error("Failed to create directory '{}': {}".format(save_dir, e_mk), source="DOWNLOADSERV")
                        return None
            except Exception as e:
                self.log.error("Failed to list root directories while checking '{}': {}".format(save_dir, e), source="DOWNLOADSERV")
                return None

            full_path = save_dir + "/" + save_file
            try:
                with open(full_path, "wb") as f:
                    while True:
                        try:
                            data = s.recv(512)
                        except Exception as e:
                            self.log.error("Socket recv failed from '{}': {}".format(url, e), source="DOWNLOADSERV")
                            return None
                        if not data:
                            break
                        try:
                            f.write(data)
                        except Exception as e:
                            self.log.error("Failed writing to '{}': {}".format(full_path, e), source="DOWNLOADSERV")
                            return None
            except Exception as e:
                self.log.error("Failed to open/write file '{}': {}".format(full_path, e), source="DOWNLOADSERV")
                return None

            self.log.debug("File saved successfully -> {}".format(full_path), source="DOWNLOADSERV")
            return full_path

        except Exception as e:
            self.log.error("Error during download from '{}': {}".format(url, e), source="DOWNLOADSERV")
            return None
        finally:
            try:
                s.close()
            except Exception:
                pass


class Git:
    def __init__(self, base_raw=None, default_branch="main"):
        self.logger = Logger()
        self.base_raw = base_raw or "https://raw.githubusercontent.com"
        self.default_branch = default_branch
        self.default_download_dir = "/"
        self.source = "GITSERV"
        self.token = zeno.gitsecret
        self._http = None

    def download_url(self, url, save_dir=None):
        try:
            user, repo, branch, filename = self._parse_github_url(url)
        except ValueError as e:
            self.logger.error(str(e), source=self.source)
            print("Download failed: invalid GitHub URL.")
            return False
        return self.download(user, repo, filename, branch=branch, save_dir=save_dir)

    def download(self, user, repo, filename, branch=None, save_dir=None):
        filename = filename or ""
        if filename.startswith("http://") or filename.startswith("https://"):
            try:
                parsed_user, parsed_repo, parsed_branch, filename = self._parse_github_url(filename)
            except ValueError as e:
                self.logger.error(str(e), source=self.source)
                print("Download failed: invalid GitHub URL.")
                return False
            user = user or parsed_user
            repo = repo or parsed_repo
            branch = branch or parsed_branch

        if not user or not repo:
            self.logger.error("Download failed: 'user' and 'repo' are required.", source=self.source)
            print("Download failed: missing repository information.")
            return False

        branch = branch or self.default_branch
        filename = filename.lstrip("/")
        if not filename:
            self.logger.error("Download failed: empty file path.", source=self.source)
            print("Download failed: no file specified.")
            return False

        original_filename = filename
        url = self._build_url(user, repo, self._encode_path(filename), branch)

        target_dir = self._resolve_download_dir(save_dir if save_dir is not None else self.default_download_dir)
        fname = original_filename.split("/")[-1]
        full_path = "{}/{}".format(target_dir.rstrip("/") or "", fname)
        if not full_path.startswith("/"):
            full_path = "/" + full_path

        print("Cloning '{}' from {}/{}...".format(fname, user, repo))
        self.logger.debug("Downloading {} -> {}".format(url, full_path), source=self.source)

        http = self._get_http()
        if http is None:
            print("Download failed: no HTTP client available.")
            return False

        # raw.githubusercontent.com returns 404 (not 401) for a bad auth
        # header, indistinguishable from "file not found" -- so try
        # unauthenticated first, retry with token only on 404.
        headers = {"User-Agent": "ZenoMicroPC"}
        try:
            resp = http.get(url, headers=headers)
        except Exception as e:
            self.logger.error("HTTP request failed for URL {}: {!r}".format(url, e), source=self.source)
            print("Download failed: connection error.")
            return False

        try:
            status = resp.status_code
        except Exception:
            status = 0

        if status == 404 and self.token:
            try:
                resp.close()
            except Exception:
                pass
            auth_headers = dict(headers)
            auth_headers["Authorization"] = "token {}".format(self.token)
            self.logger.debug("Unauthenticated request 404'd, retrying with token", source=self.source)
            try:
                resp = http.get(url, headers=auth_headers)
                status = resp.status_code
            except Exception as e:
                self.logger.error("HTTP retry-with-auth failed for URL {}: {!r}".format(url, e), source=self.source)
                print("Download failed: connection error.")
                return False

        if status != 200:
            self.logger.error("HTTP {} while downloading {} from {}".format(status, filename, url), source=self.source)
            if status == 404:
                print("Download failed: file not found.")
            else:
                print("Download failed: server returned an error.")
            try:
                resp.close()
            except Exception:
                pass
            return False

        try:
            data = resp.content
        except Exception:
            try:
                data = resp.text
                if isinstance(data, str):
                    data = data.encode()
            except Exception as e:
                self.logger.error("Failed to read HTTP response body: {}".format(e), source=self.source)
                print("Download failed: could not read response.")
                try:
                    resp.close()
                except Exception:
                    pass
                return False

        try:
            with open(full_path, "wb") as f:
                f.write(data)
        except Exception as e:
            self.logger.error("Failed to write file '{}': {}".format(full_path, e), source=self.source)
            print("Download failed: could not save file.")
            try:
                resp.close()
            except Exception:
                pass
            return False

        try:
            resp.close()
        except Exception:
            pass

        self.logger.debug("Download completed. Saved as '{}'".format(full_path), source=self.source)
        print("Downloaded '{}' successfully.".format(fname))
        return True

    def _get_http(self):
        if self._http is not None:
            return self._http
        try:
            import requests as _req
            self._http = _req
        except ImportError:
            try:
                import urequests as _req
                self._http = _req
            except ImportError as e:
                self.logger.error("No HTTP client available (requests/urequests): {}".format(e), source=self.source)
                return None
        return self._http

    def _parse_github_url(self, url):
        url = (url or "").strip()

        if url.startswith("https://raw.githubusercontent.com/"):
            parts = url[len("https://raw.githubusercontent.com/"):].split("/", 3)
            if len(parts) < 4 or not parts[3]:
                raise ValueError("Malformed raw.githubusercontent.com URL: {}".format(url))
            return parts[0], parts[1], parts[2], parts[3]

        if url.startswith("https://github.com/"):
            rest = url[len("https://github.com/"):]
            for marker in ("/blob/", "/raw/"):
                if marker in rest:
                    repo_part, _, tail = rest.partition(marker)
                    if "/" not in repo_part:
                        raise ValueError("Malformed github.com URL (missing repo): {}".format(url))
                    user, repo = repo_part.split("/", 1)
                    branch, _, path = tail.partition("/")
                    if not branch or not path:
                        raise ValueError("Malformed github.com URL (missing branch/path): {}".format(url))
                    return user, repo, branch, path
            raise ValueError("Unrecognized github.com URL (expected '/blob/' or '/raw/' in path): {}".format(url))

        raise ValueError("Unrecognized GitHub URL format: {}".format(url))

    def _encode_path(self, path):
        safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~/"
        return "".join(ch if ch in safe else "%{:02X}".format(ord(ch)) for ch in path)

    def _build_url(self, user, repo, encoded_path, branch):
        if encoded_path.startswith("/"):
            encoded_path = encoded_path[1:]
        return "/".join([self.base_raw.rstrip("/"), user, repo, branch, encoded_path])

    def _resolve_download_dir(self, preferred_dir):
        preferred_dir = preferred_dir or "/"
        if preferred_dir in ("", "/"):
            return "/"

        curr = ""
        try:
            for p in [p for p in preferred_dir.strip("/").split("/") if p]:
                curr = curr + "/" + p
                try:
                    os.stat(curr)
                except OSError:
                    try:
                        os.mkdir(curr)
                    except Exception as e:
                        self.logger.warning("Failed to create '{}' ({}), falling back to '/'".format(curr, e), source=self.source)
                        return "/"
            return curr or "/"
        except Exception as e:
            self.logger.warning("Error resolving download dir '{}': {}. Falling back to '/'".format(preferred_dir, e), source=self.source)
            return "/"

    def upload(self, user, repo, local_path, repo_path=None, branch=None, message="Upload from ESP32"):
        import ubinascii
        token = self.token
        if not token:
            self.logger.error("Upload failed: no GitHub token configured (zeno.gitsecret).", source=self.source)
            print("Upload failed: no GitHub token configured.")
            return False

        branch = branch or self.default_branch
        repo_path = (repo_path or local_path.split("/")[-1]).lstrip("/")

        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except Exception as e:
            self.logger.error("Git upload: cannot read local file '{}': {}".format(local_path, e), source=self.source)
            print("Upload failed: could not read local file.")
            return False

        try:
            b64 = ubinascii.b2a_base64(data).decode().strip()
        except Exception as e:
            self.logger.error("Git upload: base64 encode failed: {}".format(e), source=self.source)
            print("Upload failed: could not encode file.")
            return False

        print("Uploading '{}' to {}/{}...".format(repo_path, user, repo))

        api_url = "https://api.github.com/repos/{}/{}/contents/{}".format(user, repo, self._encode_path(repo_path))
        http = self._get_http()
        if http is None:
            print("Upload failed: no HTTP client available.")
            return False

        headers = {"Authorization": "token {}".format(token), "User-Agent": "ZenoMicroPC", "Accept": "application/vnd.github+json"}

        sha = None
        try:
            r = http.get(api_url + "?ref={}".format(branch), headers=headers)
            try:
                if r.status_code == 200:
                    try:
                        sha = r.json().get("sha")
                    except Exception:
                        sha = None
            finally:
                r.close()
        except Exception as e:
            self.logger.debug("Git upload: GET existing file failed/404 (new file maybe): {}".format(e), source=self.source)

        body = {"message": message, "content": b64, "branch": branch}
        if sha:
            body["sha"] = sha

        try:
            r = http.put(api_url, headers=headers, json=body)
        except Exception as e:
            self.logger.error("Git upload: PUT request failed: {}".format(e), source=self.source)
            print("Upload failed: connection error.")
            return False

        try:
            status = r.status_code
        except Exception:
            status = 0

        if status not in (200, 201):
            try:
                err_txt = r.text
            except Exception:
                err_txt = "no body"
            self.logger.error("Git upload: HTTP {} from GitHub: {}".format(status, err_txt), source=self.source)
            print("Upload failed: server returned an error.")
            try:
                r.close()
            except Exception:
                pass
            return False

        try:
            r.close()
        except Exception:
            pass

        self.logger.debug("Git upload completed: {} -> {}/{} ({})".format(local_path, user, repo, repo_path), source=self.source)
        print("Uploaded '{}' successfully.".format(repo_path))
        return True
# =============================================================================
# BluetoothManager
# =============================================================================

class BluetoothManager:
    def __init__(self, device_name="Zeno Micro PC"):
        self.device_name = device_name
        self.ble = bt.BLE()
        self.ble.active(False)
        self.connected = False
        self.rx_buffer = bytearray()
        self.conn_handle = None

    def _irq(self, event, data):
        if event == 1:
            self.conn_handle, _, _ = data
            self.connected = True
        elif event == 2:
            self.connected = False
            self.conn_handle = None
            self._advertise()
        elif event == 3:
            _, value_handle = data
            self.rx_buffer.extend(self.ble.gatts_read(value_handle))

    def on(self):
        if not self.ble.active():
            self.ble.active(True)
            self.ble.irq(self._irq)
            self._advertise()
        else:
            print("[BT] Already ON.")

    def off(self):
        try:
            self.ble.active(False)
            self.connected = False
        except Exception as e:
            print("[BT] Error turning off Bluetooth:", e)

    def _advertise(self, interval_us=500000):
        name = bytes(self.device_name, "utf-8")
        adv_data = bytearray(b"\x02\x01\x06") + bytes((len(name) + 1, 0x09)) + name
        try:
            self.ble.gap_advertise(interval_us, adv_data)
        except Exception as e:
            print("[BT] Advertisement error:", e)

    def search(self, duration=5):
        found = []
        scan_done = False

        def _scan_irq(event, data):
            nonlocal found, scan_done
            if event == bt._IRQ_SCAN_RESULT:
                addr_type, addr, adv_type, rssi, adv_data = data
                found.append((addr_type, bytes(addr), adv_type, rssi, bytes(adv_data)))
            elif event == bt._IRQ_SCAN_DONE:
                scan_done = True

        self.ble.irq(_scan_irq)
        self.ble.active(True)
        self.ble.gap_scan(duration * 1000, 30000, 30000)

        t0 = time.ticks_ms()
        while not scan_done and time.ticks_diff(time.ticks_ms(), t0) < (duration + 2) * 1000:
            time.sleep_ms(100)

        self.ble.gap_scan(None)
        return found

    def connect(self, addr_type, addr):
        try:
            self.ble.gap_connect(addr_type, addr)
        except Exception as e:
            print("[BT] Connection error:", e)

    def disconnect(self):
        try:
            if self.conn_handle is not None:
                self.ble.gap_disconnect(self.conn_handle)
                self.connected = False
                self.conn_handle = None
        except Exception as e:
            print("[BT] Disconnect error:", e)

    def send_data(self, data):
        try:
            if not self.connected or self.conn_handle is None:
                return
            if isinstance(data, str):
                data = data.encode()
            self.ble.gatts_notify(self.conn_handle, 0, data)
        except Exception as e:
            print("[BT] Send error:", e)

    def get_data(self):
        if self.rx_buffer:
            data = bytes(self.rx_buffer)
            self.rx_buffer = bytearray()
            return data
        return None


# =============================================================================
# AppInstaller
# =============================================================================

class AppInstaller:
    def __init__(self):
        self.user = "FerrariForever95"
        self.repo = "Zeno-Micro-PC"
        self.branch = "main"
        self.remote_base = "APPS"
        self.git = Git(default_branch=self.branch)
        self.apps_dir = "/SYSTEM32/APPS"

    def prompt_and_install(self):
        try:
            app_name = input("Enter app name to install (without .py): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("Install cancelled.")
            return False
        if not app_name:
            print("No app name given.")
            return False
        return self.install(app_name)

    def _build_remote_path(self, app_name: str) -> str:
        base = (self.remote_base or "").strip().strip("/")
        return f"{base}/{app_name}.py" if base else f"{app_name}.py"

    def install(self, app_name):
        remote_path = self._build_remote_path(app_name)
        print("[APPINST] Installing app:", app_name)

        try:
            d = Disk()
            d.begin()
        except Exception as e:
            print("[APPINST] WARNING: Disk init failed:", e)

        old_dir = self.git.default_download_dir
        self.git.default_download_dir = self.apps_dir
        try:
            ok = self.git.download(self.user, self.repo, remote_path, branch=self.branch)
        finally:
            self.git.default_download_dir = old_dir

        if ok:
            print("[APPINST] App '{}' installed to {}/{}.py".format(app_name, self.apps_dir, app_name))
            try:
                time.sleep(1)
            except Exception:
                pass
        else:
            print("[APPINST] Failed to install app '{}'".format(app_name))
        return ok

    def uninstall(self, name):
        d = Disk()
        d.begin()
        try:
            os.remove(f"/SYSTEM32/APPS/{name}.py")
            print("[APPINST] uninstalled", name)
        except OSError as e:
            print("[APPINST] uninstall failed:", e)

    def listapps(self):
        files = os.listdir("/SYSTEM32/APPS")
        print("Detected apps:", [f[:-3] for f in files if f.endswith(".py")])


# =============================================================================
# Wiki
# =============================================================================

_HEADERS = {"User-Agent": "ZenoOS/1.0", "Accept": "application/json"}


class Wiki:
    def __init__(self, lang="en", width=60, lines=10, out=print):
        self.lang = lang
        self.width = width
        self.lines = lines
        self.out = out
        self.buf = []
        self.pos = 0

    def _wrap(self, text):
        out = []
        for raw in text.split("\n"):
            s = raw.strip()
            if not s:
                out.append("")
                continue
            while len(s) > self.width:
                cut = s.rfind(" ", 0, self.width)
                if cut < 0:
                    cut = self.width
                out.append(s[:cut])
                s = s[cut:].lstrip()
            out.append(s)
        return out

    def fetch(self, title, preview_dots=3):
        self.buf, self.pos = [], 0
        url = "https://{}.wikipedia.org/api/rest_v1/page/summary/{}".format(self.lang, title.replace(" ", "%20"))

        try:
            r = urequests.get(url, headers=_HEADERS)
            if r.status_code != 200:
                self.out("[wiki] http", r.status_code)
                return None
            text = r.json().get("extract", "")
            r.close()
            if not text:
                self.out("[wiki] empty article")
                return None

            self.buf = self._wrap(text)
            gc.collect()

            dots, printed = 0, 0
            for line in self.buf:
                self.out(line)
                printed += 1
                dots += line.count(".")
                if dots >= preview_dots or printed >= self.lines:
                    break
            self.pos = printed
            return None
        except Exception as e:
            self.out("[wiki] error:", e)
            return None

    def next(self):
        if self.pos >= len(self.buf):
            self.out("[wiki] end of article")
            return None
        end = self.pos + self.lines
        for line in self.buf[self.pos:end]:
            self.out(line)
        self.pos = end
        return None

    def search(self, query, n=5):
        url = "https://{}.wikipedia.org/w/api.php?action=query&list=search&format=json&srsearch={}".format(self.lang, query.replace(" ", "%20"))
        try:
            r = urequests.get(url, headers=_HEADERS)
            data = r.json()
            r.close()
            for i, item in enumerate(data["query"]["search"][:n]):
                self.out(i + 1, item["title"])
            return None
        except Exception as e:
            self.out("[wiki] search error:", e)
            return None


# =============================================================================
# AppDB
# =============================================================================

_DB_DIR = "/SYSTEM32/APPS/Data"
_DB_FILE = _DB_DIR + "/appdb.json"


class AppDB:
    def __init__(self):
        self._data = {}
        self._load()

    def _load(self):
        try:
            if _DB_DIR not in os.listdir("/SYSTEM32/APPS"):
                os.mkdir(_DB_DIR)
        except Exception:
            pass
        try:
            with open(_DB_FILE, "r") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            with open(_DB_FILE, "w") as f:
                json.dump(self._data, f)
        except Exception as e:
            print("[TinyAppDB] save failed:", e)

    def set(self, app, key, value):
        app, key = str(app), str(key)
        self._data.setdefault(app, {})[key] = value
        self._save()

    def get(self, app, key, default=None):
        try:
            return self._data.get(app, {}).get(key, default)
        except Exception:
            return default

    def delete(self, app, key):
        try:
            del self._data[app][key]
            if not self._data[app]:
                del self._data[app]
            self._save()
        except Exception:
            pass

    def clear(self, app):
        if app in self._data:
            del self._data[app]
            self._save()

    def dump(self):
        return self._data


class PackageManager:
    DEFAULT_USER = "FerrariForever95"
    DEFAULT_REPO = "Zeno-Micro-PC"
    PKGLIST_PATH = "/pkglist.json"
    PKGTABLE_CACHE_PATH = "/pkgtable_cache.json"

    def __init__(self, git=None, repo_user=None, repo_name=None):
        self.logger = Logger()
        self.source = "PKGMGR"
        self.git = git or Git()
        self.fm = FileManager()
        self.um = usermanager()
        self.repo_user = repo_user or self.DEFAULT_USER
        self.repo_name = repo_name or self.DEFAULT_REPO
        # nested install() calls (deps, module providers) share one
        # pkgtable.json download; only the outermost call deletes it
        self._op_depth = 0
        # in-memory copy of the pkgtable for the duration of one
        # outermost operation, so nested calls don't re-download it
        self._pkgtable_cache = None

        # -----------------------------------------------------------
        # Command registry -- maps command name -> callable.
        # This is what makes an installed package immediately usable
        # as a shell command in ZenCMD, without a reboot.
        # Kept as a single flat dict (name -> function reference) to
        # minimize RAM: no wrapper objects, no per-package class
        # instances, just a reference to the already-imported module's
        # function.
        # -----------------------------------------------------------
        self.commands = {}
        # command name -> module name, so unregister()/reload can find
        # and drop the right entry from sys.modules without guessing
        self._command_modules = {}
        # set to the module name currently running its install(pm)
        # hook, so register() can auto-record which module owns which
        # command without requiring package authors to pass it
        # explicitly (MicroPython has no `inspect` to derive this from
        # the call stack)
        self._installing_module = None

    def install(self, name, force=False):
        if not self._require_root("install"):
            return False

        self._begin_op()
        try:
            print("[PKG] === Installing '{}' ===".format(name))
            pkgtable = self._fetch_pkgtable()
            if pkgtable is None:
                return False

            entry = self._find_package(pkgtable, name)
            if entry is None:
                self._error("Package '{}' not found in pkgtable.json".format(name))
                return False

            installed = self._load_pkglist()
            if name in installed and not force:
                print("[PKG] '{}' is already installed (v{}). Use update() or reinstall() instead.".format(name, installed[name].get("version")))
                return False

            prior_record = installed.get(name) if force else None

            for dep in entry.get("dependencies", []):
                if not self._ensure_dependency(dep, pkgtable, installed):
                    self._error("Failed to satisfy dependency '{}' for '{}'".format(dep.get("name"), name))
                    return False
                installed = self._load_pkglist()

            required_modules = entry.get("modules", [])
            missing = self._check_modules(required_modules)
            if missing:
                if not self._resolve_missing_modules(missing, pkgtable, installed):
                    self._error("Cannot install '{}': missing required module(s) with no available package in pkgtable.json: {}".format(name, ", ".join(missing)))
                    return False
                installed = self._load_pkglist()
                still_missing = self._check_modules(required_modules)
                if still_missing:
                    self._error("Cannot install '{}': still missing module(s) after attempted auto-install: {}".format(name, ", ".join(still_missing)))
                    return False

            if not self._download_package(entry):
                self._error("Failed to download package '{}'".format(name))
                return False

            installed = self._load_pkglist()
            installed[name] = self._make_pkglist_entry(entry)
            if not self._save_pkglist(installed):
                return False

            self.fm.refresh_tree(entry["install_path"])

            # a force-install that moved filename/install_path leaves an
            # orphaned old file behind -- clean it up
            if prior_record:
                old_path = self._full_path(prior_record.get("install_path"), prior_record.get("filename"))
                new_path = self._full_path(entry.get("install_path"), entry.get("file", "").split("/")[-1])
                if old_path != new_path:
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass

            # bring the new command online immediately -- import the
            # module and let it register itself, no reboot required
            self._load_and_install_module(name, installed[name])

            print("[PKG] '{}' v{} installed successfully.".format(name, entry.get("version")))
            return True
        finally:
            self._end_op()

    def _module_name_for(self, record):
        """
        Derive an importable module name for a package record.

        Packages are plain .py modules dropped under install_path, so
        the module name is just the filename without the .py suffix.
        install_path is expected to already be on sys.path (or be '/'
        or '/lib', which MicroPython searches by default) -- this
        mirrors how packages were located for exec() under the old
        architecture, just importing instead of exec'ing.
        """
        filename = record.get("filename", "")
        if filename.endswith(".py"):
            filename = filename[:-3]
        return filename

    def _load_and_install_module(self, name, record):
        """
        Import a package's module (fresh, dropping any stale cached
        copy first) and call its install(pm) hook if present.

        This is the core of the new architecture: a package is just a
        module with a module-level install(pm)/uninstall(pm) and a
        run(args, shell) function. Importing it and calling install()
        is what makes its command appear in self.commands right away.
        """
        module_name = self._module_name_for(record)
        if not module_name:
            self._error("Cannot determine module name for package '{}'".format(name))
            return False

        # drop any previously-imported copy so we always pick up the
        # freshly-downloaded file (needed for update()/reinstall())
        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            module = __import__(module_name)
        except Exception as e:
            self._error("Could not import module '{}' for package '{}': {}".format(module_name, name, e))
            return False

        installer = getattr(module, "install", None)
        if installer is not None:
            self._installing_module = module_name
            try:
                installer(self)
            except Exception as e:
                self._error("install(pm) failed for package '{}': {}".format(name, e))
                return False
            finally:
                self._installing_module = None
        else:
            # older/simple packages may not define install() at all --
            # that's fine, they just won't appear in self.commands and
            # can still be run via the legacy exec path
            self.logger.debug("Package '{}' has no install(pm) hook".format(name), source=self.source)

        return True

    def _unload_module(self, name, record):
        """
        Call a package's uninstall(pm) hook if present, then drop its
        module from sys.modules and remove any command(s) it left in
        the registry. Best-effort: failures here should never block
        the actual file removal in uninstall().
        """
        module_name = self._module_name_for(record)
        module = sys.modules.get(module_name) if module_name else None

        if module is not None:
            uninstaller = getattr(module, "uninstall", None)
            if uninstaller is not None:
                try:
                    uninstaller(self)
                except Exception as e:
                    self.logger.warning(
                        "uninstall(pm) failed for package '{}': {}".format(name, e), source=self.source
                    )

        # belt-and-suspenders: even if the package didn't call
        # self.unregister() itself, drop any command(s) that were
        # pointing at this module so nothing dangling is left behind
        stale = [cmd for cmd, mod in self._command_modules.items() if mod == module_name]
        for cmd in stale:
            self.unregister(cmd)

        if module_name and module_name in sys.modules:
            del sys.modules[module_name]

    def uninstall(self, name):
        if not self._require_root("uninstall"):
            return False

        self._begin_op()
        try:
            print("[PKG] === Uninstalling '{}' ===".format(name))
            installed = self._load_pkglist()
            if name not in installed:
                self._error("Package '{}' is not installed.".format(name))
                return False

            record = installed[name]

            # core OS components (ZenCMD, FileManager, PackageManager, Network,
            # BootConfig, etc.) must never be removed through the normal
            # uninstall path -- only the future Recovery module should touch them
            if record.get("core", False):
                self._error("Package '{}' is a core OS component and cannot be uninstalled.".format(name))
                return False

            install_dir = record.get("install_path")

            # give the package a chance to clean up after itself and
            # pull its command out of the registry before its files
            # are deleted
            self._unload_module(name, record)

            # refuse to recursively delete anything that isn't a real,
            # specific package subdirectory -- protects against wiping
            # out '/' (or the whole tree) when install_path is missing,
            # empty, or points at the filesystem root
            if not self._is_safe_install_dir(install_dir):
                self._error("Refusing to uninstall '{}': install_path '{}' is missing or unsafe to remove.".format(name, install_dir))
                return False

            if not self._remove_dir_recursive(install_dir):
                self.logger.warning("Could not fully remove folder '{}' for package '{}'".format(install_dir, name), source=self.source)

            del installed[name]
            if not self._save_pkglist(installed):
                return False

            self.fm.refresh_tree(self._parent_dir(install_dir))
            print("[PKG] '{}' uninstalled.".format(name))
            return True
        finally:
            self._end_op()

    def reinstall(self, name):
        if not self._require_root("reinstall"):
            return False
        installed = self._load_pkglist()
        if name not in installed:
            self._error("Package '{}' is not installed, cannot reinstall.".format(name))
            return False

        # wrap the whole uninstall+install sequence as a single operation so
        # they share one pkgtable.json download/cache and only clean it up once
        self._begin_op()
        try:
            return self.uninstall(name) and self.install(name, force=True)
        finally:
            self._end_op()

    def update(self, name=None):
        if not self._require_root("update"):
            return False

        self._begin_op()
        try:
            installed = self._load_pkglist()
            if name is None:
                if not installed:
                    print("[PKG] No packages installed.")
                    return True
                ok = True
                for pkg_name in list(installed.keys()):
                    ok = self._update_one(pkg_name, installed) and ok
                return ok
            return self._update_one(name, installed)
        finally:
            self._end_op()

    def run(self, command, *args):
        """
        Execute a registered command's callback.

        This is the normal, RAM-cheap path used by ZenCMD: a package's
        install() already put a function reference into self.commands,
        so running it is just a dict lookup + call -- no re-exec, no
        re-reading the file from flash.
        """
        callback = self.commands.get(command)
        if callback is None:
            # fall back to the legacy exec-based path for any package
            # installed under the old architecture (no install()/run()
            # module functions, no registry entry) -- keeps old
            # pkglist.json entries working without forcing a reinstall
            return self._run_legacy(command, *args)

        try:
            callback(list(args), self)
        except Exception as e:
            self._error("Command '{}' raised an error while running: {}".format(command, e))
            return False
        return True

    def _run_legacy(self, name, *args):
        installed = self._load_pkglist()
        if name not in installed:
            self._error("Package '{}' is not installed.".format(name))
            return False

        record = installed[name]
        full_path = self._full_path(record.get("install_path"), record.get("filename"))
        try:
            with open(full_path) as f:
                code = f.read()
        except OSError as e:
            self._error("Cannot read '{}' for package '{}': {}".format(full_path, name, e))
            return False

        try:
            exec(compile(code, full_path, "exec"), {"__name__": "__main__", "argv": list(args)})
        except Exception as e:
            self._error("Package '{}' raised an error while running: {}".format(name, e))
            return False
        return True

    # -----------------------------------------------------------------
    # Command registry helpers
    # -----------------------------------------------------------------

    def register(self, command, callback, module_name=None):
        """
        Register a command -> callback mapping. Called by a package's
        own install(pm) function, e.g.:

            def install(pm):
                pm.register(COMMAND, run)

        Overwrites any existing registration for the same command name
        (this is what makes reload()/update() work without a reboot).
        Returns True on success.
        """
        if not command or not callable(callback):
            self._error("register() requires a non-empty command name and a callable")
            return False
        self.commands[command] = callback
        module_name = module_name or self._installing_module
        if module_name:
            self._command_modules[command] = module_name
        self.logger.debug("Registered command '{}'".format(command), source=self.source)
        return True

    def unregister(self, command):
        """Remove a command from the registry. Returns True if it existed."""
        existed = command in self.commands
        self.commands.pop(command, None)
        self._command_modules.pop(command, None)
        if existed:
            self.logger.debug("Unregistered command '{}'".format(command), source=self.source)
        return existed

    def check(self, command):
        """
        Return True if `command` is currently registered and runnable.

        Note: this now checks the live command registry rather than
        pkglist.json, since a package can register a command without
        necessarily matching its own package name (or vice versa).
        """
        return command in self.commands

    def list_commands(self):
        """Return a list of every currently registered command name."""
        return list(self.commands.keys())

    def reload(self, name):
        """
        Hot-reload an already-installed package's module: drop it from
        sys.modules, re-import it, and re-run install(pm). Since
        register() overwrites any existing entry for the same command,
        this swaps the callback in place -- no reboot needed.

        Use after a package's files have been updated on disk outside
        of update()/install() (e.g. manually edited), or simply to
        force a fresh import.
        """
        installed = self._load_pkglist()
        record = installed.get(name)
        if record is None:
            self._error("Package '{}' is not installed.".format(name))
            return False
        return self._load_and_install_module(name, record)

    def info(self, name):
        installed = self._load_pkglist()
        record = installed.get(name)

        print("\n[Package] {}".format(name))
        if record:
            print("  Installed     : yes")
            print("  Version       : {}".format(record.get("version")))
            print("  Author        : {}".format(record.get("author")))
            print("  Repository    : {}".format(record.get("repository")))
            print("  Branch        : {}".format(record.get("branch")))
            print("  Install path  : {}".format(record.get("install_path")))
            print("  Filename      : {}".format(record.get("filename")))
            print("  Dependencies  : {}".format(record.get("dependencies")))
            print("  Core          : {}".format(record.get("core", False)))
        else:
            print("  Installed     : no")

        pkgtable = self._fetch_pkgtable()
        try:
            entry = self._find_package(pkgtable, name) if pkgtable else None
            if entry:
                print("  Catalog version : {}".format(entry.get("version")))
                print("  Catalog author  : {}".format(entry.get("author")))
                print("  Catalog repo    : {}".format(entry.get("repository")))
                print("  Modules needed  : {}".format(entry.get("modules")))
                print("  Catalog core    : {}".format(entry.get("core", False)))
            elif not record:
                print("  Not found in catalog either.")
            return record or entry
        finally:
            self._cleanup_pkgtable_cache()

    def list(self):
        installed = self._load_pkglist()
        if not installed:
            print("[PKG] No packages installed.")
            return []
        print("\n[Installed Packages]")
        for pkg_name, record in installed.items():
            print("  {:<20} v{:<10} ({})".format(pkg_name, record.get("version", "?"), record.get("author", "?")))
        return list(installed.keys())

    def verify(self):
        installed = self._load_pkglist()
        issues = {}
        pkgtable = self._fetch_pkgtable()
        try:
            for pkg_name, record in installed.items():
                pkg_issues = []
                full_path = self._full_path(record.get("install_path"), record.get("filename"))
                try:
                    os.stat(full_path)
                except OSError:
                    pkg_issues.append("missing file: {}".format(full_path))

                entry = self._find_package(pkgtable, pkg_name) if pkgtable else None
                missing_modules = self._check_modules(entry.get("modules", []) if entry else [])
                if missing_modules:
                    pkg_issues.append("missing modules: {}".format(", ".join(missing_modules)))

                if pkg_issues:
                    issues[pkg_name] = pkg_issues

            if issues:
                for pkg_name, pkg_issues in issues.items():
                    print("  {}:".format(pkg_name))
                    for issue in pkg_issues:
                        print("    - {}".format(issue))
            return issues
        finally:
            self._cleanup_pkgtable_cache()
    def _begin_op(self):
        self._op_depth += 1

    def _end_op(self):
        self._op_depth -= 1
        if self._op_depth <= 0:
            self._op_depth = 0
            self._cleanup_pkgtable_cache()

    def _cleanup_pkgtable_cache(self):
        self._pkgtable_cache = None
        try:
            os.remove("/pkgtable.json")
        except OSError:
            pass

    def _current_user(self):
        try:
            return self.um.current_user()
        except Exception as e:
            self.logger.debug("Could not resolve current user: {}".format(e), source=self.source)
            return None

    def _is_root(self):
        user = self._current_user()
        if not user:
            return False
        try:
            return bool(self.um.isrooted(user))
        except Exception as e:
            self.logger.debug("Could not check root status: {}".format(e), source=self.source)
            return False

    def _require_root(self, action):
        if self._is_root():
            return True
        self._error("Permission denied: '{}' requires root privileges. Use usermanager().elevate(user, password) first.".format(action))
        return False

    def _fetch_pkgtable(self):
        # reuse the in-memory copy for the duration of one outermost
        # operation (install w/ nested deps, reinstall, update, ...)
        # instead of re-downloading pkgtable.json for every nested call
        if self._pkgtable_cache is not None:
            return self._pkgtable_cache

        ok = self.git.download(self.repo_user, self.repo_name, "pkgtable.json", save_dir="/")
        if not ok:
            self._error("Could not download pkgtable.json from {}/{}".format(self.repo_user, self.repo_name))
            return None
        data = self._load_json("/pkgtable.json")
        if data is None:
            self._error("Downloaded pkgtable.json but it could not be parsed as JSON")
            return None
        self._pkgtable_cache = data
        return data

    def _find_package(self, pkgtable, name):
        return pkgtable.get(name) if pkgtable else None

    def _find_package_providing_module(self, pkgtable, module_name):
        if not pkgtable:
            return None, None
        if module_name in pkgtable:
            return module_name, pkgtable[module_name]
        for pkg_name, entry in pkgtable.items():
            if module_name in entry.get("provides", []):
                return pkg_name, entry
        return None, None

    def _load_pkglist(self):
        data = self._load_json(self.PKGLIST_PATH)
        return data if isinstance(data, dict) else {}

    def _save_pkglist(self, data):
        return self._save_json(self.PKGLIST_PATH, data)

    def _load_json(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            self.logger.debug("Could not load JSON from '{}': {}".format(path, e), source=self.source)
            return None

    def _save_json(self, path, data):
        try:
            with open(path, "w") as f:
                json.dump(data, f)
            return True
        except OSError as e:
            self._error("Could not write '{}': {}".format(path, e))
            return False

    def _make_pkglist_entry(self, entry):
        return {
            "name": entry.get("name"), "version": entry.get("version"), "author": entry.get("author"),
            "repository": entry.get("repository"), "branch": entry.get("branch") or self.git.default_branch,
            "install_path": entry.get("install_path"), "filename": entry.get("file", "").split("/")[-1],
            "dependencies": entry.get("dependencies", []),
            # essential-OS-component flag, kept only for the future Recovery
            # module; defaults to False for backward compatibility with
            # older pkgtable.json files that don't have it yet
            "core": bool(entry.get("core", False)),
        }

    def _download_package(self, entry):
        # each catalog entry carries its own author/repository/branch --
        # independent of the repo used for pkgtable.json itself
        return self.git.download(entry.get("author"), entry.get("repository"), entry.get("file"),
                                  branch=entry.get("branch"), save_dir=entry.get("install_path"))

    def _ensure_dependency(self, dep, pkgtable, installed):
        dep_name = dep.get("name")
        dep_version = dep.get("version", "0")
        if dep_name in installed:
            current_version = installed[dep_name].get("version", "0")
            if not self._version_gt(dep_version, current_version):
                return True
            return self.install(dep_name, force=True)
        return self.install(dep_name)

    def _resolve_missing_modules(self, missing_modules, pkgtable, installed):
        all_ok = True
        for mod_name in missing_modules:
            pkg_name, entry = self._find_package_providing_module(pkgtable, mod_name)
            if not pkg_name:
                all_ok = False
                continue
            if pkg_name in installed:
                all_ok = False
                continue
            if not self.install(pkg_name):
                all_ok = False
        return all_ok

    def _check_modules(self, modules):
        missing = []
        for mod_name in modules or []:
            try:
                __import__(mod_name)
            except ImportError:
                missing.append(mod_name)
        return missing

    def _update_one(self, name, installed):
        if name not in installed:
            self._error("Package '{}' is not installed.".format(name))
            return False

        pkgtable = self._fetch_pkgtable()
        if pkgtable is None:
            return False

        entry = self._find_package(pkgtable, name)
        if entry is None:
            self._error("Package '{}' no longer exists in the catalog.".format(name))
            return False

        record = installed[name]
        if entry.get("author") != record.get("author") or entry.get("repository") != record.get("repository"):
            return False
        if not self._version_gt(entry.get("version", "0"), record.get("version", "0")):
            return True
        return self.install(name, force=True)

    def _full_path(self, install_path, filename):
        return "{}/{}".format((install_path or "/").rstrip("/"), filename)

    def _parent_dir(self, path):
        path = (path or "/").rstrip("/")
        if not path:
            return "/"
        idx = path.rfind("/")
        return path[:idx] if idx > 0 else "/"

    def _is_safe_install_dir(self, path):
        # a package's install_path must be a real, specific subdirectory --
        # never missing/empty, and never the filesystem root itself, since
        # that gets handed straight to a recursive delete
        if not path:
            return False
        normalized = path.rstrip("/")
        if normalized in ("", "/", ".", ".."):
            return False
        return True

    def _remove_dir_recursive(self, path):
        if not path:
            return False

        # defense in depth: even if a caller forgets to pre-check with
        # _is_safe_install_dir, never let this recurse over '/' or an
        # otherwise unsafe path
        normalized = path.rstrip("/")
        if normalized in ("", "/", ".", ".."):
            self.logger.warning("Refusing to recursively remove unsafe path '{}'".format(path), source=self.source)
            return False

        try:
            entries = os.listdir(path)
        except OSError:
            return True

        ok = True
        for entry in entries:
            full = path.rstrip("/") + "/" + entry
            try:
                is_dir = bool(os.stat(full)[0] & 0x4000)
            except OSError:
                continue
            if is_dir:
                if not self._remove_dir_recursive(full):
                    ok = False
            else:
                try:
                    os.remove(full)
                except OSError as e:
                    self.logger.warning("Could not remove file '{}': {}".format(full, e), source=self.source)
                    ok = False

        try:
            os.rmdir(path)
        except OSError as e:
            self.logger.warning("Could not remove directory '{}': {}".format(path, e), source=self.source)
            ok = False
        return ok

    def _version_tuple(self, version):
        parts = []
        for p in str(version).split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    def _version_gt(self, a, b):
        ta, tb = self._version_tuple(a), self._version_tuple(b)
        length = max(len(ta), len(tb))
        ta += (0,) * (length - len(ta))
        tb += (0,) * (length - len(tb))
        return ta > tb

    def _error(self, message):
        self.logger.error(message, source=self.source)
        print("[PKG] Error:", message)



"""
IoTManager — refactored.

Two bugs were found by cross-checking the code against the actual
installed API (via dir(SinricPro) / dir(SinricProSwitch)):

  1. `SinricProSwitch.raise_power_state_event` does not exist.
     The real method is `send_power_state_event`. on()/off() would have
     raised AttributeError on first use.

  2. `SinricPro.handle` does not exist in the introspected build — only
     private `_process_publish_queue()` / `_process_received_queue()`
     and websocket callbacks are exposed. This suggests the loop is
     driven internally (likely asyncio) rather than pumped manually.
     `handle()` below now fails loudly with a specific message instead
     of crashing on a generic AttributeError, so you can confirm the
     correct API for your installed sinricpro version and swap it in.

Other changes:
  - on()/off() collapsed into one `_set_power()` implementation.
  - Device bookkeeping moved from a raw dict into a small `Device`
    class (still just data, no behavior) — cheaper and clearer than
    a dict with three fixed keys.
  - Added local state caching + `toggle()`, since SinricProSwitch
    doesn't expose a state getter to read back from.
  - Added `list_devices()` / `remove_device()` for basic management.
"""


class Device:
    """Metadata for one registered device."""
    __slots__ = ("id", "name", "obj", "pin", "state")

    def __init__(self, device_id, name, obj, pin=None):
        self.id = device_id
        self.name = name
        self.obj = obj
        self.pin = pin
        self.state = False  # last known ON/OFF, tracked locally

    def __repr__(self):
        return f"<Device {self.name!r} id={self.id} state={'ON' if self.state else 'OFF'}>"


class IoTManager:
    """Manager class to coordinate smart home devices via Sinric Pro."""

    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret
        self.devices = {}    # device_id -> Device
        self.name_map = {}   # lowercased friendly name -> device_id
        self.client = SinricPro()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def add_switch(self, device_id, name, pin_number=None, custom_callback=None):
        """Register a switch device and link it to an optional hardware pin."""
        hardware_pin = Pin(pin_number, Pin.OUT) if pin_number is not None else None
        switch_device = SinricProSwitch(device_id)
        device = Device(device_id, name, switch_device, hardware_pin)

        def power_state_callback(did, state):
            print(f"\n[IoTManager] Cloud command -> '{name}' ({did}) set to {'ON' if state else 'OFF'}")
            device.state = state
            if hardware_pin:
                hardware_pin.value(1 if state else 0)
            if custom_callback:
                custom_callback(did, state)
            return True

        switch_device.on_power_state(power_state_callback)
        self.client.add_device(switch_device)

        self.devices[device_id] = device
        self.name_map[name.lower()] = device_id

        print(f"[IoTManager] Registered switch '{name}' [ID: {device_id}]")
        return switch_device

    def remove_device(self, identifier):
        """Unregister a device by ID or friendly name."""
        did = self._resolve_identifier(identifier)
        if did is None:
            print(f"[IoTManager] Device '{identifier}' not found.")
            return False
        name = self.devices[did].name
        del self.name_map[name.lower()]
        del self.devices[did]
        print(f"[IoTManager] Removed device '{name}' [ID: {did}]")
        return True

    def list_devices(self):
        """Return all registered Device objects."""
        return list(self.devices.values())

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def _resolve_identifier(self, identifier):
        """Resolve either a device ID or a friendly name to a device ID."""
        if identifier in self.devices:
            return identifier
        return self.name_map.get(str(identifier).lower())

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def _set_power(self, identifier, state):
        did = self._resolve_identifier(identifier)
        if did is None:
            print(f"[IoTManager] Device '{identifier}' not found.")
            return False

        device = self.devices[did]
        label = "ON" if state else "OFF"

        # Fixed: previously called the nonexistent `raise_power_state_event`.
        device.obj.send_power_state_event(state)
        device.state = state
        print(f"[IoTManager] Local command -> Turned {label} '{device.name}'")

        if device.pin:
            device.pin.value(1 if state else 0)
        return True

    def on(self, identifier):
        """Turn a device ON using its device ID or friendly name. Usage: iot.on('Living Room Light')"""
        return self._set_power(identifier, True)

    def off(self, identifier):
        """Turn a device OFF using its device ID or friendly name. Usage: iot.off('Living Room Light')"""
        return self._set_power(identifier, False)

    def toggle(self, identifier):
        """Flip a device's last known state (tracked locally, since
        SinricProSwitch exposes no state getter to read back from)."""
        did = self._resolve_identifier(identifier)
        if did is None:
            print(f"[IoTManager] Device '{identifier}' not found.")
            return False
        return self._set_power(did, not self.devices[did].state)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        print("[IoTManager] Connecting to Sinric Pro...")
        self.client.start(self.app_key, self.app_secret)

    def handle(self):
        """
        Pump the SinricPro client's network loop.

        The installed SinricPro build (per dir(SinricPro)) exposes no
        public `handle()` — only private `_process_publish_queue()` /
        `_process_received_queue()` and websocket callbacks. That points
        to an internally-driven (likely asyncio) loop rather than a
        method you pump yourself. This raises a clear, specific error
        instead of silently failing, so you can confirm the correct API
        for your installed version and update this method accordingly.
        """
        handler = getattr(self.client, "handle", None)
        if handler is None:
            raise AttributeError(
                "SinricPro has no 'handle' method in this build. Check whether "
                "you're expected to call _process_publish_queue()/"
                "_process_received_queue() directly, or whether start() runs "
                "an asyncio loop that needs asyncio.run()/create_task() instead "
                "of manual pumping."
            )
        try:
            handler()
        except Exception as e:
            print(f"[IoTManager] Network handle error: {e}")
            
