"""Shared result reference contract; independent of storage and tool implementations."""

import re

RESULT_REF = re.compile(r"result_([a-f0-9]{32})_([1-9][0-9]{0,18})")


class ResultReadError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def parse_reference(reference):
    match = RESULT_REF.fullmatch(reference) if isinstance(reference, str) else None
    if match is None or int(match[2]) > 2**63 - 1:
        raise ResultReadError("INVALID_ARGUMENTS", "Provide a result_ref returned by a tool call.")
    return match[1], int(match[2])
