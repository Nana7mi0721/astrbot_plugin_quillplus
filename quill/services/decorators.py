# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CCv3 ``@@`` 装饰器解析（纯逻辑，无 astrbot 依赖）。

装饰器决定条目以消息深度注入到对话历史，而不是并入 system prompt：

* ``@@depth <N>`` —— 从最新消息倒数第 N 条之后插入；
* ``@@role system|user|assistant`` —— 注入消息的角色。
* ``@@@`` 是兼容写法（部分卡用三个 @），与 ``@@`` 等价。

装饰器只在正文**开头的连续行**里识别；一旦遇到第一行非 ``@@`` 内容，
后面的正文原样保留（正文里提到的 ``@@depth`` 不会被误删）。解析只做
识别与剥离，执行（真正往历史里插消息）由宿主
``interfaces/astrbot_hooks.py`` 在消息装配阶段完成。未知的 ``@@`` 指令
原样保留在正文里并记入 ``unknown``（不执行、不丢弃）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


_DEPTH_RE = re.compile(r"^[ \t]*@@@?depth[ \t]+(\d+)[ \t]*$", re.IGNORECASE)
_ROLE_RE = re.compile(r"^[ \t]*@@@?role[ \t]+(system|user|assistant)[ \t]*$", re.IGNORECASE)
_DIRECTIVE_RE = re.compile(r"^[ \t]*@@", re.IGNORECASE)

VALID_ROLES = ("system", "user", "assistant")


@dataclass
class DecoratorResult:
    """解析结果：剥离装饰器后的正文 + 解析出的深度/角色 + 未知指令。"""

    body: str
    depth: Optional[int]
    role: Optional[str]
    unknown: List[str]

    @property
    def is_depth_injection(self) -> bool:
        return self.depth is not None


def parse(content: str) -> DecoratorResult:
    """解析条目正文中的 ``@@`` 装饰器。

    只扫描正文头部的连续装饰器行；``@@depth`` / ``@@role`` 从正文中
    移除（它们不进入提示词正文），其余以 ``@@`` 开头的头部行视为未知
    指令，原样保留并记入 ``unknown``。遇到第一行非 ``@@`` 内容即停止。
    """
    text = str(content or "")
    depth: Optional[int] = None
    role: Optional[str] = None
    unknown: List[str] = []
    kept_unknown: List[str] = []
    lines = text.splitlines()
    index = 0
    total = len(lines)
    while index < total:
        stripped = lines[index].strip()
        if not stripped:
            # 允许头部空行；它们不构成「正文开始」。
            index += 1
            continue
        if not _DIRECTIVE_RE.match(stripped):
            break
        match = _DEPTH_RE.match(stripped)
        if match:
            try:
                depth = max(0, int(match.group(1)))
            except (TypeError, ValueError):
                pass
            index += 1
            continue
        match = _ROLE_RE.match(stripped)
        if match:
            role = match.group(1).lower()
            index += 1
            continue
        # 未知的头部装饰器：记录但保留原文，避免静默吞掉作者意图。
        unknown.append(stripped)
        kept_unknown.append(lines[index])
        index += 1
    body = "\n".join(kept_unknown + lines[index:]).strip("\n")
    return DecoratorResult(body=body, depth=depth, role=role, unknown=unknown)
