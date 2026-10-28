# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""记忆域调度：角色桶计数触发任务，SQLite 水位决定实际消费范围。

M3 复用插件注入的总结入口与后台注册表，冻结归属，成功后扣除已捕获计数。
不导入 AstrBot；调用者负责注入状态管理、检索器与任务入口。
"""

from __future__ import annotations

from ..core.logbridge import logger

# S3-13: 反思/总结相关阈值常量（原 QuillPlugin 类属性随迁；main.py 保留
# 同名 re-export 维持旧引用路径，M2.0 搬移期约定）
REFLECTION_TURN_THRESHOLD = 4       # 攒够 N 轮触发一次反思摘要
RECENT_LOG_LIMIT = 8                # 反思时读取的最近日志条数
MIN_LOGS_FOR_SUMMARY = 2            # 触发总结所需的最少日志条数


async def schedule_reflection(
    state_manager, retriever, spawn, config, target_id, mem_session_id, start_summary=None
) -> None:
    """计数仅触发调度；成功事实由 SQLite 水位保存，失败不重置提示计数。"""
    try:
        unsummarized = await state_manager.increment_unsummarized_turns(target_id)
        if unsummarized < REFLECTION_TURN_THRESHOLD:
            return
        from .scope import current_turn
        turn = current_turn()
        pid = turn.persona_id if turn else await state_manager.get_persona_id(target_id)
        cursor = await state_manager.get_last_learned_id(target_id,persona_id=pid)
        if start_summary:
            record, reused = start_summary(mem_session_id,legacy_last_id=cursor)
            if not reused:
                async def settle():
                    result = await record.task
                    if result and result.get("processed"):
                        await state_manager.consume_unsummarized_turns(target_id,unsummarized,persona_id=pid)
                spawn(settle())
        # 清理任务有独立 key，避免并发用户轮把同一维护任务开多份。
        spawn(retriever.memory_store.prune_memories(),kind="memory_prune",key="global")
        spawn(retriever.memory_store.cleanup_chat_logs(
            getattr(config,'rag_chat_log_retention_days',30),
            summary_enabled=getattr(config,'rag_enable_memory',False),
            reflection_enabled=getattr(config,'rag_enable_autonomous_reflection',True)),kind="chat_log_retention",key="global")
    except Exception as exc:
        logger.warning("[Quill Memory] 反思调度失败（%s）",type(exc).__name__)
