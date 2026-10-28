# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 身份契约：默认沿用旧 UMO，不推断合组或跨平台同一人。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from uuid import uuid4


@dataclass(frozen=True)
class PhysicalOrigin:
    umo: str
    platform_instance_id: str = ""
    message_type: str = ""
    sender_id: str = ""

    @property
    def key(self):
        return hashlib.sha256(json.dumps([self.umo,self.platform_instance_id,self.message_type],ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class TurnContext:
    origin_umo: str
    scope_id: str
    persona_id: str
    scope_revision: int
    turn_id: str
    actor_id: str | None = None
    platform_instance_id: str = ""
    message_type: str = ""

    @property
    def memory_session_id(self) -> str:
        return memory_session_key(self.scope_id, self.persona_id)

    def to_dict(self) -> dict:
        return asdict(self)


def memory_session_key(scope_id: str, persona_id: str = "") -> str:
    """旧键只在此编码；M3 不搬移旧行，scope 与显示名无关。"""
    return f"{scope_id}::{persona_id}" if persona_id else str(scope_id)


def memory_session_prefix(scope_id: str) -> str:
    return str(scope_id) + "::"


class ScopeResolver:
    def __init__(self):
        self.memberships = {}
        self.groups = {}
        self.actor_links = {}

    def scope_for(self, umo):
        return self.memberships.get(str(umo),{}).get("scope_id",str(umo))

    def selected_persona(self, scope_id):
        group=self.groups.get(scope_id)
        return group["persona_id"] if group else None

    def resolve(self, origin: PhysicalOrigin, persona_id: str = "", *,
                turn_id: str | None = None, scope_id: str | None = None,
                scope_revision: int = 0) -> TurnContext:
        # 相同数字 ID/昵称不构成跨平台关联；无 sender 时不制造一个“真实参与者”。
        actor = None
        if origin.platform_instance_id and origin.sender_id:
            key = json.dumps([origin.platform_instance_id, origin.sender_id], ensure_ascii=False)
            actor = "actor:" + hashlib.sha256(key.encode()).hexdigest()
        member=self.memberships.get(origin.umo)
        # 只有物理来源三元组完全吻合才使用成员关系，UMO 文本相同不构成别名。
        if member and member["origin_key"] != origin.key:
            member=None
        resolved=scope_id if scope_id is not None else member["scope_id"] if member else origin.umo
        group=self.groups.get(resolved)
        if group:
            persona_id=group["persona_id"]
            scope_revision=group["revision"]
        actor=self.actor_links.get(actor,actor)
        return TurnContext(
            origin_umo=origin.umo, scope_id=str(resolved),
            persona_id=str(persona_id or ""), scope_revision=max(0, int(scope_revision)),
            turn_id=turn_id or uuid4().hex, actor_id=actor,
            platform_instance_id=origin.platform_instance_id, message_type=origin.message_type,
        )

    def for_target(self, umo: str, persona_id: str = "") -> TurnContext:
        # 存储管理入口没有事件来源，不伪造 platform_instance_id 或参与者。
        if str(umo) in self.groups:
            return TurnContext("",str(umo),str(persona_id or ""),self.groups[str(umo)]["revision"],uuid4().hex)
        return TurnContext(str(umo),str(umo),str(persona_id or ""),0,uuid4().hex)

    def from_session_key(self, session_id: str, known_origins=()) -> TurnContext:
        # 优先匹配已知物理入口，避免无卡 UMO 自含 ::（如 IPv6）时误拆角色。
        value = str(session_id)
        for origin in sorted(set(known_origins), key=len, reverse=True):
            if value == origin:
                return self.for_target(origin)
            prefix = memory_session_prefix(origin)
            if value.startswith(prefix):
                return self.for_target(origin, value[len(prefix):])
        target, sep, persona = value.rpartition("::")
        return self.for_target(target, persona) if sep else self.for_target(value)


_current_turn: ContextVar[TurnContext | None] = ContextVar("quill_turn", default=None)


def current_turn() -> TurnContext | None:
    return _current_turn.get()


@contextmanager
def bind_turn(turn: TurnContext | None):
    token = _current_turn.set(turn)
    try:
        yield turn
    finally:
        _current_turn.reset(token)
