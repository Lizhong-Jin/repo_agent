"""User-owned workspace management, without a model connection or arbitrary Git arguments."""

import argparse
import json
import os
import subprocess
from contextlib import contextmanager

from agent.session import SessionStore
from agent.transcript import display_text
from agent.workspaces import Workspace, initialize_project
from host_support.filesystem import open_file
from host_support.locking import lock_descriptor


@contextmanager
def project_lock(root):
    store = SessionStore(root)
    if store.directory.is_symlink():
        raise ValueError("会话目录不能是符号链接")
    store.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = open_file(store.directory / ".lock", os.O_RDWR | os.O_CREAT, nonblocking=False)
    try:
        try:
            lock_descriptor(fd, blocking=False)
        except BlockingIOError:
            raise ValueError("项目仍有 Agent 会话运行；请退出并等待进程收尾后管理工作区") from None
        store._lock_fd = fd
        yield store
    finally:
        store._lock_fd = None
        os.close(fd)


def describe(workspace):
    if workspace is None or not workspace.data:
        return "当前使用原项目目录；新会话可通过 --workspace worktree 启用独立工作区。"
    data = workspace.data
    return (
        f"工作区：{workspace.id}\n状态：{data['state']}\n"
        f"执行目录：{workspace.root}\n原项目：{workspace.project}\n"
        f"基线：{data['base']}\n目标分支：{data['target']}\n"
        "任务和 /continue 在此目录执行。退出后使用 workspaces review 审查；"
        "merge 接收修改，discard 归档放弃，recover 恢复。"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立工作区管理（不调用模型）", allow_abbrev=False)
    parser.add_argument(
        "action",
        choices=["init", "list", "status", "review", "merge", "discard", "recover"],
        nargs="?",
        default="status",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--session", default="latest", help="会话序号、名称或完整 ID")
    parser.add_argument("--yes", action="store_true", help="明确执行初始化、合并或归档放弃")
    parser.add_argument("--review", help="review 输出的版本令牌；合并时必填")
    parser.add_argument("--confirm-stopped", action="store_true", help="已核实遗留进程全部停止")
    args = parser.parse_args(argv)
    if args.yes and args.action not in {"init", "merge", "discard"}:
        parser.error("--yes 仅用于 init/merge/discard")
    if args.review and args.action != "merge":
        parser.error("--review 仅用于 merge")
    if args.confirm_stopped and args.action != "recover":
        parser.error("--confirm-stopped 仅用于 recover")
    try:
        with project_lock(args.root) as store:
            if args.action == "init":
                result = initialize_project(store.project, store.directory, confirm=args.yes)
                print(display_text(json.dumps(result, ensure_ascii=False, indent=2)))
                return
            if args.action == "list":
                for path in sorted((store.directory / "workspaces").glob("*.json")):
                    print(display_text(describe(Workspace(store, path.stem))))
                return
            sid = store.catalog.resolve(args.session)["session_id"]
            workspace = Workspace(store, sid)
            if args.action == "status":
                print(display_text(describe(workspace)))
            elif args.action == "review":
                result = workspace.review()
                print(display_text(result["diff"] or "无文件差异"))
                print(result["notice"])
                print(f"审查令牌：{result['token']}")
            elif args.action in {"merge", "discard"}:
                if not args.yes:
                    raise ValueError("请审查后使用 --yes 明确选择合并或归档放弃")
                if args.action == "merge":
                    print(f"已快进合并：{workspace.merge(args.review)}；工作区保留，后续请新建会话")
                else:
                    print(f"已归档放弃；原项目未修改，工作区文件完整保留：{workspace.discard()}")
            elif args.action == "recover":
                workspace.recover(confirm_stopped=args.confirm_stopped)
                if args.confirm_stopped and (store.directory / f"{sid}.json").exists():
                    store.select(sid)
                    store.save({**store.data, "sandbox_healthy": True})
                print(display_text(describe(workspace)))
                print("未自动重放任务；恢复会话后先核实账本、文件和队列。")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, display_text(f"工作区操作未完成：{error}\n"))
