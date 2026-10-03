# Rust 基础能力

[文档首页](../docs/index.md) · [发行构建](../docs/distribution.md) · [原生沙箱](../docs/native-sandbox.md)

项目的 Rust 构建根目录统一为 `rust/`，Cargo 和 Python 打包配置集中放在这里。
当前只有一个 PyO3 扩展 crate，标准入口为 `src/lib.rs`。入口只负责注册各能力模块，
共享错误转换、文件系统原语和策略扫描实现各自独立组织：

```text
rust/
├── Cargo.toml
├── Cargo.lock
├── build.rs
├── pyproject.toml
├── README.md
├── docs/policy-scan.md
└── src/
    ├── lib.rs                # Python 模块入口
    ├── error.rs              # 共享错误与 Python 异常转换
    ├── directory_batch.rs    # 可复用目录名称块
    ├── path_nodes.rs         # 可回收的父节点/名称存储
    ├── scan_diagnostics.rs   # 结构与容量高水位
    ├── allocation_profile.rs # 可选开发分配统计
    ├── filesystem/
    │   ├── mod.rs            # fd 操作、枚举、macOS 工作区检查
    │   └── metadata.rs       # 相对 stat 与批量元数据
    └── policy_scan/
        ├── mod.rs            # 策略扫描绑定、输入转换与名称规则
        ├── directory.rs      # 策略扫描所需的目录事实读取
        └── engine.rs         # Linux 策略扫描引擎
```

Cargo/Python 发行包名统一为 **`rust-backend`**，Python 模块名为 **`rust_backend`**。
wheel 文件按 Python 规范使用下划线，例如 `rust_backend-0.6.0-…whl`；动态库名称为
`librust_backend.so`（Linux）或 `librust_backend.dylib`（macOS）。
旧 `repo-agent-policy-scan`/`repo_agent_scan` 二进制不能仅改文件名继续使用，需重新构建
并安装新包。Linux 策略扫描 API_VERSION=1；文件系统 FILESYSTEM_API_VERSION=2，
新增批量元数据接口，旧文件系统扩展需重新构建。目前不支持 Windows。

## 编译和打包

在仓库根目录执行（Python >= 3.11，Rust >= 1.85，需 C 链接器）：

```sh
# 默认尝试全部四个平台的 release 动态库，不安装扩展
python scripts/build_rust.py build

# 默认尝试全部四个平台的 ABI3 wheel；pip 在隔离环境中准备 maturin
python scripts/build_rust.py wheel
```

默认产物按扩展版本归档。版本读取自 `rust/pyproject.toml`，须与 Cargo 包版本一致；
与主应用版本独立。`--output DIR` 覆盖根目录，仍写入 `DIR/<版本>/`：

```text
rust_wheels/
└── 0.6.0/
    ├── rust_backend-0.6.0-cp311-abi3-macosx_11_0_arm64.whl
    ├── rust_backend-0.6.0-cp311-abi3-manylinux_2_28_x86_64.whl
    ├── macos-arm64/librust_backend.dylib
    └── linux-x86_64/librust_backend.so
```

Linux 动态库后缀为 `.so`。原始动态库用于编译验证；安装和分发应使用 wheel。
两个命令均使用 release 优化和 Cargo.lock，不会切换运行时扫描器配置。
Cargo 中间文件始终位于工作区外的临时目录，完成后清理；最终文件通过复制保存，
避免 Cargo 的硬链接影响 native 工作区校验。每次构建重新编译，中间产物不缓存，
Cargo 下载缓存仍可复用。已有的其他平台/版本 wheel 会保留；失败不会覆盖已有产物。

安装时将下面的文件名替换为本次命令输出的确切路径，使用 Agent 环境的 Python：

```sh
python -m pip install --no-deps rust_wheels/<版本>/<本次生成的文件名>.whl
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

## Linux/macOS Rust 文件系统后端

安装当前 0.6.0 的本机扩展（策略 API 1、文件系统 API 2）并设置 `AGENT_NATIVE_SCANNER=rust`，重启 native 后端。
默认仍为 Python；缺少扩展、文件系统 API 不兼容或扫描失败时显式报错，不静默回退。

Linux/macOS native 文件工具都接入 `src/filesystem/mod.rs` 和 `metadata.rs`：通过 Rust
进行目录枚举、逐组件 `openat` 路径遍历和批量元数据读取。两个平台使用相同的后端
选择与公共 FileAccess 注入流程，local/Docker 模式不走这条 native 文件服务。

执行命令前的检查仍按平台区分：Linux 使用 `policy_scan`，同时扫描读取根并验证
工作区硬链接和特殊文件；macOS 使用 `filesystem::check_workspace` 检查工作区
普通文件硬链接。工作区 `.venv` 等依赖目录仍受检查；macOS 不扫描 Seatbelt
已通过规则保护的整个系统/Conda 目录。
Seatbelt 的内核隔离策略保持原样，glob 匹配、路径保护判断、文本解码、匹配和预算仍由 Python 负责。

目录打开拒绝符号链接；枚举使用独立目录偏移，重复读取不会漏项；fd 由 RAII/上下文清理。
预检不跟随目录链接，失败关闭，目录扫描支持信号及应用取消；每次调用重新观察文件系统。
macOS 工作区预检与两平台文件工具遍历在单次扫描内最多缓存 32 个目录句柄，从最近已打开的祖先
进行相对访问；超过上限时淘汰最久未用的句柄。根目录、活动 reader 和枚举临时句柄
另占常数个 fd。扫描结束、提前停止或异常时关闭缓存，写操作仍重新验证父目录。

文件工具通过 `stat_many` 每批最多读取 128 项元数据，保留每项错误、符号链接本身的
属性及精确纳秒时间戳。list/find 复用候选元数据，避免终端匹配和展示时再次逐文件
打开父目录。批量减少语言边界调用，并非把 128 次 fstatat 合并为一次系统调用。
Linux 策略扫描的工作区文件检查也使用已打开目录的相对 fstatat。
原有 `DirectoryReader` 的元数据和实际文件读取校验继续有效，不以枚举结果代替读取授权。

## 安装与发行集成

源码安装、手工命令和发行构建共用 `installer/rust_extension.py`。源码安装在核心
安装成功后尝试构建并安装扩展，保留生成的 wheel 到源码根目录 `rust_wheels/<版本>/`。
发行构建先查找 `--rust-wheelhouse`（未指定则使用 `--wheelhouse`），再查找项目
`rust_wheels/<当前版本>/`；均无匹配文件时，仅为本机平台尝试构建，产物也保存在此目录。
选择依然校验包名、扩展版本、Python ABI 和平台，显式目录优先。`--rust-wheelhouse`
可指定包含版本子目录的根目录，或直接指定某版本目录/平铺 wheelhouse；不会递归搜索其他版本。

```sh
# 先将所需平台的 CI wheel 汇总到 rust_wheels/<版本>/，再构建完整发行包
python scripts/build_release.py --require-rust
# 或只构建一个平台
python scripts/build_release.py --target macos-arm64 --require-rust
```

完整发行归档仍输出到 `dist/`。最终用户安装发行包时仅安装经哈希校验的预编译扩展，
不运行编译器。未显式设置 `AGENT_NATIVE_SCANNER=rust` 时仍使用 Python。

## 验证

先将本机 wheel 安装到运行测试的 Agent Python，核验两个接口版本，再运行策略与文件工具契约：

```sh
python -c 'import rust_backend; assert rust_backend.API_VERSION == 1; assert rust_backend.FILESYSTEM_API_VERSION == 2'
python -m pytest -q tests/test_rust_policy_scan.py tests/test_policy_scan_contract.py tests/test_rust_filesystem.py tests/test_directory_batches.py tests/test_search_scan_contract.py tests/test_native_file_layer.py tests/test_linux_native.py
export CARGO_TARGET_DIR="$(mktemp -d)"
export PYO3_PYTHON="$(python -c 'import sys; print(sys.executable)')"
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
cargo test --manifest-path rust/Cargo.toml --locked
```

缺少扩展时部分 Python 用例会跳过，不能将这种结果当成 Rust 验证通过。真实隔离与 GPU 用例仍需[对应开关和环境](../docs/development.md#开发环境与验证)。Cargo 命令在依赖缓存齐全时可加 `--offline`（格式检查不需要下载依赖）。

Rust 单元测试会嵌入 Python，除了头文件还需要可链接、可加载的 Python 共享库。受管独立 Python 的 `sysconfig` 可能保留构建时的 `/install/lib`，导致链接器找不到 `libpython`；应以实际运行时目录为准，将真实库目录加入链接搜索路径（`-L native=…`），并在 macOS 设置 `DYLD_FALLBACK_LIBRARY_PATH`、Linux 设置 `LD_LIBRARY_PATH`。这属于测试链接环境准备；不能用已经安装的扩展导入成功代替 Rust 源码单元测试。

Linux 策略扫描接口与语义见 [policy_scan](docs/policy-scan.md)。

## 目录优化基准

```sh
python scripts/benchmark_directory_io.py --repeats 7
python scripts/benchmark_directory_io.py --engine python
```

临时构造宽目录和深目录，测量 list/find/search 完整文件工具调用及 Rust 工作区预检，
输出 JSON 中位数并校验两个后端结果一致。比较改动前后时保持参数和主机负载一致；
热缓存测量不包含隔离启动，也不能替代真实 WSL/Conda 环境的性能记录。


## 0.4.0 内存布局与诊断

0.4.0 增加可复用的 256 项目录工作块、可回收路径节点、固定布局统计、ASCII 匹配缓冲区，
以及文件工具内部使用的精简元数据。原 `stat/stat_many`、Linux API_VERSION=1 和
FILESYSTEM_API_VERSION=2 保持兼容；旧 API 2 扩展继续使用原有完整 Rust 元数据路径。
普通发行 wheel 不启用分配统计，开发构建可选择 `allocation-profile`。

测量命令、指标口径、资源边界与后续有序并行设计见 [内存布局与测量](docs/memory-layout.md)。

## 0.5.0 有限并行预检与策略扫描

Rust 后端新增 `AGENT_SCAN_WORKERS=2`，默认上限 2，允许 1～8；1 保持串行扫描。
只在有目录分支可并行时启动每次调用的线程池，Linux 工作区/系统读取根策略扫描和
macOS 工作区预检均接入。文件工具搜索/读取仍按原顺序执行。

配置、无序汇总后的稳定策略顺序、取消回收、资源上限及跨线程计时口径见
[有限并行扫描](docs/scan-parallel.md)。分支目录基准：
`python scripts/benchmark_scan_parallel.py --workers 1 2 4`。

## 0.6.0 目录任务批处理

默认线程上限仍为 2；新增 `AGENT_SCAN_BATCH_SIZE=32`（1～64），一次消息合并多个
已发现目录，减少小目录扫描的线程交接。1 为逐目录对照，单线程保留原串行实现。
Linux 逐个打开批内目录；macOS 合并同父目录任务并共享父句柄，不预打开整批子目录。

`python scripts/benchmark_scan_parallel.py` 现在默认使用嵌套小目录树，并交错测量
墙钟、CPU、上下文切换和批次诊断。`--root /path/to/conda` 可只读测量真实目录。
详细语义与资源边界见 [有限并行扫描](docs/scan-parallel.md)。
