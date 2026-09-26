# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LIKE 通配符转义 + session 键处理自查测试（v5.3.0 M3.3，对照重构版
VERIFICATION.md 缺陷清单的存储相关项）。

两类缺陷形态：
1. **LIKE 未转义**：用户输入 / 会话键原样拼进 LIKE 模式，``%``/``_`` 被当
   通配符——短词兜底多召回噪声（``100%`` 按 ``%100%%`` 匹配）、
   ``target_id::%`` 前缀删除误伤其他会话（``群_1`` 会连 ``群X1`` 一起删）。
   修复：``escape_like`` + ``LIKE ? ESCAPE '\\'``。
2. **session 键过滤**：FTS / LIKE 检索必须限定 session_id（此前 MATCH 不带
   session 过滤，本会话命中会被别的会话挤掉）——现将其钉为回归契约。
"""

from __future__ import annotations

from astrbot_plugin_quillplus._fts_util import escape_like
from astrbot_plugin_quillplus.kb import WritingResourceManager
from astrbot_plugin_quillplus.quill_rag.memory_store import MemoryStore


# ── escape_like 单元 ─────────────────────────────────────────────


def test_escape_like_escapes_wildcards_and_itself():
    assert escape_like("a%b") == "a\\%b"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("a\\b") == "a\\\\b"          # 转义符本身先转义
    assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"
    assert escape_like("") == ""
    assert escape_like("普通文本") == "普通文本"     # 无通配符原样返回


# ── memory_store：短词 LIKE 兜底路径 ────────────────────────────


async def _make_memory_store(tmp_path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "mem.db"))
    await store.initialize()
    return store


async def test_memory_short_token_like_is_literal(tmp_path):
    """查询 ``a_`` 只按字面匹配 ``a_``，不得把 ``axb`` 也当命中。"""
    store = await _make_memory_store(tmp_path)
    await store.add("s1", "价格a_特惠说明", [0.1] * 4)
    await store.add("s1", "价格axb特惠说明", [0.2] * 4)
    rows = await store.list_memories("s1")
    id_underscore = next(r["id"] for r in rows if r["summary"] == "价格a_特惠说明")

    scores = await store._fts_scores("s1", "a_")
    assert set(scores) == {id_underscore}
    await store.close()


async def test_memory_backslash_token_is_literal(tmp_path):
    store = await _make_memory_store(tmp_path)
    await store.add("s1", "路径xa\\yz说明", [0.1] * 4)
    await store.add("s1", "路径xayz说明", [0.2] * 4)
    rows = await store.list_memories("s1")
    id_backslash = next(r["id"] for r in rows if "xa\\yz" in r["summary"])

    scores = await store._fts_scores("s1", "a\\")
    assert set(scores) == {id_backslash}
    await store.close()


async def test_memory_like_path_stays_session_scoped(tmp_path):
    """缺陷自查（session 键处理）：LIKE 兜底命中不得跨 session 泄漏。"""
    store = await _make_memory_store(tmp_path)
    await store.add("s1", "锚点目标记忆", [0.1] * 4)
    await store.add("s2", "锚点目标记忆", [0.2] * 4)
    rows1 = await store.list_memories("s1")
    rows2 = await store.list_memories("s2")

    scores1 = await store._fts_scores("s1", "锚点")
    scores2 = await store._fts_scores("s2", "锚点")
    assert set(scores1) == {rows1[0]["id"]}
    assert set(scores2) == {rows2[0]["id"]}
    await store.close()


# ── memory_store：target_id 前缀删除（LIKE 通配符误伤）──────────


async def test_delete_all_session_memories_prefix_is_literal(tmp_path):
    """``g_1`` 的前缀删除不得连带 ``gX1``（``_`` 是任意单字符通配符）。"""
    store = await _make_memory_store(tmp_path)
    for sid in ("g_1", "g_1::p2", "gX1", "gX1::p3"):
        await store.add(sid, f"{sid} 的记忆", [0.1] * 4)

    deleted = await store.delete_all_session_memories("g_1")
    assert deleted == 2                                   # g_1 与 g_1::p2
    remaining = {r["session_id"] for r in await store.list_all_memories(100)}
    assert remaining == {"gX1", "gX1::p3"}
    await store.close()


async def test_delete_all_session_chat_logs_prefix_is_literal(tmp_path):
    store = await _make_memory_store(tmp_path)
    for sid in ("g_1", "gX1"):
        await store.log_message(sid, "user", f"{sid} 的消息")
    deleted = await store.delete_all_session_chat_logs("g_1")
    assert deleted == 1
    logs = await store.get_recent_chat_logs("gX1", limit=10)
    assert len(logs) == 1 and logs[0]["content"] == "gX1 的消息"
    await store.close()


# ── kb（WritingResourceManager）：面板 search 的 LIKE 通道 ──────


async def _make_wr(entries) -> WritingResourceManager:
    wr = WritingResourceManager(":memory:")
    await wr.initialize()
    for e in entries:
        assert await wr.add_entry(**e)
    return wr


async def test_kb_search_wildcards_are_literal():
    wr = await _make_wr([
        dict(category="场景", entry_id="e_code", keywords=["货号"],
             content="货号10_x批次", priority=5),
        dict(category="场景", entry_id="e_plain", keywords=["货号"],
             content="10x批次无下划线", priority=5),
        dict(category="场景", entry_id="e_percent", keywords=["成分"],
             content="100%纯棉材质", priority=5),
    ])
    # ``10_``：字面匹配只中 e_code（改前 ``_`` 通配会把 e_code/e_plain 都召回）
    hits = await wr.search("10_")
    assert [h["entry_id"] for h in hits] == ["e_code"]
    # ``100%``：字面匹配只中 e_percent（改前 ``%100%%`` 等价于子串 100）
    hits = await wr.search("100%")
    assert [h["entry_id"] for h in hits] == ["e_percent"]
    await wr.close()
