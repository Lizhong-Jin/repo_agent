# Sandbox v1

CLI 默认使用 Docker。宿主机只运行模型循环、持有密钥、保存日志并管理容器；文件、命令、Python 和 Git 工具均在容器中执行。Python 库直接调用 `create_default_tools` 只提供本地文件和 Git 工具；需要命令执行和隔离时使用 `SandboxSession(root).tools()`。

## 准备与使用

在 Agent 安装目录中更新安装、构建镜像（此步骤需要网络与运行中的 Docker）：

```bash
.venv/bin/python -m pip install -e .
./run_agent.sh --build-sandbox
```

构建只复制 Agent 包，不复制 `.env` 或整个项目工作目录。镜像包含 Python/pylsp、Node.js/TypeScript Language Server、Go/gopls、clangd、C/C++ 工具链、Git 和 Agent 自身依赖；项目需要的测试框架和依赖应加入受信任的派生镜像，再使用 `--sandbox-image IMAGE` 指定。v1 工具执行不开放网络，不提供模型可调用的提权接口。

随后在任务项目目录启动：

```bash
/path/to/agent/run_agent.sh
```

- `/diff`：查看副本与当前宿主机文件的差异（有输出长度限制）。
- `/apply`：明确要求回写新增、修改和删除的普通文件；遇到宿主机改动会拒绝。
- `/clear`：只清空对话，保留副本。
- 退出或单次任务结束后，会显示保留的会话目录；默认手动回写，可按下文开启 on-success。

无需模型配置即可查看和回写保存的会话：

```bash
repo-agent --sandbox-review /absolute/session-directory
repo-agent --sandbox-review /absolute/session-directory --apply
```

会话目录位于系统临时目录，包含宿主机维护的 `state.json` 和 `workspace/`。它不会挂载整个会话目录，只挂载 `workspace/`。临时目录可能被系统清理，需要长期保存时应移动整个会话目录，并使用新路径查看。无需保留时手动删除该会话目录。不要编辑 `state.json`。

需要直接编辑原项目时，显式传 `--sandbox local`：文件和 Git 工具在宿主机运行，不具有 Docker 隔离；任意命令与 Python 工具不注册。需要执行测试或脚本时使用 Docker 模式。Docker 缺失、镜像缺失、启动失败均不会自动转为 local。

更新工具保护规则后，请重新构建镜像，让容器使用同一版本：`./run_agent.sh --build-sandbox`。凭据及元数据保护规则共用 `tools/file_policy.py`，额外的缓存和依赖排除规则位于 `sandbox/policy.py`。

## 自动回写配置与恢复

在**任务项目的 `.env`** 中设置，重启会话生效：

```dotenv
AGENT_SANDBOX_WRITEBACK=on-success
```

默认值为 `manual`。启动脚本读取该项；优先级为 `--sandbox-writeback` 参数 > 已有非空环境变量 > 项目 `.env` > 默认值。直接运行 `repo-agent` 仍不读取 `.env`，请使用环境变量或 CLI 参数。旧项目执行 `./run_agent.sh --init` 可补充配置项，不覆盖已有设置；安装目录 `.env.defaults` 也可以设置该项作为新项目默认值。`--sandbox local` 与 `on-success` 同时使用会报错，避免把本地直接写入误认为受控回写。

自动回写发生在**每条用户任务正常结束后**，交互和单次任务模式均支持，不在每次工具调用后同步。决策读取实际工具结果，不依赖模型回答中的“成功”：

- 先检查是否存在待回写的文件变更。没有变更时报告“无需自动回写”，清除不再对应任何待发布文件的失败记录；下一条任务从干净副本开始，不继承只读检查的失败。容器清理未确认时仍然阻止继续。
- 有待回写变更时，命令退出码非零、超时、清理错误、工具错误会阻止回写，并显示具体原因。
- `run_python`、`run_command` 验证调用可传 `check_id`（1–64 个字母、数字、下划线或连字符）。从首次检查起使用稳定标识；相同工具、相同 cwd、相同标识的重试成功后，即使脚本或命令修正过，也能解除对应失败。其他检查、工具或目录下的成功不会清除它。标识关联表达的是模型对“同一检查”的声明，不能自动证明两段验证代码覆盖相同断言，不能通过删减断言来解除失败。
- 未提供 `check_id` 时仍按完整参数匹配重试（忽略 timeout_seconds，省略 cwd 按 `.` 处理）。已有的无标识失败不会被后来带标识的检查清除；可原样重试或检查后 `/apply`。进程清理未确认需人工检查，不由检查重试解除。
- 模型异常、中断、达到轮数上限、非正常结束等**留下待发布文件变更**时，需要手动 `/apply`，不会被下一条无关的正常任务自动写回。没有遗留变更时，新任务会清理历史阻止状态。
- 验证非法输入等预期失败场景时，用 `run_python` 的 `subprocess.run` 调用被测程序并断言预期退出码，让验证脚本自身成功返回 0。不能仅根据模型文字描述把非零退出码视为已通过；内置提示词包含此约定，自定义系统提示词需自行保留。
- 宿主机冲突或不安全文件类型会阻止回写；仍保留副本。
- `completed` 本身不代表测试通过。可配置 `AGENT_SANDBOX_VERIFY_COMMAND`，在正常完成、有待回写变更、且所有现有阻止原因已解除后，额外执行一次最终检查。未配置时不会猜测测试命令。
- 单次任务自动回写被阻止或失败时退出码为非零；交互模式报告原因并继续等待输入。

可选最终验证配置示例（项目须有相应测试，容器须已安装依赖）：

```dotenv
AGENT_SANDBOX_WRITEBACK=on-success
AGENT_SANDBOX_VERIFY_COMMAND='["python", "-m", "unittest", "discover"]'
```

命令是 JSON 参数数组，不解析 shell，在 Docker 副本根目录运行，限时 120 秒；不会在宿主机运行。优先级为 `--sandbox-verify-command` > 非空环境变量 > 启动脚本读取的项目 `.env`，CLI 传空字符串可禁用。该检查只用于 on-success，manual、`/apply`、无文件改动时不执行。

最终检查非零退出、超时、工具错误或清理异常均阻止回写；成功不能抹除其他未解决失败。失败后保留副本，修复并正常完成下一条任务时重新验证；清理异常或中断则需人工检查。最近一次最终检查的命令、退出码和有长度限制的输出存放于会话目录 `verification.json`（容器外）；此文件下次验证会覆盖。验证可能生成文件，仍按通常的排除和冲突规则处理。

模型验证调用示例：第一次 `run_python({"code": "...", "check_id": "maze_path"})` 失败后，修正代码并使用同一 `check_id` 重跑。不同检查必须使用不同标识。这个机制解决重试关联问题；检查覆盖率仍取决于实际测试内容。

每批回写（包括手动）开始前，先在会话目录的 `backups/<备份ID>/` 保存原文件和恢复记录，目录不暴露给容器。记录覆盖新增、修改和删除；备份失败时不修改原项目。回写采用逐文件替换，仍不是多文件事务；中途失败时会报告备份路径，可能已有部分文件写入。

显式恢复某批回写，无需模型或 Docker：

```bash
repo-agent --sandbox-review /absolute/session-directory --restore-backup 32位备份ID
```

恢复会检查文件是否仍为该批次写入后的内容，拒绝覆盖后续人工编辑；也会检查备份摘要。恢复操作本身同样建立备份。工作副本保留，因此恢复后请先检查副本，避免再次手动写入不想保留的修改。备份与副本都在系统临时目录，需要长期保留请保存整个会话目录。

## 工作副本与 Git 语义

导入当前磁盘文件，包含未提交修改和普通未跟踪文件。默认排除 `.env` / `.env.*`、`.pem`/`.key`、原 `.git`、日志、虚拟环境、node_modules、常见缓存与凭据目录、`.codex`/`.agents`，以及 `AGENT_ENV_FILE` 与 `AGENT_LOG_DIR` 指定的内容。符号链接、硬链接和特殊文件不导入；导出发现这些类型会拒绝。文件名过滤不能识别所有嵌入源码的密钥，请勿在可导入源码中存放凭据。

副本建立全新的 Git 基线，不导入宿主机历史、索引或 Git 配置。`git_diff`/`git_status` 表示副本相对于会话开始时的变化；`/diff` 与 `/apply` 使用宿主机记录的内容摘要，不能被修改容器内 Git 历史绕过。`/apply` 后宿主机内容摘要更新，容器 Git 初始基线不变。

所有工具共享一份副本，但每次调用启动并销毁独立容器。文件持久化；进程、内存、临时目录和环境变量修改不跨工具调用保留。网络关闭，HOME 指向容器临时目录，模型密钥不通过环境变量传入。

## 边界与限制

- 固定使用已检查镜像的 ID；非 root、只读根文件系统、移除 capabilities、禁止提权；不挂载 Docker socket、宿主机 HOME 或设备。
- 默认 512 MiB 内存及等量 memory+swap 上限、1 CPU、64 个进程、64 MiB 临时目录、单文件写入 64 MiB 上限。单次工具外层时限 130 秒，工具本身可能有更短时限；容器移除另有 15 秒时限。
- 导入和导出普通文件总量限制 256 MiB。**这不是运行期间的磁盘总量硬配额**，进程仍可能创建多个文件占用更多空间。v1 适用于个人本地项目，不作为恶意多租户服务的完整资源隔离方案。
- 正常结束、超时、Ctrl+C 都会强制移除该调用容器。清理未确认后，该后端拒绝后续调用。主机断电或 Agent 被 SIGKILL 时无法保证执行清理，可通过 `docker ps -a --filter name=repo-agent-` 检查遗留容器。
- 回写前检查所有变更的基线，每个文件写入前再次检查；使用不跟随符号链接的目录描述符和临时文件替换。**多文件回写不是事务**，I/O 失败可能产生部分回写。请避免回写瞬间并行编辑同一文件；检查和替换之间仍无法提供跨进程的原子比较并交换。
- 回写普通文件内容与可执行位；不保留 ACL、扩展属性，不同步空目录或目录删除。删除最后一个文件后空目录可能保留。
- 容器内是 Linux，不能直接使用 macOS 的虚拟环境或依赖原生 macOS 的测试。

## 验证

```bash
.venv/bin/python -m pytest -q
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox.py
```

默认测试包括副本筛选、回写、冲突、符号链接/硬链接/FIFO 拒绝、全部工具代理、缺失 Docker 时关闭执行、容器参数和中断清理。真实容器测试仅在显式设置 `RUN_SANDBOX_DOCKER_TESTS=1` 时运行，需要预先构建镜像，验证跨工具文件可见性、宿主机路径与密钥不可见、网络禁用、Git 与显式回写。

命令调用成功不等于命令退出码为零：追踪日志保留工具调用状态，并额外记录 `exit_code`、`timed_out` 和 `cleanup_failed`，不记录命令输出正文。

## 多语言代码符号

`get_symbols` 根据文件后缀选择 `tools/lsp_config.py` 中的配置；默认覆盖 Python、JS/JSX、
TS/TSX、Go、C/C++。构建镜像会对八种语言标识运行实际查询自检（`sandbox/lsp_smoke.py`）。

运行时缓存使用 `/tmp`，保持只读根文件系统、非 root、禁用网络和原有资源上限。
Go 使用本地工具链，禁用自动工具链/模块下载。新增依赖扩大镜像体积，首次构建需要更多时间。
项目的第三方依赖不包含在语言服务器安装中；应在派生镜像中按项目预置。

完整工作进程验证：

```bash
RUN_SANDBOX_DOCKER_TESTS=1 .venv/bin/pytest -q tests/test_multilang_sandbox.py
```

通过 `SANDBOX_LSP_TEST_IMAGE` 可指定待验证的镜像。新的语言配置随镜像分发；
宿主机传给工具工厂的自定义注册表不会自动传入 Docker。构建更新后启动新会话才能使用新镜像。

## GPU 算子沙箱

构建统一执行 `./run_agent.sh --build-sandbox`，启动统一执行 `./run_agent.sh "任务内容"`。
构建与启动共用 Docker 主机检测逻辑：确认 NVIDIA GPU 可用后自动选择 CUDA 环境，
否则选择普通环境；已配置但损坏的 NVIDIA runtime 会明确报错。一个 `sandbox/Dockerfile`
生成统一名称 `repo-agent-sandbox:v1` 的镜像，启动时校验环境标签，不匹配时提示重新构建。
CUDA 配置挂载 GPU，增加编译所需的内存、临时空间和执行时间，同时保持非 root、禁网、
只读根文件系统和受控回写。GPU 选择通过宿主机 CLI 的 `--sandbox-gpus` 控制。
完整构建与验证流程见 [GPU 算子开发指南](../docs/gpu-operators.md)。
