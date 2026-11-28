# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M6.1 近重复候选识别：只标记，不自动合并。

高余弦不代表同一事实。「喜欢咖啡」与「不喜欢咖啡」在字符/向量空间可能
非常接近，却语义相反。这里把「候选」与「可合并」分开：

* 规范化文本完全相同 → 视为重复，可跳过写入；
* 余弦达到阈值但存在否定/时态/数值/主客体差异 → 正常写入，并记录
  ``near_duplicate_of`` 供面板对照；
* 其余高相似候选同样不擅自合并，只标近重复。

纯函数、零 astrbot 依赖，便于后台任务与检索链共用。
"""

from __future__ import annotations

import math
import re
import unicodedata

DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.85

_PUNCT_RE = re.compile(r"[\s\u3000,，。.!！?？;；:：、\"'“”‘’()（）\[\]【】]+")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

_NEG_PAIRS = (
    ("不喜欢", "喜欢"), ("不信任", "信任"), ("不是", "是"),
    ("不想", "想"), ("不愿", "愿"), ("没", "有"), ("没有", "有"),
    ("从未", "已经"), ("不会", "会"), ("不能", "能"),
)
_PAST_WORDS = ("曾经", "以前", "过去", "当初", "那时", "昨天", "上次")
_PRESENT_WORDS = ("现在", "目前", "如今", "当前", "今天", "这次")
_PREDICATES = (
    "不信任", "不喜欢", "讨厌", "喜欢", "信任", "爱", "恨", "住在", "来自",
    "属于", "加入", "离开", "答应", "拒绝", "认识", "见过",
)
_TIME_LEAD = (
    r"(?:(?:今天|昨天|前天|上周|上星期|上个月|上月|去年|曾经|以前|过去|"
    r"当初|那时|当时|现在|目前|如今|当前|这次|最近)[，,\s]*)*"
)
_RELATION_RE = re.compile(
    r"^" + _TIME_LEAD
    + r"([\u4e00-\u9fa5A-Za-z0-9_]{1,12}?)"
    + r"(?:很|也|还|都|一直|正在|已经)?"
    + r"(" + "|".join(sorted(_PREDICATES, key=len, reverse=True)) + r")"
    + r"([\u4e00-\u9fa5A-Za-z0-9_]{1,12})$"
)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return _PUNCT_RE.sub("", text)


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / (na * nb)


def _extract_relation(text: str) -> dict | None:
    match = _RELATION_RE.match(text)
    if not match:
        return None
    return {
        "subject": match.group(1),
        "predicate": match.group(2),
        "object": match.group(3),
    }


def contradiction_reason(left: str, right: str) -> str:
    """返回确定性矛盾类别；空串表示未发现矛盾（不是「相同」）。"""
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b or a == b:
        return ""

    for neg, pos in _NEG_PAIRS:
        a_neg, b_neg = neg in a, neg in b
        # P2-1：pos 通常是 neg 的子串（例如「喜欢」在「不喜欢」中），
        # 不能只看「任一侧含 pos」。必须严格一端否定、另一端肯定。
        if a_neg and not b_neg and pos in b:
            return "negation"
        if b_neg and not a_neg and pos in a:
            return "negation"

    a_past, b_past = any(w in a for w in _PAST_WORDS), any(w in b for w in _PAST_WORDS)
    a_now, b_now = any(w in a for w in _PRESENT_WORDS), any(w in b for w in _PRESENT_WORDS)
    if (a_past and b_now) or (b_past and a_now):
        return "tense"

    a_nums = set(_NUM_RE.findall(a))
    b_nums = set(_NUM_RE.findall(b))
    if a_nums and b_nums and a_nums != b_nums:
        return "numeric"

    # P1-12：提取 (subject, predicate, object)，支持前导时间/副词。
    # 分别比较主语、客体、谓词，并识别主客体互换，避免只锚句首主语
    # 导致「Alice喜欢Bob / Alice喜欢Carol」与时间前缀句全漏判。
    ra, rb = _extract_relation(a), _extract_relation(b)
    if ra and rb:
        if (ra["subject"] == rb["subject"]
                and ra["object"] == rb["object"]
                and ra["predicate"] != rb["predicate"]):
            return "predicate"
        if (ra["subject"] == rb["object"]
                and ra["object"] == rb["subject"]
                and ra["predicate"] == rb["predicate"]):
            return "relation_swap"
        if ra["subject"] == rb["subject"] and ra["object"] != rb["object"]:
            return "object"
        if ra["object"] == rb["object"] and ra["subject"] != rb["subject"]:
            return "subject"
    return ""


def classify(summary: str, candidates: list[dict],
             *, threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD) -> dict:
    """对已有检索候选做保守判重。

    返回 ``{"action": "store"|"skip"|"near_duplicate", "candidate": ...,
    "reason": "..."}``。调用方只应在 ``skip`` 时放弃写入；``near_duplicate``
    仍要写入并记录候选 record_id。
    """
    norm = normalize_text(summary)
    threshold = (
        DEFAULT_NEAR_DUPLICATE_THRESHOLD
        if threshold is None
        else float(threshold)
    )
    threshold = max(0.0, min(threshold, 1.0))
    best = None
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        other = str(row.get("summary", "") or "")
        if norm and norm == normalize_text(other):
            return {"action": "skip", "candidate": row, "reason": "exact_duplicate"}
        sim = float(row.get("sim", 0.0) or 0.0)
        if sim < threshold:
            continue
        reason = contradiction_reason(summary, other)
        candidate = {
            "record_id": row.get("record_id"),
            "memory_id": row.get("id"),
            "sim": round(sim, 4),
            "reason": reason or "high_similarity",
        }
        if best is None or sim > float(best.get("sim", 0.0)):
            best = candidate
    if best is not None:
        return {"action": "near_duplicate", "candidate": best,
                "reason": best["reason"]}
    return {"action": "store", "candidate": None, "reason": ""}
