# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""回复文本响应域——send_message_to_user 工具消息的文本清洗（v5.3.0 M2.2）。

首批内容（自 main.py 原位搬移，M2.2 第三轮；消费方为 H2
on_using_llm_tool，经 interfaces/astrbot_hooks.py 委托调用）：

- ``_MD_PATTERNS`` / ``strip_markdown``：Markdown 标记擦除纯函数。
  Telegram 适配器没有设置 parse_mode，Markdown 语法会被原文显示，
  在 send_message_to_user 执行前用正则擦除标记，让用户看到干净文本；
- ``strip_markdown_in_plain_messages``：对 tool_args.messages 的 plain 段
  逐条套用 strip_markdown——仅 telegram/tg 平台执行（未知平台不剥离，
  避免破坏原生 Markdown 渲染），返回被改写的段数（仅用于日志统计）。

搬移纪律：三段在原 main.py 中即为零 self 依赖的模块级纯函数/自由段，
逐字照搬；logger 经 logbridge 桥接（M2.1 以来 quill/ 侧统一做法）。
H2 内与控制流交织、不宜下沉的段落（JSON 解析-回写、状态栏链调用、
注入报告、拒绝扫描）不住本模块——决策记录见
interfaces/astrbot_hooks.py 的 handle_using_llm_tool docstring。
"""

from __future__ import annotations

import re

from ..core.logbridge import logger  # noqa: F401  # 方法体 logger 引用经 logbridge 桥接

_MD_PATTERNS = [
    # Inline code (most specific first)
    (re.compile(r'`([^`\n]+)`'), r'\1'),
    # Bold-italic ***text***
    (re.compile(r'\*\*\*(.+?)\*\*\*'), r'\1'),
    (re.compile(r'___(.+?)___'), r'\1'),
    # Bold **text**
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    # Italic *text* (not adjacent to another *, protects **kwargs)
    (re.compile(r'(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)'), r'\1'),
    # Strikethrough ~~text~~
    (re.compile(r'~~(.+?)~~'), r'\1'),
    # Images ![alt](url)
    (re.compile(r'!\[([^\]]*)\]\([^)]+\)'), r'\1'),
    # Links [text](url)
    (re.compile(r'\[([^\]]+)\]\([^)]+\)'), r'\1'),
    # Reference-style links [text][ref]
    (re.compile(r'\[([^\]]+)\]\[[^\]]*\]'), r'\1'),
    # Heading markers at line start
    (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),
    # Blockquotes at line start
    (re.compile(r'^>\s?', re.MULTILINE), ''),
    # Horizontal rules
    (re.compile(r'^[-*_]{3,}[ \t]*$', re.MULTILINE), ''),
]


def strip_markdown(text: str) -> str:
    """Remove common Markdown formatting, leaving clean plain text."""
    if not text:
        return text
    for pattern, replacement in _MD_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def strip_markdown_in_plain_messages(messages, platform: str) -> int:
    """对 tool_args 消息列表的 plain 段逐条剥离 Markdown（仅 telegram/tg）。

    （自 H2 函数体逐字搬移；``messages`` 为已解析的消息列表——实际可能
    是任何类型，非 list 时静默跳过。返回被改写的 plain 段数量。）
    """
    needs_strip = platform in ("telegram", "tg")
    if not needs_strip:
        return 0
    logger.info(f"[Quill] >>> send_message_to_user 调用 (platform={platform or '?'}), 清理 Markdown...")
    modified = 0
    for msg in messages if isinstance(messages, list) else []:
        if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
            original = msg["text"]
            cleaned = strip_markdown(original)
            if cleaned != original:
                msg["text"] = cleaned
                modified += 1
    if modified:
        logger.info(f"[Quill] 已清理 {modified} 条消息中的 Markdown 标记")
    return modified
