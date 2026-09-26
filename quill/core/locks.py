# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""可重入异步锁（v5.3.0 M3.3 D5，自重构参考版 ``infrastructure/db/database.py``
的 ``_ReentrantLock`` 移植）。

为什么 ``asyncio.Lock`` 不够用
---------------------------
存储层的既有形态是"一把 ``asyncio.Lock`` 串行化 SQLite 共享连接 + 注释纪律"：
``_exec_write`` / ``_exec_fetchall`` 等辅助方法各自 ``async with self._lock``，
而 ``_init_db`` 这类需要跨多条语句保持原子性的方法在**持有锁的整块代码**里
直接使用 ``self._conn``——为什么不用辅助方法？因为 ``asyncio.Lock`` **不可
重入**：同任务二次获取会永久等待自己，即**同任务自死锁**。此前这件事只靠
"此处已在 _lock 内不能再调用 _exec_*"之类的注释维系（memory_store、
vector_store、kb 三处），任何一处注释失守（比如有人好心把直连改成复用辅助
方法）就是无报错的永久挂起。

这类死锁最难查：单线程小数据量下可能永远不触发，直到某个新代码路径恰好
在持锁块内多调了一层才突然挂住整个插件。

实现
----
记录持有者任务与重入深度。同一任务再次获取只加深度、不阻塞；其他任务正常
排队等底层锁。这正是 ``threading.RLock`` 的异步等价物。

引入本锁后，三处存储层的"注释纪律"整体解除：持锁块内调用会再取锁的辅助
方法是安全的。既有调用结构**保持不变**（不为"利用可重入"重构调用链），
只是消除了自死锁风险。

零 astrbot import（架构守卫 tests/arch/test_layering.py）。
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["ReentrantLock"]


class ReentrantLock:
    """按**任务**重入的异步锁。

    - 同一任务重复 ``acquire`` 只增加深度，不阻塞（重入）；
    - 其他任务照常排队等底层 :class:`asyncio.Lock`（串行化语义不变）；
    - 每次成功 ``acquire`` 必须对应一次 ``release``，深度归零才真正放锁；
    - 非持有者任务调用 :meth:`release` 抛 :class:`RuntimeError`。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            raise RuntimeError("释放了不属于当前任务的锁")
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "ReentrantLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()
