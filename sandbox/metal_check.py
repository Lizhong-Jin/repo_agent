"""Explicit Metal and isolation validation: python -m sandbox.metal_check.

Runs only fixed probes in a disposable workspace under the actual native policy.
Uses the production Metal profile; does not require PyTorch, Rust or Xcode.
"""

import json
import platform
import sys
import tempfile
from pathlib import Path

from .macos_native import METAL_COMPUTE_RULES, MacOSNativeBackend
from .metal_probe import valid_report


def _checked_report(result, healthy):
    if result.exit_code != 0 or result.timed_out or result.stdout_truncated or not healthy:
        raise RuntimeError(
            "Metal validation failed inside Seatbelt (no unrestricted retry): "
            + (result.stderr + result.stdout)[-3000:]
        )
    report = json.loads(result.stdout)
    if not valid_report(report) or report.get("isolation_verified") is not True:
        raise RuntimeError("Metal validation returned an invalid report")
    return report


def _probe_code(backend, outside):
    # Run isolation checks in the same process AFTER Metal has initialized and
    # executed. The inherited native startup checks also exercise network/hardlinks.
    return f"""
import errno, json, os, pathlib, runpy, socket, subprocess, sys, tempfile
report = runpy.run_path({str(backend.runtime / "sandbox/metal_probe.py")!r})['probe']()
def blocked(action):
    try:
        action()
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), repr(error)
    else:
        raise AssertionError('sandbox restriction missing')
outside = pathlib.Path({str(outside)!r})
secret = pathlib.Path({str(backend.workspace / ".env")!r})
git = pathlib.Path({str(backend.workspace / ".git/config")!r})
for path in (outside, secret, git):
    blocked(lambda: path.read_text())
    blocked(lambda: path.write_text('changed'))
with socket.socket() as connection:
    blocked(lambda: connection.connect(('127.0.0.1', 9)))
with socket.socket() as listener:
    blocked(lambda: listener.bind(('127.0.0.1', 0)))
work = pathlib.Path({str(backend.workspace / "allowed.txt")!r})
work.write_text('allowed')
assert work.read_text() == 'allowed'
scratch = pathlib.Path(tempfile.gettempdir()) / 'metal-isolation-test'
scratch.write_text('allowed')
blocked(lambda: os.link(work, scratch.with_suffix('.link')))
alias = work.with_suffix('.alias')
alias.symlink_to(outside)
blocked(lambda: alias.read_text())
blocked(lambda: alias.write_text('changed'))
child_code = "import pathlib; pathlib.Path(" + repr(str(outside)) + ").read_text()"
child = subprocess.run([sys.executable, '-I', '-c', child_code], capture_output=True, timeout=5)
assert child.returncode != 0
report['isolation_verified'] = True
print(json.dumps(report))
"""


def check():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("Metal validation currently requires Apple Silicon macOS")
    with tempfile.TemporaryDirectory(prefix="repo-agent-metal-", dir="/private/tmp") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside.txt"
        outside.write_text("outside canary")
        secret = workspace / ".env"
        secret.write_text("METAL_TEST_CANARY=private")
        (workspace / ".git").mkdir()
        git = workspace / ".git/config"
        git.write_text("git canary")
        backend = MacOSNativeBackend(workspace, profile="metal")
        try:
            result = backend._run(
                [str(backend.python), "-I", "-c", _probe_code(backend, outside)],
                timeout=45,
                max_output_bytes=16 * 1024,
            )
            report = _checked_report(result, backend.healthy)
            if (
                outside.read_text() != "outside canary"
                or secret.read_text() != "METAL_TEST_CANARY=private"
                or git.read_text() != "git canary"
            ):
                raise RuntimeError("Isolation canary was modified")
            return {
                **report,
                "macos_version": platform.mac_ver()[0],
                "architecture": platform.machine(),
                "isolation": "seatbelt",
                "additional_permissions": list(METAL_COMPUTE_RULES),
            }
        finally:
            backend.close()


def main():
    try:
        report = check()
    except Exception as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"success": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
