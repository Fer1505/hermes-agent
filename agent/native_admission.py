"""Opt-in, process-local ceilings for one native ChatGPT OAuth task tree.

No durable/account-wide accounting or security boundary is claimed here.
The host owns the root task and child references; model strings cannot mint
an auxiliary budget. Unconfirmed physical attempts stop automatic replay.
"""
from __future__ import annotations

import contextvars
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit



class NativeAdmissionDenied(RuntimeError):
    """Permanent refusal: consumers must not retry or activate a fallback."""


def _policy():
    # Read strictly, without the ordinary config loader's default-on-parse-
    # error fallback or repair writes: corrupt policy cannot disable a gate.
    try:
        from hermes_cli.config import load_config_readonly

        # Missing user configuration keeps this opt-in mode disabled, while
        # a managed policy still applies and cannot be hidden by missing files.
        config = load_config_readonly(strict=True, allow_missing_user=True, validate_native_containers=True)
        agent = config.get("agent", {})
        policy = agent.get("native_oauth_task_limits", {})
        if not isinstance(policy, dict):
            raise ValueError()
        enabled = policy.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError()
        if not enabled:
            return None
        attempts = policy.get("max_attempts")
        seconds = policy.get("max_seconds")
        if type(attempts) is not int or attempts <= 0:
            raise ValueError()
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError()
        return attempts, float(seconds)
    except Exception:
        raise NativeAdmissionDenied("Native OAuth task policy is unreadable or invalid") from None


@dataclass
class NativeTaskBudget:
    task_id: str
    max_attempts: int
    deadline: float
    attempts: int = 0
    closed: bool = False
    uncertain: bool = False
    has_descendants: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def check(self):
        with self.lock:
            if self.closed or self.uncertain:
                raise NativeAdmissionDenied("Native OAuth task is closed or has an uncertain attempt")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise NativeAdmissionDenied("Native OAuth task time ceiling reached")
            return remaining

    def reserve(self):
        with self.lock:
            remaining = self.check()
            if self.attempts >= self.max_attempts:
                raise NativeAdmissionDenied("Native OAuth physical-attempt ceiling reached")
            self.attempts += 1
            return remaining

    def mark_uncertain(self):
        with self.lock:
            self.uncertain = True


_CURRENT = contextvars.ContextVar("native_oauth_task", default=None)


def current_budget(agent=None):
    frame = _CURRENT.get()
    if frame is not None:
        owner, budget = frame
        if agent is not None and owner is not agent:
            raise NativeAdmissionDenied("Native OAuth task owner does not match the request")
        budget.check()
        return budget
    if _policy() is not None:
        raise NativeAdmissionDenied("Native OAuth request requires an active host-owned task")
    return None


def require_native_route(agent):
    budget = current_budget(agent)
    if budget is not None and (
        getattr(agent, "provider", None) != "openai-codex"
        or getattr(agent, "api_mode", None) != "codex_responses"
        or not _native_base(getattr(agent, "base_url", ""))
    ):
        raise NativeAdmissionDenied("Native OAuth task refuses an unqualified provider route")
    return budget


def _native_base(value):
    try:
        url = urlsplit(str(value))
    except ValueError:
        return False
    return (url.scheme == "https" and url.netloc == "chatgpt.com"
            and url.path.rstrip("/") == "/backend-api/codex"
            and not url.query and not url.fragment)


@contextmanager
def task_scope(agent, task_id):
    inherited = getattr(agent, "_native_admission_parent_budget", None)
    if isinstance(inherited, NativeTaskBudget):
        parent_ref = getattr(agent, "_delegate_parent_ref", None)
        parent = parent_ref() if callable(parent_ref) else None
        if parent is None or id(parent) != getattr(agent, "_native_admission_parent_identity", None):
            raise NativeAdmissionDenied("Native OAuth child has no qualified parent ownership")
        budget = inherited
        is_root = False
    else:
        policy = _policy()
        if policy is None:
            yield
            return
        if getattr(agent, "is_subagent", False) or getattr(agent, "_parent_session_id", None):
            raise NativeAdmissionDenied("Native OAuth child requires its parent's task ceiling")
        if not task_id:
            raise NativeAdmissionDenied("Native OAuth task identity is missing")
        budget = NativeTaskBudget(str(task_id), policy[0], time.monotonic() + policy[1])
        is_root = True
    agent._native_admission_budget = budget
    token = _CURRENT.set((agent, budget))
    try:
        require_native_route(agent)
        yield
    finally:
        if is_root and not budget.has_descendants:
            with budget.lock:
                budget.closed = True
        _CURRENT.reset(token)


def inherit_parent_budget(child, parent):
    budget = current_budget(parent)
    if budget is not None:
        with budget.lock:
            budget.check()
            budget.has_descendants = True
        child._native_admission_parent_budget = budget
        child._native_admission_parent_identity = id(parent)


def cancel_task(agent):
    # Existing interrupt propagation owns child lifecycle. Only latch this
    # shared admission object; a later turn cannot reopen the old child fence.
    for name in ("_native_admission_budget", "_native_admission_parent_budget"):
        budget = getattr(agent, name, None)
        if isinstance(budget, NativeTaskBudget):
            with budget.lock:
                budget.closed = True


def require_auxiliary_route(client):
    budget = current_budget()
    if budget is not None:
        from agent.auxiliary_client import CodexAuxiliaryClient, AsyncCodexAuxiliaryClient
        if not isinstance(client, (CodexAuxiliaryClient, AsyncCodexAuxiliaryClient)) or not _native_base(client.base_url):
            raise NativeAdmissionDenied("Native OAuth task refuses an unqualified auxiliary route")


class _GuardedStream:
    def __init__(self, raw, budget):
        self.raw = raw
        self.budget = budget
        self.terminal = False

    def __iter__(self):
        try:
            for event in self.raw:
                self.budget.check()
                event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
                if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                    self.terminal = True
                yield event
            if not self.terminal:
                raise NativeAdmissionDenied("Native OAuth stream ended without a terminal response")
        except NativeAdmissionDenied:
            if not self.terminal:
                self.budget.mark_uncertain()
            raise
        except Exception as exc:
            if not self.terminal:
                self.budget.mark_uncertain()
                if isinstance(exc, InterruptedError) or type(exc).__name__ == "CancelledError":
                    raise
                raise NativeAdmissionDenied("Native OAuth stream outcome is uncertain; automatic replay refused") from None
            # A received terminal result survives a later drain/close failure.
            # The native consumer already handles that transport tail.
            raise
        except BaseException:
            if not self.terminal:
                self.budget.mark_uncertain()
            raise

    def close(self):
        if not self.terminal:
            self.budget.mark_uncertain()
        close = getattr(self.raw, "close", None)
        if callable(close):
            close()


def open_native_stream(client, request, *, agent=None):
    budget = require_native_route(agent) if agent is not None else current_budget()
    if budget is None:
        return client.responses.create(**request)
    if not _native_base(client.base_url):
        raise NativeAdmissionDenied("Native OAuth physical client route is unqualified")
    # SDK retries are opaque physical attempts. Disable them for this mode;
    # our consumers own all replay and the shared counter sees every open.
    from openai import NOT_GIVEN

    timeout = _bounded_timeout(
        client.timeout, request.get("timeout", NOT_GIVEN), budget.check()
    )
    bounded = client.with_options(max_retries=0, timeout=timeout)
    # Per-request timeout overrides the SDK client default. Clamp that seam as
    # well so request_overrides cannot replace the remaining task time.
    remaining = budget.reserve()
    physical_request = dict(request)
    physical_request["timeout"] = _bounded_timeout(timeout, None, remaining)
    try:
        stream = bounded.responses.create(**physical_request)
    except BaseException as exc:
        budget.mark_uncertain()
        if isinstance(exc, (InterruptedError, KeyboardInterrupt)) or type(exc).__name__ == "CancelledError":
            raise
        raise NativeAdmissionDenied("Native OAuth attempt outcome is uncertain; automatic replay refused") from None
    if hasattr(stream, "output"):
        if getattr(stream, "status", None) not in {"completed", "incomplete", "failed"}:
            budget.mark_uncertain()
            raise NativeAdmissionDenied("Native OAuth response has no confirmed terminal status")
        budget.check()
        return stream
    return _GuardedStream(stream, budget)


def _bounded_timeout(client_timeout, request_timeout, remaining):
    """Preserve every stricter client/request phase while capping the task."""
    import httpx
    from openai import NOT_GIVEN

    phases = ("connect", "read", "write", "pool")

    def values(timeout):
        if timeout is None or timeout is NOT_GIVEN:
            return dict.fromkeys(phases, remaining)
        if isinstance(timeout, httpx.Timeout):
            raw = {phase: getattr(timeout, phase) for phase in phases}
        elif type(timeout) in (int, float):
            raw = dict.fromkeys(phases, timeout)
        else:
            raise NativeAdmissionDenied("Native OAuth request timeout shape is unqualified")
        for phase, value in raw.items():
            if value is None:
                raw[phase] = remaining
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise NativeAdmissionDenied("Native OAuth request timeout phase is invalid")
        return raw

    client_phases = values(client_timeout)
    request_phases = values(request_timeout)
    return httpx.Timeout(**{
        phase: min(remaining, client_phases[phase], request_phases[phase])
        for phase in phases
    })
