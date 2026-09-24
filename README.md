# Repo Agent

在终端中读取、修改和验证代码的 Coding Agent。支持多家模型 API、按项目保存会话、工具调用、native / Docker 沙箱，以及 CUDA / Triton / PyTorch 算子开发。

## 快速开始

需要 macOS / Linux 和 Python 3.11+。首次默认 native 模式，支持 macOS（Seatbelt）和 Linux（Bubblewrap + seccomp），不需要 Docker。Linux 需预先安装 bubblewrap 和 libseccomp，并允许非特权 user namespace；首次安装需要联网下载依赖。

普通用户可使用独立发行包，解压后运行 `./install-release.sh`；运行环境保存在用户数据目录，安装后可删除下载目录。构建与使用步骤见[独立发行版安装](docs/installation.md#独立发行版安装)。

开发者在源码目录安装：

```bash
./install.sh
```

原生安装默认准备 Python 语言服务，并询问是否补齐 JS/TS、Go、C/C++ 工具链和语言服务（回车跳过）。macOS 选择补齐后，仅为缺少或不可用的 Node.js、Go 1.25+、LLVM 调用 Homebrew。Linux 请先用发行版包管理器准备这些系统工具链，安装器只安装虚拟环境内的语言服务，不自动调用 sudo。可用 `--with-toolchains` / `--skip-toolchains` 免交互选择。安装后执行 `repo-agent toolchains list` 查看状态，`repo-agent toolchains install go` 补齐单个语言，`repo-agent toolchains install all` 补齐全部；已有可用依赖会复用。只需文件/Git 工具可用 `./install.sh --mode local`。详见[模式与依赖](docs/installation.md#模式与依赖)。

打开新终端，进入要处理的项目：

```bash
cd /path/to/project
repo-agent
```

首次启动缺少模型或 API Key 时，终端会引导配置。也可以先运行 `repo-agent config model`。不需要为每个项目复制启动文件或初始化配置；默认使用当前目录，`--root` 可指定其他项目。

**首次默认使用 macOS / Linux 原生沙箱（native），直接修改当前项目文件，支持命令、Python 和语言服务器。安装时显式选择其他模式后，启动会沿用该安装模式。** 仅需文件/Git 工具时可显式选择 local；需要 Docker 工作副本时先构建镜像，再显式选择 Docker：

```bash
repo-agent --sandbox native                      # macOS / Linux 原生沙箱
repo-agent --sandbox native --sandbox-profile standard # 强制关闭 GPU；Linux / WSL2 默认自动检测
repo-agent --sandbox local                       # 仅文件/Git 工具，不执行命令
repo-agent-build-sandbox                         # Docker 模式首次使用前构建
repo-agent --sandbox docker --sandbox-writeback manual
```

Docker 会话可使用 `/diff` 查看副本变更、`/apply` 回写原项目；local/native 的修改立即生效。旧配置的自动回写设置只作用于 Docker。原生后端权限及限制见 [macOS / Linux 原生沙箱](docs/native-sandbox.md)，安装与升级见[安装说明](docs/installation.md)。

## 当前功能

| 功能 | 说明 |
| --- | --- |
| 模型接入 | 9 个厂商预设，支持 Chat Completions、OpenAI Responses、Anthropic Messages、Gemini generateContent；[供应商列表](docs/llm.md#接入模型) |
| 对话界面 | 流式回复、可滚动历史、思考内容显示、模型切换、累计用量与上下文占比 |
| 会话管理 | 按项目保存，默认恢复最后一次会话；支持命名、改名、列表和日志跟随 |
| 上下文管理 | `/compact` 手动压缩、按预算自动压缩、原始消息归档及只读历史回查；[使用说明](docs/context-compaction.md) |
| 文件与检索 | 文件读写、局部编辑、多文件严格补丁、目录操作、文件查找、内容搜索、Git 状态与差异 |
| 隔离执行 | macOS / Linux 原生沙箱直接运行本机工具；Docker 使用工作副本，支持回写、冲突检查与备份恢复 |
| 代码语义 | Python、JS/TS、Go、C/C++、CUDA 文件的符号、定义、引用、诊断、悬浮信息和工作区符号查询；具体能力取决于语言服务器 |
| 环境查询 | `get_execution_environment` 报告实际执行模式、工具链、权限、超时与可选 GPU 状态 |
| Skills | 内置 `coding`、`debug-and-fix`、`gpu-kernel-development`；通用系统提示词配合按需加载的专业技能，支持项目 `skills/` 目录 |
| GPU 开发 | Docker、Linux / WSL2 native 默认自动检测 NVIDIA GPU；CUDA / Triton 环境自检 |
| 诊断与追踪 | 配置校验及恢复、安装诊断、对话日志、模型和工具调用记录 |
| Web 搜索 | 可选 Brave Search API，支持批量查询、域名过滤及逐项错误；由主进程联网，命令沙箱保持断网，见 [Web 搜索](docs/web-search.md) |
| 网页读取 | 公开 HTML、纯文本和 JSON 的受控抓取、正文提取与缓存分页；不需要搜索密钥，见 [Web 页面读取](docs/web-fetch.md) |

## 常用命令

在任务项目目录执行：

```bash
repo-agent                                      # 继续项目最后一次会话
repo-agent '读取 README.md，介绍这个项目'           # 单次任务，同样恢复并保存上下文
repo-agent --root /path/to/another-project       # 指定项目
repo-agent --new-session --name "修复登录问题"      # 新会话；省略名称时使用“会话 N”
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
| `/new [名称]` | 新建会话，保留当前文件和沙箱副本 |
| `/rename 名称` 或 F2 | 修改当前会话名称 |
| `/sessions`、`/logs --tail 100` | 查看会话列表、日志 |
| `/clear` | 清空模型上下文，保留显示历史和累计用量 |
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

`get_execution_environment` 在各模式均可用，local 不启动环境探测子进程。可选 `web_search` / `web_fetch` 由主进程联网，独立于上表的命令、Python 和语言服务器权限。

当前支持文本和自定义函数工具；尚无多模态输入、自动上下文压缩、跨会话知识记忆、多 Agent 协作、并行工具执行或整项任务的费用预算。保存会话用于延续对话，不会自动提炼长期知识。模型正常结束回答不等于验证通过，应结合实际测试和工具结果判断。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [安装与卸载](docs/installation.md) | 首次安装、升级、切换安装目录、诊断、卸载与恢复 |
| [macOS / Linux 原生沙箱](docs/native-sandbox.md) | 本机执行、权限策略、依赖及隔离限制 |
| [启动、交互与上下文](docs/usage.md) | 启动方式、完整命令表、快捷键、任务状态、上下文估算 |
| [会话管理](docs/sessions.md) | 命名、恢复、保存位置、兼容和沙箱关系 |
| [配置参考](docs/configuration.md) | 配置优先级、模板与内置默认值、修改及备份恢复 |
| [思考设置](docs/thinking.md) | 模型档位、可见思考、偏好与能力覆盖 |
| [日志与文件保护](docs/logging.md) | cat / tail / 跟随、两类日志、统计口径与文件保护 |
| [Sandbox](sandbox/README.md) | 副本、执行策略、自动回写、冲突与备份恢复 |
| [GPU 算子开发](docs/gpu-operators.md) | 环境检测、GPU 配置、CUDA / Triton 自检 |
| [Skills](docs/skills.md) | 内置及项目技能、发现和加载规则 |
| [项目架构](Project_Architecture.md) | 当前模块、调用流程与状态归属 |
| [统一模型接口](docs/llm.md) | Python API、协议适配、原生消息状态、错误与流式事件 |
| [开发指南](docs/development.md) | Runtime、工具扩展、语言服务器与测试 |

默认测试使用模拟模型响应。真实 Docker、多语言和 CUDA 验证需要相应环境及显式开关，命令见[开发与验证](docs/development.md#开发环境与验证)。
