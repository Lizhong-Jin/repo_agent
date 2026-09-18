"""Exercise every default language server on tiny self-contained source files.

Run in the built image: python -I -m sandbox.lsp_smoke. The normal sandbox
entrypoint is unchanged. No project dependencies or network access are needed.
"""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.factory import create_default_tools

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cuda", action="store_true", help="Also check CUDA symbols with toolkit installed"
    )
    args = parser.parse_args()
    sources = dict(SOURCES)
    if args.cuda:
        sources["example.cu"] = (
            "#include <cuda_runtime.h>\n"
            "__global__ void example(float* out) { out[threadIdx.x] = 1.0f; }\n"
        )
    with TemporaryDirectory(prefix="lsp-smoke-") as directory:
        root = Path(directory)
        (root / "go.mod").write_text("module example.com/smoke\n\ngo 1.25\n")
        for filename, content in sources.items():
            # Keep Go's package free of unrelated C/C++ translation units.
            folder = root / Path(filename).suffix[1:]
            folder.mkdir()
            (folder / filename).write_text(content, encoding="utf-8")
        tool = next(
            t
            for t in create_default_tools(root, isolated_execution=True)
            if t.definition.name == "get_symbols"
        )
        for filename in sources:
            relative = f"{Path(filename).suffix[1:]}/{filename}"
            result = tool.execute({"path": relative})
            if not result.success:
                raise RuntimeError(f"{filename}: {result.error_code}: {result.error}")
            if not any(s["name"].lower() == "example" for s in result.data["symbols"]):
                raise RuntimeError(f"{filename}: expected symbol missing: {result.data}")
            print(
                f"{filename}: {result.data['language_id']} / {result.data['server_id']} OK",
                flush=True,
            )


if __name__ == "__main__":
    main()
