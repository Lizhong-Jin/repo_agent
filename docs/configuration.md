# 配置与思考模式

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 用户配置（推荐）

运行 `./install.sh` 会将仓库 `.env.example` 复制为 `~/.config/repo-agent/.env`，安装过程无需输入模型或 API Key，也不会把环境变量中的密钥写入文件。首次启动前编辑此文件，填写 `LLM_PROVIDER`、`LLM_MODEL` 和对应 API Key；不需要复制到每个项目。重复安装保留已有文件，包括空白或尚未完成的配置；修改后重启 Agent 生效。

`repo-agent` 和 `python -m cli.main` 都会加载用户配置及工作目录 `.env`。`--root` 同时选择工作目录和项目配置位置；`AGENT_ENV_FILE` 可显式指定项目配置文件（相对路径以调用目录为准），替代默认项目 `.env`，用户配置仍作后备。指定的文件不存在时会报错。

优先级为 **命令行参数 > 已有非空环境变量 > 项目 `.env` > 用户 `.env` > 内置默认值**。项目的显式空值清除同名用户配置，例如 `LLM_BASE_URL=` 恢复厂商默认端点。所有配置都按字面量解析，不执行 shell 命令或变量展开。

用户配置目录默认为 `${XDG_CONFIG_HOME:-~/.config}/repo-agent`；也可在启动前设置 `AGENT_CONFIG_DIR` 指定目录，配置文件名始终为 `.env`。安装目录 `.env.defaults` 不会自动迁移或加载；已有用户可以将需要的值手动转入用户配置。

## 项目默认配置（兼容旧版初始化）

在安装目录创建或编辑 **`.env.defaults`**，用来保存你常用的模型、思考设置、超时等默认值。它只在 `--init` 时使用，不参与项目每次启动时的动态配置读取。

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=你常用的模型ID
LLM_THINKING=enabled
AGENT_MAX_STEPS=12
AGENT_MAX_OUTPUT_TOKENS=8192
LLM_TIMEOUT=180
```

默认文件可以只保留需要覆盖的字段；其他字段由 `.env.example` 补齐。初始化优先级为 **已有项目 `.env` > 安装目录 `.env.defaults` > `.env.example`**，已有空值也会保留。默认文件不存在时继续使用 `.env.example`。这样升级后新增的配置项仍可获得默认值，同时不会改动已有项目的选择。

编辑默认值后，在新任务目录执行安装目录的 `run_agent.sh --init` 即可。修改 `.env.defaults` 不会同步覆盖已经创建的项目；对旧项目再次 init，也只会补齐缺少的字段。建议将手动创建的文件权限设为 `600`；该文件已被现有 `.gitignore` 规则忽略；如填写 Key，会随默认值复制到新项目的 `.env`。不要把安装目录的 `.env` 当作全局默认配置。

`.env.defaults` 使用与项目 `.env` 相同的字面量 `KEY=VALUE` 格式，支持成对引号、可选 `export` 和整行注释，不执行命令或变量展开；未知、重复字段及格式错误会在创建项目文件之前报错，错误信息不包含配置值。

## 配置参考

所有已接入的运行设置都列在安装目录的 `.env.example` 中。新项目初始化时会生成完整 `.env`；旧任务目录在升级 Agent 后执行 `./run_agent.sh --init`，即可补充新增配置项。已有值不变，新增项保持默认行为。配置在启动时读取，修改后需要退出并重新启动；不是会话内热更新。

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | `deepseek` / 必填 | 厂商与模型 ID |
| `LLM_BASE_URL` | 空 | 空值使用厂商预设地址 |
| `LLM_CONTEXT_WINDOW` | 空 | 模型上下文窗口上限，仅用于显示，不会压缩历史 |
| `AGENT_MAX_STEPS` | `8` | 每条用户任务最多调用模型的轮数 |
| `AGENT_MAX_OUTPUT_TOKENS` | `4096` | 每次模型请求的输出 token 上限 |
| `LLM_TEMPERATURE` | 空 | 不传温度，使用模型默认行为；显式值需符合模型限制 |
| `LLM_TOOL_CHOICE` | `auto` | `auto` 自主选择；`none` 不调用工具；`required` 每轮强制调用工具，可能导致任务达到轮数上限 |
| `LLM_THINKING` | `auto` | 不指定开关；也可选 `enabled`、`disabled`；Claude 另支持 `adaptive` |
| `LLM_REASONING_EFFORT` | 空 | 可选思考强度，支持范围见下表 |
| `LLM_THINKING_BUDGET` | 空 | 可选思考 token 预算，必须是正整数 |
| `LLM_STREAM` | `true` | 默认流式接收；`--no-stream` 关闭 |
| `LLM_TIMEOUT` | `300` | 首批及相邻网络数据读取等待，单位秒，不是整个任务的总时限 |
| `LLM_CONNECT_TIMEOUT` | `10` | 建立连接的最长等待秒数 |
| `LLM_WRITE_TIMEOUT` | `30` | 发送请求数据的最长等待秒数 |
| `LLM_POOL_TIMEOUT` | `10` | 获取连接池连接的最长等待秒数 |
| `LLM_MAX_RETRIES` | `2` | 对可重试 HTTP 错误的额外重试次数，`0` 关闭，最大 `10`；连接错误和超时仍不自动重试 |
| `LLM_RETRY_DELAY` / `LLM_MAX_RETRY_DELAY` | `0.5` / `30` | 重试基础间隔与最大间隔，单位秒 |
| `AGENT_SYSTEM_PROMPT` | 空 | 空值使用内置提示词；非空的单行文本替换它 |
| `AGENT_LOG_DIR` | `logs` | 终端对话与执行追踪日志目录 |
| `AGENT_SANDBOX_WRITEBACK` | `manual` | `manual` 手动回写；`on-success` 按检查结果自动回写 |
| `AGENT_SANDBOX_VERIFY_COMMAND` | 空 | on-success 回写前的最终验证命令，使用 JSON 参数数组 |
| `LLM_EXTRA_JSON` | `{}` | 高级厂商原生请求参数，须为 JSON 对象，不允许覆盖统一字段或与显式思考配置冲突 |

内置提示词要求修改文件后简短汇报完成内容、文件路径、实际验证结果和必要的未完成事项，通常为 3 到 6 行；不重复粘贴已写入文件的完整代码，工具调用前也不先展示整份实现。用户明确要求代码或详细解释时仍可展开，传给工具的代码参数始终保持完整。使用默认风格时将 `AGENT_SYSTEM_PROMPT` 留空；自定义提示词会替换这些规则，需要自行加入同样的简洁要求。修改配置或升级默认提示词后重新启动会话生效。

普通设置遵循上文优先级。例如下面的临时参数会覆盖项目及用户 `.env`：

```bash
repo-agent --thinking enabled --max-steps 12 --max-output-tokens 8192 --timeout 180
```

## 思考配置

`.env` 中可以这样启用思考并增加输出上限：

```dotenv
LLM_THINKING=enabled
LLM_REASONING_EFFORT=
LLM_THINKING_BUDGET=
AGENT_MAX_STEPS=12
AGENT_MAX_OUTPUT_TOKENS=8192
LLM_TIMEOUT=180
```

此示例可用于支持思考开关的 DeepSeek 模型。Claude 手动思考还需填写预算；各模型是否支持参数由服务端确认。`auto` 的含义是“不发送思考开关”，并不保证关闭思考；只要填写了强度或预算，仍会发送这些参数。

| 厂商 | 当前映射方式 |
| --- | --- |
| OpenAI / ChatGPT | `enabled` 发送 `reasoning.effort`，未填强度时使用 `medium`；`disabled` 发送 `none`。没有独立预算映射。各模型支持的强度不同，有些不能关闭思考。 |
| Claude | `enabled` 是手动模式，预算必填且满足 `1024 <= budget < 输出上限`；`adaptive` 不填写预算。强度通过 `output_config.effort` 传递，接受 `low/medium/high/max`，具体模型可能只支持其中一部分。 |
| Gemini | 预算映射到 `thinkingBudget`，强度映射到 `thinkingLevel`，不能同时填写。强度接受 `minimal/low/medium/high`，由模型决定具体支持范围。启用且未指定强度或预算时，Gemini 3 使用 `high`，其他模型使用动态预算 `-1`。不能把降低思考强度等同于完全关闭思考。 |
| Qwen | 开关映射到 `enable_thinking`，预算映射到 `thinking_budget`；不映射强度。 |
| DeepSeek / GLM | 开关映射到 `thinking.type`，强度映射到 `reasoning_effort`；不映射独立预算。 |
| Kimi / 豆包 | 映射 `thinking.type` 开关，不映射强度或独立预算。 |
| MiniMax | 当前没有通用开关映射，使用 `auto` 并将强度、预算留空；非默认设置会报错。 |

统一思考配置在 `llm/thinking.py` 转换；`AgentRuntime` 在每一轮请求中传递温度、工具选择和转换后的参数。明确不支持的映射或相互冲突的配置会报错，不会悄悄丢弃。模型级别的支持范围由 API 最终验证；本项目不会自动更换模型 ID。默认使用流式接收，支持只接受流式调用的模型。

参数映射依据：[OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Claude 手动思考](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)、[Claude 自适应思考](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)、[Gemini thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)、[Qwen 深度思考](https://help.aliyun.com/en/model-studio/deep-thinking)、[DeepSeek thinking](https://api-docs.deepseek.com/guides/thinking_mode/)、[GLM 深度思考](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)、[Kimi 官方项目](https://github.com/MoonshotAI/Kimi-K2.5)。

完整模板见 [`.env.example`](../.env.example)，回写条件见 [Sandbox 使用说明](../sandbox/README.md)。
