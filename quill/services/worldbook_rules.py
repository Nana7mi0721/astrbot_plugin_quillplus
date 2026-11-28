# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""世界书规则状态：按逻辑 scope/persona 持久化轮次与定时效应。

M5.1 的定时效应必须跨重启恢复，并且不能因请求侧/响应侧两个入口把同一轮
记成两轮。因此状态落 ``quill_memory.db`` 的独立表：

* ``wb_rule_scopes`` 保存 ``(scope_id, persona_id)`` 的逻辑轮次与最后 turn_id；
* ``wb_rule_effects`` 保存每条规则的首见轮、粘性截止、冷却截止与最后注入轮。

``rule_turn`` 不是物理入口轮次，也不是 ``quill_rounds``：同一 turn_id 重复进入
只推进一次，跨群/跨私聊的会话组共享同一个规则时钟。

模拟链路只读取当前轮次，不推进、不写冷却/粘性；真实链路在条目**实际进入
prompt** 后才提交，避免被 token 预算截掉的条目白吃冷却。

``quill/`` 包内禁止 import astrbot，本模块只依赖标准库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Optional

_MAX_SCOPES = 300
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS wb_rule_scopes (
        scope_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        rule_turn INTEGER NOT NULL DEFAULT 0,
        last_turn TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(scope_id, persona_id)
    )""",
    """CREATE TABLE IF NOT EXISTS wb_rule_effects (
        scope_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        rule_id TEXT NOT NULL,
        first_seen_turn INTEGER NOT NULL DEFAULT 0,
        sticky_until_turn INTEGER NOT NULL DEFAULT 0,
        cooldown_until_turn INTEGER NOT NULL DEFAULT 0,
        last_triggered_turn INTEGER NOT NULL DEFAULT 0,
        last_injected_turn INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(scope_id, persona_id, rule_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wb_rule_effects_scope "
    "ON wb_rule_effects(scope_id, persona_id)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


class WorldbookRuleStore:
    """SQLite 持久化的世界书规则状态。

    连接在首次使用时惰性建立：世界书管理器先于记忆库初始化，提前在
    ``quill_memory.db`` 里建表会让 Schema 校验把空库误判成未知结构。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = str(path) if path else ""
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._legacy: Dict[str, dict] = {}
        # P1-2：备份恢复期间解压会覆盖 quill_memory.db。冻结后所有读写
        # 直接降级为空操作，禁止任何并发请求惰性重连旧/半覆盖文件。
        self._frozen = False

    # ── connection / schema ─────────────────────────────────────────────
    def _ensure_locked(self) -> sqlite3.Connection | None:
        if not self.path or self._frozen:
            return None
        if self._conn is not None:
            return self._conn
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            # 旧版把规则状态存成同名 JSON 文件。直接 sqlite3.connect()
            # 会抛 "file is not a database"，因此先读出内容并原子改名归档，
            # 再在原路径建 SQLite。
            if self._path_is_legacy_json():
                self._legacy = self._read_legacy_locked()
                self._archive_legacy_locked()
        self._conn = sqlite3.connect(
            self.path, timeout=10.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            with self._write_transaction_locked(self._conn):
                for statement in _SCHEMA:
                    self._conn.execute(statement)
            if self._legacy:
                # 迁移也放在同一保护块里：失败时关闭连接并保留内存 JSON，
                # 下一次访问重新建库后重试，避免半迁移连接被惰性复用。
                self._import_legacy_locked()
                self._legacy = {}
        except BaseException:
            try:
                self._conn.close()
            finally:
                self._conn = None
            raise
        return self._conn

    @contextmanager
    def _write_transaction_locked(self, conn: sqlite3.Connection):
        """所有写路径共用的单事务：BEGIN IMMEDIATE / COMMIT / 异常 rollback。"""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                # 回滚失败不能覆盖原始异常；连接最终由 close() 统一收口。
                pass
            raise

    def _read_legacy_locked(self) -> Dict[str, dict]:
        """旧版 JSON 状态迁移：读取后由 :meth:`_archive_legacy_locked` 改名保留。"""
        try:
            if not self.path or not os.path.isfile(self.path):
                return {}
            with open(self.path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
            # P2-12：旧文件可能带 UTF-8 BOM 或前导空白，必须先跳过这些
            # 字节再判断 JSON 根；否则会把整份历史规则静默当成空数据。
            start = 0
            while start < len(raw_text):
                char = raw_text[start]
                if char == "\ufeff" or char.isspace():
                    start += 1
                    continue
                break
            payload = raw_text[start:]
            if not payload.startswith("{"):
                return {}
            raw = json.loads(payload)
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _path_is_legacy_json(self) -> bool:
        try:
            if not self.path or not os.path.isfile(self.path):
                return False
            with open(self.path, "r", encoding="utf-8") as fh:
                while True:
                    ch = fh.read(1)
                    if not ch:
                        return False
                    if ch == "\ufeff" or ch.isspace():
                        continue
                    return ch == "{"
        except (OSError, UnicodeDecodeError):
            return False

    def _archive_legacy_locked(self) -> None:
        """把旧 JSON 原子改名为 ``*.legacy.json``，避免被 SQLite 当成库文件。"""
        if not self.path:
            return
        target = self.path + ".legacy.json"
        if os.path.exists(target):
            target = f"{self.path}.legacy.{int(time.time())}.json"
        try:
            os.replace(self.path, target)
        except OSError:
            # 改名失败（文件占用）时退回删除原 JSON：数据已读进内存并将在
            # 下一行导入 SQLite，宁可丢旧文件也不能让连接直接崩溃。
            try:
                os.remove(self.path)
            except OSError:
                raise sqlite3.DatabaseError(
                    "旧版规则状态文件无法归档，且目标路径不是 SQLite 数据库"
                )

    def _import_legacy_locked(self) -> None:
        scopes = self._legacy.get("scopes")
        if not isinstance(scopes, dict) or self._conn is None:
            return
        with self._write_transaction_locked(self._conn):
            for key, record in scopes.items():
                if not isinstance(record, dict):
                    continue
                scope_id, _, persona_id = str(key).partition("::")
                now = _now()
                self._conn.execute(
                    """INSERT INTO wb_rule_scopes
                       (scope_id, persona_id, rule_turn, last_turn, updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(scope_id, persona_id) DO UPDATE SET
                         rule_turn=MAX(rule_turn, excluded.rule_turn),
                         last_turn=excluded.last_turn, updated_at=excluded.updated_at""",
                    (
                        scope_id, persona_id, _as_int(record.get("round")),
                        str(record.get("last_turn") or ""), now,
                    ),
                )
                rules = record.get("rules")
                if not isinstance(rules, dict):
                    continue
                for rule_id, state in rules.items():
                    if not isinstance(state, dict):
                        continue
                    self._conn.execute(
                        """INSERT INTO wb_rule_effects
                           (scope_id, persona_id, rule_id, first_seen_turn,
                            sticky_until_turn, cooldown_until_turn,
                            last_triggered_turn, last_injected_turn, updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(scope_id, persona_id, rule_id) DO UPDATE SET
                             first_seen_turn=MIN(first_seen_turn, excluded.first_seen_turn),
                             sticky_until_turn=MAX(sticky_until_turn, excluded.sticky_until_turn),
                             cooldown_until_turn=MAX(cooldown_until_turn, excluded.cooldown_until_turn),
                             last_triggered_turn=MAX(last_triggered_turn, excluded.last_triggered_turn),
                             last_injected_turn=MAX(last_injected_turn, excluded.last_injected_turn),
                             updated_at=excluded.updated_at""",
                        (
                            scope_id, persona_id, str(rule_id),
                            _as_int(state.get("first_seen")),
                            _as_int(state.get("sticky_until")),
                            _as_int(state.get("cooldown_until")),
                            _as_int(state.get("last_match")),
                            _as_int(state.get("last_injected")),
                            now,
                        ),
                    )

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def freeze(self) -> None:
        """进入备份恢复冻结期：先置标志再关闭连接，期间禁止任何惰性重连。"""
        with self._lock:
            self._frozen = True
            self._close_locked()

    def unfreeze(self) -> None:
        """恢复完成后解除门控；下一次访问按需重新连接恢复后的数据库。"""
        with self._lock:
            self._frozen = False

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _key(scope_id: str, persona_id: str) -> str:
        return f"{scope_id or ''}::{persona_id or ''}"

    @staticmethod
    def _row_state(row) -> dict:
        return {
            "first_seen": int(row["first_seen_turn"] or 0),
            "sticky_until": int(row["sticky_until_turn"] or 0),
            "cooldown_until": int(row["cooldown_until_turn"] or 0),
            "last_injected": int(row["last_injected_turn"] or 0),
            "last_match": int(row["last_triggered_turn"] or 0),
        }

    def _entry_state_tx_locked(self, conn: sqlite3.Connection,
                               scope_id: str, persona_id: str, rule_id: str,
                               round_no: int, create: bool) -> Optional[dict]:
        """在调用方已开启的写事务内读取/创建规则状态，不自行 commit。"""
        row = conn.execute(
            "SELECT * FROM wb_rule_effects "
            "WHERE scope_id=? AND persona_id=? AND rule_id=?",
            (scope_id, persona_id, str(rule_id)),
        ).fetchone()
        if row is not None:
            return self._row_state(row)
        if not create:
            return None
        current = max(0, int(round_no or 0))
        now = _now()
        conn.execute(
            """INSERT INTO wb_rule_effects
               (scope_id, persona_id, rule_id, first_seen_turn,
                sticky_until_turn, cooldown_until_turn,
                last_triggered_turn, last_injected_turn, updated_at)
               VALUES(?,?,?,?,0,0,0,0,?)""",
            (scope_id, persona_id, str(rule_id), current, now),
        )
        conn.execute(
            """INSERT INTO wb_rule_scopes
               (scope_id, persona_id, rule_turn, last_turn, updated_at)
               VALUES(?,?,?,'',?)
               ON CONFLICT(scope_id, persona_id) DO UPDATE SET
                 updated_at=excluded.updated_at""",
            (scope_id, persona_id, current, now),
        )
        return {
            "first_seen": current, "sticky_until": 0,
            "cooldown_until": 0, "last_injected": 0, "last_match": 0,
        }

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM wb_rule_scopes").fetchone()[0]
        if count <= _MAX_SCOPES:
            return
        keep = conn.execute(
            "SELECT scope_id, persona_id FROM wb_rule_scopes "
            "ORDER BY updated_at DESC LIMIT ?", (_MAX_SCOPES,)
        ).fetchall()
        keep_set = {(row[0], row[1]) for row in keep}
        for row in conn.execute(
            "SELECT scope_id, persona_id FROM wb_rule_scopes"
        ).fetchall():
            if (row[0], row[1]) in keep_set:
                continue
            conn.execute(
                "DELETE FROM wb_rule_effects WHERE scope_id=? AND persona_id=?",
                (row[0], row[1]),
            )
            conn.execute(
                "DELETE FROM wb_rule_scopes WHERE scope_id=? AND persona_id=?",
                (row[0], row[1]),
            )

    # ── turn / round ────────────────────────────────────────────────────
    def begin_turn(self, scope_id: str, persona_id: str, turn_token: str = "") -> int:
        """推进逻辑轮次；同一 turn_token 重复调用不重复计数。"""
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return 0
            scope_id, persona_id = str(scope_id or ""), str(persona_id or "")
            token = str(turn_token or "")
            with self._write_transaction_locked(conn):
                row = conn.execute(
                    "SELECT rule_turn, last_turn FROM wb_rule_scopes "
                    "WHERE scope_id=? AND persona_id=?",
                    (scope_id, persona_id),
                ).fetchone()
                if row is not None and token and row["last_turn"] == token:
                    return int(row["rule_turn"] or 0)
                current = int(row["rule_turn"] or 0) if row is not None else 0
                current += 1
                conn.execute(
                    """INSERT INTO wb_rule_scopes
                       (scope_id, persona_id, rule_turn, last_turn, updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(scope_id, persona_id) DO UPDATE SET
                         rule_turn=excluded.rule_turn,
                         last_turn=excluded.last_turn,
                         updated_at=excluded.updated_at""",
                    (scope_id, persona_id, current, token, _now()),
                )
                self._prune_locked(conn)
                return current

    def current_round(self, scope_id: str, persona_id: str) -> int:
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT rule_turn FROM wb_rule_scopes "
                "WHERE scope_id=? AND persona_id=?",
                (str(scope_id or ""), str(persona_id or "")),
            ).fetchone()
            return int(row["rule_turn"] or 0) if row else 0

    # ── per-rule state ──────────────────────────────────────────────────
    def entry_state(self, scope_id: str, persona_id: str, rule_id: str,
                    round_no: int = 0, create: bool = True) -> Optional[dict]:
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return None
            scope_id, persona_id = str(scope_id or ""), str(persona_id or "")
            row = conn.execute(
                "SELECT * FROM wb_rule_effects "
                "WHERE scope_id=? AND persona_id=? AND rule_id=?",
                (scope_id, persona_id, str(rule_id)),
            ).fetchone()
            if row is not None:
                return self._row_state(row)
            if not create:
                return None
            with self._write_transaction_locked(conn):
                return self._entry_state_tx_locked(
                    conn, scope_id, persona_id, str(rule_id), round_no, True
                )

    def mark_matched(self, scope_id: str, persona_id: str, rule_id: str,
                     round_no: int) -> None:
        """记录最近一次关键词命中；只在真实匹配链调用，模拟不写。"""
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return
            scope_id, persona_id = str(scope_id or ""), str(persona_id or "")
            with self._write_transaction_locked(conn):
                self._entry_state_tx_locked(
                    conn, scope_id, persona_id, str(rule_id), round_no, True
                )
                conn.execute(
                    "UPDATE wb_rule_effects SET last_triggered_turn=?, updated_at=? "
                    "WHERE scope_id=? AND persona_id=? AND rule_id=?",
                    (_as_int(round_no), _now(), scope_id,
                     persona_id, str(rule_id)),
                )

    def mark_injected(self, scope_id: str, persona_id: str, rule_id: str,
                      round_no: int, sticky_rounds: int = 0,
                      cooldown_rounds: int = 0, carry: bool = False) -> None:
        """条目实际进入 prompt 后推进粘性/冷却；同轮重复提交幂等。

        carry=True 表示本轮只是粘性顺延（无关键词命中），只刷新
        last_injected，不重置 sticky/cooldown 计时——否则粘性每轮自我续期，
        永远不过期，剩余轮数也永远显示满额。
        """
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return
            scope_id, persona_id = str(scope_id or ""), str(persona_id or "")
            with self._write_transaction_locked(conn):
                state = self._entry_state_tx_locked(
                    conn, scope_id, persona_id, str(rule_id), round_no, True
                )
                if state is None:
                    return
                current = _as_int(round_no)
                if int(state.get("last_injected", 0) or 0) == current:
                    return
                sticky = _as_int(sticky_rounds)
                cooldown = _as_int(cooldown_rounds)
                sticky_until = int(state.get("sticky_until", 0) or 0)
                cooldown_until = int(state.get("cooldown_until", 0) or 0)
                if not carry:
                    sticky_until = current + sticky if sticky > 0 else 0
                    cooldown_from = sticky_until if sticky_until else current
                    cooldown_until = cooldown_from + cooldown if cooldown > 0 else 0
                if carry:
                    # 粘性顺延不是「关键词命中」：只刷新 last_injected，保留原
                    # last_triggered（否则最近命中时间会被无命中轮次覆盖）。
                    conn.execute(
                        """UPDATE wb_rule_effects SET
                             last_injected_turn=?, updated_at=?
                           WHERE scope_id=? AND persona_id=? AND rule_id=?""",
                        (current, _now(), scope_id, persona_id, str(rule_id)),
                    )
                else:
                    conn.execute(
                        """UPDATE wb_rule_effects SET
                             sticky_until_turn=?, cooldown_until_turn=?,
                             last_injected_turn=?, last_triggered_turn=?, updated_at=?
                           WHERE scope_id=? AND persona_id=? AND rule_id=?""",
                        (sticky_until, cooldown_until, current, current, _now(),
                         scope_id, persona_id, str(rule_id)),
                    )

    def clear_sticky(self, scope_id: str, persona_id: str, rule_id: str) -> None:
        """用户停用或条件不满足时中止粘性，避免恢复启用后继续泄漏。"""
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return
            with self._write_transaction_locked(conn):
                conn.execute(
                    "UPDATE wb_rule_effects SET sticky_until_turn=0, updated_at=? "
                    "WHERE scope_id=? AND persona_id=? AND rule_id=?",
                    (_now(), str(scope_id or ""), str(persona_id or ""), str(rule_id)),
                )

    def status(self, scope_id: str, persona_id: str, rule_id: str,
               round_no: int = 0) -> dict:
        state = self.entry_state(scope_id, persona_id, rule_id, round_no, create=False)
        if not state:
            return {"known": False, "round": int(round_no or 0)}
        current = int(round_no or 0)
        sticky_until = int(state.get("sticky_until", 0) or 0)
        cooldown_until = int(state.get("cooldown_until", 0) or 0)
        return {
            "known": True,
            "round": current,
            "first_seen": int(state.get("first_seen", 0) or 0),
            "sticky_until": sticky_until,
            "sticky_remaining": max(0, sticky_until - current),
            "cooldown_until": cooldown_until,
            # 与 prompt_builder 的口径一致：剩余 = 截止轮 − 当前轮（不含当前轮）。
            # cooldown_until=0 时不得返回 1（此前 +1 导致无冷却也显示 F1）。
            "cooldown_remaining": max(0, cooldown_until - current),
            "last_injected": int(state.get("last_injected", 0) or 0),
            "last_match": int(state.get("last_match", 0) or 0),
        }

    def snapshot(self, scope_id: str, persona_id: str) -> dict:
        with self._lock:
            conn = self._ensure_locked()
            if conn is None:
                return {"round": 0, "rules": {}}
            scope_id, persona_id = str(scope_id or ""), str(persona_id or "")
            row = conn.execute(
                "SELECT rule_turn FROM wb_rule_scopes "
                "WHERE scope_id=? AND persona_id=?",
                (scope_id, persona_id),
            ).fetchone()
            current = int(row["rule_turn"] or 0) if row else 0
            rules = {}
            for item in conn.execute(
                "SELECT rule_id FROM wb_rule_effects "
                "WHERE scope_id=? AND persona_id=? ORDER BY rule_id",
                (scope_id, persona_id),
            ).fetchall():
                rule_id = str(item["rule_id"])
                rules[rule_id] = self.status(
                    scope_id, persona_id, rule_id, current
                )
            return {"round": current, "rules": rules}
