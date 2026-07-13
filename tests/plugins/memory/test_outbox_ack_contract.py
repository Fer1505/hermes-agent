from unittest.mock import MagicMock

import pytest


OUTBOX_METADATA = {"outbox_event_id": "mw_contract_event"}


def test_provider_delivery_contracts_do_not_overclaim_exactly_once():
    from plugins.memory.byterover import ByteRoverMemoryProvider
    from plugins.memory.holographic import HolographicMemoryProvider
    from plugins.memory.honcho import HonchoMemoryProvider
    from plugins.memory.openviking import OpenVikingMemoryProvider
    from plugins.memory.retaindb import RetainDBMemoryProvider
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    providers = [
        SupermemoryMemoryProvider(),
        HolographicMemoryProvider(),
        OpenVikingMemoryProvider(),
        HonchoMemoryProvider(),
        RetainDBMemoryProvider(),
        ByteRoverMemoryProvider(),
    ]
    contracts = {p.name: p.memory_write_delivery_contract() for p in providers}

    assert {
        name
        for name, contract in contracts.items()
        if contract["delivery_semantics"] == "idempotent-at-least-once"
    } == {"supermemory", "holographic", "openviking"}
    assert {
        name
        for name, contract in contracts.items()
        if contract["delivery_semantics"] == "at-least-once"
    } == {"honcho", "retaindb", "byterover"}
    assert all(
        contract["delivery_semantics"] != "exactly-once"
        for contract in contracts.values()
    )


def test_supermemory_outbox_delivery_is_acknowledged_and_idempotent():
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    provider = SupermemoryMemoryProvider.__new__(SupermemoryMemoryProvider)
    provider._active = True
    provider._write_enabled = True
    provider._client = MagicMock()
    provider._client.add_memory.return_value = {"id": "doc-1"}
    provider._entity_context = ""
    provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)

    kwargs = provider._client.add_memory.call_args.kwargs
    assert kwargs["custom_id"] == "mw_contract_event"
    assert kwargs["metadata"]["outbox_event_id"] == "mw_contract_event"

    provider._client.add_memory.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_supermemory_outbox_requires_document_id():
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    provider = SupermemoryMemoryProvider.__new__(SupermemoryMemoryProvider)
    provider._active = True
    provider._write_enabled = True
    provider._client = MagicMock()
    provider._client.add_memory.return_value = {"status": "queued"}
    provider._entity_context = ""
    with pytest.raises(RuntimeError, match="required document id"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_holographic_outbox_delivery_propagates_failure():
    from plugins.memory.holographic import HolographicMemoryProvider

    provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
    provider._store = MagicMock()
    provider._store.add_fact.side_effect = RuntimeError("disk failure")
    with pytest.raises(RuntimeError, match="disk failure"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_holographic_outbox_requires_committed_fact_id():
    from plugins.memory.holographic import HolographicMemoryProvider

    provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
    provider._store = MagicMock()
    provider._store.add_fact.return_value = None
    with pytest.raises(RuntimeError, match="committed fact id"):
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


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"success": False},
        {"success": True, "mode": "async"},
    ],
)
def test_retaindb_outbox_requires_synchronous_success(receipt):
    from plugins.memory.retaindb import RetainDBMemoryProvider

    provider = RetainDBMemoryProvider.__new__(RetainDBMemoryProvider)
    provider._client = MagicMock()
    provider._client.add_memory.return_value = receipt
    provider._user_id = "user"
    provider._session_id = "session"
    with pytest.raises(RuntimeError, match="synchronous success acknowledgement"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_retaindb_outbox_accepts_documented_sync_success():
    from plugins.memory.retaindb import RetainDBMemoryProvider

    provider = RetainDBMemoryProvider.__new__(RetainDBMemoryProvider)
    provider._client = MagicMock()
    provider._client.add_memory.return_value = {
        "success": True,
        "mode": "sync",
        "trace_id": "trc-1",
    }
    provider._user_id = "user"
    provider._session_id = "session"
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


def test_byterover_outbox_rejects_failed_cli_result(monkeypatch):
    from plugins.memory.byterover import ByteRoverMemoryProvider

    provider = ByteRoverMemoryProvider({"auto_extract": True})
    provider._cwd = None
    monkeypatch.setattr(
        "plugins.memory.byterover._run_brv",
        MagicMock(return_value={"success": False, "error": "curation failed"}),
    )
    with pytest.raises(RuntimeError, match="curation failed"):
        provider.on_memory_write("add", "memory", "fact", metadata=OUTBOX_METADATA)


def test_openviking_outbox_delivery_is_synchronous_and_stable(monkeypatch):
    import plugins.memory.openviking as module
    from plugins.memory.openviking import OpenVikingMemoryProvider

    post = MagicMock(return_value={})
    get = MagicMock(return_value={"result": "fact"})
    client_type = MagicMock(return_value=MagicMock(post=post, get=get))
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
    get.assert_called_once_with(
        "/api/v1/content/read",
        params={"uri": payload["uri"]},
    )

    post.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        provider.on_memory_write("add", "user", "fact", metadata=OUTBOX_METADATA)


def test_openviking_outbox_replay_replaces_existing_uri_and_reads_back(monkeypatch):
    import plugins.memory.openviking as module
    from plugins.memory.openviking import OpenVikingMemoryProvider

    post = MagicMock(
        side_effect=[
            module._OpenVikingHTTPError("already exists", 409),
            {"status": "ok"},
        ]
    )
    get = MagicMock(return_value={"status": "ok", "result": "fact"})
    monkeypatch.setattr(
        module,
        "_VikingClient",
        MagicMock(return_value=MagicMock(post=post, get=get)),
    )
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://test"
    provider._api_key = ""
    provider._account = "account"
    provider._user = "user"
    provider._agent = "hermes"

    provider.on_memory_write("add", "user", "fact", metadata=OUTBOX_METADATA)

    assert [call.args[1]["mode"] for call in post.call_args_list] == [
        "create",
        "replace",
    ]
    assert post.call_args_list[0].args[1]["uri"] == post.call_args_list[1].args[1]["uri"]
    get.assert_called_once()


def test_openviking_outbox_rejects_mismatched_readback(monkeypatch):
    import plugins.memory.openviking as module
    from plugins.memory.openviking import OpenVikingMemoryProvider

    monkeypatch.setattr(
        module,
        "_VikingClient",
        MagicMock(
            return_value=MagicMock(
                post=MagicMock(return_value={"status": "ok"}),
                get=MagicMock(return_value={"status": "ok", "result": "other"}),
            )
        ),
    )
    provider = OpenVikingMemoryProvider()
    provider._client = MagicMock()
    provider._endpoint = "http://test"
    provider._api_key = ""
    provider._account = "account"
    provider._user = "user"
    provider._agent = "hermes"

    with pytest.raises(RuntimeError, match="readback did not match"):
        provider.on_memory_write("add", "user", "fact", metadata=OUTBOX_METADATA)
