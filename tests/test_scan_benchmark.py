"""Benchmark fixtures must exercise small directories and compare equivalent scans."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.benchmark_scan_parallel import configurations, make_tree


def test_small_tree_is_nested_uneven_and_deterministic(tmp_path):
    left = make_tree(tmp_path / "left", 128, 8, "small-dirs")
    right = make_tree(tmp_path / "right", 128, 8, "small-dirs")
    assert left == right and left["directories"] == 129
    assert 7 < left["files"] / 128 < 9
    sizes = [len(files) for _, _, files in os.walk(tmp_path / "left")]
    assert min(sizes) < max(sizes)
    assert (tmp_path / "left/package-000000/package-000008").is_dir()
    assert configurations([1, 2, 2, 4], [1, 32]) == [(1, 1), (2, 1), (2, 32), (4, 1), (4, 32)]


def test_real_tree_benchmark_is_read_only_and_records_interleaved_cpu_metrics(tmp_path):
    native = pytest.importorskip("rust_backend")
    if not hasattr(native, "SCAN_BATCH_VERSION"):
        pytest.skip("Requires rust-backend 0.6.0")
    root = tmp_path / "real"
    make_tree(root, 32, 8, "small-dirs")
    before = {
        str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_mtime_ns) for p in root.rglob("*")
    }
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark_scan_parallel.py"
    output = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--workers",
            "2",
            "--batch-sizes",
            "1",
            "32",
            "--repeats",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.stdout)
    assert list(report["results"]) == ["policy-read"]
    scenario = report["results"]["policy-read"]
    assert len(scenario["round_order_including_warmup"]) == 3
    assert set(scenario["settings"]) == {"w1-b1", "w2-b1", "w2-b32"}
    for settings in scenario["settings"].values():
        assert len(settings["samples"]) == 2
        assert settings["median"]["cpu_ms"] >= 0
        assert settings["median"]["voluntary_context_switches"] >= 0
        assert settings["policy_metrics_last"]["complete"] is True
    after = {
        str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_mtime_ns) for p in root.rglob("*")
    }
    assert after == before
