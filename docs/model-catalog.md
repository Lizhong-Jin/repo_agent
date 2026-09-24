# 统一模型目录

[`llm/model_catalog.py`](../llm/model_catalog.py) 是项目支持模型信息的唯一来源。思考设置、会话/启动模型选择器和上下文长度回退都使用它。后续模块请调用 `model_info(provider, model)`，不要在自己的代码里按模型名称猜能力或复制长度表。

## 字段与口径

| 字段 | 含义 |
| --- | --- |
| `provider` / `id` | 规范供应商名和发送给 API 的模型 ID；保留 MiniMax 等 ID 的大小写 |
| `thinking` | 可用 `modes`、按强度递增的 `efforts`、`default_effort`、`effort_mode`、`budget_min/max`、`aliases`、`history` |
| `min_thinking` / `max_thinking` | 从思考规则派生的最低/最高设置；可关闭时最低为 `disabled`，强制思考时取最低强度/预算；未知预算上限为 `budget:unknown` |
| `context_window` / `context_kind` | token 上限与口径：`context` 是总窗口；`input` 仅限制输入，不能与输出上限简单相加 |
| `max_output_tokens` | 标准 API 的最大输出；不是每次请求的默认值，也不承诺这么多可见正文（可能包含思考） |
| `sources` / `checked_on` / `notes` | 官方来源、核对日期及端点、地区、未核实信息的说明 |
| `selectable` | 是否出现在交互选择列表中；已知下线条目可保留历史解析但不可选择 |
| `dated_snapshots` | 历史配置可继承日期快照的思考规则；未登记快照不继承长度、不进入列表 |
| `always_thinking` | 模型始终思考但当前适配只提供默认模式时，准确描述最低/最高状态 |

`None` 表示尚未核实，不等于零或无限。`auto` 是接口默认行为，不是最低思考强度；仅支持 `auto` 且未声明始终思考时，不额外推断实际推理行为。预算上限未知的旧 Claude 型号仍要求预算小于本次请求的输出额度。

本轮核对日期为 **2026-09-24**。数据依据各模型记录中的官方文档，Qwen 采用百炼中国内地标准接口规格。别名、地区和账户可用性会变化；保留项目已有的思考适配并不代表这些变体都已核实长度。Gemini 3 Pro Preview 的官方下线状态已单独标记。原目录空缺的 DeepSeek、Moonshot、MiniMax 补入当前适配可用的模型；未实现的新版协议控制不作为可用思考选项。

## 读取与扩展

```python
from llm.model_catalog import model_info, supported_models, supported_providers

info = model_info("openai", "gpt-5.2")
print(info.min_thinking, info.max_thinking)
print(info.context_window, info.max_output_tokens)
```

`model_info` 默认只精确匹配已登记 ID（大小写不敏感），返回独立副本或 `None`。`supported_models` 只返回可选条目；`supported_providers` 由这些条目派生。`require_supported_model` 用于选择与保存边界，阻止任意文本或未登记快照进入切换流程。低层 LLM 客户端仍允许现有自定义配置，未知模型可使用显式思考能力覆盖；交互向导只能选择目录内模型。

新增模型在本文件调用 `_register`，同一组可共享思考规则，每行分别提供 `(模型 ID, 上下文上限, 最大输出)`：

```python
_register(
    "openai",
    [("your-verified-model-id", None, None)],
    thinking={"modes": ("auto", "enabled"), "efforts": ("low", "high")},
    source="https://your-official-model-documentation",
    notes="示例；实际登记前须核对接口与参数能力。",
)
```

数据不清楚时使用 `None` 并注明原因，不继承同系列模型的限制；更新后重启 agent。标准 API 最大输出不会自动提高 `AGENT_MAX_OUTPUT_TOKENS` 或恢复请求额度，这些仍由用户的请求预算设置决定。

## 会话行为

`/model` 和启动向导使用同一模型筛选组件。输入搜索词，↑/↓、鼠标滚轮或 PgUp/PgDn 浏览，Enter 选择；无匹配项不能提交。Key 仍隐藏输入，取消或保存失败保留旧模型和会话。

上下文上限优先级是 **手动设置 > 当前服务端元数据 > 内置模型目录**。目录来源在底栏明确标记；自定义网关有较低限制时可手动覆盖。不存在目录数据时仍显示未知，不会借用另一模型的上限。
