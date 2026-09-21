# Repo Agent

面向代码仓库的终端 Coding Agent。通过统一模型接口理解任务，调用文件、搜索、命令、Git 和代码符号工具，并在多轮对话中持续修改、验证项目。

**默认在 Docker 项目副本中执行工具，修改需要回写后才会影响原项目。** 模型请求、API Key 和执行日志留在宿主机，工具容器运行时不联网。

## 导航

- [功能与边界](#功能与边界)
- [快速开始](#快速开始)
- [日常使用](#日常使用)
- [模型与配置](#模型与配置)
- [执行日志](#执行日志)
- [项目结构](#项目结构)
- [开发与验证](#开发与验证)
- [详细文档](#详细文档)

## 功能与边界

| 能力 | 当前实现 |
| --- | --- |
| 模型接入 | 9 个厂商预设，覆盖 Chat Completions、OpenAI Responses、Anthropic Messages、Gemini generateContent |
| 终端交互 | 多轮上下文、流式输出、可滚动对话区、思考配置切换、token 与上下文占用显示 |
| 文件操作 | 读取、写入、局部／批量编辑、移动、删除、创建目录、查看路径信息 |
| 仓库检索 | 文件名查找、内容搜索、Git 状态与差异 |
| 隔离执行 | Docker 内运行命令和 Python；项目副本保留，支持差异检查、回写与备份恢复 |
| 代码符号 | 通过语言服务器读取 Python、JS/TS、Go、C/C++ 的代码符号 |
| Skills | 内置 `debug-and-fix`，支持项目技能与按需加载 |
| 执行追踪 | 模型／工具调用、错误、响应计时、token 用量、任务和会话汇总 |

当前 Agent 按顺序执行工具，会话保存在内存中。尚未实现上下文自动压缩、退出后恢复对话、多 agent 协作、并行工具执行或整项任务的 token／费用预算。模型层目前支持文本与自定义函数工具，不支持图片、音频、Embedding 或自动模型路由。

## 快速开始

### 1. 拉取并一键安装

需要 **macOS / Linux、Python 3.11+ 和已启动的 Docker**。首次安装需要联网下载 Python 依赖和构建工具镜像。

```bash
git clone <你的 GitHub 仓库地址> coding_agent
cd coding_agent
./install.sh
```

脚本会创建安装目录的 `.venv`、安装 Agent、生成默认配置文件、构建 Docker 工具镜像，并将 `repo-agent` 安装到 `~/.local/bin`。安装时无需提供模型信息或 API Key。配置保存到 `~/.config/repo-agent/.env`，权限为 `600`；重复安装保留已有内容，包括尚未填写的配置。

安装完成后，编辑该文件再启动 Agent：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=你的模型ID
DEEPSEEK_API_KEY=你的APIKey
```

模板还包含其他厂商的 Key 和运行参数；只需填写所选厂商的 Key。

脚本会为 Bash / Zsh 补充 PATH。安装完成后打开新终端；如果想在当前终端立即使用，执行脚本最后显示的 `export PATH=...` 命令。其他 shell 请手动配置 PATH。安装目录需要保留，命令和虚拟环境依赖该目录。

### 2. 在任意项目启动

```bash
cd /path/to/project
repo-agent

# 单次任务
repo-agent '读取 README.md，介绍这个项目'

# 显式选择工作目录
repo-agent --root /path/to/another-project
```

**当前目录就是工作目录**，无需激活虚拟环境、复制启动脚本或执行项目初始化。所有项目共用用户配置；需要项目特定设置时，可选地在该项目 `.env` 中填写覆盖项。

### 3. 安装选项与后续配置

```bash
# 已有镜像，或暂时只用文件操作时跳过构建
./install.sh --skip-sandbox

# 自定义命令目录，不修改 shell 启动文件
./install.sh --bin-dir /path/to/bin --no-path
```

`--skip-sandbox` 只跳过构建，不改变默认执行模式。无 Docker 时可以运行 `repo-agent --sandbox local`，该模式只提供文件等工具，不执行命令或 Python。默认 Docker 模式需要可用镜像；镜像构建失败时脚本报错，修复 Docker 后重新执行即可。

修改模型和密钥时编辑用户配置，重启 Agent 后生效。可用 `XDG_CONFIG_HOME` 修改配置基目录，或用 `AGENT_CONFIG_DIR` 直接指定 Repo Agent 配置目录。它们在启动前设置，不写入 `.env`。

镜像包含 Git、Python、Node.js、Go、C/C++ 工具链和语言服务器。任务项目第三方依赖仍需预置到派生镜像；宿主机 `.venv`、`node_modules` 不会复制进去。详见 [Sandbox 使用说明](sandbox/README.md)。

原有 `run_agent.sh` 和 `--init` 流程继续可用，适合需要终端录制或项目独立配置的场景，见[启动文档](docs/usage.md)。

### 4. 执行任务并回写

进入交互模式后，可以输入：

```text
读取 README.md 和项目结构，概括当前实现
$debug-and-fix 定位并修复失败的测试，报告验证结果
/diff
/apply
/exit
```

`/diff` 查看副本变更，`/apply` 回写到原项目。退出时会显示保留的沙箱会话目录，也可以稍后查看和回写：

```bash
repo-agent --sandbox-review /absolute/session-directory
repo-agent --sandbox-review /absolute/session-directory --apply
```

这些查看／回写命令不需要模型配置。副本位于系统临时目录，需要长期保存时请保存整个会话目录。

## 日常使用

### 启动方式

```bash
# 在任务项目目录执行；不传任务文字时进入交互模式
repo-agent

# 单次任务
repo-agent '读取 README.md，介绍这个项目'

# 显式使用技能；单引号避免终端展开 $变量
repo-agent '$debug-and-fix 修复启动错误并验证'

# 临时调整本次会话的轮数和输出上限
repo-agent --max-steps 12 --max-output-tokens 8192
```

`repo-agent` 默认以调用时的当前目录作为项目根目录，`--root` 可指定其他目录，并加载该目录的 `.env`。不会切换到安装目录。旧版 `run_agent.sh` 固定使用调用目录，同时录制终端对话。

### 会话命令与快捷键

| 输入 | 行为 |
| --- | --- |
| `/help` | 查看帮助 |
| `/clear` | 清空对话上下文，保留文件变更和会话累计用量 |
| `/skills` | 查看可用技能；任务开头用 `$技能名` 显式加载 |
| `/thinking` | 查看思考配置；`/thinking next` 切换预设 |
| `/context` | 查看上下文估算；`/context 131072` 设置窗口上限示例，须按实际模型填写 |
| `/diff`、`/apply` | Docker 模式下查看变更、回写原项目 |
| `/exit`、`/quit` | 退出会话 |
| Enter / Alt+Enter | 发送任务 / 换行 |
| Shift+Tab | 切换当前厂商的思考预设 |
| PgUp / PgDn / Ctrl+End | 浏览历史 / 回到最新输出 |
| Ctrl+C | 清空草稿，或请求停止正在执行的任务 |
| Ctrl+D | 空闲且输入为空时退出 |

停止采用协作方式，需要等待当前网络读取或工具返回；已经完成的文件修改不会撤销。达到轮数上限后可以输入“继续”；模型异常、中断或非正常回复会清空对话上下文。会话退出后不恢复对话。

上下文占用取最近一次请求返回的输入加输出 token 作为估算，区别于整个会话累计用量。窗口上限只影响显示，不会自动压缩历史。完整说明见[启动、交互与上下文](docs/usage.md)。

### 执行模式与回写

| 模式 | 文件操作位置 | 命令、Python、代码符号工具 | 变更生效方式 |
| --- | --- | --- | --- |
| `--sandbox docker`（默认） | 隔离项目副本 | 可用，运行时断网 | `/apply` 或配置自动回写 |
| `--sandbox local` | 原项目 | 不注册 | 直接修改原项目 |

Docker 不可用时会报错，不会自动切换到本地执行。每次工具调用使用独立容器：工作副本文件持久化，后台进程和临时环境不跨调用保留。副本使用独立 Git 基线，不带入宿主机 Git 历史。

可在任务项目 `.env` 中开启自动回写，并指定最终检查：

```dotenv
AGENT_SANDBOX_WRITEBACK=on-success
AGENT_SANDBOX_VERIFY_COMMAND='["python", "-m", "unittest", "discover"]'
```

请将检查命令替换为项目实际使用的命令，并在镜像中准备依赖。`on-success` 在任务正常结束且没有未解决执行失败或文件冲突时回写；**模型宣称完成不等于测试通过**。最终检查仅用于自动回写，不在手动 `/apply` 时执行。回写前会保存备份，多文件回写不是事务。完整条件与恢复方法见 [Sandbox 使用说明](sandbox/README.md)。

## 模型与配置

以下为本项目已实现的预设；模型 ID 由用户指定，不自动替换。

| provider | 别名 | API Key 环境变量 |
| --- | --- | --- |
| `deepseek` | — | `DEEPSEEK_API_KEY` |
| `qwen` | — | `DASHSCOPE_API_KEY` |
| `moonshot` | `kimi` | `MOONSHOT_API_KEY` |
| `zhipu` | `glm` | `ZHIPU_API_KEY` |
| `doubao` | — | `ARK_API_KEY` |
| `minimax` | — | `MINIMAX_API_KEY` |
| `openai` | `chatgpt` | `OPENAI_API_KEY` |
| `anthropic` | `claude` | `ANTHROPIC_API_KEY` |
| `gemini` | — | `GEMINI_API_KEY` |

`chatgpt` 在本项目中只是 OpenAI API 的别名，使用 API Key。各模型和网关对工具调用、思考模式等参数的支持范围需实际确认。

配置优先级为：**命令行参数 > 已有非空环境变量 > 项目 `.env` > 用户配置 `~/.config/repo-agent/.env` > 内置默认值**。项目中的显式空值会清除对应用户配置。修改 `.env` 后需重启；`/thinking` 和 `/context` 的会话内修改不会写回文件。

| 常用配置 | 默认值 | 用途 |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | `deepseek` / 必填 | 厂商与模型 |
| `LLM_BASE_URL` | 厂商预设 | 自定义 API 基址，不追加方法路径 |
| `AGENT_MAX_STEPS` | `8` | 每条任务最多调用模型的轮数 |
| `AGENT_MAX_OUTPUT_TOKENS` | `4096` | 每次模型请求的输出上限 |
| `LLM_THINKING` | `auto` | 不指定思考开关；不保证关闭思考 |
| `LLM_CONTEXT_WINDOW` | 空 | 上下文窗口上限，仅用于显示 |
| `LLM_STREAM` | `true` | 流式接收；不兼容的网关可关闭 |
| `LLM_TIMEOUT` | `300` 秒 | 首批及相邻网络数据的读取等待上限，非任务总时限 |
| `AGENT_SANDBOX_WRITEBACK` | `manual` | 手动或 `on-success` 自动回写 |
| `AGENT_LOG_DIR` | `logs` | 日志目录 |

完整配置见 [`.env.example`](.env.example) 和[配置与思考模式](docs/configuration.md)。安装目录的可选 `.env.defaults` 只用于初始化项目，不参与日常启动；其中的 API Key 也会复制到新项目。已有项目执行 `./run_agent.sh --init` 可补齐新增配置，不覆盖已有值。

## 执行日志

启动脚本在任务项目 `logs/` 下为同一会话生成三份文件：

| 文件 | 内容 |
| --- | --- |
| `session_时间_进程号.log` | 终端对话与必要提示 |
| `session_时间_进程号.trace.log` | 可读的模型／工具记录、任务和会话汇总 |
| `session_时间_进程号.trace.jsonl` | 结构化事件，适合统计和比较运行结果 |

直接使用 `repo-agent` 只生成两份追踪文件。日志包含耗时、调用次数、失败状态和接口返回的 token 用量；未知用量不会按零计算，token 统计也不等同于实际费用。完整统计口径见[执行日志与文件保护](docs/logging.md)。

文件工具及 Docker 导入／回写会过滤 `.env`、凭据、`.git`、`.codex`、`.agents`、日志等受保护路径。配置文件请在编辑器中修改。保护规则不识别普通源码中嵌入的密钥，`.gitignore` 也不是访问控制。

## 项目结构

```text
agent/                 # 同步运行循环、Tracing.py 追踪、Skills 注册与加载
cli/                   # CLI 参数、全屏终端、交互状态、项目初始化
docs/                  # 使用与开发专题文档
llm/                   # 统一数据结构、同步／异步客户端、流式解析、协议适配
tools/                 # 文件、搜索、命令、Git、语言服务器工具
sandbox/               # Docker 镜像、工作副本、执行策略、回写与恢复
examples/              # 模型调用与离线演示
tests/                 # 自动化测试
install.sh             # 一键安装、用户配置、Docker 镜像和 PATH
run_agent.sh           # 兼容旧版启动与终端录制
.env.example           # 项目配置模板
pyproject.toml         # 依赖、CLI 入口与开发工具配置
```

`eval/`、`mcp/`、`retrieval/` 目前为预留目录。`Project_Architecture.md` 是早期结构草案，实际模块以当前代码为准。

## 开发与验证

在安装目录激活 `.venv` 后执行：

```bash
# 默认测试；真实 Docker 测试需要额外开关
python -m pytest -q

# 模型层的静态与格式检查
ruff check llm tests examples
ruff format --check llm tests examples

# 构建镜像后，显式运行真实容器与多语言符号测试
RUN_SANDBOX_DOCKER_TESTS=1 python -m pytest -q tests/test_sandbox.py tests/test_multilang_sandbox.py
```

默认测试中的模型响应使用本地模拟，不消耗在线推理额度，也不能证明某个具体模型、账号或地域端点在线可用。多语言镜像构建还会执行语言服务器查询自检。

升级时在安装目录执行，再启动新会话：

```bash
git pull
./install.sh
```

脚本保留用户配置并重建工具镜像。只修改宿主机代码时可加 `--skip-sandbox`；需要开发工具时另执行 `.venv/bin/python -m pip install -e '.[dev]'`。移动安装目录后应重建虚拟环境，并更新 `~/.local/bin/repo-agent` 链接。

## 详细文档

| 文档 | 内容 |
| --- | --- |
| [启动、交互与上下文](docs/usage.md) | 初始化、启动目录、快捷键、流式计时、上下文估算 |
| [配置与思考模式](docs/configuration.md) | 完整配置项、默认值、优先级、厂商参数映射 |
| [Sandbox 使用说明](sandbox/README.md) | 镜像依赖、隔离策略、Git 语义、回写、备份恢复与限制 |
| [Skills](docs/skills.md) | 显式调用、自定义技能、加载规则、权限与验证 |
| [执行日志与文件保护](docs/logging.md) | 追踪事件、统计口径、日志限制、受保护路径 |
| [统一模型接口](docs/llm.md) | Python 调用、工具结果回传、原生状态、用量与异常 |
| [Agent Runtime 与工具开发](docs/development.md) | 运行循环、工具工厂、进程执行、错误码、多语言代码符号 |

## CUDA / Triton / PyTorch 算子开发

新增 CUDA 文件 `.cu` / `.cuh` 的符号支持，以及 `$gpu-kernel-development` 内置技能。
构建统一使用 `./run_agent.sh --build-sandbox`，任务统一使用 `./run_agent.sh "任务内容"`。
两者自动检测当前 Docker 主机：可用 NVIDIA GPU 环境安装 PyTorch、Triton、nvcc 并启用 GPU；
普通环境使用轻量依赖与原有资源配置。只有一个 `sandbox/Dockerfile`，镜像名称统一为
`repo-agent-sandbox:v1`，无需按任务类型选择启动命令。
Triton/PyTorch 的 `.py` 文件使用现有 Python 语言服务器。

构建、GPU 选择、资源配置、环境探测、实际算子自检和验证限制见 [GPU 算子开发指南](docs/gpu-operators.md)。
