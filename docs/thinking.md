# 按模型设置思考

[返回 README](../README.md) · [配置参考](configuration.md)

输入 `/thinking` 或 `/thinking list` 查看当前模型的档位、预算范围、别名、历史思考保留方式和能力规则来源。只查看不会调用模型 API、修改偏好或发送探测请求。

```text
/thinking low                 # 直接选择强度；Claude 自适应模型自动使用 adaptive
/thinking on high             # 兼容原有命令
/thinking budget 2048         # 仅适用于支持固定预算的模型
/thinking history on          # GLM：显式保留历史思考
/thinking history off         # GLM：显式要求服务端清除跨轮思考
/thinking history auto        # GLM：恢复接口默认行为
/thinking auto                # 强度/预算恢复服务端默认；历史保留选择不变
/thinking reset               # 所有思考设置恢复默认，并忘记当前模型的已保存偏好
```

**Shift+Tab** 循环模型的合法预设，保留输入草稿。运行中的任务沿用原设置，结束后可切换。`auto` 不是“低强度”：GLM-5.3 默认强度为 max。固定预算和强度是不同控制方式，不能强行给每个模型提供相同的低/中/高档位。

## 显示思考内容

思考内容以独立区块显示，默认折叠。**Ctrl+T** 在展开与折叠之间切换，生成中也可以使用，保留输入草稿；处于 hidden 模式时按 Ctrl+T 会展开。**Shift+Tab** 仍控制下一次任务的思考强度，不控制显示。

```text
/thinking display             # 查看显示模式
/thinking display expanded    # 展开已收到的内容，并继续增量显示
/thinking display collapsed   # 只显示每个思考区块的标题、耗时和字符数
/thinking display hidden      # 隐藏区块，底部仍显示运行状态
```

这些显示命令也可在任务运行期间使用。折叠与隐藏只改变界面，不丢弃已收到内容，不重新调用模型，不改变工具执行或回传给模型的原生状态。任务中断时保留已显示/已接收的片段，并提示任务未完成。

显示偏好保存到用户配置目录 `.env` 的 `AGENT_THINKING_DISPLAY`（默认路径 `~/.config/repo-agent/.env`，可由 `AGENT_CONFIG_DIR` 改变）。它适用于所有模型，和按模型保存强度的 `thinking.json` 分开。启动优先级仍为命令行 > 环境变量 > 项目配置 > 用户配置 > 默认；有更高优先级覆盖时，重启按覆盖值生效。

```bash
repo-agent --thinking-display expanded
repo-agent config set AGENT_THINKING_DISPLAY collapsed
```

流式显示与用量：

- 状态区区分等待响应、正在思考、正在输出正文；生成期间显示本次请求经过的时间和已接收思考字符数。
- 思考字符数不是 token 数。请求结束后，单独显示服务端报告的输入、输出及其中思考 token；缺失字段显示“未返回”。思考 token 已包含在输出中，不重复相加。
- 每次调用的时间行包含“首段思考”，原“首字”仍仅指正文；折叠/隐藏不会改变时间统计。
- `--no-stream` 也能显示响应中可读的思考内容，但须等完整响应返回，因此无法观察生成中的进度。
- 普通文本终端和单次任务模式支持 expanded/ collapsed/ hidden 配置；已打印的内容无法回收折叠，Ctrl+T 的即时折叠仅适用于完整会话界面。

供应商返回什么，就显示什么：GLM 等兼容接口读取 `reasoning_content`（也兼容字符串 `reasoning`、`reasoning_text`）；Claude 读取 `thinking_delta`；OpenAI Responses 读取推理摘要；Gemini generateContent 读取标记为 `thought` 的文本。这些内容可能只是摘要，不能把可见字符数当作完整内部思考量。

CLI 为已登记且适用的模型请求可读摘要：OpenAI 添加 `reasoning.summary=auto`，启用思考的 Claude 添加 `thinking.display=summarized`，Gemini 添加 `includeThoughts=true`。不会自动提高强度或启用原本关闭的思考；用户显式原生设置优先。未知模型不试探摘要参数，可使用 `LLM_EXTRA_JSON` 配置实际接口支持的字段。库用户可通过 `LLMConfig(include_thinking=True)` 开启这项摘要请求适配。

为了运行中随时展开，collapsed/hidden 也继续接收可读思考内容。显示隐藏不会节省模型的思考 token；请求服务端提供摘要本身可能影响响应耗时。只有签名、加密状态或思考用量，没有可读文本时，界面提示“接口未提供可显示的思考内容”（hidden 模式不显示此提示）。签名及加密内容只用于原生上下文回传，不渲染。追踪日志只保存时间、字符数和用量，不记录思考正文；会话恢复快照可保存模型原生状态和界面已经接收的思考区块，折叠/隐藏不等于从快照删除。

界面按约 50ms 合并流式事件；已完成文字区块缓存显示内容，折叠时不会展开拼接长篇思考。显示用的控制字符清理不修改回传的模型状态。

## 能力规则

下表描述仓库当前内置规则，不代表服务端实时能力列表。统一规则用于命令行、配置校验、会话命令、快捷键和模型切换，避免界面与请求参数不一致。

| 模型例子 | 预设及限制 |
| --- | --- |
| GLM-5.3 / Flash | auto、low、high、max；不能关闭思考；minimal/medium/xhigh 转换为 low/high/max |
| GLM-5.2 | auto、off、high、max；low/medium 转换为 high，xhigh 转换为 max |
| GLM-4.7 / GLM-5 / GLM-5.1 | auto、off、on；不发送未经确认的强度参数 |
| GPT-5 | auto、minimal、low、medium、high；不提供 off |
| GPT-5.2 | auto、off、low、medium、high、xhigh |
| Claude Sonnet 4.6 / Opus 4.6 | auto、off、自适应强度；Opus 提供 max；固定预算不可用 |
| Claude Sonnet 4.5 等手动思考模型 | auto、off、预算预设；预算至少 1024 且小于输出上限 |
| Gemini 3 Pro Preview / 3 Flash Preview | 分别提供 low/high 与 minimal/low/medium/high；不能关闭思考 |
| Gemini 2.5、已登记的 Qwen 混合思考模型 | 预算预设，按各模型的预算范围、输出上限及开关能力过滤 |
| 未登记模型 | 快捷键仅 auto；手动命令仍可使用厂商通用映射，也可显式声明能力 |

未知模型的状态栏显示“通用厂商规则（模型能力未确认）”。模型列表接口没有统一的思考能力字段，本项目不把上下文长度信息当作思考能力，不用付费请求试探参数是否可用。规则按供应商匹配模型及已知日期快照，不猜测未来版本；自定义网关可能改变模型行为，需按实际接口覆盖能力。

## 维护内置模型字典

所有内置模型信息集中在 [`llm/model_catalog.py`](../llm/model_catalog.py) 的 `MODEL_CATALOG`；[`llm/thinking_profiles.py`](../llm/thinking_profiles.py) 从目录读取思考规则，再应用用户能力覆盖。

统一目录同时保存模型 ID、思考模式/强度/预算/默认值、上下文口径及上限、最大输出、来源和核对日期。最低和最高思考设置从模式、强度顺序和预算范围派生，避免单独维护后发生矛盾。新增模型时在该文件调用 `_register`；完整字段和使用入口见[模型目录](model-catalog.md)。

`dated_snapshots=True` 为历史配置保留 `-YYYY-MM-DD` 和 `-YYYYMMDD` 后缀的思考规则匹配，精确登记优先。未单独登记的日期快照不继承长度限制，也不会出现在选择列表中。用户能力覆盖仍优先，偏好继续保存在用户配置目录的 `thinking.json`。新增供应商协议仍需实现对应适配器。

## 历史思考保留

GLM 的 `/thinking history on` 发送 `thinking.clear_thinking=false`，`off` 发送 `true`，`auto` 不发送该字段。项目完整保留并回传原生思考内容，不重排、不修改，不把思考正文混入用户可见回复。

普通智谱 API 默认不启用跨轮思考保留，Coding Plan 默认启用；自定义接口显示“由接口决定”。保留思考可能减少后续用户轮次重复推理，但会增加保留的上下文，不保证加速，也不能消除全新任务第一次调用的长思考。历史保留开关与思考强度独立，切换强度不会重置它。

## 保存和优先级

默认交互修改自动保存到用户配置目录的 `thinking.json`。按规范化供应商、完整 API 基础地址（忽略末尾斜杠）和原始模型 ID 分开保存；普通 API 与 Coding Plan、不同网关互不影响。仅保存思考设置和能力覆盖，不保存 Key、用户消息或模型思考正文。文件以 0600 权限原子替换，保存失败时本次设置不生效，原设置和文件保留。

启动优先级：

1. 显式思考命令行参数：`--thinking`、`--reasoning-effort`、`--thinking-budget`、`--thinking-history`、`--thinking-profile` 中任一项出现，本次整组设置不读取偏好。
2. 合并配置中的非默认思考设置或原生思考覆盖。
3. 当前供应商＋接口＋模型保存的偏好。
4. 服务端默认。

配置模板里的 `auto`、空强度、空预算、`{}` 是默认值，允许恢复偏好。需要明确忽略偏好时使用 `--thinking auto` 或 `--thinking-recall false`。自动恢复不会写回配置，命令行临时覆盖也不会自动覆盖偏好；只有交互切换会保存。会话内 `/model` 切换会清除旧模型配置，并恢复目标模型的偏好。

```dotenv
LLM_THINKING=auto
LLM_REASONING_EFFORT=
LLM_THINKING_BUDGET=
LLM_THINKING_HISTORY=auto
LLM_THINKING_RECALL=true
LLM_THINKING_PROFILE={}
```

对应配置命令示例：

```bash
repo-agent config set LLM_THINKING_HISTORY on
repo-agent config set LLM_THINKING_RECALL false
```

`history on` 只适用于支持该能力的模型，不应作为多个不同厂商共用的全局设置。偏好损坏或已不满足当前输出上限时明确报错，不静默降档；可临时用 `--thinking auto` 绕过旧选择并用 `/thinking reset` 忘记当前模型偏好。整个文件损坏时可关闭 recall 后启动，再修复文件。`config reset` 重置 .env，独立模型偏好由 `/thinking reset` 管理。默认卸载保留偏好；`uninstall.sh --purge` 在配置未被其他安装共享时同时删除思考偏好。

## 自定义模型和网关

`LLM_THINKING_PROFILE` 是能力覆盖，不是直接透传的 API 请求体。例如为当前模型声明只支持 low/high：

```bash
repo-agent config set LLM_THINKING_PROFILE '{"modes":["auto","enabled"],"efforts":["low","high"],"aliases":{"medium":"high"}}'
```

支持字段：`modes`、`efforts`、`effort_mode`（enabled/adaptive）、`budget_min`、`budget_max`、`history`、`default_effort`、`aliases`。未填写的字段继承内置规则，未知模型初始仅有 auto；显式设为空列表、空对象或 null 可清除对应约束。覆盖 efforts 时也应调整 aliases，所有别名必须指向有效档位。该配置只适用于当前模型；交互修改时随模型偏好保存，切换到其他模型不继承。

能力声明不会添加新的供应商协议映射；例如只有智谱映射了 clear_thinking，不能给其他供应商声明 history 来伪造该能力。高级原生参数继续使用 `LLM_EXTRA_JSON`；它包含思考参数时禁止快捷键改写，先移除原生覆盖再使用统一控制。

规则依据：[GLM 深度思考](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)、[GLM 历史思考保留](https://docs.z.ai/guides/capabilities/thinking-mode)、[OpenAI GPT-5](https://developers.openai.com/api/docs/models/gpt-5)、[GPT-5.2](https://developers.openai.com/api/docs/models/gpt-5.2)、[Claude 思考控制](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)、[Gemini 思考](https://ai.google.dev/gemini-api/docs/thinking)、[Qwen 思考](https://help.aliyun.com/zh/model-studio/deep-thinking)。内置能力不会自动联网更新，服务端新增规则可先用能力覆盖适配。
