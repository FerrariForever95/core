"""
logger.py -- Zeno OS system logger.

Zero dependencies on any other Services subsystem module (power,
filesystem, security, process, network, packages, system) so every
other module can safely import it first.
"""
from machine import Pin, I2C
from firmware import DS3231

# Global toggle: when True, Logger._write() also echoes to stdout in
# addition to persisting to the log file. Other modules that want to
# flip this at runtime should do:
#     import logger as _logger_mod
#     _logger_mod.debug_log_enabled = True
debug_log_enabled = False


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


def help():
    """Return a description of what's available in this module."""
    return (
        "logger.py -- system logging\n"
        "  Logger(log_file_user='/LOGS/systemlog.txt', boot=False)\n"
        "    .log(level, message, source='GENERAL')\n"
        "    .error(message, source='GENERAL')\n"
        "    .warning(message, source='GENERAL')\n"
        "    .debug(message, source='GENERAL')\n"
        "    .boot_complete()\n"
        "    .viewlogs(lines=None)\n"
        "    .clear_logs()\n"
        "  module var: debug_log_enabled (bool) -- toggle console echo of log writes"
    )
