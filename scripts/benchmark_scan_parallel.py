"""Interleaved Rust scan benchmark on realistic small directories or a read-only real tree.

Warm caches; each scenario uses its own child process and reuses one scanner.
Timings include worker startup/cleanup, exclude tree creation, hashing and model/tool overhead.
"""

import argparse
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_tree(root, directories, files, shape):
    """Deterministic uneven small-directory tree; no claims of copying a real Conda tree."""
    rng = random.Random(17)
    parents = [root]
    root.mkdir()
    file_count = 0
    for index in range(directories):
        parent = root if shape == "wide" else parents[index // 8]
        directory = parent / f"package-{index:06}"
        directory.mkdir()
        parents.append(directory)
        count = files if shape == "wide" else rng.randint(max(0, files - 4), files + 4)
        for entry in range(count):
            name = ".env" if entry == 0 and index % 101 == 0 else f"module-{entry}.py"
            (directory / name).touch()
        file_count += count
    return {"directories": directories + 1, "files": file_count, "shape": shape}


def configurations(workers, batch_sizes):
    return [(1, 1)] + [
        (w, b) for w in dict.fromkeys(workers) if w != 1 for b in dict.fromkeys(batch_sizes)
    ]


def fingerprint(result):
    if result is None:
        return "workspace-check-passed"
    content = {
        "masks": sorted(map(str, result.masks)),
        "git_paths": sorted(map(str, result.git_paths)),
        "counts": {
            k: v
            for k, v in result.metrics.items()
            if k != "by_root_mount" and not k.endswith("_ms")
        },
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def measure(root, empty, workers, batch_sizes, repeats, scenario, extension=None):
    if extension:
        sys.path.insert(0, str(extension))
    import resource

    import rust_backend

    from sandbox.linux_mounts import MountTable
    from sandbox.policy_scan import PolicyPlan, ScanRequest
    from sandbox.rust_policy import RustPolicyScanner
    from tools._internal.file_policy import PROTECTED_NAME_RULES

    combinations = configurations(workers, batch_sizes)
    if any(batch > 1 for _, batch in combinations) and not hasattr(
        rust_backend, "SCAN_BATCH_VERSION"
    ):
        raise RuntimeError(
            "Batch comparison requires rust-backend 0.6.0; use --batch-sizes 1 for old wheels"
        )
    if scenario == "workspace":

        def call():
            return None, rust_backend.profile_workspace(os.fsencode(root))
    else:
        reads = (root,) if scenario == "policy-read" else ()
        scanner = RustPolicyScanner()
        plan = PolicyPlan.compile(
            empty if reads else root, reads, (), name_rules=PROTECTED_NAME_RULES
        )
        request = ScanRequest(reads, True, MountTable.read().text)

        def call():
            result = scanner.scan(plan, request)
            return result, scanner.last_diagnostics

    reports = {f"w{w}-b{b}": {"samples": []} for w, b in combinations}
    baseline = None
    rng = random.Random(23)
    order = []
    # Warm every setting, then interleave settings in each measured round.
    for round_index in range(repeats + 1):
        current = list(combinations)
        if round_index:
            rng.shuffle(current)
        order.append([f"w{w}-b{b}" for w, b in current])
        for w, b in current:
            os.environ["AGENT_SCAN_WORKERS"] = str(w)
            os.environ["AGENT_SCAN_BATCH_SIZE"] = str(b)
            before = resource.getrusage(resource.RUSAGE_SELF)
            started = perf_counter()
            result, diagnostics = call()
            wall_ms = (perf_counter() - started) * 1000
            after = resource.getrusage(resource.RUSAGE_SELF)
            signature = fingerprint(result)
            if baseline is None:
                baseline = signature
            if signature != baseline:
                raise RuntimeError(
                    "Scan results changed across settings; tree may have changed during measurement"
                )
            if not round_index:
                continue
            report = reports[f"w{w}-b{b}"]
            report["samples"].append(
                {
                    "wall_ms": wall_ms,
                    "cpu_ms": (
                        (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
                    )
                    * 1000,
                    "voluntary_context_switches": after.ru_nvcsw - before.ru_nvcsw,
                    "involuntary_context_switches": after.ru_nivcsw - before.ru_nivcsw,
                }
            )
            report["diagnostics"] = diagnostics
            report["policy_metrics_last"] = getattr(result, "metrics", None)
    for report in reports.values():
        report["median"] = {
            key: round(statistics.median(s[key] for s in report["samples"]), 3)
            for key in report["samples"][0]
        }
        report["wall_ms_range"] = [
            min(s["wall_ms"] for s in report["samples"]),
            max(s["wall_ms"] for s in report["samples"]),
        ]
    return {
        "settings": reports,
        "round_order_including_warmup": order,
        "result_fingerprint": baseline,
        "batch_capability": getattr(rust_backend, "SCAN_BATCH_VERSION", None),
    }


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=["small-dirs", "wide"], default="small-dirs")
    parser.add_argument("--directories", type=positive)
    parser.add_argument("--files-per-directory", type=positive)
    parser.add_argument(
        "--root", type=Path, help="Read-only real tree; defaults to policy-read scenario"
    )
    parser.add_argument("--workers", type=int, nargs="+", choices=range(1, 9), default=[1, 2, 4])
    parser.add_argument(
        "--batch-sizes", type=int, nargs="+", choices=range(1, 65), default=[1, 16, 32, 64]
    )
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--scenarios", nargs="+", choices=["workspace", "policy", "policy-read"])
    parser.add_argument("--temporary-parent", type=Path)
    parser.add_argument("--extension-dir", type=Path)
    parser.add_argument("--child-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child-empty", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_root:
        print(
            json.dumps(
                measure(
                    args.child_root,
                    args.child_empty,
                    args.workers,
                    args.batch_sizes,
                    args.repeats,
                    args.scenarios[0],
                    args.extension_dir,
                )
            )
        )
        return
    if args.root and (args.directories or args.files_per_directory):
        parser.error("--root cannot be combined with generated-tree sizes")
    scenarios = args.scenarios or (
        ["policy-read"] if args.root else ["workspace", "policy", "policy-read"]
    )
    results = {}
    with tempfile.TemporaryDirectory(prefix="scan-parallel-", dir=args.temporary_parent) as temp:
        empty = Path(temp).resolve() / "empty"
        empty.mkdir()
        if args.root:
            root = args.root.expanduser().resolve(strict=True)
            if not root.is_dir():
                parser.error("--root must name a directory")
            tree = {"source": "existing-read-only", "root": str(root)}
        else:
            root = Path(temp).resolve() / "tree"
            small = args.shape == "small-dirs"
            tree = make_tree(
                root,
                args.directories or (8192 if small else 128),
                args.files_per_directory or (8 if small else 128),
                args.shape,
            )
            tree["source"] = "synthetic"
        for scenario in scenarios:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child-root",
                str(root),
                "--child-empty",
                str(empty),
                "--workers",
                *map(str, args.workers),
                "--batch-sizes",
                *map(str, args.batch_sizes),
                "--repeats",
                str(args.repeats),
                "--scenarios",
                scenario,
            ]
            if args.extension_dir:
                command += ["--extension-dir", str(args.extension_dir.resolve())]
            output = subprocess.run(command, check=True, capture_output=True, text=True)
            results[scenario] = json.loads(output.stdout)
    print(
        json.dumps(
            {
                "tree": tree,
                "repeats": args.repeats,
                "platform": sys.platform,
                "python": sys.version,
                "results": results,
                "scope": (
                    "warm, interleaved; includes thread lifetime; "
                    "RUSAGE_SELF includes all process threads; "
                    "excludes process tools, ledger and tree creation"
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
