import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy

import pytest

from agent import AgentRuntime
from agent.skills import LoadSkillTool, SkillRegistry
from agent.skills.registry import MAX_SKILL_BYTES, SkillError
from agent.Tracing import Tracer
from cli.interactive import run_interactive
from llm import LLMResponse, Message, ToolCall
from sandbox import SandboxSession
from tools import ReadFileTool, create_default_tools


def write_skill(root, name="sample", body="Read the failing test before editing.", header=None):
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if header is None:
        header = f"name: {name}\ndescription: Diagnose a failing widget test."
    path.write_text(f"---\n{header}\n---\n{body}\n", encoding="utf-8")
    return path


def reply(text="done", calls=()):
    return LLMResponse(
        "test", "test", Message("assistant", text, calls), "tool_calls" if calls else "stop"
    )


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(deepcopy(request))
        return next(self.responses)


def test_builtin_and_project_catalog_is_metadata_only(tmp_path):
    write_skill(tmp_path, body="Unique private instruction body")
    registry = SkillRegistry(tmp_path)
    assert registry.get("debug-and-fix").base_path is None
    assert registry.get("sample").base_path == "skills/sample"
    assert "Unique private instruction body" not in registry.prompt()
    assert "$sample" in registry.describe()
    assert registry.get("sample").source == "skills/sample/SKILL.md"


def test_yaml_multiline_and_session_snapshot(tmp_path):
    path = write_skill(
        tmp_path, header="name: sample\ndescription: >\n  Diagnose\n  widget failures."
    )
    registry = SkillRegistry(tmp_path, include_builtin=False)
    skill = registry.get("sample")
    assert skill.description == "Diagnose widget failures."
    path.write_text("broken later")
    assert registry.get("sample") == skill
    assert LoadSkillTool(registry).execute({"name": "sample"}).data == skill.payload()
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path)


@pytest.mark.parametrize(
    "header,body",
    [
        ("name: sample", "body"),
        ("name: wrong\ndescription: desc", "body"),
        ("name: sample\ndescription: []", "body"),
        ("name: sample\nname: sample\ndescription: desc", "body"),
        ("name: sample\ndescription: desc", ""),
        ("name: sample\ndescription: desc", "bad\x00text"),
        ("name: sample\ndescription: " + "x" * 1025, "body"),
        ("name: sample\ndescription: !!python/object/apply:os.system ['exit 1']", "body"),
        ("name: [invalid yaml", "body"),
    ],
)
def test_invalid_skill_is_actionable_without_echoing_contents(tmp_path, header, body):
    write_skill(tmp_path, header=header, body=body)
    with pytest.raises(SkillError, match="skills/sample/SKILL.md") as failure:
        SkillRegistry(tmp_path)
    assert "exit 1" not in str(failure.value)


def test_duplicate_builtin_is_rejected(tmp_path):
    write_skill(tmp_path, "debug-and-fix")
    with pytest.raises(SkillError, match="名称冲突"):
        SkillRegistry(tmp_path)


def test_catalog_and_file_limits(tmp_path, monkeypatch):
    write_skill(tmp_path, body="x" * MAX_SKILL_BYTES)
    with pytest.raises(SkillError, match="字节"):
        SkillRegistry(tmp_path)
    write_skill(tmp_path)
    monkeypatch.setattr("agent.skills.registry.MAX_CATALOG_CHARS", 10)
    with pytest.raises(SkillError, match="目录超过"):
        SkillRegistry(tmp_path)


@pytest.mark.parametrize("kind", ["root-link", "folder-link", "file-link", "hardlink", "fifo"])
def test_links_and_special_files_are_rejected(tmp_path, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    path = write_skill(outside)
    target = root / "skills" / "sample" / "SKILL.md"
    if kind == "root-link":
        (root / "skills").symlink_to(outside / "skills", target_is_directory=True)
    elif kind == "folder-link":
        (root / "skills").mkdir()
        target.parent.symlink_to(path.parent, target_is_directory=True)
    else:
        target.parent.mkdir(parents=True)
        if kind == "file-link":
            target.symlink_to(path)
        elif kind == "hardlink":
            os.link(path, target)
        else:
            os.mkfifo(target)
    with pytest.raises(SkillError):
        SkillRegistry(root)


def test_protected_config_path_stays_protected(tmp_path, monkeypatch):
    path = write_skill(tmp_path)
    monkeypatch.setenv("AGENT_ENV_FILE", str(path))
    with pytest.raises(SkillError, match="受保护"):
        SkillRegistry(tmp_path)


@pytest.mark.parametrize(
    "args", [{}, {"name": 1}, {"name": []}, {"name": ""}, {"name": "sample", "path": "/etc/passwd"}]
)
def test_load_arguments_are_checked(tmp_path, args):
    result = LoadSkillTool(SkillRegistry(tmp_path)).execute(args)
    assert not result.success and result.error_code == "INVALID_ARGUMENTS"


def test_unknown_skill_and_path_traversal_cannot_read_files(tmp_path):
    tool = LoadSkillTool(SkillRegistry(tmp_path))
    for name in ["unknown", "../outside", "/etc/passwd"]:
        assert tool.execute({"name": name}).error_code == "UNKNOWN_SKILL"


def test_model_selects_loads_and_continues_through_existing_loop(tmp_path):
    write_skill(tmp_path, body="Unique regression workflow")
    model = Model(reply(calls=[ToolCall("s1", "load_skill", {"name": "sample"})]), reply())
    runtime = AgentRuntime(model, skills=SkillRegistry(tmp_path))
    result = runtime.run("Widget test fails; investigate.")
    assert result.status == "completed"
    assert "Unique regression workflow" not in str(model.requests[0].messages)
    payload = json.loads(model.requests[1].messages[-1].content)
    assert payload["data"]["instructions"] == "Unique regression workflow"
    assert result.stats.skill_loads[0]["invocation"] == "model"
    assert result.stats.tool_calls[0].status == "success"


def test_explicit_load_on_followup_preserves_history_and_does_not_repeat_catalog(tmp_path):
    write_skill(tmp_path, body="Unique explicit instructions")
    model = Model(reply(), reply(), reply())
    runtime = AgentRuntime(
        model, skills=SkillRegistry(tmp_path), system_prompt="Custom instructions"
    )
    first = runtime.run("Describe the project.")
    saved = deepcopy(first.history)
    second = runtime.run("$sample $sample fix it", history=first.history)
    assert first.history == saved
    assert model.requests[1].messages[0].content == "Custom instructions"
    assert len(second.stats.skill_loads) == 1
    assert not second.stats.tool_calls
    assert second.stats.skill_loads[0]["invocation"] == "explicit"
    assert "Unique explicit instructions" in model.requests[1].messages[-1].content
    assert sum("可用技能" in m.content for m in model.requests[1].messages) == 1
    runtime.run("new task after clear")
    assert "Unique explicit instructions" not in str(model.requests[2].messages)


def test_explicit_unknown_skill_fails_before_model(tmp_path):
    model = Model()
    runtime = AgentRuntime(model, skills=SkillRegistry(tmp_path))
    with pytest.raises(SkillError, match="未知技能"):
        runtime.run("$missing fix it")
    assert model.requests == []


def test_nonprefix_mentions_and_variables_are_not_invocations(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert registry.explicit("Explain $debug-and-fix") == []
    assert registry.explicit("$PATH is a variable") == []
    assert registry.explicit("`$missing` is code") == []


def test_traces_include_skill_identity_but_not_body(tmp_path):
    write_skill(tmp_path, body="Do not copy this private workflow into logs")
    with Tracer(tmp_path / "logs") as tracer:
        result = AgentRuntime(Model(reply()), skills=SkillRegistry(tmp_path), on_event=tracer).run(
            "$sample diagnose"
        )
    events = [json.loads(line) for line in tracer.jsonl_path.read_text().splitlines()]
    load = next(e for e in events if e["event"] == "skill_loaded")
    assert load["skill"] == result.stats.skill_loads[0]
    assert "private workflow" not in tracer.jsonl_path.read_text()
    assert "private workflow" not in tracer.text_path.read_text()


def test_plain_terminal_skills_command_does_not_call_model(tmp_path, monkeypatch, capsys):
    model = Model()
    values = iter(["/skills", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    run_interactive(AgentRuntime(model, skills=SkillRegistry(tmp_path)))
    assert "$debug-and-fix" in capsys.readouterr().out
    assert model.requests == []


def test_sandbox_snapshot_resources_and_local_permissions(tmp_path):
    class Backend:
        pass

    path = write_skill(tmp_path)
    reference = path.parent / "references" / "case.txt"
    reference.parent.mkdir()
    reference.write_text("snapshot reference")
    session = SandboxSession(tmp_path, backend=Backend())
    try:
        registry = SkillRegistry(session.workspace)
        path.write_text("host changed after snapshot")
        reference.write_text("host changed reference")
        skill = registry.get("sample")
        assert skill.base_path == "skills/sample"
        result = ReadFileTool(session.workspace).execute(
            {
                "reads": [
                    {
                        "path": skill.base_path + "/references/case.txt",
                    }
                ]
            }
        )
        assert "snapshot reference" in result.data["results"][0]["data"]["content"]
        assert session.changes()[1] == []
        runtime = AgentRuntime(Model(reply()), create_default_tools(tmp_path), skills=registry)
        assert "load_skill" in runtime._tools
        assert "run_command" not in runtime._tools and "run_python" not in runtime._tools
    finally:
        shutil.rmtree(session.directory)


def test_debug_skill_repair_with_real_files_and_regression(tmp_path):
    """Only model decisions are scripted; the edit and behavioral check are real."""
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    check = tmp_path / "check.py"
    check.write_text("from calculator import add\nassert add(2, 3) == 5\nassert add(-2, 2) == 0\n")
    before = subprocess.run([sys.executable, str(check)], capture_output=True, cwd=tmp_path)
    assert before.returncode != 0 and b"AssertionError" in before.stderr
    model = Model(
        reply(calls=[ToolCall("load", "load_skill", {"name": "debug-and-fix"})]),
        reply(calls=[ToolCall("read", "read_file", {"reads": [{"path": "calculator.py"}]})]),
        reply(
            calls=[
                ToolCall(
                    "edit",
                    "edit_file",
                    {
                        "path": "calculator.py",
                        "edits": [{"old_text": "a - b", "new_text": "a + b"}],
                    },
                )
            ]
        ),
        reply("已修改加法逻辑；local 模式下测试尚未运行。"),
    )
    runtime = AgentRuntime(model, create_default_tools(tmp_path), skills=SkillRegistry(tmp_path))
    result = runtime.run("修复 add(2, 3) 返回 -1 的问题")
    assert result.status == "completed"
    assert all(t.status == "success" for t in result.stats.tool_calls)
    # Avoid a timestamp/size-based Python cache masking the edit within the same second.
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)
    after = subprocess.run([sys.executable, str(check)], capture_output=True, cwd=tmp_path)
    assert after.returncode == 0, after.stderr.decode()
