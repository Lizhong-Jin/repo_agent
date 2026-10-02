"""Compare bounded Rust scans in isolated processes on a synthetic branching tree.

Each result is a warm-cache median, including per-call worker startup/cleanup.
Run on the actual Linux/WSL filesystem of interest via --temporary-parent.
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


def measure(root, workers, repeats, scenario):
    os.environ["AGENT_SCAN_WORKERS"] = str(workers)
    import rust_backend

    from sandbox.linux_mounts import MountTable
    from sandbox.policy_scan import PolicyPlan, ScanRequest
    from sandbox.rust_policy import RustPolicyScanner
    from tools._internal.file_policy import PROTECTED_NAME_RULES

    if scenario == "workspace":

        def call():
            return rust_backend.profile_workspace(os.fsencode(root / "tree"))
    else:
        reads = (root / "tree",) if scenario == "policy-read" else ()
        workspace = root / "empty" if reads else root / "tree"
        scanner = RustPolicyScanner()
        plan = PolicyPlan.compile(workspace, reads, (), name_rules=PROTECTED_NAME_RULES)
        request = ScanRequest(reads, False, MountTable.read().text)

        def call():
            scanner.scan(plan, request)
            return scanner.last_diagnostics

    call()
    samples = []
    for _ in range(repeats):
        start = perf_counter()
        diagnostics = call()
        samples.append((perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 3), "diagnostics": diagnostics}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directories", type=positive, default=128)
    parser.add_argument("--files-per-directory", type=positive, default=128)
    parser.add_argument("--workers", type=int, nargs="+", choices=range(1, 9), default=[1, 2, 4])
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--temporary-parent", type=Path)
    parser.add_argument("--child-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--scenario", choices=["workspace", "policy", "policy-read"], help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.child_root:
        print(json.dumps(measure(args.child_root, args.workers[0], args.repeats, args.scenario)))
        return
    results = {}
    with tempfile.TemporaryDirectory(prefix="scan-parallel-", dir=args.temporary_parent) as temp:
        root = Path(temp).resolve()
        (root / "empty").mkdir()
        for index in range(args.directories):
            directory = root / "tree" / str(index)
            directory.mkdir(parents=True)
            for entry in range(args.files_per_directory):
                (directory / str(entry)).touch()
        for scenario in ("workspace", "policy", "policy-read"):
            results[scenario] = {}
            for workers in args.workers:
                output = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child-root",
                        str(root),
                        "--workers",
                        str(workers),
                        "--repeats",
                        str(args.repeats),
                        "--scenario",
                        scenario,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                results[scenario][workers] = json.loads(output.stdout)
    print(
        json.dumps(
            {
                "directories": args.directories,
                "files_per_directory": args.files_per_directory,
                "repeats": args.repeats,
                "platform": sys.platform,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
