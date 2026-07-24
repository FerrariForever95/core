"""
Services.py -- compatibility shim.

The original ~4300-line Services.py has been split into 7 subsystem
files (logger, power, filesystem, security, process, packages, system)
plus a rewritten network.py. This module re-exports everything from
them so existing `from Services import X` imports elsewhere in the
codebase (including ZenCMD.py) keep working unchanged.

Load order matters: logger has zero dependencies and loads first;
filesystem and security depend on each other only via lazy in-method
imports (no cycle at module-load time); process depends on nothing
new; packages depends on logger/filesystem/security/system; system
depends on logger. network.py is independent (only needs a Logger
instance, passed in explicitly).
"""

# ---- logger.py --------------------------------------------------------
from logger import (
    Logger,
    debug_log_enabled,
    help as _logger_help,
)

# ---- power.py -----------------------------------------------------------
from power import (
    PowerManagement,
    _BoostContext,
    CPU,
    help as _power_help,
)

# ---- filesystem.py --------------------------------------------------------
from filesystem import (
    dewrapper,
    PermissionError,
    FileNotFoundError,
    FileExistsError,
    NotADirectoryError,
    FileManager,
    SYS_DIR,
    PROGRAM_EXT, TEXT_EXT, BITMAP_EXT, IMAGE_EXT, AUDIO_EXT, VIDEO_EXT,
    JSON_EXT, ARCHIVE_EXT, EXECUTABLE_EXT, ROOT_OWNED_EXT,
    PERM_FULL, PERM_READ, PERM_READWRITE, PERM_NONE,
    PERM_READEXEC, PERM_WRITEEXEC, PERM_WRITE, PERM_EXEC,
    READ_PERMS, WRITE_PERMS, EXEC_PERMS, VALID_PERMS,
    help as _filesystem_help,
)

# ---- security.py --------------------------------------------------------
from security import (
    SystemPrivilege,
    usermanager,
    _SYSTEM_TOKEN,
    help as _security_help,
)

# ---- process.py -----------------------------------------------------------
from process import (
    Process,
    Scheduler,
    pid_type,
    pid_type_name,
    ProcessError,
    PermissionDenied,
    PID_TYPE_KERNEL, PID_TYPE_USER, PID_TYPE_THREAD,
    PID_TYPE_DAEMON, PID_TYPE_NETWORK, PID_TYPE_RESERVED, PID_TYPE_NAMES,
    NEW, READY, RUNNING, BLOCKED, ZOMBIE, DEAD,
    SIGTERM, SIGKILL, SIGSTOP, SIGCONT,
    MODE_LOOP, MODE_PERIODIC, MODE_ONCE, MODE_THREAD,
    PERSIST_PATH,
    help as _process_help,
)

# ---- network.py (full rewrite) -------------------------------------------
from network import (
    WifiManager,
    help as _network_help,
)
# Backwards-compat alias: old code that did `from Services import Network`
# and constructed it as Network(ssid, password, timeout) will need to
# switch to WifiManager(logger) -- the new module requires a Logger
# instance (see network.py docstring). No drop-in shim is provided for
# the old constructor signature since silently swallowing the missing
# logger dependency would defeat the point of making it required.

# ---- packages.py --------------------------------------------------------
from packages import (
    downloadhelper,
    Git,
    AppInstaller,
    Wiki,
    AppDB,
    PackageManager,
    help as _packages_help,
)

# ---- system.py ------------------------------------------------------------
from system import (
    Disk,
    BootConfig,
    system,
    BluetoothManager,
    Device,
    IoTManager,
    bootcfg,
    cfg,
    cfg_get,
    SD_SCK, SD_MOSI, SD_MISO, SD_CS,
    help as _system_help,
)


def help():
    """Return a combined description of every subsystem module."""
    return "\n\n".join([
        _logger_help(), _power_help(), _filesystem_help(), _security_help(),
        _process_help(), _network_help(), _packages_help(), _system_help(),
    ])
