"""Compact presentation only; accounting, configuration and logs retain exact integers."""


def format_tokens(value, *, unknown="未返回"):
    if value is None:
        return unknown
    if value < 1000:
        return str(value)
    scale, suffix = (1_000_000, "M") if value >= 1_000_000 else (1000, "k")
    hundredths = (value * 100 + scale // 2) // scale
    if suffix == "k" and hundredths >= 100_000:
        scale, suffix = 1_000_000, "M"
        hundredths = (value * 100 + scale // 2) // scale
    whole, fraction = divmod(hundredths, 100)
    decimal = ("." + f"{fraction:02d}".rstrip("0")) if fraction else ""
    return f"{whole}{decimal}{suffix}"
