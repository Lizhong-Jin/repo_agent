"""Per-run cooperative cancellation, independent of UI and tool protocols.

Contexts follow synchronous calls and asyncio tasks via ContextVar. Explicitly
bind the context when starting an unrelated worker thread or process. Cleanup
uses its own deadlines and must not call checkpoint().
"""

import asyncio
import signal
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from threading import Event, current_thread, main_thread

_current = ContextVar("run_cancellation", default=None)
_deferred = ContextVar("deferred_cancellation", default=False)


class RunCancelled(BaseException):
    """Control flow, deliberately not caught by ordinary tool error handlers."""

    def __init__(self, context):
        self.context = context
        super().__init__("Run cancelled")

    @property
    def report(self):
        return self.context.report()


class CancellationContext:
    def __init__(self):
        self.event = Event()
        self.cleanups = []
        self.tools = []

    def cancel(self):
        self.event.set()

    def check(self):
        if self.event.is_set() and not _deferred.get():
            raise RunCancelled(self)

    def wait(self, seconds):
        if _deferred.get():
            time.sleep(seconds)
        elif self.event.wait(seconds):
            self.check()

    def record_cleanup(self, status, **details):
        self.cleanups.append({"status": status, **details})

    def report(self):
        statuses = [item["status"] for item in self.cleanups]
        return deepcopy(
            {
                "status": "cancelled",
                "cleanup_status": (
                    "unknown"
                    if "unknown" in statuses
                    else "confirmed"
                    if "confirmed" in statuses
                    else "not_needed"
                ),
                "cleanups": self.cleanups,
                # Tool records describe confirmed results, not a promise of rollback
                # or a complete inventory of arbitrary subprocess side effects.
                "tools": self.tools,
                "changes": [item for item in self.tools if item["effects"] == "reported"],
                "changes_complete": not self.tools,
            }
        )


@contextmanager
def defer_cancellation():
    """Let a bounded file transaction finish; callers check again after recording its result."""
    token = _deferred.set(True)
    try:
        yield
    finally:
        _deferred.reset(token)


def current_cancellation():
    return _current.get()


def checkpoint():
    context = current_cancellation()
    if context is not None:
        context.check()


@contextmanager
def cancellation_scope(context=None, *, handle_sigint=False):
    context = context if context is not None else current_cancellation()
    context = context if context is not None else CancellationContext()
    token = _current.set(context)
    previous = None
    if handle_sigint and current_thread() is main_thread():
        previous = signal.signal(signal.SIGINT, lambda *_: context.cancel())
    try:
        yield context
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        _current.reset(token)


async def cancellable(awaitable, context=None):
    """Cancel and await the request task, including its resource finalizers."""
    context = context if context is not None else current_cancellation()
    if context is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            context.check()
            await asyncio.wait({task}, timeout=0.05)
        context.check()
        return await task
    finally:
        if not task.done():
            task.cancel()
        # Always retrieve exceptions; a cancellation/completion race must not
        # leave a detached request or an unobserved task exception.
        with suppress(asyncio.CancelledError, Exception):
            await task
