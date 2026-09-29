"""Own an asyncio I/O loop for synchronous callers, without detached requests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

from .cancellation import CancellationContext, RunCancelled, cancellable, current_cancellation


class AsyncBridge:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-dns")
        self.loop.set_default_executor(self.executor)
        self.thread = Thread(target=self.loop.run_forever, name="model-io", daemon=True)
        self.thread.start()

    def run(self, awaitable):
        context = current_cancellation() or CancellationContext()

        async def request():
            try:
                return await awaitable
            except KeyboardInterrupt:
                # A callback may still use the legacy interrupt convention.
                # Never let KeyboardInterrupt terminate the owned I/O loop.
                context.cancel()
                raise RunCancelled(context) from None

        future = asyncio.run_coroutine_threadsafe(cancellable(request(), context), self.loop)
        try:
            return future.result()
        except KeyboardInterrupt:
            context.cancel()
            # Unlike future.cancel(), this waits for response/transport finalizers.
            return future.result()

    def close(self, finalizer):
        async def close():
            try:
                await finalizer
            finally:
                await self.loop.shutdown_asyncgens()

        try:
            asyncio.run_coroutine_threadsafe(close(), self.loop).result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()
            # OS DNS resolution cannot be interrupted. A cancelled request never
            # resumes HTTP after DNS completes, and must not wait for the resolver.
            self.executor.shutdown(wait=False, cancel_futures=True)
