# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""存储层失败计数（v5.3.0 M3.2 D4 配套）。

背景
----
D4 整改前，存储层高频路径失败大多 ``logger.warning`` + 返回空，面板完全
不可观测（只有 kb 的 FTS 降级经 ``get_index_status`` 暴露过）。M3.2 把
六类高频路径（add / search / prune / delete / backup / restore）的底层
失败改为抛 :class:`quill.core.errors.StorageError`，本模块在**每个抛出点**
按类别计数，进程内聚合，经 ``/info`` 端点的 ``storage_errors`` 字段暴露。

响应体结构（只增不改，向后兼容）::

    "storage_errors": {
        "add": 0,       # 写入失败：MemoryStore.add/log_message、FaissVectorStore.add、WritingResourceManager.add_entry
        "search": 0,    # 检索失败：MemoryStore.search/search_all、FaissVectorStore.search、WR.match 兜底扫描
        "prune": 0,     # 清理失败：MemoryStore.prune_memories/cleanup_chat_logs
        "delete": 0,    # 删除失败：MemoryStore.delete_*、FaissVectorStore.delete_by_source、WritingResourceManager.delete_entry
        "backup": 0,    # 备份导出失败：web_routes._build_backup_zip
        "restore": 0,   # 恢复失败：web_routes._do_restore_bytes（组件关闭/解压/重建）
    }

线程安全：``threading.Lock`` 保护的自增（计数可能从 ``asyncio.to_thread``
里的同步段触发，不能只靠事件循环串行性）。进程内即可——重启清零，
跨进程聚合无需求。

零 astrbot import（架构守卫）；也不 import logging——计数点已在 except
块里各自记日志，这里只管数字。
"""

from __future__ import annotations

import threading

#: 六类高频路径（顺序即 /info 响应体里的展示顺序）。
CATEGORIES: tuple[str, ...] = ("add", "search", "prune", "delete", "backup", "restore")

_LOCK = threading.Lock()
_COUNTS: dict[str, int] = {cat: 0 for cat in CATEGORIES}


def note_storage_error(category: str, exc: BaseException | None = None) -> int:
    """记一次存储失败，返回该类别当前累计值。

    Args:
        category: :data:`CATEGORIES` 之一；未知类别抛 ValueError（写错
            类别名属于代码缺陷，必须在测试期暴露，而不是悄悄进错桶）。
        exc: 触发计数的原始异常。本模块**不**记日志（调用点的 except 块
            已带方法名与异常链记过），仅保留参数位以便未来扩展。
    """
    if category not in _COUNTS:
        raise ValueError(
            f"未知存储失败类别: {category!r}（合法类别: {', '.join(CATEGORIES)}）"
        )
    with _LOCK:
        _COUNTS[category] += 1
        return _COUNTS[category]


def storage_error_snapshot() -> dict[str, int]:
    """当前计数的浅拷贝（供 /info 响应体直接使用）。"""
    with _LOCK:
        return dict(_COUNTS)


def reset_storage_error_counts() -> None:
    """清零全部计数（仅测试用；生产进程不重置——计数随进程生命周期）。"""
    with _LOCK:
        for cat in _COUNTS:
            _COUNTS[cat] = 0


__all__ = ["CATEGORIES", "note_storage_error", "storage_error_snapshot", "reset_storage_error_counts"]
