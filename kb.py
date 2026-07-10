# -*- coding: utf-8 -*-
"""
Async Knowledge Base Manager (aiosqlite + FTS5)
================================================

Port of intimate_send v5.0 KnowledgeBaseManager to fully async I/O.
Identical table schema — compatible with existing .db files.
"""

import os
import re as _re
import json
from typing import List, Dict, Optional, Any

import aiosqlite

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class KnowledgeBaseManager:
    """
    Async knowledge base manager.

    Usage::
        async with KnowledgeBaseManager(db_path) as kb:
            entries = await kb.match("some text")
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

    @conn.setter
    def conn(self, value: Optional[aiosqlite.Connection]):
        self._conn = value

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
            CREATE TABLE IF NOT EXISTS knowledge_base (
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
        await c.execute("CREATE INDEX IF NOT EXISTS idx_category ON knowledge_base(category)")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_priority ON knowledge_base(priority DESC)")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_enabled ON knowledge_base(enabled)")

        # FTS5 virtual table
        # S3-5: 指定 trigram 分词器，改善中文子串匹配（SQLite 3.34+）。
        # 若旧 SQLite 不支持 trigram，回退到默认 unicode61 分词器。
        try:
            await c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    keywords, name, content,
                    content=knowledge_base, content_rowid=id,
                    tokenize='trigram'
                )
            """)
        except Exception as exc:
            logger.warning("[KB] trigram 分词器不可用，回退默认分词器: %s", exc)
            await c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    keywords, name, content,
                    content=knowledge_base, content_rowid=id
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
            CREATE TRIGGER IF NOT EXISTS update_knowledge_timestamp
            AFTER UPDATE ON knowledge_base
            BEGIN
                UPDATE knowledge_base SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END
        """)

        # FTS5 sync triggers
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ai_insert
            AFTER INSERT ON knowledge_base
            BEGIN
                INSERT INTO knowledge_fts(rowid, keywords, name, content)
                VALUES (NEW.id, NEW.keywords, NEW.name, NEW.content);
            END
        """)
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ai_delete
            AFTER DELETE ON knowledge_base
            BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, keywords, name, content)
                VALUES ('delete', OLD.id, OLD.keywords, OLD.name, OLD.content);
            END
        """)
        await c.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_ai_update
            AFTER UPDATE ON knowledge_base
            BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, keywords, name, content)
                VALUES ('delete', OLD.id, OLD.keywords, OLD.name, OLD.content);
                INSERT INTO knowledge_fts(rowid, keywords, name, content)
                VALUES (NEW.id, NEW.keywords, NEW.name, NEW.content);
            END
        """)

        await self.conn.commit()
        await self._migrate_schema()

    async def _migrate_schema(self):
        c = await self.conn.cursor()
        await c.execute("PRAGMA table_info(knowledge_base)")
        rows = await c.fetchall()
        existing = {row[1] for row in rows}

        migrations = [
            ("is_constant", "INTEGER DEFAULT 0"),
            ("inject_position", "INTEGER DEFAULT 2"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing:
                await c.execute(f"ALTER TABLE knowledge_base ADD COLUMN {col_name} {col_def}")
        await self.conn.commit()

    async def initialize(self):
        """Explicit init for non-context-manager usage."""
        await self._connect()
        await self._init_db()

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
                except Exception:
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
                INSERT INTO knowledge_base
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
        except Exception as e:
            logger.warning(f"[KB] add_entry failed: {e}")
            return False

    async def get_entry(self, entry_id: str) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM knowledge_base WHERE entry_id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_dict(row) if row else None

    async def get_entry_by_id(self, row_id: int) -> Optional[Dict]:
        async with self.conn.execute(
            "SELECT * FROM knowledge_base WHERE id = ?", (row_id,)
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
                f"UPDATE knowledge_base SET {', '.join(updates)} WHERE entry_id = ?",
                values,
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.warning(f"[KB] update_entry failed: {e}")
            return False

    async def delete_entry(self, entry_id: str) -> bool:
        try:
            cursor = await self.conn.execute(
                "DELETE FROM knowledge_base WHERE entry_id = ?", (entry_id,)
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.warning(f"[KB] delete_entry failed: {e}")
            return False

    async def enable_entry(self, entry_id: str, enabled: bool = True) -> bool:
        return await self.update_entry(entry_id, enabled=1 if enabled else 0)

    async def set_constant(self, entry_id: str, is_constant: bool) -> bool:
        try:
            cursor = await self.conn.execute(
                "UPDATE knowledge_base SET is_constant = ? WHERE entry_id = ?",
                (1 if is_constant else 0, entry_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.warning(f"[KB] set_constant failed: {e}")
            return False
    # ------------------------------------------------------------------

    async def get_all_entries(
        self, category: Optional[str] = None, enabled_only: bool = True
    ) -> List[Dict]:
        sql = "SELECT * FROM knowledge_base WHERE 1=1"
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
            "SELECT DISTINCT category FROM knowledge_base WHERE enabled = 1 ORDER BY category"
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_constant_entries(self) -> List[Dict]:
        async with self.conn.execute(
            "SELECT * FROM knowledge_base WHERE enabled = 1 AND is_constant = 1 ORDER BY priority DESC"
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
            f"SELECT * FROM knowledge_base WHERE enabled = 1 "
            f"AND ({' OR '.join(conditions)}) ORDER BY priority DESC LIMIT 20"
        )
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_stats(self) -> Dict:
        async with self.conn.execute("SELECT COUNT(*) FROM knowledge_base") as c:
            row = await c.fetchone()
            total = row[0] if row else 0
        async with self.conn.execute("SELECT COUNT(*) FROM knowledge_base WHERE enabled = 1") as c:
            row = await c.fetchone()
            enabled = row[0] if row else 0
        async with self.conn.execute(
            "SELECT category, COUNT(*) FROM knowledge_base GROUP BY category"
        ) as c:
            by_category = {r[0]: r[1] for r in await c.fetchall()}
        async with self.conn.execute("SELECT SUM(match_count) FROM knowledge_base") as c:
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
            except Exception:
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
                SELECT kb.*, fts.rank AS fts_rank
                FROM knowledge_fts fts
                JOIN knowledge_base kb ON fts.rowid = kb.id
                WHERE knowledge_fts MATCH ?
                  AND kb.enabled = 1
            """
            params: list = [safe_input]
            if category:
                sql += " AND kb.category = ?"
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
        except Exception as e:
            logger.info(f"[KB] FTS5 match failed, falling back to keyword scan: {e}")
            return await self.keyword_match(user_input, category)

    async def keyword_match(
        self, user_input: str, category: Optional[str] = None
    ) -> List[Dict]:
        sql = "SELECT * FROM knowledge_base WHERE enabled = 1"
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
                        for e in result:
                            await self._increment_match_count(e["id"])
                        await self._log_match(user_input, [e["entry_id"] for e in result], len(result))
                    return result
        except Exception as e:
            logger.info(f"[KB] FTS5 match in match() failed, using full scan: {e}")
            pass

        # --- Fallback: full table scan ---
        sql = "SELECT kb.*, 0 AS match_score FROM knowledge_base kb WHERE kb.enabled = 1"
        params: list = []
        if category:
            sql += " AND kb.category = ?"
            params.append(category)

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

        if result:
            if log_match:
                try:
                    for e in result:
                        await self.conn.execute(
                            "UPDATE knowledge_base SET match_count = match_count + 1 WHERE id = ?",
                            (e["id"],),
                        )
                    await self.conn.commit()
                except Exception as e:
                    logger.warning(f"[KB] match_count increment failed: {e}")
                await self._log_match(user_input, [e["entry_id"] for e in result], len(result))

        return result

    # ------------------------------------------------------------------
    # Match-count fallback (NEW)
    # ------------------------------------------------------------------

    async def get_top_entries_by_match_count(self, limit: int = 2) -> List[Dict]:
        """Return entries ordered by match_count (descending), for fallback when match=0."""
        async with self.conn.execute(
            "SELECT * FROM knowledge_base WHERE enabled = 1 ORDER BY match_count DESC, priority DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _increment_match_count(self, row_id: int):
        await self.conn.execute(
            "UPDATE knowledge_base SET match_count = match_count + 1 WHERE id = ?",
            (row_id,),
        )
        await self.conn.commit()

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
                except Exception:
                    pass
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
    print("=== Async KB Manager Self-Test ===\n")

    async with KnowledgeBaseManager(":memory:") as kb:
        # 1 — Add entries
        ok = await kb.add_entry(
            category="action",
            entry_id="action_footjob",
            name="【足交特化】",
            keywords=["脚", "足", "脚交", "丝袜", "踩"],
            content="【足交动作参考】\n描写要点：脚掌接触、脚趾运用",
            priority=10,
        )
        assert ok, "add_entry 1 failed"
        print("✓ add_entry 1 (action_footjob)")

        ok = await kb.add_entry(
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

        ok = await kb.add_entry(
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
        entry = await kb.get_entry("action_footjob")
        assert entry is not None and entry["entry_id"] == "action_footjob"
        assert isinstance(entry["keywords"], list) and len(entry["keywords"]) == 5
        print(f"✓ get_entry → {entry['name']}")

        # 3 — match (FTS5)
        results = await kb.match("她用脚给我弄，沾满了脚", top_k=5)
        print(f"\n✓ match('她用脚给我弄，沾满了脚') → {len(results)} results")
        for r in results:
            print(f"    {r.get('name')}: score={r.get('match_score', 0):.2f} kw={r.get('matched_keywords', [])}")

        assert len(results) >= 1, "match should find at least 1 entry"
        # footjob entry should rank high (keyword '脚' appears multiple times)
        assert results[0]["entry_id"] == "action_footjob", "footjob should be top match"

        # 4 — get_stats
        stats = await kb.get_stats()
        print(f"\n✓ get_stats → {stats}")
        assert stats["total_entries"] == 3
        assert stats["enabled_entries"] == 3

        # 5 — update / enable / set_constant
        ok = await kb.update_entry("action_footjob", priority=9, keywords=["脚", "足", "脚交"])
        assert ok, "update_entry failed"
        entry = await kb.get_entry("action_footjob")
        assert entry is not None
        assert entry["priority"] == 9
        assert len(entry["keywords"]) == 3
        print("✓ update_entry (priority + keywords)")

        ok = await kb.enable_entry("action_footjob", False)
        assert ok, "enable_entry failed"
        entry = await kb.get_entry("action_footjob")
        assert entry is not None
        assert entry["enabled"] is False
        print("✓ enable_entry(False)")
        await kb.enable_entry("action_footjob", True)  # restore

        ok = await kb.set_constant("action_footjob", True)
        assert ok, "set_constant failed"
        constants = await kb.get_constant_entries()
        assert len(constants) == 1 and constants[0]["entry_id"] == "action_footjob"
        print("✓ set_constant + get_constant_entries")

        # 6 — get_top_entries_by_match_count
        top = await kb.get_top_entries_by_match_count(limit=2)
        print(f"\n✓ get_top_entries_by_match_count → {len(top)} entries")
        for t in top:
            print(f"    {t['entry_id']} match_count={t['match_count']}")
        assert len(top) <= 2

        # 7 — search
        found = await kb.search("足交")
        assert len(found) >= 1
        print(f"✓ search('足交') → {len(found)} results")

        # 8 — delete
        ok = await kb.delete_entry("action_handjob")
        assert ok, "delete failed"
        entry = await kb.get_entry("action_handjob")
        assert entry is None
        stats2 = await kb.get_stats()
        assert stats2["total_entries"] == 2
        print("✓ delete_entry + verify count=2")

        # 9 — match_logs
        logs = await kb.get_match_logs(limit=10)
        assert len(logs) >= 1
        print(f"✓ get_match_logs → {len(logs)} entries")

        cleared = await kb.clear_match_logs()
        print(f"✓ clear_match_logs → cleared {cleared}")

        # 10 — reference text
        ref = KnowledgeBaseManager.get_reference_text(results)
        assert ref.startswith("【写作素材库参考】")
        print(f"✓ get_reference_text → {len(ref)} chars")

    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_self_test())
