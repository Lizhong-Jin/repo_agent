"""Host-side ownership tracking; never accepts process IDs supplied by a model.

Snapshots track PID + kernel start time, including observed children that change
process groups. This is best-effort supervision, not cgroup-style containment:
a child that forks and reparents between snapshots can escape observation.
"""

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    started: tuple[int, int]
    zombie: bool = False


class _BSDInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h, struct proc_bsdinfo (PROC_PIDTBSDINFO = 3).
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "flags",
            "status",
            "xstatus",
            "pid",
            "ppid",
            "uid",
            "gid",
            "ruid",
            "rgid",
            "svuid",
            "svgid",
            "reserved",
        )
    ] + [
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


class ProcessTable:
    """Read only kernel process metadata, never command lines or environments."""

    def __init__(self):
        self.proc_root = Path("/proc")
        if sys.platform == "darwin":
            self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_pidinfo.restype = ctypes.c_int
            self.lib.proc_listpids.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_listpids.restype = ctypes.c_int
        elif not sys.platform.startswith("linux"):
            raise OSError(errno.ENOTSUP, "Process supervision is unavailable on this platform")

    def read(self, pid):
        if sys.platform == "darwin":
            info = _BSDInfo()
            ctypes.set_errno(0)
            size = self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info):
                error = ctypes.get_errno()
                if error in (errno.ESRCH, errno.ENOENT):
                    return None
                raise OSError(error or errno.EIO, "Unable to inspect process identity")
            return ProcessIdentity(
                info.pid,
                info.ppid,
                info.pgid,
                (info.start_sec, info.start_usec),
                info.status == 5,
            )
        try:
            # comm is arbitrary bytes (including non-UTF-8 and parentheses).
            # Only parse the numeric fields after its final ')'. A malformed
            # snapshot must fail closed, not silently omit a possible descendant.
            raw = (self.proc_root / str(pid) / "stat").read_bytes()
            prefix, separator, suffix = raw.rpartition(b")")
            fields = suffix.split()
            if not separator or not prefix.startswith(f"{pid} (".encode()) or len(fields) < 20:
                raise OSError(errno.EIO, "Incomplete /proc process identity")
            return ProcessIdentity(
                pid,
                int(fields[1]),
                int(fields[2]),
                (int(fields[19]), 0),
                fields[0] in {b"Z", b"X", b"x"},
            )
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (ValueError, IndexError) as error:
            raise OSError(errno.EIO, "Invalid /proc process identity") from error

    def snapshot(self):
        if sys.platform == "darwin":
            # PROC_UID_ONLY: unrelated users' processes are never adopted or signalled.
            required = self.lib.proc_listpids(4, os.getuid(), None, 0)
            if required <= 0:
                raise OSError(errno.EACCES, "Unable to enumerate owned processes")
            pids = (ctypes.c_int * (required // ctypes.sizeof(ctypes.c_int) + 256))()
            size = self.lib.proc_listpids(4, os.getuid(), pids, ctypes.sizeof(pids))
            if size <= 0 or size >= ctypes.sizeof(pids):
                raise OSError(errno.EIO, "Incomplete process snapshot")
            ids = [pid for pid in pids[: size // ctypes.sizeof(ctypes.c_int)] if pid > 0]
        else:
            ids = []
            for path in self.proc_root.iterdir():
                if path.name.isdigit():
                    try:
                        if path.stat().st_uid == os.getuid():
                            ids.append(int(path.name))
                    except FileNotFoundError:
                        pass
        result = {}
        for pid in ids:
            info = self.read(pid)
            if info is not None:
                result[pid] = info
        return result


class ProcessSupervisor:
    """Track descendants of the exact Popen handle supplied by the launcher."""

    def __init__(self, process):
        self.process = process
        self.known = {}
        self.diagnostics = []
        self.next_poll = 0.0
        self.failed = False
        self.table = None
        try:
            self.table = ProcessTable()
            root = self.table.read(process.pid)
            if root is None:
                raise OSError(errno.ESRCH, "Initial process identity unavailable")
            self.root = root
            self.known[root.pid] = root
        except OSError as error:
            self.failed = True
            self.record("initial_identity", process.pid, error=error)

    def record(self, stage, pid, *, sig=None, error=None, outcome=None, via=None):
        if len(self.diagnostics) < 64:
            self.diagnostics.append(
                {
                    "stage": stage,
                    "pid": pid,
                    "signal": sig,
                    "errno": getattr(error, "errno", None),
                    "outcome": outcome or (type(error).__name__ if error else "sent"),
                    "via": via,
                }
            )

    def refresh(self, *, force=False):
        if self.table is None or not self.known:
            return []
        if not force and time.monotonic() < self.next_poll:
            return []
        self.next_poll = time.monotonic() + 0.05
        try:
            snapshot = self.table.snapshot()
        except (OSError, ValueError, IndexError) as error:
            self.failed = True
            self.record("process_snapshot", self.process.pid, error=error)
            return []
        owned = {
            pid
            for pid, old in self.known.items()
            if pid in snapshot and snapshot[pid].started == old.started
        }
        # An existing group cannot be reused until its members leave. Refuse to
        # adopt a group whose new leader has reused the original leader's PID.
        leader = snapshot.get(self.root.pid)
        same_group = leader is None or leader.started == self.root.started
        changed = True
        while changed:
            changed = False
            for pid, info in snapshot.items():
                if (
                    pid not in owned
                    and info.started >= self.root.started
                    and (info.ppid in owned or (same_group and info.pgid == self.root.pgid))
                ):
                    owned.add(pid)
                    self.known[pid] = info
                    changed = True
        return [snapshot[pid] for pid in owned if not snapshot[pid].zombie]

    def _signal(self, info, sig):
        descriptor = None
        stage = "identity"
        try:
            if (
                sys.platform.startswith("linux")
                and hasattr(os, "pidfd_open")
                and hasattr(signal, "pidfd_send_signal")
            ):
                try:
                    stage = "pidfd_open"
                    descriptor = os.pidfd_open(info.pid, 0)
                except OSError as error:
                    if error.errno != errno.ENOSYS:
                        raise
                    # Older kernels can run the existing identity-checked path.
                    # EPERM/EACCES are real policy failures, not lack of support.
                    self.record("pidfd_unavailable", info.pid, error=error, outcome="fallback")
            # Read identity AFTER opening pidfd: if the PID was reused while
            # opening it, reject the new process before sending anything.
            stage = "identity"
            current = self.table.read(info.pid)
            if current is None or current.zombie or current.started != info.started:
                return
            if descriptor is not None:
                stage = "pidfd_send_signal"
                signal.pidfd_send_signal(descriptor, sig)
            else:
                stage = "signal"
                os.kill(info.pid, sig)
            self.record(
                "terminate" if sig == signal.SIGTERM else "kill",
                info.pid,
                sig=sig,
                via="pidfd" if descriptor is not None else "pid",
            )
        except ProcessLookupError:
            pass
        except OSError as error:
            self.record(stage, info.pid, sig=sig, error=error)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def cleanup(self, drain=lambda: None):
        # Signal only identities discovered by this supervisor, never user input.
        # Keep draining while waiting so TERM handlers cannot block on full pipes.
        for sig, grace in ((signal.SIGTERM, 0.7), (signal.SIGKILL, 0.7)):
            sent = set()
            deadline = time.monotonic() + grace
            while True:
                live = self.refresh(force=True)
                for info in sorted(live, key=lambda p: p.pid == self.process.pid):
                    key = (info.pid, info.started)
                    if key not in sent:
                        self._signal(info, sig)
                        sent.add(key)
                self.process.poll()
                drain()
                if not live or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        # Always reap our direct child, even when process-table access was denied.
        if self.process.poll() is None:
            try:
                self.process.kill()
                self.record("leader_fallback", self.process.pid, sig=signal.SIGKILL)
            except OSError as error:
                self.record("leader_fallback", self.process.pid, error=error)
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self.failed = True
        live = self.refresh(force=True)
        self.record(
            "verify", self.process.pid, outcome="unknown" if self.failed or live else "confirmed"
        )
        if self.failed or live:
            return "Unable to confirm cleanup of this call's tracked processes."
        return None
