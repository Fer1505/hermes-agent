"""Actual native consumer paths; all wire calls are synthetic and counted."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import yaml
import httpx

from agent import auxiliary_client as aux
from agent import native_admission as admission
from agent.chat_completion_helpers import handle_max_iterations
from agent.codex_runtime import run_codex_stream
from hermes_constants import get_hermes_home
from run_agent import AIAgent
from tools.delegate_tool import _build_child_agent


MODEL = "gpt-5-codex"
BASE = "https://chatgpt.com/backend-api/codex"


def _terminal(text="fixture answer"):
    return SimpleNamespace(
        output=[SimpleNamespace(type="message", role="assistant", phase="final_answer",
            content=[SimpleNamespace(type="output_text", text=text)])],
        status="completed", model=MODEL,
        usage=SimpleNamespace(input_tokens=4, output_tokens=2, total_tokens=6),
    )


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    def close(self):
        self.closed = True


class Wire:
    """Provider seam only: real routing, adapters, streaming and loops run."""
    def __init__(self, outcomes=None, base=BASE):
        self.api_key = "fixture-not-a-credential"
        self.base_url = base
        self.timeout = httpx.Timeout(60)
        self.calls = []
        self.options = []
        self.outcomes = list(outcomes or [_terminal()])
        self.responses = SimpleNamespace(create=self.create)

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Stream):
            return outcome
        return Stream([SimpleNamespace(type="response.output_item.done", item=item)
            for item in outcome.output] + [SimpleNamespace(type="response.completed", response=outcome)])

    def close(self):
        pass


@pytest.fixture
def configure():
    def write(attempts=3, seconds=60, enabled=True):
        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({
            "agent": {"native_oauth_task_limits": {
                "enabled": enabled, "max_attempts": attempts, "max_seconds": seconds}},
        }))
    return write


@pytest.fixture
def make_agent(monkeypatch):
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    created = []

    def build(wire=None, **kwargs):
        agent = AIAgent(model=MODEL, provider="openai-codex", api_mode="codex_responses",
            base_url=BASE, api_key="fixture-not-a-credential", quiet_mode=True,
            skip_context_files=True, skip_memory=True, max_iterations=2, **kwargs)
        wire = wire or Wire()
        monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: wire)
        monkeypatch.setattr(agent, "_ensure_primary_openai_client", lambda **kw: wire)
        monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
        monkeypatch.setattr(agent, "_abort_request_openai_client", lambda *a, **kw: None)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a: None)
        monkeypatch.setattr(agent, "_persist_session", lambda *a, **kw: None)
        monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **kw: None)
        created.append(agent)
        return agent, wire

    yield build
    for agent in created:
        agent.close()


def _request():
    return {"model": MODEL, "instructions": "Stable fixture prefix",
        "input": [{"role": "user", "content": "fixture"}], "store": False}


def test_real_root_loop_opens_once_and_next_turn_gets_new_budget(configure, make_agent):
    configure(attempts=1)
    agent, wire = make_agent()
    first = agent.run_conversation("fixture", task_id="root-one")
    previous = agent._native_admission_budget
    assert first["final_response"] == "fixture answer"
    assert previous.attempts == 1 and previous.closed
    second = agent.run_conversation("fixture", task_id="root-two")
    assert second["final_response"] == "fixture answer"
    assert agent._native_admission_budget.task_id == "root-two"
    assert agent._native_admission_budget is not previous
    assert len(wire.calls) == 2
    assert all(option["max_retries"] == 0 for option in wire.options)


def test_physical_ceiling_includes_real_summary(configure, make_agent):
    configure(attempts=1)
    agent, wire = make_agent()
    request = _request()
    before = json.dumps(request, sort_keys=True)
    with admission.task_scope(agent, "summary-root"):
        assert run_codex_stream(agent, request, client=wire).status == "completed"
        with pytest.raises(admission.NativeAdmissionDenied, match="physical-attempt"):
            handle_max_iterations(agent, [{"role": "user", "content": "fixture"}], 1)
    assert len(wire.calls) == 1
    assert 0 < wire.calls[0]["timeout"].read <= 60
    assert json.dumps(request, sort_keys=True) == before


def test_summary_retry_counts_every_open(configure, make_agent):
    configure(attempts=2)
    agent, wire = make_agent(Wire([_terminal(""), _terminal("summary answer")]))
    with admission.task_scope(agent, "summary-retry"):
        result = handle_max_iterations(agent, [{"role": "user", "content": "fixture"}], 2)
        assert agent._native_admission_budget.attempts == 2
    assert result == "summary answer"
    assert len(wire.calls) == 2


@pytest.mark.parametrize("outcome", [ConnectionError("fixture"), Stream([ConnectionError("fixture")])])
def test_native_transport_uncertainty_refuses_replay_and_fallback(configure, make_agent, monkeypatch, outcome):
    configure(attempts=5)
    agent, wire = make_agent(Wire([outcome]))
    fallback = []
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda *a, **k: fallback.append(True))
    result = agent.run_conversation("fixture", task_id="uncertain-root")
    assert result["failed"] and result["failure_retryable"] is False
    assert result["failure_reason"] == "native_oauth_task_admission"
    assert len(wire.calls) == 1 and not fallback
    assert agent._native_admission_budget.uncertain


def test_real_sync_and_async_auxiliary_share_main_budget(configure, make_agent, monkeypatch):
    configure(attempts=2)
    agent, wire = make_agent()
    client = aux.CodexAuxiliaryClient(wire, MODEL)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k:
        (aux.AsyncCodexAuxiliaryClient(client) if k.get("async_mode") else client, MODEL))
    with admission.task_scope(agent, "aux-root"):
        assert aux.call_llm(task="compression", provider="openai-codex", model=MODEL,
            messages=[{"role": "user", "content": "fixture"}]).choices[0].message.content == "fixture answer"
        response = asyncio.run(aux.async_call_llm(task="session_search", provider="openai-codex", model=MODEL,
            messages=[{"role": "user", "content": "fixture"}]))
        assert response.choices[0].message.content == "fixture answer"
        with pytest.raises(admission.NativeAdmissionDenied, match="physical-attempt"):
            run_codex_stream(agent, _request(), client=wire)
    assert len(wire.calls) == 2


def test_auxiliary_denial_does_not_select_paid_fallback(configure, make_agent, monkeypatch):
    configure()
    agent, wire = make_agent()
    paid = SimpleNamespace(base_url="https://api.openai.com/v1", chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: pytest.fail("paid provider opened"))))
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (paid, MODEL))
    fallback = []
    monkeypatch.setattr(aux, "_try_configured_fallback_for_unavailable_client", lambda *a, **kw: fallback.append(True))
    with admission.task_scope(agent, "aux-paid"):
        with pytest.raises(admission.NativeAdmissionDenied, match="auxiliary route"):
            aux.call_llm(task="compression", provider="openai", model=MODEL,
                messages=[{"role": "user", "content": "fixture"}])
    assert not wire.calls and not fallback


def test_missing_task_context_refuses_main_and_standalone_auxiliary(configure, make_agent):
    configure()
    agent, wire = make_agent()
    with pytest.raises(admission.NativeAdmissionDenied, match="host-owned task"):
        run_codex_stream(agent, _request(), client=wire)
    with pytest.raises(admission.NativeAdmissionDenied, match="host-owned task"):
        aux.call_llm(task="curator", messages=[{"role": "user", "content": "fixture"}])
    assert not wire.calls


def test_expired_shared_deadline_refuses_summary_and_auxiliary(configure, make_agent):
    configure()
    agent, wire = make_agent()
    with admission.task_scope(agent, "expired-root"):
        agent._native_admission_budget.deadline = 0
        with pytest.raises(admission.NativeAdmissionDenied, match="time ceiling"):
            handle_max_iterations(agent, [], 1)
        with pytest.raises(admission.NativeAdmissionDenied, match="time ceiling"):
            aux.call_llm(task="compression", messages=[])
    assert not wire.calls


@pytest.mark.parametrize("attempts,seconds,enabled", [(0,60,True), (True,60,True), (1,0,True), (1,float("inf"),True), (1,60,"true")])
def test_invalid_enabled_policy_refuses_before_provider(configure, make_agent, attempts, seconds, enabled):
    configure(attempts, seconds, enabled)
    agent, wire = make_agent()
    with pytest.raises(admission.NativeAdmissionDenied, match="invalid"):
        agent.run_conversation("fixture")
    assert not wire.calls


def test_disabled_mode_preserves_unmanaged_native_consumer(configure, make_agent):
    configure(enabled=False)
    agent, wire = make_agent()
    assert run_codex_stream(agent, _request(), client=wire).status == "completed"
    assert len(wire.calls) == 1 and wire.options == []


def test_real_child_constructor_inherits_and_parallel_children_share_limit(configure, make_agent, monkeypatch):
    configure(attempts=1)
    parent, wire = make_agent()
    children = []
    with admission.task_scope(parent, "parent-root"):
        for index in range(2):
            child = _build_child_agent(task_index=index, goal="fixture", context=None, toolsets=None,
                model=None, max_iterations=2, task_count=2, parent_agent=parent)
            children.append(child)
            assert child._native_admission_parent_budget is parent._native_admission_budget
        barrier = threading.Barrier(2)
        def consume(child):
            with admission.task_scope(child, child._subagent_id):
                barrier.wait(timeout=10)
                try:
                    return run_codex_stream(child, _request(), client=wire).status
                except admission.NativeAdmissionDenied:
                    return "denied"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, children))
        assert sorted(results) == ["completed", "denied"]
        assert parent._native_admission_budget.attempts == 1
    with pytest.raises(admission.NativeAdmissionDenied, match="physical-attempt"):
        with admission.task_scope(children[0], "late-child"):
            run_codex_stream(children[0], _request(), client=wire)
    assert len(wire.calls) == 1
    for child in children:
        child.close()


def test_detached_child_retains_original_ceiling_across_parent_turn(configure, make_agent):
    configure(attempts=2)
    parent, wire = make_agent()
    with admission.task_scope(parent, "original-root"):
        child = _build_child_agent(task_index=0, goal="fixture", context=None, toolsets=None,
            model=None, max_iterations=2, task_count=1, parent_agent=parent)
        original = parent._native_admission_budget
        run_codex_stream(parent, _request(), client=wire)
    original_deadline = original.deadline
    with admission.task_scope(parent, "next-parent-turn"):
        assert parent._native_admission_budget is not original
    with admission.task_scope(child, "detached-child"):
        assert child._native_admission_budget is original
        assert original.task_id == "original-root" and original.deadline == original_deadline
        run_codex_stream(child, _request(), client=wire)
        with pytest.raises(admission.NativeAdmissionDenied, match="physical-attempt"):
            run_codex_stream(child, _request(), client=wire)
    assert len(wire.calls) == 2
    child.close()


def test_explicit_child_cancel_closes_entire_original_task(configure, make_agent):
    configure(attempts=5)
    parent, wire = make_agent()
    with admission.task_scope(parent, "cancel-root"):
        child = _build_child_agent(task_index=0, goal="fixture", context=None, toolsets=None,
            model=None, max_iterations=2, task_count=1, parent_agent=parent)
        child.interrupt("fixture stop", hard_cancel=True)
        with pytest.raises(admission.NativeAdmissionDenied, match="closed"):
            run_codex_stream(parent, _request(), client=wire)
    assert not wire.calls
    child.close()


def test_foreign_owner_and_physical_api_route_refused(configure, make_agent):
    configure()
    owner, wire = make_agent()
    foreign, _ = make_agent()
    with admission.task_scope(owner, "owner-root"):
        with pytest.raises(admission.NativeAdmissionDenied, match="owner"):
            run_codex_stream(foreign, _request(), client=wire)
        with pytest.raises(admission.NativeAdmissionDenied, match="client route"):
            run_codex_stream(owner, _request(), client=Wire(base="https://api.openai.com/v1"))
    assert not wire.calls


def test_request_timeout_cannot_escape_shared_deadline(configure, make_agent):
    configure(seconds=30)
    owner, wire = make_agent()
    request = _request()
    request["timeout"] = 900
    with admission.task_scope(owner, "timeout-root"):
        run_codex_stream(owner, request, client=wire)
    assert 0 < wire.calls[0]["timeout"].read <= 30
    assert request["timeout"] == 900


def test_no_terminal_response_latches_uncertainty(configure, make_agent):
    configure(attempts=5)
    owner, wire = make_agent(Wire([Stream([])]))
    with admission.task_scope(owner, "lost-terminal"):
        with pytest.raises(admission.NativeAdmissionDenied, match="terminal response"):
            run_codex_stream(owner, _request(), client=wire)
        with pytest.raises(admission.NativeAdmissionDenied, match="uncertain"):
            run_codex_stream(owner, _request(), client=wire)
    assert len(wire.calls) == 1


def test_child_without_constructor_binding_cannot_mint_root(configure, make_agent):
    configure()
    child, wire = make_agent(parent_session_id="foreign-parent")
    with pytest.raises(admission.NativeAdmissionDenied, match="parent's task"):
        child.run_conversation("fixture", task_id="forged-child")
    assert not wire.calls


def test_corrupt_policy_cannot_disable_gate(configure, make_agent):
    configure()
    owner, wire = make_agent()
    (get_hermes_home() / "config.yaml").write_text("agent: [unterminated")
    with pytest.raises(admission.NativeAdmissionDenied, match="unreadable"):
        run_codex_stream(owner, _request(), client=wire)
    assert not wire.calls


def test_terminal_result_survives_tail_transport_error(configure, make_agent):
    configure(attempts=1)
    final = _terminal()
    events = [SimpleNamespace(type="response.output_item.done", item=final.output[0]),
        SimpleNamespace(type="response.completed", response=final), ConnectionError("fixture tail")]
    owner, wire = make_agent(Wire([Stream(events)]))
    with admission.task_scope(owner, "terminal-tail"):
        response = run_codex_stream(owner, _request(), client=wire)
        assert response.status == "completed"
        assert not owner._native_admission_budget.uncertain
    assert len(wire.calls) == 1


def test_stream_event_cannot_extend_absolute_task_deadline(configure, make_agent):
    configure(attempts=3)
    owner, wire = make_agent()
    class ExpiringStream(Stream):
        def __iter__(self):
            owner._native_admission_budget.deadline = 0
            yield SimpleNamespace(type="response.output_text.delta", delta="fixture")
    wire.outcomes = [ExpiringStream([])]
    with admission.task_scope(owner, "absolute-deadline"):
        with pytest.raises(admission.NativeAdmissionDenied, match="time ceiling"):
            run_codex_stream(owner, _request(), client=wire)
        assert owner._native_admission_budget.uncertain
    assert len(wire.calls) == 1


def test_unqualified_child_construction_refuses_before_allocations(configure, make_agent, monkeypatch):
    configure()
    parent, wire = make_agent()
    monkeypatch.setattr("run_agent.AIAgent", lambda **kwargs: pytest.fail("child allocated without admission"))
    with pytest.raises(admission.NativeAdmissionDenied, match="host-owned task"):
        _build_child_agent(task_index=0, goal="fixture", context=None, toolsets=None,
            model=None, max_iterations=2, task_count=1, parent_agent=parent)
    assert not wire.calls


def test_shorter_client_timeouts_survive_full_task_deadline(configure, make_agent):
    configure(seconds=300)
    owner, wire = make_agent()
    wire.timeout = httpx.Timeout(connect=2, read=5, write=4, pool=1)
    with admission.task_scope(owner, "short-client-timeout"):
        run_codex_stream(owner, _request(), client=wire)
    timeout = wire.calls[0]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (2, 5, 4, 1)


def test_httpx_request_phases_preserve_stricter_defaults_and_task_deadline(configure, make_agent):
    configure(seconds=30)
    owner, wire = make_agent()
    wire.timeout = httpx.Timeout(connect=2, read=50, write=4, pool=None)
    request = _request()
    request["timeout"] = httpx.Timeout(connect=9, read=3, write=None, pool=50)
    with admission.task_scope(owner, "phase-timeout"):
        original_deadline = owner._native_admission_budget.deadline
        run_codex_stream(owner, request, client=wire)
        assert owner._native_admission_budget.deadline == original_deadline
    timeout = wire.calls[0]["timeout"]
    assert (timeout.connect, timeout.read, timeout.write) == (2, 3, 4)
    assert 0 < timeout.pool <= 30
    assert request["timeout"].pool == 50


@pytest.mark.parametrize("timeout", ["30", {"read": 30}, True, float("nan")])
def test_unqualified_timeout_refused_before_reservation(configure, make_agent, timeout):
    configure()
    owner, wire = make_agent()
    request = _request()
    request["timeout"] = timeout
    with admission.task_scope(owner, "bad-timeout"):
        with pytest.raises(admission.NativeAdmissionDenied, match="timeout"):
            run_codex_stream(owner, request, client=wire)
        assert owner._native_admission_budget.attempts == 0
    assert not wire.calls


def test_actual_blocked_root_stream_uses_existing_absolute_watchdog(configure, make_agent, monkeypatch):
    configure(seconds=0.1)
    release = threading.Event()
    aborted = threading.Event()
    class BlockedStream(Stream):
        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="fixture progress")
            assert release.wait(10), "existing root watchdog did not abort blocked stream"
            raise ConnectionError("fixture deadline abort")
    owner, wire = make_agent(Wire([BlockedStream([])]))
    def abort(*args, **kwargs):
        aborted.set()
        release.set()
    monkeypatch.setattr(owner, "_abort_request_openai_client", abort)
    started = time.monotonic()
    result = owner.run_conversation("fixture", task_id="blocked-root")
    assert aborted.is_set() and time.monotonic() - started < 6
    assert result["failed"] and result["failure_retryable"] is False
    assert owner._native_admission_budget.uncertain and len(wire.calls) == 1


def test_actual_blocked_auxiliary_uses_existing_absolute_timer(configure, make_agent, monkeypatch):
    configure(seconds=0.1)
    release = threading.Event()
    class BlockedStream(Stream):
        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="fixture progress")
            assert release.wait(10), "existing auxiliary timer did not close blocked stream"
            raise ConnectionError("fixture deadline abort")
    owner, wire = make_agent(Wire([BlockedStream([])]))
    monkeypatch.setattr(wire, "close", release.set)
    client = aux.CodexAuxiliaryClient(wire, MODEL)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, MODEL))
    with admission.task_scope(owner, "blocked-auxiliary"):
        started = time.monotonic()
        with pytest.raises(admission.NativeAdmissionDenied):
            aux.call_llm(task="compression", provider="openai-codex", model=MODEL,
                messages=[{"role": "user", "content": "fixture"}])
        assert release.is_set() and time.monotonic() - started < 6
        assert owner._native_admission_budget.uncertain
    assert len(wire.calls) == 1


def test_actual_blocked_summary_uses_existing_absolute_watchdog(configure, make_agent, monkeypatch):
    configure(seconds=0.1)
    release = threading.Event()
    class BlockedStream(Stream):
        def __iter__(self):
            yield SimpleNamespace(type="response.output_text.delta", delta="fixture progress")
            assert release.wait(10), "summary bypassed existing absolute watchdog"
            raise ConnectionError("fixture deadline abort")
    owner, wire = make_agent(Wire([BlockedStream([])]))
    monkeypatch.setattr(owner, "_abort_request_openai_client", lambda *a, **k: release.set())
    with admission.task_scope(owner, "blocked-summary"):
        started = time.monotonic()
        with pytest.raises(admission.NativeAdmissionDenied):
            handle_max_iterations(owner, [{"role": "user", "content": "fixture"}], 2)
        assert release.is_set() and time.monotonic() - started < 6
        assert owner._native_admission_budget.uncertain
    assert len(wire.calls) == 1


def test_native_interrupt_retains_control_signal_and_uncertain_fence(configure, make_agent):
    configure(attempts=3)
    owner, wire = make_agent(Wire([InterruptedError("fixture interrupt")]))
    with admission.task_scope(owner, "interrupted-open"):
        with pytest.raises(InterruptedError, match="fixture interrupt"):
            run_codex_stream(owner, _request(), client=wire)
        assert owner._native_admission_budget.uncertain
        with pytest.raises(admission.NativeAdmissionDenied, match="uncertain"):
            run_codex_stream(owner, _request(), client=wire)
    assert len(wire.calls) == 1


@pytest.mark.parametrize("first_event", [False, True])
def test_short_watchdog_with_noop_abort_latches_before_replay(configure, make_agent, monkeypatch, first_event):
    configure(attempts=5, seconds=60)
    release = threading.Event()
    aborted = threading.Event()
    class BlockedStream(Stream):
        def __iter__(self):
            if first_event:
                yield SimpleNamespace(type="response.output_text.delta", delta="fixture progress")
            assert release.wait(10), "fixture worker was not released"
            raise ConnectionError("fixture late worker exit")
    owner, wire = make_agent(Wire([BlockedStream([])]))
    monkeypatch.setattr(owner, "_abort_request_openai_client", lambda *a, **k: aborted.set())
    from agent import chat_completion_helpers as helpers
    original = helpers._env_float
    def configured(name, default):
        if name in {"HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS"}:
            return 0.05
        if name == "HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS":
            return 0
        return original(name, default)
    monkeypatch.setattr(helpers, "_env_float", configured)
    fallback = []
    monkeypatch.setattr(owner, "_try_activate_fallback", lambda *a, **k: fallback.append(True))
    try:
        result = owner.run_conversation("fixture", task_id="short-watchdog")
        assert aborted.is_set() and not release.is_set()
        assert result["failed"] and result["failure_retryable"] is False
        assert owner._native_admission_budget.uncertain
        assert len(wire.calls) == 1 and not fallback
        with pytest.raises(admission.NativeAdmissionDenied):
            run_codex_stream(owner, _request(), client=wire)
        assert len(wire.calls) == 1
    finally:
        release.set()
