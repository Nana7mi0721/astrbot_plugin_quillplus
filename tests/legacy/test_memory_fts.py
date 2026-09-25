# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""动态记忆 FTS 检索单测（临时目录，测完自动清理）。

覆盖三件事，每件都对应一个真实缺陷或迁移风险：
  1. unicode61 老库能被自动迁移到 trigram，且**数据不丢**（重建索引后回填）；
  2. ≥3 字查询经 FTS 快路径命中；
  3. 1-2 字短查询经 LIKE 兜底命中（trigram 结构上无法命中短词）；
  4. 跨 session 不串（此前 MATCH 不带 session 过滤，会取到别的会话的行）。

运行：
    cd <插件目录的父目录>
    PYTHONPATH="D:/Program/AstrBot/backend/app;$PWD" \
        "D:/Program/AstrBot/backend/python/python.exe" \
        astrbot_plugin_quillplus/tests/test_memory_fts.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
ASTRBOT_APP = os.environ.get("ASTRBOT_APP", r"D:\Program\AstrBot\backend\app")

for _p in (_PLUGIN_PARENT, ASTRBOT_APP):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from astrbot_plugin_quillplus.quill_rag.memory_store import MemoryStore  # noqa: E402

_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _assert(cond: bool, label: str) -> None:
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
        print(f"  [PASS] {label}")
    else:
        _FAILED += 1
        _FAILURES.append(label)
        print(f"  [FAIL] {label}")


def _eq(got, want, label: str) -> None:
    ok = got == want
    _assert(ok, label if ok else f"{label}\n         期望={want!r}\n         实际={got!r}")


def _vec(seed: float, dim: int = 8) -> list[float]:
    """构造一个确定性的假向量（避免依赖 embedding 模型）。"""
    return [seed + i * 0.01 for i in range(dim)]


async def t1_short_and_long(store: MemoryStore) -> None:
    print("\n== 1. 关键词通道：长词走 FTS、短词走 LIKE 兜底 ==")

    await store.add("S1", "用户喜欢在生日那天吃蛋糕", _vec(1.0))
    await store.add("S1", "用户养了一只叫小黑的猫", _vec(2.0))
    await store.add("S2", "别的会话在生日那天吃了披萨", _vec(3.0))

    hits_long = await store._fts_scores("S1", "在生日那天")
    _assert(len(hits_long) >= 1, "≥3 字查询命中（FTS 快路径）")

    hits_short = await store._fts_scores("S1", "生日")
    _assert(len(hits_short) >= 1, "2 字短查询命中（LIKE 兜底，trigram 无法命中短词）")

    hits_one = await store._fts_scores("S1", "猫")
    _assert(len(hits_one) >= 1, "1 字短查询命中（LIKE 兜底）")

    hits_none = await store._fts_scores("S1", "完全不存在的内容")
    _eq(hits_none, {}, "无命中时返回空表")


async def t2_session_isolation(store: MemoryStore) -> None:
    print("\n== 2. 跨 session 隔离（此前 MATCH 不带过滤，会取到别的会话）==")

    hits_s1 = await store._fts_scores("S1", "在生日那天")
    hits_s2 = await store._fts_scores("S2", "在生日那天")

    _assert(len(hits_s1) >= 1 and len(hits_s2) >= 1, "两侧各自命中本会话的条目")

    # 取出各会话的摘要，确认没有互相串
    s1_sums = await store._exec_fetchall(
        "SELECT summary FROM memories WHERE session_id='S1'"
    )
    s2_sums = await store._exec_fetchall(
        "SELECT summary FROM memories WHERE session_id='S2'"
    )
    s1_ids = {r[0] for r in await store._exec_fetchall("SELECT id FROM memories WHERE session_id='S1'")}
    s2_ids = {r[0] for r in await store._exec_fetchall("SELECT id FROM memories WHERE session_id='S2'")}

    _assert(set(hits_s1) <= s1_ids, "S1 的命中集合完全落在 S1 行内")
    _assert(set(hits_s2) <= s2_ids, "S2 的命中集合完全落在 S2 行内")
    _assert(len(s1_sums) == 2 and len(s2_sums) == 1, "两会话数据量如预期")

    # 短词兜底同样必须隔离
    short_s1 = await store._fts_scores("S1", "生日")
    _assert(set(short_s1) <= s1_ids, "短词 LIKE 兜底同样受 session 限定")


async def t3_search_end_to_end(store: MemoryStore) -> None:
    print("\n== 3. search() 端到端（RRF 融合后仍能召回）==")

    res = await store.search("S1", _vec(1.0), top_k=3, query_text="生日")
    _assert(len(res) >= 1, "带 query_text 的检索有结果")

    res_long = await store.search("S1", _vec(1.0), top_k=3, query_text="在生日那天")
    _assert(len(res_long) >= 1, "长查询检索有结果")

    res_empty = await store.search("S1", _vec(1.0), top_k=3, query_text="")
    _assert(len(res_empty) >= 1, "无 query_text 时纯向量通道仍工作（不回退成空）")

    # 短查询命中应当排在前面（LIKE 命中权重高于 BM25 估计）
    if res:
        _assert(
            "生日" in (res[0].get("summary") or ""),
            f"短查询命中的记忆排在首位（实得: {(res[0].get('summary') or '')[:20]}）",
        )


async def t4_migration() -> None:
    print("\n== 4. 老库迁移：unicode61 → trigram，数据不丢 ==")

    tmp = tempfile.mkdtemp(prefix="quilltest_fts_")
    db = os.path.join(tmp, "mem.db")
    try:
        # 手工造一个「旧版」库：unicode61 的 memories_fts + 已存在的数据
        import aiosqlite

        conn = await aiosqlite.connect(db)
        await conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, summary TEXT NOT NULL, chat_summary TEXT DEFAULT '', "
            "vector BLOB NOT NULL, dim INTEGER NOT NULL, "
            "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5(summary, content, tokenize='unicode61')"
        )
        await conn.execute(
            "INSERT INTO memories(session_id, summary, vector, dim) VALUES ('S1','用户在生日那天吃蛋糕',x'00',8)"
        )
        await conn.execute(
            "INSERT INTO memories_fts(rowid, summary, content) VALUES (1,'用户在生日那天吃蛋糕','')"
        )
        await conn.commit()
        await conn.close()

        # 用插件代码打开：应触发迁移 + 回填
        store = MemoryStore(db)
        await store.initialize()
        try:
            cur = await store._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
            row = await cur.fetchone()
            _assert(
                row and "trigram" in row[0].lower(),
                "迁移后 tokenizer 已是 trigram",
            )

            n = await store._exec_fetchone("SELECT COUNT(*) FROM memories_fts")
            _eq(n[0], 1, "迁移后索引条目数与原数据一致（回填生效）")

            hits = await store._fts_scores("S1", "在生日那天")
            _assert(len(hits) >= 1, "迁移后长词查询可用")
            short = await store._fts_scores("S1", "生日")
            _assert(len(short) >= 1, "迁移后短词兜底可用")
        finally:
            await store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t5_idempotent() -> None:
    print("\n== 5. 重复初始化安全（迁移幂等，不重复重建）==")

    tmp = tempfile.mkdtemp(prefix="quilltest_fts2_")
    db = os.path.join(tmp, "mem.db")
    try:
        s1 = MemoryStore(db)
        await s1.initialize()
        await s1.add("S1", "第一次写入的记忆内容", _vec(1.0))
        await s1.close()

        # 二次打开：已是 trigram，不应再走迁移分支、数据也不该被清空
        s2 = MemoryStore(db)
        await s2.initialize()
        try:
            n = await s2._exec_fetchone("SELECT COUNT(*) FROM memories")
            _eq(n[0], 1, "二次初始化后主表数据保留")
            fn = await s2._exec_fetchone("SELECT COUNT(*) FROM memories_fts")
            _eq(fn[0], 1, "二次初始化后索引条目保留")
            _assert(
                len(await s2._fts_scores("S1", "第一次写入")) >= 1,
                "二次初始化后仍可检索",
            )
        finally:
            await s2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main() -> int:
    print("=== 动态记忆 FTS 检索 Self-Test ===")

    tmp = tempfile.mkdtemp(prefix="quilltest_fts_main_")
    try:
        store = MemoryStore(os.path.join(tmp, "main.db"))
        await store.initialize()
        try:
            await t1_short_and_long(store)
            await t2_session_isolation(store)
            await t3_search_end_to_end(store)
        finally:
            await store.close()
        await t4_migration()
        await t5_idempotent()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果: {_PASSED} passed, {_FAILED} failed ===")
    if _FAILURES:
        print("\n失败项：")
        for f in _FAILURES:
            print("  - " + f)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
