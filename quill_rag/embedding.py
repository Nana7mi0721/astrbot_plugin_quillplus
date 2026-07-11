# -*- coding: utf-8 -*-
"""QuillEmbeddingProvider — 封装 AstrBot Embedding Provider，支持 API + 本地 fallback。"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class QuillEmbeddingProvider:
    """封装 AstrBot Embedding Provider。

    优先级:
    1. 用户配置的 API Embedding Provider（如 SiliconFlow）
    2. 本地模型 BAAI/bge-small-zh-v1.5（sentence-transformers）
    """

    def __init__(self, context, provider_id: str = "", enable_local: bool = True):
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.enable_local = enable_local
        self._local_model = None  # 懒加载
        self._dim = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """将文本列表转为向量列表。"""
        if not texts:
            return []

        # 1. 尝试 API provider
        if self.provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
                if provider:
                    result = await provider.get_embeddings(texts)
                    if result:
                        self._dim = len(result[0])
                        return result
            except Exception as e:
                logger.warning(f"[Quill RAG] API embedding 失败: {e}")

        # 2. fallback 本地模型
        if self.enable_local:
            return await self._embed_local(texts)

        raise ValueError(
            "无可用 embedding provider。请在配置中设置 embedding_provider_id"
            "或启用本地嵌入模型。"
        )

    async def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """使用本地 sentence-transformers 模型。"""
        try:
            if self._local_model is None:
                logger.info("[Quill RAG] 加载本地 embedding 模型 BAAI/bge-small-zh-v1.5...")
                self._local_model = self._load_local_model()
            loop = asyncio.get_running_loop()
            embeddings = await loop.run_in_executor(
                None,
                lambda: self._local_model.encode(texts, convert_to_numpy=True)
            )
            result = embeddings.tolist()
            if result:
                self._dim = len(result[0])
            return result
        except ImportError:
            raise ValueError(
                "本地 embedding 需要 sentence_transformers。请运行: "
                "pip install sentence-transformers"
            )
        except Exception as e:
            raise ValueError(f"本地 embedding 失败: {e}")

    def _load_local_model(self):
        """懒加载本地模型。"""
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer("BAAI/bge-small-zh-v1.5")
        except Exception as e:
            raise ImportError(f"加载本地模型失败: {e}")

    def get_dim(self) -> int:
        """获取向量维度。"""
        if self._dim is not None:
            return self._dim
        # 尝试从 API provider 获取
        if self.provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
                if provider and hasattr(provider, 'get_dim'):
                    return provider.get_dim()
            except Exception as e:
                logger.debug("[Quill Embedding] get_dim 查询 provider 失败: %s", e)
        # 本地模型默认维度
        return 512  # bge-small-zh-v1.5 的维度

    def get_status(self) -> dict:
        """返回当前 provider 状态信息。"""
        provider_type = "none"
        if self.provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
                if provider:
                    provider_type = "api"
            except Exception as e:
                logger.debug("[Quill Embedding] get_status 查询 provider 失败: %s", e)
        if provider_type == "none" and self.enable_local:
            provider_type = "local"
        return {
            "provider_id": self.provider_id,
            "type": provider_type,
            "enable_local": self.enable_local,
            "dim": self.get_dim(),
        }
