# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillPlus 统一异常体系（v5.3.0 M3.2，自重构参考版移植）。

设计要点
--------
1. **单一基类**：所有插件受控异常都继承 :class:`QuillError`，调用方可用一个
   ``except QuillError`` 兜住全部已知失败，同时仍能按类型细分处理。
2. **用户可见 / 内部可见分离**：``message`` 是要给终端用户看的中文短句，
   ``detail`` 是只进日志（或错误信封 detail 位）的内部上下文。旧版本把
   ``f"{prefix}: {exc}"`` 直接回给前端，导致 OSError 泄漏绝对路径、
   sqlite3 泄漏表名列名；从类型层面把「想给用户看什么」变成显式参数。
3. **携带上下文**：``context`` 是自由字典，用于记录 ``session_id``、
   ``entry_id`` 等定位信息，随异常进日志。
4. **异常链**：底层 except 块抛 ``StorageError(...) from exc``，原始异常
   永远可追溯（D4 整改的统一写法，见 quill_rag 各存储模块）。

本模块零 astrbot import（架构守卫 tests/arch/test_layering.py）。
"""

from __future__ import annotations

from typing import Any


class QuillError(Exception):
    """QuillPlus 全部受控异常的基类。

    Attributes:
        message: 可直接展示给终端用户的中文说明。
        detail: 仅写日志/信封 detail 位的内部细节（异常原文、路径、SQL 等）。
        context: 定位问题用的键值对（session_id、entry_id 等）。
        status_code: 对应的 HTTP 状态码，供 Web 接口层直接使用。
    """

    status_code: int = 500
    #: 供前端做程序化分支的错误代码（稳定，不随文案变化）。
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.context = context or {}

    def __str__(self) -> str:
        # detail 拼进 str()：面板既有 error_text(prefix, e) 信封不需要改动
        # 就能同时带上中文摘要与原始异常文本（原始信息是 detail 的超集）。
        if self.detail:
            return f"{self.message} | {self.detail}"
        return self.message

    def to_payload(self) -> dict[str, Any]:
        """转成下发给前端的错误信封（不含 ``detail``，防内部信息泄漏）。"""
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
        }


# ── 输入 / 校验 ──────────────────────────────────────────────


class ValidationError(QuillError):
    """请求参数不合法。"""

    status_code = 400
    code = "validation_error"


class NotFoundError(QuillError):
    """请求的资源不存在。"""

    status_code = 404
    code = "not_found"


class ConflictError(QuillError):
    """资源已存在或状态冲突。"""

    status_code = 409
    code = "conflict"


class PermissionDeniedError(QuillError):
    """调用方没有执行该操作的权限。"""

    status_code = 403
    code = "permission_denied"


# ── 依赖 / 环境 ──────────────────────────────────────────────


class DependencyMissingError(QuillError):
    """可选依赖未安装，对应能力不可用（如 faiss 未安装）。"""

    status_code = 503
    code = "dependency_missing"


class StorageError(QuillError):
    """数据层失败（SQLite、FAISS、文件系统、序列化）。

    M3.2 D4 约定：存储层高频路径（add/search/prune/delete/backup/restore）
    的底层失败抛本异常（``from exc`` 保留异常链），**由调用方决定降级**——
    聊天链路放行/返回空、面板链路回错误信封。每次抛出前经
    :mod:`quill.core.storage_stats` 计数（/info 端点 ``storage_errors`` 字段）。
    """

    status_code = 500
    code = "storage_error"


class MigrationError(QuillError):
    """旧数据迁移失败。原始数据必须原样保留。"""

    status_code = 500
    code = "migration_error"


# ── 外部服务 ────────────────────────────────────────────────


class ProviderError(QuillError):
    """外部 LLM / Embedding / Rerank 提供方调用失败。"""

    status_code = 502
    code = "provider_error"


class UnsupportedFormatError(ValidationError):
    """文件或数据格式不被支持（校验失败的一类，状态码随 ValidationError）。"""

    code = "unsupported_format"


__all__ = [
    "ConflictError",
    "DependencyMissingError",
    "MigrationError",
    "NotFoundError",
    "PermissionDeniedError",
    "ProviderError",
    "QuillError",
    "StorageError",
    "UnsupportedFormatError",
    "ValidationError",
]
