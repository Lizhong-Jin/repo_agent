"""Windows user command/PATH integration; callers own journals and approval."""

import hashlib
import json
import os
from pathlib import Path

from .storage import atomic_write


def command_receipt(command):
    return command.with_name(command.name + ".repo-agent.json")


def command_payloads(command, target):
    content = target.read_bytes()
    if not content.startswith(b"MZ"):
        raise ValueError(f"Not a Windows launcher: {target}")
    receipt = {"version": 1, "target": str(target), "sha256": hashlib.sha256(content).hexdigest()}
    return {
        command: content,
        command_receipt(command): (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8"),
    }


def command_state(command):
    receipt = command_receipt(command)
    if not command.exists() and not receipt.exists():
        return None
    try:
        if command.is_symlink() or receipt.is_symlink():
            raise ValueError
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            record.get("version") != 1
            or not Path(record["target"]).is_absolute()
            or hashlib.sha256(command.read_bytes()).hexdigest() != record["sha256"]
        ):
            raise ValueError
        return record["target"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError(
            f"保留已有命令 {command}（归属或内容已改变）；请选择其他 --bin-dir"
        ) from None


def publish_command(command, target):
    for path, content in command_payloads(command, target).items():
        atomic_write(path, content, prefix=".command-", sync=True)


def read_user_path():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return {"value": None, "type": winreg.REG_EXPAND_SZ}
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        raise ValueError("用户 PATH 注册表类型不受支持")
    return {"value": value, "type": kind}


def write_user_path(value):
    import ctypes
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        if value["value"] is None:
            try:
                winreg.DeleteValue(key, "Path")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "Path", 0, value["type"], value["value"])
    # Notify other applications; this does not change the parent terminal's PATH.
    send = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
    send.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = ctypes.c_void_p
    result = ctypes.c_size_t()
    send(0xFFFF, 0x1A, 0, "Environment", 2, 2000, ctypes.byref(result))


def path_change(bin_dir):
    if ";" in str(bin_dir):
        raise ValueError("Windows PATH 目录不能包含分号；请使用 --no-path 或其他 --bin-dir")
    before = read_user_path()
    entries = (before["value"] or "").split(";")

    def normalized(value):
        return os.path.normcase(os.path.expandvars(value.strip('"'))).rstrip("\\/")

    if any(normalized(entry) == normalized(str(bin_dir)) for entry in entries if entry):
        return None
    value = (before["value"] or "").rstrip(";")
    return {
        "before": before,
        "after": {"value": value + (";" if value else "") + str(bin_dir), "type": before["type"]},
        "bin_dir": str(bin_dir),
    }


def apply_path_change(change, *, restore=False):
    before, after = (
        (change["after"], change["before"]) if restore else (change["before"], change["after"])
    )
    current = read_user_path()
    if current == after:
        return
    if current != before:
        raise ValueError("用户 PATH 已被其他程序修改，保留记录，请手动检查")
    write_user_path(after)


def validate_path_change(change):
    if (
        not isinstance(change, dict)
        or not isinstance(change.get("bin_dir"), str)
        or not Path(change["bin_dir"]).is_absolute()
        or ";" in change["bin_dir"]
    ):
        raise ValueError("无效 Windows PATH 记录")
    for key in ("before", "after"):
        item = change.get(key)
        if (
            not isinstance(item, dict)
            or set(item) != {"type", "value"}
            or item["type"] not in (1, 2)
            or (item["value"] is not None and not isinstance(item["value"], str))
        ):
            raise ValueError("无效 Windows PATH 记录")
    before = (change["before"]["value"] or "").rstrip(";")
    expected = before + (";" if before else "") + change["bin_dir"]
    if change["after"]["value"] != expected or change["after"]["type"] != change["before"]["type"]:
        raise ValueError("Windows PATH 记录不能修改已有条目")
