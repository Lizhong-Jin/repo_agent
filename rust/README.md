# Rust 基础能力

项目的 Rust 构建根目录统一为 `rust/`，Cargo 和 Python 打包配置集中放在这里。
当前只有一个 PyO3 扩展 crate，`policy_scan/` 保留策略扫描源码和模块说明，
通过 `Cargo.toml` 的 `[lib].path` 指向 `policy_scan/src/lib.rs`。
后续同一扩展的目录扫描和文件系统基础能力可按职责增加源码模块，共用根目录构建配置。
如果未来需要多个独立 crate，再引入 Cargo workspace；各独立 crate 仍需自己的包清单。

```text
rust/
├── Cargo.toml
├── Cargo.lock
├── build.rs
├── pyproject.toml
├── README.md
└── policy_scan/
    ├── README.md
    └── src/
        ├── lib.rs
        ├── fs.rs
        └── engine.rs
```

迁移只调整源码路径，不改变发行包名 `repo-agent-policy-scan`、模块名
`repo_agent_scan` 或扫描器的语义。目前不支持 Windows。

## 编译和打包

在仓库根目录执行（Python >= 3.11，Rust >= 1.85，需 C 链接器）：

```sh
# 仅编译本机 release 动态库，不需要 maturin，不安装扩展
python scripts/build_rust.py build

# 构建可安装的本机 ABI3 wheel；pip 自动在隔离环境中准备 maturin
python scripts/build_rust.py wheel
```

默认产物目录如下，已加入 Git 忽略规则；`--output DIR` 可覆盖：

```text
rust_wheels/
├── macos-arm64/librepo_agent_scan.dylib  # build：按本机平台分目录
└── repo_agent_policy_scan-0.1.0-cp311-abi3-<platform>.whl  # wheel
```

Linux 动态库后缀为 `.so`。原始动态库用于编译验证；安装和分发应使用 wheel。
两个命令均使用 release 优化和 Cargo.lock，不会切换运行时扫描器配置。
Cargo 中间文件始终位于工作区外的临时目录，完成后清理；最终文件通过复制保存，
避免 Cargo 的硬链接影响 native 工作区校验。每次构建重新编译，中间产物不缓存，
Cargo 下载缓存仍可复用。已有的其他平台/版本 wheel 会保留；失败不会覆盖已有产物。

安装时将下面的文件名替换为本次命令输出的确切路径，使用 Agent 环境的 Python：

```sh
python -m pip install --no-deps rust_wheels/<本次生成的文件名>.whl
```

同目录可存放多平台 wheel，不要用 `*.whl` 一次安装全部平台文件。

## 离线与发行包

```sh
# 当前环境已有 maturin>=1.9,<2，且 Cargo 依赖已缓存
python scripts/build_rust.py wheel --offline --no-build-isolation
# 保留构建隔离：从本地目录提供 maturin wheel
python scripts/build_rust.py wheel --offline --wheelhouse /path/to/build-dependencies
# Linux 发行基线校验：应在符合基线的构建环境中运行
python scripts/build_rust.py wheel --compatibility manylinux_2_28
```

`--compatibility` 让 maturin 检查兼容性，不会自动建立 manylinux 容器或降低所依赖的
glibc 版本。正式跨平台产物使用 `.github/workflows/policy-scan-wheels.yml`；其 Linux
构建保留 maturin-action 的 manylinux 容器，输出也统一到 `rust_wheels/`。
本机命令不提供跨平台编译功能。macOS 发行基线为 x86_64 10.15、arm64 11.0。

源码安装、手工命令和发行构建共用 `installer/rust_extension.py`。源码安装在核心
安装成功后尝试构建并安装扩展，保留生成的 wheel 到源码根目录 `rust_wheels/`。
发行构建先查找 `--rust-wheelhouse`（未指定则使用 `--wheelhouse`），再查找项目
`rust_wheels/`；均无匹配文件时，仅为本机平台尝试构建，产物也保存在此目录。
选择依然校验扩展版本、Python ABI 和平台，显式目录优先。

```sh
# 先将所需平台的 CI wheel 汇总到 rust_wheels/，再构建完整发行包
python scripts/build_release.py --require-rust
# 或只构建一个平台
python scripts/build_release.py --target macos-arm64 --require-rust
```

完整发行归档仍输出到 `dist/`。最终用户安装发行包时仅安装经哈希校验的预编译扩展，
不运行编译器。未显式设置 `AGENT_NATIVE_SCANNER=rust` 时仍使用 Python。

扫描契约和测试方式见 [policy_scan](policy_scan/README.md)。
