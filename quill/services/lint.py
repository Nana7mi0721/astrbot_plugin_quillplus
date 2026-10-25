# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""世界书/角色卡静态体检器（M1b 模块 5）。

纯本地分析：零模型调用、零 IO（数据由调用方从既有管理器读出后传入），
纯数据入纯数据出（分层落位 quill/services/，零 astrbot 导入）。

五类检查（规划 §四 M1b 模块 5）：

1. ``duplicate_keyword`` —— 相同关键词出现在多条启用条目（同时触发挤占预算）；
2. ``broad_keyword`` —— 单字符关键词（中文单字几乎必然误命中）；以及
   ``empty_keys`` —— 无可用关键词且非常驻的死条目；
3. ``unreachable_entry`` —— 灵敏度不可达：严格/平衡档下多词关键词整串
   永不子串命中（含分隔符的 key 只有宽松档才会按片段拆开）；
4. ``ghost_binding`` —— 角色卡绑定（世界书/素材分类/RAG 文档）指向不存在
   的资源（调用方传入现存名字集合）；
5. ``budget_ratio`` —— 常驻内容（世界书 constant 条目 + 素材常驻）字符量
   占 max_prompt_length 的百分比，超 60% 黄色预警。

返回 finding 列表：``{check, level, worldbook, title, detail}``。
level 仅 ``"warn"``/``"info"``（体检器不产生 error——它不判对错，只报风险）。
"""

from __future__ import annotations

from . import condition as _condition_mod

#: 常驻预算水位线（占比超过即黄色预警；规划 §四 模块 5 第 5 条）
_CONSTANT_BUDGET_WARN_RATIO = 0.6


def _entry_keys(entry: dict) -> list[str]:
    keys = entry.get("keys", [])
    if not isinstance(keys, (list, tuple)):
        keys = [keys]
    return [k for k in keys if isinstance(k, str) and k.strip()]


def lint_worldbooks(worldbooks: list[dict], sensitivity: float = 0.7) -> list[dict]:
    """检查世界书列表（结构同 WorldbookManager.worldbooks 的值列表）。

    ``sensitivity`` 用于第 3 类检查的档位判定（严格 <0.3 / 平衡 0.3-0.7 /
    宽松 >0.7，与 worldbook._min_match_required 同一口径——这里按档位复述，
    不 import worldbook（那会引入 astrbot logger 依赖，破坏本层分层））。
    """
    findings: list[dict] = []

    def _add(check: str, level: str, wb: str, title: str, detail: str) -> None:
        findings.append({
            "check": check, "level": level,
            "worldbook": wb, "title": title, "detail": detail,
        })

    for wb in worldbooks or []:
        wb_name = str(wb.get("name", "") or "?")
        keyword_map: dict[str, list[str]] = {}
        constant_chars = 0
        for entry in wb.get("entries", []) or []:
            title = str(entry.get("title", "") or entry.get("id", "?"))
            if not entry.get("enabled", True):
                continue
            keys = _entry_keys(entry)
            is_constant = bool(entry.get("is_constant", False))

            # 条件语法体检（模块 6 的配套：坏条件永远不成立，作者无感知）
            cond = entry.get("condition")
            if cond and _condition_mod.parse_condition(str(cond)) is None:
                _add("condition_syntax", "warn", wb_name, title,
                     f"condition 字段格式错误，永远不成立：{str(cond)[:60]!r}")

            if is_constant:
                constant_chars += len(str(entry.get("content", "") or ""))
                continue  # 常驻条目不参与关键词检查（它们不看关键词）

            # 2. 死条目 / 高危宽泛词
            if not keys:
                _add("empty_keys", "warn", wb_name, title,
                     "启用且非常驻，但没有任何可用关键词——永远不会被触发")
                continue
            for k in keys:
                if len(k.strip()) <= 1:
                    _add("broad_keyword", "warn", wb_name, title,
                         f"单字符关键词 {k!r}：几乎任意文本都会命中，建议加长或改宽松档")

            # 3. 灵敏度不可达：多词关键词（含空白/分隔符）在严格/平衡档下
            # 只按整串子串匹配——用户输入几乎不可能原样包含整串
            # 「魔法 学院」；宽松档才会拆片段（worldbook._key_segments）。
            if sensitivity is not None and sensitivity <= 0.7:
                multi = [k for k in keys if len(k.split()) > 1 or any(
                    c in k for c in "，,、/|｜"
                )]
                if multi and len(multi) == len(keys):
                    _add("unreachable_entry", "warn", wb_name, title,
                         f"全部关键词为多词整串（如 {multi[0][:20]!r}），当前灵敏度档位"
                         f"（{sensitivity}，{'平衡' if sensitivity >= 0.3 else '严格'}档）"
                         "按整串匹配，实际几乎不可触发；建议拆词或调高灵敏度")

            # 1. 重复关键词（同一本书内跨条目）
            for k in keys:
                keyword_map.setdefault(k.lower(), []).append(title)

        for kw, titles in keyword_map.items():
            if len(titles) > 1:
                _add("duplicate_keyword", "warn", wb_name, titles[0],
                     f"关键词 {kw[:20]!r} 同时出现在 {len(titles)} 条启用条目"
                     f"（{'、'.join(titles[:3])}{'…' if len(titles) > 3 else ''}）"
                     "——同时触发会挤占注入预算")

    return findings


def lint_persona_bindings(persona: dict, *,
                          worldbook_names: set[str],
                          wr_categories: set[str],
                          rag_sources: set[str]) -> list[dict]:
    """检查角色卡的幽灵绑定（规划第 4 类）。

    ``persona`` 为 persona_manager 的角色卡 dict；三个名字集合由调用方从
    各管理器取现存值。绑定合法（auto 模式 / 名字存在）的条目不产生 finding。
    """
    findings: list[dict] = []
    ext = (persona or {}).get("quill_extensions", {}) or {}
    pid = str((persona or {}).get("id", "") or (persona or {}).get("name", "?"))

    def _add(check: str, detail: str) -> None:
        findings.append({"check": check, "level": "warn",
                         "worldbook": "", "title": pid, "detail": detail})

    if ext.get("wb_mode") == "custom":
        for name in ext.get("bound_worldbooks", []) or []:
            if name not in worldbook_names:
                _add("ghost_binding", f"绑定世界书 {name!r} 不存在（可能已删除），该绑定静默失效")
    if ext.get("wr_mode") == "custom":
        for cat in ext.get("bound_writing_resource", []) or []:
            if cat not in wr_categories:
                _add("ghost_binding", f"绑定素材分类 {cat!r} 不存在，该分类下将无素材注入")
    if ext.get("rag_mode") == "custom":
        for src in ext.get("bound_rag_docs", []) or []:
            # grp: 分组绑定由运行期展开，静态侧只查非分组项
            if not str(src).startswith("grp:") and str(src) not in rag_sources:
                _add("ghost_binding", f"绑定 RAG 文档 {src!r} 不存在（kb: 前缀的内置库绑定请到内置库侧核对）")
    return findings


def lint_constant_budget(worldbooks: list[dict], wr_constant_chars: int,
                         max_prompt_length: int,
                         persona_bindings: list[tuple] | None = None) -> list[dict]:
    """常驻预算水位检查（规划第 5 类）。

    ``wr_constant_chars``：素材库常驻条目总字符（调用方从 kb 侧汇总；
    素材常驻走同一 system prompt）。占比 = 常驻字符 / max_prompt_length，
    超 60% 黄色预警——常驻先吃预算，动态命中只能用剩下的。

    M1b-Fix Bug5（核验报告 §9.2#5）：``persona_bindings`` 传入
    ``[(角色卡名, 绑定书名列表 | None=auto 全量 | []=disabled 不注入,
    core_prompts 字符量)]`` 时改按**单角色卡峰值**计水位——任何一轮注入
    只会装「该卡绑定的书 + 该卡 core_prompts + 素材常驻」，全部书加总
    对多卡用户是虚高报警。峰值 = max(各卡的上述三项之和)；无角色卡数据
    时退回全量加总（auto 模式本来就全量注入，口径仍真实）。
    """
    wb_chars_map: dict[str, int] = {}
    total_all = 0
    for wb in worldbooks or []:
        name = str(wb.get("name", "") or "?")
        chars = 0
        for entry in wb.get("entries", []) or []:
            if entry.get("enabled", True) and entry.get("is_constant", False):
                chars += len(str(entry.get("content", "") or ""))
        wb_chars_map[name] = wb_chars_map.get(name, 0) + chars
        total_all += chars
    wr = int(wr_constant_chars or 0)

    peak_label = None
    total = total_all + wr
    if persona_bindings:
        peak = -1
        for label, books, core_chars in persona_bindings:
            if books is None:          # auto：全量书
                wb_side = total_all
            else:                       # custom：绑定书；disabled：空集
                wb_side = sum(wb_chars_map.get(str(b), 0) for b in (books or []))
            cand = wb_side + int(core_chars or 0) + wr
            if cand > peak:
                peak, peak_label = cand, str(label)
        total = peak if peak >= 0 else total
    budget = int(max_prompt_length or 0)
    if budget <= 0 or total <= 0:
        return []
    basis = (f"单角色卡峰值（「{peak_label}」：绑定世界书常驻 + 卡内提示词 + 素材常驻）"
             if peak_label is not None else "全部世界书常驻加总（无角色卡数据，auto 口径）")
    ratio = total / budget
    if ratio > _CONSTANT_BUDGET_WARN_RATIO:
        return [{
            "check": "budget_ratio", "level": "warn",
            "worldbook": "", "title": "常驻内容水位",
            "detail": f"常驻内容 {total} 字符（{basis}），占 max_prompt_length"
                      f"（{budget}）的 {ratio:.0%}——超过 {_CONSTANT_BUDGET_WARN_RATIO:.0%} 水位线，"
                      "该角色卡下动态命中（世界书关键词/素材/RAG）只剩少量预算，建议精简常驻条目",
        }]
    return [{
        "check": "budget_ratio", "level": "info",
        "worldbook": "", "title": "常驻内容水位",
        "detail": f"常驻内容 {total} 字符（{basis}），占预算 {ratio:.0%}——水位健康",
    }]
