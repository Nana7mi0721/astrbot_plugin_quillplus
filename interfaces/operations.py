# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 跨 SQLite/JSON 操作登记与幂等续做；不把它称为跨文件事务。"""

import asyncio
import weakref
import time

from astrbot.api import logger

from ..quill.core.errors import StorageError

_locks = weakref.WeakValueDictionary()


async def _resume(plugin, operation):
    store = plugin.rag_memory_store
    payload = operation["payload"]
    from ..quill.services.scope import memory_session_key
    if not isinstance(payload.get('scope_id'),str) or not isinstance(payload.get('persona_id'),str) or memory_session_key(payload['scope_id'],payload['persona_id']) != operation['session_id']:
        raise StorageError('待恢复操作的归属信息无效，已保留原记录并暂停相关会话')
    scope, persona = payload["scope_id"], payload["persona_id"]
    kind, phase, operation_id = operation["kind"], operation["phase"], operation["operation_id"]
    if phase not in ('registered','database_done','state_done'):
        raise StorageError('待恢复操作阶段无效，已保留原记录')
    if kind not in ("clear_memory", "reset_conversation"):
        raise StorageError("存在不受支持的待恢复操作，已保留记录")
    counts = payload.get("counts") or {"memories": 0, "chat_logs": 0}
    if phase == "registered":
        counts = await store.clear_session(operation["session_id"], memories=kind == "clear_memory",
                                           logs=True, operation_id=operation_id)
        phase = "database_done"
    if phase == "database_done":
        await plugin.state_manager.reset_role_counters(scope, persona, reset_quill=kind == "reset_conversation")
        await store.update_operation(operation_id, "state_done")
        phase = "state_done"
    conversation_cleared = False
    if phase == "state_done":
        cid = payload.get("conversation_id")
        targets=payload['conversation_targets'] if 'conversation_targets' in payload else ([{'umo':payload['origin_umo'],'cid':cid}] if cid else [])
        if targets and kind == "reset_conversation":
            manager = getattr(plugin.context, "conversation_manager", None)
            if manager is None:
                raise StorageError("对话管理器暂不可用，重置操作保留待恢复")
            # 只清登记时绑定的 conversation，不在完成时查新的当前卡。
            for target in targets:
                await manager.update_conversation(target['umo'],target['cid'],[])
            conversation_cleared = True
        await store.update_operation(operation_id, "completed", "completed")
    return {**counts, "conversation_cleared": conversation_cleared, "operation_id": operation_id,
            'affected':payload.get('affected') or ([payload['origin_umo']] if payload.get('origin_umo') else [])}


async def recover_operations(plugin, *, scope_id=None):
    store = getattr(plugin, "rag_memory_store", None)
    if store is None or not getattr(store, "_writable", False):
        return
    pending = await store.pending_operations(scope_id=scope_id)
    blocked = 0
    for operation in pending:
        key = operation["session_id"]
        lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 并发恢复者必须重读阶段，不能拿过期 registered 状态再次清日志。
            latest = next(iter(await store.pending_operations(operation_id=operation['operation_id'])), None)
            if latest:
                if latest['payload'].get('retry_after', 0) > time.time():
                    blocked += 1
                    continue
                try:
                    await _resume(plugin, latest)
                except Exception as exc:
                    await store.fail_operation(latest['operation_id'], exc)
                    blocked += 1
                    logger.error('[Quill Recovery] 操作 %s 恢复失败（%s），保留阶段并阻止相关会话继续', latest['operation_id'], type(exc).__name__)
    if blocked and scope_id is not None:
        raise StorageError("此会话有未完成的清理/重置，已暂停生成；请在任务页检查存储并稍后重试", detail="scope_recovery_blocked")


async def run_scope_operation(plugin, kind, scope_id, persona_id, *, origin_umo=None, expected_revision=None):
    from ..quill.services.scope import current_turn
    physical=origin_umo or scope_id
    scope_id=plugin.state_manager.role_scope(scope_id)
    grouped=scope_id in plugin.scope_resolver.groups
    if grouped:
        async with plugin.shared_scopes.admin_lock:
            plugin.shared_scopes.guard_idle(scope_id)
            turn=current_turn()
            revision = turn.scope_revision if turn else expected_revision
            if revision != plugin.scope_resolver.groups[scope_id]['revision']:
                raise ValueError('会话组已变更，请重新核对影响范围')
            return await _run_scope_operation(plugin,kind,scope_id,persona_id,origin_umo=physical)
    return await _run_scope_operation(plugin,kind,scope_id,persona_id,origin_umo=physical)


async def _run_scope_operation(plugin,kind,scope_id,persona_id,*,origin_umo):
    physical=origin_umo
    grouped=scope_id in plugin.scope_resolver.groups
    store = getattr(plugin, "rag_memory_store", None)
    if store is None or not getattr(store, "_writable", False):
        raise StorageError("记忆库暂不可用，未执行跨文件清理")
    session_id = plugin._get_memory_session_id(scope_id, persona_id)
    lock = _locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        pending = next(iter(await store.pending_operations(session_id=session_id)), None)
        if pending:
            result = await _resume(plugin, pending)
            if pending["kind"] == kind:
                return result
        mapping = await plugin.state_manager.get_persona_conv_map(physical)
        payload = {"scope_id": scope_id, "persona_id": persona_id,
                   "origin_umo": physical,
                   "conversation_id": mapping.get(persona_id) if kind == "reset_conversation" else None}
        if grouped:
            from ..quill.services.character import conversation_binding_key
            members=[o for o in plugin.shared_scopes.index['origins'] if o['scope_id']==scope_id]
            payload['affected']=[o['umo'] for o in members]
            payload['conversation_targets']=[]
            for member in members:
                convs=await plugin.state_manager.get_persona_conv_map(member['umo'])
                cid=convs.get(conversation_binding_key(scope_id,persona_id,member['epoch']))
                if cid: payload['conversation_targets'].append({'umo':member['umo'],'cid':cid})
        operation_id = await store.begin_operation(kind, session_id, payload)
        return await _resume(plugin, {"operation_id":operation_id,"kind":kind,"session_id":session_id,
                                      "phase":"registered","payload":payload})
