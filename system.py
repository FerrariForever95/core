"""
system.py -- low-level system control for Zeno OS.

system (RAM/security housekeeping, guardian daemon, restart/optlevel),
Disk (SD card mount/format/info), BootConfig (persisted boot config),
Wiki (already in packages.py -- not duplicated here), BluetoothManager
(BLE), Device + IoTManager (Sinric Pro smart-home integration).
"""
import os
import time
import json
import gc
import machine
import micropython
import pystone_lowmem
import ubluetooth as bt
import zeno
from machine import Pin, SPI

from firmware import SDCard
from sinricpro import SinricPro
from sinricpro.devices.sinricpro_switch import SinricProSwitch

from logger import Logger

SD_SCK, SD_MOSI, SD_MISO, SD_CS = 40, 6, 5, 7

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


def help():
    """Return a description of what's available in this module."""
    return (
        "system.py -- low-level system control\n"
        "  system(opt_level=0, debug=False)\n"
        "    .restart() / .optlevel(level) / .info() / .mode(m)\n"
        "    .memconfig(percent=25) / .force_mem() / .mem_usage() / .perf_test()\n"
        "    .ram_guard(warn_pct=80, crit_pct=92) / .security_scan() / .checkup()\n"
        "    .start_guardian(sched, interval_ms=10000)\n"
        "    .firmware_update() / .boot_update()\n"
        "  Disk(mount_point='/MemDisk')\n"
        "    .begin() / .check() / .unmount() / .format(filesystem=os.VfsFat) / .info(path=None)\n"
        "  BootConfig() -- persisted /LOGS/bootcfg.json\n"
        "    .get(key, default=None) / .set(key, value) / .show()\n"
        "  BluetoothManager(device_name='Zeno Micro PC')\n"
        "    .on() / .off() / .search(duration=5) / .connect(addr_type, addr) / .disconnect()\n"
        "    .send_data(data) / .get_data()\n"
        "  IoTManager(app_key, app_secret) -- Sinric Pro smart-home devices\n"
        "    .add_switch(device_id, name, pin_number=None) / .on/.off/.toggle(identifier)\n"
        "    .list_devices() / .remove_device(identifier) / .start() / .handle()"
    )
