"""
Services.py -- compatibility shim.

The original ~4300-line Services.py has been split into 7 subsystem
files (logger, power, filesystem, security, process, packages, system)
plus a rewritten network.py. Every one of those now ships and updates
as its own package at /bin/<name>/<name>.py -- same as any third-party
package -- and is loaded here through syspathmanager.SysPathManager, the
single shared dynamic-import helper Services/ZenCMD both use. This module
re-exports everything from them so existing `from Services import X`
imports elsewhere in the codebase (including ZenCMD.py) keep working
unchanged.

Load order matters: logger has zero dependencies and loads first;
filesystem and security depend on each other only via lazy in-method
imports (no cycle at load time); process depends on nothing new;
packages depends on logger/filesystem/security/system; system depends
on logger. network.py is independent (only needs a Logger instance,
passed in explicitly).

If /bin/<name>/<name>.py is missing or broken for one of these core
modules, that's a boot-time problem for 'recover' to fix -- this shim
does not fall back to anything else, on purpose (see syspathmanager.py's
note about recovery.py deliberately not depending on it).
"""
from syspathmanager import SysPathManager

_BIN = "/bin"


def _load(name):
    return SysPathManager.import_from(_BIN, name, name)


# ---- logger.py --------------------------------------------------------
_logger_mod = _load("logger")
Logger = _logger_mod.Logger
debug_log_enabled = _logger_mod.debug_log_enabled
_logger_help = _logger_mod.help

# ---- power.py -----------------------------------------------------------
_power_mod = _load("power")
PowerManagement = _power_mod.PowerManagement
_BoostContext = _power_mod._BoostContext
CPU = _power_mod.CPU
_power_help = _power_mod.help

# ---- filesystem.py --------------------------------------------------------
_filesystem_mod = _load("filesystem")
dewrapper = _filesystem_mod.dewrapper
PermissionError = _filesystem_mod.PermissionError
FileNotFoundError = _filesystem_mod.FileNotFoundError
FileExistsError = _filesystem_mod.FileExistsError
NotADirectoryError = _filesystem_mod.NotADirectoryError
FileManager = _filesystem_mod.FileManager
SYS_DIR = _filesystem_mod.SYS_DIR
PROGRAM_EXT = _filesystem_mod.PROGRAM_EXT
TEXT_EXT = _filesystem_mod.TEXT_EXT
BITMAP_EXT = _filesystem_mod.BITMAP_EXT
IMAGE_EXT = _filesystem_mod.IMAGE_EXT
AUDIO_EXT = _filesystem_mod.AUDIO_EXT
VIDEO_EXT = _filesystem_mod.VIDEO_EXT
JSON_EXT = _filesystem_mod.JSON_EXT
ARCHIVE_EXT = _filesystem_mod.ARCHIVE_EXT
EXECUTABLE_EXT = _filesystem_mod.EXECUTABLE_EXT
ROOT_OWNED_EXT = _filesystem_mod.ROOT_OWNED_EXT
PERM_FULL = _filesystem_mod.PERM_FULL
PERM_READ = _filesystem_mod.PERM_READ
PERM_READWRITE = _filesystem_mod.PERM_READWRITE
PERM_NONE = _filesystem_mod.PERM_NONE
PERM_READEXEC = _filesystem_mod.PERM_READEXEC
PERM_WRITEEXEC = _filesystem_mod.PERM_WRITEEXEC
PERM_WRITE = _filesystem_mod.PERM_WRITE
PERM_EXEC = _filesystem_mod.PERM_EXEC
READ_PERMS = _filesystem_mod.READ_PERMS
WRITE_PERMS = _filesystem_mod.WRITE_PERMS
EXEC_PERMS = _filesystem_mod.EXEC_PERMS
VALID_PERMS = _filesystem_mod.VALID_PERMS
_filesystem_help = _filesystem_mod.help

# ---- security.py --------------------------------------------------------
_security_mod = _load("security")
SystemPrivilege = _security_mod.SystemPrivilege
usermanager = _security_mod.usermanager
_SYSTEM_TOKEN = _security_mod._SYSTEM_TOKEN
_security_help = _security_mod.help

# ---- process.py -----------------------------------------------------------
_process_mod = _load("process")
Process = _process_mod.Process
Scheduler = _process_mod.Scheduler
pid_type = _process_mod.pid_type
pid_type_name = _process_mod.pid_type_name
ProcessError = _process_mod.ProcessError
PermissionDenied = _process_mod.PermissionDenied
PID_TYPE_KERNEL = _process_mod.PID_TYPE_KERNEL
PID_TYPE_USER = _process_mod.PID_TYPE_USER
PID_TYPE_THREAD = _process_mod.PID_TYPE_THREAD
PID_TYPE_DAEMON = _process_mod.PID_TYPE_DAEMON
PID_TYPE_NETWORK = _process_mod.PID_TYPE_NETWORK
PID_TYPE_RESERVED = _process_mod.PID_TYPE_RESERVED
PID_TYPE_NAMES = _process_mod.PID_TYPE_NAMES
NEW = _process_mod.NEW
READY = _process_mod.READY
RUNNING = _process_mod.RUNNING
BLOCKED = _process_mod.BLOCKED
ZOMBIE = _process_mod.ZOMBIE
DEAD = _process_mod.DEAD
SIGTERM = _process_mod.SIGTERM
SIGKILL = _process_mod.SIGKILL
SIGSTOP = _process_mod.SIGSTOP
SIGCONT = _process_mod.SIGCONT
MODE_LOOP = _process_mod.MODE_LOOP
MODE_PERIODIC = _process_mod.MODE_PERIODIC
MODE_ONCE = _process_mod.MODE_ONCE
MODE_THREAD = _process_mod.MODE_THREAD
PERSIST_PATH = _process_mod.PERSIST_PATH
_process_help = _process_mod.help

# ---- network.py (full rewrite) -------------------------------------------
_network_mod = _load("network")
WifiManager = _network_mod.WifiManager
_network_help = _network_mod.help
# Backwards-compat note: old code that did `from Services import Network`
# and constructed it as Network(ssid, password, timeout) will need to
# switch to WifiManager(logger) -- the new module requires a Logger
# instance (see network.py docstring). No drop-in shim is provided for
# the old constructor signature since silently swallowing the missing
# logger dependency would defeat the point of making it required.

# ---- packages.py --------------------------------------------------------
_packages_mod = _load("packages")
downloadhelper = _packages_mod.downloadhelper
Git = _packages_mod.Git
AppInstaller = _packages_mod.AppInstaller
Wiki = _packages_mod.Wiki
AppDB = _packages_mod.AppDB
PackageManager = _packages_mod.PackageManager
_packages_help = _packages_mod.help

# ---- system.py ------------------------------------------------------------
_system_mod = _load("system")
Disk = _system_mod.Disk
BootConfig = _system_mod.BootConfig
system = _system_mod.system
BluetoothManager = _system_mod.BluetoothManager
Device = _system_mod.Device
IoTManager = _system_mod.IoTManager
bootcfg = _system_mod.bootcfg
cfg = _system_mod.cfg
cfg_get = _system_mod.cfg_get
SD_SCK = _system_mod.SD_SCK
SD_MOSI = _system_mod.SD_MOSI
SD_MISO = _system_mod.SD_MISO
SD_CS = _system_mod.SD_CS
_system_help = _system_mod.help


def help():
    """Return a combined description of every subsystem module."""
    return "\n\n".join([
        _logger_help(), _power_help(), _filesystem_help(), _security_help(),
        _process_help(), _network_help(), _packages_help(), _system_help(),
    ])
