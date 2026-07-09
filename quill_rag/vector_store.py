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

    def __init__(self, db_path: str, index_path: str, dim: int = 768, embedding_provider=None):
        """初始化向量存储。

        S2-10 修复：接受 embedding_provider 动态确定期望 dim。
        若已有 FAISS 索引 dim 与期望 dim 不一致（例如切换了 embedding provider），
        将重建索引（旧索引数据失效，需重新上传文档）。
        """
        self.db_path = db_path
        self.index_path = index_path
        # 期望 dim：优先用 embedding_provider 动态获取，否则用传入的 dim
        self.expected_dim = int(embedding_provider.get_dim()) if embedding_provider else int(dim)
        self.dim = self.expected_dim
        self._index = None
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._init_db()
        self._load_index()

    # F4 修复：SQLite 共享连接必须串行化，与 FAISS 共用同一把锁
    def _exec_write(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _exec_fetchall(self, sql: str, params=()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _exec_fetchone(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _init_db(self):
        """初始化 SQLite 表（复用长连接）。"""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("""
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
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
            self._conn.commit()

    def close(self):
        """关闭数据库连接（插件卸载时调用）。"""
        try:
            self._conn.close()
        except Exception as e:
            logger.debug("[Quill RAG] vector_store close 失败: %s", e)

    def _load_index(self):
        """加载 FAISS 索引（如存在）。

        S2-10 修复：若已有索引 dim 与 expected_dim 不一致，重建索引并清空孤儿 FAISS ID。
        """
        try:
            import faiss
            if os.path.exists(self.index_path):
                self._index = faiss.read_index(self.index_path)
                loaded_dim = self._index.d
                if loaded_dim != self.expected_dim:
                    # 切换了 embedding provider，维度不匹配，重建索引
                    logger.warning(
                        f"[Quill RAG] FAISS 索引 dim={loaded_dim} 与期望 dim={self.expected_dim} 不一致，"
                        f"重建索引（旧文档需重新上传）。"
                    )
                    self._index = None
                    try:
                        os.remove(self.index_path)
                    except OSError as e:
                        logger.debug("[Quill RAG] 删除旧索引文件失败（可忽略）: %s", e)
                    # 清空 SQLite 中的孤儿 faiss_id（指向已失效的索引）
                    self._exec_write(
                        "UPDATE chunks SET faiss_id = -1 WHERE faiss_id >= 0"
                    )
                    self.dim = self.expected_dim
                    self._create_index()
                else:
                    self.dim = loaded_dim
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
        """F11 修复：SQLite 先写 pending 行（faiss_id=-1）拿 rowid → FAISS 写入 →
        失败则回滚 SQLite → 成功则回填 faiss_id。保证两库一致性。

        S1-3 修复：FAISS ID 直接用 SQLite row_id（AUTOINCREMENT 单调递增、全局唯一），
        彻底删除基于 ntotal 的 ID 生成逻辑（删除后 ntotal 下降会撞库）。
        S1-4 修复：IndexFlatIP 计算内积，add/search 前必须 L2 归一化，否则非真余弦相似度。
        """
        if not texts or not embeddings:
            return
        if doc_id == "":
            doc_id = source

        # 1. SQLite 单条插入，精确拿每行 rowid（faiss_id=-1 标记 pending）
        row_ids = []
        with self._lock:
            for i, text in enumerate(texts):
                cur = self._conn.execute(
                    "INSERT INTO chunks (doc_id, source, chunk_index, content, faiss_id) "
                    "VALUES (?, ?, ?, ?, -1)",
                    (doc_id, source, i, text)
                )
                row_ids.append(cur.lastrowid)
            self._conn.commit()

        # 2. FAISS 写入
        if self._index is not None:
            try:
                # S1-3: 用 row_ids 作为 FAISS ID（全局唯一，不会因删除而撞库）
                ids = np.array(row_ids, dtype=np.int64)
                # S1-4: L2 归一化，使 IndexFlatIP 等价于余弦相似度
                emb_array = np.array(embeddings, dtype=np.float32)
                norms = np.linalg.norm(emb_array, axis=1, keepdims=True)
                norms[norms == 0] = 1.0  # 防止除零
                emb_array = emb_array / norms
                with self._lock:
                    self._index.add_with_ids(emb_array, ids)
                    self._save_index()
            except Exception as e:
                # 3. FAISS 失败：回滚 SQLite（用精确 row_ids 删除 pending 行）
                with self._lock:
                    placeholders = ",".join("?" for _ in row_ids)
                    self._conn.execute(
                        f"DELETE FROM chunks WHERE id IN ({placeholders})",
                        row_ids
                    )
                    self._conn.commit()
                logger.warning(f"[Quill RAG] FAISS 写入失败，已回滚 {len(row_ids)} 行 SQLite: {e}")
                return
            # 4. 回填 faiss_id（S1-3 后 faiss_id == row_id，但仍写入以保持一致性和 search 性能）
            with self._lock:
                for rid in row_ids:
                    self._conn.execute(
                        "UPDATE chunks SET faiss_id = ? WHERE id = ?",
                        (rid, rid)
                    )
                self._conn.commit()
        else:
            # FAISS 未初始化，SQLite 行保留 faiss_id=-1（search 会过滤）
            return

    def search(self, query_embedding: list[float], top_k: int = 9, allowed_sources: list[str] = None) -> list[dict]:
        """FAISS 检索，支持通过 allowed_sources 按文档 source 过滤。

        S1-4 修复：query 向量也需 L2 归一化。
        """
        if self._index is None or self._index.ntotal == 0:
            return []
        try:
            # S1-4: L2 归一化 query
            query = np.array([query_embedding], dtype=np.float32)
            qnorm = np.linalg.norm(query)
            if qnorm > 0:
                query = query / qnorm

            # 多召回一些以补偿幽灵向量。如果有文档过滤限制，大幅放大召回池以防被过滤空。
            recall_k = min(top_k * 3, self._index.ntotal)
            if allowed_sources is not None:
                recall_k = min(max(top_k * 10, 100), self._index.ntotal)

            with self._lock:
                scores, ids = self._index.search(query, recall_k)

            results = []
            try:
                for score, idx in zip(scores[0], ids[0]):
                    if idx < 0:
                        continue
                    row = self._exec_fetchone(
                        "SELECT content, source, chunk_index, faiss_id FROM chunks WHERE faiss_id = ?",
                        (int(idx),)
                    )

                    if row:
                        # F11 修复：过滤 faiss_id 无效的孤儿行（pending 行 faiss_id=-1）
                        # S1-3 修复后 row_id 从 1 开始，0 不再是合法 ID，但仍用 < 0 过滤更安全
                        if row[3] is not None and row[3] < 0:
                            continue
                        doc_source = row[1]
                        if allowed_sources is not None and doc_source not in allowed_sources:
                            continue

                        results.append({
                            "content": row[0],
                            "source": doc_source,
                            "chunk_index": row[2],
                            "score": float(score),
                        })

                        if len(results) >= top_k:
                            break
            except Exception as e:
                logger.warning(f"[Quill RAG] SQLite 检索失败: {e}")
            return results
        except Exception as e:
            logger.warning(f"[Quill RAG] FAISS 检索失败: {e}")
            return []

    def delete_by_source(self, source: str) -> int:
        """删除某文档的所有块，并尝试从 FAISS 索引中移除对应向量。"""
        try:
            rows = self._exec_fetchall(
                "SELECT faiss_id FROM chunks WHERE source = ? AND faiss_id >= 0", (source,)
            )
            to_remove = np.array([r[0] for r in rows], dtype=np.int64)
        except Exception:
            to_remove = np.array([], dtype=np.int64)

        try:
            cursor = self._exec_write("DELETE FROM chunks WHERE source = ?", (source,))
            deleted = cursor.rowcount
        except Exception:
            deleted = 0

        if deleted > 0 and len(to_remove) > 0 and self._index is not None:
            try:
                with self._lock:
                    self._index.remove_ids(to_remove)
                    self._save_index()
                logger.info(f"[Quill RAG] 已从 FAISS 索引移除 {len(to_remove)} 条向量 (source={source})")
            except Exception:
                logger.info(f"[Quill RAG] FAISS remove_ids 失败 (source={source})，索引中将保留幽灵向量，"
                            "可通过 /doc reload 完全重建")

        return deleted

    def list_documents(self) -> list[dict]:
        """列出所有已上传文档。"""
        try:
            rows = self._exec_fetchall(
                "SELECT source, doc_id, COUNT(*) as chunk_count, MIN(created_at) as created_at "
                "FROM chunks GROUP BY source, doc_id"
            )
            return [
                {"source": r[0], "doc_id": r[1], "chunk_count": r[2], "created_at": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def get_stats(self) -> dict:
        """返回存储统计。"""
        try:
            total_chunks = self._exec_fetchone("SELECT COUNT(*) FROM chunks")[0]
            total_docs = self._exec_fetchone("SELECT COUNT(DISTINCT source) FROM chunks")[0]
        except Exception:
            total_chunks = 0
            total_docs = 0
        return {
            "total_chunks": total_chunks,
            "total_docs": total_docs,
            "faiss_vectors": self._index.ntotal if self._index else 0,
            "dim": self.dim,
        }

    def load_index(self):
        """重新加载 FAISS 索引（供 /doc reload 调用）。"""
        with self._lock:
            self._load_index()
