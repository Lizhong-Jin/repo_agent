import json
from types import SimpleNamespace

import pytest

from sandbox import build, environment


def reply(value="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=value, stderr=stderr)


def mock_daemon(monkeypatch, *, nvidia=True, arch="x86_64", probe=None, installed=True):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "info":
            return reply(
                json.dumps(
                    {
                        "OSType": "linux",
                        "Architecture": arch,
                        "Runtimes": {"nvidia": {}} if nvidia else {"runc": {}},
                    }
                )
            )
        if args[0] == "image":
            return reply("sha256:image", 0 if installed else 1)
        if args[0] == "rm":
            return reply(stderr="No such container", code=1)
        if args[0] == "run":
            return probe if probe is not None else reply("GPU 0: test (UUID: GPU-1234)\n")
        pytest.fail(str(args))

    monkeypatch.setattr(environment, "_run", run)
    return calls


def test_no_nvidia_runtime_uses_standard_without_pulling(monkeypatch):
    calls = mock_daemon(monkeypatch, nvidia=False, arch="aarch64")
    result = environment.detect_environment()
    assert result.profile == "standard"
    assert len(calls) == 1


def test_gpu_probe_uses_docker_daemon_and_existing_image(monkeypatch):
    # Host platform is deliberately irrelevant: the daemon can be on another host.
    calls = mock_daemon(monkeypatch)
    result = environment.detect_environment()
    assert result.profile == "cuda"
    command = next(c for c in calls if c[0] == "run")
    assert "--gpus" in command and "--runtime=nvidia" in command
    assert "--mount" not in command and "--privileged" not in command
    assert command[-2:] == [environment.DEFAULT_IMAGE, "-L"]
    assert calls[-1][:2] == ["rm", "-f"]


def test_first_gpu_setup_uses_small_probe_image(monkeypatch):
    calls = mock_daemon(monkeypatch, installed=False)
    assert environment.detect_environment().profile == "cuda"
    assert "ubuntu:22.04" in next(c for c in calls if c[0] == "run")


def test_no_devices_is_standard(monkeypatch):
    mock_daemon(monkeypatch, probe=reply("No devices were found", code=6))
    assert environment.detect_environment().profile == "standard"


def test_broken_runtime_does_not_silently_fall_back(monkeypatch):
    calls = mock_daemon(monkeypatch, probe=reply(code=1, stderr="driver/library version mismatch"))
    with pytest.raises(ValueError, match="探测失败"):
        environment.detect_environment()
    assert calls[-1][0] == "rm"


def test_probe_timeout_still_cleans_up(monkeypatch):
    calls = mock_daemon(monkeypatch)
    original = environment._run

    def fail(args, **kwargs):
        if args[0] == "run":
            raise ValueError("timeout")
        return original(args, **kwargs)

    monkeypatch.setattr(environment, "_run", fail)
    with pytest.raises(ValueError, match="timeout"):
        environment.detect_environment()
    assert calls[-1][0] == "rm"


def test_unsupported_gpu_architecture_is_explicit(monkeypatch):
    mock_daemon(monkeypatch, arch="aarch64")
    with pytest.raises(ValueError, match="x86_64"):
        environment.detect_environment()


def test_manual_standard_bypasses_gpu_probe(monkeypatch):
    calls = mock_daemon(monkeypatch)
    assert environment.detect_environment(profile="standard").profile == "standard"
    assert len(calls) == 1


def test_daemon_failure_is_not_cpu_detection(monkeypatch):
    monkeypatch.setattr(environment, "_run", lambda *a, **kw: reply(code=1))
    with pytest.raises(ValueError, match="无法连接"):
        environment.detect_environment()


@pytest.mark.parametrize(
    "actual, expected, allowed, succeeds",
    [
        ("cuda", "cuda", False, True),
        ("standard", "cuda", False, False),
        (None, "standard", False, False),
        (None, "cuda", True, True),
    ],
)
def test_image_profile_guard(monkeypatch, actual, expected, allowed, succeeds):
    labels = {environment.PROFILE_LABEL: actual} if actual else {}
    monkeypatch.setattr(environment, "_run", lambda *a, **kw: reply(json.dumps(labels)))
    if succeeds:
        environment.check_image_profile("image", expected, allow_unlabelled=allowed)
    else:
        with pytest.raises(ValueError, match="build-sandbox"):
            environment.check_image_profile("image", expected, allow_unlabelled=allowed)


@pytest.mark.parametrize(
    "profile, base",
    [
        ("cuda", environment.CUDA_BASE),
        ("standard", environment.STANDARD_BASE),
    ],
)
def test_one_dockerfile_and_tag_for_both_profiles(monkeypatch, tmp_path, profile, base):
    monkeypatch.setattr(build, "_docker", lambda: "docker")
    command = build.build_command(tmp_path, profile, environment.DEFAULT_IMAGE)
    assert command[command.index("-f") + 1] == str(tmp_path / "sandbox" / "Dockerfile")
    assert command[command.index("-t") + 1] == "repo-agent-sandbox:v1"
    assert f"BASE_IMAGE={base}" in command
    assert f"SANDBOX_PROFILE={profile}" in command
    assert command[-1] == str(tmp_path)


@pytest.mark.parametrize("profile, expected_gpu", [("standard", None), ("cuda", "all")])
def test_normal_cli_uses_detected_profile_without_extra_flags(
    tmp_path, monkeypatch, profile, expected_gpu
):
    from cli import main as cli

    monkeypatch.setattr("llm.LLMClient.get_context_limit", lambda self, **kw: None)
    policies = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-key")
    monkeypatch.setattr("sys.argv", ["repo-agent", "--sandbox", "docker", "--model", "mock", "--root", str(tmp_path)])
    monkeypatch.setattr(
        "cli.execution_environment.detect_environment",
        lambda **kw: environment.DockerEnvironment(profile, "test", "x86_64"),
    )
    monkeypatch.setattr("cli.execution_environment.check_image_profile", lambda *a, **kw: None)

    def session(root, policy, **kwargs):
        policies.append(policy)
        def tools(*, writeback_mode):
            assert writeback_mode == "manual"
            return []
        return SimpleNamespace(workspace=root, directory=root, tools=tools)

    monkeypatch.setattr("cli.execution_environment.SandboxSession", session)
    monkeypatch.setattr("cli.application.run_interactive", lambda *a, **kw: None)
    cli.main()
    assert policies[0].gpus == expected_gpu
    assert policies[0].image == "repo-agent-sandbox:v1"
