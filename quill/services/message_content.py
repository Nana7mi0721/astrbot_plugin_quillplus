# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""宿主消息的可见文本视图；不把推理、媒体或工具结构的 repr 当作正文。"""
from __future__ import annotations


def text_content(content):
    if isinstance(content,str):
        return content
    if isinstance(content,list):
        return ''.join(p['text'] for p in content if isinstance(p,dict) and p.get('type')=='text' and isinstance(p.get('text'),str))
    return ''


def plain_history_message(message):
    """可压缩的纯文本轮；think 可以忽略，未知片段与工具调用绝不删掉后冒充纯文本。"""
    if not isinstance(message,dict) or message.get('role') not in ('user','assistant') or message.get('tool_calls') or message.get('tool_call_id'):
        return None
    content=message.get('content')
    if not isinstance(content,(str,list)):
        return None
    if isinstance(content,list) and any(not isinstance(p,dict) or p.get('type') not in ('text','think','thinking','redacted_thinking') or (p.get('type')=='text' and not isinstance(p.get('text'),str)) for p in content):
        return None
    return {'role':message['role'],'content':text_content(content)}


def map_text_content(content, transform):
    """只处理连续 text 片段；媒体/推理/工具是边界，绝不把后文移动到图片之前。"""
    if isinstance(content,str):
        return transform(content)
    if not isinstance(content,list):
        return content
    result=[];pending=[]
    def flush():
        if not pending:
            return
        raw=''.join(p['text'] for p in pending);clean=transform(raw)
        if clean==raw:
            result.extend(pending)
        elif clean:
            result.append({**pending[0],'text':clean})
        pending.clear()
    for part in content:
        if isinstance(part,dict) and part.get('type')=='text' and isinstance(part.get('text'),str):
            pending.append(part)
        else:
            flush();result.append(part)
    flush()
    return result
