"""Structured probe results with the legacy CLI tuple presentation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    level: str
    component: str
    detail: str
    location: str = "host"
    stage: str = "availability"

    def row(self):
        return self.level, self.component, self.detail


def report_ok(rows):
    return not any(
        (row.level if isinstance(row, Diagnostic) else row[0]) == "ERROR" for row in rows
    )


def print_diagnostics(rows):
    for row in rows:
        level, label, detail = row.row() if isinstance(row, Diagnostic) else row
        print(f"[{level}] {label}：{detail}")
    return report_ok(rows)
