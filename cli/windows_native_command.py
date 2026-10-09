"""Inspect, collect or explicitly discard Windows native private profiles."""

import argparse
import json
import sys

from host_support.paths import app_directory
from host_support.windows_isolation import WindowsIsolationAPI
from host_support.windows_recovery import recover_profiles
from host_support.windows_security import PrivateWindowsSecurity


def main(argv=None):
    parser = argparse.ArgumentParser(prog="repo-agent native-cleanup")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出正在使用、保留及可回收的隔离资源")
    sub.add_parser("collect", help="回收已停止且无待恢复改动的隔离资源")
    discard = sub.add_parser("discard", help="明确丢弃指定副本及其回写备份")
    discard.add_argument("id", help="记录的 UUID 或 repo-agent-call-UUID 名称")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("此命令仅用于 Windows native")
    try:
        root = app_directory("state") / "windows-native"
        root.mkdir(parents=True, exist_ok=True)
        api = WindowsIsolationAPI()
        PrivateWindowsSecurity(api).set_acl(root)
        report = recover_profiles(
            root,
            api,
            dry_run=args.action == "list",
            discard=args.id.removeprefix("repo-agent-call-") if args.action == "discard" else None,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["failed"] or args.action == "discard" and report["active"]:
            parser.exit(1)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Windows native 回收失败：{error}\n")
