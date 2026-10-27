"""Canonical, exact single-operation scopes; no executable commands or credentials."""

import base64
import json
import re
from copy import deepcopy
from urllib.parse import urlsplit

from host_support.windows_files import validate_component


def fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        raise ValueError("Unexpected operation fields")


def text(value, limit=1000):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
    ):
        raise ValueError("Invalid text")
    return value


def digest(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
        raise ValueError("Invalid digest")
    return value


def relative_path(value):
    text(value, 1000)
    if (
        "\\" in value
        or value.startswith("/")
        or any(p in {"", ".", ".."} for p in value.split("/"))
    ):
        raise ValueError("Use a portable workspace-relative path")
    for part in value.split("/"):
        validate_component(part)
    return value


def https_url(value):
    from ._internal.web_http import public_url

    result = public_url(value)
    parsed = urlsplit(result)
    if parsed.scheme != "https" or parsed.port not in {None, 443} or urlsplit(value).fragment:
        raise ValueError("Only public HTTPS on port 443 without fragments")
    return result


def branch(value):
    text(value, 200)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value)
        or any(
            p in {"", ".", ".."} or p.startswith(".") or p.endswith((".", ".lock"))
            for p in value.split("/")
        )
        or ".." in value
    ):
        raise ValueError("Invalid branch")


def validate_scope(capability, value):
    if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False)) > 64000:
        raise ValueError("Invalid or oversized scope")
    scope = deepcopy(value)
    if capability == "network":
        fields(scope, {"url"})
        scope["url"] = https_url(scope["url"])
    elif capability == "dependencies":
        manager = scope.get("manager")
        if manager not in {"python", "npm"}:
            raise ValueError("Unknown dependency manager")
        collection = "wheels" if manager == "python" else "packages"
        fields(scope, {"manager", "lock_path", "lock_sha256", collection})
        relative_path(scope["lock_path"])
        digest(scope["lock_sha256"])
        if manager == "npm":
            packages = scope["packages"]
            if not isinstance(packages, list) or not 1 <= len(packages) <= 64:
                raise ValueError("A lock must contain 1–64 npm packages")
            seen = set()
            for package in packages:
                fields(package, {"path", "url", "integrity"})
                relative_path(package["path"])
                if (
                    not re.fullmatch(
                        r"node_modules/(?:@[a-z0-9_-]+/)?[a-z0-9_.-]+(?:/node_modules/(?:@[a-z0-9_-]+/)?[a-z0-9_.-]+)*",
                        package["path"],
                    )
                    or package["path"] in seen
                ):
                    raise ValueError("Invalid npm package path")
                seen.add(package["path"])
                package["url"] = https_url(package["url"])
                parsed = urlsplit(package["url"])
                if (
                    parsed.hostname != "registry.npmjs.org"
                    or parsed.query
                    or not parsed.path.endswith(".tgz")
                ):
                    raise ValueError("Only npm registry tarballs are supported")
                integrity = package["integrity"]
                if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                    raise ValueError("A SHA-512 npm integrity is required")
                try:
                    decoded = base64.b64decode(integrity[7:], validate=True)
                except ValueError:
                    raise ValueError("Invalid npm integrity") from None
                if len(decoded) != 64:
                    raise ValueError("Invalid npm integrity size")
            return scope
        wheels = scope["wheels"]
        if not isinstance(wheels, list) or not 1 <= len(wheels) <= 32:
            raise ValueError("A lock must contain 1–32 wheels")
        seen = set()
        for wheel in wheels:
            fields(wheel, {"url", "sha256"})
            wheel["url"] = https_url(wheel["url"])
            parsed = urlsplit(wheel["url"])
            filename = parsed.path.rsplit("/", 1)[-1]
            if (
                parsed.hostname != "files.pythonhosted.org"
                or parsed.query
                or not re.fullmatch(
                    r"[A-Za-z0-9_.!+]+-[A-Za-z0-9_.!+]+(?:-[0-9][A-Za-z0-9_.]*)?-py3-none-any\.whl",
                    filename,
                )
                or filename.casefold() in seen
            ):
                raise ValueError("Use distinct pure Python py3-none-any wheels from PyPI")
            seen.add(filename.casefold())
            digest(wheel["sha256"])
    elif capability in {"github_read", "github_write"}:
        if not isinstance(scope.get("repo"), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", scope["repo"]
        ):
            raise ValueError("Use owner/repository")
        operation = scope.get("operation")
        if capability == "github_read":
            if operation in {"repository", "pulls", "runs", "branches"}:
                fields(scope, {"repo", "operation"})
            elif operation in {"pull", "run", "jobs"}:
                fields(scope, {"repo", "operation", "number"})
                if type(scope["number"]) is not int or not 1 <= scope["number"] <= 2**63 - 1:
                    raise ValueError("Invalid GitHub identifier")
            elif operation == "verify_operation":
                fields(scope, {"repo", "operation", "operation_id", "record_digest"})
                digest(scope["operation_id"], 32)
                digest(scope["record_digest"])
            else:
                raise ValueError("Unsupported GitHub read")
        elif operation == "create_pull_request":
            fields(scope, {"repo", "operation", "head", "base", "title", "body"})
            branch(scope["head"])
            branch(scope["base"])
            text(scope["title"], 200)
            if not isinstance(scope["body"], str) or len(scope["body"]) > 16000:
                raise ValueError("Invalid PR body")
        elif operation == "publish_files":
            fields(scope, {"repo", "operation", "branch", "expected_sha", "message", "files"})
            branch(scope["branch"])
            digest(scope["expected_sha"], 40)
            text(scope["message"], 1000)
            files = scope["files"]
            if not isinstance(files, list) or not 1 <= len(files) <= 32:
                raise ValueError("Publish 1–32 explicit files")
            seen = set()
            for file in files:
                fields(file, {"path", "sha256", "size"})
                relative_path(file["path"])
                digest(file["sha256"])
                if (
                    type(file["size"]) is not int
                    or not 0 <= file["size"] <= 1024**2
                    or file["path"].casefold() in seen
                ):
                    raise ValueError("Invalid or duplicate file")
                seen.add(file["path"].casefold())
            if sum(f["size"] for f in files) > 4 * 1024**2:
                raise ValueError("Publication exceeds 4 MiB")
        else:
            raise ValueError("Unsupported GitHub mutation")
    else:
        raise ValueError("Unknown service capability")
    return scope
