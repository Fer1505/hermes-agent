import logging
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import active_sessions


def _check_registry_writes_private_temp_and_destination_under_permissive_umask(
    tmp_path, monkeypatch, mask, existing_mode
):
    state = tmp_path / 'home' / 'runtime' / 'active_sessions.json'
    if existing_mode is not None:
        state.parent.mkdir(parents=True, mode=0o700)
        state.write_text('{"entries": []}', encoding='utf-8')
        state.chmod(existing_mode)
    real_replace = os.replace
    replacements = []

    def checked_replace(source, target):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert Path(target) == state
        replacements.append(source)
        return real_replace(source, target)

    monkeypatch.setattr(active_sessions.os, 'replace', checked_replace)
    original_mask = os.umask(mask)
    try:
        active_sessions._write_entries(state, [{'session_id': 'synthetic'}])
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        active_sessions._write_entries(state, [])
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
        assert state.read_text(encoding='utf-8') == '{"entries": []}'
    finally:
        os.umask(original_mask)
    assert len(replacements) == 2
    assert not list(state.parent.glob('*.tmp'))


def _check_lock_normalizes_held_inode_and_lease_rewrites_remain_private(tmp_path, mask):
    home = tmp_path / 'home'
    lock_path = home / 'runtime' / 'active_sessions.lock'
    original_mask = os.umask(mask)
    try:
        with active_sessions._FileLock(lock_path) as lock:
            assert stat.S_IMODE(os.fstat(lock._fh.fileno()).st_mode) == 0o600
            assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
        original_inode = lock_path.stat().st_ino
        lock_path.chmod(0o644)
        with active_sessions._FileLock(lock_path) as lock:
            assert os.fstat(lock._fh.fileno()).st_ino == original_inode
            assert lock_path.stat().st_ino == original_inode
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        lease, error = active_sessions.try_acquire_active_session(
            session_id='synthetic-old', surface='cli', config={},
            registry_home=home, track_liveness=True
        )
        assert error is None and lease is not None
        state = home / 'runtime' / 'active_sessions.json'
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert active_sessions.transfer_active_session(lease, session_id='synthetic-new')
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        lease.release()
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert active_sessions.active_session_registry_snapshot(home) == []
    finally:
        os.umask(original_mask)


def _check_lock_refuses_link_without_changing_target_permissions(tmp_path, link_kind):
    target = tmp_path / 'target'
    target.write_bytes(b'untouched')
    target.chmod(0o644)
    lock_path = tmp_path / 'runtime' / 'active_sessions.lock'
    lock_path.parent.mkdir()
    if link_kind == 'symlink':
        lock_path.symlink_to(target)
    else:
        os.link(target, lock_path)
    with pytest.raises((OSError, RuntimeError)):
        with active_sessions._FileLock(lock_path):
            pytest.fail('linked lock must not be acquired')
    assert target.read_bytes() == b'untouched'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def _check_private_registry_replace_failure_preserves_existing_bytes_and_mode(
    tmp_path, monkeypatch, existing_mode
):
    state = tmp_path / 'runtime' / 'active_sessions.json'
    state.parent.mkdir()
    original = b'{"entries": []}\n'
    state.write_bytes(original)
    state.chmod(existing_mode)

    def failed_replace(source, target):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError('synthetic replace failure')

    monkeypatch.setattr(active_sessions.os, 'replace', failed_replace)
    with pytest.raises(OSError, match='synthetic replace failure'):
        active_sessions._write_entries(state, [{'session_id': 'synthetic-new'}])
    assert state.read_bytes() == original
    assert stat.S_IMODE(state.stat().st_mode) == existing_mode
    assert not list(state.parent.glob('*.tmp'))


def test_private_registry_temp_collision_preserves_the_existing_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    state = tmp_path / 'runtime' / 'active_sessions.json'
    state.parent.mkdir()
    state.write_bytes(b'{"entries": []}\n')
    collision = state.with_name(f'{state.name}.{os.getpid()}.collision.tmp')
    collision.write_bytes(b'not ours')
    monkeypatch.setattr(active_sessions.uuid, 'uuid4', lambda: SimpleNamespace(hex='collision'))
    with pytest.raises(FileExistsError):
        active_sessions._write_entries(state, [])
    assert state.read_bytes() == b'{"entries": []}\n'
    assert collision.read_bytes() == b'not ours'


def _check_private_state_normalization_failure_closes_descriptor(tmp_path, monkeypatch, kind):
    state = tmp_path / 'runtime' / 'active_sessions.json'
    state.parent.mkdir()
    state.write_bytes(b'{"entries": []}\n')
    state.chmod(0o600)
    descriptors = []

    def failed_fchmod(fd, mode):
        assert mode == 0o600
        descriptors.append(fd)
        raise PermissionError('synthetic normalization failure')

    monkeypatch.setattr(active_sessions.os, 'fchmod', failed_fchmod)
    with pytest.raises(PermissionError, match='synthetic normalization failure'):
        if kind == 'registry':
            active_sessions._write_entries(state, [])
        else:
            with active_sessions._FileLock(state.with_suffix('.lock')):
                pytest.fail('normalization must fail before lock acquisition')
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert state.read_bytes() == b'{"entries": []}\n'
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert not list(state.parent.glob('*.tmp'))


# Exercise POSIX guarantees on each real native CI host; do not fake the OS.


@pytest.mark.linux_only
@pytest.mark.parametrize('mask', [0o022, 0o000])
@pytest.mark.parametrize('existing_mode', [None, 0o600, 0o644])
def test_registry_writes_private_temp_and_destination_under_permissive_umask_linux(tmp_path, monkeypatch, mask, existing_mode):
    _check_registry_writes_private_temp_and_destination_under_permissive_umask(tmp_path, monkeypatch, mask, existing_mode)


@pytest.mark.macos_only
@pytest.mark.parametrize('mask', [0o022, 0o000])
@pytest.mark.parametrize('existing_mode', [None, 0o600, 0o644])
def test_registry_writes_private_temp_and_destination_under_permissive_umask_macos(tmp_path, monkeypatch, mask, existing_mode):
    _check_registry_writes_private_temp_and_destination_under_permissive_umask(tmp_path, monkeypatch, mask, existing_mode)


@pytest.mark.linux_only
@pytest.mark.parametrize('mask', [0o022, 0o000])
def test_lock_normalizes_held_inode_and_lease_rewrites_remain_private_linux(tmp_path, mask):
    _check_lock_normalizes_held_inode_and_lease_rewrites_remain_private(tmp_path, mask)


@pytest.mark.macos_only
@pytest.mark.parametrize('mask', [0o022, 0o000])
def test_lock_normalizes_held_inode_and_lease_rewrites_remain_private_macos(tmp_path, mask):
    _check_lock_normalizes_held_inode_and_lease_rewrites_remain_private(tmp_path, mask)


@pytest.mark.linux_only
@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_lock_refuses_link_without_changing_target_permissions_linux(tmp_path, link_kind):
    _check_lock_refuses_link_without_changing_target_permissions(tmp_path, link_kind)


@pytest.mark.macos_only
@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_lock_refuses_link_without_changing_target_permissions_macos(tmp_path, link_kind):
    _check_lock_refuses_link_without_changing_target_permissions(tmp_path, link_kind)


@pytest.mark.linux_only
@pytest.mark.parametrize('existing_mode', [0o600, 0o644])
def test_private_registry_replace_failure_preserves_existing_bytes_and_mode_linux(tmp_path, monkeypatch, existing_mode):
    _check_private_registry_replace_failure_preserves_existing_bytes_and_mode(tmp_path, monkeypatch, existing_mode)


@pytest.mark.macos_only
@pytest.mark.parametrize('existing_mode', [0o600, 0o644])
def test_private_registry_replace_failure_preserves_existing_bytes_and_mode_macos(tmp_path, monkeypatch, existing_mode):
    _check_private_registry_replace_failure_preserves_existing_bytes_and_mode(tmp_path, monkeypatch, existing_mode)


@pytest.mark.linux_only
@pytest.mark.parametrize('kind', ['registry', 'lock'])
def test_private_state_normalization_failure_closes_descriptor_linux(tmp_path, monkeypatch, kind):
    _check_private_state_normalization_failure_closes_descriptor(tmp_path, monkeypatch, kind)


@pytest.mark.macos_only
@pytest.mark.parametrize('kind', ['registry', 'lock'])
def test_private_state_normalization_failure_closes_descriptor_macos(tmp_path, monkeypatch, kind):
    _check_private_state_normalization_failure_closes_descriptor(tmp_path, monkeypatch, kind)


def test_resolve_max_concurrent_sessions_values(caplog):
    assert active_sessions.resolve_max_concurrent_sessions({}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": None}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": 0}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": -1}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "3"}) == 3
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"gateway": {"max_concurrent_sessions": 4}}
        )
        == 4
    )
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"max_concurrent_sessions": 2, "gateway": {"max_concurrent_sessions": 4}}
        )
        == 2
    )

    caplog.set_level(logging.WARNING)
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "many"}) is None
    assert any(
        "Ignoring invalid max_concurrent_sessions='many'" in record.message
        for record in caplog.records
    )












def test_cross_process_acquire_claims_only_one_last_slot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[2]
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    go_file = tmp_path / "go"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    script = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from hermes_cli.active_sessions import try_acquire_active_session\n"
        "idx = os.environ['WORKER_INDEX']\n"
        "worker_count = int(os.environ['WORKER_COUNT'])\n"
        "delayed_worker = os.environ.get('DELAYED_WORKER_INDEX')\n"
        "ready_dir = Path(os.environ['READY_DIR'])\n"
        "results_dir = Path(os.environ['RESULTS_DIR'])\n"
        "go_file = Path(os.environ['GO_FILE'])\n"
        "(ready_dir / idx).write_text('ready', encoding='utf-8')\n"
        "deadline = time.time() + 10\n"
        "while not go_file.exists():\n"
        "    if time.time() > deadline:\n"
        "        raise RuntimeError('timed out waiting for go file')\n"
        "    time.sleep(0.01)\n"
        "if idx == delayed_worker:\n"
        "    time.sleep(2.5)\n"
        "lease, message = try_acquire_active_session(\n"
        "    session_id=f'process-{idx}',\n"
        "    surface='cli',\n"
        "    config={'max_concurrent_sessions': 1},\n"
        ")\n"
        "if lease is None:\n"
        "    (results_dir / idx).write_text('BLOCK', encoding='utf-8')\n"
        "    print('BLOCK', flush=True)\n"
        "else:\n"
        "    (results_dir / idx).write_text('OK', encoding='utf-8')\n"
        "    print('OK', flush=True)\n"
        "    deadline = time.time() + 10\n"
        "    while len(list(results_dir.iterdir())) < worker_count:\n"
        "        if time.time() > deadline:\n"
        "            raise RuntimeError('timed out waiting for all workers to attempt acquire')\n"
        "        time.sleep(0.01)\n"
        "    lease.release()\n"
    )
    workers: list[subprocess.Popen[str]] = []
    try:
        for index in range(6):
            worker_env = env.copy()
            worker_env["WORKER_INDEX"] = str(index)
            worker_env["WORKER_COUNT"] = "6"
            worker_env["DELAYED_WORKER_INDEX"] = "5"
            worker_env["READY_DIR"] = str(ready_dir)
            worker_env["RESULTS_DIR"] = str(results_dir)
            worker_env["GO_FILE"] = str(go_file)
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        deadline = time.time() + 10
        while len(list(ready_dir.iterdir())) < len(workers):
            if time.time() > deadline:
                raise AssertionError("workers did not become ready")
            time.sleep(0.01)
        go_file.write_text("go", encoding="utf-8")

        outputs = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()

    assert outputs.count("OK") == 1
    assert outputs.count("BLOCK") == len(workers) - 1
    assert active_sessions.active_session_registry_snapshot() == []




def test_release_orphaned_leases_reclaims_only_unowned_own_pid_entries(tmp_path, monkeypatch):
    """A long-lived server must reclaim leases whose session skipped teardown.

    ``_prune_dead`` only fires when the owning pid dies, so a ``hermes
    dashboard`` running for days holds a leaked lease until restart. The
    process reconciles against the leases it still owns instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 5}
    kept, orphan = (
        active_sessions.try_acquire_active_session(
            session_id=sid, surface="desktop", config=cfg
        )[0]
        for sid in ("kept", "orphaned")
    )
    # Another live process's lease is not ours to reclaim.
    active_sessions._write_entries(
        active_sessions._state_path(),
        active_sessions._read_entries(active_sessions._state_path())
        + [{"lease_id": "elsewhere", "session_id": "other", "surface": "cli", "pid": os.getpid() }],
    )

    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None


def test_release_under_profile_home_override_targets_acquisition_registry(
    tmp_path, monkeypatch
):
    """Regression for #85431: a lease acquired against the root HERMES_HOME
    must release from the root registry even when ``release()`` runs inside a
    profile home override (native multiplex runs agent cleanup under
    ``_profile_runtime_scope``). Before the fix the root entry survived and
    the session cap filled with phantom leases."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    lease, error = active_sessions.try_acquire_active_session(
        session_id="agent:worker:telegram:dm:synthetic",
        surface="gateway:telegram",
        config={"max_concurrent_sessions": 2},
    )
    assert lease is not None and error is None
    root_registry = root / "runtime" / "active_sessions.json"
    assert root_registry.exists()

    token = set_hermes_home_override(str(profile))
    try:
        lease.release()
    finally:
        reset_hermes_home_override(token)

    assert lease.released is True
    remaining = active_sessions._read_entries(root_registry)
    assert remaining == []
    # No phantom registry created under the profile home.
    assert not (profile / "runtime" / "active_sessions.json").exists()


def test_transfer_under_profile_home_override_targets_acquisition_registry(
    tmp_path, monkeypatch
):
    """Sibling site of #85431: transfer must also update the registry the
    lease was acquired against, not one resolved from the current override."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    lease, error = active_sessions.try_acquire_active_session(
        session_id="before",
        surface="gateway:telegram",
        config={"max_concurrent_sessions": 2},
    )
    assert lease is not None and error is None

    token = set_hermes_home_override(str(profile))
    try:
        assert active_sessions.transfer_active_session(lease, session_id="after")
    finally:
        reset_hermes_home_override(token)

    root_registry = root / "runtime" / "active_sessions.json"
    entries = active_sessions._read_entries(root_registry)
    assert [entry["session_id"] for entry in entries] == ["after"]


def test_liveness_registry_corruption_fails_closed_without_overwrite(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    state_path.parent.mkdir(parents=True)
    corrupt = "{not-json"
    state_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        with active_sessions.active_session_liveness_guard("session-1"):
            pass

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.active_session_registry_snapshot()

    assert state_path.read_text(encoding="utf-8") == corrupt

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.try_acquire_active_session(
            session_id="desktop-1",
            surface="desktop",
            config={},
            track_liveness=True,
        )
    assert state_path.read_text(encoding="utf-8") == corrupt

    # The pre-existing concurrency-cap path remains available/fail-open, but
    # must not erase evidence that strict liveness ownership is unknown.
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-1",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is not None and message is None
    assert lease.enabled is False
    assert state_path.read_text(encoding="utf-8") == corrupt
    lease.release()
    assert state_path.read_text(encoding="utf-8") == corrupt


def test_strict_registry_rejects_structurally_invalid_entries(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    base = {
        "lease_id": "lease-1",
        "session_id": "session-1",
        "surface": "desktop",
        "pid": os.getpid(),
        "track_liveness": True,
    }
    invalid_entries = (
        {key: value for key, value in base.items() if key != "lease_id"},
        {**base, "lease_id": ""},
        {key: value for key, value in base.items() if key != "session_id"},
        {**base, "session_id": "  "},
        {**base, "pid": 0},
        {**base, "pid": 1.5},
        {**base, "surface": 1},
        {**base, "track_liveness": "yes"},
        {**base, "metadata": []},
        {**base, "process_start_time": "not-a-number"},
        {**base, "process_start_time": "nan"},
    )

    for entry in invalid_entries:
        active_sessions._write_entries(state_path, [entry])
        original = state_path.read_text(encoding="utf-8")
        with pytest.raises(active_sessions.ActiveSessionRegistryError):
            with active_sessions.active_session_liveness_guard("session-1"):
                pass
        assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "second_session_id",
    ("session-a", "session-b"),
    ids=("exact-duplicate", "conflicting-duplicate"),
)
def test_strict_registry_rejects_duplicate_lease_ids(
    tmp_path, monkeypatch, second_session_id
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    active_sessions._write_entries(
        state_path,
        [
            {
                "lease_id": "duplicate-lease",
                "session_id": "session-a",
                "surface": "desktop",
                "pid": os.getpid(),
                "track_liveness": True,
            },
            {
                "lease_id": "duplicate-lease",
                "session_id": second_session_id,
                "surface": "desktop",
                "pid": os.getpid(),
                "track_liveness": True,
            },
        ],
    )
    original = state_path.read_text(encoding="utf-8")

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.active_session_registry_snapshot()

    assert state_path.read_text(encoding="utf-8") == original


def test_cap_transfer_does_not_overwrite_registry_corruption(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-old",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is not None and message is None

    corrupt = "{not-json"
    state_path.write_text(corrupt, encoding="utf-8")
    assert not active_sessions.transfer_active_session(
        lease,
        session_id="cli-new",
    )
    assert lease.session_id == "cli-old"
    assert state_path.read_text(encoding="utf-8") == corrupt

    lease.release()
    assert lease.released is True
    assert state_path.read_text(encoding="utf-8") == corrupt


def test_liveness_guard_rejects_unknown_pid_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    active_sessions._write_entries(
        state_path,
        [
            {
                "lease_id": "unknown-owner",
                "session_id": "session-1",
                "surface": "desktop",
                "pid": 12345,
                "track_liveness": True,
            }
        ],
    )
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda _pid: (_ for _ in ()).throw(OSError("pid lookup unavailable")),
    )

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        with active_sessions.active_session_liveness_guard("session-1"):
            pass

    original = state_path.read_text(encoding="utf-8")
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-cap-session",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is not None and message is None
    assert lease.enabled is False
    assert state_path.read_text(encoding="utf-8") == original


def test_liveness_release_failure_is_retryable(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-1",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    original_write = active_sessions._write_entries
    monkeypatch.setattr(
        active_sessions,
        "_write_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        lease.release()
    assert lease.released is False

    monkeypatch.setattr(active_sessions, "_write_entries", original_write)
    lease.release()
    assert lease.released is True
    assert active_sessions.active_session_registry_snapshot() == []


def test_liveness_transfer_upserts_missing_entry_without_consuming_a_new_slot(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={"max_concurrent_sessions": 1},
        track_liveness=True,
    )
    assert lease is not None and message is None
    (home / "runtime" / "active_sessions.json").unlink()

    assert active_sessions.transfer_active_session(lease, session_id="session-new")
    snapshot = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in snapshot] == [
        (lease.lease_id, "session-new")
    ]

    blocked, limit_message = active_sessions.try_acquire_active_session(
        session_id="session-other",
        surface="desktop",
        config={"max_concurrent_sessions": 1},
        track_liveness=True,
    )
    assert blocked is None
    assert limit_message is not None
    lease.release()


def test_liveness_transfer_write_failure_keeps_old_id_for_retry(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    original_write = active_sessions._write_entries
    monkeypatch.setattr(
        active_sessions,
        "_write_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        active_sessions.transfer_active_session(lease, session_id="session-new")
    assert lease.session_id == "session-old"

    monkeypatch.setattr(active_sessions, "_write_entries", original_write)
    assert active_sessions.transfer_active_session(lease, session_id="session-new")
    assert lease.session_id == "session-new"
    lease.release()


def test_release_wins_against_transfer_waiting_on_same_lease_lock(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    release_wrote = threading.Event()
    allow_release = threading.Event()
    transfer_at_lock = threading.Event()
    original_write = active_sessions._write_entries
    original_enter = active_sessions._FileLock.__enter__

    def _blocking_write(path, entries):
        original_write(path, entries)
        if threading.current_thread().name == "lease-release":
            release_wrote.set()
            assert allow_release.wait(timeout=5)

    def _instrumented_enter(lock):
        if threading.current_thread().name == "lease-transfer":
            transfer_at_lock.set()
        return original_enter(lock)

    monkeypatch.setattr(active_sessions, "_write_entries", _blocking_write)
    monkeypatch.setattr(active_sessions._FileLock, "__enter__", _instrumented_enter)
    transfer_result: list[bool] = []
    release_thread = threading.Thread(target=lease.release, name="lease-release")
    transfer_thread = threading.Thread(
        target=lambda: transfer_result.append(
            active_sessions.transfer_active_session(lease, session_id="session-new")
        ),
        name="lease-transfer",
    )

    release_thread.start()
    assert release_wrote.wait(timeout=5)
    transfer_thread.start()
    assert transfer_at_lock.wait(timeout=5)
    allow_release.set()
    release_thread.join(timeout=5)
    transfer_thread.join(timeout=5)

    assert not release_thread.is_alive()
    assert not transfer_thread.is_alive()
    assert transfer_result == [False]
    assert lease.released is True
    assert active_sessions.active_session_registry_snapshot() == []
