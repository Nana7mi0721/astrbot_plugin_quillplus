# -*- coding: utf-8 -*-
"""QuillEmbeddingProvider — 封装 AstrBot Embedding Provider，支持 API + 本地 fallback。"""

from __future__ import annotations

import asyncio

from astrbot.api import logger


class QuillEmbeddingProvider:
    """封装 AstrBot Embedding Provider。

    优先级:
    1. 用户配置的 API Embedding Provider（如 SiliconFlow）
    2. 本地模型 BAAI/bge-small-zh-v1.5（sentence-transformers）
    """

    def __init__(self, context, provider_id: str = "", enable_local: bool = True, usage_recorder=None):
        self.context = context
        self.usage_recorder = usage_recorder
        self.provider_id = (provider_id or "").strip()
        self.enable_local = enable_local
        self._local_model = None  # 懒加载
        self._dim = None
        self._api_dim = None

    async def embed(self, texts: list[str], session_id=None) -> list[list[float]]:
        """将文本列表转为向量列表。"""
        if not texts:
            return []

        # 1. 尝试 API provider
        if self.provider_id and self.context:
            # M6 P1-14：重启后 _api_dim 仍是 None；若首次 API 抖动就回落
            # 本地异维模型，会把已按 API 维度建立/持久化的索引打坏。
            # provider 能报告维度时，先把它当作「已有索引维度」保守锚定。
            if self._api_dim is None:
                try:
                    provider = self.context.get_provider_by_id(self.provider_id)
                    get_dim = getattr(provider, "get_dim", None)
                    if callable(get_dim):
                        reported = int(get_dim() or 0)
                        if reported > 0:
                            self._api_dim = reported
                except Exception:
                    pass
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
                if provider is not None:
                    if self.usage_recorder is not None:
                        result = await self.usage_recorder.invoke(
                            lambda: provider.get_embeddings(texts), purpose="embedding",
                            provider_id=self.provider_id, session_id=session_id)
                    else:
                        result = await provider.get_embeddings(texts)
                    if not result:
                        raise RuntimeError("API embedding returned empty response")
                    dim = len(result[0])
                    if self._api_dim and dim != self._api_dim:
                        raise ValueError(
                            "API embedding 维度与既有索引不一致"
                            f"（返回 {dim} 维，索引 {self._api_dim} 维）"
                        )
                    self._dim = dim
                    self._api_dim = dim
                    return result
                api_error = "provider_unavailable"
            except Exception as e:
                logger.warning(f"[Quill RAG] API embedding 失败: {e}")
                api_error = type(e).__name__
            # P1-15：provider 缺失、空返回和异常同属「API 路不可用」。
            # 只要既有索引/已知 API 维度存在，就绝不回落本地异维模型。
            if self._api_dim:
                raise ValueError(
                    "API embedding 暂时失败，已保留原索引维度等待重试"
                    f"（{self._api_dim} 维，{api_error}）"
                )

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
                # 模型下载/加载为同步重 IO 操作，放入线程池避免阻塞事件循环
                self._local_model = await asyncio.to_thread(self._load_local_model)
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
        """获取向量维度。**0 = 未知**（尚未成功 embed 过，且 provider 不提供维度）。

        M3.3 F1：不再硬编码 512。此前未知时返回本地模型 bge-small-zh-v1.5
        的 512——对维度非 512 的 provider（真机实测 SiliconFlow bge-m3 =
        1024）是错误答案：FaissVectorStore 按 512 建索引/校验写入，全部
        上传被拒。维度未知的正确表达是 0，实际索引维度由 vector_store 用
        「已加载索引 / 首批真实向量」决定。
        """
        if self._dim is not None:
            return self._dim
        # 尝试从 API provider 获取
        if self.provider_id and self.context:
            try:
                provider = self.context.get_provider_by_id(self.provider_id)
                if provider and hasattr(provider, 'get_dim'):
                    dim = provider.get_dim()
                    if dim:
                        return int(dim)
            except Exception as e:
                logger.debug("[Quill Embedding] get_dim 查询 provider 失败: %s", e)
        # 未知：0（调用方按「维度待定」处理，禁止再猜一个具体数值）
        return 0

    def anchor_api_dim(self, dim: int) -> None:
        """用已加载索引维度锚定 API 向量防线，避免重启后首次抖动回落本地。"""
        try:
            value = int(dim or 0)
        except (TypeError, ValueError):
            return
        if value > 0 and self._api_dim is None:
            self._api_dim = value

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
