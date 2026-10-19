"""Pinned Python/npm environments: verified bytes only, never host-side package code."""

import base64
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import unicodedata
import zipfile
from pathlib import PurePosixPath

from host_support.cancellation import checkpoint
from host_support.windows_files import validate_snapshot_names

from ..service_scopes import fields, relative_path
from .file_policy import is_protected_name


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_portable_paths(files, seen):
    validate_snapshot_names(files)
    prefixes = {}
    for name in files:
        parts = name.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            key = unicodedata.normalize("NFC", prefix).casefold()
            if prefixes.setdefault(key, prefix) != prefix:
                raise ValueError("Colliding dependency paths")
            if count < len(parts) and key in seen:
                raise ValueError("Conflicting dependency paths")


def read_workspace(access, relative, limit):
    relative_path(relative)
    with access.open_read(access.root / relative) as stream:
        before = stream.fileno()
        original = os.fstat(before)
        data = stream.read(limit + 1)
        after = os.fstat(before)
        if len(data) > limit or (
            original.st_size,
            original.st_mtime_ns,
            original.st_ctime_ns,
            original.st_nlink,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, 1):
            raise ValueError("Workspace file changed or exceeds size limit")
        return data


def lock_scope(access, lock_path, manager="python"):
    raw = read_workspace(access, lock_path, 64000)
    value = json.loads(raw)
    if manager == "npm":
        if value.get("lockfileVersion") != 3 or not isinstance(value.get("packages"), dict):
            raise ValueError("Use npm package-lock.json version 3")
        packages = []
        for path, record in value["packages"].items():
            if path == "":
                continue  # Root scripts/configuration are never executed or applied.
            if not isinstance(record, dict) or record.get("link") or record.get("inBundle"):
                raise ValueError("Linked/bundled npm dependencies are unsupported")
            packages.append(
                {"path": path, "url": record.get("resolved"), "integrity": record.get("integrity")}
            )
        return {
            "manager": "npm",
            "lock_path": lock_path,
            "lock_sha256": sha256(raw),
            "packages": packages,
        }
    fields(value, {"version", "wheels"})
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported dependency lock version")
    return {
        "manager": "python",
        "lock_path": lock_path,
        "lock_sha256": sha256(raw),
        "wheels": value["wheels"],
    }


def wheel_path(entry):
    # ZipInfo.filename has already had NUL suffixes removed and, on Windows,
    # backslashes replaced. Validate the original central-directory name before
    # reading ANY member (including WHEEL metadata), never the sanitized alias.
    original = entry.orig_filename
    if original != entry.filename:
        raise ValueError("Wheel member name was normalized by the ZIP reader")
    # Only a single trailing slash denotes a directory. rstrip would hide empty
    # components in names such as pkg// that the path policy must still reject.
    name = original[:-1] if original.endswith("/") else original
    relative_path(name)
    return name


def unpack_wheels(downloads):
    """Validate the entire set before writing, including cross-wheel collisions."""
    files, seen, total = {}, set(), 0
    for blob in downloads:
        checkpoint()
        with zipfile.ZipFile(io.BytesIO(blob)) as wheel:
            entries = wheel.infolist()
            if len(entries) + len(files) > 10000:
                raise ValueError("Too many wheel entries")
            names = []
            for entry in entries:
                checkpoint()
                names.append(wheel_path(entry))
            metadata = [item for item in entries if item.filename.endswith(".dist-info/WHEEL")]
            if len(metadata) != 1 or metadata[0].file_size > 8192:
                raise ValueError("Invalid wheel metadata")
            description = wheel.read(metadata[0]).decode("utf-8")
            if (
                "Root-Is-Purelib: true" not in description.splitlines()
                or "Tag: py3-none-any" not in description.splitlines()
            ):
                raise ValueError("Only portable pure Python wheels are supported")
            for entry, name in zip(entries, names, strict=True):
                checkpoint()
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or entry.flag_bits & 1:
                    raise ValueError("Links, special files and encrypted entries are forbidden")
                if (
                    is_protected_name(name)
                    or any(p.endswith(".data") for p in name.split("/"))
                    or name.lower().endswith((".pth", ".so", ".pyd", ".dll"))
                    or PurePosixPath(name).name.lower()
                    in {"sitecustomize.py", "usercustomize.py", "repo-agent-manifest.json"}
                ):
                    raise ValueError("Wheel needs unsupported or protected installation paths")
                if entry.is_dir():
                    continue
                key = unicodedata.normalize("NFC", name).casefold()
                if key in seen or entry.file_size > 32 * 1024**2:
                    raise ValueError("Duplicate or oversized wheel file")
                seen.add(key)
                total += entry.file_size
                if total > 128 * 1024**2:
                    raise ValueError("Dependency environment exceeds 128 MiB")
                with wheel.open(entry) as incoming:
                    data = incoming.read(entry.file_size + 1)
                if len(data) != entry.file_size:
                    raise ValueError("Invalid wheel entry size")
                files[name] = data
    validate_portable_paths(files, seen)
    return files


def unpack_npm(downloads, packages):
    files, seen, total = {}, set(), 0
    for blob, package in zip(downloads, packages, strict=True):
        checkpoint()
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as compressed:
            raw = compressed.read(128 * 1024**2 + 1)
        if len(raw) > 128 * 1024**2:
            raise ValueError("npm archive expansion exceeds limit")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            count = 0
            for entry in archive:
                checkpoint()
                count += 1
                if count + len(files) > 10000:
                    raise ValueError("Too many npm archive entries")
                if entry.isdir() and entry.name.rstrip("/") == "package":
                    continue
                if (
                    not entry.name.startswith("package/")
                    or not (entry.isdir() or entry.isfile())
                    or entry.issparse()
                ):
                    raise ValueError("npm archive must contain regular files below package/")
                relative = entry.name[len("package/") :].rstrip("/")
                relative_path(relative)
                # Nested dependencies are installed only from their own pinned
                # lock records, never smuggled in as bundled node_modules.
                if (
                    "node_modules" in relative.split("/")
                    or is_protected_name(relative)
                    or PurePosixPath(relative).name.casefold() == "repo-agent-manifest.json"
                ):
                    raise ValueError("Unsupported npm archive path")
                if entry.isdir():
                    continue
                name = package["path"] + "/" + relative
                key = unicodedata.normalize("NFC", name).casefold()
                if key in seen or entry.size > 32 * 1024**2:
                    raise ValueError("Duplicate or oversized npm file")
                seen.add(key)
                total += entry.size
                if total > 128 * 1024**2:
                    raise ValueError("npm environment exceeds 128 MiB")
                with archive.extractfile(entry) as source:
                    data = source.read(entry.size + 1)
                if len(data) != entry.size:
                    raise ValueError("Invalid npm archive size")
                files[name] = data
    validate_portable_paths(files, seen)
    return files


def install(access, scope, http):
    if sha256(read_workspace(access, scope["lock_path"], 64000)) != scope["lock_sha256"]:
        raise ValueError("Approved dependency lock changed")
    relative = ".agent-dependencies/" + scope["lock_sha256"]
    destination = access.root / relative
    try:
        access.stat(destination)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Environment already exists; never overwrite an existing environment")
    downloads, size = [], 0
    packages = scope["wheels"] if scope["manager"] == "python" else scope["packages"]
    for package in packages:
        checkpoint()
        blob = http.request("GET", package["url"], limit=32 * 1024**2)["body"]
        size += len(blob)
        verified = (
            sha256(blob) == package["sha256"]
            if scope["manager"] == "python"
            else "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
            == package["integrity"]
        )
        if size > 64 * 1024**2 or not verified:
            raise ValueError("Package hash mismatch or download budget exceeded")
        downloads.append(blob)
    files = (
        unpack_wheels(downloads)
        if scope["manager"] == "python"
        else unpack_npm(downloads, packages)
    )
    access.mkdir(destination, parents=True)
    # The completion receipt is written last. Failed/cancelled installs cannot
    # masquerade as complete and never change a previously installed environment.
    files["repo-agent-manifest.json"] = json.dumps(
        {"version": 1, "lock": scope, "files": {p: sha256(b) for p, b in files.items()}},
        sort_keys=True,
    ).encode()
    for name, data in files.items():
        checkpoint()
        target = destination / name
        access.mkdir(target.parent, parents=True, exist_ok=True)
        staged = access.stage(target, data, 0o644)
        try:
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    return {
        "directory": relative,
        "manager": scope["manager"],
        "status": "installed",
        "files": len(files) - 1,
        "usage": "Use explicit sys.path in sandboxed Python, or require() via the returned "
        "directory's node_modules path in sandboxed Node. No host code or lifecycle scripts ran.",
        "console_scripts_installed": False,
        "dependencies_resolved": False,
    }
