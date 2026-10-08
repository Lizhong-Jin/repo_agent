"""Session-owned workspaces; durable intents surround non-transactional Git operations.

All callers hold SessionStore's project lock. Git metadata is managed by the host,
never by model-provided commands. Completed/abandoned workspaces remain recoverable.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from host_support.git_worktree import Git
from host_support.storage import atomic_write, sync_directory
from tools._internal.file_policy import (
    PROTECTED_NAMES,
    PROTECTED_SUFFIXES,
    is_protected_name,
    runtime_protected_paths,
)

EXCLUDED = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
}
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def save_json(path, data):
    if path.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise ValueError("工作区状态路径不能经过符号链接")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, json.dumps(data, ensure_ascii=True).encode(), sync=True, mode=0o600)
    sync_directory(path.parent)


def excluded(name):
    return is_protected_name(name) or bool(set(Path(name).parts) & EXCLUDED)


def file_bytes(root, name):
    path = root / name
    if path.resolve() != path.absolute() or not path.is_relative_to(root):
        raise ValueError(f"工作区快照不能跟随符号链接：{name}")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
        raise ValueError(f"不支持的文件类型、硬链接或超过 32 MiB：{name}")
    with path.open("rb") as stream:
        content = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    if signature(before) != signature(after) or signature(after) != signature(path.lstat()):
        raise ValueError(f"读取期间文件发生变化：{name}")
    if len(content) > MAX_FILE:
        raise ValueError(f"文件超过 32 MiB：{name}")
    return content, before


def initial_files(root):
    """Preview an explicit baseline, excluding credentials, generated data and agent state."""
    result, total = {}, 0
    protected = tuple(runtime_protected_paths(root))
    # Ask Git to honor project .gitignore files without initializing the source.
    with tempfile.TemporaryDirectory(prefix="repo-agent-init-preview-") as temporary:
        git = Git(temporary)
        git.run("init", "--template=")
        names = git.run(
            "ls-files", "--others", "--exclude-standard", "-z", env={"GIT_WORK_TREE": str(root)}
        )
    for encoded in names.split(b"\0"):
        if not encoded:
            continue
        name = os.fsdecode(encoded)
        if excluded(name) or any((root / name).resolve().is_relative_to(p) for p in protected):
            continue
        content, info = file_bytes(root, name)
        total += len(content)
        if total > MAX_TOTAL or len(result) >= 10000:
            raise ValueError("初始化基线超过 256 MiB 或 10000 个文件")
        result[name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "mode": "100755" if info.st_mode & 0o111 else "100644",
        }
    return result


def initialize_project(root, state_directory, *, confirm=False):
    """Explicit user action, independent of model credentials and tool permissions."""
    root = Path(root).resolve(strict=True)
    git = Git(root)
    journal = Path(state_directory) / "workspace-init.json"
    previous = json.loads(journal.read_text()) if journal.exists() else None
    if previous is not None and (
        not isinstance(previous, dict)
        or not isinstance(previous.get("state"), str)
        or previous.get("state") not in {"initializing", "completed"}
        or not isinstance(previous.get("files"), dict)
    ):
        raise ValueError("初始化状态记录无效，请人工核实")
    # Check ancestors as well as .git files: never create a nested repository.
    existing = git.text("rev-parse", "--show-toplevel", ok=(0, 128))
    resuming = previous is not None and previous.get("state") != "completed"
    if existing and not (resuming and Path(existing).resolve() == root):
        raise ValueError("项目已属于 Git 仓库，无需初始化；请使用仓库根目录")
    if not existing and os.path.lexists(root / ".git"):
        raise ValueError("项目已有无法识别的 .git 元数据；请先人工修复")
    manifest = initial_files(root)
    if not confirm:
        return {"files": manifest, "notice": "仅预览；--yes 将初始化 Git 并保存上述文件的初始基线"}
    if resuming and previous["files"] != manifest:
        raise ValueError("初始化中断后文件已变化；请检查 workspace-init.json 与项目，未自动重做")
    intent = previous if resuming else {"state": "initializing", "files": manifest}
    save_json(journal, intent)
    if not existing:
        # Empty template avoids inheriting user-provided hooks at initialization.
        git.run("init", "--template=", "--initial-branch=main")
    git.check_repository()
    patterns = sorted(PROTECTED_NAMES | EXCLUDED | {".env", ".env.*"})
    patterns.extend("*" + suffix for suffix in PROTECTED_SUFFIXES)
    for path in runtime_protected_paths(root):
        if path.is_relative_to(root):
            patterns.append("/" + path.relative_to(root).as_posix().replace(" ", "\\ ") + "/")
    ignore = root / ".git" / "info" / "exclude"
    if ignore.resolve() != ignore.absolute():
        raise ValueError("Git exclude 路径不能经过符号链接")
    ignore.parent.mkdir(parents=True, exist_ok=True)
    old = ignore.read_bytes() if ignore.exists() else b""
    if b"# Repo Agent baseline exclusions" not in old:
        atomic_write(
            ignore,
            old + b"\n# Repo Agent baseline exclusions\n" + ("\n".join(patterns) + "\n").encode(),
            sync=True,
        )
    head = git.text("rev-parse", "--verify", "HEAD", ok=(0, 128))
    if head:
        if head != intent.get("commit"):
            raise ValueError("初始化期间出现未知提交；请人工核实")
    else:
        entries = {}
        for name, item in manifest.items():
            content, _ = file_bytes(root, name)
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("初始化文件已变化，未发布基线")
            oid = git.text("hash-object", "-w", "--path=" + name, "--stdin", data=content)
            entries[name] = (item["mode"], oid)
        commit = intent.get("commit") or git.commit(
            git.tree(entries), message="Repo Agent: initial project baseline"
        )
        intent.update(commit=commit)
        save_json(journal, intent)
        git.run("update-ref", "refs/heads/main", commit, "0" * len(commit))
    # Populate the index without changing any project file.
    git.run("reset", "--mixed", intent["commit"], "--")
    intent["state"] = "completed"
    save_json(journal, intent)
    return intent


class Workspace:
    def __init__(self, store, session_id=None):
        self.store = store
        self.id = session_id or store.id
        if not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise ValueError("无效的工作区会话 ID")
        self.project = store.project
        self.path = store.directory / "workspaces" / (self.id + ".json")
        self.root = store.directory.parent.parent / "workspaces" / store.directory.name / self.id
        if self.root.resolve() != self.root or self.root.is_relative_to(self.project):
            raise ValueError("独立工作区必须位于原项目外，且不能经过符号链接")
        self.branch = "refs/heads/repo-agent/" + self.id
        if self.path.is_symlink() or self.path.parent.resolve() != self.path.parent.absolute():
            raise ValueError("工作区状态路径不能经过符号链接")
        self.data = json.loads(self.path.read_text()) if self.path.exists() else None
        if self.data is not None:
            if not isinstance(self.data, dict) or any(
                self.data.get(k) != v for k, v in self.identity().items()
            ):
                raise ValueError("工作区记录与当前项目/会话不匹配")
            if (
                not isinstance(self.data.get("state"), str)
                or self.data.get("state")
                not in {"creating", "ready", "running", "blocked", "merging", "merged", "archived"}
                or not isinstance(self.data.get("base"), str)
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.data["base"])
                or not isinstance(self.data.get("target"), str)
                or not self.data["target"].startswith("refs/heads/")
                or not isinstance(self.data.get("common"), str)
                or not Path(self.data["common"]).is_absolute()
            ):
                raise ValueError("无效的工作区状态记录")

    def identity(self):
        return {
            "version": 1,
            "id": self.id,
            "project": str(self.project),
            "root": str(self.root),
            "branch": self.branch,
        }

    def save(self):
        save_json(self.path, self.data)

    def create(self):
        git = Git(self.project)
        common = git.check_repository()
        git.require_clean()
        base, target = git.head(), git.branch()
        git.entries()  # Reject unsupported trees before changing the repository.
        self.data = {
            **self.identity(),
            "state": "creating",
            "base": base,
            "target": target,
            "common": str(common),
            "review": None,
            "cleanup_status": "not_needed",
        }
        self.save()  # No branch or directory exists before the intent is durable.
        self._create()

    def _create(self):
        if self.root.exists():
            self.validate()
            destination = Git(self.root)
            if destination.head() != self.data["base"]:
                raise ValueError("创建中的工作区已变化；请人工核实")
            index = Path(
                destination.text("rev-parse", "--path-format=absolute", "--git-path", "index")
            )
            if not index.exists() and {p.name for p in self.root.iterdir()} == {".git"}:
                # Resume a no-checkout worktree only while it is still empty.
                destination.check_filters(self.data["base"])
                destination.run("reset", "--hard", self.data["base"], "--")
            destination.require_clean()
        else:
            git = Git(self.project)
            git.check_repository()
            existing = git.text("rev-parse", "--verify", self.branch, ok=(0, 128))
            if existing and existing != self.data["base"]:
                raise ValueError("工作区分支已存在且基线不匹配")
            self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if existing:
                git.run(
                    "worktree",
                    "add",
                    "--no-checkout",
                    str(self.root),
                    self.branch.removeprefix("refs/heads/"),
                )
            else:
                git.run(
                    "worktree",
                    "add",
                    "--no-checkout",
                    "-b",
                    self.branch.removeprefix("refs/heads/"),
                    str(self.root),
                    self.data["base"],
                )
            destination = Git(self.root)
            destination.check_filters(self.data["base"])
            destination.run("reset", "--hard", self.data["base"], "--")
            self.validate()
        self.data["state"] = "ready"
        self.save()

    def validate(self):
        if not self.data or not self.root.is_dir() or self.root.resolve() != self.root:
            raise ValueError("独立工作区缺失或路径已变化；不会回退到原项目执行")
        git = Git(self.root)
        if str(git.check_repository()) != self.data["common"] or git.branch() != self.branch:
            raise ValueError("工作区 Git 身份已变化，禁止继续")
        source = Git(self.project)
        if str(source.check_repository()) != self.data["common"]:
            raise ValueError("原项目 Git 身份已变化")
        git.run("merge-base", "--is-ancestor", self.data["base"], git.head())
        return git

    def require_ready(self):
        if not self.data or self.data["state"] != "ready":
            state = self.data["state"] if self.data else "missing"
            raise ValueError(
                f"工作区状态为 {state}；请用 workspaces status/recover 核实，未执行任务"
            )
        return self.validate()

    def begin_run(self, task_id):
        self.require_ready()
        self.data.update(
            state="running", active_task=task_id, review=None, cleanup_status="unknown"
        )
        self.save()

    def finish_run(self, cleanup_status):
        self.data.update(
            state="blocked" if cleanup_status == "unknown" else "ready",
            cleanup_status=cleanup_status,
            active_task=None,
        )
        self.save()

    def snapshot(self):
        git = self.validate()
        head = git.head()
        baseline = git.entries(self.data["base"])
        entries = git.entries(head)
        names = (
            set(baseline)
            | set(entries)
            | {
                os.fsdecode(n)
                for n in git.run(
                    "ls-files", "--cached", "--others", "--exclude-standard", "-z"
                ).split(b"\0")
                if n
            }
        )
        total = 0
        for name in sorted(names):
            path = self.root / name
            if not path.exists() and not path.is_symlink():
                if is_protected_name(name) and name in baseline:
                    raise ValueError(f"受保护的已跟踪文件被删除：{name}")
                entries.pop(name, None)
                continue
            if name not in entries and name not in baseline and excluded(name):
                continue
            content, info = file_bytes(self.root, name)
            total += len(content)
            if total > MAX_TOTAL or len(names) > 10000:
                raise ValueError("工作区快照超过 256 MiB 或 10000 个文件")
            # External filters are rejected by validate(); built-in EOL handling
            # must still match Git's checkout semantics on Windows.
            oid = git.text("hash-object", "-w", "--path=" + name, "--stdin", data=content)
            mode = (
                entries.get(name, ("100644",))[0]
                if os.name == "nt"
                else ("100755" if info.st_mode & 0o111 else "100644")
            )
            if is_protected_name(name) and baseline.get(name) != (mode, oid):
                raise ValueError(f"受保护文件的修改不能自动接收：{name}")
            entries[name] = (mode, oid)
        if git.head() != head:
            raise ValueError("生成快照时工作区分支已变化")
        return head, git.tree(entries)

    def review(self):
        self.require_ready()
        head, tree = self.snapshot()
        if self.snapshot() != (head, tree):
            raise ValueError("审查期间文件发生变化，请重试")
        token = hashlib.sha256(f"{self.id}:{self.data['base']}:{head}:{tree}".encode()).hexdigest()
        self.data["review"] = {"token": token, "head": head, "tree": tree}
        self.save()
        git = Git(self.root)
        diff = git.text(
            "diff", "--no-ext-diff", "--no-textconv", "--binary", self.data["base"], tree, "--"
        )
        return {
            "token": token,
            "diff": diff,
            "root": str(self.root),
            "notice": "审查版本已固定；测试结果请结合任务报告核实。文件变化会使此令牌失效。",
        }

    def merge(self, token):
        git = self.require_ready()
        review = self.data.get("review")
        if not review or review["token"] != token:
            raise ValueError("请先 workspaces review，并提供对应的 --review 令牌")
        if self.snapshot() != (review["head"], review["tree"]):
            raise ValueError("审查后工作区发生变化，请重新审查")
        source = Git(self.project)
        source.require_clean()
        if source.branch() != self.data["target"] or source.head() != self.data["base"]:
            raise ValueError("目标分支已切换或前进；第一版仅支持原基线上的快进合并")
        source.check_filters(review["tree"])
        commit = git.commit(review["tree"], parent=review["head"])
        self.data.update(state="merging", merge_commit=commit)
        self.save()
        git.run("update-ref", self.branch, commit, review["head"])
        git.run("reset", "--mixed", commit, "--")
        source.require_clean()
        if source.branch() != self.data["target"] or source.head() != self.data["base"]:
            raise ValueError("目标分支在合并前发生变化；工作区已保留，请 recover 后重新核实")
        source.check_filters(commit)
        source.run("merge", "--ff-only", "--no-edit", "--no-stat", "--no-overwrite-ignore", commit)
        if source.head() != commit:
            raise ValueError("合并结果未确认，请检查后 recover")
        self.data["state"] = "merged"
        self.save()
        return commit

    def discard(self):
        self.require_ready()
        # First release archives in place: even ignored/untracked files survive.
        # Do not use worktree remove --force or recursively delete user data.
        self.data.update(state="archived", review=None)
        self.save()
        return str(self.root)

    def recover(self, *, confirm_stopped=False):
        if not self.data:
            raise ValueError("此会话没有独立工作区")
        state = self.data["state"]
        if state == "creating":
            self._create()
            return
        self.validate()
        if state == "merging":
            source = Git(self.project)
            if source.branch() != self.data["target"]:
                raise ValueError("目标分支已切换，请人工核实合并状态")
            if source.head() == self.data["merge_commit"]:
                source.require_clean()
                self.data["state"] = "merged"
            elif source.head() == self.data["base"]:
                self.data.update(state="ready", review=None)
            else:
                raise ValueError("目标分支出现未知提交，请人工核实；未自动回滚")
        elif state in {"running", "blocked", "archived"}:
            if not confirm_stopped:
                raise ValueError(
                    "请核实遗留进程已停止，再使用 --confirm-stopped 恢复；不会自动重放任务"
                )
            self.data.update(state="ready", cleanup_status="confirmed", review=None)
        elif state != "ready":
            raise ValueError("已合并工作区不能继续执行，请启动新会话")
        self.save()


def prepare_workspace(store, requested=None):
    recorded = (store.data or {}).get("workspace_id")
    if (
        requested != "worktree"
        and not recorded
        and not (store.directory / "workspaces" / (store.id + ".json")).exists()
    ):
        return None
    workspace = Workspace(store)
    if recorded and not workspace.data:
        raise ValueError("会话工作区记录缺失；禁止回退到原项目")
    if workspace.data:
        if requested == "direct":
            raise ValueError("此会话绑定独立工作区；原目录执行请使用 --new-session")
        workspace.require_ready()
        return workspace
    if requested != "worktree":
        return None
    if store.data and (store.data["history"] or store.data.get("task_queue", {}).get("tasks")):
        raise ValueError("已有直接执行历史的会话不能更换工作区；请使用 --new-session")
    workspace.create()
    return workspace
