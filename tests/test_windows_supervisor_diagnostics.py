"""Exercise the supervisor acceptance harness without a Windows SDK or kernel."""

import json

import pytest
import test_windows_isolation_kernel as kernel


class Supervisor:
    def __init__(self, *, exited=False):
        self.returncode = 1 if exited else None
        self.kill_error = None

    def poll(self):
        return self.returncode

    def kill(self):
        if self.kill_error:
            raise self.kill_error
        self.returncode = -9

    def communicate(self, timeout):
        return b"supervisor output", b"original startup error: WinError 203"


def harness(monkeypatch, tmp_path, *, ready, report=None, recovery_error=None):
    rows = [{"profile": "test-profile"}]
    if ready:
        rows.extend([{"leader": 123}, {"child": 456}, {"ready": 456}])
    (tmp_path / "supervisor.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    supervisor = Supervisor(exited=not ready)
    monkeypatch.setattr(kernel.subprocess, "Popen", lambda *args, **kwargs: supervisor)
    monkeypatch.setattr(kernel, "WindowsIsolationAPI", lambda: object())
    monkeypatch.setattr(kernel, "assert_dead", lambda pid: None)
    recovered = []

    def recover(root, api):
        recovered.append(root)
        if recovery_error:
            raise recovery_error
        return report or {"removed": [], "active": [], "retained": [], "failed": []}

    monkeypatch.setattr(kernel, "recover_profiles", recover)
    return supervisor, recovered


@pytest.mark.parametrize("cleanup", ["already_released", "failed_report", "raises"])
def test_startup_failure_keeps_supervisor_diagnostics(monkeypatch, tmp_path, cleanup):
    report = {"removed": [], "active": [], "retained": [], "failed": ["cleanup failed"]}
    _, recovered = harness(
        monkeypatch,
        tmp_path,
        ready=False,
        report=report if cleanup == "failed_report" else None,
        recovery_error=OSError("cleanup failed") if cleanup == "raises" else None,
    )
    with pytest.raises(AssertionError, match="Supervisor exited before readiness") as raised:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    notes = "\n".join(raised.value.__notes__)
    assert "WinError 203" in notes and "exit=1" in notes
    if cleanup != "already_released":
        assert "cleanup failed" in notes
    assert recovered == [tmp_path]


def test_early_exit_includes_probe_result(monkeypatch, tmp_path):
    harness(monkeypatch, tmp_path, ready=False)
    with (tmp_path / "supervisor.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"probe_result": {"exit_code": 91, "stderr": "error=5"}}) + "\n")
    with pytest.raises(AssertionError, match="Supervisor exited before readiness") as caught:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert "'exit_code': 91" in str(caught.value)
    assert "error=5" in str(caught.value)


def test_readiness_log_tolerates_windows_blank_lines(monkeypatch, tmp_path):
    report = {"removed": ["test-profile"], "active": [], "retained": [], "failed": []}
    supervisor, _ = harness(monkeypatch, tmp_path, ready=True, report=report)
    log = tmp_path / "supervisor.jsonl"
    log.write_bytes(log.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\r\n"))
    kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert supervisor.returncode == -9


@pytest.mark.parametrize("removed", [[], ["test-profile"]])
def test_successful_kill_still_requires_expected_profile_recovery(monkeypatch, tmp_path, removed):
    report = {"removed": removed, "active": [], "retained": [], "failed": []}
    supervisor, recovered = harness(monkeypatch, tmp_path, ready=True, report=report)
    if removed:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    else:
        with pytest.raises(AssertionError):
            kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert supervisor.returncode == -9
    assert recovered == [tmp_path]
    assert not (tmp_path / "supervisor.jsonl").exists()


def test_cleanup_error_without_primary_failure_still_fails(monkeypatch, tmp_path):
    error = OSError("recovery unavailable")
    harness(monkeypatch, tmp_path, ready=True, recovery_error=error)
    with pytest.raises(OSError) as raised:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert raised.value is error


def test_readiness_timeout_keeps_output_and_cleans_up(monkeypatch, tmp_path):
    supervisor, recovered = harness(monkeypatch, tmp_path, ready=False)
    supervisor.returncode = None
    clock = iter([0, 21])
    monkeypatch.setattr(kernel.time, "monotonic", lambda: next(clock))
    with pytest.raises(AssertionError, match="Supervisor did not become ready") as raised:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert "supervisor output" in "\n".join(raised.value.__notes__)
    assert supervisor.returncode == -9 and recovered == [tmp_path]


def test_multiple_cleanup_failures_preserve_original_exception(monkeypatch, tmp_path):
    supervisor, recovered = harness(
        monkeypatch, tmp_path, ready=True, recovery_error=OSError("recovery unavailable")
    )
    original = RuntimeError("original descendant verification failure")

    def verify(pid):
        raise original

    calls = 0

    def communicate(timeout):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise kernel.subprocess.TimeoutExpired("supervisor", timeout)
        return b"", b""

    monkeypatch.setattr(kernel, "assert_dead", verify)
    monkeypatch.setattr(supervisor, "communicate", communicate)
    with pytest.raises(RuntimeError) as raised:
        kernel.test_real_supervisor_death_kills_entire_job(tmp_path / "probe.exe", tmp_path)
    assert raised.value is original
    notes = "\n".join(original.__notes__)
    assert "Supervisor shutdown: TimeoutExpired" in notes
    assert "Profile recovery: OSError: recovery unavailable" in notes
    assert recovered == [tmp_path]
    assert not (tmp_path / "supervisor.jsonl").exists()
