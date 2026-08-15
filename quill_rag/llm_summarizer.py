# -*- coding: utf-8 -*-
"""LLM 摘要生成 — 调用 AstrBot LLM Provider 将对话精简为摘要。"""

from __future__ import annotations

import json
import logging

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
            if response:
                completion = getattr(response, "completion_text", None) or getattr(response, "text", "")
                if completion:
                    summary = completion.strip()
                    logger.info(f"[Quill Memory] 多轮摘要生成成功: {summary[:30]}...")
                    return summary
        except Exception as e:
            logger.warning(f"[Quill Memory] 多轮摘要生成失败: {e}")
        return ""


    async def reflect_on_logs(self, combined_text: str) -> dict:
        if not self.provider_id or not self.context:
            return {}
            
        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            if not provider:
                return {}
                
            prompt = f"以下是该用户最近的详细对话日志：\n\n{combined_text}\n\n请你作为心理侧写师和记忆整理大师，输出JSON格式进行高维度归纳。必须包含三个字段：\n1. new_core_traits: 字符串，总结角色的心理变化、态度转变、确立的新关系等高维度设定。\n2. crucial_facts: 字符串，总结不可逆的重大客观事件或设定的核心信息。\n3. trivial_summaries: 字符串列表，将日常的琐碎闲聊精简为几条独立的简短陈述句。"
            system_prompt = "你是一个专门负责信息提纯与归纳的AI。严格返回合法的JSON对象，包含 'new_core_traits', 'crucial_facts', 'trivial_summaries' (数组) 字段。"
            
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
            if response:
                completion = getattr(response, "completion_text", None) or getattr(response, "text", "")
                if completion:
                    data = self._extract_json(completion)
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            logger.warning(f"[Quill Memory] 反思生成失败: {e}")
        return {}

    @staticmethod
    def _extract_json(text: str):
        """从 LLM 输出中提取首个完整 JSON 对象。

        用 raw_decode 从每个 '{' 开始尝试：贪婪正则在输出包含多个 JSON 块
        （思考过程 + 正式输出）或被 Markdown 代码块包裹时会解析失败。
        """
        decoder = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        # 兜底：整体解析（保持原行为）
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
