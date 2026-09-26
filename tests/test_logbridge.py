# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""logbridge 回退行为测试（v5.3.0 M4.4 去 logging 化）。

M4.4 起 ``quill/core/logbridge.py`` 不再 import 内置 logging——set_logger
未调用时回退内置**全 no-op 空 logger**（理由见其模块 docstring：生产路径
main.py __init__ 最早处即注入宿主 logger，回退只防御测试 / 异常加载顺序，
静默优于绕开 astrbot.api 另起日志通道）。本文件钉住该回退契约：

- 未注入 → get_logger() 返回空 logger，任何方法调用静默、零输出；
- 注入 → 代理转发到宿主对象，换绑即时生效。

注意 logbridge 是模块级单例（``_LOGGER`` 全局），测试用 monkeypatch
改写并在用例结束自动还原，不污染其他用例（真实注入由 main.py __init__
在其他测试里完成）。
"""

from __future__ import annotations

from astrbot_plugin_quillplus.quill.core import logbridge


def test_fallback_without_set_logger_is_silent_noop(monkeypatch, capsys):
    """未注入宿主 logger：回退 no-op，任何日志调用不炸、零输出。"""
    monkeypatch.setattr(logbridge, "_LOGGER", None)

    fallback = logbridge.get_logger()
    # 标准六个级别 + 宿主自定义方法 + 非方法属性，一律 no-op 可调用
    fallback.debug("d")
    fallback.info("i %s", "arg")
    fallback.warning("w")
    fallback.error("e")
    fallback.exception("exc")          # 标准 logging 签名要求活跃异常，no-op 不应炸
    fallback.critical("c")
    fallback.success("宿主自定义方法面")   # astrbot _PluginContextLogger 形态
    assert fallback.isEnabledFor(20) is None  # 属性兜底同为 no-op
    assert capsys.readouterr().out == ""      # 静默：不写 stdout
    assert not capsys.readouterr().err        # 也不写 stderr


def test_set_logger_binds_and_rebinds_instantly(monkeypatch):
    """注入后代理转发宿主对象；换绑（含解绑）即时生效。"""
    monkeypatch.setattr(logbridge, "_LOGGER", None)

    calls: list[str] = []

    class _Host:
        def info(self, msg, *args, **kwargs):
            calls.append(f"host:{msg}")

    logbridge.set_logger(_Host())
    assert logbridge.get_logger().__class__ is _Host
    logbridge.logger.info("hello")     # 经 _LoggerProxy 转发
    assert calls == ["host:hello"]

    # 换绑为 None（模拟异常加载顺序）→ 回退 no-op，不炸
    monkeypatch.setattr(logbridge, "_LOGGER", None)
    logbridge.logger.warning("swallowed")
    assert calls == ["host:hello"]


def test_module_has_no_logging_import():
    """合规契约：logbridge 模块零 logging import 语句（M4.4 上架硬约束）。

    按 AST 断言而非文本 grep——模块 docstring 记录了"为何禁止内置
    logging"，正文里出现该词是文档而非违规。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(logbridge))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "logging" for alias in node.names), node
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "logging", node
