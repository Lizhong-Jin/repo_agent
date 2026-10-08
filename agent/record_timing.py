"""Millisecond precision for owned diagnostic seconds at serialization boundaries.

Never apply these helpers to model messages, tool arguments/results or provider state.
Timers and aggregates retain their original precision in memory.
"""


def seconds(value):
    """Keep None/integers intact; JSON still stores numbers, never formatted strings."""
    return round(value, 3) if isinstance(value, float) else value


def timing_fields(record):
    """Round only the timing fields of one owned record, without entering its payloads."""
    return {
        key: seconds(value) if key.endswith("_seconds") else value for key, value in record.items()
    }


def metric_seconds(value):
    """Copy a report's diagnostic metrics tree, excluding all execution evidence."""
    if isinstance(value, dict):
        return {
            key: seconds(item) if key.endswith("_seconds") else metric_seconds(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [metric_seconds(item) for item in value]
    return value


def format_seconds(value):
    return "未返回" if value is None else f"{value:.3f}s"
