"""Backend admission control, independent of tool dispatch and agent sessions."""

from contextlib import contextmanager
from threading import Condition, RLock

from host_support.cancellation import checkpoint

_creation_lock = RLock()


class BackendGate:
    """Concurrent reads or one exclusive call, with priority for waiting writers.

    This gate is per backend, not per tool. It also protects callers that bypass
    the agent scheduler. Never acquire it recursively or upgrade a read lease.
    """

    def __init__(self):
        self.metadata = RLock()
        self._condition = Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def hold(self, *, read=False):
        with self._condition:
            if not read:
                self._waiting_writers += 1
            try:
                while self._writer or (self._waiting_writers if read else self._readers):
                    checkpoint()
                    self._condition.wait(0.05)
                checkpoint()
                if read:
                    self._readers += 1
                else:
                    self._writer = True
            finally:
                if not read:
                    self._waiting_writers -= 1
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                if read:
                    self._readers -= 1
                else:
                    self._writer = False
                self._condition.notify_all()


def backend_gate(backend):
    # Lazy creation also supports lightweight test/embedded backend adapters.
    with _creation_lock:
        gate = getattr(backend, "_execution_gate", None)
        if gate is None:
            gate = backend._execution_gate = BackendGate()
        return gate
