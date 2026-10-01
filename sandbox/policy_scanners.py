"""Explicit scanner selection; never retry a failed native scan with another engine."""

import os

from .policy_scan import PolicyScanner


def create_policy_scanner(engine: str | None = None) -> PolicyScanner:
    engine = (os.environ.get("AGENT_NATIVE_SCANNER") or "python") if engine is None else engine
    if engine == "python":
        from .linux_policy import PythonPolicyScanner

        return PythonPolicyScanner()
    if engine == "rust":
        from .rust_policy import RustPolicyScanner

        return RustPolicyScanner()
    raise ValueError("AGENT_NATIVE_SCANNER 必须为 python 或 rust")
