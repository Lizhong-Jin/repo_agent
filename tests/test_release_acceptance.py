"""Release gates must fail closed, even when pytest or a prerequisite looks green."""

import json
from pathlib import Path

import pytest

from scripts import release_acceptance as acceptance


@pytest.fixture
def evidence(tmp_path):
    identity = {"commit": "a" * 40, "version": "0.1.5", "run_id": "123", "run_attempt": "1"}
    needs = {job: {"result": "success"} for job in acceptance.JOBS}
    for target in acceptance.TARGETS:
        for profile in ("package", "native"):
            if profile == "native" and target.startswith("windows"):
                continue
            directory = tmp_path / "input" / f"{profile}-{target}"
            directory.mkdir(parents=True)
            required = acceptance.selectors(profile, target)
            cases = []
            for selector in required:
                file, name = selector.split("::")
                parameters = (None,)
                if name == "test_real_release_survives_download_removal":
                    parameters = ("False", "True")
                elif name == "test_real_unreadable_system_directory_is_hidden_on_all_mount_paths":
                    parameters = ("0", "73")
                for parameter in parameters:
                    suffix = f"[{parameter}]" if parameter else ""
                    cases.append(
                        f'<testcase classname="{file[:-3].replace("/", ".")}" '
                        f'name="{name}{suffix}"/>'
                    )
            junit = directory / "tests.xml"
            junit.write_text(
                "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>"
            )
            report = {
                "schema": 1,
                **identity,
                "profile": profile,
                "target": target,
                "status": "passed",
                "exit_code": 0,
                "junit_sha256": acceptance.sha256(junit),
                "tests": acceptance.junit_results(junit, required),
            }
            if profile == "package":
                report["upgrade_from"] = {
                    "name": f"repo-agent-0.1.4-{target}"
                    + (".zip" if target.startswith("windows") else ".tar.gz"),
                    "sha256": "b" * 64,
                }
                suffix = ".zip" if target.startswith("windows") else ".tar.gz"
                asset = directory / f"repo-agent-0.1.5-{target}{suffix}"
                asset.write_bytes(b"test asset")
                report["asset"] = {"name": asset.name, "sha256": acceptance.sha256(asset)}
            acceptance.write_json(directory / "acceptance.json", report)
    return tmp_path / "input", tmp_path / "output", needs, identity


def test_gate_preserves_exact_tested_bytes(evidence):
    source, output, needs, identity = evidence
    manifest = acceptance.gate(source, output, needs, identity)
    assert len(manifest["assets"]) == 5 and len(manifest["evidence"]) == 9
    assert all((output / a["name"]).read_bytes() == b"test asset" for a in manifest["assets"])


@pytest.mark.parametrize("state", ["failure", "skipped", "cancelled", None])
def test_gate_requires_every_job(evidence, state):
    source, output, needs, identity = evidence
    if state is None:
        del needs["native"]
    else:
        needs["native"]["result"] = state
    with pytest.raises(ValueError, match="workflow"):
        acceptance.gate(source, output, needs, identity)
    assert not output.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "commit",
        "run_attempt",
        "version",
        "asset",
        "junit",
        "status",
        "exit_code",
        "traversal",
    ],
)
def test_gate_rejects_stale_incomplete_or_changed_evidence(evidence, damage):
    source, output, needs, identity = evidence
    path = source / "package-linux-arm64/acceptance.json"
    report = json.loads(path.read_text())
    if damage == "missing":
        path.unlink()
    elif damage == "duplicate":
        acceptance.write_json(source / "zz-extra/acceptance.json", report)
    elif damage == "asset":
        (path.parent / report["asset"]["name"]).write_bytes(b"changed")
    elif damage == "junit":
        (path.parent / "tests.xml").write_text("<testsuites/>")
    else:
        if damage == "traversal":
            report["asset"]["name"] = "../outside.tar.gz"
        else:
            report[damage] = 1 if damage == "exit_code" else "wrong"
        acceptance.write_json(path, report)
    with pytest.raises(ValueError):
        acceptance.gate(source, output, needs, identity)
    assert not output.exists()


@pytest.mark.parametrize("child", ["<skipped/>", "<failure/>", "<error/>"])
def test_mandatory_tests_cannot_skip(tmp_path, child):
    path = tmp_path / "tests.xml"
    path.write_text(f'<testsuite><testcase name="required">{child}</testcase></testsuite>')
    with pytest.raises(ValueError, match="Mandatory"):
        acceptance.junit_results(path, ["tests/example.py::required"])


def test_zero_and_unrelated_tests_do_not_qualify(tmp_path):
    path = tmp_path / "tests.xml"
    for content in ("<testsuite/>", '<testsuite><testcase name="other"/></testsuite>'):
        path.write_text(content)
        with pytest.raises(ValueError):
            acceptance.junit_results(path, ["tests/example.py::required"])


def test_failed_preflight_still_writes_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "host_target", lambda: "windows-x86_64")
    assert not acceptance.run("native", "linux-arm64", tmp_path)
    report = json.loads((tmp_path / "acceptance.json").read_text())
    assert report["status"] == "failed" and "actual platform" in report["error"]


def test_workflows_use_same_commit_reusables_and_always_gate():
    import yaml

    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    release = yaml.safe_load((workflows / "release-acceptance.yml").read_text())
    jobs = release["jobs"]
    assert set(jobs["release-gate"]["needs"]) == acceptance.JOBS
    assert jobs["release-gate"]["if"] == "always()"
    targets = {entry["target"] for entry in jobs["packages"]["strategy"]["matrix"]["include"]}
    assert targets == set(acceptance.TARGETS)
    for name in ("source", "rust", "wheels", "docker", "native"):
        assert jobs[name]["uses"].startswith("./.github/workflows/")


@pytest.mark.parametrize("requested,expected", [("auto", "v0.1.4"), ("v0.1.3", "v0.1.3")])
def test_upgrade_baseline_is_older_stable_only(requested, expected):
    from scripts.release_baseline import select

    releases = [
        {"tagName": tag, "isDraft": False, "isPrerelease": False}
        for tag in ("v0.1.3", "v0.1.4", "v0.1.5", "v0.2.0", "v0.1.5rc1", "other")
    ]
    assert select(releases, "0.1.5", requested) == expected
    with pytest.raises(ValueError):
        select(releases, "0.1.5", "v0.2.0")


def test_upgrade_baseline_cannot_silently_use_draft_or_prerelease():
    from scripts.release_baseline import select

    for field in ("isDraft", "isPrerelease"):
        release = {"tagName": "v0.1.4", "isDraft": False, "isPrerelease": False, field: True}
        with pytest.raises(ValueError):
            select([release], "0.1.5")


def test_parameter_case_cannot_be_replaced_with_duplicate(tmp_path):
    path = tmp_path / "tests.xml"
    case = (
        '<testcase classname="tests.test_release_distribution" '
        'name="test_real_release_survives_download_removal[False]"/>'
    )
    path.write_text("<testsuite>" + case * 2 + "</testsuite>")
    with pytest.raises(ValueError, match="parameter"):
        acceptance.junit_results(
            path,
            ["tests/test_release_distribution.py::test_real_release_survives_download_removal"],
        )


def test_all_workflows_parse_and_reusable_targets_exist():
    import yaml

    root = Path(__file__).resolve().parents[1]
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert isinstance(workflow["jobs"], dict), path
        for job in workflow["jobs"].values():
            if job.get("uses", "").startswith("./"):
                called = yaml.safe_load((root / job["uses"]).read_text())
                assert "workflow_call" in called.get("on", called.get(True, {}))
    targets = {
        line.split()[0]
        for line in (root / "runtime/python.lock").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert targets == set(acceptance.TARGETS)


def test_all_platforms_must_use_the_same_upgrade_baseline(evidence):
    source, output, needs, identity = evidence
    path = source / "package-linux-arm64/acceptance.json"
    report = json.loads(path.read_text())
    report["upgrade_from"]["name"] = "repo-agent-0.1.3-linux-arm64.tar.gz"
    acceptance.write_json(path, report)
    with pytest.raises(ValueError, match="different upgrade"):
        acceptance.gate(source, output, needs, identity)
    assert not output.exists()
