"""One terminal outcome contract for Hermes' API presentation surfaces."""
from dataclasses import dataclass


TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted", "incomplete"})


@dataclass(frozen=True)
class APIRunOutcome:
    status: str
    completed: bool
    partial: bool
    failed: bool
    interrupted: bool
    error: str | None
    error_code: str | None

    @property
    def finish_reason(self) -> str:
        if self.completed:
            return "stop"
        return "length" if self.error_code == "output_truncated" else "error"

    @property
    def response_status(self) -> str:
        # The supported Responses SSE union has no response.cancelled event.
        # Preserve interruption in Hermes metadata, with response.incomplete.
        return "incomplete" if self.interrupted and self.status == "cancelled" else self.status

    def metadata(self) -> dict:
        return {"status": self.status, "completed": self.completed, "partial": self.partial,
                "failed": self.failed, "interrupted": self.interrupted,
                "error": self.error, "error_code": self.error_code}

    def headers(self) -> dict:
        headers = {"X-Hermes-Completed": str(self.completed).lower(),
                   "X-Hermes-Partial": str(self.partial).lower(),
                   "X-Hermes-Interrupted": str(self.interrupted).lower()}
        if self.error:
            headers["X-Hermes-Error"] = " ".join(self.error.split())[:200]
        return headers

    def response_fields(self) -> dict:
        fields = {"status": self.response_status}
        if not self.completed:
            fields["hermes"] = self.metadata()
        if self.status == "failed":
            fields["error"] = {"code": "server_error", "message": self.error}
        elif self.response_status == "incomplete":
            # Do not invent an OpenAI reason for native compression deferral,
            # invalid tool calls or interruption. Preserve that detail above.
            fields["incomplete_details"] = (
                {"reason": "max_output_tokens"} if self.error_code == "output_truncated" else None
            )
        return fields


def api_run_outcome(result, *, error=None) -> APIRunOutcome:
    """Interpret native flags without mutating result or provider replay.

    Legacy dictionaries may omit completed. Explicit failure/partial/interrupt
    signals always override success. An error alone is a failure, but an error
    explaining an explicitly partial or interrupted turn retains that outcome.
    This describes turn termination, not business success or delivery proof.
    """
    valid = isinstance(result, dict)
    data = result if valid else {}
    raw_error = error if error is not None else data.get("error")
    partial = bool(data.get("partial"))
    interrupted = bool(data.get("interrupted"))
    failed = bool(data.get("failed") or error is not None or not valid
                  or (raw_error and not partial and not interrupted))
    completed = bool(data.get("completed", True)) and not (failed or partial or interrupted)
    status = ("failed" if failed else "cancelled" if interrupted else
              "completed" if completed else "incomplete")
    code = None
    message = None
    if not completed:
        from agent.redact import redact_sensitive_text
        code = {"failed": "agent_error", "cancelled": "agent_interrupted",
                "incomplete": "agent_incomplete"}[status]
        if status == "incomplete" and partial and "truncat" in str(raw_error or "").lower():
            code = "output_truncated"
        message = redact_sensitive_text(str(raw_error or {
            "failed": "Agent run failed.", "cancelled": "Agent run was interrupted.",
            "incomplete": "Agent run did not complete.",
        }[status]), force=True)
    return APIRunOutcome(status, completed, partial, failed, interrupted, message, code)
