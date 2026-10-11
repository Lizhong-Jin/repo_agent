"""Model-free container acceptance; Windows Desktop must be tested on Windows.

Only disposable projects and containers created by this run are modified. The
JSON report distinguishes container removal from host process-tree certainty.
"""

import argparse
import json
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope

from .environment import DEFAULT_IMAGE, check_image_profile, detect_environment
from .lsp_smoke import SOURCES
from .policy import SandboxPolicy
from .session import SandboxSession


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def expect_refusal(operation):
    try:
        operation()
    except (ValueError, OSError):
        return
    raise RuntimeError("Expected conflict refusal")


class Acceptance:
    def __init__(self, image=DEFAULT_IMAGE):
        self.image = image
        self.report = {
            "schema": 1,
            "started_at": datetime.now(UTC).isoformat(),
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            "image": image,
            "success": False,
            "checks": [],
            "cancellations": [],
            "retained_directories": [],
            "manual_ctrl_c": "not_run",
            "scope": "standard Linux containers; no GPU, installer or real-model validation",
        }

    @contextmanager
    def project(self):
        # Windows TEMP can contain an 8.3 alias (e.g. RUNNER~1). Canonicalize
        # this host-created directory before relocating the session into it;
        # file services must continue rejecting aliases supplied to them.
        directory = Path(tempfile.mkdtemp(prefix="repo-agent-acceptance-")).resolve(strict=True)
        root = directory / "项目 space,中文"
        root.mkdir()
        session = None
        passed = False
        try:
            (root / "original.txt").write_bytes(b"before\r\n")
            (root / "delete.txt").write_bytes(b"delete me\r\n")
            (root / ".env").write_bytes(b"ACCEPTANCE_SECRET=hidden")
            (root / "sample.py").write_text(
                'def example(value: int) -> int:\n    """Return the input."""\n'
                "    return value\n\n\nresult = example(42)\n",
                encoding="utf-8",
            )
            (root / "bad.py").write_text("print(undefined_acceptance_name)\n", encoding="utf-8")
            (root / "go.mod").write_text("module example.com/smoke\n\ngo 1.25\n")
            for name, content in SOURCES.items():
                folder = root / Path(name).suffix[1:]
                folder.mkdir()
                (folder / name).write_text(content, encoding="utf-8")
            session = SandboxSession(root, SandboxPolicy(image=self.image))
            # Exercise the *actual bind mount*, not merely a Unicode source root.
            moved = directory / "副本 space,中文"
            session.directory.rename(moved)
            session.directory, session.workspace = moved, moved / "workspace"
            session._save()
            self.session = session
            self.tools = {tool.definition.name: tool for tool in session.tools()}
            self.names = []
            self.called = set()
            self.outer_timeout = None
            original = session.backend.runner.run

            def tracked(command, **options):
                self.names.append(command[command.index("--name") + 1])
                if self.outer_timeout is not None:
                    options["timeout_seconds"] = self.outer_timeout
                return original(command, **options)

            session.backend.runner.run = tracked
            self.report["image_id"] = session.backend.image
            self.report["container_user"] = session.backend.container_user
            yield
            self.assert_removed()
            passed = True
        finally:
            if passed and session is not None and session.backend.healthy:
                shutil.rmtree(directory)
            else:
                self.report["retained_directories"].append(str(directory))
                if session is not None and not session.directory.is_relative_to(directory):
                    self.report["retained_directories"].append(str(session.directory))

    def assert_removed(self):
        # A daemon error is NOT evidence of absence. Never remove unrelated containers.
        result = subprocess.run(
            [
                self.session.backend.executable,
                "ps",
                "-a",
                "--filter",
                "name=repo-agent-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        remaining = set(result.stdout.splitlines()) & set(self.names)
        require(not remaining, f"Owned containers remain: {sorted(remaining)}")

    def call(self, name, arguments):
        result = self.tools[name].execute(arguments)
        require(result.success, f"{name}: {json.dumps(asdict(result), ensure_ascii=True)}")
        self.called.add(name)
        return result.data

    def command(self, name, arguments):
        data = self.call(name, arguments)
        require(
            data.get("exit_code") == 0
            and not data.get("timed_out")
            and not data.get("cleanup_error"),
            f"{name}: {data}",
        )
        return data

    def compiled_tools(self):
        """Compile and run in the disposable workspace; keep /tmp non-executable."""
        self.command(
            "run_shell",
            {
                "script": "set -eu\n"
                "node -e 'if (6*7 !== 42) process.exit(1)'\n"
                # /tmp remains noexec. Build in the disposable workspace,
                # including Go's intermediate files and cache, then remove
                # artifacts so they cannot enter later writeback checks.
                'build_dir="$(mktemp -d ./acceptance-build.XXXXXX)"\n'
                "trap 'rm -rf -- \"$build_dir\"' EXIT\n"
                "printf 'int main(void){return 0;}' > \"$build_dir/check.c\"\n"
                'cc "$build_dir/check.c" -o "$build_dir/check-c"\n'
                '"$build_dir/check-c"\n'
                'c++ "$build_dir/check.c" -o "$build_dir/check-cpp"\n'
                '"$build_dir/check-cpp"\n'
                "printf 'package main\\nfunc main() {}\\n' > \"$build_dir/check.go\"\n"
                'mkdir "$build_dir/go-tmp" "$build_dir/go-cache"\n'
                'export GOTMPDIR="$PWD/$build_dir/go-tmp"\n'
                'export GOCACHE="$PWD/$build_dir/go-cache"\n'
                'go build -o "$build_dir/check-go" "$build_dir/check.go"\n'
                '"$build_dir/check-go"',
                "timeout_seconds": 120,
            },
        )

    def toolchain(self):
        with self.project():
            data = self.call("get_execution_environment", {"sections": ["execution", "system"]})
            require(data["system"]["os"] == "Linux", "Expected Linux container")
            require(data["execution"]["network"] == "disabled", "Expected disabled network")
            for name in ("git_status", "git_diff", "git_log"):
                self.call(name, {})
            require(
                self.call("git_show", {"path": "original.txt"})["content"] == "before\r\n",
                "Git baseline changed bytes",
            )
            for filename in SOURCES:
                data = self.call("get_symbols", {"path": f"{Path(filename).suffix[1:]}/{filename}"})
                require(any(s["name"].lower() == "example" for s in data["symbols"]), filename)
            position = {"path": "sample.py", "line": 6, "column": 11}
            definitions = self.call("go_to_definition", position)["definitions"]
            require(
                any(
                    x.get("path") == "sample.py" and x["range"]["start"]["line"] == 1
                    for x in definitions
                ),
                "Definition lookup",
            )
            references = self.call("find_references", {**position, "include_declaration": True})
            require(
                any(
                    x.get("path") == "sample.py" and x["range"]["start"]["line"] == 6
                    for x in references["references"]
                ),
                "Reference lookup",
            )
            require(self.call("get_hover", position)["hover"], "Hover is empty")
            diagnostics = self.call("get_diagnostics", {"path": "bad.py"})["diagnostics"]
            require(
                any("undefined_acceptance_name" in x["message"] for x in diagnostics),
                "Missing expected diagnostic",
            )
            symbols = self.call(
                "search_workspace_symbols", {"query": "example", "server_id": "pylsp"}
            )
            require(any(x["name"] == "example" for x in symbols["symbols"]), "Workspace symbols")
            self.call("make_directory", {"path": "files"})
            self.call("write_file", {"path": "files/中文.txt", "content": "first\n"})
            self.call(
                "edit_file",
                {"path": "files/中文.txt", "edits": [{"old_text": "first", "new_text": "second"}]},
            )
            self.call(
                "apply_patch",
                {
                    "patch": "*** Begin Patch\n*** Update File: files/中文.txt\n"
                    "@@\n-second\n+third\n*** End Patch"
                },
            )
            data = self.call("read_file", {"reads": [{"path": "files/中文.txt"}]})
            require("third" in json.dumps(data), "Read did not observe patch")
            for name, args in (
                ("list_files", {"path": "files"}),
                ("find_files", {"pattern": "files/*.txt"}),
                ("search_files", {"query": "third", "path": "files"}),
                ("get_path_info", {"path": "files/中文.txt"}),
            ):
                require("中文.txt" in json.dumps(self.call(name, args), ensure_ascii=False), name)
            self.call("move_file", {"source": "files/中文.txt", "destination": "files/moved.txt"})
            require((self.session.workspace / "files/moved.txt").read_bytes() == b"third\n", "Move")
            self.call("delete_file", {"path": "files/moved.txt"})
            require(not (self.session.workspace / "files/moved.txt").exists(), "Delete")
            self.command("run_command", {"command": ["python", "-c", "assert 6 * 7 == 42"]})
            self.compiled_tools()
            self.command(
                "run_python",
                {
                    "code": "import os, socket\nfrom pathlib import Path\n"
                    "assert os.getuid() != 0\nassert not Path('/workspace/.env').exists()\n"
                    "assert os.statvfs('/tmp').f_flag & os.ST_NOEXEC\n"
                    f"assert not Path({str(self.session.root)!r}).exists()\n"
                    "assert not Path('/var/run/docker.sock').exists()\n"
                    "assert 'ACCEPTANCE_SECRET' not in os.environ\n"
                    "try: socket.create_connection(('1.1.1.1',443),timeout=1)\n"
                    "except OSError: pass\nelse: raise AssertionError('network accessible')\n"
                    "try: Path('/opt/agent/forbidden').write_text('x')\n"
                    "except OSError: pass\nelse: raise AssertionError('image writable')\n"
                },
            )
            require(
                self.called == set(self.tools), f"Uncovered tools: {set(self.tools) - self.called}"
            )
            self.report["tools"] = sorted(self.called)

    def writeback(self):
        with self.project():
            root = self.session.root
            self.call(
                "write_file", {"path": "original.txt", "content": "after\n", "overwrite": True}
            )
            self.call("write_file", {"path": "新增.txt", "content": "new\n"})
            self.call("delete_file", {"path": "delete.txt"})
            require((root / "original.txt").read_bytes() == b"before\r\n", "Premature writeback")
            require(not (root / "新增.txt").exists(), "Premature file creation")
            (root / "original.txt").write_bytes(b"human edit\r\n")
            expect_refusal(self.session.apply)
            require(
                not (root / "新增.txt").exists() and (root / "delete.txt").exists(),
                "Partial conflict write",
            )
            (root / "original.txt").write_bytes(b"before\r\n")
            resumed = SandboxSession.resume(
                self.session.directory, root, self.session.policy, backend=self.session.backend
            )
            require(
                set(resumed.apply()) == {"original.txt", "delete.txt", "新增.txt"}, "Writeback set"
            )
            backup = resumed.last_backup.name
            require((root / "original.txt").read_bytes() == b"after\n", "LF writeback")
            require(
                (root / "新增.txt").read_bytes() == b"new\n" and not (root / "delete.txt").exists(),
                "Add/delete",
            )
            (root / "original.txt").write_bytes(b"later edit")
            expect_refusal(lambda: resumed.restore(backup))
            require(
                (root / "original.txt").read_bytes() == b"later edit",
                "Restore overwrote human edit",
            )
            require(
                (root / "新增.txt").exists() and not (root / "delete.txt").exists(),
                "Partial restore despite conflict",
            )
            (root / "original.txt").write_bytes(b"after\n")
            resumed.restore(backup)
            require((root / "original.txt").read_bytes() == b"before\r\n", "CRLF restore")
            require((root / "delete.txt").read_bytes() == b"delete me\r\n", "Deletion restore")
            require(not (root / "新增.txt").exists(), "Addition restore")

    def long_command(self):
        # Detached, TERM-ignoring descendant: removal must stop its file writes too.
        child = (
            "import signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n Path('heartbeat').write_text(str(time.time_ns()))\n time.sleep(.05)\n"
        )
        return (
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True)\n"
            "while not Path('heartbeat').exists(): time.sleep(.05)\n"
            "Path('ready').write_text('ready')\nprint('ready', flush=True)\ntime.sleep(110)\n"
        )

    def stopped(self):
        self.assert_removed()
        heartbeat = self.session.workspace / "heartbeat"
        require(heartbeat.exists(), "Detached child never started; cleanup check invalid")
        before = heartbeat.read_bytes()
        time.sleep(0.3)
        require(heartbeat.read_bytes() == before, "Detached child still writing")
        require(not (self.session.root / "heartbeat").exists(), "Interrupted files reached host")
        require(
            self.session.guard.reason(SimpleNamespace(status="completed"), self.session.backend),
            "Interrupted task should block automatic writeback",
        )
        self.command("run_python", {"code": "assert 6 * 7 == 42"})

    def timeouts(self):
        for outer in (False, True):
            with self.project():
                self.outer_timeout = 15 if outer else None
                value = self.tools["run_command"].execute(
                    {
                        "command": ["python", "-c", self.long_command()],
                        "timeout_seconds": 110 if outer else 5,
                    }
                )
                require(value.data.get("timed_out"), f"Expected timeout: {value}")
                require(value.success != outer, f"Wrong timeout layer: {value}")
                require(value.data.get("cleanup_status") == "confirmed", f"Cleanup: {value}")
                self.outer_timeout = None
                self.stopped()

    def cancellation(self, *, manual=False):
        with self.project():
            context = CancellationContext()
            stop = threading.Event()
            ready = threading.Event()

            def watch():
                while not stop.wait(0.05):
                    if (self.session.workspace / "ready").exists():
                        ready.set()
                        if manual:
                            print("READY: press Ctrl+C now (within 90 seconds).", flush=True)
                        else:
                            context.cancel()
                        return

            thread = threading.Thread(target=watch, daemon=True)
            thread.start()
            caught = None
            try:
                with cancellation_scope(context, handle_sigint=manual):
                    self.tools["run_command"].execute(
                        {
                            "command": ["python", "-c", self.long_command()],
                            "timeout_seconds": 100,
                        }
                    )
            except RunCancelled as error:
                caught = error.report
            finally:
                stop.set()
                thread.join(timeout=2)
            require(ready.is_set() and caught is not None, "Cancellation must occur after READY")
            require(
                any(
                    x.get("container") in self.names and x["status"] == "confirmed"
                    for x in caught["cleanups"]
                ),
                f"Container cleanup unconfirmed: {caught}",
            )
            self.report["cancellations"].append(
                {"method": "terminal_sigint" if manual else "context_cancel", "report": caught}
            )
            require(caught["cleanup_status"] == "confirmed", f"Host cleanup unconfirmed: {caught}")
            self.stopped()
            if manual:
                self.report["manual_ctrl_c"] = "host_and_container_cleanup_passed"

    def run(self, *, manual=False):
        environment = detect_environment(profile="standard", image=self.image)
        self.report["daemon_architecture"] = environment.architecture
        check_image_profile(self.image, "standard")
        self.report["docker_versions"] = subprocess.run(
            [
                shutil.which("docker"),
                "version",
                "--format",
                "{{.Client.Version}} / {{.Server.Version}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        phases = [
            ("toolchain", self.toolchain),
            ("writeback_restore", self.writeback),
            ("inner_outer_timeouts", self.timeouts),
            ("cancellation", self.cancellation),
        ]
        if manual:
            phases.append(("terminal_ctrl_c", lambda: self.cancellation(manual=True)))
        for name, operation in phases:
            print(f"Checking {name}...", flush=True)
            row = {"name": name, "status": "failed"}
            self.report["checks"].append(row)
            operation()
            row["status"] = "passed"
        self.report["success"] = True
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--report", type=Path, required=True, help="Save UTF-8 JSON, including failures"
    )
    parser.add_argument(
        "--manual-ctrl-c", action="store_true", help="Also require Ctrl+C in a real terminal"
    )
    args = parser.parse_args()
    acceptance = Acceptance(args.image)
    try:
        acceptance.run(manual=args.manual_ctrl_c)
    except (Exception, KeyboardInterrupt, RunCancelled) as error:
        acceptance.report["error"] = f"{type(error).__name__}: {error}"
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(acceptance.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(acceptance.report, ensure_ascii=True))
    raise SystemExit(0 if acceptance.report["success"] else 1)


if __name__ == "__main__":
    main()
