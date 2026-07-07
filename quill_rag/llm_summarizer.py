# -*- coding: utf-8 -*-
"""LLM 摘要生成 — 调用 AstrBot LLM Provider 将对话精简为摘要。"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是一个记忆整理助手。请将以下对话精简为一条不超过50字的核心事件摘要。
摘要应包含：关键人物、事件、情感变化。不要使用标点符号以外的特殊字符。
只输出摘要文本，不要任何前缀或解释。"""


class QuillSummarizer:
    """使用 AstrBot LLM Provider 生成对话摘要。"""

    def __init__(self, context, provider_id: str = "", system_prompt: str = DEFAULT_SYSTEM_PROMPT):
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.system_prompt = system_prompt

    CONTEXT_SUMMARY_PROMPT = """你是一个记忆整理助手。请将以下多轮对话精简为一条不超过80字的核心事件摘要。
重点提取：关键角色关系、重要事件转折、情感变化轨迹。忽略日常寒暄和重复内容。
只输出摘要文本，不要任何前缀或解释。"""

    async def summarize(self, user_input: str, ai_response: str) -> str:
        """将对话精简为摘要。

        Args:
            user_input: 用户输入
            ai_response: AI 回复

        Returns:
            摘要文本（失败时返回空字符串）
        """
        if not self.provider_id or not self.context:
            return ""

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                return ""

            prompt = f"用户：{user_input}\nAI：{ai_response}"
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=self.system_prompt,
            )
            if response and getattr(response, "completion_text", ""):
                summary = response.completion_text.strip()
                logger.info(f"[Quill Memory] 摘要生成成功: {summary[:30]}...")
                return summary
        except Exception as e:
            logger.warning(f"[Quill Memory] 摘要生成失败: {e}")
        return ""

    async def context_summarize(self, combined_text: str, turn_count: int = 0) -> str:
        """将多轮对话历史精简为一条摘要。

        Args:
            combined_text: 拼接的多轮对话文本
            turn_count: 对话轮次数

        Returns:
            摘要文本（失败时返回空字符串）
        """
        if not self.provider_id or not self.context:
            return ""

        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                return ""

            prompt = f"以下是 {turn_count} 轮对话：\n\n{combined_text}\n\n请总结核心事件："
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=self.CONTEXT_SUMMARY_PROMPT,
            )
            if response and getattr(response, "completion_text", ""):
                summary = response.completion_text.strip()
                logger.info(f"[Quill Memory] 多轮摘要生成成功: {summary[:30]}...")
                return summary
        except Exception as e:
            logger.warning(f"[Quill Memory] 多轮摘要生成失败: {e}")
        return ""
