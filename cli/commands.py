"""Dispatch independent CLI commands and review retained sandbox copies."""

from installer.paths import version_info
from sandbox import SandboxSession


def dispatch_command(argv):
    if argv[:1] == ["native-cleanup"]:
        from .windows_native_command import main as cleanup_main

        cleanup_main(argv[1:])
        return True
    if argv[:1] == ["workspaces"]:
        from .workspaces_command import main as workspaces_main

        workspaces_main(argv[1:])
        return True
    if argv[:1] == ["--root"] and argv[2:3] == ["workspaces"]:
        from .workspaces_command import main as workspaces_main

        workspaces_main(["--root", argv[1], *argv[3:]])
        return True
    if argv[:1] in (["version"], ["--version"]):
        import json

        print(json.dumps(version_info(), ensure_ascii=False, indent=2))
        return True
    if argv[:1] == ["uninstall"]:
        from installer.uninstall import main as uninstall_main

        uninstall_main(argv[1:])
        return True
    # Session queries also accept the common --root PATH sessions ... form.
    if argv[:1] == ["--root"] and argv[2:3] == ["sessions"]:
        from .sessions_command import main as sessions_main

        sessions_main(["--root", argv[1], *argv[3:]])
        return True
    if argv[:1] == ["sessions"]:
        from .sessions_command import main as sessions_main

        sessions_main(argv[1:])
        return True
    if argv[:1] == ["toolchains"]:
        from installer.toolchains import main as toolchains_main

        toolchains_main(argv[1:])
        return True
    if argv[:1] == ["doctor"]:
        from .doctor import main as doctor_main

        doctor_main(argv[1:])
        return True
    if argv[:1] == ["config"]:
        from .config_command import main as config_main

        config_main(argv[1:])
        return True
    return False


def review_sandbox(parser, args):
    try:
        session = SandboxSession.review(args.sandbox_review)
        if args.apply and args.restore_backup:
            parser.error("--apply 与 --restore-backup 不能同时使用")
        if args.restore_backup:
            print(f"已恢复 {len(session.restore(args.restore_backup))} 个文件。")
            return
        print(session.diff())
        if args.apply:
            print(f"已回写 {len(session.apply())} 个文件。")
            if session.last_backup:
                print(f"回写前备份：{session.last_backup}")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    return
