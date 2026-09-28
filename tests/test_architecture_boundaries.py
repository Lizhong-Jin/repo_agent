"""Keep terminal presentation out of reusable and bootstrap-only services."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "package,forbidden",
    [
        ("agent", {"cli", "installer", "configuration"}),
        ("sandbox", {"cli", "configuration"}),
        ("configuration", {"cli", "agent", "tools", "sandbox", "installer"}),
        ("installer", {"cli", "agent", "tools", "sandbox", "llm"}),
        ("host_support", {"cli", "agent", "tools", "sandbox", "installer", "configuration"}),
    ],
)
def test_service_dependencies_do_not_point_back_to_terminal(package, forbidden):
    violations = []
    for path in (ROOT / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Include function-local imports: delaying an import does not remove coupling.
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            for module in modules:
                if module.split(".")[0] in forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
    assert not violations, "Forbidden dependency direction:\n" + "\n".join(violations)


@pytest.mark.parametrize("entry", ["setup", "uninstall", "release_install"])
def test_installer_entrypoints_work_without_site_packages_from_foreign_cwd(tmp_path, entry):
    # A task directory must not supply the implementation used by bootstrap scripts.
    for name in ("installer", "configuration", "host_support"):
        (tmp_path / f"{name}.py").write_text("raise RuntimeError('untrusted task module')\n")
    result = subprocess.run(
        [sys.executable, "-E", "-s", "-S", "-B", str(ROOT / "installer" / f"{entry}.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--help" in result.stdout


@pytest.mark.parametrize("target", ["macos-arm64", "windows-x86_64"])
@pytest.mark.parametrize("entry", ["setup", "uninstall", "release_install"])
def test_packaged_bootstrap_runs_without_source_or_site_packages(tmp_path, target, entry):
    import build_manifest

    bundle = tmp_path / "release"
    build_manifest.copy_files(ROOT, bundle, build_manifest.bootstrap_files(ROOT, target=target))
    workspace = tmp_path / "unrelated project"
    workspace.mkdir()
    for name in ("installer", "configuration", "host_support"):
        (workspace / f"{name}.py").write_text("raise RuntimeError('task module imported')\n")
    result = subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-S",
            "-B",
            str(bundle / "installer" / f"{entry}.py"),
            "--help",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--help" in result.stdout
