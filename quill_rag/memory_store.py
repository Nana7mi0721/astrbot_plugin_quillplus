# -*- coding: utf-8 -*-
"""MemoryStore — SQLite BLOB + NumPy 余弦相似度。用于动态记忆（session 隔离）。"""

from __future__ import annotations

import logging
import sqlite3
import struct
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class MemoryStore:
    """动态记忆存储：SQLite + BLOB 向量 + NumPy 余弦相似度。

    不使用 FAISS，因为：
    - 每个 session 的记忆条数有限（几十到几百条）
    - 原生 FAISS 不支持强力的元数据 SQL 过滤
    - SQLite BLOB + NumPy 逻辑简单 10 倍，且绝不会出 Bug
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化 SQLite 表。"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    chat_summary TEXT DEFAULT '',
                    vector BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_session ON chat_logs(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_ts ON chat_logs(timestamp)")
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self):
        """获取配置好的 SQLite 连接。"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _encode_vector(self, vector: list[float]) -> bytes:
        """将向量列表编码为 BLOB。"""
        if not vector:
            raise ValueError("Cannot encode empty vector")
        arr = np.array(vector, dtype=np.float32)
        if not np.all(np.isfinite(arr)):
            raise ValueError("Vector contains NaN or Inf")
        return arr.tobytes()

    def _decode_vector(self, blob: bytes, dim: int) -> np.ndarray:
        """从 BLOB 解码向量。"""
        expected = dim * 4  # float32 = 4 bytes
        if len(blob) != expected:
            raise ValueError(f"BLOB size {len(blob)} != expected {expected}")
        return np.frombuffer(blob, dtype=np.float32).copy()

    def add(self, session_id: str, summary: str, vector: list[float], chat_summary: str = ""):
        """添加一条记忆。"""
        if not session_id or not summary or not vector:
            return
        dim = len(vector)
        blob = self._encode_vector(vector)
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO memories (session_id, summary, chat_summary, vector, dim) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, summary, chat_summary, blob, dim)
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[Quill Memory] 添加记忆失败: {e}")
        finally:
            conn.close()

    def search(self, session_id: str, query_vector: list[float], top_k: int = 3) -> list[dict]:
        """按 session_id 隔离检索，NumPy 余弦相似度排序。"""
        if not session_id or not query_vector:
            return []

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, summary, chat_summary, vector, dim, timestamp FROM memories "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT 500",
                (session_id,)
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Quill Memory] 检索失败: {e}")
            return []
        finally:
            conn.close()

        if not rows:
            return []

        # 构建向量矩阵
        try:
            query = np.array(query_vector, dtype=np.float32)
            vectors = []
            valid_rows = []
            for row in rows:
                try:
                    vec = self._decode_vector(row[3], row[4])
                    if len(vec) == len(query):
                        vectors.append(vec)
                        valid_rows.append(row)
                except Exception:
                    continue

            if not vectors:
                return []

            matrix = np.stack(vectors)
            # 余弦相似度（epsilon 取 float32 精度上限，避免零除且减少失真）
            eps = np.finfo(np.float32).eps  # ~1.19e-7
            query_norm_val = np.linalg.norm(query)
            if query_norm_val < eps:
                return []  # 零向量无法计算相似度
            query_norm = query / query_norm_val
            matrix_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            # 过滤零向量行
            valid_mask = (matrix_norms.ravel() >= eps)
            if not np.any(valid_mask):
                return []
            matrix_normalized = matrix[valid_mask] / matrix_norms[valid_mask]
            similarities = matrix_normalized @ query_norm

            # 检查 NaN/Inf
            if not np.all(np.isfinite(similarities)):
                logger.warning("[Quill Memory] Similarities contain NaN/Inf")
                return []

            # 取 top_k（映射回 valid_rows 的索引）
            valid_indices = np.where(valid_mask)[0]
            top_local = np.argsort(similarities)[::-1][:top_k]
            top_indices = valid_indices[top_local]
            results = []
            for idx in top_indices:
                row = valid_rows[idx]
                results.append({
                    "id": row[0],
                    "summary": row[1],
                    "chat_summary": row[2],
                    "timestamp": row[5],
                    "score": float(similarities[idx]),
                })
            return results
        except Exception as e:
            logger.warning(f"[Quill Memory] 相似度计算失败: {e}")
            return []

    def list_sessions(self) -> list[dict]:
        """列出所有有记忆的 session。"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT session_id, COUNT(*) as count, MIN(timestamp) as first, "
                "MAX(timestamp) as last FROM memories GROUP BY session_id"
            ).fetchall()
            return [
                {"session_id": r[0], "count": r[1], "first": r[2], "last": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def list_memories(self, session_id: str, limit: int = 50) -> list[dict]:
        """列出某 session 的所有记忆。"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, summary, chat_summary, timestamp FROM memories "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return [
                {"id": r[0], "summary": r[1], "chat_summary": r[2], "timestamp": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def delete_session_memories(self, session_id: str) -> int:
        """删除某 session 的所有记忆。"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    def get_recent_chat_logs(self, session_id: str, limit: int = 8) -> list[dict]:
        """获取最近聊天记录（正序返回，供上下文恢复用）"""
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT role, content FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            result = [{"role": r[0], "content": r[1]} for r in rows]
            result.reverse()
            return result
        except Exception as e:
            logger.warning(f"[Quill Memory] 获取聊天日志失败: {e}")
            return []
        finally:
            conn.close()

    def log_message(self, session_id: str, role: str, content: str):
        """记录一条原始对话"""
        if not session_id or not content or not content.strip():
            return
        if role not in ("user", "assistant"):
            return
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO chat_logs (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content[:2000])
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[Quill Memory] 聊天日志记录失败: {e}")
        finally:
            conn.close()

    def list_chat_logs(self, session_id: str, limit: int = 200) -> list[dict]:
        """按 session 查询原始对话日志"""
        if not session_id:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return [
                {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def export_chat_logs(self, session_id: str, format: str = "markdown") -> str:
        """导出对话日志为文本格式"""
        if not session_id:
            return ""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            ).fetchall()
        finally:
            conn.close()

        if format == "txt":
            lines = [f"[{r[2]}] {r[0]}: {r[1]}" for r in rows]
            return "\n\n".join(lines)
        lines = [f"# 对话记录 — `{session_id}`\n"]
        for r in rows:
            role_label = "**用户**" if r[0] == "user" else "**AI**"
            lines.append(f"{role_label}: {r[1]}\n")
        return "\n".join(lines)

    def cleanup_chat_logs(self, retention_days: int) -> int:
        """清理超过保留天数的对话日志"""
        if retention_days <= 0:
            return 0
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM chat_logs WHERE timestamp < datetime('now', ?)",
                (f"-{retention_days} days",)
            )
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"[Quill Memory] 对话日志清理失败: {e}")
            return 0
        finally:
            conn.close()

    def delete_session_chat_logs(self, session_id: str) -> int:
        """删除某 session 的所有对话日志"""
        if not session_id:
            return 0
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete_memory(self, memory_id: int) -> bool:
        """删除单条记忆。"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """返回存储统计。"""
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM memories"
            ).fetchone()[0]
        finally:
            conn.close()
        return {"total_memories": total, "total_sessions": sessions}

    def list_all_memories(self, limit: int = 200) -> list[dict]:
        """列出全部记忆（跨 session），按创建时间倒序。

        供前端记忆浏览表格展示用，默认最新在前。
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, session_id, summary, timestamp FROM memories "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [
                {"id": r[0], "session_id": r[1], "summary": r[2], "timestamp": r[3]}
                for r in rows
            ]
        finally:
            conn.close()

    def search_all(self, query_vector: list[float], top_k: int = 5) -> list[dict]:
        """跨 session 向量检索（全局搜索，不限制 session_id）。

        供 Debug 用：直接传入 query_vector，返回全局最相关的 top_k 条。
        """
        if not query_vector:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, session_id, summary, chat_summary, vector, dim, timestamp FROM memories "
                "ORDER BY timestamp DESC LIMIT 2000"
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Quill Memory] 向量检索查询失败: {e}")
            return []
        finally:
            conn.close()

        if not rows:
            return []

        try:
            query = np.array(query_vector, dtype=np.float32)
            query_norm_val = np.linalg.norm(query)
            if query_norm_val < 1e-9:
                return []
            query_norm = query / query_norm_val

            valid_rows = []
            vectors = []
            for row in rows:
                try:
                    vec = np.frombuffer(row[4], dtype=np.float32)
                    if len(vec) != row[5]:
                        continue
                    norm = np.linalg.norm(vec)
                    if norm < 1e-9:
                        continue
                    valid_rows.append(row)
                    vectors.append(vec / norm)
                except Exception:
                    continue

            if not vectors:
                return []

            matrix = np.stack(vectors)
            similarities = matrix @ query_norm
            if not np.all(np.isfinite(similarities)):
                return []

            top_indices = np.argsort(similarities)[::-1][:top_k]
            results = []
            for idx in top_indices:
                row = valid_rows[idx]
                results.append({
                    "id": row[0],
                    "session_id": row[1],
                    "summary": row[2],
                    "chat_summary": row[3],
                    "timestamp": row[6],
                    "score": float(similarities[idx]),
                })
            return results
        except Exception as e:
            logger.warning(f"[Quill Memory] 向量检索计算失败: {e}")
            return []
