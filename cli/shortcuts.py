"""Platform-aware labels for terminal keys, not desktop application shortcuts."""

import sys

_MAC_LABELS = {
    "Enter": "Return",
    # Escape followed by Return works without configuring Option as Meta.
    "Alt+Enter": "Esc → Return",
    "F2": "fn+F2",
    "PgUp": "fn+↑",
    "PgDn": "fn+↓",
    "Ctrl+End": "Esc → g",
}


def shortcut_label(key: str) -> str:
    # CPU architecture cannot distinguish macOS from Windows/Linux on ARM.
    # Control sequences stay Control on macOS; Command is handled by the terminal.
    return _MAC_LABELS.get(key, key) if sys.platform == "darwin" else key


def shortcut_help() -> str:
    text = (
        f"{shortcut_label('Alt+Enter')} 换行；"
        f"{shortcut_label('F2')} 改名；"
        f"滚轮或 {shortcut_label('PgUp')}/{shortcut_label('PgDn')} 浏览历史；"
        f"{shortcut_label('Ctrl+End')} 恢复跟随。"
    )
    if sys.platform == "darwin":
        text += (
            "\nMac：Ctrl 指 Control（⌃）；→ 表示先按 Esc，松开后再按下一个键。"
            "Option+Return 需终端将 Option 配置为 Meta；"
            "F2 已设为标准功能键时可直接按。"
        )
    return text
