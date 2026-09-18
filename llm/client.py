"""Sync/async HTTP clients sharing exactly the same serializers and error policy."""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self

import httpx

from .adapters import ADAPTERS
from .errors import (
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    InvalidResponseError,
    LLMConnectionError,
    LLMError,
    LLMTimeoutError,
    ProviderError,
    RateLimitError,
)
from .events import RequestEvents
from .providers import get_provider
from .schemas import LLMRequest, LLMResponse
from .streaming import StreamAssembler


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    timeout: float = 300.0
    max_retries: int = 2
    retry_delay: float = 0.5
    max_retry_delay: float = 30.0

    stream: bool = True
    connect_timeout: float = 10.0
    write_timeout: float = 30.0
    pool_timeout: float = 10.0

    def __post_init__(self) -> None:
        get_provider(self.provider)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigurationError("Specify a model ID available in your provider account")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 10:
            raise ConfigurationError("max_retries must be an integer between 0 and 10")
        for key in ("timeout", "retry_delay", "max_retry_delay"):
            value = getattr(self, key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"{key} must be a finite non-negative number")
        if type(self.stream) is not bool:
            raise ConfigurationError("stream must be a boolean")
        for key in ("timeout", "connect_timeout", "write_timeout", "pool_timeout"):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ConfigurationError(f"{key} must be a finite positive number")


class _ClientCore:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.provider = get_provider(config.provider)
        api_key = (
            config.api_key if config.api_key is not None else os.getenv(self.provider.api_key_env)
        )
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConfigurationError(f"Set {self.provider.api_key_env} or pass api_key explicitly")
        api_key = api_key.strip()
        self.adapter = ADAPTERS[self.provider.api_format](self.provider.name, config.model)
        base = config.base_url if config.base_url is not None else self.provider.base_url
        try:
            url = httpx.URL(base)
        except (TypeError, httpx.InvalidURL):
            raise ConfigurationError("base_url must be an absolute HTTP(S) API base URL") from None
        if (
            url.scheme not in {"https", "http"}
            or not url.host
            or url.query
            or url.fragment
            or url.userinfo
        ):
            raise ConfigurationError(
                "base_url must be HTTP(S), without credentials, query or fragment"
            )
        self.url = str(url).rstrip("/") + self.adapter.path()
        if config.stream and self.provider.api_format == "gemini":
            self.url = self.url.removesuffix(":generateContent") + ":streamGenerateContent?alt=sse"
        self._timeout = httpx.Timeout(
            config.timeout,
            connect=config.connect_timeout,
            write=config.write_timeout,
            pool=config.pool_timeout,
        )
        self._headers = {"Content-Type": "application/json"}
        if self.provider.api_format == "anthropic":
            self._headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        elif self.provider.api_format == "gemini":
            self._headers["x-goog-api-key"] = api_key
        else:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def _body(self, request: LLMRequest) -> dict:
        request.validate()
        body = self.adapter.encode(request)
        if self.provider.api_format != "gemini":
            body["stream"] = self.config.stream
        return body

    def _error(self, response: httpx.Response) -> LLMError | None:
        if 200 <= response.status_code < 300:
            return None
        status = response.status_code
        retryable = status in {429, 500, 502, 503, 504, 529}
        cls = ProviderError
        if status in {401, 403}:
            cls = AuthenticationError
        elif status == 429:
            cls = RateLimitError
        elif 400 <= status < 500:
            cls = InvalidRequestError
        return cls(
            f"{self.provider.name} API returned HTTP {status}",
            provider=self.provider.name,
            status_code=status,
            request_id=response.headers.get("x-request-id") or response.headers.get("request-id"),
            retryable=retryable,
        )

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("retry-after")
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = -1
            if math.isfinite(seconds) and seconds >= 0:
                return min(seconds, self.config.max_retry_delay)
        return min(
            self.config.retry_delay * 2**attempt * random.uniform(0.75, 1.25),
            self.config.max_retry_delay,
        )

    def _decode(self, response: httpx.Response) -> LLMResponse:
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise InvalidResponseError("Expected a JSON object")
            if data.get("error"):
                raise ProviderError(
                    "Provider returned an error response", provider=self.provider.name
                )
            # Some compatible APIs report application errors inside HTTP 200 responses.
            if (data.get("base_resp") or {}).get("status_code", 0) != 0:
                raise ProviderError(
                    "Provider returned an application error", provider=self.provider.name
                )
            return self.adapter.decode(data)
        except ProviderError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            IndexError,
            AttributeError,
            InvalidRequestError,
            InvalidResponseError,
        ):
            raise InvalidResponseError(
                "Invalid provider response or tool arguments; response was not retried",
                provider=self.provider.name,
            ) from None


class LLMClient(_ClientCore):
    def __init__(self, config: LLMConfig, *, http_client: httpx.Client | None = None) -> None:
        super().__init__(config)
        self._owned = http_client is None
        self._http = http_client if http_client is not None else httpx.Client()

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.generate_with_events(request)

    def generate_with_events(self, request: LLMRequest, on_event=None) -> LLMResponse:
        events = RequestEvents(on_event)
        try:
            result = self._generate(request, events)
            if not events.first_text:
                events.text(result.text)
            return result
        finally:
            events.emit("end")

    def _generate(self, request: LLMRequest, events: RequestEvents) -> LLMResponse:
        body = self._body(request)
        for attempt in range(self.config.max_retries + 1):
            try:
                with self._http.stream(
                    "POST",
                    self.url,
                    headers=self._headers,
                    json=body,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    error = self._error(response)
                    if error is None:
                        if "text/event-stream" in response.headers.get("content-type", ""):
                            assembler = StreamAssembler(self.provider.api_format, events.text)
                            for line in response.iter_lines():
                                events.data()
                                assembler.feed(line)
                                if assembler.done:
                                    break
                            return self._decode(httpx.Response(200, json=assembler.finish()))
                        response.read()
                        events.data()
                        return self._decode(response)
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(
                    f"Model {type(exc).__name__}: connection or data wait timed out; not retried",
                    provider=self.provider.name,
                ) from None
            except httpx.RequestError:
                raise LLMConnectionError(
                    "Model connection failed", provider=self.provider.name
                ) from None
            if not error.retryable or attempt == self.config.max_retries:
                raise error
            time.sleep(self._delay(response, attempt))
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncLLMClient(_ClientCore):
    def __init__(self, config: LLMConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config)
        self._owned = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return await self.generate_with_events(request)

    async def generate_with_events(self, request: LLMRequest, on_event=None) -> LLMResponse:
        events = RequestEvents(on_event)
        try:
            result = await self._generate(request, events)
            if not events.first_text:
                events.text(result.text)
            return result
        finally:
            events.emit("end")

    async def _generate(self, request: LLMRequest, events: RequestEvents) -> LLMResponse:
        body = self._body(request)
        for attempt in range(self.config.max_retries + 1):
            try:
                async with self._http.stream(
                    "POST",
                    self.url,
                    headers=self._headers,
                    json=body,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    error = self._error(response)
                    if error is None:
                        if "text/event-stream" in response.headers.get("content-type", ""):
                            assembler = StreamAssembler(self.provider.api_format, events.text)
                            async for line in response.aiter_lines():
                                events.data()
                                assembler.feed(line)
                                if assembler.done:
                                    break
                            return self._decode(httpx.Response(200, json=assembler.finish()))
                        await response.aread()
                        events.data()
                        return self._decode(response)
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(
                    f"Model {type(exc).__name__}: connection or data wait timed out; not retried",
                    provider=self.provider.name,
                ) from None
            except httpx.RequestError:
                raise LLMConnectionError(
                    "Model connection failed", provider=self.provider.name
                ) from None
            if not error.retryable or attempt == self.config.max_retries:
                raise error
            await asyncio.sleep(self._delay(response, attempt))
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
