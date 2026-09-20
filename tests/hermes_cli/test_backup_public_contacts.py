"""Real contact history survives file-loss recovery without renewed authority."""
import json
import shutil

import pytest

from agent import public_contacts as contacts
from agent.compaction_display import project_compaction_message_for_display
from hermes_cli import backup
from hermes_state import SessionDB
from tests.agent.test_public_contact_history import save_reply
from tests.tools.test_public_contact_delivery import extraction, source_authorization, SID, PHONE


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_kind", ["quick", "pre-update-sibling"])
@pytest.mark.parametrize("grant_kind", ["exact", "source"])
async def test_quick_snapshot_restores_saved_contact_without_reviving_grant(extraction, monkeypatch, tmp_path, snapshot_kind, grant_kind, request):
    if grant_kind == "source":
        request.getfixturevalue("source_authorization")
    ref, content, replay, db_path, original = await save_reply(extraction)
    (extraction.home / 'config.yaml').write_text('security: {}\n')
    if snapshot_kind == "quick":
        snapshot = backup.create_quick_snapshot(hermes_home=extraction.home)
    else:
        # Real sibling discovery, scoped to synthetic profile roots.
        monkeypatch.setattr('hermes_cli.profiles._get_default_hermes_home', lambda: extraction.home)
        monkeypatch.setattr('hermes_cli.profiles._get_profiles_root', lambda: tmp_path / 'no-other-profiles')
        results = backup.create_pre_update_snapshots_all_profiles(invoking_home=tmp_path / 'invoking', keep=1,
                                                                   max_file_size=1024 * 1024)
        snapshot = results['default']
    directory = extraction.home / 'public-contact-receipts'
    shutil.rmtree(directory)  # Simulated file loss in the owned fixture only.
    assert PHONE not in project_compaction_message_for_display(original)['content']
    assert backup.restore_quick_snapshot(snapshot, hermes_home=extraction.home)
    with SessionDB(db_path) as db:
        restored = db.get_messages(SID)[0]
    assert PHONE in project_compaction_message_for_display(restored)['content']
    assert restored['content'] == content
    assert restored['codex_message_items'] == original['codex_message_items']
    assert json.loads(restored['codex_message_items']) == replay
    assert all((p.stat().st_mode & 0o077) == 0 for p in directory.rglob('*.json'))
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE


@pytest.mark.asyncio
async def test_receipt_created_during_database_snapshot_is_recoverable(extraction, monkeypatch):
    ref, content, _, db_path, _ = await save_reply(extraction)
    real_copy = backup._safe_copy_db
    inserted = []

    def copy_with_committed_message(source, destination, *args, **kwargs):
        if source == db_path and not inserted:
            with SessionDB(db_path) as db:
                db.append_message(SID, 'assistant', content='New saved reply: ' + ref)
                inserted.append(db.get_messages(SID)[-1])
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(backup, '_safe_copy_db', copy_with_committed_message)
    snapshot = backup.create_quick_snapshot(hermes_home=extraction.home)
    shutil.rmtree(extraction.home / 'public-contact-receipts')
    assert backup.restore_quick_snapshot(snapshot, hermes_home=extraction.home)
    (extraction.home / 'config.yaml').write_text('security: {}\n')
    with SessionDB(db_path) as db:
        rows = db.get_messages(SID)
    assert len(rows) == 2 and rows[-1]['id'] == inserted[0]['id']
    assert all(PHONE in project_compaction_message_for_display(row)['content'] for row in rows)
    assert contacts.render_contact_references(ref, session_id=SID) == contacts.UNAVAILABLE


@pytest.mark.asyncio
async def test_recovered_receipts_do_not_cross_profile_authority(extraction, tmp_path, monkeypatch):
    _, _, _, _, original = await save_reply(extraction)
    snapshot = backup.create_quick_snapshot(hermes_home=extraction.home)
    other = tmp_path / 'other-profile'
    other.mkdir()
    target = other / 'state-snapshots' / snapshot
    shutil.copytree(extraction.home / 'state-snapshots' / snapshot, target)
    monkeypatch.setenv('HERMES_HOME', str(other))
    assert backup.restore_quick_snapshot(snapshot, hermes_home=other)
    with SessionDB(other / 'state.db') as db:
        row = db.get_messages(SID)[0]
    assert row['content'] == original['content']
    assert PHONE not in project_compaction_message_for_display(row)['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['copy', 'size', 'root-link', 'directory-link', 'file-link'])
async def test_incomplete_receipt_snapshot_keeps_previous_recovery(extraction, monkeypatch, tmp_path, failure):
    await save_reply(extraction)
    previous = backup.create_quick_snapshot(hermes_home=extraction.home)
    snapshots = extraction.home / 'state-snapshots'
    prior_files = {p.relative_to(snapshots / previous).as_posix(): p.read_bytes()
                   for p in (snapshots / previous).rglob('*') if p.is_file()}
    directory = extraction.home / 'public-contact-receipts'
    options = {}
    if failure == 'copy':
        def fail_copy(*args, **kwargs):
            raise OSError('synthetic receipt-copy failure')
        monkeypatch.setattr(backup.shutil, 'copyfileobj', fail_copy)
    elif failure == 'size':
        options['max_file_size'] = 1
    elif failure == 'root-link':
        moved = tmp_path / 'moved-receipts'
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    elif failure == 'directory-link':
        (directory / 'foreign').symlink_to(tmp_path, target_is_directory=True)
    else:
        (directory / 'foreign.json').symlink_to(extraction.home / 'config.yaml')
    with pytest.raises(backup.BackupError, match='Public-contact snapshot incomplete'):
        backup.create_quick_snapshot(hermes_home=extraction.home, keep=1, **options)
    assert [p.name for p in snapshots.iterdir()] == [previous]
    assert {p.relative_to(snapshots / previous).as_posix(): p.read_bytes()
            for p in (snapshots / previous).rglob('*') if p.is_file()} == prior_files
