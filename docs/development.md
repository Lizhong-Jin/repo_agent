# Agent Runtime 与工具开发

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 开发环境与验证

在安装目录执行，安装流程见[安装说明](installation.md)：

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check llm tests examples
.venv/bin/ruff format --check llm tests examples
```

默认测试模拟模型响应，不消耗在线推理额度；安装入口测试使用安装目录的 `.venv/bin/repo-agent`，因此应先建立可运行的安装。默认测试通过不代表真实模型、Docker 或 GPU 环境已经联调通过。

```bash
repo-agent-build-sandbox
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox.py tests/test_multilang_sandbox.py
RUN_CUDA_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_gpu_support.py
```

真实 Docker 和 CUDA 测试按环境分别运行；GPU 测试需要已构建的 CUDA 镜像及可用的 Linux NVIDIA 主机。不要在无相应环境时把跳过的测试计为通过。

模块关系见[项目架构](../Project_Architecture.md)。本页说明库调用和工具扩展；用户会话命令见[使用说明](usage.md)。

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

`RunResult` 包含 `status`、`text`（最终正文及其自动续写片段）、`response`、`responses`（各轮响应）、`history`、`steps`、`resumable`、`notice` 和 `stats`（本次任务的模型/工具记录、用量和耗时）。异常或中断时可从 `runtime.last_stats` 查看统计；库调用可以通过 `on_event` 接入自己的日志处理器，或使用 `with Tracer(log_dir) as tracer:` 并传入 `on_event=tracer`。`Tracer` 从 `agent.Tracing` 导入；离开上下文时写入该运行片段汇总并关闭文件。状态含义：

- `completed`：模型正常结束回答；仅表示对话结束，不等于代码通过验证。
- `max_steps`：达到模型调用轮数上限，Runtime 未配置时为 8 轮，0 表示无上限；最后一轮请求的工具仍会执行并记入历史，但不会再调用模型。
- `stopped`：自动恢复耗尽/失败、拦截或其他非正常结束原因；`resumable` 表示是否保留了可以继续的历史。长度截断会先按 `max_recoveries` 尝试恢复，截断回复中的工具调用不执行。

未知工具、工具失败及执行异常会作为错误结果回传，让模型有机会修正。认证、网络等模型调用异常保留为 `LLMError` 交给调用方；单任务 CLI 打印错误并以非零状态退出，交互 CLI 在任务失败后允许继续输入。Runtime 不重复实现 HTTP 重试，也不负责关闭传入的模型客户端。

Runtime 负责同步任务循环、轮数限制和输出截断恢复。CLI 通过 `SavedConversation`、`SessionStore` 和 `SessionCatalog` 管理跨进程保存、恢复、命名与日志，直接使用 Runtime 的库调用方需自行管理持久化。当前没有上下文压缩、并行工具执行或整项任务的 token／费用预算。命令、Python、Git、语言服务器及 Docker 后端各自管理执行超时；Runtime 不统一管理工具超时。`read_file` 的路径范围和读取限制仍由工具自身管理。

## 统一创建工具

`tools/` 顶层只保留导出入口、工厂和具体工具实现；公共组件统一放在 `_internal/`：

```text
tools/
├── __init__.py
├── factory.py
├── execute.py
├── filesystem.py
├── git_tools.py
├── semantic.py
├── web_tools.py          # WebSearchTool、WebFetchTool、create_web_tools
└── _internal/            # 基础类型、错误、文件保护、进程管理、LSP、Web 后端与缓存
```

原有 `from tools import ToolResult, ProcessRunner, ...` 公共导出保持可用。直接使用公共组件的仓库代码改为从 `tools._internal.<模块>` 导入；两个 Web 工具统一从 `tools.web_tools` 导入。

`tools/factory.py` 的 `create_default_tools(workspace_root)` 集中创建指定工作区的工具；CLI 的 local 模式直接使用；Docker/native 模式通过代理在各自的隔离 worker 内使用：

```python
from tools import create_default_tools

runtime = AgentRuntime(client, tools=create_default_tools("."))
```

新增工作区工具时，实现 `Tool` 接口的 `definition` 和 `execute(arguments)`，然后在 `tools/factory.py` 中导入该工具并加入返回列表。工具特有的构造参数也在这里配置，CLI 和 Runtime 无需逐个修改。Runtime 继续负责按工具名称注册和执行，并拒绝重复名称。Web 工具由 `tools/web_tools.py` 的 `create_web_tools()` 单独创建，在主进程追加，不进入默认工厂或 sandbox worker。

每次调用工厂都会创建新的工具实例，避免不同工作区共享工具状态。需要自定义工具组合时，仍可直接向 `AgentRuntime` 传入工具列表。默认组合包含基础环境查询、读取、写入、编辑、多文件严格补丁、列目录、查找文件、内容搜索、创建目录、删除、移动、路径信息和 Git 工具。命令、Python 与语言服务器工具仅在 `isolated_execution=True` 时注册；该开关供已建立隔离环境的受信任调用方使用，本身不创建沙箱。工具类均可从 `tools` 导入。工厂统一创建方式，不改变工具自身行为。

### 文件工具公共组件

`tools/filesystem.py` 保留 11 个文件工具的公开类、参数定义、错误映射和返回协议，公共实现拆分如下：

- `tools/_internal/_workspace.py`：轻量 `WorkspaceTool` 基类，统一工作区根目录及正整数限制校验；内置写操作共用的进程内锁也在此处。
- `tools/_internal/file_policy.py`：`PathPolicy` 保存一次操作使用的保护路径配置，允许复用当前条目的元数据检查硬链接；保留 `is_credential_path()` 供其他工具调用。相对环境配置路径仍按当前工作目录解析。
- `tools/_internal/_file_io.py`：`FileSnapshot` 和 `read_snapshot()` 负责有上限的字节读取，按需计算 SHA-256；严格读取模式检查打开前后文件身份。`StagedWrites` 统一临时文件、同步落盘、权限复制、替换及失败/取消清理。文本编码与换行规则由工具决定。
- `tools/_internal/_file_entries.py`：共享目录条目检查和内容搜索遍历。普通文件的一次元数据查询同时用于类型、大小和硬链接保护；符号链接单独查询目标，保留链接自身的类型信息。

列目录、查找文件和搜索内容在每次调用时创建新的 `PathPolicy`，扫描中复用配置和当前条目的元数据，不跨调用缓存文件状态。搜索仍保留保护目录剪枝、隐藏文件规则和扫描预算；查找文件仍使用原有 glob 语义和精确总数。补丁提交前重新读取字节、校验身份和摘要，不再重复解码、分析换行或拆行；首次替换前的全量检查和每次替换前的检查都保留。

`tests/test_filesystem_components.py` 验证元数据查询次数、保护配置刷新、每文件一次文本解析、提交前摘要检查，以及共享写入组件的取消清理。目录查询次数测试排除 Python 不同版本在路径解析内部执行的查询，不将它等同于全部文件系统调用次数。

### 严格补丁编辑

`apply_patch` 接受 `*** Begin Patch` / `*** Update File: path` / `@@` / `*** End Patch` 格式，只修改已有 UTF-8 文件。上下文必须精确且唯一匹配原始文件；`@@` 后的文字只用于错误提示，不参与定位。新增、删除文件和重命名继续使用对应文件工具。空文件没有可匹配行，使用 `write_file` 写入。重叠 hunk（包括上下文重叠）和混合换行文件会被拒绝。

每次调用先完成全部解析、路径检查和内容准备，再创建临时文件。规范化路径或 `(st_dev, st_ino)` 重复时拒绝整个请求，防止 `a.txt`、`./a.txt` 或大小写别名被当成不同文件。`expected_files` 可校验读取时的 SHA-256，路径须与补丁节的写法一致；即使未提供该参数，提交前也会重新校验文件身份、元数据和摘要。首次替换前检查全部目标，每次替换前再次检查当前目标，变化或无法核验时返回 `FILE_CHANGED`。

同一 Python 进程内的写入、编辑、补丁、创建目录、删除和移动工具共用可重入锁。该锁不覆盖外部编辑器、命令进程和其他 worker；提交前检查与替换之间仍存在很小的竞态窗口，不提供原子比较并替换，也不承诺跨文件事务或崩溃回滚。

准备/提交失败时，`ToolResult.data` 包含 `committed`、`not_committed`、`files_changed`、`failed_path` 和已提交文件的 `changes`（包含修改前后 SHA-256）。这些信息描述本次调用已执行的替换，不保证文件此后不会被其他程序修改。模型应先核对部分结果，再构造新补丁；不要直接重试整份补丁。取消会先清理临时文件再传播异常，但已经提交的文件不会回滚；重启后应重新读取检查。进程被强制杀死或系统崩溃时，Python 的 `finally` 无法保证执行。

补丁保留 UTF-8 BOM 和原有一致的 LF/CRLF/CR 换行风格，按实际内容行定位，不把文件末尾换行解释为额外空行。上下文行的终止符保持不变；删除没有终止符的最后一行，不会同时删除前一行的终止符。替换/追加到无末尾换行的文件时，新末行仍不自动加终止符。匹配使用 KMP，单个 hunk 的行比较次数为 O(文件行数 + 上下文行数)，找到两个匹配即返回歧义，不收集无限候选。

专项回归：`python -m pytest tests/test_apply_patch.py tests/test_file_policy.py -q`，涵盖路径保护、别名、EOF、BOM/权限、并发变化、取消、部分提交和重复行匹配复杂度。

### 复用进程执行逻辑

`tools/_internal/process_runner.py` 的 `ProcessRunner` 负责进程启动、环境变量校验、有限输出缓存、超时和清理；`tools/execute.py` 的 `RunCommandTool` 负责模型参数、工作目录策略和 `ToolResult` 封装。其他工具可直接复用 runner：

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

`run()` 返回独立的 `ProcessResult`，包含退出码、stdout/stderr、超时、清理错误、耗时和截断标志；非零退出码和超时通过结果返回。新增 `status`（`completed` / `timed_out`）、`cleanup_status`（`not_needed` / `confirmed` / `unknown`）、`output_complete`、`pid`、`process_group_id` 和 `cleanup_diagnostics`，将运行状态、清理状态及输出是否完整分开。`completed` 表示已退出，不代表退出码为零。超时或清理失败仍返回已收集的首尾输出，超时、截断或清理失败时 `output_complete=false`。非法调用参数抛出 `ValueError`，操作系统启动失败抛出 `ProcessStartError`，取消会在清理后继续向上传播。可重复使用同一个 runner，每次运行的进程和输出缓存独立。

`ProcessRunner` 是受信任应用代码使用的底层接口，不直接暴露给模型，不应用文件保护策略。`RunCommandTool` / `RunPythonTool` 默认返回 `SANDBOX_REQUIRED`；只有受信任的隔离执行调用方显式启用 `execution_allowed=True`。该参数不在模型工具 schema 中，CLI 不提供本地绕过开关。`create_default_tools()` 默认不注册命令工具；Docker worker 显式启用；native 主进程复用命令工具的参数和路径校验，但将 runner 替换为始终通过平台沙箱（Seatbelt 或 Bubblewrap/seccomp）启动的执行器。这个开关本身不创建隔离环境。

超时覆盖运行和输出收集，不因持续输出自动续期，清理通常额外耗时最多约 3 秒；`cleanup_error` 表示清理未完成或无法确认，Windows 后代进程清理仍为尽力处理。native 使用 `supervise_tree=True`，由 `tools/_internal/process_supervisor.py` 在外层跟踪 PID 与内核启动时间、观察后代并执行 TERM → KILL → 核验，避免依赖沙箱 worker 内部的进程组探测。该选项支持 macOS/Linux，属于采样式监督，不能保证发现采样间迅速脱离的所有后代；不向模型暴露任意 PID 终止接口。native 仅在清理无法确认时暂停执行与写入，正常超时清理成功后继续工作，详见 [原生沙箱](native-sandbox.md#超时结果与恢复)。

Linux 的进程身份来自 `/proc/<pid>/stat`，按字节解析以容忍任意进程名。支持时使用 pidfd 绑定目标，在打开句柄后再次核验身份，再发送 TERM/KILL，最终关闭句柄；缺少 Python API 或内核返回 `ENOSYS` 时回退到 PID 信号。权限拒绝不会触发这个回退。诊断记录 `via=pidfd/pid`，以及 `pidfd_open` / `pidfd_send_signal` 等失败阶段。可移植单元测试位于 `tests/test_linux_process_supervisor.py`，真实 namespace、超时和取消测试位于 `tests/test_linux_native.py`；Linux native 已有独立 PID namespace 作为进程生命周期边界，pidfd 本身不提供资源配额或沙箱隔离。

## 工具错误码

`tools/_internal/errors.py` 集中定义通用的 `ToolErrorCode` 和默认错误提示，使用 `tool_error()` 创建失败的 `ToolResult`。工具通过这一入口返回公共错误。

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

## get_execution_environment 工具

`tools/execute.py` 的 `GetExecutionEnvironmentTool` 已加入默认工具集，local、native 和 Docker 模式均可调用。
模型参数只有可选的 `sections` 数组：

```json
{"sections": ["execution", "system", "runtimes", "gpu"]}
```

传 `{}` 默认返回前三组，GPU 检查需要显式选择。各组含义：

| 分组 | 返回内容 |
| --- | --- |
| `execution` | 执行模式、命令权限、工作目录、网络规则、可写目录、回写方式、资源限制和跨调用状态保留规则 |
| `system` | 实际工具执行环境的 OS、架构和 Python 版本 |
| `runtimes` | 当前 Python，以及 Node、npm、Git、Go、GCC、Clang、CMake、Ninja 的可用状态和版本首行 |
| `gpu` | GPU 名称、显存、计算能力、PyTorch/Triton 状态、PyTorch CUDA 版本、nvcc 信息和 NVIDIA 驱动查询结果 |

Docker 的策略信息由后端通过独立于模型参数的 `execution_context` 传给 worker；报告使用后端固定的镜像 ID 和实际资源配置，不把 Docker 主机的总 CPU/内存当作容器配额。CLI 将实际的 `manual` / `on-success` 回写配置附加到结果；直接使用 `session.tools()` 时未指定回写策略则报告 `unknown`，也可调用 `session.tools(writeback_mode="manual")` 显式提供。查询系统、版本和 GPU 均发生在执行工具的容器内，不会查询 CLI 所在机器来代替容器结果。

native 模式返回实际平台的本机工具链信息及 Seatbelt 或 Bubblewrap/seccomp 策略，不声明容器资源配额；权限和依赖要求见[原生沙箱说明](native-sandbox.md)。

local 模式仅返回基础系统信息、当前 Python 和本地文件操作规则；其他版本及 GPU 返回 `unknown`，不启动子进程。`execution_allowed` 与 `execution_context` 仅允许受信任的应用代码在构造时设置，不能通过模型参数开启隔离执行权限；这些设置本身不创建沙箱。

组件状态区分 `available`、`missing`、`unavailable`、`unknown`，分别表示确认可用、未找到、检查失败或能力不可用、未检查或无法确定。GPU 总状态依据 PyTorch CUDA 的实际可用性；没有 PyTorch 时为 `unknown`，不能由此判断机器没有 GPU。驱动信息、PyTorch 编译时 CUDA 版本及 nvcc toolkit 信息分别保留，不视为同一个版本。`operator_environment_ready` 延续既有算子自检的组合条件，仅供该流程参考。

版本探测每项最多 3 秒，GPU Python 探测最多 30 秒，整次探测共享默认 45 秒预算（子进程超时取整及进程清理可能额外耗时），每个子进程的每路输出限制为 32 KiB。组件缺失、失败、超时和截断以分项状态返回，整体工具仍可成功；进程清理无法确认时中止后续探测并返回 `PROBE_CLEANUP_FAILED`。不会输出环境变量、凭据、完整包清单或原始失败 stderr。每次调用重新探测，不缓存结果。

GPU 部分复用 `sandbox/compute_probe.py`，通过 `python -I` 在独立子进程执行，避免导入工作区中的同名模块。Docker 更新工具后需重建镜像并重启后端；native 需重启 Agent，以更新会话启动时复制的受信任工具实现。

## read_file 工具

```python
from tools import ReadFileTool

reader = ReadFileTool(workspace_root=".")
result = reader.execute({"reads": [
    {"path": "llm/schemas.py", "start_line": 1, "end_line": 40},
    {"path": "tools/_internal/base.py"},
]})
for entry in result.data.get("results", []):
    if entry["success"]:
        print(entry["path"], entry["data"]["content"])
    else:
        print(entry["path"], entry["error"])
```

工具名仍为 `read_file`，参数统一为 `reads` 数组，读取单个文件也传一个数组项；不再接受顶层 `path`、`start_line`、`end_line`。每项独立指定路径和行范围。`path` 相对于初始化时固定的项目根目录，也支持项目范围内的绝对路径。工具读取 UTF-8 文本（支持 BOM），返回带行号的内容；`start_line` 默认 1，`end_line` 包含该行，省略时以文件末尾为目标。

默认每次最多 32 项、每个文件最多 2 MiB、整次调用的带行号正文合计最多 262,144 字符（256 × 1024，不含结果元数据和 JSON 转义开销）。可在构造 `ReadFileTool` 时调整 `max_reads`、`max_file_bytes`、`max_output_chars`；不再使用 `max_lines`。字符额度按请求顺序分配，只返回完整行。某项未读完时，其 `data.truncated=True`，用 `data.next_start_line` 继续读取，并保留原请求的 `end_line`。若第一条请求行也放不下，该项返回 `OUTPUT_TOO_LARGE`；可单独重试该文件，单行超过总上限则需调整应用侧字符上限。显式指定较小行范围可为后续文件预留额度。

`data.results` 与请求逐项对应，保留顺序和重复路径，每项包含 `path`、`success`、`data`，失败项另含 `error.code` 和 `error.message`。成功项包含实际解析后的工作区相对路径、正文、行范围、总行数、续读位置和原始文件字节的 SHA-256。`data.total_output_chars` 为本次返回的正文字符总数。空文件返回成功、空正文和 `total_lines=0`。

读取前先校验整批参数；参数不合法时返回 `INVALID_ARGUMENTS`，不读取任何文件。文件不存在、路径受保护、越界、编码或容量等错误按文件返回，并继续处理其他项；任意项失败时外层 `success=False`、`error_code=READ_FAILED`，已成功的内容仍保留在 `data.results`。因此调用方应检查逐项结果，不能只看外层成功标志。截断但已返回完整行的项算成功，可按续读位置继续获取剩余内容。

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
`tools/_internal/lsp_config.py` 集中维护语言配置；工具根据校验后的真实文件路径选择语言服务器。
它注册到 `create_default_tools(..., isolated_execution=True)`，由 native / Docker 后端提供；local 工具集不启动语言服务器。

| 文件类型 | LSP language_id | 服务器 |
| --- | --- | --- |
| `.py`、`.pyi` | `python` | pylsp |
| `.js`、`.mjs`、`.cjs` | `javascript` | typescript-language-server |
| `.jsx` | `javascriptreact` | typescript-language-server |
| `.ts`、`.mts`、`.cts`（含 `.d.ts`） | `typescript` | typescript-language-server |
| `.tsx` | `typescriptreact` | typescript-language-server |
| `.go` | `go` | gopls |
| `.c`、`.h` | `c` | clangd |
| `.cu`、`.cuh` | `cuda` | clangd |
| `.cpp`、`.cc`、`.cxx`、`.C`、`.hpp`、`.hh`、`.hxx` | `cpp` | clangd |

`get_symbols` 根据文件类型选择服务器，模型无需手动选择：

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

**Docker 安装和验证**

镜像包含 Python/pylsp、Node.js/TypeScript/TypeScript Language Server、Go/gopls、clangd 和 C/C++ 工具链。
Node.js 与 Go 通过独立构建阶段安装；TypeScript 与 TypeScript Language Server 使用
`dependencies/node/package-lock.json` 和 `npm ci` 固定依赖，gopls 的版本由 Dockerfile 构建参数固定。`pip install '.[lsp]'` 只安装 Python 语言服务器，不安装其他运行时。

```bash
repo-agent-build-sandbox
```

standard 镜像构建执行 `python -I -m sandbox.lsp_smoke`，验证八种非 CUDA 语言标识；CUDA 镜像使用 `--cuda`，额外验证 CUDA 符号。
运行中的后端固定镜像 ID；重启 Agent 时按当前配置重新创建后端，恢复工作副本不会恢复旧容器。
完整容器路径的验证命令：

```bash
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_multilang_sandbox.py
```

也可设置 `SANDBOX_LSP_TEST_IMAGE` 验证另一个镜像标签。
服务器缓存均放在 `/tmp`，适配只读根文件系统及非 root 用户；运行期不联网，也不自动下载 Go 工具链或模块。
语言工具链已包含，但项目依赖需预置到受信任的派生镜像：`.venv`、`node_modules` 不会从宿主机复制。
对于复杂工程，需提供正确的 `tsconfig.json`、`go.mod`/`go.work`、`compile_commands.json` 等项目配置；
C/C++ 编译数据库中的路径必须适用于容器内的 `/workspace`，不能直接依赖宿主机绝对路径。

每次语义工具调用创建并关闭自己的客户端，不跨调用复用进程。当前还提供 `go_to_definition`、
`find_references`、`get_diagnostics`、`get_hover` 和 `search_workspace_symbols`；各工具按服务器
能力查询，不意味着每个服务器都支持所有功能。服务器缺失、超时、能力不足、输出过大或
文件在分析期间改变时返回明确错误；工作区符号查询的文档符号回退见下节。


native 复用同一工具与语言注册表，但使用本机工具链、真实工作区路径和每次调用的私有临时目录；
不需要镜像，安装及实际查询验证见[原生沙箱](native-sandbox.md)。

## 引用查询与文件诊断

`find_references` 与 `get_diagnostics` 都注册在隔离执行工具集中，使用同一语言注册表。
引用查询接受 `path`、1 起始的 `line` / UTF-16 `column`，以及布尔值 `include_declaration`
（默认 true）；仅接受 LSP 的 `Location[] | null` 返回格式。结果去重后按路径、位置排序分页。
定义跳转仍支持 `Location` 和 `LocationLink`，不受引用查询的格式约束影响。

本地目标 URI 必须使用绝对文件路径，受保护名称、符号链接原始名称及解析后的目标均会检查。
即使应用在构造工具时允许外部位置，也不会返回受保护文件的位置。`omitted_external_references`
及定义工具的对应字段计入所有被路径策略过滤的位置，包括受保护位置。

`.[lsp]` 依赖包含 Pyflakes，为默认 Python 服务器提供语法错误和基本静态检查；
它不提供完整类型检查。仅安装裸 `python-lsp-server` 而没有诊断插件时，空诊断不能说明
Python 源码有效。已有环境需更新安装 `pip install -e ".[lsp]"`，Docker 环境需重建镜像。

`get_diagnostics` 接受 `path`、`start_index`、`limit`，返回诊断列表、严重程度计数、报告来源和版本。
优先使用 pull diagnostics，否则等待 `publishDiagnostics`；没有收到报告会超时，不作为零诊断成功。
`total_diagnostics == 0` 才表示该报告没有诊断；空分页不能表示整份报告为空。
`freshness_verified` 表示拉取报告或推送版本已对应查询文档，未带版本的推送报告为 false。
它不保证语言服务器完成所有分析，也不保证依赖文件的分析状态；不能代替编译和测试。
严重程度统计覆盖完整的去重报告，不只当前页。关联信息目前只返回数量，不返回原始 `data`。

引用和诊断每次调用都重新查询。分页期间不要修改相关工作区文件；返回的 `sha256` 只代表查询
源文件，不是整个引用集合或工作区快照。确定性排序不能保证变动中的结果集合具有稳定分页。

Python logger `tools.semantic` 在显式启用 DEBUG 时记录服务器 stderr 的末尾最多 8192 字符，
记录发生在服务器关闭之后，也覆盖请求失败的情况；不写入模型工具结果或 worker 标准输出。
日志仅用于排查问题，不根据 stderr 是否非空判断查询成败。

相关回归测试：`tests/test_find_references.py`、`tests/test_get_diagnostics.py`；实际 pylsp
跨文件引用测试在安装了 pylsp 时运行，其缓存目录设在测试临时目录中。

## SearchWorkspaceSymbols：跨文件查找符号

`search_workspace_symbols` 已在隔离执行工具集中注册。参数为 `query`、可选的
`server_id` 和 `limit`；默认最多返回 100 项。同一服务器支持多个语言时只算一个
服务器，例如仅配置 clangd 的 C/C++/CUDA 时可以省略 `server_id`。同一服务器 ID
必须使用相同启动命令；不同语言的超时取最大值。结果用 `language_ids` 表示支持的
语言集合，不把跨语言搜索标成某一种语言。

工具在创建客户端前检查 `execution_allowed`。文件 URI 同时检查原始路径和解析后的
真实路径，保护规则也适用于允许返回的外部位置；被过滤的受保护结果计入
`omitted_protected_symbols`。

每次沙箱调用仍使用独立会话。服务器支持文档符号时，工具先在本次会话内同步并查询
工作区源文件，以便 clangd 等服务器建立已打开文件的索引，再执行 `workspace/symbol`。
原生搜索保持服务器返回顺序。服务器不支持工作区搜索、但支持文档符号时（如默认
pylsp），使用逐文件符号查询回退，对符号名称进行不区分大小写的子串匹配，保留遍历
顺序；`search_mode` 为 `document_symbols`，否则为 `workspace`。

扫描默认限制为 64 个候选源文件、5,000 个目录项、累计 8 MiB 源码、单文件 2 MiB；
可在构造工具时通过 `max_workspace_files`、`max_workspace_entries`、
`max_workspace_bytes`、`max_file_bytes` 调整。扫描跳过受保护路径、目录符号链接、
`.venv`、`venv`、`node_modules`、`__pycache__`、`build`、`dist`、`target`。
回退最多累计 10,000 个匹配符号；`.gitignore` 不决定扫描权限。
初始化后的扫描、分析、搜索和延迟解析共用剩余时间预算，单个请求不重新获得完整超时。
文件变化或无法分析时计入 `coverage.skipped_files`；达到扫描上限时
`coverage.scan_truncated=true`。这些限制不等于整个项目已经完成索引：原生结果的
`coverage.index_completeness` 为 `unknown`，回退结果为 `scanned_files_only`。
空结果只能描述服务器索引或已扫描范围，不能证明整个仓库不存在该符号。

支持 `workspaceSymbol/resolve` 的服务器可以返回没有范围的符号。工具只为当前
`limit` 范围内的候选执行解析，原样传回服务器的 opaque `data`，解析后重新校验路径。
单个解析被服务器拒绝时保留 `location_resolved=false`；协议、传输或超时错误仍返回
对应工具错误。`unresolved_locations` 是过滤、去重后所有候选中的未解析数量，可能
包含未返回的候选。输出位置使用从 1 开始的行号和 UTF-16 列号，结束位置不包含在范围内。

输出超过 JSON 字符预算时自动减少返回数量；单个符号名称或路径过长时省略该项，计入
`omitted_oversized_symbols`，不改写标识符或路径。`total_symbols` 表示本次服务器响应
或扫描结果经过过滤、去重后的数量，不是全仓库符号总数；`truncated` 表示候选结果未
全部返回，与扫描范围的 `coverage.scan_truncated` 分开。仅必要元数据也无法容纳时
返回 `OUTPUT_TOO_LARGE`。结果跨独立会话不保证排序稳定，因此不提供跨调用分页。

## 构建独立发行版与更新依赖

维护者需要完整 Python 3.11+ 和 uv。普通用户运行发行包安装器无需 uv。

```bash
# 重新解析并更新 Python 版本及带哈希的 pip 清单（默认执行升级）
python3 scripts/lock_dependencies.py
# 仅检查 pyproject、uv.lock 与三个 pip 清单是否一致
python3 scripts/lock_dependencies.py --check
# 修改 dependencies/node/package.json 后更新 npm 锁文件
npm install --package-lock-only --ignore-scripts --prefix dependencies/node
# 构建并校验发行包；可用 --uv 指定 uv 的完整路径
python3 scripts/build_release.py
```

每次发布先更新 `pyproject.toml` 的版本号并重新生成锁文件。构建前检查 Python 锁文件一致性；构建器使用临时隔离环境和固定构建依赖，输出 `dist/repo_agent-<版本>-py3-none-any.whl`、`dist/repo-agent-<版本>.tar.gz` 及其 `.sha256`。不会上传或发布。

发行包通过明确的文件清单收集源码、默认模板和资源，不复制 `.venv`、`.git`、项目 `.env`、日志或下载缓存。wheel 包含用于独立重建 Docker 镜像的源码资源压缩包。`release.json` 记录版本、wheel 和各文件 SHA256；独立安装先校验，再写入用户版本目录。安装器不搬迁已创建的虚拟环境，以免破坏入口脚本中的绝对路径。

运行单元测试后，可用构建产物执行真实安装验收。该测试联网下载依赖，在临时用户目录安装，删除下载目录后检查启动、配置恢复、doctor、镜像构建上下文和卸载；镜像入口使用模拟 Docker，不构建真实镜像、不调用模型 API：

```bash
REPO_AGENT_TEST_ARCHIVE="$PWD/dist/repo-agent-0.1.0.tar.gz" \
  python3 -m pytest -q tests/test_release_distribution.py
```
