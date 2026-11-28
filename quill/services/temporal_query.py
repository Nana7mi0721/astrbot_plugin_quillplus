# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M6.3 时间/人物/事件查询解析（纯服务层）。

现实相对时间用标准库日历精确计算；剧情时间只保留原文线索，不把「剧情
昨天」伪造成公历日期。检索侧先按 session/status 过滤，再做加权，不允许
先全局召回再过滤权限。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

_REL_RE = re.compile(
    r"(今天|昨天|前天|上周|上星期|上个月|上月|去年|最近(?:\d+|[一二两三四五六七八九十]+)天)"
)
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_STORY_MARK = re.compile(r"剧情(?:里|中|上)?|故事(?:里|中)?|上次|那次|当时|之前那")
_STORY_TIME_RE = re.compile(
    r"(剧情(?:里|中|上)?|故事(?:里|中)?)?\s*"
    r"(今天|昨天|前天|上周|上星期|上个月|去年|清晨|上午|中午|下午|傍晚|晚上|深夜|凌晨)"
)
_STOP = {
    "什么", "怎么", "为什么", "我们", "你们", "他们", "这个", "那个",
    "一下", "现在", "目前", "还记得", "说过", "提起", "关于", "事情",
    "剧情", "故事", "上次", "那次", "当时", "之前",
}


def _now(now=None) -> datetime:
    value = now or datetime.now().astimezone()
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value


def _day_start(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _cn_int(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if len(raw) == 2 and raw[0] == "十":
        return 10 + _CN_NUM.get(raw[1], 0)
    if len(raw) == 2 and raw[1] == "十":
        return _CN_NUM.get(raw[0], 0) * 10
    if len(raw) == 3 and raw[1] == "十":
        return _CN_NUM.get(raw[0], 0) * 10 + _CN_NUM.get(raw[2], 0)
    return 0


def _real_range(token: str, now: datetime) -> tuple[datetime, datetime] | None:
    start_today = _day_start(now)
    if token == "今天":
        return start_today, start_today + timedelta(days=1)
    if token == "昨天":
        return start_today - timedelta(days=1), start_today
    if token == "前天":
        return start_today - timedelta(days=2), start_today - timedelta(days=1)
    if token in ("上周", "上星期"):
        monday = start_today - timedelta(days=start_today.weekday())
        return monday - timedelta(days=7), monday
    if token in ("上个月", "上月"):
        first = start_today.replace(day=1)
        prev_last = first - timedelta(days=1)
        prev_first = prev_last.replace(day=1)
        return prev_first, first
    if token == "去年":
        start = start_today.replace(month=1, day=1) - timedelta(days=1)
        first = start.replace(month=1, day=1)
        return first, first.replace(year=first.year + 1)
    match = re.fullmatch(r"最近(\d+|[一二两三四五六七八九十]+)天", token)
    if match:
        days = max(1, _cn_int(match.group(1)))
        return start_today - timedelta(days=days - 1), start_today + timedelta(days=1)
    return None


def _entities(text: str) -> list[str]:
    cleaned = _REL_RE.sub(" ", text or "")
    cleaned = _STORY_MARK.sub(" ", cleaned)
    # 引号内专名优先，再从连续中文片段取 2-6 字候选。
    quoted = re.findall(r"[“\"'「『]([^”\"'」』]{1,12})[”\"'」』]", cleaned)
    chunks = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,6}", cleaned)
    terms: list[str] = []
    for item in quoted + chunks:
        term = item.strip()
        if not term or term in _STOP or any(term in stop for stop in _STOP):
            continue
        if term not in terms:
            terms.append(term)
    return terms[:8]


def analyze(text: str, *, now=None) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {"kind": "none", "entities": [], "keywords": ""}
    current = _now(now)
    story = bool(_STORY_MARK.search(raw) or "剧情" in raw)
    story_match = _STORY_TIME_RE.search(raw)
    if story:
        story_expr = ""
        if story_match:
            story_expr = (story_match.group(0) or "").strip()
        return {
            "kind": "story",
            "story_expr": story_expr,
            "story_tokens": [t for t in (story_match.groups() if story_match else ()) if t],
            "entities": _entities(raw),
            "keywords": raw,
        }
    match = _REL_RE.search(raw)
    if match:
        span = _real_range(match.group(1), current)
        if span:
            return {
                "kind": "real",
                "token": match.group(1),
                "start": span[0].date().isoformat(),
                "end": span[1].date().isoformat(),
                "start_ts": span[0].isoformat(),
                "end_ts": span[1].isoformat(),
                "entities": _entities(raw),
                "keywords": raw,
            }
    return {"kind": "none", "entities": _entities(raw), "keywords": raw}


def requires_older_window(analysis: dict, *, now=None) -> bool:
    """现实时间查询是否越过常规 30 天活跃窗口。"""
    if not isinstance(analysis, dict) or analysis.get("kind") != "real":
        return False
    if analysis.get("token") in ("上个月", "上月", "去年"):
        return True
    start = _parse_date(analysis.get("start_ts") or "")
    if start is None:
        return False
    return start < _now(now) - timedelta(days=30)


def extract_story_time(text: str) -> str:
    """从摘要/对话原文提取剧情时间表达式；没有线索时返回空串。"""
    raw = str(text or "")
    # 单纯「今天/昨天」属于现实时间，不能写成 story_time 冒充剧情内日期。
    if not _STORY_MARK.search(raw) and "剧情" not in raw:
        return ""
    match = _STORY_TIME_RE.search(raw)
    return (match.group(0) or "").strip() if match else ""


def extract_participants(text: str) -> list[str]:
    """保守抽取参与人物候选；只用于过滤/加权，不当作事实写入。"""
    raw = str(text or "")
    names = re.findall(r"[“\"'「『]([^”\"'」』]{1,12})[”\"'」』]", raw)
    names += re.findall(r"([\u4e00-\u9fa5]{2,4})(?=说|问|笑|哭|答|喊|道)", raw)
    out = []
    for name in names:
        name = name.strip()
        if name and name not in out and name not in _STOP:
            out.append(name)
    return out[:12]


def _parse_date(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # 记忆/日志时间戳来自 SQLite CURRENT_TIMESTAMP，是不带时区的 UTC。
        # 这里统一补 UTC，避免按本地时区理解后与「昨天」边界错开数小时。
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(value[:len(fmt) + 4], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _as_aware(value: datetime, reference: datetime):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def event_time_from_log(value: str) -> str | None:
    """把 SQLite 日志时间转换为带时区的 ISO-8601 事件时间。"""
    parsed = _parse_date(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def relevance(memory: dict, analysis: dict, now=None) -> float:
    current = _now(now)
    kind = analysis.get("kind")
    boost = 0.0
    if kind == "real":
        event = _parse_date(memory.get("event_time") or "")
        source = event or _parse_date(memory.get("timestamp") or "")
        if source is not None:
            source = _as_aware(source, current)
            start = _parse_date(analysis.get("start_ts") or "")
            end = _parse_date(analysis.get("end_ts") or "")
            if start and end:
                start = _as_aware(start, current)
                end = _as_aware(end, current)
                if start <= source < end:
                    boost += 0.004
                else:
                    # 明确时间窗外的候选保留（可能是矛盾/前情），但降低排序。
                    boost -= 0.002
    elif kind == "story":
        story_text = str(memory.get("story_time") or "")
        haystack = story_text + " " + str(memory.get("summary") or "")
        if analysis.get("story_expr") and analysis["story_expr"] in haystack:
            boost += 0.003
        for token in analysis.get("story_tokens") or []:
            if token and token in haystack:
                boost += 0.0008
    participants = []
    try:
        raw_participants = json.loads(memory.get("participants_json") or "[]")
        if isinstance(raw_participants, list):
            participants = [str(item) for item in raw_participants if str(item).strip()]
    except (TypeError, ValueError):
        participants = []
    haystack = (
        str(memory.get("summary") or "")
        + str(memory.get("chat_summary") or "")
        + " ".join(participants)
    )
    for term in analysis.get("entities") or []:
        if term and term in haystack:
            boost += 0.001
    # P2-4：RRF 单项上限约 1/61≈0.0164，两路融合通常 ~0.03；
    # 时间/人物加权必须留在同一量纲，否则会完全淹没语义排序。
    return max(-0.004, min(boost, 0.006))


def rerank(results: list[dict], analysis: dict, top_k: int, *, now=None,
           funnel: dict | None = None) -> list[dict]:
    if not results or analysis.get("kind") == "none" and not analysis.get("entities"):
        kept = results[:top_k]
        if funnel is not None:
            funnel["candidates"] = max(int(funnel.get("candidates", 0) or 0), len(results))
            funnel["matched"] = len(kept)
        return kept
    ranked = []
    for row in results:
        item = dict(row)
        item["temporal_boost"] = round(relevance(item, analysis, now=now), 4)
        item["temporal_score"] = float(item.get("rrf_score", 0.0) or 0.0) + item["temporal_boost"]
        ranked.append(item)
    ranked.sort(key=lambda r: (
        -float(r.get("temporal_score", 0.0) or 0.0),
        -float(r.get("rrf_score", 0.0) or 0.0),
        str(r.get("id", "")),
    ))
    limit = max(1, int(top_k or 1))
    kept = ranked[:limit]
    if funnel is not None:
        # P2-5：memory_store 写的是 RRF 截断前漏斗；这里按时间重排再次
        # 截断后回填真实 matched 与近失，面板才能解释「检索有候选但没注入」。
        funnel["candidates"] = max(int(funnel.get("candidates", 0) or 0), len(ranked))
        funnel["matched"] = len(kept)
        filtered = funnel.get("filtered")
        if not isinstance(filtered, list):
            filtered = []
            funnel["filtered"] = filtered
        for rank, row in enumerate(ranked[limit:], start=limit + 1):
            filtered.append({
                "reason": (
                    "temporal_below_top_k("
                    f"名次{rank}, temporal {float(row.get('temporal_score', 0.0) or 0.0):.4f})"
                ),
                "rank": rank,
                **({"id": row["id"]} if row.get("id") is not None else {}),
            })
    return kept
