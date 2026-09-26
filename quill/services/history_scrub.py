# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H3 历史 contexts 清洗的增量游标缓存（v5.3.0 M3.4，债务 D8）。

问题（PLAN §M3.4 / BASELINE D8）
--------------------------------
``on_llm_request``（H3）每轮对**全部**历史 contexts 做两遍正则清洗：

1. 步 2 注入报告行抹除（``plugin._scrub_inject_report``——报告行只该出现在
   用户看到的那条消息里，回显进上下文会被模型模仿）；
2. 步 3 状态栏关闭时的历史清洗（``plugin._strip_status_artifacts``——抹掉
   回灌上下文里已渲染的状态栏示范）。

历史 contexts 每轮只增长一两条（框架滑动窗口），旧消息的清洗结果**永远
不变**，却每轮全部重洗——100 轮长对话下钩子耗时随历史长度线性增长。

机制（本模块）
--------------
按会话（target_id）维护「已清洗游标」，把两遍清洗从 O(全部历史) 降为
O(新增消息)。核心是**按消息内容指纹的逐条记忆化**：

* 游标键 = 消息 content 的 SHA-256 摘要（``_digest``）。**刻意不用列表
  下标**：req.contexts 每轮都在变（框架滑动窗口截头、上下文恢复前插、
  换卡整体更换），下标既不稳定也不可移植；内容指纹对「同一条消息」
  天然稳定，对「内容变了的消息」自动失效（重洗即正确）。两个清洗函数
  都是纯文本变换、与 role/位置无关，故指纹只需覆盖 content。
* 每会话两份通道游标（``SessionScrubCursor.scrub`` / ``.strip``），与
  H3 的两遍清洗一一对应：**报告行抹除**跑在上下文恢复**之前**（恢复来的
  chat_logs 不经此通道——既有语义，勾结构见 H3，本模块不感知），
  **状态栏剥离**跑在恢复**之后**。两通道各持：
  - ``cache``：已清洗指纹集合 ``{digest(输入) -> 清洗输出}``——命中即
    直接复用输出，**不跑正则**；
  - ``last_seq``：最近一次清洗的指纹序列（按当轮 contexts 顺序）——
    用于把缓存 GC 到当前活跃窗口（每轮淘汰窗口外的条目，内存贴着
    实际会话规模走）。

为什么逐字节一致（验收红线）
----------------------------
增量路径与全量路径调的是**同一个清洗函数**（经 ``plugin._scrub_inject_report``
/ ``plugin._strip_status_artifacts`` 动态分发，与改前 ``self._*`` 同一
路径，无任何"增量版"正则）。两个函数都是纯函数：输出只由 (输入文本,
love_fields, 模块级正则常量) 决定。记忆化的正确性因此归约为一句话：
**指纹命中 ⇒ 输入文本与上次完全相同 ⇒ 同一纯函数给出同一输出**。缓存
返回值与"当场重洗"在字节上不可区分。具体保障：

1. 缓存键是输入全文的 SHA-256，不是近似指纹——内容差一个字节即 miss；
2. 清洗函数的全部外部输入都在世代指纹里（见下），任何一项变化 → 游标
   整体重置 → 当轮全量重洗（宁慢勿错）；
3. 未命中一律调用真函数并把结果入缓存；脏缓存（类型被外力改坏）重置；
4. 逐条替换语义与改前逐字一致：报告行通道永远重建 dict
   ``{**c, "content": ...}``；剥离通道仅在清洗后文本变化时才换新 dict、
   否则原 dict 原样透传（未清洗的消息返回原文，不是空串）。

世代号（游标失效条件）
----------------------
``_inputs_fingerprint`` 返回清洗函数全部外部输入的指纹元组，每次清洗前
与游标上记录的指纹比对，不一致即整体重置（当轮退化为全量重洗）：

* ``SCHEMA_GEN``——模块级整数，注入报告行格式或剥离正则集在**代码层面**
  变化时人工 +1（进程内不变；游标缓存本身也是进程内存活，此处是自述
  版本 + 人工失效杠杆）；
* ``plugin._INJECT_REPORT_LINE_RE.pattern``——报告行正则（main.py 类属性
  re-export，M2.0 约定）；
* ``_STRIP_PATTERNS`` 的 (pattern, replacement) 摘要——剥离正则全集
  （quill/services/statusbar/strip.py）；
* ``tuple(plugin.props.love_fields)``——状态栏字段表，**唯一会运行期
  变化的成分**（面板保存整体替换 plugin.config 后，props 访问器实时读
  到新表）。字段表变化使两个通道的游标同时失效——对报告行通道是
  有意的过度失效（该函数不读字段表），换 rare 事件的少量重洗换机制
  简单可靠。

内存护栏
--------
* 会话数：``MAX_SESSIONS``（LRU，``OrderedDict`` 每次访问移到队尾，
  超限淘汰最旧）——防 target_id 无界增长；
* 单通道缓存条目：``MAX_PASS_CACHE`` 上限 + 每轮 GC 到当前窗口
  （``_gc_pass_cache``）——防单会话无界增长。GC/淘汰只影响性能
  （被淘汰的消息下次重洗，结果不变），永不影响正确性。

并发与异常
----------
与 ``_inject_reports`` 等既有进程内缓存同一假设：事件循环单线程访问，
无锁。清洗函数抛出的异常**原样上抛**（与改前的列表推导一致，由 H3
注册桩顶层 try 降级）；此时 req.contexts 保持原值（赋值发生在函数
返回之后）、last_seq 不推进，已成功条目的缓存写入仍然有效（纯函数
的输出与全量重洗一致）。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from .statusbar.strip import _STRIP_PATTERNS

# 世代指纹的固定成分：报告行格式 / 剥离正则集在代码层面变化时 +1。
SCHEMA_GEN = 1

# 内存护栏：会话游标数上限（LRU 超限淘汰最旧）。
MAX_SESSIONS = 256
# 单会话单通道缓存条目上限（超出后新条目不再入缓存，下轮 GC 收敛）。
MAX_PASS_CACHE = 512


class _PassCursor:
    """单个清洗通道（一个纯函数）的增量游标。

    ``cache`` 即"已清洗 hash 集合"（指纹 → 清洗输出），``last_seq`` 即
    "最近一次清洗的指纹序列"——两者合起来构成该通道的游标状态。
    """

    __slots__ = ("gen", "cache", "last_seq")

    def __init__(self) -> None:
        # 构建缓存时的输入指纹（世代号）；None 表示"尚无有效世代"。
        self.gen = None
        # 已清洗指纹集合：digest(输入文本) -> 清洗输出。
        self.cache = {}
        # 最近一次清洗的指纹序列（按当轮 contexts 顺序，含重复）。
        self.last_seq = []


class SessionScrubCursor:
    """单个会话（target_id）的游标：H3 两遍清洗各一份通道游标。"""

    __slots__ = ("scrub", "strip")

    def __init__(self) -> None:
        # 步 2：注入报告行抹除（上下文恢复之前，作用于框架给的 contexts）。
        self.scrub = _PassCursor()
        # 步 3：状态栏关闭时的历史清洗（上下文恢复之后，含恢复来的
        # chat_logs——它们不经 scrub 通道，这正是两份游标分开的原因）。
        self.strip = _PassCursor()


class HistoryScrubCursors:
    """按会话（target_id）的游标存储，LRU 容量上限 ``MAX_SESSIONS``。

    挂在插件实例上（``plugin._history_scrub_cursors``，经
    ``_cursors_for`` 懒创建）而非模块级单例：与 ``_inject_reports`` 同一
    形态——实例隔离让测试宿主天然各持一份，进程内也只有 QuillPlugin
    一个实例。
    """

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: "OrderedDict[str, SessionScrubCursor]" = OrderedDict()

    def cursor_for(self, target_id: str) -> SessionScrubCursor:
        """取（或建）会话游标；LRU 访问记账 + 超限淘汰最旧。"""
        cur = self._sessions.get(target_id)
        if cur is None:
            while len(self._sessions) >= MAX_SESSIONS:
                self._sessions.popitem(last=False)
            cur = SessionScrubCursor()
            self._sessions[target_id] = cur
        else:
            self._sessions.move_to_end(target_id)
        return cur


def _cursors_for(plugin) -> HistoryScrubCursors:
    """插件实例上的游标存储（懒创建，形态同 main.py 的 _inject_reports）。"""
    store = getattr(plugin, "_history_scrub_cursors", None)
    if not isinstance(store, HistoryScrubCursors):
        store = HistoryScrubCursors()
        plugin._history_scrub_cursors = store
    return store


def _digest(text: str) -> str:
    """消息内容指纹（SHA-256 全长 hex）。

    surrogatepass：JSON 解析出的字符串理论上可含孤立代理项，编不过
    严格 UTF-8；指纹必须对**任何**输入可用（异常走不到缓存，退化全量
    重洗是允许的，但指纹阶段抛错会连累整轮清洗，不必要）。
    """
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _inputs_fingerprint(plugin) -> tuple:
    """清洗函数全部外部输入的指纹（世代号，失效条件详见模块 docstring）。

    两个通道共用一份指纹：报告行正则与剥离正则集是模块常量（进程内
    不变），love_fields 是唯一运行期可变成分——字段表变化时两通道同时
    全量重洗（对 scrub 通道是过度失效，宁慢勿错，见模块 docstring）。
    """
    scrub_re = getattr(plugin, "_INJECT_REPORT_LINE_RE", None)
    fields = getattr(getattr(plugin, "props", None), "love_fields", None)
    return (
        SCHEMA_GEN,
        getattr(scrub_re, "pattern", None),
        tuple((p.pattern, r) for p, r in _STRIP_PATTERNS),
        tuple(fields) if isinstance(fields, list) else None,
    )


def _reset_if_stale(cursor: _PassCursor, gen: tuple) -> None:
    """世代/一致性守卫：指纹变化或内部状态异常 → 整体重置（回退全量）。

    类型检查是防御性的：cache/last_seq 只会被本模块写入，被外力改坏
    （测试注入、未来重构失误）时宁可全量重洗，不可带着脏状态继续。
    """
    if (cursor.gen != gen
            or not isinstance(cursor.cache, dict)
            or not isinstance(cursor.last_seq, list)):
        cursor.gen = gen
        cursor.cache = {}
        cursor.last_seq = []


def _gc_pass_cache(cursor: _PassCursor, seq: list) -> None:
    """把缓存 GC 到当前活跃窗口（淘汰 seq 之外的指纹）。

    只在 seq 非空时执行：空 seq（contexts 全空、靠垫回撑起的轮次）不清
    缓存——上一窗口的条目大概率下轮还在窗口里，白洗。淘汰只影响性能
    （重新清洗结果不变），永不影响正确性。
    """
    if not seq:
        return
    keep = set(seq)
    if keep != cursor.cache.keys():
        cursor.cache = {k: v for k, v in cursor.cache.items() if k in keep}


def scrub_inject_report_history(plugin, target_id: str, contexts: list) -> list:
    """H3 步 2：历史 contexts 注入报告行抹除——增量版。

    逐字节等价的改前实现（列表推导，见 interfaces/astrbot_hooks.py
    handle_llm_request 本轮调用点注释）::

        req.contexts = [
            ({**c, "content": plugin._scrub_inject_report(c.get("content", ""))}
             if isinstance(c, dict) and isinstance(c.get("content"), str) else c)
            for c in req.contexts
        ]

    差异仅在 ``plugin._scrub_inject_report`` 的调用被游标缓存挡住：指纹
    命中直接复用上次输出（不跑正则），未命中调用真函数并记账。dict 重建
    语义（eligible 项一律 ``{**c, ...}``）与非 eligible 项原样透传逐字
    保持。返回新列表（调用方照旧整体赋回 req.contexts）。
    """
    cursor = _cursors_for(plugin).cursor_for(target_id).scrub
    gen = _inputs_fingerprint(plugin)
    _reset_if_stale(cursor, gen)
    cache = cursor.cache
    new_seq: list = []
    out: list = []
    for c in contexts:
        if isinstance(c, dict) and isinstance(c.get("content"), str):
            raw = c.get("content", "")
            key = _digest(raw)
            cleaned = cache.get(key)
            if cleaned is None:
                cleaned = plugin._scrub_inject_report(raw)
                if len(cache) < MAX_PASS_CACHE:
                    cache[key] = cleaned
            new_seq.append(key)
            out.append({**c, "content": cleaned})
        else:
            out.append(c)
    cursor.last_seq = new_seq
    _gc_pass_cache(cursor, new_seq)
    return out


def strip_status_history(plugin, target_id: str, contexts: list) -> list:
    """H3 步 3：状态栏关闭时的历史 contexts 整套剥离——增量版。

    逐字节等价的改前实现::

        for c in req.contexts:
            if isinstance(c, dict) and isinstance(c.get("content"), str):
                clean = plugin._strip_status_artifacts(
                    c["content"], plugin.props.love_fields
                )
                _scrubbed.append({**c, "content": clean} if clean != c["content"] else c)
            else:
                _scrubbed.append(c)

    游标语义同 ``scrub_inject_report_history``，另有两点通道特有语义
    逐字保持：清洗后文本**未变化**时原 dict 对象原样透传（不重建）；
    ``clean`` 可为空串——命中判定用 ``is None`` 而非真值测试，空串作为
    合法清洗结果同样入缓存、同样命中。
    """
    cursor = _cursors_for(plugin).cursor_for(target_id).strip
    gen = _inputs_fingerprint(plugin)
    _reset_if_stale(cursor, gen)
    cache = cursor.cache
    new_seq: list = []
    out: list = []
    for c in contexts:
        if isinstance(c, dict) and isinstance(c.get("content"), str):
            raw = c.get("content", "")
            key = _digest(raw)
            clean = cache.get(key)
            if clean is None:
                clean = plugin._strip_status_artifacts(
                    raw, plugin.props.love_fields
                )
                if len(cache) < MAX_PASS_CACHE:
                    cache[key] = clean
            new_seq.append(key)
            out.append({**c, "content": clean} if clean != raw else c)
        else:
            out.append(c)
    cursor.last_seq = new_seq
    _gc_pass_cache(cursor, new_seq)
    return out
