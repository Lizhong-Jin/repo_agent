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
├── filesystem/mod.rs  # macOS 工作区检查、fd 相对路径遍历与目录枚举
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
# 默认尝试全部四个平台的 release 动态库，不安装扩展
python scripts/build_rust.py build

# 默认尝试全部四个平台的 ABI3 wheel；pip 在隔离环境中准备 maturin
python scripts/build_rust.py wheel
```

默认产物目录如下，已加入 Git 忽略规则；`--output DIR` 可覆盖：

```text
rust_wheels/
├── macos-arm64/librepo_agent_scan.dylib  # build：按本机平台分目录
└── repo_agent_policy_scan-0.2.0-cp311-abi3-<platform>.whl  # wheel
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

构建支持 Linux/macOS 的 x86_64、arm64 四个目标。默认全部尝试，缺少工具链的目标
显示安装提示，其他目标继续构建；只要有目标未完成就返回非零状态，保留成功产物。
不会自动安装系统工具链。使用以下命令选择范围或只检查环境：

```sh
python scripts/build_rust.py wheel --check
python scripts/build_rust.py wheel --target host
python scripts/build_rust.py wheel --target linux-x86_64 --target linux-arm64
python scripts/build_rust.py build --target host
```

- macOS 主机可以构建上述四个目标。macOS 两种架构需要 Xcode Command Line Tools
  （`xcode-select --install`）以及相应 Rust 标准库。
- Linux 主机支持两种 Linux 目标；当前入口不配置 Linux 到 macOS 的 Apple SDK 工具链，
  会提示改用 macOS 主机或现有 CI 构建这两个目标。Windows 扩展尚未实现。
- 跨平台 Linux 构建使用 maturin 内置的 cargo-zigbuild 支持和 Zig 链接器；从
  https://ziglang.org/download/ 安装 Zig 并将 `zig` 放入 PATH。无需单独安装 cargo-zigbuild。
- 缺少目标标准库时按提示执行 `rustup target add <目标>`。全部目标对应
  `x86_64-unknown-linux-gnu`、`aarch64-unknown-linux-gnu`、
  `x86_64-apple-darwin`、`aarch64-apple-darwin`。
- `build --target host` 直接调用 Cargo，不需要 maturin；跨目标的 `build` 通过
  wheel 构建路径处理 ABI/链接配置，再提取唯一动态库，仅保存动态库。
- wheel 的 Linux 目标默认校验 manylinux 2.28，macOS 最低版本为 x86_64 10.15、arm64 11.0。
  Linux 本机编译使用本机链接器；若系统库高于兼容基线，校验会失败，请使用项目 CI 的
  manylinux 容器。`--compatibility` 不会自动创建容器或改变本机系统库。
- `--offline` 只使用本地 Python 构建依赖及 Cargo 缓存；工具链本身也必须预先安装。
  `--no-build-isolation` 需要当前 Python 中已有 maturin，预检会检查并给出安装命令。

跨编译产物不在构建机上尝试导入。`.github/workflows/policy-scan-wheels.yml` 继续在
各目标系统上构建并运行扫描/文件工具契约测试；本机跨编译不替代目标系统的验证。

## macOS Rust 文件系统后端

安装 0.2.0 或更新的本机扩展并设置 `AGENT_NATIVE_SCANNER=rust`，重启 native 后端。
默认仍为 Python；缺少扩展、文件系统 API 不兼容或扫描失败时显式报错，不静默回退。

macOS 接入 `filesystem/mod.rs`：执行命令前检查完整工作区中的普通文件硬链接，
文件工具通过 Rust 进行目录枚举和逐组件 `openat` 路径遍历。工作区 `.venv` 等依赖目录
仍受检查，不扫描 Seatbelt 已通过规则保护的整个系统/Conda 目录。
Seatbelt 的内核隔离策略保持原样，glob 匹配、路径保护判断、文本解码、匹配和预算仍由 Python 负责。

目录打开拒绝符号链接；枚举使用独立目录偏移，重复读取不会漏项；fd 由 RAII/上下文清理。
预检不跟随目录链接，失败关闭，目录扫描支持信号及应用取消；每次调用重新观察文件系统。
工作区检查队列不保留目录 fd，因此深目录不会持有与深度等量的句柄，但仍需从根重新
逐组件打开各目录，这是后续可以优化的开销。
原有 `DirectoryReader` 的元数据和实际文件读取校验继续有效，不以枚举结果代替读取授权。

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
