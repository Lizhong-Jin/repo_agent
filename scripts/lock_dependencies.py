"""Generate or verify pip exports from the single uv.lock source of truth."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def export_locks(root, uv, *, check=False, offline=False):
    subprocess.run(
        [uv, "lock", "--check" if check else "--upgrade", *(["--offline"] if offline else [])],
        cwd=root,
        check=True,
    )
    for kind, options in (
        ("core", ["--no-default-groups"]),
        ("lsp", ["--no-default-groups", "--extra", "lsp"]),
        ("dev", ["--no-default-groups", "--extra", "dev"]),
        ("build", ["--only-group", "build"]),
    ):
        content = subprocess.run(
            [
                uv,
                "export",
                "--locked",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                *options,
                *(["--offline"] if offline else []),
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        target = root / f"requirements-{kind}.lock"
        if check:
            if target.read_text() != content:
                raise ValueError(f"{target.name} 与 uv.lock 不一致；请重新生成锁文件")
        else:
            target.write_text(content)


def main():
    parser = argparse.ArgumentParser(description="更新或检查 Python 锁文件；普通用户安装无需 uv")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--uv", default=os.environ.get("UV") or shutil.which("uv"))
    args = parser.parse_args()
    if not args.uv:
        parser.error("未找到 uv；安装 uv 或通过 --uv 指定构建工具路径")
    export_locks(Path(__file__).resolve().parents[1], args.uv, check=args.check)


if __name__ == "__main__":
    main()
