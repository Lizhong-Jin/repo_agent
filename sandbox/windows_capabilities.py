"""Session-scoped observations, never permission grants or fallback decisions."""

import json

DOCKER_GUIDANCE = (
    "repo-agent --sandbox docker --sandbox-profile standard "
    "--sandbox-writeback manual --new-session"
)
LSP_TOOLS = frozenset(
    {
        "get_symbols",
        "go_to_definition",
        "find_references",
        "get_diagnostics",
        "get_hover",
        "search_workspace_symbols",
    }
)


class CapabilityUnavailable(Exception):
    def __init__(self, capability, report):
        self.capability = capability
        self.report = report


def assess_probe(capability, result, workspace):
    diagnostics = {
        "exit_code": result.exit_code,
        "exit_code_hex": (
            f"0x{result.exit_code & 0xFFFFFFFF:08X}" if result.exit_code is not None else None
        ),
        "timed_out": result.timed_out,
        "cleanup_status": result.cleanup_status,
        "stdout_truncated": result.stdout_truncated or len(result.stdout) > 4096,
        "stderr_truncated": result.stderr_truncated or len(result.stderr) > 4096,
        "stdout": result.stdout[:4096],
        "stderr": result.stderr[:4096],
    }
    if result.cleanup_status != "confirmed" or result.cleanup_error:
        return {"status": "error", "reason": "cleanup_unconfirmed", "diagnostics": diagnostics}
    valid = False
    if (
        result.exit_code == 0
        and not result.timed_out
        and not (result.stdout_truncated or result.stderr_truncated)
    ):
        if capability == "git":
            valid = result.stdout.strip() == str(workspace)
        else:
            try:
                payload = json.loads(result.stdout)
                valid = payload.get("success") is True and any(
                    item.get("name") == "native_capability_example"
                    for item in payload["data"]["symbols"]
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
    if valid:
        return {
            "status": "available",
            "scope": "git_discovery" if capability == "git" else "python_reference_lsp",
            "diagnostics": diagnostics,
        }
    text = (result.stdout + result.stderr).lower()
    complete = not (result.timed_out or result.stdout_truncated or result.stderr_truncated)
    compatibility = complete and (
        "unable to read current working directory: permission denied" in text
        or ("[winerror 5]" in text and "resolve" in text)
        or (result.exit_code is not None and result.exit_code & 0xFFFFFFFF == 0xC06D007E)
        or ("dll load failed" in text and "_ctypes" in text)
    )
    dependency = complete and "no module named 'pylsp'" in text
    return {
        "status": "unavailable" if compatibility or dependency else "error",
        "reason": "lpac_compatibility"
        if compatibility
        else "missing_dependency"
        if dependency
        else "probe_failed",
        "diagnostics": diagnostics,
    }
