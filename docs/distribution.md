# 构建与分发

[文档首页](index.md) · [开发与验证](development.md) · [用户安装说明](installation.md)

面向发布维护者，命令在源码仓库根目录运行。构建器只生成平台完整包，内置独立 Python、Agent wheel、安装器和对应平台的运行依赖；不再提供轻量发行包构建。所有产物按版本、平台分目录保存，不自动上传发布。

## 构建独立发行版与更新依赖

维护者需要完整 Python 3.13 和 uv；推荐使用源码安装生成的 `.venv`。uv 需单独准备或通过 `--uv` 指定路径，普通用户安装发行包无需 uv 或预装 Python。

每次发布先更新 `pyproject.toml` 的版本号，再同步依赖锁文件：

```bash
# 重新解析并更新 Python 依赖版本及带哈希的 pip 清单（默认升级）
.venv/bin/python scripts/lock_dependencies.py
# 仅检查 pyproject、uv.lock 与四个 pip 清单是否一致
.venv/bin/python scripts/lock_dependencies.py --check
# 修改 dependencies/node/package.json 后更新 npm 锁文件
npm install --package-lock-only --ignore-scripts --prefix dependencies/node
```

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

一次构建只校验一次依赖锁、创建一次构建环境、构建一次通用 Agent wheel；随后为每个平台分别收集 Python 和依赖，生成独立清单及压缩包，避免混入其他平台的 wheels。

只构建一个平台，或更换输出根目录：

```bash
.venv/bin/python scripts/build_release.py --target macos-arm64
.venv/bin/python scripts/build_release.py --target linux-x86_64 --output /path/to/releases
```

完整包可从其他平台组装，但必须按目标 ABI 收集 wheels。没有匹配 wheel 时失败，不自动从源码编译第三方依赖。支持的系统版本及解释器约束见 [Python 环境](python-environments.md#受管运行时)。跨平台构建成功不代表已经通过目标系统的执行验证。

## 产物目录与包内容

默认输出到 `dist/<版本>/<平台>/`；`--output` 仅替换 `dist` 这一层，版本来自 `pyproject.toml`。例如：

```text
dist/
└── 0.1.1/
    ├── macos-arm64/
    │   ├── repo-agent-0.1.1-macos-arm64.tar.gz
    │   └── repo-agent-0.1.1-macos-arm64.tar.gz.sha256
    ├── macos-x86_64/
    │   ├── repo-agent-0.1.1-macos-x86_64.tar.gz
    │   └── repo-agent-0.1.1-macos-x86_64.tar.gz.sha256
    ├── linux-arm64/
    │   ├── repo-agent-0.1.1-linux-arm64.tar.gz
    │   └── repo-agent-0.1.1-linux-arm64.tar.gz.sha256
    └── linux-x86_64/
        ├── repo-agent-0.1.1-linux-x86_64.tar.gz
        └── repo-agent-0.1.1-linux-x86_64.tar.gz.sha256
```

Agent 的 `py3-none-any` wheel 作为包内组件放在 `wheels/`，不再单独输出到发布目录。每个压缩包只有一个 `repo-agent-<版本>-<平台>/` 顶层文件夹，内含固定的 `runtime/python.tar.gz`、运行时锁文件、平台标记和 `wheelhouse/`。完整包不包含项目的 PyTorch/CUDA 依赖、系统工具链或开发用 pytest/Ruff；后者通过源码开发材料准备。

发行包通过明确的文件清单收集源码、默认模板和资源，不复制构建机器的 `.venv`、`.git`、项目 `.env`、日志或缓存。wheel 内置用于重建 Docker 镜像的源码资源。`release.json` 记录版本、wheel 和各文件 SHA256，安装前逐项校验。上游 Python 内部的链接保留在固定哈希校验的内层归档中，外层归档仍只接受普通文件/目录。

安装时从内层归档部署共享运行时，再在最终路径创建 `.venv`，不搬迁已经创建的虚拟环境。构建输出目录与安装目录是两回事；安装后仍位于用户数据目录的 `repo-agent/versions/<版本>/`。

## 离线构建

单平台构建沿用本地运行时文件和 wheelhouse：

```bash
.venv/bin/python scripts/build_release.py --target linux-x86_64 --offline \
  --runtime-archive /path/to/kit/runtime/python.tar.gz \
  --wheelhouse /path/to/kit/wheelhouse
```

全平台离线构建将 `--runtime-archive` 指向目录，文件名必须与平台对应：

```text
/path/to/runtime-archives/
├── macos-arm64.tar.gz
├── macos-x86_64.tar.gz
├── linux-arm64.tar.gz
└── linux-x86_64.tar.gz
```

```bash
.venv/bin/python scripts/build_release.py --offline \
  --runtime-archive /path/to/runtime-archives \
  --wheelhouse /path/to/all-platform-wheels
```

这些文件是各平台原始的 Python 上游归档，可从[离线开发材料](development.md#离线开发环境)中的 `runtime/python.tar.gz` 复制并改名，内容必须匹配 `runtime/python.lock` 的 SHA256。共享 wheelhouse 应汇集所有目标的运行依赖以及构建机器可用的构建依赖；当前构建依赖均有通用 wheels。同名、同内容的通用 wheel 只保留一份。

单个运行时文件必须搭配 `--target`；提供目录时，开始构建前检查所选平台的归档是否齐全。`--offline` 同时禁止 pip 联网并传给 uv 锁文件检查；缺少匹配 wheels 或哈希不符时失败。开发材料准备脚本仍按单个平台工作，省略它的 `--target` 时选择当前平台，具体用法见开发指南。

## 校验、失败处理与安装验收

构建前检查依赖锁与分发清单；打包后核对实际 wheel、内置 Docker 上下文和完整归档的文件列表及内容。各平台分别写入临时归档，通过校验后替换正式产物。同版本同平台重建会覆盖其产物，不清理其他平台或版本。

任一平台失败时整个命令返回非零；本次先前已完成的平台包保留，依赖准备或归档校验失败不会覆盖该平台的已有压缩包。可用 `--target` 单独重试失败平台。旧布局下的本地产物不自动迁移或删除，新构建统一写入版本目录；发布时从对应版本目录选择文件，避免误用旧包。

```bash
.venv/bin/python -m pytest -q tests/test_release_builder.py tests/test_build_manifest.py tests/test_release_distribution.py
```

真实安装验收选择与当前机器匹配的完整包。以下示例使用临时用户目录和受管 Python 离线安装，删除下载目录后检查启动、配置恢复、doctor、Docker 构建上下文和卸载；Docker 入口使用模拟程序，不构建真实镜像、不调用模型 API：

```bash
REPO_AGENT_TEST_ARCHIVE="$PWD/dist/0.1.1/macos-arm64/repo-agent-0.1.1-macos-arm64.tar.gz" \
  .venv/bin/python -m pytest -q tests/test_release_distribution.py
```

上述验收使用 local 模式验证安装生命周期。macOS native、Linux native、WSL GPU 仍须分别进行[真实沙箱验证](native-sandbox.md#验证)，不能以打包成功替代。

运行时版本、上游 URL 和 SHA256 统一维护在 `runtime/python.lock`。更新时重新验证所有发布平台及标准库组件，保留上游许可文件，不要仅更新下载 URL。

## 统一分发清单

`build_manifest.py` 是分发文件选择与资源路径映射的唯一维护入口，只依赖 Python 标准库。`build_support.py` 负责接入 setuptools；`scripts/build_release.py` 负责构建和组装，两者共用该清单。

- 在已有源码包内增删 `.py` 文件会自动改变分发文件集合；新增顶层包需修改 `PACKAGES`。
- 保持包内相对路径的 JSON、模板等资源，加入 `PACKAGE_RESOURCES`；复制到 wheel 内特定位置的资源，加入 `RESOURCE_FILES` 的来源／目标映射。
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
