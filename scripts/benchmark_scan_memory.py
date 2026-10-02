"""Isolated-process scan profiles; Rust allocator counts require allocation-profile.

RSS includes Python/imports; tracemalloc excludes native Rust allocations. Time
runs separately from tracemalloc. Trees are synthetic and removed by the parent.
Use --extension-dir to compare a separately built extension without installing it.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def worker(root, operation, repeats, extension):
    if extension:
        sys.path.insert(0, str(extension))
    import resource
    import tracemalloc

    import rust_backend

    from host_support.rust_filesystem import RustFilesystem
    from sandbox.linux_mounts import MountTable
    from sandbox.policy_scan import PolicyPlan, ScanRequest
    from sandbox.rust_policy import RustPolicyScanner
    from tools._internal.file_access import FileAccess
    from tools._internal.file_policy import PROTECTED_NAME_RULES
    from tools.filesystem import FindFileTool, SearchFilesTool

    native = RustFilesystem()
    if operation in {"policy", "policy-read"}:
        reads = (root / "wide", root / "deep") if operation == "policy-read" else ()
        workspace = root / "empty-workspace" if reads else root
        plan = PolicyPlan.compile(workspace, reads, (), name_rules=PROTECTED_NAME_RULES)
        request = ScanRequest(reads, False, MountTable.read().text)
        scanner = RustPolicyScanner()

        def call():
            return scanner.scan(plan, request)
    elif operation == "workspace":

        def call():
            return native.check_workspace(root)
    elif operation == "metadata":
        names = sorted(p.name for p in (root / "wide").iterdir())

        def call():
            with FileAccess(root, directory_backend=native).activate() as access:
                with access.read_directory(root / "wide") as reader:
                    method = getattr(reader, "scan_metadata", reader.stat_many)
                    for index in range(0, len(names), 128):
                        values = method(names[index : index + 128])
                        assert all(not isinstance(item, OSError) for item in values)
                        # Exercise the fields consumers use, not just allocation.
                        assert sum(v.st_size for v in values) >= 0
    else:
        tool = (
            FindFileTool(root, max_results=1_000_000)
            if operation == "find"
            else SearchFilesTool(root, max_files_scanned=1_000_000)
        )
        args = {"pattern": "**/*"} if operation == "find" else {"query": "absent"}

        def call():
            with FileAccess(root, directory_backend=native).activate():
                result = tool.execute(args)
                assert result.success, result

    call()
    samples = []
    for _ in range(repeats):
        start = perf_counter()
        call()
        samples.append((perf_counter() - start) * 1000)
    stats = getattr(rust_backend, "allocation_stats", None)
    before = stats(True) if stats else None
    result = call()
    after = stats() if stats else None
    allocation = (
        None
        if not stats
        else {
            "allocations": after[0] - before[0],
            "reallocations": after[1] - before[1],
            "requested_bytes": after[2] - before[2],
            "peak_extra_live_bytes": max(0, after[4] - before[3]),
        }
    )
    tracemalloc.start()
    call()
    _, peak_python = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    diagnostics = None
    if operation in {"policy", "policy-read"}:
        diagnostics = scanner.last_diagnostics
    elif operation == "workspace" and hasattr(rust_backend, "profile_workspace"):
        diagnostics = rust_backend.profile_workspace(os.fsencode(root))
    elif operation == "metadata" and hasattr(rust_backend, "profile_metadata"):
        diagnostics = {"observe_ms": 0.0, "python_conversion_ms": 0.0, "entries": 0}
        with FileAccess(root, directory_backend=native).activate() as access:
            with access.directory(root / "wide") as fd:
                for index in range(0, len(names), 128):
                    packed = b"\0".join(os.fsencode(n) for n in names[index : index + 128]) + b"\0"
                    batch = rust_backend.profile_metadata(fd, packed)
                    for key in diagnostics:
                        diagnostics[key] += batch[key]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "median_ms": round(statistics.median(samples), 3),
        "rust_allocations": allocation,
        "python_traced_peak_bytes": peak_python,
        "process_peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
        "policy_metrics": getattr(result, "metrics", None),
        "diagnostics": diagnostics,
        "scope": "warm caches; fresh scan; RSS includes imports; Rust counts exclude Python/libc",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=positive, default=10000)
    parser.add_argument("--depth", type=positive, default=80)
    parser.add_argument("--repeats", type=positive, default=5)
    parser.add_argument("--extension-dir", type=Path)
    parser.add_argument(
        "--operations",
        nargs="+",
        choices=["policy", "policy-read", "workspace", "metadata", "find", "search"],
        default=["policy", "workspace", "metadata"],
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.operations[0], args.repeats, args.extension_dir)))
        return
    results = {}
    with tempfile.TemporaryDirectory(prefix="scan-memory-") as temporary:
        root = Path(temporary).resolve()
        if "policy-read" in args.operations:
            (root / "empty-workspace").mkdir()
        wide = root / "wide"
        wide.mkdir()
        for i in range(args.files):
            (wide / f"{i:06}.txt").write_text("ordinary text\n")
        path = root / "deep"
        for _ in range(args.depth):
            path /= "d"
            path.mkdir(parents=True)
            (path / "file").write_text("ordinary text\n")
        for operation in args.operations:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(root),
                "--operations",
                operation,
                "--repeats",
                str(args.repeats),
            ]
            if args.extension_dir:
                command += ["--extension-dir", str(args.extension_dir.resolve())]
            child = subprocess.run(
                command, capture_output=True, text=True, check=True, env=os.environ.copy()
            )
            results[operation] = json.loads(child.stdout)
    print(json.dumps({"files": args.files, "depth": args.depth, "results": results}, indent=2))


if __name__ == "__main__":
    main()
