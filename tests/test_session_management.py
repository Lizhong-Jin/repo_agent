"""User-facing names, concurrent queries, continuous logs and real terminal follow."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.session import SessionStore
from agent.Tracing import Tracer
from cli.sessions_command import main, show_log, ui_command
from cli.tui import ConversationUI
from test_sessions import open_conversation, perform  # noqa: F401 (shared fixture)


def options(session="latest", **kwargs):
    return SimpleNamespace(session=session, kind="chat", tail=None, cat=False,
                           follow=False, path=False, **kwargs)


def content(conversation, kind="chat"):
    return conversation.store.catalog.log_path(conversation.store.id, kind).read_text()


def test_names_sequences_and_project_isolation(open_conversation, tmp_path):
    first = open_conversation()
    assert first.label == "会话 1 · #1"
    first.rename("修复登录")
    sid = first.store.id
    first.store.close()
    again = open_conversation()
    assert again.store.id == sid and again.label == "修复登录 · #1"
    again.new_session(name="中文 and spaces")
    assert again.label == "中文 and spaces · #2"
    again.new_session()
    assert again.label == "会话 3 · #3"
    assert open_conversation(tmp_path / "other").label == "会话 1 · #1"


@pytest.mark.parametrize("name", ["", " ", "a" * 81, "a\nb", "a\x1bb", "a\tb"])
def test_invalid_name_leaves_current_conversation(open_conversation, name):
    c = open_conversation()
    sid = c.store.id
    with pytest.raises(ValueError, match="名称"):
        c.new_session(name=name)
    with pytest.raises(ValueError, match="名称"):
        c.rename(name)
    assert c.store.id == sid and c.label == "会话 1 · #1"


def test_external_rename_while_running_survives_checkpoint_and_keeps_latest(open_conversation, capsys):
    c = open_conversation()
    old_sid = c.store.id
    c.new_session()
    pointer = (c.store.directory / "latest.json").read_bytes()
    main(["rename", "2", "外部修改", "--root", str(c.store.project)])
    assert c.label == "外部修改 · #2"
    c.checkpoint(strict=True)
    assert c.store.data["name"] == "外部修改"
    main(["rename", "1", "旧会话新名称", "--root", str(c.store.project)])
    assert (c.store.directory / "latest.json").read_bytes() == pointer
    assert c.store.catalog.get(old_sid)["name"] == "旧会话新名称"
    main(["list", "--root", str(c.store.project)])
    output = capsys.readouterr().out
    assert "运行中 · 默认恢复" in output and "旧会话新名称" in output


def test_duplicate_names_require_sequence(open_conversation):
    c = open_conversation()
    c.rename("same")
    c.new_session(name="same")
    with pytest.raises(ValueError, match="同名.*2, 1"):
        c.store.catalog.resolve("same")
    assert c.store.catalog.resolve("2")["session_id"] == c.store.id


def test_catalog_queries_require_no_model_or_sandbox(tmp_path, monkeypatch, capsys):
    from cli import main as cli
    def forbidden(*args, **kwargs):
        pytest.fail("read-only command attempted to initialize runtime/config")
    monkeypatch.setattr(cli, "LLMClient", forbidden)
    monkeypatch.setattr(cli, "configured_environment", forbidden)
    monkeypatch.setattr(cli, "SandboxSession", forbidden)
    for argv in (["sessions", "list", "--root", str(tmp_path)],
                 ["--root", str(tmp_path), "sessions", "list"]):
        monkeypatch.setattr(sys, "argv", ["repo-agent", *argv])
        cli.main()
    assert "暂无" in capsys.readouterr().out
    assert not SessionStore(tmp_path).directory.exists()


def test_cat_tail_path_and_clear_keep_chat_without_recursive_log_views(open_conversation):
    c = open_conversation()
    perform(c, "记住颜色")
    full = content(c)
    out = io.StringIO()
    show_log(c.store.catalog, options(), output=out)
    assert out.getvalue() == full
    args = options()
    args.tail = 2
    out = io.StringIO()
    show_log(c.store.catalog, args, output=out)
    assert out.getvalue() == "".join(full.splitlines(keepends=True)[-2:])
    view = ui_command(c, "/logs --tail 100")
    assert "记住颜色" in view
    c.checkpoint()
    assert content(c) == full
    args.tail, args.path = None, True
    out = io.StringIO()
    show_log(c.store.catalog, args, output=out)
    assert Path(out.getvalue().strip()).is_absolute()
    c.clear()
    assert "记住颜色" in content(c) and "上下文已清空" in content(c)
    c.new_session()
    assert "记住颜色" not in content(c)


def test_missing_chat_is_reported_instead_of_silently_rebuilt(open_conversation):
    c = open_conversation()
    perform(c, "remember")
    c.store.catalog.log_path(c.store.id).unlink()
    with pytest.raises(FileNotFoundError, match="丢失"):
        show_log(c.store.catalog, options(), output=io.StringIO())
    assert not c.store.catalog.log_path(c.store.id).exists()


def test_old_snapshots_migrate_once_with_no_invented_trace_association(open_conversation):
    import shutil
    c = open_conversation()
    perform(c, "old text")
    sid, catalog, directory = c.store.id, c.store.catalog, c.store.directory
    c.store.close()
    (directory / "index").unlink()
    shutil.rmtree(directory / sid)
    # Simulate the old schema, without title fields or any journal.
    path = directory / f"{sid}.json"
    data = json.loads(path.read_text())
    for key in ("name", "sequence", "created_at"):
        data.pop(key)
    path.write_text(json.dumps(data))
    assert catalog.entries()[0]["name"] == "会话 1"
    first = catalog.ensure_chat(sid).read_text()
    assert "old text" in first and "旧会话快照" in first
    assert catalog.ensure_chat(sid).read_text() == first
    args = options()
    args.kind = "trace"
    with pytest.raises(ValueError, match="旧版本执行日志未关联"):
        show_log(catalog, args, output=io.StringIO())
    restored = open_conversation()
    assert restored.label == "会话 1 · #1" and "old text" in content(restored)


def test_traces_span_restart_and_switch_cleanly_on_new(open_conversation, tmp_path):
    c = open_conversation()
    old_sid = c.store.id
    with Tracer(tmp_path / "traces", session_store=c.store) as tracer:
        c.tracer = tracer
        c.runtime.on_event = tracer
        perform(c, "first")
        old_path = c.store.catalog.log_path(old_sid, "jsonl")
        run_one = tracer.run_id
    c.store.close()
    again = open_conversation()
    with Tracer(tmp_path / "other-traces", session_store=again.store) as tracer:
        again.tracer = tracer
        again.runtime.on_event = tracer
        perform(again, "second")
        run_two = tracer.run_id
        assert run_one != run_two
        again.new_session(name="fresh")
        perform(again, "third")
        new_path = again.store.catalog.log_path(again.store.id, "jsonl")
    events = [json.loads(line) for line in old_path.read_text().splitlines()]
    assert {event["session_id"] for event in events} == {old_sid}
    assert {event["run_id"] for event in events} == {run_one, run_two}
    assert sum(event["event"] == "task_end" for event in events) == 2
    new_events = [json.loads(line) for line in new_path.read_text().splitlines()]
    assert {event["session_id"] for event in new_events} == {again.store.id}
    assert sum(event["event"] == "task_end" for event in new_events) == 1
    assert "first" not in content(again) and "third" in content(again)


def test_follow_in_separate_process_stays_on_selected_session(open_conversation, tmp_path):
    c = open_conversation()
    source = Path(__file__).resolve().parents[1]
    destination = tmp_path / "follow.txt"
    env = {**os.environ, "PYTHONPATH": str(source), "PYTHONUNBUFFERED": "1"}
    with destination.open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cli.main", "sessions", "logs", "1", "--tail", "100", "-f",
             "--root", str(c.store.project)], stdout=output, stderr=subprocess.PIPE, env=env,
            cwd=tmp_path, text=True,
        )
        def wait_for(text):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if text in destination.read_text():
                    return
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read())
                time.sleep(0.03)
            pytest.fail("follow output timed out")
        try:
            wait_for("开始会话")
            perform(c, "follow marker 中文")
            wait_for("follow marker 中文")
            c.rename("new title")
            wait_for("new title")
            c.new_session(name="other")
            perform(c, "do not follow this")
            proc.send_signal(signal.SIGINT)
            assert proc.wait(timeout=5) == 0
            assert "do not follow this" not in destination.read_text()
            # The follow process cannot acquire/release the Agent's execution lock.
            with pytest.raises(ValueError, match="已有 Agent"):
                SessionStore(c.store.project).open()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)


def test_tail_unicode_zero_and_negative(open_conversation):
    c = open_conversation()
    c.store.catalog.append_chat(c.store.id, "中文\n" * 40000 + "最后一行🙂")
    args = options()
    args.tail = 2
    out = io.StringIO()
    show_log(c.store.catalog, args, output=out)
    assert out.getvalue() == "中文\n最后一行🙂"
    args.tail = 0
    out = io.StringIO()
    show_log(c.store.catalog, args, output=out)
    assert not out.getvalue()
    args.tail = -1
    with pytest.raises(ValueError, match="非负"):
        show_log(c.store.catalog, args, output=out)


def test_log_symlinks_and_hardlinks_rejected(open_conversation, tmp_path):
    c = open_conversation()
    secret = tmp_path / "secret"
    secret.write_text("do not read")
    path = c.store.catalog.log_path(c.store.id)
    path.unlink()
    path.symlink_to(secret)
    with pytest.raises(OSError):
        show_log(c.store.catalog, options(), output=io.StringIO())
    path.unlink()
    os.link(secret, path)
    with pytest.raises(ValueError, match="独立普通文件"):
        show_log(c.store.catalog, options(), output=io.StringIO())
    assert secret.read_text() == "do not read"


def test_tui_names_rename_edit_and_new_command(open_conversation):
    async def run():
        c = open_conversation()
        with create_pipe_input() as pipe:
            ui = ConversationUI(c.runtime, conversation=c, status=c.status,
                                terminal_input=pipe, terminal_output=DummyOutput())
            running = asyncio.create_task(ui.run_async())
            async def until(predicate):
                async def wait():
                    while not predicate():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait(), 3)
            await until(lambda: ui.app.is_running)
            assert "会话 1" in ui.header_text()
            pipe.send_text('/rename "new title"\r')
            await until(lambda: "new title" in ui.session_title)
            ui.editor.text = "unfinished input"
            pipe.send_text("\x1bOQ")  # F2 opens the editor without discarding the task draft.
            await until(lambda: ui.renaming)
            ui.editor.text = "edited title"
            pipe.send_text("\r")
            await until(lambda: not ui.renaming)
            assert ui.editor.text == "unfinished input" and "edited title" in ui.header_text()
            ui.editor.text = ""
            pipe.send_text('/new "second name"\r')
            await until(lambda: "second name" in ui.session_title)
            pipe.send_text('/sessions\r/logs --tail 10\r')
            await until(lambda: "默认恢复" in ui.transcript)
            pipe.send_text('/exit\r')
            await asyncio.wait_for(running, 3)
            assert c.label == "second name · #2" and not c.runtime.llm.requests
    asyncio.run(run())


def test_named_cli_start_resume_and_live_commands(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from cli import main as cli
    from test_sessions import Model

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr(cli, "LLMClient", lambda config: nullcontext(Model(config)))
    base = ["repo-agent", "--sandbox", "local", "--model", "m", "--root", str(tmp_path)]
    for flags, commands in [
        (["--new-session", "--name", "from CLI"], ["one", "/exit"]),
        ([], ["/rename renamed", "/sessions", "/logs --tail 10", "/new next", "two", "/exit"]),
    ]:
        monkeypatch.setattr(sys, "argv", [*base, *flags])
        inputs = iter(commands)
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        cli.main()
    catalog = SessionStore(tmp_path).catalog
    assert catalog.resolve("1")["name"] == "renamed"
    assert catalog.resolve("2")["name"] == "next"
    first = catalog.log_path(catalog.resolve("1")["session_id"]).read_text()
    second = catalog.log_path(catalog.resolve("2")["session_id"]).read_text()
    assert "你> one" in first and "你> two" not in first
    assert "你> two" in second and "你> one" not in second
    assert "已恢复此项目上次会话：from CLI · #1" in capsys.readouterr().out
    pointer = (catalog.directory / "latest.json").read_bytes()
    monkeypatch.setattr(sys, "argv", [*base, "--name", "must not silently rename"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert catalog.resolve("latest")["name"] == "next"
    assert (catalog.directory / "latest.json").read_bytes() == pointer


def test_broken_latest_does_not_block_reading_other_sessions(open_conversation):
    c = open_conversation()
    (c.store.directory / "latest.json").write_text("corrupt pointer")
    assert c.store.catalog.resolve("1")["session_id"] == c.store.id
    assert c.store.catalog.warnings
    args = options("1")
    out = io.StringIO()
    show_log(c.store.catalog, args, output=out)
    assert "开始会话" in out.getvalue()


def test_log_append_failure_does_not_retry_task_or_lose_snapshot(open_conversation, monkeypatch):
    c = open_conversation()
    monkeypatch.setattr(c.store.catalog, "append_chat",
                        lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    perform(c, "only once")
    assert len(c.runtime.llm.requests) == 1
    assert any(m.content == "only once" for m in c.history)
    assert c.log_error and c.store.data["pending_task"] is None


def test_failed_new_trace_open_keeps_new_context_consistent(open_conversation, tmp_path, monkeypatch):
    c = open_conversation()
    with Tracer(tmp_path / "traces", session_store=c.store) as tracer:
        c.tracer = tracer
        c.runtime.on_event = tracer
        perform(c, "old")
        sid = c.store.id
        original = c.store.catalog.log_directory
        def unavailable(new_sid, **kwargs):
            if new_sid != sid:
                raise OSError("cannot create new logs")
            return original(new_sid, **kwargs)
        monkeypatch.setattr(c.store.catalog, "log_directory", unavailable)
        c.new_session(name="fresh")
        assert c.store.id != sid and not c.history and tracer.error
        perform(c, "new")
        assert not any(m.content == "old" for m in c.history)
        assert any(m.content == "new" for m in c.history)
