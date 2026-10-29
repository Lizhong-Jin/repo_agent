"""Authority, lifetime and external input contracts, without executing host probes."""

import json
import os
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import AgentRuntime
from cli.arguments import parse_arguments, validate_execution_options
from cli.execution_environment import open_execution_environment
from cli.task_controller import TaskController
from llm import LLMResponse, Message, ToolCall
from sandbox.permission_inputs import map_input_arguments, stage_permission_inputs
from sandbox.windows_workspace import copy_private_tree
from tools.access_policy import AccessPolicy
from tools.permissions import PermissionManager, PermissionTool, permission_tools


@pytest.fixture
def setup(tmp_path):
    workspace, external = tmp_path / "project", tmp_path / "input"
    workspace.mkdir()
    external.mkdir()
    (external / "sample.txt").write_text("input-data", encoding="utf-8")
    return PermissionManager(workspace, backend="native"), external


def read(manager, identity, path="sample.txt", operation="read_file"):
    return PermissionTool(manager, "read_external").execute(
        {"root_id": identity, "operation": operation, "arguments": {"reads": [{"path": path}]}}
    )


def test_strict_default_request_is_not_authority_and_can_be_denied(setup):
    manager, external = setup
    before = manager.profile
    result = PermissionTool(manager, "request_permission").execute(
        {"capability": "external_read", "path": str(external), "reason": "Read reference input"}
    )
    identity = result.data["request"]["id"]
    assert result.success and result.data["approved"] is False
    assert manager.profile == before
    assert not read(manager, identity).success
    manager.decide(identity, approve=False)
    assert manager.profile == before and not manager.pending


def test_approved_reads_are_bounded_and_revoke_immediately(setup):
    manager, external = setup
    request = manager.request("external_read", path=str(external), reason="inspect")
    manager.decide(request["id"], approve=True)
    grant = manager.profile.read_only_roots[0]
    assert read(manager, grant.id).success
    assert not read(manager, grant.id, "../project/anything").success
    assert not read(manager, grant.id, operation="write_file").success
    assert not (external / "anything").exists()
    manager.revoke(grant.id)
    assert not read(manager, grant.id).success
    assert manager.snapshot()["policy"] == "strict"


@pytest.mark.parametrize("name", [".env", ".ssh/id_rsa", ".git/config", "x.pem", ".agents/a"])
def test_sensitive_content_never_read_or_staged(setup, tmp_path, name):
    manager, external = setup
    secret = external / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("private")
    identity = manager.grant("external_read", str(external))
    assert not read(manager, identity, name).success
    paths, _ = stage_permission_inputs(
        SimpleNamespace(permissions=manager), tmp_path / "private" / "copies"
    )
    assert (Path(paths[identity]) / "sample.txt").read_text() == "input-data"
    assert not (Path(paths[identity]) / name).exists()


def test_runtime_protection_resolved_again_for_external_access(setup, tmp_path, monkeypatch):
    manager, external = setup
    identity = manager.grant("external_read", str(external))
    monkeypatch.setenv("AGENT_ENV_FILE", str(external / "sample.txt"))
    assert not read(manager, identity).success
    paths, _ = stage_permission_inputs(
        SimpleNamespace(permissions=manager), tmp_path / "private" / "copies"
    )
    assert not (Path(paths[identity]) / "sample.txt").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_links_cannot_launder_secrets(setup, tmp_path, kind):
    manager, external = setup
    secret = tmp_path / "secret"
    secret.write_text("private")
    alias = external / "alias"
    try:
        if kind == "symlink":
            alias.symlink_to(secret)
        else:
            os.link(secret, alias)
    except OSError:
        pytest.skip("Host does not permit creating this link type")
    identity = manager.grant("external_read", str(external))
    assert not read(manager, identity, "alias").success
    with pytest.raises((OSError, ValueError)):
        stage_permission_inputs(
            SimpleNamespace(permissions=manager), tmp_path / "private" / "copies"
        )


def test_root_replacement_invalidates_authority(setup, tmp_path):
    manager, external = setup
    identity = manager.grant("external_read", str(external))
    external.rename(tmp_path / "old")
    external.mkdir()
    (external / "sample.txt").write_text("replacement")
    assert not read(manager, identity).success
    with pytest.raises(ValueError, match="替换"):
        stage_permission_inputs(
            SimpleNamespace(permissions=manager), tmp_path / "private" / "copies"
        )


@pytest.mark.parametrize("path", ["workspace", "ancestor", "protected", "root", "relative"])
def test_reject_broad_protected_and_nonabsolute_grants(setup, tmp_path, path):
    manager, external = setup
    secret = external / ".ssh"
    secret.mkdir()
    candidate = {
        "workspace": manager.profile.workspace,
        "ancestor": tmp_path,
        "protected": secret,
        "root": Path(tmp_path.anchor),
        "relative": "input",
    }[path]
    with pytest.raises(ValueError):
        manager.grant("external_read", str(candidate))
    assert manager.snapshot()["policy"] == "strict"


@pytest.mark.parametrize(
    "backend,mode", [("local", "develop"), ("docker", "develop"), ("native", "review")]
)
def test_unsupported_combinations_fail_closed(tmp_path, backend, mode):
    manager = PermissionManager(tmp_path, backend=backend, access_mode=mode)
    with pytest.raises(ValueError):
        manager.request("external_read", path=str(tmp_path.parent), reason="read")
    parser, args = parse_arguments(
        ["--sandbox", backend, "--mode", mode, "--read-only-root", str(tmp_path.parent)]
    )
    with pytest.raises(SystemExit):
        validate_execution_options(parser, args)


@pytest.mark.parametrize("capability", ["full_access", "external_write", "network", "execute"])
def test_no_full_access_or_implicit_network_grants(setup, capability):
    manager, external = setup
    with pytest.raises(ValueError):
        manager.request(capability, path=str(external), reason="please")


def test_host_discovery_is_opt_in_metadata_only(setup, tmp_path, monkeypatch):
    manager, _ = setup
    binaries = tmp_path / "bin"
    binaries.mkdir()
    name = "probe.exe" if os.name == "nt" else "probe"
    program = binaries / name
    program.write_text("this is NOT executable program content")
    program.chmod(0o755)
    manager.search_path = ("", ".", str(binaries))
    tool = PermissionTool(manager, "get_host_environment")
    assert not tool.execute({"programs": [name]}).success
    manager.grant("host_discovery")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("Executed host probe"))
    result = tool.execute({"programs": [name, "missing"]})
    assert result.success and result.data["programs"] == {name: str(program), "missing": None}
    assert result.data["execution_authorized"] is False
    assert result.data["versions_probed"] is False
    assert "PATH" not in result.data
    assert not tool.execute({"programs": ["../probe"]}).success
    manager.revoke("host_discovery")
    assert not tool.execute({}).success


def test_stage_has_no_writeback_preserves_execution_and_bounds_copy(setup, tmp_path):
    manager, external = setup
    script = external / "run"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    identity = manager.grant("external_read", str(external))
    paths, mapping = stage_permission_inputs(
        SimpleNamespace(permissions=manager), tmp_path / "private" / "copy"
    )
    target = Path(paths[identity])
    if os.name == "posix":
        assert (target / "run").stat().st_mode & 0o111
    mapped = map_input_arguments([str(script), str(external / "sample.txt")], mapping)
    assert mapped == (str(target / "run"), str(target / "sample.txt"))
    assert map_input_arguments([f"print({str(script)!r})"], mapping) == (f"print({str(script)!r})",)
    (target / "sample.txt").write_text("private-copy")  # Host can change the private copy.
    assert (external / "sample.txt").read_text() == "input-data"
    with pytest.raises(ValueError, match="limit"):
        copy_private_tree(external, tmp_path / "bounded", limit=1)


class Model:
    def __init__(self, calls):
        self.calls, self.requests = calls, []

    def generate(self, request):
        self.requests.append(request)
        assert len(self.requests) == 1, "Permission request must stop before another model turn"
        return LLMResponse(
            "test", "test", Message("assistant", tool_calls=self.calls), "tool_calls"
        )


def test_request_pauses_and_blocks_remainder_of_same_batch(setup):
    manager, _ = setup
    calls = [
        ToolCall(
            "one", "request_permission", {"capability": "host_discovery", "reason": "locate Slurm"}
        ),
        ToolCall("two", "get_host_environment", {}),
    ]
    model = Model(calls)
    runtime = AgentRuntime(
        model, permission_tools(manager), access_policy=AccessPolicy(permissions=manager)
    )
    result = runtime.run("Find sbatch")
    assert result.status == "stopped" and result.resumable
    assert result.pause_reason == "permission_required"
    assert len(model.requests) == 1
    assert not manager.profile.host_discovery
    assert json.loads(result.history[-1].content)["error"]["code"] == "PERMISSION_PENDING"


def test_review_policy_admits_only_its_own_manager(setup):
    manager, _ = setup
    with pytest.raises(ValueError, match="match"):
        AccessPolicy("review", permissions=manager)
    manager = PermissionManager(manager.profile.workspace, backend="local", access_mode="review")
    other = PermissionManager(manager.profile.workspace, backend="local", access_mode="review")
    policy = AccessPolicy("review", permissions=manager)
    assert policy.allows(PermissionTool(manager, "get_permissions"))
    assert not policy.allows(PermissionTool(other, "get_permissions"))


def bind(conversation):
    manager = PermissionManager(conversation.store.project, backend="native")
    conversation.runtime.access_policy = replace(
        conversation.runtime.access_policy, permissions=manager
    )
    return manager, TaskController(
        conversation.runtime, conversation=conversation, write=lambda *_: None
    )


def test_user_approval_persisted_and_failed_save_rolls_back(open_conversation, monkeypatch):
    conversation = open_conversation()
    manager, controller = bind(conversation)
    request = manager.request("host_discovery", reason="find tools")
    save = conversation.checkpoint
    monkeypatch.setattr(
        conversation, "checkpoint", lambda **kw: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError, match="disk"):
        controller.command("/permissions approve " + request["id"])
    assert not manager.profile.host_discovery and request["id"] in manager.pending
    assert controller.persistence_blocked
    monkeypatch.setattr(conversation, "checkpoint", save)
    controller.command("/permissions approve " + request["id"])
    assert manager.profile.host_discovery and not manager.pending
    assert conversation.store.data["permissions"]["host_discovery"]
    controller.command("/permissions revoke all")
    assert manager.snapshot()["policy"] == "strict"


@pytest.mark.parametrize("blocked", ["active", "closing", "cleanup_blocked"])
def test_cannot_grant_while_busy_or_cleanup_uncertain(open_conversation, blocked):
    manager, controller = bind(open_conversation())
    request = manager.request("host_discovery", reason="find tools")
    setattr(controller, blocked, True)
    with pytest.raises(ValueError):
        controller.command("/permissions approve " + request["id"])
    assert not manager.profile.host_discovery


def test_new_session_revokes_and_snapshot_cannot_restore_authority(open_conversation):
    conversation = open_conversation()
    manager, _ = bind(conversation)
    manager.grant("host_discovery")
    conversation.checkpoint(strict=True)
    snapshot = conversation.store.data
    conversation.new_session()
    assert manager.snapshot()["policy"] == "strict"
    conversation.store.select(snapshot["session_id"])
    _, args = parse_arguments(["--sandbox", "local"])
    with ExitStack() as cleanup:
        env = open_execution_environment(
            args, conversation.store.project, conversation.store, None, cleanup
        )
    assert env.permissions.snapshot()["policy"] == "strict"


def test_explicit_cli_grant_only_authority_in_review(tmp_path):
    _, args = parse_arguments(["--mode", "review", "--allow-host-discovery"])
    with ExitStack() as cleanup:
        env = open_execution_environment(args, tmp_path, None, None, cleanup)
    assert env.permissions.profile.host_discovery
    assert env.native is None and env.sandbox is None


def test_backend_private_paths_are_not_overridable_by_read_grants(setup, tmp_path):
    manager, external = setup
    state = external / "windows-native"
    state.mkdir()
    (state / "recovery.json").write_text("private-state")
    identity = manager.grant("external_read", str(external))
    backend = SimpleNamespace(
        workspace=manager.profile.workspace, protected_paths=(state,), windows_state_root=state
    )
    manager.bind_backend(backend)
    assert not read(manager, identity, "windows-native/recovery.json").success
    paths, _ = stage_permission_inputs(backend, tmp_path / "call" / "inputs")
    assert not (Path(paths[identity]) / "windows-native").exists()
    with pytest.raises(ValueError):
        manager.grant("external_read", str(state))


def test_binding_rejects_pregranted_private_root(setup):
    manager, external = setup
    manager.grant("external_read", str(external))
    with pytest.raises(ValueError, match="保护"):
        manager.bind_backend(
            SimpleNamespace(workspace=manager.profile.workspace, windows_state_root=external)
        )


def test_input_cannot_copy_current_profile_into_itself(setup):
    manager, external = setup
    profile = external / "private-profile"
    profile.mkdir()
    (profile / "request.json").write_text("never expose call state")
    identity = manager.grant("external_read", str(external))
    paths, _ = stage_permission_inputs(SimpleNamespace(permissions=manager), profile / "inputs")
    assert not (Path(paths[identity]) / profile.name).exists()
    assert not read(manager, identity, "private-profile/request.json").success
    with pytest.raises(ValueError):
        manager.grant("external_read", str(profile))


@pytest.mark.skipif(os.name == "nt", reason="These are legal POSIX names, invalid Windows names")
def test_posix_input_preserves_legal_filenames_without_relaxing_windows_copy(setup, tmp_path):
    manager, external = setup
    (external / "a:b.txt").write_text("data")
    (external / "CON").write_text("data")
    identity = manager.grant("external_read", str(external))
    paths, _ = stage_permission_inputs(
        SimpleNamespace(permissions=manager), tmp_path / "call/inputs"
    )
    assert (Path(paths[identity]) / "a:b.txt").read_text() == "data"
    assert (Path(paths[identity]) / "CON").read_text() == "data"
    with pytest.raises(ValueError):
        copy_private_tree(external, tmp_path / "windows-copy")


def test_permission_pause_continue_requires_decision_without_replaying(open_conversation):
    from dataclasses import replace as replace_result

    from cli.task_execution import TaskOutcome

    conversation = open_conversation()
    manager, controller = bind(conversation)
    controller.enqueue("original request")
    ticket = controller.start_next()
    outcome = controller.execute(ticket)
    request = manager.request("host_discovery", reason="locate program")
    paused = replace_result(
        outcome.value, status="stopped", resumable=True, pause_reason="permission_required"
    )
    controller.finish(ticket, TaskOutcome("result", paused))
    assert controller.queue.paused
    with pytest.raises(ValueError, match="处理申请"):
        controller.command("/continue")
    controller.command("/permissions deny " + request["id"])
    assert controller.queue.paused and not manager.profile.host_discovery
    controller.command("/continue")
    next_ticket = controller.start_next()
    assert next_ticket["continuation_of"] == ticket["id"]
    continued = controller.execute(next_ticket)
    assert continued.value.status == "completed"
    controller.finish(next_ticket, continued)
    assert len(conversation.runtime.llm.requests) == 2
