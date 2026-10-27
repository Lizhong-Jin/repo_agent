"""Ambiguous external mutations need durable identities and separately approved GETs."""

import json
from copy import deepcopy

import pytest
from test_scoped_services import HTTP, approve, github_responses, publish_arguments, tool
from test_scoped_services import service as service

from host_support.execution_receipt import PersistenceError
from tools._internal.operation_journal import OperationJournal, fingerprint


@pytest.fixture
def publication(service):
    args = publish_arguments(service)
    github_responses(service)
    action = tool(service, "github")
    approve(service, action.execute(args))
    return args, action


def test_patch_timeout_preserves_candidate_before_transmission(service, publication):
    args, action = publication
    normal = service.http.respond

    def timeout(method, url, **kw):
        if method == "PATCH":
            (record,) = service.journal.records()
            assert record["stage"] == "mutation_requested"
            assert record["candidate_commit"] == kw["payload"]["sha"]
            raise TimeoutError("secret must not appear")
        return normal(method, url, **kw)

    service.http.respond = timeout
    result = action.execute(args)
    assert not result.success and "secret must not appear" not in str(result)
    (record,) = service.journal.records()
    assert record["stage"] == "mutation_requested"
    assert "test-secret-token" not in json.dumps(record)
    service.http.calls.clear()
    service.http.respond = lambda *a, **kw: json.dumps({"object": {"sha": "d" * 40}}).encode()
    check = {"operation": "verify_operation", "repo": args["repo"], "operation_id": record["id"]}
    approve(service, action.execute(check))
    confirmed = action.execute(check)
    assert confirmed.success and confirmed.data["state"] == "confirmed"
    assert all(call[0][0] == "GET" for call in service.http.calls)
    assert service.journal.read(record["id"])["stage"] == "mutation_requested"
    assert not service.manager.profile.operations


def test_identical_unresolved_write_cannot_be_resubmitted(service, publication):
    args, action = publication
    normal = service.http.respond
    service.http.respond = lambda method, url, **kw: (
        (_ for _ in ()).throw(TimeoutError()) if method == "PATCH" else normal(method, url, **kw)
    )
    assert not action.execute(args).success
    service.http.calls.clear()
    approve(service, action.execute(args))
    assert not action.execute(args).success
    assert not service.http.calls and len(service.journal.records()) == 1


@pytest.mark.parametrize(
    "stage", ["prepared", "objects_created", "mutation_requested", "completed"]
)
def test_storage_failure_stops_dispatch_and_never_sends_without_durable_intent(
    service, publication, monkeypatch, stage
):
    args, action = publication
    save = service.journal.save

    def fail(record):
        if record["stage"] == stage:
            raise PersistenceError("disk full")
        save(record)

    monkeypatch.setattr(service.journal, "save", fail)
    with pytest.raises(PersistenceError):
        action.execute(args)
    assert not service.manager.profile.operations
    if stage != "completed":
        assert all(call[0][0] != "PATCH" for call in service.http.calls)
    if stage == "prepared":
        assert not service.http.calls
    else:
        assert service.journal.records()


def pr_args():
    return {
        "operation": "create_pull_request",
        "repo": "owner/project",
        "head": "feature",
        "base": "main",
        "title": "Reviewed title",
        "body": "Approved body",
    }


def test_pr_timeout_without_id_never_confirms_similar_pr(service):
    args, action = pr_args(), tool(service, "github")
    approve(service, action.execute(args))
    service.http.respond = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
    assert not action.execute(args).success
    (record,) = service.journal.records()
    service.http.calls.clear()
    service.http.respond = lambda *a, **kw: json.dumps(
        [{"number": 17, "title": args["title"], "body": args["body"]}]
    ).encode()
    result = service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert result["state"] == "unknown" and result["candidates"] == [17]
    assert len(service.http.calls) == 1 and service.http.calls[0][0][0] == "GET"


def test_pr_response_id_survives_restart_and_verifies_identity(service):
    args, action = pr_args(), tool(service, "github")
    approve(service, action.execute(args))
    service.http.respond = lambda *a, **kw: b'{"number":17,"html_url":"untrusted"}'
    assert action.execute(args).success
    (record,) = service.journal.records()
    assert record["number"] == 17
    restored = OperationJournal(service.journal.directory.parent.parent, service.journal.session_id)
    service.http.respond = lambda *a, **kw: (
        b'{"number":17,"base":{"repo":{"full_name":"owner/project"}}}'
    )
    assert (
        restored.verify(record["id"], service.github, expected=fingerprint(record))["state"]
        == "confirmed"
    )


@pytest.mark.parametrize("remote", ["descendant", "diverged", "moved", "same_base"])
def test_publication_verification_checks_ancestry_and_branch_race(service, publication, remote):
    args, action = publication
    assert action.execute(args).success
    (record,) = service.journal.records()
    refs = 0

    def response(method, url, **kw):
        nonlocal refs
        assert method == "GET"
        if "/git/ref/" in url:
            refs += 1
            head = "a" if remote == "same_base" or remote == "moved" and refs == 2 else "e"
            return json.dumps({"object": {"sha": head * 40}}).encode()
        return json.dumps(
            {
                "status": "ahead" if remote in {"descendant", "moved"} else "diverged",
                "merge_base_commit": {"sha": "d" * 40},
            }
        ).encode()

    service.http.respond = response
    result = service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert result["state"] == ("confirmed" if remote == "descendant" else "unknown")


def test_verification_requires_new_approval_after_record_changes(service, publication):
    args, action = publication
    assert action.execute(args).success
    (record,) = service.journal.records()
    service.http.calls.clear()
    check = {"operation": "verify_operation", "repo": args["repo"], "operation_id": record["id"]}
    approve(service, action.execute(check))
    service.journal.update(record, verification={"state": "unknown"})
    result = action.execute(check)
    assert result.data["permission_required"] and not service.http.calls
    wrong = deepcopy(check)
    wrong["repo"] = "other/repo"
    assert not action.execute(wrong).success


def test_failed_verification_preserves_previous_receipt(service, publication):
    args, action = publication
    assert action.execute(args).success
    (record,) = service.journal.records()
    service.http.respond = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert service.journal.read(record["id"]) == record


def test_recovery_network_action_is_explicit_and_get_only(open_conversation):
    from agent.recovery import Recovery
    from tools._internal.github_service import GitHubService

    conversation = open_conversation()
    journal = OperationJournal(conversation.store.directory, conversation.store.id)
    record = journal.begin(pr_args())
    journal.update(record, stage="response_received", number=23)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    action = "verify:" + record["id"]
    with pytest.raises(ValueError, match="allow-network"):
        recovery.apply(plan["plan"], action)
    http = HTTP()
    http.respond = lambda *a, **kw: b'{"number":23,"base":{"repo":{"full_name":"owner/project"}}}'
    report = recovery.apply(plan["plan"], action, github=GitHubService(http))
    assert report["operations"][0]["verification"] == "confirmed"
    assert all(call[0][0] == "GET" for call in http.calls)
    with pytest.raises(ValueError, match="过期"):
        recovery.apply(plan["plan"], action, github=GitHubService(http))


def test_cancel_after_request_keeps_unknown_record_and_consumes_grant(service):
    from host_support.cancellation import (
        CancellationContext,
        RunCancelled,
        cancellation_scope,
        checkpoint,
    )

    args, action = pr_args(), tool(service, "github")
    approve(service, action.execute(args))
    context = CancellationContext()

    def cancel(*a, **kw):
        context.cancel()
        checkpoint()

    service.http.respond = cancel
    with cancellation_scope(context), pytest.raises(RunCancelled):
        action.execute(args)
    (record,) = service.journal.records()
    assert record["stage"] == "mutation_requested" and not service.manager.profile.operations


def test_record_corruption_or_foreign_session_cannot_drive_network(service):
    record = service.journal.begin(pr_args())
    path = service.journal.directory / (record["id"] + ".json")
    record["session_id"] = "a" * 32
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert not service.http.calls


def test_not_submitted_is_only_derived_from_local_durable_stage(service):
    record = service.journal.begin(pr_args())
    result = service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert result["state"] == "not_submitted" and not service.http.calls


def test_absent_pr_candidate_does_not_prove_non_creation(service):
    record = service.journal.begin(pr_args())
    service.journal.update(record, stage="mutation_requested")
    service.http.respond = lambda *a, **kw: b"[]"
    result = service.journal.verify(record["id"], service.github, expected=fingerprint(record))
    assert result["state"] == "unknown" and result["candidates"] == []


def test_operation_journal_follows_explicit_new_session(tmp_path):
    current = ["a" * 32]
    journal = OperationJournal(tmp_path, lambda: current[0])
    first = journal.begin(pr_args())
    current[0] = "b" * 32
    assert journal.records() == []
    second = journal.begin(pr_args())
    assert second["session_id"] != first["session_id"]
    with pytest.raises(FileNotFoundError):
        journal.read(first["id"])
