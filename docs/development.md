# 开发指南：Runtime 与验证

[文档首页](index.md) · [项目首页](../README.md)

本文面向源码开发者，命令默认在仓库根目录执行。先阅读[项目架构](../Project_Architecture.md)，工具扩展见[工具开发参考](tools.md)，打包发布见[构建与分发](distribution.md)。独立发行版用户不需要运行开发检查。

## 开发环境与验证

源码安装现在自动安装 `dev` 依赖组中的 pytest、Ruff 及固定的构建依赖。默认先准备锁定的受管 Python，再按[安装说明](installation.md)运行 `./install.sh`；已有安装也可以只补齐开发依赖：

```bash
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
.venv/bin/python -m pip install --require-hashes -r requirements-build.lock
.venv/bin/python -m pytest -q
.venv/bin/ruff check agent cli llm tools sandbox scripts examples tests build_support.py build_manifest.py
.venv/bin/ruff format --check agent cli llm tools sandbox scripts examples tests build_support.py build_manifest.py
python3 build_manifest.py
python3 scripts/lock_dependencies.py --check
```

开发依赖安装可能联网；锁文件检查需要 uv。Ruff 命令表示建议覆盖范围，不表示当前仓库已经通过全部规则。默认测试使用模拟模型，不消耗在线推理额度；安装入口测试需要仓库 `.venv/bin/repo-agent` 可运行。构建清单测试使用已有的 setuptools/wheel，因此旧安装补齐时上方也安装锁定的构建依赖。独立发行版不安装开发依赖。

将以下验证分别记录，不能用默认测试通过替代真实环境验收：

| 检查层次 | 入口 | 条件与范围 |
| --- | --- | --- |
| 默认回归 | `.venv/bin/python -m pytest -q` | 模拟模型、文件/配置/会话、工具协议、构建产物；部分测试按依赖跳过 |
| macOS native | [原生验证](native-sandbox.md#验证) | `RUN_SANDBOX_NATIVE_TESTS=1`；可用 Seatbelt，外层环境允许启动 |
| Linux native | [原生验证](native-sandbox.md#验证) | `RUN_SANDBOX_LINUX_TESTS=1`；Bubblewrap、libseccomp、可用 namespaces |
| Docker 与多语言 | 下方命令 | `RUN_SANDBOX_DOCKER_TESTS=1`；已构建镜像及可用 daemon |
| GPU | [算子验证](gpu-operators.md#验证边界) | Docker / native 各有开关，必须有实际 NVIDIA GPU 和所需依赖 |
| 发行安装 | [安装验收](distribution.md#校验失败处理与安装验收) | 显式提供当前平台完整包，使用受管 Python 离线安装；测试以模拟 Docker 验证构建入口 |

```bash
repo-agent-build-sandbox
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox.py tests/test_writeback.py tests/test_multilang_sandbox.py
```

开关只选择测试，不能补齐依赖；缺少目标平台或硬件时的跳过不计为通过。真实模型 API 的可用性、参数和计费需要另行验证，不属于默认套件的结论。

## 离线开发环境

在联网机器上使用 Python 3.13（与受管运行时主次版本一致）准备完整开发依赖；目标架构不必与准备机器相同：

```bash
.venv/bin/python scripts/prepare_python_bundle.py --target linux-x86_64 --with-dev --output dist/dev-kit-linux-x86_64
```

可选目标为 `linux-x86_64`、`linux-arm64`、`macos-x86_64`、`macos-arm64`。此开发材料脚本每次处理一个平台，省略 `--target` 时选择当前平台。输出包括固定版本的 `runtime/python.tar.gz`、平台标记、`wheelhouse/` 和依赖清单指纹；输出目录须为空。`--with-dev` 加入固定构建工具、pytest、Ruff 及其依赖。没有该选项只准备 native 所需运行依赖。

将源码与此目录复制到目标机器，按[离线安装](installation.md#离线安装)执行。开发材料须与源码的锁文件匹配；依赖升级后重新生成。首次准备仍需联网，不把运行时和 wheels 提交到 Git。已具备材料时，准备脚本也支持 `--offline --runtime-archive ... --wheelhouse ...`。

源码采用 editable 安装，修改代码后重启 Agent 即可加载；改动依赖声明或锁文件后重新安装。`.venv` 用于 Agent 开发和测试，任务项目的 Python 由 native 单独选择，见[环境规则](python-environments.md)。

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

Runtime 负责同步任务循环、轮数限制和输出截断恢复。CLI 通过 `SavedConversation`、`SessionStore` 和 `SessionCatalog` 管理跨进程保存、恢复、命名与日志，直接使用 Runtime 的库调用方需自行管理持久化。CLI 已通过 `SavedConversation` 将 `ContextCompactor.before_request` 接入 Runtime，支持自动检查及 `/compact` 手动压缩，并由 `HistoryArchive` 保存原始消息。独立使用 Runtime 仅提供回调入口，需要调用方自行装配压缩及持久化。当前没有并行工具执行或整项任务的 token／费用预算。命令、Python、Git、语言服务器及 Docker 后端各自管理执行超时；Runtime 不统一管理工具超时。`read_file` 的路径范围和读取限制仍由工具自身管理。

### 压缩集成边界

- `CompactionSettings` 是压缩默认值的唯一代码来源，CLI 的 `RUNTIME_OPTIONS` 读取其默认值；触发比例为 0.75，目标为 0.45，用户显式配置优先。
- `CompactionNotNeeded` 表示没有可移出近期范围的完整历史；自动模式只在输入仍处于安全预算内时原样继续，超出预算时提示拆分输入。手动模式显示无需压缩。不能用通用 `ValueError` 捕获掩盖归档或保存失败。
- `pins` 是有序的用户发言事件列表：`ref` 指向可去重的原文，`occurrence` 以快照 ID 和原始位置区分同一文本的不同出现。已移入交接前缀的事件沿用原 ID；仍留在近期消息中的事件不重复加入 `pins`。
- 旧版没有 `occurrence` 的会话仍可恢复，已有摘要不会被自动重写。修复不追溯重建旧版已经省略的重复发言；原始有序快照仍可核查。

策略、参数与存储路径见[上下文压缩与历史回查](context-compaction.md)。

## 工具开发与分发入口

工具工厂、Git 执行边界、文件工具、进程管理和 LSP 协议集中在[工具开发参考](tools.md)。默认工厂不会创建隔离环境；新增执行工具必须由已建立隔离的后端注册。

分发清单、依赖更新、产物验证及安装验收集中在[构建与分发](distribution.md)。新增或删除源码模块后，需同步生成配置并校验实际产物。

<details>
<summary>旧章节入口（兼容已有链接）</summary>

## 统一创建工具

见[工具开发参考：统一创建工具](tools.md#统一创建工具)。

### Git 工具的执行边界

见[工具开发参考：Git 工具的执行边界](tools.md#git-工具的执行边界)。

### 文件工具公共组件

见[工具开发参考：文件工具公共组件](tools.md#文件工具公共组件)。

### 严格补丁编辑

见[工具开发参考：严格补丁编辑](tools.md#严格补丁编辑)。

### 复用进程执行逻辑

见[工具开发参考：复用进程执行逻辑](tools.md#复用进程执行逻辑)。

## 工具错误码

见[工具开发参考：工具错误码](tools.md#工具错误码)。

## get_execution_environment 工具

见[工具开发参考：get_execution_environment 工具](tools.md#get_execution_environment-工具)。

## read_file 工具

见[工具开发参考：read_file 工具](tools.md#read_file-工具)。

## GetSymbols：读取多语言代码符号

见[工具开发参考：GetSymbols：读取多语言代码符号](tools.md#getsymbols读取多语言代码符号)。

## 引用查询与文件诊断

见[工具开发参考：引用查询与文件诊断](tools.md#引用查询与文件诊断)。

## SearchWorkspaceSymbols：跨文件查找符号

见[工具开发参考：SearchWorkspaceSymbols：跨文件查找符号](tools.md#searchworkspacesymbols跨文件查找符号)。

## 构建独立发行版与更新依赖

发行构建脚本 `scripts/build_release.py` 默认构建所有支持的平台；加 `--target` 只构建指定平台。仅生成完整包，输出到 `dist/<版本>/<平台>/`，不再提供轻量包或单独导出 Agent wheel。完整命令、离线材料组织和验收方法见[构建与分发](distribution.md#构建独立发行版与更新依赖)。

### 统一分发清单

见[统一分发清单](distribution.md#统一分发清单)。

</details>
