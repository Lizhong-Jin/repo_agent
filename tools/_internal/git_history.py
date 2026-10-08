"""Bounded Git object queries and shared validation for history tools."""

import math
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from host_support.cancellation import checkpoint
from host_support.git_filters import ExternalGitFilter, GitFilterCheckError, check_external_filters

from .base import ToolEffects, ToolResult
from .file_policy import is_credential_path
from .process_runner import ProcessRunner, ProcessStartError

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class GitHistoryError(Exception):
    def __init__(self, code, message, result=None):
        self.code, self.message, self.result = code, message, result
        super().__init__(message)


def integer(arguments, name, default, lower, upper):
    value = arguments.get(name, default)
    if type(value) is not int or not lower <= value <= upper:
        raise GitHistoryError(
            "INVALID_ARGUMENTS", f"{name} must be an integer from {lower} to {upper}."
        )
    return value


def revision_argument(arguments):
    revision = arguments.get("revision", "HEAD")
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > 256
        or not revision.isprintable()
        or any(c.isspace() for c in revision)
        or revision.startswith("-")
        or ":" in revision
        or ".." in revision
        or "\\" in revision
    ):
        raise GitHistoryError(
            "INVALID_ARGUMENTS",
            "revision must select one commit, without ranges, options or object paths.",
        )
    return revision


def parse_commits(text, *, body=False):
    if not text:
        return []
    fields = text.split("\0")
    width = 6 if body else 5
    if fields[-1] != "" or (len(fields) - 1) % width:
        raise GitHistoryError("GIT_PARSE_ERROR", "Git returned incomplete commit metadata.")
    records = []
    for offset in range(0, len(fields) - 1, width):
        oid, parents, author, date, subject = fields[offset : offset + 5]
        if not OID.fullmatch(oid) or any(not OID.fullmatch(p) for p in parents.split()):
            raise GitHistoryError("GIT_PARSE_ERROR", "Git returned invalid commit metadata.")
        item = {
            "commit": oid,
            "parents": parents.split(),
            "author": author,
            "author_date": date,
            "subject": subject,
        }
        if body:
            item["message"] = fields[offset + 5]
        records.append(item)
    return records


class GitHistoryBase:
    def __init__(
        self,
        workspace_root,
        *,
        base_env,
        execution_allowed=False,
        max_output_bytes=64 * 1024,
        timeout_seconds=30,
    ):
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        for name, value in (
            ("max_output_bytes", max_output_bytes),
            ("timeout_seconds", timeout_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.execution_allowed = execution_allowed
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        env = dict(base_env)
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="safe.directory",
            GIT_CONFIG_VALUE_0=str(self.workspace_root),
            GIT_NO_LAZY_FETCH="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
        )
        self.runner = ProcessRunner(max_output_bytes=max_output_bytes, base_env=env)

    def command(self, args, cwd, deadline, *, complete=True, allowed=(0,)):
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitHistoryError("GIT_TIMEOUT", "Git history query timed out.")
        result = self.runner.run(
            [
                "git",
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "log.showSignature=false",
                "-c",
                "maintenance.auto=false",
                *args,
            ],
            cwd=cwd,
            timeout_seconds=math.ceil(remaining),
        )
        if result.cleanup_error or result.cleanup_status == "unknown":
            raise GitHistoryError(
                "GIT_CLEANUP_FAILED", "Git process cleanup could not be confirmed.", result
            )
        if result.timed_out or time.monotonic() > deadline:
            raise GitHistoryError("GIT_TIMEOUT", "Git history query timed out.", result)
        if result.exit_code not in allowed:
            raise GitHistoryError(
                "GIT_ERROR", "Git could not read the requested repository or object.", result
            )
        if complete and (result.stdout_truncated or result.stderr_truncated):
            raise GitHistoryError(
                "OUTPUT_TOO_LARGE",
                "Git metadata exceeds the output limit; narrow the request.",
                result,
            )
        return result

    def repository(self, arguments, deadline):
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd:
            raise GitHistoryError("INVALID_ARGUMENTS", "cwd must be a workspace directory.")
        requested = self.workspace_root / cwd
        target = requested.resolve()
        if is_credential_path(requested, target):
            raise GitHistoryError("PROTECTED_FILE", "cwd is protected.")
        if not target.is_relative_to(self.workspace_root):
            raise GitHistoryError("PATH_OUTSIDE_WORKSPACE", "cwd must stay inside the workspace.")
        if not target.is_dir():
            raise GitHistoryError("NOT_A_DIRECTORY", "cwd must be an existing directory.")
        root_result = self.command(["rev-parse", "--show-toplevel"], target, deadline)
        root = Path(root_result.stdout.rstrip("\r\n")).resolve()
        if not root.is_relative_to(self.workspace_root):
            raise GitHistoryError(
                "PATH_OUTSIDE_WORKSPACE", "Repository root must stay inside the workspace."
            )
        if is_credential_path(root, root):
            raise GitHistoryError("PROTECTED_FILE", "Repository root is protected.")
        # Older Git versions may not honor GIT_NO_LAZY_FETCH. Refuse promisor
        # configuration rather than risking automatic network/object downloads.
        config = self.command(
            [
                "config",
                "--includes",
                "--null",
                "--get-regexp",
                r"^(extensions\.partialclone|remote\..*\.promisor)$",
            ],
            root,
            deadline,
            allowed=(0, 1),
        )
        if config.stdout or config.exit_code == 0:
            raise GitHistoryError(
                "GIT_PARTIAL_CLONE_UNSUPPORTED",
                "History queries require locally available objects; partial clones are refused.",
            )
        if not self.execution_allowed:

            def filter_command(args, *, allowed=(0,)):
                result = self.command(args, root, deadline, allowed=allowed)
                if result.exit_code == 1 and result.stdout:
                    raise GitFilterCheckError("Invalid Git filter configuration")
                return result.stdout

            try:
                check_external_filters(filter_command)
            except GitFilterCheckError as error:
                raise GitHistoryError(
                    "GIT_CONFIG_CHECK_FAILED", "Cannot verify Git filter selection."
                ) from error
            except ExternalGitFilter as error:
                raise GitHistoryError(
                    "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX",
                    "Repository paths use external filters; use native or Docker isolation.",
                ) from error
        return root

    def resolve_commit(self, revision, root, deadline):
        result = self.command(
            ["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
            root,
            deadline,
            allowed=(0, 128),
        )
        oid = result.stdout.strip()
        if result.exit_code or not OID.fullmatch(oid):
            raise GitHistoryError("INVALID_REVISION", "revision did not resolve to one commit.")
        return oid

    def path(self, value, root):
        if not isinstance(value, str) or not value.strip() or "\0" in value or "\\" in value:
            raise GitHistoryError(
                "INVALID_ARGUMENTS",
                "Paths must be literal workspace paths without NUL or backslashes.",
            )
        requested = Path(os.path.abspath(self.workspace_root / value))
        resolved = requested.resolve()
        if is_credential_path(requested, resolved):
            raise GitHistoryError("PROTECTED_FILE", "The requested path is protected.")
        if not all(p.is_relative_to(self.workspace_root) for p in (requested, resolved)):
            raise GitHistoryError("PATH_OUTSIDE_WORKSPACE", "Path must stay inside the workspace.")
        if not all(p.is_relative_to(root) for p in (requested, resolved)):
            raise GitHistoryError(
                "PATH_OUTSIDE_REPOSITORY", "Path must stay inside the repository."
            )
        return requested.relative_to(root).as_posix()

    def paths(self, arguments, root):
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 64:
            raise GitHistoryError(
                "INVALID_ARGUMENTS", "paths must be an array of at most 64 literal paths."
            )
        return list(dict.fromkeys(self.path(p, root) for p in paths))

    def metadata(self, root, oid, deadline, *, limit=1, offset=0, paths=(), body=False):
        fmt = "%H%x00%P%x00%an%x00%aI%x00%s" + ("%x00%B" if body else "")
        result = self.command(
            [
                "log",
                "--no-patch",
                "--no-notes",
                "--no-show-signature",
                "--no-use-mailmap",
                "--no-decorate",
                "--encoding=UTF-8",
                "--topo-order",
                "-z",
                f"--format={fmt}",
                f"--max-count={limit}",
                f"--skip={offset}",
                oid,
                "--",
                *paths,
            ],
            root,
            deadline,
        )
        return parse_commits(result.stdout, body=body), result

    def execute(self, arguments):
        try:
            if not isinstance(arguments, dict) or set(arguments) - self.allowed_arguments:
                raise GitHistoryError(
                    "INVALID_ARGUMENTS",
                    "Allowed arguments: " + ", ".join(sorted(self.allowed_arguments)),
                )
            return self.query(arguments, time.monotonic() + self.timeout_seconds)
        except GitHistoryError as error:
            data = {}
            if error.result is not None:
                data = {
                    k: v for k, v in asdict(error.result).items() if k not in {"stdout", "stderr"}
                }
            return ToolResult(False, data, error.code, error.message, ToolEffects.process(data))
        except ProcessStartError as error:
            code = "GIT_NOT_FOUND" if isinstance(error.cause, FileNotFoundError) else "GIT_ERROR"
            return ToolResult(False, error_code=code, error="Unable to start Git.")
        except (OSError, RuntimeError):
            return ToolResult(
                False, error_code="GIT_ERROR", error="Unable to inspect repository paths."
            )
