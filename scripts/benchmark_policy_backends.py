"""Compare installed Python/Rust scanners on the same trees, without OS sandbox launch."""

import argparse
import json
import platform
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.linux_mounts import MountTable  # noqa: E402
from sandbox.policy_scan import PolicyPlan, ScanRequest  # noqa: E402
from sandbox.policy_scanners import create_policy_scanner  # noqa: E402
from scripts.benchmark_linux_policy import positive, summarize, synthetic_tree  # noqa: E402
from tools._internal.file_policy import PROTECTED_NAME_RULES, runtime_protected_paths  # noqa: E402


def benchmark(workspace, read_paths, repeats=7):
    plan = PolicyPlan.compile(
        workspace,
        tuple(read_paths),
        tuple(runtime_protected_paths(workspace)),
        name_rules=PROTECTED_NAME_RULES,
    )
    engines = {name: create_policy_scanner(name) for name in ("python", "rust")}
    samples = {name: [] for name in engines}
    metrics = {}
    expected = None
    for repetition in range(repeats + 1):
        order = list(engines) if repetition % 2 == 0 else list(reversed(engines))
        for name in order:
            call = ScanRequest(tuple(read_paths), False, MountTable.read().text)
            started = perf_counter()
            actual = engines[name].scan(plan, call)
            duration = (perf_counter() - started) * 1000
            signature = actual.masks, actual.git_paths
            if expected is not None and signature != expected:
                raise AssertionError("Protection differs or tree changed during benchmark")
            expected = signature
            if repetition:
                samples[name].append(duration)
            metrics[name] = actual.metrics
    return {
        "scope": "warm caches; fresh scans; no OS sandbox launch; native adapter included",
        "host": platform.system(),
        "python": platform.python_version(),
        "repeats": repeats,
        "results": {name: summarize(values) for name, values in samples.items()},
        "last_policy": metrics,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--read-path", type=Path, action="append", default=[])
    parser.add_argument("--files", type=positive, default=20000)
    parser.add_argument("--repeats", type=positive, default=7)
    args = parser.parse_args()
    if args.workspace:
        result = benchmark(
            args.workspace.resolve(strict=True),
            tuple(p.absolute() for p in args.read_path),
            args.repeats,
        )
    else:
        if args.read_path:
            parser.error("--read-path requires --workspace")
        with tempfile.TemporaryDirectory(prefix="policy-backends-") as temporary:
            workspace, reads = synthetic_tree(Path(temporary).resolve(), args.files)
            result = benchmark(workspace, reads, args.repeats)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
