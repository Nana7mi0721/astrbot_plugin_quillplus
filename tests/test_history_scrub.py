# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H3 历史 contexts 增量清洗游标测试（v5.3.0 M3.4，债务 D8 热路径优化）。

验收面（PLAN §M3.4）
--------------------
1. **等价性**：增量清洗结果与"逐条全量清洗"**逐字节一致**——100 轮长对话
   逐轮比对（注入报告行、状态栏渲染产物/原始标记/裸字段行/剧情分支标记、
   普通文本、空串、重复内容、非 dict / 非 str content 混合；含上下文恢复
   垫回与滑动窗口两种历史演化形态）。对照 oracle 是改前 H3 步 2-3 的
   原版列表推导/循环（逐字保真），另与"全新游标的干净宿主"（= 全量路径）
   交叉验证。
2. **性能**（耗时曲线平稳的可测代理，不做真实时间断言——CI 抖动）：
   计数 monkeypatch 包住 ``QuillPlugin._scrub_inject_report`` /
   ``_strip_status_artifacts``（经 plugin 动态分发的同一 patch 点，与
   test_hook_snapshots 注入约定一致），断言第 N 轮只清洗**新增消息数**
   而非 O(N)：持续历史每轮恒 2 次（每轮新增 2 条）；fresh+垫回场景每轮
   恒 1 次（滑动日志窗口重叠 7 条 + 新 1 条）。
3. **游标失效**：love_fields 变化（唯一运行期可变输入）→ 两个通道都全量
   重洗且输出按新字段表计算；注入报告行正则变化（世代指纹的报告格式
   成分）→ 全量重洗；游标状态被外力改坏 → 回退全量（宁慢勿错）。
4. **内存护栏**：会话 LRU 超限淘汰最旧（淘汰后重访重洗、结果正确）；
   单通道缓存条目上限溢出时未入缓存的尾部下轮重洗。

机制细节（键设计 / 失效条件 / 为何逐字节一致）见
quill/services/history_scrub.py 模块 docstring。
"""

from __future__ import annotations

import re
import types

import pytest

from astrbot_plugin_quillplus import main as M
from astrbot_plugin_quillplus.quill.services import history_scrub as HS

# 复用 H3 快照的宿主/事件桩（真实 QuillPlugin 壳 + 真 _scrub/_strip 类实现，
# 保证清洗走与线上一致的动态分发路径）。
from test_hook_snapshots import (
    UMO,
    _drain_spawned,
    _EvH3,
    _mk_h3_host,
    _ReqH3,
    _run_h3,
)

TID = "test:umo::history-scrub"

# ── 样本（与 test_hook_snapshots 同源的泄漏形态）────────────────────

RENDERED = (
    "**状态栏**\n```\n好感度：88/100（爱意）\n关系阶段：亲密恋人\n"
    "心情：雀跃\n位置：海滩\n穿着：泳装\n当前想法：想法\n\n"
    ">>> 剧情走向 <<<\n1. 继续当前话题\n<<< 请选择 >>>\n```"
)
DIRTY_LOVE = "[LOVE_DATA] 88/100（爱意） | 亲密恋人 | 温柔 | 海滩 | 泳装 | 想法"
DIRTY_STATUS = "前文。[STATUS]好感度=65[/STATUS] 后文。"
BARE_FIELDS = "正文。\n好感度：65\n心情：开心"
PLOT_ASCII = ">>| 剧情走向 |<< 继续调查森林 >>| 请选择 |<<"
REPORT_LINE = "〔注入〕世界书×2 记忆×1"
PLAIN = "普通的剧情正文，没有残留。"


# ── 宿主与 oracle ────────────────────────────────────────────────────


def _mk_host(love_fields=None):
    """最小 QuillPlugin 壳：只挂增量清洗服务触碰的面（t26 手法）。

    清洗函数（QuillPlugin._scrub_inject_report / _strip_status_artifacts）
    与报告行正则（_INJECT_REPORT_LINE_RE）都走类属性，无需实例化；服务
    另读 props.love_fields（世代指纹 + 剥离字段表实参）。
    """
    h = object.__new__(M.QuillPlugin)
    fields = list(love_fields if love_fields is not None
                  else M._DEFAULT_LOVE_FIELDS_RAW)
    h.props = types.SimpleNamespace(love_fields=fields)
    return h


def _oracle_scrub(host, contexts):
    """改前 H3 步 2 的原版列表推导（逐字保真，等价性 oracle）。"""
    return [
        ({**c, "content": host._scrub_inject_report(c.get("content", ""))}
         if isinstance(c, dict) and isinstance(c.get("content"), str) else c)
        for c in contexts
    ]


def _oracle_strip(host, contexts):
    """改前 H3 步 3 的原版循环（逐字保真，等价性 oracle）。"""
    _scrubbed = []
    for c in contexts:
        if isinstance(c, dict) and isinstance(c.get("content"), str):
            clean = host._strip_status_artifacts(
                c["content"], host.props.love_fields
            )
            _scrubbed.append({**c, "content": clean}
                             if clean != c["content"] else c)
        else:
            _scrubbed.append(c)
    return _scrubbed


def _oracle_step23(host, raw_window, restored, sb_off):
    """改前 H3 步 2-3 的块结构（fresh 判定 / 垫回 / 开关方向逐字保真）。"""
    ctxs = _oracle_scrub(host, raw_window)
    contexts_is_fresh = not ctxs or len(ctxs) <= 1
    if contexts_is_fresh and restored:
        ctxs = list(restored) + ctxs
    if sb_off and ctxs:
        ctxs = _oracle_strip(host, ctxs)
    return ctxs


def _incremental_step23(host, raw_window, restored, sb_off, tid=TID):
    """新 H3 步 2-3 的块结构（与 interfaces/astrbot_hooks.py 逐字对应）。"""
    ctxs = HS.scrub_inject_report_history(host, tid, raw_window)
    contexts_is_fresh = not ctxs or len(ctxs) <= 1
    if contexts_is_fresh and restored:
        ctxs = list(restored) + ctxs
    if sb_off and ctxs:
        ctxs = HS.strip_status_history(host, tid, ctxs)
    return ctxs


def _snap(contexts):
    """框架每轮重新给出 contexts 的建模：浅拷贝 dict 项防两路交叉污染。"""
    return [dict(c) if isinstance(c, dict) else c for c in contexts]


def _restored_logs():
    """上下文恢复垫回的 8 条 chat_logs（含报告行与渲染栏——恢复来的
    日志不经报告行抹除、但经状态栏剥离，改前语义，oracle 一并复刻）。"""
    return [
        {"id": 1, "role": "user", "content": "历史消息1"},
        {"id": 2, "role": "assistant", "content": f"历史回复2\n\n{REPORT_LINE}"},
        {"id": 3, "role": "user", "content": "历史消息3"},
        {"id": 4, "role": "assistant", "content": "历史栏4\n" + RENDERED},
        {"id": 5, "role": "user", "content": "历史消息5"},
        {"id": 6, "role": "assistant", "content": DIRTY_LOVE},
        {"id": 7, "role": "user", "content": "历史消息7"},
        {"id": 8, "role": "assistant", "content": PLAIN},
    ]


def _gen_pairs(turn, duplicate=False):
    """第 turn 轮的一对（user, assistant）消息；assistant 按轮次混入
    报告行/渲染栏/原始标记/裸字段/剧情分支/普通文本变体。

    duplicate=True 时每 5 轮复用完全相同的历史内容（跨轮指纹去重路径）；
    计数类测试保持 duplicate=False 保证"每轮新增 2 条唯一内容"的计数数学。"""
    user = {"role": "user", "content": f"用户第{turn}轮输入"}
    if duplicate and turn % 5 == 4:
        ai = PLAIN
    else:
        variant = turn % 6
        if variant == 0:
            ai = f"回复{turn}\n\n{REPORT_LINE}\n\n\n\n尾声{turn}"
        elif variant == 1:
            ai = f"  {REPORT_LINE}\n正文{turn}。\n\n〔注入〕记忆×3\n\n\n\n收尾{turn}"
        elif variant == 2:
            ai = f"前文{turn}\n" + RENDERED + "\n后文"
        elif variant == 3:
            ai = f"{DIRTY_LOVE}\n{DIRTY_STATUS}（{turn}）"
        elif variant == 4:
            ai = f"{BARE_FIELDS}\n{PLOT_ASCII}（{turn}）"
        else:
            ai = f"{PLAIN}（第{turn}轮）"
    return [user, {"role": "assistant", "content": ai}]


def _counting_patch(monkeypatch):
    """计数 wrapper 包住两个清洗函数（类属性 patch 点 = 动态分发点）。

    返回计数 dict；wrapper 委托 patch 前捕获的原实现，清洗行为不变。
    """
    calls = {"scrub": 0, "strip": 0}
    orig_scrub = M.QuillPlugin._scrub_inject_report
    orig_strip = M.QuillPlugin._strip_status_artifacts

    def _scrub(text):
        calls["scrub"] += 1
        return orig_scrub(text)

    def _strip(text, fields=None):
        calls["strip"] += 1
        return orig_strip(text, fields)

    monkeypatch.setattr(M.QuillPlugin, "_scrub_inject_report",
                        staticmethod(_scrub))
    monkeypatch.setattr(M.QuillPlugin, "_strip_status_artifacts",
                        staticmethod(_strip))
    return calls


# ── 1. 等价性：增量 == 逐条全量（逐字节，100 轮逐轮比对）────────────


def test_incremental_matches_full_clean_byte_for_byte_over_100_turns():
    """100 轮长对话（滑动窗口 + 首轮垫回）：每轮增量结果与改前 oracle
    （同一宿主真函数逐条重洗）以及全新游标宿主的全量路径三方逐字节相等。"""
    host = _mk_host()
    fresh_host = _mk_host()  # 无游标历史 → 每条都 miss = 全量路径
    raw_history: list = []
    restored = _restored_logs()
    last = None
    for turn in range(100):
        if turn == 0:
            # 首轮仅 1 条 → fresh（≤1）→ 上下文恢复垫回参与比对
            raw_history.extend(_gen_pairs(turn, duplicate=True)[:1])
        else:
            raw_history.extend(_gen_pairs(turn, duplicate=True))
        window = raw_history[-35:]  # 框架滑动窗口截头
        inc = _incremental_step23(host, _snap(window), _restored_logs(),
                                  sb_off=True)
        full = _oracle_step23(fresh_host, _snap(window),
                              [dict(c) for c in _restored_logs()],
                              sb_off=True)
        assert inc == full, f"第 {turn} 轮增量与全量不一致"
        last = (inc, full)
    # 末轮再与干净宿主（全量）交叉验证一次
    inc, full = last
    assert inc == full
    # 渲染栏/裸字段/报告行确实被洗过（防止两边都因"没洗"而假绿）
    joined = "\n".join(
        c.get("content", "") for c in inc if isinstance(c, dict))
    assert "**状态栏**" not in joined
    assert "〔注入〕" not in joined
    assert "[LOVE_DATA]" not in joined


def test_incremental_matches_full_clean_when_status_bar_enabled():
    """开启方向（不洗历史）：增量与全量都原样保留，两通道结果一致。"""
    host = _mk_host()
    fresh_host = _mk_host()
    raw_history: list = []
    for turn in range(30):
        raw_history.extend(_gen_pairs(turn))
        window = raw_history[-20:]
        inc = _incremental_step23(host, _snap(window), [], sb_off=False)
        full = _oracle_step23(fresh_host, _snap(window), [], sb_off=False)
        assert inc == full, f"第 {turn} 轮增量与全量不一致"
    # 开启方向渲染栏是合法历史，原样保留
    assert any("**状态栏**" in c.get("content", "")
               for c in inc if isinstance(c, dict))


# ── 2. 性能：第 N 轮只清洗新增消息数（耗时平稳的可测代理）──────────


def test_regex_calls_constant_per_turn_persistent_history(monkeypatch):
    """持续历史（每轮新增 2 条，无窗口滑动）：60 轮里每轮正则调用恒为
    2（新增消息数），总量 120 次而非全量的 3660 次——耗时曲线平稳。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    history: list = []
    per_turn_scrub: list = []
    per_turn_strip: list = []
    for turn in range(60):
        history.extend(_gen_pairs(turn))
        ctxs = HS.scrub_inject_report_history(host, TID, _snap(history))
        per_turn_scrub.append(calls["scrub"])
        calls["scrub"] = 0
        ctxs = HS.strip_status_history(host, TID, ctxs)
        per_turn_strip.append(calls["strip"])
        calls["strip"] = 0
    assert per_turn_scrub == [2] * 60
    assert per_turn_strip == [2] * 60


def test_regex_calls_constant_per_turn_fresh_with_restoration(monkeypatch):
    """fresh+垫回场景（contexts 每轮为空、垫回最近 8 条增长的日志）：
    垫回窗口重叠 7 条 + 新 1 条 → 第 2 轮起每轮恒 1 次剥离调用；
    报告行通道对空 contexts 恒 0 次。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    logs = [{"id": i, "role": "user" if i % 2 else "assistant",
             "content": f"日志内容{i}"} for i in range(8)]
    per_turn_strip: list = []
    for turn in range(40):
        logs.append({"id": 8 + turn, "role": "assistant",
                     "content": f"日志内容{8 + turn}"})
        recent = [dict(c) for c in logs[-8:]]
        ctxs = HS.scrub_inject_report_history(host, TID, [])
        assert calls["scrub"] == 0
        ctxs = recent + ctxs  # 步 2 垫回（改前时序：报告行洗在垫回前）
        HS.strip_status_history(host, TID, ctxs)
        per_turn_strip.append(calls["strip"])
        calls["strip"] = 0
    assert per_turn_strip[0] == 8
    assert per_turn_strip[1:] == [1] * 39


def test_regex_calls_constant_per_turn_with_sliding_window(monkeypatch):
    """滑动窗口形态：窗口恒 20 条、每轮滑入 2 条新消息 → 每轮恒 2 次
    （窗口内旧消息全部命中，被滑出的条目由 GC 淘汰）。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    history: list = []
    per_turn_scrub: list = []
    for turn in range(50):
        history.extend(_gen_pairs(turn))
        window = history[-20:]
        HS.scrub_inject_report_history(host, TID, _snap(window))
        per_turn_scrub.append(calls["scrub"])
        calls["scrub"] = 0
    assert per_turn_scrub == [2] * 50


# ── 3. 游标失效：世代指纹 ────────────────────────────────────────────


def test_love_fields_change_triggers_full_rewash_with_new_fields(monkeypatch):
    """字段表变化（面板保存整体替换 config 的建模）→ 世代指纹变化 →
    两通道全量重洗，且输出按**新字段表**计算（裸字段行判定随表变化）。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    ctx = [{"role": "assistant",
            "content": f"正文。\n好感度：88\n心情：开心\n尾行。\n\n{REPORT_LINE}"}]

    out1 = HS.strip_status_history(host, TID, _snap(ctx))
    assert calls["strip"] == 1
    assert "好感度" not in out1[0]["content"]  # 默认字段表：裸字段行被擦

    out1s = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1

    # 换字段表（唯一运行期可变输入）
    host.props.love_fields = ["催眠度", "信赖度"]
    calls["strip"] = calls["scrub"] = 0  # 只统计失效后的这一轮

    out2 = HS.strip_status_history(host, TID, _snap(ctx))
    assert calls["strip"] == 1  # 全量重洗（指纹失效）
    assert "好感度：88" in out2[0]["content"]  # 新表下不再是裸字段行 → 保留
    assert "催眠度" not in out2[0]["content"]

    out2s = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1  # scrub 通道一并失效重洗（有意的过度失效）
    assert out2s[0]["content"] == out1s[0]["content"]  # 重洗结果仍正确

    # 字段表恢复不变 → 缓存重新生效
    calls["strip"] = calls["scrub"] = 0
    out3 = HS.strip_status_history(host, TID, _snap(ctx))
    out3s = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["strip"] == 0
    assert calls["scrub"] == 0
    assert out3 == out2 and out3s == out2s


def test_report_regex_change_invalidates_cursor_and_changes_output(monkeypatch):
    """注入报告行格式变化（世代指纹的报告格式成分）→ 全量重洗，且输出
    反映新正则（旧格式行不再被抹）。指纹读取面（类属性）与实现读取面
    （模块全局）同步替换。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    ctx = [{"role": "assistant",
            "content": f"前。\n{REPORT_LINE}\n\n\n\n后。"}]
    out1 = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1
    assert out1[0]["content"] == "前。\n\n后。"

    new_re = re.compile(r"^[ \t]*〖报告〗.*$", re.MULTILINE)
    monkeypatch.setattr(M, "_INJECT_REPORT_LINE_RE", new_re)
    monkeypatch.setattr(M.QuillPlugin, "_INJECT_REPORT_LINE_RE", new_re)
    calls["scrub"] = 0  # 只统计失效后的这一轮

    out2 = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1  # 指纹变化 → 全量重洗
    # 新正则不匹配旧格式行 → 原样保留（重洗确实用了新正则，非缓存回放）
    assert out2[0]["content"] == ctx[0]["content"]


def test_corrupt_cursor_state_falls_back_to_full_rewash(monkeypatch):
    """游标状态被外力改坏（cache 非 dict）→ 一致性守卫重置 → 回退全量，
    且重置后状态恢复可用（宁慢勿错）。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    ctx = [{"role": "user", "content": "消息"}]
    HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1
    calls["scrub"] = 0

    cur = HS._cursors_for(host).cursor_for(TID)
    cur.scrub.cache = None  # 外力改坏
    out = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1  # 回退全量重洗
    assert isinstance(HS._cursors_for(host).cursor_for(TID).scrub.cache, dict)
    assert out[0]["content"] == "消息"

    calls["scrub"] = 0
    HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 0  # 守卫重置后缓存恢复生效

    cur = HS._cursors_for(host).cursor_for(TID)
    cur.scrub.last_seq = "not-a-list"  # 另一半状态同样有守卫
    HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1


def test_strip_cache_survives_status_bar_toggle_gap(monkeypatch):
    """开关方向语义（关闭才洗、开启不洗）+ 缓存跨开关间隙存活：
    关→开（剥离通道不推进）→关：第三轮全部命中缓存，结果与第一轮逐字节
    一致（纯函数 + 指纹未变，命中即正确）。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    ctx = [{"role": "assistant", "content": f"前文\n{RENDERED}\n尾行"}]
    out1 = HS.strip_status_history(host, TID, _snap(ctx))
    assert calls["strip"] == 1

    # 开启轮次：钩子的开关分支直接跳过剥离通道（此处以"不调用"建模），
    # 游标与缓存原样休眠
    calls["strip"] = 0
    out3 = HS.strip_status_history(host, TID, _snap(ctx))
    assert calls["strip"] == 0  # 间隙后重开 → 全部命中
    assert out3 == out1


# ── 4. 内存护栏 ──────────────────────────────────────────────────────


def test_cursor_store_lru_evicts_oldest_beyond_cap():
    """会话游标 LRU：超过 MAX_SESSIONS 淘汰最旧；最旧会话重访拿到的是
    全新游标（= 回退全量重洗，正确性不受影响）。"""
    store = HS.HistoryScrubCursors()
    first = store.cursor_for("s-oldest")
    first.scrub.gen = "marker"  # 特征标记：被淘汰后重建的游标不再携带
    for i in range(HS.MAX_SESSIONS + 10):
        store.cursor_for(f"s{i}")
    assert len(store._sessions) == HS.MAX_SESSIONS
    again = store.cursor_for("s-oldest")
    assert again is not first
    assert again.scrub.gen is None  # 全新游标 → 下轮全量重洗


def test_evicted_session_rewashes_correctly_on_next_visit(monkeypatch):
    """淘汰后重访：重洗发生且结果与首洗一致（淘汰只影响性能不影响正确性）。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    ctx = [{"role": "user", "content": "会被淘汰的会话消息"}]
    out1 = HS.scrub_inject_report_history(host, "old-session", _snap(ctx))
    assert calls["scrub"] == 1

    filler = [{"role": "user", "content": "filler"}]
    for i in range(HS.MAX_SESSIONS):
        HS.scrub_inject_report_history(host, f"fill-{i}", _snap(filler))
    calls["scrub"] = 0  # filler 首洗计数清零

    out2 = HS.scrub_inject_report_history(host, "old-session", _snap(ctx))
    assert calls["scrub"] == 1  # 游标已被 LRU 淘汰 → 重洗
    assert out2 == out1  # 结果与首洗逐字节一致


def test_pass_cache_cap_overflow_rewashes_uncapped_tail(monkeypatch):
    """单通道缓存条目上限：溢出轮次超出上限的尾部不入缓存，下轮重洗
    （上限是护栏，宁慢勿错）；上限内的头部仍然命中。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    n = HS.MAX_PASS_CACHE + 100
    big = [{"role": "user", "content": f"消息{i}"} for i in range(n)]
    HS.scrub_inject_report_history(host, TID, _snap(big))
    assert calls["scrub"] == n  # 首轮全部要洗（缓存只收到上限为止）

    calls["scrub"] = 0
    HS.scrub_inject_report_history(host, TID, _snap(big))
    assert calls["scrub"] == n - HS.MAX_PASS_CACHE  # 未入缓存的尾部重洗


# ── 5. 逐条替换语义（红线 b：未清洗返回原文，清洗替换文本）──────────


def test_noneligible_items_pass_through_untouched():
    """非 dict 项 / 非 str content 项 / 无 content 键的 dict：原样透传
    （对象同一性保持，不添不改）；空串 content 合法清洗后仍为空串。"""
    host = _mk_host()
    nonstr = {"role": "user", "content": 123}
    bare = "裸字符串项"
    nokey = {"role": "system"}
    ctx = [nonstr, bare, nokey, {"role": "user", "content": ""}]
    out = HS.scrub_inject_report_history(host, TID, ctx)
    assert out[0] is nonstr
    assert out[1] is bare
    assert out[2] is nokey and "content" not in out[2]
    assert out[3] == {"role": "user", "content": ""}

    out2 = HS.strip_status_history(host, TID, out)
    assert out2[0] is nonstr
    assert out2[1] is bare
    assert out2[2] is nokey
    assert out2[3] == {"role": "user", "content": ""}


def test_strip_keeps_same_dict_when_content_unchanged():
    """剥离未改变内容 → 原 dict 对象原样透传（改前 ``else c`` 语义）；
    改变 → 新 dict、其余键保留、content 为清洗后文本。"""
    host = _mk_host()
    plain = {"role": "user", "content": PLAIN, "extra": "保留"}
    dirty = {"role": "assistant", "content": f"前文\n{RENDERED}", "extra": "保留"}
    out = HS.strip_status_history(host, TID, [plain, dirty])
    assert out[0] is plain
    assert out[0]["content"] == PLAIN
    assert out[1] is not dirty
    assert out[1]["extra"] == "保留"
    assert "**状态栏**" not in out[1]["content"]
    assert out[1]["content"].startswith("前文")


def test_duplicate_contents_share_one_cached_cleaning(monkeypatch):
    """同轮/跨轮的完全相同内容：共享同一份清洗结果（指纹去重），且与
    逐条独立清洗逐字节一致。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_host()
    fresh_host = _mk_host()
    shared = f"回复正文\n\n{REPORT_LINE}"
    ctx = [{"role": "assistant", "content": shared},
           {"role": "assistant", "content": shared}]
    out = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 1  # 同内容只洗一次
    assert out == _oracle_scrub(fresh_host, _snap(ctx))

    calls["scrub"] = 0
    out2 = HS.scrub_inject_report_history(host, TID, _snap(ctx))
    assert calls["scrub"] == 0  # 跨轮命中
    assert out2 == out


# ── 6. 钩子级集成：真实 H3 两轮调用 ─────────────────────────────────


async def test_h3_hook_two_turns_incremental_and_equivalent(monkeypatch):
    """经真实注册桩连跑两轮 H3（原始历史持续形态）：第二轮旧消息全部
    命中游标（报告行抹除只跑新增那几条），且第二轮清洗结果与全新游标
    宿主的全量路径逐字节一致——接线（target_id / req.contexts 回写）
    与机制在钩子层同时得到验证。"""
    calls = _counting_patch(monkeypatch)
    host = _mk_h3_host(sb_panel=False)  # 关状态栏：步 3 剥离通道参与
    ev = _EvH3()
    turn1_raw = [
        {"role": "user", "content": "第一轮用户消息"},
        {"role": "assistant", "content": f"第一轮回复\n\n{REPORT_LINE}"},
    ]
    req1 = _ReqH3(contexts=[dict(c) for c in turn1_raw])
    await _run_h3(host, ev, req1)
    await _drain_spawned(host)
    scrub1, strip1 = calls["scrub"], calls["strip"]
    assert scrub1 == 2 and strip1 == 2  # 首轮：全部清洗

    calls["scrub"] = calls["strip"] = 0
    # 第二轮：框架历史 = 第一轮原始消息 + 新增两条
    turn2_raw = turn1_raw + [
        {"role": "user", "content": "第二轮用户消息"},
        {"role": "assistant", "content": f"第二轮回复\n{BARE_FIELDS}"},
    ]
    req2 = _ReqH3(contexts=[dict(c) for c in turn2_raw])
    await _run_h3(host, ev, req2)
    await _drain_spawned(host)
    # 旧消息命中游标：只洗新增的 2 条（而非 O(4)）
    assert calls["scrub"] == 2
    assert calls["strip"] == 2

    # 第二轮结果 == 全量路径（全新游标宿主对同一原始输入的改前清洗）
    fresh_host = _mk_h3_host(sb_panel=False)
    fresh_req = _ReqH3(contexts=[dict(c) for c in turn2_raw])
    await _run_h3(fresh_host, _EvH3(), fresh_req)
    await _drain_spawned(fresh_host)
    assert req2.contexts == fresh_req.contexts
