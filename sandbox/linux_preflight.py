"""Trusted Linux startup checks, all executed inside ONE native sandbox.

The controller gives isolation and CUDA separate subprocesses and deadlines.
This stdlib-only script is launched with -I from the frozen trusted code copy.
"""

import argparse
import ctypes
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter


def check_isolation(workspace, denied):
    def blocked(action):
        try:
            action()
        except OSError as error:
            allowed = (errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS)
            assert error.errno in allowed, repr(error)
        else:
            raise AssertionError("native restriction missing")

    blocked(lambda: denied.read_text())
    blocked(lambda: denied.write_text("changed"))
    blocked(lambda: socket.socket())
    blocked(lambda: socket.socket(socket.AF_UNIX))
    blocked(lambda: Path("/proc/1/root" + str(denied)).read_text())
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.unshare(0x10000000) == -1 and ctypes.get_errno() == errno.EPERM
    path = Path(tempfile.gettempdir()) / "probe"
    path.write_text("ok")
    assert path.read_text() == "ok"
    path.unlink()
    with tempfile.NamedTemporaryFile(dir=workspace, prefix=".native-probe-") as file:
        file.write(b"probe")
        file.flush()
        blocked(lambda: os.link(file.name, str(path)))
    child = subprocess.run(
        [sys.executable, "-I", "-c", "import socket; socket.socket()"], capture_output=True,
    )
    assert child.returncode != 0


def run_check(command, timeout):
    started = perf_counter()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {"exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr[-2000:], "timed_out": False,
                "duration_ms": (perf_counter() - started) * 1000}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout": "", "stderr": "startup check timed out",
                "timed_out": True, "duration_ms": (perf_counter() - started) * 1000}
    except OSError as error:
        return {"exit_code": None, "stdout": "", "stderr": str(error),
                "timed_out": False, "duration_ms": (perf_counter() - started) * 1000}


def probe(workspace, denied, *, gpu):
    isolation = run_check([
        sys.executable, "-I", str(Path(__file__).resolve()), "--isolation-only",
        "--workspace", str(workspace), "--denied", str(denied),
    ], 30)
    report = {"isolation": isolation}
    if isolation["exit_code"] == 0 and isolation["stdout"].strip() == "native-ok" and gpu:
        report["gpu"] = run_check([
            sys.executable, "-I", str(Path(__file__).with_name("native_gpu_probe.py")),
        ], 90)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--denied", type=Path, required=True)
    parser.add_argument("--isolation-only", action="store_true")
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args(argv)
    if args.isolation_only:
        check_isolation(args.workspace, args.denied)
        print("native-ok")
        return 0
    report = probe(args.workspace, args.denied, gpu=args.gpu)
    print(json.dumps(report))
    return 0 if all(check["exit_code"] == 0 for check in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
