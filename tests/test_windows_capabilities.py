"""Capability observations must not expand Windows isolation or mask unknown failures."""

import json

import pytest

from host_support.cancellation import CancellationContext, RunCancelled
from host_support.processes import ProcessResult
from sandbox.native_execution import NativeCleanupError
from sandbox.windows_capabilities import CapabilityUnavailable, assess_probe
from sandbox.windows_native import WindowsNativeBackend


def process(**values):
    return ProcessResult(
        **(
            {
                "exit_code": 1,
                "stdout": "",
                "stderr": "unexpected failure",
                "timed_out": False,
                "cleanup_error": None,
                "duration_ms": 1,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "cleanup_status": "confirmed",
            }
            | values
        )
    )


def backend(tmp_path):
    value = object.__new__(WindowsNativeBackend)
    value.workspace = tmp_path
    value.healthy = True
    value.last_cleanup = {}
    value.read_paths = ()
    value.capability_reports = {}
    return value


@pytest.mark.parametrize(
    "name,values",
    [
        (
            "git",
            {
                "exit_code": 128,
                "stderr": "fatal: Unable to read current working directory: Permission denied",
            },
        ),
        ("lsp", {"stderr": "Path.resolve: [WinError 5] Access is denied"}),
        ("lsp", {"exit_code": 0xC06D007E}),
    ],
)
def test_known_incompatibility_is_cached_and_does_not_poison_files(tmp_path, name, values):
    value = backend(tmp_path)
    calls = []
    value._probe_capability = lambda item: calls.append(item) or process(**values)
    for _ in range(2):
        with pytest.raises(CapabilityUnavailable) as error:
            value._require_capability(name)
        assert error.value.report["status"] == "unavailable"
        assert error.value.report["reason"] == "lpac_compatibility"
    assert calls == [name] and value.healthy
    (tmp_path / "hello.txt").write_text("readable")
    assert value._execute(tmp_path, "read_file", {"reads": [{"path": "hello.txt"}]}).success


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"timed_out": True},
        {"exit_code": 0, "stdout": "bad protocol"},
        {"stdout_truncated": True},
        {"stderr": "Permission denied unrelated to cwd"},
    ],
)
def test_unrecognized_failures_are_errors_not_supported_denials(tmp_path, values):
    assert assess_probe("git", process(**values), tmp_path)["status"] == "error"


@pytest.mark.parametrize("flag", ["timed_out", "stdout_truncated", "stderr_truncated"])
def test_incomplete_probe_is_not_accepted_as_known_incompatibility(tmp_path, flag):
    result = process(
        exit_code=128,
        stderr="fatal: Unable to read current working directory: Permission denied",
        **{flag: True},
    )
    assert assess_probe("git", result, tmp_path)["status"] == "error"


@pytest.mark.parametrize("name", ["git", "lsp"])
def test_success_requires_reference_result_and_is_cached(tmp_path, name):
    value = backend(tmp_path)
    stdout = (
        str(tmp_path)
        if name == "git"
        else json.dumps(
            {
                "success": True,
                "data": {"symbols": [{"name": "native_capability_example"}]},
            }
        )
    )
    calls = []
    value._probe_capability = lambda item: (
        calls.append(item) or process(exit_code=0, stdout=stdout, stderr="")
    )
    value._require_capability(name)
    value._require_capability(name)
    assert calls == [name]
    assert value.capability_reports[name]["status"] == "available"


@pytest.mark.parametrize("failure", ["status", "exception"])
def test_probe_cleanup_failure_disables_entire_backend(tmp_path, failure):
    value = backend(tmp_path)

    def probe(_):
        if failure == "exception":
            raise NativeCleanupError("unconfirmed")
        return process(cleanup_status="unknown")

    value._probe_capability = probe
    result = value._execute(tmp_path, "get_symbols", {"path": "sample.py"})
    assert result.error_code == "NATIVE_UNHEALTHY"
    assert not value.healthy
    denied = value._execute(tmp_path, "write_file", {"path": "file", "content": "x"})
    assert denied.error_code == "NATIVE_UNHEALTHY"
    assert not (tmp_path / "file").exists()


def test_cancellation_is_not_cached_as_incompatibility(tmp_path):
    value = backend(tmp_path)

    def probe(_):
        raise RunCancelled(CancellationContext())

    value._probe_capability = probe
    with pytest.raises(RunCancelled):
        value._require_capability("lsp")
    assert value.capability_reports == {}


def test_lsp_rejection_returns_actionable_result_without_worker_fallback(tmp_path):
    value = backend(tmp_path)
    value.capability_reports["lsp"] = {"status": "unavailable", "reason": "lpac_compatibility"}
    value._run = lambda *a, **kw: pytest.fail("No process may start after cached rejection")
    result = value._execute(tmp_path, "get_symbols", {"path": "sample.py"})
    assert result.error_code == "NATIVE_CAPABILITY_UNAVAILABLE"
    assert "--sandbox docker" in result.error and "--new-session" in result.error
    assert result.data["executor_healthy"] and not result.data["execution_allowed"]


def test_fixed_probes_ignore_project_arguments_and_reset_scope(tmp_path):
    value = backend(tmp_path)
    value._prepare_workspace = lambda: None
    calls = []

    def run(*args, **kwargs):
        calls.append((value._checking_capability, args, kwargs))
        return process()

    value._run = run
    value._probe_capability("git")
    value._probe_capability("lsp")
    assert calls[0][1] == (["git", "rev-parse", "--show-toplevel"],)
    assert calls[0][2]["git_read"]
    assert calls[1][2]["request"] == {"name": "get_symbols", "arguments": {"path": "sample.py"}}
    assert value._checking_capability is None


def test_probe_context_does_not_silently_switch_workspaces(tmp_path):
    value = backend(tmp_path)
    value._probe_capability = lambda _: pytest.fail("Wrong workspace must be rejected first")
    with pytest.raises(ValueError, match="工作区"):
        value._execute(tmp_path / "other", "get_symbols", {})


def test_report_bounds_diagnostics_and_marks_omissions(tmp_path):
    report = assess_probe("git", process(stdout="x" * 5000, stderr="y" * 5000), tmp_path)
    assert report["status"] == "error"
    for stream in ("stdout", "stderr"):
        assert len(report["diagnostics"][stream]) == 4096
        assert report["diagnostics"][f"{stream}_truncated"]


@pytest.mark.parametrize(
    "values", [{"cleanup_status": "unknown"}, {"cleanup_error": "profile release failed"}]
)
def test_valid_output_with_incomplete_cleanup_never_advertises_availability(tmp_path, values):
    result = process(exit_code=0, stdout=str(tmp_path), stderr="", **values)
    report = assess_probe("git", result, tmp_path)
    assert report["status"] == "error" and report["reason"] == "cleanup_unconfirmed"
