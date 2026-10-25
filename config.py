# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillConfig — 从 AstrBot 注入的 config dict 读取配置，提供属性访问 + 校验。

AstrBot 启动时扫描 _conf_schema.json，实例化 AstrBotConfig(dict) 注入 __init__。
本类包装该 dict，提供类型安全的属性访问和默认值回退。
"""

from __future__ import annotations

import re


# 状态栏默认字段（当 config 解析失败时的硬编码回退）
_DEFAULT_LOVE_FIELDS = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法"]

# 不渲染 Markdown 的平台（按 AstrBot 适配器注册名匹配）。
# 这些平台上 `**状态栏**` 与 ``` 围栏会原样显示，故改用纯文本模板。
# 依据：框架里 aiocqhttp 直接发字符串；QQ 官方 API 虽有 markdown 段类型，
# 但个人/群机器人实际多按纯文本呈现。用户可用 plain_platforms 覆盖。
_DEFAULT_PLAIN_PLATFORMS = [
    "aiocqhttp", "qq_official", "qq_official_webhook",
    "wecom", "wecom_ai_bot", "weixin_oc", "weixin_official_account",
    "dingtalk", "line",
]


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


def _safe_bool(value, default: bool = False) -> bool:
    """P2-6: 安全 bool 转换。

    配置面板可能传字符串（如 "false"/"0"/"off"），直接 bool("false") 会得 True。
    这里统一识别常见假值字符串，其余按真值处理。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "0", "false", "no", "off", "none", "null"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
        return bool(value)
    return bool(value)


class QuillConfig:
    """Quill 插件配置解析层。"""

    def __init__(self, config: dict | None = None):
        self._raw = config or {}

        # ── rag ──
        rag = _get_nested(self._raw, "rag", {}) or {}
        self.rag_llm_provider_id: str = str(rag.get("llm_provider_id", "") or "").strip()
        self.rag_embedding_provider_id: str = str(rag.get("embedding_provider_id", "") or "").strip()
        self.rag_rerank_provider_id: str = str(rag.get("rerank_provider_id", "") or "").strip()
        self.rag_enable_local_embedding: bool = _safe_bool(rag.get("enable_local_embedding"), True)
        self.rag_chunk_size: int = _safe_int(rag.get("chunk_size", 500), 500)
        self.rag_chunk_overlap: int = _safe_int(rag.get("chunk_overlap", 50), 50)
        self.rag_top_k: int = _safe_int(rag.get("top_k", 3), 3)
        self.rag_dense_top_k: int = _safe_int(rag.get("dense_top_k", 5), 5)
        # AstrBot 内置知识库（kb: 前缀可绑定源）。默认开：没建库时零副作用，
        # 关闭后完全不碰 kb_manager，行为与旧版一致。
        self.rag_astrbot_kb_enabled: bool = _safe_bool(rag.get("astrbot_kb_enabled"), True)
        self.rag_enable_memory: bool = _safe_bool(rag.get("enable_memory"))
        self.rag_enable_autonomous_reflection: bool = _safe_bool(rag.get("enable_autonomous_reflection"), True)
        self.rag_enable_chat_logging: bool = _safe_bool(rag.get("enable_chat_logging"), True)
        self.rag_chat_log_retention_days: int = _safe_int(rag.get("chat_log_retention_days", 30), 30)

        # ── worldbook ──
        wb = _get_nested(self._raw, "worldbook", {}) or {}
        self.worldbook_enabled: bool = _safe_bool(wb.get("enabled"), True)
        self.worldbook_max_dynamic: int = _safe_int(wb.get("max_dynamic_entries", 4), 4)
        self.worldbook_max_token: int = _safe_int(wb.get("max_token_limit", 4000), 4000)
        self.worldbook_sensitivity: float = _safe_float(wb.get("match_sensitivity", 0.7), 0.7)
        self.worldbook_injection_pos: str = str(wb.get("injection_position", "user_prefix"))
        self.worldbook_show_log: bool = _safe_bool(wb.get("show_trigger_log"))
        self.worldbook_always_activate: bool = _safe_bool(wb.get("always_activate"))
        # Issue #1：角色卡导入自动转换内嵌世界书（character_book）并绑定
        self.worldbook_card_embed_import: bool = _safe_bool(wb.get("card_embed_worldbook"), True)

        # ── writing_resource ──
        wr = _get_nested(self._raw, "writing_resource", {}) or {}
        self.wr_enabled: bool = _safe_bool(wr.get("enabled"), True)
        self.wr_max_entries: int = _safe_int(wr.get("max_entries", 4), 4)
        self.wr_fallback_top: int = _safe_int(wr.get("fallback_top_count", 2), 2)
        self.wr_dedup_limit: int = _safe_int(wr.get("category_dedup_limit", 3), 3)

        # ── performance ──
        perf = _get_nested(self._raw, "performance", {}) or {}
        self.max_prompt_length: int = _safe_int(perf.get("max_prompt_length", 50000), 50000)
        self.min_output_length: int = _safe_int(perf.get("min_output_length", 400), 400)
        self.max_output_length: int = _safe_int(perf.get("max_output_length", 0), 0)

        # ── status_bar ──
        sb = _get_nested(self._raw, "status_bar", {}) or {}
        # L12 修复：这里漏传了默认值。`_safe_bool(v)` 在 v 为 None（配置里没有
        # 该键）时返回 False，而 _conf_schema.json 给该键的默认值是 true ——
        # 从旧版升级上来、用户又没动过这个开关时，面板显示「启用」而代码按
        # 「关闭」跑，状态栏整条链路被静默关掉。姊妹键（下方 plain_platforms
        # 一档及 performance 各键）都显式写了默认值，这里补齐同一口径。
        self.status_bar_enabled: bool = _safe_bool(sb.get("enabled"), True)
        self.status_bar_format: str = str(sb.get("format_template", "**状态栏**\n```\n{content}\n```"))
        # 纯文本平台的渲染模板：QQ/微信这类不渲染 Markdown 的平台，
        # `**` 与 ``` 会原样显示，用户看到的是「**状态栏** ```」这种噪声。
        # 未知平台仍走 format_template（保持原行为，不破坏原生 Markdown 渲染）。
        self.status_bar_format_plain: str = str(
            sb.get("format_template_plain", "───── 状态栏 ─────\n{content}\n────────────────")
        )
        # 哪些平台算「纯文本」。按适配器注册名匹配（aiocqhttp / qq_official ...），
        # 用户可覆盖以适配自建网关。
        _plain_raw = sb.get("plain_platforms", "") or ""
        if isinstance(_plain_raw, str) and _plain_raw.strip():
            self.status_bar_plain_platforms: list[str] = [
                p.strip().lower() for p in re.split(r"[|,]", _plain_raw) if p.strip()
            ]
        else:
            self.status_bar_plain_platforms = list(_DEFAULT_PLAIN_PLATFORMS)
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
        self.status_bar_llm_extract: bool = _safe_bool(sb.get("llm_extract"))
        # P0-2: 状态栏 LLM 提取独立 provider（留空回退到 RAG 摘要 LLM）
        self.status_bar_llm_provider_id: str = str(sb.get("llm_provider_id", "") or "").strip()
        # P1-7: 状态栏字段未匹配时的占位符文本（可配置，默认"未设置"）
        self.status_bar_default_placeholder: str = str(sb.get("default_placeholder", "未设置"))
        # 状态栏数值变化标注（相对上一轮的 ↑↓ 幅度）
        self.status_bar_show_delta: bool = _safe_bool(sb.get("show_delta", True))

        # ── Decision 决策模式（结构化判定：推荐选择度 + 分支路由）──
        # 剧情走向「推荐选择度」（选项后追加 ▸ N%）+ 自由文本回复的分支路由。
        # 判定模型凭据独立配置（status_bar.decision_*），不经过 AstrBot 提供商
        # ——决策模型没有 chat/completions 端点，openai_compatible 不可用；
        # System One / OpenAI Decisions 两族 wire format 由 decision_client 适配。
        # 全链 fail-open：判定失败/低置信/模型不对 → 维持普通渲染。
        # 迁移：v5.3.x 的 jev_enabled / jev_confidence_floor 在新键缺省时回退读取
        # （AstrBot 升级时会把 schema 默认值合并进旧配置，但老用户开过 JEV 的
        # 意愿不该被默认 false 覆盖）。
        _dec_enabled = sb.get("decision_enabled")
        if _dec_enabled is None:
            _dec_enabled = sb.get("jev_enabled")
        self.status_bar_decision_enabled: bool = _safe_bool(_dec_enabled)
        self.status_bar_decision_base_url: str = str(
            sb.get("decision_base_url", "https://openrouter.ai/api/v1") or ""
        ).strip() or "https://openrouter.ai/api/v1"
        self.status_bar_decision_api_key: str = str(sb.get("decision_api_key", "") or "").strip()
        self.status_bar_decision_model: str = str(sb.get("decision_model", "") or "").strip()
        _dec_fmt = str(sb.get("decision_api_format", "auto") or "auto").strip().lower()
        self.status_bar_decision_api_format: str = (
            _dec_fmt if _dec_fmt in ("auto", "systemone", "openai") else "auto"
        )
        _dec_floor = sb.get("decision_confidence_floor")
        if _dec_floor is None:
            _dec_floor = sb.get("jev_confidence_floor")
        # 置信度低于该值不标百分比、不路由（分布平坦时数字没有信息量）
        self.status_bar_decision_confidence_floor: float = min(
            max(_safe_float(_dec_floor, 0.6), 0.0), 1.0
        )

        # ── debug ──
        dbg = _get_nested(self._raw, "debug", {}) or {}
        self.debug_enabled: bool = _safe_bool(dbg.get("enabled"))
        # 注入报告独立于 debug 日志开关：debug 是「控制台刷屏」，本项是
        # 「进聊天记录」，两者受众与副作用完全不同，不能共用一个开关
        # （生产环境常开着 debug 日志排错，但绝不希望 RP 回复里多一行调试信息）。
        self.show_inject_report: bool = _safe_bool(dbg.get("show_inject_report"))

        # ── trace（M1a 注入诊断：ring buffer，纯内存不落盘）──
        tr = _get_nested(self._raw, "trace", {}) or {}
        # 默认开：trace 是零成本采集（就地附带既有信号，不新增执行），
        # 关闭它是排障手段而非常态——与 debug.enabled（默认关，开了刷屏）
        # 方向相反，故默认值不同。
        self.trace_enabled: bool = _safe_bool(tr.get("enabled"), True)
        # 每会话保留轮数（写入侧每轮回写 store，热生效；下限 1）
        self.trace_buffer_per_session: int = max(
            1, _safe_int(tr.get("buffer_per_session", 20), 20)
        )

        # ── permissions ──
        perm = _get_nested(self._raw, "permissions", {}) or {}
        admin_raw = perm.get("admin_users", "") or ""
        if isinstance(admin_raw, str) and admin_raw.strip():
            # 不再在函数内 `import re`：那会让整个 __init__ 里的 re 变成局部名，
            # 上面 plain_platforms 那段先用到 re 就会 UnboundLocalError。
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
            f"wr={'on' if self.wr_enabled else 'off'}, "
            f"debug={'on' if self.debug_enabled else 'off'})"
        )
