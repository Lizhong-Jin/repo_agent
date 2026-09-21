"""Exercise every default language server on tiny self-contained source files.

Run in the built image: python -I -m sandbox.lsp_smoke. The normal sandbox
entrypoint is unchanged. No project dependencies or network access are needed.
"""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.factory import create_default_tools

FAMILIES = {
    ".py": "python",
    ".js": "typescript",
    ".jsx": "typescript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".c": "cpp",
    ".cpp": "cpp",
    ".cu": "cpp",
}

SOURCES = {
    "example.py": "def example():\n    return 1\n",
    "example.js": "export function example() { return 1; }\n",
    "example.jsx": "export function Example() { return <div />; }\n",
    "example.ts": "export function example(): number { return 1; }\n",
    "example.tsx": "export function Example() { return <div />; }\n",
    "example.go": "package example\nfunc Example() int { return 1 }\n",
    "example.c": "int example(void) { return 1; }\n",
    "example.cpp": "int example() { return 1; }\n",
}


def check_services(*, mode="direct", languages=("python", "typescript", "go", "cpp"), cuda=False):
    sources = dict(SOURCES)
    if cuda:
        sources["example.cu"] = (
            "#include <cuda_runtime.h>\n"
            "__global__ void example(float* out) { out[threadIdx.x] = 1.0f; }\n"
        )
    sources = {
        name: content
        for name, content in sources.items()
        if FAMILIES[Path(name).suffix] in languages
    }
    rows = []
    # Explicit /tmp is writable in both read-only Docker containers and native workers.
    with TemporaryDirectory(prefix="lsp-smoke-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        (root / "go.mod").write_text("module example.com/smoke\n\ngo 1.25\n")
        for filename, content in sources.items():
            folder = root / Path(filename).suffix[1:]
            folder.mkdir()
            (folder / filename).write_text(content, encoding="utf-8")
        backend = None
        try:
            if mode == "native":
                from .native import NativeBackend

                backend = NativeBackend(root)
                tools = backend.tools()
            else:
                tools = create_default_tools(root, isolated_execution=True)
            tool = next(t for t in tools if t.definition.name == "get_symbols")
            for filename in sources:
                relative = f"{Path(filename).suffix[1:]}/{filename}"
                result = tool.execute({"path": relative})
                found = result.success and any(
                    s["name"].lower() == "example" for s in result.data["symbols"]
                )
                rows.append(
                    (
                        "OK" if found else "ERROR",
                        filename,
                        (
                            "符号查询通过"
                            if found
                            else "语言服务无法返回测试符号；请检查依赖及沙箱权限"
                        ),
                    )
                )
        except (OSError, ValueError, StopIteration):
            rows.append(
                (
                    "ERROR",
                    "原生沙箱" if mode == "native" else "语言服务",
                    "无法启动诊断；请检查依赖和系统是否允许沙箱执行",
                )
            )
        finally:
            if backend is not None:
                backend.close()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--mode", choices=["direct", "native"], default="direct")
    parser.add_argument("--languages", default="python,typescript,go,cpp")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    languages = args.languages.split(",")
    if not languages or any(name not in set(FAMILIES.values()) for name in languages):
        parser.error("不支持的诊断语言")
    rows = check_services(mode=args.mode, languages=languages, cuda=args.cuda)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for level, name, detail in rows:
            print(f"[{level}] {name}: {detail}", flush=True)
    if any(row[0] == "ERROR" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
