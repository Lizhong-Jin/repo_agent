# 安装与卸载

[返回 README](../README.md)

安装目录保存源码和专用 `.venv`；用户配置与命令入口可以由多个安装共用。卸载以安装记录和当前文件状态为依据，源码、Git 修改、任务项目、历史日志、沙箱副本及回写备份始终保留。

## 安装与首次启动

需要 macOS / Linux、Python 3.11+；默认使用 native 模式，无需 Docker；macOS 使用 Seatbelt，Linux 使用 Bubblewrap + seccomp。首次安装联网下载依赖。在源码目录执行：

```bash
./install.sh --check
./install.sh
```

安装创建专用 `.venv`，将模板复制到用户配置，并把 `repo-agent`、`repo-agent-build-sandbox` 安装到 `~/.local/bin`。默认补充 Bash / Zsh 的 PATH；打开新终端或执行安装结尾打印的命令。其他 shell 自行配置 PATH。源码和安装目录需要保留，命令依赖其中的虚拟环境。

首次在目标项目运行 `repo-agent` 时，若缺少模型或 Key，交互终端会引导设置；也可先运行 `repo-agent config model`。安装本身无需模型信息。已有用户配置不会被覆盖，新模板与内置默认值的区别见[配置参考](configuration.md#模板与内置默认值)。

```bash
./install.sh --mode native                  # macOS / Linux 原生模式，询问是否补齐额外语言服务
./install.sh --mode docker                  # Docker 模式，构建包含语言服务的镜像
./install.sh --mode local                   # 仅文件/Git 工具，不安装语言服务
./install.sh --bin-dir /path/to/bin --no-path # 自定义命令目录，不改 shell 配置
```

首次默认 native，通过 macOS / Linux 原生沙箱执行工具并直接修改原项目。安装成功会记录所选模式，后续 `repo-agent` 和 `doctor` 沿用该模式；可用 `--sandbox` 或 `doctor --mode` 临时覆盖。Windows 请在 WSL2 内安装和运行；local 仅支持文件/Git 工具。详见[原生沙箱](native-sandbox.md)。`--skip-sandbox` 仅在 Docker 安装模式下跳过镜像构建，不会改变所选模式；native/local 不检查 Docker。

## Linux native 前置依赖

Debian/Ubuntu 先安装 `bubblewrap libseccomp2`，Fedora 安装 `bubblewrap libseccomp`；需要 Bubblewrap 0.8+ 和可用的非特权 user namespace。随后可运行 `./install.sh --mode native --skip-toolchains`。基础安装准备 Python 语言服务；额外语言需要预先安装 Node.js/npm、Go 1.25+、clangd，再运行补齐命令。Linux 不使用 Homebrew 自动安装系统工具，不调用 sudo；内核或 AppArmor 禁止 namespace 时，安装后的实际沙箱检查会失败并恢复安装。详见[原生沙箱说明](native-sandbox.md)。

## 升级与重建镜像

先退出本安装中正在运行的 Agent，再在安装目录执行：

```bash
git pull
./install.sh
```

重装默认沿用此前安装模式；安装保留已有用户配置；新增配置项可以参考 `.env.example` 补充。仅更新宿主机代码时可使用 `--skip-sandbox`；改动工具、沙箱、镜像依赖或保护规则后需要重建镜像：

```bash
repo-agent-build-sandbox
```

构建不读取模型配置，以安装目录源码为构建上下文。`--profile auto|standard|cuda` 选择环境，`--image` 指定标签。构建与启动共用 Docker 主机环境检测，规则见 [GPU 指南](gpu-operators.md)。重启 Agent 会按当前配置重新建立执行后端；`--new-session` 还会创建新的工作副本。

## 切换安装目录

在新目录运行 `./install.sh` 时，如果目标命令目录中的 `repo-agent` 或 `repo-agent-build-sandbox` 已指向另一份安装，会先列出两个命令需要替换的位置，并询问 `是否继续安装并替换命令？[y/N]`。输入 `y` 或 `yes` 后继续；回车、输入其他内容、Ctrl+C 或没有可读取的输入均取消，且不创建虚拟环境、下载依赖、构建镜像或写入安装记录。同目录重复安装不需要确认命令替换；native 的工具链补齐仍会询问。

新目录的依赖安装和镜像构建成功后，命令才会切换；这些步骤失败时旧命令仍可用。旧安装目录和用户配置保留。之后卸载旧目录不会删除指向新目录的命令；卸载当前新目录会移除命令，不自动回退到旧目录。其他普通文件或无法识别的同名链接不会被覆盖；安装期间链接若被其他操作改变，会停止覆盖并提示重试。

检测范围是 `--bin-dir` 指定的命令目录（默认 `~/.local/bin`）；多个命令目录并存时，终端使用 PATH 中排在前面的命令。

## 卸载

在需要卸载的安装目录执行：

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

默认卸载移除当前安装管理的 `.venv` 和仍指向该安装的两个命令链接。配置和镜像默认保留，之后可再次执行带清理选项的卸载命令。重复卸载会跳过已经删除的内容。

卸载器只依赖 Python 3.11+ 标准库，不导入 Agent 或第三方依赖；即使虚拟环境或依赖损坏，也可运行。`AGENT_PYTHON` 可以指定可用解释器。默认卸载无需 Docker；只有显式请求 `--remove-image` 时才检查 Docker。

## 清理边界

| 内容 | 行为 |
| --- | --- |
| 安装目录 `.venv` | 安装标记和目录身份均匹配时删除；目录被替换或改成符号链接时保留 |
| `repo-agent`、`repo-agent-build-sandbox` | 仅删除记录中仍指向当前安装的链接；指向其他副本或变成普通文件时保留 |
| Shell 配置 | 仅删除安装器添加、未被修改且不再共享的标记块；用户原有内容保留 |
| 通用 `~/.local/bin` PATH | 保留，其他程序也可能依赖此目录 |
| 用户配置 `.env`、`thinking.json` 及 `.env.backups` 中的受管备份 | 默认保留；`--purge` 删除安装记录中的配置文件、思考偏好及含历史 Key 的受管备份，其他安装仍使用或无法确认归属时保留 |
| 项目 `.env`、源码、Git、会话快照及日志、沙箱副本及备份 | 不清理；配置文件如果被显式指定为用户配置，则按上一行处理 |
| Docker 镜像 | 默认保留；请求清理时校验 Docker 主机、镜像 ID、共享安装和容器使用情况；不使用强制删除或全局 prune |
| 系统 Python、Anaconda、Docker | 不卸载 |

镜像标签对应的镜像已被重新构建、切换了 Docker 主机、仍有容器使用，或另一个登记安装可能使用时，会保留并说明原因。Docker 无法访问时，已授权的本地文件清理仍会完成，保留镜像记录并返回非零退出码；Docker 恢复后可重试。

没有记录的旧版安装不会按文件名猜测删除。升级后重新运行 `./install.sh --skip-sandbox`，即可记录当前安装的资源；旧版未标记的 Shell 配置块仍保留，未记录的旧镜像也不会自动删除。如果命令已经指向另一份副本，安装器会在开始安装前列出旧、新位置，确认后切换；也可通过 `--bin-dir` 选择独立命令目录。

## 安装记录与多个副本

安装器在创建虚拟环境前开始记录，在写入配置、命令链接和 Shell 配置前更新记录；镜像成功构建后记录其 ID 和 Docker 主机标识。依赖安装或最终切换失败时，会自动恢复旧环境、命令链接、Shell 配置块和安装记录；首次安装失败则移除本次创建的环境。若恢复未完成，保留恢复记录并要求先恢复再卸载。

安装目录的 `.repo-agent-install.json` 是本地记录，已加入 Git 忽略和 Agent 文件保护；`.venv` 内还有配套归属标记。登记表位于 `${XDG_STATE_HOME:-~/.local/state}/repo-agent/installations`，权限仅限当前用户，其中只保存路径、归属和状态，不保存 API Key 或配置内容。

普通卸载成功后删除安装目录记录，用户级登记表保留一份卸载回执，用于重复卸载或之后执行 `--purge` / `--remove-image`。使用自定义 `XDG_STATE_HOME` 时，后续操作应保持同一设置。

复制或移动后的安装记录不能用来卸载原目录。重新安装该副本会建立独立身份；其他副本的命令、共享配置和镜像受到保护。记录损坏、链接被改变或归属不明确时，卸载器保留相关内容并输出原因。

卸载不是 Git 回滚，也不会清空系统临时目录、其他项目目录或历史开发缓存。它不会恢复安装前已有虚拟环境中的旧依赖版本；重新安装时会创建新的运行环境。

## 安装检查、失败恢复与诊断

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

每次重装都重新创建 `.venv` 中的 Python 环境和命令入口，避免复用复制来的解释器；native 的受管 JS/TS、Go 服务可保留复用。旧环境临时保存在安装目录的 `.repo-agent-install-transaction/venv`。新环境在最终路径构建，避免移动后命令的解释器路径失效；**同目录重装期间请先退出本安装的 Agent 会话，不要同时启动命令**。这不是零停机升级。

安装会验证 `pip check`、核心模块导入和两个命令的 `--help`。native 还会在实际原生沙箱中查询各语言示例的符号，失败会触发恢复；Docker 构建内也包含各语言符号测试。Docker 镜像先构建到临时标签，构建成功后才更新默认标签；若后续切换失败，恢复原标签。成功安装后仍使用共享默认标签，本功能不提供多个安装之间的永久镜像版本隔离。恢复不会删除构建缓存或强制清理容器。

普通异常和 Ctrl+C 会触发恢复；进程被强制结束后，下次安装会先处理恢复记录，也可独立执行 `--recover`。如果文件或链接被其他程序替换，或恢复镜像时 Docker 不可用，会保留恢复记录并提示处理，不覆盖外部修改。已存在的用户配置和安装新建的默认模板都保留。

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

首次安装模式为 native；重装默认沿用本安装记录。模式绑定安装目录，不会从任务目录读取；用户配置中的模型与 Key 不受影响。`--check` 只检查和说明将要补齐的工具，不调用 Homebrew 安装、不运行 pip 安装。

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

列表展示宿主机 native 模式的 Python、JS/TS（含 JSX/TSX）、Go、C/C++/CUDA 文件服务。CUDA 文件复用 clangd，完整 CUDA 编译环境仍需 CUDA 镜像。该列表的“已安装”表示程序可启动，实际符号查询由补齐命令和 `doctor` 验证；Docker 镜像状态用 `repo-agent doctor --mode docker` 检查。

补齐命令用于 macOS / Linux 宿主机，不切换默认模式、不重装 Agent、不修改模型配置。成功补齐的语言会记入安装记录；失败时可重试同一命令，此前已完成的语言和系统工具链保留。JS/TS、Go 先下载到临时目录，下载或验证失败不会替换原有服务；Python 使用当前虚拟环境的 pip 安装，失败后可能保留已安装的 Python 包。执行补齐前先退出当前安装的 Agent，完成后重启。

后续 `doctor` 验证该安装记录中的语言范围。首次缺少 Homebrew 时会明确提示访问 https://brew.sh，不执行远程脚本安装 Homebrew。Homebrew 包已存在但版本过旧或仍不可用时，会尝试更新对应包；升级后的工具链仍需通过检查。

Python 依赖、`gopls` 和 npm 的 JS/TS 语言服务位于本安装 `.venv` 内，失败恢复和卸载会一起处理。npm 安装不使用全局安装，也不执行依赖的生命周期脚本。运行时会使用这些受管目录和常见 Homebrew 工具链路径，Go 缓存位于沙箱的私有临时目录。

**Homebrew 安装或更新的 Node.js、Go、LLVM 属于共享系统工具，不在失败恢复和卸载时删除或降级。** Homebrew/npm/Go 的下载缓存也不进行全局清理。安装前须退出本安装的 Agent 会话。

普通安装不包含开发工具；需要测试和静态检查时，另行安装 `.[dev]`。GPU 相关依赖继续由 CUDA 镜像配置提供，native 安装不会在 macOS 上安装 CUDA/Triton。
