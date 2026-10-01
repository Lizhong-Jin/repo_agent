# 工具开发参考

[文档首页](index.md) · [Runtime 与验证](development.md) · [项目架构](../Project_Architecture.md)

面向扩展工具或维护执行协议的开发者。以下命令默认在已安装开发依赖的源码目录运行；示例中的 `client` 需由调用方配置。

- [统一创建工具](#统一创建工具)
- [按需加载工具组](#按需加载工具组)
- [工具错误码](#工具错误码)
- [run_shell 工具与平台行为](#run_shell-工具与平台行为)
- [get_execution_environment 工具](#get_execution_environment-工具)
- [read_file 工具](#read_file-工具)
- [search_files 工具](#search_files-工具)
- [GetSymbols：读取多语言代码符号](#getsymbols读取多语言代码符号)
- [引用查询与文件诊断](#引用查询与文件诊断)
- [SearchWorkspaceSymbols：跨文件查找符号](#searchworkspacesymbols跨文件查找符号)

## 统一创建工具

`tools/` 顶层只保留导出入口、工厂和具体工具实现；公共组件统一放在 `_internal/`：

```text
tools/
├── __init__.py
├── factory.py
├── dispatch.py           # 按执行类别调度，检查隔离执行入口
├── tool_groups.py        # 集中分组目录、load_tool_group 与会话可见性状态
├── execute.py
├── filesystem.py
├── git_tools.py
├── semantic.py
├── web_tools.py          # WebSearchTool、WebFetchTool、create_web_tools
└── _internal/            # 基础类型、错误、文件保护、进程管理、LSP、Web 后端与缓存
```

原有 `from tools import ToolResult, ProcessRunner, ...` 公共导出保持可用。直接使用公共组件的仓库代码改为从 `tools._internal.<模块>` 导入；两个 Web 工具统一从 `tools.web_tools` 导入。

`tools/factory.py` 的 `create_default_tools(workspace_root)` 集中创建指定工作区的工具；CLI 的 local 模式直接使用，Docker 通过代理在容器内执行。native 将内置文件工具交给轻量文件服务，命令、Git、Python、环境探测及 LSP 继续通过操作系统隔离执行，见[原生沙箱说明](native-sandbox.md)：

```python
from agent import AgentRuntime
from tools import DEFAULT_TOOL_GROUPS, create_default_tools

# client 是已配置的同步 LLM 客户端；默认只创建 local 工具，不建立沙箱。
runtime = AgentRuntime(
    client, tools=create_default_tools("."), tool_groups=DEFAULT_TOOL_GROUPS,
)
```

新增工作区工具时，实现 `Tool` 接口的 `definition` 和 `execute(arguments)`，在具体类中显式声明 `execution_kind`，然后在 `tools/factory.py` 中导入该工具并加入返回列表。纯文件工具加入 `create_file_tools()`；工具特有的构造参数也在工厂配置。Runtime 按工具名称注册，由统一调度器执行，并拒绝重复名称和缺失/无效的调度规则。Web 工具由 `tools/web_tools.py` 的 `create_web_tools()` 单独创建，在主进程追加，不进入默认工厂或 sandbox worker。

每次调用工厂都会创建新的工具实例，避免不同工作区共享工具状态。需要自定义工具组合时，仍可直接向 `AgentRuntime` 传入工具列表。默认组合包含基础环境查询、读取、写入、编辑、多文件严格补丁、列目录、查找文件、内容搜索、创建目录、删除、移动、路径信息和 Git 工具。命令、Python 与语言服务器工具仅在 `isolated_execution=True` 时注册；该开关供已建立隔离环境的受信任调用方使用，本身不创建沙箱。工具类均可从 `tools` 导入。工厂统一创建方式，不改变工具自身行为。

## 工具执行调度

`ExecutionKind` 和 `ToolDispatcher` 可从 `tools` 导入。调度规则属于受信任代码的内部元数据，不加入模型工具参数；工具组只控制可见性，不改变调度或授权。

| 规则 | 当前工具 | 执行位置 |
| --- | --- | --- |
| `HOST_CONTROL` | 技能加载、工具组加载、历史搜索/读取 | 主进程内的受控状态/元数据操作 |
| `TRUSTED_FILE` | 11 个文件工具 | native 轻量文件服务；Docker 工作副本中的隔离工具；local 文件实现 |
| `TRUSTED_NETWORK` | `web_search`、`web_fetch` | 主进程中的受控网络后端 |
| `SANDBOXED_PROCESS` | 命令、Python、Git、LSP、执行环境查询 | native/Docker 代理；已建立隔离的 worker 内才执行原始实现 |

local 兼容例外只允许**确切的内置类**且 `execution_allowed=False`：Git 保留已有外部过滤器检查，执行环境查询只返回不启动进程的基础信息。其他原始进程工具即使设置了 `execution_allowed=True`，交给普通 `AgentRuntime` 仍返回 `SANDBOX_REQUIRED`；该属性本身不会建立隔离。

每个具体工具类必须自行声明规则，新增子类也不能仅继承父类声明：

```python
from tools import ExecutionKind, ToolResult

class MyControlTool:
    execution_kind = ExecutionKind.HOST_CONTROL

    # definition 按既有 Tool 接口提供。
    def execute(self, arguments):
        return ToolResult(True, {"status": "ready"})
```

工厂、Runtime 注册和 sandbox worker 会检查声明。缺失声明、字符串/布尔值等无效类型会立即报错；调用时再检查规则是否与注册时一致。代理必须显式接收并保留原工具规则，不能使用默认规则掩盖遗漏；未知 native 工具在扫描和启动进程前被拒绝。沙箱 worker 拒绝注册主进程控制/网络工具。

只有受信任的应用代码可以注册工具。调度器不会通过静态分析证明 `HOST_CONTROL` 或 `TRUSTED_FILE` 的实现没有执行代码；第三方 Python 插件不能仅凭自我声明被当作可信实现。文件工具不得启动项目脚本、命令或 LSP；需要这些能力时使用隔离执行接口。`ToolDispatcher(inside_sandbox=True)` 仅供操作系统隔离已经建立的 worker 使用，禁止在普通主进程中把它当作绕过开关。

## 按需加载工具组

CLI 默认开启按需加载。工厂和 sandbox worker 仍提供当前执行环境允许的完整工具集合，Runtime 单独管理“哪些定义发给模型、哪些调用现在允许执行”。加载器在宿主 Runtime 中运行；加载后的文件、Git、命令及语义工具仍通过原有 local/native/Docker 工具实例或代理执行，不改变隔离边界。

### 当前划分

| 类别 | 工具 | 启用时机 |
| --- | --- | --- |
| 通用工作区工具 | `get_execution_environment`、`read_file`、`list_files`、`find_files`、`search_files`、`get_path_info` | 初始可见 |
| 通用辅助能力 | `load_tool_group`、已注册的技能加载、历史搜索/读取、Web 搜索/读取 | 初始可见；Web 仍取决于配置 |
| `file_editing` | `write_file`、`edit_file`、`apply_patch`、`make_directory`、`delete_file`、`move_file` | 按需加载；也适用于非代码文件修改 |
| `coding` | `git_status`、`git_diff`、`run_command`、`run_shell`、`run_python`、`get_symbols`、`go_to_definition`、`find_references`、`get_diagnostics`、`get_hover`、`search_workspace_symbols` | 按需加载；local 只提供其中的 Git 工具 |

标准 CLI 初始发送 10 个工具定义，配置 Web 后至多 12 个。`load_tool_group` 的描述包含组目录、用途和当前环境可用的工具名称，不预先塞入专用工具的完整参数定义。

模型调用示例：

```json
{"group": "file_editing"}
```

加载返回组名、可用/不可用工具和 `already_loaded`，不执行任何组内操作。下一轮模型请求才携带新增定义；同一回复中先加载、紧接着调用新工具仍返回 `TOOL_NOT_LOADED`。未知组返回 `UNKNOWN_TOOL_GROUP`，完全不可用的组返回 `TOOL_GROUP_UNAVAILABLE`。组内部分工具未注册时只启用当前可用成员；不能通过加载恢复宿主未授权或未配置的能力。

原来的 `git`、`code_execution`、`code_intelligence` 已合并为 `coding`，旧组名不再有效。local 加载或恢复 `coding` 会保留 Git 能力，但不会获得命令、Python 或语言服务权限。

新增定义按组加载顺序追加，保持已有工具定义的前缀顺序。上下文估算和自动压缩使用当前实际可见的工具集合。加载状态属于会话，在用户连续任务之间保留；保存时只记录组名，恢复时用当前目录及执行环境重新计算成员，移除未知/完全不可用的组并提示。历史压缩不清除加载状态；`/clear`、新建会话会重置。旧会话没有该字段时从初始集合开始，不能凭历史里的成功调用自动恢复工具权限。

### 新增、移动和自定义组

默认分类只维护在 `tools/tool_groups.py` 的 `DEFAULT_TOOL_GROUPS` 中：

1. 把工具从一个 `ToolGroup.tools` 元组移到另一个元组即可改变分类，执行代码和工厂不需要跟着改。
2. 从所有组中移除某工具名后，它就成为初始可见工具。未分类的宿主扩展也默认初始可见。
3. 增加新的 `ToolGroup` 条目即可增加分组。工具本身仍需通过相应工厂/宿主入口注册；列入分组不会自动安装依赖、建立连接或授予权限。

```python
from tools import DEFAULT_TOOL_GROUPS, ToolGroup

groups = (*DEFAULT_TOOL_GROUPS,
          ToolGroup("literature", "Search papers and inspect bibliographic records.",
                    ("search_papers", "read_paper")))
runtime = AgentRuntime(client, tools=registered_tools, tool_groups=groups)
```

也可以完全替换分组序列。组名或工具归属重复会在 Runtime 初始化时失败；一个工具只能属于一个专用组，`load_tool_group` 不能被放入专用组。每个 Runtime 都创建自己的可见性状态，不修改全局目录。

库调用为了兼容现有应用，省略 `tool_groups` 时保持原来的全量可见行为；传入 `DEFAULT_TOOL_GROUPS` 即与 CLI 一致。独立使用 Runtime 时可调用 `reset_tool_groups()` 重置，或用 `restore_tool_groups(names)` 恢复并获取未能恢复的组名。这里按需加载的是模型定义及调用资格，工具对象仍按原有工厂创建；没有增加自动任务分类、自动启动语言服务器或卸载工具接口。

### Git 工具的执行边界

`git_diff` 和 `git_status` 读取工作树时都可能触发 Git 的内容过滤器。local 模式在实际 diff/status 前读取包含 `include`、`includeIf` 和 worktree 配置的过滤器设置；存在非空 `filter.*.clean` 或 `filter.*.process` 时返回 `GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX`。即使驱动暂未被属性选中、请求只看 staged，也保守拒绝；不会静默跳过转换后返回可能失真的差异。配置读取失败、截断、格式异常或清理未确认时拒绝继续；超时单独返回 `GIT_TIMEOUT`，其他核验失败返回 `GIT_CONFIG_CHECK_FAILED`。错误不回显过滤器命令。

`GitDiffTool` / `GitStatusTool` 的 `execution_allowed` 仅由受信任代码构造时设置，工厂将其连接到 `isolated_execution`。native/Docker worker 已建立隔离，允许过滤器继承沙箱权限；local 默认关闭。模型参数不能开启此权限，也不会自动切换执行模式。Git 工具固定忽略子模块工作树脏状态，diff 使用短格式展示子模块提交变化，避免递归调用未检查的子仓库过滤器；需要子模块内部差异时，使用 `cwd` 明确选择该仓库并重新检查。

这是配置前置拒绝机制，不是 local 模式的 OS 沙箱。diff 的路径枚举和内容生成前分别重新检查，但检查与执行之间仍存在宿主并发修改配置的窗口；需要对抗不可信并发修改时使用 native/Docker。隔离模式仍使用现有工作区可写策略，并非专用只读 Git 沙箱。

### 文件工具公共组件

`tools/filesystem.py` 保留 11 个文件工具的公开类、参数定义、错误映射和返回协议，公共实现拆分如下：

- `tools/_internal/_workspace.py`：轻量 `WorkspaceTool` 基类，统一工作区根目录及正整数限制校验；内置写操作共用的进程内锁也在此处。
- `tools/_internal/file_policy.py`：`PathPolicy` 保存一次操作使用的保护路径配置，允许复用当前条目的元数据检查硬链接；保留 `is_credential_path()` 供其他工具调用。相对环境配置路径仍按当前工作目录解析。
- `host_support/filesystem.py`：提供不跟随链接的描述符操作和 native 轻量文件服务；`tools/_internal/file_access.py` 为它注入工作区 `PathPolicy` 与只读目录约束。底层机制不替代工具层授权。
- `tools/_internal/_file_io.py`：`FileSnapshot` 和 `read_snapshot()` 负责有上限的字节读取，按需计算 SHA-256；`snapshot_stat()` 与后续读取使用同一文件服务的元数据语义，避免 Windows 路径/句柄时间含义不同而误判。严格读取模式检查打开前后文件身份。`StagedWrites` 统一临时文件、同步落盘、权限复制、替换及失败/取消清理。文本编码与换行规则由工具决定。
- `tools/_internal/_file_entries.py`：共享目录条目检查和内容搜索遍历。普通文件的一次元数据查询同时用于类型、大小和硬链接保护；符号链接单独查询目标，保留链接自身的类型信息。共享文件服务中的搜索候选借用当前 `DirectoryReader`，在目录作用域内复用父目录句柄；预算耗尽和异常退出会关闭作用域。
- `host_support/file_scan.py`、`host_support/path_rules.py`：目录读取协议和名称匹配机制；实际保护名单继续由工具策略维护。目录身份固定不代表内容是原子快照，读取仍须校验实际打开的文件。
- `tools/_internal/text_search.py`：对已完整解码、受大小限制的内容惰性分行，只将 CRLF、CR、LF 作为换行；不额外构造完整行列表，其他 Unicode 分隔符留在原行。文本搜索不调用 Rust 扩展或外部搜索程序。

列目录、查找文件和搜索内容在每次调用时创建新的 `PathPolicy`，扫描中复用配置和当前条目的元数据，不跨调用缓存文件状态。搜索仍保留保护目录剪枝、隐藏文件规则和扫描预算；查找文件仍使用原有 glob 语义和精确总数。补丁提交前重新读取字节、校验身份和摘要，不再重复解码、分析换行或拆行；首次替换前的全量检查和每次替换前的检查都保留。

`tests/test_filesystem_components.py` 验证元数据查询次数、保护配置刷新、每文件一次文本解析、提交前摘要检查，以及共享写入组件的取消清理。目录查询次数测试排除 Python 不同版本在路径解析内部执行的查询，不将它等同于全部文件系统调用次数。

### 严格补丁编辑

`apply_patch` 接受 `*** Begin Patch` / `*** Update File: path` / `@@` / `*** End Patch` 格式，只修改已有 UTF-8 文件。上下文必须精确且唯一匹配原始文件；`@@` 后的文字只用于错误提示，不参与定位。新增、删除文件和重命名继续使用对应文件工具。空文件没有可匹配行，使用 `write_file` 写入。重叠 hunk（包括上下文重叠）和混合换行文件会被拒绝。

每次调用先完成全部解析、路径检查和内容准备，再创建临时文件。规范化路径或 `(st_dev, st_ino)` 重复时拒绝整个请求，防止 `a.txt`、`./a.txt` 或大小写别名被当成不同文件。`expected_files` 可校验读取时的 SHA-256，路径须与补丁节的写法一致；即使未提供该参数，提交前也会重新校验文件身份、元数据和摘要。首次替换前检查全部目标，每次替换前再次检查当前目标，变化或无法核验时返回 `FILE_CHANGED`。

同一 Python 进程内的写入、编辑、补丁、创建目录、删除和移动工具共用可重入锁。该锁不覆盖外部编辑器、命令进程和其他 worker；提交前检查与替换之间仍存在很小的竞态窗口，不提供原子比较并替换，也不承诺跨文件事务或崩溃回滚。

准备/提交失败时，`ToolResult.data` 包含 `committed`、`not_committed`、`files_changed`、`failed_path` 和已提交文件的 `changes`（包含修改前后 SHA-256）。这些信息描述本次调用已执行的替换，不保证文件此后不会被其他程序修改。模型应先核对部分结果，再构造新补丁；不要直接重试整份补丁。取消会先清理临时文件再传播异常，但已经提交的文件不会回滚；重启后应重新读取检查。进程被强制杀死或系统崩溃时，Python 的 `finally` 无法保证执行。

补丁保留 UTF-8 BOM 和原有一致的 LF/CRLF/CR 换行风格，按实际内容行定位，不把文件末尾换行解释为额外空行。上下文行的终止符保持不变；删除没有终止符的最后一行，不会同时删除前一行的终止符。替换/追加到无末尾换行的文件时，新末行仍不自动加终止符。匹配使用 KMP，单个 hunk 的行比较次数为 O(文件行数 + 上下文行数)，找到两个匹配即返回歧义，不收集无限候选。

专项回归：`.venv/bin/python -m pytest tests/test_apply_patch.py tests/test_file_policy.py -q`，涵盖路径保护、别名、EOF、BOM/权限、并发变化、取消、部分提交和重复行匹配复杂度。

### 复用进程执行逻辑

`host_support/processes.py` 的 `ProcessRunner` 负责进程启动、环境变量校验、有限输出缓存、超时和清理；`tools/_internal/process_runner.py` 保留兼容导出，`tools/execute.py` 的 `RunCommandTool` 负责模型参数、工作目录策略和 `ToolResult` 封装。其他受信任工具可直接复用 runner：

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

`ProcessRunner` 是受信任应用代码使用的底层接口，不直接暴露给模型，不应用文件保护策略。`RunCommandTool` / `RunPythonTool` / `RunShellTool` 默认返回 `SANDBOX_REQUIRED`；只有受信任的隔离执行调用方显式启用 `execution_allowed=True`。该参数不在模型工具 schema 中，CLI 不提供本地绕过开关。`create_default_tools()` 默认不注册命令工具；Docker worker 显式启用；native 主进程复用命令工具的参数和路径校验，但将 runner 替换为始终通过平台沙箱（Seatbelt 或 Bubblewrap/seccomp）启动的执行器。这个开关本身不创建隔离环境。

超时覆盖运行和输出收集，不因持续输出自动续期，清理通常额外耗时最多约 3 秒；`cleanup_error` 表示清理未完成或无法确认，Windows 后代进程清理仍为尽力处理。native 使用 `supervise_tree=True`，由 `host_support/supervision.py` 在外层跟踪 PID 与内核启动时间、观察后代并执行 TERM → KILL → 核验，避免依赖沙箱 worker 内部的进程组探测。该选项支持 macOS/Linux，属于采样式监督，不能保证发现采样间迅速脱离的所有后代；不向模型暴露任意 PID 终止接口。native 仅在清理无法确认时暂停执行与写入，正常超时清理成功后继续工作，详见 [原生沙箱](native-sandbox.md#超时结果与恢复)。

Linux 的进程身份来自 `/proc/<pid>/stat`，按字节解析以容忍任意进程名。支持时使用 pidfd 绑定目标，在打开句柄后再次核验身份，再发送 TERM/KILL，最终关闭句柄；缺少 Python API 或内核返回 `ENOSYS` 时回退到 PID 信号。权限拒绝不会触发这个回退。诊断记录 `via=pidfd/pid`，以及 `pidfd_open` / `pidfd_send_signal` 等失败阶段。可移植单元测试位于 `tests/test_linux_process_supervisor.py`，真实 namespace、超时和取消测试位于 `tests/test_linux_native.py`；Linux native 已有独立 PID namespace 作为进程生命周期边界，pidfd 本身不提供资源配额或沙箱隔离。

## 工具错误码

`tools/_internal/errors.py` 集中定义通用的 `ToolErrorCode` 和默认错误提示，使用 `tool_error()` 创建失败的 `ToolResult`。工具通过这一入口返回公共错误。

```python
from tools import ToolErrorCode, tool_error

# 可在 execute() 中返回以下结果；这里展示如何构造。
missing = tool_error(ToolErrorCode.FILE_NOT_FOUND)

# 参数、容量限制等需要具体说明时，可覆盖默认提示
invalid = tool_error(ToolErrorCode.INVALID_ARGUMENTS, "start_line must be positive.")

# 工具特有错误保留在工具中，并明确提供提示
no_match = tool_error("NO_MATCH", "old_text does not occur in the file.")
```

通用类别包括参数校验、路径越界/保护、权限、不存在、文件/目录类型、符号链接、已有路径、编码、容量限制和基础读写失败。现有错误码字符串及 `ToolResult.to_message()` 的返回结构保持不变；例如 `PARENT_NOT_DIRECTORY`、`FILE_EXISTS` 与 `PATH_ALREADY_EXISTS` 继续保留原码，便于兼容既有调用方和日志。

`LINE_OUT_OF_RANGE`、`NO_CHANGES`、`NO_MATCH`、`MULTIPLE_MATCHES`、`EDIT_RESULT_TOO_LARGE`、`SEARCH_ERROR`、`CREATE_DIRECTORY_ERROR` 和 `DELETE_FILE_ERROR` 等仍由相应工具定义。工具特有错误必须提供说明，不需要加入公共枚举。新增工具可直接复用上述公共入口。

## search_files 工具

`search_files` 按行查找 UTF-8（可含 BOM）文本中的字面子串，不解析正则表达式。目录递归搜索，也可选择单个文件；默认区分大小写，`case_sensitive=false` 使用 Unicode `casefold()`。每个命中行返回一次工作区相对路径、从 1 开始的行号和文本。

```json
{"query": "TODO", "path": "src", "glob": "*.py", "case_sensitive": true, "include_hidden": false}
```

`path` 默认为 `.`。`glob` 匹配完整工作区相对路径，使用 `/` 分隔，`*` 可匹配 `/`；选择子目录不会改变匹配基准。隐藏文件默认排除，`include_hidden=true` 也不能读取受保护凭据或绕过路径限制。二进制、过大、无法读取和非 UTF-8 文件跳过。

工具实例默认最多扫描 10,000 个候选文件，单文件 2 MiB，返回 100 个命中行；每行正文片段最多 2,000 字符，前后省略标记另占最多 6 字符；命中文本累计预算 20,000 字符（不包含结果 JSON 的其他字段）。返回 `files_scanned`、`skipped_files`、`files_with_matches`、`truncated` 和 `truncation_reason`；达到预算时应结合截断标记判断完整性，不能把部分结果当作全库无遗漏查询。大小写敏感且整段内容不含查询时直接跳过逐行处理；惰性分行不等于流式读取整个文件。

## run_shell 工具与平台行为

`tools/execute.py` 的 `RunShellTool` 已加入 `coding` 按需组，仅在隔离执行工具集中注册。单个程序及原样参数使用 `run_command`；管道、重定向、循环和多行 Bash 源码使用 `run_shell`：

```json
{"script": "python build.py && python test.py 2>&1 | tee test.log", "cwd": ".", "timeout_seconds": 60}
```

参数为 `script`、可选的 `cwd` 和 `timeout_seconds`。脚本默认上限为 64 KiB UTF-8 字节，拒绝 NUL；超时与命令工具共用宿主上限，默认最多 60 秒，不因持续输出而延长。每次调用独立，目录、变量和函数不跨调用保存，无交互终端或持久后台服务。

执行器从 `/bin/bash`、`/usr/bin/bash` 选择可执行的系统 Bash，不搜索工作区 PATH，不自动换成 `sh`。内部向共享 `ProcessRunner` 传入 argv，继续使用 `shell=False`。Bash 使用 `--noprofile --norc -p -o pipefail -c`：不读取登录/启动脚本，不导入环境中的 Bash 函数和 shell 选项；自定义环境中的 `BASH_ENV`、`ENV`、`SHELLOPTS`、`BASHOPTS` 和 `BASH_FUNC_*` 也会移除。`-p` 用于稳定启动行为，不构成隔离机制。

默认启用 `pipefail`，未自动启用 `errexit`/`nounset`。管道任一成员失败可反映在管道退出状态，但 `false; true` 等脚本仍可能最终返回零；有依赖的步骤使用 `&&` 或显式错误检查。只报告脚本整体退出码，不推断所有内部命令是否通过。非零退出码通过正常 `ToolResult.data` 返回，回写检查据此阻止自动回写。

结果沿用 `exit_code`、`stdout`、`stderr`、`timed_out`、截断标志、`cleanup_status`、`output_complete` 等字段，另附 `shell=bash` 和 `shell_executable`。超时仍保留已收集的首尾输出。shell runner 启用进程树监督，native 则复用外层监督执行器，避免 worker JSON 丢失部分输出。正常结束也清理已观察到的后台后代；监督仍有采样局限，不能保证发现采样间迅速脱离的进程，不应把此工具用于启动持久服务。清理未确认时保持现有阻止执行/写入/自动回写的处理。

| 实际执行位置 | 行为 |
| --- | --- |
| macOS / Linux native | 两个工具均可用；缺少系统 Bash 时 `run_shell` 返回 `SHELL_UNAVAILABLE` |
| Windows 宿主 + Linux Docker | 两个工具均在 Linux 容器执行，脚本使用 Bash 语法；宿主注册 schema 时不检查 Windows shell |
| WSL2 中的 Linux native | 按 Linux 处理，仍需满足现有原生沙箱依赖 |
| Windows local | 不注册 `run_command`、`run_shell`、`run_python`；加载 `coding` 不会授予执行权限 |
| Windows native | 现有 native 后端拒绝启动，不回退到未隔离执行 |
| 受信任代码在 Windows 直接启用工具类 | `run_shell` 返回 `SHELL_UNSUPPORTED_PLATFORM`，不启动进程、不自动使用 Git Bash、PowerShell 或 cmd；`run_command` 保持现有 argv 执行行为 |

两个工具的名称和参数独立，同时注册没有冲突。Windows 的 `run_command` 不是 Linux 命令兼容层：程序必须在执行环境中存在，shell 内置命令需要显式调用解释器；`.bat`/`.cmd` 可能由系统 shell 解释，不能假定所有 argv 都按字面传递。底层 Windows 后代清理仍为尽力处理，本次不新增 Windows 原生沙箱。

`get_execution_environment` 的 execution 节增加 `shell_execution_allowed` 和 `shell`（方言、系统路径、可用状态及原因），描述实际执行器。local/退化状态不启动 Bash 探测。

命令、Python、shell 共用 `_ProcessTool` 的工作目录、超时、启动错误和结果转换。`PROCESS_EXECUTION_TOOLS` 集中维护需要退出码检查及 `check_id` 的执行工具集合，native 直接输出分支、Docker 代理和回写检查共同引用。新增其他执行工具时还需实现 native 对应的构造分支。`check_id` 只由 Docker 代理添加，验证失败后可用相同工具、目录与 ID 的成功重试解除；无 ID 时仍按精确操作匹配。

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
