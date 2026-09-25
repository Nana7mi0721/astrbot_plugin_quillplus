# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""手写 self-test 脚本的 pytest 包装层（M1.2）。

背景：tests/legacy/ 下三个脚本是 M1 之前的保护网本体（尤其状态栏
1461 行 fixture），其断言是"行为等价"验收的对照面。PLAN §M1.2 要求
断言 100% 保留——因此**不改 legacy 脚本一行断言**，只做两件事：
  1. import 副作用引导（模块级 sys.path / stub 安装在脚本内已有）；
  2. 把每个 t* 用例映射为独立 pytest 测试函数，失败互相隔离。

实现方式：import legacy 脚本会直接执行其模块级代码（含 stub 安装与
插件导入），这是其既有语义，保持不动。t24 需要一个协程插件实例，
用事件循环内构造（asyncio_mode=auto 下同步测试跑在无循环环境，
故显式 new_event_loop）。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY = os.path.join(_HERE, "legacy")


def _load(name: str):
    """导入 legacy 脚本（sys.modules 缓存共享副作用与 _Host 构造）。"""
    if name not in sys.modules:
        path = os.path.join(_LEGACY, f"{name}.py")
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


SB = pytest.importorskip  # noqa: E501  (语义占位，见下)


# ── legacy.test_status_bar_parsers（26 用例）────────────────────

sb = _load("test_status_bar_parsers")

_ALL_SB_CASES = [
    "t1_status_block", "t2_love_data", "t3_legacy_status", "t4_raw_regex",
    "t5_lenient", "t6_plot_paths", "t7_strip", "t8_import_surface",
    "t9_normalize", "t10_delta_format", "t11_annotate", "t12_delta_roundtrip",
    "t13_inject_report", "t14_report_cache_semantics", "t15_strip_dynamic_fields",
    "t16_session_state_gate", "t17_fence_robustness", "t18_contract_single_source",
    "t19_platform_template", "t20_l4_threshold_and_strip",
    "t21_statusbar_session_override", "t22_statusbar_dispatch",
    "t23_l2_applies_template", "t24_handle_status_bar_coroutine",
    "t25_level_registry_and_gate", "t26_send_hook_safety_net",
]

# t24 内部用 asyncio.run() 起子循环（真机 4.28.0 下 aiosqlite 驱动需要），
# 在 pytest-asyncio 的事件循环里再 run 会报 "cannot be called from a
# running event loop"——改在新线程的新循环里执行，语义不变。


def _make_sb_case(func_name: str):
    case = getattr(sb, func_name)

    if func_name == "t24_handle_status_bar_coroutine":
        def _run() -> None:
            import threading

            result: list = []

            def _worker() -> None:
                result.append(case())

            t = threading.Thread(target=_worker)
            t.start()
            t.join()
            if result and isinstance(result[0], BaseException):
                raise result[0]
    else:
        def _run() -> None:
            case()
    return _run


for _name in _ALL_SB_CASES:
    _fn = _make_sb_case(_name)
    _fn.__name__ = f"test_legacy_sb_{_name}"
    _fn.__doc__ = f"legacy/test_status_bar_parsers.py::{_name}（断言零改动）"
    globals()[_fn.__name__] = _fn


def test_legacy_sb_self_test_fully_green() -> None:
    """legacy 自测的最终判定：main() 应全绿（t* 断言无失败）。"""
    sb._PASSED = sb._FAILED = 0
    sb._FAILURES.clear()
    assert sb.main() == 0, f"legacy self-test 存在失败项: {sb._FAILURES}"
    assert sb._PASSED > 0


# ── legacy.test_memory_fts（5 用例，async）──────────────────────

fts = _load("test_memory_fts")

_ALL_FTS_CASES = [
    "t1_short_and_long", "t2_session_isolation", "t3_search_end_to_end",
    "t4_migration", "t5_idempotent",
]

_FTS_TMP = None
_FTS_STORE = None


async def _fts_store():
    """临时目录 + 已初始化的 MemoryStore（t1-t3 共享；测完 close）。"""
    global _FTS_TMP, _FTS_STORE
    if _FTS_STORE is None:
        _FTS_TMP = __import__("tempfile").mkdtemp(prefix="quilltest_fts_pytest_")
        _FTS_STORE = fts.MemoryStore(os.path.join(_FTS_TMP, "main.db"))
        await _FTS_STORE.initialize()
    return _FTS_STORE


def _make_fts_case(func_name: str):
    func = getattr(fts, func_name)

    if func_name in ("t4_migration", "t5_idempotent"):
        async def _run() -> None:
            await func()
    else:
        async def _run() -> None:
            store = await _fts_store()
            await func(store)
    return _run


for _name in _ALL_FTS_CASES:
    _fn = _make_fts_case(_name)
    _fn.__name__ = f"test_legacy_fts_{_name}"
    _fn.__doc__ = f"legacy/test_memory_fts.py::{_name}（断言零改动）"
    globals()[_fn.__name__] = _fn


async def test_legacy_fts_self_test_fully_green() -> None:
    """legacy FTS 自测的最终判定（含临时目录清理）。"""
    global _FTS_TMP, _FTS_STORE
    fts._PASSED = fts._FAILED = 0
    fts._FAILURES.clear()
    assert await fts.main() == 0, f"legacy FTS self-test 存在失败项: {fts._FAILURES}"
    assert fts._PASSED > 0
    # main() 自建 store，重置共享态，保证与逐用例执行互不干扰
    _FTS_STORE = None
    _FTS_TMP = None


# ── legacy.test_config_projection（4 用例）────────────────────
# M2.3 起该脚本断言经主代理批准整体重写（投影消解 → 访问器覆盖守卫），
# 函数名同步更新；接口（_PASSED/_FAILED/_FAILURES/main）不变。

cp = _load("test_config_projection")

_ALL_CP_CASES = ["t1_accessor_coverage", "t2_projection_eliminated",
                 "t3_mixin_delegate_surface", "t4_save_rollback_semantics"]

for _name in _ALL_CP_CASES:
    def _make_cp(func_name: str):
        func = getattr(cp, func_name)

        def _run() -> None:
            func()
        return _run
    _fn = _make_cp(_name)
    _fn.__name__ = f"test_legacy_cp_{_name}"
    _fn.__doc__ = f"legacy/test_config_projection.py::{_name}（M2.3 重写后的访问器覆盖守卫）"
    globals()[_fn.__name__] = _fn


def test_legacy_cp_self_test_fully_green() -> None:
    """legacy 配置访问器守卫自测的最终判定。"""
    cp._PASSED = cp._FAILED = 0
    cp._FAILURES.clear()
    assert cp.main() == 0, f"legacy config-projection 存在失败项: {cp._FAILURES}"
    assert cp._PASSED > 0
