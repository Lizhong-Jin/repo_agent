# 统一异常恢复与外部操作核实

[文档首页](index.md) · [执行账本](execution-ledger.md) · [任务队列](task-queue.md) · [权限](permissions.md)

恢复入口汇总会话保存、执行账本、队列、工作区、清理状态、严格交付和 GitHub 操作证据。它不调用模型，不重放工具，不恢复历史权限，不自动继续队列、合并或重试远端写入。

## 先检查，再选择一个动作

退出当前 Agent 并等待收尾后，在项目目录运行：

```bash
repo-agent recover
repo-agent recover plan --session 1
repo-agent recover status --session 1 --json
```

`--root` 指定项目；`--session` 支持序号、名称、完整 ID 或默认 `latest`。序号优先于同值名称，与普通会话选择规则一致；同名会话应使用序号或完整 ID。独立入口不要求模型配置，不启动 native/Docker 后端，也不加载项目模型配置。

`status` 和 `plan` 都只检查：不补完 `.pending-save`，不迁移目录索引、不保存会话、不写 Git 对象，也不联网。它们获取已存在的项目锁；没有状态目录时不创建目录，有活动会话时拒绝独立操作。目录或记录损坏时输出 `evidence.unreadable`，不生成可执行动作。检查结果是有范围的诊断，不代表测试、交付或进程清理已经通过。

会话绑定的工作区记录缺失时停止恢复，不退回原项目目录。ready/running/blocked 工作区还须通过目录、Git 身份、分支和基线检查；目录丢失或被替换时先恢复匹配的工作区资料。

输出包含问题代码、阻止的操作、可选动作和计划编号。每次只应用一项，下面的 `PLAN` 须替换为刚生成的完整编号，动作须来自该计划：

```bash
repo-agent recover apply PLAN --session 1 --action repair-save
```

应用前重新检查会话、目录索引、待发布保存、账本水位、工作区状态、可观察文件内容、相关报告、外部操作及恢复收据。状态变化会使计划过期。每次操作后使用返回的新计划；不要连续套用旧编号。计划绑定检查时可观察的内容，不提供文件系统原子快照或对外部编辑器的独占锁。文件扫描沿用任务报告的数量、大小、时间及排除范围，扫描不完整时不提供修改动作。

## 可选动作及边界

| 动作 | 作用 | 保留的边界 |
| --- | --- | --- |
| `repair-save` | 复用会话存储的 redo 机制，补完 `.pending-save` 和目录索引发布 | 项目级待发布记录可能属于另一会话；不会把未完成任务改为成功 |
| `reconcile-session` | 将保存中的 running 任务标为 interrupted；保存恢复说明和新增账本证据，并更新水位 | 队列暂停；未知工具不重放，账本原始记录不改写 |
| `workspace-recover` | 复用工作区创建/合并中断恢复，根据真实 Git 状态收尾 | 不再次调用 merge，不回滚外部提交，不恢复已归档工作区 |
| `confirm-cleanup` | 记录用户核实说明，恢复会话健康标记及 running/blocked 工作区 | 需显式确认；不终止进程、不证明历史命令成功 |
| `refresh-delivery` | 使用现有报告和当前内容重新保存交付判定 | 不运行检查、不接受例外，不把中断任务变成通过 |
| `verify:操作ID` | 核实已记录的 GitHub 发布或 PR 创建结果 | 独立授权，只执行 GET；不重试写入、不授予新写入权限 |
| `checkpoint` | 会话内重试保存当前内存状态 | 只在空闲且保存失败时可用；保存后队列暂停 |

存在待发布保存时先处理保存。会话快照早于账本时先核实和协调，清理未确认时不能刷新交付。协调崩溃中的 running/pending 任务，或快照之后仍为 started/uncertain 的工具调用，会将会话健康标记设为未知，需要再核实进程；不能因为队列已无运行任务就认定执行环境安全。未确认调用检查覆盖全部新增账本事件，不受恢复摘要最多 30 条的展示限制。

普通启动和会话切换在建立执行环境之前检查恢复状态：保存的 running/pending、清理未确认或账本水位不一致会要求先运行 `recover plan`。换用 local/native/Docker 不能清除该会话的阻塞。直接装配 `SavedConversation` 时也会保留阻塞，控制器拒绝继续派发；保存和再次重启不会把新后端的健康状态当成旧执行已清理的证据。

确认清理前，用户须检查本次执行及其遗留子进程、容器或 Job。不能仅凭主进程 PID 不存在就确认所有子进程已经停止：

```bash
repo-agent recover apply PLAN --session 1 --action confirm-cleanup \
  --confirm-stopped --note "已检查本次执行及全部遗留子进程，确认停止；保留工作副本待核实"
```

这属于人工确认，记录 `basis=user_confirmation`；不会把原报告的 `cleanup_status=unknown` 改成成功。后续严格交付仍需新验证尝试。Windows native 可先用 `repo-agent native-cleanup list` 查看资源，资源回收和明确丢弃继续使用该专属入口；Docker 副本审查、回写及备份恢复继续使用沙箱管理入口。安装损坏仍通过安装器 `--recover` 处理，不依赖应用成功启动。

普通存储或元数据协调完成后队列保持暂停。`repair-save` 保留原待发布快照的语义，远端核实只保存核实结果；二者也不会派发任务。重启时仍按既有规则暂停等待任务。核实环境、文件和交付后，用户再选择 `/queue resume`、`/continue` 或新的修复任务。

## 会话内入口

```text
/recover
/recover plan
/recover apply PLAN checkpoint
```

普通终端和 TUI 共用同一入口。检查包含未保存的内存状态、当前任务和权限申请。任务运行或收尾时不提供修改动作；执行中检查仅为当时观察，不生成可执行恢复动作。权限申请是正常暂停，按 `/permissions approve ID` 或 `deny ID` 处理，再 `/continue`。

会话内第一版只执行 `checkpoint`，其他动作需要退出后重新生成独立计划，避免内存状态覆盖磁盘恢复结果。检查命令本身不启动队列。

## GitHub 操作记录

所有经宿主服务发出的新 GitHub 写入先保存独立记录：

```text
<项目状态目录>/operations/<session_id>/<operation_id>.json
```

记录包括批准的完整 scope 及摘要、会话/操作身份、保存修订号、创建时间、发送阶段，以及已取得的候选提交或 PR 编号。文件内容只保留路径、摘要和大小；不复制上传文件正文，不保存 GitHub token、HTTP 认证头或传输异常正文。批准的 PR 正文和提交消息会保留，因此操作记录属于私有会话数据，不应直接公开分享。

| 发送阶段 | 含义 |
| --- | --- |
| `prepared` | 操作意图已保存；尚未提交分支更新或 PR 创建请求 |
| `objects_created` | 候选提交身份已保存；尚未提交分支更新请求 |
| `mutation_requested` | 即将发送最终写入；可能未发送、部分完成或已成功，不能从缺少响应判断失败 |
| `response_received` | 已收到写入响应并保存必要标识；后续收尾可能中断 |
| `completed` | 服务正常返回前的操作记录已保存；不表示远端内容以后不会变化 |

文件发布在更新分支前持久化候选提交。更新响应必须包含批准的分支引用、候选提交及 commit 类型，才记录 response_received；缺字段、格式错误或身份不匹配时保留 mutation_requested，返回待核实。对象创建阶段中断可能留下未引用的 Git 对象，但不代表分支已经更新。PR 创建在 POST 前保存阶段，取得响应后保存 PR 编号；不会为了关联身份而擅自修改已批准的 PR 正文。

必须保存的操作证据写入失败会抛出持久化错误并停止派发。取消、超时、重启不会恢复一次性授权。同一会话中，完整 scope 相同且仍处于 `mutation_requested` 或 `response_received`、未核实成功的写入会被拒绝再次发送；响应已落盘但完成记录失败也不能重复创建 PR。这不是跨会话或 GitHub 端的全局幂等保证，也不能通过改变请求参数来推断旧操作未生效。

## 显式核实远端结果

先查看 `recover plan` 中的操作 ID、仓库与动作，再明确允许该项联网查询：

```bash
repo-agent recover apply PLAN --session 1 --action verify:OPERATION_ID --allow-network
```

该命令由用户直接发起；`--allow-network` 只授权核实所选记录，不能用于其他恢复动作。不需要模型或会话内批准记录。私有仓库查询使用启动进程显式提供的 `REPO_AGENT_GITHUB_TOKEN`；不寻找 Git/gh 凭据。查询仍通过原有固定 GitHub API、公网连接检查、大小限制、超时和取消机制。查询失败保留原记录，不把失败解释为“远端不存在”。

查询绑定计划中的操作记录摘要；即使记录在计划校验后、恢复收据保存期间改变，也会在发起 HTTP 前拒绝，须重新生成计划。核实返回后再次检查记录修订，避免覆盖期间出现的新证据。

会话中也可由模型调用 `github` 的 `verify_operation` 操作，提供 `repo` 和 `operation_id`。该调用按现有流程生成一次性 `github_read` 申请，绑定当前操作记录摘要；用户通过 `/permissions` 批准后 `/continue`，再次调用相同参数才会核实。记录改变时需新申请，历史写入批准不授权核实查询。

| 核实结果 | 判定依据 |
| --- | --- |
| `confirmed` | 文件发布：目标分支当前指向候选提交，或经 compare 证明包含候选提交且复读分支未变化；PR：找到已保存创建响应编号对应的仓库 PR |
| `not_submitted` | 本地持久化阶段证明尚未提交分支更新/PR 创建；可能存在未引用 Git 对象，不代表没有任何网络操作 |
| `unknown` | 缺少身份、远端分支分叉/变化、未找到可确认结果，或只发现相似 PR；不能推断从未执行 |

文件发布核实证明的是查询时分支包含候选提交，不证明当前所有文件仍与候选一致，也不证明操作由哪个客户端唯一造成。PR 标题、正文和分支相似只形成候选列表；缺少保存的 PR 编号时，即使只找到一个相似项也保持 unknown。候选查询只取首屏 30 项，未找到也不证明未创建。核实不修改原始发送阶段或任务交付状态，另外保存观察结果与核实时间。

API 查询依据见 [GitHub compare](https://docs.github.com/en/rest/commits/commits#compare-two-commits) 和 [PR 查询](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)。旧版本已经发生但没有操作记录的写入无法追补身份，仍需结合执行账本和远端现场人工核实。

## 恢复本身中断与测试

每次应用先写 `<项目状态目录>/recovery/<计划编号>.json` 的 started 收据，完成后更新为 completed。旧计划不能重复应用；中断后重新检查当前证据并生成新计划。started 收据保留为历史不确定性，不代表动作一定没执行，也不能据此重放远端写入。该收据与其他文件不是整体事务；已有会话 redo、工作区状态机和操作记录分别负责自己的恢复。

```bash
python -m pytest -q tests/test_unified_recovery.py tests/test_external_operations.py
python -m pytest -q tests/test_scoped_services.py tests/test_execution_ledger.py \
  tests/test_task_queue.py tests/test_delivery.py tests/test_workspaces.py
```

测试使用临时项目、真实本地 Git、注入的网络响应和保存故障，覆盖只读检查、失效计划、锁冲突、中断收尾、清理确认、取消、发送前落盘、PR 模糊匹配和分支竞争。它们加入默认回归及 Windows contracts。模拟 API 测试不代表真实 GitHub token/组织权限或公网集成验收；真实远端验证须在测试仓库中显式批准并执行。
