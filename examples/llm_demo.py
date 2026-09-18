"""python -m examples.llm_demo --mock, or set a provider key and pass --model."""

import argparse
import json
import os
from contextlib import ExitStack

import httpx

from llm import LLMClient, LLMConfig, LLMError, LLMRequest, Message, ToolDefinition

ADD = ToolDefinition(
    name="add",
    description="Add two integers",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
)


def mock_model(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["messages"][-1]["role"] == "tool":
        message = {"role": "assistant", "content": "工具返回：1 + 2 = 3。"}
        finish = "stop"
    else:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_demo",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
                }
            ],
        }
        finish = "tool_calls"
    return httpx.Response(
        200,
        json={
            "id": "demo",
            "model": "mock-model",
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="统一模型调用与工具回传示例")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "deepseek"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL"))
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL"))
    parser.add_argument("--mock", action="store_true", help="使用本地模拟响应，不发送网络请求")
    args = parser.parse_args()
    if not args.mock and not args.model:
        parser.error("请用 --model 或 LLM_MODEL 指定账号可用的模型 ID")
    config = LLMConfig(
        provider="deepseek" if args.mock else args.provider,
        model="mock-model" if args.mock else args.model,
        api_key="mock-key" if args.mock else None,
        base_url=None if args.mock else args.base_url,
    )
    history = [
        Message("system", "你是代码助手。需要计算时使用工具。"),
        Message("user", "请调用 add 工具计算 1 + 2，然后告诉我结果。"),
    ]
    try:
        with ExitStack() as stack:
            transport = (
                stack.enter_context(httpx.Client(transport=httpx.MockTransport(mock_model)))
                if args.mock
                else None
            )
            client = stack.enter_context(LLMClient(config, http_client=transport))
            for _ in range(4):
                response = client.generate(LLMRequest(history, tools=[ADD]))
                print(f"[{response.provider}/{response.model}] {response.finish_reason}")
                print(f"usage={response.usage}")
                if response.text:
                    print(response.text)
                if response.finish_reason != "tool_calls":
                    if response.finish_reason != "stop":
                        raise SystemExit(f"模型未正常完成：{response.finish_reason}")
                    return
                history.append(response.to_message())
                for call in response.tool_calls:
                    # Validate the selected tool and arguments before executing anything.
                    values = call.arguments
                    valid = (
                        call.name == "add"
                        and set(values) == {"a", "b"}
                        and all(type(value) is int for value in values.values())
                    )
                    if valid:
                        result = {"sum": values["a"] + values["b"]}
                        print(f"tool: add({values}) -> {result}")
                        history.append(Message.tool_result(call, result))
                    else:
                        history.append(
                            Message.tool_result(
                                call, "Unknown tool or invalid arguments", is_error=True
                            )
                        )
            raise SystemExit("已达到示例的 4 轮调用上限")
    except LLMError as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from None


if __name__ == "__main__":
    main()
