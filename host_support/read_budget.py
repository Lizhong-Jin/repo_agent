"""Per-call cooperative read budgets, independent of tools and model protocols."""

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

from .cancellation import checkpoint


@dataclass(frozen=True)
class ReadLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_entries: int = 100_000
    max_directories: int = 10_000
    timeout_seconds: float = 10.0

    def __post_init__(self):
        for value in (self.max_bytes, self.max_entries, self.max_directories):
            if type(value) is not int or value < 1:
                raise ValueError("Read limits must be positive integers")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Read timeout must be finite and positive")


class ReadBudgetExceeded(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class ReadBudget:
    def __init__(self, limits):
        self.limits = limits
        self.deadline = monotonic() + limits.timeout_seconds
        self.bytes = self.entries = self.directories = 0

    def consume(self, *, bytes=0, entries=0, directories=0):
        checkpoint()
        if monotonic() >= self.deadline:
            raise ReadBudgetExceeded("timeout")
        for name, amount in (("bytes", bytes), ("entries", entries), ("directories", directories)):
            if getattr(self, name) + amount > getattr(self.limits, "max_" + name):
                raise ReadBudgetExceeded("max_" + name)
            setattr(self, name, getattr(self, name) + amount)

    def report(self):
        return {
            "bytes_read": self.bytes,
            "entries_visited": self.entries,
            "directories_visited": self.directories,
        }


_current = ContextVar("read_budget", default=None)


def read_checkpoint(**amounts):
    checkpoint()
    budget = _current.get()
    if budget is not None:
        budget.consume(**amounts)


@contextmanager
def read_budget_scope(limits):
    budget = ReadBudget(limits)
    token = _current.set(budget)
    try:
        read_checkpoint()
        yield budget
    finally:
        _current.reset(token)


def bounded_read(source, limit, expected_size):
    """Bound actual I/O; callers still verify file identity after reading."""
    budget = _current.get()
    read_checkpoint()
    if budget is not None and expected_size > budget.limits.max_bytes - budget.bytes:
        raise ReadBudgetExceeded("max_bytes")
    chunks = []
    count = 0
    while count < limit:
        read_checkpoint()
        size = min(64 * 1024, limit - count)
        if budget is not None:
            remaining = budget.limits.max_bytes - budget.bytes
            if not remaining:
                # The initial size fits. Identity verification detects growth;
                # do not issue another read past the cumulative byte allowance.
                break
            size = min(size, remaining)
        chunk = source.read(size)
        read_checkpoint(bytes=len(chunk))
        chunks.append(chunk)
        count += len(chunk)
        if not chunk:
            break
    return b"".join(chunks)
