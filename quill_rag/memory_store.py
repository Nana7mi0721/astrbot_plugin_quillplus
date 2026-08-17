# -*- coding: utf-8 -*-
"""MemoryStore — SQLite BLOB + NumPy 余弦相似度。用于动态记忆（session 隔离）。"""

from __future__ import annotations

import logging
import sqlite3
import asyncio
import aiosqlite

import numpy as np
from collections import OrderedDict
import json

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
        self._conn = None
        self._lock = asyncio.Lock()
        self._cache = OrderedDict()
        self._MAX_CACHE = 50

    # F4 修复：SQLite 共享连接（check_same_thread=False）必须由调用方串行化。
    # 以下三个辅助方法统一在 self._lock 保护下执行 execute+commit/fetch。
    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path, timeout=10.0)
        await self._init_db()

    async def _exec_write(self, sql: str, params=()) -> aiosqlite.Cursor:
        """执行写操作并提交，返回 cursor（线程安全）"""
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cur

    async def _exec_fetchall(self, sql: str, params=()) -> list:
        """执行读操作并 fetchall，返回行列表（线程安全）"""
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchall()

    async def _exec_fetchone(self, sql: str, params=()):
        """执行读操作并 fetchone，返回单行或 None（线程安全）"""
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchone()

    async def _init_db(self):
        """初始化 SQLite 表（复用长连接）。"""
        async with self._lock:
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.execute("""
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
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)")
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_session ON chat_logs(session_id)")
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_ts ON chat_logs(timestamp)")
            
            await self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(summary, content, tokenize='unicode61');")
            # Create triggers to sync FTS
            await self._conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
              INSERT INTO memories_fts(rowid, summary, content) VALUES (new.id, new.summary, new.chat_summary);
            END;
            ''')
            await self._conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
              DELETE FROM memories_fts WHERE rowid = old.id;
            END;
            ''')
            await self._conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
              DELETE FROM memories_fts WHERE rowid = old.id;
              INSERT INTO memories_fts(rowid, summary, content) VALUES (new.id, new.summary, new.chat_summary);
            END;
            ''')
            await self._conn.commit()

            # Backfill FTS5
            # 注意：此处已在 self._lock 内，不能再调用 _exec_* 辅助方法（内部会重复获取锁导致死锁），
            # 必须直接使用 self._conn。
            try:
                cur = await self._conn.execute("SELECT COUNT(*) FROM memories_fts")
                row = await cur.fetchone()
                if row and row[0] == 0:
                    await self._conn.execute("INSERT INTO memories_fts(rowid, summary, content) SELECT id, summary, chat_summary FROM memories")
                    await self._conn.commit()
            except Exception as e:
                logger.warning("[Quill Memory] FTS5 回填失败: %s", e)

            # Schema 热迁移：新增记忆质量管理字段（兼容老数据库）
            for stmt in (
                "ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT '[]'",
                "ALTER TABLE memories ADD COLUMN strength INTEGER DEFAULT 10",
                "ALTER TABLE memories ADD COLUMN useful_count INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN useful_score REAL DEFAULT 0.0",
                "ALTER TABLE memories ADD COLUMN is_active INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN is_core INTEGER DEFAULT 0",
            ):
                try:
                    await self._conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            await self._conn.commit()

    
    async def _invalidate_cache(self, session_id: str):
        if session_id in self._cache:
            del self._cache[session_id]

    async def clear_cache(self):
        """P2-6: 清空所有 LRU 缓存。在配置变更或手动清理时调用。"""
        self._cache.clear()

    async def update_core_memory(self, session_id: str, new_traits: str, crucial_facts: str):
        await self._invalidate_cache(session_id)
        # Check if core memory exists
        rows = await self._exec_fetchall("SELECT id, summary FROM memories WHERE session_id = ? AND is_core = 1", (session_id,))
        core_content = json.dumps({"traits": new_traits, "facts": crucial_facts}, ensure_ascii=False)
        if rows:
            await self._exec_write("UPDATE memories SET summary = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?", (core_content, rows[0][0]))
        else:
            await self._exec_write("INSERT INTO memories (session_id, summary, vector, dim, is_core) VALUES (?, ?, ?, ?, ?)", (session_id, core_content, b'', 0, 1))


    async def close(self):
        """关闭数据库连接（插件卸载时调用）。"""
        try:
            if self._conn:
                await self._conn.close()
        except Exception as e:
            logger.debug("[Quill Memory] close 失败: %s", e)

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

    async def add(self, session_id: str, summary: str, vector: list[float], chat_summary: str = ""):
        """添加一条记忆。"""
        if not session_id or not summary or not vector:
            return
        dim = len(vector)
        blob = self._encode_vector(vector)
        try:
            await self._exec_write(
                "INSERT INTO memories (session_id, summary, chat_summary, vector, dim) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, summary, chat_summary, blob, dim)
            )
            await self._invalidate_cache(session_id)
        except Exception as e:
            logger.warning(f"[Quill Memory] 添加记忆失败: {e}")


    async def _get_cached_vectors(self, session_id: str):
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
            return self._cache[session_id]
        
        # Load all active memories for session
        rows = await self._exec_fetchall(
            '''SELECT id, summary, chat_summary, vector, dim, timestamp,
                      strength, useful_count, useful_score, is_active,
                      is_core
               FROM memories
               WHERE session_id = ?
                 AND (is_core = 1 OR is_active = 1 OR (julianday('now') - julianday(timestamp)) < 30)
               ORDER BY timestamp DESC LIMIT 1000''',
            (session_id,)
        )
        if not rows:
            return None, None
            
        vectors = []
        valid_rows = []
        import numpy as np
        for row in rows:
            # 核心记忆行（update_core_memory 写入 vector=b''、dim=0）不携带向量，
            # 跳过之：核心记忆经 get_core_memories 独立注入；且空向量与正常维度
            # 向量 np.stack 会因形状不一致抛 ValueError，导致该会话检索整体失效。
            if not row[3] or not row[4]:
                continue
            try:
                vec = self._decode_vector(row[3], row[4])
                vectors.append(vec)
                valid_rows.append(row)
            except Exception:
                pass
                
        if not vectors:
            return None, None

        try:
            matrix = np.stack(vectors)
        except ValueError as e:
            # P1-4: 维度不匹配时（如 Embedding 提供商切换后旧向量残留），跳过该会话
            logger.warning("[Quill Memory] 向量维度不匹配，跳过会话 %s: %s", session_id, e)
            return None, None
        self._cache[session_id] = (valid_rows, matrix)
        if len(self._cache) > getattr(self, '_MAX_CACHE', 50):
            self._cache.popitem(last=False)
            
        return valid_rows, matrix

    async def search(self, session_id: str, query_vector: list[float], top_k: int = 3, query_text: str = "") -> list[dict]:
        if not session_id or not query_vector:
            return []
            
        # 1. FTS5 BM25 search
        fts_scores = {}
        if query_text:
            try:
                # Basic tokenization for FTS
                safe_query = query_text.replace('"', '').replace("'", "")
                fts_rows = await self._exec_fetchall(
                    "SELECT rowid, bm25(memories_fts) FROM memories_fts WHERE memories_fts MATCH ? LIMIT 50",
                    (safe_query,)
                )
                for row in fts_rows:
                    fts_scores[row[0]] = -row[1] # BM25 returns negative scores in SQLite
            except Exception:
                pass
                
        # 2. Vector Search (cached)
        import numpy as np
        rows, matrix = await self._get_cached_vectors(session_id)
        if not rows:
            return []

query = np.array(query_vector, dtype=np.float32)
        # P1-4: 维度校验 — 查询向量维度与存储向量不匹配时跳过（Embedding 切换后）
        if query.shape[0] != matrix.shape[1]:
            logger.warning("[Quill Memory] 查询向量维度 %d 与存储向量 %d 不匹配，跳过搜索", query.shape[0], matrix.shape[1])
            return []

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
        
        results = []
        idx = 0
        for i, is_valid in enumerate(valid_mask):
            if is_valid:
                row = rows[i]
                sim = float(similarities[idx])
                idx += 1
                
                # Ebbinghaus decay: decay = exp(-lambda * days)
                import datetime
                try:
                    ts = datetime.datetime.strptime(row[5], "%Y-%m-%d %H:%M:%S")
                    # SQLite CURRENT_TIMESTAMP 为 naive UTC，统一按 naive UTC 比较
                    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                    age_days = (now_utc - ts).total_seconds() / 86400.0
                except Exception:
                    age_days = 0.0
                decay = np.exp(-0.02 * age_days)
                
                # Frequency score
                useful_count = row[7]
                freq_boost = min(1.0, useful_count * 0.1)
                
                final_vec_score = sim * decay + freq_boost * 0.2
                
                row_id = row[0]
                results.append({
                    "id": row_id,
                    "summary": row[1],
                    "chat_summary": row[2],
                    "timestamp": row[5],
                    "strength": row[6],
                    "useful_count": useful_count,
                    "age_days": age_days,
                    "is_core": row[10],
                    "vec_score": final_vec_score,
                    "fts_score": fts_scores.get(row_id, 0.0)
                })
                
        if not results:
            return []
            
        # RRF (Reciprocal Rank Fusion)
        results.sort(key=lambda x: x["vec_score"], reverse=True)
        for rank, r in enumerate(results):
            r["vec_rank"] = rank + 1
            
        results.sort(key=lambda x: x["fts_score"], reverse=True)
        for rank, r in enumerate(results):
            r["fts_rank"] = rank + 1 if r["fts_score"] > 0 else 1000
            
        k = 60
        for r in results:
            r["rrf_score"] = (1.0 / (k + r["vec_rank"])) + (1.0 / (k + r["fts_rank"]) if r["fts_rank"] < 1000 else 0)
            
        # Ignore core memories in top-k since they are injected automatically
        non_core = [r for r in results if not r["is_core"]]
        non_core.sort(key=lambda x: x["rrf_score"], reverse=True)
        return non_core[:top_k]

    async def mark_memories_used(self, memory_ids: list[int], score_add: float = 1.5):
        """更新被召回记忆的有用性统计。"""
        if not memory_ids:
            return
        try:
            placeholders = ",".join("?" for _ in memory_ids)
            # Find sessions to invalidate cache
            rows = await self._exec_fetchall(f"SELECT DISTINCT session_id FROM memories WHERE id IN ({placeholders})", tuple(memory_ids))
            for r in rows:
                await self._invalidate_cache(r[0])
            await self._exec_write(
                f"""UPDATE memories
                    SET useful_count = useful_count + 1,
                        useful_score = useful_score + ?,
                        strength = MIN(100, strength + 1),
                        is_active = 1
                    WHERE id IN ({placeholders})""",
                [score_add] + memory_ids
            )
        except Exception as e:
            logger.warning(f"[Quill Memory] 更新记忆有用性失败: {e}")

    async def prune_memories(self) -> int:
        """分档遗忘清理任务（无情斩杀低价值记忆）。核心记忆(is_core=1)永不清理。"""
        try:
            # P3-2 修复：除原有 is_active=0 清理外，对 is_active=1 但超过 60 天
            # 未更新的低价值记忆也执行降级清理，避免记忆表无限膨胀。
            cursor = await self._exec_write("""
                DELETE FROM memories
                WHERE is_core = 0 AND (
                    (is_active = 0 AND (
                        (useful_score < 3 AND julianday('now') - julianday(timestamp) > 3)
                        OR
                        (useful_score >= 3 AND useful_score < 10 AND julianday('now') - julianday(timestamp) > 9)
                    ))
                    OR
                    (is_active = 1 AND useful_score < 10
                     AND julianday('now') - julianday(timestamp) > 60)
                )
            """)
            deleted = cursor.rowcount
            if deleted > 0:
                # 被删除的记忆可能仍在会话向量缓存中（幽灵召回），直接清空
                self._cache.clear()
                logger.info(f"[Quill Memory] 记忆修剪: 清理了 {deleted} 条过期低价值记忆")
            return deleted
        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆修剪失败: {e}")
            return 0

    async def get_chat_logs_after(self, session_id: str, after_id: int, limit: int = 50) -> list[dict]:
        """获取指定 session 中 after_id 之后的对话日志（增量读取）。"""
        if not session_id:
            return []
        try:
            rows = await self._exec_fetchall(
                "SELECT id, role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (session_id, after_id, limit)
            )
            return [
                {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill Memory] get_chat_logs_after 失败: %s", e)
            return []

    async def list_memories(self, session_id: str, limit: int = 50) -> list[dict]:
        """列出某 session 的所有记忆。"""
        if not session_id:
            return []
        try:
            rows = await self._exec_fetchall(
                "SELECT id, summary, chat_summary, timestamp, strength, useful_count, useful_score, is_active, is_core FROM memories "
                "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit)
            )
            return [
                {
                    "id": r[0], "summary": r[1], "chat_summary": r[2], "timestamp": r[3],
                    "strength": r[4], "useful_count": r[5], "useful_score": r[6], "is_active": r[7],
                    "is_core": r[8]
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill Memory] list_memories 失败: %s", e)
            return []

    async def set_core(self, memory_id: int, is_core: bool) -> bool:
        """设置/取消记忆的核心锚定状态。核心记忆不参与 Top-K 竞争，直接注入 prompt。"""
        try:
            # 缓存中的 is_core 标记需要同步失效，否则置锚后的记忆仍参与 Top-K 竞争
            rows = await self._exec_fetchall("SELECT session_id FROM memories WHERE id = ?", (memory_id,))
            if rows:
                await self._invalidate_cache(rows[0][0])
            cursor = await self._exec_write(
                "UPDATE memories SET is_core = ? WHERE id = ?",
                (1 if is_core else 0, memory_id)
            )
            return cursor.rowcount > 0
        except Exception as e:
            logger.warning("[Quill Memory] set_core 失败: %s", e)
            return False

    async def get_core_memories(self, session_id: str) -> list[dict]:
        """获取某 session 的所有核心记忆（is_core=1），无条件注入 prompt。"""
        if not session_id:
            return []
        try:
            rows = await self._exec_fetchall(
                "SELECT id, summary FROM memories WHERE session_id = ? AND is_core = 1 ORDER BY timestamp DESC",
                (session_id,)
            )
            return [{"id": r[0], "summary": r[1]} for r in rows]
        except Exception as e:
            logger.warning("[Quill Memory] get_core_memories 失败: %s", e)
            return []

    async def delete_session_memories(self, session_id: str) -> int:
        """删除某 session 的所有记忆。"""
        if not session_id:
            return 0
        try:
            cursor = await self._exec_write("DELETE FROM memories WHERE session_id = ?", (session_id,))
            # 失效向量缓存，否则已删记忆仍会从缓存被召回
            await self._invalidate_cache(session_id)
            return cursor.rowcount
        except Exception as e:
            logger.warning("[Quill Memory] delete_session_memories 失败: %s", e)
            return 0

    async def delete_all_session_memories(self, target_id: str) -> int:
        """删除某 target_id 下所有 session 的记忆（含 target_id 本身和 target_id::* 所有 persona）。

        用于 /quill reset 场景：用户可能切换过多个角色卡，每个 persona 有独立的
        mem_session_id（target_id::persona_id）。此方法一次性清理全部。
        """
        if not target_id:
            return 0
        try:
            cursor = await self._exec_write(
                "DELETE FROM memories WHERE session_id = ? OR session_id LIKE ?",
                (target_id, target_id + "::%"),
            )
            # 按 target_id 前缀失效所有 persona 维度的会话缓存
            prefix = target_id + "::"
            for key in [k for k in self._cache if k == target_id or k.startswith(prefix)]:
                await self._invalidate_cache(key)
            return cursor.rowcount
        except Exception as e:
            logger.warning("[Quill Memory] delete_all_session_memories 失败: %s", e)
            return 0

    async def count_session_memories(self, session_id: str) -> int:
        """统计某 session 的记忆总数（供分页显示真实 total）。"""
        if not session_id:
            return 0
        try:
            row = await self._exec_fetchone(
                "SELECT COUNT(*) FROM memories WHERE session_id = ?", (session_id,)
            )
            return row[0] if row else 0
        except Exception as e:
            logger.warning("[Quill Memory] count_session_memories 失败: %s", e)
            return 0

    async def get_recent_chat_logs(self, session_id: str, limit: int = 8) -> list[dict]:
        """获取最近聊天记录（正序返回，供上下文恢复用）"""
        if not session_id:
            return []
        try:
            rows = await self._exec_fetchall(
                "SELECT role, content FROM chat_logs "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            )
            result = [{"role": r[0], "content": r[1]} for r in rows]
            result.reverse()
            return result
        except Exception as e:
            logger.warning(f"[Quill Memory] 获取聊天日志失败: {e}")
            return []

    async def log_message(self, session_id: str, role: str, content: str):
        """记录一条原始对话"""
        if not session_id or not content or not content.strip():
            return
        if role not in ("user", "assistant"):
            return
        try:
            await self._exec_write(
                "INSERT INTO chat_logs (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content[:2000])
            )
        except Exception as e:
            logger.warning(f"[Quill Memory] 聊天日志记录失败: {e}")

    async def list_chat_logs(self, session_id: str, limit: int = 200) -> list[dict]:
        """按 session 查询原始对话日志"""
        if not session_id:
            return []
        try:
            rows = await self._exec_fetchall(
                "SELECT id, role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
                (session_id, limit)
            )
            return [
                {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill Memory] list_chat_logs 失败: %s", e)
            return []

    async def export_chat_logs(self, session_id: str, format: str = "markdown") -> str:
        """导出对话日志为文本格式"""
        if not session_id:
            return ""
        try:
            rows = await self._exec_fetchall(
                "SELECT role, content, timestamp FROM chat_logs "
                "WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            )
        except Exception as e:
            logger.warning("[Quill Memory] export_chat_logs 失败: %s", e)
            return ""

        if format == "txt":
            lines = [f"[{r[2]}] {r[0]}: {r[1]}" for r in rows]
            return "\n\n".join(lines)
        lines = [f"# 对话记录 — `{session_id}`\n"]
        for r in rows:
            role_label = "**用户**" if r[0] == "user" else "**AI**"
            lines.append(f"{role_label}: {r[1]}\n")
        return "\n".join(lines)

    async def cleanup_chat_logs(self, retention_days: int) -> int:
        """清理超过保留天数的对话日志"""
        if retention_days <= 0:
            return 0
        try:
            cursor = await self._exec_write(
                "DELETE FROM chat_logs WHERE timestamp < datetime('now', ?)",
                (f"-{retention_days} days",)
            )
            return cursor.rowcount
        except Exception as e:
            logger.warning(f"[Quill Memory] 对话日志清理失败: {e}")
            return 0

    async def delete_session_chat_logs(self, session_id: str) -> int:
        """删除某 session 的所有对话日志"""
        if not session_id:
            return 0
        try:
            cursor = await self._exec_write("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
            return cursor.rowcount
        except Exception as e:
            logger.warning("[Quill Memory] delete_session_chat_logs 失败: %s", e)
            return 0

    async def delete_all_session_chat_logs(self, target_id: str) -> int:
        """删除某 target_id 下所有 session 的对话日志（含 target_id 本身和 target_id::* 所有 persona）。

        用于 /quill reset 场景：清理所有角色卡的对话日志，防止切换角色卡后
        Context Restoration 垫入旧上下文。
        """
        if not target_id:
            return 0
        try:
            cursor = await self._exec_write(
                "DELETE FROM chat_logs WHERE session_id = ? OR session_id LIKE ?",
                (target_id, target_id + "::%"),
            )
            return cursor.rowcount
        except Exception as e:
            logger.warning("[Quill Memory] delete_all_session_chat_logs 失败: %s", e)
            return 0

    async def delete_memory(self, memory_id: int) -> bool:
        """删除单条记忆。"""
        try:
            rows = await self._exec_fetchall("SELECT session_id FROM memories WHERE id = ?", (memory_id,))
            if rows:
                await self._invalidate_cache(rows[0][0])
        except Exception:
            pass
        
        try:
            cursor = await self._exec_write("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0
        except Exception as e:
            logger.warning("[Quill Memory] delete_memory 失败: %s", e)
            return False

    async def get_stats(self) -> dict:
        """返回存储统计。"""
        try:
            total = (await self._exec_fetchone("SELECT COUNT(*) FROM memories"))[0]
            sessions = (await self._exec_fetchone(
                "SELECT COUNT(DISTINCT session_id) FROM memories"
            ))[0]
            today = (await self._exec_fetchone(
                "SELECT COUNT(*) FROM memories WHERE date(timestamp) = date('now')"
            ))[0]
        except Exception as e:
            logger.warning("[Quill Memory] get_stats 失败: %s", e)
            return {"total_memories": 0, "total_sessions": 0, "today_count": 0}
        return {"total_memories": total, "total_sessions": sessions, "today_count": today}

    async def list_all_memories(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """列出全部记忆（跨 session），按创建时间倒序，支持分页。"""
        try:
            rows = await self._exec_fetchall(
                "SELECT id, session_id, summary, chat_summary, timestamp, strength, useful_count, useful_score, is_active, is_core FROM memories "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            return [
                {
                    "id": r[0], "session_id": r[1], "summary": r[2], "chat_summary": r[3],
                    "timestamp": r[4], "strength": r[5], "useful_count": r[6],
                    "useful_score": r[7], "is_active": r[8], "is_core": r[9]
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill Memory] list_all_memories 失败: %s", e)
            return []

    async def list_sessions(self) -> list[dict]:
        """P2-1: 列出所有有记忆的会话，按最近更新时间倒序。"""
        try:
            rows = await self._exec_fetchall(
                "SELECT session_id, COUNT(*) as mem_count, MAX(timestamp) as last_active "
                "FROM memories GROUP BY session_id ORDER BY last_active DESC"
            )
            return [
                {"session_id": r[0], "mem_count": r[1], "last_active": r[2]}
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill Memory] list_sessions 失败: %s", e)
            return []

    async def count_all_memories(self) -> int:
        """返回全部记忆总数（用于分页统计）。"""
        try:
            return (await self._exec_fetchone("SELECT COUNT(*) FROM memories"))[0]
        except Exception as e:
            logger.warning("[Quill Memory] count_all_memories 失败: %s", e)
            return 0

    async def get_memory_by_id(self, memory_id: int) -> dict | None:
        """获取单条记忆完整详情（含 chat_summary）。"""
        try:
            row = await self._exec_fetchone(
                "SELECT id, session_id, summary, chat_summary, timestamp, "
                "strength, useful_count, useful_score, is_active, is_core FROM memories WHERE id = ?",
                (memory_id,)
            )
            if not row:
                return None
            return {
                "id": row[0], "session_id": row[1], "summary": row[2],
                "chat_summary": row[3], "timestamp": row[4],
                "strength": row[5], "useful_count": row[6],
                "useful_score": row[7], "is_active": row[8], "is_core": row[9]
            }
        except Exception as e:
            logger.warning("[Quill Memory] get_memory_by_id 失败: %s", e)
            return None

    async def search_all(self, query_vector: list[float], top_k: int = 5, session_ids: list[str] | None = None) -> list[dict]:
        """跨 session 向量检索（全局搜索，不限制 session_id）。

        P2-7 修复：新增可选 session_ids 白名单参数。传入时仅检索指定会话的记忆，
        None 表示全量（保持向后兼容，仅限管理面板等受信任调用方使用）。
        """
        if not query_vector:
            return []
        try:
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                rows = await self._exec_fetchall(
                    f"SELECT id, session_id, summary, chat_summary, vector, dim, timestamp FROM memories "
                    f"WHERE session_id IN ({placeholders}) "
                    f"ORDER BY timestamp DESC LIMIT 2000",
                    list(session_ids)
                )
            else:
                rows = await self._exec_fetchall(
                    "SELECT id, session_id, summary, chat_summary, vector, dim, timestamp FROM memories "
                    "ORDER BY timestamp DESC LIMIT 2000"
                )
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
