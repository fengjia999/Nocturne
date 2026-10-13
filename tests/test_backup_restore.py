from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
import backup_restore as backup

from backup_restore import (
    BackupError, CONTROL_DIR, apply_pending_restore, export_backup, restore_status,
    stage_backup, validate_backup,
)


def test_export_restore_preserves_memory_and_database(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "permanent").mkdir()
    (root / "permanent" / "memory.md").write_text('我记得“2026”里的事\n', encoding='utf-8')
    (root / "nearfield" / "days").mkdir(parents=True)
    (root / "nearfield" / "days" / "2026-10-13.md").write_text('第一人称日记', encoding='utf-8')
    (root / ".api_auth.json").write_text('old-secret', encoding='utf-8')
    with sqlite3.connect(root / "embeddings.db") as conn:
        conn.execute('create table embedding(value text)')
        conn.execute('insert into embedding values (?)', ('original',))
        conn.commit()
    archive = tmp_path / "backup.zip"
    export_backup(root, archive)
    assert validate_backup(archive)['files'] == 3
    with zipfile.ZipFile(archive) as z:
        assert '.api_auth.json' not in z.namelist()
        assert z.read('data/permanent/memory.md').decode() == '我记得“2026”里的事\n'
        db = tmp_path / 'snapshot.db'
        db.write_bytes(z.read('data/embeddings.db'))
        with sqlite3.connect(db) as conn:
            assert conn.execute('select value from embedding').fetchone()[0] == 'original'
    (root / 'permanent' / 'memory.md').write_text('new-data', encoding='utf-8')
    stage_backup(root, archive)
    assert restore_status(root)['pending']
    assert (root / 'permanent' / 'memory.md').read_text() == 'new-data'
    assert apply_pending_restore(root)
    assert (root / 'permanent' / 'memory.md').read_text() == '我记得“2026”里的事\n'
    assert (root / '.api_auth.json').read_text() == 'old-secret'
    assert restore_status(root)['previous_available']
    assert not restore_status(root)['pending']
    assert (root / '.nocturne-restore' / 'previous' / 'permanent' / 'memory.md').read_text() == 'new-data'
    assert not apply_pending_restore(root)


def test_reject_zip_slip_and_preserve_current_data(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'memory.md').write_text('safe')
    archive = tmp_path / 'evil.zip'
    payload = b'bad'
    manifest = {'format':'nocturne-data-backup', 'version':1, 'files': [
        {'path':'../../evil.txt', 'size':len(payload), 'sha256':hashlib.sha256(payload).hexdigest()}]}
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('manifest.json', json.dumps(manifest))
        z.writestr('data/../../evil.txt', payload)
    with pytest.raises(BackupError):
        stage_backup(root, archive)
    assert (root / 'memory.md').read_text() == 'safe'


def test_reject_bad_digest(tmp_path):
    archive = tmp_path / 'bad.zip'
    manifest = {'format':'nocturne-data-backup','version':1,'files':[
        {'path':'memory.md','size':4,'sha256':'0'*64}]}
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('manifest.json', json.dumps(manifest))
        z.writestr('data/memory.md', 'data')
    with pytest.raises(BackupError, match='checksum'):
        validate_backup(archive)


def test_import_must_not_include_auth_file(tmp_path):
    archive = tmp_path / 'bad.zip'
    data = b'secret'
    manifest = {'format':'nocturne-data-backup', 'version':1,'files':[
        {'path':'.api_auth.json', 'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()}]}
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('manifest.json', json.dumps(manifest))
        z.writestr('data/.api_auth.json', data)
    with pytest.raises(BackupError):
        validate_backup(archive)


def test_restart_rollback_handles_partially_moved_original_data(tmp_path):
    root = tmp_path / 'vault'
    control = root / '.nocturne-restore'
    old = control / 'old'
    old.mkdir(parents=True)
    (root / 'unmoved.md').write_text('unmoved original')
    (old / 'moved.md').write_text('moved original')
    (root / 'new-only.md').write_text('new import')
    (root / '.api_auth.json').write_text('secret')
    state = {'state':'installing', 'old_entries':['unmoved.md','moved.md'],
             'new_entries':['new-only.md']}
    (control / 'installing.json').write_text(json.dumps(state))
    assert not apply_pending_restore(root)
    assert (root / 'unmoved.md').read_text() == 'unmoved original'
    assert (root / 'moved.md').read_text() == 'moved original'
    assert not (root / 'new-only.md').exists()
    assert (root / '.api_auth.json').read_text() == 'secret'


def test_reject_duplicate_archive_entries(tmp_path):
    root = tmp_path / 'vault'; root.mkdir()
    (root / 'note.md').write_text('ok')
    dest = tmp_path / 'normal.zip'
    export_backup(root, dest)
    with zipfile.ZipFile(dest, 'a') as z:
        z.writestr('data/note.md', 'conflicting')
    with pytest.raises(BackupError):
        validate_backup(dest)


def test_refuse_overwriting_already_pending_restore(tmp_path):
    root = tmp_path / 'vault'; root.mkdir()
    (root / 'note.md').write_text('ok')
    dest = tmp_path / 'backup.zip'
    export_backup(root, dest)
    stage_backup(root, dest)
    with pytest.raises(BackupError, match='already pending'):
        stage_backup(root, dest)


def _simple_backup(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('backed-up')
    archive = tmp_path / 'backup.zip'
    export_backup(root, archive)
    return root, archive


def test_export_enforces_total_not_per_file_limit(tmp_path, monkeypatch):
    root = tmp_path / 'vault'; root.mkdir()
    (root / 'one.md').write_bytes(b'123456')
    (root / 'two.md').write_bytes(b'123456')
    dest = tmp_path / 'backup.zip'
    monkeypatch.setattr(backup, 'MAX_TOTAL_SIZE', 10)
    with pytest.raises(BackupError, match='too large'):
        export_backup(root, dest)
    assert not dest.exists()
    assert (root / 'one.md').read_bytes() == b'123456'
    monkeypatch.setattr(backup, 'MAX_TOTAL_SIZE', 12)
    assert export_backup(root, dest)['bytes'] == 12
    assert validate_backup(dest)['bytes'] == 12


def test_export_enforces_archive_and_manifest_limits(tmp_path, monkeypatch):
    root, archive = _simple_backup(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(backup, 'MAX_ARCHIVE_SIZE', 100)
        with pytest.raises(BackupError, match='ZIP exceeds'):
            export_backup(root, archive)
        assert not archive.exists()
    monkeypatch.setattr(backup, 'MAX_MANIFEST_SIZE', 10)
    with pytest.raises(BackupError, match='Manifest too large'):
        export_backup(root, archive)
    assert not archive.exists()


def test_restore_preserves_all_reserved_env_and_config_files(tmp_path):
    root, archive = _simple_backup(tmp_path)
    reserved = ['.env', '.env.production', '.env.local', '.api_auth.json', 'config.yaml']
    for name in reserved:
        (root / name).write_text('private-' + name)
    stage_backup(root, archive)
    assert apply_pending_restore(root)
    for name in reserved:
        assert (root / name).read_text() == 'private-' + name
        assert not (root / CONTROL_DIR / 'previous' / name).exists()


def test_interleaved_upload_cannot_replace_pending_backup(tmp_path, monkeypatch):
    root, first = _simple_backup(tmp_path)
    other_root = tmp_path / 'other'; other_root.mkdir()
    (other_root / 'note.md').write_text('other archive')
    second = tmp_path / 'second.zip'; export_backup(other_root, second)
    original_copy = backup.shutil.copyfile
    rejected = []
    def copy(source, destination, *args, **kwargs):
        with pytest.raises(BackupError, match='operation is in progress'):
            stage_backup(root, second)
        rejected.append(True)
        return original_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(backup.shutil, 'copyfile', copy)
    stage_backup(root, first)
    assert rejected == [True]
    with zipfile.ZipFile(root / CONTROL_DIR / 'pending.zip') as archive:
        assert archive.read('data/note.md') == b'backed-up'
    assert not list((root / CONTROL_DIR).glob('incoming-*.zip'))


def test_restore_lock_is_shared_with_startup_apply(tmp_path):
    root, archive = _simple_backup(tmp_path)
    with backup._restore_lock(root):
        with pytest.raises(BackupError, match='operation is in progress'):
            stage_backup(root, archive)
        with pytest.raises(BackupError, match='operation is in progress'):
            apply_pending_restore(root)
    assert not restore_status(root)['pending']


def test_restore_lock_works_across_processes(tmp_path):
    import subprocess
    import sys
    root, archive = _simple_backup(tmp_path)
    script = ('import sys; from backup_restore import stage_backup, BackupError; '
              '\ntry: stage_backup(sys.argv[1],sys.argv[2])'
              '\nexcept BackupError as exc: print(str(exc))')
    with backup._restore_lock(root):
        result = subprocess.run([sys.executable, '-c', script, str(root), str(archive)],
                                capture_output=True, text=True, timeout=10,
                                cwd=Path(backup.__file__).parent)
    assert result.returncode == 0, result.stderr
    assert 'operation is in progress' in result.stdout
    assert not restore_status(root)['pending']


def test_failed_atomic_marker_replacement_keeps_previous_journal(tmp_path, monkeypatch):
    marker = tmp_path / 'installing.json'
    backup._atomic_json(marker, {'state': 'installing'})
    original_replace = backup.os.replace
    def replace(source, destination):
        if Path(destination) == marker:
            raise OSError('simulated failed journal rename')
        return original_replace(source, destination)
    monkeypatch.setattr(backup.os, 'replace', replace)
    with pytest.raises(OSError):
        backup._atomic_json(marker, {'state': 'complete'})
    assert json.loads(marker.read_text()) == {'state': 'installing'}
    assert not list(tmp_path.glob('journal-*.tmp'))


def test_interrupted_completion_marker_rolls_back_on_next_start(tmp_path, monkeypatch):
    root, archive = _simple_backup(tmp_path)
    (root / 'note.md').write_text('original before restore')
    stage_backup(root, archive)
    original_replace = backup.os.replace
    def crash(source, destination):
        if Path(destination).name == 'installing.json' and json.loads(Path(source).read_text()).get('state') == 'complete':
            raise KeyboardInterrupt('simulate process interruption before commit')
        return original_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(backup.os, 'replace', crash)
        with pytest.raises(KeyboardInterrupt):
            apply_pending_restore(root)
    marker = root / CONTROL_DIR / 'installing.json'
    assert json.loads(marker.read_text())['state'] == 'installing'
    assert not apply_pending_restore(root)
    assert (root / 'note.md').read_text() == 'original before restore'
    assert not marker.exists()
    assert not restore_status(root)['pending']


def test_completion_marker_error_rolls_back_immediately(tmp_path, monkeypatch):
    root, archive = _simple_backup(tmp_path)
    (root / 'note.md').write_text('original before restore')
    stage_backup(root, archive)
    original_atomic = backup._atomic_json
    def fail(path, state):
        if state['state'] == 'complete':
            raise OSError('simulated journal write failure')
        return original_atomic(path, state)
    with monkeypatch.context() as patch:
        patch.setattr(backup, '_atomic_json', fail)
        with pytest.raises(OSError):
            apply_pending_restore(root)
    assert (root / 'note.md').read_text() == 'original before restore'
    assert not (root / CONTROL_DIR / 'installing.json').exists()
    assert apply_pending_restore(root)


def test_initial_marker_failure_leaves_original_data_untouched(tmp_path, monkeypatch):
    root, archive = _simple_backup(tmp_path)
    (root / 'note.md').write_text('original before restore')
    stage_backup(root, archive)
    def fail(*args):
        raise OSError('simulated initial journal failure')
    with monkeypatch.context() as patch:
        patch.setattr(backup, '_atomic_json', fail)
        with pytest.raises(OSError):
            apply_pending_restore(root)
    assert (root / 'note.md').read_text() == 'original before restore'
    assert not (root / CONTROL_DIR / 'old').exists()
    assert apply_pending_restore(root)


def test_restart_after_rollback_removed_old_directory_cleans_marker(tmp_path):
    root, _ = _simple_backup(tmp_path)
    control = root / CONTROL_DIR; control.mkdir()
    backup._atomic_json(control / 'installing.json', {'state': 'installing', 'old_entries': ['note.md'], 'new_entries': []})
    assert not apply_pending_restore(root)
    assert not (control / 'installing.json').exists()
    assert (root / 'note.md').read_text() == 'backed-up'


def test_restart_finishes_housekeeping_after_complete_marker(tmp_path, monkeypatch):
    root, archive = _simple_backup(tmp_path)
    (root / 'note.md').write_text('original before restore')
    stage_backup(root, archive)
    original_replace = backup.os.replace
    def crash(source, destination):
        if Path(destination).name == 'previous':
            raise KeyboardInterrupt('simulate interruption after commit')
        return original_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(backup.os, 'replace', crash)
        with pytest.raises(KeyboardInterrupt):
            apply_pending_restore(root)
    marker = root / CONTROL_DIR / 'installing.json'
    assert json.loads(marker.read_text())['state'] == 'complete'
    assert not apply_pending_restore(root)
    assert (root / 'note.md').read_text() == 'backed-up'
    assert (root / CONTROL_DIR / 'previous' / 'note.md').read_text() == 'original before restore'
    assert not restore_status(root)['pending']
    assert not marker.exists()


def test_live_sqlite_wal_is_included_in_export(tmp_path):
    root = tmp_path / 'vault'; root.mkdir()
    with sqlite3.connect(root / 'embeddings.db') as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE memories(value TEXT)')
        conn.execute('INSERT INTO memories VALUES (?)', ('committed in WAL',))
        conn.commit()
        assert (root / 'embeddings.db-wal').exists()
        archive = tmp_path / 'backup.zip'
        export_backup(root, archive)
        with zipfile.ZipFile(archive) as output:
            assert all(not name.endswith(('-wal', '-shm')) for name in output.namelist())
            snapshot = tmp_path / 'snapshot.db'
            snapshot.write_bytes(output.read('data/embeddings.db'))
        with sqlite3.connect(snapshot) as restored:
            assert restored.execute('SELECT value FROM memories').fetchone()[0] == 'committed in WAL'
