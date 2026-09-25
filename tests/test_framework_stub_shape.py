# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""stub 形状守卫（M1.1 的制度化工序）。

framework_stubs 的形状必须与真机 AstrBot 4.28.0 一致（BASELINE.md §1.1）。
本测试在每次 CI 中强制执行：谁把 request.json/form/files 改成同步形状，
这里立刻红——那正是重构版"面板全写操作失效、三道防线全漏"的根因。

注意：这里直接实例化 framework_stubs 自己的 _RequestProxy 做校验，
**不**从 astrbot.api.web 导入——本地带真机跑时 sys.modules 里是真实框架
的 request 代理（在 handler 上下文外调用会抛 RuntimeError），测它没有
意义；要守卫的是 stub 本身的形状。
"""

from __future__ import annotations

import asyncio

from tests.infra import framework_stubs


def test_stub_shape_matches_real_astrbot() -> None:
    framework_stubs.assert_shape_contract()


def test_json_default_semantics() -> None:
    """stub request.json 解析失败时回退 default —— 对齐真机 L200-212 行为。"""
    proxy = framework_stubs._RequestProxy()
    token = proxy.set(json=b"not-a-json-object")
    try:
        got = asyncio.run(proxy.json(default={}))
        assert got == {}, f"坏 JSON 应回退 default，实际 {got!r}"
    finally:
        proxy.reset(token)


def test_request_context_roundtrip() -> None:
    """RequestContext 上下文管理器：进得去、出得来（解绑干净）。"""
    proxy = framework_stubs._RequestProxy()
    with framework_stubs.RequestContext(
        json={"a": 1}, query={"b": "2"}, files={}
    ):
        assert asyncio.run(proxy.json()) == {"a": 1}
        assert proxy.query.get("b") == "2"
    # 退出后回到空上下文（该代理共享类级 _CURRENT，空态返回 default）
    assert asyncio.run(proxy.json(default={"x": 9})) == {"x": 9}


def test_upload_file_shape() -> None:
    """PluginUploadFile：async read、filename/content_type 属性。"""
    f = framework_stubs.upload("a.png", b"\x89PNG", "image/png")
    assert asyncio.run(f.read()) == b"\x89PNG"
    assert f.filename == "a.png"
    assert f.content_type == "image/png"


def test_multidict_getlist() -> None:
    d = framework_stubs.PluginMultiDict({"k": ["v1", "v2"], "s": "one"})
    assert d.getlist("k") == ["v1", "v2"]
    assert d.getlist("s") == ["one"]
    assert d.getlist("missing") == []
