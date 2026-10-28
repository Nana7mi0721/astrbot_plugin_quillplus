# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 轻量任务注册：活动任务拒重，取消只在调用者声明的安全点生效。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

from ..core.errors import QuillError


class TaskFailure(QuillError):
    def __init__(self,message,code="task_failed"):
        super().__init__(message)
        self.code=code


class TaskCancelling(QuillError):
    status_code = 409
    code = 'task_cancelling'


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    key: str
    status: str = "queued"
    progress: int = 0
    total: int | None = None
    message: str = "等待执行"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self):
        return {key: value for key, value in vars(self).items() if key not in ("task", "cancel_event")}


class TaskHandle:
    def __init__(self, record):
        self.record = record

    async def checkpoint(self, progress=None, *, total=None, message=None):
        if progress is not None:
            self.record.progress = max(0, int(progress))
        if total is not None:
            self.record.total = max(0, int(total))
        if message is not None:
            self.record.message = str(message)[:200]
        if self.record.cancel_event.is_set():
            raise asyncio.CancelledError()
        await asyncio.sleep(0)  # 让取消/查询请求在批次之间获得调度。
        if self.record.cancel_event.is_set():
            raise asyncio.CancelledError()


class TaskRegistry:
    def __init__(self, retention_seconds=600, max_finished=100, max_running=100):
        self.records = {}
        self.active = {}
        self.retention_seconds = retention_seconds
        self.max_finished = max_finished
        self.max_running = max_running

    def _prune(self):
        finished = sorted((r for r in self.records.values() if r.finished_at is not None),
                          key=lambda r: r.finished_at, reverse=True)
        now = time.time()
        for index, record in enumerate(finished):
            if index >= self.max_finished or now - record.finished_at > self.retention_seconds:
                self.records.pop(record.task_id, None)

    def submit(self, kind, key, factory, *, total=None):
        self._prune()
        identity = (str(kind), str(key))
        existing = self.active.get(identity)
        if existing:
            if self.records[existing].status == "cancelling":
                raise TaskCancelling("上一个任务正在取消，请等待结束后重试")
            return self.records[existing], True
        if len(self.active) >= self.max_running:
            raise ValueError("后台任务数量已达上限，请稍后重试")
        record = TaskRecord(uuid4().hex, identity[0], identity[1], total=total)
        self.records[record.task_id] = record
        self.active[identity] = record.task_id

        async def run():
            handle = TaskHandle(record)
            try:
                await handle.checkpoint()
                record.status, record.started_at, record.message = "running", time.time(), "执行中"
                result = await factory(handle)
                # 已提交的最后一步不因来迟的取消请求被错误标成回滚。
                record.status, record.message = "completed", "已完成"
                reason=(record.result or {}).get("reason")
                if reason == "insufficient_logs":
                    record.message="新对话不足，未调用摘要模型"
                elif reason in ("empty","duplicate"):
                    record.message="已处理，无需新增记忆"
                return result
            except asyncio.CancelledError:
                record.status, record.message = "cancelled", "已取消，已提交步骤保留"
                raise
            except Exception as exc:
                # 取消请求不能掩盖刚发生的存储/模型故障，部分提交结果仍留在记录中。
                record.status, record.error, record.message = "failed", type(exc).__name__, "任务失败"
                if isinstance(exc,QuillError):
                    record.error,record.message=exc.code,exc.message[:200]
                raise
            finally:
                record.finished_at = time.time()
                if self.active.get(identity) == record.task_id:
                    self.active.pop(identity, None)
                self._prune()

        record.task = asyncio.create_task(run())
        return record, False

    def get(self, task_id):
        self._prune()
        return self.records.get(task_id)

    def cancel(self, task_id):
        record = self.get(task_id)
        if record is None:
            return None
        if record.finished_at is None:
            record.cancel_event.set()
            record.status, record.message = "cancelling", "等待当前步骤结束后取消"
        return record.to_dict()

    def list(self):
        self._prune()
        return [r.to_dict() for r in sorted(self.records.values(), key=lambda r: r.created_at, reverse=True)]
