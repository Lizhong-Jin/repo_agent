"""Errors shared by every provider. Messages never include API keys or response bodies."""


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable


class ConfigurationError(LLMError):
    pass


class InvalidRequestError(LLMError):
    pass


class AuthenticationError(LLMError):
    pass


class RateLimitError(LLMError):
    pass


class ProviderError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class InvalidResponseError(LLMError):
    pass
