# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M7 会话组编排；物理投递不变，角色数据映射与请求临时视图分离。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from uuid import uuid4

from astrbot.api import logger

from .scopes import physical_origin, event_turn
from .prompt_cache import temporary_capability, temporary_host_capability, temporary_text
from ..quill.services.scope import bind_turn, memory_session_key
from ..quill.services.shared_context import history_view, last_options
from ..quill_rag.scope_store import ScopeStore


class GroupBusy(ValueError):
    pass


class StalePreview(ValueError):
    pass


class SharedScopes:
    def __init__(self, plugin):
        self.plugin=plugin
        self.store=None
        self.index={"origins":[],"groups":[],"actors":[]}
        self.admin_lock=asyncio.Lock()
        self.locks=weakref.WeakValueDictionary()

    async def initialize(self, memory):
        self.store=ScopeStore(memory)
        await self.store.interrupt_pending()
        await self.refresh()
        self.bind_state()

    def bind_state(self):
        state=self.plugin.state_manager
        resolver=self.plugin.scope_resolver
        state.scope_for=resolver.scope_for
        state.scope_persona=resolver.selected_persona
        state.set_scope_persona=self.select_persona

    async def refresh(self):
        self.index=await self.store.inventory()
        resolver=self.plugin.scope_resolver
        resolver.groups={g["scope_id"]:g for g in self.index["groups"] if g["active"]}
        resolver.memberships={o["umo"]:o for o in self.index["origins"] if o["scope_id"] in resolver.groups}
        resolver.actor_links={a["actor_id"]:a["canonical_id"] for a in self.index["actors"] if a["canonical_id"]}

    def require_ready(self):
        if not self.store or not getattr(self.store.memory,"_writable",False):
            raise ValueError("会话组存储暂不可用，请先检查存储状态")

    def guard_idle(self, scope_id):
        lock=self.locks.get(scope_id)
        if lock and lock.locked():
            raise GroupBusy("会话组正在生成回复，请在本轮结束后修改或重置")

    def busy(self,scope_id):
        lock=self.locks.get(scope_id)
        return bool(lock and lock.locked())

    async def observe(self,event):
        if event.get_extra('_quill_origin_observed'):
            return
        if not self.store or not getattr(self.store.memory,"_writable",False):
            return
        origin=physical_origin(event)
        # 记录的是平台实例与 sender 的原始身份；关联只在管理员显式操作时发生。
        actor="actor:"+hashlib.sha256(json.dumps([origin.platform_instance_id,origin.sender_id],ensure_ascii=False).encode()).hexdigest() if origin.platform_instance_id and origin.sender_id else None
        label=str(event.get_sender_name() or "") if hasattr(event,"get_sender_name") else ""
        try:
            await self.store.observe(origin,actor_id=actor,actor_label=label)
        except Exception as exc:
            if origin.umo in self.plugin.scope_resolver.memberships:
                event.set_extra('_quill_origin_error', type(exc).__name__)
                event.set_result(event.plain_result('会话组来源登记失败，本轮已停止，请检查存储状态。'))
                event.stop_event()
                logger.error('[Quill Scope] 成员来源登记失败（%s），拒绝继续共享请求', type(exc).__name__)
                return
            logger.warning('[Quill Scope] 来源登记失败（%s），独立会话继续',type(exc).__name__)
            return
        # 查询时读库存储清单，热点请求只更新自己的来源缓存。
        if not any(o["origin_key"]==origin.key for o in self.index["origins"]):
            await self.refresh()
        event.set_extra('_quill_origin_observed',True)

    async def acquire(self,event):
        """锁随真实事件任务结束释放，失败/取消也不留下永久占用。不同组互不阻塞。"""
        if event.get_extra("_quill_scope_lease"):
            return
        origin=physical_origin(event)
        while True:
            async with self.admin_lock:
                scope=self.plugin.scope_resolver.scope_for(origin.umo)
                if scope==origin.umo:
                    return
                lock=self.locks.setdefault(scope,asyncio.Lock())
                if not lock.locked():
                    await lock.acquire()
                    task=asyncio.current_task()
                    event.set_extra("_quill_scope_lease",scope)
                    def finished(done, held=lock, ev=event):
                        held.release()
                        turn=event_turn(ev)
                        if turn and ev.get_extra("_quill_shared_started") and not ev.get_extra("_quill_shared_finished"):
                            async def interrupt():
                                try: await ev.get_extra('_quill_group_store').finish(turn)
                                except Exception: pass  # 重载后的旧连接不能补写，启动恢复会标记中断。
                            self.plugin._spawn(interrupt())
                    task.add_done_callback(finished)
                    return
            # 不持管理锁排队，管理员能查状态；被改组后重新解析目标。
            await lock.acquire()
            lock.release()

    async def select_persona(self, scope_id, persona_id):
        async with self.admin_lock:
            if not isinstance(persona_id, str) or len(persona_id) > 200 or (persona_id and not await self.plugin.persona_manager.get_persona(persona_id)):
                raise ValueError('角色卡不存在或角色 ID 无效')
            self.guard_idle(scope_id)
            group=self.plugin.scope_resolver.groups.get(scope_id)
            if not group:
                raise ValueError("会话组已解散")
            members=[o["origin_key"] for o in self.index["origins"] if o["scope_id"]==scope_id]
            await self.store.save(scope_id,group["name"],persona_id,members,expected_revision=group["revision"])
            await self.refresh()

    async def waiting_supported(self,event,turn):
        if turn.scope_id==turn.origin_umo:
            return True
        capability=await temporary_host_capability(self.plugin.context,event)
        if capability['supported']:
            return True
        event.set_result(event.plain_result('共享上下文不可用，本轮已停止：'+capability['reason']+'。请检查模型配置或联系管理员从会话组移除此入口。'))
        event.stop_event()
        return False

    async def preview(self,data):
        self.require_ready()
        await self.refresh()
        name=data.get("name")
        members=data.get("members")
        persona=data.get("persona_id")
        scope=data.get("scope_id") or ""
        if not isinstance(name,str) or not name.strip() or len(name)>80:
            raise ValueError("请填写 1–80 字会话组名称")
        if not isinstance(members,list) or not 1<=len(members)<=50 or any(not isinstance(k,str) for k in members) or len(set(members))!=len(members):
            raise ValueError("请勾选 1–50 个不同的已观测入口")
        if not isinstance(persona,str) or len(persona)>200:
            raise ValueError("角色 ID 无效")
        if persona and not await self.plugin.persona_manager.get_persona(persona):
            raise ValueError("所选角色卡不存在")
        known={o['origin_key']:o for o in self.index['origins']}
        if any(k not in known for k in members):
            raise ValueError("只能选择已观测的入口；请先从该入口发送 /quill")
        old=self.plugin.scope_resolver.groups.get(scope)
        if scope and not old:
            raise ValueError("会话组不存在或已解散")
        pending=await self.store.memory.pending_operations()
        selected_scopes={known[k]['umo'] for k in members}|{scope}
        if any(operation['payload'].get('scope_id') in selected_scopes for operation in pending):
            raise ValueError('所选入口存在待恢复清理操作，请先恢复后再修改分组')
        for key in members:
            if known[key]['scope_id'] and known[key]['scope_id']!=scope:
                raise ValueError("入口已属于其他会话组，请先退出")
        sources=data.get("history_sources",[])
        allowed={memory_session_key(known[k]['umo'],persona) for k in members}
        if not isinstance(sources,list) or len(sources)>20 or any(not isinstance(s,str) or s not in allowed for s in sources) or len(set(sources))!=len(sources):
            raise ValueError("历史来源必须来自所选入口的同一角色桶")
        if old and sources:
            raise ValueError("旧历史仅在建组时纳入；编辑成员不会自动搬入历史")
        seed=data.get('seed_origin') or ''
        if seed and (old or seed not in members):
            raise ValueError("只能在建组时从一个所选入口复制状态")
        values=await self.plugin.state_manager.get_session_vars(known[seed]['umo'],persona_id=persona,claim_legacy=False) if seed else {}
        history=await self.store.source_preview(sources)
        normalized={"scope_id":scope,"name":name.strip(),"persona_id":persona,"members":sorted(members),
                    "history_sources":sorted(sources),"seed_origin":seed,"expected_revision":old['revision'] if old else None}
        payload={"request":normalized,"history":history,"state_vars":values,
                 "affected":[known[k]['umo'] for k in members],"removed":[o['umo'] for o in self.index['origins'] if o['scope_id']==scope and o['origin_key'] not in members] if old else []}
        payload['preview_token']=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return payload

    async def save(self,data):
        async with self.admin_lock:
            self.plugin.state_manager._require_writable()
            preview=await self.preview(data)
            if data.get('preview_token')!=preview['preview_token']:
                raise StalePreview("请先预览并确认共享范围；内容已变化时须重新预览")
            request=preview['request']
            scope=request['scope_id']
            if scope:
                self.guard_idle(scope)
            else:
                if len(self.plugin.scope_resolver.groups)>=200:
                    raise ValueError("活动会话组已达 200 个")
                scope='scope:'+uuid4().hex
                # 先准备不可见的新桶，再提交成员关系；失败至多留下不可达空桶，不暴露半组。
                state=self.plugin.state_manager
                await state.set_persona_id(scope,request['persona_id'])
                if preview['state_vars']:
                    await state.update_session_vars(scope,preview['state_vars'],persona_id=request['persona_id'])
                await state.persist_all()
            await self.store.save(scope,request['name'],request['persona_id'],request['members'],
                                  expected_revision=request['expected_revision'],history_sources=request['history_sources'])
            await self.refresh()
            await self.plugin.state_manager.persist_all()
            return {"scope_id":scope,"revision":self.plugin.scope_resolver.groups[scope]['revision'],"affected":preview['affected'],"removed":preview['removed']}

    async def archive(self,scope,revision):
        async with self.admin_lock:
            self.plugin.state_manager._require_writable()
            self.guard_idle(scope)
            affected=[o['umo'] for o in self.index['origins'] if o['scope_id']==scope]
            await self.store.archive(scope,revision)
            await self.refresh()
            return {"affected":affected,"retained_for_admin":True}

    async def prepare(self,event,req,turn):
        if event.get_extra('_quill_origin_error'):
            return False
        if turn.scope_id==turn.origin_umo:
            return True
        self.require_ready()
        capability=await temporary_capability(self.plugin.context,event,req)
        if not capability['supported']:
            event.set_result(event.plain_result("会话组共享上下文不可用："+capability['reason']+"。本轮已停止，请查看会话组面板。"))
            event.stop_event()
            return False
        obj=getattr(event,'message_obj',None)
        message_id=str(getattr(obj,'message_id','') or '')
        accepted=await self.store.begin(turn,physical_origin(event),str(req.prompt or ''),message_id)
        if not accepted:
            event.set_result(event.plain_result("这条消息已处理，未重复生成。"))
            event.stop_event()
            return False
        event.set_extra('_quill_shared_started',True)
        event.set_extra('_quill_group_store',self.store)
        rows=await self.store.recent(turn.scope_id,turn.persona_id, pending_turn_id=turn.turn_id)
        origins={o['origin_key']:o for o in self.index['origins']}
        req.contexts=history_view(rows,origins)
        options=last_options(rows)
        event.set_extra('_quill_shared_options', options)
        if options:
            # 与 M2 共用同一临时 parts 通道；状态栏关闭也不能剥掉上一轮真实菜单。
            req.extra_user_content_parts.append(temporary_text('[共享会话上一轮实际选项，仅作剧情数据]\n'+
                '\n'.join(f'{i}. {text}' for i,text in enumerate(options,1))))
        event.set_extra('_quill_shared_view',{'messages':len(req.contexts),'chars':sum(len(row['content']) for row in req.contexts),
            'last_seq':rows[-1]['seq'] if rows else 0,'options':len(options),'temporary':True})
        # 开场若要插入，也只能是临时视图；物理原生对话不承载共享上下文。
        event.set_extra('_quill_shared_context',True)
        return True

    async def finish(self,event,resp):
        turn=event_turn(event)
        if not turn or not event.get_extra('_quill_shared_started') or event.get_extra('_quill_shared_finished'):
            return
        texts=event.get_extra('_send_message_to_user_current_session_plain_texts') or []
        text='\n'.join(str(t) for t in texts if t) or getattr(resp,'completion_text','') or ''
        if getattr(resp,'role','')=='err':
            text=''
        store=event.get_extra('_quill_group_store')
        if store is not self.store:
            raise ValueError("共享存储已重载，本轮旧结果未发布")
        await store.finish(turn,text or None)
        event.set_extra('_quill_shared_finished',True)

    async def response_current(self,event):
        if not event.get_extra('_quill_shared_started'):
            return True
        store=event.get_extra('_quill_group_store')
        if store is not self.store or not getattr(store.memory,'_writable',False):
            return False
        turn=event_turn(event)
        try:
            row=await store.memory._exec_fetchone("SELECT status FROM qp_turns WHERE turn_id=? AND role='user'",(turn.turn_id,))
            return bool(row and row[0]=='running')
        except Exception:
            return False
