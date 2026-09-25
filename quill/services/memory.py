# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""记忆域服务——多轮反思调度（v5.3.0 M2.2 自 main.py H5 原位下沉）。

PLAN §2.1 目标结构中的 memory.py 位（quill_rag/memory_store 的服务层演进
落点）。本轮承载 ``schedule_reflection``（原 H5 ``on_llm_tool_respond``
的"N 轮反思触发"内层块逐字搬入，M2.2 第五轮，消费方为
interfaces/astrbot_hooks.py 的 handle_llm_tool_respond）；
quill_rag/memory_store.py 存储层本身按 PLAN 后续里程碑再演进归入本模块。

依赖注入约定：原内层块引用 ``self.state_manager``、``self.rag_retriever``、
``self._spawn``、``self.config`` 与三个阈值常量（原 QuillPlugin 类属性
``REFLECTION_TURN_THRESHOLD`` 等）——下沉时改为**显式参数** + 模块常量，
块内其余部分一字未改：内层 try/except 的 warning 吞掉语义、阈值判断与
摘要/修剪/清理的调用顺序逐字保真（行为快照钉住，tests/test_hook_snapshots.py
H5 节）。日志经 quill/core/logbridge.py 的代理 ``logger``（宿主 main.py
注入的同一个 astrbot logger）；本模块零 astrbot import（架构守卫）。
"""

from __future__ import annotations

from ..core.logbridge import logger

# S3-13: 反思/总结相关阈值常量（原 QuillPlugin 类属性随迁；main.py 保留
# 同名 re-export 维持旧引用路径，M2.0 搬移期约定）
REFLECTION_TURN_THRESHOLD = 4       # 攒够 N 轮触发一次反思摘要
RECENT_LOG_LIMIT = 8                # 反思时读取的最近日志条数
MIN_LOGS_FOR_SUMMARY = 2            # 触发总结所需的最少日志条数


async def schedule_reflection(
    state_manager, retriever, spawn, config, target_id, mem_session_id
) -> None:
    """N 轮反思触发：攒够 N 轮对话后生成摘要。

    （原 main.py H5 ``on_llm_tool_respond`` 内层块逐字搬移，M2.2 第五轮；
    ``self.state_manager`` → ``state_manager``、``self.rag_retriever`` →
    ``retriever``、``self._spawn`` → ``spawn``、``self.config`` →
    ``config``、``self.REFLECTION_*`` → 模块常量。）

    降级语义（逐字保真，不得"顺手"上提或收紧）：块内任何异常 warning
    吞掉（「反思调度失败」）——不上抛框架，落日志等记忆存储主流程也不
    因反思失败中断。

    调用顺序（顺序即行为，不得重排）：
    increment → 未达阈值只打 debug 进度日志；达阈值（>=4）则 reset 轮次
    → 读最近 RECENT_LOG_LIMIT(8) 条日志 → 够 MIN_LOGS_FOR_SUMMARY(2) 条
    才 spawn 摘要（带异常记日志的 done 回调，S1-1 修复：保留 task 引用
    防 GC）→ 无条件 spawn ``prune_memories``（分档遗忘）与
    ``cleanup_chat_logs``（保留天数 ``getattr(config,
    'rag_chat_log_retention_days', 30)``）。
    """
    try:
        unsummarized = await state_manager.increment_unsummarized_turns(target_id)

        if unsummarized >= REFLECTION_TURN_THRESHOLD:
            await state_manager.reset_unsummarized_turns(target_id)
            recent_logs = await retriever.memory_store.get_recent_chat_logs(mem_session_id, limit=RECENT_LOG_LIMIT)

            if len(recent_logs) >= MIN_LOGS_FOR_SUMMARY:
                # S1-1 修复：改用 _spawn 保留 task 引用，防止 GC 中断
                sum_task = spawn(
                    retriever.summarize_contexts(mem_session_id, contexts=recent_logs)
                )
                def _log_summary_result(t):
                    exp = t.exception()
                    if exp:
                        logger.warning(f"[Quill Memory] 多轮总结异常: {exp}")
                sum_task.add_done_callback(_log_summary_result)

            # 顺带跑一次记忆修剪（分档遗忘）
            if retriever.memory_store:
                spawn(retriever.memory_store.prune_memories())
                # 清理过期对话日志（无人值守，避免长期运行服务器日志膨胀）
                spawn(
                    retriever.memory_store.cleanup_chat_logs(
                        getattr(config, 'rag_chat_log_retention_days', 30)
                    )
                )
        else:
            logger.debug(f"[Quill Memory] 记忆收集进度: {unsummarized}/4 轮")
    except Exception as e:
        logger.warning(f"[Quill Memory] 反思调度失败: {e}")
