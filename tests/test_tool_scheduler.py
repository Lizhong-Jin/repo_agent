"""Concurrency contracts, barriers and structured cancellation without timing races."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier, Event, Lock, get_ident

import pytest

from agent.tool_scheduler import ToolScheduler
from host_support.cancellation import (
    CancellationContext,
    RunCancelled,
    cancellation_scope,
    current_cancellation,
)
from host_support.execution_receipt import PersistenceError
from tools import ExecutionKind, ToolDispatcher
from tools.factory import create_default_tools
from tools.scheduling import INDEPENDENT, READ_ONLY, SERIAL, SchedulingPolicy, scheduling_policy_of


def schedule(calls, invoke, *, policy=lambda _: READ_ONLY, workers=4, finish=None):
    return list(
        ToolScheduler(workers).execute(
            calls,
            policy=policy,
            invoke=invoke,
            on_start=lambda call: call,
            on_finish=finish or (lambda *_: None),
        )
    )


def test_bounded_reads_preserve_order_and_never_cross_barriers():
    rendezvous = Barrier(2)
    lock = Lock()
    active = 0
    peak = 0
    completed = set()

    def invoke(call):
        nonlocal active, peak
        if call == 2:
            assert active == 0 and completed == {0, 1}
            completed.add(call)
            return call
        if call > 2:
            assert 2 in completed
        with lock:
            active += 1
            peak = max(peak, active)
        rendezvous.wait(timeout=3)
        with lock:
            active -= 1
            completed.add(call)
        return call

    assert schedule(range(5), invoke, policy=lambda c: SERIAL if c == 2 else READ_ONLY) == list(
        range(5)
    )
    assert peak == 2


def test_limit_and_single_worker_opt_out():
    rendezvous = Barrier(2)
    assert schedule(range(6), lambda i: (rendezvous.wait(3), i)[1], workers=2) == list(range(6))
    caller = get_ident()
    assert schedule(range(3), lambda _: get_ident(), workers=1) == [caller] * 3


def test_partial_consumption_leaves_no_workers_or_context_bound():
    before = current_cancellation()
    completed = []
    results = ToolScheduler(2).execute(
        [1, 2, 3],
        policy=lambda _: READ_ONLY,
        invoke=lambda c: c,
        on_start=lambda c: c,
        on_finish=lambda c, *_: completed.append(c),
    )
    assert next(results) == 1
    assert set(completed) == {1, 2, 3}
    assert current_cancellation() is before
    results.close()
    assert current_cancellation() is before


@pytest.mark.parametrize("workers", [0, -1, 33, True, 2.5])
def test_invalid_worker_limit(workers):
    with pytest.raises(ValueError):
        ToolScheduler(workers)


def test_context_is_copied_per_worker_and_callbacks_stay_on_coordinator():
    marker = ContextVar("marker", default="missing")
    marker.set("parent")
    rendezvous = Barrier(2)
    caller = get_ident()
    finishes = []
    with cancellation_scope() as context:

        def invoke(call):
            assert marker.get() == "parent"
            assert current_cancellation() is context
            marker.set(call)
            rendezvous.wait(3)
            return marker.get()

        result = schedule(
            ["a", "b"], invoke, finish=lambda *args: finishes.append((get_ident(), args))
        )
    assert result == ["a", "b"]
    assert marker.get() == "parent"
    assert {thread for thread, _ in finishes} == {caller}


def test_fatal_receipt_failure_cancels_and_joins_peers_before_raising():
    both_started = Barrier(2)
    cleaning = Event()
    release = Event()
    cleaned = Event()
    invoked = []

    def invoke(call):
        invoked.append(call)
        both_started.wait(3)
        if call == "fatal":
            raise PersistenceError("disk full")
        assert current_cancellation().event.wait(3)
        cleaning.set()
        assert release.wait(3)
        cleaned.set()
        current_cancellation().check()

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(schedule, ["slow", "fatal", "later"], invoke, workers=2)
        try:
            assert cleaning.wait(3)
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(PersistenceError, match="disk full"):
            future.result(timeout=3)
    assert cleaned.is_set() and "later" not in invoked


def test_external_cancellation_waits_for_all_inflight_calls():
    context = CancellationContext()
    both_started = Barrier(3)
    release = Event()
    finishes = []

    def invoke(call):
        both_started.wait(3)
        assert release.wait(3)
        return call

    def run():
        with cancellation_scope(context):
            return schedule([1, 2, 3], invoke, workers=2, finish=lambda *a: finishes.append(a))

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run)
        try:
            both_started.wait(3)
            context.cancel()
            assert not future.done()
        finally:
            release.set()
        with pytest.raises(RunCancelled):
            future.result(timeout=3)
    assert {call for call, _, _ in finishes} == {1, 2}


def test_undeclared_or_inherited_policy_remains_serial():
    class Explicit:
        scheduling_policy = INDEPENDENT

    class Subclass(Explicit):
        pass

    assert scheduling_policy_of(Explicit()) == INDEPENDENT
    assert scheduling_policy_of(Subclass()) == SERIAL
    assert scheduling_policy_of(object()) == SERIAL
    assert not SchedulingPolicy(reentrant=True, session_barrier=False).parallel


def test_policy_is_validated_atomically_and_cannot_change_after_registration(tmp_path):
    tool = create_default_tools(tmp_path)[0]
    dispatcher = ToolDispatcher()
    original = tool.scheduling_policy
    tool.scheduling_policy = "parallel"
    with pytest.raises(ValueError, match="scheduling_policy"):
        dispatcher.register(tool)
    assert not dispatcher.tools
    tool.scheduling_policy = original
    dispatcher.register(tool)
    tool.scheduling_policy = INDEPENDENT
    with pytest.raises(ValueError, match="changed"):
        dispatcher.execute(tool.definition.name, {})


def test_builtin_policy_inventory(tmp_path):
    tools = create_default_tools(tmp_path, isolated_execution=True)
    assert {t.definition.name for t in tools if scheduling_policy_of(t).parallel} == {
        "read_file",
        "list_files",
        "find_files",
        "search_files",
        "get_path_info",
    }
    assert all("scheduling_policy" in vars(type(t)) for t in tools)
    assert all(
        not scheduling_policy_of(t).parallel
        for t in tools
        if t.execution_kind is ExecutionKind.SANDBOXED_PROCESS
    )


def test_rolling_refill_does_not_wait_for_slow_peer_or_cross_barrier():
    third_started = Event()
    completed = set()
    active = 0
    peak = 0
    lock = Lock()
    finishes = []

    def invoke(call):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if call == 0:
            # Old fixed batches deadlock here: 2 cannot start until 0 ends.
            assert third_started.wait(3)
        elif call == 2:
            assert 1 in finishes  # coordinator has processed the freed slot
            third_started.set()
        elif call == 3:
            assert completed == {0, 1, 2} and active == 1
        elif call == 4:
            assert 3 in completed
        with lock:
            active -= 1
            completed.add(call)
        return call

    assert schedule(
        range(5),
        invoke,
        workers=2,
        policy=lambda call: SERIAL if call == 3 else READ_ONLY,
        finish=lambda call, *_: finishes.append(call),
    ) == list(range(5))
    assert peak == 2 and completed == set(range(5))


def test_refilled_failure_stops_pending_calls_and_joins_slow_peer():
    failed = Event()
    invoked = []
    cleaned = Event()

    def invoke(call):
        invoked.append(call)
        if call == 0:
            assert failed.wait(3)
            assert current_cancellation().event.wait(3)
            cleaned.set()
            current_cancellation().check()
        if call == 2:
            failed.set()
            raise PersistenceError("refill failed")
        return call

    with pytest.raises(PersistenceError, match="refill failed"):
        schedule(range(6), invoke, workers=2)
    assert set(invoked) == {0, 1, 2} and cleaned.is_set()
