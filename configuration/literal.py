"""Literal .env parsing and template merging, usable by stdlib-only installers."""

import re

ASSIGNMENT = re.compile(r"\s*(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)")


def parse_config(text, *, source, keys=None):
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError(f"{source} 第 {number} 行格式错误，应为 KEY=VALUE")
        key, value = match.groups()
        if keys is not None and key not in keys:
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"{source} 第 {number} 行引号不匹配")
            value = value[1:-1]
        if "\x00" in value:
            raise ValueError(f"{source} 第 {number} 行包含不支持的字符")
        values[key] = value
    return values


def merge_template(template, previous, *, template_path, config_path):
    """Keep the new template's keys/comments and the old file's explicit values."""
    defaults = parse_config(template, source=template_path)
    values = parse_config(previous, source=config_path, keys=defaults)
    lines = []
    for line in template.splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match and match[1] in values and values[match[1]] != defaults[match[1]]:
            key = match[1]
            # The parser strips only the outer quotes; embedded quotes and backslashes
            # are literal, just as in configuration.environment.save_user_config.
            line = f'{key}="{values[key]}"'
        lines.append(line)
    return "\n".join(lines) + "\n"
