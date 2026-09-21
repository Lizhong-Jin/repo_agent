# 统一模型接口

[返回 README](../README.md)

本文中的命令默认在 Agent 安装目录执行；任务项目目录会单独注明。

## 已实现的统一接口

- 文本多轮对话：`system / user / assistant / tool`。
- 工具定义、多个工具调用、工具执行结果与错误回传。
- 同步 `LLMClient` 与异步 `AsyncLLMClient`，均调用 `generate(request)`。
- 统一文本、工具参数、结束原因、token 用量及异常类型。
- 保留厂商原生状态，支持工具调用续接和对话序列化。
- 流式正文和可见思考事件、模型元数据查询及思考参数适配。
- 超时、有限次数的限流／服务端错误重试，以及可注入的 HTTP 客户端。

## 接入模型

| provider | 别名 | 协议 | API Key 环境变量 |
| --- | --- | --- | --- |
| `deepseek` | — | Chat Completions | `DEEPSEEK_API_KEY` |
| `qwen` | — | 阿里云百炼 OpenAI 兼容 | `DASHSCOPE_API_KEY` |
| `moonshot` | `kimi` | Chat Completions | `MOONSHOT_API_KEY` |
| `zhipu` | `glm` | Chat Completions | `ZHIPU_API_KEY` |
| `doubao` | — | 火山方舟 Chat Completions | `ARK_API_KEY` |
| `minimax` | — | OpenAI 兼容 | `MINIMAX_API_KEY` |
| `openai` | `chatgpt` | OpenAI Responses | `OPENAI_API_KEY` |
| `anthropic` | `claude` | Anthropic Messages | `ANTHROPIC_API_KEY` |
| `gemini` | — | Google generateContent | `GEMINI_API_KEY` |

这里的 `chatgpt` 是 OpenAI API 的别名，使用独立的 API Key，不是 ChatGPT 网页会话或订阅登录。

模型 ID 由调用方填写，不自动替换；内置思考能力字典会对已登记模型进行参数校验。填写你账号实际可用且支持文本／工具调用的模型 ID；豆包也可按账号要求填写推理接入点 ID。厂商和具体模型的参数支持范围仍以官方接口为准。

```bash
export DEEPSEEK_API_KEY='你的 API Key'
python -m examples.llm_demo --provider deepseek --model '你的模型 ID'
```

同样可以使用 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 控制示例。库本身仅自动读取对应的 API Key 变量，其他配置通过 `LLMConfig` 显式传入。`.env.example` 是配置参考，库不会自动加载 `.env`。

## 最小调用

```python
from llm import LLMClient, LLMConfig, LLMRequest, Message

config = LLMConfig(provider="deepseek", model="你的模型 ID")
with LLMClient(config) as client:
    response = client.generate(LLMRequest(messages=[
        Message("system", "你是 Python 代码助手。"),
        Message("user", "解释一下生成器和迭代器的区别。"),
    ]))
    print(response.text)
    print(response.finish_reason)
    print(response.usage)
```

更换提供商时，改 `provider`、`model` 和对应凭证即可。`base_url` 可以覆盖地域或专用接入地址；填写带版本路径的 API 基址，**不要追加** `/chat/completions`、`/responses` 等方法路径。各厂商按量付费与套餐端点、地域和 Key 应匹配，预设以普通 API 端点为主。

异步调用使用相同请求与返回类型：

```python
from llm import AsyncLLMClient, LLMConfig, LLMRequest, Message

async def ask():
    async with AsyncLLMClient(LLMConfig("gemini", "你的模型 ID")) as client:
        return await client.generate(LLMRequest([Message("user", "解释这个项目的架构")]))
```

## 工具调用和上下文

```python
from llm import LLMClient, LLMConfig, LLMRequest, Message, ToolDefinition

tool = ToolDefinition(
    name="add", description="计算两个整数之和",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
)
history = [Message("user", "用 add 计算 1 + 2")]
with LLMClient(LLMConfig("deepseek", "你的模型 ID")) as client:
    response = client.generate(LLMRequest(history, tools=[tool]))
    if response.finish_reason == "tool_calls":
        history.append(response.to_message())
        for call in response.tool_calls:
            # 完整的参数校验、执行和结果回传见 examples/llm_demo.py。
            args = call.arguments
            valid = (call.name == "add" and set(args) == {"a", "b"}
                     and all(type(v) is int for v in args.values()))
            if valid:
                history.append(Message.tool_result(call, {"sum": args["a"] + args["b"]}))
            else:
                history.append(Message.tool_result(call, "工具或参数不合法", is_error=True))
        followup = client.generate(LLMRequest(history, tools=[tool]))
        print(followup.text)
```

本层只传递工具调用，不自动执行函数，也不验证参数是否满足完整 JSON Schema。它会验证参数是合法 JSON 对象；工具名称白名单、参数 schema 校验、权限和实际执行属于后续 `tools/` 与 `agent/runtime.py`。

仅在 `finish_reason == "tool_calls"` 时进入正常工具执行流程。`length` 可能含部分结果，`blocked` 表示被拦截，`other` 表示需要上层检查；不要把它们当作成功。`provider_finish_reason` 和 `raw` 保留原始信息。

必须使用 `response.to_message()` 回填历史，不能只用 `response.text` 重建 assistant 消息。封装分别保留：

- OpenAI 的完整 output items，包括 reasoning、加密内容和 message phase。
- Claude 的完整 content blocks，包括 thinking、redacted thinking 及签名。
- Gemini 的原始 parts 和 thought signatures；缺少原生调用 ID 时仅在本地生成关联 ID。
- 国内兼容接口返回的完整 assistant 消息，包括 `reasoning_content` / `reasoning_details` 等字段。

这些状态绑定请求使用的 provider 和 model。本层拒绝跨模型直接转发原生状态，也会检查 assistant 文本/工具参数是否在保留旧状态的同时被修改。库调用方切换模型时应建立新历史，或明确移除不兼容原生状态；payload 视为不透明数据，不手工修改。CLI 的 `/model` 清空模型上下文；重启时配置变更则保留通用消息并去除旧原生状态，见[会话恢复](sessions.md#保存恢复与新会话)。

可通过 `message.to_dict()` 和 `Message.from_dict(data)` 保存、恢复 JSON 对话历史。对话需从 user 开始，system 消息集中在开头，一次 assistant 的所有工具结果必须按原调用顺序回传完毕才能请求下一步。自定义调用方若并发执行工具，回传时仍须恢复原顺序，以兼容不带调用 ID 的 Gemini 响应；本项目 Runtime 当前顺序执行工具。

## 参数、用量和错误

`LLMRequest` 的公共参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `messages` | 必填 | 完整对话历史 |
| `tools` | 空 | 自定义函数工具，参数使用 object JSON Schema |
| `max_output_tokens` | 4096 | 映射到各厂商的输出 token 上限；思考 token 的消耗依模型定义 |
| `temperature` | `None` | 未设置时不发送，采用模型默认值 |
| `tool_choice` | `auto` | `auto`、`none`、`required`；智谱预设目前仅支持 `auto` |
| `extra` | `{}` | 厂商原生选项，不能覆盖公共字段或切换成流式、后台、多候选模式 |

不同模型对 temperature、思考预算、工具选择和 JSON Schema 的支持有差异。本层保留明确的错误，不偷偷删除参数或改用另一模型。

厂商专有选项示例（是否支持以所选模型文档为准）：

```python
# DeepSeek 等兼容接口的 thinking 选项
request = LLMRequest(history, extra={"thinking": {"type": "enabled"}})
# OpenAI Responses 的 reasoning 选项
request = LLMRequest(history, extra={"reasoning": {"effort": "medium"}})
# Gemini 的原生 generationConfig 与统一参数合并
request = LLMRequest(history, extra={"generationConfig": {
    "thinkingConfig": {"thinkingBudget": 1024}
}})
```

MiniMax 预设默认开启 `reasoning_split=True`，用于将思考内容从正文中分离；可通过 `extra` 覆盖。OpenAI 使用 `store=False` 和完整历史回传。

`Usage.input_tokens` 统一包含缓存读写；`output_tokens` 在厂商有报告时包含思考 token。Claude 的缓存读写会加回总输入，Gemini 的 thoughts 会加回总输出。缓存、思考的细分计数另外保存，**不能再次加进总数**。缺失值是 `None`；厂商未提供 total、但输入输出都已知时才计算 total。用量不等于费用，本层不硬编码价格。

统一异常包括 `ConfigurationError`、`InvalidRequestError`、`AuthenticationError`、`RateLimitError`、`ProviderError`、`LLMTimeoutError`、`LLMConnectionError` 和 `InvalidResponseError`。

默认连接超时 10 秒、读取超时 300 秒、写入超时 30 秒、连接池等待超时 10 秒。读取超时限制相邻网络数据间隔，不限制整次响应的总时长；对 429、500、502、503、504、529 最多重试两次，参考 Retry-After，最长单次等待 30 秒。超时／连接异常不自动重试，避免不确定状态下重复推理；异常响应、非法工具 JSON 也不重试。整个 Agent 的任务时间和成本预算由上层负责。HTTP 错误对象保留状态码和 request ID，不把响应正文或凭证写进异常消息。

## 流式事件与计时

同步和异步客户端都支持 `generate_with_events(request, callback)`，回调接收 `(kind, text, elapsed_seconds)`，应快速返回。流式解析与请求构造独立；`generate()` 同样返回完整 `LLMResponse`。`LLMConfig(stream=False)` 关闭流式请求；客户端也能解析返回普通 JSON 的兼容网关。

CLI 通过 `LLMConfig(include_thinking=True)` 为已适配的模型请求可读思考摘要。此设置不自动提高强度或启用被关闭的思考；显示、能力及偏好规则见[思考设置](thinking.md)。工具只在响应完成、参数拼接并校验成功后交给 Runtime 执行。

`model_end.model_call` 的主要计时字段：

| 字段 | 含义 |
| --- | --- |
| `first_data_seconds` | 首行响应数据到达，可能仅为 SSE 心跳 |
| `first_text_seconds` | 首个正文片段到达，不含思考文本 |
| `first_display_seconds` | 首次正文输出进入显示处理的时间 |
| `first_thinking_seconds` | 首段可读思考到达 |
| `response_seconds` | 本次完整模型响应耗时 |
| `thinking_characters` / `thinking_available` | 可见思考字符数、是否收到可读内容 |

计时从客户端本次调用开始，可包含 HTTP 重试等待。未发生的计时为 `null`；无正文的纯工具调用没有正文首字时间。普通 JSON 的首行数据和思考时间只能在完整响应读取后记录，不能当作流式首字节指标。

全屏界面约每 50ms 合并输出；显示延迟是写入显示队列或终端输出的估计，不包括终端模拟器实际绘制时间。思考字符数不等于 token，推理用量以服务端返回的 `Usage` 为准。

## 模型上下文元数据

同步客户端 `get_context_limit(refresh=False)` 从当前配置的服务地址查询上限，同一客户端缓存结果（包括未知结果）。Gemini/Anthropic 使用模型详情接口，兼容接口查询模型列表并精确匹配 ID。查询不使用生成请求，不会把输出上限当成上下文窗口，也不转发凭证到其他地址或跟随重定向。

此查询只用于界面显示；手动配置上限优先，查询失败时仍能继续正常对话。字段与输入/完整窗口计算口径见[上下文占用估算](usage.md#上下文占用估算)。

## 代码组织与验证

```text
llm/
├── base.py                    # 上层依赖的同步／异步 Protocol
├── schemas.py                 # 统一请求、响应、消息、工具与用量
├── providers.py               # 提供商端点、别名和凭证变量
├── client.py                  # HTTP、同步／异步生命周期、重试与错误转换
├── errors.py                  # 统一异常
├── streaming.py / events.py   # 流式解析与增量事件
├── model_limits.py            # 模型上下文元数据
├── thinking.py                # 统一思考设置到协议参数的映射
├── thinking_profiles.py       # 能力匹配与自定义覆盖
├── thinking_catalog.py        # 内置模型能力字典
├── visible_thinking.py        # 可见思考摘要请求适配
└── adapters/
    ├── base.py                # 公共转换辅助与原生状态校验
    ├── chat_completions.py    # 六家国内提供商的兼容接口
    ├── openai.py              # OpenAI Responses
    ├── anthropic.py           # Claude Messages
    └── gemini.py              # Gemini generateContent
```

纯格式转换与 HTTP 分离，Agent 主循环只依赖 `LLM` 或 `AsyncLLM`。扩展同协议厂商通常只需添加 Provider 预设；新协议增加 Adapter 后注册到 `ADAPTERS`。

开发环境与检查命令见[开发与验证](development.md#开发环境与验证)。

自动化协议验证使用模拟 HTTP 响应与请求契约，不代表所有厂商均已通过真实凭证联调。模拟测试不能证明某个具体模型、账号权限或地域端点在线可用。

## 官方协议参考

以下为协议参考入口；本项目支持范围以适配器及内置能力规则为准，不代表服务端实时支持列表：

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) 与 [reasoning 状态回传](https://developers.openai.com/api/docs/guides/reasoning)。
- [Claude Messages API](https://platform.claude.com/docs/en/api/messages/create)。
- [Gemini generateContent API](https://ai.google.dev/api/generate-content) 与 [thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)。
- [DeepSeek API](https://api-docs.deepseek.com/) 与 [thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
- [通义千问 OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) 与 [地域基址](https://help.aliyun.com/zh/model-studio/base-url)。
- [Kimi Chat Completions](https://platform.kimi.com/docs/api/chat)。
- [智谱对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)。
- [火山方舟 Chat Completions](https://www.volcengine.com/docs/82379/1494384)。
- [MiniMax OpenAI 兼容接口](https://platform.minimaxi.com/docs/api-reference/text-openai-api)。
