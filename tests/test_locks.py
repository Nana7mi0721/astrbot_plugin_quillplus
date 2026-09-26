# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""可重入异步锁回归测试（v5.3.0 M3.3 D5，对应 VERIFICATION.md 缺陷 1 的同类形态）。

参考重构版的死锁回归模式：可能挂起的业务场景用
``asyncio.wait_for(timeout=5.0)`` 守卫——若把 ReentrantLock 退回普通
``asyncio.Lock``，同任务重入的用例会以 TimeoutError 失败（自死锁），
而不是把测试进程挂死。

**守卫的写法有讲究（实测教训）**：``asyncio.wait_for(coro, t)`` 会把 coro
包成**独立任务**再执行。若单独包 ``lock.acquire()``，锁的持有者会变成那个
转瞬即逝的内层任务（任务级重入语义下，"谁的任务 acquire 谁持有"），外层
测试任务的重入判定随之失效——这是测试写法的坑，不是锁的缺陷（生产代码里
``async with self._lock`` 的获取与使用始终在同一任务内）。因此本文件的
守卫一律包**完整场景**，保证场景内的 acquire/release 在同一任务内；
"另一任务"用例则显式把违规操作放进独立任务。
"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_quillplus.quill.core.locks import ReentrantLock


async def test_same_task_nested_acquire_does_not_block():
    """同任务重入：只加深度、不阻塞；深度归零才真正放锁。

    普通 asyncio.Lock 在第二个 acquire 处永久挂起（同任务自死锁）→
    本用例以 TimeoutError 失败。
    """

    async def _scenario():
        lock = ReentrantLock()
        await lock.acquire()
        await lock.acquire()        # 重入：不阻塞
        await lock.acquire()        # 三层重入
        assert lock.locked
        lock.release()
        lock.release()
        assert lock.locked          # 深度 1，仍持有
        lock.release()
        assert not lock.locked      # 深度 0，真正释放

    await asyncio.wait_for(_scenario(), 5.0)


async def test_release_by_other_task_raises():
    """非持有者任务释放 → RuntimeError，且原持有不受影响。

    "另一任务"必须用 ``ensure_future`` 显式创建：``wait_for(coro, t)`` 在
    Python 3.10 会把 coro 包成新任务、3.12 则在当前任务内直接 await——
    显式建任务才能保证跨版本的任务身份（见模块 docstring）。
    """

    async def _scenario():
        lock = ReentrantLock()
        await lock.acquire()

        result: dict = {}

        async def _wrong_release():
            try:
                lock.release()
            except RuntimeError as e:
                result["err"] = e

        task = asyncio.ensure_future(_wrong_release())
        done, _pending = await asyncio.wait({task}, timeout=5.0)
        assert task in done, "违规释放任务未按时完成"
        assert isinstance(result.get("err"), RuntimeError), "非持有者释放未报错"
        # 锁仍被本任务持有，可正常释放
        assert lock.locked
        lock.release()
        assert not lock.locked

    await asyncio.wait_for(_scenario(), 5.0)


async def test_release_without_acquire_raises():
    lock = ReentrantLock()
    with pytest.raises(RuntimeError):
        lock.release()


async def test_cross_task_queuing_serializes():
    """跨任务串行化语义与 asyncio.Lock 一致：同一任务在临界区内不交错。"""
    lock = ReentrantLock()
    order: list[str] = []

    async def _worker(name: str, delay: float):
        async with lock:
            order.append(f"{name}:in")
            await asyncio.sleep(delay)
            order.append(f"{name}:out")

    await asyncio.wait_for(
        asyncio.gather(_worker("A", 0.05), _worker("B", 0.0)), 5.0
    )
    # 串行化：每个任务的 in/out 相邻（另一个任务必须等它退出临界区）
    assert order.index("A:out") - order.index("A:in") == 1
    assert order.index("B:out") - order.index("B:in") == 1


async def test_nested_business_call_completes_under_timeout_guard():
    """存储层形态复刻：外层持锁方法调用内层会再次取锁的方法。

    改用普通 asyncio.Lock 时，wait_for 会在 5 秒后以 TimeoutError 失败
    （同任务自死锁）；ReentrantLock 下立即完成。
    """
    lock = ReentrantLock()
    calls: list[str] = []

    class _Store:
        async def _exec(self, tag: str):
            async with lock:
                calls.append(tag)

        async def outer(self):
            async with lock:
                calls.append("outer")
                await self._exec("inner")   # 旧锁在此自死锁

    await asyncio.wait_for(_Store().outer(), 5.0)
    assert calls == ["outer", "inner"]


async def test_async_with_form_reentry():
    """``async with`` 形态的重入与退出（存储层三处均为该形态）。"""
    async def _scenario():
        lock = ReentrantLock()
        async with lock:
            async with lock:
                assert lock.locked
            assert lock.locked          # 内层退出只减深度
        assert not lock.locked

    await asyncio.wait_for(_scenario(), 5.0)


async def test_memory_store_helper_callable_while_holding_lock(tmp_path):
    """真件复刻：MemoryStore 持锁期间调用 _exec_* 辅助方法（改前被注释纪律
    禁止、误犯即永久挂起的模式），现在安全。

    整条流程包进同一个任务再交给 wait_for 守卫（见模块 docstring：
    不能对 store 方法单独 wait_for，否则任务身份错位、重入判定失效）。
    """
    from astrbot_plugin_quillplus.quill_rag.memory_store import MemoryStore

    store = MemoryStore(str(tmp_path / "m.db"))

    async def _scenario():
        await store.initialize()
        await store.add("s1", "锚点记忆", [0.1] * 4)
        async with store._lock:  # 持锁
            rows = await store.list_memories("s1")  # 内部再取锁（改前禁止的形态）
        assert rows and rows[0]["summary"] == "锚点记忆"

    await asyncio.wait_for(_scenario(), 5.0)
    await store.close()
