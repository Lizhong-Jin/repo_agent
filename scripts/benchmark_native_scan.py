"""Compare the former two-pass Linux preflight with the shared one-pass scan.

Run from a source checkout. Does not launch a sandbox or modify --workspace.
Without --workspace, builds and removes a synthetic tree in a temporary directory.
"""

import argparse
import json
import os
import platform
import stat
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.linux_native import LinuxNativeBackend  # noqa: E402


def previous_scan(root):
    """Reference algorithm from native.py + linux_native.py before this change."""
    def inaccessible(error):
        raise error

    for directory, _, names in os.walk(root, followlinks=False, onerror=inaccessible):
        for name in names:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError(f"Hard link: {path}")
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            mode = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise ValueError(f"Special file: {directory}/{name}")


def measured_calls(scan):
    counts = {"directory_enumerations": 0, "metadata_queries": 0}
    original_scandir, original_stat, original_lstat = os.scandir, os.stat, os.lstat

    def scandir(*args, **kwargs):
        counts["directory_enumerations"] += 1
        return original_scandir(*args, **kwargs)

    def metadata(method, *args, **kwargs):
        counts["metadata_queries"] += 1
        return method(*args, **kwargs)

    with patch.object(os, "scandir", scandir), patch.object(
        os, "stat", lambda *a, **kw: metadata(original_stat, *a, **kw),
    ), patch.object(os, "lstat", lambda *a, **kw: metadata(original_lstat, *a, **kw)):
        scan()
    return counts


def benchmark(root, repeats):
    backend = object.__new__(LinuxNativeBackend)
    backend.workspace = root
    backend.read_paths = ()
    scans = {"before": lambda: previous_scan(root), "after": backend._check_workspace}
    for scan in scans.values():
        scan()  # Warm filesystem caches equally; not a cold-cache benchmark.
    timings = {name: [] for name in scans}
    for repetition in range(repeats):
        order = list(scans) if repetition % 2 == 0 else list(reversed(scans))
        for name in order:
            start = time.perf_counter()
            scans[name]()
            timings[name].append((time.perf_counter() - start) * 1000)
    return {
        "host": platform.system(), "python": platform.python_version(),
        "workspace": str(root), "repeats": repeats,
        "scope": "Linux workspace validation algorithm only; no namespaces or mount-policy scan",
        "count_scope": "Python os.scandir/stat/lstat calls; excludes DirEntry internal syscalls",
        "results": {
            name: {"median_ms": round(statistics.median(timings[name]), 3),
                   **measured_calls(scan)}
            for name, scan in scans.items()
        },
    }


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--files", type=positive, default=10_000)
    parser.add_argument("--repeats", type=positive, default=7)
    args = parser.parse_args()
    if args.workspace:
        root = args.workspace.resolve(strict=True)
        if not root.is_dir():
            parser.error("--workspace must be a directory")
        result = benchmark(root, args.repeats)
    else:
        with tempfile.TemporaryDirectory(prefix="native-scan-benchmark-") as temporary:
            root = Path(temporary).resolve()
            for index in range(args.files):
                category = ("src", ".venv", "logs")[index % 3]
                path = root / category / str(index % 100) / f"file-{index}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"benchmark\n")
            result = benchmark(root, args.repeats)
            result["synthetic_files"] = args.files
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
