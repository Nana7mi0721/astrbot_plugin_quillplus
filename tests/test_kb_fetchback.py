# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""kb（WritingResourceManager）M3.3.5 回表改造回归测试。

改前形态：FTS 命中后的兜底扫描 ``SELECT wr.* / SELECT * … LIMIT 2000`` 把
全库 2000 条**含 content 全文**的完整行拖进 Python，而打分只用到
keywords/aliases/secondary_keywords/name 几列。改后两段式：瘦身列扫描
（LIMIT 2000 语义不变）→ 命中后按 id 回表（SELECT 只取命中的行）。

本套件钉三件事：
1. 命中集合 / 打分 / 顺序与改前逐条一致（含 LIMIT 2000 扫描边界语义）；
2. SQL 层不再出现"全表捞 content"——经连接代理记录实际执行的 SQL 断言；
3. FTS 正常路径（命中即返回 / 部分命中返回）完全不经全表扫描。
"""

from __future__ import annotations

import json

import sqlite3

from astrbot_plugin_quillplus.kb import WritingResourceManager


class _TraceConn:
    """aiosqlite 连接代理：记录经 conn.execute 执行的 SQL（断言数据搬运量）。

    注意 execute 必须**同步转发**：aiosqlite 的 ``async with conn.execute(...)``
    用法要求 execute 立即返回其协议对象（Cursor），不能包成 async def。
    """

    def __init__(self, conn):
        self._conn = conn
        self.sqls: list[str] = []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *a, **k):
        self.sqls.append(" ".join(sql.split()))
        return self._conn.execute(sql, *a, **k)


def _entry(entry_id, keywords, content, name=None, priority=5, aliases=None,
           category="场景"):
    return dict(
        category=category, entry_id=entry_id, name=name, keywords=keywords,
        content=content, priority=priority, aliases=aliases,
    )


async def _make_wr(entries=None) -> WritingResourceManager:
    wr = WritingResourceManager(":memory:")
    await wr.initialize()
    wr._conn = _TraceConn(wr._conn)   # 包一层 SQL 记录代理
    for e in (entries or []):
        assert await wr.add_entry(**e), f"add_entry failed: {e['entry_id']}"
    return wr


def _assert_no_full_content_fetch(wr: WritingResourceManager):
    """改前的两条全表捞取模式必须消失（FTS JOIN 带 MATCH 只取命中行，
    允许 wr.*）；其余 SELECT（扫描/回表）按 content 列规则断言。"""
    for s in wr._conn.sqls:
        if " match " in f" {s.lower()} ":
            # FTS JOIN：MATCH 驱动，只取命中的行，不受全表断言约束
            continue
        assert not (
            "wr.*" in s or s.startswith("SELECT * FROM writing_resource WHERE enabled")
        ), f"回旧版全表捞取: {s}"
        if not s.lstrip().upper().startswith("SELECT"):
            continue
        if "id in (" in s.lower():
            continue   # 按 id 回表只取命中行，允许带全列
        head = s.split(" FROM ")[0].lower()
        assert "content" not in head, f"扫描路径捞 content 回归: {s}"


# ── 1. keyword_match（短词兜底路径）─────────────────────────────


async def test_keyword_match_fetchback_slim_and_equivalent():
    entries = [
        _entry("e_rain", ["雨", "下雨"], "雨点敲打屋檐 " * 30, priority=10),
        _entry("e_snow", ["雪"], "雪花飘落 " * 30),
        _entry("e_wind", ["风"], "北风呼啸 " * 30),
    ]
    wr = await _make_wr(entries)
    await wr.update_entry("e_snow", enabled=False)   # 禁用条目不得命中

    matched = await wr.keyword_match("雨")
    assert [e["entry_id"] for e in matched] == ["e_rain"]
    e = matched[0]
    # 回表后是完整行：content / priority 等字段齐全（注入方要用 content）
    assert e["content"].startswith("雨点敲打屋檐")
    assert e["priority"] == 10 and e["enabled"] is True
    assert e["match_score"] == 3
    assert e["matched_keywords"] == ["雨"]
    _assert_no_full_content_fetch(wr)
    # 正向断言：确实走了瘦身扫描 + 按 id 回表
    assert any(s.startswith("SELECT id, keywords FROM writing_resource") for s in wr._conn.sqls)
    assert any("id in (" in s.lower() for s in wr._conn.sqls)
    await wr.close()


async def test_keyword_match_no_hit_returns_empty():
    wr = await _make_wr([_entry("e_rain", ["雨"], "雨点敲打屋檐 " * 30)])
    assert await wr.keyword_match("雪") == []
    _assert_no_full_content_fetch(wr)
    await wr.close()


# ── 2. match() 兜底扫描（FTS 故障降级路径：行为不变、搬运变少）──


async def test_match_fallback_scan_slim_and_equivalent(monkeypatch):
    """FTS 故障 → 兜底扫描（降级路径保持）：命中集合/打分/顺序与改前一致。"""
    entries = [
        _entry("e_foot", ["脚", "足"], "足部动作要点 " * 30, priority=5),
        _entry("e_hand", ["手"], "手部动作要点 " * 30, priority=10),
        _entry("e_both", ["脚"], "手脚并用要点 " * 30, priority=7, aliases=["手"]),
    ]
    wr = await _make_wr(entries)

    async def _fts_broken(*a, **k):
        raise sqlite3.OperationalError("injected fts failure")

    monkeypatch.setattr(wr, "fts_match", _fts_broken)

    results = await wr.match("脚手", top_k=5, log_match=False)
    ids = [e["entry_id"] for e in results]
    # 打分（score = kw×3 + alias×2 + priority×0.1）：
    #   e_both「脚」3 + alias「手」2 + 0.7 = 5.7
    #   e_hand「手」3 + 1.0 = 4.0
    #   e_foot「脚」3 + 0.5 = 3.5（「足」不在查询里）
    assert ids == ["e_both", "e_hand", "e_foot"]
    assert all(e["content"] for e in results)       # 回表后 content 齐全
    _assert_no_full_content_fetch(wr)
    # 正向断言：瘦身扫描 SQL 出现，且命中后按 id 回表
    assert any(
        s.startswith("SELECT wr.id, wr.category, wr.name, wr.keywords") for s in wr._conn.sqls
    )
    assert any("id in (" in s.lower() for s in wr._conn.sqls)
    await wr.close()


async def test_match_zero_hit_still_scans_within_2000(monkeypatch):
    """FTS 故障 + 关键词零命中：仍走扫描（改前行为），结果为空不抛错。"""
    wr = await _make_wr([_entry("e_rain", ["雨"], "雨点敲打屋檐 " * 30)])

    async def _fts_broken(*a, **k):
        raise sqlite3.OperationalError("injected fts failure")

    monkeypatch.setattr(wr, "fts_match", _fts_broken)
    assert await wr.match("雪", top_k=3, log_match=False) == []
    _assert_no_full_content_fetch(wr)
    await wr.close()


# ── 3. FTS 正常路径：命中即返回，完全不碰全表 ───────────────────


async def test_match_fts_hit_returns_without_any_full_scan():
    wr = await _make_wr([
        _entry("e_street", ["雨"], "她缓缓走在雨中的街道，伞沿滴水 " * 20, priority=5),
        _entry("e_other", ["风"], "北风呼啸 " * 20, priority=9),
    ])
    wr._conn.sqls.clear()
    results = await wr.match("雨中的街道", top_k=1, log_match=False)
    assert [e["entry_id"] for e in results] == ["e_street"]
    assert not any("LIMIT 2000" in s for s in wr._conn.sqls), (
        f"FTS 命中路径不应有任何全表扫描: {wr._conn.sqls}"
    )
    _assert_no_full_content_fetch(wr)
    await wr.close()


async def test_match_fts_partial_hits_return_without_full_scan():
    """FTS 正常但命中数不足 top_k：返回已有命中（宁缺勿滥），不补扫描。"""
    wr = await _make_wr([
        _entry("e_street", ["雨"], "她缓缓走在雨中的街道，伞沿滴水 " * 20, priority=5),
    ])
    wr._conn.sqls.clear()
    results = await wr.match("雨中的街道", top_k=5, log_match=False)
    assert [e["entry_id"] for e in results] == ["e_street"]
    assert not any("LIMIT 2000" in s for s in wr._conn.sqls)
    await wr.close()


# ── 4. LIMIT 2000 扫描边界语义保持 ──────────────────────────────


async def test_keyword_match_2000_limit_semantics_preserved():
    """位于扫描窗口内/外的同名关键词条目：窗口内命中、窗口外不命中（与改前一致）。"""
    wr = WritingResourceManager(":memory:")
    await wr.initialize()
    wr._conn = _TraceConn(wr._conn)

    pad = (None, None, json.dumps(["垫位词"], ensure_ascii=False), "填充内容 " * 5, 1)
    rows = [("场景", f"pad_{i}", *pad) for i in range(1998)]
    rows.append(("场景", "special_in", None, None, json.dumps(["锚"], ensure_ascii=False), "锚点条目 " * 5, 5))  # noqa: E501  rowid 1999
    rows += [("场景", f"pad2_{i}", *pad) for i in range(102)]                     # rowid 2000-2101
    rows.append(("场景", "special_out", None, None, json.dumps(["锚"], ensure_ascii=False), "窗外条目 " * 5, 5))  # noqa: E501  rowid 2102
    await wr._conn.executemany(
        "INSERT INTO writing_resource (category, entry_id, name, description, "
        "keywords, content, priority) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await wr._conn.commit()
    wr._conn.sqls.clear()

    matched = await wr.keyword_match("锚")
    assert [e["entry_id"] for e in matched] == ["special_in"]
    _assert_no_full_content_fetch(wr)
    await wr.close()


# ── 5. 既有语义抽查：FTS JOIN 路径的 fts_rank 契约 ──────────────


async def test_fts_match_still_returns_rank_and_full_row():
    wr = await _make_wr([
        _entry("e_street", ["雨"], "她缓缓走在雨中的街道，伞沿滴水 " * 20, priority=5),
    ])
    hits = await wr.fts_match("雨中的街道", top_k=3)
    assert len(hits) == 1
    assert hits[0]["entry_id"] == "e_street"
    assert "fts_rank" in hits[0] and hits[0]["content"]
    await wr.close()
