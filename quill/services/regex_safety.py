# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""安全正则子集（M5.3）。

Python 标准库 ``re`` 没有单次匹配超时。``asyncio.wait_for(to_thread(...))``
只能放弃等待，无法终止灾难回溯线程，因此不能把用户/角色卡提供的正则直接
交给 ``re.search``。本模块先做静态校验，只放行可证明不会产生指数级回溯的
子集；校验失败时调用方降级为「不执行」并把原因写进导入/体检报告。

约束：
* 模式长度 <= 500；
* 待扫描文本最多取前 20000 字符；
* 禁止反向引用、lookaround、条件组、原子组与注释组；
* 禁止「带量词的捕获组再被量词修饰」的嵌套量词（如 ``(a+)+``）；
* 禁止连续量词与零宽量词组合（如 ``a**``、``a+?+``）。

这是保守子集，不是完整正则兼容层：被拒的模式可以用多个普通关键词替代。
"""

from __future__ import annotations

import re

MAX_PATTERN_CHARS = 500
MAX_SCAN_CHARS = 20_000

_FORBIDDEN_GROUPS = (
    "(?=", "(?!", "(?<=", "(?<!", "(?>", "(?#",
    "(?(", "(?P=", "(?P>", "(?<",
)
_BACKREF_RE = re.compile(r"\\[1-9]")
_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:[^()\\]|\\.)*[+*?}](?:[^()\\]|\\.)*\)[+*?{]"
)
_REPEATED_QUANTIFIER_RE = re.compile(r"[+*?}][+*?]|\{[^}]*\}[+*?]")


def validate_pattern(pattern: str) -> str | None:
    """返回拒绝原因；``None`` 表示模式在安全子集内。"""
    if not isinstance(pattern, str):
        return "regex_not_string"
    if not pattern:
        return "regex_empty"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"regex_too_long({len(pattern)}>{MAX_PATTERN_CHARS})"
    for token in _FORBIDDEN_GROUPS:
        if token in pattern:
            return f"regex_forbidden_construct({token})"
    if _BACKREF_RE.search(pattern):
        return "regex_backreference"
    if _NESTED_QUANTIFIER_RE.search(pattern):
        return "regex_nested_quantifier"
    if _REPEATED_QUANTIFIER_RE.search(pattern):
        return "regex_repeated_quantifier"
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"regex_syntax({exc.msg})"
    return None


def search(pattern: str, text: str) -> tuple[bool, str | None]:
    """安全搜索；返回 ``(是否命中, 拒绝原因)``。

    拒绝原因非空时调用方应把该条目视为未命中，并把原因写入诊断。
    """
    reason = validate_pattern(pattern)
    if reason:
        return False, reason
    try:
        return bool(re.search(pattern, (text or "")[:MAX_SCAN_CHARS], re.IGNORECASE)), None
    except re.error as exc:
        return False, f"regex_syntax({exc.msg})"
