"""
healthmon -- system health monitor for Zeno Micro-PC.

Watches heap usage, CPU frequency/temp (where available), flash/disk
free space, and uptime, logging via the existing Logger and raising
warnings before a device silently runs out of memory or storage and
crashes headless. Designed to run as a lightweight periodic task on
the existing Scheduler (Services.Scheduler), so it costs almost
nothing between checks.

Package contract:
    install(pm)      -- registers the 'health' command, and spawns a
                         periodic check on zeno.sched if available.
    uninstall(pm)     -- kills the periodic task, unregisters command.
    run(args, shell)  -- entry point for the 'health' command:
                             health status
                             health check           (run one check now)
                             health thresholds       (show current limits)
                             health set <metric> <value>
                             health interval <seconds>
                             health history

Thresholds default conservatively for an ESP32 (roughly 320KB heap):
    heap_free_min_bytes : warn below this much free heap
    disk_free_min_pct   : warn below this % free on the flash filesystem
    max_uptime_reset_h  : optional -- reboot after N hours uptime (0=off),
                            useful for devices that slowly leak memory in
                            third-party packages you don't control

All checks are cheap (gc.mem_free(), os.statvfs(), time deltas) so
running every 30-60s is safe even on a busy device.
"""

import gc
import os
import time
import json

try:
    import machine
except ImportError:
    machine = None

STATE_PATH = "/health_state.json"
COMMAND = "health"

_pm_ref = None
_logger = None
_scheduler_ref = None
_scheduled_pid = None
_check_interval_s = 60
_boot_time = time.time()

_thresholds = {
    "heap_free_min_bytes": 20_000,
    "disk_free_min_pct": 10,
    "max_uptime_reset_h": 0,  # 0 = disabled
}

_history = []  # list of {"time":..., "heap_free":..., "disk_free_pct":..., "alerts":[...]}
_HISTORY_MAX = 20


def _log(level, message):
    if _logger is None:
        print("[healthmon] {}".format(message))
        return
    getattr(_logger, level, _logger.debug)(message, source="HEALTHMON")


def _load_thresholds():
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
            _thresholds.update(saved.get("thresholds", {}))
    except (OSError, ValueError):
        pass


def _save_thresholds():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump({"thresholds": _thresholds}, f)
    except OSError as e:
        _log("error", "Could not save thresholds: {}".format(e))


def _disk_free_pct(path="/"):
    try:
        stats = os.statvfs(path)
        total = stats[2] * stats[0]
        free = stats[3] * stats[0]
        if total == 0:
            return None
        return round((free / total) * 100, 1)
    except Exception as e:
        _log("warning", "statvfs failed for '{}': {}".format(path, e))
        return None


def _cpu_freq():
    if machine is None:
        return None
    try:
        return machine.freq()
    except Exception:
        return None


def _uptime_hours():
    return round((time.time() - _boot_time) / 3600.0, 2)


def check():
    """Run one health check now, log any threshold breaches, and
    record a snapshot in the rolling history."""
    gc.collect()
    heap_free = gc.mem_free()
    disk_pct = _disk_free_pct("/")
    freq = _cpu_freq()
    uptime_h = _uptime_hours()

    alerts = []
    if heap_free < _thresholds["heap_free_min_bytes"]:
        alerts.append("LOW HEAP: {} bytes free (threshold {})".format(
            heap_free, _thresholds["heap_free_min_bytes"]))
    if disk_pct is not None and disk_pct < _thresholds["disk_free_min_pct"]:
        alerts.append("LOW DISK: {}% free (threshold {}%)".format(
            disk_pct, _thresholds["disk_free_min_pct"]))

    max_uptime = _thresholds.get("max_uptime_reset_h", 0)
    if max_uptime and uptime_h >= max_uptime:
        alerts.append("UPTIME LIMIT REACHED: {}h >= {}h -- scheduled reset".format(
            uptime_h, max_uptime))

    for alert in alerts:
        _log("warning", alert)

    snapshot = {
        "time": time.time(),
        "heap_free": heap_free,
        "disk_free_pct": disk_pct,
        "cpu_freq": freq,
        "uptime_h": uptime_h,
        "alerts": alerts,
    }
    _history.append(snapshot)
    if len(_history) > _HISTORY_MAX:
        del _history[0]

    if alerts and max_uptime and uptime_h >= max_uptime and machine is not None:
        _log("warning", "Resetting device due to max_uptime_reset_h policy.")
        time.sleep(1)
        machine.reset()

    return snapshot


def status():
    gc.collect()
    heap_free = gc.mem_free()
    disk_pct = _disk_free_pct("/")
    freq = _cpu_freq()
    uptime_h = _uptime_hours()

    print("\n[health] Current status")
    print("  Uptime       : {} h".format(uptime_h))
    print("  Heap free    : {} bytes".format(heap_free))
    print("  Disk free    : {}%".format(disk_pct if disk_pct is not None else "unknown"))
    print("  CPU freq     : {}".format("{} Hz".format(freq) if freq else "unknown"))
    print("  Check interval: {}s".format(_check_interval_s))
    return {
        "uptime_h": uptime_h, "heap_free": heap_free,
        "disk_free_pct": disk_pct, "cpu_freq": freq,
    }


def thresholds():
    print("\n[health] Thresholds")
    for key, value in _thresholds.items():
        print("  {:<24} {}".format(key, value))
    return dict(_thresholds)


def set_threshold(name, value):
    if name not in _thresholds:
        print("[health] Unknown threshold '{}'. Use 'health thresholds' to list valid names.".format(name))
        return False
    try:
        value = float(value) if "." in str(value) else int(value)
    except ValueError:
        print("[health] Threshold value must be numeric.")
        return False
    _thresholds[name] = value
    _save_thresholds()
    print("[health] Set {} = {}".format(name, value))
    return True


def set_interval(seconds):
    global _check_interval_s, _scheduled_pid
    seconds = int(seconds)
    if seconds < 5:
        print("[health] Minimum interval is 5 seconds.")
        return False
    _check_interval_s = seconds

    if _scheduler_ref is not None:
        _respawn_task()
    print("[health] Check interval set to {}s.".format(seconds))
    return True


def history():
    if not _history:
        print("[health] No history yet -- run 'health check' or wait for the next scheduled check.")
        return []
    print("\n[health] Recent checks")
    for snap in _history[-10:]:
        flag = " !" if snap["alerts"] else ""
        print("  heap={:<8} disk={:<6} uptime={}h{}".format(
            snap["heap_free"], snap.get("disk_free_pct"), snap["uptime_h"], flag))
    return list(_history)


def _respawn_task():
    global _scheduled_pid
    if _scheduler_ref is None:
        return
    try:
        from Services import SIGTERM, MODE_PERIODIC
        if _scheduled_pid is not None:
            try:
                _scheduler_ref.kill(_scheduled_pid, SIGTERM)
            except Exception:
                pass
        _scheduled_pid = _scheduler_ref.spawn(
            "healthmon_check", lambda: check(), mode=MODE_PERIODIC,
            period=_check_interval_s * 1000,
        )
    except Exception as e:
        _log("error", "Could not schedule health checks: {}".format(e))


def help():
    print("  health status               Show current heap/disk/uptime")
    print("  health check                Run one check now (logs alerts)")
    print("  health thresholds           List alert thresholds")
    print("  health set <name> <value>   Change a threshold")
    print("  health interval <seconds>   Change how often auto-checks run")
    print("  health history              Show recent check snapshots")


def run(args, shell):
    if not args:
        help()
        return True

    verb = args[0]
    rest = args[1:]

    if verb == "status":
        status()
    elif verb == "check":
        check()
    elif verb == "thresholds":
        thresholds()
    elif verb == "set":
        if len(rest) < 2:
            print("[health] Usage: health set <name> <value>")
            return False
        set_threshold(rest[0], rest[1])
    elif verb == "interval":
        if not rest:
            print("[health] Usage: health interval <seconds>")
            return False
        set_interval(rest[0])
    elif verb == "history":
        history()
    else:
        print("[health] Unknown subcommand '{}'".format(verb))
        help()
        return False
    return True


def _discover_scheduler():
    try:
        import zeno
        return getattr(zeno, "sched", None)
    except Exception:
        return None


def install(pm):
    global _pm_ref, _logger, _scheduler_ref, _boot_time
    _pm_ref = pm
    _logger = getattr(pm, "logger", None)
    _scheduler_ref = _discover_scheduler()
    _boot_time = time.time()
    _load_thresholds()
    pm.register(COMMAND, run)

    if _scheduler_ref is not None:
        _respawn_task()
        _log("debug", "healthmon installed; periodic checks every {}s.".format(_check_interval_s))
    else:
        _log("warning", "No Scheduler found -- run 'health check' manually or install a scheduler first.")


def uninstall(pm):
    global _scheduled_pid
    if _scheduler_ref is not None and _scheduled_pid is not None:
        try:
            from Services import SIGTERM
            _scheduler_ref.kill(_scheduled_pid, SIGTERM)
        except Exception:
            pass
        _scheduled_pid = None
    pm.unregister(COMMAND)
    _log("debug", "healthmon uninstalled.")
