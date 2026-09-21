"""Minimal synchronous agent loop: model -> tools -> observations -> model."""

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from llm import LLM, InvalidResponseError, LLMError, LLMRequest, LLMResponse, Message, ToolCall
from llm.token_estimation import estimate_context_tokens
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
    notice: str = ""
    resumable: bool = False
    responses: tuple[LLMResponse, ...] = ()

    @property
    def text(self) -> str:
        """Final answer, including its text continuation chunks (without merging history)."""
        parts = [self.response.text]
        for previous in reversed(self.responses[:-1]):
            if (
                previous.finish_reason != "length"
                or previous.tool_calls
                or previous.truncated_tool_calls
            ):
                break
            parts.insert(0, previous.text)
        return "".join(parts)

    @property
    def undisplayed_text(self) -> str:
        """Fallback output for non-streaming clients, without duplicating streamed chunks."""
        parts = []
        records = self.stats.model_calls if self.stats else []
        responses = self.responses or (self.response,)
        for index, response in enumerate(responses):
            displayed = index < len(records) and records[index].first_display_seconds is not None
            if response.text and not displayed:
                if parts and index and responses[index - 1].finish_reason != "length":
                    parts.append("\n")
                parts.append(response.text)
        return "".join(parts)


class AgentRuntime:
    def __init__(
        self,
        llm: LLM,
        tools: Sequence[Tool] = (),
        *,
        max_steps: int = 8,
        max_output_tokens: int = 4096,
        max_recoveries: int = 2,
        recovery_max_output_tokens: int | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        temperature: float | None = None,
        tool_choice: str = "auto",
        request_extra: dict[str, Any] | None = None,
        on_event: Callable[[str, RunStats], None] | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        if type(max_steps) is not int or max_steps < 0:
            raise ValueError("max_steps must be a non-negative integer (0 means unlimited)")
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if type(max_recoveries) is not int or max_recoveries < 0:
            raise ValueError("max_recoveries must be a non-negative integer")
        if recovery_max_output_tokens is not None and (
            type(recovery_max_output_tokens) is not int
            or recovery_max_output_tokens < max_output_tokens
        ):
            raise ValueError("recovery_max_output_tokens must be >= max_output_tokens")
        if not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be text")
        self.llm = llm
        self.max_steps = max_steps
        self.max_output_tokens = max_output_tokens
        self.max_recoveries = max_recoveries
        # None keeps the configured model limit; opt in to raising it only when
        # the provider/model supports the explicitly configured recovery ceiling.
        self.recovery_max_output_tokens = recovery_max_output_tokens or max_output_tokens
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

    def _request(
        self, messages: Sequence[Message], *, max_output_tokens: int | None = None
    ) -> LLMRequest:
        return LLMRequest(
            self._with_skills(messages),
            tools=self._definitions,
            max_output_tokens=max_output_tokens or self.max_output_tokens,
            temperature=self.temperature,
            tool_choice=self.tool_choice,
            extra=deepcopy(self.request_extra),
        )

    def _with_skills(self, messages: Sequence[Message]) -> Sequence[Message]:
        if self.skills is not None:
            # Request-only metadata works with resumed histories and never rewrites
            # provider-native assistant messages or repeatedly grows stored history.
            messages = list(messages)
            position = next(
                (index for index, message in enumerate(messages) if message.role != "system"),
                len(messages),
            )
            messages.insert(position, Message("system", self.skills.prompt()))
        return messages

    def estimate_context_tokens(self, history: Sequence[Message] = ()) -> int:
        """Estimate the loaded context without generating or adding a user task."""
        messages = list(history)
        if not messages and self.system_prompt:
            messages.append(Message("system", self.system_prompt))
        return estimate_context_tokens(self._with_skills(messages), self._definitions)

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
            trace.stats.stop_reason = result.notice or None
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

        recoveries = 0
        output_limit = self.max_output_tokens
        responses = []

        def finish(status, notice="", *, resumable=False):
            if status == "max_steps":
                notice = "达到轮数上限，任务尚未完成；上下文已保留，可输入“继续”。"
                resumable = True
            return RunResult(
                status,
                response,
                tuple(messages),
                step,
                stats,
                notice,
                resumable,
                tuple(responses),
            )

        step = 0
        while self.max_steps == 0 or step < self.max_steps:
            step += 1
            self.check_cancelled()
            request = self._request(messages, max_output_tokens=output_limit)
            try:
                response = self._generate(request, step, trace)
            except LLMError as error:
                if not recoveries:
                    raise
                return finish(
                    "stopped",
                    f"自动恢复请求失败（{type(error).__name__}）：{error}。"
                    "任务尚未完成；已保留此前正文和有效上下文，可输入“继续”。",
                    resumable=True,
                )
            self.check_cancelled()
            responses.append(response)
            if response.finish_reason == "length":
                # No native tool or reasoning payload from a truncated response is
                # replayed. A valid-looking tool argument object can still be partial.
                tool_truncated = response.truncated_tool_calls or bool(response.tool_calls)
                if response.text:
                    messages.append(Message("assistant", response.text))
                instruction = (
                    "上一轮因输出上限中断，其中所有工具调用均未执行。"
                    "请重新生成完整的工具调用；不要假设操作已经完成。"
                    "可以减少单次调用数量或拆分操作，但每次工具参数必须完整。"
                    if tool_truncated
                    else "上一条回复因输出上限中断。请直接从中断处继续，补全未完成的句子；"
                    "不要重复已有内容，不要添加续写开场白，完成原任务后正常结束。"
                )
                # Internal recovery instruction, never rendered as a user-entered message.
                if tool_truncated or response.text:
                    messages.append(Message("user", instruction))
                if step == self.max_steps:
                    return finish("max_steps")
                if not tool_truncated and not response.text.strip():
                    return finish(
                        "stopped",
                        "输出额度耗尽但未产生可续写正文，已停止自动恢复；"
                        "请检查思考预算或输出上限。上下文已保留。",
                        resumable=True,
                    )
                if recoveries >= self.max_recoveries:
                    return finish(
                        "stopped",
                        f"输出仍被截断，已达到自动恢复上限（{self.max_recoveries} 次）。"
                        "任务尚未完成；已保留正文和有效上下文，可输入“继续”。",
                        resumable=True,
                    )
                if (
                    recoveries
                    and not tool_truncated
                    and len(responses) > 1
                    and response.text.strip() == responses[-2].text.strip()
                ):
                    return finish(
                        "stopped",
                        "续写重复了上一段内容，已停止自动恢复；"
                        "任务尚未完成，上下文已保留，可输入“继续”并补充要求。",
                        resumable=True,
                    )
                recoveries += 1
                if tool_truncated:
                    output_limit = min(output_limit * 2, self.recovery_max_output_tokens)
                trace.recovery(
                    step, "tools" if tool_truncated else "text", recoveries, output_limit
                )
                continue

            messages.append(response.to_message())
            if response.finish_reason == "stop":
                if response.tool_calls:
                    if recoveries:
                        messages.pop()
                        return finish(
                            "stopped",
                            "恢复回复的结束原因与工具调用不一致，已停止且未执行本轮工具。"
                            "任务尚未完成，上下文已保留，可输入“继续”。",
                            resumable=True,
                        )
                    raise InvalidResponseError("Model reported stop with pending tool calls")
                return finish("completed", resumable=True)
            if response.finish_reason != "tool_calls":
                if recoveries:
                    # Keep only safe content after an unsuccessful recovery.
                    messages.pop()
                    return finish(
                        "stopped",
                        f"自动恢复以 {response.finish_reason} 结束；任务尚未完成。"
                        "已保留此前正文和有效上下文，可输入“继续”。",
                        resumable=True,
                    )
                return finish("stopped", f"模型非正常结束：{response.finish_reason}")

            ids = [call.id for call in response.tool_calls]
            if not ids or len(set(ids)) != len(ids) or used_call_ids.intersection(ids):
                if recoveries:
                    messages.pop()
                    return finish(
                        "stopped",
                        "恢复回复的工具调用标识缺失或重复，已停止且未执行本轮工具。"
                        "任务尚未完成，上下文已保留，可输入“继续”。",
                        resumable=True,
                    )
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
            recoveries = 0
            output_limit = self.max_output_tokens

        return finish("max_steps")

    def _generate(self, request: LLMRequest, step: int, trace: RunTrace) -> LLMResponse:
        with trace.model(step) as record:
            record.thinking = deepcopy(self.thinking_settings)

            def event(kind, text, elapsed):
                if kind not in {"end", "thinking_end", "usage"}:
                    self.check_cancelled()
                field = {
                    "first_data": "first_data_seconds",
                    "first_text": "first_text_seconds",
                    "first_thinking": "first_thinking_seconds",
                    "end": "response_seconds",
                }.get(kind)
                if field:
                    setattr(record, field, elapsed)
                if kind == "first_thinking":
                    record.thinking_available = True
                elif kind == "thinking_unavailable":
                    record.thinking_available = False
                if kind == "thinking_delta":
                    record.thinking_characters += len(text)
                if self.on_model_event:
                    displayed = self.on_model_event(kind, text, elapsed, record)
                    if kind == "text" and record.first_display_seconds is None:
                        record.first_display_seconds = displayed

            generate = getattr(self.llm, "generate_with_events", None)
            response = generate(request, event) if generate else self.llm.generate(request)
            record.usage = response.usage
            record.finish_reason = response.finish_reason
            event("usage", "", record.response_seconds or 0.0)
        return response

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
