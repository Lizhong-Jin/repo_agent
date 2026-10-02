"""Project session management without model configuration or an execution lock."""

import argparse
import io
import shlex
import sys
import time
from collections import deque
from pathlib import Path

from agent.session import SessionStore, open_log
from agent.transcript import display_text


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def log_arguments(parser):
    parser.add_argument(
        "session", nargs="?", default="latest", help="会话序号、完整 ID、名称或 latest"
    )
    parser.add_argument("--kind", choices=["chat", "trace", "jsonl"], default="chat")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tail", type=int, metavar="N", help="只显示最后 N 行")
    group.add_argument("--cat", action="store_true", help="显示全部内容（默认）")
    parser.add_argument(
        "-f", "--follow", action="store_true", help="持续跟随此会话，Ctrl+C 退出查看"
    )
    parser.add_argument("--path", action="store_true", help="只打印日志绝对路径")


def format_sessions(catalog):
    rows = catalog.entries()
    if not rows:
        text = "此项目暂无保存的会话。"
    else:
        lines = ["序号\t名称\t最近活动时间\t状态"]
        for row in rows:
            status = "运行中" if row["active"] else "已停止"
            if row["latest"]:
                status += " · 默认恢复"
            lines.append(f"{row['sequence']}\t{row['name']}\t{row['updated_at']}\t{status}")
        text = "\n".join(lines)
    if catalog.warnings:
        text += "\n" + "\n".join(catalog.warnings)
    return display_text(text)


def get_log(catalog, row, kind):
    path = catalog.log_path(row["session_id"], kind)
    if kind == "chat":
        catalog.ensure_chat(row["session_id"])
    try:
        with open_log(path):
            pass
    except FileNotFoundError:
        raise ValueError(
            f"此会话的 {kind} 日志缺失或尚未生成：{path}；"
            "旧版本执行日志未关联，请查看项目 logs/ 或 AGENT_LOG_DIR"
        ) from None
    return path


def tail_text(stream, count):
    """Read from the end, without loading/scanning a large file for a small tail."""
    if count < 0:
        raise ValueError("--tail 必须为非负整数")
    binary = stream.buffer
    binary.seek(0, 2)
    end = position = binary.tell()
    chunks = deque()
    newlines = 0
    while count and position > 0 and newlines <= count:
        size = min(position, 65536)
        position -= size
        binary.seek(position)
        chunk = binary.read(size)
        chunks.appendleft(chunk)
        newlines += chunk.count(b"\n")
    text = b"".join(chunks).splitlines(keepends=True)[-count:] if count else []
    stream.seek(end)
    return b"".join(text).decode("utf-8")


def show_log(catalog, options, *, output=None):
    output = output or sys.stdout
    if options.tail is not None and options.tail < 0:
        raise ValueError("--tail 必须为非负整数")
    if options.path and (options.follow or options.tail is not None or options.cat):
        raise ValueError("--path 不能和 --tail、--cat、-f 一起使用")
    row = catalog.resolve(options.session)
    path = get_log(catalog, row, options.kind)
    if options.path:
        output.write(str(path) + "\n")
        return
    stream = open_log(path)
    try:
        count = options.tail
        if options.follow and count is None and not options.cat:
            count = 10
        if count is not None:
            output.write(display_text(tail_text(stream, count)))
        else:
            while chunk := stream.read(65536):
                output.write(display_text(chunk))
        output.flush()
        if not options.follow:
            return
        identity = path.stat().st_dev, path.stat().st_ino
        while True:
            chunk = stream.read(65536)
            if chunk:
                output.write(display_text(chunk))
                output.flush()
                continue
            info = path.stat()
            if (info.st_dev, info.st_ino) != identity or info.st_size < stream.tell():
                stream.close()
                stream = open_log(path)
                identity = info.st_dev, info.st_ino
            time.sleep(0.2)
    finally:
        stream.close()


def ui_command(conversation, task):
    """Shared, bounded command output. Does not add log views to the chat journal."""
    parts = task.split(maxsplit=1)
    command, argument = parts[0], parts[1] if len(parts) > 1 else ""
    if command == "/ledger":
        if argument.strip():
            raise ValueError("用法：/ledger；完整记录可用 sessions ledger <序号> --json")
        return display_text(conversation.ledger.describe())
    if command == "/rename":
        words = shlex.split(argument)
        if not words:
            raise ValueError("用法：/rename 新名称")
        return conversation.rename(" ".join(words))
    if command == "/sessions":
        if argument.strip():
            raise ValueError("用法：/sessions")
        return format_sessions(conversation.store.catalog)
    if command == "/logs":
        parser = CommandParser(add_help=False, allow_abbrev=False)
        log_arguments(parser)
        args = parser.parse_args(shlex.split(argument))
        if args.follow or args.cat:
            raise ValueError("请在另一个终端使用 repo-agent sessions logs <序号> --cat 或 -f")
        if not args.path:
            args.tail = 100 if args.tail is None else args.tail
            if not 0 <= args.tail <= 1000:
                raise ValueError("界面内 --tail 支持 0–1000 行；更多内容请使用终端命令")
        # Omitted selector means this UI's session, independent of latest pointer.
        if args.session == "latest":
            args.session = conversation.store.id
        output = io.StringIO()
        show_log(conversation.store.catalog, args, output=output)
        text = output.getvalue()
        if len(text) > 200000:
            text = "[界面仅显示最后 200000 个字符；完整内容请使用终端命令]\n" + text[-200000:]
        return text or "日志暂无内容。"
    raise ValueError("未知会话管理命令")


def new_name(task):
    words = shlex.split(task)
    return " ".join(words[1:]) if len(words) > 1 else None


def main(argv=None):
    # Accept --root before or after the subcommand, without loading model config.
    root_parser = CommandParser(add_help=False, allow_abbrev=False)
    root_parser.add_argument("--root", default=".")
    parser = CommandParser(
        prog="repo-agent sessions",
        allow_abbrev=False,
        description="查看和管理当前项目会话；支持 --root 指定项目",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出会话")
    ledger = commands.add_parser("ledger", help="查看持久化工具执行证据")
    ledger.add_argument("session", nargs="?", default="latest")
    ledger.add_argument("--limit", type=int, default=100)
    ledger.add_argument("--before", type=int, help="只显示此事件序号之前的记录，用于翻页")
    ledger.add_argument(
        "--json", action="store_true", help="输出完整结构化结果（可能包含文件内容）"
    )
    rename = commands.add_parser("rename", help="更改会话名称")
    rename.add_argument("session")
    rename.add_argument("name")
    logs = commands.add_parser("logs", help="查看或实时跟随会话日志")
    log_arguments(logs)
    try:
        root_args, rest = root_parser.parse_known_args(argv)
        args = parser.parse_args(rest)
        catalog = SessionStore(Path(root_args.root)).catalog
        if args.command == "list":
            print(format_sessions(catalog))
        elif args.command == "rename":
            row = catalog.resolve(args.session)
            name = catalog.rename(row["session_id"], args.name)
            print(f"会话名称已更新：{name} · #{row['sequence']}")
        elif args.command == "ledger":
            import json

            from agent.execution_ledger import ExecutionLedger

            if not 1 <= args.limit <= 10000:
                raise ValueError("--limit 支持 1–10000")
            if args.before is not None and not 0 < args.before < 2**63:
                raise ValueError("--before 必须是有效的正整数事件序号")
            store = SessionStore(Path(root_args.root))
            store.id = catalog.resolve(args.session)["session_id"]
            journal = ExecutionLedger(store)
            print(
                display_text(
                    json.dumps(
                        journal.evidence(limit=args.limit, before=args.before),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                if args.json
                else display_text(journal.describe(limit=args.limit, before=args.before))
            )
        else:
            show_log(catalog, args)
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        return
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"会话管理失败：{error}\n")


if __name__ == "__main__":
    main()
