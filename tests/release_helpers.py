"""Real installation acceptance helpers; never used in product code."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def offline_prefix():
    if os.getenv("REQUIRE_RELEASE_ACCEPTANCE") != "1":
        return []
    if sys.platform == "darwin":
        return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]
    if sys.platform == "linux":
        return [
            "bwrap",
            "--unshare-net",
            "--bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--",
        ]
    raise ValueError("Kernel network guard is only configured for Linux/macOS")


def installed_flow(run, python, tmp_path, *, rust=False, native_run=None):
    if os.getenv("REQUIRE_RELEASE_ACCEPTANCE") != "1":
        return
    if rust:
        run(
            [
                python,
                "-I",
                "-c",
                "import rust_backend; assert rust_backend.API_VERSION == 1; "
                "assert rust_backend.FILESYSTEM_API_VERSION == 2",
            ]
        )
    run = native_run or run
    root = tmp_path / "installed delivery"
    for phase in ("prepare", "resume"):
        run(
            [
                python,
                "-I",
                ROOT / "scripts/installed_acceptance.py",
                "--root",
                root,
                "--phase",
                phase,
            ]
        )
