"""Structured visible transcript, separate from native model conversation history."""

from dataclasses import dataclass, field

from .record_timing import seconds


def display_text(text):
    # Terminal control sequences must never act as cursor movement or OSC commands.
    return "".join(
        c if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) < 160 else "�" for c in text
    )


@dataclass
class Block:
    kind: str
    chunks: list[str] = field(default_factory=list)
    characters: int = 0
    step: int = 0
    started: float = 0.0
    ended: float | None = None
    _text: str = ""

    def append(self, text):
        self.chunks.append(text)
        self.characters += len(text)

    def text(self):
        if self.chunks:
            self._text += display_text("".join(self.chunks))
            self.chunks.clear()
        return self._text


class Transcript:
    def __init__(self):
        self.blocks = []
        self.active_thinking = None
        self.agent_lines = set()

    def to_records(self):
        return [
            {
                "kind": b.kind,
                "text": b.text(),
                "step": b.step,
                "started": seconds(b.started),
                "ended": seconds(b.ended),
            }
            for b in self.blocks
        ]

    @classmethod
    def from_records(cls, records):
        import math

        result = cls()
        if not isinstance(records, list):
            raise ValueError("会话显示记录无效；可使用 --new-session")
        for row in records:
            if (
                not isinstance(row, dict)
                or not {"kind", "text", "step", "started", "ended"}.issubset(row)
                or row.get("kind") not in {"user", "text", "agent", "thinking"}
                or not isinstance(row.get("text"), str)
                or type(row.get("step")) is not int
                or row["step"] < 0
                or type(row.get("started")) not in {int, float}
                or not math.isfinite(row["started"])
                or row["started"] < 0
                or (
                    row.get("ended") is not None
                    and (
                        type(row["ended"]) not in {int, float}
                        or not math.isfinite(row["ended"])
                        or row["ended"] < row["started"]
                    )
                )
            ):
                raise ValueError("会话显示记录无效；可使用 --new-session")
            block = Block(row["kind"], step=row["step"], started=row["started"], ended=row["ended"])
            block.append(row["text"])
            result.blocks.append(block)
        return result

    def append(self, text, *, kind="text"):
        if not text:
            return
        if kind != "user" and self.blocks and self.blocks[-1].kind == kind:
            block = self.blocks[-1]
        else:
            block = Block(kind)
            self.blocks.append(block)
        block.append(text)

    def thinking(self, kind, text, step, elapsed):
        if kind in {"thinking_start", "thinking_delta"} and self.active_thinking is None:
            self.active_thinking = Block("thinking", step=step, started=elapsed)
            self.blocks.append(self.active_thinking)
        if kind == "thinking_delta":
            self.active_thinking.append(text)
        elif kind == "thinking_end" and self.active_thinking is not None:
            self.active_thinking.ended = elapsed
            self.active_thinking = None

    def render(self, mode):
        parts, user_lines, thinking_lines = [], {}, set()
        line = 0
        self.agent_lines.clear()
        previous_kind = None
        for block in self.blocks:
            if block.kind == "thinking":
                if mode == "hidden":
                    continue
                duration = (
                    "进行中" if block.ended is None else f"{block.ended - block.started:.2f}s"
                )
                mark = "▾" if mode == "expanded" else "▸"
                label = (
                    f"{mark} 思考 · 模型 #{block.step} · {duration}"
                    f" · 已接收 {block.characters:,} 字符"
                )
                value = "\n" + label + "\n"
                if mode == "expanded":
                    value += block.text() + "\n"
                thinking_lines.update(range(line + 1, line + value.count("\n")))
            else:
                value = block.text()
                if (
                    parts
                    and not parts[-1].endswith("\n")
                    and (block.kind == "agent" or previous_kind == "agent")
                ):
                    parts.append("\n")
                    line += 1
                if block.kind == "agent":
                    self.agent_lines.update(range(line, line + len(value.splitlines())))
                if block.kind == "user":
                    for offset in range(len(value.splitlines())):
                        user_lines[line + offset] = offset == 0
            parts.append(value)
            previous_kind = block.kind
            line += value.count("\n")
        return "".join(parts), user_lines, thinking_lines
