# -*- coding: utf-8 -*-
"""检索 + 注入逻辑。Doc RAG 检索（FAISS + 重排）和记忆检索（NumPy）的统一入口。"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class QuillRetriever:
    """统一检索入口。

    - Doc RAG: FAISS 召回 + Rerank 重排
    - Memory: SQLite + NumPy 余弦相似度
    """

    def __init__(
        self,
        embedding_provider,
        vector_store=None,
        reranker=None,
        memory_store=None,
        summarizer=None,
        top_k: int = 3,
        enable_memory: bool = False,
        config=None,
    ):
        self.embedding = embedding_provider
        self.vector_store = vector_store      # Doc RAG (FAISS)
        self.reranker = reranker              # Doc RAG 重排
        self.memory_store = memory_store      # 动态记忆 (SQLite)
        self.summarizer = summarizer          # LLM 摘要
        self.top_k = top_k
        self.enable_memory = enable_memory
        self.config = config

    async def search_documents(self, query: str) -> list[dict]:
        """Doc RAG 检索：FAISS 召回 + Rerank 重排（在线程池中执行同步IO）。"""
        if not self.vector_store or not query:
            return []
        try:
            query_emb = await self.embedding.embed([query])
            if not query_emb:
                return []
            # 直接从 config._raw 底层字典中读取真实配置值，用 or {} 防止 NoneType
            rag_config = getattr(self, 'config', None) and getattr(self.config, '_raw', None) or {}
            dense_top_k = int(rag_config.get('rag', {}).get('dense_top_k', self.top_k))
            # 将同步 FAISS 检索放入线程池，避免阻塞主事件循环
            raw_results = await asyncio.to_thread(
                self.vector_store.search, query_emb[0], dense_top_k * 3
            )
            if not raw_results:
                return []
            if self.reranker:
                return await self.reranker.rerank(query, raw_results, top_k=self.top_k)
            return raw_results[:self.top_k]
        except Exception as e:
            logger.warning(f"[Quill RAG] 文档检索失败: {e}")
            return []

    async def search_memories(self, session_id: str, query: str) -> list[dict]:
        """动态记忆检索：SQLite session 隔离 + NumPy 余弦相似度（在线程池中执行）。"""
        if not self.memory_store or not self.enable_memory or not session_id or not query:
            return []
        try:
            query_emb = await self.embedding.embed([query])
            if not query_emb:
                return []
            # 将 IO + NumPy 计算放入线程池
            return await asyncio.to_thread(
                self.memory_store.search, session_id, query_emb[0], self.top_k
            )
        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆检索失败: {e}")
            return []

    async def store_memory(self, session_id: str, user_input: str, ai_response: str):
        """存储一条对话记忆（在线程池中执行数据库写入）。"""
        if not self.memory_store or not self.enable_memory or not session_id:
            return
        try:
            summary = ""
            if self.summarizer:
                summary = await self.summarizer.summarize(user_input, ai_response)
            if not summary:
                summary = (user_input + " " + ai_response)[:50]

            vector = await self.embedding.embed([summary])
            if vector:
                chat_summary = (user_input + " " + ai_response)[:100]
                # 将同步数据库写入放入线程池
                await asyncio.to_thread(
                    self.memory_store.add, session_id, summary, vector[0], chat_summary
                )
                logger.info(f"[Quill Memory] 记忆已存储: session={session_id} summary={summary[:30]}...")
        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆存储失败: {e}")

    async def store_memory_direct(self, session_id: str, content: str):
        """直接存储一条用户提供的记忆内容（无需 user_input/ai_response 配对）。
        用于 /memory learn 命令手动添加记忆。"""
        if not self.memory_store or not self.enable_memory or not session_id or not content:
            return
        try:
            summary = content.strip()
            if self.summarizer:
                summary = await self.summarizer.summarize(content, "")
                if not summary:
                    summary = content.strip()
            summary = summary[:200]

            vector = await self.embedding.embed([summary])
            if vector:
                await asyncio.to_thread(
                    self.memory_store.add, session_id, summary, vector[0], content[:100]
                )
                logger.info(f"[Quill Memory] 直接记忆存储: session={session_id} summary={summary[:30]}...")
        except Exception as e:
            logger.warning(f"[Quill Memory] 直接记忆存储失败: {e}")
            raise

    def format_for_prompt(self, doc_results: list[dict], memory_results: list[dict]) -> str:
        """将检索结果格式化为注入 prompt 的文本。"""
        parts = []
        if doc_results:
            doc_texts = [f"[文档-{r.get('source', '?')}]: {r['content']}" for r in doc_results]
            parts.append("## [参考资料]\n" + "\n\n".join(doc_texts))
        if memory_results:
            mem_texts = [f"- {r['summary']}" for r in memory_results]
            parts.append("## [相关记忆]\n" + "\n".join(mem_texts))
        return "\n\n".join(parts)
