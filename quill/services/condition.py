# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""世界书条目条件解锁的安全求值器（M1b 模块 6，规划 §六 点子 A）。

条目可选 ``condition`` 字段（如 ``好感度>=70``、``剧情阶段==约会``）：关键词
命中之外还要条件成立才注入；``is_constant`` 常驻条目同样支持（变成「条件型
常驻」——好感度门槛类剧情锁的天然形态）。``session_vars`` 直接复用步 14 组装
extra_info 时已有的会话变量快照（状态栏解析产物 + 工具写入的用户变量）。

安全模型（红线：绝不 eval/exec/compile）：

* 表达式必须是 ``变量名 比较符 字面量`` 三段式；
* 比较符白名单 ``>= > <= < == !=``（长符先试，防 ``>=`` 被拆成 ``>`` + ``=``），
  另有集合成员判定 ``变量 in 值1,值2``（M1b-Fix P1-d：``in`` 是日常语，
  但 ``min>=3`` 这类变量名含 "in" 子串——符号比较符先行、``in`` 走独立
  的锚定正则（两侧空白分词），不会误拆变量名）；
* 字面量：带引号字符串按字符串比，否则能转 float 按数值比，退回字符串比；
* 排序比较（> >= < <=）两侧必须都是数值，否则按不成立处理（fail-close）；
* ``in`` 的字面量按逗号/中文逗号/顿号拆成集合，成员按字符串等值判定
  （数值形态不做类型转换——状态栏变量读出来就是字符串，等值即够）；
* 变量未采集 → 不成立（没读到的状态不猜）；
* 不支持逻辑组合——``&&``/``||``/``and`` 不是比较符，会被并进字面量按
  字符串比较处理，结果恒 fail-close（不成立）：复杂解锁拆成多条条目即可，
  换取求值器零注入面、零动态执行。

求值器纯数据入纯数据出（分层：quill/services/ 零 astrbot 导入），
语法校验另供体检器（quill/services/lint.py）复用 ``parse_condition``。
"""

from __future__ import annotations

import re

# 长符在前：split(">=") 优先于 split(">")，否则 "a>=1" 会被拆成 "a" > "=1"
_OPERATORS = (">=", "<=", "==", "!=", ">", "<")

# ``in`` 成员判定：必须两侧空白分词（\S+ 变量名 + 空白 + in + 空白 + 集合），
# 否则 "min>=3" / "king==x" 里的 "in" 子串会被误当运算符。
_IN_RE = re.compile(r"^\s*(\S+)\s+in\s+(.+?)\s*$")
# 集合分隔符：英文/中文逗号、顿号
_IN_SPLIT_RE = re.compile(r"[,，、]")


def parse_condition(condition: str):
    """把条件表达式拆成 (变量名, 比较符, 字面量)；格式不对返回 None。

    拆出来的字面量保留原样（引号不去、数值不转）——比较语义在
    ``evaluate`` 里统一处理，lint 只需要三段结构。
    """
    if not isinstance(condition, str):
        return None
    for op in _OPERATORS:
        if op in condition:
            left, _, right = condition.partition(op)
            var, value = left.strip(), right.strip()
            if var and value and _single_operator(condition, op):
                return var, op, value
            return None
    m = _IN_RE.match(condition)
    if m and m.group(1) and m.group(2):
        return m.group(1), "in", m.group(2)
    return None


def _single_operator(text: str, op: str) -> bool:
    """确认整串里只出现这一个比较符（``a>=1`` 合法，``a>=1<b`` 不合法）。"""
    rest = text.replace(op, "", 1)
    return not any(other in rest for other in _OPERATORS)


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(condition: str, session_vars: dict | None):
    """求值条目条件。返回 ``(是否成立, 说明)``——成立时说明为空串，
    不成立时说明可直接拼进 ``condition_unmet(...)``（含变量现值或缺失原因）。
    """
    parsed = parse_condition(condition or "")
    if parsed is None:
        return False, "表达式格式错误（应为 变量 比较符 值）"
    var, op, literal = parsed
    variables = session_vars or {}
    if var not in variables:
        return False, f"{var} 未采集"
    actual = variables[var]

    # in 集合成员判定先行：字面量是逗号列表，成员各自带引号（'a','b'），
    # 不能落进下面「整串带引号」的单字符串分支。
    if op == "in":
        members = []
        for part in _IN_SPLIT_RE.split(literal):
            p = part.strip()
            if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
                p = p[1:-1]
            if p:
                members.append(p)
        if not members:
            return False, "in 集合为空"
        ok = str(actual).strip() in members
        return ok, "" if ok else f"{var}={actual!r} in {'/'.join(members[:5])}"

    # 字符串字面量：去引号后按字符串比较（== / != 有意义，排序比较走数值）
    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in ("'", '"'):
        expected = literal[1:-1]
        if op == "==":
            ok = str(actual) == expected
        elif op == "!=":
            ok = str(actual) != expected
        else:
            return False, f"字符串字面量不支持 {op}"
        return ok, "" if ok else f"{var}={actual!r} {op} {expected!r}"

    a_num, l_num = _as_number(actual), _as_number(literal)
    if a_num is not None and l_num is not None:
        result = {
            ">=": a_num >= l_num, ">": a_num > l_num,
            "<=": a_num <= l_num, "<": a_num < l_num,
            "==": a_num == l_num, "!=": a_num != l_num,
        }[op]
        return result, "" if result else f"{var}={actual} {op} {literal}"

    # 双方（至少一方）非数值：只有等值比较有意义
    if op == "==":
        ok = str(actual).strip() == literal.strip()
    elif op == "!=":
        ok = str(actual).strip() != literal.strip()
    else:
        return False, f"{var}={actual!r} 非数值，不支持 {op}"
    return ok, "" if ok else f"{var}={actual!r} {op} {literal}"
