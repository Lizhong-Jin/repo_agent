"""Model-free Docker acceptance checks against disposable project files."""

import argparse
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from .environment import DEFAULT_IMAGE, check_image_profile, detect_environment
from .policy import SandboxPolicy
from .session import SandboxSession


def run_smoke(image: str = DEFAULT_IMAGE) -> list[str]:
    """Require a standard Linux image; never use native or local as a fallback."""
    detect_environment(profile="standard", image=image)
    check_image_profile(image, "standard")
    completed = []
    with tempfile.TemporaryDirectory(prefix="repo-agent-docker-check-") as directory:
        root = Path(directory) / "project"
        root.mkdir()
        source = "def example():\n    return 42\n"
        (root / "sample.py").write_bytes(source.encode())
        (root / ".env").write_text("DOCKER_SMOKE_SECRET=hidden\n", encoding="utf-8")
        session = SandboxSession(root, SandboxPolicy(image=image))
        try:
            tools = {tool.definition.name: tool for tool in session.tools()}

            def call(name, arguments):
                result = tools[name].execute(arguments)
                if not result.success:
                    raise RuntimeError(f"{name}: {json.dumps(asdict(result), ensure_ascii=True)}")
                return result.data

            def require(condition, message):
                if not condition:
                    raise RuntimeError(message)

            environment = call("get_execution_environment", {"sections": ["execution", "system"]})
            require(environment["system"]["os"] == "Linux", "Expected Linux executor")
            require(environment["execution"]["mode"] == "docker", "Expected Docker backend")
            require(environment["execution"]["network"] == "disabled", "Expected no network")
            completed.append("environment")

            for name in ("git_status", "git_diff", "git_log"):
                call(name, {})
            require(call("git_show", {"path": "sample.py"})["content"] == source, "Git baseline")
            completed.append("git")
            symbols = call("get_symbols", {"path": "sample.py"})
            require(any(item["name"] == "example" for item in symbols["symbols"]), "Python LSP")
            completed.append("python_lsp")

            result = call(
                "run_python",
                {
                    "code": (
                        "import os, socket\n"
                        "from pathlib import Path\n"
                        "assert os.getuid() != 0\n"
                        "assert not Path('/workspace/.env').exists()\n"
                        "assert not Path('/var/run/docker.sock').exists()\n"
                        f"assert not Path({str(root)!r}).exists()\n"
                        "assert 'DOCKER_SMOKE_SECRET' not in os.environ\n"
                        "try:\n socket.create_connection(('1.1.1.1', 443), timeout=1)\n"
                        "except OSError:\n pass\n"
                        "else:\n raise AssertionError('network accessible')\n"
                        "try:\n Path('/opt/agent/smoke-write').write_text('forbidden')\n"
                        "except OSError:\n pass\n"
                        "else:\n raise AssertionError('image writable')\n"
                        "Path('result.txt').write_text('container result\\n')\n"
                    )
                },
            )
            require(result["exit_code"] == 0, f"Isolation probe failed: {result}")
            completed.append("python_and_isolation")
            command = call("run_command", {"command": ["python", "--version"]})
            require(command["exit_code"] == 0, f"Command failed: {command}")
            call("read_file", {"reads": [{"path": "result.txt"}]})
            require(not (root / "result.txt").exists(), "Host changed before apply")
            require(session.apply() == ["result.txt"], "Unexpected writeback")
            require(
                (root / "result.txt").read_bytes() == b"container result\n", "Writeback content"
            )
            session.restore(session.last_backup.name)
            require(not (root / "result.txt").exists(), "Backup restore failed")
            completed.append("writeback_and_restore")

            timeout = call(
                "run_python", {"code": "import time; time.sleep(60)", "timeout_seconds": 1}
            )
            require(timeout["timed_out"], f"Expected timeout: {timeout}")
            require(timeout["cleanup_status"] == "confirmed", f"Cleanup unconfirmed: {timeout}")
            recovered = call("run_python", {"code": "print('recovered')"})
            require(recovered["exit_code"] == 0, f"Recovery failed: {recovered}")
            completed.append("timeout_and_recovery")
        finally:
            if session.backend.healthy:
                shutil.rmtree(session.directory)
            else:
                # A possibly running container still owns this mount.
                print(f"Cleanup unconfirmed; retained workspace: {session.directory}")
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    try:
        completed = run_smoke(args.image)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Docker acceptance failed: {error}\n")
    print(json.dumps({"success": True, "checks": completed}))


if __name__ == "__main__":
    main()
