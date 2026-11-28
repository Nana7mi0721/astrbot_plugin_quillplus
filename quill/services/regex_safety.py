# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe regex subset (M5.3).

Python's re has no per-match timeout; asyncio cannot kill a backtracking
thread. This module statically validates a conservative subset using the
regex parse tree (re._parser / sre_parse), rejecting nested quantifiers,
branches under quantified repeats, backreferences, lookaround, atomic groups
and too many risky repeats in one branch. Large bounded repeats and adjacent
overlapping repeats are treated as backtracking hazards too. Non-nested lazy
quantifiers such as .*?  a+?  a{1,3}? and one-shot ? groups are allowed. This
is intentionally conservative, but mutually exclusive top-level branches must
not consume each other's quantifier budget.
"""

from __future__ import annotations

import re

try:
    from re import _parser as _sre_parse
except ImportError:
    try:
        import sre_parse as _sre_parse
    except ImportError:
        _sre_parse = None


MAX_PATTERN_CHARS = 500
MAX_SCAN_CHARS = 20000
MAX_TOP_LEVEL_UNBOUNDED = 2
_RISKY_REPEAT_HI = 20
_RISKY_REPEAT_SPAN = 10

_FORBIDDEN_GROUPS = (
    "(?=", "(?!", "(?<=", "(?<!", "(?>", "(?#",
    "(?(", "(?P=", "(?P>", "(?<",
)
_BACKREF_RE = re.compile(r"\\[1-9]")


def _fold_codepoint(codepoint):
    if 65 <= int(codepoint) <= 90:
        return int(codepoint) + 32
    return int(codepoint)


_SAMPLE_CODEPOINTS = frozenset(
    _fold_codepoint(ord(char))
    for char in (
        "".join(chr(code) for code in range(128))
        + "你好中文测试αβγδεζηθАБВабв"
        + "٠١٢٣٤٥٦٧٨٩éÉñÑßÜü日本語한국어😀"
        + "\u00a0\u2003\u3000"
    )
)
_EMPTY_CHARS = ((), frozenset())
_ALL_CHARS = (((0, 0x10FFFF),), _SAMPLE_CODEPOINTS)


def _find_forbidden_group(pattern):
    """返回真正的禁用组前缀；字符类和转义文本里的 ``(?=`` 等不算。

    P2-11：此前用裸子串扫描，`[(?=]` 这类合法字符类会被误杀。这里只在
    组起始位置识别，并跳过字符类与反斜杠转义。
    """
    index = 0
    in_class = False
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            # Python 允许 `[]]` 用首位的 ] 表示字面量。
            if index < length and pattern[index] == "^":
                index += 1
            if index < length and pattern[index] == "]":
                index += 1
            continue
        if char == "(":
            for token in _FORBIDDEN_GROUPS:
                if pattern.startswith(token, index):
                    return token
        index += 1
    return ""


def _fold_interval(span):
    """把区间中的 ASCII 大写折叠为小写，供 IGNORECASE 首字符比较。"""
    low, high = int(span[0]), int(span[1])
    pieces = []
    if low < 65:
        pieces.append((low, min(high, 64)))
    if low <= 90 and high >= 65:
        pieces.append((max(low, 65) + 32, min(high, 90) + 32))
    if high > 90:
        pieces.append((max(low, 91), high))
    return pieces


def _merge_intervals(intervals):
    cleaned = []
    for low, high in intervals:
        if low <= high:
            cleaned.append((int(low), int(high)))
    if not cleaned:
        return ()
    cleaned.sort()
    merged = [cleaned[0]]
    for low, high in cleaned[1:]:
        prev_low, prev_high = merged[-1]
        if low <= prev_high + 1:
            merged[-1] = (prev_low, max(prev_high, high))
        else:
            merged.append((low, high))
    return tuple(merged)


def _chars_union(left, right):
    """首字符集合：(显式区间, 常见字符样本) 的并集。"""
    return (
        _merge_intervals(tuple(left[0]) + tuple(right[0])),
        frozenset(left[1]) | frozenset(right[1]),
    )


def _chars_intersect(left, right):
    left_intervals, left_samples = left
    right_intervals, right_samples = right
    left_index = right_index = 0
    while left_index < len(left_intervals) and right_index < len(right_intervals):
        left_low, left_high = left_intervals[left_index]
        right_low, right_high = right_intervals[right_index]
        if left_high < right_low:
            left_index += 1
        elif right_high < left_low:
            right_index += 1
        else:
            return True
    if left_samples & right_samples:
        return True
    for codepoint in left_samples:
        for low, high in right_intervals:
            if low <= codepoint <= high:
                return True
    for codepoint in right_samples:
        for low, high in left_intervals:
            if low <= codepoint <= high:
                return True
    return False


def _fallback_validate(pattern):
    if _BACKREF_RE.search(pattern):
        return "regex_backreference"
    if re.search(r"\)[+*{]", pattern) and re.search(r"[+*}]", pattern[:pattern.rfind(")")]):
        return "regex_nested_quantifier"
    if len(re.findall(r"(?<!\\)[+*]", pattern)) > MAX_TOP_LEVEL_UNBOUNDED:
        return "regex_unbounded_quantifier_limit"
    return None


def _ast_validate(pattern):
    if _sre_parse is None:
        return _fallback_validate(pattern)
    parser = getattr(_sre_parse, "parse")
    try:
        parsed = parser(pattern, re.IGNORECASE)
    except Exception:
        return "regex_syntax"

    forbidden = {
        getattr(_sre_parse, name)
        for name in ("ASSERT", "ASSERT_NOT", "GROUPREF", "GROUPREF_EXISTS", "ATOMIC_GROUP")
        if hasattr(_sre_parse, name)
    }
    repeat_ops = {
        getattr(_sre_parse, name)
        for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
        if hasattr(_sre_parse, name)
    }
    branch_op = getattr(_sre_parse, "BRANCH", None)
    subpattern_op = getattr(_sre_parse, "SUBPATTERN", None)
    maxrepeat = getattr(_sre_parse, "MAXREPEAT", None)
    literal_op = getattr(_sre_parse, "LITERAL", None)
    in_op = getattr(_sre_parse, "IN", None)
    negate_op = getattr(_sre_parse, "NEGATE", None)
    range_op = getattr(_sre_parse, "RANGE", None)
    category_op = getattr(_sre_parse, "CATEGORY", None)
    any_op = getattr(_sre_parse, "ANY", None)
    any_all_op = getattr(_sre_parse, "ANY_ALL", None)
    not_literal_op = getattr(_sre_parse, "NOT_LITERAL", None)
    at_op = getattr(_sre_parse, "AT", None)
    category_word = getattr(_sre_parse, "CATEGORY_WORD", None)
    category_not_word = getattr(_sre_parse, "CATEGORY_NOT_WORD", None)
    category_digit = getattr(_sre_parse, "CATEGORY_DIGIT", None)
    category_not_digit = getattr(_sre_parse, "CATEGORY_NOT_DIGIT", None)
    category_space = getattr(_sre_parse, "CATEGORY_SPACE", None)
    category_not_space = getattr(_sre_parse, "CATEGORY_NOT_SPACE", None)

    def is_unbounded(hi):
        return maxrepeat is not None and hi == maxrepeat

    def repeat_can_loop(hi):
        if is_unbounded(hi):
            return True
        try:
            return int(hi) > 1
        except (TypeError, ValueError):
            return True

    def is_expensive_repeat(lo, hi):
        if is_unbounded(hi):
            return True
        try:
            low, high = int(lo), int(hi)
        except (TypeError, ValueError):
            return True
        return high > _RISKY_REPEAT_HI or (high - low) > _RISKY_REPEAT_SPAN

    def category_matches(category, codepoint):
        char = chr(codepoint)
        if category is category_word:
            return char.isalnum() or char == "_"
        if category is category_not_word:
            return not (char.isalnum() or char == "_")
        if category is category_digit:
            return char.isdecimal()
        if category is category_not_digit:
            return not char.isdecimal()
        if category is category_space:
            return char.isspace()
        if category is category_not_space:
            return not char.isspace()
        # 未知类别保守视为任意字符，避免静默漏掉新的 Python 类别。
        return True

    def category_chars(category):
        return (
            (),
            frozenset(
                codepoint for codepoint in _SAMPLE_CODEPOINTS
                if category_matches(category, codepoint)
            ),
        )

    def item_matches(item_op, item_av, codepoint):
        if literal_op is not None and item_op is literal_op:
            return _fold_codepoint(item_av) == codepoint
        if range_op is not None and item_op is range_op:
            low, high = item_av
            return any(
                low_fold <= codepoint <= high_fold
                for low_fold, high_fold in _fold_interval((low, high))
            )
        if category_op is not None and item_op is category_op:
            return category_matches(item_av, codepoint)
        return True

    def item_intervals(item_op, item_av):
        if literal_op is not None and item_op is literal_op:
            codepoint = _fold_codepoint(item_av)
            return [(codepoint, codepoint)]
        if range_op is not None and item_op is range_op:
            return list(_fold_interval(item_av))
        return []

    def token_first(op, av):
        """返回 ((显式区间, 常见字符样本), 是否可匹配空串)。"""
        if literal_op is not None and op is literal_op:
            codepoint = _fold_codepoint(av)
            return (((codepoint, codepoint),), frozenset((codepoint,))), False
        if in_op is not None and op is in_op:
            negated = any(
                negate_op is not None and item_op is negate_op
                for item_op, _ in av
            )
            intervals = []
            matched = set()
            unknown_item = False
            for item_op, item_av in av:
                if negate_op is not None and item_op is negate_op:
                    continue
                if not (
                    (literal_op is not None and item_op is literal_op)
                    or (range_op is not None and item_op is range_op)
                    or (category_op is not None and item_op is category_op)
                ):
                    unknown_item = True
                    continue
                intervals.extend(item_intervals(item_op, item_av))
                matched.update(
                    codepoint for codepoint in _SAMPLE_CODEPOINTS
                    if item_matches(item_op, item_av, codepoint)
                )
            if unknown_item:
                return _ALL_CHARS, False
            if negated:
                return (
                    (),
                    frozenset(_SAMPLE_CODEPOINTS - matched),
                ), False
            return (_merge_intervals(intervals), frozenset(matched)), False
        if (
            (any_op is not None and op is any_op)
            or (any_all_op is not None and op is any_all_op)
        ):
            return _ALL_CHARS, False
        if category_op is not None and op is category_op:
            return category_chars(av), False
        if not_literal_op is not None and op is not_literal_op:
            excluded = _fold_codepoint(av)
            return (
                (),
                frozenset(cp for cp in _SAMPLE_CODEPOINTS if cp != excluded),
            ), False
        if op in repeat_ops:
            _lo, _hi, body = av
            first, body_nullable = sequence_first(body)
            return first, int(_lo) == 0 or body_nullable
        if subpattern_op is not None and op is subpattern_op:
            return sequence_first(av[-1])
        if branch_op is not None and op is branch_op:
            first = _EMPTY_CHARS
            nullable = False
            for branch in av[1]:
                branch_first, branch_nullable = sequence_first(branch)
                first = _chars_union(first, branch_first)
                nullable = nullable or branch_nullable
            return first, nullable
        if at_op is not None and op is at_op:
            return _EMPTY_CHARS, True
        # 未知/零宽操作按可消费处理，交给 forbidden 检查兜底。
        return _ALL_CHARS, False

    def sequence_first(tokens):
        first = _EMPTY_CHARS
        for op, av in tokens:
            token_chars, nullable = token_first(op, av)
            first = _chars_union(first, token_chars)
            if not nullable:
                return first, False
        return first, True

    def repeat_descriptor(body):
        first, _nullable = sequence_first(body)
        return (first,)

    def descriptors_overlap(left, right):
        for left_desc in left:
            for right_desc in right:
                if _chars_intersect(left_desc[0], right_desc[0]):
                    return True
        return False

    def sequence_edges(tokens):
        """返回首尾风险量词，用于识别相邻/可重叠的回溯源。

        量词或组之间允许存在零宽/可空节点：只要中间部分能被跳过，左右两个
        风险量词就仍可能落在同一段文本上，因此保留左侧 tail 继续比较。
        """
        lead = []
        trail = []
        has_content = False
        opening = True
        nullable = True
        hazard = False
        for op, av in tokens:
            token_lead, token_trail, token_content, token_nullable, token_hazard = (
                token_edges(op, av)
            )
            hazard = hazard or token_hazard
            if token_content:
                has_content = True
                if opening:
                    # 可空前缀不影响「下一个风险量词能否落在组首」，
                    # 例如 `(?:b?a+)` 的前导 b? 可跳过，a+ 仍可能贴住组前。
                    lead.extend(token_lead)
                if trail and token_lead and descriptors_overlap(trail, token_lead):
                    hazard = True
                if token_nullable:
                    trail = list(trail) + list(token_trail)
                else:
                    trail = list(token_trail)
                    opening = False
            nullable = nullable and token_nullable
        return lead, trail, has_content, nullable, hazard

    def token_edges(op, av):
        if op in repeat_ops:
            low, hi, body = av
            body_lead, body_trail, body_content, body_nullable, body_hazard = (
                sequence_edges(body)
            )
            nullable = int(low) == 0 or body_nullable
            if is_expensive_repeat(low, hi):
                descriptor = repeat_descriptor(body)
                return [descriptor], [descriptor], body_content, nullable, body_hazard
            if not repeat_can_loop(hi):
                # `?` 只执行一次，组内风险量词仍可能与组外量词相连，但
                # 不应把组本身再算作一个可重复回溯源。
                return body_lead, body_trail, body_content, nullable, body_hazard
            return [], [], body_content, nullable, body_hazard
        if subpattern_op is not None and op is subpattern_op:
            return sequence_edges(av[-1])
        if branch_op is not None and op is branch_op:
            lead = []
            trail = []
            has_content = False
            nullable = False
            hazard = False
            for branch in av[1]:
                branch_lead, branch_trail, branch_content, branch_nullable, branch_hazard = (
                    sequence_edges(branch)
                )
                lead.extend(branch_lead)
                trail.extend(branch_trail)
                has_content = has_content or branch_content
                nullable = nullable or branch_nullable
                hazard = hazard or branch_hazard
            return lead, trail, has_content, nullable, hazard
        if at_op is not None and op is at_op:
            return [], [], False, True, False
        return [], [], True, False, False

    def walk(tokens, in_repeat, risk_count):
        _lead, _trail, _content, _nullable, hazard = sequence_edges(tokens)
        if hazard:
            return "regex_unbounded_quantifier_limit", risk_count
        for op, av in tokens:
            if op in forbidden:
                return "regex_forbidden_construct", risk_count
            if op in repeat_ops:
                if in_repeat:
                    return "regex_nested_quantifier", risk_count
                low, hi, body = av
                if is_expensive_repeat(low, hi):
                    risk_count += 1
                    if risk_count > MAX_TOP_LEVEL_UNBOUNDED:
                        return "regex_unbounded_quantifier_limit", risk_count
                reason, risk_count = walk(
                    body, repeat_can_loop(hi), risk_count
                )
                if reason:
                    return reason, risk_count
            elif subpattern_op is not None and op is subpattern_op:
                reason, risk_count = walk(av[-1], in_repeat, risk_count)
                if reason:
                    return reason, risk_count
            elif branch_op is not None and op is branch_op:
                # 量词内部出现分支（含被 SUBPATTERN 包裹的分支）会产生
                # 多项式回溯，例如 `(a|a)+` / `(?:x|y)+`。顶层分支彼此
                # 互斥，必须各自独立计数，不能共享预算误杀。
                if in_repeat:
                    return "regex_nested_quantifier", risk_count
                branch_count = risk_count
                for branch in av[1]:
                    reason, current_count = walk(branch, in_repeat, risk_count)
                    if reason:
                        return reason, current_count
                    branch_count = max(branch_count, current_count)
                risk_count = branch_count
        return None, risk_count

    reason, _risk_count = walk(parsed, False, 0)
    return reason


def validate_pattern(pattern):
    if not isinstance(pattern, str):
        return "regex_not_string"
    if not pattern:
        return "regex_empty"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"regex_too_long({len(pattern)}>{MAX_PATTERN_CHARS})"
    token = _find_forbidden_group(pattern)
    if token:
        return f"regex_forbidden_construct({token})"
    reason = _ast_validate(pattern)
    if reason:
        return reason
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"regex_syntax({exc.msg})"
    return None


def search(pattern, text):
    reason = validate_pattern(pattern)
    if reason:
        return False, reason
    try:
        return bool(re.search(pattern, (text or "")[:MAX_SCAN_CHARS], re.IGNORECASE)), None
    except re.error as exc:
        return False, f"regex_syntax({exc.msg})"
