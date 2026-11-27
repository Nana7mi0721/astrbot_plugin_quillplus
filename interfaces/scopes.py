# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 事件身份适配：在第一处请求入口固定，之后不读新的当前角色。"""

from dataclasses import replace

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


async def freeze_turn(plugin, event, *, recover=True, refresh=False) -> TurnContext:
    """固定本轮身份；``refresh=True`` 用于共享排队后重新读取权威角色。

    参数事件在排队期间可能被别的入口换卡（共享会话组只允许一个真实轮），
    此时调用方必须在 acquire 之后重冻结。turn_id 保持不变，避免同一轮被
    规则时钟/日志侧当成两轮；persona_id 与 scope_revision 取最新值。
    """
    existing = event_turn(event)
    if existing is not None and not refresh:
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
    turn = plugin.scope_resolver.resolve(
        origin, persona_id,
        turn_id=existing.turn_id if existing is not None else None,
    )
    if existing is not None and turn.persona_id == existing.persona_id \
            and turn.scope_id == existing.scope_id \
            and turn.scope_revision == existing.scope_revision:
        turn = replace(existing)
    member=plugin.scope_resolver.memberships.get(origin.umo)
    if member and member['origin_key']==origin.key:
        event.set_extra('_quill_member_epoch',member['epoch'])
    event.set_extra("_quill_turn_context", turn)
    return turn
