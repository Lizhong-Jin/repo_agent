"""Write-ahead ownership records for private Windows native profiles.

Only recorded UUID identities are eligible. A held lease protects active calls;
recovery never executes project code and never applies saved workspace changes.
"""

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from .filesystem import open_file
from .locking import lock_descriptor
from .storage import atomic_write


def profile_name(identity):
    if not re.fullmatch("[0-9a-f]{32}", identity):
        raise ValueError("Invalid Windows isolation identity")
    return "repo-agent-call-" + identity


def job_name(identity):
    profile_name(identity)
    # Global allows recovery after a session change. Only our random names are opened.
    return "Global\\repo-agent-native-" + identity


def _clear_control(root, identity):
    # This host-only tree is derived from our UUID, never from a journal path.
    control = root / "calls" / identity
    if control.is_symlink() or (
        control.exists() and getattr(control.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Recovery control directory is a reparse point")
    if control.exists():
        shutil.rmtree(control)


class RecoveryLease:
    def __init__(self, root):
        self.root = Path(root)
        self.identity = uuid.uuid4().hex
        self.name = profile_name(self.identity)
        self.job = job_name(self.identity)
        self.path = self.root / (self.identity + ".json")
        self.fd = open_file(
            self.root / (self.identity + ".lock"), os.O_RDWR | os.O_CREAT, nonblocking=False
        )
        try:
            lock_descriptor(self.fd, blocking=False)
            self.record = {"version": 1, "name": self.name, "retain": False}
            self.save()  # Durable intent precedes profile or process creation.
        except BaseException:
            os.close(self.fd)
            raise

    def save(self):
        atomic_write(self.path, json.dumps(self.record).encode(), sync=True, mode=0o600)

    def retain(self, reason):
        self.record.update(retain=True, reason=str(reason)[:2000])
        self.save()

    def finish(self, *, released):
        try:
            if released:
                _clear_control(self.root, self.identity)
                self.path.unlink()
        finally:
            os.close(self.fd)


def recover_profiles(root, api, *, dry_run=False, discard=None):
    """Return a report; failures retain the exact record for the next attempt."""
    root = Path(root)
    if discard is not None:
        profile_name(discard)
        if not (root / (discard + ".json")).exists():
            raise ValueError("No recorded Windows native profile with this ID")
    report = {"removed": [], "active": [], "retained": [], "failed": []}
    for path in sorted(root.glob("*.json")):
        identity = path.stem
        if discard is not None and identity != discard:
            continue
        try:
            expected = profile_name(identity)
            fd = open_file(root / (identity + ".lock"), os.O_RDWR | os.O_CREAT, nonblocking=False)
        except (OSError, ValueError) as error:
            report["failed"].append({"record": path.name, "error": str(error)})
            continue
        try:
            try:
                lock_descriptor(fd, blocking=False)
            except BlockingIOError:
                report["active"].append(expected)
                continue
            try:
                try:
                    source = open_file(path)
                except FileNotFoundError:
                    continue  # Owner finished between enumeration and locking.
                with os.fdopen(source, "rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Oversized recovery record")
                record = json.loads(raw)
                if (
                    record.get("version") != 1
                    or record.get("name") != expected
                    or type(record.get("retain")) is not bool
                ):
                    raise ValueError("Invalid recovery record")
                if record["retain"] and identity != discard:
                    report["retained"].append(expected)
                    report.setdefault("details", []).append(
                        {"profile": expected, "reason": str(record.get("reason", ""))[:2000]}
                    )
                    continue
                if not api.job_is_empty(job_name(identity)):
                    report["active"].append(expected)
                    continue
                if dry_run:
                    report.setdefault("eligible", []).append(expected)
                    continue
                api.recover_profile(expected)
                _clear_control(root, identity)
                path.unlink()
                report["removed"].append(expected)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                report["failed"].append({"record": path.name, "error": str(error)})
        finally:
            os.close(fd)
    return report
