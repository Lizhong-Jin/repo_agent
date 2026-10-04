"""Rolling tool segments; no UI, ledger, sandbox implementation or tool-name knowledge.

Only contiguous, explicitly reentrant reads run together. Everything else is a
barrier. A segment is fully joined before returning, including on fatal failure.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context

from host_support.cancellation import (
    RunCancelled,
    cancellation_scope,
    checkpoint,
    current_cancellation,
)


class ToolScheduler:
    def __init__(self, max_workers=4):
        if type(max_workers) is not int or not 1 <= max_workers <= 32:
            raise ValueError("max_tool_workers must be an integer from 1 to 32")
        self.max_workers = max_workers

    def execute(self, calls, *, policy, invoke, on_start, on_finish, check=checkpoint):
        """Yield in input order; progress callbacks run only on the caller thread.

        Workers own execution and synchronous receipts. Ordinary tool failures
        are values; raised exceptions stop dispatch and cancel cooperative peers.
        """
        pending = []
        for call in calls:
            if not policy(call).parallel:
                if pending:
                    yield from self._segment(pending, invoke, on_start, on_finish, check)
                    pending = []
                yield from self._segment([call], invoke, on_start, on_finish, check)
            else:
                pending.append(call)
        if pending:
            yield from self._segment(pending, invoke, on_start, on_finish, check)

    def _segment(self, calls, invoke, on_start, on_finish, check):
        # Restore ContextVars before yielding to consumers, even if they stop
        # consuming or raise between results. No worker outlives this scope.
        with cancellation_scope():
            return self._run_segment(calls, invoke, on_start, on_finish, check)

    def _run_segment(self, calls, invoke, on_start, on_finish, check):
        if len(calls) == 1 or self.max_workers == 1:
            results = []
            for call in calls:
                check()
                token = on_start(call)
                try:
                    result = invoke(call)
                except BaseException as error:
                    on_finish(token, None, error)
                    raise
                on_finish(token, result, None)
                results.append(result)
            check()
            return results
        context = current_cancellation()
        results = [None] * len(calls)
        errors = []
        futures = {}
        next_index = 0

        def run(call):
            try:
                checkpoint()
                return invoke(call)
            except BaseException:
                if context is not None:
                    context.cancel()
                raise

        # Each submission gets a distinct Context, retaining the shared run's
        # cancellation signal. invoke installs a fresh call-specific receipt.
        with ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="agent-tool"
        ) as pool:
            try:
                while futures or next_index < len(calls):
                    check()
                    while len(futures) < self.max_workers and next_index < len(calls):
                        # Fatal worker failures signal cancellation immediately,
                        # including before the coordinator collects their future.
                        check()
                        checkpoint()
                        call = calls[next_index]
                        token = on_start(call)
                        try:
                            future = pool.submit(copy_context().run, run, call)
                        except BaseException as error:
                            on_finish(token, None, error)
                            raise
                        futures[future] = (next_index, token)
                        next_index += 1
                    completed, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                    self._collect(completed, futures, results, errors, on_finish)
                    if errors:
                        break
            except BaseException as error:
                errors.append(error)
            finally:
                if errors and context is not None:
                    context.cancel()
                # Do not abandon running calls or skip their durable receipts.
                # Cancellation is no longer checked on the joining thread.
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    self._collect(completed, futures, results, errors, on_finish)
        if errors:
            # Preserve the original fatal error over cancellation of its peers.
            raise next((e for e in errors if not isinstance(e, RunCancelled)), errors[0])
        check()
        return results

    @staticmethod
    def _collect(completed, futures, results, errors, on_finish):
        for future in completed:
            index, token = futures.pop(future)
            error = None
            try:
                results[index] = future.result()
            except BaseException as failure:
                error = failure
                errors.append(failure)
            try:
                on_finish(token, results[index], error)
            except BaseException as failure:
                errors.append(failure)
