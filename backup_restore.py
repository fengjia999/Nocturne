"""Portable Nocturne data backups.

The restore is staged while the server is live and applied before the first
SQLite connection is opened on the next process start. A failed/incomplete
installation rolls back to the previous data on restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT = "nocturne-data-backup"
VERSION = 1
CONTROL_DIR = ".nocturne-restore"
SKIP_NAMES = {CONTROL_DIR, ".api_auth.json", ".env", "config.yaml"}
MAX_FILES = 15000
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_SIZE = 1024 * 1024 * 1024
MAX_MANIFEST_SIZE = 4 * 1024 * 1024
CHUNK = 1024 * 1024


class BackupError(ValueError):
    pass


def _reserved_name(name: str) -> bool:
    return name in SKIP_NAMES or name.startswith(".env.")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    """Never truncate the previous recovery journal before its replacement exists."""
    fd, filename = tempfile.mkstemp(prefix="journal-", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _restore_lock(root: Path):
    """Serialize staging and startup restore across requests and processes."""
    control = root / CONTROL_DIR
    control.mkdir(parents=True, exist_ok=True)
    with (control / "restore.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise BackupError("Another restore operation is in progress") from exc
        try:
            yield control
        finally:
            handle.seek(0)
            release()


def _readable_files(root: Path):
    result = []
    if not root.is_dir():
        return result
    for base, dirs, files in os.walk(root, followlinks=False):
        current = Path(base)
        dirs[:] = [d for d in sorted(dirs) if not (current / d).is_symlink()
                   and not (current == root and d == CONTROL_DIR)]
        for name in sorted(files):
            path = current / name
            rel = path.relative_to(root)
            if _reserved_name(rel.parts[0]) or path.is_symlink():
                continue
            if path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            if path.is_file():
                result.append((path, rel.as_posix()))
    return result


def _sqlite_snapshot(src: Path, output: Path) -> None:
    # Opening a consistent SQLite snapshot also includes committed WAL pages.
    with sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=10) as conn:
        with sqlite3.connect(str(output)) as dst:
            conn.backup(dst)


def export_backup(root: str | Path, destination: str | Path) -> dict:
    root = Path(root).resolve()
    destination = Path(destination)
    entries = _readable_files(root)
    if len(entries) > MAX_FILES:
        raise BackupError("Too many files to back up")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
        "notes": "Runtime credentials, API keys and external Nearfield paths are excluded.",
    }
    with tempfile.TemporaryDirectory(prefix="nocturne-sqlite-") as temporary:
        try:
            total = 0
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for index, (src, rel) in enumerate(entries):
                    _safe_relative(rel)
                    source = src
                    if src.suffix in (".db", ".sqlite", ".sqlite3"):
                        source = Path(temporary) / f"db-{index}.sqlite"
                        _sqlite_snapshot(src, source)
                    digest = hashlib.sha256()
                    size = 0
                    with source.open("rb") as file, archive.open("data/" + rel, "w") as member:
                        while block := file.read(CHUNK):
                            size += len(block)
                            total += len(block)
                            if total > MAX_TOTAL_SIZE:
                                raise BackupError("Backup is too large")
                            digest.update(block)
                            member.write(block)
                            if archive.fp.tell() > MAX_ARCHIVE_SIZE:
                                raise BackupError("Backup ZIP exceeds size limit")
                    manifest["files"].append({"path": rel, "size": size, "sha256": digest.hexdigest()})
                encoded_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                if len(encoded_manifest) > MAX_MANIFEST_SIZE:
                    raise BackupError("Manifest too large")
                archive.writestr("manifest.json", encoded_manifest)
            if destination.stat().st_size > MAX_ARCHIVE_SIZE:
                raise BackupError("Backup ZIP exceeds size limit")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return {"files": len(entries), "bytes": sum(f["size"] for f in manifest["files"]), "created_at": manifest["created_at"]}


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError("Invalid archive path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise BackupError("Unsafe archive path")
    if _reserved_name(path.parts[0]) or CONTROL_DIR in path.parts:
        raise BackupError("Reserved archive path")
    return value


def validate_backup(archive_path: str | Path) -> dict:
    archive_path = Path(archive_path)
    if archive_path.stat().st_size > MAX_ARCHIVE_SIZE:
        raise BackupError("Backup ZIP exceeds size limit")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1:
                raise BackupError("Too many archive entries")
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise BackupError("Duplicate entries or missing manifest")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_SIZE:
                raise BackupError("Manifest too large")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
                raise BackupError("Unsupported backup format")
            files = manifest.get("files")
            if not isinstance(files, list) or len(files) > MAX_FILES:
                raise BackupError("Invalid backup manifest")
            expected = {}
            total = 0
            for item in files:
                if not isinstance(item, dict):
                    raise BackupError("Invalid manifest item")
                rel = _safe_relative(item.get("path"))
                if rel in expected or not isinstance(item.get("size"), int) or item["size"] < 0:
                    raise BackupError("Duplicate file or invalid file size")
                if not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
                    raise BackupError("Invalid checksum")
                total += item["size"]
                if total > MAX_TOTAL_SIZE:
                    raise BackupError("Backup exceeds uncompressed size limit")
                expected["data/" + rel] = item
            actual = set()
            for info in infos:
                if info.filename == "manifest.json":
                    continue
                if info.is_dir() or info.filename not in expected:
                    raise BackupError("Unexpected archive entry")
                # Unix mode for a symlink (file type 0120000) is forbidden.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BackupError("Symbolic links are not allowed")
                if info.file_size != expected[info.filename]["size"]:
                    raise BackupError("Archive size does not match manifest")
                actual.add(info.filename)
            if actual != set(expected):
                raise BackupError("Backup is missing data")
            for name, entry in expected.items():
                digest = hashlib.sha256()
                with archive.open(name) as member:
                    while block := member.read(CHUNK):
                        digest.update(block)
                if digest.hexdigest() != entry["sha256"]:
                    raise BackupError("Backup checksum mismatch: " + name)
            return {"files": len(files), "bytes": total, "created_at": manifest.get("created_at", "")}
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as exc:
        raise BackupError(f"Invalid backup archive: {type(exc).__name__}") from exc


def stage_backup(root: str | Path, incoming: str | Path) -> dict:
    root = Path(root)
    if Path(incoming).stat().st_size > MAX_ARCHIVE_SIZE:
        raise BackupError("Backup ZIP exceeds size limit")
    with _restore_lock(root) as control:
        pending = control / "pending.zip"
        if pending.exists() or (control / "installing.json").exists():
            raise BackupError("A restore is already pending")
        fd, filename = tempfile.mkstemp(prefix="incoming-", suffix=".zip", dir=control)
        os.close(fd)
        staged = Path(filename)
        try:
            shutil.copyfile(incoming, staged)
            data = validate_backup(staged)
            with staged.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(staged, pending)
            _sync_directory(control)
        finally:
            staged.unlink(missing_ok=True)
    return data


def _top_level_data(root: Path):
    return sorted(p for p in root.iterdir() if not _reserved_name(p.name))


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _rollback(root: Path, control: Path, state: dict) -> None:
    old = control / "old"
    if not old.exists():
        (control / "installing.json").unlink(missing_ok=True)
        return
    previous_names = set(state.get("old_entries", []))
    imported_names = set(state.get("new_entries", []))
    # Only remove paths known to be installed by this restore. Other paths
    # may still be original data waiting to be moved into `old`.
    for name in imported_names - previous_names:
        _remove_path(root / name)
    for name in previous_names:
        original = old / name
        if original.exists() or original.is_symlink():
            _remove_path(root / name)
            os.replace(original, root / name)
    old.rmdir()
    _sync_directory(root)
    _sync_directory(control)
    (control / "installing.json").unlink(missing_ok=True)


def apply_pending_restore(root: str | Path) -> bool:
    """Run before SQLite/database engines initialize. Uploads only stage data."""
    root = Path(root)
    with _restore_lock(root):
        return _apply_pending_restore(root)


def _apply_pending_restore(root: Path) -> bool:
    control = root / CONTROL_DIR
    marker = control / "installing.json"
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        if state.get("state") == "complete":
            # New data was fully installed; finish housekeeping after a crash.
            old = control / "old"
            if old.exists():
                previous = control / "previous"
                _remove_path(previous)
                os.replace(old, previous)
            (control / "pending.zip").unlink(missing_ok=True)
            marker.unlink()
            return False
        _rollback(root, control, state)
        (control / "pending.zip").unlink(missing_ok=True)
        return False
    pending = control / "pending.zip"
    if not pending.exists():
        return False
    validate_backup(pending)
    staged = control / "new"
    _remove_path(staged)
    staged.mkdir(parents=True)
    try:
        with zipfile.ZipFile(pending) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for item in manifest["files"]:
                rel = _safe_relative(item["path"])
                dest = staged / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("data/" + rel) as inp, dest.open("wb") as out:
                    shutil.copyfileobj(inp, out, CHUNK)
                    out.flush()
                    os.fsync(out.fileno())
        for directory in sorted((p for p in staged.rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            _sync_directory(directory)
        _sync_directory(staged)
        old = control / "old"
        if old.exists():
            raise BackupError("Previous restoration has not been cleaned up")
        state = {"state": "installing", "old_entries": [p.name for p in _top_level_data(root)],
                 "new_entries": [p.name for p in staged.iterdir()]}
        _atomic_json(marker, state)
        try:
            old.mkdir()
            for name in state["old_entries"]:
                os.replace(root / name, old / name)
            for name in state["new_entries"]:
                os.replace(staged / name, root / name)
            _sync_directory(old)
            _sync_directory(root)
            _sync_directory(control)
            _atomic_json(marker, {**state, "state": "complete"})
        except Exception:
            _rollback(root, control, state)
            raise
        previous = control / "previous"
        _remove_path(previous)
        os.replace(old, previous)
        pending.unlink()
        marker.unlink()
        return True
    finally:
        _remove_path(staged)


def restore_status(root: str | Path) -> dict:
    control = Path(root) / CONTROL_DIR
    marker = control / "installing.json"
    state = None
    if marker.exists():
        try:
            state = json.loads(marker.read_text(encoding="utf-8")).get("state")
        except Exception:
            state = "unknown"
    return {"pending": (control / "pending.zip").exists(),
            "previous_available": (control / "previous").exists(),
            "restore_state": state}
