"""Private configuration backups; recovery works even when the current file is malformed."""

import os
import re
from datetime import datetime, timezone
from uuid import uuid4

from host_support.locking import file_lock
from host_support.storage import atomic_write

BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.env")


def config_lock(path):
    return file_lock(path.parent / ".config.lock")


def read_bytes(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("用户配置必须是普通文件")
    return path.read_bytes() if path.exists() else None


def backup_directory(path):
    directory = path.parent / ".env.backups"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("配置备份目录必须是普通目录")
    return directory


def backups(path):
    directory = backup_directory(path)
    return sorted(
        (
            p
            for p in directory.glob("*.env")
            if BACKUP_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        ),
        reverse=True,
    )


def create_backup(path, content):
    directory = backup_directory(path)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex + ".env"
    target = directory / name
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
    return target


def replace_config(path, content, before):
    """Caller holds config_lock. Preserve malformed content verbatim before replacing it."""
    if read_bytes(path) != before:
        raise ValueError("用户配置已被其他操作修改，请重试")
    backup = create_backup(path, before) if before is not None and before != content else None
    def unchanged():
        if read_bytes(path) != before:
            raise ValueError("用户配置已被其他操作修改，请重试")

    atomic_write(path, content, prefix=".model-settings-", before_replace=unchanged)
    return backup


def backup_config(path):
    with config_lock(path):
        content = read_bytes(path)
        if content is None:
            raise ValueError("用户配置尚不存在，无需备份")
        return create_backup(path, content)


def restore_config(path, name, validate):
    if not BACKUP_NAME.fullmatch(name):
        raise ValueError("请使用 config backups 列出的备份名")
    with config_lock(path):
        source = backup_directory(path) / name
        content = read_bytes(source)
        if content is None:
            raise ValueError("备份不存在")
        validate(source)
        return replace_config(path, content, read_bytes(path))


def reset_config(path, template):
    with config_lock(path):
        return replace_config(path, template.read_bytes(), read_bytes(path))
