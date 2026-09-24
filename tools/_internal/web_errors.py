"""Safe, provider-independent Web errors; never include remote response bodies."""


class WebError(Exception):
    def __init__(self, code: str, message: str, *, retryable=False, http_status=None):
        super().__init__(message)
        self.details = {"code": code, "message": message, "retryable": retryable}
        if http_status is not None:
            self.details["http_status"] = http_status
