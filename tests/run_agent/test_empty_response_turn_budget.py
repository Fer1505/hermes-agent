from types import SimpleNamespace

from agent.conversation_loop import _claim_empty_response_retry


def test_empty_response_retry_budget_survives_context_counter_resets():
    agent = SimpleNamespace(
        _empty_content_retries=0,
        _empty_content_retries_total=0,
    )

    for expected_total in range(1, 7):
        if agent._empty_content_retries == 3:
            # Compression or provider fallback deliberately refreshes the
            # per-context budget, but never the whole-turn ceiling.
            agent._empty_content_retries = 0
        assert _claim_empty_response_retry(
            agent,
            truly_empty=True,
            has_structured=False,
            prefill_exhausted=False,
        )
        assert agent._empty_content_retries_total == expected_total

    agent._empty_content_retries = 0
    assert not _claim_empty_response_retry(
        agent,
        truly_empty=True,
        has_structured=False,
        prefill_exhausted=False,
    )
    assert agent._empty_content_retries_total == 6
