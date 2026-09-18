# 启动、交互与上下文

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 安装一次，在任意目录启动

```bash
# 拉取仓库后，在安装目录执行一次
./install.sh

# 打开新终端，在目标项目启动
cd /path/to/project
repo-agent
repo-agent '修复测试失败并验证'
repo-agent --root /path/to/another-project
```

安装脚本创建独立虚拟环境、询问并保存用户模型配置、构建 Docker 镜像，安装 `~/.local/bin/repo-agent` 并配置 Bash / Zsh 的 PATH。当前终端的 PATH 无法由子进程修改，需新开终端或执行安装结尾打印的命令。无需逐个初始化项目。`--root` 选择工作目录及其 `.env`，默认使用当前目录；用户配置见[配置说明](configuration.md)。

重复执行安装脚本保留配置。`--skip-sandbox` 跳过镜像构建，`--non-interactive` 从环境变量读取首次模型配置，`--no-path` 不修改 shell 配置。默认 Docker 隔离和手动回写行为保持不变。

`repo-agent` 生成两份执行追踪文件；若需要额外的完整终端录制，可使用下面保留的旧启动脚本流程。

## 构建统一沙箱

首次使用或更新沙箱代码后执行：

```bash
./run_agent.sh --build-sandbox
```

构建不需要模型配置。它检测当前 Docker 主机，自动选择普通环境或 CUDA 环境，
使用同一个 Dockerfile 和镜像名称。任务启动时会再次检测并校验镜像环境；普通开发和
GPU 算子开发使用下面相同的启动命令。Linux GPU 主机需先配置 NVIDIA 驱动和
NVIDIA Container Toolkit，详细检测规则见 [GPU 算子开发指南](gpu-operators.md)。

## 项目初始化与启动脚本（可选旧版流程）

在 macOS 和 Linux 上，Agent 代码和 `.venv` 保留在安装目录；每个任务目录独立保存 `.env`、轻量启动脚本和 `logs/`。首次在一个任务目录使用时执行：

```bash
cd '/你的项目目录'
/path/to/coding_agent/run_agent.sh --init
```

初始化会生成 `run_agent.sh`、权限为 `600` 的 `.env`，并向 `.gitignore` 补充配置和日志的忽略规则。已有启动脚本会保留；已有配置文件只补充缺少的配置项，重复初始化不会覆盖原有值或 Key。初始化合并 `.env.example` 和安装目录的 `.env.defaults`，生成项目配置；不读取安装目录的 `.env`，也不复制代码或虚拟环境。如果在 `.env.defaults` 中填写了 API Key，这些 Key 也会复制到新项目。

手动填写任务目录中的 `.env`：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=你的模型ID
DEEPSEEK_API_KEY=你的真实Key
```

随后在任务目录启动：

```bash
./run_agent.sh
# 或只执行一条任务
./run_agent.sh '读取 README.md，介绍这个项目'
```

项目内脚本只转发到共享安装目录，升级启动逻辑无需逐个修改项目。若安装目录移动，在终端设置 `export AGENT_HOME='/新的安装目录'`，或修改项目启动脚本中的 `agent_default_home`。`AGENT_HOME` 必须指向包含 Agent 代码和 `.venv` 的安装目录；它在读取 `.env` 前使用，不能写在 `.env` 中。

**启动时的当前目录**作为 Agent 根目录、默认 `.env` 所在目录和默认日志目录的父目录。即使使用启动脚本的绝对路径，也不会切换到脚本所在目录。脚本固定使用启动目录作为根目录，不通过 `--root` 切换。也可以直接在任务目录调用安装目录的 `run_agent.sh`，行为一致。

已有非空环境变量优先于 `.env`，CLI 模型参数优先级最高，例如 `./run_agent.sh --provider qwen --model '模型ID'`。可以通过 `AGENT_LOG_DIR` 指定其他日志目录，通过启动前设置的 `AGENT_ENV_FILE` 指定配置文件；相对路径均相对于启动目录。`.env` 仅支持字面量 `KEY=VALUE`、可选 `export` 前缀、成对引号和整行注释，不执行变量展开或命令替换。未配置模型或 Key 时，CLI 会报出缺少的配置名称。

## 交互与流式输出

当前范围是**流式/非流式文本与自定义函数工具**。图片、音频、模型内置搜索工具、Embedding 和模型路由未实现。

默认 `LLM_STREAM=true`（`--stream`），覆盖 Chat Completions、OpenAI Responses、Anthropic 和 Gemini。客户端逐段读取 SSE，完整拼接并校验文本、思考状态和工具参数后返回 `LLMResponse`；终端实时显示正文增量（包括中间轮次的正文），最终回复不重复打印；思考正文和工具参数不直接输出。工具只在流完整结束且校验通过后执行。可用 `LLM_STREAM=false` 或 `--no-stream` 兼容不支持流式的网关；网关返回普通 JSON 也可正常解析。

每次模型响应结束后显示首行数据等待、首个正文等待、首字后的生成时间、显示延迟和响应总时长。计时从客户端本次调用开始，包含可能发生的 HTTP 重试退避；首行数据包含 SSE 心跳，不等同于首个正文 token。普通 JSON 响应的首行数据指标在完整读取后记录，不能作为首字节指标。没有正文（例如纯工具调用）的首字及显示时间记为未收到。失败时已展示的正文可能不完整，工具仍须完整验证后执行。

`.trace.jsonl` 的 `model_end.model_call` 包含 `first_data_seconds`、`first_text_seconds`、`first_display_seconds`、`response_seconds` 和 `thinking`；未发生的事件为 `null`。显示延迟是本地写入并刷新终端输出的耗时估计，不测量终端模拟器实际绘制屏幕的时间。同步和异步客户端均可通过 `generate_with_events(request, callback)` 接收 `(kind, text, elapsed_seconds)`；回调须快速返回。

交互输入框底部显示当前思考模式、强度、预算及输出上限，以及本次 session 累计输入/输出 token 和工作目录的绝对路径（以 `--root` 为准，Docker 模式显示对应的原项目目录）。每次模型请求结束后累计接口返回的用量；`/clear`、任务失败和思考切换不会清零，新启动的 session 从零开始。接口未返回的 token 标为未知，部分请求缺少用量时标为“部分已知”，不当作零。非交互终端在每次输入前打印同样的统计。按 **Shift+Tab** 循环切换厂商对应的预设，保留当前输入；也可使用：

```text
/thinking
/thinking next
/thinking off
/thinking on high
/thinking auto
/thinking on budget=2048
/thinking adaptive high
```

`/thinking` 无参数只查看；预算示例适用于支持预算的厂商，`adaptive` 仅映射 Claude。预设只经过客户端参数映射校验，具体模型或网关是否支持仍以服务端为准。`auto` 表示不发送思考配置，不是自动降低思考强度。设置仅在当前会话生效，不修改 `.env`，无需清空历史；已发出的模型请求不会被更改。生成期间输入框仍可编辑草稿，但不会并发提交第二个任务；思考切换需等待当前任务结束。非交互终端使用普通输入，可通过 `/thinking` 切换。`LLM_EXTRA_JSON` 已含原生思考参数时拒绝快捷切换，避免静默覆盖。

交互终端使用持续运行的 `prompt_toolkit.Application`：顶部会话标题，中间可滚动对话区，底部固定输入框、运行状态、思考配置和 token/目录状态栏。模型/工具在后台执行，UI 始终响应按键；流式片段按约 25ms 合并到聊天区，避免逐 token 重建画面。工具进度实时显示。界面展示的“显示延迟”是输出进入 UI 队列的估计，不包含这一合并窗口或终端绘制时间。

- Enter 发送；Alt+Enter 换行；Tab 补全命令。
- PgUp / PgDn 浏览历史；Ctrl+End 回到最新内容并恢复自动跟随。
- Shift+Tab 切换思考预设；Ctrl+C 清空草稿或请求停止运行中的任务。
- 停止为协作式：等待当前网络读取、流式事件或工具返回后终止，不强杀正在写文件的线程；此时不能启动另一任务或退出。Ctrl+D 在空输入且空闲时退出。
- `/help`、`/clear`、`/thinking`、`/diff`、`/apply`、`/exit` 保持可用。退出全屏界面后输出可读会话记录，保留终端回滚历史；管道输入及单次任务继续使用普通文本模式。

终端快捷键使用 [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)，安装项目依赖后即可使用。

超时默认值（秒）：`LLM_CONNECT_TIMEOUT=10`、`LLM_TIMEOUT=300`、`LLM_WRITE_TIMEOUT=30`、`LLM_POOL_TIMEOUT=10`，对应参数 `--connect-timeout`、`--timeout`、`--write-timeout`、`--pool-timeout`。读取超时限制等待首批数据及相邻网络数据的间隔，不限制整个生成时长；持续收到数据或心跳时可以生成超过 300 秒。长时间无任何数据的思考模型可提高 `--timeout`。超时、断流、无完成标记及损坏的工具参数不会自动重试或执行部分工具调用。HTTP 429 和可重试服务端错误仍按原有上限退避重试。

协议参考：[Chat Completions](https://github.com/openai/openai-python/blob/main/src/openai/lib/streaming/chat/_completions.py)、[Responses](https://platform.openai.com/docs/api-reference/responses-streaming)、[Anthropic](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Gemini](https://ai.google.dev/api/generate-content)。

## 多轮上下文与任务状态

每条输入是自然语言任务，不是直接执行的终端命令。Agent 会按需使用已注册的读写文件工具。后续任务保留之前的消息、工具结果和厂商原生状态；模型和项目根目录在本次会话内固定。

如果模型正在执行任务，需要退出时先按 Ctrl+C 中断，回到输入区后输入 `/exit` 或 `/quit`；在输入提示处也可以按 Ctrl+D 退出。已经完成的文件修改不会撤销。

`--max-steps` 对每条任务单独计数。达到轮数上限后仍保留已执行的工具结果，可以输入“继续”请求下一步。模型调用失败、任务中断或回复截断／被拦截时，会清空上下文，避免下一次请求带入不完整的工具调用；已经执行的文件操作不会撤销，也不会自动重试整个任务。

会话暂存在内存中，退出后不恢复；暂不压缩长对话，遇到上下文限制可以使用 `/clear`。交互模式与单任务模式使用同一组模型参数和环境变量，直接运行 CLI 和启动脚本都会自动加载配置。

### 上下文占用估算

底栏同时显示 session 累计用量和当前上下文估算。上下文使用**最近一次请求的 input_tokens + output_tokens**，不是整个 session token 的累加；包含接口归入输出的思考 token，可能与下一次实际发送的 token 数不同。新生成的工具结果在下一次请求返回用量后计入，期间显示待更新；草稿不计入。缺少任一用量字段显示未知，不沿用旧百分比。`/clear`、中断或失败导致历史清空时重置上下文显示，但不重置 session 累计用量。

模型窗口上限须与实际服务商/网关一致，通过 `.env` 的 `LLM_CONTEXT_WINDOW` 或 `--context-window` 设置；未设置时显示上限未知，不猜测模型容量。也可以在会话中输入 `/context 131072`（仅示例，替换为实际上限），立即更新比例；`/context` 查看当前值。会话修改不写回配置文件。该设置仅用于显示，不更改模型请求或自动压缩历史，`AGENT_MAX_OUTPUT_TOKENS` 仍单独控制输出上限。
