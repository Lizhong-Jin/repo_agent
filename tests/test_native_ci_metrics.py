"""CI records the real first startup, including incomplete and repeated attempts."""

import json

import pytest
from native_ci_metrics import install_startup_recorder

from sandbox.native_common import NativeBackendBase


@pytest.mark.parametrize("failure", [False, True])
def test_startup_recorder_preserves_outcome_and_appends_metrics(tmp_path, monkeypatch, failure):
    calls = []
    metrics = {"total_ms": 123, "runs": [{"policy": {"attempt_count": 2}}]}

    def initialize(backend, workspace):
        calls.append(workspace)
        backend.startup_metrics = metrics
        if failure:
            raise ValueError("startup failed")

    monkeypatch.setattr(NativeBackendBase, "__init__", initialize)
    path = tmp_path / "evidence" / "startup.jsonl"
    install_startup_recorder(monkeypatch, path, "test::startup")
    for _ in range(2):
        if failure:
            with pytest.raises(ValueError, match="startup failed"):
                NativeBackendBase(tmp_path)
        else:
            NativeBackendBase(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert calls == [tmp_path, tmp_path]  # No warm-up or repeated initialization.
    assert len(rows) == 2
    assert all(row["startup"] == metrics for row in rows)
    assert all(row["status"] == ("failed" if failure else "passed") for row in rows)
    assert all(
        row["error"] == ({"type": "ValueError", "message": "startup failed"} if failure else None)
        for row in rows
    )
