"""Injected durable result sink; no dependency on agents, tools or UI."""

from contextlib import contextmanager
from contextvars import ContextVar


class PersistenceError(OSError):
    """A required execution record could not be committed; stop the run."""


class ExecutionUncertainError(OSError):
    """A returned result reports unconfirmed process cleanup; stop the run."""


_receipt = ContextVar("execution_receipt", default=None)


@contextmanager
def receipt_scope(callback):
    token = _receipt.set(callback)
    try:
        yield
    finally:
        _receipt.reset(token)


def record_result(result, effects):
    callback = _receipt.get()
    if callback is not None:
        callback(result, effects)
