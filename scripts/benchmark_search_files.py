"""Compare scoped directory reads with a per-entry reopening reference.

Both paths use the CURRENT search algorithm and metadata checks. This isolates
handle reuse; it is not a benchmark of every change from a previous release.
Existing workspaces are read only. Synthetic trees are temporary and removed.
"""

import argparse
import json
import platform
import statistics
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_support import filesystem as file_service  # noqa: E402
from tools._internal.file_access import FileAccess  # noqa: E402
from tools.filesystem import SearchFilesTool  # noqa: E402


class ReopeningDirectory:
    def __init__(self, access, path):
        self.access, self.path = access, path

    def names(self):
        return [entry.name for entry in self.access.iterdir(self.path)]

    def stat(self, name):
        return self.access.stat(self.path / name)

    def open_read(self, name):
        return self.access.open_read(self.path / name)


class ReopeningAccess(FileAccess):
    @contextmanager
    def read_directory(self, path):
        yield ReopeningDirectory(self, Path(path))


def benchmark(root, *, repeats=7, query="__benchmark_absent__", glob=None):
    tool = SearchFilesTool(root, max_files_scanned=1_000_000)
    arguments = {"query": query, **({"glob": glob} if glob else {})}
    engines = {"reopening_reference": ReopeningAccess, "scoped": FileAccess}

    def search(engine):
        with engine(root).activate():
            return tool.execute(arguments)

    expected = search(FileAccess)
    if not expected.success:
        raise ValueError(f"Search failed: {expected.error_code}")
    for engine in engines.values():
        assert search(engine) == expected
    samples = {name: [] for name in engines}
    for repetition in range(repeats):
        order = list(engines) if repetition % 2 == 0 else list(reversed(engines))
        for name in order:
            started = perf_counter()
            actual = search(engines[name])
            samples[name].append((perf_counter() - started) * 1000)
            if actual != expected:
                raise ValueError("Search results differ or workspace changed during benchmark")

    results = {}
    original_open = file_service.open_directory
    for name, engine in engines.items():
        opens = 0

        def opened(*args, **kwargs):
            nonlocal opens
            opens += 1
            return original_open(*args, **kwargs)

        with patch.object(file_service, "open_directory", opened):
            search(engine)
        results[name] = {
            "median_ms": round(statistics.median(samples[name]), 3),
            "directory_opens": opens,
        }
    return {
        "host": platform.system(),
        "python": platform.python_version(),
        "scope": "native file service only; warm caches; same search algorithm; no OS sandbox",
        "repeats": repeats,
        "files_scanned": expected.data["files_scanned"],
        "truncated": expected.data["truncated"],
        "results": results,
    }


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--files", type=positive, default=2000)
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--query", default="__benchmark_absent__")
    parser.add_argument("--glob")
    args = parser.parse_args()

    def measure(root):
        return benchmark(root, repeats=args.repeats, query=args.query, glob=args.glob)

    if args.workspace:
        result = measure(args.workspace.resolve(strict=True))
    else:
        with tempfile.TemporaryDirectory(prefix="search-scan-benchmark-") as temporary:
            root = Path(temporary).resolve()
            for index in range(args.files):
                path = root / "src" / f"package-{index % 20}" / f"module-{index}.py"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ordinary source line\n" * 20)
            result = measure(root)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
