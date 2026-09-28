"""Prepare local/native/Docker tools and register cleanup at resource acquisition."""

from dataclasses import dataclass

from sandbox import SandboxPolicy, SandboxSession
from sandbox.environment import DEFAULT_IMAGE, check_image_profile, detect_environment
from sandbox.native import create_native_backend as NativeBackend
from tools import create_default_tools


@dataclass
class ExecutionEnvironment:
    tools: list
    sandbox: object = None
    native: object = None


def describe_retained_sandbox(session):
    print(
        f"Sandbox 副本已保留：{session.directory}\n"
        f"查看：repo-agent --sandbox-review {session.directory}\n"
        "回写：在查看命令后加 --apply"
    )


def open_execution_environment(args, workspace_root, store, capabilities, cleanup):
    session = native = None
    if args.sandbox == "docker":
        environment = detect_environment(
            profile=args.sandbox_profile, image=args.sandbox_image or DEFAULT_IMAGE
        )
        sandbox_policy = SandboxPolicy.for_profile(
            environment.profile, image=args.sandbox_image, gpus=args.sandbox_gpus
        )
        check_image_profile(
            sandbox_policy.image,
            environment.profile,
            allow_unlabelled=args.sandbox_image is not None,
        )
        print(f"[沙箱环境：{environment.profile}] {environment.reason}", flush=True)
        previous_sandbox = store.data.get("sandbox") if store.data else None
        if previous_sandbox and store.data["mode"] == "docker":
            if store.data.get("sandbox_healthy") is False:
                raise ValueError("上次沙箱清理未确认；请检查遗留容器后用 --new-session 启动")
            session = SandboxSession.resume(
                previous_sandbox,
                workspace_root,
                sandbox_policy,
                verify_command=args.sandbox_verify_command,
            )
        else:
            session = SandboxSession(
                workspace_root,
                sandbox_policy,
                verify_command=args.sandbox_verify_command,
            )
        cleanup.callback(describe_retained_sandbox, session)
        tools = session.tools(writeback_mode=args.sandbox_writeback)
    elif args.sandbox == "native":
        if (
            store.data
            and store.data["mode"] == "native"
            and store.data.get("sandbox_healthy") is False
        ):
            raise ValueError("上次原生进程清理未确认；请检查遗留进程后用 --new-session 启动")
        native = NativeBackend(
            workspace_root,
            profile=args.sandbox_profile,
            gpus=args.sandbox_gpus,
            project_python=args.project_python,
        )
        cleanup.callback(native.close)
        tools = native.tools()
        chosen = native.execution_context().get("python_environments", {})
        if chosen:
            print(f"项目 Python：{chosen['project']}（{chosen['source']}）")
        platform_label = capabilities.label
        print(
            f"[执行环境：{platform_label} native] 工具断网；直接修改原项目，无副本回写",
            flush=True,
        )
        if capabilities.gpu:
            gpu = native.execution_context()["gpu_access"]
            if gpu["enabled"]:
                print(
                    f"[原生 GPU：{gpu['selection']}] CUDA kernel 自检通过；无显存配额",
                    flush=True,
                )
            else:
                reason = (
                    "已显式关闭 GPU"
                    if args.sandbox_profile == "standard"
                    else "未检测到 NVIDIA CUDA 设备"
                )
                print(f"[原生环境：standard] {reason}", flush=True)
    else:
        tools = create_default_tools(workspace_root)
        print("[执行环境：local] 直接修改原项目；不执行命令、Python 或语言服务器", flush=True)
    return ExecutionEnvironment(tools, session, native)
