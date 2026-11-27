# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CCv3 ``@@`` 装饰器解析（纯逻辑，无 astrbot 依赖）。

装饰器决定条目以消息深度注入到对话历史，而不是并入 system prompt：

* ``@@depth <N>`` —— 从最新消息倒数第 N 条之后插入；
* ``@@role system|user|assistant`` —— 注入消息的角色。

解析只做识别与剥离，执行（真正往历史里插消息）由宿主
``interfaces/astrbot_hooks.py`` 在消息装配阶段完成。未知的 ``@@`` 指令
原样保留在正文里（不执行、不丢弃），避免把作者自定义的控制行吞掉。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


_DEPTH_RE = re.compile(r"^[ \t]*@@depth[ \t]+(\d+)[ \t]*$", re.IGNORECASE)
_ROLE_RE = re.compile(r"^[ \t]*@@role[ \t]+(system|user|assistant)[ \t]*$", re.IGNORECASE)
_KNOWN_PREFIX_RE = re.compile(r"^[ \t]*@@(depth|role)\b", re.IGNORECASE)

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

    ``@@depth`` / ``@@role`` 从正文中移除（它们不进入提示词正文）；
    其余以 ``@@`` 开头的行视为未知指令，原样保留。
    """
    text = str(content or "")
    depth: Optional[int] = None
    role: Optional[str] = None
    unknown: List[str] = []
    kept: List[str] = []
    for line in text.splitlines():
        match = _DEPTH_RE.match(line)
        if match:
            try:
                depth = max(0, int(match.group(1)))
            except (TypeError, ValueError):
                pass
            continue
        match = _ROLE_RE.match(line)
        if match:
            role = match.group(1).lower()
            continue
        if _KNOWN_PREFIX_RE.match(line):
            # 形如 `@@depth abc` 的已知前缀但参数非法：保留原文，避免静默吞掉。
            unknown.append(line.strip())
            kept.append(line)
            continue
        kept.append(line)
    body = "\n".join(kept).strip("\n")
    return DecoratorResult(body=body, depth=depth, role=role, unknown=unknown)
