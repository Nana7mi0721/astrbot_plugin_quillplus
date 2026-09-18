# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime data locations for Quill.

Why this module exists
----------------------
Historically every runtime artifact (SQLite databases, world books, persona
cards, the chat-state JSON) lived *inside* the plugin directory, under
``knowledge/``, ``worldbooks/`` and ``data/``.  That placement breaks plugin
updates on Windows:

* the plugin keeps long-lived ``aiosqlite`` connections open, so those ``.db``
  files are held by the running process;
* AstrBot's updater deletes the whole plugin directory before extracting a new
  archive (``remove_dir(plugin_path)`` in ``star/updater.py``);
* Windows refuses to delete a file another process has open, so the update
  aborts partway (``WinError 32``), leaving the installation half-updated.

AstrBot's convention is to keep per-plugin runtime data outside the plugin
directory, in ``<AstrBot data>/plugin_data/<plugin name>/`` — that path is
never touched by the updater.  This module resolves that location and moves
existing data there once, so updates no longer fight over locked files.

The move is safe by construction:

* a file is only moved when the destination does not already exist, so a
  half-finished migration simply resumes on the next start;
* every SQLite database is moved together with its ``-wal`` / ``-shm``
  sidecars, so commits still sitting in the WAL are not lost;
* if the data directory cannot be resolved or written, everything falls back
  to the legacy in-plugin paths and the plugin keeps working as before.

Pure standard library, no AstrBot import at module scope: the resolver is
unit-testable on its own and never breaks plugin import when AstrBot's helper
is unavailable.
"""

from __future__ import annotations

import os
import shutil

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_quillplus"

# Sidecars must travel with their database: SQLite pairs them by file name, so a
# database separated from its WAL silently loses the transactions it holds.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# Legacy in-plugin directory -> name inside the new data root.
# ``knowledge`` and ``worldbooks`` keep their names so the archive layout and
# the on-disk layout stay stable across the move.  The old ``data`` directory is
# flattened into the root instead, because its contents (quill_state.json,
# quill_personas/) are already plugin-specific — and a nested ``data`` folder
# inside the data root would collide with legacy archive entry names.
_MIGRATIONS = (
    ("knowledge", None),
    ("worldbooks", None),
    ("data", ("quill_state.json", "quill_personas", "quill_avatars")),
)


def plugin_data_root(plugin_dir: str) -> str:
    """Return the directory that should hold this plugin's runtime data.

    Prefers AstrBot's ``plugin_data/<plugin name>/``.  Falls back to
    ``<plugin_dir>/data_runtime/`` when the AstrBot helper is unavailable, and
    reports the legacy layout only when the directory cannot be created at all.
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        # Standalone / test context: keep data out of the plugin dir anyway so
        # the "don't write inside the plugin" invariant still holds.
        base = os.path.join(plugin_dir, "data_runtime")
    try:
        os.makedirs(base, exist_ok=True)
        probe = os.path.join(base, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return base
    except OSError as exc:
        logger.warning(
            "[Quill Paths] 无法使用外部数据目录 %s（%s），回退到插件内旧路径",
            base,
            exc,
        )
        return ""


def _move_one(src: str, dst: str) -> bool:
    """Move *src* to *dst* if needed. Returns True when a move happened."""
    if not os.path.exists(src):
        return False
    if os.path.exists(dst):
        # Destination already present: never clobber it. The legacy copy is left
        # in place so nothing is lost if this was an unexpected conflict.
        logger.info("[Quill Paths] 目标已存在，保留旧文件不动: %s", dst)
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(src, dst)
    except OSError as exc:
        # Cross-device moves can refuse on Windows while a handle is open; fall
        # back to copying so the data still reaches the new location.
        logger.warning("[Quill Paths] 移动失败 %s -> %s（%s），改为复制", src, dst, exc)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except OSError as exc2:
            logger.warning("[Quill Paths] 复制也失败，保留原位: %s", exc2)
            return False
    return True


def _migrate_file(src: str, dst: str) -> bool:
    """Move a database (plus sidecars) or a plain file."""
    moved = _move_one(src, dst)
    if moved and src.endswith(".db"):
        for suffix in _SIDECAR_SUFFIXES:
            _move_one(src + suffix, dst + suffix)
    return moved


def migrate_legacy_data(plugin_dir: str, data_root: str) -> dict:
    """Move legacy in-plugin runtime data into *data_root*.

    ``knowledge/`` and ``worldbooks/`` keep their directory name inside the data
    root; the old ``data/`` contents are flattened to the root (see _MIGRATIONS).

    Idempotent and resumable: already-migrated items are skipped, and an
    existing destination file is never overwritten.  Returns a summary dict
    describing what moved, for logging at startup.
    """
    summary: dict[str, object] = {"moved": [], "skipped": [], "root": data_root}
    if not data_root:
        return summary

    for legacy_sub, names in _MIGRATIONS:
        legacy_dir = os.path.join(plugin_dir, legacy_sub)
        if not os.path.isdir(legacy_dir):
            continue
        items = names
        if items is None:
            try:
                items = sorted(os.listdir(legacy_dir))
            except OSError:
                continue
        # knowledge/worldbooks keep their name in the data root; the old data/
        # directory is flattened (its files are already plugin-specific).
        dest_dir = os.path.join(data_root, legacy_sub) if names is None else data_root
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("[Quill Paths] 无法创建 %s: %s", dest_dir, exc)
            continue
        for name in items:
            src = os.path.join(legacy_dir, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(dest_dir, name)
            try:
                if _migrate_file(src, dst):
                    summary["moved"].append(f"{legacy_sub}/{name}")
                else:
                    summary["skipped"].append(f"{legacy_sub}/{name}")
            except OSError as exc:
                logger.warning("[Quill Paths] 迁移 %s 失败: %s", src, exc)
                summary["skipped"].append(f"{legacy_sub}/{name}")

    if summary["moved"]:
        logger.info(
            "[Quill Paths] 已把 %d 项运行数据迁移到 %s（插件更新不再与其冲突）",
            len(summary["moved"]),
            data_root,
        )
    return summary


def resolve_data_layout(plugin_dir: str) -> dict:
    """Resolve every runtime path the plugin needs, migrating legacy data once.

    Returns a dict of absolute paths.  When the external data root is
    unavailable the legacy in-plugin paths are returned unchanged, so the
    plugin degrades to the previous behaviour instead of failing to start.
    """
    data_root = plugin_data_root(plugin_dir)
    migrated = False
    if data_root:
        before = os.path.isdir(os.path.join(plugin_dir, "knowledge"))
        summary = migrate_legacy_data(plugin_dir, data_root)
        migrated = bool(summary["moved"]) or not before

    if not data_root:
        # Legacy layout, unchanged.
        return {
            "data_root": "",
            "knowledge_dir": os.path.join(plugin_dir, "knowledge"),
            "worldbooks_dir": os.path.join(plugin_dir, "worldbooks"),
            "state_dir": os.path.join(plugin_dir, "data"),
            "personas_dir": os.path.join(plugin_dir, "data", "quill_personas"),
            "avatars_dir": os.path.join(plugin_dir, "data", "quill_avatars"),
            "imports_dir": os.path.join(plugin_dir, "data", "imports"),
            "legacy": True,
            "migrated": False,
        }

    paths = {
        "data_root": data_root,
        "knowledge_dir": os.path.join(data_root, "knowledge"),
        "worldbooks_dir": os.path.join(data_root, "worldbooks"),
        "state_dir": data_root,
        "personas_dir": os.path.join(data_root, "quill_personas"),
        "avatars_dir": os.path.join(data_root, "quill_avatars"),
        "imports_dir": os.path.join(data_root, "imports"),
        "legacy": False,
        "migrated": migrated,
    }
    for key in ("knowledge_dir", "worldbooks_dir", "state_dir", "personas_dir",
                "avatars_dir", "imports_dir"):
        try:
            os.makedirs(paths[key], exist_ok=True)
        except OSError as exc:
            logger.warning("[Quill Paths] 无法创建 %s: %s", paths[key], exc)
    return paths


# ----------------------------------------------------------------------
# Backup / restore mapping
# ----------------------------------------------------------------------
#
# Archive entries are stored relative to the data root, e.g.
# ``knowledge/quill_wr.db``, ``worldbooks/x.json``, ``quill_state.json``.
# Older archives (produced before the data root existed) nested the state under
# a ``data/`` prefix — ``data/quill_state.json``, ``data/quill_personas/...``.
# Restoring accepts both: a leading ``data/`` is stripped and the rest is
# resolved against the data root, which is exactly the inverse of the
# migration above, so archives remain interchangeable across versions.

# Top-level names that may be restored. Anything else in an archive is skipped
# so a hand-made zip cannot drop arbitrary files next to the data.
_RESTORABLE_TOP = ("knowledge", "worldbooks")


def backup_sources(paths: dict, plugin_dir: str) -> list[tuple[str, str]]:
    """Return ``(archive_prefix, source_dir)`` pairs to archive.

    With the data root in place the whole root is archived in one pass, so
    entries come out as ``knowledge/...``, ``worldbooks/...`` and the state
    files at the top level.  In the legacy fallback layout the three in-plugin
    directories are archived under their own names, which keeps the exact
    historical archive layout.
    """
    if paths.get("legacy"):
        return [
            (name, os.path.join(plugin_dir, name))
            for name in ("data", "knowledge", "worldbooks")
            if os.path.isdir(os.path.join(plugin_dir, name))
        ]
    root = paths["data_root"]
    return [("", root)] if root and os.path.isdir(root) else []


def resolve_archive_dest(paths: dict, plugin_dir: str, arcname: str) -> str | None:
    """Map one archive entry name to its absolute destination path.

    Returns ``None`` when the entry must be skipped.  Guards against path
    traversal and against writing outside the data areas.
    """
    name = arcname.replace("\\", "/").lstrip("/")
    if not name or ".." in name.split("/"):
        return None
    parts = name.split("/")
    if parts[0] == "data":
        parts = parts[1:]  # legacy archives nested the state under data/
        if not parts:
            return None
    rel = "/".join(parts)
    if paths.get("legacy"):
        root = plugin_dir
    else:
        root = paths["data_root"] or plugin_dir
    # Only the data areas are restorable; everything else in an archive is
    # ignored so a crafted zip cannot overwrite plugin code.
    if rel.split("/")[0] not in _RESTORABLE_TOP and rel.split("/")[0] not in (
        "quill_state.json", "quill_personas", "quill_avatars", "imports",
    ):
        return None
    dest = os.path.normpath(os.path.join(root, *parts))
    root_norm = os.path.normpath(root)
    if dest != root_norm and not dest.startswith(root_norm + os.sep):
        return None
    return dest
