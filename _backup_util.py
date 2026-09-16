# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup helpers: consistent SQLite snapshots for the zip archive.

Why this module exists
----------------------
``quill_wr.db`` / ``quill_memory.db`` / ``quill_rag.db`` all run in WAL mode
(see ``kb.py`` / ``quill_rag/*.py``).  In WAL mode a ``.db`` file on disk is
*not* the whole database: transactions committed since the last checkpoint
live only in the ``-wal`` sidecar.  Copying the live ``.db`` bytes therefore
captures a stale snapshot — a hot backup can read 0 rows even though the
running process has committed rows.

The archive deliberately does **not** ship ``-wal`` / ``-shm``: replaying a
sidecar captured at a different instant over a restored main file is worse
than not shipping it at all.  Instead each ``.db`` file is replaced by a
snapshot taken through SQLite's online backup API, which merges the main file
and the WAL into one self-contained, transactionally consistent database.

Pure standard library on purpose: importable (and unit-testable) without
AstrBot or aiosqlite installed.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile

logger = logging.getLogger(__name__)

# SQLite main database files always start with this 16-byte magic.
SQLITE_MAGIC = b"SQLite format 3\x00"

# Never archive these: scratch files, plus WAL sidecars (see module docstring).
_SKIP_SUFFIXES = (".tmp", ".tmp.bak", "-wal", "-shm", "-journal")

# Suffixes checked when collecting sidecar files for a database path.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def is_sqlite_file(path: str) -> bool:
    """True if *path* starts with the SQLite file header."""
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def is_sqlite_bytes(data: bytes) -> bool:
    """True if the buffer starts with the SQLite file header."""
    return data[: len(SQLITE_MAGIC)] == SQLITE_MAGIC


def snapshot_sqlite(src: str, dst: str) -> None:
    """Write a consistent copy of database *src* to *dst*.

    Uses the online backup API, which reads through the WAL: every committed
    transaction is included even when it has not been checkpointed into the
    main file yet.  The source is opened read-only so a concurrent writer is
    never disturbed (it may briefly hold a read lock).
    """
    uri = "file:" + _uri_path(src) + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def collect_sidecars(db_path: str) -> list[str]:
    """Return existing SQLite sidecar paths (``-wal`` / ``-shm`` / ``-journal``)."""
    return [db_path + suffix for suffix in _SIDECAR_SUFFIXES if os.path.exists(db_path + suffix)]


def remove_sidecars(db_path: str) -> list[str]:
    """Delete stale sidecars of *db_path*, returning the paths actually removed.

    Called before overwriting a database from an archive: an old ``-wal``
    belongs to the *previous* database image, and SQLite would replay it over
    the freshly restored main file, silently reverting or corrupting it.
    """
    removed = []
    for path in collect_sidecars(db_path):
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            logger.warning("[Quill Backup] 清理陈旧 sidecar 失败 %s: %s", path, exc)
    return removed


def _uri_path(path: str) -> str:
    """Percent-quote a filesystem path for a SQLite URI (Windows drive-safe)."""
    abspath = os.path.abspath(path).replace("\\", "/")
    if not abspath.startswith("/"):
        abspath = "/" + abspath
    from urllib.parse import quote

    return quote(abspath, safe="/:")


def _snapshot_to_temp(src: str, tmp_dir: str) -> str:
    """Snapshot *src* into a temp file inside *tmp_dir*; caller must remove it."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=tmp_dir)
    os.close(fd)
    try:
        snapshot_sqlite(src, tmp_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


def build_backup_zip(dirs: list[str], buf: io.BytesIO) -> tuple[int, list[str]]:
    """Write a zip of *dirs* into *buf*.

    Returns ``(file_count, warnings)``.  Database files are archived as
    consistent snapshots (see module docstring); every other file is copied
    verbatim.  A database that cannot be snapshotted falls back to a raw byte
    copy and is recorded in ``warnings`` so callers can surface the risk
    instead of silently shipping a snapshot that may be missing WAL commits.

    Archive entry names are unchanged from the previous implementation
    (``<basename-of-dir>/<relative-path>``, forward slashes), so archives
    remain interchangeable with older releases.
    """
    count = 0
    warnings: list[str] = []
    tmp_root = tempfile.mkdtemp(prefix="quill_backup_")

    def _archive(is_sqlite: bool, fpath: str, arcname: str, zf: zipfile.ZipFile) -> None:
        nonlocal count
        if is_sqlite:
            try:
                snap = _snapshot_to_temp(fpath, tmp_root)
            except Exception as exc:
                warnings.append(f"{arcname}: 数据库快照失败，已退回原始文件副本 ({type(exc).__name__})")
                logger.warning("[Quill Backup] %s 快照失败，退回裸拷: %s", arcname, exc, exc_info=True)
            else:
                try:
                    zf.write(snap, arcname)
                    count += 1
                    return
                finally:
                    try:
                        os.remove(snap)
                    except OSError:
                        pass
        zf.write(fpath, arcname)
        count += 1

    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for base in dirs:
                for root, _dirs, files in os.walk(base):
                    for fname in files:
                        if fname.endswith(_SKIP_SUFFIXES):
                            continue
                        fpath = os.path.join(root, fname)
                        arcname = os.path.join(
                            os.path.basename(base), os.path.relpath(fpath, base)
                        ).replace(os.sep, "/")
                        try:
                            _archive(is_sqlite_file(fpath), fpath, arcname, zf)
                        except OSError as exc:
                            # A single unreadable file must not abort the backup.
                            warnings.append(f"{arcname}: 读取失败 ({type(exc).__name__})")
                            logger.warning("[Quill Backup] 归档 %s 失败: %s", arcname, exc)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return count, warnings
