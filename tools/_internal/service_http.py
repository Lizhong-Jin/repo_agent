"""One HTTPS request, pinned public peers, no ambient credentials or redirects."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import RLock

import httpcore
import httpx

from host_support.cancellation import cancellable, checkpoint

from ..service_scopes import https_url
from .web_http import PublicNetworkBackend, read_body

REQUEST_TIMEOUT_SECONDS = 60


class ServiceHTTP:
    def __init__(self, network_backend_factory=PublicNetworkBackend):
        self.network_backend_factory = network_backend_factory
        self._loop = None
        self._closed = False
        self._lock = RLock()

    def request(self, method, url, *, payload=None, token=None, limit=256 * 1024):
        checkpoint()
        with self._lock:
            if self._closed:
                raise ValueError("HTTP service is closed")
            if self._loop is None:
                # Reuse one bounded DNS executor. asyncio.run() would wait for
                # an uninterruptible OS resolver after cancellation/timeout.
                # No user task runs in the background; this loop runs only while
                # serving a synchronous, serialized request.
                self._loop = asyncio.new_event_loop()
                self._loop.set_default_executor(
                    ThreadPoolExecutor(max_workers=1, thread_name_prefix="service-dns")
                )
            try:
                return self._loop.run_until_complete(
                    cancellable(self._request(method, url, payload, token, limit))
                )
            except BaseException:
                self._cancel_tasks()
                raise

    def _cancel_tasks(self):
        tasks = asyncio.all_tasks(self._loop)
        for task in tasks:
            task.cancel()
        if tasks:

            async def drain():
                await asyncio.gather(*tasks, return_exceptions=True)

            self._loop.run_until_complete(drain())

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._loop is not None:
                try:
                    self._cancel_tasks()
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                finally:
                    # Like the existing Web backend, do not wait for OS DNS.
                    # Cancelled resolver results can never initiate HTTP.
                    self._loop.close()

    async def _request(self, method, url, payload, token, limit):
        url = https_url(url)
        headers = {
            "Accept": "application/json, text/plain",
            "Accept-Encoding": "identity",
            "User-Agent": "repo-agent-services/1",
        }
        if token:
            if not url.startswith("https://api.github.com/repos/"):
                raise ValueError("Credential destination mismatch")
            headers.update(
                Authorization="Bearer " + token, **{"X-GitHub-Api-Version": "2022-11-28"}
            )
        content = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        if content is not None:
            headers["Content-Type"] = "application/json"
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async with httpcore.AsyncConnectionPool(
                network_backend=self.network_backend_factory(),
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            ) as pool:
                async with pool.stream(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    extensions={"timeout": {"connect": 10, "read": 20, "write": 10, "pool": 20}},
                ) as response:
                    # No request is replayed, including remote writes. A timeout
                    # after transmission is an ambiguous outcome requiring inspection.
                    if not 200 <= response.status < 300 or response.status == 206:
                        raise ValueError("Remote request rejected (redirects disabled)")
                    return read_result(
                        response.status,
                        await read_body(
                            response.aiter_stream(), httpx.Headers(response.headers), limit=limit
                        ),
                    )


def read_result(status, body):
    return {"status": status, "body": body}
