# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 数据库版本入口：先识别、备份与恢复副本校验，再允许迁移。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger

if __package__:
    from ._backup_util import snapshot_sqlite
    from .quill.core.errors import StorageError
else:  # 保留 kb.py 原有的顶层脚本加载方式。
    from _backup_util import snapshot_sqlite
    from quill.core.errors import StorageError


SCHEMA_TARGETS = {"memory":2,"writing":1,"documents":1}
# 这是迁移审计元数据；实际幂等 DDL 由各 store 在一个事务内执行，提交前统一验结构。
MIGRATIONS = {kind:{0:{"to":1,"name":"legacy_to_baseline_v1"}} for kind in SCHEMA_TARGETS}
MIGRATIONS["memory"][1] = {"to":2,"name":"shared_scopes_and_transcript"}
REQUIRED_TABLES = {
    "memory": {"memories": {"id", "session_id", "summary", "vector", "dim"},
               "chat_logs": {"id", "session_id", "role", "content"}},
    "writing": {"writing_resource": {"id", "entry_id", "category", "keywords", "content"},
                "knowledge_base": {"id", "entry_id", "category", "keywords", "content"}},
    "documents": {"chunks": {"id", "doc_id", "source", "chunk_index", "content", "faiss_id"}},
}
REQUIRED_TABLES['memory'].update({
    'qp_origins':{'origin_key','umo','platform_id','message_type'},
    'qp_scopes':{'scope_id','name','persona_id','revision','active'},
    'qp_members':{'origin_key','scope_id','epoch'},
    'qp_turns':{'seq','scope_id','persona_id','turn_id','role','content','status','revision'},
    'qp_inbox':{'origin_key','message_id','content_hash','turn_id','status','attempts'},
    'qp_actors':{'actor_id','platform_id','sender_id','canonical_id'},
})


class SchemaCompatibilityError(StorageError):
    pass


def legacy_fts_repair_plan(tables, triggers):
    """仅识别已知历史索引的孤儿空壳；不放行未知虚拟表或真实数据损坏。"""
    names = {row[0] for row in tables}
    orphans = []
    for name, sql in tables:
        if name not in ("knowledge_fts", "knowledge_base_fts") or not sql:
            continue
        content = re.search(r"content\s*=\s*['\"]?(knowledge_base)['\"]?(?:\s*[,)]|\s*$)", sql, re.I)
        if re.search(r"USING\s+fts5\s*\(", sql, re.I) and content and content[1] not in names:
            orphans.append(name)
    if not orphans:
        return [], []
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    plan = []
    for name, target, sql in triggers:
        if target == "writing_resource" and sql and any(re.search(r"\b" + fts + r"\b", sql) for fts in orphans):
            plan.append(("DROP TRIGGER IF EXISTS " + quote(name), ()))
    # DROP TABLE 会尝试构造缺少内容表/影子表的旧索引。限定名称、FTS5 声明和缺表
    # 三重证据后才删除其 schema 壳；实际素材与当前 writing_resource_fts 均不在计划内。
    plan.append(("PRAGMA writable_schema=ON", ()))
    for name in orphans:
        plan.append(("DELETE FROM sqlite_master WHERE type='table' AND name=?", (name,)))
        for suffix in ("_data", "_idx", "_docsize", "_config", "_content"):
            if name + suffix in names:
                plan.append(("DROP TABLE " + quote(name + suffix), ()))
    return orphans, plan


def _verify_restorable(path: Path, kind=""):
    # 快照保持原样；在恢复副本上实际执行限定修复，再检查整个库，不能吞掉损坏错误。
    with tempfile.TemporaryDirectory(prefix="quill_schema_verify_", dir=path.parent) as temporary:
        restored = Path(temporary) / "restored.db"
        shutil.copyfile(path, restored)
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(restored.read_bytes()).digest():
            raise StorageError("迁移备份副本校验失败")
        with closing(sqlite3.connect(restored)) as db:
            if kind == "writing":
                orphans, plan = legacy_fts_repair_plan(
                    db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall(),
                    db.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'").fetchall())
                if plan:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        for sql, args in plan:
                            db.execute(sql, args)
                        # P6：DDL 自身也可能递增 cookie；按此连接执行后的值失效缓存，不能回写规划时旧值。
                        cookie=db.execute("PRAGMA schema_version").fetchone()[0]
                        db.execute(f"PRAGMA schema_version={cookie + 1}")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                    finally:
                        db.execute("PRAGMA writable_schema=OFF")
                    logger.warning("[Quill Schema] 恢复副本移除了已知孤儿 FTS 壳 %s，继续完整检查；原始快照保留", orphans)
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise StorageError("迁移备份无法通过完整性检查")


def _backup(path: str, kind: str, version: int) -> str:
    folder = Path(path).resolve().parent / ".schema_backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / f"{kind}.v{version}.{uuid4().hex}.db"
    # 失败的快照/校验副本也保留，不能在升级失败时删掉唯一的恢复线索。
    snapshot_sqlite(path, str(target))
    os.chmod(target, 0o600)
    # 相同一致性快照的失败重试不堆积副本；只在确有相同恢复线索时删除冗余新快照。
    digest = hashlib.sha256(target.read_bytes()).digest()
    for existing in folder.glob(f"{kind}.v{version}.*.db"):
        if existing != target and existing.stat().st_size == target.stat().st_size and hashlib.sha256(existing.read_bytes()).digest() == digest:
            target.unlink()
            target = existing
            break
    try:
        _verify_restorable(target, kind)
    except Exception:
        logger.error("[Quill Schema] 恢复副本校验失败，原始快照保留于 %s", target)
        raise
    os.chmod(target, 0o600)  # POSIX 权限；Windows 权限继承目录 ACL，不宣称已收紧 ACL。
    return str(target)


async def prepare_database(connection, path: str, kind: str) -> dict:
    version = (await (await connection.execute("PRAGMA user_version")).fetchone())[0]
    target=SCHEMA_TARGETS[kind]
    if version > target or version < 0:
        raise SchemaCompatibilityError(
            f"{kind} 数据库版本 {version} 高于支持范围，已拒绝写入",
            detail="schema_future_version",context={"schema_version":version,"supported_version":target,"kind":kind},
        )
    steps=[]
    cursor_version=version
    while cursor_version<target:
        migration=MIGRATIONS[kind].get(cursor_version)
        if not migration or migration['to']<=cursor_version:
            raise SchemaCompatibilityError("缺少已登记的数据库升级路径，已拒绝写入")
        steps.append({"from":cursor_version,**migration})
        cursor_version=migration['to']
    names = {row[0] for row in await (await connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )).fetchall()}
    expected = REQUIRED_TABLES[kind]
    present = names.intersection(expected)
    if names and not present:
        raise SchemaCompatibilityError("数据库结构不属于已知版本，已拒绝迁移", detail="schema_unrecognized")
    for table in present:
        columns = {row[1] for row in await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()}
        if not expected[table].issubset(columns):
            raise SchemaCompatibilityError("数据库关键字段缺失，已拒绝迁移", detail="schema_missing_columns")
    missing = set(expected) - names if kind != "writing" else not present
    if version == target and missing:
        raise SchemaCompatibilityError("数据库版本与实际表结构不一致", detail="schema_version_mismatch")
    backup = None
    orphan_fts = []
    if kind == "writing":
        orphan_fts, _ = legacy_fts_repair_plan(
            await (await connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")).fetchall(), [])
    if names and (version < target or orphan_fts):
        backup = await asyncio.to_thread(_backup, path, kind, version)
        logger.info("[Quill Schema] %s 旧库快照已写入，恢复副本通过 integrity_check", kind)
    return {"version": version, "target_version": target,
            "backup": backup, "kind": kind, "fresh": not names,"migration_steps":steps,"legacy_fts_orphans":orphan_fts}


async def finish_database(connection, *, kind):
    target=SCHEMA_TARGETS[kind]
    # 不仅凭版本登记盖章：同一迁移事务的最终结构必须满足当前读写协议。
    names = {r[0] for r in await (await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    expected = REQUIRED_TABLES[kind]
    tables = set(expected) & names
    if not tables or (kind != "writing" and tables != set(expected)):
        raise SchemaCompatibilityError("迁移后的表结构不完整，已回滚", detail="schema_version_mismatch")
    for table in tables:
        columns = {r[1] for r in await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()}
        if not expected[table].issubset(columns):
            raise SchemaCompatibilityError("迁移后的关键字段缺失，已回滚", detail="schema_missing_columns")
    await connection.execute(f"PRAGMA user_version={target}")
    await connection.commit()
    return target
