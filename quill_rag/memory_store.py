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
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        """初始化 SQLite 表（复用长连接）。"""
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("""
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
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_session ON chat_logs(session_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_ts ON chat_logs(timestamp)")
        self._conn.commit()

        # Schema 热迁移：新增记忆质量管理字段（兼容老数据库）
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN strength INTEGER DEFAULT 10")
        except Exception:
            pass
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN useful_count INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN useful_score REAL DEFAULT 0.0")
        except Exception:
            pass
        try:
            self._conn.execute("ALTER TABLE memories ADD COLUMN is_active INTEGER DEFAULT 0")
        except Exception:
            pass
        self._conn.commit()

    def close(self):
        """关闭数据库连接（插件卸载时调用）。"""
        try:
            self._conn.close()
        except Exception:
            pass

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
        try:
            self._conn.execute(
                "INSERT INTO memories (session_id, summary, chat_summary, vector, dim) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, summary, chat_summary, blob, dim)
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"[Quill Memory] 添加记忆失败: {e}")

    def search(self, session_id: str, query_vector: list[float], top_k: int = 3) -> list[dict]:
        """按 session_id 隔离检索，NumPy 余弦相似度排序，含时间衰减。"""
        if not session_id or not query_vector:
            return []

        try:
            rows = self._conn.execute(
                """SELECT id, summary, chat_summary, vector, dim, timestamp,
                          strength, useful_count, useful_score, is_active,
                          (julianday('now') - julianday(timestamp)) AS age_days
                   FROM memories WHERE session_id = ? ORDER BY timestamp DESC LIMIT 500""",
                (session_id,)
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Quill Memory] 检索失败: {e}")
            return []

        if not rows:
            return []

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
            eps = np.finfo(np.float32).eps
            query_norm_val = np.linalg.norm(query)
            if query_norm_val < eps:
                return []
            query_norm = query / query_norm_val
            matrix_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            valid_mask = (matrix_norms.ravel() >= eps)
            if not np.any(valid_mask):
                return []
            matrix_normalized = matrix[valid_mask] / matrix_norms[valid_mask]
            similarities = matrix_normalized @ query_norm

            if not np.all(np.isfinite(similarities)):
                logger.warning("[Quill Memory] Similarities contain NaN/Inf")
                return []

            # 时间衰减：is_active=1 的记忆不衰减，被动记忆按 age_days 衰减
            final_scores = []
            for idx, sim in enumerate(similarities):
                row = valid_rows[idx]
                is_active = row[9]
                age_days = float(row[10])
                if is_active:
                    final_scores.append(float(sim))
                else:
                    decay_factor = 1.0 / (1.0 + 0.05 * max(0, age_days))
                    final_scores.append(float(sim) * decay_factor)

            final_scores = np.array(final_scores)
            valid_indices = np.where(valid_mask)[0]
            top_local = np.argsort(final_scores)[::-1][:top_k]
            top_indices = valid_indices[top_local]
            results = []
            for idx in top_indices:
                row = valid_rows[idx]
                results.append({
                    "id": row[0],
                    "summary": row[1],
                    "chat_summary": row[2],
                    "timestamp": row[5],
                    "strength": row[6],
                    "useful_count": row[7],
                    "useful_score": float(row[8]),
                    "score": float(final_scores[idx]),
                })
            return results
        except Exception as e:
            logger.warning(f"[Quill Memory] 相似度计算失败: {e}")
            return []

    def mark_memories_used(self, memory_ids: list[int], score_add: float = 1.5):
        """更新被召回记忆的有用性统计。"""
        if not memory_ids:
            return
        try:
            placeholders = ",".join("?" for _ in memory_ids)
            self._conn.execute(
                f"""UPDATE memories
                    SET useful_count = useful_count + 1,
                        useful_score = useful_score + ?,
                        strength = MIN(100, strength + 1)
                    WHERE id IN ({placeholders})""",
                [score_add] + memory_ids
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"[Quill Memory] 更新记忆有用性失败: {e}")

    def prune_memories(self) -> int:
        """分档遗忘清理任务（无情斩杀低价值记忆）。"""
        try:
            cursor = self._conn.execute("""
                DELETE FROM memories
                WHERE is_active = 0 AND (
                    (useful_score < 3 AND julianday('now') - julianday(timestamp) > 3)
                    OR
                    (useful_score >= 3 AND useful_score < 10 AND julianday('now') - julianday(timestamp) > 9)
                )
            """)
            deleted = cursor.rowcount
            self._conn.commit()
            if deleted > 0:
                logger.info(f"[Quill Memory] 记忆修剪: 清理了 {deleted} 条过期低价值记忆")
            return deleted
        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆修剪失败: {e}")
            return 0

    def get_chat_logs_after(self, session_id: str, after_id: int, limit: int = 50) -> list[dict]:
        """获取指定 session 中 after_id 之后的对话日志（增量读取）。"""
        if not session_id:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (session_id, after_id, limit)
            ).fetchall()
            return [
                {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def list_sessions(self) -> list[dict]:
        """列出所有有记忆的 session。"""
        try:
            rows = self._conn.execute(
                "SELECT session_id, COUNT(*) as count, MIN(timestamp) as first, "
                "MAX(timestamp) as last FROM memories GROUP BY session_id"
            ).fetchall()
            return [
                {"session_id": r[0], "count": r[1], "first": r[2], "last": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def list_memories(self, session_id: str, limit: int = 50) -> list[dict]:
        """列出某 session 的所有记忆。"""
        if not session_id:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, summary, chat_summary, timestamp, strength, useful_count, useful_score, is_active FROM memories "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return [
                {
                    "id": r[0], "summary": r[1], "chat_summary": r[2], "timestamp": r[3],
                    "strength": r[4], "useful_count": r[5], "useful_score": r[6], "is_active": r[7]
                }
                for r in rows
            ]
        except Exception:
            return []

    def delete_session_memories(self, session_id: str) -> int:
        """删除某 session 的所有记忆。"""
        if not session_id:
            return 0
        try:
            cursor = self._conn.execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
            self._conn.commit()
            return cursor.rowcount
        except Exception:
            return 0

    def get_recent_chat_logs(self, session_id: str, limit: int = 8) -> list[dict]:
        """获取最近聊天记录（正序返回，供上下文恢复用）"""
        if not session_id:
            return []
        try:
            rows = self._conn.execute(
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

    def log_message(self, session_id: str, role: str, content: str):
        """记录一条原始对话"""
        if not session_id or not content or not content.strip():
            return
        if role not in ("user", "assistant"):
            return
        try:
            self._conn.execute(
                "INSERT INTO chat_logs (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content[:2000])
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"[Quill Memory] 聊天日志记录失败: {e}")

    def list_chat_logs(self, session_id: str, limit: int = 200) -> list[dict]:
        """按 session 查询原始对话日志"""
        if not session_id:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return [
                {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def export_chat_logs(self, session_id: str, format: str = "markdown") -> str:
        """导出对话日志为文本格式"""
        if not session_id:
            return ""
        try:
            rows = self._conn.execute(
                "SELECT role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            ).fetchall()
        except Exception:
            return ""

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
        try:
            cursor = self._conn.execute(
                "DELETE FROM chat_logs WHERE timestamp < datetime('now', ?)",
                (f"-{retention_days} days",)
            )
            self._conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"[Quill Memory] 对话日志清理失败: {e}")
            return 0

    def delete_session_chat_logs(self, session_id: str) -> int:
        """删除某 session 的所有对话日志"""
        if not session_id:
            return 0
        try:
            cursor = self._conn.execute("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
            self._conn.commit()
            return cursor.rowcount
        except Exception:
            return 0

    def delete_memory(self, memory_id: int) -> bool:
        """删除单条记忆。"""
        try:
            cursor = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return cursor.rowcount > 0
        except Exception:
            return False

    def get_stats(self) -> dict:
        """返回存储统计。"""
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM memories"
            ).fetchone()[0]
        except Exception:
            return {"total_memories": 0, "total_sessions": 0}
        return {"total_memories": total, "total_sessions": sessions}

    def list_all_memories(self, limit: int = 200) -> list[dict]:
        """列出全部记忆（跨 session），按创建时间倒序。"""
        try:
            rows = self._conn.execute(
                "SELECT id, session_id, summary, timestamp, strength, useful_count, useful_score, is_active FROM memories "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [
                {
                    "id": r[0], "session_id": r[1], "summary": r[2], "timestamp": r[3],
                    "strength": r[4], "useful_count": r[5], "useful_score": r[6], "is_active": r[7]
                }
                for r in rows
            ]
        except Exception:
            return []

    def search_all(self, query_vector: list[float], top_k: int = 5) -> list[dict]:
        """跨 session 向量检索（全局搜索，不限制 session_id）。"""
        if not query_vector:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, session_id, summary, chat_summary, vector, dim, timestamp FROM memories "
                "ORDER BY timestamp DESC LIMIT 2000"
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Quill Memory] 向量检索查询失败: {e}")
            return []

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
