"""Sharding must retain every Windows native case, exactly once per Python."""

import ast
from collections import Counter
from pathlib import Path

import yaml


def test_windows_native_groups_cover_all_parameter_cases():
    root = Path(__file__).resolve().parents[1]
    source = "tests/test_windows_native_kernel.py"
    expected = []
    for node in ast.parse((root / source).read_text()).body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        cases = [""]
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "parametrize"
            ):
                cases = [f"[{value}]" for value in ast.literal_eval(decorator.args[1])]
        expected.extend(f"{source}::{node.name}{case}" for case in cases)
    workflow = yaml.safe_load((root / ".github/workflows/host-files.yml").read_text())
    matrix = workflow["jobs"]["windows-native"]["strategy"]["matrix"]
    assert matrix["python"] == ["3.11", "3.13"]
    groups = matrix["include"]
    assert Counter(group["group"] for group in groups) == Counter(matrix["group"])
    actual = [case for group in groups for case in group["tests"].split()]
    assert Counter(actual) == Counter(expected)
