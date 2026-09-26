"""Benchmark Linux policy generation against its pre-optimization algorithm.

No kernel isolation is required for scanning. --end-to-end additionally launches
real Linux native sandboxes, including startup/GPU probes. Existing inputs are
read only; synthetic inputs are created and removed in a temporary directory.
"""

import argparse
import errno
import json
import math
import os
import platform
import statistics
import sys
import tempfile
from pathlib import Path, PurePath
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox.linux_native import LinuxNativeBackend  # noqa: E402
from tools._internal.file_policy import (  # noqa: E402
    PROTECTED_NAMES,
    PROTECTED_SUFFIXES,
    runtime_protected_paths,
)


def is_protected_name(path):
    # Freeze the previous hot-path implementation as well as the traversal.
    return any(
        part.lower() in PROTECTED_NAMES or part.lower() == ".env"
        or part.lower().startswith(".env.") or part.lower().endswith(PROTECTED_SUFFIXES)
        for part in PurePath(path).parts
    )


def _outermost(paths):
    # Deliberately retain the old pruning algorithm in this reference.
    result = []
    for path in sorted(set(paths), key=lambda p: (len(p.parts), str(p))):
        if not any(path.is_relative_to(parent) for parent in result):
            result.append(path)
    return result


def reference_mount_policy(self, read_paths, *, git_read):
    masks, git_paths = [], []
    roots = _outermost([self.workspace, *read_paths])

    def inaccessible(error):
        # Read-only system trees can contain root-only directories (e.g.
        # WSL's /lib/modules/.../lost+found). Lack of list permission does
        # NOT prevent opening known filenames in an execute-only directory.
        # Hide the entire unscanned subtree, never simply skip its contents.
        if (not isinstance(error, PermissionError)
                or error.errno not in {errno.EACCES, errno.EPERM}
                or not error.filename):
            raise error
        path = Path(error.filename)
        if (not path.is_absolute() or not path.is_relative_to(root)
                or path.is_relative_to(self.workspace)
                or path.resolve(strict=True).is_relative_to(self.workspace)):
            raise error
        masks.append(path)

    def protected(path):
        if any(path == p or path.is_relative_to(p) for p in self.protected_paths):
            return True
        if path.name.lower() == ".git" and git_read:
            git_paths.append(path)
            return False
        # Check the leaf: a readable .git must still hide its credential/log children.
        return is_protected_name(path.name)

    for root in roots:
        if protected(root):
            masks.append(root)
            continue
        if not root.is_dir():
            continue
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
            for name in list(dirs) + names:
                path = Path(directory) / name
                if protected(path):
                    # Read-only toolchains often contain cert.pem symlinks. Hide
                    # their containing directory instead of following a mount
                    # target into another subtree. Workspace aliases fail closed.
                    masks.append(path.parent if path.is_symlink()
                                 and not path.is_relative_to(self.workspace) else path)
                    if name in dirs:
                        dirs.remove(name)
    masks = _outermost(masks)
    for path in (*masks, *git_paths):
        if path.is_symlink():
            raise ValueError(f"Linux native 受保护挂载点不能是符号链接：{path}")
    return masks, git_paths


def policy_backend(workspace, read_paths, protected_paths=()):
    backend = object.__new__(LinuxNativeBackend)
    backend.workspace = workspace
    backend.read_paths = tuple(read_paths)
    backend.protected_paths = tuple(protected_paths)
    return backend


def summarize(samples):
    return {"median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3)}


def benchmark(workspace, read_paths, *, repeats=7, git_read=False):
    backend = policy_backend(workspace, read_paths, runtime_protected_paths(workspace))
    def before():
        backend._check_workspace()
        return reference_mount_policy(backend, read_paths, git_read=git_read)

    scans = {
        "before": before,
        "after": lambda: backend._mount_policy(read_paths, git_read=git_read),
    }
    expected = scans["before"]()  # Warm filesystem caches, not a cold-cache benchmark.
    timings = {name: [] for name in scans}
    for repetition in range(repeats):
        order = list(scans) if repetition % 2 == 0 else list(reversed(scans))
        for name in order:
            started = perf_counter()
            actual = scans[name]()
            timings[name].append((perf_counter() - started) * 1000)
            if tuple(map(set, actual)) != tuple(map(set, expected)):
                raise AssertionError("Protection changed or input tree mutated during benchmark")
    result = {
        "scope": "workspace validation + policy scan; filesystem caches warm; "
                 "no cross-call scan cache",
        "host": platform.system(), "python": platform.python_version(), "repeats": repeats,
        "results": {name: summarize(samples) for name, samples in timings.items()},
        "last_policy": backend.last_policy_metrics,
    }
    result["speedup"] = round(statistics.median(timings["before"])
                              / statistics.median(timings["after"]), 2)
    return result


def synthetic_tree(base, files):
    workspace, system, interpreter = base / "project", base / "usr", base / "python"
    workspace.mkdir()
    for directory in (system / "lib", interpreter):
        directory.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('hello')\n")
    (workspace / ".env").write_text("benchmark secret")
    for index in range(files):
        root = system / "lib" if index % 3 == 0 else interpreter
        directory = root / f"package-{index % 200}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"module-{index}.py").touch()
    for root in (system / "lib", interpreter):
        (root / ".env.local").write_text("benchmark secret")
        (root / ".git/logs").mkdir(parents=True)
        (root / ".git/config").touch()
    alias = base / "lib"
    alias.symlink_to(system / "lib", target_is_directory=True)
    return workspace, (system, interpreter, alias)


def end_to_end(workspace, profile, repeats):
    if sys.platform != "linux":
        raise ValueError("--end-to-end requires a Linux host with usable native isolation")
    backend = LinuxNativeBackend(workspace, profile=profile)
    try:
        runs, samples = [], []
        for _ in range(repeats):
            outcome = backend.execute(workspace, "run_command", {"command": ["/usr/bin/true"]})
            if not outcome.success or outcome.data.get("exit_code") != 0:
                raise RuntimeError(str(outcome))
            runs.append(backend.last_tool_metrics)
            samples.append(backend.last_tool_metrics["total_ms"])
        return {"startup": backend.startup_metrics, "profile": profile,
                "gpu_enabled": backend.gpu is not None, "commands": summarize(samples),
                "runs": runs}
    finally:
        backend.close()


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--read-path", type=Path, action="append", default=[])
    parser.add_argument("--files", type=positive, default=30000)
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--git-read", action="store_true")
    parser.add_argument("--end-to-end", action="store_true")
    parser.add_argument("--sandbox-profile", choices=("standard", "auto", "cuda"), default="auto")
    args = parser.parse_args()
    if args.read_path and not args.workspace:
        parser.error("--read-path requires --workspace")
    if args.end_to_end and sys.platform != "linux":
        parser.error("--end-to-end requires Linux")

    def measure(workspace, read_paths):
        result = benchmark(workspace, read_paths, repeats=args.repeats, git_read=args.git_read)
        if args.end_to_end:
            result["end_to_end"] = end_to_end(workspace, args.sandbox_profile, args.repeats)
        return result

    if args.workspace:
        workspace = args.workspace.resolve(strict=True)
        if not workspace.is_dir():
            parser.error("--workspace must be a directory")
        # Preserve aliases: resolving these paths would hide duplicate scan costs.
        read_paths = tuple(path.absolute() for path in args.read_path)
        if any(not path.exists() for path in read_paths):
            parser.error("each --read-path must exist")
        result = measure(workspace, read_paths)
    else:
        with tempfile.TemporaryDirectory(prefix="linux-policy-benchmark-") as temporary:
            workspace, read_paths = synthetic_tree(Path(temporary).resolve(), args.files)
            result = measure(workspace, read_paths)
            result["synthetic_files"] = args.files
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
