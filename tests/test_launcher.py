"""Exercise the macOS launcher using a mock agent inside a real terminal session."""

import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli.init_project import initialize_project

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS script utility")
SOURCE = Path(__file__).resolve().parents[1] / "run_agent.sh"
KEY_NAMES = {
    "LLM_THINKING",
    "LLM_REASONING_EFFORT",
    "LLM_THINKING_BUDGET",
    "LLM_TEMPERATURE",
    "LLM_TOOL_CHOICE",
    "LLM_TIMEOUT",
    "LLM_CONTEXT_WINDOW",
    "LLM_STREAM",
    "LLM_CONNECT_TIMEOUT",
    "LLM_WRITE_TIMEOUT",
    "LLM_POOL_TIMEOUT",
    "LLM_MAX_RETRIES",
    "LLM_RETRY_DELAY",
    "LLM_MAX_RETRY_DELAY",
    "LLM_EXTRA_JSON",
    "AGENT_MAX_STEPS",
    "AGENT_MAX_OUTPUT_TOKENS",
    "AGENT_SYSTEM_PROMPT",
    "LLM_MODEL",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "AGENT_ENV_FILE",
    "AGENT_HOME",
    "AGENT_SESSION_ID",
    "AGENT_LOG_DIR",
    "AGENT_SANDBOX_WRITEBACK",
    "AGENT_SANDBOX_VERIFY_COMMAND",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "ARK_API_KEY",
    "MINIMAX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
}


@pytest.fixture
def setup_launcher(tmp_path):
    install = tmp_path / "agent install"
    project = tmp_path / "other project"
    binaries = install / ".venv" / "bin"
    binaries.mkdir(parents=True)
    project.mkdir()
    shutil.copy2(SOURCE, install / "run_agent.sh")
    (binaries / "activate").write_text("export AGENT_TEST_ACTIVATED=yes\n")
    (binaries / "python").write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
print("SETTINGS=" + json.dumps({
    "cwd": os.getcwd(), "args": sys.argv[1:], "model": os.getenv("LLM_MODEL"),
    "provider": os.getenv("LLM_PROVIDER"), "activated": os.getenv("AGENT_TEST_ACTIVATED"),
    "key_loaded": os.getenv("DEEPSEEK_API_KEY") == "test-secret-key",
    "thinking": os.getenv("LLM_THINKING"), "max_steps": os.getenv("AGENT_MAX_STEPS"),
    "stream": os.getenv("LLM_STREAM"),
    "connect_timeout": os.getenv("LLM_CONNECT_TIMEOUT"),
    "write_timeout": os.getenv("LLM_WRITE_TIMEOUT"),
    "pool_timeout": os.getenv("LLM_POOL_TIMEOUT"),
    "context_window": os.getenv("LLM_CONTEXT_WINDOW"),
    "timeout": os.getenv("LLM_TIMEOUT"), "extra": os.getenv("LLM_EXTRA_JSON"),
    "writeback": os.getenv("AGENT_SANDBOX_WRITEBACK"),
    "verify_command": os.getenv("AGENT_SANDBOX_VERIFY_COMMAND"),
}), flush=True)
print("READY", flush=True)
while True:
    try:
        task = input("you> ")
    except EOFError:
        break
    if task == "/exit":
        print("bye", flush=True)
        break
    if task == "fail":
        print("mock error", file=sys.stderr, flush=True)
        sys.exit(7)
    print("agent> response", flush=True)
"""
    )
    (binaries / "python").chmod(0o755)
    (project / ".env").write_text(
        "# config\nexport LLM_PROVIDER='deepseek'\nLLM_MODEL=\"file-model\"\n"
        "DEEPSEEK_API_KEY='test-secret-key'"
    )
    env = {k: v for k, v in os.environ.items() if k not in KEY_NAMES}
    return install, project, env


def terminal_run(
    command, *, cwd, env, commands="hello\n/exit\n", ready_marker=b"READY", followup=None
):
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    sent = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
                if not sent and ready_marker in output:
                    os.write(master, commands.encode())
                    sent = True
                if sent and followup and followup[0].encode() in output:
                    os.write(master, followup[1].encode())
                    followup = None
            elif process.poll() is not None:
                break
        else:
            pytest.fail("Launcher did not finish its terminal session")
        return process.wait(timeout=3), output.decode(errors="replace")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)


def test_load_config_caller_root_and_two_sided_log(setup_launcher):
    install, project, env = setup_launcher
    status, output = terminal_run([str(install / "run_agent.sh")], cwd=project, env=env)
    assert status == 0
    assert '"model": "file-model"' in output
    assert '"key_loaded": true' in output
    assert '"activated": "yes"' in output
    assert f'"--root", "{project.resolve()}"' in output
    assert f'"cwd": "{project.resolve()}"' in output
    logs = list((project / "logs").glob("*.log"))
    assert len(logs) == 1
    log = logs[0].read_text()
    assert "hello" in log  # Input is terminal echo, not a print by the mock agent.
    assert "agent> response" in log
    assert "bye" in log
    assert "test-secret-key" not in log + output
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert not (install / "logs").exists()


@pytest.mark.parametrize("relocated", [False, True])
def test_project_wrapper_reuses_installation(setup_launcher, relocated):
    install, project, env = setup_launcher
    (install / ".env.example").write_text("LLM_MODEL=template\n")
    initialize_project(project, install)
    if relocated:
        moved = install.with_name("moved agent's install")
        install.rename(moved)
        env["AGENT_HOME"] = str(moved)
    status, output = terminal_run([str(project / "run_agent.sh")], cwd=project, env=env)
    assert status == 0
    assert '"model": "file-model"' in output
    assert '"key_loaded": true' in output
    assert '"activated": "yes"' in output
    assert f'"--root", "{project.resolve()}"' in output
    assert len(list((project / "logs").glob("*.log"))) == 1


def test_actual_cli_session_without_model_requests(setup_launcher):
    install, project, env = setup_launcher
    with (project / ".env").open("a") as config:
        config.write("\nLLM_BASE_URL=\nLLM_THINKING=enabled\nAGENT_MAX_STEPS=12\nLLM_TIMEOUT=30\n")
    env.update(AGENT_ENV_FILE=str(project / ".env"), AGENT_LOG_DIR=str(project / "actual logs"))
    status, output = terminal_run(
        [str(SOURCE), "--sandbox", "local"],
        cwd=project,
        env=env,
        commands="/help\n/clear\n/exit\n",
        ready_marker="你> ".encode(),
    )
    assert status == 0
    assert "项目目录：" not in output
    log = next(
        p for p in (project / "actual logs").glob("*.log") if not p.name.endswith(".trace.log")
    ).read_text()
    assert "/help" in log
    assert "上下文已清空" in log
    assert "会话已结束" in log
    assert "思考设置：" in log
    trace = next((project / "actual logs").glob("*.trace.jsonl"))
    metadata = json.loads(trace.read_text().splitlines()[0])
    assert metadata["workspace"] == str(project.resolve())
    assert metadata["provider"] == "deepseek"
    assert "CODING AGENT" in log and "Session tokens" in log
    assert "test-secret-key" not in log + output


def test_actual_launcher_can_use_only_user_config(setup_launcher, tmp_path):
    _, project, env = setup_launcher
    (project / ".env").unlink()
    config_dir = tmp_path / "user config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "LLM_PROVIDER=qwen\nLLM_MODEL=global-model\nDASHSCOPE_API_KEY=test-secret-key\n"
    )
    env["AGENT_CONFIG_DIR"] = str(config_dir)
    status, output = terminal_run(
        [str(SOURCE), "--sandbox", "local"],
        cwd=project,
        env=env,
        commands="/exit\n",
        ready_marker="你> ".encode(),
    )
    assert status == 0, output
    metadata = json.loads(
        next((project / "logs").glob("*.trace.jsonl")).read_text().splitlines()[0]
    )
    assert metadata["model"] == "global-model"
    assert metadata["provider"] == "qwen"
    assert metadata["workspace"] == str(project.resolve())
    assert "test-secret-key" not in output


def test_launcher_exports_runtime_settings_literally(setup_launcher):
    install, project, env = setup_launcher
    with (project / ".env").open("a") as config:
        config.write(
            "\nLLM_THINKING=disabled\nAGENT_MAX_STEPS=12\nLLM_TIMEOUT=45\n"
            "LLM_CONTEXT_WINDOW=131072\nLLM_STREAM=false\nLLM_CONNECT_TIMEOUT=7\nLLM_WRITE_TIMEOUT=25\nLLM_POOL_TIMEOUT=8\n"
            "LLM_EXTRA_JSON='{\"top_p\":0.9}'\n"
        )
    env["LLM_THINKING"] = "enabled"
    status, output = terminal_run([str(install / "run_agent.sh")], cwd=project, env=env)
    assert status == 0
    assert '"thinking": "enabled"' in output
    assert '"max_steps": "12"' in output
    assert '"timeout": "45"' in output
    assert '"stream": "false"' in output
    assert '"context_window": "131072"' in output
    assert '"connect_timeout": "7"' in output
    assert '"write_timeout": "25"' in output
    assert '"pool_timeout": "8"' in output
    assert "top_p" in output


def test_environment_override_symlink_and_custom_log_dir(setup_launcher):
    install, project, env = setup_launcher
    shortcut = project / "start-agent"
    shortcut.symlink_to(install / "run_agent.sh")
    env.update(LLM_MODEL="env-model", AGENT_LOG_DIR=str(project / "custom logs"))
    status, output = terminal_run([str(shortcut)], cwd=project, env=env)
    assert status == 0
    assert '"model": "env-model"' in output
    assert len(list((project / "custom logs").glob("*.log"))) == 1


def test_child_failure_exit_status_and_stderr_are_preserved(setup_launcher):
    install, project, env = setup_launcher
    status, _ = terminal_run(
        [str(install / "run_agent.sh")], cwd=project, env=env, commands="fail\n"
    )
    assert status == 7
    log = next((project / "logs").glob("*.log")).read_text()
    assert "mock error" in log


def test_env_values_are_not_executed_and_shell_trace_hides_key(setup_launcher):
    install, project, env = setup_launcher
    (project / ".env").write_text(
        "LLM_MODEL=$(touch should-not-exist)\nDEEPSEEK_API_KEY=test-secret-key\n"
    )
    status, output = terminal_run(
        ["bash", "-x", str(install / "run_agent.sh")], cwd=project, env=env
    )
    assert status == 0
    assert "$(touch should-not-exist)" in output
    assert not (project / "should-not-exist").exists()
    assert "test-secret-key" not in output


@pytest.mark.parametrize("problem", ["missing_venv", "bad_env", "missing_env_override"])
def test_startup_errors_fail_without_logging_credentials(setup_launcher, problem):
    install, project, env = setup_launcher
    if problem == "missing_venv":
        (install / ".venv" / "bin" / "activate").unlink()
    elif problem == "bad_env":
        (project / ".env").write_text("DEEPSEEK_API_KEY='test-secret-key\n")
    else:
        env["AGENT_ENV_FILE"] = str(project / "missing.env")
    result = subprocess.run(
        [str(install / "run_agent.sh")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "test-secret-key" not in result.stdout + result.stderr
    assert not (project / "logs").exists()


def test_actual_task_failure_statistics_reach_session_log(setup_launcher):
    _, project, env = setup_launcher
    # Reserved parameter rejection happens before HTTP, so no model API is contacted.
    env["LLM_EXTRA_JSON"] = '{"stream":true}'
    status, output = terminal_run(
        [str(SOURCE), "--sandbox", "local"],
        cwd=project,
        env=env,
        ready_marker="你> ".encode(),
        commands="hello\n",
        followup=("上下文已清空", "/exit\n"),
    )
    assert status == 0  # Interactive errors return to the prompt; /exit exits normally.
    log = next((project / "logs").glob("*.trace.log")).read_text()
    assert "[任务 1]" in log
    assert "统计：" not in output
    assert "总耗时=" not in output
    assert "统计：状态=failed，模型调用=1 次" in log
    assert "工具调用=0 次" in log
    assert "总耗时=" in log
    assert "合计=未返回" in log
    assert "test-secret-key" not in log + output


@pytest.mark.parametrize("override,expected", [(None, "on-success"), ("manual", "manual")])
def test_launcher_loads_writeback_from_project_config(setup_launcher, override, expected):
    install, project, env = setup_launcher
    with (project / ".env").open("a") as config:
        config.write("\nAGENT_SANDBOX_WRITEBACK=on-success\n")
    if override is not None:
        env["AGENT_SANDBOX_WRITEBACK"] = override
    status, output = terminal_run([str(install / "run_agent.sh")], cwd=project, env=env)
    assert status == 0
    assert f'"writeback": "{expected}"' in output


def test_launcher_loads_verification_argv_without_shell_evaluation(setup_launcher):
    install, project, env = setup_launcher
    command = '["python", "check.py", "$(touch should-not-exist)"]'
    with (project / ".env").open("a") as config:
        config.write("\nAGENT_SANDBOX_VERIFY_COMMAND='" + command + "'\n")
    status, output = terminal_run([str(install / "run_agent.sh")], cwd=project, env=env)
    assert status == 0
    assert json.dumps(command) in output
    assert not (project / "should-not-exist").exists()
