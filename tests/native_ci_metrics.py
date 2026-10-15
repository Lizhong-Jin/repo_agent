"""Opt-in CI evidence from actual startup, without an extra warm-up sandbox."""

import json
from pathlib import Path

from sandbox.native_common import NativeBackendBase


def install_startup_recorder(monkeypatch, destination, nodeid):
    path = Path(destination).resolve()
    original = NativeBackendBase.__init__

    def initialize(backend, *args, **kwargs):
        status = "failed"
        failure = None
        try:
            original(backend, *args, **kwargs)
            status = "passed"
        except BaseException as error:
            failure = {"type": type(error).__name__, "message": str(error)[:4096]}
            raise
        finally:
            row = {
                "test": nodeid,
                "platform": backend.platform_name,
                "status": status,
                "error": failure,
                "startup": getattr(backend, "startup_metrics", {}),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=True) + "\n")

    monkeypatch.setattr(NativeBackendBase, "__init__", initialize)
