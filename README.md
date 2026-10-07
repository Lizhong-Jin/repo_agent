# Repo Agent

在终端中读取、修改和验证代码的 Coding Agent。支持多家模型 API、按项目保存会话、工具调用、native / Docker sandbox。

## 快速开始

安装器默认准备独立 Python，无需预装。macOS / Linux 首次默认 native 模式，使用 Seatbelt 或 Bubblewrap + seccomp，不需要 Docker；Linux 需预先安装 bubblewrap 和 libseccomp，并允许非特权 user namespace。Windows x86_64 已提供 ZIP 安装、local 文件/Git 模式和 Docker 适配，首次默认 local；尚无 Windows 原生沙箱，实机验证要求见[平台适配边界](docs/platform-adaptation.md)。

普通用户选择对应系统和架构的平台完整包：macOS/Linux 解压 `.tar.gz` 后运行 `./install-release.sh`；Windows 解压 ZIP 后按[Windows 安装说明](docs/installation.md#windows-x86_64-zip-安装)运行 `install_release.ps1`。运行环境保存在用户数据目录，安装后可删除下载目录。构建默认覆盖五个平台目标，产物位于 `dist/<版本>/<平台>/`，详见[构建与分发](docs/distribution.md)。

macOS/Linux（含 WSL2）开发者在源码目录安装，首次通常联网准备依赖；离线方式见[安装说明](docs/installation.md#离线安装)：

```bash
./install.sh
```

原生安装默认准备 Python 语言服务，并询问是否补齐语言工具链和语言服务（回车跳过）。Linux 请先用发行版包管理器准备这些系统工具链，安装器只安装虚拟环境内的语言服务，不自动调用 sudo。可用 `--with-toolchains` / `--skip-toolchains` 免交互选择。安装后执行 `repo-agent toolchains list` 查看状态，`repo-agent toolchains install go` 补齐单个语言，`repo-agent toolchains install all` 补齐全部；已有可用依赖会复用。只需文件/Git 工具可用 `./install.sh --mode local`。详见[模式与依赖](docs/installation.md#模式与依赖)。

Linux/macOS native 可选用 Rust 后端处理目录访问、批量元数据和执行前检查，默认仍为 Python。源码安装会尝试编译扩展；完整发行包可携带预编译 wheel，最终用户安装不需要 Rust 编译器。离线材料和启用方式见 [Rust 构建与后端说明](rust/README.md)。

Agent 与项目 Python 分开管理；native 自动使用已激活的 venv/Conda 或项目 `.venv`，也可用 `--project-python` 指定，见 [Python 环境](docs/python-environments.md)。

打开新终端，进入要处理的项目：

```bash
cd /path/to/project
repo-agent
```

首次启动缺少模型或 API Key 时，终端会引导配置。也可以先运行 `repo-agent config model`。不需要为每个项目复制启动文件或初始化配置；默认使用当前目录，`--root` 可指定其他项目。

**macOS / Linux 首次安装默认 native，Windows 首次安装默认 local；启动沿用已记录的安装模式。** native 直接修改当前项目并支持隔离命令、Python 和语言服务器；local 不执行通用命令。需要 Docker 工作副本时先准备镜像，再显式选择 Docker。Windows 还需 Git for Windows 和 Docker Desktop 的 Linux 容器模式：

```bash
repo-agent --sandbox native                      # macOS / Linux 原生沙箱
repo-agent --sandbox native --sandbox-profile standard # 强制关闭 GPU；Linux / WSL2 默认自动检测
repo-agent --sandbox local                       # 文件/Git 工具，不注册通用命令工具
repo-agent-build-sandbox                         # Docker 模式首次使用前构建
repo-agent --sandbox docker --sandbox-writeback manual
```

Docker 会话可使用 `/diff` 查看副本变更、`/apply` 回写原项目；local/native 的修改立即生效。旧配置的自动回写设置只作用于 Docker。原生后端权限及限制见 [macOS / Linux 原生沙箱](docs/native-sandbox.md)，安装与升级见[安装说明](docs/installation.md)。

## 当前功能

| 功能 | 说明 |
| --- | --- |
| 模型接入 | 9 个厂商预设，支持 Chat Completions、OpenAI Responses、Anthropic Messages、Gemini generateContent；[供应商列表](docs/llm.md#接入模型) |
| 对话界面 | 流式回复、可滚动历史、思考内容显示、模型切换、累计用量与上下文占比 |
| 会话管理 | 按项目保存，默认恢复最后一次会话；支持命名、改名、列表、指定恢复、会话切换和日志跟随 |
| 任务与执行证据 | 持久化串行队列、取消与暂停、`/continue` 续接；执行账本保存回执，支持按引用回读结果，见[执行账本](docs/execution-ledger.md)；任务结束展示[交付报告](docs/task-reports.md)，TUI 用 F3 或 `/report` 打开独立报告页，查看差异、验证与人工验收 |
| 工具并发 | 相邻且允许并发的调用滚动调度，默认最多 4 个；写入、进程和 Docker 代理保持串行，见[调度策略](docs/tools.md#并发调度策略) |
| 上下文管理 | `/compact` 手动/自动压缩、独立思考策略、有限精简与格式修复、失败诊断、原始消息归档及只读历史回查；[使用说明](docs/context-compaction.md) |
| 文件与检索 | 文件读写、局部编辑、多文件严格补丁、目录操作、文件查找、内容搜索、Git 状态、差异与提交历史 |
| 隔离执行 | macOS / Linux 原生沙箱直接运行本机工具；Docker 使用工作副本，支持回写、冲突检查与备份恢复 |
| 代码语义 | Python、JS/TS、Go、C/C++、CUDA 文件的符号、定义、引用、诊断、悬浮信息和工作区符号查询；具体能力取决于语言服务器 |
| 环境查询 | `get_execution_environment` 报告实际执行模式、工具链、权限、超时与可选 GPU 状态 |
| Skills | 内置 `coding`、`debug-and-fix`、`gpu-kernel-development`；通用系统提示词配合按需加载的专业技能，支持项目 `skills/` 目录 |
| GPU 开发 | Docker、Linux / WSL2 native 默认自动检测 NVIDIA GPU；CUDA / Triton 环境自检 |
| 诊断与追踪 | 配置校验及恢复、安装诊断、对话日志、模型和工具调用记录 |
| Web 搜索 | 可选 Brave Search API，支持批量查询、域名过滤及逐项错误；由主进程联网，命令沙箱保持断网，见 [Web 搜索](docs/web-search.md) |
| 网页读取 | 公开 HTML、纯文本和 JSON 的受控抓取、正文提取、缓存分页与 `web_find` 快照内查找；不需要搜索密钥，见 [Web 页面读取](docs/web-fetch.md) |

## 常用命令

在任务项目目录执行：

```bash
repo-agent                                      # 继续项目最后一次会话
repo-agent '读取 README.md，介绍这个项目'           # 单次任务，同样恢复并保存上下文
repo-agent --root /path/to/another-project       # 指定项目
repo-agent --new-session --name "修复登录问题"      # 新会话；省略名称时使用“会话 N”
repo-agent --session 3                           # 恢复指定会话
repo-agent sessions list                         # 查看会话
repo-agent sessions rename 1 "登录问题排查"        # 改名
repo-agent sessions logs 1 --tail 100 -f          # 持续查看对话日志
repo-agent sessions logs 1 --kind trace          # 查看执行日志
repo-agent config show                           # 生效配置及来源，Key 隐藏
repo-agent doctor                               # 按安装模式诊断依赖和实际语言服务
```

会话内常用输入：

| 输入 | 行为 |
| --- | --- |
| `/help` | 查看命令 |
| `/continue` | 达到调用上限后继续任务，再获得当前 `max_steps` 轮预算 |
| `/ledger` | 查看近期工具执行证据及已保存结果引用 |
| `/report [任务编号] [--diff]` | 查看任务交付报告：文件变化、工具操作差异、验证清单与实际执行证据 |
| `/queue` | 查看和管理持久化串行队列，见[任务队列](docs/task-queue.md) |
| `/new [名称]` | 新建会话，保留当前文件和沙箱副本 |
| `/switch 序号或名称` | 保存当前会话，恢复指定会话的上下文和用量 |
| `/rename 名称` 或 F2 | 修改当前会话名称 |
| `/sessions`、`/logs --tail 100` | 查看会话列表、日志 |
| `/clear` | 清空模型上下文，保留显示历史和累计用量 |
| `/compact` | 压缩工作上下文，先归档原文，再保留摘要及近期消息 |
| `/model` | 设置并切换模型 |
| `/thinking`、`/context` | 查看思考设置、上下文占用 |
| `/skills` | 查看可用技能 |
| `/diff`、`/apply` | Docker 副本差异与回写 |
| `/exit`、`/quit` | 保存并退出 |

快捷键、任务中断及截断恢复见[使用说明](docs/usage.md)。命名、保存位置与恢复规则见[会话管理](docs/sessions.md)。

## 执行环境与边界

| 模式 | 操作位置 | 命令、Python、代码符号工具 | 文件如何生效 |
| --- | --- | --- | --- |
| `--sandbox local` | 原项目 | 不注册 | 直接修改原项目 |
| `--sandbox native`（默认，macOS / Linux） | 原项目 | 可用，平台原生隔离，工具断网 | 直接修改原项目，无回写备份 |
| `--sandbox docker` | 项目工作副本 | 可用，工具运行时断网 | 手动或按配置自动回写 |

Docker 失败不会自动切换为 local。每次工具调用使用独立容器：工作副本文件保留，进程和 `/tmp` 不跨调用保留。Docker GPU 模式需要可用的 NVIDIA 驱动和 NVIDIA Container Toolkit；Linux / WSL2 native GPU 使用本机驱动及已准备的框架/Toolkit，无需 Docker。两者均需在实际执行环境核验依赖，详见[GPU 算子开发](docs/gpu-operators.md)。

local 的 Git 工具仍会启动 Git 子进程；发现外部 clean/process 过滤器配置时拒绝查询，需使用 native/Docker。子模块查询及并发配置限制见 [Git 执行边界](docs/tools.md#git-工具的执行边界)。

`get_execution_environment` 在各模式均可用，local 不启动环境探测子进程。可选 `web_search` / `web_fetch` 由主进程联网，独立于上表的命令、Python 和语言服务器权限。启用网页读取时同时提供 `web_find`，仅在已有网页快照中定位关键词，不联网。

当前支持文本和自定义函数工具；尚无多模态输入、跨会话知识记忆、多 Agent 协作或整项任务的费用预算。保存会话用于延续对话，不会自动提炼长期知识。模型正常结束回答不等于验证通过，应结合实际测试和工具结果判断。

## 文档导航

完整目录见 [文档首页](docs/index.md)。常用入口：

- 开始使用：[安装与升级](docs/installation.md)、[命令与快捷键](docs/usage.md)、[配置参考](docs/configuration.md)。
- 管理对话：[会话恢复](docs/sessions.md)、[任务队列](docs/task-queue.md)、[执行账本](docs/execution-ledger.md)、[任务交付报告](docs/task-reports.md)、[上下文压缩](docs/context-compaction.md)、[思考设置](docs/thinking.md)、[日志](docs/logging.md)。
- 执行任务：[原生沙箱](docs/native-sandbox.md)、[Docker 与回写](sandbox/README.md)、[GPU 开发](docs/gpu-operators.md)、[Skills](docs/skills.md)、[Web 搜索](docs/web-search.md)、[网页读取](docs/web-fetch.md)。
- 参与开发：[项目架构](Project_Architecture.md)、[开发与验证](docs/development.md)、[工具开发](docs/tools.md)、[模型接口](docs/llm.md)、[模型目录](docs/model-catalog.md)、[构建与分发](docs/distribution.md)。

默认测试使用模拟模型响应。真实 native、Docker、多语言、GPU 和发行安装验证需要相应环境及显式开关，见[验证矩阵](docs/development.md#开发环境与验证)。
