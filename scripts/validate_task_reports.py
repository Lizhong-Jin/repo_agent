#!/usr/bin/env python3
"""Offline real-backend acceptance and bounded snapshot benchmarks.

Run with the project's Python. Native selects macOS/Linux/WSL without fallback.
Only temporary fixture projects are modified; --project is scanned read-only.
The scripted model drives real tools; no API, credentials or inference is used.
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import AgentRuntime  # noqa: E402
from agent.conversation import SavedConversation  # noqa: E402
from agent.execution_ledger import ExecutionLedger  # noqa: E402
from agent.session import SessionStore  # noqa: E402
from agent.task_queue import TaskQueue  # noqa: E402
from agent.task_reports import ReportCapture, load_report, snapshot
from cli.session_status import SessionStatus  # noqa: E402
from cli.task_controller import TaskController  # noqa: E402
from host_support.cancellation import current_cancellation  # noqa: E402
from llm import LLMConfig, LLMResponse, Message, ToolCall  # noqa: E402
from sandbox.native import NativeBackend  # noqa: E402
from sandbox.policy import SandboxPolicy  # noqa: E402
from sandbox.session import SandboxSession  # noqa: E402
from tools.filesystem import WriteFileTool  # noqa: E402


class ScriptedModel:
    config = LLMConfig("deepseek", "offline-acceptance", api_key="offline-unused")

    def __init__(self):
        self.steps = []
        self.ordinal = 0

    def generate(self, request):
        step = self.steps.pop(0) if self.steps else None
        if callable(step):
            step()
            step = self.steps.pop(0) if self.steps else None
        self.ordinal += 1
        message = (
            Message("assistant", tool_calls=[ToolCall(str(self.ordinal), step[0], step[1])])
            if step
            else Message("assistant", "fixture completed")
        )
        return LLMResponse(
            self.config.provider,
            self.config.model,
            message,
            finish_reason="tool_calls" if step else "stop",
        )


def conversation(project, state, model, tools, *, backend=None, sandbox=None, new=False):
    store = SessionStore(project, directory=state, new=new).open()
    runtime = AgentRuntime(model, tools)
    saved = SavedConversation(
        store,
        runtime,
        model.config,
        SessionStatus(project),
        sandbox=sandbox,
        execution_mode="docker" if sandbox else "native",
        execution_backend=backend,
    )
    control = TaskController(
        runtime,
        conversation=saved,
        sandbox=sandbox,
        write=lambda *_: None,
        write_model=lambda *_: None,
    )
    return store, saved, control


def execute(control, model, steps, title):
    model.steps = list(steps)
    control.resume()
    control.enqueue(title)
    ticket = control.start_next()
    start = time.perf_counter()
    outcome = control.execute(ticket)
    execute_seconds = time.perf_counter() - start
    start = time.perf_counter()
    control.finish(ticket, outcome)
    finish_seconds = time.perf_counter() - start
    report = load_report(control.conversation.store, control.conversation.ledger)
    path = Path(control.queue.get(ticket["number"])["result"]["report"])
    return report, {
        "execute_seconds": execute_seconds,
        "finish_seconds": finish_seconds,
        "report_bytes": path.stat().st_size,
        "metrics": report.get("metrics"),
    }


def check(identity, command):
    return {
        "id": identity,
        "kind": "command",
        "title": identity,
        "expectation": "fixture assertion succeeds",
        "command": command,
    }


def plan(items):
    return "plan_verification", {"items": items}


def run(command):
    return "run_command", {"command": command}


def crash_child(project, state):
    model = ScriptedModel()
    store, _, control = conversation(project, state, model, [WriteFileTool(project)])
    model.steps = [
        ("write_file", {"path": "crash.txt", "content": "durable effect"}),
        lambda: os._exit(73),
    ]
    control.enqueue("crash after durable receipt")
    control.execute(control.start_next())
    store.close()
    raise AssertionError("crash point not reached")


def benchmark(project, repeats=3):
    samples = []
    for texts in (True, False):
        for _ in range(repeats):
            start = time.perf_counter()
            snap = snapshot(project, texts=texts)
            samples.append(
                {
                    "texts": texts,
                    "seconds": time.perf_counter() - start,
                    "files": len(snap["files"]),
                    "complete": snap["complete"],
                    "issues": snap["issues"],
                    "json_bytes": len(json.dumps(snap, ensure_ascii=False).encode()),
                    "metrics": snap.get("metrics"),
                }
            )
    return {
        "project": str(project),
        "samples": samples,
        "median_seconds": statistics.median(s["seconds"] for s in samples),
    }


def report_benchmark(project):
    samples = []
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix="report-metrics-") as directory:
            store = SessionStore(project, directory=Path(directory)).open()
            try:
                queue = TaskQueue()
                task = queue.add("read-only report benchmark")
                queue.claim({})
                saved = SimpleNamespace(store=store, ledger=ExecutionLedger(store), mode="local")
                start = time.perf_counter()
                capture = ReportCapture(saved, task)
                setup_seconds = time.perf_counter() - start
                start = time.perf_counter()
                path = capture.finish("completed", {"writeback_ok": True})
                samples.append(
                    {
                        "start_seconds": setup_seconds,
                        "finish_seconds": time.perf_counter() - start,
                        "report_bytes": path.stat().st_size,
                        "complete": capture.report["final"]["complete"],
                        "observed_changes": len(capture.report["changes"]),
                    }
                )
            finally:
                store.close()
    return samples


def acceptance(mode, image, project=None):
    checks, timings = [], []
    details = {
        "mode": mode,
        "platform": platform.platform(),
        "python": sys.version,
        "model": "scripted; no model API",
        "checks": checks,
        "timings": timings,
    }
    with tempfile.TemporaryDirectory(prefix="report-acceptance-") as directory:
        base = Path(directory).resolve()
        root, state = base / "project", base / "state"
        root.mkdir()
        (root / "preexisting.txt").write_text("user work predating task")
        model = ScriptedModel()
        sandbox = (
            SandboxSession(root, policy=SandboxPolicy(image=image)) if mode == "docker" else None
        )
        backend = sandbox.backend if sandbox else NativeBackend(root)
        tools = sandbox.tools() if sandbox else backend.tools()
        workspace = sandbox.workspace if sandbox else root
        python = "python" if sandbox else sys.executable
        details["environment"] = backend.execution_context()
        store = None
        try:
            store, saved, control = conversation(
                root, state, model, tools, backend=backend, sandbox=sandbox
            )
            ok = [
                python,
                "-c",
                "from pathlib import Path; assert Path('artifact.txt').read_text() == 'agent'",
            ]
            fail = [python, "-c", "raise SystemExit(7)"]
            manual = {
                "id": "manual",
                "kind": "manual",
                "title": "manual UI review",
                "expectation": "human review pending",
            }

            def external_edit():
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; "
                        "Path(sys.argv[1]).write_text('external')",
                        str(workspace / "artifact.txt"),
                    ],
                    check=True,
                    timeout=10,
                )

            report, timing = execute(
                control,
                model,
                [
                    plan(
                        [
                            check("ok", ok),
                            check("fail", fail),
                            check("unrun", [python, "-c", "pass"]),
                            manual,
                        ]
                    ),
                    ("write_file", {"path": "artifact.txt", "content": "agent"}),
                    run(ok),
                    run(fail),
                    external_edit,
                ],
                "mixed changes and verification",
            )
            timings.append(timing)
            states = {i["id"]: i["state"] for i in report["verification_items"]}
            assert states == {
                "ok": "needs_recheck",
                "fail": "failed",
                "unrun": "not_run",
                "manual": "manual_pending",
            }, states
            op = next(o for o in report["operations"] if o["path"] == "artifact.txt")
            assert op["final_state"] == "changed_after" and "external" not in op["diff"]
            assert "preexisting.txt" not in {f["path"] for f in report["changes"]}
            assert saved.ledger.read_result(op["ledger"]["result_ref"]) is not None
            checks.extend(
                [
                    "external edit not attributed to tool",
                    "preexisting changes excluded",
                    "failed/unrun/manual checks visible",
                    "verification marked stale",
                    "ledger reference resolves",
                ]
            )
            if sandbox:
                assert not (root / "artifact.txt").exists() and report["host_changes"] == []
                sandbox.apply()
                assert (root / "artifact.txt").read_text() == "external"
                checks.append("Docker copy separated and explicit writeback applied")
            stable = [
                python,
                "-c",
                "from pathlib import Path; assert Path('artifact.txt').read_text() == 'external'",
            ]
            report, timing = execute(
                control, model, [plan([check("stable", stable)]), run(stable)], "stable validation"
            )
            timings.append(timing)
            assert report["verification_items"][0]["state"] == "passed"
            sid = store.id
            store.close()
            store, saved, control = conversation(
                root, state, model, tools, backend=backend, sandbox=sandbox
            )
            assert (
                store.id == sid
                and load_report(store, saved.ledger)["verification_items"][0]["state"] == "passed"
            )
            saved.new_session()
            old = SessionStore(root, directory=state)
            old.id = sid
            assert (
                load_report(old, ExecutionLedger(old))["verification_items"][0]["state"] == "passed"
            )
            checks.append("restart and session switch preserve reports")
            report, timing = execute(
                control,
                model,
                [
                    ("write_file", {"path": "cancelled.txt", "content": "retained"}),
                    lambda: current_cancellation().cancel(),
                ],
                "cancel after write",
            )
            timings.append(timing)
            assert report["state"] == "cancelled" and any(
                o["path"] == "cancelled.txt" for o in report["operations"]
            )
            checks.append("cancellation retains recorded changes")
        finally:
            if store:
                store.close()
            if hasattr(backend, "close"):
                backend.close()
            if sandbox:
                shutil.rmtree(sandbox.directory)
        crash_root = base / "crash"
        crash_root.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--crash-root",
                str(crash_root),
                "--crash-state",
                str(state),
            ],
            timeout=30,
        )
        assert result.returncode == 73
        with_store = SessionStore(crash_root, directory=state).open()
        try:
            crash_report = load_report(with_store, ExecutionLedger(with_store))
            assert crash_report["state"] == "incomplete" and crash_report["operations"]
            assert "final" not in crash_report
            checks.append("hard process exit recovers receipts without inventing final snapshot")
        finally:
            with_store.close()
        for count in (100, 2000):
            fixture = base / f"files-{count}"
            fixture.mkdir()
            for index in range(count):
                (fixture / f"source-{index:05}.py").write_text("x = 1\n" * 160)
            details[f"benchmark_{count}"] = benchmark(fixture)
            details[f"benchmark_{count}"]["reports"] = report_benchmark(fixture)
        if project:
            details["project_benchmark"] = benchmark(project)
            details["project_benchmark"]["reports"] = report_benchmark(project)
    details["status"] = "passed"
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "docker"], default="native")
    parser.add_argument("--image", default="repo-agent-sandbox:v1")
    parser.add_argument("--project", type=Path, help="optional read-only snapshot benchmark")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--crash-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--crash-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_root:
        crash_child(args.crash_root, args.crash_state)
    started = time.perf_counter()
    try:
        result = acceptance(args.mode, args.image, args.project.resolve() if args.project else None)
    except Exception as error:
        result = {
            "status": "failed",
            "mode": args.mode,
            "platform": platform.platform(),
            "error": f"{type(error).__name__}: {error}",
        }
    result["elapsed_seconds"] = time.perf_counter() - started
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
