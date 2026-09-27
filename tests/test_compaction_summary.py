"""Citations, bounded format repair and private failure evidence."""

import json
import stat
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_compaction import conversation as conversation  # shared integration fixture
from test_compaction import large_history

from agent.compaction_diagnostics import save_diagnostic
from agent.compaction_summary import (
    SECTIONS,
    SummaryValidationError,
    parse_summary,
    prepare_records,
    repair_messages_payload,
)
from agent.Tracing import RunTrace
from llm import Message
from llm.independent import IndependentRequestPolicy

REF = "a" * 32 + "/m00001"
OLD_REF = "b" * 32 + "/m00002"


def summary(ref="R1"):
    result = {key: [] for key in SECTIONS}
    result["progress"] = [{"text": "需继续验证", "refs": [ref]}]
    return result


def test_short_refs_only_authorize_top_level_records():
    body = json.dumps(summary(OLD_REF))
    wire, mapping = prepare_records(
        [
            {"ref": REF, "content": body, "tool_calls": [{"arguments": {"source": OLD_REF}}]},
            {"ref": REF, "fragment": "foreign source " + OLD_REF},
        ]
    )
    assert mapping == {"R1": REF, "R2": REF}
    assert OLD_REF not in json.dumps(wire)
    assert json.loads(wire[0]["content"])["progress"][0]["refs"] == ["R1"]
    assert wire[0]["tool_calls"][0]["arguments"]["source"] == "R1"
    candidate = summary()
    candidate["progress"][0]["refs"] = ["R1", "R2", "R1"]
    assert parse_summary(json.dumps(candidate), mapping) == summary(REF)
    for bad_ref in (REF, OLD_REF, "R3"):
        with pytest.raises(SummaryValidationError, match="REF_NOT_ALLOWED"):
            parse_summary(json.dumps(summary(bad_ref)), mapping)


@pytest.mark.parametrize(
    "wrap",
    [lambda text: text, lambda text: "\ufeff" + text, lambda text: "```json\n" + text + "\n```"],
)
def test_parse_exact_object_or_single_json_fence(wrap):
    assert parse_summary(wrap(json.dumps(summary())), {"R1": REF}) == summary(REF)


@pytest.mark.parametrize(
    "text,code,path",
    [
        ("secret invalid candidate", "JSON_PARSE", "$"),
        (json.dumps(list(SECTIONS)), "ROOT_TYPE", "$"),
        ('{"secret key": [], "secret key": []}', "DUPLICATE_KEY", "$"),
        ('{"secret key": NaN}', "JSON_CONSTANT", "$"),
        (json.dumps({"secret key": []}), "SECTION_KEYS", "$"),
        (json.dumps({**summary(), "progress": {}}), "SECTION_TYPE", "progress"),
        (json.dumps({**summary(), "progress": ["secret value"]}), "ITEM_FIELDS", "progress[0]"),
        (
            json.dumps({**summary(), "progress": [{"text": "", "refs": ["R1"]}]}),
            "TEXT_TYPE",
            "progress[0].text",
        ),
        (
            json.dumps({**summary(), "progress": [{"text": "secret", "refs": "R1"}]}),
            "REFS_TYPE",
            "progress[0].refs",
        ),
        (
            json.dumps({**summary(), "progress": [{"text": "secret", "refs": []}]}),
            "REFS_TYPE",
            "progress[0].refs",
        ),
        (json.dumps(summary("secret reference")), "REF_NOT_ALLOWED", "progress[0].refs[0]"),
        (json.dumps(summary({"secret": "reference"})), "REF_NOT_ALLOWED", "progress[0].refs[0]"),
        (json.dumps({key: [] for key in SECTIONS}), "EMPTY_SUMMARY", "$"),
        ("explanation\n```json\n" + json.dumps(summary()) + "\n```", "JSON_PARSE", "$"),
    ],
)
def test_validation_codes_are_specific_and_never_echo_candidate(text, code, path):
    with pytest.raises(SummaryValidationError) as caught:
        parse_summary(text, {"R1": REF})
    error = caught.value
    assert (error.code, error.path) == (code, path)
    assert "secret" not in str(error) + repr(error) + json.dumps(error.public())


def test_repair_payload_limits_sources_and_only_hints_known_refs():
    wire, mapping = prepare_records([{"ref": REF, "content": "source" * 10000 + OLD_REF}])
    candidate = json.dumps(summary(REF)) + OLD_REF
    payload = repair_messages_payload(
        candidate, SummaryValidationError("JSON_PARSE"), wire, mapping, 2048
    )
    assert payload["candidate"] == candidate
    assert payload["reference_hints"] == {REF: "R1"}
    assert len(payload["records"][0]["excerpt"]) == 512
    assert len(json.dumps(payload)) < len(json.dumps(wire)) / 10


def script_responses(conv, monkeypatch, responses):
    generate = conv.runtime.llm.generate
    pending = iter(responses)

    def respond(request):
        response = generate(request)
        action = next(pending)
        if isinstance(action, BaseException):
            raise action
        if action is None:
            return response
        if isinstance(action, tuple):
            content, finish = action
        else:
            content, finish = action, "stop"
        return replace(response, message=Message("assistant", content), finish_reason=finish)

    monkeypatch.setattr(conv.runtime.llm, "generate", respond)


def diagnostics(conv):
    directory = conv.store.directory / conv.store.id / "compaction-diagnostics"
    return {path: json.loads(path.read_text()) for path in directory.glob("*.json")}


def capture_events(conv):
    events = []

    def receive(name, stats):
        conv.status(name, stats)
        events.append((name, deepcopy(stats)))

    conv.runtime.on_event = receive
    return events


def test_one_repair_uses_excerpts_commits_and_counts_all_usage(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    conv.status.totals.update(input_tokens=500, output_tokens=50)
    script_responses(conv, monkeypatch, ["SECRET_FAILED_CANDIDATE", None])
    events = capture_events(conv)
    conv.compact()
    assert conv.history != large_history()
    assert conv.status.totals == {"input_tokens": 700, "output_tokens": 90}
    assert conv.store.data["status"]["calls"] == 2
    assert conv.runtime._task_number == 0
    requests = conv.runtime.llm.requests
    assert len(requests) == 2
    original, repair = [json.loads(r.messages[-1].content) for r in requests]
    assert repair["repair"] and repair["candidate"] == "SECRET_FAILED_CANDIDATE"
    assert all(len(r["excerpt"]) <= 512 for r in repair["records"])
    assert len(requests[1].messages[-1].content) < len(requests[0].messages[-1].content) / 5
    assert original["summary_token_target"] == 2048
    files = diagnostics(conv)
    assert len(files) == 1
    path, data = next(iter(files.items()))
    assert data["candidate"] == "SECRET_FAILED_CANDIDATE"
    assert data["validation_error"]["code"] == "JSON_PARSE"
    assert data["stage"] == "summary" and data["snapshot"]
    conv.archive.validate_refs(data["reference_map"].values())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "SECRET_FAILED_CANDIDATE" not in repr(events)
    assert "SECRET_FAILED_CANDIDATE" not in json.dumps(conv.store.data)
    calls = events[-1][1].model_calls
    assert [call.stage for call in calls] == ["summary", "repair"]
    assert all(call.purpose == "compaction" for call in calls)


def test_failed_repair_preserves_original_and_both_candidates(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    script_responses(conv, monkeypatch, ["SECRET_CANDIDATE", json.dumps(summary(OLD_REF))])
    events = capture_events(conv)
    with pytest.raises(SummaryValidationError, match="REF_NOT_ALLOWED") as caught:
        conv.compact()
    assert len(conv.runtime.llm.requests) == 2
    assert conv.history == large_history() and conv.compaction_state is None
    assert conv.status.totals == {"input_tokens": 200, "output_tokens": 40}
    files = diagnostics(conv)
    assert len(files) == 2
    assert caught.value.diagnostic_id in [path.stem for path in files]
    assert {d["stage"] for d in files.values()} == {"summary", "repair"}
    assert "SECRET_CANDIDATE" not in repr(events)
    assert OLD_REF not in str(caught.value)
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]


@pytest.mark.parametrize(
    "failure,code,output_tokens",
    [
        (ValueError("SECRET_TRANSPORT_BODY"), "REPAIR_REQUEST_FAILED", 20),
        (("SECRET_PARTIAL", "length"), "OUTPUT_TRUNCATED", 40),
    ],
)
def test_repair_is_bounded_even_on_transport_failure_or_truncation(
    conversation, monkeypatch, failure, code, output_tokens
):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    script_responses(conv, monkeypatch, ["SECRET_INITIAL", failure])
    events = capture_events(conv)
    with pytest.raises(SummaryValidationError) as caught:
        conv.compact()
    assert caught.value.code == code and caught.value.diagnostic_id
    assert len(conv.runtime.llm.requests) == 2
    assert conv.status.totals["output_tokens"] == output_tokens
    assert conv.history == large_history()
    assert "SECRET_" not in str(caught.value) + repr(events)
    assert [
        s.compaction["validation_error"]["code"] for n, s in events if n == "compaction_validation"
    ][-1] == code


def test_cancel_during_repair_keeps_history_and_first_call_usage(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    script_responses(conv, monkeypatch, ["invalid", KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        conv.compact()
    assert conv.history == large_history() and conv.compaction_state is None
    assert conv.status.totals == {"input_tokens": 100, "output_tokens": 20}
    assert len(diagnostics(conv)) == 1


def test_diagnostic_write_failure_is_visible_without_fake_id(conversation, monkeypatch):
    import agent.compaction as module

    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    script_responses(conv, monkeypatch, ["SECRET_INITIAL", "SECRET_REPAIR"])

    def fail(*args, **kwargs):
        raise OSError("SECRET_STORAGE_DETAIL")

    monkeypatch.setattr(module, "save_diagnostic", fail)
    events = capture_events(conv)
    with pytest.raises(SummaryValidationError) as caught:
        conv.compact()
    assert caught.value.diagnostic_id is None
    assert conv.history == large_history()
    assert not diagnostics(conv)
    assert any(s.compaction.get("diagnostic_save_error") == "OSError" for _, s in events)
    assert "SECRET_" not in str(caught.value) + repr(events)


@pytest.mark.parametrize("linked_component", ["session", "diagnostics"])
def test_diagnostic_writer_refuses_symlink_directories(tmp_path, linked_component):
    directory = tmp_path / "state"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = SimpleNamespace(directory=directory, id="a" * 32)
    session = directory / store.id
    if linked_component == "session":
        session.symlink_to(outside, target_is_directory=True)
    else:
        session.mkdir()
        (session / "compaction-diagnostics").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        save_diagnostic(store, {"candidate": "private"})
    assert not list(outside.iterdir())


def test_archive_failure_never_triggers_model_repair(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)

    def missing(*args):
        raise ValueError("archive missing")

    monkeypatch.setattr(conv.archive, "validate_refs", missing)
    with pytest.raises(ValueError, match="archive missing"):
        conv.compact()
    assert not conv.runtime.llm.requests
    assert conv.history == large_history()


def test_short_aliases_are_local_to_each_chunk_and_resolve_to_real_archive(conversation):
    conv = conversation()
    messages = [Message("user", "first"), Message("assistant", "second")]
    _, ids = conv.archive.archive(messages)
    policy = IndependentRequestPolicy.from_config(conv.runtime.llm.config)
    results = []
    with RunTrace(0, conv.status) as trace:
        for message_id, message in zip(ids, messages, strict=True):
            results.append(
                conv.compactor._summary(
                    [{"ref": conv.archive.ref(message_id), "content": message.content}],
                    trace,
                    policy,
                    256,
                )
            )
    assert [r["progress"][0]["refs"] for r in results] == [[conv.archive.ref(i)] for i in ids]
    assert all(
        json.loads(r.messages[-1].content)["records"][0]["ref"] == "R1"
        for r in conv.runtime.llm.requests
    )


def test_recompress_sanitizes_old_refs_but_archive_preserves_original(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    conv.compact()
    old_summary = conv.history[2]
    old_refs = [
        ref
        for items in json.loads(old_summary.content.split("\n", 1)[1]).values()
        for item in items
        for ref in item["refs"]
    ]
    conv.checkpoint(
        history=[
            *conv.history,
            Message("assistant", "new history " * 5000),
            Message("user", "继续"),
        ],
        strict=True,
    )
    conv.runtime.llm.requests.clear()
    conv.compact()
    assert conv.archive.search(old_summary.content[:100])["matches"]
    for request in conv.runtime.llm.requests:
        wire = json.loads(request.messages[-1].content)["records"]
        assert all(ref not in json.dumps(wire) for ref in old_refs)
    conv.archive.validate_refs(old_refs)


def test_chunk_planner_estimates_linear_amount_of_text(conversation, monkeypatch):
    import agent.compaction as module

    conv = conversation(window=1000000)
    policy = IndependentRequestPolicy.from_config(conv.runtime.llm.config)
    estimate = module.estimate_context_tokens
    observed = []

    def counted(messages, *args):
        observed.append(sum(len(message.content) for message in messages))
        return estimate(messages, *args)

    monkeypatch.setattr(module, "estimate_context_tokens", counted)
    monkeypatch.setattr(conv.compactor, "_summary", lambda *a, **k: summary(REF))
    costs = []
    for n in (100, 200, 400):
        observed.clear()
        records = [{"ref": REF, "content": '中文\\"data\n' * 100} for _ in range(n)]
        with RunTrace(0, None) as trace:
            conv.compactor._summarize(records, trace, policy, 2048)
        costs.append(sum(observed))
    assert costs[1] < costs[0] * 2.1
    assert costs[2] < costs[1] * 2.1


def test_full_request_guard_runs_before_model_even_for_repair(conversation):
    conv = conversation(window=4096)
    policy = IndependentRequestPolicy.from_config(conv.runtime.llm.config)
    messages = [Message("system", "repair"), Message("user", "中文" * 10000)]
    with RunTrace(0, None) as trace, pytest.raises(ValueError, match="摘要请求输入无法容纳"):
        conv.compactor._generate_summary(
            messages, trace, policy, stage="repair", retry_output=False
        )
    assert not conv.runtime.llm.requests


def test_diagnostic_failed_write_removes_partial_file(tmp_path, monkeypatch):
    import agent.compaction_diagnostics as module

    store = SimpleNamespace(directory=tmp_path, id="a" * 32)

    def fail(fd):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError, match="disk full"):
        save_diagnostic(store, {"candidate": "private"})
    assert not list((tmp_path / store.id / "compaction-diagnostics").iterdir())


@pytest.mark.parametrize("window", [12000, 24000])
def test_multichunk_unicode_and_escaped_sources_fit_final_requests(conversation, window):
    from llm.token_estimation import estimate_context_tokens

    conv = conversation(window=window)
    messages = [
        Message("user" if i % 2 == 0 else "assistant", '中文"\\\n' * 250)
        for i in range(16)
    ]
    _, ids = conv.archive.archive(messages)
    records = [
        {"ref": conv.archive.ref(i), "content": m.content}
        for i, m in zip(ids, messages, strict=True)
    ]
    policy = IndependentRequestPolicy.from_config(conv.runtime.llm.config)
    with RunTrace(0, conv.status) as trace:
        result = conv.compactor._summarize(records, trace, policy, 2048)
    requests = conv.runtime.llm.requests
    assert len(requests) > 1
    for request in requests:
        assert (
            estimate_context_tokens(request.messages)
            + request.max_output_tokens
            + conv.compactor.headroom(window)
        ) <= window
    conv.archive.validate_refs(
        ref for items in result.values() for item in items for ref in item["refs"]
    )
