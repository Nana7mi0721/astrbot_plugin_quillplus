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
import asyncio
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
        # F4 对齐：与 memory/vector store 一致，串行化 execute+commit 写序列，
        # 防止并发协程（聊天匹配 × Web 面板编辑）交错提交半途事务
        self._lock = asyncio.Lock()
        # FTS 降级可见性：此前索引失效只记一条 INFO（默认控制台级别下不可见），
        # 然后静默退化成全表扫描，面板 /info 也不暴露，问题完全不可观测。
        self._fts_ok: Optional[bool] = None   # None=尚未尝试
        self._fts_error: Optional[str] = None
        self._count_write_errors = 0          # match_count 递增失败次数
        self._log_write_errors = 0            # match_logs 写入失败次数

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

        # 旧触发器名（rename 主表前必须先 DROP，否则引用会失效）。
        # 实际数据库里残留的是 knowledge_ai_*（rename 时没有一起改名的结果），
        # knowledge_base_ai_* 是更早一代的命名，两种都列上，多列无害。
        # 注意：仅靠这份名单不够 —— 情况 1「旧表不存在」会提前 return，
        # 真正的兜底由 _drop_orphan_legacy_triggers 在 _init_db 之后无条件执行。
        legacy_triggers = [
            "update_knowledge_timestamp",
            "knowledge_base_ai_insert",
            "knowledge_base_ai_delete",
            "knowledge_base_ai_update",
            "knowledge_ai_insert",
            "knowledge_ai_delete",
            "knowledge_ai_update",
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
        # 注意：不能直接 SELECT COUNT(*) FROM writing_resource_fts 来判断索引是否
        # 为空 —— writing_resource_fts 是 external-content 表（content=writing_resource），
        # 对它的 COUNT(*) 会回落到内容表，无论索引里有没有数据都返回主表行数。
        # 真实索引条数要看 _docsize 影子表（每有一条索引记录就写一行）。
        await c.execute("SELECT COUNT(*) FROM writing_resource_fts_docsize")
        fts_count = (await c.fetchone())[0]
        if fts_count > 0:
            return
        await c.execute("SELECT COUNT(*) FROM writing_resource")
        main_count = (await c.fetchone())[0]
        if main_count == 0:
            return
        logger.info(f"[WR] 重建 FTS 索引 ({main_count} 条)")
        # 用 fts5 官方的 'rebuild' 命令而不是手工 INSERT SELECT：手工插入的
        # rowid 与 trigram 分词器的 docsize 记录不匹配时，索引条目会处于
        # 「半写」状态，MATCH 会漏词但又不报错。
        await c.execute("INSERT INTO writing_resource_fts(writing_resource_fts) VALUES('rebuild')")
        await self.conn.commit()

    async def _drop_orphan_legacy_triggers(self, c):
        """删除指向「内容表已消失」的 FTS 表的遗留触发器。

        旧版本（表名还是 knowledge_base 的时代）在迁移时只 rename 了主表，三个
        触发器 knowledge_ai_insert / _delete / _update 留在了 writing_resource 上，
        而它们写入的 knowledge_fts 声明的是 content=knowledge_base —— 内容表已随
        rename 消失。此时 SQLite 执行触发器内部的 INSERT/UPDATE 时读不到内容行，
        直接把错误冒泡成外层 DML 的失败：DatabaseError("database disk image is
        malformed")，于是整张 writing_resource 变成只读（列表能看、改不了删不掉）。

        残留分两种：knowledge_fts 虚拟表还在、但它声明的 content=knowledge_base
        已经消失；或者虚拟表本身也没了（只在触发器 SQL 里留个名字）。两种都会让
        对 writing_resource 的写全部失败，所以这里同时处理：
          1. 扫描所有 FTS 虚拟表，找出内容表已不存在的「悬空索引」；
          2. 连同它的 _data/_idx/_docsize/_config 影子表一起 DROP（否则下次启动
             还会被当成悬空索引反复命中）；
          3. 把引用这些悬空索引的触发器清掉，包括引用一个根本不存在的表的触发器。
        幂等，每次启动跑一遍无副作用。
        """
        await c.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        table_rows = await c.fetchall()
        tables = {row[0] for row in table_rows}

        # 找出内容表已不存在的 FTS 虚拟表
        orphan_fts = {}
        for name, sql in table_rows:
            if not sql or "VIRTUAL TABLE" not in sql.upper():
                continue
            match = _re.search(r"content\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", sql)
            if match and match.group(1) not in tables:
                orphan_fts[name] = match.group(1)

        await c.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        )
        dropped = []
        for name, target, sql in await c.fetchall():
            if target != "writing_resource" or not sql:
                continue
            # 命中悬空索引，或引用了一个连 sqlite_master 里都没有的表
            hit = [t for t in orphan_fts if t in sql]
            if not hit:
                hit = [
                    t for t in ("knowledge_fts", "knowledge_base_fts")
                    if t in sql and t not in tables
                ]
            if hit:
                await c.execute(f"DROP TRIGGER IF EXISTS {name}")
                dropped.append((name, hit[0], orphan_fts.get(hit[0], "已不存在")))

        for name in list(orphan_fts):
            logger.warning(
                f"[WR] 清理悬空 FTS 索引 {name}（其内容表 {orphan_fts[name]} 已不存在）"
            )
            # 只删影子表，不删虚拟表本身：内容表缺失时 fts5 模块无法构造这个
            # 虚拟表，DROP TABLE 会报 "vtable constructor failed"。影子表清掉后
            # 它已是空壳，只要没有触发器引用就不会再被触碰。整个清理都是尽力而为，
            # 任何一步失败都不该阻断启动（关键修复是上面 DROP TRIGGER）。
            for suffix in ("_data", "_idx", "_docsize", "_config"):
                try:
                    await c.execute(f"DROP TABLE IF EXISTS {name}{suffix}")
                except Exception as e:
                    logger.debug(f"[WR] 清理影子表 {name}{suffix} 失败（可忽略）: {e}")

        if dropped or orphan_fts:
            await self.conn.commit()
            for name, fts, content in dropped:
                logger.warning(
                    f"[WR] 已清理失效触发器 {name}：它写入 {fts}，"
                    f"而其内容表 {content} 已不存在（此前会导致写入报 malformed）"
                )

    async def initialize(self):
        """Explicit init for non-context-manager usage."""
        await self._connect()
        await self._migrate_legacy_tables()
        await self._init_db()
        # 必须在 _init_db 之后：_init_db 会创建 writing_resource_fts 与配套的
        # writing_resource_ai_* 触发器，只有这时才能准确判断哪些触发器指向的
        # 表是真的不存在，从而只清掉历史残留、不动正常触发器。
        await self._drop_orphan_legacy_triggers(await self.conn.cursor())
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
            async with self._lock:
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
            async with self._lock:
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
            async with self._lock:
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
            async with self._lock:
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

        # entry.get("name", "") 只在键缺失时给默认值；name 列存的是 NULL 时
        # 返回 None，直接 .lower() 会抛 AttributeError —— 而 add_entry(name=None)
        # 是允许的（导入路径就不传 name），于是「库里只要有一条无名字的素材，
        # 之后每次 match() 都崩」。这里显式兜 None。
        name_lower = (entry.get("name") or "").lower()
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
        # 中文标点也作为分隔符切开，得到较短的语义块；
        # trigram 分词器下 <3 字符的 token 无法命中，直接过滤
        cleaned = _re.sub(r'[\"\'()*^~{}，。！？；：、—…·\s]+', ' ', text)
        tokens = [t for t in cleaned.split() if len(t) >= 3]
        if not tokens:
            return ""
        # OR 连接：任一语义块命中即召回（靠 bm25 rank 排序）。
        # 此前用 AND 连接整句短语，中文长句几乎必然 miss，
        # FTS 快速路径形同虚设、每次都回退全表扫描。
        return " OR ".join(f'"{t}"' for t in tokens)

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
            self._note_fts_ok()
            result = []
            for r in rows:
                entry = self._row_to_dict(r)
                entry["match_score"] = entry.get("fts_rank", 0)
                result.append(entry)
            return result
        except (sqlite3.Error, ValueError) as e:
            self._note_fts_failure(e)
            return await self.keyword_match(user_input, category)

    def _note_fts_ok(self):
        """标记 FTS 可用；从降级状态恢复时记一条 INFO。"""
        if self._fts_ok is False:
            logger.info("[WR] FTS5 索引已恢复可用")
        self._fts_ok = True
        self._fts_error = None

    def _note_fts_failure(self, exc: Exception):
        """记录 FTS 失效。首次 WARNING（含原始异常），后续降为 DEBUG 防刷屏。"""
        first = self._fts_ok is not False
        self._fts_ok = False
        self._fts_error = f"{type(exc).__name__}: {exc}"
        if first:
            logger.warning(
                "[WR] FTS5 索引不可用，已降级为全表扫描（结果可能变慢、排序变差）: %s",
                exc,
                exc_info=True,
            )
        else:
            logger.debug("[WR] FTS5 仍不可用: %s", exc)

    async def keyword_match(
        self, user_input: str, category: Optional[str] = None
    ) -> List[Dict]:
        sql = "SELECT * FROM writing_resource WHERE enabled = 1"
        params: list = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        # 与 match() 的回退路径保持同一上限：此前无 LIMIT，索引失效时会把整张
        # 表连同 content 全文载入内存。
        sql += " LIMIT 2000"
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
            self._note_fts_failure(e)
            pass

        # --- Fallback: full table scan ---
        sql = "SELECT wr.*, 0 AS match_score FROM writing_resource wr WHERE wr.enabled = 1"
        params: list = []
        if category:
            sql += " AND wr.category = ?"
            params.append(category)
        # 回退路径仅在 FTS5 不可用时触发；上限从 500 放宽到 2000，
        # 避免较大素材库中位于后面的条目永远无法被匹配到。
        sql += " LIMIT 2000"

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
        """批量递增多条记录的 match_count，统一 FTS 与全表扫描路径的更新逻辑。

        失败只记计数与日志、不影响返回的匹配结果（计数是统计量，不该让本轮
        注入失败），但计数会通过 get_index_status() 暴露到面板，避免"悄悄少记"。
        """
        if not row_ids:
            return
        try:
            placeholders = ",".join("?" for _ in row_ids)
            async with self._lock:
                await self.conn.execute(
                    f"UPDATE writing_resource SET match_count = match_count + 1 WHERE id IN ({placeholders})",
                    row_ids,
                )
                await self.conn.commit()
        except sqlite3.Error as e:
            self._count_write_errors += 1
            logger.error(f"[WR] match_count 批量递增数据库错误: {e}")
        except Exception as e:
            self._count_write_errors += 1
            logger.error(f"[WR] match_count 批量递增失败: {e}", exc_info=True)

    async def _log_match(self, user_input: str, matched_ids: List[str], match_count: int):
        # 写日志失败不能连累调用方：此前无 try，match_logs 一旦写不进去
        # （库被锁/磁盘满/表结构损坏）整个 match() 会抛异常，本轮 WR 注入静默消失。
        try:
            async with self._lock:
                await self.conn.execute(
                    "INSERT INTO match_logs (user_input, matched_entries, match_count) VALUES (?, ?, ?)",
                    (user_input[:500], json.dumps(matched_ids, ensure_ascii=False), match_count),
                )
                await self.conn.commit()
        except Exception as e:
            self._log_write_errors += 1
            logger.warning("[WR] match_logs 写入失败（不影响本轮匹配结果）: %s", e, exc_info=True)

    def get_index_status(self) -> Dict:
        """FTS 索引与匹配日志健康状况，供 /info 暴露（降级必须可见）。"""
        return {
            "fts_ok": self._fts_ok,
            "fts_error": self._fts_error,
            "count_write_errors": self._count_write_errors,
            "log_write_errors": self._log_write_errors,
        }

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
        # 与 _log_match 的 INSERT 共用同一把锁：此前这里绕过 _lock 直接
        # execute+commit，并发写入会与 DELETE 交错。
        async with self._lock:
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
