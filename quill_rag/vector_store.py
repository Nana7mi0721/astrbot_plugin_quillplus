# -*- coding: utf-8 -*-
"""FaissVectorStore — FAISS 向量存储 + SQLite 元数据。仅用于 Doc RAG（全局文档检索）。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class FaissVectorStore:
    """FAISS 向量存储 + SQLite 元数据。

    仅用于 Doc RAG（全局文档检索），动态记忆使用 MemoryStore。
    """

    def __init__(self, db_path: str, index_path: str, dim: int = 768):
        self.db_path = db_path
        self.index_path = index_path
        self.dim = dim
        self._index = None
        self._lock = threading.Lock()
        self._init_db()
        self._load_index()

    def _init_db(self):
        """初始化 SQLite 表。"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    faiss_id INTEGER DEFAULT -1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self):
        """获取配置好的 SQLite 连接。"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _load_index(self):
        """加载 FAISS 索引（如存在）。"""
        try:
            import faiss
            if os.path.exists(self.index_path):
                self._index = faiss.read_index(self.index_path)
                self.dim = self._index.d
                logger.info(f"[Quill RAG] FAISS 索引已加载: {self.index_path} (dim={self.dim})")
            else:
                self._create_index()
        except ImportError:
            logger.warning("[Quill RAG] faiss 未安装，向量检索不可用")
        except Exception as e:
            logger.warning(f"[Quill RAG] FAISS 索引加载失败: {e}")
            self._create_index()

    def _create_index(self):
        """创建新的 FAISS 索引。"""
        try:
            import faiss
            base = faiss.IndexFlatIP(int(self.dim))
            self._index = faiss.IndexIDMap(base)
            logger.info(f"[Quill RAG] 新建 FAISS 索引 (dim={self.dim})")
        except ImportError:
            pass

    def _save_index(self):
        """持久化 FAISS 索引到磁盘。"""
        if self._index is None:
            return
        try:
            import faiss
            dir_path = os.path.dirname(self.index_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            faiss.write_index(self._index, self.index_path)
        except Exception as e:
            logger.warning(f"[Quill RAG] FAISS 索引保存失败: {e}")

    def add(self, texts: list[str], embeddings: list[list[float]], source: str, doc_id: str = ""):
        """添加文本块和对应向量。"""
        if not texts or not embeddings:
            return
        if doc_id == "":
            doc_id = source

        # 写入 FAISS（先获取 FAISS id）
        faiss_ids = []
        if self._index is not None:
            try:
                emb_array = np.array(embeddings, dtype=np.float32)
                ids = np.arange(self._index.ntotal, self._index.ntotal + len(texts), dtype=np.int64)
                with self._lock:  # 【架构师补充：将内存操作和持久化操作放在同一个原子锁内】
                    self._index.add_with_ids(emb_array, ids)
                    self._save_index()
                faiss_ids = ids.tolist()
            except Exception as e:
                logger.warning(f"[Quill RAG] FAISS 添加失败: {e}")

        # 写入 SQLite（批量事务插入，提升大文件写入性能）
        conn = self._get_conn()
        try:
            rows = [
                (doc_id, source, i, text, faiss_ids[i] if i < len(faiss_ids) else -1)
                for i, text in enumerate(texts)
            ]
            conn.executemany(
                "INSERT INTO chunks (doc_id, source, chunk_index, content, faiss_id) VALUES (?, ?, ?, ?, ?)",
                rows
            )
            conn.commit()
        finally:
            conn.close()

    def search(self, query_embedding: list[float], top_k: int = 9, allowed_sources: list[str] = None) -> list[dict]:
        """FAISS 检索，支持通过 allowed_sources 按文档 source 过滤。"""
        if self._index is None or self._index.ntotal == 0:
            return []
        try:
            query = np.array([query_embedding], dtype=np.float32)

            # 多召回一些以补偿幽灵向量。如果有文档过滤限制，大幅放大召回池以防被过滤空。
            recall_k = min(top_k * 3, self._index.ntotal)
            if allowed_sources:
                recall_k = min(max(top_k * 10, 100), self._index.ntotal)

            with self._lock:
                scores, ids = self._index.search(query, recall_k)

            results = []
            conn = self._get_conn()
            try:
                for score, idx in zip(scores[0], ids[0]):
                    if idx < 0:
                        continue
                    # 联表校验，并执行文档过滤
                    row = conn.execute(
                        "SELECT content, source, chunk_index FROM chunks WHERE faiss_id = ?",
                        (int(idx),)
                    ).fetchone()

                    if row:
                        doc_source = row[1]
                        # 【核心过滤逻辑】如果指定了允许的文档，且当前块不属于这些文档，则跳过
                        if allowed_sources and doc_source not in allowed_sources:
                            continue

                        results.append({
                            "content": row[0],
                            "source": doc_source,
                            "chunk_index": row[2],
                            "score": float(score),
                        })

                        # 满载返回
                        if len(results) >= top_k:
                            break
            finally:
                conn.close()
            return results
        except Exception as e:
            logger.warning(f"[Quill RAG] FAISS 检索失败: {e}")
            return []

    def delete_by_source(self, source: str) -> int:
        """删除某文档的所有块。"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()
        # 注意：FAISS 不支持删除，需要重建索引
        # 简化处理：标记删除，下次重建时清理

    def list_documents(self) -> list[dict]:
        """列出所有已上传文档。"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT source, doc_id, COUNT(*) as chunk_count, MIN(created_at) as created_at "
                "FROM chunks GROUP BY source, doc_id"
            ).fetchall()
            return [
                {"source": r[0], "doc_id": r[1], "chunk_count": r[2], "created_at": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """返回存储统计。"""
        conn = self._get_conn()
        try:
            total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            total_docs = conn.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        finally:
            conn.close()
        return {
            "total_chunks": total_chunks,
            "total_docs": total_docs,
            "faiss_vectors": self._index.ntotal if self._index else 0,
            "dim": self.dim,
        }
