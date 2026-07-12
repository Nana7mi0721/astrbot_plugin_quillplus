# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillConfig — 从 AstrBot 注入的 config dict 读取配置，提供属性访问 + 校验。

AstrBot 启动时扫描 _conf_schema.json，实例化 AstrBotConfig(dict) 注入 __init__。
本类包装该 dict，提供类型安全的属性访问和默认值回退。
"""

from __future__ import annotations


# 状态栏默认字段（当 config 解析失败时的硬编码回退）
_DEFAULT_LOVE_FIELDS = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法"]


def _get_nested(raw: dict, group: str, default=None):
    """从 config dict 中安全读取嵌套分组（返回整个子 dict）。"""
    group_dict = raw.get(group, default)
    if not isinstance(group_dict, dict):
        return default if default is not None else {}
    return group_dict


def _safe_int(value, default: int = 0) -> int:
    """S3-9: 安全 int 转换，非法值回退默认值，防止配置解析崩溃。"""
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    """S3-9: 安全 float 转换，非法值回退默认值，防止配置解析崩溃。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class QuillConfig:
    """Quill 插件配置解析层。"""

    def __init__(self, config: dict | None = None):
        self._raw = config or {}

        # ── rag ──
        rag = _get_nested(self._raw, "rag", {}) or {}
        self.rag_llm_provider_id: str = str(rag.get("llm_provider_id", "") or "").strip()
        self.rag_embedding_provider_id: str = str(rag.get("embedding_provider_id", "") or "").strip()
        self.rag_rerank_provider_id: str = str(rag.get("rerank_provider_id", "") or "").strip()
        self.rag_enable_local_embedding: bool = bool(rag.get("enable_local_embedding", True))
        self.rag_chunk_size: int = _safe_int(rag.get("chunk_size", 500), 500)
        self.rag_chunk_overlap: int = _safe_int(rag.get("chunk_overlap", 50), 50)
        self.rag_top_k: int = _safe_int(rag.get("top_k", 3), 3)
        self.rag_dense_top_k: int = _safe_int(rag.get("dense_top_k", 5), 5)
        self.rag_enable_memory: bool = bool(rag.get("enable_memory", False))
        self.rag_enable_chat_logging: bool = bool(rag.get("enable_chat_logging", True))
        self.rag_chat_log_retention_days: int = _safe_int(rag.get("chat_log_retention_days", 30), 30)

        # ── worldbook ──
        wb = _get_nested(self._raw, "worldbook", {}) or {}
        self.worldbook_enabled: bool = bool(wb.get("enabled", True))
        self.worldbook_max_dynamic: int = _safe_int(wb.get("max_dynamic_entries", 4), 4)
        self.worldbook_max_token: int = _safe_int(wb.get("max_token_limit", 4000), 4000)
        self.worldbook_sensitivity: float = _safe_float(wb.get("match_sensitivity", 0.7), 0.7)
        self.worldbook_injection_pos: str = str(wb.get("injection_position", "user_prefix"))
        self.worldbook_show_log: bool = bool(wb.get("show_trigger_log", False))
        self.worldbook_always_activate: bool = bool(wb.get("always_activate", False))

        # ── knowledge_base ──
        kb = _get_nested(self._raw, "knowledge_base", {}) or {}
        self.kb_enabled: bool = bool(kb.get("enabled", True))
        self.kb_max_entries: int = _safe_int(kb.get("max_entries", 4), 4)
        self.kb_fallback_top: int = _safe_int(kb.get("fallback_top_count", 2), 2)
        self.kb_dedup_limit: int = _safe_int(kb.get("category_dedup_limit", 3), 3)

        # ── performance ──
        perf = _get_nested(self._raw, "performance", {}) or {}
        self.max_prompt_length: int = _safe_int(perf.get("max_prompt_length", 50000), 50000)
        self.min_output_length: int = _safe_int(perf.get("min_output_length", 400), 400)
        self.max_output_length: int = _safe_int(perf.get("max_output_length", 0), 0)

        # ── status_bar ──
        sb = _get_nested(self._raw, "status_bar", {}) or {}
        self.status_bar_enabled: bool = bool(sb.get("enabled", True))
        self.status_bar_format: str = str(sb.get("format_template", "**状态栏**\n```\n{content}\n```"))
        # 解析剧情走向选项
        plot_raw = sb.get("plot_paths", "") or ""
        if isinstance(plot_raw, str) and plot_raw.strip():
            self.status_bar_plot_paths: list[str] = [p.strip() for p in plot_raw.split("|") if p.strip()]
        else:
            self.status_bar_plot_paths = ["继续当前话题", "转换场景", "结束互动"]
        # 解析字段列表
        fields_raw = sb.get("fields", "") or ""
        if isinstance(fields_raw, str) and fields_raw.strip():
            self.status_bar_fields: list[str] = [f.strip() for f in fields_raw.split("|") if f.strip()]
        else:
            self.status_bar_fields = list(_DEFAULT_LOVE_FIELDS)
        # 确保至少 6 个字段（不足补空字符串）
        while len(self.status_bar_fields) < 6:
            self.status_bar_fields.append("")
        # 方案C: LLM 智能提取开关（默认关闭，高成本兜底）
        self.status_bar_llm_extract: bool = bool(sb.get("llm_extract", False))
        # P0-2: 状态栏 LLM 提取独立 provider（留空回退到 RAG 摘要 LLM）
        self.status_bar_llm_provider_id: str = str(sb.get("llm_provider_id", "") or "").strip()

        # ── refusal ──
        ref = _get_nested(self._raw, "refusal", {}) or {}
        self.refusal_enabled: bool = bool(ref.get("enabled", True))
        patterns_raw = ref.get("patterns", "") or ""
        if isinstance(patterns_raw, str) and patterns_raw.strip():
            self.refusal_patterns: list[str] = [p.strip() for p in patterns_raw.splitlines() if p.strip()]
        else:
            self.refusal_patterns = ["我不能", "我无法", "这违反", "我不应该", "这不合适", "我拒绝"]

        # ── debug ──
        dbg = _get_nested(self._raw, "debug", {}) or {}
        self.debug_enabled: bool = bool(dbg.get("enabled", False))
        self.panel_theme: str = str(dbg.get("panel_theme", "light"))

        # ── permissions ──
        perm = _get_nested(self._raw, "permissions", {}) or {}
        admin_raw = perm.get("admin_users", "") or ""
        if isinstance(admin_raw, str) and admin_raw.strip():
            import re
            self.admin_users: list[str] = [
                u.strip() for u in re.split(r'[,\n|]+', admin_raw) if u.strip()
            ]
        elif isinstance(admin_raw, list):
            self.admin_users = [str(u).strip() for u in admin_raw if str(u).strip()]
        else:
            self.admin_users = []

    def get_raw(self) -> dict:
        """返回原始 config dict（只读用途）。"""
        return self._raw

    def __repr__(self) -> str:
        return (
            f"QuillConfig(wb={'on' if self.worldbook_enabled else 'off'}, "
            f"kb={'on' if self.kb_enabled else 'off'}, "
            f"debug={'on' if self.debug_enabled else 'off'})"
        )
