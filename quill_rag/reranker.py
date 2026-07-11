# -*- coding: utf-8 -*-
"""QuillReranker — 封装 AstrBot Rerank Provider，对检索候选重新打分。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class QuillReranker:
    """封装 AstrBot Rerank Provider。

    对 FAISS 召回的候选列表重新打分，筛选最精准的结果。
    优先使用 rerank_provider，否则 fallback 到 LLM 评分。
    """

    def __init__(self, context, rerank_provider_id: str = "", fallback_llm_id: str = ""):
        self.context = context
        self.rerank_provider_id = (rerank_provider_id or "").strip()
        self.fallback_llm_id = (fallback_llm_id or "").strip()

    async def rerank(self, query: str, candidates: list[dict], top_k: int = 3) -> list[dict]:
        """对候选列表重新打分，返回 top_k 最相关结果。

        Args:
            query: 用户查询
            candidates: FAISS 召回的候选列表，每项含 'content' 字段
            top_k: 返回结果数

        Returns:
            重排后的 top_k 结果
        """
        if not candidates:
            return []
        if len(candidates) <= top_k:
            return candidates

        # 1. 尝试使用 rerank_provider
        if self.rerank_provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.rerank_provider_id)
                if provider and hasattr(provider, 'rerank'):
                    return await self._rerank_with_provider(query, candidates, top_k, provider)
            except Exception as e:
                logger.warning(f"[Quill RAG] Rerank provider 失败: {e}")

        # 2. fallback: 尝试 LLM 评分，再按原始 score 排序
        if self.fallback_llm_id and self.context:
            try:
                llm_provider = self.context.get_provider_by_id(self.fallback_llm_id)
                if llm_provider:
                    return await self._rerank_with_llm(query, candidates, top_k, llm_provider)
            except Exception as e:
                logger.warning(f"[Quill RAG] LLM fallback provider 获取失败: {e}")
        return sorted(candidates, key=lambda x: x.get('score', 0), reverse=True)[:top_k]

    async def _rerank_with_provider(self, query, candidates, top_k, provider) -> list[dict]:
        """调用 provider 的 rerank 方法。

        AstrBot 的 rerank 返回 list[RerankResult]，每项含 .index 和 .relevance_score。
        rerank 方法可能是同步或异步，需兼容处理（参考 angel_memory）。
        """
        import inspect
        try:
            texts = [c.get('content', '') for c in candidates]
            # 调用 rerank（可能是 sync 或 async）—— 使用真实 query，不要用候选内容覆盖
            rerank_resp = provider.rerank(query=query, documents=texts)
            if inspect.isawaitable(rerank_resp):
                rerank_resp = await rerank_resp

            if not rerank_resp:
                return candidates[:top_k]

            # 解析 RerankResult 列表
            scored = {}
            for item in rerank_resp:
                if hasattr(item, 'index') and hasattr(item, 'relevance_score'):
                    # RerankResult 对象
                    idx = item.index
                    score = float(item.relevance_score)
                elif isinstance(item, dict):
                    idx = item.get('index', -1)
                    score = float(item.get('relevance_score', item.get('score', 0)))
                else:
                    continue
                if 0 <= idx < len(candidates):
                    scored[idx] = score

            # 按重排分数排序，取 top_k
            for idx, cand in enumerate(candidates):
                cand['rerank_score'] = scored.get(idx, 0.0)
            return sorted(candidates, key=lambda x: x.get('rerank_score', 0), reverse=True)[:top_k]
        except Exception as e:
            logger.warning(f"[Quill RAG] Rerank 调用失败: {e}")
        return candidates[:top_k]

    async def _rerank_with_llm(self, query: str, candidates: list[dict], top_k: int, provider) -> list[dict]:
        """使用 LLM 对候选列表打分（fallback 方案）。

        当 rerank provider 不可用时，让 LLM 对候选内容按相关性打分。
        失败时回退到按原始 score 排序。
        """
        import re
        try:
            # 构建编号列表（截断避免 token 过长）
            items = []
            for i, c in enumerate(candidates):
                content = (c.get('content', '') or '')[:200]
                items.append(f"[{i}] {content}\n")

            system_prompt = (
                "你是一个文档相关性评估助手。根据用户查询，为每个候选文档打分（0-10，10最相关）。"
                "只返回分数列表，格式：[0]=分数 [1]=分数 [2]=分数 ..."
            )
            prompt = (
                f"用户查询：{query}\n\n"
                f"候选文档列表：\n{''.join(items)}\n"
                f"请为每个文档打分（0-10）："
            )

            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
            text = getattr(response, "completion_text", "") or str(response)

            # 解析 [idx]=score 格式
            scores = {}
            for match in re.finditer(r'\[(\d+)\]\s*=\s*([\d.]+)', text):
                idx = int(match.group(1))
                score = float(match.group(2))
                if 0 <= idx < len(candidates):
                    scores[idx] = score

            if scores:
                for idx, cand in enumerate(candidates):
                    cand['llm_score'] = scores.get(idx, 0.0)
                return sorted(candidates, key=lambda x: x.get('llm_score', 0), reverse=True)[:top_k]
        except Exception as e:
            logger.warning(f"[Quill RAG] LLM 降级重排失败: {e}")
        return sorted(candidates, key=lambda x: x.get('score', 0), reverse=True)[:top_k]

    def get_status(self) -> dict:
        """返回 reranker 状态。"""
        has_provider = False
        if self.rerank_provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.rerank_provider_id)
                has_provider = provider is not None
            except Exception as e:
                logger.debug("[Quill Reranker] get_status 查询 provider 失败: %s", e)
        return {
            "rerank_provider_id": self.rerank_provider_id,
            "has_rerank_provider": has_provider,
            "fallback_llm_id": self.fallback_llm_id,
        }
