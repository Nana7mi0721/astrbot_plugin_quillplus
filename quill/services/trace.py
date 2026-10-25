# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ContextTrace — 本轮注入上下文的结构化诊断记录（M1a，R1 规范）。

定位
----
「这一轮到底注入了什么、为什么」此前只活在 ``_inject_reports`` 这个
「模块名 → 命中数」的扁平 dict 里，面板与 ``/quill debug`` 都只能看到
计数。本模块把它升级为带激活判定 / 段序 / 耗时 / 降级状态的完整结构，
供 ``/quill debug`` 文本报告与面板诊断页**同源**消费（同 trace_id
同结论——防「两套诊断各说各话」的硬验收项，规划 §四 M1a）。

设计约束（R1，全部是红线）
--------------------------
* **纯数据零副作用**：本模块不 import astrbot（分层禁令，AGENT.md §三），
  也不做任何 IO；采集由 interfaces/astrbot_hooks.py 在 22 步编排内
  **就地附带**（禁止第二条执行链路——重复执行 = 行为漂移之源）。
* **敏感数据不落盘**：TraceStore 纯内存 ring buffer，重启即清（诊断
  近因，可接受）。trace 结构**不含**原始 target_id / 用户原文 / 来源
  原文：会话标识只存 ``hash_target_id`` 的 SHA-256 前 16 位（不可逆），
  ``error`` / ``warnings`` 只记类别不记内容。后续若落盘必须带保留期
  配置与总量上限（M1a 不做）。
* **故障隔离在调用侧**：采集路径任何异常只许 warning 日志、不许影响
  注入主链路（局部守卫在 hooks 里，本模块自身不吞异常——数据结构
  抛错应当被发现而不是被埋）。

存储语义
--------
* 每会话上限 ``per_session_limit``（默认 20，热配置：``configure()``
  每轮由采集方回写当前配置值，写入时按新值裁剪——刻意用 list 而非
  ``deque(maxlen=...)``：maxlen 构造期固定，热改配置不生效）；
* 全局跨会话上限 ``global_limit``（默认 200）：超限逐出「最旧会话的
  最早轮」。各会话队列内天然按时间序，逐出时比较各队首的 ``seq``
  （单调递增计数器，比解析 ISO 时间串便宜且无时钟回拨问题）；
* ``inflight``：请求钩子 ``begin()`` 开始、响应钩子 ``commit()`` 落库
  之间的「进行中」trace。同一会话连续两次 ``begin()``（agent loop 每次
  迭代都会过一遍 on_llm_request）时，旧的按现状先落库——保留
  「请求发出但没等到响应收口」的诊断价值；落库前补算 ``duration_ms``
  并回填调用侧带来的 ``pending_statusbar_level``（M1a-Fix 1.2：H2 工具
  路径记下的级别此前在这一瞬间被丢弃，stale 轮级别恒为 None）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..core.logbridge import logger


def hash_target_id(target_id: str) -> str:
    """会话标识脱敏：SHA-256 前 16 位 hex（不可逆，作 trace 的会话键）。"""
    return hashlib.sha256(str(target_id).encode("utf-8")).hexdigest()[:16]


def funnel_filtered_items(funnel: Any) -> list:
    """M1b 模块 1：漏斗 filtered 列表 → TraceModuleResult.items 近失条。

    各检索模块的 funnel 出参（candidates/matched/filtered）里，只有
    ``filtered`` 需要进 items——留存条各模块有自己的命中明细口径（世界书
    走 trigger_log、WR 走条目名、记忆只记 id）。只取标识与 reason（条目名
    是作者起的标识符，非对话原文——与 trigger_log 的 title 同一脱敏口径），
    条数截 12 防长尾刷屏。hooks 与 _route_core（simulate）共用本函数，
    防两处各写一份漂移。

    M1b-Fix P1-f：各模块 filtered 行的标识键不统一（世界书 ``title``、
    文档 RAG ``source_ref``、记忆 ``id``/名次 ``rank``），旧实现只认
    title，记忆与 RAG 的近失全显示成 "?"。四级回退：title →
    source_ref → mem#id → mem#名次rank → "?"。
    """
    if not isinstance(funnel, dict):
        return []
    out: list = []
    for f in (funnel.get("filtered") or [])[:12]:
        if not isinstance(f, dict):
            continue
        ref = str(f.get("title") or "").strip() \
            or str(f.get("source_ref") or "").strip()
        if not ref:
            mid = str(f.get("id") or "").strip()
            ref = f"mem#{mid}" if mid else (
                f"mem#名次{f['rank']}" if f.get("rank") is not None else "?"
            )
        out.append({"source_ref": ref[:40],
                    "filtered_reason": str(f.get("reason", ""))})
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class TraceModuleResult:
    """单个注入模块在本轮的结果（M1a：items 明细允许只填 source_ref）。"""

    module: str                 # worldbook | wr | rag_doc | memory | core_memory
                                # | persona | context_restore | statusbar
    ok: bool = True             # 成功 / 降级（失败 = ok=False + error 类别）
    degraded: str | None = None # 降级级别（复用既有降级链命名，如状态栏
                                # 六级链级别名；RAG 检索降级类别）
    stage_ms: int = 0           # 该段耗时（毫秒级采样，不追求精确）
    candidates: int = 0         # 候选数
    hits: int = 0               # 最终命中数
    error: str | None = None    # 错误类别（不含敏感内容，如 "local_error"）
    items: list[dict[str, Any]] = field(default_factory=list)
                                # 每条命中 {source_ref(脱敏), score_or_kw,
                                # filtered_reason|None}；M1a 允许只填
                                # source_ref + hits 计数，明细留 M1b


@dataclass
class ContextTrace:
    """一轮 LLM 请求的完整注入诊断。"""

    trace_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=_now_iso)
    target_id_hash: str = ""    # hash_target_id(target_id)
    persona_id: str = ""
    activated: bool = False
    activation_reason: str | None = None   # 激活词 / 【】 / WR 关键词
    module_results: list[TraceModuleResult] = field(default_factory=list)
    injection_order: list[str] = field(default_factory=list)
                                # 实际注入段序（与 22 步段序映射）
    token_estimate: dict[str, int] = field(default_factory=dict)
                                # 各模块字符量→粗估 token（标注"估算"）
    truncation_events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    # 响应段补充（handle_llm_response 收尾时填）：
    statusbar_level: str | None = None     # 状态栏六级链本轮实际命中级别
    # 注入报告同源锚点：_remember_inject_report 镜像进来的标准统计
    # （wb/mem/core_mem/wr/doc/doc_sources），_get_inject_report 由此取数，
    # 保证 /quill debug、回复末尾报告行、面板三处同一份数据。
    inject_stats: dict[str, Any] = field(default_factory=dict)
    seq: int = 0                # 全局单调序号（ring buffer 逐出比较用）
    persona_name_at_turn: str = ""
                                # 本轮角色卡显示名（诊断展示用；改名后对照）
    # 请求侧 perf 计数起点（duration_ms = 收口时刻 - 它）。下划线前缀 =
    # 内部字段，to_dict 不外泄（面板/端点不感知）。
    _t0_perf: float = field(default_factory=time.perf_counter, repr=False)
    # M1b 模块 7：脱敏注入预览——最终 system prompt 的分段画像
    # （{name, chars, text?}；RAG 段只给长度不给正文——检索原文红线）。
    # 每会话只在**最新一条**保留（TraceStore 落库时清旧），内存与敏感面双控。
    prompt_preview: list[dict[str, Any]] = field(default_factory=list)

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0_perf) * 1000)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 内部字段（下划线前缀）不进对外序列化
        return {k: v for k, v in d.items() if not k.startswith("_")}


class TraceStore:
    """纯内存 ring buffer：每会话最近 N 轮 + 全局上限 200。"""

    def __init__(
        self,
        per_session_limit: int = 20,
        global_limit: int = 200,
    ) -> None:
        self.per_session_limit = max(1, int(per_session_limit))
        self.global_limit = max(1, int(global_limit))
        self._sessions: dict[str, list[ContextTrace]] = {}
        self._inflight: dict[str, ContextTrace] = {}
        self._seq = 0
        # M1b 模块 7：commit 监听者（SSE 实时视图）。同步回调（事件循环
        # 单线程内调用），异常逐个吞掉——订阅侧故障绝不影响落库。
        self._listeners: list = []

    # ── 配置（每轮回写，热生效）──────────────────────────────
    def configure(self, per_session_limit: int, global_limit: int = 200) -> None:
        old_per = self.per_session_limit
        self.per_session_limit = max(1, int(per_session_limit))
        self.global_limit = max(1, int(global_limit))
        if self.per_session_limit != old_per:
            for queue in self._sessions.values():
                del queue[: max(0, len(queue) - self.per_session_limit)]
        self._trim_global()

    # ── 生命周期 ─────────────────────────────────────────────
    def begin(self, target_id_hash: str, persona_id: str = "",
              persona_name: str = "",
              pending_statusbar_level: str | None = None) -> ContextTrace:
        """开一条进行中 trace。同会话已有 inflight 时先按现状落库。

        ``pending_statusbar_level``：调用侧在 begin 前从状态栏级别瞬态
        通道取出的残留值（Agent Loop 二次迭代场景里 H2 刚记下的级别）。
        stale 轮不经 H4 的 finalize 取值，不回填就以 duration_ms=0、
        statusbar_level=None 落库（M1a-Fix 1.2）。就地回填（parsers 侧
        ``_note_status_level``）已覆盖常规路径，这里是兜底通道。
        """
        stale = self._inflight.pop(target_id_hash, None)
        if stale is not None:
            stale.duration_ms = stale.elapsed_ms()
            if stale.statusbar_level is None and pending_statusbar_level:
                stale.statusbar_level = pending_statusbar_level
            stale.warnings.append("上一轮未等到响应收口（先落库）")
            self._append_committed(stale)
        trace = ContextTrace(
            target_id_hash=target_id_hash,
            persona_id=persona_id,
            persona_name_at_turn=persona_name,
        )
        self._inflight[target_id_hash] = trace
        return trace

    def commit(self, trace: ContextTrace) -> None:
        """响应钩子收口：落库（未知/已换代的 inflight 直接忽略）。"""
        cur = self._inflight.get(trace.target_id_hash)
        if cur is not trace:
            return
        self._inflight.pop(trace.target_id_hash, None)
        self._append_committed(trace)

    def discard(self, trace: ContextTrace) -> None:
        """采集方判定不落库（如 trace.enabled=false 轮）时丢弃 inflight。"""
        if self._inflight.get(trace.target_id_hash) is trace:
            self._inflight.pop(trace.target_id_hash, None)

    # ── 查询 ─────────────────────────────────────────────────
    def inflight(self, target_id_hash: str) -> ContextTrace | None:
        return self._inflight.get(target_id_hash)

    def latest(self, target_id_hash: str) -> ContextTrace | None:
        queue = self._sessions.get(target_id_hash)
        return queue[-1] if queue else self._inflight.get(target_id_hash)

    def latest_for_report(self, target_id_hash: str) -> ContextTrace | None:
        """注入报告取数专用（M1a-Fix 1.1）。

        ``latest()`` 的 queue 优先有两个坑：H2（工具调用路径）阶段本轮
        trace 仍在 ``_inflight``（H4 才 commit），第 2 轮起 queue 非空时
        报告行读到的是**上一轮**的 inject_stats；未激活轮落库后
        queue[-1].inject_stats 为 {}，/quill debug 的注入行与 Trace 行
        跨轮错位。这里按「报告可用性」取数：

        1. inflight 已有 inject_stats（本轮过了步 18.5）→ 本轮优先；
        2. 否则取最近一条激活轮（未激活轮 stats 为空，跳过）；
        3. 全为未激活轮 → queue[-1]（调用侧据此渲染「本轮未激活」）。
        """
        inflight = self._inflight.get(target_id_hash)
        if inflight is not None and inflight.inject_stats:
            return inflight
        queue = self._sessions.get(target_id_hash) or []
        for trace in reversed(queue):
            if trace.activated or trace.inject_stats:
                return trace
        return queue[-1] if queue else inflight

    def recent(self, target_id_hash: str, limit: int = 20) -> list[ContextTrace]:
        queue = self._sessions.get(target_id_hash) or []
        if limit <= 0:
            return list(queue)
        return list(queue[-limit:])

    def get(self, trace_id: str) -> ContextTrace | None:
        for queue in self._sessions.values():
            for trace in reversed(queue):
                if trace.trace_id == trace_id:
                    return trace
        for trace in self._inflight.values():
            if trace.trace_id == trace_id:
                return trace
        return None

    def sessions_summary(self) -> list[dict[str, Any]]:
        """面板会话下拉：有 trace 的会话（hash 键 + 摘要，不含原始 id）。"""
        out: list[dict[str, Any]] = []
        for key, queue in self._sessions.items():
            if not queue:
                continue
            last = queue[-1]
            out.append({
                "target_id_hash": key,
                "count": len(queue),
                "persona_id": last.persona_id,
                "persona_name": last.persona_name_at_turn,
                "latest_started_at": last.started_at,
            })
        out.sort(key=lambda s: s["latest_started_at"], reverse=True)
        return out

    def __len__(self) -> int:
        return sum(len(q) for q in self._sessions.values())

    # ── 内部 ─────────────────────────────────────────────────
    def add_listener(self, fn) -> None:
        """注册 commit 监听者（M1b 模块 7，SSE 用）。签名 ``fn(trace_dict)``。"""
        if callable(fn) and fn not in self._listeners:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, trace: "ContextTrace") -> None:
        if not self._listeners:
            return
        payload = trace.to_dict()
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception as exc:  # noqa: BLE001 - 订阅侧故障不波及落库/主链路，但留痕不裸吞
                logger.warning("[Quill Trace] trace 监听者回调失败（已忽略）: %s", exc)

    def _append_committed(self, trace: ContextTrace) -> None:
        self._seq += 1
        trace.seq = self._seq
        queue = self._sessions.setdefault(trace.target_id_hash, [])
        # M1b 模块 7：注入预览只保留会话最新一条（新 commit 进队前清掉
        # 旧轮的预览文本——内存占用与敏感面双控）。
        for older in queue:
            older.prompt_preview = []
        queue.append(trace)
        if len(queue) > self.per_session_limit:
            del queue[: len(queue) - self.per_session_limit]
        self._trim_global()
        self._notify(trace)

    def _trim_global(self) -> None:
        total = sum(len(q) for q in self._sessions.values())
        while total > self.global_limit and self._sessions:
            # 全局最旧 = 各会话队首中 seq 最小者（队内按序，只需比队首）
            oldest_key = min(
                self._sessions,
                key=lambda k: self._sessions[k][0].seq if self._sessions[k] else 1 << 62,
            )
            queue = self._sessions[oldest_key]
            queue.pop(0)
            if not queue:
                self._sessions.pop(oldest_key, None)
            total -= 1
