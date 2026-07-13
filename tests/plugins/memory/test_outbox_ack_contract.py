from unittest.mock import MagicMock

import pytest


OUTBOX_METADATA = {"outbox_event_id": "mw_contract_event"}


def test_supermemory_outbox_delivery_is_acknowledged_and_idempotent():
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    provider = SupermemoryMemoryProvider.__new__(SupermemoryMemoryProvider)
    provider._active = True
    provider._write_enabled = True
    provider._client = MagicMock()
    provider._entity_context = ""
    provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)

    kwargs = provider._client.add_memory.call_args.kwargs
    assert kwargs["custom_id"] == "mw_contract_event"
    assert kwargs["metadata"]["outbox_event_id"] == "mw_contract_event"

    provider._client.add_memory.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_holographic_outbox_delivery_propagates_failure():
    from plugins.memory.holographic import HolographicMemoryProvider

    provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
    provider._store = MagicMock()
    provider._store.add_fact.side_effect = RuntimeError("disk failure")
    with pytest.raises(RuntimeError, match="disk failure"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_honcho_outbox_delivery_requires_positive_acknowledgement():
    from plugins.memory.honcho import HonchoMemoryProvider

    provider = HonchoMemoryProvider.__new__(HonchoMemoryProvider)
    provider._cron_skipped = False
    provider._recall_mode = "auto"
    provider._session_ready = lambda: True
    provider._manager = MagicMock()
    provider._manager.create_conclusion.return_value = False
    provider._session_key = "session"
    with pytest.raises(RuntimeError, match="not accepted"):
        provider.on_memory_write("add", "user", "preference", metadata=OUTBOX_METADATA)


def test_retaindb_outbox_delivery_propagates_failure():
    from plugins.memory.retaindb import RetainDBMemoryProvider

    provider = RetainDBMemoryProvider.__new__(RetainDBMemoryProvider)
    provider._client = MagicMock()
    provider._client.add_memory.side_effect = RuntimeError("offline")
    provider._user_id = "user"
    provider._session_id = "session"
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_byterover_outbox_delivery_propagates_failure(monkeypatch):
    from plugins.memory.byterover import ByteRoverMemoryProvider

    provider = ByteRoverMemoryProvider({"auto_extract": True})
    provider._cwd = None
    monkeypatch.setattr(
        "plugins.memory.byterover._run_brv",
        MagicMock(side_effect=RuntimeError("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_openviking_outbox_delivery_is_synchronous_and_stable(monkeypatch):
    import plugins.memory.openviking as module
    from plugins.memory.openviking import OpenVikingMemoryProvider

    post = MagicMock(return_value={})
    client_type = MagicMock(return_value=MagicMock(post=post))
    monkeypatch.setattr(module, "_VikingClient", client_type)
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://test"
    provider._api_key = ""
    provider._account = "account"
    provider._user = "user"
    provider._agent = "hermes"

    provider.on_memory_write("add", "user", "fact", metadata=OUTBOX_METADATA)
    payload = post.call_args.args[1]
    assert "mw_contract_event" not in payload["content"]
    assert len(payload["uri"].rsplit("mem_", 1)[1].split(".md", 1)[0]) == 24
    assert provider._memory_write_threads == set()

    post.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "user", "fact", metadata=OUTBOX_METADATA)
