# 构建与分发

[文档首页](index.md) · [开发与验证](development.md) · [用户安装说明](installation.md)

面向发布维护者，命令在源码仓库根目录运行。构建器只生成平台完整包，内置独立 Python、Agent wheel、安装器和对应平台的运行依赖；不再提供轻量发行包构建。所有产物按版本、平台分目录保存，不自动上传发布。

## 构建独立发行版与更新依赖

维护者需要完整 Python 3.13 和 uv；推荐使用源码安装生成的 `.venv`。uv 需单独准备或通过 `--uv` 指定路径，普通用户安装发行包无需 uv 或预装 Python。

每次发布先更新 `pyproject.toml` 的版本号，再同步依赖锁文件：

```bash
# 同步 Python 锁文件及带哈希的 pip 清单，默认保留已有依赖版本
.venv/bin/python scripts/lock_dependencies.py
# 主动升级允许范围内的 Python 依赖版本，并同步 pip 清单
.venv/bin/python scripts/lock_dependencies.py --upgrade
# 仅检查 pyproject、uv.lock 与四个 pip 清单是否一致
.venv/bin/python scripts/lock_dependencies.py --check
# 修改 dependencies/node/package.json 后更新 npm 锁文件
npm install --package-lock-only --ignore-scripts --prefix dependencies/node
```

默认模式运行 `uv lock`，优先沿用 `uv.lock` 中已有的版本；新增依赖或修改约束时，仍可能调整相关版本。只有显式传入 `--upgrade` 才主动升级依赖，建议将依赖升级与普通版本发布分开进行并验证。`--check` 只检查一致性，不修改锁文件，不能与 `--upgrade` 同时使用。上述命令均不安装或更新当前 `.venv` 中的包。

### 默认构建全部平台

```bash
.venv/bin/python scripts/build_release.py
```

不加 `--target` 时，按 `runtime/python.lock` 中声明的平台逐个构建，目前为：

| 平台标识 | 目标系统 |
| --- | --- |
| `macos-arm64` | Apple Silicon Mac |
| `macos-x86_64` | Intel Mac |
| `linux-arm64` | ARM64 Linux |
| `linux-x86_64` | x86_64 Linux，含对应架构的 WSL2 |
| `windows-x86_64` | Windows 10 1809+ / Windows 11 x86_64，ZIP |

一次构建只校验一次依赖锁、创建一次构建环境、构建一次通用 Agent wheel；随后为每个平台分别收集 Python 和依赖，生成独立清单及压缩包，避免混入其他平台的 wheels。

只构建一个平台，或更换输出根目录：

```bash
.venv/bin/python scripts/build_release.py --target windows-x86_64
.venv/bin/python scripts/build_release.py --target macos-arm64
.venv/bin/python scripts/build_release.py --target linux-x86_64 --output /path/to/releases
```

Windows 构建机可运行 `python -X utf8 scripts/build_release.py --target windows-x86_64`（Python 3.13，已安装 uv 与构建准备工具）。

完整包可从其他平台组装，但必须按目标 ABI 收集 wheels。没有匹配 wheel 时失败，不自动从源码编译第三方依赖。支持的系统版本及解释器约束见 [Python 环境](python-environments.md#受管运行时)。跨平台构建成功不代表已经通过目标系统的执行验证。

## 产物目录与包内容

macOS/Linux 发行包仅提供顶层 `install-release.sh`，安装、恢复和备用卸载共用内部的 `scripts/installer-entry.sh`；源码仓库继续保留 `install.sh` / `uninstall.sh`。新构建清单使用 schema 4，显式记录目标平台并校验 `installer/`、`configuration/` 等安装所需文件；新版安装器仍可读取 schema 1～3 的旧发行包。构建器在写出压缩包前使用安装器的校验逻辑验证清单，避免发布无法安装的包。修改后需要重新构建发行包，已有压缩包不会自动变化。发布时应按上面的流程递增版本号。

Windows ZIP 同样使用 schema 4（旧版为 schema 3），记录 `windows-x86_64` 目标并仅提供顶层 `install_release.ps1`；同一脚本通过 `--uninstall`、`--check`、`--recover` 完成维护，不附带另一份卸载入口或 POSIX Shell 安装脚本。共用 Python 安装事务、配置保留和归属检查。

默认输出到 `dist/<版本>/<平台>/`；`--output` 仅替换 `dist` 这一层，版本来自 `pyproject.toml`。以下以源码当前版本 0.1.4 演示目录，不表示已有产物与未提交修改同步：

```text
dist/
└── 0.1.4/
    ├── macos-arm64/
    │   ├── repo-agent-0.1.4-macos-arm64.tar.gz
    │   └── repo-agent-0.1.4-macos-arm64.tar.gz.sha256
    ├── macos-x86_64/
    │   ├── repo-agent-0.1.4-macos-x86_64.tar.gz
    │   └── repo-agent-0.1.4-macos-x86_64.tar.gz.sha256
    ├── linux-arm64/
    │   ├── repo-agent-0.1.4-linux-arm64.tar.gz
    │   └── repo-agent-0.1.4-linux-arm64.tar.gz.sha256
    ├── linux-x86_64/
    │   ├── repo-agent-0.1.4-linux-x86_64.tar.gz
    │   └── repo-agent-0.1.4-linux-x86_64.tar.gz.sha256
    └── windows-x86_64/
        ├── repo-agent-0.1.4-windows-x86_64.zip
        └── repo-agent-0.1.4-windows-x86_64.zip.sha256
```

Agent 的 `py3-none-any` wheel 作为包内组件放在 `wheels/`，不再单独输出到发布目录。每个压缩包只有一个 `repo-agent-<版本>-<平台>/` 顶层文件夹，内含运行时锁文件、平台标记和 `wheelhouse/`。macOS/Linux 内含固定的 `runtime/python.tar.gz`；Windows 上游 tar.gz 经固定 SHA256 校验后在构建阶段展开为 `runtime/python/python.exe` 等文件，避免安装引导依赖系统 Python 或 tar。当前构建器会移除有对应源码的 `.pyc`，遇到无源码字节码则拒绝构建；Windows 维护入口也校验和清理运行时缓存，具体边界见[安装说明](installation.md#windows-x86_64-zip-安装)。Windows 只收集 local/Docker 核心依赖，并按 Windows 目标评估锁文件的平台条件，不携带 native 语言服务依赖。完整包不包含项目的 PyTorch/CUDA 依赖、系统工具链或开发用 pytest/Ruff；后者通过源码开发材料准备。

发行包通过明确的文件清单收集源码、默认模板和资源，不复制构建机器的 `.venv`、`.git`、项目 `.env`、日志或缓存。wheel 内置用于重建 Docker 镜像的源码资源。`release.json` 记录版本、wheel 和各文件 SHA256，安装前逐项校验。macOS/Linux 的上游 Python 内部链接保留在固定哈希校验的内层归档中；Windows 展开步骤拒绝链接。外层发行归档只接受普通文件/目录。

安装时从内层归档（Windows 为已展开文件）部署共享运行时，再在最终路径创建 `.venv`，不搬迁已经创建的虚拟环境。构建输出目录与安装目录是两回事；安装后仍位于用户数据目录的 `repo-agent/versions/<版本>/`。

## Rust 扩展与发行集成

Rust 源码统一位于 `rust/`。手工编译使用 `python scripts/build_rust.py build`，构建 wheel 使用 `python scripts/build_rust.py wheel`，两者默认尝试全部四种 Linux/macOS 目标，缺少工具链时提示安装并返回非零状态，`--target host` 可仅构建本机。最终产物默认写入 `rust_wheels/<版本>/`；完整发行归档仍写入 `dist/`。详见 [Rust 构建入口](../rust/README.md)。

可选 Rust 后端以独立的平台 wheel `rust-backend` 按构建材料随完整发行包分发，主应用仍为通用 Python wheel。构建器优先从 `--rust-wheelhouse` 指定目录选择扩展；未指定时先搜索 `--wheelhouse`；随后搜索项目 `rust_wheels/<当前版本>/`。显式目录支持版本子目录结构或平铺的 wheelhouse。按扩展版本、目标平台和 CPython ABI3 筛选，Linux 只接受符合当前发行基线的 manylinux wheel，不接受依赖本机环境的 `linux_*` 标签。找到后复制到包内 `wheels/`，通过 `release.json` 的 `rust_wheel` 字段和文件哈希登记。

没有匹配的预编译 wheel 时，只对与构建机相同的平台尝试本机编译，生成的 wheel 保存在 `rust_wheels/<版本>/`；其他平台需要预先提供 wheel，不会自动跨平台编译。缺少编译器、构建失败或没有兼容产物时，默认提示并生成仅含 Python 扫描器的包。正式发布可使用 `--require-rust`，确保所选 Linux/macOS 目标均含 Rust 扩展；任一缺失都会在替换发行归档前失败。Windows 当前不支持此扩展，不要求也不附带它。

```bash
# 将 CI 产出的各平台 Rust wheel 汇总到项目 rust_wheels/<版本>/ 后打包
.venv/bin/python scripts/build_release.py --require-rust
# 只构建一个平台，同样可使用预编译 wheel，无需本机安装 Rust 编译器
.venv/bin/python scripts/build_release.py --target linux-arm64 --rust-wheelhouse /path/to/rust_wheels --require-rust
```

工作流 `.github/workflows/policy-scan-wheels.yml` 构建 Linux/macOS 的 x86_64、arm64 四种 wheel，执行加载及差分测试后保存为工作流产物；不自动发布。Linux 使用 manylinux 2.28，macOS 最低版本为 x86_64 的 10.15 和 arm64 的 11.0。源码归档仍包含构建输入。构建细节见 [Rust 扫描器说明](../rust/docs/policy-scan.md)。

发行包安装直接安装已登记并校验的 Rust wheel，不下载、不编译，也不需要用户安装 Rust/Cargo/maturin。扩展为可选组件，安装或加载失败不会撤销核心安装；未附带扩展的发行包没有 `rust_wheel` 字段，可继续安装核心程序。若清单已登记扩展但文件缺失或哈希不符，发行完整性校验会拒绝安装；这不同于核心安装提交后可选扩展的安装/导入失败。默认扫描器仍为 `python`，安装成功后可显式选择 `rust`。

## 离线构建

离线安装、组装发行包和编译 Rust 的依赖不同：

| 操作 | 提前准备 | 是否需要 Rust 编译器 |
| --- | --- | --- |
| 最终用户安装完整包 | 对应平台归档、所选模式的系统依赖；Docker 模式另备镜像 | 不需要；包内若含扩展则安装预编译 wheel |
| 从源码组装主发行包 | 构建机 Python 3.13、uv、运行时原始归档、构建及目标运行依赖 wheels | 核心打包不需要；缺少本机扩展 wheel 时会尝试可选编译，失败默认告警 |
| 组装必须包含 Rust 的包 | 上述材料及每个所选 Linux/macOS 目标的匹配扩展 wheel，使用 `--require-rust` | 提供齐全的预编译 wheel 时不需要 |
| 从源码编译扩展 | Rust 工具链、链接器/SDK、目标标准库、Cargo 依赖缓存；wheel 构建另需 maturin | 需要；跨目标 Linux 构建还需 Zig |

单平台构建沿用本地运行时文件和 wheelhouse：

```bash
.venv/bin/python scripts/build_release.py --target linux-x86_64 --offline \
  --runtime-archive /path/to/kit/runtime/python.tar.gz \
  --wheelhouse /path/to/kit/wheelhouse
```

如需同时保证包含 Rust，先取得当前扩展版本的对应平台 wheel，再使用：

```bash
.venv/bin/python scripts/build_release.py --target linux-x86_64 --offline \
  --runtime-archive /path/to/kit/runtime/python.tar.gz \
  --wheelhouse /path/to/kit/wheelhouse \
  --rust-wheelhouse /path/to/rust_wheels --require-rust
```

显式 `--rust-wheelhouse` 也作为尝试本机扩展编译时的 Python 构建依赖来源；若依赖该编译步骤，目录中还需提供 maturin wheel，不能只准备主应用的四份锁文件。已提供匹配扩展时直接使用，不会调用编译器。Rust 自身的 Cargo 缓存不由 Python wheelhouse 替代。

全平台离线构建将 `--runtime-archive` 指向目录，文件名必须与平台对应：

```text
/path/to/runtime-archives/
├── macos-arm64.tar.gz
├── macos-x86_64.tar.gz
├── linux-arm64.tar.gz
├── linux-x86_64.tar.gz
└── windows-x86_64.tar.gz
```

```bash
.venv/bin/python scripts/build_release.py --offline \
  --runtime-archive /path/to/runtime-archives \
  --wheelhouse /path/to/all-platform-wheels
```

这些文件是各平台原始的 Python 上游归档，可从[离线开发材料](development.md#离线开发环境)中的 `runtime/python.tar.gz` 复制并改名，内容必须匹配 `runtime/python.lock` 的 SHA256。Windows 输入也必须使用锁文件中原始上游 `.tar.gz`，不能将最终 ZIP 当作运行时输入；Windows 开发材料会展开运行时，因此离线构建时应另行保留原始 tar.gz。共享 wheelhouse 应汇集所有目标的运行依赖以及构建机器可用的构建依赖；当前构建依赖均有通用 wheels。同名、同内容的通用 wheel 只保留一份。

单个运行时文件必须搭配 `--target`；提供目录时，开始构建前检查所选平台的归档是否齐全。`--offline` 同时禁止 pip 联网并传给 uv 锁文件检查；缺少匹配 wheels 或哈希不符时失败。开发材料准备脚本仍按单个平台工作，省略它的 `--target` 时选择当前平台，具体用法见开发指南。

## 校验、失败处理与安装验收

构建前检查依赖锁与分发清单；打包后核对实际 wheel、内置 Docker 上下文和完整归档的文件列表及内容。各平台分别写入临时归档，通过校验后替换正式产物。同版本同平台重建会覆盖其产物，不清理其他平台或版本。

任一平台失败时整个命令返回非零；本次先前已完成的平台包保留，依赖准备或归档校验失败不会覆盖该平台的已有压缩包。可用 `--target` 单独重试失败平台。旧布局下的本地产物不自动迁移或删除，新构建统一写入版本目录；发布时从对应版本目录选择文件，避免误用旧包。

```bash
.venv/bin/python -m pytest -q tests/test_release_builder.py tests/test_build_manifest.py tests/test_release_distribution.py
```

真实安装验收选择与当前机器匹配的完整包。以下示例使用临时用户目录和受管 Python 离线安装，删除下载目录后检查启动、配置恢复、doctor、Docker 构建上下文和卸载；Docker 入口使用模拟程序，不构建真实镜像、不调用模型 API：

```bash
REPO_AGENT_TEST_ARCHIVE="$PWD/dist/0.1.4/macos-arm64/repo-agent-0.1.4-macos-arm64.tar.gz" \
  .venv/bin/python -m pytest -q tests/test_release_distribution.py
```

Windows 在真实 Windows 主机上执行以下验收，覆盖 PowerShell 5.1 引导、离线安装、删除下载目录后运行 `.exe`、重装保留配置、恢复与卸载：

```powershell
$env:REPO_AGENT_WINDOWS_ARCHIVE = (Resolve-Path 'dist/0.1.4/windows-x86_64/repo-agent-0.1.4-windows-x86_64.zip').Path
python -m pytest -q tests/test_windows_release.py
```

`.github/workflows/host-files.yml` 配置了 Windows ZIP 构建与上述实际生命周期验收，前序步骤成功后上传 ZIP 与校验文件。非 Windows 测试仅覆盖格式、命令归属与事务契约，真实 Windows 用例会跳过，不能代替 Windows 验收。

上述验收使用 local 模式验证安装生命周期。macOS native、Linux native、WSL GPU 仍须分别进行[真实沙箱验证](native-sandbox.md#验证)，不能以打包成功替代。

运行时版本、上游 URL 和 SHA256 统一维护在 `runtime/python.lock`。更新时重新验证所有发布平台及标准库组件，保留上游许可文件，不要仅更新下载 URL。

## 统一分发清单

`build_manifest.py` 是分发文件选择与资源路径映射的唯一维护入口，只依赖 Python 标准库。`build_support.py` 负责接入 setuptools；`scripts/build_release.py` 负责构建和组装，两者共用该清单。

- 在已有源码包内增删 `.py` 文件会自动改变分发文件集合；新增顶层包需修改 `PACKAGES`。
- 保持包内相对路径的 JSON、模板等资源，加入 `PACKAGE_RESOURCES`；复制到 wheel 内特定位置的资源，加入 `RESOURCE_FILES` 的来源／目标映射。
- Rust 的构建配置、源码和说明由 `NATIVE_SOURCES` 显式列出；新增模块时补充清单，并同步生成分发配置。
- 构建辅助文件在 `BUILD_FILES` 中声明；安装和恢复脚本在 `INSTALL_SCRIPTS` 中声明。wheel/Docker 输入与安装器文件按用途组合，安装脚本不必放进 wheel。

新增、删除模块或调整上述声明后执行：

```bash
python3 build_manifest.py --write
python3 build_manifest.py
.venv/bin/python -m pytest -q tests/test_build_manifest.py tests/test_release_distribution.py
python3 scripts/build_release.py
```

`--write` 自动更新 `MANIFEST.in`、`.dockerignore` 和 `pyproject.toml` 中带标记的 setuptools 配置区块。其他项目配置保持原样。将清单和这些生成文件一起提交；不要单独手改生成文件。默认不带参数只检查，不修改文件。

Docker 过滤文件按实际选中的文件生成精确允许列表，新增 `.py` 文件后也需要重新生成。Dockerfile 通过该列表复制上下文，不再重复列出各个源码包和根目录资源。包内 `.env`、虚拟环境、缓存、日志及未声明的 JSON 等文件不会被顺带收集。

wheel、sdist、发行包构建及源码模式的镜像重建入口都会检查清单与配置一致性，缺少必需文件或出现符号链接会失败。发行包生成前还会比较实际 wheel、内置 Docker 上下文的文件列表和内容；归档写入完成后再验证归档内容。重复构建会清理旧包构建目录，避免已删除模块残留。源码包也按统一清单过滤文件，并保留 setuptools 元数据。

`tests/test_build_manifest.py` 包含实际 wheel 构建、删除模块后重复构建、sdist 解包后重建 wheel、新增 JSON 资源、生成配置漂移和未声明文件排除等回归；默认使用测试环境中已有的 setuptools/wheel，不联网安装构建工具。

`requirements-dev.lock` 从 `pyproject.toml` 的 `dev` extra 导出，包含 pytest、Ruff 及其依赖；源码安装自动使用该清单，发行版安装不使用它。它随统一构建清单提供，不表示普通用户运行环境会安装开发工具。

## 安装代码与资源归属

安装、恢复和发行校验实现位于 `installer/`；wheel 资源位于 `installer/resources/`。Shell 和 PowerShell 引导直接调用 `installer/` 中的入口，不再提供上述实现的 `cli/` 包装层。schema 4 校验新路径，schema 1～3 继续按各自原有的 `cli/` 路径校验。分发引导同时包含 `configuration/` 和 `host_support/`；安装仅导入配置存储，不加载模型配置或终端依赖。目录职责见[代码组织](code-organization.md)。
