# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H6（on_decorating_result）行为快照测试（v5.3.0 M2.2 钩子薄化第一轮）。

作用
----
M2.2 的"先立保护网再走钢丝"：对 H6 现行为（含剥离器两档强度、顶层降级、
只减法不补栏）建立快照，然后做「剥离器下沉 quill/services/statusbar/
strip.py + 钩子薄化为注册桩+interfaces 委托」，搬移前后本文件**一字不改**
重跑，证明等价（BASELINE §8.2 验收面；行为要点见 BASELINE §2 H6 行）。

宿主建模
--------
沿用 tests/legacy/test_status_bar_parsers.py::t26 的手法：
  - ``object.__new__(QuillPlugin)`` 轻量宿主 + setattr（不走 __init__，
    不建 RAG/世界书等重资源）；
  - props 用 SimpleNamespace（M2.3 后 main.py 自身代码经 self.props 读
    配置；Mixin 侧裸读 self.<attr> 走 __getattr__ 兜底——两条读路径都
    建模，与 t26 一致）；
  - 必须用**真实的** ``astrbot.core.message.components.Plain``（钩子里做
    isinstance 判断，自造同名字段对象会被跳过——t26 记载的第一版假失败）。

注入点约定（搬移前后稳定的唯一耦合）
--------------------------------
剥离器异常注入 patch 的是 ``QuillPlugin._strip_raw_markers`` /
``QuillPlugin._strip_status_artifacts`` 类属性：搬移前 H6 以
``self._strip_*(...)`` 调用；搬移后 interfaces 实现以
``plugin._strip_*(...)`` 调用（逐字翻译，保持动态分发路径不变）——
两个世界都被同一个 patch 点命中。用例同时断言注入**确实命中**
（调用记录非空），防止注入静默失效造成假绿。
"""

from __future__ import annotations

import types

import pytest

from astrbot_plugin_quillplus import main as M
from astrbot.core.message.components import Plain

# ── 样本（与 t26 同源的泄漏形态 + 渲染产物）─────────────────────────

DIRTY_LOVE = "[LOVE_DATA] 88/100（爱意） | 亲密恋人 | 温柔 | 海滩 | 泳装 | 想法"
DIRTY_STATUS = "前文。[STATUS]好感度=65[/STATUS] 后文。"
CLEAN = "普通的剧情正文，没有残留。"
RENDERED = (
    "**状态栏**\n```\n好感度：88/100（爱意）\n关系阶段：亲密恋人\n"
    "心情：雀跃\n位置：海滩\n穿着：泳装\n当前想法：想法\n\n"
    ">>> 剧情走向 <<<\n1. 继续当前话题\n<<< 请选择 >>>\n```"
)
CUSTOM_TPL = (
    "[[CUSTOMTPL]]\n好感度：88/100（爱意）\n关系阶段：亲密恋人\n[[/CUSTOMTPL]]"
)
BARE_FIELDS = "正文。\n好感度：65\n心情：开心"


# ── 宿主/事件桩（t26 手法）──────────────────────────────────────────


class _State:
    def __init__(self, mode: str = "auto"):
        self._mode = mode

    async def get_status_bar_mode(self, _tid):
        return self._mode


class _Result:
    """result.chain 容器（真实 Plain 组件）。"""

    def __init__(self, texts):
        self.chain = [Plain(t) for t in texts]


class _Ev:
    def __init__(self, result):
        self._r = result

    def get_result(self):
        return self._r

    # _get_target_id 先探测 unified_msg_origin（存在即用），桩提供之
    def get_sender_id(self):
        return "10000"

    def unified_msg_origin(self):
        return "test:umo"


def _mk_host(enabled: bool, mode: str = "auto"):
    """轻量 QuillPlugin 宿主：props + Mixin 读路径双建模（见 t26）。"""
    h = object.__new__(M.QuillPlugin)
    h.props = types.SimpleNamespace(
        love_fields=list(M._DEFAULT_LOVE_FIELDS_RAW),
        status_bar_enabled=enabled,
    )
    h.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    h.status_bar_default_placeholder = "未设置"
    h.status_bar_enabled = enabled          # 面板全局（实例字典遮蔽路径）
    h.state_manager = _State(mode)          # 会话级覆盖
    return h


async def _run_hook(host, event) -> None:
    """经注册桩调用（搬移前后都是 QuillPlugin.on_decorating_result）。"""
    await M.QuillPlugin.on_decorating_result(host, event)


# ── 用例 ────────────────────────────────────────────────────────────


async def test_h6_enabled_strips_raw_markers_keeps_clean_and_rendered():
    """状态栏开启：只擦原始标记；干净段落与已渲染栏原样保留；不补栏。"""
    host = _mk_host(enabled=True)
    ev = _Ev(_Result([DIRTY_LOVE, CLEAN, DIRTY_STATUS]))
    n_before = len(ev.get_result().chain)

    await _run_hook(host, ev)

    chain = ev.get_result().chain
    # 不补栏：链长度不变（只减法）
    assert len(chain) == n_before
    # 裸 [LOVE_DATA] / [STATUS] 标记被剥
    assert "[LOVE_DATA]" not in chain[0].text
    assert "[STATUS]" not in chain[2].text
    assert "前文。" in chain[2].text and "后文。" in chain[2].text
    # 干净段落原样保留
    assert chain[1].text == CLEAN


async def test_h6_enabled_never_eats_rendered_bar_or_custom_template():
    """开启时绝不能用整套剥离器：渲染产物与自定义模板不得被掏空。

    这是第一版兜底钩子的真实回归（t26 记载）：整套剥离的第一条模式匹配
    ``**状态栏**...``` ```，会把 L1/L2 正常渲染的栏整段删掉。
    """
    host = _mk_host(enabled=True)
    ev_r = _Ev(_Result([RENDERED]))
    await _run_hook(host, ev_r)
    assert ev_r.get_result().chain[0].text == RENDERED

    ev_c = _Ev(_Result([CUSTOM_TPL]))
    await _run_hook(host, ev_c)
    assert ev_c.get_result().chain[0].text == CUSTOM_TPL


async def test_h6_disabled_full_strip():
    """状态栏关闭：全套 _strip_status_artifacts 生效（渲染产物也清）。"""
    host = _mk_host(enabled=False)
    ev = _Ev(_Result([RENDERED, BARE_FIELDS, DIRTY_LOVE, CLEAN]))
    n_before = len(ev.get_result().chain)

    await _run_hook(host, ev)

    chain = ev.get_result().chain
    assert len(chain) == n_before                      # 不补栏
    assert chain[0].text.strip() == ""                 # 渲染栏整段消失
    assert chain[1].text == "正文。"                   # 裸字段行被剥
    assert "[LOVE_DATA]" not in chain[2].text          # 原始标记也被剥
    assert chain[3].text == CLEAN                      # 干净正文不受影响


async def test_h6_session_override_both_directions():
    """会话级覆盖 > 面板全局：off 覆盖 on 走全套剥离；on 覆盖 off 只擦标记。"""
    # off 覆盖 on → 按关闭处理
    host = _mk_host(enabled=True, mode="off")
    ev = _Ev(_Result([RENDERED]))
    await _run_hook(host, ev)
    assert ev.get_result().chain[0].text.strip() == ""

    # on 覆盖 off → 按开启处理（只擦原始标记，渲染栏保留）
    host2 = _mk_host(enabled=False, mode="on")
    ev2 = _Ev(_Result([RENDERED, DIRTY_LOVE]))
    await _run_hook(host2, ev2)
    chain = ev2.get_result().chain
    assert chain[0].text == RENDERED
    assert "[LOVE_DATA]" not in chain[1].text


async def test_h6_none_result_safe():
    """result 为 None：安全返回，不抛（降级语义=放行）。"""
    host = _mk_host(enabled=True)

    class _EvNone:
        def get_result(self):
            return None

    await _run_hook(host, _EvNone())  # 不应抛


async def test_h6_empty_chain_safe():
    """chain 为空 / result 无 chain 属性 / Plain 文本为空：均安全跳过。"""
    host = _mk_host(enabled=True)

    class _Empty:
        chain = []

    class _NoChain:
        pass

    class _EmptyText:
        chain = [Plain("")]

    for result in (_Empty(), _NoChain(), _EmptyText()):
        await _run_hook(host, _Ev(result))  # 不应抛


async def test_h6_non_plain_components_untouched():
    """非 Plain 组件（图片等）不被触碰，也不能因访问 .text 抛异常。"""
    host = _mk_host(enabled=True)

    class _Img:
        pass

    img = _Img()
    result = _Result([DIRTY_LOVE])
    result.chain.append(img)
    ev = _Ev(result)

    await _run_hook(host, ev)

    chain = ev.get_result().chain
    assert chain[1] is img                      # 同一对象、未被改动
    assert "[LOVE_DATA]" not in chain[0].text   # 文本段仍被擦净


@pytest.mark.parametrize(
    ("case", "enabled", "stripper"), [
        ("enabled_raw_markers", True, "_strip_raw_markers"),
        ("disabled_full_strip", False, "_strip_status_artifacts"),
    ]
)
async def test_h6_stripper_exception_degrades(monkeypatch, case, enabled, stripper):
    """异常注入：剥离器抛异常 → 钩子吞掉放行，不中断发送（顶层降级）。

    patch 点为 QuillPlugin 类属性（搬移前后 H6 都经 plugin/self._strip_*
    动态分发，见文件头「注入点约定」）。calls 非空断言防止注入静默失效。
    """
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected stripper failure")

    monkeypatch.setattr(M.QuillPlugin, stripper, staticmethod(_boom))

    host = _mk_host(enabled=enabled)
    ev = _Ev(_Result([DIRTY_LOVE, CLEAN]))

    # 不应抛（抛了会中断整条回复的发送）
    await _run_hook(host, ev)

    assert len(calls) >= 1, "异常注入未命中实际调用路径（假绿）"
    # 剥离器在第一个组件上就抛 → 两段文本都未被改动
    chain = ev.get_result().chain
    assert chain[0].text == DIRTY_LOVE
    assert chain[1].text == CLEAN


async def test_h6_event_exception_degrades(monkeypatch):
    """event.get_result() 自身抛异常：同样被顶层降级吞掉，不外抛。"""
    host = _mk_host(enabled=True)

    class _BadEv:
        def get_result(self):
            raise RuntimeError("模拟异常")

    await _run_hook(host, _BadEv())  # 不应抛
