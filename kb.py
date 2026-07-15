# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Async Writing Resource Manager (aiosqlite + FTS5)
================================================

Port of intimate_send v5.0 WritingResourceManager to fully async I/O.
Identical table schema — compatible with existing .db files.
"""

import os
import re as _re
import json
import sqlite3
from typing import List, Dict, Optional, Any

import aiosqlite

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class WritingResourceManager:
    """
    Async writing resource manager.

    Usage::
        async with WritingResourceManager(db_path) as wr:
            entries = await wr.match("some text")
    """

    # ------------------------------------------------------------------
    # Construction / lifecycle
    # ------------------------------------------------------------------

    def __init__(self, db_path: str, category_dedup_limit: int = 0):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self.category_dedup_limit = category_dedup_limit

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized. Call initialize() or use async with."
        return self._conn

    async def _connect(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path, timeout=10.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys = ON")

    async def _init_db(self):
        c = await self.conn.cursor()

        # Main table
        await c.execute("""
            CREATE TABLE IF NOT EXISTS writing_resource (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category VARCHAR(50) NOT NULL,
                entry_id VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(200),
                description TEXT,
                keywords TEXT NOT NULL,
                secondary_keywords TEXT,
                aliases TEXT,
                content TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                match_count INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                is_constant INTEGER DEFAULT 0,
                inject_position INTEGER DEFAULT 2,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes
        await c.execute("CREATE INDEX IF NOT EXISTS idx_category ON writing_resource(category)")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_priority ON writing_resource(priority DESC)")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_enabled ON writing_resource(enabled)")

        # FTS5 virtual table
        # S3-5: 指定 trigram 分词器，改善中文子串匹配（SQLite 3.34+）。
        # 若旧 SQLite 不支持 trigram，回退到默认 unicode61 分词器。
        try:
            await c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS writing_resource_fts USING fts5(
                    keywords, name, content,
                    content=writing_resource, content_rowid=id,
                    tokenize='trigram'
                )
            """)
        except sqlite3.Error as exc:
            logger.warning("[WR] trigram 分词器不可用，回退默认分词器: %s", exc)
            await c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS writing_resource_fts USING fts5(
                    keywords, name, content,
                    content=writing_resource, content_rowid=id
                )
            """)

        # Match logs
        await c.execute("""
            CREATE TABLE IF NOT EXISTS match_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_input TEXT NOT NULL,
                matched_entries TEXT,
                match_count INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Timestamp trigger
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS update_writing_resource_timestamp
            AFTER UPDATE ON writing_resource
            BEGIN
                UPDATE writing_resource SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END
        """)

        # FTS5 sync triggers
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS writing_resource_ai_insert
            AFTER INSERT ON writing_resource
            BEGIN
                INSERT INTO writing_resource_fts(rowid, keywords, name, content)
                VALUES (NEW.id, NEW.keywords, NEW.name, NEW.content);
            END
        """)
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS writing_resource_ai_delete
            AFTER DELETE ON writing_resource
            BEGIN
                INSERT INTO writing_resource_fts(writing_resource_fts, rowid, keywords, name, content)
                VALUES ('delete', OLD.id, OLD.keywords, OLD.name, OLD.content);
            END
        """)
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS writing_resource_ai_update
            AFTER UPDATE ON writing_resource
            BEGIN
                INSERT INTO writing_resource_fts(writing_resource_fts, rowid, keywords, name, content)
                VALUES ('delete', OLD.id, OLD.keywords, OLD.name, OLD.content);
                INSERT INTO writing_resource_fts(rowid, keywords, name, content)
                VALUES (NEW.id, NEW.keywords, NEW.name, NEW.content);
            END
        """)

        await self.conn.commit()
        await self._migrate_schema()

    async def _migrate_schema(self):
        c = await self.conn.cursor()
        await c.execute("PRAGMA table_info(writing_resource)")
        rows = await c.fetchall()
        existing = {row[1] for row in rows}

        migrations = [
            ("is_constant", "INTEGER DEFAULT 0"),
            ("inject_position", "INTEGER DEFAULT 2"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing:
                await c.execute(f"ALTER TABLE writing_resource ADD COLUMN {col_name} {col_def}")
        await self.conn.commit()

    async def _migrate_legacy_tables(self):
        """迁移旧表名 knowledge_base → writing_resource（kb→wr 重命名兼容）。

        场景：用户旧版本使用 quill_kb.db（表名 knowledge_base），升级后文件被
        重命名为 quill_wr.db，但表名未变。新代码查询 writing_resource 表会得到
        空结果。此方法在 _init_db 之前执行，确保旧表被安全 rename。

        三种情况：
          1. 旧表不存在 → 无需迁移
          2. 旧表存在、新表不存在 → 直接 rename 旧表
          3. 新旧表共存 → 若新表空且旧表有数据，迁移数据；否则丢弃旧表
        """
        c = await self.conn.cursor()

        # 旧触发器名（rename 主表前必须先 DROP，否则引用会失效）
        legacy_triggers = [
            "update_knowledge_timestamp",
            "knowledge_base_ai_insert",
            "knowledge_base_ai_delete",
            "knowledge_base_ai_update",
        ]

        # 检查旧主表是否存在
        await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_base'"
        )
        old_main = await c.fetchone()
        if not old_main:
            return  # 情况 1：无需迁移

        # 检查新主表是否存在
        await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='writing_resource'"
        )
        new_main = await c.fetchone()

        if not new_main:
            # 情况 2：旧表存在、新表不存在 → 直接 rename
            logger.info("[WR] 迁移旧表: knowledge_base → writing_resource")
            for t in legacy_triggers:
                await c.execute(f"DROP TRIGGER IF EXISTS {t}")
            await c.execute("DROP TABLE IF EXISTS knowledge_base_fts")
            await c.execute("ALTER TABLE knowledge_base RENAME TO writing_resource")
            await self.conn.commit()
            logger.info("[WR] 旧表迁移完成（FTS 索引将在 _init_db 后重建）")
            return

        # 情况 3：新旧表共存
        await c.execute("SELECT COUNT(*) FROM writing_resource")
        new_count = (await c.fetchone())[0]
        await c.execute("SELECT COUNT(*) FROM knowledge_base")
        old_count = (await c.fetchone())[0]

        if new_count == 0 and old_count > 0:
            # 新表为空（_init_db 刚建的空表）、旧表有数据 → 迁移数据
            logger.warning(
                f"[WR] 新表为空且旧表有数据 (新={new_count}, 旧={old_count})，迁移旧表"
            )
            await c.execute("DROP TABLE writing_resource")
            for t in legacy_triggers:
                await c.execute(f"DROP TRIGGER IF EXISTS {t}")
            await c.execute("DROP TABLE IF EXISTS knowledge_base_fts")
            await c.execute("ALTER TABLE knowledge_base RENAME TO writing_resource")
            await self.conn.commit()
            logger.info("[WR] 旧表迁移完成（FTS 索引将在 _init_db 后重建）")
        else:
            # 新表已有数据或旧表也空 → 以新表为准，丢弃旧表
            logger.warning(
                f"[WR] 新旧表共存 (新={new_count}, 旧={old_count})，丢弃旧表 knowledge_base"
            )
            for t in legacy_triggers:
                await c.execute(f"DROP TRIGGER IF EXISTS {t}")
            await c.execute("DROP TABLE IF EXISTS knowledge_base_fts")
            await c.execute("DROP TABLE IF EXISTS knowledge_base")
            await self.conn.commit()

    async def _rebuild_fts_index(self):
        """表名迁移后 FTS 索引可能为空，检测并重建。

        _init_db 用 CREATE VIRTUAL TABLE IF NOT EXISTS 创建 FTS 表，若主表刚从
        knowledge_base rename 而来，FTS 表是新建的空表，需要手动回填索引。
        """
        c = await self.conn.cursor()
        await c.execute("SELECT COUNT(*) FROM writing_resource_fts")
        fts_count = (await c.fetchone())[0]
        if fts_count > 0:
            return
        await c.execute("SELECT COUNT(*) FROM writing_resource")
        main_count = (await c.fetchone())[0]
        if main_count == 0:
            return
        logger.info(f"[WR] 重建 FTS 索引 ({main_count} 条)")
        await c.execute(
            """
            INSERT INTO writing_resource_fts(rowid, keywords, name, content)
            SELECT id, keywords, name, content FROM writing_resource
            """
        )
        await self.conn.commit()

    async def initialize(self):
        """Explicit init for non-context-manager usage."""
        await self._connect()
        await self._migrate_legacy_tables()
        await self._init_db()
        await self._rebuild_fts_index()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> Dict:
        result = dict(row)

        for field in ("keywords", "aliases", "secondary_keywords"):
            if field in result and result[field]:
                try:
                    result[field] = json.loads(result[field])
                except (json.JSONDecodeError, TypeError):
                    logger.debug("[WR] 字段 %s JSON 解码失败，使用空列表", field)
                    result[field] = []
            elif field in result:
                result[field] = []
            # field missing entirely — leave it (e.g. fts_rank)

        if "enabled" in result:
            result["enabled"] = bool(result["enabled"])
        if "is_constant" in result:
            result["is_constant"] = bool(result["is_constant"])

        return result

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_entry(
        self,
        category: str,
        entry_id: str,
        keywords: List[str],
        content: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        secondary_keywords: Optional[List[str]] = None,
        priority: int = 5,
        is_constant: bool = False,
    ) -> bool:
        try:
            await self.conn.execute(
                """
                INSERT INTO writing_resource
                (category, entry_id, name, description, keywords,
                 secondary_keywords, aliases, content, priority, is_constant)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    category,
                    entry_id,
                    name,
                    description,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(secondary_keywords, ensure_ascii=False) if secondary_keywords else None,
                    json.dumps(aliases, ensure_ascii=False) if aliases else None,
                    content,
                    priority,
                    1 if is_constant else 0,
                ),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False
        except sqlite3.Error as e:
            logger.error(f"[WR] add_entry 数据库错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[WR] add_entry 失败: {e}", exc_info=True)
            return False

    async def get_entry(self, entry_id: str) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM writing_resource WHERE entry_id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None

    async def update_entry(self, entry_id: str, **kwargs) -> bool:
        allowed_fields = [
            "category", "name", "description", "keywords",
            "secondary_keywords", "aliases", "content", "priority", "enabled", "is_constant",
        ]
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                if key in ("keywords", "aliases") and isinstance(value, list):
                    value = json.dumps(value, ensure_ascii=False)
                if key == "secondary_keywords" and isinstance(value, list):
                    value = json.dumps(value, ensure_ascii=False)
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            return False
        values.append(entry_id)
        try:
            cursor = await self.conn.execute(
                f"UPDATE writing_resource SET {', '.join(updates)} WHERE entry_id = ?",
                values,
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError as e:
            logger.warning(f"[WR] update_entry 唯一性冲突: {e}")
            return False
        except sqlite3.Error as e:
            logger.error(f"[WR] update_entry 数据库错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[WR] update_entry 失败: {e}", exc_info=True)
            return False

    async def delete_entry(self, entry_id: str) -> bool:
        try:
            cursor = await self.conn.execute(
                "DELETE FROM writing_resource WHERE entry_id = ?", (entry_id,)
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"[WR] delete_entry 数据库错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[WR] delete_entry 失败: {e}", exc_info=True)
            return False

    async def enable_entry(self, entry_id: str, enabled: bool = True) -> bool:
        return await self.update_entry(entry_id, enabled=1 if enabled else 0)

    async def set_constant(self, entry_id: str, is_constant: bool) -> bool:
        try:
            cursor = await self.conn.execute(
                "UPDATE writing_resource SET is_constant = ? WHERE entry_id = ?",
                (1 if is_constant else 0, entry_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"[WR] set_constant 数据库错误: {e}")
            return False
        except Exception as e:
            logger.error(f"[WR] set_constant 失败: {e}", exc_info=True)
            return False
    # ------------------------------------------------------------------

    async def get_all_entries(
        self, category: Optional[str] = None, enabled_only: bool = True
    ) -> List[Dict]:
        sql = "SELECT * FROM writing_resource WHERE 1=1"
        params: list = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY priority DESC, match_count DESC"

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_categories(self) -> List[str]:
        async with self.conn.execute(
            "SELECT DISTINCT category FROM writing_resource WHERE enabled = 1 ORDER BY category"
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_constant_entries(self) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM writing_resource WHERE enabled = 1 AND is_constant = 1 ORDER BY priority DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def search(self, query: str, fields: Optional[List[str]] = None) -> List[Dict]:
        if fields is None:
            fields = ["name", "content", "keywords"]
        conditions = []
        params: list = []
        for field in fields:
            conditions.append(f"{field} LIKE ?")
            params.append(f"%{query}%")
        sql = (
            f"SELECT * FROM writing_resource WHERE enabled = 1 "
            f"AND ({' OR '.join(conditions)}) ORDER BY priority DESC LIMIT 20"
        )
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_stats(self) -> Dict:
        async with self.conn.execute("SELECT COUNT(*) FROM writing_resource") as c:
            row = await c.fetchone()
            total = row[0] if row else 0
        async with self.conn.execute("SELECT COUNT(*) FROM writing_resource WHERE enabled = 1") as c:
            row = await c.fetchone()
            enabled = row[0] if row else 0
        async with self.conn.execute(
            "SELECT category, COUNT(*) FROM writing_resource GROUP BY category"
        ) as c:
            by_category = {r[0]: r[1] for r in await c.fetchall()}
        async with self.conn.execute("SELECT SUM(match_count) FROM writing_resource") as c:
            row = await c.fetchone()
            total_matches = (row[0] if row else 0) or 0
        async with self.conn.execute("SELECT COUNT(*) FROM match_logs") as c:
            row = await c.fetchone()
            total_logs = row[0] if row else 0
        return {
            "total_entries": total,
            "enabled_entries": enabled,
            "disabled_entries": total - enabled,
            "by_category": by_category,
            "total_matches": int(total_matches),
            "total_logs": total_logs,
        }

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_list(val: Any) -> list:
        """Coerce a value to a list (handles JSON strings, single values)."""
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                return parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, TypeError):
                return val.split(",") if val else []
        return [val] if val else []

    def _score_entry(self, entry: Dict, user_input_lower: str) -> tuple:
        """Score a single entry against user input.

        Returns (match_score, matched_keywords) or (0, []) if no match.
        """
        keywords = self._ensure_list(entry.get("keywords", []))
        aliases = self._ensure_list(entry.get("aliases", []))
        match_score = 0.0
        matched_keywords: List[str] = []

        for kw in keywords:
            if kw and kw.lower() in user_input_lower:
                match_score += 3
                matched_keywords.append(kw)

        sec_kws = entry.get("secondary_keywords", [])
        if sec_kws and match_score > 0:
            sec_hit = any(s.lower() in user_input_lower for s in sec_kws if s)
            if not sec_hit:
                match_score *= 0.3

        for alias in aliases:
            if alias and alias.lower() in user_input_lower:
                match_score += 2
                matched_keywords.append(f"({alias})")

        name_lower = entry.get("name", "").lower()
        if name_lower and len(name_lower) >= 2 and name_lower in user_input_lower:
            if name_lower not in [kw.lower() for kw in matched_keywords]:
                match_score += 1

        return match_score, matched_keywords

    def _dedup_by_category(self, entries: List[Dict]) -> List[Dict]:
        if self.category_dedup_limit <= 0:
            return entries
        category_counts: Dict[str, int] = {}
        deduped: List[Dict] = []
        for entry in entries:
            cat = entry.get("category", "")
            count = category_counts.get(cat, 0)
            if count < self.category_dedup_limit:
                deduped.append(entry)
                category_counts[cat] = count + 1
        return deduped

    # ------------------------------------------------------------------
    # FTS5 helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_fts5(text: str) -> str:
        cleaned = _re.sub(r'[\"\'()*^~{}]', ' ', text)
        tokens = [t.strip() for t in cleaned.split() if t.strip()]
        if not tokens:
            return ""
        return " ".join(f'"{t}"' for t in tokens)

    async def fts_match(
        self, user_input: str, top_k: int = 5, category: Optional[str] = None
    ) -> List[Dict]:
        try:
            safe_input = self._escape_fts5(user_input)
            if not safe_input.strip():
                return await self.keyword_match(user_input, category)

            sql = """
                SELECT wr.*, fts.rank AS fts_rank
                FROM writing_resource_fts fts
                JOIN writing_resource wr ON fts.rowid = wr.id
                WHERE writing_resource_fts MATCH ?
                  AND wr.enabled = 1
            """
            params: list = [safe_input]
            if category:
                sql += " AND wr.category = ?"
                params.append(category)
            sql += " ORDER BY fts.rank LIMIT ?"
            params.append(top_k)

            async with self.conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
            result = []
            for r in rows:
                entry = self._row_to_dict(r)
                entry["match_score"] = entry.get("fts_rank", 0)
                result.append(entry)
            return result
        except (sqlite3.Error, ValueError) as e:
            logger.info(f"[WR] FTS5 match failed, falling back to keyword scan: {e}")
            return await self.keyword_match(user_input, category)

    async def keyword_match(
        self, user_input: str, category: Optional[str] = None
    ) -> List[Dict]:
        sql = "SELECT * FROM writing_resource WHERE enabled = 1"
        params: list = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

        user_input_lower = user_input.lower()
        matched: List[Dict] = []

        for r in rows:
            entry = self._row_to_dict(r)
            kw_list = entry.get("keywords", [])
            for kw in kw_list:
                if kw and kw.lower() in user_input_lower:
                    entry["matched_keywords"] = [kw]
                    entry["match_score"] = 3
                    matched.append(entry)
                    break

        matched.sort(key=lambda x: x["match_score"], reverse=True)
        return matched

    # ------------------------------------------------------------------
    # Main match (FTS5 fast path + keyword fallback)
    # ------------------------------------------------------------------

    async def match(
        self,
        user_input: str,
        top_k: int = 5,
        min_match: int = 1,
        category: Optional[str] = None,
        log_match: bool = True,
    ) -> List[Dict]:
        user_input_lower = user_input.lower()

        # --- FTS5 fast path ---
        try:
            fts_candidates = await self.fts_match(user_input, top_k=top_k * 3, category=category)
            if fts_candidates:
                matched_entries: List[Dict] = []
                for entry in fts_candidates:
                    score, matched_kw = self._score_entry(entry, user_input_lower)
                    if score >= min_match:
                        entry["match_score"] = score + entry.get("priority", 5) * 0.1
                        entry["matched_keywords"] = matched_kw
                        entry["fts_base"] = entry.get("fts_rank", entry.get("match_score", 0))
                        matched_entries.append(entry)

                matched_entries.sort(key=lambda x: x["match_score"], reverse=True)
                matched_entries = self._dedup_by_category(matched_entries)
                if len(matched_entries) >= top_k:
                    result = matched_entries[:top_k]
                    if result and log_match:
                        await self._increment_match_counts([e["id"] for e in result])
                        await self._log_match(user_input, [e["entry_id"] for e in result], len(result))
                    return result
        except (sqlite3.Error, ValueError) as e:
            logger.info(f"[WR] FTS5 match in match() failed, using full scan: {e}")
            pass

        # --- Fallback: full table scan ---
        sql = "SELECT wr.*, 0 AS match_score FROM writing_resource wr WHERE wr.enabled = 1"
        params: list = []
        if category:
            sql += " AND wr.category = ?"
            params.append(category)
        sql += " LIMIT 500"

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

        matched_entries = []
        for r in rows:
            entry = self._row_to_dict(r)
            score, matched_kw = self._score_entry(entry, user_input_lower)
            if score >= min_match:
                entry["match_score"] = score + entry.get("priority", 5) * 0.1
                entry["matched_keywords"] = matched_kw
                matched_entries.append(entry)

        matched_entries.sort(key=lambda x: x["match_score"], reverse=True)
        matched_entries = self._dedup_by_category(matched_entries)
        result = matched_entries[:top_k]

        if result and log_match:
            await self._increment_match_counts([e["id"] for e in result])
            await self._log_match(user_input, [e["entry_id"] for e in result], len(result))

        return result

    # ------------------------------------------------------------------
    # Match-count fallback (NEW)
    # ------------------------------------------------------------------

    async def get_top_entries_by_match_count(self, limit: int = 2) -> List[Dict]:
        """Return entries ordered by match_count (descending), for fallback when match=0."""
        async with self.conn.execute(
            "SELECT * FROM writing_resource WHERE enabled = 1 ORDER BY match_count DESC, priority DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _increment_match_counts(self, row_ids: list[int]):
        """批量递增多条记录的 match_count，统一 FTS 与全表扫描路径的更新逻辑。"""
        if not row_ids:
            return
        try:
            placeholders = ",".join("?" for _ in row_ids)
            await self.conn.execute(
                f"UPDATE writing_resource SET match_count = match_count + 1 WHERE id IN ({placeholders})",
                row_ids,
            )
            await self.conn.commit()
        except sqlite3.Error as e:
            logger.error(f"[WR] match_count 批量递增数据库错误: {e}")
        except Exception as e:
            logger.error(f"[WR] match_count 批量递增失败: {e}", exc_info=True)

    async def _log_match(self, user_input: str, matched_ids: List[str], match_count: int):
        await self.conn.execute(
            "INSERT INTO match_logs (user_input, matched_entries, match_count) VALUES (?, ?, ?)",
            (user_input[:500], json.dumps(matched_ids, ensure_ascii=False), match_count),
        )
        await self.conn.commit()

    async def get_match_logs(self, limit: int = 50, offset: int = 0) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM match_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            log = dict(r)
            if log.get("matched_entries"):
                try:
                    log["matched_entries"] = json.loads(log["matched_entries"])
                except (json.JSONDecodeError, TypeError):
                    logger.debug("[WR] match_logs.matched_entries JSON 解码失败")
            result.append(log)
        return result

    async def clear_match_logs(self) -> int:
        cursor = await self.conn.execute("DELETE FROM match_logs")
        await self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Reference text (pure computation, no I/O)
    # ------------------------------------------------------------------

    @staticmethod
    def get_reference_text(matched_entries: List[Dict], header: str = "【写作素材库参考】") -> str:
        if not matched_entries:
            return ""
        parts = [header, "=" * 40, ""]
        for entry in matched_entries:
            parts.append(entry.get("name", entry.get("entry_id", "")))
            parts.append(entry.get("content", ""))
            parts.extend(("", "-" * 40, ""))
        parts.extend(["=" * 40, "请结合以上参考，自主生成增强描写。"])
        return "\n".join(parts)


# ==================================================================
# Self-test
# ==================================================================

async def _self_test():
    print("=== Async WR Manager Self-Test ===\n")

    async with WritingResourceManager(":memory:") as wr:
        # 1 — Add entries
        ok = await wr.add_entry(
            category="action",
            entry_id="action_footjob",
            name="【足交特化】",
            keywords=["脚", "足", "脚交", "丝袜", "踩"],
            content="【足交动作参考】\n描写要点：脚掌接触、脚趾运用",
            priority=10,
        )
        assert ok, "add_entry 1 failed"
        print("✓ add_entry 1 (action_footjob)")

        ok = await wr.add_entry(
            category="liquid",
            entry_id="liquid_wet",
            name="【液体描写】",
            keywords=["湿", "滑", "流水", "沾"],
            secondary_keywords=["体液", "润滑"],
            content="【液体描写参考】\n描写要点：程度、质感",
            priority=8,
        )
        assert ok, "add_entry 2 failed"
        print("✓ add_entry 2 (liquid_wet)")

        ok = await wr.add_entry(
            category="action",
            entry_id="action_handjob",
            name="【手交特化】",
            keywords=["手", "握", "套弄", "撸"],
            content="【手交动作参考】\n描写要点：手势、节奏",
            priority=7,
        )
        assert ok, "add_entry 3 failed"
        print("✓ add_entry 3 (action_handjob)")

        # 2 — get_entry
        entry = await wr.get_entry("action_footjob")
        assert entry is not None and entry["entry_id"] == "action_footjob"
        assert isinstance(entry["keywords"], list) and len(entry["keywords"]) == 5
        print(f"✓ get_entry → {entry['name']}")

        # 3 — match (FTS5)
        results = await wr.match("她用脚给我弄，沾满了脚", top_k=5)
        print(f"\n✓ match('她用脚给我弄，沾满了脚') → {len(results)} results")
        for r in results:
            print(f"    {r.get('name')}: score={r.get('match_score', 0):.2f} kw={r.get('matched_keywords', [])}")

        assert len(results) >= 1, "match should find at least 1 entry"
        # footjob entry should rank high (keyword '脚' appears multiple times)
        assert results[0]["entry_id"] == "action_footjob", "footjob should be top match"

        # 4 — get_stats
        stats = await wr.get_stats()
        print(f"\n✓ get_stats → {stats}")
        assert stats["total_entries"] == 3
        assert stats["enabled_entries"] == 3

        # 5 — update / enable / set_constant
        ok = await wr.update_entry("action_footjob", priority=9, keywords=["脚", "足", "脚交"])
        assert ok, "update_entry failed"
        entry = await wr.get_entry("action_footjob")
        assert entry is not None
        assert entry["priority"] == 9
        assert len(entry["keywords"]) == 3
        print("✓ update_entry (priority + keywords)")

        ok = await wr.enable_entry("action_footjob", False)
        assert ok, "enable_entry failed"
        entry = await wr.get_entry("action_footjob")
        assert entry is not None
        assert entry["enabled"] is False
        print("✓ enable_entry(False)")
        await wr.enable_entry("action_footjob", True)  # restore

        ok = await wr.set_constant("action_footjob", True)
        assert ok, "set_constant failed"
        constants = await wr.get_constant_entries()
        assert len(constants) == 1 and constants[0]["entry_id"] == "action_footjob"
        print("✓ set_constant + get_constant_entries")

        # 6 — get_top_entries_by_match_count
        top = await wr.get_top_entries_by_match_count(limit=2)
        print(f"\n✓ get_top_entries_by_match_count → {len(top)} entries")
        for t in top:
            print(f"    {t['entry_id']} match_count={t['match_count']}")
        assert len(top) <= 2

        # 7 — search
        found = await wr.search("足交")
        assert len(found) >= 1
        print(f"✓ search('足交') → {len(found)} results")

        # 8 — delete
        ok = await wr.delete_entry("action_handjob")
        assert ok, "delete failed"
        entry = await wr.get_entry("action_handjob")
        assert entry is None
        stats2 = await wr.get_stats()
        assert stats2["total_entries"] == 2
        print("✓ delete_entry + verify count=2")

        # 9 — match_logs
        logs = await wr.get_match_logs(limit=10)
        assert len(logs) >= 1
        print(f"✓ get_match_logs → {len(logs)} entries")

        cleared = await wr.clear_match_logs()
        print(f"✓ clear_match_logs → cleared {cleared}")

        # 10 — reference text
        ref = WritingResourceManager.get_reference_text(results)
        assert ref.startswith("【写作素材库参考】")
        print(f"✓ get_reference_text → {len(ref)} chars")

    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_self_test())
