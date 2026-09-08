"""
Services.py -- Zeno OS core services, single file.

Unified kernel services architecture for ESP32-S3 (8MB PSRAM / 16MB Flash).
Handles Logging, Power Management, VFS, CFS Process Scheduling,
Security, Wi-Fi Networking, Storage Subsystems, and Lazy Package Management.
"""

import os
import sys
import gc
import time
import json
import ssl
import usocket
import urequests
import micropython
import machine
from machine import Pin, SPI, I2C

try:
    import ubluetooth as bt
except ImportError:
    bt = None

try:
    import _thread
    _HAVE_THREAD = True
except ImportError:
    _HAVE_THREAD = False

try:
    import urandom
    _HAVE_URANDOM = True
except ImportError:
    import random as urandom
    _HAVE_URANDOM = False

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

try:
    import network as _network
except ImportError:
    _network = None

try:
    import uasyncio as asyncio
    _HAVE_UASYNCIO = True
except ImportError:
    asyncio = None
    _HAVE_UASYNCIO = False

try:
    from firmware import SDCard
except ImportError:
    SDCard = None

try:
    import zeno
except ImportError:
    class _FallbackZeno:
        user = "root"
        password = ""
        gitsecret = ""
        authorized = True
        boot_cap = None
    zeno = _FallbackZeno()

# ============================================================================
# SYSTEM CONSTANTS & TOKENS
# ============================================================================
_SYSTEM_TOKEN = "zeno_kernel_internal_elevated"
debug_log_enabled = True

# ============================================================================
# LOGGER SERVICE
# ============================================================================
class Logger:
    LEVELS = {0: "ERROR", 1: "WARNING", 2: "DEBUG"}

    def __init__(self, log_file_user="/LOGS/systemlog.txt", boot=False):
        self.boot = boot
        self.log_file_user = log_file_user
        self._ensure_dir("/LOGS")
        self._create_file(self.log_file_user)
        self._boot_marker = "[BOOT_START]"

        if self.boot:
            self._write(self._boot_marker)
            self.debug("Logger initialized. Boot starting...", source="BOOT")

    def _ensure_dir(self, path):
        try:
            if path.strip("/") not in os.listdir("/"):
                os.mkdir(path)
        except Exception:
            pass

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

    def help(self):
        print("Logger: log(level, msg), error(msg), warning(msg), debug(msg), viewlogs(lines), clear_logs()")


# ============================================================================
# POWER MANAGEMENT & HARDWARE CONTROL
# ============================================================================
_PLATFORM_LEVELS = {
    "esp32": {"low": 80_000_000, "normal": 160_000_000, "high": 240_000_000, "turbo": 240_000_000},
    "rp2":   {"low": 48_000_000, "normal": 125_000_000, "high": 200_000_000, "turbo": 250_000_000},
}
_LEVEL_ORDER = ("low", "normal", "high", "turbo")

class PowerManagement:
    def __init__(self, logger=None):
        self.logger = logger or Logger()
        self.source = "POWERMGR"
        self._platform = sys.platform
        self.levels = dict(_PLATFORM_LEVELS.get(self._platform, {}))
        if not self.levels:
            base = self._raw_freq() or 160_000_000
            self.levels = {"low": base, "normal": base, "high": base, "turbo": base}
        self.baseline = "normal" if "normal" in self.levels else _LEVEL_ORDER[0]
        self._requests = {}

    def help(self):
        print("PowerManagement: status(), levels_list(), set_level(lvl), boost(reason, lvl), release(reason)")

    def status(self):
        current = self._raw_freq()
        print("\n[Power]")
        print(f"  Platform      : {self._platform}")
        print(f"  Current freq  : {self._fmt_hz(current)}")
        print(f"  Baseline tier : {self.baseline} ({self._fmt_hz(self.levels.get(self.baseline))})")
        if self._requests:
            print("  Active boosts :")
            for reason, level in self._requests.items():
                print(f"    - {reason:<20} -> {level}")
        else:
            print("  Active boosts : none")
        return {"platform": self._platform, "current_hz": current, "requests": dict(self._requests)}

    def levels_list(self):
        print(f"\n[Power] Available tiers ({self._platform}):")
        for name in _LEVEL_ORDER:
            if name in self.levels:
                print(f"  {name:<8} {self._fmt_hz(self.levels[name])}")
        return dict(self.levels)

    def set_level(self, level):
        return self._apply(level)

    def baseline_set(self, level):
        if level not in self.levels:
            self._error(f"Unknown level '{level}'")
            return False
        self.baseline = level
        if not self._requests:
            self._apply(level)
        return True

    def boost(self, reason, level="high"):
        if level not in self.levels:
            self._error(f"Unknown level '{level}'")
            return False
        self._requests[reason] = level
        return self._apply(self._highest_requested())

    def release(self, reason):
        if reason in self._requests:
            del self._requests[reason]
        target = self._highest_requested() or self.baseline
        return self._apply(target)

    def auto_scale(self, load_percent, reason="auto"):
        if load_percent is None:
            return
        if load_percent >= 70:
            self.boost(reason, "high")
        elif load_percent >= 40:
            self.boost(reason, "normal")
        else:
            self.release(reason)

    def _highest_requested(self):
        active = [lvl for lvl in self._requests.values() if lvl in self.levels]
        if not active:
            return None
        return max(active, key=lambda lvl: _LEVEL_ORDER.index(lvl))

    def _apply(self, level):
        if level not in self.levels:
            return False
        hz = self.levels[level]
        try:
            machine.freq(hz)
            self.logger.debug(f"CPU freq -> {level} ({self._fmt_hz(hz)})", source=self.source)
            return True
        except Exception as e:
            self._error(f"Failed to set frequency: {e}")
            return False

    def _raw_freq(self):
        try:
            return machine.freq()
        except Exception:
            return None

    def _fmt_hz(self, hz):
        if not hz:
            return "unknown"
        return f"{hz / 1_000_000:.0f} MHz"

    def _error(self, message):
        self.logger.error(message, source=self.source)
        print("[POWER] Error:", message)


class CPU:
    def __init__(self, model="ESP32-S3"):
        self.model = model
        self.usage_pct = 0

    def report_frame(self, busy_us, idle_us):
        total = busy_us + idle_us
        if total <= 0:
            return
        self.usage_pct = (busy_us * 100) // total

    def usage(self):
        return self.usage_pct

    def reboot(self):
        time.sleep_ms(50)
        machine.reset()

    def shutdown(self):
        time.sleep_ms(100)
        if hasattr(machine, "deepsleep"):
            machine.deepsleep()
        elif hasattr(machine, "poweroff"):
            machine.poweroff()

    def sleep_ms(self, ms):
        machine.lightsleep(ms)

    def set_freq(self, hz):
        machine.freq(hz)

    def get_freq(self):
        return machine.freq()

    def reset_cause(self):
        return machine.reset_cause()


# ============================================================================
# SECURITY & PRIVILEGE MODEL
# ============================================================================
class SystemPrivilege:
    _active_depth = 0

    def __init__(self, token, reason="system operation"):
        if token != _SYSTEM_TOKEN:
            raise PermissionError("Invalid system privilege token")
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
    USERINFO_PATH = "/userinfo.json"
    _session_root = False

    def __init__(self):
        self._ensure_userstore()

    def _ensure_userstore(self):
        if not self._exists(self.USERINFO_PATH):
            default_user = getattr(zeno, "user", "root")
            default_pwd = getattr(zeno, "password", "")
            data = {"user": default_user, "password": default_pwd, "root": False}
            try:
                with open(self.USERINFO_PATH, "w") as f:
                    json.dump(data, f)
            except Exception:
                pass

    def _exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def __read__(self):
        try:
            with open(self.USERINFO_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {"user": getattr(zeno, "user", "root"), "password": getattr(zeno, "password", ""), "root": False}

    def __write__(self, data):
        try:
            with open(self.USERINFO_PATH, "w") as f:
                json.dump(data, f)
            return True
        except Exception:
            return False

    def is_session_root(self):
        return usermanager._session_root or SystemPrivilege.active()

    def isrooted(self, username=None):
        return self.is_session_root()

    def current_user(self):
        return self.__read__().get("user", "user")

    def userinfo(self):
        info = dict(self.__read__())
        info.pop("password", None)
        info["session_root"] = self.is_session_root()
        return info

    def userdebug(self):
        return self.userinfo()

    def elevate(self, username, password):
        stored = self.__read__()
        if username == stored.get("user") and str(password) == str(stored.get("password")):
            usermanager._session_root = True
            stored["root"] = True
            self.__write__(stored)
            return True
        return False

    def delevate(self, username, password):
        stored = self.__read__()
        if username == stored.get("user") and str(password) == str(stored.get("password")):
            usermanager._session_root = False
            return True
        return False

    def change_password(self, username, old_password, new_password):
        stored = self.__read__()
        if username == stored.get("user") and str(old_password) == str(stored.get("password")):
            stored["password"] = str(new_password)
            if self.__write__(stored):
                setattr(zeno, "password", str(new_password))
                return True
        return False

    def change_username(self, current_user, password, new_user):
        stored = self.__read__()
        if current_user == stored.get("user") and str(password) == str(stored.get("password")):
            stored["user"] = str(new_user).strip()
            if self.__write__(stored):
                setattr(zeno, "user", str(new_user).strip())
                return True
        return False

    def rebuild(self, system_token=None):
        if system_token != _SYSTEM_TOKEN:
            raise PermissionError("Invalid system privilege token")
        with SystemPrivilege(system_token, "rebuild"):
            fresh = {"user": getattr(zeno, "user", "root"), "password": getattr(zeno, "password", ""), "root": False}
            self.__write__(fresh)
            usermanager._session_root = False
            return True


# ============================================================================
# FILESYSTEM & VFS FACADE
# ============================================================================
SYS_DIR = "/.sys"
PERM_FULL, PERM_READ, PERM_READWRITE = 0, 1, 2
VALID_PERMS = {0, 1, 2, 3, 4, 5, 6, 7}

class FileManager:
    def __init__(self):
        self.um = usermanager()
        self._fd_table = {}
        self._next_fd = 3
        self._ensure_sys()

    def _ensure_sys(self):
        try:
            if ".sys" not in os.listdir("/"):
                os.mkdir(SYS_DIR)
        except Exception:
            pass

    def _norm(self, path):
        if not path:
            return "/"
        parts = [p for p in str(path).split("/") if p and p != "."]
        out = []
        for p in parts:
            if p == "..":
                if out:
                    out.pop()
            else:
                out.append(p)
        return "/" + "/".join(out) if out else "/"

    def exists(self, path):
        try:
            os.stat(self._norm(path))
            return True
        except OSError:
            return False

    def metadata(self, path):
        p = self._norm(path)
        if not self.exists(p):
            raise OSError(f"File not found: {p}")
        st = os.stat(p)
        is_dir = bool(st[0] & 0x4000)
        return {
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else (st[6] if len(st) > 6 else 0),
            "owner": "root" if p.startswith("/system") or p.startswith("/.sys") else "user",
            "permission": "rwx" if is_dir else "rw-",
        }

    def listdir(self, path="/", show_hidden=False):
        p = self._norm(path)
        entries = os.listdir(p)
        if not show_hidden:
            entries = [e for e in entries if not e.startswith(".")]
        return sorted(entries)

    def mkdir(self, path, owner=None, permission=None):
        p = self._norm(path)
        os.mkdir(p)
        return True

    def delete(self, path):
        p = self._norm(path)
        if p == "/":
            raise PermissionError("Cannot remove root")
        meta = self.metadata(p)
        if meta["type"] == "directory":
            for child in self.listdir(p, show_hidden=True):
                self.delete(p + "/" + child)
            os.rmdir(p)
        else:
            os.remove(p)
        return True

    def open(self, path, mode="r"):
        p = self._norm(path)
        h = open(p, mode)
        fd = self._next_fd
        self._next_fd += 1
        self._fd_table[fd] = h
        return fd

    def read(self, fd, size=-1):
        if fd not in self._fd_table:
            raise ValueError(f"Invalid fd: {fd}")
        return self._fd_table[fd].read(size)

    def write(self, fd, data):
        if fd not in self._fd_table:
            raise ValueError(f"Invalid fd: {fd}")
        return self._fd_table[fd].write(data)

    def close(self, fd):
        h = self._fd_table.pop(fd, None)
        if h:
            h.close()
            return True
        return False

    def create(self, path, initial=""):
        p = self._norm(path)
        with open(p, "w") as f:
            f.write(initial)
        return True

    def copy(self, src, dst):
        s_path, d_path = self._norm(src), self._norm(dst)
        meta = self.metadata(s_path)
        if meta["type"] == "directory":
            self.mkdir(d_path)
            for item in self.listdir(s_path, show_hidden=True):
                self.copy(s_path + "/" + item, d_path + "/" + item)
        else:
            with open(s_path, "rb") as sf, open(d_path, "wb") as df:
                while True:
                    b = sf.read(512)
                    if not b:
                        break
                    df.write(b)
        return True

    def move(self, src, dst):
        s_path, d_path = self._norm(src), self._norm(dst)
        try:
            os.rename(s_path, d_path)
        except OSError:
            self.copy(s_path, d_path)
            self.delete(s_path)
        return True

    def refresh_tree(self, path="/", system_token=None):
        return True


# ============================================================================
# PROCESS SCHEDULER (CFS COOPERATIVE + PREEMPTIVE THREADING)
# ============================================================================
PID_TYPE_KERNEL  = 1
PID_TYPE_USER    = 2
PID_TYPE_THREAD  = 3
PID_TYPE_DAEMON  = 4

class Process:
    __slots__ = ("pid", "owner", "name", "func", "mode", "priority", "state", "vruntime", "exit_code")

    def __init__(self, pid, owner, name, func, mode="loop", priority=0):
        self.pid = pid
        self.owner = owner
        self.name = name
        self.func = func
        self.mode = mode
        self.priority = priority
        self.state = "READY"
        self.vruntime = 0
        self.exit_code = None

    def as_row(self):
        return f"{self.pid:<6} {self.owner:<8} {self.name:<16} {self.state:<8} {self.priority:<4} {self.mode:<8}"


class Scheduler:
    def __init__(self, logger=None, power=None):
        self.logger = logger or Logger()
        self.power = power or PowerManagement(self.logger)
        self.table = {}
        self._active_pid = None
        self.running = False
        self._next_pid = 1000

    def spawn(self, name, func, *, mode="loop", priority=0, owner="root"):
        pid = self._next_pid
        self._next_pid += 1
        p = Process(pid, owner, name, func, mode, priority)
        self.table[pid] = p

        if mode == "thread" and _HAVE_THREAD:
            def _runner():
                self._active_pid = p.pid
                p.state = "RUNNING"
                try:
                    p.func(p.pid)
                    p.exit_code = 0
                except Exception as e:
                    p.exit_code = -1
                    self.logger.error(f"Thread {p.name} failed: {e}", "SCHED")
                p.state = "DEAD"
            _thread.start_new_thread(_runner, ())
        return pid

    def kill(self, pid, sig=15, system_token=None):
        if pid in self.table:
            self.table[pid].state = "DEAD"
            del self.table[pid]
            return True
        return False

    def list(self):
        print(f"{'PID':<6} {'OWNER':<8} {'NAME':<16} {'STATE':<8} {'NI':<4} {'MODE':<8}")
        for p in self.table.values():
            print(p.as_row())
        return list(self.table.keys())

    def ps(self):
        return [p.name for p in self.table.values()]

    def checkpoint(self, pid):
        p = self.table.get(pid)
        if p and p.state == "DEAD":
            raise SystemExit

    def tick(self):
        for pid, p in list(self.table.items()):
            if p.mode != "thread" and p.state == "READY":
                self._active_pid = pid
                p.state = "RUNNING"
                try:
                    p.func()
                    if p.mode == "once":
                        p.state = "DEAD"
                    else:
                        p.state = "READY"
                except Exception as e:
                    self.logger.error(f"Task {p.name} exception: {e}", "SCHED")
                    p.state = "DEAD"
                self._active_pid = None


# ============================================================================
# WI-FI NETWORKING SERVICE
# ============================================================================
PROFILES_PATH = "/LOGS/wifi_profiles.json"

class WifiManager:
    def __init__(self, logger=None, timeout=15):
        self.logger = logger or Logger()
        self.source = "NETWORK"
        self.timeout = timeout
        self.wlan = _network.WLAN(_network.STA_IF) if _network else None

    def _load_profiles(self):
        try:
            with open(PROFILES_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_profiles(self, p):
        try:
            with open(PROFILES_PATH, "w") as f:
                json.dump(p, f)
            return True
        except Exception:
            return False

    def connect(self, ssid, password, save=False):
        if not self.wlan:
            print("[Wifi] Network hardware unavailable")
            return False
        self.wlan.active(True)
        self.wlan.connect(ssid, password)
        t0 = time.time()
        while not self.wlan.isconnected():
            if time.time() - t0 > self.timeout:
                self.logger.error(f"Connection timeout to {ssid}", self.source)
                return False
            time.sleep_ms(200)
        self.logger.debug(f"Connected to {ssid}: {self.wlan.ifconfig()[0]}", self.source)
        if save:
            prof = self._load_profiles()
            prof[ssid] = password
            self._save_profiles(prof)
        return True

    def isconnected(self):
        return bool(self.wlan and self.wlan.isconnected())

    def disconnect(self):
        if self.wlan:
            self.wlan.disconnect()
            self.wlan.active(False)
        return True

    def scan(self):
        if not self.wlan:
            return []
        self.wlan.active(True)
        nets = []
        for n in self.wlan.scan():
            nets.append(n[0].decode("utf-8", "ignore"))
        return nets

    def status(self):
        if not self.isconnected():
            return {"connected": False, "ip": None}
        cfg = self.wlan.ifconfig()
        return {"connected": True, "ip": cfg[0], "mask": cfg[1], "gw": cfg[2], "dns": cfg[3]}

    def help(self):
        print("WifiManager: connect(ssid, pwd, save=False), disconnect(), scan(), status(), isconnected()")


# ============================================================================
# DISK & MASS STORAGE (SPI SD & RAMDISK)
# ============================================================================
SD_SCK, SD_MOSI, SD_MISO, SD_CS = 40, 6, 5, 7

class Disk:
    def __init__(self, mount_point="/MemDisk"):
        self.mount_point = mount_point
        self.logger = Logger()
        self.spi = None
        self.sd = None
        self.cs = Pin(SD_CS, Pin.OUT) if hasattr(machine, "Pin") else None

    def begin(self):
        if SDCard is None:
            self.logger.warning("SDCard driver missing in firmware.", "DISK")
            return False
        try:
            self.spi = SPI(1, baudrate=20_000_000, polarity=0, phase=0,
                           sck=Pin(SD_SCK, Pin.OUT), mosi=Pin(SD_MOSI, Pin.OUT), miso=Pin(SD_MISO, Pin.OUT))
            self.sd = SDCard(self.spi, self.cs)
            os.mount(self.sd, self.mount_point)
            self.logger.debug(f"Mounted SD at {self.mount_point}", "DISK")
            return True
        except Exception as e:
            self.logger.error(f"SD mount failed: {e}", "DISK")
            return False

    def unmount(self):
        try:
            os.umount(self.mount_point)
            return True
        except Exception as e:
            self.logger.error(f"Unmount error: {e}", "DISK")
            return False

    def info(self, path="/"):
        try:
            st = os.statvfs(path)
            total = st[2] * st[0]
            free = st[3] * st[0]
            print(f"Mount: {path} | Total: {total / (1024*1024):.2f} MB | Free: {free / (1024*1024):.2f} MB")
            return {"total": total, "free": free}
        except Exception as e:
            print(f"[Disk] statvfs error: {e}")
            return None


class BootConfig:
    def __init__(self):
        self.cfg_path = "/LOGS/bootcfg.json"
        self.default = {"BOOT_MODE": "NORMAL", "WIFI_AUTOCONNECT": True, "MODE": "PERFORMANCE"}
        self.config = self._load()

    def _load(self):
        try:
            with open(self.cfg_path, "r") as f:
                return json.load(f)
        except Exception:
            return dict(self.default)

    def save(self):
        try:
            with open(self.cfg_path, "w") as f:
                json.dump(self.config, f)
            return True
        except Exception:
            return False

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, val):
        self.config[key] = val
        self.save()


# ============================================================================
# SYSTEM CONTROL & DIAGNOSTICS
# ============================================================================
class system:
    def __init__(self, opt_level=0, debug=False):
        self.log = Logger()

    def restart(self):
        machine.reset()

    def info(self):
        print(f"Architecture: {sys.platform} (MicroPython {sys.version})")
        print(f"CPU Frequency: {machine.freq() // 1_000_000} MHz")
        free, alloc = gc.mem_free(), gc.mem_alloc()
        print(f"PSRAM/Heap: Total={(free+alloc)/(1024*1024):.2f}MB | Free={free/(1024*1024):.2f}MB")

    def collect(self):
        b = gc.mem_free()
        gc.collect()
        a = gc.mem_free()
        print(f"Reclaimed {a - b} bytes.")


# ============================================================================
# HTTP TRANSFER, GIT & LAZY PACKAGE MANAGER
# ============================================================================
class downloadhelper:
    def __init__(self):
        self.log = Logger()

    def download_file(self, url, save_dir="/", save_file=None):
        if not urequests:
            return None
        res = urequests.get(url)
        try:
            if res.status_code == 200:
                name = save_file or url.split("/")[-1]
                path = f"{save_dir.rstrip('/')}/{name}"
                with open(path, "wb") as f:
                    f.write(res.content)
                return path
        finally:
            res.close()
        return None


class Git:
    def __init__(self):
        self.dh = downloadhelper()

    def download(self, user, repo, filepath, branch="main", save_dir="/"):
        url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{filepath.lstrip('/')}"
        return bool(self.dh.download_file(url, save_dir=save_dir))


class PackageManager:
    PKGLIST_PATH = "/pkglist.json"

    def __init__(self):
        self.git = Git()
        self.commands = {}
        self._command_modules = {}

    def _load_pkglist(self):
        try:
            with open(self.PKGLIST_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def register(self, command, callback, module_name=None):
        self.commands[command] = callback
        if module_name:
            self._command_modules[command] = module_name
        return True

    def check(self, name):
        if name in self.commands:
            return True
        installed = self._load_pkglist()
        return name in installed

    def run(self, name, *args):
        if name in self.commands:
            return self.commands[name](list(args), self)
        
        installed = self._load_pkglist()
        if name in installed:
            rec = installed[name]
            mod_path = f"{rec.get('install_path', '/bin/' + name)}/{rec.get('filename', name + '.py')}"
            if mod_path not in sys.path:
                sys.path.append(rec.get("install_path", "/bin/" + name))
            with open(mod_path, "r") as f:
                code = f.read()
            exec(compile(code, mod_path, "exec"), {"__name__": "__main__", "argv": list(args)})
            return True
        print(f"[PKG] Command/Package '{name}' not found.")
        return False

    def list(self):
        pkgs = self._load_pkglist()
        print("\nInstalled Packages:")
        for k, v in pkgs.items():
            print(f"  {k:<16} v{v.get('version', '?')}")
        return list(pkgs.keys())


# ============================================================================
# BLUETOOTH & IOT MANAGER STUBS
# ============================================================================
class BluetoothManager:
    def __init__(self, device_name="Zeno Micro PC"):
        self.device_name = device_name
        self.ble = bt.BLE() if bt else None

    def on(self):
        if self.ble:
            self.ble.active(True)

    def off(self):
        if self.ble:
            self.ble.active(False)

    def status(self):
        return "BLE Active" if (self.ble and self.ble.active()) else "BLE Inactive"


class IoTManager:
    def __init__(self, app_key=None, app_secret=None):
        self.app_key = app_key
        self.app_secret = app_secret

    def status(self):
        return "IoT Bus Offline"
