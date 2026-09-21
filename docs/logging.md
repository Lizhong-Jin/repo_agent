# 日志与文件保护

[返回 README](../README.md) · [会话管理](sessions.md)

## 查看日志

以下命令在任务项目目录运行，也可以追加 `--root /path/to/project`：

```bash
repo-agent sessions list
repo-agent sessions logs 1                      # 全部对话，类似 cat
repo-agent sessions logs 1 --cat                # 显式显示全部
repo-agent sessions logs 1 --tail 100            # 最后 100 行
repo-agent sessions logs 1 --tail 100 -f         # 持续跟随新增内容
repo-agent sessions logs latest --kind trace    # 可读执行日志
repo-agent sessions logs 1 --kind jsonl          # 结构化执行事件
repo-agent sessions logs 1 --path                # 只显示日志绝对路径
repo-agent sessions logs 1 > conversation.log   # 输出不带颜色，可重定向
```

会话可按项目内序号、完整 ID 或名称查找；重名时需使用序号。省略选择器默认 `latest`。`--path` 返回的文件可直接交给系统 `cat`、`tail` 或 `tail -f`。

会话界面内使用 `/logs [序号] --tail 100`，省略序号表示当前会话。界面最多查看 1000 行，超长内容保留末尾 200000 个字符；完整输出和持续跟随使用独立终端。

`-f` 未指定行数时先显示最后 10 行；`--cat -f` 先显示全部。查看器固定跟随选中的会话，Agent 重启后继续读该会话的新增记录，`/new` 不会使查看器切到新会话。Ctrl+C 只退出查看，不中断 Agent。文件被替换或截短时从新内容继续读；文件被删除时明确报错退出。

查询不启动模型或沙箱，不占用 Agent 的执行锁，也不改变默认恢复的会话。日志缺失时会提示；旧版本无法可靠关联的执行日志需到原项目 `logs/` 或 `AGENT_LOG_DIR` 查找。

## 文件位置与内容

### 按会话连续保存

位置：`${XDG_STATE_HOME:-~/.local/state}/repo-agent/sessions/<项目路径哈希>/<会话ID>/`。

| 文件 / `--kind` | 内容 | 追加时机 |
| --- | --- | --- |
| `chat.log` / `chat`（默认） | 用户输入、完整回复、恢复/改名/清空等必要提示 | 输入任务时及任务结束后；不逐字记录生成片段 |
| `trace.log` / `trace` | 模型、工具、错误、计时及用量 | 每条执行事件后立即刷新 |
| `trace.jsonl` / `jsonl` | 同一批执行事件，一行一个 JSON 对象 | 每条执行事件后立即刷新 |

目录权限为 `700`，日志权限为 `600`。文件跨重启追加，改名不会改变文件路径。对话日志不新增思考正文或供应商原生状态，异常任务可能没有完整回复；查看日志不会把查看结果再次写入对话日志。

会话快照 `<会话ID>.json` 用于恢复模型上下文，采用整体替换保存，不能作为增量日志 `tail -f`。其中可包含消息、工具结果、原生状态及可见思考记录。对话和日志可能包含项目内容，配置中的 API Key 不作为追踪元数据记录。

### 按运行片段保存

每次启动或界面内 `/new`，还会在项目 `logs/` 生成一组独立追踪文件：

```text
session_时间_随机标识.trace.log
session_时间_随机标识.trace.jsonl
```

`AGENT_LOG_DIR` 只改变这些运行片段日志的位置，不改变会话快照或连续日志目录。未设置时使用 `--root` 对应项目的 `logs/`；显式相对路径以调用目录为准。

文件权限为 `600`，创建时拒绝覆盖同名文件。每个片段有独立的 `run_id` 和统计汇总；`session_id` 指向持久化会话，因此同一会话可以包含多个运行片段。重启后的界面累计用量延续会话，片段汇总只统计该次运行，两者可能不同。

## 事件与统计口径

追踪实现位于 [agent/Tracing.py](../agent/Tracing.py)：`RunTrace` 记录一次任务的模型及工具调用，`Tracer` 写入文件和运行片段汇总。CLI 管理创建、关闭及 `/new` 时的切换。

JSONL 包含 `schema_version`、带时区的 `timestamp`、`session_id`、`run_id` 及任务事件的唯一 `task_id`。主要事件包括 `session_start/end`、`task_start/end`、`model_start/end`、`tool_start/end`、`model_changed`、`recovery` 和 `skill_loaded`。

- **模型调用**：开始、结束、耗时、结束原因及 token 用量。模型次数是 Runtime 调用模型接口的尝试次数，包括失败和中断；底层 HTTP 重试不另算一次 Runtime 调用。
- **工具调用**：模型轮次、名称、调用 ID、路径与参数摘要、成功/失败/中断状态、错误码和耗时。命令另记录 `exit_code`、`timed_out`、`cleanup_failed`；工具调用成功不等于命令退出码为零。
- **任务汇总**：模型和工具次数、失败/中断、用量、总耗时。每条自然语言输入是一项任务，管理命令与空输入不算任务。
- **片段汇总**：任务数量与状态分布、累计用量、任务执行总耗时，以及包含输入等待的片段总耗时。

工具参数只保留路径、小型选项及正文长度，不重复记录文件正文、编辑片段或工具返回正文。追踪元数据不记录 Key、系统提示词和完整模型请求；`skill_loaded` 记录技能名、来源、SHA-256 和加载方式。

输入 token 包含服务端计入输入的缓存用量；输出包含服务端计入输出的思考用量。缓存与思考是细分项，不再次加到总量。每次调用都可能重复计量历史，因此会话累计输入输出之和可以远大于模型上下文窗口。

未返回的用量显示“未返回”；仅部分请求有用量时，显示已知小计和覆盖次数，不按零补齐，也不等同于实际费用。详细计时字段见[流式事件与计时](llm.md#流式事件与计时)。

日志写入失败不会重试或撤销已执行的文件操作。对话日志错误在检查点提示，追踪错误在退出时提示。强制结束或断电可能缺少结束事件，最后一个完整任务之后的内容不保证全部保留；恢复依据会话快照。

## 文件保护边界

文件工具与 Docker 导入/回写共用 [tools/file_policy.py](../tools/file_policy.py)。受保护项包括任意层级的 `.env` / `.env.*`、`.git`、安装元数据、`.codex`、`.agents`、`logs`、常见凭据目录与文件、私钥扩展名，以及 `AGENT_ENV_FILE`、`AGENT_LOG_DIR` 指定路径和用户会话状态目录。

文件工具检查符号链接原名和目标，拒绝多硬链接普通文件；列表和搜索过滤受保护项。Git diff 先筛选路径，再读取差异，并禁用重命名检测，避免引用受保护来源。沙箱额外排除虚拟环境、依赖目录和缓存，见 [Sandbox](../sandbox/README.md#工作副本与-git-语义)。

配置文件请通过配置命令或编辑器修改。文件名规则不能识别嵌入普通源码的密钥，也不构成抵御恶意并发路径替换的操作系统隔离；`.gitignore` 不负责访问控制。
