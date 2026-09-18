"""Minimal synchronous agent loop: model -> tools -> observations -> model."""

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from llm import LLM, InvalidResponseError, LLMRequest, LLMResponse, Message, ToolCall
from tools import Tool, ToolResult

from .skills import LoadSkillTool, SkillRegistry
from .Tracing import RunStats, RunTrace

DEFAULT_SYSTEM_PROMPT = (
    "你是一个代码仓库助手。根据用户任务，按需调用提供的工具获取信息，再给出回答。"
    "文件路径相对于配置的项目根目录。只根据实际读取的内容描述代码，"
    "不要声称已经执行未执行的操作。工具返回错误时，修正参数或说明限制。"
    "默认使用简洁的中文回复。完成代码或文件修改后，通常用 3 到 6 行说明："
    "完成了什么、涉及的文件路径、实际验证结果，以及必要的未完成事项。"
    "已经通过工具写入或修改的代码，不要在面向用户的回复中重复粘贴完整文件、"
    "完整实现或长篇 diff; 只在确有必要时引用少量关键代码。"
    "执行过程中的文字说明也应简短，不要先展示完整代码再调用工具写入。"
    "传给写入或编辑工具的代码参数必须完整准确，不能用省略号或说明文字代替。"
    "用户明确要求完整代码、示例、diff 或详细讲解时，按用户要求提供相应内容。"
    "验证非法输入等预期失败场景时，用 run_python 调用 subprocess.run 并断言"
    "实际退出码等于预期值，使验证本身通过时返回 0; "
    "不要仅凭被测程序的非零退出码就声称验证成功。"
    "当工具支持 check_id 时，从首次验证起为每项检查提供稳定标识；修正验证脚本后"
    "复用同一 check_id 和 cwd 重跑，保留原有断言，不同检查使用不同标识。"
)


@dataclass(frozen=True)
class RunResult:
    status: Literal["completed", "max_steps", "stopped"]
    response: LLMResponse
    history: tuple[Message, ...]
    steps: int
    stats: RunStats | None = None

    @property
    def text(self) -> str:
        """Last model output; check status before treating it as a completed answer."""
        return self.response.text


class AgentRuntime:
    def __init__(
        self,
        llm: LLM,
        tools: Sequence[Tool] = (),
        *,
        max_steps: int = 8,
        max_output_tokens: int = 4096,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float | None = None,
        tool_choice: str = "auto",
        request_extra: dict[str, Any] | None = None,
        on_event: Callable[[str, RunStats], None] | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be text")
        self.llm = llm
        self.max_steps = max_steps
        self.max_output_tokens = max_output_tokens
        self.system_prompt = system_prompt
        self.skills = skills
        self.temperature = temperature
        self.tool_choice = tool_choice
        self.request_extra = deepcopy(request_extra) if request_extra is not None else {}
        self.on_event = on_event
        self.last_stats: RunStats | None = None
        self._task_number = 0
        self.on_model_event = None
        self.check_cancelled = lambda: None
        self.thinking_settings = None
        self._tools: dict[str, Tool] = {}
        definitions = []
        registered_tools = [*tools, *([LoadSkillTool(skills)] if skills is not None else [])]
        for tool in registered_tools:
            definition = deepcopy(tool.definition)
            if definition.name in self._tools:
                raise ValueError(f"Duplicate tool name: {definition.name}")
            self._tools[definition.name] = tool
            definitions.append(definition)
        self._definitions = tuple(definitions)
        # Validate request settings before an interactive session accepts its first task.
        self._request([Message("user", "Validate configuration")])

    def _request(self, messages: Sequence[Message]) -> LLMRequest:
        if self.skills is not None:
            # Request-only metadata works with resumed histories and never rewrites
            # provider-native assistant messages or repeatedly grows stored history.
            messages = list(messages)
            position = next(
                (index for index, message in enumerate(messages) if message.role != "system"),
                len(messages),
            )
            messages.insert(position, Message("system", self.skills.prompt()))
        return LLMRequest(
            messages,
            tools=self._definitions,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            tool_choice=self.tool_choice,
            extra=deepcopy(self.request_extra),
        )

    def run(self, task: str, *, history: Sequence[Message] = ()) -> RunResult:
        """Run a task, optionally continuing history. Never mutate the caller's messages."""
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be non-empty text")
        self._task_number += 1
        trace = RunTrace(self._task_number, self.on_event)
        self.last_stats = trace.stats
        with trace:
            result = self._run(task, history, trace)
            trace.stats.status = result.status
            return result

    def _run(self, task: str, history: Sequence[Message], trace: RunTrace) -> RunResult:
        stats = trace.stats
        messages = list(deepcopy(history))
        if not messages and self.system_prompt:
            messages.append(Message("system", self.system_prompt))
        messages.append(Message("user", task))
        # Validate supplied history before accessing or executing its tool calls.
        self._request(messages)
        if self.skills is not None:
            selected = self.skills.explicit(task)
            if selected:
                messages[-1] = Message(
                    "user",
                    task
                    + "\n\n本轮显式指定的技能（JSON 数据；按用户任务范围使用）：\n"
                    + json.dumps([skill.payload() for skill in selected], ensure_ascii=False),
                )
                for skill in selected:
                    trace.skill_loaded(skill, "explicit")
        used_call_ids = {call.id for message in messages for call in message.tool_calls}

        for step in range(1, self.max_steps + 1):
            self.check_cancelled()
            request = self._request(messages)
            with trace.model(step) as record:
                record.thinking = deepcopy(self.thinking_settings)

                def event(kind, text, elapsed):
                    if kind != "end":
                        self.check_cancelled()
                    field = {
                        "first_data": "first_data_seconds",
                        "first_text": "first_text_seconds",
                        "end": "response_seconds",
                    }.get(kind)
                    if field:
                        setattr(record, field, elapsed)
                    if self.on_model_event:
                        displayed = self.on_model_event(kind, text, elapsed, record)
                        if kind == "text" and record.first_display_seconds is None:
                            record.first_display_seconds = displayed

                generate = getattr(self.llm, "generate_with_events", None)
                response = generate(request, event) if generate else self.llm.generate(request)
                record.usage = response.usage
                record.finish_reason = response.finish_reason
            self.check_cancelled()
            messages.append(response.to_message())
            if response.finish_reason == "stop":
                if response.tool_calls:
                    raise InvalidResponseError("Model reported stop with pending tool calls")
                return RunResult("completed", response, tuple(messages), step, stats)
            if response.finish_reason != "tool_calls":
                # Length-limited or blocked responses must not trigger tool execution.
                return RunResult("stopped", response, tuple(messages), step, stats)

            ids = [call.id for call in response.tool_calls]
            if not ids or len(set(ids)) != len(ids) or used_call_ids.intersection(ids):
                raise InvalidResponseError("Model returned missing or reused tool call IDs")
            used_call_ids.update(ids)
            for call in response.tool_calls:
                self.check_cancelled()
                with trace.tool(step, call) as record:
                    observation = self._execute(call)
                    messages.append(observation)
                    trace.tool_result(record, observation)
                    if (
                        self.skills is not None
                        and call.name == "load_skill"
                        and not observation.is_error
                    ):
                        trace.skill_loaded(self.skills.get(call.arguments["name"]), "model")

        return RunResult("max_steps", response, tuple(messages), self.max_steps, stats)

    def _execute(self, call: ToolCall) -> Message:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                success=False,
                error_code="UNKNOWN_TOOL",
                error=f"Unknown tool. Available tools: {', '.join(self._tools) or '(none)'}.",
            ).to_message(call)
        try:
            # A tool must not mutate the assistant history or its provider-specific state.
            result = tool.execute(deepcopy(call.arguments))
            if not isinstance(result, ToolResult):
                raise TypeError("Tools must return ToolResult")
            return result.to_message(call)
        except Exception as error:
            # Interrupts (KeyboardInterrupt/SystemExit) are intentionally not swallowed.
            return ToolResult(
                success=False,
                error_code="TOOL_EXECUTION_ERROR",
                error=f"Tool execution failed ({type(error).__name__}).",
            ).to_message(call)
