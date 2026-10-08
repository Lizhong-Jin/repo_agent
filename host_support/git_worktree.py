"""Host-owned Git operations. No shell, hooks, user identity changes or remote access."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .cancellation import current_cancellation
from .git_filters import ExternalGitFilter, check_external_filters
from .paths import find_windows_executable


class Git:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        executable = (
            find_windows_executable("git", exclude=(self.root,))
            if os.name == "nt"
            else shutil.which("git")
        )
        if not executable or Path(executable).resolve().is_relative_to(self.root):
            raise ValueError("需要安装可信的 Git；不能使用项目目录中的 git 可执行文件")
        self.executable = str(Path(executable).resolve())
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") and k not in {"LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}
        }
        self.env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_LITERAL_PATHSPECS="1",
            GIT_AUTHOR_NAME="Repo Agent",
            GIT_AUTHOR_EMAIL="repo-agent@localhost",
            GIT_COMMITTER_NAME="Repo Agent",
            GIT_COMMITTER_EMAIL="repo-agent@localhost",
        )

    def run(self, *args, data=None, env=None, ok=(0,)):
        cancellation = current_cancellation()
        if cancellation is not None:
            cancellation.check()
        command = [
            self.executable,
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.pager=cat",
            *map(str, args),
        ]
        # Disk-backed capture also bounds memory for unexpectedly large repositories.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                command,
                cwd=self.root,
                env={**self.env, **(env or {})},
                input=data,
                stdout=output,
                stderr=errors,
                timeout=30,
                check=False,
            )
            output.seek(0)
            payload = output.read(16 * 1024 * 1024 + 1)
            errors.seek(0)
            diagnostic = errors.read(4096).decode("utf-8", "replace")
        if result.returncode not in ok:
            raise ValueError(f"Git {args[0]} 未完成：{diagnostic.strip()}")
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("Git 输出超过工作区管理限制")
        return payload

    def text(self, *args, **kwargs):
        return self.run(*args, **kwargs).decode("utf-8", "surrogateescape").strip()

    def check_repository(self):
        if Path(self.text("rev-parse", "--show-toplevel")).resolve() != self.root:
            raise ValueError("请使用 Git 仓库根目录；第一版不在仓库子目录或子模块内创建工作区")
        if self.text("rev-parse", "--show-superproject-working-tree"):
            raise ValueError("第一版暂不支持子模块工作区")
        self.check_filters()
        return Path(self.text("rev-parse", "--path-format=absolute", "--git-common-dir"))

    def check_filters(self, revision=None):
        # A private index checks incoming paths/attributes using the destination's
        # configuration before checkout/merge, without touching the user's index.
        with tempfile.TemporaryDirectory(prefix="repo-agent-filter-index-") as temporary:
            env = {}
            if revision is not None:
                env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
                self.run("read-tree", revision, env=env)

            def command(args, *, allowed=(0,)):
                return self.run(*args, env=env, ok=allowed).decode("utf-8", "surrogateescape")

            try:
                check_external_filters(command, checkout=True)
            except ExternalGitFilter as error:
                raise ValueError(
                    "项目文件使用了外部 Git clean/smudge/process 过滤器（含 LFS），"
                    "工作区管理暂不支持"
                ) from error

    def head(self):
        return self.text("rev-parse", "--verify", "HEAD^{commit}")

    def branch(self):
        return self.text("symbolic-ref", "--quiet", "HEAD")

    def require_clean(self):
        self.check_filters()
        flags = self.run("ls-files", "-v", "-z").split(b"\0")
        if any(row and (row[:1] == b"S" or row[:1].islower()) for row in flags):
            raise ValueError("第一版不支持 sparse/skip-worktree/assume-unchanged 索引标记")
        if self.run("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            raise ValueError("原项目有未提交或未跟踪的修改；请先处理，再创建或合并工作区")

    def entries(self, revision="HEAD"):
        entries = {}
        for row in self.run("ls-tree", "-r", "-z", revision).split(b"\0"):
            if row:
                metadata, name = row.split(b"\t", 1)
                mode, kind, oid = metadata.decode("ascii").split()
                if kind != "blob" or mode not in {"100644", "100755"}:
                    raise ValueError("第一版工作区暂不支持已跟踪的符号链接或子模块")
                entries[os.fsdecode(name)] = (mode, oid)
        return entries

    def tree(self, entries):
        # An independent index preserves the user's staging area, including on failure.
        with tempfile.TemporaryDirectory(prefix="repo-agent-index-") as temporary:
            env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self.run("read-tree", "--empty", env=env)
            content = b"".join(
                f"{mode} {oid}\t".encode() + os.fsencode(name) + b"\0"
                for name, (mode, oid) in sorted(entries.items())
            )
            self.run("update-index", "-z", "--index-info", data=content, env=env)
            return self.text("write-tree", env=env)

    def commit(self, tree, *, parent=None, message="Repo Agent workspace checkpoint"):
        args = ["commit-tree", tree]
        if parent:
            args.extend(["-p", parent])
        return self.text(*args, "-m", message)
