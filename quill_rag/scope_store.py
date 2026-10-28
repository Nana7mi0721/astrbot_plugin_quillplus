# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M7 逻辑会话持久层；复用记忆库连接/事务，不触碰宿主 preferences。"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from ..quill.services.scope import memory_session_key


async def create_scope_tables(conn):
    statements = [
        """CREATE TABLE IF NOT EXISTS qp_origins(origin_key TEXT PRIMARY KEY,umo TEXT UNIQUE NOT NULL,
            platform_id TEXT NOT NULL,message_type TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',last_seen TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS qp_actors(actor_id TEXT PRIMARY KEY,platform_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',canonical_id TEXT,last_seen TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS qp_scopes(scope_id TEXT PRIMARY KEY,name TEXT NOT NULL,
            persona_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS qp_members(origin_key TEXT PRIMARY KEY,scope_id TEXT NOT NULL,
            epoch TEXT NOT NULL,joined_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS qp_turns(seq INTEGER PRIMARY KEY AUTOINCREMENT,scope_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,turn_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,
            origin_key TEXT,actor_id TEXT,source_ref TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(turn_id,role))""",
        "CREATE INDEX IF NOT EXISTS qp_turn_scope ON qp_turns(scope_id,persona_id,seq)",
        """CREATE TABLE IF NOT EXISTS qp_inbox(origin_key TEXT NOT NULL,message_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,turn_id TEXT NOT NULL,status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(origin_key,message_id,content_hash))""",
        """CREATE TABLE IF NOT EXISTS qp_actor_origins(actor_id TEXT NOT NULL,origin_key TEXT NOT NULL,
            PRIMARY KEY(actor_id,origin_key))""",
    ]
    for sql in statements:
        await conn.execute(sql)


class ScopeStore:
    def __init__(self, memory):
        self.memory = memory

    async def observe(self, origin, label="", actor_id=None, actor_label=""):
        async def write(conn):
            existing=await (await conn.execute("SELECT origin_key FROM qp_origins WHERE umo=?",(origin.umo,))).fetchone()
            if existing and existing[0]!=origin.key:
                raise ValueError("同一 UMO 出现不同平台实例，拒绝推断成员关系")
            await conn.execute("""INSERT INTO qp_origins(origin_key,umo,platform_id,message_type,label) VALUES(?,?,?,?,?)
                ON CONFLICT(origin_key) DO UPDATE SET last_seen=CURRENT_TIMESTAMP,label=excluded.label""",
                (origin.key,origin.umo,origin.platform_instance_id,origin.message_type,str(label)[:120]))
            if actor_id and origin.sender_id:
                await conn.execute("""INSERT INTO qp_actors(actor_id,platform_id,sender_id,label) VALUES(?,?,?,?)
                    ON CONFLICT(actor_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP,label=excluded.label""",
                    (actor_id,origin.platform_instance_id,origin.sender_id,str(actor_label)[:120]))
                await conn.execute("INSERT OR IGNORE INTO qp_actor_origins VALUES(?,?)",(actor_id,origin.key))
        await self.memory._exec_tx(write)

    async def inventory(self):
        origins=await self.memory._exec_fetchall("""SELECT o.origin_key,o.umo,o.platform_id,o.message_type,o.label,o.last_seen,
            m.scope_id,m.epoch FROM qp_origins o LEFT JOIN qp_members m USING(origin_key)
            WHERE m.scope_id IS NOT NULL OR o.origin_key IN (SELECT origin_key FROM qp_origins ORDER BY last_seen DESC LIMIT 1000)
            ORDER BY o.last_seen DESC""")
        groups=await self.memory._exec_fetchall("SELECT scope_id,name,persona_id,revision,active,created_at FROM qp_scopes ORDER BY created_at")
        actors=await self.memory._exec_fetchall("""SELECT actor_id,platform_id,sender_id,label,canonical_id FROM qp_actors
            WHERE canonical_id IS NOT NULL OR actor_id IN (SELECT actor_id FROM qp_actors ORDER BY last_seen DESC LIMIT 1000) ORDER BY last_seen DESC""")
        return {
            "origins":[dict(zip(("origin_key","umo","platform_id","message_type","label","last_seen","scope_id","epoch"),row)) for row in origins],
            "groups":[dict(zip(("scope_id","name","persona_id","revision","active","created_at"),row)) for row in groups],
            "actors":[dict(zip(("actor_id","platform_id","sender_id","label","canonical_id"),row)) for row in actors],
        }

    async def save(self, scope_id, name, persona_id, members, *, expected_revision=None, history_sources=()):
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or not isinstance(persona_id,str) or len(persona_id)>200:
            raise ValueError('会话组名称或角色 ID 无效')
        async def write(conn):
            old=await (await conn.execute("SELECT revision FROM qp_scopes WHERE scope_id=? AND active=1",(scope_id,))).fetchone()
            if expected_revision is not None and (not old or old[0]!=expected_revision):
                raise ValueError("会话组已变更，请刷新后重试")
            for key in members:
                row=await (await conn.execute("SELECT scope_id FROM qp_members WHERE origin_key=?",(key,))).fetchone()
                if row and row[0]!=scope_id:
                    raise ValueError("入口已属于其他会话组，请先退出")
            revision=old[0]+1 if old else 1
            await conn.execute("""INSERT INTO qp_scopes(scope_id,name,persona_id,revision) VALUES(?,?,?,?)
                ON CONFLICT(scope_id) DO UPDATE SET name=excluded.name,persona_id=excluded.persona_id,revision=excluded.revision""",
                (scope_id,name,persona_id,revision))
            existing=dict(await (await conn.execute("SELECT origin_key,epoch FROM qp_members WHERE scope_id=?",(scope_id,))).fetchall())
            await conn.execute("DELETE FROM qp_members WHERE scope_id=?",(scope_id,))
            for key in members:
                await conn.execute("INSERT INTO qp_members(origin_key,scope_id,epoch) VALUES(?,?,?)",(key,scope_id,existing.get(key,uuid4().hex)))
            # 只在首次建组、显式预览确认后复制指定来源；原记录永远保留。
            target=memory_session_key(scope_id,persona_id)
            for source in history_sources:
                await conn.execute("""INSERT OR IGNORE INTO memories
                    (session_id,summary,chat_summary,vector,dim,record_id,source_ref,created_at,
                     trust_level,process_status,is_core,strength,useful_count,useful_score,is_active)
                    SELECT ?,summary,chat_summary,vector,dim,?||record_id,?||record_id,created_at,
                    trust_level,process_status,is_core,strength,useful_count,useful_score,is_active
                    FROM memories WHERE session_id=?""",(target,scope_id+":", "history:"+source+":",source))
                logs=await (await conn.execute("SELECT id,role,content,record_id,timestamp FROM chat_logs WHERE session_id=? ORDER BY id",(source,))).fetchall()
                for log_id,role,content,rid,created in logs:
                    ref="history:"+source+":"+rid
                    turn_id=scope_id+":"+rid
                    await conn.execute("""INSERT INTO qp_turns(scope_id,persona_id,turn_id,role,content,source_ref,status,revision,created_at)
                        VALUES(?,?,?,?,?,?,'completed',?,?)""",(scope_id,persona_id,turn_id,role,content,ref,revision,created))
                    await conn.execute("""INSERT INTO chat_logs(session_id,role,content,record_id,origin_ref,turn_id,process_status,timestamp)
                        VALUES(?,?,?,?,?,?,'imported',?)""",(target,role,content,scope_id+":"+rid,source,turn_id,created))
            return revision
        return await self.memory._exec_tx(write)

    async def archive(self, scope_id, revision):
        async def write(conn):
            cur=await conn.execute("UPDATE qp_scopes SET active=0,revision=revision+1 WHERE scope_id=? AND revision=? AND active=1",(scope_id,revision))
            if cur.rowcount!=1:
                raise ValueError("会话组已变更，请刷新后重试")
            await conn.execute("DELETE FROM qp_members WHERE scope_id=?",(scope_id,))
        await self.memory._exec_tx(write)

    async def source_preview(self, sessions):
        result=[]
        for sid in sessions:
            memories=await self.memory._exec_fetchone("SELECT COUNT(*),MAX(id) FROM memories WHERE session_id=?",(sid,))
            logs=await self.memory._exec_fetchone("SELECT COUNT(*),MAX(id) FROM chat_logs WHERE session_id=?",(sid,))
            if memories[0]>1000 or logs[0]>1000:
                raise ValueError("单来源历史超过 1000 条，请先整理或选择从此刻共享")
            samples=await self.memory._exec_fetchall("SELECT role,content FROM chat_logs WHERE session_id=? ORDER BY id DESC LIMIT 3",(sid,))
            records=await self.memory._exec_fetchall("SELECT record_id,summary,chat_summary,hex(vector),is_core,trust_level FROM memories WHERE session_id=? ORDER BY id",(sid,))
            source_logs=await self.memory._exec_fetchall("SELECT record_id,role,content FROM chat_logs WHERE session_id=? ORDER BY id",(sid,))
            result.append({"session_id":sid,"memories":memories[0],"logs":logs[0],"memory_max":memories[1],"log_max":logs[1],
                           "fingerprint":hashlib.sha256(repr((records,source_logs)).encode()).hexdigest(),
                           "samples":[{"role":r[0],"text":r[1][:300]} for r in reversed(samples)]})
        return result

    async def _revision_chain(self, connection, turn_id, scope_id, persona_id):
        chain = []
        while turn_id and turn_id not in chain:
            row = await (await connection.execute("SELECT source_ref FROM qp_turns WHERE turn_id=? AND role='user' AND scope_id=? AND persona_id=?", (turn_id,scope_id,persona_id))).fetchone()
            try:
                parent = json.loads(row[0]).get('revision_of') if row else None
            except (ValueError, TypeError, AttributeError):
                parent = None
            if not parent or parent in chain:
                break
            chain.append(parent)
            turn_id = parent
        return chain

    async def recent(self, scope_id, persona_id, limit=40, *, pending_turn_id=None):
        async with self.memory._lock:
            self.memory._require_readable()
            hidden = await self._revision_chain(self.memory._conn,pending_turn_id,scope_id,persona_id) if pending_turn_id else []
            extra = " AND turn_id NOT IN (" + ','.join('?' for _ in hidden) + ")" if hidden else ''
            rows=await self.memory._exec_fetchall("""SELECT seq,turn_id,role,content,origin_key,actor_id,source_ref,created_at FROM qp_turns t
                WHERE scope_id=? AND persona_id=? AND status='completed'
                AND NOT EXISTS (SELECT 1 FROM qp_turns r WHERE r.scope_id=t.scope_id AND r.persona_id=t.persona_id
                    AND r.role='user' AND r.status IN ('completed','superseded')
                    AND json_extract(CASE WHEN json_valid(r.source_ref) THEN r.source_ref ELSE '{}' END,'$.revision_of')=t.turn_id)"""
                + extra + " ORDER BY seq DESC LIMIT ?", (scope_id,persona_id,*hidden,limit))
        return [dict(zip(("seq","turn_id","role","content","origin_key","actor_id","source_ref","created_at"),row)) for row in reversed(rows)]

    async def begin(self, turn, origin, content, message_id=""):
        digest=hashlib.sha256(content.encode()).hexdigest()
        async def write(conn):
            previous=None
            if message_id:
                old=await (await conn.execute("SELECT status,turn_id FROM qp_inbox WHERE origin_key=? AND message_id=? AND content_hash=?",(origin.key,message_id,digest))).fetchone()
                if old and old[0] in ('running','completed'):
                    return False
                previous=await (await conn.execute("SELECT turn_id FROM qp_inbox WHERE origin_key=? AND message_id=? ORDER BY rowid DESC LIMIT 1",(origin.key,message_id))).fetchone()
                await conn.execute("""INSERT INTO qp_inbox(origin_key,message_id,content_hash,turn_id,status) VALUES(?,?,?,?,'running')
                    ON CONFLICT(origin_key,message_id,content_hash) DO UPDATE SET turn_id=excluded.turn_id,status='running',attempts=attempts+1""",
                    (origin.key,message_id,digest,turn.turn_id))
            source=json.dumps({'message_id':message_id or None,'content_hash':digest,'revision_of':previous[0] if previous else None},ensure_ascii=False)
            await conn.execute("""INSERT OR IGNORE INTO qp_turns(scope_id,persona_id,turn_id,role,content,origin_key,actor_id,source_ref,status,revision)
                VALUES(?,?,?,'user',?,?,?,?,'running',?)""",(turn.scope_id,turn.persona_id,turn.turn_id,content[:12000],origin.key,turn.actor_id,source,turn.scope_revision))
            return True
        return await self.memory._exec_tx(write)

    async def finish(self, turn, content=None):
        async def write(conn):
            row=await (await conn.execute("SELECT status,origin_key FROM qp_turns WHERE turn_id=? AND role='user'",(turn.turn_id,))).fetchone()
            if not row or row[0]!='running':
                return {'applied':False}
            status='completed' if content else 'interrupted'
            # 失败/取消的输入保留审计，但不伪装成下一入口已完成的经历。
            await conn.execute("UPDATE qp_turns SET status=? WHERE turn_id=? AND status='running'",(status,turn.turn_id))
            await conn.execute("UPDATE qp_inbox SET status=? WHERE turn_id=? AND status='running'",(status,turn.turn_id))
            if content:
                previous = await self._revision_chain(conn,turn.turn_id,turn.scope_id,turn.persona_id)
                for old_turn in previous:
                    # 只有成功的新修订替换消费视图；失败重试不抹掉上次已完成的经历。
                    await conn.execute("UPDATE qp_turns SET status='superseded' WHERE turn_id=? AND scope_id=? AND persona_id=? AND status='completed'", (old_turn,turn.scope_id,turn.persona_id))
                await conn.execute("""INSERT OR IGNORE INTO qp_turns(scope_id,persona_id,turn_id,role,content,origin_key,status,revision)
                    VALUES(?,?,?,'assistant',?,?,'completed',?)""",(turn.scope_id,turn.persona_id,turn.turn_id,content[:12000],row[1],turn.scope_revision))
            return {'applied':True}
        await self.memory._exec_tx(write)

    async def interrupt_pending(self):
        async def write(conn):
            await conn.execute("UPDATE qp_turns SET status='interrupted' WHERE status='running'")
            await conn.execute("UPDATE qp_inbox SET status='interrupted' WHERE status='running'")
        await self.memory._exec_tx(write)

    async def link_actors(self, actors, canonical_id):
        async def write(conn):
            for actor in actors:
                cur=await conn.execute("UPDATE qp_actors SET canonical_id=? WHERE actor_id=?",(canonical_id,actor))
                if cur.rowcount!=1:
                    raise ValueError("参与者尚未观测到")
        await self.memory._exec_tx(write)

    async def actor_scopes(self, actors):
        scopes = set()
        for actor in actors:
            origins = await self.memory._exec_fetchall("SELECT origin_key FROM qp_actor_origins WHERE actor_id=?",(actor,))
            if origins:
                rows = await self.memory._exec_fetchall("SELECT DISTINCT m.scope_id FROM qp_members m JOIN qp_actor_origins a USING(origin_key) WHERE a.actor_id=?",(actor,))
            else:
                # 旧库未记参与者入口关系；仅在资料不足时保守保护同平台组，等真实事件补齐。
                rows = await self.memory._exec_fetchall("SELECT DISTINCT m.scope_id FROM qp_members m JOIN qp_origins o USING(origin_key) JOIN qp_actors a ON a.platform_id=o.platform_id WHERE a.actor_id=?",(actor,))
            scopes.update(row[0] for row in rows)
        return scopes
