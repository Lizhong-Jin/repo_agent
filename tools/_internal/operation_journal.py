"""Durable GitHub operation evidence. Receipts never confer authority to retry."""

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from host_support.execution_receipt import PersistenceError
from host_support.filesystem import open_file
from host_support.storage import atomic_write, sync_directory
from tools.service_scopes import digest, validate_scope

STAGES = {"prepared", "objects_created", "mutation_requested", "response_received", "completed"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Invalid operation/session ID")
    return value


def read_json(path):
    with os.fdopen(open_file(path), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024:
            raise ValueError("Invalid operation record")
        return json.loads(stream.read(256 * 1024 + 1))


class OperationJournal:
    def __init__(self, directory, session_id):
        self.root = Path(directory)
        self._session_id = session_id

    @property
    def session_id(self):
        value = self._session_id() if callable(self._session_id) else self._session_id
        return identity(value)

    @property
    def directory(self):
        return self.root / "operations" / self.session_id

    def _check_directory(self):
        if self.directory.resolve() != self.directory.absolute():
            raise ValueError("Operation directory cannot traverse symbolic links")

    def read(self, operation_id):
        self._check_directory()
        record = read_json(self.directory / (identity(operation_id) + ".json"))
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("id") != operation_id
            or record.get("session_id") != self.session_id
            or record.get("stage") not in STAGES
            or type(record.get("revision")) is not int
            or record["revision"] < 1
        ):
            raise ValueError("Operation identity or state mismatch")
        validate_scope("github_write", record["scope"])
        if record.get("scope_digest") != fingerprint(record["scope"]):
            raise ValueError("Operation scope mismatch")
        if "candidate_commit" in record:
            digest(record["candidate_commit"], 40)
        if "number" in record and (type(record["number"]) is not int or record["number"] < 1):
            raise ValueError("Invalid recorded PR number")
        return record

    def records(self):
        self._check_directory()
        return [self.read(path.stem) for path in sorted(self.directory.glob("*.json"))]

    def save(self, record):
        try:
            self._check_directory()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Persist newly created directory entries before any external effect.
            # fsync(record) alone does not make its parent hierarchy durable.
            for parent in (self.directory.parent, self.root, self.root.parent):
                sync_directory(parent)
            path = self.directory / (identity(record["id"]) + ".json")
            atomic_write(
                path, json.dumps(record, ensure_ascii=True).encode(), sync=True, mode=0o600
            )
            sync_directory(self.directory)
        except (OSError, ValueError) as error:
            raise PersistenceError("外部操作证据保存失败；停止派发，先核实操作记录") from error

    def begin(self, scope):
        scope = validate_scope("github_write", scope)
        for previous in self.records():
            if (
                previous["scope_digest"] == fingerprint(scope)
                and previous["stage"] == "mutation_requested"
                and previous.get("verification", {}).get("state") != "confirmed"
            ):
                raise ValueError("Identical external write is unresolved; verify its operation ID")
        record = {
            "version": 1,
            "id": uuid4().hex,
            "session_id": self.session_id,
            "scope": scope,
            "scope_digest": fingerprint(scope),
            "stage": "prepared",
            "revision": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.save(record)  # No HTTP write is allowed before this succeeds.
        return record

    def update(self, record, **changes):
        current = self.read(record["id"])
        if current != record:
            raise ValueError("Operation evidence changed; inspect again")
        updated = {**record, **changes, "revision": record["revision"] + 1}
        self.save(updated)
        record.clear()
        record.update(updated)

    def verify(self, operation_id, github, *, expected):
        record = self.read(operation_id)
        if fingerprint(record) != expected:
            raise ValueError("Operation evidence changed; request a new verification")
        result = github.verify(deepcopy(record))
        # Preserve the original write stage; verification is a separate observation.
        self.update(record, verification={**result, "checked_at": datetime.now(UTC).isoformat()})
        return {"operation_id": operation_id, **result}
