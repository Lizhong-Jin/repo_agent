# Agent Runtime 与工具开发

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 运行最小 Agent

配置好对应厂商的 API Key 和模型 ID 后，在项目根目录运行：

```bash
source .venv/bin/activate
export DEEPSEEK_API_KEY='你的 API Key'
export LLM_MODEL='你的模型 ID'
python -m cli.main '读取 llm/schemas.py，解释统一消息结构' --provider deepseek --root .
```

也可以设置 `LLM_PROVIDER`、`LLM_BASE_URL`，或者使用 `--model`、`--base-url` 显式指定。CLI 自动加载用户配置和工作目录的 `.env`。安装项目后，同样可以运行 `repo-agent '你的任务'`。

Python 调用：

```python
from agent import AgentRuntime
from llm import LLMClient, LLMConfig
from tools import ReadFileTool

with LLMClient(LLMConfig("deepseek", "你的模型 ID")) as client:
    runtime = AgentRuntime(client, tools=[ReadFileTool(".")], max_steps=8)
    result = runtime.run("读取 README.md，概括已实现的功能")
    print(result.status, result.steps)
    print(result.text)
```

运行流程：用户任务 → 模型回复 → 按名称查找工具 → 顺序执行工具 → 回传结果 → 再次调用模型。每次调用保留完整历史及厂商状态；一轮返回多个工具调用时，按原顺序执行和回传。默认 `run()` 开启独立任务；传入 `runtime.run("后续任务", history=previous_result.history)` 可延续上下文，输入历史不会被修改。交互 CLI 自动管理这份历史。

`RunResult` 包含 `status`、`text`（最后一条模型正文）、`response`、`history`、`steps` 和 `stats`（本次任务的模型/工具记录、用量和耗时）。异常或中断时可从 `runtime.last_stats` 查看统计；库调用可以通过 `on_event` 接入自己的日志处理器，或使用 `with Tracer(log_dir) as tracer:` 并传入 `on_event=tracer`。`Tracer` 从 `agent.Tracing` 导入；离开上下文时写入会话汇总并关闭文件。状态含义：

- `completed`：模型正常结束回答；仅表示对话结束，不等于代码通过验证。
- `max_steps`：达到模型调用轮数上限，默认 8 轮；最后一轮请求的工具仍会执行并记入历史，但不会再调用模型。
- `stopped`：模型返回长度截断、拦截或其他非正常结束原因；不执行该回复中的工具调用。

未知工具、工具失败及执行异常会作为错误结果回传，让模型有机会修正。认证、网络等模型调用异常保留为 `LLMError` 交给调用方；单任务 CLI 打印错误并以非零状态退出，交互 CLI 在任务失败后允许继续输入。Runtime 不重复实现 HTTP 重试，也不负责关闭传入的模型客户端。

Runtime 当前实现同步、内存中的基础循环和轮数上限，尚无上下文压缩、任务恢复、并行工具执行或整个任务的 token／费用预算。命令、Python、Git、语言服务器及 Docker 后端各自管理执行超时；Runtime 不统一管理工具超时。`read_file` 的路径范围和读取限制仍由工具自身管理。

## 统一创建工具

`tools/factory.py` 的 `create_default_tools(workspace_root)` 集中创建指定工作区的工具；CLI 的 local 模式直接使用，Docker 模式通过会话代理在容器内使用：

```python
from tools import create_default_tools

runtime = AgentRuntime(client, tools=create_default_tools("."))
```

新增工具时，实现 `Tool` 接口的 `definition` 和 `execute(arguments)`，然后在 `tools/factory.py` 中导入该工具并加入返回列表。工具特有的构造参数也在这里配置，CLI 和 Runtime 无需逐个修改。Runtime 继续负责按工具名称注册和执行，并拒绝重复名称。

每次调用工厂都会创建新的工具实例，避免不同工作区共享工具状态。需要自定义工具组合时，仍可直接向 `AgentRuntime` 传入工具列表。默认组合包含读取、写入、编辑、批量编辑、列目录、查找文件、内容搜索、创建目录、删除、移动、路径信息和 Git 工具。命令、Python 与代码符号工具仅在 `isolated_execution=True` 时注册；该开关供已建立隔离环境的受信任调用方使用，本身不创建沙箱。工具类均可从 `tools` 导入。工厂统一创建方式，不改变工具自身行为。

### 复用进程执行逻辑

`tools/process_runner.py` 的 `ProcessRunner` 负责进程启动、环境变量校验、有限输出缓存、超时和清理；`tools/execute.py` 的 `RunCommandTool` 负责模型参数、工作目录策略和 `ToolResult` 封装。其他工具可直接复用 runner：

```python
import sys
from pathlib import Path
from tools import ProcessRunner, ProcessStartError

runner = ProcessRunner(max_output_bytes=32 * 1024)
try:
    result = runner.run(
        [sys.executable, "-c", "print('hello')"],
        cwd=Path(".").resolve(),
        timeout_seconds=30,
    )
except ProcessStartError as error:
    print(f"启动失败：{type(error.cause).__name__}")
else:
    print(result.exit_code, result.stdout, result.timed_out)
```

`run()` 返回独立的 `ProcessResult`，包含退出码、stdout/stderr、超时、清理错误、耗时和截断标志；非零退出码和超时通过结果返回。非法调用参数抛出 `ValueError`，操作系统启动失败抛出 `ProcessStartError`，取消会在清理后继续向上传播。可重复使用同一个 runner，每次运行的进程和输出缓存独立。

`ProcessRunner` 是受信任应用代码使用的底层接口，不直接暴露给模型，不应用文件保护策略。`RunCommandTool` / `RunPythonTool` 默认返回 `SANDBOX_REQUIRED`；只有受信任的隔离执行调用方显式启用 `execution_allowed=True`。该参数不在模型工具 schema 中，CLI 不提供本地绕过开关。`create_default_tools()` 默认不注册命令工具；Docker worker 显式启用，宿主机只创建代理定义，不执行原始命令工具。这个开关本身不创建隔离环境。

超时覆盖运行和输出收集，清理可能额外耗时最多约 3 秒；`cleanup_error` 表示清理未完成或无法确认，Windows 后代进程清理仍为尽力处理。

## 工具错误码

`tools/errors.py` 集中定义通用的 `ToolErrorCode` 和默认错误提示，使用 `tool_error()` 创建失败的 `ToolResult`。各工具不再重复实现 `_error()`。

```python
from tools import ToolErrorCode, tool_error

# 通用错误，使用统一提示
return tool_error(ToolErrorCode.FILE_NOT_FOUND)

# 参数、容量限制等需要具体说明时，可覆盖默认提示
return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "start_line must be positive.")

# 工具特有错误保留在工具中，并明确提供提示
return tool_error("NO_MATCH", "old_text does not occur in the file.")
```

通用类别包括参数校验、路径越界/保护、权限、不存在、文件/目录类型、符号链接、已有路径、编码、容量限制和基础读写失败。现有错误码字符串及 `ToolResult.to_message()` 的返回结构保持不变；例如 `PARENT_NOT_DIRECTORY`、`FILE_EXISTS` 与 `PATH_ALREADY_EXISTS` 继续保留原码，便于兼容既有调用方和日志。

`LINE_OUT_OF_RANGE`、`NO_CHANGES`、`NO_MATCH`、`MULTIPLE_MATCHES`、`EDIT_RESULT_TOO_LARGE`、`SEARCH_ERROR`、`CREATE_DIRECTORY_ERROR` 和 `DELETE_FILE_ERROR` 等仍由相应工具定义。工具特有错误必须提供说明，不需要加入公共枚举。新增工具可直接复用上述公共入口。

## read_file 工具

```python
from tools import ReadFileTool

reader = ReadFileTool(workspace_root=".")
result = reader.execute({"path": "llm/schemas.py", "start_line": 1, "end_line": 40})
if result.success:
    print(result.data["content"])
else:
    print(result.error_code, result.error)
```

`path` 相对于初始化时固定的项目根目录，也支持项目范围内的绝对路径。工具读取 UTF-8 文本（支持 BOM），返回带行号的内容；`start_line` 默认 1，`end_line` 包含该行，省略时以文件末尾为目标。默认每次最多 200 行，超出时 `truncated=True`，可用 `next_start_line` 继续读取。

默认文件上限 2 MiB、返回正文上限 20,000 字符；超限返回错误，不静默丢弃字符。可在构造 `ReadFileTool` 时调整 `max_lines`、`max_file_bytes`、`max_output_chars`。空文件返回成功、空正文和 `total_lines=0`；不存在的文件、越界行号、目录、二进制及不支持的编码返回结构化错误。

接入统一模型接口时，传入 `reader.definition`，收到工具调用后执行并回传：

```python
from llm import LLMClient, LLMConfig, LLMRequest, Message
from tools import ReadFileTool

reader = ReadFileTool(".")
history = [Message("user", "读取 README.md 前 20 行，说明项目实现了什么。")]
with LLMClient(LLMConfig("deepseek", "你的模型 ID")) as client:
    response = client.generate(LLMRequest(history, tools=[reader.definition]))
    if response.finish_reason == "tool_calls":
        history.append(response.to_message())
        for call in response.tool_calls:
            if call.name == reader.definition.name:
                history.append(reader.execute(call.arguments).to_message(call))
            else:
                history.append(Message.tool_result(call, "Unknown tool", is_error=True))
        followup = client.generate(LLMRequest(history, tools=[reader.definition]))
        print(followup.text)
```

工具会解析路径并拒绝 `..` 或符号链接导致的项目范围外访问；这属于路径检查，不是操作系统沙箱，也不提供抵御并发恶意路径替换的隔离保证。受保护名称和配置／日志路径不可读取，详见[文件保护边界](logging.md#文件保护边界)；`.gitignore` 不决定读取权限。

## GetSymbols：读取多语言代码符号

`tools/semantic.py` 提供 `GetSymbolsTool`，模型调用名为 `get_symbols`。
`tools/lsp_config.py` 集中维护语言配置；工具根据校验后的真实文件路径选择语言服务器。
它注册到 `create_default_tools(..., isolated_execution=True)`；普通宿主机工具集不启动服务器。

| 文件类型 | LSP language_id | 服务器 |
| --- | --- | --- |
| `.py`、`.pyi` | `python` | pylsp |
| `.js`、`.mjs`、`.cjs` | `javascript` | typescript-language-server |
| `.jsx` | `javascriptreact` | typescript-language-server |
| `.ts`、`.mts`、`.cts`（含 `.d.ts`） | `typescript` | typescript-language-server |
| `.tsx` | `typescriptreact` | typescript-language-server |
| `.go` | `go` | gopls |
| `.c`、`.h` | `c` | clangd |
| `.cpp`、`.cc`、`.cxx`、`.C`、`.hpp`、`.hh`、`.hxx` | `cpp` | clangd |

只注册一个工具，模型无需选择服务器：

```json
{"path": "src/App.tsx", "limit": 50, "start_index": 0}
```

结果包含 `language_id`、`server_id`、`symbols`、`total_symbols`、`next_start_index`、
`truncated` 和源文件 `sha256`。位置使用 **1-based 行号与 UTF-16 列号**，范围终点不包含在范围内。
树形结果按前序展开，`parent_index` 指向完整列表中的父符号索引；平面结果只保留
`container_name`，不推断嵌套关系。分页期间应保持文件不变，可用 `sha256` 比较内容版本。

**选择规则与扩展**

- 最长后缀优先；同长度时优先精确大小写匹配，因此 `.c` 是 C、`.C` 是 C++。
  没有精确匹配时可匹配小写配置（例如 `.PY` → Python）。未知后缀明确返回 `UNSUPPORTED_LANGUAGE`。
- `.h` 默认归 C；C++ 项目需要把 `.h` 从 C 配置移至 C++ 配置。编译数据库仍决定 clangd 的实际编译参数。
- `LspLanguageConfig` 保存服务器标识、语言标识、扩展名、启动参数和超时；`LspRegistry` 校验重复扩展名，
  并复制为不可变配置。默认超时为 20 秒，Go 为 30 秒，均为客户端单个请求的超时。
- 程序可以向 `GetSymbolsTool` 或 `create_default_tools` 传入 `lsp_registry`，替换默认配置。
  添加语言需同时安装服务器及运行时；配置表中登记名称本身不会安装软件。
- 原有 `command`、`language_id`、`file_extensions` 构造参数仍支持单语言模式，不能与 `lsp_registry` 混用。
  命令不能通过模型参数或工作区文件注入。
- Docker 工作进程使用镜像内的默认配置。自定义注册表只影响直接构造的工具，
  不会自动跨容器传递；要修改 Docker 默认支持范围，应更新镜像中的配置表并重新构建。

**沙箱安装和验证**

镜像包含 Python/pylsp、Node.js/TypeScript/TypeScript Language Server、Go/gopls、clangd 和 C/C++ 工具链。
Node.js 与 Go 通过独立构建阶段安装；TypeScript、TypeScript Language Server、gopls 的版本在
Dockerfile 中以构建参数固定。`pip install '.[lsp]'` 只安装 Python 语言服务器，不安装其他运行时。

```bash
./run_agent.sh --build-sandbox
```

构建会执行 `python -I -m sandbox.lsp_smoke`，对表中的八种语言标识进行真实符号查询。
构建完成后创建新的沙箱会话；已有会话绑定旧镜像 ID，不会自动切换。
完整容器路径的验证命令：

```bash
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/pytest -q tests/test_multilang_sandbox.py
```

也可设置 `SANDBOX_LSP_TEST_IMAGE` 验证另一个镜像标签。
服务器缓存均放在 `/tmp`，适配只读根文件系统及非 root 用户；运行期不联网，也不自动下载 Go 工具链或模块。
语言工具链已包含，但项目依赖需预置到受信任的派生镜像：`.venv`、`node_modules` 不会从宿主机复制。
对于复杂工程，需提供正确的 `tsconfig.json`、`go.mod`/`go.work`、`compile_commands.json` 等项目配置；
C/C++ 编译数据库中的路径必须适用于容器内的 `/workspace`，不能直接依赖宿主机绝对路径。

目前每次调用仍创建并关闭一个客户端，适配单请求沙箱工作进程；尚不支持跨调用进程复用，
也未提供定义跳转、引用查找或诊断的 Agent 工具封装。服务器缺失、超时、能力不足、输出过大或
文件在分析期间改变时返回明确错误，不回退成文本搜索。
