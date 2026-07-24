"""
process.py -- Zeno OS process model and scheduler.

Process/pid_type/pid_type_name/_weight/ProcessError/PermissionDenied
plus the Scheduler that drives them. Note: the original Services.py
had two duplicate copies of this block (roughly lines 1423-1666); the
stale duplicate has been dropped here, keeping only one definition of
each.
"""
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


def help():
    """Return a description of what's available in this module."""
    return (
        "process.py -- process model + scheduler\n"
        "  Process(pid, ppid, owner, name, func, mode, period, priority)  -- PCB\n"
        "  pid_type(pid) / pid_type_name(pid) -- decode the type digit encoded in a pid\n"
        "  Scheduler(logger=None, power=None)\n"
        "    .spawn(name, func, mode='loop', owner=..., priority=0, period=None)\n"
        "    .kill(pid, sig, system_token=None) / .nice(pid, priority, system_token=None)\n"
        "    .wait(pid, timeout_ms=None) / .reap(pid) / .list() / .getpid()\n"
        "    .start(frame_ms=16) / .stop() / .killall(system_token=None)\n"
        "    .set_power_policy(policy) / .power_status()\n"
        "  Exceptions: ProcessError, PermissionDenied"
    )
