"""Measure list/find/search and Rust preflight on the same synthetic tree.

Warm caches, complete file-tool calls, no OS sandbox startup. Save JSON before
and after a change using the same arguments and an otherwise idle machine.
The temporary tree is removed; this script never scans a real workspace.
"""

import argparse
import json
import platform
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_support.rust_filesystem import RustFilesystem  # noqa: E402
from tools._internal.file_access import FileAccess  # noqa: E402
from tools.filesystem import FindFileTool, ListFileTool, SearchFilesTool  # noqa: E402


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def benchmark(root, repeats, engines):
    results = {}
    expected = {}
    for engine in engines:
        native = RustFilesystem() if engine == "rust" else None
        operations = {
            "list": (ListFileTool(root), {"path": "large"}),
            "find": (FindFileTool(root, max_results=10_000), {"pattern": "**/*"}),
            "search": (SearchFilesTool(root, max_files_scanned=10_000), {"query": "absent"}),
        }
        for name, (tool, args) in operations.items():
            times = []
            for _ in range(repeats + 1):
                start = perf_counter()
                with FileAccess(root, directory_backend=native).activate():
                    result = tool.execute(args)
                times.append((perf_counter() - start) * 1000)
                if not result.success or result != expected.setdefault(name, result):
                    raise ValueError(f"{engine}/{name}: unsuccessful or unequal results")
            results[f"{engine}_{name}_ms"] = round(statistics.median(times[1:]), 3)
        if native:
            times = []
            for _ in range(repeats + 1):
                start = perf_counter()
                native.check_workspace(root)
                times.append((perf_counter() - start) * 1000)
            results["rust_preflight_ms"] = round(statistics.median(times[1:]), 3)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=positive, default=1500)
    parser.add_argument("--depth", type=positive, default=40)
    parser.add_argument("--repeats", type=positive, default=5)
    parser.add_argument("--engine", choices=["python", "rust", "both"], default="both")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="directory-io-benchmark-") as tmp:
        root = Path(tmp).resolve()
        large = root / "large"
        large.mkdir()
        for index in range(args.files):
            (large / f"{index:04}.txt").write_text("ordinary line\n" * 10)
        path = root / "deep"
        for _ in range(args.depth):
            path /= "d"
            path.mkdir(parents=True)
            for index in range(10):
                (path / f"{index}.txt").write_text("ordinary line\n" * 10)
        results = benchmark(
            root, args.repeats, ["python", "rust"] if args.engine == "both" else [args.engine]
        )
    print(
        json.dumps(
            {
                "host": platform.platform(),
                "python": platform.python_version(),
                "scope": "warm caches; complete file tools; excludes sandbox startup and LLM",
                "files": args.files + args.depth * 10,
                "depth": args.depth,
                "repeats": args.repeats,
                "budgets": {"list_entries": 200, "find_results": 10_000, "search_files": 10_000},
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
