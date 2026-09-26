# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quill/ 包的日志桥（M2.1 引入；M4.4 去 logging 化）。

背景：架构守卫（tests/arch/test_layering.py）禁止 quill/ 包内 import
astrbot.*，而原 main.py 的方法体大量引用模块级 ``logger``（来自
``astrbot.api``）。搬移这些方法时若逐行改写日志调用，既违反"方法体
一字不改"的搬移纪律，也会制造大量无谓 diff。因此：

    main.py（组合根）在 ``QuillPlugin.__init__`` 最早处调用
    ``logbridge.set_logger(logger)`` 把宿主 logger 注入进来；
    quill/ 内部模块经 ``from ...core.logbridge import logger`` 拿到一个
    代理对象——属性读取时转发给当前桥接的 logger。方法体里
    ``logger.info(...)`` 的写法与原 main.py 完全一致，且任何时刻换绑
    都即时生效（代理每次属性访问都现取 get_logger()）。

未 set_logger 时的回退（M4.4 修订）：内置的**全 no-op 空 logger**，
一切日志调用静默吞掉。为什么是静默 no-op 而不是另起一条日志通道：

1. 生产路径 main.py 在 ``QuillPlugin.__init__`` 最早处就 set_logger，
   回退只在测试环境 / 异常加载顺序下被触达；
2. AstrBot 插件市场 LLM Guard 硬约束：logger 只能从 ``astrbot.api``
   导入，严禁 Python 内置 logging 模块（v5.2.5 前身即因内置 logging
   被拒审）。回退若 ``import logging`` 自建通道，等于在合规红线上
   开口子——M4.4 起本文件对 logging 模块**零 import**；
3. 回退场景（单测 / 非宿主加载）下日志本无去处，静默优于绕开
   astrbot.api 另起炉灶。

合规说明：本文件不 import 标准库 logging、不 import astrbot（架构守卫
只查 astrbot.*；上架合规约束由宿主 main.py 满足——quill/ 用的正是宿主
注入的那个 logger 对象）。
"""

from __future__ import annotations

from typing import Any

_LOGGER: Any = None


def set_logger(l: Any) -> None:
    """注入宿主（main.py）的 logger。应在插件 __init__ 最早处调用。"""
    global _LOGGER
    _LOGGER = l


def get_logger() -> Any:
    """取当前桥接的 logger；未注入时回退内置 no-op 空 logger。

    （为何静默 no-op 而非另起日志通道，见模块 docstring。）
    """
    if _LOGGER is not None:
        return _LOGGER
    return _NULL_LOGGER


def _noop(*args: Any, **kwargs: Any) -> None:
    """空 logger 的一切方法兜底：吞掉调用，返回 None。"""
    return None


class _NullLogger:
    """set_logger 未调用时的兜底 logger：全部日志调用静默 no-op。

    不显式枚举 info/warning/... 方法——宿主 logger（astrbot 的
    ``_PluginContextLogger``）本身就有超出标准库的方法面，统一经
    ``__getattr__`` 兜底为 no-op 可调用，回退路径只求不炸、不求保真。
    """

    __slots__ = ()

    def __getattr__(self, name: str):
        return _noop


_NULL_LOGGER = _NullLogger()


class _LoggerProxy:
    """把属性读取转发给 ``get_logger()`` 的最小代理。

    存在的唯一目的：让搬移进 quill/ 的方法体保持 ``logger.xxx(...)``
    原样（模块级名字 ``logger`` 仍然可用），真正的 logger 在每次属性
    访问时现取，``set_logger`` 换绑后无需重建任何引用。
    """

    __slots__ = ()

    def __getattr__(self, name):
        return getattr(get_logger(), name)


# quill/ 内模块统一 `from ...core.logbridge import logger`：
# 方法体内的 logger 引用与原 main.py 一字不差。
logger = _LoggerProxy()
