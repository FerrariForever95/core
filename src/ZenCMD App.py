# ===========================================================================
# Zeno UI App: ZenCMD Graphical Terminal (Ubuntu Shell Style)
# Uses zeno.ui and from Graphics import Graphics as gfx exclusively
# ===========================================================================

import os
import sys
import json
import time
import zeno

if "/bin/Graphics" not in sys.path:
    sys.path.append("/bin/Graphics")

from Graphics import Graphics as gfx

# Map required Graphics API attributes
color565        = gfx.color565
UIWindow        = gfx.UIWindow
UIButton        = gfx.UIButton
UIText          = gfx.UIText
UISlider        = gfx.UISlider
UIToggleSwitch  = gfx.UIToggleSwitch
UIProgressBar   = gfx.UIProgressBar
UIPanel         = gfx.UIPanel
UIListView      = gfx.UIListView
Keyboard        = gfx.Keyboard

# Use zeno.ui exclusively as the lcd object
lcd = zeno.ui
gfx.bind_lcd(lcd)

APP_NAME    = "ZenCMD"
APP_AUTHOR  = "Phoenix"
APP_VERSION = "3.9-GUI"

# Ubuntu Terminal Color Palette
UBUNTU_BG      = color565(24, 6, 20)      # Deep Ubuntu Aubergine/Black
PROMPT_BACK    = color565(38, 12, 32)     # Input line background
SYS_GREEN      = color565(78, 212, 38)    # System output & prompt green
USER_WHITE     = color565(255, 255, 255)  # User command text white
BORDER_COL     = color565(70, 70, 72)

# Global UI references
screen = None
term_list = None
kb = None
_in_modal_input = False
_modal_prompt_str = ""

# =================================================
# NON-TOUCH DUAL-COLOR TERMINAL LISTVIEW
# =================================================
class TerminalListView(UIListView):
    """Subclass of UIListView with touch disabled and Ubuntu-style
    green system text / white user input rendering."""

    def __init__(self, lcd_obj, x, y, w, h, item_h=12, bg=UBUNTU_BG):
        super().__init__(lcd_obj, x, y, w, h, items=[], item_h=item_h,
                         bg=bg, fg=SYS_GREEN, text_x=4, highlight=False)
        self.max_cols = max(1, (self.w - 8) // 8)
        self.max_lines = 150

    def handle_touch(self, pt):
        # Strictly disable touch interaction on the terminal log view
        return False

    def add_line(self, text, kind="sys", redraw=True):
        """Append text to terminal.
        kind='sys'  -> Green system text
        kind='user' -> White user text
        kind='cmd'  -> Tuple ('cmd', prompt_str, user_cmd_str)
        """
        if kind == "cmd" and isinstance(text, tuple):
            _, p_str, u_str = text
            full_len = len(p_str) + len(u_str)
            if full_len <= self.max_cols:
                self.items.append(("cmd", p_str, u_str))
            else:
                # First line gets prompt + start of user command
                avail = max(1, self.max_cols - len(p_str))
                self.items.append(("cmd", p_str, u_str[:avail]))
                rem = u_str[avail:]
                while rem:
                    self.items.append(("user", rem[:self.max_cols]))
                    rem = rem[self.max_cols:]
        else:
            raw_str = str(text)
            for ln in raw_str.split("\n"):
                if not ln:
                    self.items.append((kind, ""))
                    continue
                while len(ln) > self.max_cols:
                    self.items.append((kind, ln[:self.max_cols]))
                    ln = ln[self.max_cols:]
                if ln:
                    self.items.append((kind, ln))

        # Trim scrollback buffer to save RAM
        while len(self.items) > self.max_lines:
            self.items.pop(0)

        # Auto-scroll to bottom
        self.scroll = self._max_scroll()
        if redraw and self.visible:
            self.draw()

    def clear_terminal(self, redraw=True):
        self.items = []
        self.scroll = 0
        if redraw and self.visible:
            self.draw()

    def draw(self):
        if not self.visible:
            return
        gfx.fill_rect(self.x, self.y, self.w, self.h, self.bg)
        gfx.draw_rect(self.x, self.y, self.w, self.h, BORDER_COL, fill=False)
        if not self.items:
            return

        self.scroll = gfx._clamp(self.scroll, 0, self._max_scroll())
        first = self.scroll // self.item_h
        offset = self.scroll % self.item_h
        rows = (self.h + offset + self.item_h - 1) // self.item_h
        bottom = self.y + self.h

        for i in range(rows):
            idx = first + i
            if idx >= len(self.items):
                break
            iy = self.y + i * self.item_h - offset
            ty = iy + (self.item_h - 8) // 2
            if ty < self.y + 1 or ty + 8 > bottom - 1:
                continue

            entry = self.items[idx]
            if isinstance(entry, tuple):
                etype = entry[0]
                if etype == "cmd":
                    p_str, u_str = entry[1], entry[2]
                    # Draw green shell prompt
                    gfx.draw_text(self.x + self.text_x, ty, p_str, SYS_GREEN, self.bg, gfx.FONT_8X8, 1)
                    # Draw white user command right after prompt
                    px_offset = self.x + self.text_x + len(p_str) * 8
                    if u_str:
                        gfx.draw_text(px_offset, ty, u_str, USER_WHITE, self.bg, gfx.FONT_8X8, 1)
                elif etype == "user":
                    gfx.draw_text(self.x + self.text_x, ty, entry[1], USER_WHITE, self.bg, gfx.FONT_8X8, 1)
                else:
                    gfx.draw_text(self.x + self.text_x, ty, entry[1], SYS_GREEN, self.bg, gfx.FONT_8X8, 1)
            else:
                gfx.draw_text(self.x + self.text_x, ty, str(entry), SYS_GREEN, self.bg, gfx.FONT_8X8, 1)


# =================================================
# MICROPYTHON-SAFE OUTPUT CAPTURE (GUI ROUTED)
# =================================================
_real_print = print
_OUT = None          # None = GUI TerminalListView, otherwise an _OutputCapture (for pipes)
_gui_line_buf = ""


def print(*args, sep=" ", end="\n", file=None):
    global _gui_line_buf
    if file is not None:
        _real_print(*args, sep=sep, end=end, file=file)
        return
    s = sep.join([str(a) for a in args]) + end

    # Always echo to serial REPL for hardware debugging
    _real_print(s, end="")

    if _OUT is not None:
        _OUT.write(s)
    elif term_list is not None:
        _gui_line_buf += s
        while "\n" in _gui_line_buf:
            line, _gui_line_buf = _gui_line_buf.split("\n", 1)
            term_list.add_line(line, kind="sys", redraw=False)
        term_list.draw()


def draw_input_bar(cursor_on=True):
    """Draws the Ubuntu-style live command input bar right above the keyboard."""
    if screen is None or kb is None:
        return
    bx, by, bw, bh = 4, 154, gfx.WIDTH - 8, 22
    gfx.fill_rect(bx, by, bw, bh, PROMPT_BACK)
    gfx.draw_rect(bx, by, bw, bh, BORDER_COL, fill=False)

    p_str = _modal_prompt_str if _in_modal_input else (_prompt() + " ")
    u_str = kb.buffer + ("_" if cursor_on else " ")

    max_chars = (bw - 8) // 8
    total_len = len(p_str) + len(u_str)
    if total_len > max_chars:
        # Keep the right end of the typed command visible
        avail_u = max(1, max_chars - len(p_str))
        u_str = u_str[-avail_u:]

    ty = by + (bh - 8) // 2
    gfx.draw_text(bx + 4, ty, p_str, SYS_GREEN, PROMPT_BACK, gfx.FONT_8X8, 1)
    gfx.draw_text(bx + 4 + len(p_str) * 8, ty, u_str, USER_WHITE, PROMPT_BACK, gfx.FONT_8X8, 1)


def gui_input(prompt_text=""):
    """Graphical replacement for blocking input() used by super, passwd, recover, etc."""
    global _in_modal_input, _modal_prompt_str
    if term_list is None or kb is None:
        return _real_input(prompt_text)

    if prompt_text:
        term_list.add_line(prompt_text, kind="sys", redraw=True)

    prev_ok = kb.ok_action
    submitted = [None]

    def _on_modal_ok(buf_text):
        submitted[0] = buf_text

    _in_modal_input = True
    _modal_prompt_str = ">> "
    kb.clear_buffer()
    kb.ok_action = _on_modal_ok
    draw_input_bar(True)

    last_buf = kb.buffer
    while submitted[0] is None:
        if screen.exit_btn and screen.exit_btn.handle_touch(gfx.get_touch()):
            break
        kb.check_touch()
        if kb.buffer != last_buf:
            last_buf = kb.buffer
            draw_input_bar(True)
        time.sleep_ms(20)

    res = submitted[0] if submitted[0] is not None else ""
    term_list.add_line(("cmd", _modal_prompt_str, res), kind="cmd", redraw=True)

    kb.clear_buffer()
    kb.ok_action = prev_ok
    _in_modal_input = False
    _modal_prompt_str = ""
    draw_input_bar(True)
    return res


_real_input = input
input = gui_input


# =================================================
# ZENCMD CORE & RECOVERY ENGINE
# =================================================
try:
    import urandom
except ImportError:
    import random as urandom

try:
    import network as _recovery_network
except ImportError:
    _recovery_network = None

try:
    import netservice as _netservice
except ImportError:
    _netservice = None

try:
    import urequests as _recovery_urequests
except ImportError:
    _recovery_urequests = None


class Recovery:
    """Lightweight, dependency-free recovery utility. Rebuilds every
    'core' package listed in pkgtable.json directly from GitHub."""

    PKGTABLE_URL = "https://raw.githubusercontent.com/FerrariForever95/Zeno-Micro-PC/main/pkgtable.json"
    PKGLIST_PATH = "/pkglist.json"
    ZENO_PATH    = "/zeno.py"

    ZENO_TEMPLATE = """import urandom,time

ui=None
tsk=None
log=None
net=None
usr=None
fm=None

boot_cap=urandom.getrandbits(32)

wallpaper=f""

boot_time=None

authorized=False

password="{password}"

user="{user}"

gitsecret="{gitsecret}"

ssid="{ssid}"

wifi_password="{wifi_password}"
"""

    def __init__(self):
        self._creds = {}
        self._pkgtable = None
        self._results = {"installed": [], "updated_flagged": [], "failed": []}
        self.zeno_regenerated = False

    def help(self):
        print("  recover                Rebuild core OS components from pkgtable.json")
        print("                         Works even if Services/PackageManager/Git/")
        print("                         Network/zeno.py are missing or corrupted.")

    def run(self):
        print("\n=== Zeno OS Recovery ===")
        print("Rebuilding core OS components from pkgtable.json.\n")

        zeno_existed = self._file_exists(self.ZENO_PATH)

        if zeno_existed:
            print("[recover] Found existing zeno.py -- using its stored Wi-Fi credentials.")
            ssid, password = self._read_zeno_wifi()
            if not ssid:
                print("[recover] Could not read usable Wi-Fi credentials from zeno.py.")
                ssid, password = self._prompt_wifi_only()
        else:
            print("[recover] zeno.py not found -- collecting setup details.")
            self._creds = self._prompt_all()
            ssid = self._creds.get("ssid")
            password = self._creds.get("wifi_password")

        if not self._connect_wifi(ssid, password):
            print("[recover] Could not establish Wi-Fi connection. Aborting recovery.")
            return False

        self._pkgtable = self._download_pkgtable()
        if self._pkgtable is None:
            print("[recover] Could not download/parse pkgtable.json. Aborting recovery.")
            return False

        core_pkgs = [n for n, e in self._pkgtable.items() if e.get("core", False)]
        if not core_pkgs:
            print("[recover] No packages flagged core:true were found in pkgtable.json.")
        else:
            print("[recover] Core packages to restore: {}".format(", ".join(core_pkgs)))

        installed = self._load_pkglist()
        seen = set()
        for name in core_pkgs:
            self._restore_package(name, installed, seen)

        self._save_pkglist(installed)

        print("\n[recover] Recovery summary:")
        print("  Restored/repaired  : {}".format(", ".join(self._results["installed"]) or "none"))
        print("  Flagged for update : {}".format(", ".join(self._results["updated_flagged"]) or "none"))
        print("  Failed             : {}".format(", ".join(self._results["failed"]) or "none"))

        if not zeno_existed:
            if self._results["failed"]:
                print("[recover] Skipping zeno.py generation -- one or more core packages failed.")
            else:
                self._write_zeno_py(self._creds)
                self.zeno_regenerated = True
                print("[recover] Generated new zeno.py.")
            self._creds = {}

        print("=== Recovery complete ===\n")
        return not self._results["failed"]

    def _prompt_all(self):
        creds = {}
        creds["user"] = input("Username: ").strip()
        creds["password"] = input("Password: ").strip()
        creds["ssid"] = input("Wi-Fi SSID: ").strip()
        creds["wifi_password"] = input("Wi-Fi Password: ").strip()
        creds["gitsecret"] = input("Git Token (Enter to skip): ").strip()
        return creds

    def _prompt_wifi_only(self):
        ssid = input("Wi-Fi SSID: ").strip()
        password = input("Wi-Fi Password: ").strip()
        return ssid, password

    def _file_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def _read_zeno_wifi(self):
        try:
            with open(self.ZENO_PATH) as f:
                text = f.read()
        except OSError:
            return None, None

        ssid = self._extract_assignment(text, "ssid")
        password = self._extract_assignment(text, "wifi_password")
        return ssid, password

    def _extract_assignment(self, text, var_name):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(var_name + "=") or line.startswith(var_name + " ="):
                _, _, rhs = line.partition("=")
                rhs = rhs.strip()
                if len(rhs) >= 2 and rhs[0] == rhs[-1] and rhs[0] in ("'", '"'):
                    return rhs[1:-1]
                return rhs or None
        return None

    def _write_zeno_py(self, creds):
        content = self.ZENO_TEMPLATE.format(
            password=creds.get("password", ""),
            user=creds.get("user", ""),
            gitsecret=creds.get("gitsecret", ""),
            ssid=creds.get("ssid", ""),
            wifi_password=creds.get("wifi_password", ""),
        )
        with open(self.ZENO_PATH, "w") as f:
            f.write(content)

    def _connect_wifi(self, ssid, password):
        if not ssid:
            print("[recover] No SSID available to connect with.")
            return False
        if _recovery_network is None:
            print("[recover] 'network' module unavailable -- assuming connection.")
            return True
        try:
            wlan = _recovery_network.WLAN(_recovery_network.STA_IF)
            wlan.active(True)
            if wlan.isconnected():
                return True
            wlan.connect(ssid, password)
            for _ in range(20):
                if wlan.isconnected():
                    print("[recover] Wi-Fi connected.")
                    return True
                time.sleep(0.5)
            print("[recover] Wi-Fi connection timed out.")
            return False
        except Exception as e:
            print("[recover] Wi-Fi error: {}".format(e))
            return False

    def _download_pkgtable(self):
        raw = self._http_get_text(self.PKGTABLE_URL)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError as e:
            print("[recover] pkgtable.json is not valid JSON: {}".format(e))
            return None

    def _http_get_text(self, url):
        if _recovery_urequests is None:
            print("[recover] 'urequests' module unavailable -- cannot download {}".format(url))
            return None
        try:
            resp = _recovery_urequests.get(url)
            try:
                if resp.status_code != 200:
                    print("[recover] HTTP {} fetching {}".format(resp.status_code, url))
                    return None
                return resp.text
            finally:
                resp.close()
        except Exception as e:
            print("[recover] Download failed for {}: {}".format(url, e))
            return None

    def _raw_url(self, author, repo, branch, file_path):
        return "https://raw.githubusercontent.com/{}/{}/{}/{}".format(
            author, repo, branch or "main", file_path)

    def _mkdirs(self, path):
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                os.mkdir(cur)
            except OSError:
                pass

    def _full_path(self, install_path, filename):
        return "{}/{}".format((install_path or "/").rstrip("/"), filename)

    def _write_and_verify(self, path, data):
        directory = path.rsplit("/", 1)[0] or "/"
        self._mkdirs(directory)
        try:
            with open(path, "w") as f:
                f.write(data)
        except OSError as e:
            print("[recover] Could not write '{}': {}".format(path, e))
            return False

        try:
            written_size = os.stat(path)[6]
        except OSError:
            print("[recover] Verification failed: '{}' missing after write.".format(path))
            return False

        if written_size != len(data):
            print("[recover] Verification failed: size mismatch writing '{}'.".format(path))
            return False
        return True

    def _file_is_healthy(self, path):
        try:
            return os.stat(path)[6] > 0
        except OSError:
            return False

    def _load_pkglist(self):
        try:
            with open(self.PKGLIST_PATH) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_pkglist(self, data):
        try:
            with open(self.PKGLIST_PATH, "w") as f:
                json.dump(data, f)
        except OSError as e:
            print("[recover] Could not write pkglist.json: {}".format(e))

    def _restore_package(self, name, installed, seen, chain=None):
        if name in seen:
            return name not in self._results["failed"]
        seen.add(name)
        chain = chain or []
        if name in chain:
            print("[recover] Dependency cycle detected involving '{}' -- skipping.".format(name))
            self._results["failed"].append(name)
            return False
        chain = chain + [name]

        entry = self._pkgtable.get(name)
        if entry is None:
            print("[recover] '{}' not found in pkgtable.json -- skipping.".format(name))
            self._results["failed"].append(name)
            return False

        try:
            for dep in entry.get("dependencies", []):
                dep_name = dep.get("name") if isinstance(dep, dict) else dep
                if not dep_name:
                    continue
                if not self._restore_package(dep_name, installed, seen, chain):
                    print("[recover] Dependency '{}' failed -- cannot restore '{}'.".format(dep_name, name))
                    self._results["failed"].append(name)
                    return False

            filename = entry.get("file", "").split("/")[-1]
            target_path = self._full_path(entry.get("install_path"), filename)
            record = installed.get(name)

            if record:
                existing_path = self._full_path(record.get("install_path"), record.get("filename"))
                healthy = self._file_is_healthy(existing_path)
                same_version = record.get("version") == entry.get("version")

                if healthy and same_version:
                    return True

                if healthy and not same_version:
                    record["update needed"] = True
                    self._results["updated_flagged"].append(name)
                    return True

            data = self._http_get_text(self._raw_url(
                entry.get("author"), entry.get("repository"), entry.get("branch"), entry.get("file")))
            if data is None:
                self._results["failed"].append(name)
                return False

            if not self._write_and_verify(target_path, data):
                self._results["failed"].append(name)
                return False

            installed[name] = {
                "name": entry.get("name", name),
                "version": entry.get("version"),
                "author": entry.get("author"),
                "repository": entry.get("repository"),
                "branch": entry.get("branch") or "main",
                "install_path": entry.get("install_path"),
                "filename": filename,
                "dependencies": entry.get("dependencies", []),
                "core": bool(entry.get("core", False)),
            }
            self._results["installed"].append(name)
            print("[recover] Restored '{}' v{}.".format(name, entry.get("version")))
            return True

        except Exception as e:
            print("[recover] Unexpected error restoring '{}': {}".format(name, e))
            self._results["failed"].append(name)
            return False


# =================================================
# SERVICE LOADING
# =================================================
class _NullLogger:
    def debug(self, msg, source=None):
        pass

    def warning(self, msg, source=None):
        print("[WARN] {}".format(msg))

    def error(self, msg, source=None):
        print("[ERROR] {}".format(msg))


class _FallbackZeno:
    user = "recovery"
    hostname = "zeno-device"


def _fresh_import(name):
    for mod_name in [k for k in list(sys.modules.keys()) if k == name or k.startswith(name + ".")]:
        try:
            del sys.modules[mod_name]
        except Exception:
            pass
    return __import__(name)


def _inject_print(prefix):
    for k in list(sys.modules.keys()):
        if k == prefix or k.startswith(prefix + "."):
            try:
                setattr(sys.modules[k], "print", print)
            except Exception:
                pass


def _load_services():
    global Services, Network, Disk, downloadhelper, system, Logger, Git, WifiManager
    global BluetoothManager, BootConfig, usermanager, FileManager, PackageManager
    global zeno, logger, _um, _fm, _pm, MODULES

    try:
        Services = _fresh_import("Services")
        _inject_print("Services")
    except Exception as e:
        Services = None
        print("[ZenCMD] Warning: /Services unavailable ({}).".format(e))

    def _svc(name):
        return getattr(Services, name, None) if Services is not None else None

    Disk             = _svc("Disk")
    downloadhelper   = _svc("downloadhelper")
    system           = _svc("system")
    Logger           = _svc("Logger")
    Git              = _svc("Git")
    BluetoothManager = _svc("BluetoothManager")
    BootConfig       = _svc("BootConfig")
    usermanager      = _svc("usermanager")
    FileManager      = _svc("FileManager")
    PackageManager   = _svc("PackageManager")
    Scheduler        = _svc("Scheduler")
    IoTManager       = _svc("IoTManager")
    _WifiManagerCls  = _svc("WifiManager")

    logger = Logger() if Logger else _NullLogger()
    _um = usermanager() if usermanager else None
    _fm = FileManager() if FileManager else None
    _pm = PackageManager() if PackageManager else None

    Network = (lambda cls=_WifiManagerCls: cls(logger)) if _WifiManagerCls is not None else None

    all_modules = {
        "net":          Network,
        "disk":         Disk,
        "downserv":     downloadhelper,
        "system":       system,
        "log":          Logger,
        "git":          Git,
        "bootmgr":      BootConfig,
        "bluetoothmgr": BluetoothManager,
        "pkg":          PackageManager,
        "ps":           Scheduler,
        "iot":          IoTManager
    }
    MODULES = {k: v for k, v in all_modules.items() if v is not None}

    return Services is not None


ZENCMD_VERSION = "3.9"
ZENOS_NAME     = "Zeno OS"

PRIVILEGED_PREFIXES = (
    "bootmgr", "mount", "umount", "unmount", "format", "mkfs",
    "shutdown", "reboot", "factory", "removeuser", "elevate", "delevate",
    "service", "reload", "reloadmodule", "kernel", "driver", "pkg",
    "install", "remove", "uninstall", "reinstall", "update", "mountzfs", "bootlog",
)

current_path    = "/Home"
active_module   = None
module_instance = None

history_log     = []
aliases         = {}
env_vars        = {}
jobs            = []
_PIPE_IN        = None


def _is_super():
    if _um is None:
        return False
    try:
        return _um.isrooted(zeno.user)
    except Exception:
        return False


def _require_super(cmd_word):
    for prefix in PRIVILEGED_PREFIXES:
        if cmd_word == prefix or cmd_word.startswith(prefix + " "):
            if not _is_super():
                print("Access denied. Command requires Super Mode. Use 'super' first.")
                return True
    return False


def _prompt():
    d = current_path.rstrip("/") or ""
    if d.startswith("/"):
        d = d[1:]

    uname = getattr(zeno, "user", "user")
    if _is_super():
        path_part = f"root/{d}" if d else "root/"
        return f"{path_part}#:$"
    else:
        path_part = f"{uname}/{d}:$" if d else f"{uname}/:$"
        if active_module:
            return f"{path_part}[{active_module}]>"
        return f"{path_part}>"


def _normalize_path(path):
    parts = []
    for p in path.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if parts:
                parts.pop()
            continue
        parts.append(p)
    return "/" + "/".join(parts) if parts else "/"


def _abs(path):
    if path.startswith("/"):
        combined = path
    else:
        base = current_path if current_path.endswith("/") else current_path + "/"
        combined = base + path
    return _normalize_path(combined)


def _pjoin(parent, name):
    parent = parent.rstrip("/") or "/"
    return name if parent == "/" else parent + "/" + name


def human_size(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def _is_dir(path):
    try:
        return _fm.metadata(path).get("type") == "directory"
    except Exception:
        try:
            return (os.stat(path)[0] & 0x4000) != 0
        except Exception:
            return False


def _fs_file(path):
    if _fm is not None:
        if not _fm.exists(path):
            return False
        try:
            return _fm.metadata(path).get("type") != "directory"
        except Exception:
            return False
    try:
        return (os.stat(path)[0] & 0x4000) == 0
    except Exception:
        return False


def resolve_program(name, cwd):
    targets = [name] if name.endswith(".py") else [name, name + ".py"]
    if name.startswith("/"):
        for t in targets:
            if _fs_file(t):
                return t
        return None
    for t in targets:
        p = _pjoin(cwd, t)
        if _fs_file(p):
            return p
    print("[SYSRUN] Searching filesystem...")
    stack = ["/"]
    seen  = set()
    while stack:
        base = stack.pop()
        if base in seen:
            continue
        seen.add(base)
        try:
            entries = _fm.listdir(base, show_hidden=True) if _fm else os.listdir(base)
        except Exception:
            continue
        for e in entries:
            full = _pjoin(base, e)
            if _is_dir(full):
                stack.append(full)
            elif e in targets:
                return full
    return None


def run_python_file(path):
    if _fm is not None:
        fd = _fm.open(path, "r")
        try:
            code = _fm.read(fd)
        finally:
            _fm.close(fd)
    else:
        with open(path, "r") as f:
            code = f.read()
    exec(code, {"__name__": "__main__", "__file__": path, "print": print, "input": input})


def list_dir(path, long=False):
    try:
        entries = _fm.listdir(path, show_hidden=True) if _fm else os.listdir(path)
    except Exception as e:
        print("[ls] Cannot list:", e)
        return

    print(f"\n{path}")
    print("-" * 36)
    for f in entries:
        full = _pjoin(path, f)
        if path == "/" and (f.endswith(".py") or f.endswith(".mpy")) and not _is_super():
            continue
        if f in ["pkglist.json", "pkgtable.json"]:
            continue
        try:
            meta = _fm.metadata(full) if _fm else {"type": "directory" if _is_dir(full) else "file", "size": os.stat(full)[6], "owner": "root"}
        except Exception:
            meta = {"type": "unknown", "size": 0, "owner": "?"}
        is_d = meta.get("type") == "directory"
        if long:
            sz    = human_size(meta.get("size", 0))
            kind  = "<DIR> " if is_d else "      "
            owner = meta.get("owner", "?")
            print(f" {kind}{f:18} {sz:>9} {owner}")
        else:
            tag = "/" if is_d else ""
            print(f"  {f}{tag}")


def tree_dir(path, prefix=""):
    try:
        entries = _fm.listdir(path, show_hidden=True) if _fm else os.listdir(path)
    except Exception:
        print(prefix + "[unreadable]")
        return
    for e in entries:
        full = _pjoin(path, e)
        if path == "/" and e.endswith(".py") and not _is_super():
            continue
        if _is_dir(full):
            print(prefix + "|-- " + e + "/")
            tree_dir(full, prefix + "|   ")
        else:
            print(prefix + "|-- " + e)


def convert_arg(arg):
    try:
        return float(arg) if "." in arg else int(arg)
    except Exception:
        return arg


def _is_intlike(s):
    try:
        int(s)
        return True
    except Exception:
        return False


def _split(cmd):
    parts  = []
    buf    = []
    in_q   = False
    q_char = None
    for ch in cmd:
        if in_q:
            if ch == q_char:
                in_q = False
            else:
                buf.append(ch)
        elif ch in ('"', "'"):
            in_q   = True
            q_char = ch
        elif ch == " ":
            if buf:
                parts.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _split_top(raw, delims):
    segments = []
    buf    = []
    in_q   = False
    q_char = None
    for ch in raw:
        if in_q:
            buf.append(ch)
            if ch == q_char:
                in_q = False
        elif ch in ('"', "'"):
            in_q   = True
            q_char = ch
            buf.append(ch)
        elif ch in delims:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf))
    return segments


class _OutputCapture:
    def __init__(self):
        self.lines = []
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line)

    def flush(self):
        pass


def _read_lines(args, idx=0):
    if len(args) > idx:
        p = _abs(args[idx])
        try:
            if _fm is not None:
                fd = _fm.open(p, "r")
                try:
                    data = _fm.read(fd)
                finally:
                    _fm.close(fd)
            else:
                with open(p, "r") as f:
                    data = f.read()
            return data.splitlines()
        except Exception as e:
            print("[read]", e)
            return False
    if _PIPE_IN is not None:
        return _PIPE_IN
    return None


def _need_lines(args, idx, usage):
    lines = _read_lines(args, idx)
    if lines is False:
        return None
    if lines is None:
        print(usage)
        return None
    return lines


BUILTIN_HELP = {
    "pwd":         "pwd                    Print working directory",
    "cd":          "cd <path>              Change directory (supports .. and .)",
    "ls":          "ls [-l]                List directory contents",
    "dir":         "dir                    Alias for ls",
    "tree":        "tree [path]            Show directory tree",
    "mkdir":       "mkdir <dir>            Create directory",
    "rmdir":       "rmdir <dir>            Remove empty directory",
    "rm":          "rm <path>              Remove file",
    "cp":          "cp <src> <dst>         Copy file",
    "mv":          "mv <src> <dst>         Move/rename file",
    "touch":       "touch <file>           Create empty file",
    "cat":         "cat <file>             Print file contents (or piped input)",
    "head":        "head <file> [n]        Print first n lines",
    "tail":        "tail <file> [n]        Print last n lines",
    "search":      "search [-ivnc] <pat> [file]   Grep-like search",
    "grep":        "grep [-ivnc] <pat> [file]     Same as search",
    "wc":          "wc [-lwc] [file]       Count lines/words/chars",
    "sort":        "sort [-rn] [file]      Sort lines",
    "uniq":        "uniq [-c] [file]       Collapse repeated adjacent lines",
    "echo":        "echo <text>            Print text",
    "clear":       "clear                  Clear terminal (cls alias)",
    "cls":         "cls                    Alias for clear",
    "history":     "history                Show command history",
    "which":       "which <cmd>            Find command location",
    "whereis":     "whereis <name>         Locate binary on filesystem",
    "find":        "find <path> <name>     Search for files",
    "stat":        "stat <path>            File status information",
    "file":        "file <path>            Describe file type",
    "whoami":      "whoami                 Show current user",
    "id":          "id                     Show user identity",
    "hostname":    "hostname               Show device hostname",
    "date":        "date                   Show current date",
    "time":        "time                   Show current time",
    "uptime":      "uptime                 Show system uptime",
    "version":     "version                Show ZenCMD and OS version",
    "df":          "df                     Disk free (filesystem usage)",
    "du":          "du <path>              Disk usage of path",
    "free":        "free                   Show free memory",
    "memdebug":    "memdebug               Detailed memory debug",
    "ps":          "ps                     Show running processes",
    "kill":        "kill <pid>             Kill process by ID",
    "jobs":        "jobs                   List background jobs",
    "env":         "env                    Show environment variables",
    "export":      "export KEY=VALUE       Set environment variable",
    "alias":       "alias [name=cmd]       Define or list aliases",
    "unalias":     "unalias <name>         Remove alias",
    "mount":       "mount [src dst]        Mount filesystem (SUPER)",
    "mountzfs":    "mountzfs               Mount ZFS volume (SUPER)",
    "sync":        "sync                   Sync filesystem buffers",
    "bootlog":     "bootlog                Show boot log (SUPER)",
    "log":         "log                    Show ZenCMD log",
    "service":     "service <name> <op>    Manage services (SUPER)",
    "services":    "services               List all services",
    "reload":      "reload                 Reload ZenCMD config (SUPER)",
    "reloadmodule":"reloadmodule <mod>     Reload a module (SUPER)",
    "shutdown":    "shutdown               Power off device (SUPER)",
    "reboot":      "reboot                 Reboot device (SUPER)",
    "factory":     "factory                Factory reset (SUPER)",
    "super":       "super                  Elevate to Super Mode",
    "unsuper":     "unsuper                Exit Super Mode",
    "passwd":      "passwd                 Change your account password",
    "chusername":  "chusername             Change your account username",
    "userdebug":   "userdebug              Show current user debug",
    "whoisroot":   "whoisroot              Check if current user is rooted",
    "userlist":    "userlist               List all users",
    "useradd":     "useradd <user> <pw> [--root]   Create a user (SUPER)",
    "userdel":     "userdel <user>         Delete a user (SUPER)",
    "userauth":    "userauth <user> <pw>   Check username/password",
    "userhome":    "userhome <user>        Print user home directory",
    "userswitch":  "userswitch <user>      Switch user context (SUPER)",
    "debug":       "debug [on|off]         Show/set debug mode",
    "perfmode":    "perfmode [mode]        Show/set performance mode",
    "graphics":    "graphics [on|off]      Show/set graphics subsystem",
    "wifi":        "wifi [on|off]          Show/set Wi-Fi state",
    "ble":         "ble [on|off]           Show/set BLE state",
    "bootverbose": "bootverbose [on|off]   Show/set verbose boot",
    "powersave":   "powersave [on|off]     Show/set power saving",
    "modules":     "modules                List available modules",
    "enter":       "enter <module>         Enter module context",
    "leave":       "leave                  Leave current module",
    "sysrun":      "sysrun <file>          Run a .py file",
    "pkgrun":      "pkgrun <pkg> [args]    Run an installed package",
    "recover":     "recover                Rebuild core OS from pkgtable.json",
    "help":        "help [cmd|module]      Show help",
    "exit":        "exit / quit            Exit module or ZenCMD",
    "quit":        "quit                   Alias for exit",
}


def _shell_help():
    print(f"ZenCMD {ZENCMD_VERSION} -- {ZENOS_NAME}")
    print("Commands: pwd, cd, ls, tree, mkdir, rmdir, rm, cp, mv, touch")
    print("Files: cat, head, tail, grep, wc, sort, uniq, echo, stat, find")
    print("Sys: whoami, id, hostname, date, time, uptime, version, df, free")
    print("Env: env, export, alias, unalias, history, clear")
    print("Users: super, unsuper, passwd, userlist, useradd, userdel")
    print("Zeno: modules, enter, leave, sysrun, pkgrun, recover, exit")
    print("Type 'help <cmd>' for details.")


_HELP_NAMES = ("help", "Help", "HELP", "show_help", "usage", "_help", "help_text")


def _show_help(obj, extra=()):
    for n in _HELP_NAMES:
        try:
            h = getattr(obj, n)
        except Exception:
            continue
        try:
            if callable(h):
                if extra:
                    try:
                        r = h(*extra)
                    except TypeError:
                        r = h()
                else:
                    r = h()
            else:
                r = h
        except Exception as e:
            print("[help] '{}' raised: {}".format(n, e))
            return False
        if r is not None:
            if isinstance(r, (list, tuple)):
                for item in r:
                    print(item)
            else:
                print(r)
        return True
    return False


def _list_methods(obj):
    names = []
    try:
        for n in dir(obj):
            if n.startswith("_"):
                continue
            try:
                a = getattr(obj, n)
            except Exception:
                continue
            if callable(a):
                names.append(n)
    except Exception:
        pass
    return names


def _instantiate(name):
    if active_module == name and module_instance is not None:
        return module_instance
    try:
        return MODULES[name]()
    except Exception as e:
        print("[help] Could not load module '{}': {}".format(name, e))
        return None


def _module_help(name, extra=()):
    inst = _instantiate(name)
    if inst is None:
        return
    if _show_help(inst, extra):
        return
    methods = _list_methods(inst)
    print("Module '{}' commands: {}".format(name, ", ".join(methods)))


def _cmd_help(args):
    if not args:
        if active_module and module_instance is not None:
            if _show_help(module_instance):
                return
            methods = _list_methods(module_instance)
            print("Available commands: " + ", ".join(methods))
            return
        _shell_help()
        return

    topic = args[0].lower()
    if topic in MODULES:
        _module_help(topic, tuple(args[1:]))
        return
    if topic in BUILTIN_HELP:
        print(BUILTIN_HELP[topic])
        return
    if active_module and module_instance is not None:
        if _show_help(module_instance, tuple(args)):
            return
        if topic in _list_methods(module_instance):
            print("'{}' is a command of module '{}'.".format(topic, active_module))
            return
    if _pm is not None:
        try:
            if _pm.check(topic):
                _pm.run(topic, "help")
                return
        except Exception as e:
            print("[help] Package help failed: {}".format(e))
            return
    print(f"No help for '{topic}'.")


def _cmd_super():
    if _is_super():
        print("Already in Super Mode.")
        return
    if _um is None:
        print("Super Mode unavailable (usermanager missing).")
        return
    pwd = input(f"Password for {zeno.user}: ")
    _um.elevate(zeno.user, pwd)
    if _is_super():
        logger.debug(f"Super Mode entered by {zeno.user}", source="ZenCMD")
        print("Entering Super Mode...")
    else:
        logger.warning(f"Failed Super Mode attempt by {zeno.user}", source="ZenCMD")
        print("Authentication failed.")


def _cmd_unsuper():
    if not _is_super():
        print("Not in Super Mode.")
        return
    pwd = input(f"Confirm password for {zeno.user}: ")
    _um.delevate(zeno.user, pwd)
    if not _is_super():
        logger.debug(f"Super Mode exited by {zeno.user}", source="ZenCMD")
        print("Exited Super Mode.")
    else:
        print("Authentication failed.")


def _cmd_passwd(args):
    if _um is None:
        print("usermanager service missing.")
        return
    old_pwd = input(f"Current password for {zeno.user}: ")
    new_pwd = input("New password: ")
    confirm_pwd = input("Confirm new password: ")
    if new_pwd != confirm_pwd:
        print("Passwords do not match.")
        return
    if not new_pwd:
        print("Password cannot be empty.")
        return
    if _um.change_password(zeno.user, old_pwd, new_pwd):
        print("Password updated.")


def _cmd_chusername(args):
    if _um is None:
        print("usermanager service missing.")
        return
    pwd = input(f"Current password for {zeno.user}: ")
    new_user = input("New username: ").strip()
    if not new_user:
        print("Username cannot be empty.")
        return
    if _um.change_username(zeno.user, pwd, new_user):
        print("Username change requested.")


def _cmd_ls(args):
    long  = "-l" in args
    paths = [a for a in args if not a.startswith("-")]
    path  = _abs(paths[0]) if paths else current_path
    list_dir(path, long=long)


def _cmd_cd(args):
    global current_path
    if not args:
        current_path = "/"
        return
    target = _abs(args[0])
    exists = _fm.exists(target) if _fm else True
    if not exists or not _is_dir(target):
        print(f"[cd] Not a directory: {target}")
        return
    current_path = target


def _cmd_mkdir(args):
    if not args:
        print("Usage: mkdir <dir>")
        return
    for d in args:
        try:
            if _fm:
                _fm.mkdir(_abs(d))
            else:
                os.mkdir(_abs(d))
            print("Created:", d)
        except Exception as e:
            print("[mkdir]", e)


def _cmd_rmdir(args):
    if not args:
        print("Usage: rmdir <dir>")
        return
    p = _abs(args[0])
    try:
        if not _is_dir(p):
            print(f"[rmdir] Not a directory: {p}")
            return
        entries = _fm.listdir(p, show_hidden=True) if _fm else os.listdir(p)
        if entries:
            print(f"[rmdir] Directory not empty: {p}")
            return
        if _fm:
            _fm.delete(p)
        else:
            os.rmdir(p)
        print("Removed:", p)
    except Exception as e:
        print("[rmdir]", e)


def _cmd_rm(args):
    if not args:
        print("Usage: rm <file>")
        return
    p = _abs(args[0])
    try:
        if _is_dir(p):
            print(f"[rm] Is a directory (use rmdir): {p}")
            return
        if _fm:
            _fm.delete(p)
        else:
            os.remove(p)
        print("Removed:", p)
    except Exception as e:
        print("[rm]", e)


def _cmd_cp(args):
    if len(args) < 2:
        print("Usage: cp <src> <dst>")
        return
    src, dst = _abs(args[0]), _abs(args[1])
    try:
        _fm.copy(src, dst)
        print(f"Copied {src} -> {dst}")
    except Exception as e:
        print("[cp]", e)


def _cmd_mv(args):
    if len(args) < 2:
        print("Usage: mv <src> <dst>")
        return
    src, dst = _abs(args[0]), _abs(args[1])
    try:
        _fm.move(src, dst)
        print(f"Moved {src} -> {dst}")
    except Exception as e:
        print("[mv]", e)


def _cmd_touch(args):
    if not args:
        print("Usage: touch <file>")
        return
    p = _abs(args[0])
    try:
        if _fm.exists(p):
            fd = _fm.open(p, "a")
            _fm.close(fd)
        else:
            _fm.create(p, "")
        print("Touched:", p)
    except Exception as e:
        print("[touch]", e)


def _cmd_cat(args):
    lines = _need_lines(args, 0, "Usage: cat <file>")
    if lines is None:
        return
    print("\n".join(lines))


def _head_tail_args(args):
    n = 10
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args) and _is_intlike(args[i + 1]):
            n = int(args[i + 1])
            i += 2
            continue
        if a.startswith("-") and len(a) > 1 and _is_intlike(a[1:]):
            n = int(a[1:])
        elif _is_intlike(a) and (files or _PIPE_IN is not None):
            n = int(a)
        else:
            files.append(a)
        i += 1
    return n, files


def _cmd_head(args):
    n, files = _head_tail_args(args)
    lines = _need_lines(files, 0, "Usage: head <file> [n]")
    if lines is None:
        return
    for line in lines[:max(n, 0)]:
        print(line)


def _cmd_tail(args):
    n, files = _head_tail_args(args)
    lines = _need_lines(files, 0, "Usage: tail <file> [n]")
    if lines is None or n <= 0:
        return
    for line in lines[max(len(lines) - n, 0):]:
        print(line)


def _cmd_search(args):
    usage = "Usage: grep [-ivnc] <pattern> [file]"
    ic = inv = num = cnt = False
    rest = []
    for a in args:
        if a.startswith("-") and len(a) > 1 and not _is_intlike(a):
            for ch in a[1:]:
                if ch == "i": ic = True
                elif ch == "v": inv = True
                elif ch == "n": num = True
                elif ch == "c": cnt = True
                else:
                    print("[grep] unknown option: -" + ch)
                    return
        else:
            rest.append(a)
    if not rest:
        print(usage)
        return
    pattern = rest[0].lower() if ic else rest[0]
    lines = _need_lines(rest, 1, usage)
    if lines is None:
        return
    matched = 0
    for i, line in enumerate(lines, 1):
        hay = line.lower() if ic else line
        hit = (pattern in hay)
        if inv:
            hit = not hit
        if not hit:
            continue
        matched += 1
        if cnt:
            continue
        print(f"{i}:{line}" if num else line)
    if cnt:
        print(matched)
    elif matched == 0:
        print("[search] no matches")


def _cmd_wc(args):
    flags = [a for a in args if a.startswith("-") and len(a) > 1]
    rest = [a for a in args if not (a.startswith("-") and len(a) > 1)]
    want = "".join(f[1:] for f in flags) or "lwc"
    lines = _need_lines(rest, 0, "Usage: wc [-lwc] [file]")
    if lines is None:
        return
    nl = len(lines)
    nw = sum(len(l.split()) for l in lines)
    nc = sum(len(l) + 1 for l in lines)
    out = []
    if "l" in want: out.append(str(nl))
    if "w" in want: out.append(str(nw))
    if "c" in want: out.append(str(nc))
    print(" ".join(out))


def _cmd_sort(args):
    rev = "-r" in args or "-rn" in args or "-nr" in args
    numeric = "-n" in args or "-rn" in args or "-nr" in args
    rest = [a for a in args if not (a.startswith("-") and len(a) > 1)]
    lines = _need_lines(rest, 0, "Usage: sort [-rn] [file]")
    if lines is None:
        return
    def _key(l):
        if not numeric:
            return l
        tok = l.strip().split(" ")[0] if l.strip() else ""
        try:
            return float(tok)
        except Exception:
            return 0.0
    for line in sorted(lines, key=_key, reverse=rev):
        print(line)


def _cmd_uniq(args):
    count = "-c" in args
    rest = [a for a in args if not (a.startswith("-") and len(a) > 1)]
    lines = _need_lines(rest, 0, "Usage: uniq [-c] [file]")
    if lines is None:
        return
    prev = None
    n = 0
    for line in lines:
        if prev is not None and line == prev:
            n += 1
            continue
        if prev is not None:
            print(f"{n:4} {prev}" if count else prev)
        prev = line
        n = 1
    if prev is not None:
        print(f"{n:4} {prev}" if count else prev)


def _cmd_stat(args):
    if not args:
        print("Usage: stat <path>")
        return
    p = _abs(args[0])
    try:
        meta = _fm.metadata(p)
    except Exception:
        print("[stat] Not found:", p)
        return
    print(f" Path  : {p}")
    print(f" Type  : {meta.get('type')}")
    print(f" Size  : {human_size(meta.get('size', 0))}")
    print(f" Owner : {meta.get('owner')}")


def _cmd_file(args):
    if not args:
        print("Usage: file <path>")
        return
    p = _abs(args[0])
    try:
        meta = _fm.metadata(p)
        print(p + ": " + meta.get("type", "unknown"))
    except Exception:
        print(p + ": no such file")


def _cmd_find(args):
    if len(args) < 2:
        print("Usage: find <path> <name>")
        return
    base, query = _abs(args[0]), args[1]
    stack = [base]
    found = 0
    while stack:
        d = stack.pop()
        try:
            entries = _fm.listdir(d, show_hidden=True) if _fm else os.listdir(d)
        except Exception:
            continue
        for e in entries:
            full = _pjoin(d, e)
            if query in e:
                print(full)
                found += 1
            if _is_dir(full):
                stack.append(full)
    if found == 0:
        print("No matches.")


def _cmd_which(args):
    if not args:
        print("Usage: which <cmd>")
        return
    name = args[0]
    if name in BUILTIN_HELP:
        print(f"{name}: ZenCMD built-in")
    elif name in MODULES:
        print(f"{name}: ZenCMD module")
    else:
        p = resolve_program(name, current_path)
        print(p if p else f"{name}: not found")


def _cmd_whereis(args):
    _cmd_which(args)


def _cmd_echo(args):
    out = [str(env_vars.get(a[1:], "")) if a.startswith("$") else a for a in args]
    print(" ".join(out))


def _cmd_env(args):
    for k, v in env_vars.items():
        print(f" {k}={v}")


def _cmd_export(args):
    if not args:
        _cmd_env([])
        return
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            env_vars[k.strip()] = v.strip()
            print(f"Set {k}={v}")
        else:
            print(f"export: invalid: {a}")


def _cmd_alias(args):
    if not args:
        for k, v in aliases.items():
            print(f" alias {k}='{v}'")
        return
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            aliases[k.strip()] = v.strip()
        elif a in aliases:
            print(f" alias {a}='{aliases[a]}'")
        else:
            print(f"alias: {a}: not found")


def _cmd_unalias(args):
    if not args:
        print("Usage: unalias <name>")
        return
    for a in args:
        if a in aliases:
            del aliases[a]
            print(f"Removed alias: {a}")
        else:
            print(f"unalias: {a}: not found")


def _cmd_history(args):
    for i, h in enumerate(history_log, 1):
        print(f" {i:3}  {h}")


def _cmd_userdebug(args):
    if _um is None:
        print("usermanager unavailable.")
        return
    debug = _um.userdebug() if hasattr(_um, "userdebug") else _um.userinfo()
    print(f" User   : {debug.get('user', '?')}")
    print(f" Rooted : {debug.get('root', False)}")


def _cmd_whoisroot(args):
    if _um is None:
        print("usermanager unavailable.")
        return
    print(f"{zeno.user} is {'rooted' if _um.isrooted(zeno.user) else 'not rooted'}.")


def _cmd_whoami_str():
    return "root" if _is_super() else getattr(zeno, "user", "user")


def _cmd_id(args):
    uid = 0 if _is_super() else 1000
    print(f"uid={uid}({_cmd_whoami_str()}) gid={uid}")


def _cmd_hostname(args):
    try:
        import network
        print(network.WLAN().config("hostname"))
    except Exception:
        print(getattr(zeno, "hostname", "zeno-device"))


def _cmd_version(args):
    print(f"{ZENOS_NAME} -- ZenCMD {ZENCMD_VERSION}")
    print(f"MicroPython {sys.version}")


def _cmd_uptime(args):
    t = time.ticks_ms() // 1000
    print(f"Uptime: {t // 3600}h {(t % 3600) // 60}m {t % 60}s")


def _cmd_date(args):
    try:
        import utime
        t = utime.localtime()
        print(f"{t[0]}-{t[1]:02d}-{t[2]:02d}")
    except Exception as e:
        print("[date]", e)


def _cmd_time_cmd(args):
    try:
        import utime
        t = utime.localtime()
        print(f"{t[3]:02d}:{t[4]:02d}:{t[5]:02d}")
    except Exception as e:
        print("[time]", e)


def _cmd_df(args):
    try:
        import uos
        st = uos.statvfs("/")
        total = st[2] * st[0]
        free  = st[3] * st[0]
        print(f" Total: {human_size(total)} | Used: {human_size(total - free)} | Free: {human_size(free)}")
    except Exception as e:
        print("[df]", e)


def _cmd_du(args):
    path = _abs(args[0]) if args else current_path
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            entries = _fm.listdir(d, show_hidden=True) if _fm else os.listdir(d)
        except Exception:
            continue
        for e in entries:
            full = _pjoin(d, e)
            if _is_dir(full):
                stack.append(full)
            else:
                try:
                    total += _fm.metadata(full).get("size", 0)
                except Exception:
                    pass
    print(f" {human_size(total)}  {path}")


def _cmd_free(args):
    import gc
    gc.collect()
    free  = gc.mem_free()
    alloc = gc.mem_alloc()
    print(f" Total: {human_size(free + alloc)} | Used: {human_size(alloc)} | Free: {human_size(free)}")


def _cmd_memdebug(args):
    _cmd_free(args)


def _cmd_ps(args):
    print(" PID  NAME")
    print("   1  zencmd (GUI shell)")
    for i, j in enumerate(jobs, 2):
        print(f" {i:3}  {j}")


def _cmd_kill(args):
    if not args:
        print("Usage: kill <pid>")
        return
    print(f"[kill] Signal sent to PID {args[0]}")


def _cmd_jobs(args):
    if not jobs:
        print("No background jobs.")
    for i, j in enumerate(jobs, 1):
        print(f" [{i}] {j}")


def _cmd_sync(args):
    try:
        import uos
        uos.sync()
        print("Synced.")
    except Exception as e:
        print("[sync]", e)


def _cmd_mount(args):
    if len(args) < 2:
        print("Usage: mount <src> <dst>")
        return
    print(f"[mount] Mounting {args[0]} -> {args[1]}")


def _cmd_mountzfs(args):
    try:
        import zfs
        zfs.mount()
        print("ZFS mounted.")
    except Exception as e:
        print("[mountzfs]", e)


def _cmd_bootlog(args):
    _cmd_cat(["/bootlog.txt"])


def _cmd_log(args):
    _cmd_cat(["/log.txt"])


def _cmd_services(args):
    print("Available services:", ", ".join(MODULES.keys()))


def _cmd_service(args):
    if len(args) < 2:
        print("Usage: service <name> <op>")
        return
    print(f"[service] {args[0]} {args[1]}")


def _cmd_reload(args):
    _load_services()
    print("[reload] Services reloaded.")


def _cmd_reloadmodule(args):
    global module_instance
    if not args:
        print("Usage: reloadmodule <module>")
        return
    name = args[0].lower()
    if name not in MODULES:
        print("Unknown module:", name)
        return
    if active_module == name:
        module_instance = MODULES[name]()
        print(f"Reloaded module: {name}")
    else:
        print(f"Module {name} not active.")


def _cmd_shutdown(args):
    print("Shutting down...")
    try:
        import machine
        machine.poweroff()
    except Exception:
        print("[shutdown] machine.poweroff() unavailable.")


def _cmd_reboot(args):
    print("Rebooting...")
    try:
        import machine
        machine.reset()
    except Exception:
        print("[reboot] machine.reset() unavailable.")


def _cmd_factory(args):
    confirm = input("Type YES to confirm factory reset: ")
    if confirm.strip() == "YES":
        print("Factory reset initiated...")
    else:
        print("Cancelled.")


def _cmd_modules(args):
    print("Modules:", ", ".join(MODULES.keys()))


def _cmd_sysrun(args):
    if not args:
        print("Usage: sysrun <file>")
        return
    name = args[0]
    path = resolve_program(name, current_path)
    if not path:
        print(f"[sysrun] Not found: {name}")
        return
    if path.endswith(".py"):
        print("[sysrun] Running", path)
        try:
            run_python_file(path)
        except Exception as e:
            print("[sysrun] Error:", e)
    else:
        _cmd_cat([path])


def _cmd_pkgrun(args):
    if not args:
        print("Usage: pkgrun <package> [args...]")
        return
    if PackageManager is None:
        print("[pkgrun] PackageManager unavailable.")
        return
    name     = args[0]
    pkg_args = [convert_arg(a) for a in args[1:]]
    pm = PackageManager()
    pm.run(name, *pkg_args)


def _cmd_recover(args):
    try:
        rec = Recovery()
        ok = rec.run()
    except Exception as e:
        print("[recover] Recovery failed:", e)
        return
    if not ok:
        print("[recover] Finished with failures.")
        return
    if _load_services():
        print("[recover] Core modules re-imported.")


def _enter_module(name):
    global active_module, module_instance
    cls = MODULES.get(name.lower())
    if cls is None:
        print(f"No such module: {name}")
        return
    try:
        module_instance = cls()
    except Exception as e:
        print(f"[enter] Could not load '{name}': {e}")
        module_instance = None
        return
    active_module = name.lower()
    print(f">> {active_module}")


def _leave_module():
    global active_module, module_instance
    if active_module:
        print(f"Left module: {active_module}")
        active_module   = None
        module_instance = None
    else:
        print("Not inside a module.")


def _dispatch_module(instance, fn, args):
    if hasattr(instance, fn):
        r = getattr(instance, fn)(*args)
        if r is not None:
            print(r)
    else:
        print(f"No method: {fn}")


def _cmd_userlist(args):
    try:
        from Services import usermanager
        um = usermanager()
        users = um.list_users()
        if users:
            print("Users:", ", ".join(users))
        else:
            print("No users found")
    except Exception as e:
        print(f"Error listing users: {e}")


def _cmd_useradd(args):
    if len(args) < 2:
        print("Usage: useradd <username> <password> [--root]")
        return
    username, password = args[0], args[1]
    is_root = len(args) > 2 and args[2] == "--root"
    try:
        from Services import usermanager
        um = usermanager()
        _, message = um.create_user(username, password, is_root)
        print(message)
    except Exception as e:
        print(f"Error: {e}")


def _cmd_userdel(args):
    if not args:
        print("Usage: userdel <username>")
        return
    try:
        from Services import usermanager
        um = usermanager()
        _, message = um.delete_user(args[0])
        print(message)
    except Exception as e:
        print(f"Error: {e}")


def _cmd_userauth(args):
    if len(args) < 2:
        print("Usage: userauth <username> <password>")
        return
    try:
        from Services import usermanager
        um = usermanager()
        _, message = um.authenticate_user(args[0], args[1])
        print(message)
    except Exception as e:
        print(f"Error: {e}")


def _cmd_userhome(args):
    if not args:
        print("Usage: userhome <username>")
        return
    try:
        from Services import usermanager
        um = usermanager()
        print(um.get_user_home_path(args[0]) or "Not found")
    except Exception as e:
        print(f"Error: {e}")


def _cmd_userswitch(args):
    if not args:
        print("Usage: userswitch <username>")
        return
    username = args[0]
    _ = input(f"Password for {username}: ").strip()
    try:
        from Services import usermanager
        um = usermanager()
        success, message = um.switch_user_context(username)
        print(message)
        if success:
            zeno.user = username
    except Exception as e:
        print(f"Error: {e}")


def _toggle_cmd(args, attr, label, cmd_name):
    try:
        if len(args) == 0:
            print(f"{label}: {'on' if getattr(zeno, attr, False) else 'off'}")
        elif len(args) == 1:
            v = args[0].lower()
            if v in ("on", "true", "1", "yes"):
                setattr(zeno, attr, True)
                if hasattr(zeno, "save_config"):
                    zeno.save_config()
                print(f"{label} enabled")
            elif v in ("off", "false", "0", "no"):
                setattr(zeno, attr, False)
                if hasattr(zeno, "save_config"):
                    zeno.save_config()
                print(f"{label} disabled")
            else:
                print(f"Usage: {cmd_name} [on|off]")
    except Exception as e:
        print(f"Error: {e}")


def _cmd_debug(args):       _toggle_cmd(args, "debug_enabled", "Debug mode", "debug")
def _cmd_graphics(args):    _toggle_cmd(args, "graphics_enabled", "Graphics", "graphics")
def _cmd_wifi_cfg(args):    _toggle_cmd(args, "wifi_enabled", "WiFi", "wifi")
def _cmd_ble_cfg(args):     _toggle_cmd(args, "ble_enabled", "BLE", "ble")
def _cmd_bootverbose(args): _toggle_cmd(args, "boot_verbose", "Verbose boot", "bootverbose")
def _cmd_powersave(args):   _toggle_cmd(args, "power_saving_aggressive", "Power saving", "powersave")


def _cmd_perfmode(args):
    try:
        if not args:
            print(f"Performance mode: {getattr(zeno, 'performance_mode', 'balanced')}")
        elif args[0].lower() in ("performance", "powersaving", "balanced"):
            zeno.performance_mode = args[0].lower()
            if hasattr(zeno, "save_config"):
                zeno.save_config()
            print(f"Performance mode: {zeno.performance_mode}")
        else:
            print("Usage: perfmode [performance|powersaving|balanced]")
    except Exception as e:
        print(f"Error: {e}")


def _cmd_clear(args):
    if _OUT is None and term_list is not None:
        term_list.clear_terminal(redraw=True)
    else:
        print("\n" * 10)


BUILTINS = {
    "pwd":          (lambda a: print(current_path), False),
    "cd":           (_cmd_cd,          False),
    "ls":           (_cmd_ls,          False),
    "dir":          (_cmd_ls,          False),
    "tree":         (lambda a: tree_dir(_abs(a[0]) if a else current_path), False),
    "mkdir":        (_cmd_mkdir,       False),
    "rmdir":        (_cmd_rmdir,       False),
    "rm":           (_cmd_rm,          False),
    "cp":           (_cmd_cp,          False),
    "mv":           (_cmd_mv,          False),
    "touch":        (_cmd_touch,       False),
    "cat":          (_cmd_cat,         False),
    "head":         (_cmd_head,        False),
    "tail":         (_cmd_tail,        False),
    "search":       (_cmd_search,      False),
    "grep":         (_cmd_search,      False),
    "wc":           (_cmd_wc,          False),
    "sort":         (_cmd_sort,        False),
    "uniq":         (_cmd_uniq,        False),
    "echo":         (_cmd_echo,        False),
    "stat":         (_cmd_stat,        False),
    "file":         (_cmd_file,        False),
    "find":         (_cmd_find,        False),
    "which":        (_cmd_which,       False),
    "whereis":      (_cmd_whereis,     False),
    "whoami":       (lambda a: print(_cmd_whoami_str()), False),
    "id":           (_cmd_id,          False),
    "hostname":     (_cmd_hostname,    False),
    "date":         (_cmd_date,        False),
    "time":         (_cmd_time_cmd,    False),
    "uptime":       (_cmd_uptime,      False),
    "version":      (_cmd_version,     False),
    "df":           (_cmd_df,          False),
    "du":           (_cmd_du,          False),
    "free":         (_cmd_free,        False),
    "memdebug":     (_cmd_memdebug,    False),
    "ps":           (_cmd_ps,          False),
    "kill":         (_cmd_kill,        False),
    "jobs":         (_cmd_jobs,        False),
    "env":          (_cmd_env,         False),
    "export":       (_cmd_export,      False),
    "alias":        (_cmd_alias,       False),
    "unalias":      (_cmd_unalias,     False),
    "history":      (_cmd_history,     False),
    "clear":        (_cmd_clear,       False),
    "cls":          (_cmd_clear,       False),
    "userdebug":    (_cmd_userdebug,   False),
    "whoisroot":    (_cmd_whoisroot,   False),
    "passwd":       (_cmd_passwd,      False),
    "chusername":   (_cmd_chusername,  False),
    "modules":      (_cmd_modules,     False),
    "sysrun":       (_cmd_sysrun,      False),
    "pkgrun":       (_cmd_pkgrun,      False),
    "recover":      (_cmd_recover,     False),
    "mount":        (_cmd_mount,       True),
    "mountzfs":     (_cmd_mountzfs,    True),
    "sync":         (_cmd_sync,        False),
    "bootlog":      (_cmd_bootlog,     True),
    "log":          (_cmd_log,         False),
    "service":      (_cmd_service,     True),
    "services":     (_cmd_services,    False),
    "reload":       (_cmd_reload,      True),
    "reloadmodule": (_cmd_reloadmodule, True),
    "shutdown":     (_cmd_shutdown,    True),
    "reboot":       (_cmd_reboot,      True),
    "factory":      (_cmd_factory,     True),
    "userlist":     (_cmd_userlist,    False),
    "useradd":      (_cmd_useradd,     True),
    "userdel":      (_cmd_userdel,     True),
    "userauth":     (_cmd_userauth,    False),
    "userhome":     (_cmd_userhome,    False),
    "userswitch":   (_cmd_userswitch,  True),
    "debug":        (_cmd_debug,       False),
    "perfmode":     (_cmd_perfmode,    False),
    "graphics":     (_cmd_graphics,    False),
    "wifi":         (_cmd_wifi_cfg,    False),
    "ble":          (_cmd_ble_cfg,     False),
    "bootverbose":  (_cmd_bootverbose, False),
    "powersave":    (_cmd_powersave,   False),
}

aliases["ll"]  = "ls -l"
aliases["cls"] = "clear"
aliases["q"]   = "exit"

_HELP_FLAGS = ("help", "--help", "-h", "?")


def _preprocess(raw):
    parts = _split(raw)
    if not parts:
        return raw
    first = parts[0]
    if first in aliases:
        expansion = aliases[first]
        rest      = raw[len(first):].lstrip()
        return (expansion + " " + rest).strip() if rest else expansion
    return raw


def _execute(raw):
    global current_path, active_module, module_instance

    cmd = _preprocess(raw).strip()
    if not cmd:
        return

    parts = _split(cmd)
    verb  = parts[0].lower()
    args  = parts[1:]

    if verb in ("exit", "quit"):
        if active_module:
            _leave_module()
        else:
            on_exit()
        return

    if verb == "leave":
        _leave_module()
        return

    if verb == "help":
        _cmd_help(args)
        return

    if verb == "super":
        _cmd_super()
        return

    if verb == "unsuper":
        _cmd_unsuper()
        return

    if verb in MODULES and args and args[0].lower() in _HELP_FLAGS:
        _module_help(verb, tuple(args[1:]))
        return

    if verb in BUILTIN_HELP and verb not in MODULES and args and args[0] in ("--help", "-h"):
        print(BUILTIN_HELP[verb])
        return

    if verb == "enter":
        if args:
            _enter_module(args[0])
        else:
            print("Usage: enter <module>")
        return

    if verb in MODULES and not args:
        _enter_module(verb)
        return

    if active_module and module_instance:
        if verb not in BUILTINS and verb not in MODULES:
            if _require_super(verb):
                return
            _dispatch_module(module_instance, verb, [convert_arg(a) for a in args])
            return

    if verb in MODULES and args:
        fn    = args[0]
        margs = [convert_arg(a) for a in args[1:]]
        if _require_super(fn):
            return
        m = MODULES[verb]()
        _dispatch_module(m, fn, margs)
        return

    if verb in BUILTINS:
        handler, needs_super = BUILTINS[verb]
        if needs_super and not _is_super():
            print("Access denied. Command requires Super Mode. Use 'super' first.")
            return
        handler(args)
        return

    if _pm is not None and _pm.check(verb):
        _pm.run(verb, *args)
        return

    print(f"Unknown command: {verb} (type 'help')")


def _run_single(cmd_line, pipe_in=None, capture=False):
    global _PIPE_IN, _OUT
    _PIPE_IN = pipe_in

    if not capture:
        try:
            _execute(cmd_line)
        finally:
            _PIPE_IN = None
        return None

    cap = _OutputCapture()
    _OUT = cap
    try:
        _execute(cmd_line)
    finally:
        _OUT = None
        _PIPE_IN = None
    if cap._buf:
        cap.lines.append(cap._buf)
    return cap.lines


def _run_statement(stmt):
    segments = [s.strip() for s in _split_top(stmt, "|") if s.strip()]
    if not segments:
        return
    if len(segments) == 1:
        _run_single(segments[0], pipe_in=None, capture=False)
        return

    data = None
    for i, seg in enumerate(segments):
        is_last = (i == len(segments) - 1)
        data = _run_single(seg, pipe_in=data, capture=not is_last)


def handle(raw):
    raw = raw.strip()
    if not raw:
        return
    statements = [s.strip() for s in _split_top(raw, ";") if s.strip()]
    for stmt in statements:
        history_log.append(stmt)
        _run_statement(stmt)


# =================================================
# APP EXIT & UI EVENT LOOP
# =================================================
def on_exit():
    global input, print
    input = _real_input
    print = _real_print
    if "screen" in globals() and screen is not None:
        gfx._unreg(
            *(screen.widgets + ([screen.exit_btn] if screen.exit_btn else []))
        )
    if "APP_EXIT" in globals():
        raise APP_EXIT
    else:
        raise SystemExit(0)


def on_kb_submit(cmd_str):
    """Callback fired when 'OK' is pressed on the virtual keyboard."""
    cmd_line = cmd_str.strip()
    p_str = _prompt() + " "

    # 1. Echo the prompt (Green) and user input (White) into the non-touch UIListView
    term_list.add_line(("cmd", p_str, cmd_line), kind="cmd", redraw=True)

    # 2. Clear the keyboard buffer and refresh input bar immediately
    kb.clear_buffer()
    draw_input_bar(True)

    # 3. Execute the ZenCMD command
    if cmd_line:
        try:
            handle(cmd_line)
        except Exception as e:
            if "APP_EXIT" in globals() and isinstance(e, APP_EXIT):
                raise e
            if isinstance(e, SystemExit):
                on_exit()
            print("[ZenCMD] Error:", e)

    draw_input_bar(True)


def main():
    global screen, term_list, kb

    screen = UIWindow(
        lcd,
        title="ZenCMD Terminal",
        background=UBUNTU_BG,
        on_exit=on_exit,
        taskbar_color=color565(45, 45, 48),
        title_color=color565(255, 255, 255),
        taskbar_height=30,
        exit_button=True
    )

    # Non-touch UIListView for terminal history & outputs (y=32..152)
    term_list = TerminalListView(
        lcd,
        x=4,
        y=32,
        w=gfx.WIDTH - 8,
        h=120,
        item_h=11,
        bg=UBUNTU_BG
    )
    screen.add(term_list)

    # Start window & backlight
    screen.start(animate=False)
    gfx.pin.on()

    # Load ZenCMD services & print startup banner in green into the UIListView
    _load_services()
    print(f"{ZENOS_NAME} -- ZenCMD {ZENCMD_VERSION}")
    print(f"Logged in as: {getattr(zeno, 'user', 'user')} | Type 'help'")
    if Services is None:
        print("[ZenCMD] Services unavailable -- run 'recover'")

    # Initialize & render bottom Keyboard (y=182..312, height=130)
    kb = Keyboard(lcd, height=130, ok_action=on_kb_submit)
    kb.render()

    # Draw live Ubuntu prompt input bar (y=154..176)
    draw_input_bar(True)

    last_buf = kb.buffer
    cursor_state = True
    last_blink = time.ticks_ms()

    while True:
        # Poll window exit button
        screen.poll()

        # Poll on-screen keyboard
        kb.check_touch()

        # Redraw live input line whenever user types or cursor blinks
        now = time.ticks_ms()
        buf_changed = (kb.buffer != last_buf)
        blink_due = (time.ticks_diff(now, last_blink) >= 500)

        if buf_changed or blink_due:
            if blink_due:
                cursor_state = not cursor_state
                last_blink = now
            if buf_changed:
                last_buf = kb.buffer
                cursor_state = True
                last_blink = now
            draw_input_bar(cursor_state)

        time.sleep_ms(25)


main()
