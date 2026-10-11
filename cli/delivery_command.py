"""User-only delivery inspection and decisions, without model or command execution."""

import argparse

from agent import delivery
from agent.transcript import display_text
from agent.workspaces import Workspace

from .workspaces_command import project_lock


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="交付状态与显式例外（不执行验证命令）", allow_abbrev=False
    )
    parser.add_argument(
        "action", choices=["status", "refresh", "accept-exception"], nargs="?", default="status"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--session", default="latest")
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if (args.action == "accept-exception") != (args.note is not None):
        parser.error("accept-exception 必须提供 --note；其他操作不能使用 --note")
    try:
        with project_lock(args.root) as store:
            store.select(args.session)
            if store.data is None:
                raise ValueError("会话尚未保存")
            policy = delivery.validate_policy(store.data.get("delivery_policy", delivery.DEFAULT))
            task = delivery.latest_task(store.data.get("task_queue", {"tasks": []}))
            workspace = Workspace(store) if store.data.get("workspace_id") else None
            root = workspace.root if workspace else store.project
            if args.action != "status":
                if policy["mode"] != "strict" or task is None:
                    raise ValueError("没有严格交付任务")
                if store.data.get("sandbox_healthy") is not True or (
                    workspace and workspace.data["state"] != "ready"
                ):
                    raise ValueError("执行环境/工作区未恢复，不能重新判定或接受例外")
                task["result"]["delivery"] = delivery.publish(
                    store, task, policy, root, workspace, exception=args.note
                )
                store.save(store.data)
            print(
                display_text(
                    delivery.describe(delivery.inspect(store, task, policy, root, workspace))
                )
            )
            if args.action != "status":
                print("未自动执行检查、恢复队列或合并。")
    except (OSError, ValueError) as error:
        parser.exit(1, display_text(f"交付操作未完成：{error}\n"))
