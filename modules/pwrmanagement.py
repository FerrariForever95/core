# =================================================
# PowerManagement -- CPU frequency / power scaling service
# =================================================
# Fits the same pattern as the other Services classes (Network, Disk,
# PackageManager, ...): a plain class with print()-based status output,
# a help() method for ZenCMD's "<module> help", and methods callable
# either one-shot ("power boost download") or via "enter power".
#
# Frequency is controlled through machine.freq(), which is what actually
# changes CPU frequency at runtime on ESP32 / RP2040 / STM32 boards under
# MicroPython. There is no OS scheduler here to measure "load" for you --
# this service exposes named levels and a boost/release mechanism so the
# rest of the OS can *request* more performance when it knows it needs it
# (e.g. before a big package download, or during a slow computation) and
# automatically fall back once nobody's asking for it anymore.

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
