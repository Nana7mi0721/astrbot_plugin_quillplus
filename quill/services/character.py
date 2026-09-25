# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""角色卡 → AstrBot 对话隔离（v5.3.0 M2.2 自 main.py 原位下沉）。

PLAN §2.1 目标结构中的 persona_manager 演进位：角色卡域的服务层落点。
本轮只承载 ``ensure_persona_conversation``（原
``QuillPlugin._ensure_persona_conversation`` 逐字搬入）；后续角色卡域
逻辑（如 M3.0 F2「换卡重置 quill_rounds」将挂在这里的切换点）继续归入
本模块。

依赖注入约定：原方法体引用 ``self.context``（取 conversation_manager）、
``self.state_manager``、``self._get_target_id`` 三处宿主状态——下沉时
改为**显式参数**传入，方法体其余部分一字未改。日志经 quill/core/
logbridge.py 的代理 ``logger``（宿主 main.py 注入的同一个 astrbot
logger），写法与原 main.py 一致；本模块零 astrbot import（架构守卫）。
"""

from __future__ import annotations

from ..core.logbridge import logger


async def ensure_persona_conversation(
    context,
    state_manager,
    event,
    resolve_target_id,
) -> None:
    """把当前角色卡切到它自己的 AstrBot 对话上，实现对话历史隔离。

    （原 ``QuillPlugin._ensure_persona_conversation`` 方法体逐字搬移，
    M2.2 第二轮；``self.context`` → ``context``、``self.state_manager`` →
    ``state_manager``、``self._get_target_id`` → ``resolve_target_id``。）

    背景：AstrBot 的 conversation 只按 UMO 切分，**不按角色卡切分**。
    插件的 memories / chat_logs 早已按 `UMO::persona` 隔离，唯独
    AstrBot 侧那段对话历史（即 `req.contexts`）没有隔离，导致切换角色卡后
    新角色仍能读到上一个角色的对话。

    这里给每张角色卡绑定一个独立 conversation：
      * 切到某张卡 → 切到它上次用的对话（切回来仍能看到那段历史）；
      * 该卡首次使用 → 新建一个空对话。

    时序上必须挂在 `on_waiting_llm_request`：该事件在 AstrBot
    `_get_session_conv()` **之前**触发（internal.py:225 vs 239），
    因此这里的切换**对本轮立即生效**；若放到 on_llm_request 则要下一轮才生效。

    一切异常都只记日志并放行：拿不到 conversation_manager 或接口变动时，
    插件退回「不分对话」的原有行为，绝不让隔离逻辑打断正常聊天。
    """
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None:
        return
    try:
        umo = resolve_target_id(event)
        persona_id = await state_manager.get_persona_id(umo)
        key = persona_id or ""  # 未绑卡时归入空串一档，同样独立
        mapping = await state_manager.get_persona_conv_map(umo)
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        target_cid = mapping.get(key)

        if target_cid and target_cid == curr_cid:
            return  # 快路径：已在正确对话，零额外查询

        if target_cid:
            # 对话可能已被 Dashboard 删除；switch 不校验存在性（conversation_mgr.py:126），
            # 不校验会导致每轮都切到一个不存在的 cid 而不断新建/泄漏。
            try:
                conv = await conv_mgr.get_conversation(umo, target_cid)
            except Exception:
                conv = None
            if conv is None:
                logger.info(
                    f"[Quill] 角色卡 {key or '(未绑定)'} 的原对话 {target_cid[:8]} 已不存在，将重建"
                )
                await state_manager.forget_persona_conv(umo, key)
                target_cid = None

        if target_cid is None:
            if not mapping and curr_cid:
                # 首次启用隔离：当前角色卡接管现有对话，历史不断
                target_cid = curr_cid
                logger.info(
                    f"[Quill] 角色卡 {key or '(未绑定)'} 接管当前对话 "
                    f"{curr_cid[:8]}（首次启用对话隔离）"
                )
            else:
                target_cid = await conv_mgr.new_conversation(
                    umo, event.get_platform_id()
                )
                logger.info(
                    f"[Quill] 已为角色卡 {key or '(未绑定)'} 新建独立对话 "
                    f"{str(target_cid)[:8]}（对话历史将相互隔离）"
                )
            await state_manager.set_persona_conv(umo, key, target_cid)

        if curr_cid != target_cid:
            await conv_mgr.switch_conversation(umo, target_cid)
            logger.info(
                f"[Quill] 对话已切换 → {str(target_cid)[:8]} "
                f"(角色卡: {key or '(未绑定)'})"
            )
    except Exception as e:
        logger.warning(f"[Quill] 角色卡对话隔离失败，本轮沿用当前对话: {e}")
