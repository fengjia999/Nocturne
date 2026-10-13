from pathlib import Path
import ast
import asyncio
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import backup_restore

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_has_private_backup_controls():
    page = (ROOT / 'dashboard.html').read_text(encoding='utf-8')
    assert 'id="settings-view"' in page
    assert 'id="memory-restore-file"' in page
    assert 'downloadMemoryBackup()' in page
    assert 'stageMemoryRestore()' in page
    assert "loadMemoryBackupStatus();" in page
    assert "'/api/backup/restore'" in page


def test_restore_runs_before_db_connections_and_is_authenticated():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    assert server.index('apply_pending_restore(config["buckets_dir"])') < server.index('embedding_engine = EmbeddingEngine(config)')
    for route in ('/api/backup/status', '/api/backup/export', '/api/backup/restore'):
        assert f'@mcp.custom_route("{route}"' in server
    assert 'x-nocturne-restore' in server
    assert '_require_auth(request)' in server


def _export_handler(root):
    # Exercise the real route body without initializing Nocturne's engines or
    # opening application databases during this isolated error-path test.
    node = next(node for node in ast.parse((ROOT / 'server.py').read_text()).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == 'api_backup_export')
    node.decorator_list = []
    namespace = {'_require_auth': lambda request: None, 'config': {'buckets_dir': str(root)},
                 'export_backup': backup_restore.export_backup, 'logger': MagicMock(),
                 'Path': Path, 'datetime': datetime}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'backup-route-test', 'exec'), namespace)
    return namespace['api_backup_export'], namespace['logger']


async def _direct_thread_call(function, *args):
    return function(*args)


def test_export_failure_returns_json_after_helper_removed_temporary_zip(tmp_path, monkeypatch):
    (tmp_path / 'note.md').write_text('test')
    monkeypatch.setattr(backup_restore, 'MAX_TOTAL_SIZE', 0)
    handler, logger = _export_handler(tmp_path)
    with patch.object(asyncio, 'to_thread', _direct_thread_call):
        response = asyncio.run(handler(None))
    assert response.status_code == 500
    assert json.loads(response.body)['error'] == '备份导出失败，请查看服务日志'
    logger.exception.assert_called_once_with('Backup export failed')


def test_download_cleanup_tolerates_an_already_removed_file(tmp_path):
    (tmp_path / 'note.md').write_text('test')
    handler, _ = _export_handler(tmp_path)
    with patch.object(asyncio, 'to_thread', _direct_thread_call):
        response = asyncio.run(handler(None))
    assert Path(response.path).exists()
    Path(response.path).unlink()
    response.background.func()
    response.background.func()
