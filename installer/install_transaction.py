"""Durable install rollback. Venvs are built at their final path to preserve shebangs."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from host_support.paths import installed_command, public_command

from .installation import (
    COMMANDS,
    DEFAULT_IMAGE,
    MANIFEST,
    registry_dir,
    registry_name,
    write_json,
)

TRANSACTION = ".repo-agent-install-transaction"


def snapshot(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"无法备份非普通文件：{path}")
    return (
        None
        if not path.exists()
        else {
            "bytes": base64.b64encode(path.read_bytes()).decode(),
            "mode": path.stat().st_mode & 0o777,
        }
    )


def restore_file(path, value):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"文件已变化，未覆盖：{path}")
    if value is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".restore-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(base64.b64decode(value["bytes"]))
            os.chmod(temporary, value["mode"])
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class InstallTransaction:
    def __init__(self, root, bin_dir, states):
        self.root = root
        self.directory = root / TRANSACTION
        self.directory.mkdir(mode=0o700)
        self.state = {
            "root": str(root),
            "home": str(Path.home().resolve()),
            "committed": False,
            "bin_dir": str(bin_dir),
            "links": states,
            "changed_links": [],
            "shell": [],
            "records": {},
            "venv": None,
            "new_venv": None,
            "image": None,
            "staging_image": None,
        }
        try:
            local = root / MANIFEST
            if local.exists() and not local.is_symlink():
                previous = json.loads(local.read_text(encoding="utf-8"))
                if (
                    isinstance(previous, dict)
                    and previous.get("root") == str(root)
                    and previous.get("registry") != str(registry_dir() / registry_name(root))
                ):
                    raise ValueError("安装登记位置已改变；请恢复原 XDG_STATE_HOME 后重试")
            for path in (root / MANIFEST, registry_dir() / registry_name(root)):
                self.state["records"][str(path)] = snapshot(path)
            self.save()
        except BaseException:
            shutil.rmtree(self.directory)
            raise

    def save(self):
        write_json(self.directory / "state.json", self.state)

    def fresh_venv(self):
        venv = self.root / ".venv"
        if venv.exists():
            if venv.is_symlink() or not venv.is_dir():
                raise ValueError(".venv 必须为普通目录")
            info = venv.stat()
            self.state["venv"] = [info.st_dev, info.st_ino]
            self.save()
            venv.rename(self.directory / "venv")
        prepared = self.directory / "new-venv"
        prepared.mkdir(mode=0o700)
        info = prepared.stat()
        self.state["new_venv"] = [info.st_dev, info.st_ino]
        self.save()
        prepared.rename(venv)

    def change_link(self, name):
        if os.name == "nt":
            from host_support.windows_install import command_payloads

            payloads = command_payloads(
                public_command(Path(self.state["bin_dir"]), name),
                installed_command(self.root, name),
            )
            self.state.setdefault("windows_commands", {})[name] = {
                str(path): {"before": snapshot(path), "after": base64.b64encode(content).decode()}
                for path, content in payloads.items()
            }
        self.state["changed_links"].append(name)
        self.save()

    def append_shell(self, path, block):
        before = path.read_bytes() if path.exists() else b""
        self.state["shell"].append(
            {
                "path": str(path),
                "block": block,
                "created": not path.exists(),
                "length": len(before),
                "digest": hashlib.sha256(before).hexdigest(),
            }
        )
        self.save()

    def stage_image(self):
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", DEFAULT_IMAGE],
            capture_output=True,
            text=True,
            timeout=15,
        )
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ID}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        self.state["image"] = {
            "old": result.stdout.strip() if result.returncode == 0 else None,
            "new": None,
            "daemon": daemon,
        }
        self.state["staging_image"] = "repo-agent-install:" + uuid4().hex
        self.save()
        return self.state["staging_image"]

    def promote_image(self):
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.state["staging_image"]],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.state["image"]["new"] = result.stdout.strip()
        self.save()
        subprocess.run(
            ["docker", "tag", self.state["staging_image"], DEFAULT_IMAGE], check=True, timeout=15
        )

    def commit(self):
        self.state["committed"] = True
        try:
            self.save()
        except BaseException:
            self.state["committed"] = False
            raise
        try:
            self.cleanup()
        except OSError:
            entry = (
                "install_release.ps1"
                if os.name == "nt"
                else "install-release.sh"
                if (self.root / "release.json").is_file()
                else "install.sh"
            )
            print(f"安装已完成；旧环境备份清理未完成，可稍后执行 ./{entry} --recover。")

    def cleanup(self):
        tag = self.state["staging_image"]
        if tag:
            try:
                result = subprocess.run(
                    ["docker", "image", "rm", tag], capture_output=True, timeout=15
                )
                if result.returncode:
                    print(f"临时镜像标签已保留，可稍后手动清理：{tag}")
            except (OSError, subprocess.SubprocessError):
                print(f"临时镜像标签已保留，可稍后手动清理：{tag}")
        # Keep the journal until directory cleanup is complete, so cleanup failures are retryable.
        for path in self.directory.iterdir():
            if path.name == "state.json":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        (self.directory / "state.json").unlink()
        self.directory.rmdir()

    def rollback(self):
        errors = []

        def attempt(action):
            try:
                action()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(str(error))

        # Restore links first, but never overwrite a target changed by someone else.
        def link(name):
            if os.name == "nt":
                entries = self.state.get("windows_commands", {}).get(name)
                if entries is None:
                    raise ValueError("缺少 Windows 命令恢复记录")
                for filename, entry in entries.items():
                    path = Path(filename)
                    current = snapshot(path)
                    if current == entry["before"]:
                        continue
                    if current is None or current["bytes"] != entry["after"]:
                        raise ValueError(f"命令已被外部修改，未覆盖：{path}")
                    restore_file(path, entry["before"])
                return
            path = Path(self.state["bin_dir"]) / name
            old = self.state["links"][name]
            current = os.readlink(path) if path.is_symlink() else None
            if current == old and (current is not None or not path.exists()):
                return
            if current != str(installed_command(self.root, name)):
                raise ValueError(f"命令已被外部修改，未覆盖：{path}")
            if old is None:
                path.unlink()
            else:
                # Use a temporary link in the command directory for cross-filesystem installs.
                with tempfile.TemporaryDirectory(dir=path.parent) as directory:
                    replacement = Path(directory) / name
                    replacement.symlink_to(old)
                    os.replace(replacement, path)

        for name in self.state["changed_links"]:
            attempt(lambda name=name: link(name))
        for change in reversed(self.state.get("windows_path", [])):
            from host_support.windows_install import apply_path_change

            attempt(lambda change=change: apply_path_change(change, restore=True))
        for item in reversed(self.state["shell"]):

            def shell(item=item):
                path = Path(item["path"])
                if not path.exists():
                    return
                if path.is_symlink():
                    raise ValueError(f"shell 文件已变化，未覆盖：{path}")
                content = path.read_bytes()
                block = item["block"].encode()
                if block in content:
                    remaining = content.replace(block, b"", 1)
                else:
                    prefix, suffix = content[: item["length"]], content[item["length"] :]
                    if hashlib.sha256(prefix).hexdigest() != item["digest"] or not block.startswith(
                        suffix
                    ):
                        raise ValueError(f"shell 配置块已被外部修改，保留恢复记录：{path}")
                    remaining = prefix  # Also undo an interrupted/partial append.
                if item["created"] and not remaining:
                    path.unlink()
                elif content != remaining:
                    path.write_bytes(remaining)

            attempt(shell)

        def venv():
            path = self.root / ".venv"
            backup = self.directory / "venv"
            if self.state["new_venv"] and path.exists():
                info = path.stat()
                identity = [info.st_dev, info.st_ino]
                if identity == self.state["venv"] and not backup.exists():
                    return  # Already restored by a previous recovery attempt.
                if path.is_symlink() or identity != self.state["new_venv"]:
                    raise ValueError(".venv 已被外部替换，保留备份并停止恢复")
                shutil.rmtree(path)
            if backup.exists() or backup.is_symlink():
                info = backup.stat()
                if backup.is_symlink() or [info.st_dev, info.st_ino] != self.state["venv"]:
                    raise ValueError("旧环境备份已被外部替换，停止恢复")
                if path.exists() or path.is_symlink():
                    raise ValueError(".venv 被占用，保留旧环境备份")
                backup.rename(path)

        attempt(venv)

        def image():
            info = self.state["image"]
            if not info or not info["new"]:
                return
            daemon = subprocess.run(
                ["docker", "info", "--format", "{{.ID}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            if daemon != info["daemon"]:
                raise ValueError("Docker 主机已改变，保留镜像恢复记录")
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", DEFAULT_IMAGE],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip() == info["new"]:
                command = (
                    ["docker", "tag", info["old"], DEFAULT_IMAGE]
                    if info["old"]
                    else ["docker", "image", "rm", DEFAULT_IMAGE]
                )
                subprocess.run(command, check=True, capture_output=True, timeout=15)

        attempt(image)
        if not errors:
            for path, value in self.state["records"].items():
                attempt(lambda path=path, value=value: restore_file(Path(path), value))
        if errors:
            raise ValueError(
                "恢复未完成，保留恢复记录；重新运行安装入口并加 --recover 重试："
                + "; ".join(errors)
            )
        self.cleanup()
        print("已恢复安装前的环境、命令和 PATH；用户配置及其备份保留。")


def recover_install(root):
    directory = root / TRANSACTION
    if not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink() or (directory / "state.json").is_symlink():
        raise ValueError("安装恢复目录不能是符号链接")
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("无法识别安装恢复记录")
    if state.get("root") != str(root) or state.get("home") != str(Path.home().resolve()):
        raise ValueError("恢复记录属于其他目录或用户；请先手动移走复制来的恢复目录")
    expected = {str(root / MANIFEST), str(registry_dir() / registry_name(root))}
    if set(state.get("records", {})) != expected:
        raise ValueError("恢复记录的登记位置与当前环境不符；请恢复原 XDG_STATE_HOME 后重试")
    try:
        if (
            type(state["committed"]) is not bool
            or not Path(state["bin_dir"]).is_absolute()
            or Path(state["bin_dir"]).resolve() != Path(state["bin_dir"])
            or set(state["links"]) != set(COMMANDS)
            or any(
                value is not None and not isinstance(value, str)
                for value in state["links"].values()
            )
            or not isinstance(state["changed_links"], list)
            or any(name not in COMMANDS for name in state["changed_links"])
            or not isinstance(state["shell"], list)
        ):
            raise ValueError
        for item in state["shell"]:
            path = Path(item["path"])
            if (
                not path.is_absolute()
                or path.parent.resolve() != path.parent
                or not re.fullmatch(
                    r"\n# >>> Repo Agent [0-9a-f]{32}\n[^\n]*\n# <<< Repo Agent [0-9a-f]{32}\n",
                    item["block"],
                )
                or type(item["created"]) is not bool
                or type(item["length"]) is not int
                or item["length"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", item["digest"])
            ):
                raise ValueError
        for key in ("venv", "new_venv"):
            identity = state[key]
            if identity is not None and (
                not isinstance(identity, list)
                or len(identity) != 2
                or any(type(number) is not int or number < 0 for number in identity)
            ):
                raise ValueError
        for value in state["records"].values():
            if value is not None:
                base64.b64decode(value["bytes"], validate=True)
                if type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o777:
                    raise ValueError
        if "windows_commands" in state:
            from host_support.windows_install import command_receipt

            for name, entries in state["windows_commands"].items():
                if os.name != "nt" or name not in state["changed_links"]:
                    raise ValueError
                command = public_command(Path(state["bin_dir"]), name)
                if set(entries) != {str(command), str(command_receipt(command))}:
                    raise ValueError
                for entry in entries.values():
                    base64.b64decode(entry["after"], validate=True)
                    before = entry["before"]
                    if before is not None:
                        base64.b64decode(before["bytes"], validate=True)
                        if type(before["mode"]) is not int or not 0 <= before["mode"] <= 0o777:
                            raise ValueError
        for change in state.get("windows_path", []):
            from host_support.windows_install import validate_path_change

            if os.name != "nt" or change["bin_dir"] != state["bin_dir"]:
                raise ValueError
            validate_path_change(change)
        tag = state["staging_image"]
        if tag is not None and not re.fullmatch(r"repo-agent-install:[0-9a-f]{32}", tag):
            raise ValueError
        info = state["image"]
        if info is not None:
            if not isinstance(info["daemon"], str) or not info["daemon"]:
                raise ValueError
            if any(
                info[key] is not None and not re.fullmatch(r"sha256:[0-9a-f]+", info[key])
                for key in ("old", "new")
            ):
                raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("安装恢复记录损坏或资源位置发生变化，未执行恢复") from None
    transaction = object.__new__(InstallTransaction)
    transaction.root, transaction.directory, transaction.state = root, directory, state
    if state["committed"]:
        transaction.cleanup()
    else:
        print("检测到中断的安装，正在恢复……")
        transaction.rollback()
