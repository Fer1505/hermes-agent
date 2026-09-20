"""Quick snapshots must retain request deduplication and cron watermarks."""
import contextlib
import json
import shutil

import pytest

from hermes_cli import backup


@pytest.mark.parametrize("store_name", ["runs_idempotency.db", "cron/notepad.db"])
def test_quick_snapshot_restores_operational_state(tmp_path, monkeypatch, store_name):
    from cron import notepad
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    home = tmp_path / "source"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    if store_name == "runs_idempotency.db":
        with contextlib.closing(RunIdempotencyStore(str(home / store_name))) as store:
            store.reserve("scope", "request", "fingerprint", "original-run", {"status": "completed"})
    else:
        monkeypatch.setattr(notepad, "NOTEPAD_FILE", home / store_name)
        notepad.set_note("job", "watermark", "event-already-processed")

    snapshot_id = backup.create_quick_snapshot(hermes_home=home)
    assert snapshot_id
    snapshot = home / "state-snapshots" / snapshot_id
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert store_name in manifest["files"]
    assert not manifest["failed_dbs"] and not manifest["oversized_skipped"]
    restored = tmp_path / "restored" / store_name
    restored.parent.mkdir(parents=True)
    shutil.copy2(snapshot / store_name, restored)

    if store_name == "runs_idempotency.db":
        with contextlib.closing(RunIdempotencyStore(str(restored))) as store:
            outcome, result = store.reserve("scope", "request", "fingerprint", "duplicate-run", {"status": "queued"})
            assert outcome == "reused" and result["run_id"] == "original-run"
            assert store.lookup("scope", "request", "different")[0] == "conflict"
    else:
        monkeypatch.setattr(notepad, "NOTEPAD_FILE", restored)
        assert notepad.get_note("job", "watermark") == "event-already-processed"
