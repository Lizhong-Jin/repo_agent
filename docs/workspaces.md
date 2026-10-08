# 独立 Git 工作区（第一版）

local/native 可以选择在独立 worktree 中开发。文件工具、native 命令、项目技能和任务报告使用同一执行目录；会话、任务队列和执行账本仍归属于原项目。运行结束不会自动合并或删除工作区。

## 开始执行

已有 Git 仓库应有初始提交，且启动时工作目录和暂存区干净；第一版不携带未提交修改。请从仓库根目录启动：

```sh
repo-agent --root /path/to/project --new-session --sandbox native --workspace worktree
# 只需要文件和受控 Git 读取，不开放命令执行：
repo-agent --root /path/to/project --new-session --sandbox local --workspace worktree
```

`--workspace direct` 保持原目录执行。省略该参数时，新会话使用原目录，已有会话恢复自己的选择。独立工作区丢失、身份变化或进程状态未确认时会拒绝启动，不会回退到原目录。

同一会话的串行队列、重试和 `/continue` 复用同一工作区，每项任务仍有独立报告。`/workspace` 显示路径与状态；`/report` 或 TUI 的 F3 查看任务证据。第一版在独立工作区会话中禁用原地 `/new`，需要退出后通过 `--new-session --workspace worktree` 建立新的开发会话。`/switch` 恢复目标会话自己的绑定。

worktree 是代码工作目录隔离，不是安全沙箱。local 仍不能执行任意命令、Python 或语言服务器，不能据此声称测试通过。native 继续使用现有 OS 沙箱，只允许修改执行工作区；四个受控 Git 读取工具由宿主提供，不把原仓库共享 Git 元数据开放给任意命令。

## 没有 Git 的项目

初始化是用户命令，不要求模型配置，也不是可传入任意路径和参数的模型 Shell 工具。

```sh
# 预览将纳入初始基线的文件、大小及内容摘要；不创建项目 .git
repo-agent workspaces init --root /path/to/project
# 明确授权创建 Git 仓库及初始基线
repo-agent workspaces init --root /path/to/project --yes
```

尊重项目 `.gitignore`，排除现有受保护文件、凭据、日志、依赖目录和构建产物；额外排除规则写入 `.git/info/exclude`，不改写项目 `.gitignore`。基线提交使用 `Repo Agent <repo-agent@localhost>`，不修改用户全局 Git 身份，不设置远端、不推送，不执行 Git hooks。

已经属于父目录仓库的项目不会被再次初始化。初始化中断时保留操作记录；文件和已发布提交符合记录时，可重新执行同一命令完成恢复，不自动覆盖未知状态。单文件上限 32 MiB、总基线上限 256 MiB、最多 10000 个文件。

## 审查与接收修改

先退出 Agent 并等待进程收尾。工作区管理与运行中的会话使用同一个项目锁，第一版不支持同项目并发管理或多任务并行。

```sh
repo-agent workspaces list --root /path/to/project
repo-agent workspaces status --root /path/to/project --session 1
repo-agent workspaces review --root /path/to/project --session 1
```

`--session` 支持序号、名称、完整会话 ID，默认 `latest`。审查输出包含新增、修改和删除的文件差异，以及绑定工作区、基线、分支 HEAD 和内容树的审查令牌。被忽略且未跟踪的环境文件不进入合并。历史测试结果来自任务报告；如果手动修改过工作区，应重新验证，审查命令本身不会执行测试。

若工作区已创建，但模型或执行环境初始化失败，首份会话快照可能尚未保存。此时用 `workspaces list` 找到 ID，并显式指定 `--session` 恢复；不会将它自动设为默认会话。

```sh
repo-agent workspaces merge --root /path/to/project --session 1 --review REVIEW_TOKEN --yes
```

将 `REVIEW_TOKEN` 替换为刚才的完整令牌。文件或分支变化后旧令牌失效；再次开始任务也会清除令牌。合并只接受原目标分支仍在创建基线且原目录干净的情况，采用快进，不自动解决冲突、不自动 stash，也不覆盖被忽略的同名文件。受保护文件的变化不能自动接收。

合并时把审查内容建立为工作区分支上的提交，再快进原分支。合并前记录操作意图与提交 ID；若 Git 操作成功但记录保存中断，可核对实际 HEAD 恢复状态。合并完成后工作区保留，后续开发使用新会话。

## 放弃、恢复和清理

```sh
repo-agent workspaces discard --root /path/to/project --session 1 --yes
repo-agent workspaces recover --root /path/to/project --session 1
```

**第一版的 discard 是原地归档放弃，不删除目录或分支，也不释放磁盘空间。** 未跟踪文件、被忽略的文件和环境目录完整保留，原项目不被修改。后续可在核实进程停止后恢复；清理磁盘及归档导出留待后续版本，不自动调用 `worktree remove --force`。

创建和合并中断时，`recover` 检查实际 Git 状态，不会自动重放模型任务。运行中崩溃、清理结果不确定以及恢复已归档工作区，需要用户先核实遗留进程已停止，再明确执行：

```sh
repo-agent workspaces recover --root /path/to/project --session 1 --confirm-stopped
repo-agent --root /path/to/project --session 1 --sandbox native
```

`--confirm-stopped` 是用户的确认，不会代替进程检查或杀死任意进程。恢复会话后仍应检查文件和账本，再选择 `/continue` 或 `/queue resume`。

工作区状态与任务结果分别保存。`ready` 仅表示当前没有未确认执行，可以审查/继续，并不表示任务或测试成功。状态包括 `creating`、`ready`、`running`、`blocked`、`merging`、`merged`、`archived`。状态文件位于项目会话目录的 `workspaces/`，执行目录位于会话根目录旁的 `workspaces/<project-key>/<session-id>/`，正常退出不会删除。

## Python 与第一版边界

native worktree 不继承启动终端的 venv、Conda、PATH Python 或 `AGENT_PROJECT_PYTHON`，优先发现执行工作区自己的 `.venv`，否则使用 Agent Python。显式 `--project-python` 必须是工作区内的环境入口。项目依赖需要在工作区准备；不要通过复用原项目的 editable 安装来验证另一份代码。日志和模型进程本身仍按现有宿主配置运行。

第一版支持已有提交的普通 Git 仓库和显式初始化后的仓库，以及 local/native 两种执行方式。暂不支持 Docker worktree、非 Git 私有副本、子模块、已跟踪符号链接、文件实际使用的外部 Git clean/smudge/process 过滤器（包括 LFS）、稀疏/跳过工作区索引标记、目标前进后的自动整合。Git 操作关闭 hooks、自动维护和外部 diff/textconv。

Windows 下，Git 操作会临时启用 `core.longpaths`，允许工作区内的文件路径超过 260 字符，不修改用户 Git 配置。但 Windows 启动进程仍限制工作目录长度：项目和独立工作区的绝对根路径最多为 258 个 UTF-16 单位（大部分字符占 1 个，部分字符如 emoji 占 2 个）。超限会在创建工作区分支和目录之前拒绝；请缩短项目路径，或将 `XDG_STATE_HOME` 设为较短的状态目录后建立新会话。已有超限工作区保留原状，不会自动移动或删除。

项目锁只协调本应用；其他编辑器或手动 Git 操作不受它约束。重要步骤会复核文件和分支，发现不一致则停止；无法把磁盘文件、Git 操作和状态文件变成一个全局事务。

仅安装 Git LFS 或配置未被项目文件使用的过滤器，不影响工作区功能。初始化、审查、创建和合并会按工作目录及暂存区的有效属性检查实际使用情况；创建与合并还会按目标工作区的配置检查待检出的内容。全局属性、嵌套 `.gitattributes`、属性宏和 `.git/info/attributes` 均参与检查。核验失败时停止，保留用户配置和原有暂存区。
