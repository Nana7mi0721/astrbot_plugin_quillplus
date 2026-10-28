# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 事件身份适配：在第一处请求入口固定，之后不读新的当前角色。"""

from ..quill.services.scope import PhysicalOrigin, TurnContext


def physical_origin(event) -> PhysicalOrigin:
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if not umo:
        raise ValueError("缺少会话来源，不能以发送者身份代替会话")
    platform_id = str(event.get_platform_id() or "") if hasattr(event, "get_platform_id") else ""
    message_type = event.get_message_type() if hasattr(event, "get_message_type") else ""
    message_type = str(getattr(message_type, "value", message_type) or "")
    return PhysicalOrigin(umo, platform_id, message_type, str(event.get_sender_id() or ""))


def event_turn(event) -> TurnContext | None:
    value = event.get_extra("_quill_turn_context")
    return value if isinstance(value, TurnContext) else None


async def freeze_turn(plugin, event, *, recover=True) -> TurnContext:
    existing = event_turn(event)
    if existing is not None:
        return existing
    origin = physical_origin(event)
    provisional = plugin.scope_resolver.resolve(origin)
    scope_id=provisional.scope_id
    from .operations import recover_operations
    try:
        if recover:
            await recover_operations(plugin,scope_id=scope_id)
    except Exception:
        event.set_extra('_quill_recovery_blocked', True)
        event.set_result(event.plain_result('此会话有未完成的清理或重置，本轮已停止。请查看任务页的存储状态后重试。'))
        event.stop_event()
        raise
    persona_id = await plugin.state_manager.get_selected_persona_id(origin.umo)
    turn = plugin.scope_resolver.resolve(origin, persona_id)
    member=plugin.scope_resolver.memberships.get(origin.umo)
    if member and member['origin_key']==origin.key:
        event.set_extra('_quill_member_epoch',member['epoch'])
    event.set_extra("_quill_turn_context", turn)
    return turn
