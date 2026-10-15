"""Run mandatory acceptance profiles and gate exact, already tested release assets."""

import argparse
import ast
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64", "windows-x86_64")
JOBS = {"source", "rust", "wheels", "docker", "native", "packages"}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity():
    return {
        "commit": os.environ.get("GITHUB_SHA")
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
        "version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
    }


def host_target():
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}[platform.system()]
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine().lower()
    ]
    return f"{system}-{arch}"


def selectors(profile, target):
    if target not in TARGETS:
        raise ValueError("Unknown release target")
    permission_file = "tests/test_permission_kernel.py"
    permission_tests = (
        [
            f"{permission_file}::{node.name}"
            for node in ast.parse((ROOT / permission_file).read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        if profile in {"native", "permissions"}
        else []
    )
    if profile == "permissions":
        return permission_tests
    if profile == "package":
        if target.startswith("windows"):
            return [
                "tests/test_windows_release.py::test_real_windows_release_survives_download_removal_and_uninstalls",
                "tests/test_release_upgrade.py::test_real_previous_release_upgrade",
            ]
        return [
            "tests/test_release_distribution.py::test_real_release_survives_download_removal",
            "tests/test_release_upgrade.py::test_real_previous_release_upgrade",
        ]
    if profile != "native" or target.startswith("windows"):
        raise ValueError("Unsupported acceptance profile")
    file, marker = (
        ("tests/test_linux_native.py", "REAL_LINUX")
        if target.startswith("linux")
        else ("tests/test_native_sandbox.py", "REAL_NATIVE")
    )
    # Keep every real kernel test mandatory. TypeScript is a separate optional capability.
    return permission_tests + [
        f"{file}::{node.name}"
        for node in ast.parse((ROOT / file).read_text()).body
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Name) and d.id == marker for d in node.decorator_list)
        and node.name != "test_real_linux_node_child_process_and_typescript"
    ]


def junit_results(path, required):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("No acceptance tests executed")
    rows = [
        {
            "name": case.attrib["name"],
            "class": case.attrib.get("classname", ""),
            "state": next(
                (s for s in ("failure", "error", "skipped") if case.find(s) is not None), "passed"
            ),
        }
        for case in cases
    ]
    if any(row["state"] != "passed" for row in rows) or list(root.iter("error")):
        raise ValueError("Mandatory acceptance contains failed, skipped, or errored tests")
    observed = [row["class"] + "." + row["name"] for row in rows]
    expected = set()
    for selector in required:
        base = selector.replace(".py::", ".").replace("/", ".")
        suffixes = ("",)
        if selector.endswith("::test_real_release_survives_download_removal"):
            suffixes = ("[False]", "[True]")
        elif selector.endswith(
            "::test_real_unreadable_system_directory_is_hidden_on_all_mount_paths"
        ):
            suffixes = ("[0]", "[73]")
        expected.update(base + suffix for suffix in suffixes)
    if set(observed) != expected or len(observed) != len(expected):
        raise ValueError(
            "Mandatory acceptance test identities or parameter cases missing/duplicated"
        )
    return rows


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(profile, target, output, archive=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": 1,
        **identity(),
        "profile": profile,
        "target": target,
        "status": "failed",
        "environment": {"platform": platform.platform(), "python": sys.version},
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        if host_target() != target:
            raise ValueError("Acceptance must run on the package's actual platform/architecture")
        required = selectors(profile, target)
        env = os.environ.copy()
        if profile in {"native", "permissions"}:
            startup_report = output / "startup.jsonl"
            startup_report.unlink(missing_ok=True)
            env["NATIVE_STARTUP_REPORT"] = str(startup_report)
            env[
                "RUN_WINDOWS_ISOLATION_TESTS"
                if target.startswith("windows")
                else "RUN_SANDBOX_LINUX_TESTS"
                if target.startswith("linux")
                else "RUN_SANDBOX_NATIVE_TESTS"
            ] = "1"
        else:
            if archive is None:
                raise ValueError("Package acceptance requires the exact archive")
            archive = Path(archive).resolve(strict=True)
            suffix = ".zip" if target.startswith("windows") else ".tar.gz"
            expected = f"repo-agent-{report['version']}-{target}{suffix}"
            if archive.name != expected:
                raise ValueError("Archive version/platform filename mismatch")
            checksum = sha256(archive)
            if (
                archive.with_name(archive.name + ".sha256").read_text().strip()
                != f"{checksum}  {archive.name}"
            ):
                raise ValueError("Archive checksum mismatch")
            # Preserve the exact tested bytes, with the report, before executing installation.
            destination = output / archive.name
            if destination != archive:
                shutil.copyfile(archive, destination)
            archive = destination
            report["asset"] = {"name": archive.name, "sha256": checksum}
            env[
                "REPO_AGENT_WINDOWS_ARCHIVE"
                if target.startswith("windows")
                else "REPO_AGENT_TEST_ARCHIVE"
            ] = str(archive)
            previous = Path(env["REPO_AGENT_PREVIOUS_ARCHIVE"]).resolve(strict=True)
            report["upgrade_from"] = {"name": previous.name, "sha256": sha256(previous)}
            env["REQUIRE_RELEASE_ACCEPTANCE"] = "1"
        junit = output / "tests.xml"
        junit.unlink(missing_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v",
                "-ra",
                "--durations=15",
                *required,
                f"--junitxml={junit}",
            ],
            cwd=ROOT,
            env=env,
            timeout=1800,
        )
        report["exit_code"] = result.returncode
        report["tests"] = junit_results(junit, required)
        report["junit_sha256"] = sha256(junit)
        if result.returncode:
            raise ValueError("Acceptance process failed")
        if archive is not None:
            if sha256(archive) != report["asset"]["sha256"]:
                raise ValueError("Archive changed during acceptance")
            if sha256(previous) != report["upgrade_from"]["sha256"]:
                raise ValueError("Upgrade baseline changed during acceptance")
        report["status"] = "passed"
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    write_json(output / "acceptance.json", report)
    return report["status"] == "passed"


def gate(directory, output, needs, expected_identity):
    if set(needs) != JOBS or any(v.get("result") != "success" for v in needs.values()):
        raise ValueError("Required workflow failed, was skipped/cancelled, or is missing")
    expected = {("package", t) for t in TARGETS} | {
        ("native", t) for t in TARGETS if not t.startswith("windows")
    }
    found, assets, evidence, baselines = set(), [], [], set()
    for path in sorted(Path(directory).rglob("acceptance.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        key = record["profile"], record["target"]
        if key not in expected or key in found:
            raise ValueError("Duplicate or unexpected acceptance profile")
        if (
            record.get("schema") != 1
            or record.get("status") != "passed"
            or record.get("exit_code") != 0
        ):
            raise ValueError("Unsuccessful acceptance record")
        if any(record.get(k) != v for k, v in expected_identity.items()):
            raise ValueError("Acceptance belongs to another commit, version, run or attempt")
        junit = path.parent / "tests.xml"
        if (
            sha256(junit) != record["junit_sha256"]
            or junit_results(junit, selectors(*key)) != record["tests"]
        ):
            raise ValueError("Test evidence differs from accepted results")
        if key[0] == "package":
            baseline = record["upgrade_from"]
            if not baseline["name"].startswith("repo-agent-") or len(baseline["sha256"]) != 64:
                raise ValueError("Missing upgrade baseline identity")
            asset = record["asset"]
            suffix = ".zip" if key[1].startswith("windows") else ".tar.gz"
            ending = f"-{key[1]}{suffix}"
            if not baseline["name"].endswith(ending):
                raise ValueError("Upgrade baseline platform mismatch")
            baselines.add(baseline["name"][len("repo-agent-") : -len(ending)])
            if asset["name"] != f"repo-agent-{expected_identity['version']}-{key[1]}{suffix}":
                raise ValueError("Invalid release asset identity")
            source = path.parent / asset["name"]
            if source.is_symlink() or sha256(source) != asset["sha256"]:
                raise ValueError("Release asset changed after acceptance")
            assets.append((source, asset))
        evidence.append(
            {
                "profile": key[0],
                "target": key[1],
                "report_sha256": sha256(path),
                "upgrade_from": record.get("upgrade_from"),
                "junit_sha256": record["junit_sha256"],
                "tests": len(record["tests"]),
            }
        )
        found.add(key)
    if found != expected:
        raise ValueError(f"Missing platform evidence: {sorted(expected - found)}")
    if len(baselines) != 1:
        raise ValueError("Platform reports used different upgrade baseline versions")
    output = Path(output)
    if output.exists():
        raise ValueError("Approved output must not already exist")
    output.mkdir(parents=True)
    for source, asset in assets:
        destination = output / asset["name"]
        shutil.copyfile(source, destination)
        if sha256(destination) != asset["sha256"]:
            raise ValueError("Release asset changed while copying")
        destination.with_name(destination.name + ".sha256").write_text(
            f"{asset['sha256']}  {asset['name']}\n"
        )
    manifest = {
        "schema": 1,
        **expected_identity,
        "status": "passed",
        "jobs": needs,
        "upgrade_version": next(iter(baselines)),
        "assets": [asset for _, asset in assets],
        "evidence": evidence,
    }
    write_json(output / "release-acceptance.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--profile", choices=("native", "package", "permissions"), required=True)
    runner.add_argument("--target", choices=TARGETS, required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--archive", type=Path)
    final = commands.add_parser("gate")
    final.add_argument("--input", type=Path, required=True)
    final.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        return 0 if run(args.profile, args.target, args.output, args.archive) else 1
    expected = identity()
    if (
        os.environ.get("GITHUB_REF_TYPE") == "tag"
        and os.environ.get("GITHUB_REF_NAME") != "v" + expected["version"]
    ):
        raise ValueError("Release tag must match project version")
    gate(args.input, args.output, json.loads(os.environ["RELEASE_NEEDS_JSON"]), expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
