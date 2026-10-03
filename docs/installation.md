# 安装与卸载

[文档首页](index.md) · [项目首页](../README.md)

提供源码安装和独立发行版安装两种方式。源码安装目录保存源码和专用 `.venv`；发行版运行环境保存在用户数据目录，下载目录可以删除。用户配置与命令入口可以由多个安装共用。卸载以安装记录和当前文件状态为依据，源码、Git 修改、任务项目、历史日志、沙箱副本及回写备份始终保留。

## 独立发行版安装

普通用户使用与目标系统和架构匹配的完整包 `repo-agent-<版本>-<平台>.tar.gz`，包含独立 Python、Agent wheel 和 Python 语言服务依赖，不需要预装 Python。macOS/Linux 支持 ARM64、x86_64，WSL2 使用对应架构的 Linux 包。Windows x86_64 使用自带 Python 与核心运行依赖的 `.zip` 包，入口见下节。包中不含系统工具链或项目的 PyTorch 等依赖。详见 [Python 环境](python-environments.md)。

维护者的构建产物位于 `dist/<版本>/<平台>/`，每个压缩包旁有对应的 `.sha256` 文件；构建器默认生成全部平台，只提供完整包。构建命令与目录结构见[构建与分发](distribution.md)。

发行压缩包自带与文件名对应的顶层文件夹，直接解压即可，无需预先创建空目录。以下以源码当前版本 0.1.4 的 macOS ARM64 包名演示路径，实际操作须替换为所下载的版本与平台；示例不表示现有归档已包含工作区的未发布修改：

```bash
tar -xzf repo-agent-0.1.4-macos-arm64.tar.gz
cd repo-agent-0.1.4-macos-arm64
./install-release.sh
./install-release.sh --check
```

`.env.example` 等隐藏文件包含在该文件夹内，保持发行文件原样，安装后再通过用户配置设置模型。

已有源码时，也可以直接安装构建好的压缩包；更新后的安装器兼容新格式和旧版平铺格式：

```bash
./install-release.sh --archive dist/0.1.4/macos-arm64/repo-agent-0.1.4-macos-arm64.tar.gz
# 使用从可信发布来源取得的 SHA256 做整个压缩包校验
./install-release.sh --archive dist/0.1.4/macos-arm64/repo-agent-0.1.4-macos-arm64.tar.gz --sha256 <SHA256>
```

默认位置：

| 内容 | 位置 |
| --- | --- |
| 发行版文件和独立 `.venv` | `${XDG_DATA_HOME:-~/.local/share}/repo-agent/versions/<版本>/` |
| 命令 | `~/.local/bin/repo-agent`、`repo-agent-build-sandbox` |
| 模型和 API Key 配置 | `${XDG_CONFIG_HOME:-~/.config}/repo-agent/.env`，支持原有 `AGENT_CONFIG_DIR` 覆盖 |
| 安装记录、恢复信息及下载日志 | `${XDG_STATE_HOME:-~/.local/state}/repo-agent/` |

`--data-dir` 自定义整个 `repo-agent` 数据目录；`--bin-dir`、`--no-path`、`--mode`、`--languages`、`--with-toolchains`、`--skip-toolchains`、`--skip-sandbox` 与源码安装语义相同。安装不询问模型或 Key，只创建缺失的用户配置。命令保留调用时的工作目录。

安装完成后可删除下载目录及原源码。发行版以普通 wheel 安装，默认配置模板、内置 Skills、语言服务锁文件和 Docker 构建上下文均包含在发行包内。

```bash
repo-agent version                 # 版本、安装类型、安装位置、Python 路径
repo-agent config show
repo-agent config reset            # 先备份用户配置，再恢复该发行版的默认模板
repo-agent toolchains list
repo-agent-build-sandbox
repo-agent uninstall --dry-run
repo-agent uninstall
```

卸载沿用归属检查：删除当前版本的 `.venv` 和仍指向它的命令，默认保留用户配置、系统工具链、发行版文件及恢复入口，不回退至旧安装。需要重装时可在该版本目录运行 `./install-release.sh`。如需删除已卸载版本的剩余文件，请先确认不再需要该目录内的恢复入口或修改后再手动删除。

当前 macOS/Linux 构建器生成的 schema 4 发行包（此前 schema 2 也使用单入口布局）仅保留一个顶层入口 `install-release.sh`，不包含源码专用的 `install.sh` 和 `uninstall.sh`。较早的 schema 1 包保留原有脚本，不能仅凭版本号判断是否支持新参数；旧包卸载可使用 `repo-agent uninstall` 或包内 `uninstall.sh`。对于新包，命令无法启动时，可在已安装版本目录执行：

```bash
./install-release.sh --recover             # 恢复中断的安装
./install-release.sh --uninstall --dry-run # 预览卸载
./install-release.sh --uninstall           # 默认保留用户配置
# 可按需追加 --purge 或 --remove-image
```

也可以从解压目录运行备用卸载入口；默认定位用户数据目录下的同一版本，安装时指定过 `--data-dir` 的，需传入相同路径。在已安装版本目录运行则自动识别自定义数据目录。备用卸载不下载 Python，不要求 `venv`/`ensurepip`；虚拟环境损坏时会尝试已有受管 Python 和系统 Python 3.11+，也可通过 `AGENT_PYTHON` 显式指定。

相同发行包允许重复安装。同版本号但内容不同的发行包会被拒绝，维护者应递增版本号；如确需替换，先卸载并手动移走原版本目录。安装其他版本时，发现旧命令会先询问确认；核心安装失败保留旧命令和旧环境。失败后保留已校验的版本文件以便重试，继续使用原安装命令即可；仅恢复事务可以在该版本目录执行 `./install-release.sh --recover`。当前没有自动检查更新、下载更新或回滚命令。

压缩包 SHA256 和包内逐文件哈希用于发现损坏、文件缺失和内容不一致，不等同于发行者签名；请从可信来源取得安装器和发行包。

## Windows x86_64 ZIP 安装

支持 Windows 10 1809+ / Windows 11 x86_64、Windows PowerShell 5.1+。ZIP 内置锁定的 Python 3.13、pip、venv 和 Windows 离线 wheels，不需要预装 Python 或管理员权限。只提供一个安装脚本 `install_release.ps1`，卸载、检查和恢复均由它的参数完成。

```powershell
Expand-Archive .\repo-agent-0.1.4-windows-x86_64.zip -DestinationPath .
Set-Location .\repo-agent-0.1.4-windows-x86_64
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_release.ps1 --check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_release.ps1 --offline
```

上面的执行策略仅作用于本次 PowerShell 进程，不修改系统策略。首次默认 `local`；需要命令执行时选择 `--mode docker`，预先安装 Git for Windows 并启动 Docker Desktop 的 Linux 容器模式。离线 Docker 安装还须提前准备镜像并传 `--skip-sandbox`。Windows 不提供 `native` 沙箱；macOS/Linux 的默认 native 行为不变。

目录布局沿用上表（`~` 为用户目录），命令名为 `repo-agent.exe`、`repo-agent-build-sandbox.exe`。安装器将独立 Python 校验并复制到用户数据目录的共享缓存，再在最终版本目录创建 `.venv`；安装成功后可以删除 ZIP 和解压目录。`AGENT_PYTHON_CACHE` 可指定缓存位置；`AGENT_PYTHON` 可指定完整解释器路径或 `system`。`--data-dir`、`--bin-dir` 支持含空格的路径，请用引号包裹。

Windows 新构建包的运行时缓存按上游版本和发行清单区分，升级不原地覆盖旧解释器。引导会清理源码型运行时中可再生成的字节码缓存，包括维护操作；不会因此跳过源码或 DLL 的完整性校验。安装、卸载与发行安装 Python 入口的管道输出统一使用 UTF-8。

默认将命令目录加入当前用户的 PATH（HKCU），不修改系统 PATH，也不写 Bash/Zsh 配置。打开新终端，或执行安装末尾给出的当前终端 PATH 命令；`--no-path` 完全跳过此操作。命令通过归属记录管理，已有其他安装时确认接管；重装保留配置，核心失败恢复旧虚拟环境、命令和未被外部修改的 PATH。

卸载前退出该版本的所有 Agent 进程，在已安装版本目录（默认如下）运行同一个脚本：

```powershell
Set-Location "$HOME/.local/share/repo-agent/versions/0.1.4"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_release.ps1 --recover
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_release.ps1 --uninstall --dry-run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_release.ps1 --uninstall
# 如需清理未共享的用户配置，在卸载参数后追加 --purge
```

检查、恢复和卸载直接使用版本目录附带的 Python，不下载或创建运行时缓存，也不依赖 `.venv` 完好。卸载移除该版本虚拟环境与仍归属它的命令；默认保留用户配置、发行文件、共享 Python 缓存、项目、日志和备份。默认共享的 `~/.local/bin` PATH 保留；自定义命令目录仅在无其他安装共用且 PATH 未被外部修改时恢复。`--purge` / `--remove-image` 的归属保护与 macOS/Linux 相同。

## 源码安装与首次启动

需要 macOS / Linux（含 WSL2）。安装器默认复用或下载锁定的受管 Python，不依赖用户的 Conda 环境。显式 `AGENT_PYTHON=/路径/bin/python` 使用指定解释器；`AGENT_PYTHON=system` 启用系统 Python 搜索。默认 native 模式无需 Docker；Linux 仍需 Bubblewrap 和 libseccomp。常规源码安装首次联网准备运行、构建和开发依赖。

```bash
./install.sh
./install.sh --check
```

源码安装创建专用 `.venv`，自动安装开发依赖 pytest、Ruff 和构建工具，将模板复制到用户配置，并把 `repo-agent`、`repo-agent-build-sandbox` 安装到 `~/.local/bin`。默认补充 Bash / Zsh 的 PATH；打开新终端或执行安装结尾打印的命令。其他 shell 自行配置 PATH。源码和安装目录需要保留，命令依赖其中的虚拟环境。

Linux/macOS 源码安装在核心安装成功后自动尝试编译并安装 Rust 策略扫描扩展。需要已有 Rust >= 1.85（cargo/rustc）和 C 链接器，安装器不会自动安装编译器；maturin 构建依赖在隔离环境中准备，Cargo 中间产物写入项目外的临时目录，最终 wheel 保留到源码根目录 `rust_wheels/<版本>/`。手工编译和打包入口见 [Rust 构建说明](../rust/README.md)。缺少工具、下载或编译失败、取消该可选步骤均只提示，不撤销核心安装。离线编译需要本地 maturin wheel 和已缓存的 Cargo 依赖，准备不足时跳过扩展。

附带预编译扩展的发行包会直接安装并验证包内 Rust wheel，安装机不需要 Rust 编译器，也不联网编译。Windows 当前不支持此扩展。安装扩展不会修改扫描器配置，默认仍为 `AGENT_NATIVE_SCANNER=python`；Linux/macOS 用户可改为 `rust` 启用，两平台 native 文件工具的 Rust 后端均需要 兼容 FILESYSTEM_API_VERSION=2 的扩展（当前版本 0.6.0）。若旧配置显式选择了 `rust` 而本次扩展安装失败，应改回 `python` 或补装扩展后再运行。详情见 [Rust 扫描器说明](../rust/docs/policy-scan.md)。

首次在目标项目运行 `repo-agent` 时，若缺少模型或 Key，交互终端会引导设置；也可先运行 `repo-agent config model`。安装本身无需模型信息。已有用户配置会按新模板重建，保留仍有效的旧值（含显式空值），补充新增项，丢弃模板已移除的项；替换前自动备份。新模板与内置默认值的区别见[配置参考](configuration.md#模板与内置默认值)。

```bash
./install.sh --mode native                  # macOS / Linux 原生模式，询问是否补齐额外语言服务
./install.sh --mode docker                  # Docker 模式，构建包含语言服务的镜像
./install.sh --mode local                   # 仅文件/Git 工具，不安装语言服务
./install.sh --bin-dir /path/to/bin --no-path # 自定义命令目录，不改 shell 配置
```

首次默认 native，通过 macOS / Linux 原生沙箱执行工具并直接修改原项目。安装成功会记录所选模式，后续 `repo-agent` 和 `doctor` 沿用该模式；可用 `--sandbox` 或 `doctor --mode` 临时覆盖。Windows 源码 Shell 安装请在 WSL2 内运行，原生 Windows 使用上面的 ZIP 包；local 仅支持文件/Git 工具。详见[原生沙箱](native-sandbox.md)。`--skip-sandbox` 仅在 Docker 安装模式下跳过镜像构建，不会改变所选模式；native/local 不检查 Docker。

## 离线安装

平台完整发行包已包含运行时和运行依赖。首次离线安装先解压，再从包内运行入口；从其他目录使用 `--archive` 时，该入口本身仍需要已有引导 Python 或本地运行时归档：

```bash
./install-release.sh --offline --skip-toolchains
```

源码安装需要额外的构建和开发依赖。在联网的准备机器上生成与目标平台匹配的开发材料，步骤见[开发指南](development.md#离线开发环境)。将材料复制到离线机器后，在源码目录运行：

```bash
AGENT_PYTHON_ARCHIVE=/path/to/kit/runtime/python.tar.gz \
  ./install.sh --offline --wheelhouse /path/to/kit/wheelhouse --skip-toolchains
```

离线安装会在替换现有 `.venv` 前检查 wheels 是否齐全，缺失或哈希不匹配时失败。不会偷偷访问包源；普通 pip 缓存不作为离线材料的替代品。Docker 模式需预先准备镜像，并加 `--skip-sandbox`；系统依赖和额外语言工具链也需提前安装。完整发行包在未指定外部 wheelhouse 时默认从包内 wheels 安装 Python 依赖；显式 `--offline` 进一步禁止安装流程选择下载额外工具链或构建镜像。

## Linux native 前置依赖

Debian/Ubuntu 先安装 `bubblewrap libseccomp2`，Fedora 安装 `bubblewrap libseccomp`；需要 Bubblewrap 0.8+ 和可用的非特权 user namespace。随后可运行 `./install.sh --mode native --skip-toolchains`。基础安装准备 Python 语言服务；额外语言需要预先安装 Node.js/npm、Go 1.25+、clangd，再运行补齐命令。Linux 不使用 Homebrew 自动安装系统工具，不调用 sudo；内核或 AppArmor 禁止 namespace 时，安装后的实际沙箱检查会失败并恢复安装。详见[原生沙箱说明](native-sandbox.md)。

## 源码升级与重建镜像

先退出本安装中正在运行的 Agent，再在安装目录执行：

```bash
git pull
./install.sh
```

重装默认沿用此前安装模式；用户配置按 `.env.example` 的分组和注释重建，保留模板内配置项的旧值，新增项采用模板值，弃用项和未知项移除。旧文件格式损坏时停止合并并保留原文件。仅更新宿主机代码时可使用 `--skip-sandbox`；改动工具、沙箱、镜像依赖或保护规则后需要重建镜像：

```bash
repo-agent-build-sandbox
```

构建不读取模型配置。源码安装使用源码构建上下文；独立发行版将内置构建资源解压到临时目录，构建结束后自动清理。`--profile auto|standard|cuda` 选择环境，`--image` 指定标签。构建与启动共用 Docker 主机环境检测，规则见 [GPU 指南](gpu-operators.md)。重启 Agent 会按当前配置重新建立执行后端；`--new-session` 还会创建新的工作副本。

## 切换安装目录

在新目录运行 `./install.sh` 时，如果目标命令目录中的 `repo-agent` 或 `repo-agent-build-sandbox` 已指向另一份安装，会先列出两个命令需要替换的位置，并询问 `是否继续安装并替换命令？[y/N]`。输入 `y` 或 `yes` 后继续；回车、输入其他内容、Ctrl+C 或没有可读取的输入均取消，不创建 Agent 虚拟环境、安装依赖、构建镜像或写入安装记录。用于启动安装器的共享 Python 可能已在此之前下载并校验，取消后保留。同目录重复安装不需要确认命令替换；native 的工具链补齐仍会询问。

新目录的依赖安装和镜像构建成功后，命令才会切换；这些步骤失败时旧命令仍可用。旧安装目录保留，用户配置按新模板合并。之后卸载旧目录不会删除指向新目录的命令；卸载当前新目录会移除命令，不自动回退到旧目录。其他普通文件或无法识别的同名链接不会被覆盖；安装期间链接若被其他操作改变，会停止覆盖并提示重试。

检测范围是 `--bin-dir` 指定的命令目录（默认 `~/.local/bin`）；多个命令目录并存时，终端使用 PATH 中排在前面的命令。

## 卸载

macOS/Linux 命令仍可用时，可以执行 `repo-agent uninstall`；命令损坏时使用下表入口。Windows 请先退出 Agent，再使用版本目录中的 PowerShell 脚本卸载；该入口使用附带 Python，不依赖待删除的 `.venv`。

| 安装类型 | 预览卸载 | 执行卸载 |
| --- | --- | --- |
| 源码安装 | `./uninstall.sh --dry-run` | `./uninstall.sh` |
| macOS/Linux 当前发行包 | `./install-release.sh --uninstall --dry-run` | `./install-release.sh --uninstall` |
| Windows 当前 ZIP | `./install_release.ps1 --uninstall --dry-run` | `./install_release.ps1 --uninstall` |

在对应安装目录执行。下列清理选项以源码入口为例；macOS/Linux 发行包使用 `./install-release.sh --uninstall`，Windows 使用 `./install_release.ps1 --uninstall` 后追加同样的选项。Windows 执行策略与完整命令见[ZIP 安装](#windows-x86_64-zip-安装)。

```bash
./uninstall.sh --dry-run
./uninstall.sh

# 同时清除未被其他安装共用的用户配置，包括 API Key
./uninstall.sh --purge

# 同时移除本安装构建、仍匹配记录且未被使用的镜像标签
./uninstall.sh --remove-image

# 可以组合；预览不会修改文件或 Docker 镜像
./uninstall.sh --dry-run --purge --remove-image
```

默认卸载移除当前安装管理的 `.venv` 和仍归属该安装的两个命令入口；macOS/Linux 使用链接，Windows 使用 `.exe` 与配套归属记录。配置和镜像默认保留，之后可再次执行带清理选项的卸载命令。重复卸载会跳过已经删除的内容。

卸载器只依赖 Python 3.11+ 标准库，不导入 Agent 或第三方依赖；即使虚拟环境或依赖损坏，也可运行。`AGENT_PYTHON` 可以指定可用解释器。默认卸载无需 Docker；只有显式请求 `--remove-image` 时才检查 Docker。

## 清理边界

| 内容 | 行为 |
| --- | --- |
| 安装目录 `.venv` | 安装标记和目录身份均匹配时删除；目录被替换或改成符号链接时保留 |
| `repo-agent`、`repo-agent-build-sandbox` | macOS/Linux 仅删除仍指向当前安装的已登记链接；Windows 按归属记录和哈希处理安装的 `.exe`。外部替换或修改的命令保留 |
| Shell 配置 / Windows 用户 PATH | macOS/Linux 仅删除安装器添加、未被修改且不再共享的标记块；Windows 按记录比较并恢复 HKCU 用户 PATH，外部修改时保留。用户原有内容不直接覆盖 |
| 通用 `~/.local/bin` PATH | 保留，其他程序也可能依赖此目录 |
| 用户配置 `.env`、`thinking.json` 及 `.env.backups` 中的受管备份 | 默认保留；`--purge` 删除安装记录中的配置文件、思考偏好及含历史 Key 的受管备份，其他安装仍使用或无法确认归属时保留 |
| 项目 `.env`、源码、Git、会话快照及日志、沙箱副本及备份 | 不清理；配置文件如果被显式指定为用户配置，则按上一行处理 |
| Docker 镜像 | 默认保留；请求清理时校验 Docker 主机、镜像 ID、共享安装和容器使用情况；不使用强制删除或全局 prune |
| 系统 Python、Anaconda、Docker | 不卸载 |
| 受管 Python 缓存 | 保留，其他 Agent 安装的虚拟环境可能仍在使用 |

镜像标签对应的镜像已被重新构建、切换了 Docker 主机、仍有容器使用，或另一个登记安装可能使用时，会保留并说明原因。Docker 无法访问时，已授权的本地文件清理仍会完成，保留镜像记录并返回非零退出码；Docker 恢复后可重试。

没有记录的旧版安装不会按文件名猜测删除。源码安装升级后重新运行 `./install.sh --skip-sandbox`；发行版在其安装目录重新运行 `./install-release.sh --skip-sandbox`，即可记录当前安装的资源；旧版未标记的 Shell 配置块仍保留，未记录的旧镜像也不会自动删除。如果命令已经指向另一份副本，安装器会在开始安装前列出旧、新位置，确认后切换；也可通过 `--bin-dir` 选择独立命令目录。

## 安装记录与多个副本

安装器在创建虚拟环境前开始记录，在写入配置、命令链接和 Shell 配置前更新记录；镜像成功构建后记录其 ID 和 Docker 主机标识。依赖安装或最终切换失败时，会自动恢复旧环境、命令链接、Shell 配置块和安装记录；首次安装失败则移除本次创建的环境。若恢复未完成，保留恢复记录并要求先恢复再卸载。

用户配置不随安装事务回滚：已创建或已合并的配置会保留。合并前的原文件保存在用户配置目录的 `.env.backups/`，可使用 `repo-agent config backups` 查看和 `config restore` 恢复；重复安装且配置内容未变化时不新增备份。

安装目录的 `.repo-agent-install.json` 是本地记录，已加入 Git 忽略和 Agent 文件保护；`.venv` 内还有配套归属标记。登记表位于 `${XDG_STATE_HOME:-~/.local/state}/repo-agent/installations`，macOS/Linux 使用用户私有权限，Windows 继承用户目录 ACL；其中只保存路径、归属和状态，不保存 API Key 或配置内容。

普通卸载成功后删除安装目录记录，用户级登记表保留一份卸载回执，用于重复卸载或之后执行 `--purge` / `--remove-image`。使用自定义 `XDG_STATE_HOME` 时，后续操作应保持同一设置。

复制或移动后的安装记录不能用来卸载原目录。重新安装该副本会建立独立身份；其他副本的命令、共享配置和镜像受到保护。记录损坏、链接被改变或归属不明确时，卸载器保留相关内容并输出原因。

卸载不是 Git 回滚，也不会清空系统临时目录、其他项目目录或历史开发缓存。它不会恢复安装前已有虚拟环境中的旧依赖版本；重新安装时会创建新的运行环境。

## 安装检查、失败恢复与诊断

以下 Shell 命令用于 macOS/Linux 源码安装。macOS/Linux 发行版在已安装版本目录使用 `./install-release.sh --check` 或 `./install-release.sh --recover`；Windows 使用 `./install_release.ps1` 的同名选项。`repo-agent doctor` 适用于源码与发行版安装。

```bash
./install.sh --check                  # 按安装模式检查环境，不做修改、不询问补齐
./install.sh --check --with-toolchains # 同时检查补齐额外工具链的条件
./install.sh --recover                # 只恢复上次中断的安装，不继续重装
repo-agent doctor                    # 按安装模式检查依赖、配置和实际语言服务
repo-agent doctor --mode native       # 实测原生沙箱内的语言服务
repo-agent doctor --mode docker       # 实测现有镜像内的语言服务
repo-agent doctor --mode local        # 只检查核心依赖、文件/Git 模式
repo-agent doctor --root /path/to/project
```

安装前检查 Python 3.11+、venv/ensurepip、项目文件、已有虚拟环境路径和版本、CA 信任库；native 按系统检查 sandbox-exec 或 bubblewrap/libseccomp，以及所选工具链，Docker 模式才检查 Docker 服务。检查证书文件和本地信任库不会访问 PyPI，不能保证网络、代理或远端证书可用。证书失败时应修复 Python 信任库或通过 `PIP_CERT` 提供可信 CA，不关闭校验。

缺少 `venv` 或 `ensurepip` 时，检查会列出具体缺失项、当前解释器和基础 Python 路径，并按来源提供修复步骤。Debian/Ubuntu 系统 Python 提示安装匹配次版本的 `pythonX.Y-venv`；Homebrew 提示重装对应 Python formula；Conda 提示修复指定环境中的 Python。Fedora/RHEL 提供所属包查询命令，Arch 提示修复 Python 包；自定义 Python 提示通过原安装方式修复，避免误装到系统解释器。

这些提示只展示命令，不会自动运行系统包管理器。修复后，按输出中的 `AGENT_PYTHON=... ./install.sh --check` 重新检查，保留此前的 `--mode` 等选项；通过后去掉 `--check` 安装。`venv` 和 `ensurepip` 属于 Python 发行版组件，不使用 `pip install venv/ensurepip` 修复。包名与命令参考 [Ubuntu 包信息](https://packages.ubuntu.com/en/noble-updates/python3.12-venv)、[Homebrew Python](https://docs.brew.sh/Language-Runtimes-and-Packages)、[Conda install](https://docs.conda.io/projects/conda/en/stable/commands/install.html)。

每次重装都重新创建 `.venv` 中的 Python 环境和命令入口，避免复用复制来的解释器；native 的受管 JS/TS、Go 服务可保留复用。旧环境临时保存在安装目录的 `.repo-agent-install-transaction/venv`。新环境在最终路径构建，避免移动后命令的解释器路径失效；**同目录重装期间请先退出本安装的 Agent 会话，不要同时启动命令**。这不是零停机升级。

安装会验证 `pip check`、核心模块导入和两个命令的 `--help`。native 核心阶段会在实际原生沙箱中查询 Python 示例的符号，失败会触发恢复；Docker 构建内也包含各语言符号测试。Docker 镜像先构建到临时标签，构建成功后才更新默认标签；若后续切换失败，恢复原标签。成功安装后仍使用共享默认标签，本功能不提供多个安装之间的永久镜像版本隔离。恢复不会删除构建缓存或强制清理容器。

核心安装阶段的普通异常和 Ctrl+C 会触发恢复；进程被强制结束后，下次安装会先处理恢复记录，也可独立执行 `--recover`。如果文件或链接被其他程序替换，或恢复镜像时 Docker 不可用，会保留恢复记录并提示处理，不覆盖外部修改。已存在的用户配置和安装新建的默认模板都保留。

native 安装分两阶段：先完成核心依赖、Python 语言服务、沙箱验证、配置和命令安装，并提交安装记录；然后逐个补齐或验证额外语言。额外工具链缺失、下载失败或符号验证失败只产生 WARN，不撤销核心安装，并继续尝试其余语言；补齐期间 Ctrl+C 停止后续语言，核心安装仍保留。核心成功后安装命令返回 0，即使部分额外语言未完成；需要严格检查单个补齐结果时运行 `repo-agent toolchains install <语言>`（失败返回 1）。未完成的语言记入安装记录，doctor 提示重试命令，成功补齐后清除提示。`--check --with-toolchains` 仍可严格检查额外工具链的前提条件，不安装任何内容。

下载步骤（pip、npm、Go、Homebrew）统一分类错误：证书、认证、包版本/下载源和构建错误不盲目重试；DNS、代理连接、临时连接错误、限流和超时有限重试，默认最多额外重试 2 次，间隔 2、4 秒。每次尝试沿用工具自身的下载缓存，不清理全局缓存；这不保证从中断的字节处继续下载。单步默认超时 600 秒，可根据构建速度调整。Docker 构建沿用自身输出及恢复流程，不纳入该下载重试器。

```bash
AGENT_INSTALL_RETRIES=2 AGENT_INSTALL_TIMEOUT=900 ./install.sh
AGENT_INSTALL_PROXY=http://127.0.0.1:7890 ./install.sh
AGENT_INSTALL_GOPROXY=https://your-go-proxy.example repo-agent toolchains install go
```

`AGENT_INSTALL_RETRIES` 支持 0–5，`AGENT_INSTALL_TIMEOUT` 支持 1–3600 秒。`AGENT_INSTALL_PYPI_INDEX`、`AGENT_INSTALL_NPM_REGISTRY`、`AGENT_INSTALL_GOPROXY` 分别指定 Python、npm 和 Go 下载源；`AGENT_INSTALL_PROXY` 指定 HTTP(S) 代理。这些设置只传给安装子进程，不修改全局配置；未指定时继续使用已有包源/代理设置。证书使用既有的 `PIP_CERT` 等设置，不关闭证书校验。

下载输出保存到 `${XDG_STATE_HOME:-~/.local/state}/repo-agent/install-logs/`，终端显示步骤、重试次数、失败分类及日志路径。macOS/Linux 日志文件权限为 600，Windows 日志继承用户目录 ACL；过滤环境中的密钥及常见 URL/认证凭据；日志保留用于失败后的排查。排查后可自行删除该日志目录。`--check` 不下载、不创建这些日志。

`doctor` 不修改配置或任务项目、不发送模型请求、不拉取镜像。它检查当前命令实际指向、PATH 重复入口、运行依赖、配置来源与参数冲突、失效安装记录，并按模式实测语言服务。测试使用自动清理的临时示例；Docker 测试使用断网、只读、无项目挂载的临时容器。ERROR 返回退出码 1，仅 WARN 返回 0；尚未填写模型/Key 是 WARN。诊断不会显示密钥。虚拟环境损坏导致全局命令无法启动时，可在安装目录执行：

```bash
python3 -B cli/doctor.py --skip-docker
```

安装、卸载和配置写入使用进程锁；锁文件会保留，但进程退出后锁自动释放。不要在操作仍运行时删除锁文件。复制项目时应排除 `.venv` 和 `.repo-agent-install-transaction`；恢复记录属于原目录时会拒绝恢复。


## 模式与依赖

| 安装方式 | 宿主机 Python 环境 | 外部工具与验证 |
| --- | --- | --- |
| `./install.sh --mode native` | 核心依赖及 `python-lsp-server` | 询问是否补齐 Node.js/npm、Go 1.25+、LLVM/clangd 及 JS/TS、Go 服务；验证实际安装的语言服务 |
| `./install.sh --mode docker` | 核心依赖 | 需要已有 Docker 服务；构建镜像，其中包含语言服务、Git、编译工具并进行符号测试 |
| `./install.sh --mode local` | 核心依赖 | 仅文件/Git 工具，不提供命令执行和语言服务，不依赖 Docker |

macOS/Linux 首次安装模式为 native，Windows 为 local；重装默认沿用本安装记录。模式绑定安装目录，不会从任务目录读取；用户配置中的模型与 Key 不受影响。`--check` 只检查和说明将要补齐的工具，不调用 Homebrew 安装、不运行 pip 安装。

native 始终安装 Python 语言服务；安装时询问是否补齐额外工具链和语言服务，回车或输入结束默认跳过。可用 `--with-toolchains` 自动同意、`--skip-toolchains` 自动跳过；`--languages python,typescript` 限制补齐范围。跳过时不下载 gopls/npm 服务，也不调用 Homebrew；缺少 Homebrew 不阻止基础安装。同目录重装保留受管的 JS/TS、Go 服务，重新验证此前已启用且仍可用的语言。

安装结束会显示各语言的工具链与语言服务状态；也可独立操作，无需填写模型或 API Key：

```bash
repo-agent toolchains list                      # 不联网，分别检查工具链与服务能否启动
repo-agent toolchains install go                # 已有 Go 时仅补 gopls；已有可用服务则跳过下载
repo-agent toolchains install python typescript # 同时补齐指定语言
repo-agent toolchains install all               # 补齐全部支持的语言
./install.sh --skip-toolchains                  # 基础安装，不下载额外语言服务
./install.sh --with-toolchains --languages go   # 免交互补齐 Python 和 Go
```

列表展示宿主机 native 模式的 Python、JS/TS（含 JSX/TSX）、Go、C/C++/CUDA 文件服务。CUDA 文件复用 clangd；Linux / WSL2 native 的实际编译需预装本机 CUDA Toolkit 和所需框架，Docker 则使用 CUDA 镜像。符号查询成功不代表 GPU 或编译环境可用。该列表的“已安装”表示程序可启动，实际符号查询由补齐命令和 `doctor` 验证；Docker 镜像状态用 `repo-agent doctor --mode docker` 检查。

补齐命令用于 macOS / Linux 宿主机，不切换默认模式、不重装 Agent、不修改模型配置。成功补齐的语言会记入安装记录；失败时可重试同一命令，此前已完成的语言和系统工具链保留。JS/TS、Go 先下载到临时目录，下载或验证失败不会替换原有服务；Python 使用当前虚拟环境的 pip 安装，失败后可能保留已安装的 Python 包。执行补齐前先退出当前安装的 Agent，完成后重启。

后续 `doctor` 验证该安装记录中的语言范围。首次缺少 Homebrew 时会明确提示访问 [Homebrew 网站](https://brew.sh)，不执行远程脚本安装 Homebrew。Homebrew 包已存在但版本过旧或仍不可用时，会尝试更新对应包；升级后的工具链仍需通过检查。额外检查失败不会撤销已经成功的核心安装。

Python 依赖、`gopls` 和 npm 的 JS/TS 语言服务位于本安装 `.venv` 内，失败恢复和卸载会一起处理。npm 安装不使用全局安装，也不执行依赖的生命周期脚本。运行时会使用这些受管目录和常见 Homebrew 工具链路径，Go 缓存位于沙箱的私有临时目录。

**Homebrew 安装或更新的 Node.js、Go、LLVM 属于共享系统工具，不在失败恢复和卸载时删除或降级。** Homebrew/npm/Go 的下载缓存也不进行全局清理。安装前须退出本安装的 Agent 会话。

独立发行版不包含开发工具；源码安装自动安装锁定的 pytest、Ruff 和构建依赖。Docker 的 GPU 依赖由 CUDA 镜像提供；Linux / WSL2 native 使用用户预装的驱动、项目 Python 环境中的 PyTorch/Triton 及系统 Toolkit。native 安装不会自动安装这些计算依赖，macOS native 不提供本项目的 NVIDIA CUDA GPU 支持。

## 依赖锁定

`uv.lock` 是 Python 依赖的统一版本来源；`requirements-core.lock`、`requirements-lsp.lock`、`requirements-build.lock`、`requirements-dev.lock` 是带哈希的 pip 安装清单，分别用于核心运行、包含 Python 语言服务的运行环境、源码构建和开发测试。源码安装自动安装开发测试依赖（pytest、Ruff）；独立发行版只安装运行所需依赖。源码安装和发行版安装均使用锁文件，不在安装时重新解析范围依赖。发行版安装 wheel 时不触发构建或隐式安装依赖；源码安装在固定构建依赖下保留 editable 模式。

Python 安装启用 `--require-hashes --only-binary=:all:`，平台/Python 版本没有匹配 wheel 时会明确失败，不静默回退到本地编译。可选语言服务失败仍不阻断核心安装，网络代理、证书提示、重试及事务恢复机制继续有效。

JS/TS 使用 `dependencies/node/package-lock.json` 和 `npm ci`，固定传递依赖并校验 npm integrity；原生补齐和 Docker 镜像使用同一清单。Go 的 gopls 继续固定版本并使用 Go 模块校验。受管 Python 另由 `runtime/python.lock` 固定。显式选择的系统 Python、Node.js、Go、LLVM、Linux 系统包及 Docker 基础镜像不属于应用锁文件；已有兼容工具链仍会复用。CUDA 镜像额外的 pytest/ninja 等工具也不在本次 Python 核心锁内，因此不宣称整个系统或 GPU 镜像可以逐字节复现。
