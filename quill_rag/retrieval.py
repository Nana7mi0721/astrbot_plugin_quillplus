# -*- coding: utf-8 -*-
"""检索 + 注入逻辑。Doc RAG 检索（FAISS + 重排）和记忆检索（NumPy）的统一入口。"""

from __future__ import annotations

import asyncio
import logging

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
        # F5 修复：保留后台 task 引用
        self._bg_tasks: set = set()

    def _spawn(self, coro):
        """启动后台任务并保留引用，防止被 GC 中断"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    async def search_documents(self, query: str, allowed_sources: list[str] = None) -> list[dict]:
        """Doc RAG 检索：FAISS 召回 + Rerank 重排（支持按源文档过滤）。"""
        if not self.vector_store or not query:
            return []
        try:
            query_emb = await self.embedding.embed([query])
            if not query_emb:
                return []

            rag_config = getattr(self, 'config', None) and getattr(self.config, '_raw', None) or {}
            dense_top_k = int(rag_config.get('rag', {}).get('dense_top_k', self.top_k))

            # 传递 allowed_sources 给 vector_store 过滤
            raw_results = await asyncio.to_thread(
                self.vector_store.search, query_emb[0], dense_top_k * 3, allowed_sources
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
            results = await asyncio.to_thread(
                self.memory_store.search, session_id, query_emb[0], self.top_k
            )
            if results:
                mem_ids = [r["id"] for r in results]
                self._spawn(asyncio.to_thread(self.memory_store.mark_memories_used, mem_ids, 1.5))
            return results
        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆检索失败: {e}")
            return []

    async def get_core_memories(self, session_id: str) -> list[dict]:
        """获取核心记忆（is_core=1），无条件注入，不参与 Top-K 竞争。"""
        if not self.memory_store or not session_id:
            return []
        try:
            return await asyncio.to_thread(self.memory_store.get_core_memories, session_id)
        except Exception as e:
            logger.warning(f"[Quill Memory] 核心记忆获取失败: {e}")
            return []

    async def log_chat_message(self, session_id: str, role: str, content: str):
        """存储一条原始对话记录（在线程池中执行）"""
        if not self.memory_store or not session_id or not content:
            return
        if not getattr(self.config, 'rag_enable_chat_logging', True):
            return
        try:
            await asyncio.to_thread(
                self.memory_store.log_message, session_id, role, content
            )
        except Exception as e:
            logger.warning(f"[Quill ChatLog] 对话记录失败: {e}")

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

    async def summarize_contexts(self, session_id: str, contexts: list[dict]) -> str:
        """自动总结多轮对话历史，生成一条记忆并存储。

        Args:
            session_id: 会话 ID
            contexts: 对话历史列表，每项含 role/content

        Returns:
            生成的摘要文本
        """
        if not self.memory_store or not self.enable_memory:
            raise RuntimeError("动态记忆功能未启用")
        if not contexts:
            raise ValueError("对话历史不足，无法总结。")

        # 1. 只取最近 6 轮（最多 12 条消息）
        recent = contexts[-12:]

        # 2. 格式化为结构化对话文本
        lines = []
        turn_count = 0
        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")[:300]
            if role == "user":
                lines.append(f"用户：{content}")
                turn_count += 1
            elif role == "assistant":
                lines.append(f"AI：{content}")
        combined = "\n".join(lines)

        # 3. 调用 LLM 做结构化多轮摘要（复用 summarizer provider）
        summary = ""
        if self.summarizer and hasattr(self.summarizer, 'context_summarize'):
            summary = await self.summarizer.context_summarize(combined, turn_count)
        elif self.summarizer:
            # 降级：用单轮 summarizer 兜底
            summary = await self.summarizer.summarize(combined[:1500], "")
        if not summary:
            summary = f"最近{turn_count}轮对话片段：" + combined[:150]
        summary = summary[:200]

        # 4. Embedding → 存储
        vector = await self.embedding.embed([summary])
        if not vector:
            raise RuntimeError("Embedding 生成失败")

        # 5. 防重复：检查是否已有语义高度重叠的记忆
        if self.memory_store:
            try:
                existing = await asyncio.to_thread(
                    self.memory_store.search, session_id, vector[0], top_k=3
                )
                for mem in existing:
                    if mem.get("score", 0) > 0.92:
                        logger.info(f"[Quill Memory] 跳过重复记忆: session={session_id} similarity={mem['score']:.2f}")
                        return summary  # 已存在高度相似记忆，跳过存储
            except Exception:
                logger.debug("[Quill Memory] 重复记忆检查失败，继续存储", exc_info=True)

        await asyncio.to_thread(
            self.memory_store.add, session_id, summary, vector[0],
            chat_summary=combined[:100]
        )
        logger.info(f"[Quill Memory] 上下文自动总结: session={session_id} turns={turn_count} summary={summary[:30]}...")

        return summary

    def format_for_prompt(self, doc_results: list[dict], memory_results: list[dict], core_memories: list[dict] = None) -> str:
        """将检索结果格式化为 XML 规范文本。核心记忆优先注入，不参与 Top-K 竞争。"""
        parts = []
        # 核心记忆优先注入（无条件，类似人设基石）
        if core_memories:
            core_texts = [f"  [{i+1}] {r['summary']}" for i, r in enumerate(core_memories)]
            parts.append("<core_memory>\n" + "\n".join(core_texts) + "\n</core_memory>")

        if doc_results:
            doc_texts = [f"  [{i+1}] [文档-{r.get('source', '?')}] {r['content']}" for i, r in enumerate(doc_results)]
            parts.append("<documents>\n" + "\n".join(doc_texts) + "\n</documents>")

        if memory_results:
            mem_texts = [
                f"  [{i+1}] [记忆] {r['summary']} (引用:{r.get('useful_count', 0)}次)"
                for i, r in enumerate(memory_results)
            ]
            parts.append("<memories>\n" + "\n".join(mem_texts) + "\n</memories>")

        if parts:
            tag_hint = "、".join(filter(None, [
                "<core_memory>" if core_memories else None,
                "<memories>" if memory_results else None,
                "<documents>" if doc_results else None,
            ]))
            parts.append(f"请自然地结合上述 {tag_hint} 提供的信息进行回复，保持设定的连贯性。")

        return "\n\n".join(parts)
