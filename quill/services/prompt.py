# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quill.services.prompt — Prompt 装配域服务（PLAN §2.1 目标位：prompt_builder
演进落点）。

M2.2 第六轮（H3 薄化）首批下沉：只收 H3 的 22 步编排中**真正独立成块的纯
逻辑**（无 async、无插件实例状态、显式参数）。其余步骤——context restoration
垫回、激活 gate、tail message 拼接等——或携带跨步存活值（mem_session_id /
_sb_effective），或本身是编排控制流（早退 return），强搬即重写，留在
interfaces/astrbot_hooks.handle_llm_request（决策记录见彼处 docstring）。

依赖规则：本包禁止 import astrbot（tests/arch/test_layering.py 机械校验）。
"""

from __future__ import annotations


def build_context_text(user_input: str, contexts) -> str:
    """拼多轮上下文文本（H3 步骤 8，自 main.py 原位搬移，M2.2 第六轮）。

    供 WR 关键词匹配使用的「末 4 条历史 + 本轮输入」拼接：取 contexts 的
    最后 4 条，只收 user/assistant 角色的字符串 content，以换行连接，本轮
    user_input 固定在首位。

    下沉判据（决策记录）：本块是 22 步中唯一零 async、零插件实例状态、
    零控制流交织的纯函数段——(user_input, contexts) → str，显式参数即完整
    依赖面。调用点（interfaces/astrbot_hooks.handle_llm_request）原位一行
    替换，求值时序不变（req.contexts 在此之前已被守卫规范化为 list）。
    """
    context_text = user_input
    if contexts and isinstance(contexts, list):
        recent = contexts[-4:]
        parts = [user_input]
        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                parts.append(content)
        context_text = "\n".join(parts)
    return context_text
