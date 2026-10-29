# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 独立用量台账：只存元数据，绝不参与剧情读档或记忆恢复。"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
import hashlib
from pathlib import Path
from uuid import uuid4

import aiosqlite

try:
    from ..quill.services.usage import native_cache_reported
except ImportError:  # 保留直接脚本加载。
    from quill.services.usage import native_cache_reported


def _private_record(record):
    """M12：台账只保留与 Trace 一致的不可逆短标识；迁移旧元数据，不碰剧情库。"""
    value = dict(record)
    if value.get("identity_format") != "sha256_16":
        for key in ("source_umo", "scope_id", "actor_id"):
            text = str(value.get(key) or "")
            value[key] = hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""
        value["identity_format"] = "sha256_16"
    return value


class UsageStore:
    def __init__(self, path: str):
        self.path = path
        self._db = None
        self._lock = asyncio.Lock()
        self._last_prune = 0.0
        self._writes_since_prune = 0
        self._last_limits = None

    @property
    def available(self):
        return self._db is not None

    async def initialize(self):
        async with self._lock:
            if self._db is not None:
                return
            db = await aiosqlite.connect(self.path, timeout=2)
            try:
                db.row_factory = aiosqlite.Row
                version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
                if version not in (0, 1, 2):
                    raise ValueError("unsupported_usage_schema:" + str(version))
                if version < 2 and await (await db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_calls'")).fetchone():
                    # 隔离坏行之前保留一致性快照；原 payload 可能含旧身份，不能复制到可查询台账。
                    try:
                        from .._backup_util import snapshot_sqlite
                    except ImportError:
                        from _backup_util import snapshot_sqlite
                    folder = Path(self.path).parent / '.schema_backups'
                    folder.mkdir(mode=0o700, exist_ok=True)
                    await asyncio.to_thread(snapshot_sqlite,self.path,str(folder / ('usage.v'+str(version)+'.'+uuid4().hex+'.db')))
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("""CREATE TABLE IF NOT EXISTS usage_calls (
                    call_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                    created_at REAL NOT NULL, source_umo TEXT NOT NULL,
                    scope_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL, model TEXT NOT NULL,
                    purpose TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL)""")
                await db.execute("CREATE TABLE IF NOT EXISTS usage_quarantine(call_id TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,error_class TEXT NOT NULL)")
                await db.execute("CREATE TABLE IF NOT EXISTS usage_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL)")
                if version < 2:
                    cursor = await db.execute("SELECT * FROM usage_calls ORDER BY call_id")
                    while rows := await cursor.fetchmany(256):
                        for row in rows:
                            try:
                                decoded = json.loads(row['payload'])
                                if not isinstance(decoded,dict):
                                    raise ValueError('usage_payload_not_object')
                                record = _private_record({**dict(row), **decoded})
                                record.pop('payload',None)
                                # 迁移不能让坏 usage/费用字段在下一次查询时继续毒化整页。
                                _Totals().add(record)
                                payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
                            except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError) as exc:
                                await db.execute("INSERT OR REPLACE INTO usage_quarantine VALUES(?,?,?)", (row['call_id'],hashlib.sha256(row['payload'].encode()).hexdigest(),type(exc).__name__))
                                await db.execute("DELETE FROM usage_calls WHERE call_id=?", (row['call_id'],))
                                continue
                            await db.execute("UPDATE usage_calls SET source_umo=?,scope_id=?,payload=? WHERE call_id=?",
                                (record["source_umo"], record["scope_id"], payload, row["call_id"]))
                    await cursor.close()
                for name, columns in (("usage_order", "created_at DESC, call_id DESC"),
                                      ("usage_scope", "scope_id, created_at DESC"),
                                      ("usage_purpose", "purpose, created_at DESC"),
                                      ("usage_provider", "provider_id, created_at DESC")):
                    await db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON usage_calls({columns})")
                await db.execute("PRAGMA user_version=2")
                await db.commit()
                self._db = db
            except BaseException:
                await db.rollback()
                await db.close()
                raise

    async def put(self, record: dict, retention_days: int, max_records: int):
        record = _private_record(record)
        async with self._lock:
            if self._db is None:
                raise RuntimeError("usage_store_unavailable")
            try:
                # 重复最终回调幂等，不能静默改写已冻结的历史费用/身份。
                await self._db.execute("INSERT OR IGNORE INTO usage_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    record["call_id"], record["turn_id"], record["created_at"],
                    record["source_umo"], record["scope_id"], record["persona_id"],
                    record["provider_id"], record["model"], record["purpose"], record["status"],
                    json.dumps(record, ensure_ascii=False, allow_nan=False),
                ))
                self._writes_since_prune += 1
                # M8：不再每条 INSERT 都执行两次 DELETE；物理上限最多延后 99 条。
                if self._last_limits != (retention_days, max_records) or time.monotonic() - self._last_prune >= 60 or self._writes_since_prune >= 100:
                    await self._prune(retention_days, max_records)
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise

    async def _prune(self, days: int, limit: int):
        await self._db.execute("DELETE FROM usage_calls WHERE created_at < ?",
                               (time.time() - max(1, days) * 86400,))
        await self._db.execute("""DELETE FROM usage_calls WHERE call_id IN
            (SELECT call_id FROM usage_calls ORDER BY created_at DESC, call_id DESC LIMIT -1 OFFSET ?)""",
                               (max(1, limit),))
        self._last_prune, self._writes_since_prune = time.monotonic(), 0
        self._last_limits = (days, limit)

    async def query(self, *, days=7, scope_id="", source_umo="", persona_id="", provider_id="",
                    purpose="", model="", limit=50, offset=0,
                    retention_days=30, max_records=20000, scope_groups=None):
        # GET 无清理副作用；汇总覆盖所有匹配样本，limit 只控制明细。
        days = min(max(1, int(days)), max(1, retention_days), 366)
        clauses, args = ["created_at >= ?"], [time.time() - days * 86400]
        for key, value in (("scope_id", scope_id), ("source_umo", source_umo), ("persona_id", persona_id),
                           ("provider_id", provider_id), ("purpose", purpose), ("model", model)):
            if value:
                clauses.append(f"{key} = ?")
                args.append(value)
        limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
        summary, items, sessions = _Totals(), [], {}
        groups = {key: {} for key in ("group", "purpose", "model", "persona_id", "provider_id")}
        daily = {}
        async with self._lock:
            if self._db is None:
                raise RuntimeError("usage_store_unavailable")
            cursor = await self._db.execute("SELECT payload FROM usage_calls WHERE "
                + " AND ".join(clauses) + " ORDER BY created_at DESC, call_id DESC", args)
            try:
                while rows := await cursor.fetchmany(256):
                    for row in rows:
                        record = json.loads(row[0])
                        # P1：只重解释旧版漏判的宿主归一化证据，原始台账、计数和金额均不改写。
                        usage = record.get('usage') or {}
                        if usage.get('usage_source') == 'native' and not usage.get('cache_reported') and native_cache_reported(usage):
                            record['usage'] = {**usage, 'cache_reported': True, 'cache_unknown': False,
                                               'cache_report_source': 'host_normalized', 'cache_evidence_reclassified': True}
                        if offset <= summary.calls < offset + limit:
                            items.append(record)
                        summary.add(record)
                        for dimension, buckets in groups.items():
                            if dimension == "group":
                                scope = record.get("scope_id") or ""
                                key = scope if scope in (scope_groups or {}) else "独立会话"
                            else:
                                key = record.get(dimension) or "unknown"
                            if key not in buckets:
                                buckets[key] = _Totals()
                            buckets[key].add(record)
                        day = time.strftime("%Y-%m-%d", time.gmtime(record["created_at"]))
                        if day not in daily:
                            daily[day] = _Totals()
                        daily[day].add(record)
                        sid = record.get("scope_id", "")
                        if sid not in sessions:
                            sessions[sid] = {"scope_id": sid, "last_at": record["created_at"], "calls": 0}
                        sessions[sid]["calls"] += 1
            finally:
                await cursor.close()
        # 选择器保留整个日期窗口的入口/逻辑会话，不因当前筛选收缩到一个选项。
        async with self._lock:
            inventories = {}
            for column in ('scope_id','source_umo'):
                rows = await (await self._db.execute(f"SELECT {column},COUNT(*),MAX(created_at) FROM usage_calls WHERE created_at>=? GROUP BY {column}", (args[0],))).fetchall()
                inventories[column] = [{column:r[0],'calls':r[1],'last_at':r[2]} for r in rows if r[0]]
        return {"summary": summary.result(), "groups": {
                    key: [{"key": name, **({"label": (scope_groups or {}).get(name, name)} if key == "group" else {}), **totals.result()} for name, totals in buckets.items()]
                    for key, buckets in groups.items()}, "sessions": inventories["scope_id"], "origins": inventories["source_umo"],
                "daily": [{"day": day, **totals.result()} for day, totals in sorted(daily.items())],
                "items": items, "total": summary.calls, "limit": limit, "offset": offset,
                "timezone": "UTC", "identity_format": "sha256_16"}

    async def clear(self):
        async with self._lock:
            if self._db is None:
                raise RuntimeError("usage_store_unavailable")
            await self._db.execute("DELETE FROM usage_calls")
            await self._db.commit()

    async def note_gaps(self, count):
        if count <= 0:
            return
        async with self._lock:
            if self._db is None:
                raise RuntimeError('usage_store_unavailable')
            await self._db.execute("INSERT INTO usage_meta VALUES('dropped_records',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (count,))
            await self._db.commit()

    async def diagnostics(self):
        async with self._lock:
            if self._db is None:
                raise RuntimeError('usage_store_unavailable')
            row = await (await self._db.execute("SELECT value FROM usage_meta WHERE key='dropped_records'")).fetchone()
            quarantined = (await (await self._db.execute("SELECT COUNT(*) FROM usage_quarantine")).fetchone())[0]
            return {'dropped_records': row[0] if row else 0, 'quarantined_records': quarantined}

    async def close(self):
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None


class _Totals:
    """一遍解析与累加，避免保留全量 payload 后反复扫描。"""
    def __init__(self):
        self.calls = self.known = self.cache_known = self.applicable = self.partial = self.priced = 0
        self.non_user = self.eligible = self.hit = 0
        self.turns = set()
        self.tokens = dict.fromkeys(("input_total", "input_cached", "cache_write", "output"), 0)
        self.fields = dict.fromkeys(self.tokens, 0)
        self.costs, self.cost_sources = {}, {}

    def add(self, row):
        self.calls += 1
        usage = row.get("usage") or {}
        user = row.get("turn_kind") == "user"
        if user:
            self.turns.add(row["turn_id"])
        self.non_user += not user
        self.known += usage.get("input_total") is not None or usage.get("output") is not None
        for key in self.tokens:
            value = usage.get(key)
            if value is not None:
                self.tokens[key] += value
                self.fields[key] += 1
        applicable = usage.get("cache_applicable", row.get("purpose") not in ("embedding", "rerank", "decision", "decision_probe"))
        applicable = applicable and row.get("status") == "completed"
        self.applicable += bool(applicable)
        if applicable and usage.get("cache_reported") and not usage.get("cache_unknown", False) and usage.get("input_total") is not None:
            self.cache_known += 1
            self.eligible += usage["input_total"]
            self.hit += usage.get("input_cached") or 0
        self.partial += row.get("billing_coverage") != "complete"
        amount, currency = row.get("cost_amount"), row.get("currency")
        if amount is not None and currency:
            self.priced += 1
            source = row.get("cost_source", "unknown")
            self.costs[currency] = self.costs.get(currency, Decimal(0)) + Decimal(amount)
            bucket = self.cost_sources.setdefault(currency, {})
            bucket[source] = bucket.get(source, Decimal(0)) + Decimal(amount)

    def result(self):
        return {"calls": self.calls, "turns": len(self.turns), "non_user_calls": self.non_user,
                "known_usage_calls": self.known, "cache_reported_calls": self.cache_known,
                "cache_applicable_calls": self.applicable,
                "cache_unknown_calls": self.applicable - self.cache_known,
                "partial_calls": self.partial, "priced_calls": self.priced,
                "unpriced_calls": self.calls - self.priced, "field_coverage": self.fields,
                **{k: v if self.fields[k] else None for k, v in self.tokens.items()},
                "cache_hit_rate": self.hit / self.eligible if self.eligible else None,
                "cache_eligible_input": self.eligible,
                "cache_coverage": self.cache_known / self.applicable if self.applicable else None,
                "usage_coverage": self.known / self.calls if self.calls else None,
                "costs": {c: format(v, "f") for c, v in self.costs.items()},
                "costs_legacy_may_mix_sources": True,
                "costs_by_source": {c: {s: format(v, "f") for s, v in bucket.items()}
                                    for c, bucket in self.cost_sources.items()}}


def _summarize(records: list[dict]) -> dict:
    totals = _Totals()
    for row in records:
        totals.add(row)
    return totals.result()
