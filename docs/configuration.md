# 配置参考

[返回 README](../README.md)

`repo-agent config` 可在任务项目目录执行；`--root` 指定用于计算生效配置的项目。

## 用户配置

运行 `./install.sh` 会将 `.env.example` 复制为 `~/.config/repo-agent/.env`，安装过程无需输入模型或 API Key。已有用户配置保持不变。

`repo-agent` 和 `python -m cli.main` 都读取用户配置及工作目录 `.env`。优先级为 **命令行参数 > 已有非空环境变量 > 项目 `.env` > 用户 `.env` > 内置默认值**。项目显式空值可以清除同名用户配置，例如 `LLM_BASE_URL=` 恢复厂商默认端点。配置按字面量解析，不执行命令或变量展开。

用户配置目录默认为 `${XDG_CONFIG_HOME:-~/.config}/repo-agent`，也可在启动前通过 `AGENT_CONFIG_DIR` 指定。`--root` 同时选择工作目录及项目 `.env`；`AGENT_ENV_FILE` 可指定项目配置文件（相对路径以调用目录为准），指定文件不存在时会报错。

## 查看和修改配置

这些命令可以在任意工作目录运行，不要求先配置模型，不启动 Docker 或 Agent 会话，也不发出 API 请求：

```bash
repo-agent config show                 # 查看按当前工作目录计算的生效值及来源
repo-agent config show --user          # 只看用户配置保存的值
repo-agent config show --root /path/to/project
repo-agent config path                 # 查看用户配置文件位置
repo-agent config edit                 # 交互选择一个配置项并修改
repo-agent config edit LLM_TIMEOUT     # 直接交互修改指定项
repo-agent config set LLM_TIMEOUT 120
repo-agent config set AGENT_MAX_STEPS 12
# 不限制每条任务的模型调用轮数
repo-agent config set AGENT_MAX_STEPS 0
repo-agent config set LLM_BASE_URL ''   # 保存空值，恢复厂商默认地址
repo-agent config set DEEPSEEK_API_KEY # 隐藏输入 API Key，不把 Key 放在命令行中
repo-agent config model                # 选择供应商、填写模型和 Key，保存后退出
```

`repo-agent config` 等同于 `config show`。显示时 Key 仅标注已设置或未设置；空配置项会说明其默认行为和来源。生效视图不包含另一次启动额外传入的参数，也不表示已运行会话中的临时设置；需要查看保存的原始值时使用 `--user`。

`edit` 显示配置项供选择，回车保留原值，输入 `:empty` 清空；Ctrl+C 取消。`set` 检查数值、枚举和 JSON 等基础格式，只更新用户 `.env`，保留其他配置和注释；API Key 通过隐藏输入保存。参数间及模型特有的兼容性仍在启动或模型调用时检查。保存后重启 Agent 生效，当前会话切换模型仍使用 `/model`。如果保存的用户值被项目或环境变量覆盖，命令会提示来源。`--root` 只用于判断覆盖关系，不会把改动写入项目配置。

## 模板与内置默认值

首次安装复制当前 `.env.example`；重复安装不覆盖用户文件。只有未配置或显式留空时才回退到内置默认值。当前模板与内置值有以下差异：

| 配置项 | 当前安装模板 | 内置默认值 |
| --- | --- | --- |
| `AGENT_MAX_STEPS` | `50` | `8` |
| `AGENT_MAX_OUTPUT_TOKENS` | `51200` | `4096` |
| `AGENT_SANDBOX_WRITEBACK` | `manual` | `manual` |

这些值不是模型能力声明；输出上限仍需符合实际模型限制。已有配置可能与两列都不同，使用 `repo-agent config show` 查看生效值和来源。模板变更不会自动同步到已有用户配置。

## 在 Agent 中设置和切换模型

交互终端启动时，若缺少模型名或 API Key，会在创建沙箱前自动进入设置向导。已有完整配置则直接进入会话；希望启动时重新选择，可以运行：

```bash
repo-agent --configure-model
```

向导从项目当前支持的供应商列表中选择（可输入序号或供应商名称），模型名称手动填写，API Key 隐藏输入。已配置同一供应商时，模型名和 Key 可以回车保留；Ctrl+C 取消。向导中的模型名称仍需手动输入，配置命令保存时不发出 API 请求。进入会话或会话内切换模型时，会查询模型元数据以获取上下文上限；查询失败不影响使用，生成权限和 Key 是否有效仍由实际模型调用验证。

会话中输入 `/model` 可再次打开相同向导。仅在当前任务结束后切换；全屏界面底部显示当前供应商和模型。设置成功后立即生效，并保存到用户 `.env`，作为以后启动的默认设置。用户配置以权限 `600` 原子更新，保留其他供应商的 Key、注释及无关设置；Key 不进入对话、任务历史或追踪日志。取消或保存失败时，当前模型和上下文保持不变。

切换成功后清空模型对话上下文，已完成的文件修改、沙箱副本和会话 token 总计保留。思考设置恢复目标模型在该接口上保存的偏好（没有偏好时为 `auto`），清除旧模型的原生额外参数及旧上下文窗口上限，并重新查询新模型的上限，避免把旧模型的特有参数带到新模型。切换供应商时恢复新供应商默认 API 地址；同一供应商保留当前自定义地址。需要自定义地址时仍可编辑用户配置或通过 `--base-url` 指定。

会话中的显式选择立即作用于本次会话；下次启动仍遵循上述配置优先级，项目 `.env`、环境变量或命令行参数可以覆盖保存的用户默认值。非交互输入不会询问或明文读取 Key，缺少配置时给出文件路径；可在终端完成设置后再用于脚本。

默认卸载保留用户配置；`./uninstall.sh --purge` 才会清理未共享的用户配置和 Key，详见[安装与卸载](installation.md)。

## 配置参考

所有已接入的运行设置都列在安装目录的 `.env.example` 中。首次安装将模板复制为用户配置；重复安装保留文件内容。升级后可对照模板手动补充新增选项，未配置的选项使用内置默认值。项目 `.env` 是可选覆盖文件，只需填写与默认配置不同的选项。配置在启动时读取，修改后需要退出并重新启动；不是会话内热更新。

| 配置项 | 内置默认值（未配置时） | 作用 |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | `deepseek` / 必填 | 厂商与模型 ID |
| `LLM_BASE_URL` | 空 | 空值使用厂商预设地址 |
| `LLM_CONTEXT_WINDOW` | 空 | 留空从服务端自动获取；正整数手动覆盖，仅用于显示，不会压缩历史 |
| `AGENT_MAX_STEPS` | `8` | 每条用户任务最多调用模型的轮数；`0` 表示无上限 |
| `AGENT_MAX_OUTPUT_TOKENS` | `4096` | 每次模型请求的输出 token 上限 |
| `AGENT_MAX_RECOVERIES` | `2` | 连续输出截断的额外恢复次数，文字/工具共享计数；0 关闭；正常工具轮完成后重置 |
| `AGENT_RECOVERY_MAX_OUTPUT_TOKENS` | 留空 | 工具参数重生成时的输出额度上限；每次翻倍至此值。留空沿用原额度；显式值须不低于原额度且符合模型限制 |
| `LLM_TEMPERATURE` | 空 | 不传温度，使用模型默认行为；显式值需符合模型限制 |
| `LLM_TOOL_CHOICE` | `auto` | `auto` 自主选择；`none` 不调用工具；`required` 每轮强制调用工具，可能导致任务达到轮数上限 |
| `LLM_THINKING` | `auto` | 不指定开关；也可选 `enabled`、`disabled`；Claude 另支持 `adaptive` |
| `LLM_REASONING_EFFORT` | 空 | 可选思考强度，支持范围见 [思考设置](thinking.md) |
| `LLM_THINKING_BUDGET` | 空 | 可选思考 token 预算，必须是正整数 |
| `LLM_THINKING_HISTORY` | `auto` | GLM 历史思考保留：auto / on / off |
| `LLM_THINKING_RECALL` | `true` | 是否自动保存并恢复每个模型的交互思考偏好 |
| `LLM_THINKING_PROFILE` | `{}` | 当前模型能力覆盖 JSON，见 [思考设置](thinking.md) |
| `AGENT_THINKING_DISPLAY` | `collapsed` | 思考区块显示：collapsed / expanded / hidden |
| `LLM_STREAM` | `true` | 默认流式接收；`--no-stream` 关闭 |
| `LLM_TIMEOUT` | `300` | 首批及相邻网络数据读取等待，单位秒，不是整个任务的总时限 |
| `LLM_CONNECT_TIMEOUT` | `10` | 建立连接的最长等待秒数 |
| `LLM_WRITE_TIMEOUT` | `30` | 发送请求数据的最长等待秒数 |
| `LLM_POOL_TIMEOUT` | `10` | 获取连接池连接的最长等待秒数 |
| `LLM_MAX_RETRIES` | `2` | 对可重试 HTTP 错误的额外重试次数，`0` 关闭，最大 `10`；连接错误和超时仍不自动重试 |
| `LLM_RETRY_DELAY` / `LLM_MAX_RETRY_DELAY` | `0.5` / `30` | 重试基础间隔与最大间隔，单位秒 |
| `AGENT_SYSTEM_PROMPT` | 空 | 空值使用内置提示词；非空的单行文本替换它 |
| `AGENT_LOG_DIR` | 项目 `logs/` | 运行片段追踪日志；不改变连续会话日志或快照位置 |
| `AGENT_SANDBOX_WRITEBACK` | `manual` | 仅 Docker：`manual` 手动回写；`on-success` 按检查结果自动回写；local/native 忽略此配置 |
| `AGENT_SANDBOX_VERIFY_COMMAND` | 空 | on-success 回写前的最终验证命令，使用 JSON 参数数组 |
| `LLM_EXTRA_JSON` | `{}` | 高级厂商原生请求参数，须为 JSON 对象，不允许覆盖统一字段或与显式思考配置冲突 |

内置提示词要求修改文件后简短汇报完成内容、文件路径、实际验证结果和必要的未完成事项，通常为 3 到 6 行；不重复粘贴已写入文件的完整代码，工具调用前也不先展示整份实现。用户明确要求代码或详细解释时仍可展开，传给工具的代码参数始终保持完整。使用默认风格时将 `AGENT_SYSTEM_PROMPT` 留空；自定义提示词会替换这些规则，需要自行加入同样的简洁要求。修改配置或升级默认提示词后重新启动会话生效。

普通设置遵循上文优先级。例如下面的临时参数会覆盖项目及用户 `.env`：

```bash
repo-agent --thinking enabled --max-steps 12 --max-output-tokens 8192 --timeout 180
```

## 思考配置

`LLM_THINKING=auto` 表示不指定思考开关，不保证关闭思考；单独填写强度或预算仍可能发送这些参数。强度、预算、历史保留和显示方式是独立设置，支持范围取决于当前模型。

交互命令、按模型保存的偏好、档位及能力覆盖统一见[思考设置](thinking.md)。内置规则由 [llm/thinking_catalog.py](../llm/thinking_catalog.py) 维护，协议映射位于 [llm/thinking.py](../llm/thinking.py)。不支持或冲突的组合会报错，服务端仍决定实际可用能力。

完整配置模板见 [`.env.example`](../.env.example)，回写条件见 [Sandbox](../sandbox/README.md#自动回写配置与恢复)。

## 校验与恢复用户配置

```bash
repo-agent config validate              # 校验当前目录的配置和合并后的设置
repo-agent config validate --user       # 只校验用户配置
repo-agent config unset LLM_TIMEOUT     # 删除此项用户覆盖值
repo-agent config backup                # 手动备份，当前文件损坏时也可以使用
repo-agent config backups               # 列出备份名（新到旧），不显示内容
repo-agent config restore <备份名>       # 恢复指定备份
repo-agent config reset                 # 恢复安装目录 .env.example 模板
```

`validate` 检查格式、数值范围、供应商思考参数及额外 JSON 的组合冲突，也提示未知字段、重复字段和缺少模型/Key。无效格式或参数返回 1；提示性问题返回 0。它不请求模型 API，因此不能验证账户权限、余额或远端模型是否存在。`unset` 删除用户文件中的条目，与将值设置为空不同；项目配置和环境变量仍按既有优先级生效。

通过配置命令或模型向导修改已有用户配置时，旧内容会自动备份到同目录 `.env.backups/`；目录权限 `700`，备份及新配置权限 `600`。备份包含历史 API Key，不在终端输出内容，也不会自动过期。

`restore` 只接受 `backups` 列出的备份名，恢复前校验备份格式和参数组合，并先备份当前文件。当前 `.env` 格式错误也可以恢复。`reset` 同样先备份，包括损坏文件的原始内容，再复制 `.env.example`；默认模板中的模型和 Key 为空，需要重新配置。没有可用备份时可用此入口恢复。

这些操作只修改用户配置，不改项目 `.env` 或进程环境变量；已运行的会话不受影响，下次启动生效。默认卸载保留备份；`uninstall.sh --purge` 在配置不被共享时一并清理受管配置备份，保留备份目录中的其他文件。

## 思考内容显示偏好

`AGENT_THINKING_DISPLAY=collapsed` 为默认值；也支持 `expanded`、`hidden`，启动参数为 `--thinking-display`。该项为用户级界面偏好，交互命令 `/thinking display ...` 和 Ctrl+T 自动保存到用户 `.env`，不写入按模型保存强度的 `thinking.json`。显示命令可以在生成中生效，详细行为见 [思考显示](thinking.md#显示思考内容)。

## 数据目录与启动参数

| 设置 | 默认位置 / 作用 |
| --- | --- |
| `AGENT_CONFIG_DIR` | 覆盖用户配置目录；未设置时使用 `${XDG_CONFIG_HOME:-~/.config}/repo-agent` |
| `AGENT_ENV_FILE` | 覆盖项目配置文件；未设置时读取项目根目录 `.env` |
| `XDG_STATE_HOME` | 用户状态基目录，默认 `~/.local/state`；包含会话和安装登记 |
| `AGENT_LOG_DIR` | 运行片段日志；显式相对路径以调用目录为准，留空使用项目 `logs/` |

配置目录和状态目录变量在启动前设置，不属于 `.env` 中的运行参数。`--new-session`、`--name`、`--root` 是启动选项；会话名称和上下文保存在会话状态中，不写入模型配置。完整存储结构见[会话管理](sessions.md#保存位置)。
