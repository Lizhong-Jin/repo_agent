"""Explicit recovery management, independent of model configuration and startup."""

import argparse
import json
import os
from contextlib import contextmanager

from agent.recovery import Recovery, describe
from agent.session import SessionStore
from agent.transcript import display_text
from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.filesystem import open_file
from host_support.locking import lock_descriptor


@contextmanager
def recovery_lock(store):
    # Never open SessionStore or the catalog here: both may replay pending saves.
    if store.directory.resolve() != store.directory.absolute():
        raise ValueError("会话目录不能经过符号链接")
    try:
        fd = open_file(store.directory / ".lock", nonblocking=False)
    except FileNotFoundError:
        raise ValueError("没有现有项目锁；请检查会话状态目录，未创建或修改任何记录") from None
    try:
        try:
            lock_descriptor(fd, blocking=False)
        except BlockingIOError:
            raise ValueError(
                "项目仍有活动会话；使用 /recover 查看，退出并等待收尾后再操作"
            ) from None
        store._lock_fd = fd
        yield
    finally:
        store._lock_fd = None
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="统一异常恢复与外部操作核实", allow_abbrev=False)
    parser.add_argument("action", choices=["status", "plan", "apply"], nargs="?", default="status")
    parser.add_argument("plan", nargs="?")
    parser.add_argument("--action", dest="operation")
    parser.add_argument("--root", default=".")
    parser.add_argument("--session", default="latest")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--confirm-stopped", action="store_true")
    parser.add_argument("--note")
    parser.add_argument(
        "--allow-network", action="store_true", help="明确授权所选操作的 GitHub 只读核实"
    )
    args = parser.parse_args(argv)
    if args.action != "apply" and any(
        (args.plan, args.operation, args.confirm_stopped, args.note, args.allow_network)
    ):
        parser.error("修改或联网选项只能用于 apply")
    if args.action == "apply" and (not args.plan or not args.operation):
        parser.error("apply 需要计划编号及 --action")
    if args.allow_network and not (args.operation or "").startswith("verify:"):
        parser.error("--allow-network 仅适用于 verify:操作ID")
    if (args.confirm_stopped or args.note) and args.operation != "confirm-cleanup":
        parser.error("--confirm-stopped/--note 仅适用于 confirm-cleanup")
    try:
        store = SessionStore(args.root)
        with recovery_lock(store):
            recovery = Recovery(store, args.session)
            if args.action == "apply":
                github = http = None
                try:
                    if args.allow_network:
                        from tools._internal.github_service import GitHubService
                        from tools._internal.service_http import ServiceHTTP

                        http = ServiceHTTP()
                        github = GitHubService(
                            http, os.environ.get("REPO_AGENT_GITHUB_TOKEN") or None
                        )
                    with cancellation_scope(CancellationContext(), handle_sigint=True):
                        report = recovery.apply(
                            args.plan,
                            args.operation,
                            confirm_stopped=args.confirm_stopped,
                            note=args.note,
                            github=github,
                        )
                finally:
                    if http:
                        http.close()
            else:
                report = recovery.inspect()
            print(
                display_text(
                    json.dumps(report, ensure_ascii=False, indent=2)
                    if args.json
                    else describe(report)
                )
            )
            if report["plan"] is None:
                parser.exit(1)
    except (RunCancelled, KeyboardInterrupt):
        parser.exit(130, "恢复已取消；保留记录，重新检查后再操作。远端结果不可据此推断。\n")
    except Exception as error:
        # Remote exception text is not a safe diagnostic channel for credentials.
        message = (
            "远端核实未完成；旧记录保留，请重新生成计划，不要直接重试写入"
            if args.allow_network
            else str(error)
            if isinstance(error, (OSError, ValueError))
            else f"恢复证据或操作异常（{type(error).__name__}）；保留记录并重新检查"
        )
        parser.exit(1, display_text(f"恢复未完成：{message}\n"))
