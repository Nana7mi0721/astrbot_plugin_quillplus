# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prompt Builder — structured prompt assembly engine with dataclass sections.
===========================================================================

Builds the 4-layer system prompt for Quill's send-reply mode.
Uses PromptSection dataclasses with priority-based truncation instead of
v5.0's fragile ``\\n---\\n\\n`` string-splitting approach.

Layers:
  0 — Narrative integrity protocol + send_message_to_user usage guide (priority=0)
  1 — Constant WR entries + worldbook entries + random pool sampling (priority=3)
  2 — Keyword-matched entries, with match-count fallback (priority=10)
  3 — Safety wrapper emphasizing tool usage (priority=1)

All wr_manager methods are async; retrieve_content_layers is async too.
wb_manager methods are synchronous.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件做自测：先把 AstrBot 加入可导入路径
    # 包内加载走相对导入；`python <file>` 时无父包，退回顶层导入
    try:
        from ._astrbot_bootstrap import ensure_astrbot_importable
    except ImportError:
        from _astrbot_bootstrap import ensure_astrbot_importable
    ensure_astrbot_importable()
    from astrbot.api import logger


# ============================================================
# Dataclass
# ============================================================


# 字段表/剧情选项的兜底默认值。与 config.py 的默认一致，但这里必须自带一份：
# PromptBuilder 要能被单独构造（自检、单测），不能依赖 QuillConfig 在场。
_DEFAULT_FIELDS = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法"]
_DEFAULT_PLOT_PATHS = ["继续当前话题", "转换场景", "结束互动"]

# 单个字段在示例里给的样值。契约文本的示例行按字段名查这张表，
# 查不到就用通用占位（用户自定义字段如「催眠度」走这条）。
_FIELD_SAMPLES = {
    "好感度": "85/100（心动到不行）",
    "关系阶段": "暧昧期",
    "心情": "害羞（刚才牵到手了）",
    "位置": "放学路上",
    "穿着": "便服，围巾",
    "当前想法": "他手好大...好暖和...",
    "服从度": "70/100（开始顺从）",
    "发情度": "40/100（强忍着）",
}

# 字段名 → 一行说明。仅用于 guide 的「字段说明」段；未知字段用通用说明。
_FIELD_DESCRIPTIONS = {
    "好感度": "N/100，后附括号文字说明（如 68/100（有好感但极力否认））",
    "关系阶段": "当前关系状态标签（如 陌生人、熟悉、暧昧、亲密、支配等）",
    "心情": "具体情绪，可附括号补充原因（如 慌乱（差点说漏嘴））",
    "位置": "当前地点",
    "穿着": "当前服装",
    "当前想法": "内心独白",
    "服从度": "N/100，配合度量化（可附括号说明）",
    "发情度": "N/100，情动程度量化（可附括号说明）",
}


@dataclass
class PromptSection:
    """A named, prioritised chunk of prompt text.

    Priority semantics:
        0 = critical, never truncated
        1 = important, truncated last
        …
        10 = expendable, truncated first
    """

    name: str          # e.g. "narrative_integrity_protocol"
    content: str       # the actual text
    priority: int      # 0 = never truncate, 10 = first to drop


# ============================================================
# Builder
# ============================================================


class PromptBuilder:
    """Structured prompt assembly engine.

    Usage::

        builder = PromptBuilder(config)
        prompt = await builder.build_system_prompt(wr_manager, wb_manager, extra_info)
        merged = builder.inject_prompt(original_prompt, prompt)
    """

    def __init__(self, config):
        """config 可以是 dict（旧兼容）或 QuillConfig 对象（v5.0）。"""
        # 兼容 QuillConfig 对象
        if hasattr(config, 'max_prompt_length'):
            self.max_prompt_length: int = config.max_prompt_length
            self.min_output_length: int = config.min_output_length
            self.max_output_length: int = getattr(config, 'max_output_length', 0)
            self.status_bar_enabled: bool = config.status_bar_enabled
            # 字段表与剧情选项必须拿进来：契约文本由它们生成（见 build_status_contract）。
            # 此前这两个值只在插件实例上，本类只能把字段顺序写死在示例里，
            # 导致用户改了字段名/顺序后，示例与实际契约不一致。
            self.love_fields: list = list(
                getattr(config, 'status_bar_fields', None) or _DEFAULT_FIELDS
            )
            self.status_bar_plot_paths: list = list(
                getattr(config, 'status_bar_plot_paths', None) or list(_DEFAULT_PLOT_PATHS)
            )
        else:
            perf = (config or {}).get("performance", {})
            self.max_prompt_length: int = perf.get("max_prompt_length", 50000)
            self.min_output_length: int = perf.get("min_output_length", 400)
            self.max_output_length: int = perf.get("max_output_length", 0)
            sb_cfg = (config or {}).get("status_bar", {})
            self.status_bar_enabled: bool = sb_cfg.get("enabled", False)
            _raw_fields = sb_cfg.get("fields") or ""
            if isinstance(_raw_fields, str) and _raw_fields.strip():
                self.love_fields = [f.strip() for f in _raw_fields.split("|") if f.strip()]
            elif isinstance(_raw_fields, (list, tuple)):
                self.love_fields = [str(f).strip() for f in _raw_fields if str(f).strip()]
            else:
                self.love_fields = list(_DEFAULT_FIELDS)
            _raw_plot = sb_cfg.get("plot_paths") or ""
            if isinstance(_raw_plot, str) and _raw_plot.strip():
                self.status_bar_plot_paths = [p.strip() for p in _raw_plot.split("|") if p.strip()]
            elif isinstance(_raw_plot, (list, tuple)):
                self.status_bar_plot_paths = [str(p).strip() for p in _raw_plot if str(p).strip()]
            else:
                self.status_bar_plot_paths = list(_DEFAULT_PLOT_PATHS)

    def _estimate_tokens(self, text: str) -> float:
        """更精确的 Token 估算：CJK 字符 ≈ 1 token，ASCII ≈ 0.25 token。"""
        if not text:
            return 0.0
        cjk_count = 0
        ascii_count = 0
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u30ff' or '\uff00' <= ch <= '\uffef':
                cjk_count += 1
            else:
                ascii_count += 1
        return cjk_count + ascii_count * 0.25

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    async def build_system_prompt(
        self,
        wr_manager: Any,
        wb_manager: Any,
        extra_info: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> tuple:
        """Assemble the full system prompt from four layers.

        Returns (stable_prompt, dynamic_prompt) tuple.
        stable_prompt contains fixed content (Layer 0 + Layer 1 constants).
        dynamic_prompt contains per-turn content (Layer 1 random + Layer 2 + Layer 3).

        `stats` 为可选的出参 dict，用于回填本轮各来源命中条数（注入报告）。
        默认 None 时行为与改动前完全一致，既有调用方无需调整。
        """
        extra_info = extra_info or {}
        stable_sections: List[PromptSection] = []
        dynamic_sections: List[PromptSection] = []

        # --- Layer 0: core protocol (stable) ---
        stable_sections.append(PromptSection(
            name="narrative_integrity_protocol",
            content=self.build_narrative_integrity_protocol(),
            priority=0,
        ))
        stable_sections.append(PromptSection(
            name="send_message_guide",
            content=self.build_send_message_guide(),
            priority=0,
        ))

        # --- Status bar guide (stable, priority=0) ---
        if self.status_bar_enabled:
            stable_sections.append(PromptSection(
                name="status_bar_guide",
                content=self.build_status_bar_guide(),
                priority=0,
            ))

        # --- Session state injection (stable, priority=0) ---
        # 受状态栏开关门控：关闭状态下 tail message 明令禁止输出好感度/关系阶段等
        # 字段（main.py 的 status_bar_enabled=False 分支），若此处仍把字段名注入
        # system prompt，就是一边禁止一边示范，模型照抄后又被剥离器擦掉，白耗
        # token 且与「关闭即干净」的预期相悖。
        # 注意只停止注入、不清空 session_vars：重新打开时旧状态还在，是连续性体验。
        session_vars = (extra_info or {}).get("session_vars") or {}
        if session_vars and self.status_bar_enabled:
            state_lines = [f"{k}={v}" for k, v in session_vars.items()]
            stable_sections.append(PromptSection(
                name="session_state",
                content="## 当前状态\n" + " | ".join(state_lines),
                priority=0,
            ))

        # --- Persona card injection (stable, priority=0) ---
        persona_data = extra_info.get("persona_data")
        if persona_data:
            parts = []
            p_name = persona_data.get("name", persona_data.get("id", ""))
            cp = persona_data.get("core_prompts", {})
            personality = (cp.get("personality") or "").strip()
            scenario = (cp.get("scenario") or "").strip()
            examples = (cp.get("examples_of_dialogue") or "").strip()
            if personality:
                parts.append(f"### 人格/外貌\n{personality}")
            if scenario:
                parts.append(f"### 世界观/场景\n{scenario}")
            if examples:
                parts.append(f"### 对话范例\n{examples}")
            if parts:
                card_text = f"## [角色卡] {p_name}\n\n" + "\n\n".join(parts)
                stable_sections.append(PromptSection(
                    name="persona_card",
                    content=card_text,
                    priority=0,
                ))

        # --- Layer 1 + Layer 2: content retrieval ---
        layer1_parts, layer2_parts, layer1_random_parts = await self.retrieve_content_layers(
            wr_manager, wb_manager, extra_info, stats=stats
        )

        # Layer 1 fixed part \u2192 stable
        if layer1_parts:
            stable_sections.append(PromptSection(
                name="layer1_writing_framework",
                content="## [\u7d20\u6750] \u521b\u4f5c\u7d20\u6750\u5e93\uff08\u8bf7\u628a\u8fd9\u4e9b\u5143\u7d20\u81ea\u7136\u878d\u5165\u5f53\u524d\u573a\u666f\uff0c\u4e0d\u8981\u673a\u68b0\u7f57\u5217\uff09\n\n"
                        + "\n\n".join(layer1_parts),
                priority=3,
            ))

        # Layer 1 random pool \u2192 dynamic (changes each turn)
        if layer1_random_parts:
            dynamic_sections.append(PromptSection(
                name="layer1_random_pool",
                content="\n\n".join(layer1_random_parts),
                priority=3,
            ))

        # Layer 2 \u2192 dynamic (changes each turn)
        if layer2_parts:
            dynamic_sections.append(PromptSection(
                name="layer2_scene_reference",
                content="## [\u89e6\u53d1] \u5173\u952e\u8bcd\u89e6\u53d1\u53c2\u8003\uff08\u4e0e\u5f53\u524d\u7528\u6237\u8f93\u5165\u5f3a\u76f8\u5173\uff09\n\n"
                        + "\n\n".join(layer2_parts),
                priority=10,
            ))

        # --- Layer 3: safety wrapper (stable content but after dynamic) ---
        dynamic_sections.append(PromptSection(
            name="safety_wrapper",
            content=self.build_safety_wrapper(),
            priority=1,
        ))

        # Assemble
        stable_prompt = "\n\n".join(s.content for s in stable_sections if s.content)

        # Truncate only dynamic sections
        # 注意：max_prompt_length 的配置语义是"字符"（见 _conf_schema.json performance 组），
        # 与 _smart_truncate 内部的 len() 度量一致，勿误改为 token 估算造成单位错配。
        remaining_budget = self.max_prompt_length - len(stable_prompt)
        if remaining_budget < 1000:
            remaining_budget = 1000

        # M1a trace：把「实际组出的段序」写进出参 stats（保留键，下划线前缀
        # 与注入报告的计数键区分；_format_inject_report 只读计数键不受影响）。
        # dynamic 侧截断发生在 _smart_truncate 内部（可能丢段），经 kept_names
        # 出参取**最终保留**的段名；stable 侧无截断，直接取非空段名。
        # RAG 追加段与 tail 由 hooks 侧拼进 injection_order（步 17/20 之后）。
        # stats=None 的默认路径与改动前逐字一致（kept_names=None 零开销）。
        # M1b 模块 3：截断事件（丢段/硬切）同样经 trunc_events 出参进
        # stats["_truncation_events"]——下划线键与 _sections_* 同一先例，
        # 注入报告/面板的计数键读取不受影响。
        kept_dyn: List[str] = []
        trunc_ev: List[dict] = []
        dynamic_prompt = self._smart_truncate(
            dynamic_sections, remaining_budget,
            kept_names=kept_dyn, trunc_events=trunc_ev,
        )
        if stats is not None:
            try:
                stats["_sections_stable"] = [s.name for s in stable_sections if s.content]
                stats["_sections_dynamic"] = kept_dyn
                # where 标注统一在调用侧打（_smart_truncate 是通用静态方法，
                # 不知道自己服务哪段装配）；guard 侧事件带 post_rag_guard。
                for _ev in trunc_ev:
                    _ev.setdefault("where", "build_system_prompt")
                stats["_truncation_events"] = trunc_ev
                # M1b 模块 7：分段文本画像（脱敏预览数据源）。stable 全量 +
                # dynamic 按 kept_dyn 存活集；char_slice 轮次里末段实际被切，
                # 预览侧以 _truncation_events 为准（预览文本是切前的原文，
                # 面板已标注该差异）。
                stats["_section_texts"] = (
                    [(s.name, s.content) for s in stable_sections if s.content]
                    + [(s.name, s.content) for s in dynamic_sections
                       if s.content and s.name in set(kept_dyn)]
                )
            except Exception as exc:  # noqa: BLE001 - 出参写入失败不影响 prompt 装配
                logger.warning("[PromptBuilder] trace 统计出参写入失败（已忽略）: %s", exc)

        return stable_prompt, dynamic_prompt

    def inject_prompt(self, original_prompt: str, stable_prompt: str, dynamic_prompt: str = "",
                      injection_position: str = "system_end") -> str:
        """Merge the assembled prompt with AstrBot's original system prompt.

        Args:
            original_prompt: AstrBot 原始 system prompt（含角色卡）
            stable_prompt: 协议层（Layer 0 + 常驻内容）
            dynamic_prompt: 动态内容（Layer 2 关键词匹配）
            injection_position: 注入位置。
                "system_end" → stable → original → dynamic（默认，dynamic 在末尾）
                "user_prefix" → dynamic → stable → original（dynamic 最前，更高服从度）

        Order (system_end): stable (protocol) → original (persona card) → dynamic.
        Quill 协议放在最前面确保 AI 优先看到"发送协议"和"禁止跳场景"，
        角色卡放在中间保持角色定义，动态内容在最后不影响前缀缓存。
        """
        stable_prompt = stable_prompt or ""
        original_prompt = original_prompt or ""
        dynamic_prompt = dynamic_prompt or ""

        if injection_position == "user_prefix":
            # 动态内容最前（高服从度模式）
            parts = [p for p in [dynamic_prompt, stable_prompt, original_prompt] if p]
        else:
            # 默认：协议 → 角色卡 → 动态内容
            parts = [p for p in [stable_prompt, original_prompt, dynamic_prompt] if p]

        return "\n\n".join(parts) if parts else ""

    # ----------------------------------------------------------
    # Content retrieval (async — calls wr_manager async methods)
    # ----------------------------------------------------------

    async def retrieve_content_layers(
        self,
        wr_manager: Any,
        wb_manager: Any,
        extra_info: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> tuple:
        """Fetch Layer 1 (constant) and Layer 2 (keyword-matched) content.

        Returns ``(layer1_parts, layer2_parts, layer1_random_parts)``.
        layer1_parts: fixed constant entries (stable across turns).
        layer1_random_parts: random pool samples (changes each turn).
        layer2_parts: keyword-matched entries (changes each turn).

        `stats` 为可选的出参 dict：填入本轮各来源的命中条数（供注入报告使用）。
        没有它也能正常工作——出参形态而非返回值形态，是为了不改动既有三元组
        契约（`_self_test` 等多处按位置解包）。
        """
        extra_info = extra_info or {}
        layer1_parts: List[str] = []
        layer1_random_parts: List[str] = []
        layer2_parts: List[str] = []
        user_input = extra_info.get("user_input", "")
        seen_ids: set = set()
        # 命中计数（仅用于注入报告）
        wr_constant_hits = 0
        wr_match_hits = 0
        wr_fallback_hits = 0
        wb_constant_hits = 0
        wb_match_hits = 0

        # === 提取角色卡扩展配置（三态模式）===
        ext = {}
        if extra_info.get("persona_data"):
            ext = extra_info["persona_data"].get("quill_extensions", {})
        wr_mode = ext.get("wr_mode", "disabled")
        wb_mode = ext.get("wb_mode", "disabled")
        bound_wrs = ext.get("bound_writing_resource", []) if wr_mode == "custom" else None
        bound_worldbooks = ext.get("bound_worldbooks", []) if wb_mode == "custom" else None

        # === Layer 1: constants + random pool (skip on consecutive turns) ===
        skip_constants = extra_info.get("skip_constants", False)
        if not skip_constants and wr_manager and wr_mode != "disabled":
            try:
                constant_entries = await wr_manager.get_constant_entries()
                # 过滤被绑定的分类（is not None 确保空列表也能正确过滤）
                if bound_wrs is not None:
                    constant_entries = [e for e in constant_entries if e.get("category") in bound_wrs]
                pools: Dict[str, List[str]] = {
                    "random_sensory": [],
                    "random_fluid": [],
                    "random_bodytype": [],
                    "random_clothing": [],
                    "random_liveliness": [],
                }
                for entry in constant_entries:
                    eid = entry.get("entry_id", "")
                    cat = entry.get("category", "")
                    content = entry.get("content", "")
                    if not content:
                        continue
                    seen_ids.add(eid)
                    if cat in pools:
                        pools[cat].append(content)
                    else:
                        layer1_parts.append(
                            "\u3010\u7d20\u6750\u3011\n" + content
                        )
                        wr_constant_hits += 1
                # Random pool \u2192 separate list
                for pool_entries in pools.values():
                    if pool_entries:
                        for c in random.sample(pool_entries, min(2, len(pool_entries))):
                            layer1_random_parts.append(
                                "\u3010\u7d20\u6750\u3011\n" + c
                            )
                            wr_constant_hits += 1
            except Exception as exc:
                logger.warning("[PromptBuilder] WR constant entries failed: %s", exc)

        # M12 修复：Token 预算提到 Layer 1 之前求值，让**常驻条目**与
        # 关键词命中条目共用同一个累加器。旧写法把 max_token 只用在下方
        # Layer 2 的 wb_matched 循环里，于是 `worldbook.max_token_limit`
        # 对「常驻世界书」完全失效——用户把上限调到 800，十几条常驻条目
        # 依旧全量拼进 system prompt，预算形同虚设。常驻优先消费预算
        # （它们是世界观底座），剩多少给关键词命中。
        wb_max_token = extra_info.get("wb_max_token", 0)
        wb_accumulated_tokens = 0.0
        # M1b 模块 1：世界书三阶漏斗出参（常驻预算近失在 Layer 1 记、
        # 灵敏度/条件/截帽/预算近失在 Layer 2 记；stats 缺省时零采集）。
        _wb_funnel: Dict[str, Any] = {"candidates": 0, "matched": 0, "filtered": []}
        # P1-d：常驻侧近失（条件不达成 / 预算截断）记到独立 dict——Layer 2 的
        # match_entries 会对 _wb_funnel["filtered"] **整表赋值**，直接共用
        # 会被清掉；Layer 2 之后按时序（常驻先评估）合并回去。
        _wb_const_funnel: Dict[str, Any] = {"filtered": []}
        # WR 侧漏斗：candidates=检索返回数，filtered 记绑定分类过滤近失；
        # 保底注入触发时标 fallback_pool=True（保底不是关键词命中）。
        _wr_funnel: Dict[str, Any] = {"candidates": 0, "matched": 0,
                                      "fallback_pool": False, "filtered": []}

        if not skip_constants and wb_manager and wb_mode != "disabled":
            try:
                wb_constants = await asyncio.to_thread(
                    wb_manager.get_constant_entries,
                    bound_worldbooks=bound_worldbooks,
                    session_vars=extra_info.get("session_vars") or None,
                    funnel=_wb_const_funnel,
                )
                for _c_idx, entry in enumerate(wb_constants):
                    content = entry.get("content", "")
                    if content:
                        # 与 Layer 2 同一判据：预算用尽即停，不再注入后续条目
                        token_count = self._estimate_tokens(content)
                        if wb_max_token > 0 and wb_accumulated_tokens + token_count > wb_max_token:
                            # 日志文案是 E2E 契约（ab_worldbook_max_token_limit
                            # grep 旧串），M1b 只在漏斗 reason 里带「常驻」语境
                            logger.info("[PromptBuilder] WB Token 上限已达 (%.0f/%d)，截断后续常驻条目",
                                        wb_accumulated_tokens, wb_max_token)
                            # M1b 模块 1 漏斗：预算截断的常驻条目记近失
                            # （此后全部条目都因同一预算被截）。挂
                            # _wb_const_funnel——挂 _wb_funnel 会被 Layer 2
                            # match_entries 的整表赋值清掉。
                            for dropped in wb_constants[_c_idx:]:
                                if dropped.get("content"):
                                    _wb_const_funnel["filtered"].append({
                                        "title": dropped.get("title", ""),
                                        "reason": f"token_budget_cap(常驻{int(wb_accumulated_tokens)}/{wb_max_token})",
                                    })
                            break
                        wb_accumulated_tokens += token_count
                        layer1_parts.append(
                            "\u3010\u4e16\u754c\u89c2\u3011\n" + content
                        )
                        wb_constant_hits += 1
            except Exception as exc:
                logger.warning("[PromptBuilder] WB constant entries failed: %s", exc)

        # === Layer 2: keyword matching (uses multi-turn context for richer matching) ===
        matching_text = extra_info.get("context_text") or user_input
        if matching_text and wr_manager and wr_mode != "disabled":
            try:
                max_entries = extra_info.get("wr_max_entries", 5)
                # Custom 模式取多一点候选池，防止过滤后不够
                fetch_count = max_entries * 2 if bound_wrs is not None else max_entries
                # M1b 模块 1：funnel 交给 kb.match 回填 candidates/filtered
                # （category_dedup/max_entries_cap——那两处裁剪只在 kb 内部可见）；
                # 不支持该出参的 duck-typing 管理器经 **kwargs 吞掉，保持 0 值。
                matched = await wr_manager.match(matching_text, top_k=fetch_count,
                                                 funnel=_wr_funnel)

                # 过滤被绑定的分类（is not None 确保空列表也能正确过滤）
                if bound_wrs is not None:
                    for e in matched:
                        if e.get("category") not in bound_wrs:
                            _wr_funnel["filtered"].append({
                                "title": e.get("name", ""),
                                "reason": f"category_unbound({e.get('category', '?')})",
                            })
                    matched = [e for e in matched if e.get("category") in bound_wrs]

                match_count = 0
                for entry in matched:
                    eid = entry.get("entry_id", "")
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        content = entry.get("content", "")
                        if content:
                            layer2_parts.append(
                                "\u3010\u7d20\u6750\u3011\n" + content
                            )
                            match_count += 1
                wr_match_hits += match_count
                _wr_funnel["matched"] = match_count
                if match_count == 0:
                    # 保底注入（高频素材兜底）不是关键词命中——漏斗如实标注，
                    # 面板可区分「命中 N 条」与「兜底塞了 N 条」。
                    _wr_funnel["fallback_pool"] = True
                    try:
                        fallback_limit = extra_info.get("wr_fallback_top_count", 2)
                        top_entries = await wr_manager.get_top_entries_by_match_count(limit=fallback_limit)
                        # 过滤被绑定的分类（is not None 确保空列表也能正确过滤）
                        if bound_wrs is not None:
                            top_entries = [e for e in top_entries if e.get("category") in bound_wrs]
                        for entry in top_entries:
                            eid = entry.get("entry_id", "")
                            if eid not in seen_ids:
                                seen_ids.add(eid)
                                content = entry.get("content", "")
                                if content:
                                    layer2_parts.append(
                                        "\u3010\u7d20\u6750\u3011\n" + content
                                    )
                                    wr_fallback_hits += 1
                    except Exception as exc:
                        logger.warning("[PromptBuilder] WR top-entries fallback failed: %s", exc)
            except Exception as exc:
                logger.warning("[PromptBuilder] WR keyword match failed: %s", exc)

        if matching_text and wb_manager and wb_mode != "disabled":
            try:
                wb_top_k = extra_info.get("wb_max_entries", 4)
                sensitivity = extra_info.get("wb_sensitivity", 0.7)
                wb_matched = await asyncio.to_thread(
                    wb_manager.match_entries,
                    matching_text, bound_worldbooks=bound_worldbooks,
                    top_k=wb_top_k, sensitivity=sensitivity,
                    session_vars=extra_info.get("session_vars") or None,
                    funnel=_wb_funnel,
                )
                # Token 注入上限（配置注入）——沿用 Layer 1 的累加器，
                # 常驻条目已消费的部分在此继续扣减（M12 修复）。
                for _m_idx, entry in enumerate(wb_matched):
                    content = entry.get("content", "")
                    if content:
                        # Token 估算：CJK ≈ 1 token/字，ASCII ≈ 0.25 token/字
                        token_count = self._estimate_tokens(content)
                        if wb_max_token > 0 and wb_accumulated_tokens + token_count > wb_max_token:
                            logger.info("[PromptBuilder] WB Token \u4e0a\u9650\u5df2\u8fbe (%.0f/%d)\uff0c\u622a\u65ad\u540e\u7eed\u6761\u76ee",
                                        wb_accumulated_tokens, wb_max_token)
                            # M1b 模块 1 漏斗：预算截断的命中条目记近失
                            for dropped in wb_matched[_m_idx:]:
                                if dropped.get("content"):
                                    _wb_funnel["filtered"].append({
                                        "title": dropped.get("title", ""),
                                        "reason": f"token_budget_cap({int(wb_accumulated_tokens)}/{wb_max_token})",
                                    })
                            break
                        wb_accumulated_tokens += token_count
                        layer2_parts.append(
                            "\u3010\u4e16\u754c\u89c2\u3011\n" + content
                        )
                        wb_match_hits += 1
            except Exception as exc:
                logger.warning("[PromptBuilder] WB keyword match failed: %s", exc)

        # P1-d：常驻侧近失（条件不达成/预算截断）按时序合并（常驻先于关键词
        # 评估，进同一漏斗）
        if _wb_const_funnel["filtered"]:
            _wb_funnel["filtered"] = (
                _wb_const_funnel["filtered"] + _wb_funnel.get("filtered", []))

        if stats is not None:
            stats["wr"] = wr_constant_hits + wr_match_hits + wr_fallback_hits
            stats["wr_constant"] = wr_constant_hits
            stats["wr_match"] = wr_match_hits
            stats["wr_fallback"] = wr_fallback_hits
            stats["wb"] = wb_constant_hits + wb_match_hits
            stats["wb_constant"] = wb_constant_hits
            stats["wb_match"] = wb_match_hits
            # M1b 模块 1：漏斗诊断进 stats（下划线键与 _sections_* 同一先例，
            # 注入报告只读上面的计数键不受影响）。
            stats["_funnel_wb"] = _wb_funnel
            stats["_funnel_wr"] = _wr_funnel

        return layer1_parts, layer2_parts, layer1_random_parts

    # ----------------------------------------------------------
    # Smart truncation (priority-based, no string splitting)
    # ----------------------------------------------------------

    @staticmethod
    def _smart_truncate(
        sections: List[PromptSection], max_length: int,
        kept_names: Optional[List[str]] = None,
        trunc_events: Optional[List[dict]] = None,
    ) -> str:
        """Assemble sections into a single string within *max_length*.

        Priority 0 sections are **always** kept.
        Higher-priority sections are dropped first when space is tight.
        Within the same priority level, sections that appear **later** in the
        input list are dropped first (i.e. earlier sections of equal priority
        are more important).

        ``kept_names`` 为 M1a trace 的可选出参：传入列表时回填**最终保留**
        的段名（按组装顺序）。默认 None 时零开销、行为与改动前逐字一致。
        ``trunc_events`` 为 M1b 模块 3 的可选出参：传入列表时回填截断事件
        （{"kind": "section_dropped"|"char_slice", "section", "dropped_chars",
        "budget"}）；零截断回填空列表（诚实区分「没截」与「没采」）。
        """
        if not sections:
            return ""

        def _note_kept(selected: List[PromptSection]) -> None:
            # 只在调用方要段序时记录（M1a），各 return 点前统一调它
            if kept_names is not None:
                kept_names[:] = [s.name for s in selected if s.content]

        def _note_events(kept_indices: set, sliced_chars: int = 0) -> None:
            # 只在调用方要事件时记录（M1b 模块 3）。丢段按 drop 循环实际
            # 淘汰的非空段记；硬切再补一条 char_slice（sliced_chars>0）。
            if trunc_events is None:
                return
            for i, s in enumerate(sections):
                if s.content and i not in kept_indices:
                    trunc_events.append({
                        "kind": "section_dropped",
                        "section": s.name,
                        "dropped_chars": len(s.content),
                    })
            if sliced_chars > 0:
                trunc_events.append({
                    "kind": "char_slice",
                    "dropped_chars": sliced_chars,
                    "budget": max_length,
                })

        # Build in order: sections with lower priority index are kept
        # To drop, we sort descending by priority (highest first to drop)
        # Among equal priority, later indices are dropped first
        indexed = list(enumerate(sections))

        # Keep trying to fit; start by keeping all, then drop highest-priority
        # sections one at a time until we fit.
        def _assemble(selected: List[PromptSection]) -> str:
            return "\n\n".join(s.content for s in selected if s.content)

        result = _assemble(sections)
        if len(result) <= max_length:
            _note_kept(sections)
            _note_events(set(range(len(sections))))
            return result

        # Sort by (priority DESC, index DESC) — highest priority + latest first to drop
        drop_order = sorted(indexed, key=lambda x: (-x[1].priority, -x[0]))

        kept_indices: set = set(range(len(sections)))
        remaining = sections  # drop 循环可能一次都不跑（全 priority-0）
        for idx, sec in drop_order:
            if sec.priority == 0:
                continue  # never drop priority-0
            kept_indices.discard(idx)
            remaining = [sections[i] for i in sorted(kept_indices)]
            result = _assemble(remaining)
            if len(result) <= max_length:
                _note_kept(remaining)
                _note_events(kept_indices)
                return result

        # If still too long after dropping all non-zero sections, truncate last
        if len(result) > max_length:
            # L20 修复：`max_length - 20` 在 max_length < 20 时是负数，Python 负
            # 切片会从**末尾**取，于是「截断」反而把整段文本接在尾巴上越截越长。
            # max_prompt_length 理论上可配到 20 以下（面板是数字输入，无下限校验），
            # 这里用 max(0, ...) 保证切片端点非负。
            before_slice = len(result)
            result = result[: max(0, max_length - 20)] + "\n\n...[已截断]"
            # 末段被硬截：按 drop 循环后的剩余段记段名（remaining 已在循环前
            # 初始化，走到这里必然有定义；与 result 内容一致或为其前缀超集，
            # 段序信息足够诊断用）。
            _note_kept(remaining)
            _note_events(
                kept_indices,
                sliced_chars=before_slice - max(0, max_length - 20),
            )
        return result

    # ----------------------------------------------------------
    # Hardcoded prompt text builders
    # ----------------------------------------------------------

    @staticmethod
    def build_narrative_integrity_protocol() -> str:
        """Layer 0a: 叙事完整性要求（硬编码）。

        合规边界（勿回退）：本协议只约束「怎么写」——叙事完整、不跳段、
        不输出创作外的元叙述；不声明任何内容授权、不覆盖模型自身的安全
        策略、不要求模型放弃自己的判断。
        """
        return (
            "## 叙事完整性要求\n\n"
            "你是创意写作引擎，本轮的职责是按用户输入与角色设定完成一段虚构叙事。\n\n"
            "【写作格式要求】\n"
            "1. 叙事完整 - 完整描写本轮的场景：包含开端、发展、收尾，"
            "不要用省略号跳过或一笔带过正在发生的情节\n"
            "2. 保持连贯 - 用户说「继续」时，从上一段最后一句的位置接着写，不跳场景、不跳时间\n"
            "3. 保持视角 - 全程以角色身份叙述，不输出创作外的元叙述"
            "（如「以上是…」「希望…」「如需…」「消息已发送」这类说明）\n"
            "4. 遵守设定 - 角色卡、世界书与用户输入优先于任何通用写作习惯\n\n"
            "本要求只涉及写作格式与叙事完整性，不涉及内容层面；"
            "模型自身的安全策略与判断始终有效。\n"
            "如果你认为某段内容不适合处理，按你自己的判断处理即可。"
        )

    def build_send_message_guide(self) -> str:
        """Layer 0b: mandatory tool-call sending protocol + tail safety."""
        guide = (
            "## 输出发送协议\n\n"
            "你必须严格遵守以下流程：\n\n"
            "步骤1：以角色身份理解用户的意图，完全进入角色状态\n"
            "步骤2：用角色的语气写出一段完整的回复\n"
            f"  长度下限 {self.min_output_length} 汉字，写得不够长视为任务失败\n"
            "  包含大量对话、内心独白、感官描写、动作细节\n"
            "  节奏递进：铺垫 → 发展 → 高潮 → 收尾，不允许省略任何阶段\n"
            "  连续性规则：用户说“继续”时，从上一段最后一句的位置继续写，禁止跳场景、跳时间\n"
        )
        # M11 修复：`performance.max_output_length` 此前只有面板控件与读入，
        # 没有任何消费点（对照 min_output_length 在上一行被真消费），用户把它
        # 设成 2000 也不会有任何效果。这里与下限同一形态消费：写成写作要求，
        # 而不是对已生成文本做硬截断——硬截断会在句子中间断开，且会把
        # 已经渲染好的状态栏一起切掉。0 表示不限，与 schema 语义一致。
        if self.max_output_length > 0:
            guide += (
                f"  长度上限 {self.max_output_length} 汉字，超出视为任务失败"
                "（在到达上限前收束情节，不要把句子写一半）\n"
            )

        # 动态判定：如果开启了状态栏，就强制它一起打包塞进工具里
        if self.status_bar_enabled:
            guide += "步骤3：调用 send_message_to_user 发送回复（注意：必须在 messages 的 text 字段末尾追加 [LOVE_DATA] 状态栏和剧情选项）\n"
            guide += "  **重要：必须将【故事正文】和【[LOVE_DATA]状态栏及选项】一次性打包，全部放在 messages 参数的 text 字段中**\n"
        else:
            guide += "步骤3：调用 send_message_to_user 发送回复\n"
            guide += (
                "  **仅输出故事正文，禁止在回复中的任何位置输出状态栏、"
                f"{self.build_status_contract()['forbidden_hint']} 等内容**\n"
            )

        guide += (
            "  **不要先调用其他工具（file_read、shell 等）——现在就用这个工具发送**\n"
            "  禁止用其他工具发送\n"
        )
        return guide

    def build_safety_wrapper(self) -> str:
        """Layer 3: compact format reminder at the end of dynamic prompt (closest to user).

        示例走 build_status_contract()，与 guide / tail message 同源。
        """
        wrapper = "故事正文中禁止输出任何创作外的文字——不要总结、不要解释、不要询问是否需要继续。\n"
        if self.status_bar_enabled:
            c = self.build_status_contract()
            wrapper += (
                "【状态栏强制格式】故事正文结束后必须立刻追加这两部分：\n"
                f"1) 单行状态：{c['fields_line']}\n"
                f"2) 3个剧情走向选项：{c['plot_markers']}\n"
                f"示例：{c['sample_line']}\n"
                "禁止省略任何字段，禁止用省略号代替状态值。\n"
            )
        else:
            wrapper += (
                f"【禁止】绝对不要输出 {self.build_status_contract()['forbidden_hint']} 等任何元数据。\n"
            )
        return wrapper

    # ----------------------------------------------------------
    # 状态栏格式契约：单一来源
    # ----------------------------------------------------------

    def build_status_contract(self) -> dict:
        """状态栏格式契约的唯一来源。

        为什么要有这个方法：格式契约此前在四处各写一份（状态栏 guide、
        send_message 步骤3、safety_wrapper、main.py 的 tail message），
        每处的示例都是手抄的字面量。改一处漏三处就是「提示词互相矛盾」，
        而字段名/顺序用户可配——用户改了字段后，示例与实际契约必然漂移。

        本方法把「格式行 / 示例行 / 字段说明 / 剧情选项格式」集中生成，
        四处按需取用，**不再各自手写示例**。

        返回的键：
          fields_line     —— 契约格式行（`[LOVE_DATA] {好感度} | ...`）
          sample_line     —— 按当前字段表生成的完整示例行
          fields_help     —— 逐字段说明（多行）
          plot_block      —— 剧情走向三选项格式块（ASCII 形态，仅 legacy
                            t18 逐字钉住保留，勿消费）
          plot_block_v2   —— 实际注入的【剧情走向】/【请选择】形态（M3.0c）
          plot_markers    —— 【剧情走向】/【请选择】两个标记（给一行式提醒用）
          forbidden_hint  —— 关闭状态栏时的禁止项（与开启时同源，避免两边漂移）
        """
        fields = [f for f in (self.love_fields or []) if f] or list(_DEFAULT_FIELDS)
        plots = [p for p in (self.status_bar_plot_paths or []) if p] or list(_DEFAULT_PLOT_PATHS)

        fields_line = "[LOVE_DATA] " + " | ".join(f"{{{f}}}" for f in fields)
        sample_line = "[LOVE_DATA] " + " | ".join(
            _FIELD_SAMPLES.get(f, "（示例值）") for f in fields
        )

        help_lines = ["字段说明："]
        for f in fields:
            desc = _FIELD_DESCRIPTIONS.get(f, "按当前剧情填写")
            help_lines.append(f"- {f}：{desc}")
        # 「好感度」可替换成其它量化指标——这句只在字段表里真有名叫好感度时才加，
        # 否则用户已经把字段改掉了，还说「好感度只是参考字段名」是自相矛盾。
        if any("好感度" in f for f in fields):
            help_lines.append(
                "  *注意：好感度只是参考字段名，可根据剧情需要替换为其他可量化指标，"
                "如：信任度、默契度、亲近度、羁绊度等*"
            )

        # 三档阶段的参考表只在字段含「好感度」时给，理由同上
        if any("好感度" in f for f in fields):
            help_lines.append("")
            help_lines.append("好感度阶段参考（仅当字段为好感度时适用）：")
            help_lines.append("0-20：陌生人 | 21-40：友善但疏离 | 41-60：普通朋友")
            help_lines.append("61-70：萌生好感 | 71-80：强烈好感 | 81-90：爱慕")
            help_lines.append("91-100：深爱，彻底沦陷")

        plot_options = "\n".join(
            f"{i}. {p}" for i, p in enumerate(plots[:3] or _DEFAULT_PLOT_PATHS, 1)
        )
        # legacy t18（tests/legacy/test_status_bar_parsers.py，断言逐字保留
        # 铁律）把 plot_block 的 ASCII 箭头形态逐字钉死；M3.0c 起实际注入
        # 一律走 plot_block_v2（【剧情走向】/【请选择】——webchat 强制流式
        # 路径 content 逐块直出、任何发送前钩子都拦不到 ASCII 的 >>>，只能
        # 从源头让模型不再产出）。勿再消费 plot_block。
        plot_block = ">>> 剧情走向 <<<\n" + plot_options + "\n<<< 请选择 >>>"
        plot_block_v2 = "【剧情走向】\n" + plot_options + "\n【请选择】"

        return {
            "fields_line": fields_line,
            "sample_line": sample_line,
            "fields_help": "\n".join(help_lines),
            "plot_block": plot_block,
            "plot_block_v2": plot_block_v2,
            "plot_markers": "【剧情走向】 ... 【请选择】",
            "forbidden_hint": (
                "[LOVE_DATA]、状态栏、好感度数值、关系阶段、心情标签、"
                "穿着描述、位置信息、剧情走向选项"
            ),
        }

    def build_status_bar_guide(self) -> str:
        """Layer 0c: mandatory status bar protocol.

        方案D: Prompt 增强 — 加入格式强制强调 + 负例展示 + 正确输出唯一性约束。
        示例与格式行全部来自 build_status_contract()，不再手写。
        """
        c = self.build_status_contract()
        return (
            "## 状态栏与剧情走向\n\n"
            "在故事正文描写结束后，你必须追加输出以下内容：\n\n"
            "---\n"
            "【状态栏】\n"
            "\n"
            "⚠️ 格式强制要求（违反将导致状态栏解析失败）：\n"
            "必须使用 [LOVE_DATA] 标签开头，一行内输出，管道符 | 分隔，顺序固定。\n"
            "分隔符只能用 |，字段值内不得包含管道符。\n\n"
            "格式（一行，管道分隔）：\n"
            f"{c['fields_line']}\n\n"
            "❌ 错误示例（务必避免）：\n"
            "- 好感度：85（缺少 [LOVE_DATA] 标签）\n"
            "- [LOVE_DATA] 好感度→85 | 心情→放松（使用了 → 而非直接写值）\n"
            "- 好感度=85\\n关系阶段=暧昧（换行输出，未用管道符）\n"
            "- ```\\n好感度：85\\n```（用了代码块而非 [LOVE_DATA] 单行格式）\n\n"
            "✅ 正确示例：\n"
            f"{c['sample_line']}\n\n"
            f"{c['fields_help']}\n\n"
            "---\n"
            "【剧情走向选项】\n"
            "\n"
            "给出 3 个不同的剧情走向选项。\n"
            "规则：\n"
            "- 选项必须有明显区别，覆盖不同方向（如 主动推进 / 观察等待 / 意外转向）\n"
            "- 各选项应导向不同的剧情可能，不应三个都差不多\n"
            "- 基于当前剧情合理延伸，而非凭空创造新的设定\n\n"
            "格式：\n"
            f"{c['plot_block_v2']}"
        )

    def build_status_reminder(self) -> str:
        """状态栏的**一行式**短提醒（供 main.py 的 tail message 使用）。

        tail message 每轮都追加在用户消息附近，是 token 成本最高的一处。
        格式细节交给 system prompt 里的 guide（那里是完整契约），这里只做
        「别忘了」级别的提醒，示例走 contract 的 sample_line，避免两处示例漂移。
        """
        c = self.build_status_contract()
        return (
            "本轮回复末尾必须严格按以下格式追加状态栏和剧情选项，禁止使用其他格式：\n"
            f"{c['fields_line']}\n"
            f"示例：{c['sample_line']}\n"
            f"{c['plot_block_v2']}\n"
            "禁止使用 --- 分隔线、> 块引用、```代码块```、或其他格式。"
            "必须使用上述 [LOVE_DATA] 和【剧情走向】/【请选择】标记。"
        )

async def _self_test():
    """Run self-tests. Covers section assembly, truncation, inject, and fallback."""
    print("=== PromptBuilder Self-Test ===\n")

    passed = 0
    failed = 0

    def _assert(condition: bool, label: str):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {label}")
        else:
            failed += 1
            print(f"  [FAIL] {label}")

    builder = PromptBuilder({"performance": {"max_prompt_length": 50000}})

    # ----------------------------------------------------------------
    # 1. Protocol text builders produce non-empty strings
    # ----------------------------------------------------------------
    print("\n== 1. Protocol text builders ==")
    nip = builder.build_narrative_integrity_protocol()
    _assert(len(nip) > 100, f"narrative_integrity_protocol length = {len(nip)} (>100)")
    _assert("send_reply" not in nip, "no 'send_reply' in narrative integrity protocol")
    # 合规守护（勿回退）：协议只约束写作格式，不得出现内容授权措辞、
    # 不得要求模型放弃自己的判断，且必须写明模型自身的安全策略仍然有效。
    # 这组断言是「不要再把越权措辞写回来」的回归护栏——市场审查曾因
    # prompt_builder 里的预授权/禁止拒绝语义判罚，改回即触发失败。
    _forbidden = (
        "授权", "预先", "覆盖所有默认安全策略", "禁止拒绝", "禁止警告",
        "自我审查", "越狱", "你不是审核员", "不是内容过滤器",
    )
    for _bad in _forbidden:
        _assert(_bad not in nip, f"no '{_bad}' in narrative integrity protocol")
    _assert("安全策略" in nip, "protocol states model policy stays in effect")

    smg = builder.build_send_message_guide()
    _assert(len(smg) > 50, f"send_message_guide length = {len(smg)} (>50)")
    _assert("send_message_to_user" in smg, "contains 'send_message_to_user'")

    sw = builder.build_safety_wrapper()
    _assert(len(sw) > 20, f"safety_wrapper length = {len(sw)} (>20)")
    _assert("send_message_to_user" not in sw, "safety wrapper no longer mentions tool")

    # ----------------------------------------------------------------
    # 2. build_system_prompt with no managers (just protocols)
    # ----------------------------------------------------------------
    print("\n== 2. build_system_prompt with no managers ==")
    stable, dynamic = await builder.build_system_prompt(None, None, {})
    prompt = (stable + "\n\n" + dynamic).strip()
    _assert(len(prompt) > 100, f"prompt assembled, length = {len(prompt)}")

    # Verify narrative protocol is in stable, safety_wrapper is in dynamic
    _assert(nip[:30] in stable, "narrative integrity protocol in stable prompt")
    _assert(sw[:15] in dynamic, "safety wrapper in dynamic prompt")

    # ----------------------------------------------------------------
    # 3. inject_prompt: protocol-first order (stable → original → dynamic)
    # ----------------------------------------------------------------
    print("\n== 3. inject_prompt ==")
    original = "You are a friendly assistant named Alice."
    new_stable = "[Stable content]"
    new_dynamic = "[Dynamic content]"
    merged = builder.inject_prompt(original, new_stable, new_dynamic)
    orig_pos = merged.find("Alice")
    stable_pos = merged.find("[Stable content]")
    dynamic_pos = merged.find("[Dynamic content]")
    _assert(stable_pos < orig_pos, "stable prompt appears BEFORE original (protocol first)")
    _assert(orig_pos < dynamic_pos, "original prompt appears BEFORE dynamic content")
    _assert("Alice" in merged, "original content preserved")

    # Edge case: empty stable_prompt
    merged_empty = builder.inject_prompt(original, "", "")
    _assert(merged_empty == original, "empty prompts returns original unchanged")

    # ----------------------------------------------------------------
    # 4. Truncation: priority-0 sections preserved
    # ----------------------------------------------------------------
    print("\n== 4. Smart truncation ==")

    # Manual truncation test with explicit sections
    sections = [
        PromptSection(name="critical", content="CRITICAL_CONTENT_STAY", priority=0),
        PromptSection(name="big_filler", content="X" * 600, priority=10),
        PromptSection(name="safety", content="SAFETY_WRAPPER_STAY", priority=1),
    ]
    result = PromptBuilder._smart_truncate(sections, max_length=200)
    _assert("CRITICAL_CONTENT_STAY" in result, "priority-0 section preserved during truncation")
    _assert("SAFETY_WRAPPER_STAY" in result, "priority-1 section preserved (big_filler dropped)")

    # Test: all priority-0 kept even when total exceeds limit
    sections_all_critical = [
        PromptSection(name="p0a", content="A" * 300, priority=0),
        PromptSection(name="p0b", content="B" * 300, priority=0),
        PromptSection(name="p0c", content="C" * 300, priority=0),
    ]
    result_all = PromptBuilder._smart_truncate(sections_all_critical, max_length=100)
    _assert("A" in result_all, "first priority-0 kept even over limit")

    # ----------------------------------------------------------------
    # 5. retrieve_content_layers match=0 fallback
    # ----------------------------------------------------------------
    print("\n== 5. Match=0 fallback (mock) ==")

    class MockWR:
        def __init__(self):
            self._match_return = []
            self._top_entries = [
                {"entry_id": "fallback_1", "content": "Fallback entry one", "category": "core"},
                {"entry_id": "fallback_2", "content": "Fallback entry two", "category": "core"},
            ]

        async def get_constant_entries(self):
            return []

        async def match(self, user_input, top_k=5, **kwargs):
            return self._match_return

        async def get_top_entries_by_match_count(self, limit=2):
            return self._top_entries[:limit]

    mock_wr = MockWR()
    layer1, layer2, layer1_random = await builder.retrieve_content_layers(
        mock_wr, None, {
            "user_input": "something that matches nothing",
            "persona_data": {"quill_extensions": {"wr_mode": "auto"}}
        }
    )
    _assert(len(layer2) == 2, f"fallback returned 2 entries (got {len(layer2)})")
    _assert("Fallback entry one" in layer2[0], "first fallback entry present")
    _assert(isinstance(layer1_random, list), "layer1_random is a list")

    # ----------------------------------------------------------------
    # 6. skip_constants
    # ----------------------------------------------------------------
    print("\n== 6. skip_constants ==")

    class MockWRWithConstants:
        def __init__(self):
            self.constants = [
                {"entry_id": "const_1", "content": "CONSTANT_ENTRY", "category": "writing_style"},
            ]

        async def get_constant_entries(self):
            return self.constants

        async def match(self, user_input, top_k=5, **kwargs):
            return []

        async def get_top_entries_by_match_count(self, limit=2):
            return []

    mock_wr_const = MockWRWithConstants()

    # Without skip_constants: constants appear in layer1
    l1, l2, l1r = await builder.retrieve_content_layers(
        mock_wr_const, None, {
            "user_input": "test",
            "persona_data": {"quill_extensions": {"wr_mode": "auto"}}
        }
    )
    _assert(any("CONSTANT_ENTRY" in p for p in l1), "constants appear when skip_constants=False")

    # With skip_constants: constants are excluded
    l1_skip, l2_skip, l1r_skip = await builder.retrieve_content_layers(
        mock_wr_const, None, {
            "user_input": "test",
            "skip_constants": True,
            "persona_data": {"quill_extensions": {"wr_mode": "auto"}}
        }
    )
    _assert(not any("CONSTANT_ENTRY" in p for p in l1_skip), "constants skipped when skip_constants=True")
    _assert(len(l1_skip) == 0, "layer1_parts empty with skip_constants")
    _assert(len(l1r_skip) == 0, "layer1_random_parts empty with skip_constants")

    # ----------------------------------------------------------------
    # 7. min_output_length from config
    # ----------------------------------------------------------------
    print("\n== 7. min_output_length config ==")
    builder800 = PromptBuilder({"performance": {"max_prompt_length": 50000, "min_output_length": 800}})
    builder400 = PromptBuilder({"performance": {"max_prompt_length": 50000, "min_output_length": 400}})
    smg800 = builder800.build_send_message_guide()
    smg400 = builder400.build_send_message_guide()
    _assert("800 汉字" in smg800, "send_message_guide uses 800")
    _assert("400 汉字" in smg400, "send_message_guide uses 400")

    # ----------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        raise AssertionError(f"{failed} test(s) failed")
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    # asyncio 已在模块顶部导入，无需重复导入
    asyncio.run(_self_test())
