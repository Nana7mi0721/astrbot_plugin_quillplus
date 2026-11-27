# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe regex subset (M5.3).

Python's re has no per-match timeout; asyncio cannot kill a backtracking
thread. This module statically validates a conservative subset using the
regex parse tree (re._parser / sre_parse), rejecting nested quantifiers,
branches under quantifiers, backreferences, lookaround, atomic groups and
too many top-level unbounded quantifiers. Non-nested lazy quantifiers such as
.*?  a+?  a{1,3}? are allowed. This is intentionally conservative.
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

_FORBIDDEN_GROUPS = (
    "(?=", "(?!", "(?<=", "(?<!", "(?>", "(?#",
    "(?(", "(?P=", "(?P>", "(?<",
)
_BACKREF_RE = re.compile(r"\\[1-9]")


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

    state = {"unbounded": 0}

    def walk(tokens, in_repeat):
        for op, av in tokens:
            if op in forbidden:
                return "regex_forbidden_construct"
            if op in repeat_ops:
                if in_repeat:
                    return "regex_nested_quantifier"
                _lo, hi, body = av
                if branch_op is not None and any(bop is branch_op for bop, _ in body):
                    return "regex_nested_quantifier"
                if maxrepeat is not None and hi == maxrepeat:
                    state["unbounded"] += 1
                    if state["unbounded"] > MAX_TOP_LEVEL_UNBOUNDED:
                        return "regex_unbounded_quantifier_limit"
                reason = walk(body, True)
                if reason:
                    return reason
            elif op is subpattern_op:
                reason = walk(av[-1], in_repeat)
                if reason:
                    return reason
            elif branch_op is not None and op is branch_op:
                # 量词内部出现分支（含被 SUBPATTERN 包裹的分支）会产生
                # 多项式回溯，例如 `(a|a)+` / `(?:x|y)+`。
                if in_repeat:
                    return "regex_nested_quantifier"
                _flags, branches = av
                for branch in branches:
                    reason = walk(branch, in_repeat)
                    if reason:
                        return reason
        return None

    return walk(parsed, False)


def validate_pattern(pattern):
    if not isinstance(pattern, str):
        return "regex_not_string"
    if not pattern:
        return "regex_empty"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"regex_too_long({len(pattern)}>{MAX_PATTERN_CHARS})"
    for token in _FORBIDDEN_GROUPS:
        if token in pattern:
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
