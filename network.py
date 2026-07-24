"""
network.py -- Wi-Fi networking for Zeno OS.

A full rewrite of the old thin Network class. Everything here returns
data (bool / dict / list) instead of printing -- callers, including the
ZenCMD 'wifi' command wrapper, get structured results back to work
with. All status/error output goes through the required Logger
dependency instead of bare print(). No bare print() calls anywhere in
this module.

Covers: multi-profile WiFi credential storage (with priority), scan,
connect (by SSID, or auto-connect to the best known network in range),
disconnect, connection status, and a background auto-reconnect
watchdog (uasyncio-backed, degrading gracefully with a logged warning
if uasyncio isn't available).

Package contract (matches the rest of this codebase's installable
commands): module-level install(pm) / uninstall(pm) / run(args, shell).
"""
import json
import time

import network as _network

try:
    import uasyncio as asyncio
    _HAVE_UASYNCIO = True
except ImportError:
    asyncio = None
    _HAVE_UASYNCIO = False


PROFILES_PATH = "/LOGS/wifi_profiles.json"
COMMAND = "wifi"

WATCHDOG_POLL_MS = 5_000        # how often the watchdog checks link state
WATCHDOG_RECONNECT_DELAY_MS = 2_000  # pause between reconnect attempts


class WifiManager:
    """Owns one network.WLAN(STA_IF) and a set of saved connection
    profiles. Logger is a required, primary dependency -- constructed
    the same way Logger() is constructed elsewhere at boot -- not
    treated as optional, since every status/error path in this class
    goes through it instead of print()."""

    def __init__(self, logger, profiles_path=PROFILES_PATH, timeout=15):
        self.logger = logger
        self.source = "NETWORK"
        self.profiles_path = profiles_path
        self.timeout = timeout
        self.wlan = _network.WLAN(_network.STA_IF)

        self._watchdog_running = False
        self._watchdog_task = None

    # =============================================================
    # logging helpers -- every status/error path funnels through here
    # =============================================================
    def _debug(self, message):
        self.logger.debug(message, source=self.source)

    def _warn(self, message):
        self.logger.warning(message, source=self.source)

    def _error(self, message):
        self.logger.error(message, source=self.source)

    # =============================================================
    # profile storage -- multiple saved SSID/password profiles,
    # each with a priority used to pick the "best known network in
    # range" for auto-connect.
    # =============================================================
    def _load_profiles(self):
        try:
            with open(self.profiles_path, "r") as f:
                data = json.loads(f.read())
            if not isinstance(data, dict):
                return {}
            return data
        except Exception as e:
            self._debug("No existing profile store ({}); starting empty.".format(e))
            return {}

    def _save_profiles(self, profiles):
        try:
            with open(self.profiles_path, "w") as f:
                f.write(json.dumps(profiles))
            return True
        except Exception as e:
            self._error("Failed to save WiFi profiles: {}".format(e))
            return False

    def save_profile(self, ssid, password, priority=0):
        """Store (or update) a saved SSID/password profile. Higher
        priority wins when more than one saved network is in range.
        Returns True on success."""
        if not ssid:
            self._error("save_profile: ssid is required")
            return False
        profiles = self._load_profiles()
        profiles[ssid] = {"password": str(password or ""), "priority": int(priority)}
        ok = self._save_profiles(profiles)
        if ok:
            self._debug("Saved WiFi profile for '{}' (priority={})".format(ssid, priority))
        return ok

    def remove_profile(self, ssid):
        """Delete a saved profile. Returns True if it existed."""
        profiles = self._load_profiles()
        if ssid not in profiles:
            self._warn("remove_profile: no saved profile for '{}'".format(ssid))
            return False
        del profiles[ssid]
        ok = self._save_profiles(profiles)
        if ok:
            self._debug("Removed WiFi profile for '{}'".format(ssid))
        return ok

    def list_profiles(self):
        """Return {ssid: {"priority": int}} for every saved profile
        (passwords are not included in the returned dict)."""
        profiles = self._load_profiles()
        return {ssid: {"priority": rec.get("priority", 0)} for ssid, rec in profiles.items()}

    # =============================================================
    # scan
    # =============================================================
    def scan(self):
        """Scan for nearby access points. Returns a list of dicts:
        [{"ssid": str, "bssid": bytes, "channel": int, "rssi": int,
          "authmode": int, "hidden": bool}, ...]
        Returns an empty list (and logs the error) on failure."""
        try:
            self.wlan.active(True)
            raw = self.wlan.scan()
        except Exception as e:
            self._error("WiFi scan failed: {}".format(e))
            return []

        results = []
        for entry in raw:
            try:
                ssid_bytes, bssid, channel, rssi, authmode, hidden = entry
                results.append({
                    "ssid": ssid_bytes.decode("utf-8", "ignore") if isinstance(ssid_bytes, (bytes, bytearray)) else str(ssid_bytes),
                    "bssid": bssid,
                    "channel": channel,
                    "rssi": rssi,
                    "authmode": authmode,
                    "hidden": bool(hidden),
                })
            except Exception as e:
                self._warn("Skipping malformed scan entry: {}".format(e))
        self._debug("Scan found {} network(s)".format(len(results)))
        return results

    # =============================================================
    # connect / disconnect
    # =============================================================
    def _do_connect(self, ssid, password):
        wlan = self.wlan
        wlan.active(False)
        time.sleep_ms(200)
        wlan.active(True)
        time.sleep_ms(200)
        try:
            wlan.config(pm=wlan.PM_NONE)
        except Exception as e:
            self._debug("pm config failed (non-fatal): {}".format(e))

        self._debug("MAC: {}".format(wlan.config('mac')))
        self._debug("Connecting to '{}'".format(ssid))
        wlan.connect(ssid, password)

        start = time.time()
        while not wlan.isconnected():
            status = wlan.status()
            if status in (_network.STAT_WRONG_PASSWORD, _network.STAT_NO_AP_FOUND, _network.STAT_CONNECT_FAIL):
                self._error("Connect to '{}' failed with fatal status {}".format(ssid, status))
                return False
            if time.time() - start > self.timeout:
                self._error("Connect to '{}' timed out after {}s".format(ssid, self.timeout))
                return False
            time.sleep_ms(500)

        self._debug("Connected to '{}': {}".format(ssid, wlan.ifconfig()))
        return True

    def connect(self, ssid=None, password=None, save=False, priority=0):
        """Connect to a specific SSID, or -- if ssid is None -- auto-
        connect to the best known network currently in range (highest
        saved priority among visible saved profiles). Returns True/False."""
        if ssid is None:
            return self.auto_connect()

        if password is None:
            profiles = self._load_profiles()
            record = profiles.get(ssid)
            password = record.get("password", "") if record else ""

        ok = self._do_connect(ssid, password)
        if ok and save:
            self.save_profile(ssid, password, priority=priority)
        return ok

    def auto_connect(self):
        """Scan, then connect to whichever saved profile is both in
        range and has the highest priority. Returns True/False."""
        profiles = self._load_profiles()
        if not profiles:
            self._warn("auto_connect: no saved WiFi profiles")
            return False

        seen = self.scan()
        seen_ssids = {n["ssid"] for n in seen}
        candidates = [
            (ssid, rec) for ssid, rec in profiles.items() if ssid in seen_ssids
        ]
        if not candidates:
            self._warn("auto_connect: no saved profile is currently in range")
            return False

        candidates.sort(key=lambda pair: pair[1].get("priority", 0), reverse=True)
        best_ssid, best_rec = candidates[0]
        self._debug("auto_connect: selected '{}' (priority={})".format(best_ssid, best_rec.get("priority", 0)))
        return self._do_connect(best_ssid, best_rec.get("password", ""))

    def disconnect(self):
        """Disconnect and power down the WiFi radio. Returns True."""
        try:
            self.wlan.disconnect()
        except Exception as e:
            self._debug("disconnect() raised (non-fatal): {}".format(e))
        try:
            self.wlan.active(False)
        except Exception as e:
            self._warn("Failed to deactivate WiFi radio: {}".format(e))
            return False
        self._debug("WiFi disconnected")
        return True

    # =============================================================
    # status
    # =============================================================
    def status(self):
        """Return a dict describing current connection state:
        {"connected": bool, "ssid": str|None, "ip": str|None,
         "subnet": str|None, "gateway": str|None, "dns": str|None,
         "rssi": int|None}"""
        try:
            connected = bool(self.wlan.isconnected())
        except Exception as e:
            self._error("status: could not read link state: {}".format(e))
            return {"connected": False, "ssid": None, "ip": None,
                    "subnet": None, "gateway": None, "dns": None, "rssi": None}

        result = {"connected": connected, "ssid": None, "ip": None,
                  "subnet": None, "gateway": None, "dns": None, "rssi": None}

        if not connected:
            return result

        try:
            ip, subnet, gateway, dns = self.wlan.ifconfig()
            result["ip"], result["subnet"], result["gateway"], result["dns"] = ip, subnet, gateway, dns
        except Exception as e:
            self._warn("status: ifconfig() failed: {}".format(e))

        try:
            essid = self.wlan.config('essid')
            result["ssid"] = essid
        except Exception as e:
            self._debug("status: could not read essid: {}".format(e))

        try:
            result["rssi"] = self.wlan.status('rssi')
        except Exception as e:
            self._debug("status: could not read rssi: {}".format(e))

        return result

    def isconnected(self):
        try:
            return bool(self.wlan.isconnected())
        except Exception as e:
            self._error("isconnected() failed: {}".format(e))
            return False

    # =============================================================
    # background auto-reconnect watchdog
    # =============================================================
    def start_watchdog(self, poll_ms=WATCHDOG_POLL_MS):
        """Start a background task that periodically checks the link
        and calls auto_connect() if it's down. Uses uasyncio when
        available; if uasyncio isn't present on this build, logs a
        warning and returns False rather than raising -- callers
        should fall back to polling watchdog_tick() manually (e.g.
        from the process Scheduler) in that case."""
        if not _HAVE_UASYNCIO:
            self._warn("uasyncio not available on this build; background "
                        "auto-reconnect watchdog disabled. Call watchdog_tick() "
                        "manually (e.g. from a periodic Scheduler task) instead.")
            return False

        if self._watchdog_running:
            self._debug("Watchdog already running")
            return True

        self._watchdog_running = True

        async def _loop():
            while self._watchdog_running:
                try:
                    self.watchdog_tick()
                except Exception as e:
                    self._error("watchdog tick raised: {}".format(e))
                await asyncio.sleep_ms(poll_ms)

        self._watchdog_task = asyncio.create_task(_loop())
        self._debug("Auto-reconnect watchdog started (poll_ms={})".format(poll_ms))
        return True

    def stop_watchdog(self):
        """Stop the background watchdog task, if running."""
        if not self._watchdog_running:
            return False
        self._watchdog_running = False
        if self._watchdog_task is not None:
            try:
                self._watchdog_task.cancel()
            except Exception as e:
                self._debug("watchdog task cancel raised (non-fatal): {}".format(e))
            self._watchdog_task = None
        self._debug("Auto-reconnect watchdog stopped")
        return True

    def watchdog_tick(self):
        """One watchdog check: if not connected, try auto_connect().
        Safe to call manually on builds without uasyncio (e.g. wired
        into the process Scheduler as a periodic task). Returns True
        if the link was (or already is) up after this call."""
        if self.isconnected():
            return True
        self._warn("Link down; attempting auto-reconnect")
        time.sleep_ms(WATCHDOG_RECONNECT_DELAY_MS)
        return self.auto_connect()


def help():
    """Return a description of what's available in this module."""
    return (
        "network.py -- WiFi networking\n"
        "  WifiManager(logger, profiles_path='/LOGS/wifi_profiles.json', timeout=15)\n"
        "    .save_profile(ssid, password, priority=0) / .remove_profile(ssid) / .list_profiles()\n"
        "    .scan() -> list[dict]\n"
        "    .connect(ssid=None, password=None, save=False, priority=0) -> bool\n"
        "    .auto_connect() -> bool  -- connects to best known network in range\n"
        "    .disconnect() -> bool\n"
        "    .status() -> dict  -- connected/ssid/ip/subnet/gateway/dns/rssi\n"
        "    .isconnected() -> bool\n"
        "    .start_watchdog(poll_ms=5000) / .stop_watchdog() / .watchdog_tick()\n"
        "      -- background auto-reconnect; degrades gracefully (logged warning) "
        "without uasyncio, use watchdog_tick() manually instead\n"
        "  Command contract: install(pm) / uninstall(pm) / run(args, shell) "
        "registers/exposes 'wifi' as a ZenCMD command"
    )


# =====================================================================
# Package contract -- install(pm) / uninstall(pm) / run(args, shell)
# Registers 'wifi' as a ZenCMD command, matching how other installable
# packages in this codebase (see packages.py / PackageManager) wire
# themselves into the shell.
# =====================================================================

_manager = None


def _get_manager(shell=None):
    global _manager
    if _manager is not None:
        return _manager

    logger = None
    if shell is not None:
        logger = getattr(shell, "logger", None) or getattr(shell, "log", None)
    if logger is None:
        try:
            import zeno
            logger = zeno.log
        except Exception:
            from logger import Logger
            logger = Logger()

    _manager = WifiManager(logger)
    return _manager


def install(pm):
    """Package contract hook: register the 'wifi' command."""
    pm.register(COMMAND, run, module_name="network")


def uninstall(pm):
    """Package contract hook: stop the watchdog and drop the command."""
    global _manager
    if _manager is not None:
        _manager.stop_watchdog()
    pm.unregister(COMMAND)


def run(args, shell):
    """ZenCMD command entry point: `wifi <subcommand> [...]`.

    Subcommands:
      scan
      connect [ssid] [password] [--save] [--priority N]
      disconnect
      status
      profiles
      forget <ssid>
      watchdog start|stop|tick
    """
    mgr = _get_manager(shell)
    args = list(args or [])
    if not args:
        return mgr.status()

    sub = args[0]
    rest = args[1:]

    if sub == "scan":
        return mgr.scan()

    if sub == "connect":
        ssid = rest[0] if len(rest) > 0 else None
        password = rest[1] if len(rest) > 1 and not rest[1].startswith("--") else None
        save = "--save" in rest
        priority = 0
        if "--priority" in rest:
            try:
                priority = int(rest[rest.index("--priority") + 1])
            except (ValueError, IndexError):
                priority = 0
        return mgr.connect(ssid=ssid, password=password, save=save, priority=priority)

    if sub == "disconnect":
        return mgr.disconnect()

    if sub == "status":
        return mgr.status()

    if sub == "profiles":
        return mgr.list_profiles()

    if sub == "forget":
        if not rest:
            mgr._error("forget requires an ssid")
            return False
        return mgr.remove_profile(rest[0])

    if sub == "watchdog":
        action = rest[0] if rest else "tick"
        if action == "start":
            return mgr.start_watchdog()
        if action == "stop":
            return mgr.stop_watchdog()
        return mgr.watchdog_tick()

    mgr._error("Unknown wifi subcommand: {}".format(sub))
    return False
