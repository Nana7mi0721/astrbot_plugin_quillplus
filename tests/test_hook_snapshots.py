# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H6/H1/H2/H4/H5 行为快照测试（v5.3.0 M2.2 钩子薄化）。

作用
----
M2.2 的"先立保护网再走钢丝"：对钩子现行为建立快照，然后做「业务逻辑
下沉 quill/services/ + 钩子薄化为注册桩+interfaces 委托」，搬移前后本文件
（对应用例）**一字不改**重跑，证明等价（BASELINE §8.2 验收面）。

- 第一轮（commit 958722e）：H6（on_decorating_result）——剥离器两档强度、
  顶层降级、只减法不补栏；下沉 quill/services/statusbar/strip.py。
- 第二轮（commit ad44d14）：H1（on_waiting_llm_request）——/reinject 字面
  拦截、stream_mode 三态、内层取值异常静默、`_ensure_persona_conversation`
  对话隔离（含其内部 try/except 吞掉语义）、H1 **无顶层 try** 的降级怪癖
  （BASELINE §2 H1 行：`state_manager.get_state` 抛出会上抛框架，与 H6
  不同，不得补 try）；下沉 quill/services/character.py。
- 第三轮（commit 96e0d99）：H2（on_using_llm_tool）——三重闸门原样放行、
  telegram/tg Markdown 剥离（未知平台不剥离）、状态栏开/关两档、注入报告
  追加到最后一条 plain、JSON 字符串解析-修改-回写（失败即放行）、拒绝模式
  补充扫描（只扫首条 plain）、顶层异常 error 降级放行；telegram 剥离下沉
  quill/services/response.py。
- 第四轮（commit 756513a）：H4（on_llm_response）——前置清洗（[B:...] 解密
  安全网、中断标记擦除）、**状态栏段不受 `_quill_activated` gate 限制**
  （§2.1 不对称点，钉住）、开启提取渲染 + session_vars 持久化、无栏兜底
  （有 persona 才补）、关闭整套剥离、已处理标记下只剥残留不二次渲染、
  注入报告去重追加、gate 后 chat_logs 落库（开关 + `_quill_assistant_logged`
  防双写）、拒绝扫描、顶层异常 error 降级放行（resp 不清空）。本轮无新增
  下沉。
- 第五轮（本轮）：H5（on_llm_tool_respond）——三个早退 gate（非 SMT 工具 /
  未激活 / `_quill_memorized` 去重）、**无钩子级顶层 try** 的降级怪癖
  （gate 间异常上抛框架，BASELINE §2 H5 行，全插件唯一，钉住防误补）、
  **不**把 `_quill_activated` 置 False 的历史行为（§2.1，钉住）、助手回复
  落 chat_logs（防双写与 H4 互斥）、N 轮反思调度（阈值 4 / 读 8 条 /
  最少 2 条边界 + prune/cleanup 调用断言）、两个内层块 warning 吞掉；
  反思调度下沉 quill/services/memory.py（内层降级语义与常量随迁保真）。

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

import json
import logging
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


# ════════════════════════════════════════════════════════════════════
# H1（on_waiting_llm_request）行为快照（M2.2 第二轮）
#
# 行为要点（BASELINE §2 H1 行）：
#   ① 先 _ensure_persona_conversation（必须早于框架 _get_session_conv()，
#      自带全量 try/except：隔离失败只记日志放行）；
#   ② 内层小块取 message_str/_get_target_id，except: return；
#   ③ 拦截字面 /reinject、/重新注入（用 **sender_id** 非 target_id，
#      重置 quill_rounds 后 set_result return，不继续）；
#   ④ 按 state.stream_mode off/on/auto 设 enable_streaming extra
#      （auto 且激活词/【】括号时关流式）。
#   降级怪癖（刻意保留，勿补 try）：H1 **无钩子级顶层 try**——
#   state_manager.get_state 抛出会上抛框架（与 H6 相反）。
#
# 注入点约定（同上）：用例一律经注册桩
# ``QuillPlugin.on_waiting_llm_request(host, event)`` 进入——搬移前逻辑
# 在 main.py 方法体内，搬移后桩一行委托 interfaces → character 服务，
# 入口不变，两个世界命中同一用例。
# ════════════════════════════════════════════════════════════════════

UMO = "aiocqhttp:GroupMessage:12345-67890"


class _StateH1:
    """state_manager 桩：流式偏好 + persona 对话映射，全部调用留痕。"""

    def __init__(self, stream_mode="auto", persona_id="", conv_map=None):
        self.stream_mode = stream_mode
        self._persona_id = persona_id
        self._conv_map = dict(conv_map) if conv_map else {}
        # 留痕
        self.get_state_calls: list = []
        self.reset_rounds_calls: list = []
        self.set_conv_calls: list = []
        self.forget_conv_calls: list = []
        self.persona_calls: list = []
        # 异常注入点
        self.get_state_error = None
        self.get_persona_id_error = None

    async def get_state(self, tid):
        self.get_state_calls.append(tid)
        if self.get_state_error is not None:
            raise self.get_state_error
        return types.SimpleNamespace(stream_mode=self.stream_mode)

    async def reset_quill_rounds(self, user_id):
        self.reset_rounds_calls.append(user_id)

    async def get_persona_id(self, umo):
        self.persona_calls.append("get_persona_id")
        if self.get_persona_id_error is not None:
            raise self.get_persona_id_error
        return self._persona_id

    async def get_persona_conv_map(self, umo):
        self.persona_calls.append("get_persona_conv_map")
        return dict(self._conv_map)

    async def set_persona_conv(self, umo, persona_key, cid):
        self.persona_calls.append("set_persona_conv")
        self.set_conv_calls.append((umo, persona_key, cid))
        self._conv_map[persona_key] = cid

    async def forget_persona_conv(self, umo, persona_key):
        self.persona_calls.append("forget_persona_conv")
        self.forget_conv_calls.append((umo, persona_key))
        self._conv_map.pop(persona_key, None)


class _ConvMgr:
    """conversation_manager 桩：当前会话 + 已知对话表 + 全部调用留痕。"""

    def __init__(self, curr_cid="c-curr", conversations=None,
                 get_conv_error=None):
        self._curr_cid = curr_cid
        self._conversations = dict(conversations) if conversations else {}
        self._get_conv_error = get_conv_error
        self.curr_calls: list = []
        self.get_conv_calls: list = []
        self.new_calls: list = []
        self.switch_calls: list = []
        self._new_seq = 0

    async def get_curr_conversation_id(self, umo):
        self.curr_calls.append(umo)
        return self._curr_cid

    async def get_conversation(self, umo, cid):
        self.get_conv_calls.append(cid)
        if self._get_conv_error is not None:
            raise self._get_conv_error
        return self._conversations.get(cid)

    async def new_conversation(self, umo, platform_id):
        self.new_calls.append((umo, platform_id))
        self._new_seq += 1
        cid = f"c-new-{self._new_seq}"
        self._conversations[cid] = object()
        return cid

    async def switch_conversation(self, umo, cid):
        self.switch_calls.append((umo, cid))
        self._curr_cid = cid


class _DetectorStub:
    """activation_detector 桩：布尔可配 + 调用留痕（H1 只 OR 两个布尔）。"""

    def __init__(self, activated=False, has_bracket=False):
        self.activated = activated
        self.has_bracket = has_bracket
        self.calls: list = []

    def should_activate(self, message):
        self.calls.append(("should_activate", message))
        return self.activated

    def check_brackets(self, message):
        self.calls.append(("check_brackets", message))
        return self.has_bracket


class _EvH1:
    """H1 事件桩：unified_msg_origin 为字符串属性（真机 property 形状）。"""

    def __init__(self, text="", umo=UMO, sender_id="10000",
                 platform_id="aiocqhttp"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._platform_id = platform_id
        self.extras: dict = {}
        self.result = None

    def get_sender_id(self):
        return self._sender_id

    def get_platform_id(self):
        return self._platform_id

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_result(self, result):
        self.result = result


def _mk_h1_host(state=None, detector=None, conv_mgr="default"):
    """轻量 QuillPlugin 宿主（t26 手法）：只挂 H1 触碰的协作对象。

    conv_mgr="default" → 带默认 _ConvMgr；None → context 上没有
    conversation_manager（隔离逻辑早退路径）。
    """
    h = object.__new__(M.QuillPlugin)
    h.state_manager = state if state is not None else _StateH1()
    h.activation_detector = detector if detector is not None else _DetectorStub()
    if conv_mgr == "default":
        conv_mgr = _ConvMgr()
    if conv_mgr is not None:
        h.context = types.SimpleNamespace(conversation_manager=conv_mgr)
    else:
        h.context = types.SimpleNamespace()
    return h


async def _run_h1(host, event):
    """经注册桩调用（搬移前后都是 QuillPlugin.on_waiting_llm_request）。"""
    await M.QuillPlugin.on_waiting_llm_request(host, event)


def _result_text(result) -> str:
    """跨 stub/真机取 MessageEventResult 的纯文本。"""
    return "".join(getattr(c, "text", "") for c in getattr(result, "chain", []))


# ── H1：/reinject 字面拦截 ──────────────────────────────────────────


@pytest.mark.parametrize("literal", ["/reinject", "/重新注入", "/reinject  "])
async def test_h1_reinject_intercepts_on_sender_id(literal):
    """字面拦截：按 **sender_id**（非 target_id/UMO）重置 quill_rounds，
    set_result 回执后 return，不再走流式分支。角色卡对话切换仍先执行
    （persona 步骤排在最前的时序快照）。"""
    state = _StateH1()
    host = _mk_h1_host(state=state)
    ev = _EvH1(text=literal, sender_id="u-reinject", umo=UMO)

    await _run_h1(host, ev)

    # sender_id 维度（怪癖：不用 target_id）
    assert state.reset_rounds_calls == ["u-reinject"]
    # 回执文本
    assert ev.result is not None
    assert "已重置注入状态" in _result_text(ev.result)
    # 不继续：流式分支未触达
    assert state.get_state_calls == []
    assert "enable_streaming" not in ev.extras
    # persona 对话切换仍先于拦截执行（此处首次启用接管当前对话）
    assert state.set_conv_calls == [(UMO, "", "c-curr")]


async def test_h1_reinject_non_literal_passes_through():
    """/reinject 带参数不是字面匹配：照常走流式分支，不重置。"""
    state = _StateH1(stream_mode="on")
    host = _mk_h1_host(state=state)
    ev = _EvH1(text="/reinject now", sender_id="u1")

    await _run_h1(host, ev)

    assert state.reset_rounds_calls == []
    assert state.get_state_calls == [UMO]
    assert ev.extras.get("enable_streaming") is True
    assert ev.result is None


# ── H1：stream_mode 三态 ────────────────────────────────────────────


async def test_h1_stream_mode_off_sets_false_and_stops():
    state = _StateH1(stream_mode="off")
    detector = _DetectorStub(activated=True)   # off 分支优先，不碰检测器
    host = _mk_h1_host(state=state, detector=detector)
    ev = _EvH1(text="随便聊聊")

    await _run_h1(host, ev)

    assert ev.extras.get("enable_streaming") is False
    assert detector.calls == []                # off/on 短路，不求值激活


async def test_h1_stream_mode_on_sets_true_and_stops():
    state = _StateH1(stream_mode="on")
    host = _mk_h1_host(state=state)
    ev = _EvH1(text="随便聊聊")

    await _run_h1(host, ev)

    assert ev.extras.get("enable_streaming") is True


async def test_h1_stream_mode_auto_quiet_keeps_streaming():
    """auto 且无激活词无括号：**不设置** extra（交回框架默认流式决策）。"""
    state = _StateH1(stream_mode="auto")
    detector = _DetectorStub(activated=False, has_bracket=False)
    host = _mk_h1_host(state=state, detector=detector)
    ev = _EvH1(text="今天天气不错")

    await _run_h1(host, ev)

    assert "enable_streaming" not in ev.extras
    # auto 分支两个检测都以原始 user_input 求值
    assert ("should_activate", "今天天气不错") in detector.calls
    assert ("check_brackets", "今天天气不错") in detector.calls


@pytest.mark.parametrize(
    ("activated", "has_bracket", "text"),
    [
        (True, False, "请插入状态栏"),   # 激活词 → 关流式
        (False, True, "【切换场景】"),   # 【】括号 → 关流式
        (True, True, "【插入】"),        # 双命中 → 同样关流式
    ],
)
async def test_h1_stream_mode_auto_activated_disables_streaming(
    activated, has_bracket, text
):
    state = _StateH1(stream_mode="auto")
    detector = _DetectorStub(activated=activated, has_bracket=has_bracket)
    host = _mk_h1_host(state=state, detector=detector)
    ev = _EvH1(text=text)

    await _run_h1(host, ev)

    assert ev.extras.get("enable_streaming") is False


async def test_h1_auto_with_real_detector_word_and_bracket(tmp_path):
    """auto 分支接真实 ActivationDetector（临时词表）：激活词/【】关流式、
    普通消息不动 extra——钉住 user_input 原样传入的接线。"""
    from astrbot_plugin_quillplus.activation import ActivationDetector

    yaml_path = tmp_path / "triggers.yaml"
    yaml_path.write_text(
        "activation_words:\n  - 插入\nexact_match_words:\n  - love\n",
        encoding="utf-8",
    )
    detector = ActivationDetector(str(yaml_path))
    state = _StateH1(stream_mode="auto")
    host = _mk_h1_host(state=state, detector=detector)

    ev_word = _EvH1(text="请插入状态栏")
    await _run_h1(host, ev_word)
    assert ev_word.extras.get("enable_streaming") is False

    ev_bracket = _EvH1(text="【切换场景】")
    await _run_h1(host, ev_bracket)
    assert ev_bracket.extras.get("enable_streaming") is False

    ev_quiet = _EvH1(text="今天天气不错")
    await _run_h1(host, ev_quiet)
    assert "enable_streaming" not in ev_quiet.extras


# ── H1：内层取值异常 → 静默 return ──────────────────────────────────


async def test_h1_inner_extraction_failure_silent_return():
    """message_str 取值抛异常：内层 except 吞掉静默 return——
    不 set_result、不重置、不读流式偏好（内层小块语义快照）。"""
    state = _StateH1()
    host = _mk_h1_host(state=state, conv_mgr=None)  # 隔离步骤无 manager 早退

    class _BadTextEv(_EvH1):
        def __init__(self):
            # 绕开父类 __init__ 对 message_str 的赋值（子类是无 setter 的
            # 只读 property，赋值会 AttributeError 干扰被测行为）
            self.unified_msg_origin = UMO
            self._sender_id = "u1"
            self._platform_id = "aiocqhttp"
            self.extras = {}
            self.result = None

        @property
        def message_str(self):
            raise RuntimeError("message_str 取值失败")

    ev = _BadTextEv()

    await _run_h1(host, ev)  # 不应抛

    assert state.reset_rounds_calls == []
    assert state.get_state_calls == []
    assert ev.extras == {}
    assert ev.result is None


async def test_h1_target_id_failure_silent_return():
    """_get_target_id 抛异常（无 UMO 且 get_sender_id 炸）：同样静默 return。"""
    state = _StateH1()
    host = _mk_h1_host(state=state, conv_mgr=None)

    class _BadTargetEv(_EvH1):
        def __init__(self):
            # 同上：绕开父类对 unified_msg_origin 的赋值
            self.message_str = "普通消息"
            self._sender_id = "10000"
            self._platform_id = "aiocqhttp"
            self.extras = {}
            self.result = None

        @property
        def unified_msg_origin(self):
            return ""  # 空 UMO → _get_target_id 回退 get_sender_id()

        def get_sender_id(self):
            raise RuntimeError("sender_id 取值失败")

    ev = _BadTargetEv()

    await _run_h1(host, ev)  # 不应抛

    assert state.get_state_calls == []
    assert ev.extras == {}


# ── H1：降级怪癖 —— 无顶层 try，get_state 抛出上抛框架 ───────────────


async def test_h1_get_state_failure_propagates_no_top_try():
    """H1 与 H6 的不对称点（BASELINE §2 H1 行）：钩子**无顶层 try**，
    state_manager.get_state 抛出**原样上抛框架**——本用例刻意钉住该怪癖，
    防止后续"顺手"补 try 改变降级位置。"""
    state = _StateH1(stream_mode="auto")
    state.get_state_error = RuntimeError("状态存储损坏")
    host = _mk_h1_host(state=state)
    ev = _EvH1(text="普通消息")

    with pytest.raises(RuntimeError, match="状态存储损坏"):
        await _run_h1(host, ev)


# ── H1 ①：_ensure_persona_conversation 对话隔离 ─────────────────────


async def test_h1_persona_no_conv_manager_noop():
    """context 无 conversation_manager：隔离步骤零查询早退，钩子照常继续。"""
    state = _StateH1(stream_mode="on")
    host = _mk_h1_host(state=state, conv_mgr=None)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert state.persona_calls == []           # 零额外查询
    assert state.get_state_calls == [UMO]
    assert ev.extras.get("enable_streaming") is True


async def test_h1_persona_internal_error_swallowed_and_flow_continues():
    """隔离逻辑内部异常（state_manager.get_persona_id 炸）：被其自带的全量
    try 吞掉只记日志，本轮沿用当前对话，H1 后续流式分支照常执行。"""
    state = _StateH1(stream_mode="on")
    state.get_persona_id_error = RuntimeError("persona 读取失败")
    conv_mgr = _ConvMgr()
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)  # 隔离失败不外抛

    assert state.get_state_calls == [UMO]      # 继续走流式分支
    assert ev.extras.get("enable_streaming") is True
    assert conv_mgr.switch_calls == []         # 沿用当前对话，未切换
    assert state.set_conv_calls == []


async def test_h1_persona_fast_path_already_on_target():
    """已在目标卡专属对话：快路径 return，零切换/零查询，钩子继续。"""
    state = _StateH1(stream_mode="on", persona_id="p1", conv_map={"p1": "c-curr"})
    conv_mgr = _ConvMgr(curr_cid="c-curr")
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.get_conv_calls == []
    assert conv_mgr.switch_calls == []
    assert state.set_conv_calls == []
    assert ev.extras.get("enable_streaming") is True


async def test_h1_persona_switch_back_restores_card_conversation():
    """旧卡切回：目标 cid 存在 → switch 恢复该卡上次对话（历史不断）。"""
    state = _StateH1(stream_mode="on", persona_id="p1",
                     conv_map={"p1": "c-p1", "p2": "c-p2"})
    conv_mgr = _ConvMgr(curr_cid="c-p2", conversations={"c-p1": object(),
                                                        "c-p2": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.get_conv_calls == ["c-p1"]     # 校验存在性
    assert conv_mgr.switch_calls == [(UMO, "c-p1")]
    assert state.set_conv_calls == []              # 已登记，无需重写
    assert ev.extras.get("enable_streaming") is True


async def test_h1_persona_first_enable_takes_over_current_conversation():
    """首次启用隔离（映射空且有当前对话）：当前卡**接管**现有对话，不新建。"""
    state = _StateH1(stream_mode="on", persona_id="p1", conv_map={})
    conv_mgr = _ConvMgr(curr_cid="c-curr")
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.new_calls == []                    # 接管而非新建
    assert state.set_conv_calls == [(UMO, "p1", "c-curr")]
    assert conv_mgr.switch_calls == []                 # 本来就在该对话
    assert ev.extras.get("enable_streaming") is True


async def test_h1_persona_new_card_gets_fresh_conversation():
    """换新卡（已有其他卡的映射）：新建独立对话并切换——对话历史隔离。"""
    state = _StateH1(stream_mode="on", persona_id="p-new",
                     conv_map={"p-old": "c-old"})
    conv_mgr = _ConvMgr(curr_cid="c-old", conversations={"c-old": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息", platform_id="aiocqhttp")

    await _run_h1(host, ev)

    assert conv_mgr.new_calls == [(UMO, "aiocqhttp")]
    assert state.set_conv_calls == [(UMO, "p-new", "c-new-1")]
    assert conv_mgr.switch_calls == [(UMO, "c-new-1")]
    assert ev.extras.get("enable_streaming") is True


async def test_h1_persona_dead_conversation_rebuilt():
    """映射指向已被 Dashboard 删除的对话：forget 后**重建**新独立对话
    （不校验存在性会导致每轮切到不存在的 cid 不断新建/泄漏——原注释语义）。"""
    state = _StateH1(stream_mode="on", persona_id="p1", conv_map={"p1": "c-dead"})
    conv_mgr = _ConvMgr(curr_cid="c-curr", conversations={"c-curr": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.get_conv_calls == ["c-dead"]
    assert state.forget_conv_calls == [(UMO, "p1")]
    # 旧映射非空 → 不走"首次接管"，走重建
    assert conv_mgr.new_calls == [(UMO, "aiocqhttp")]
    assert state.set_conv_calls == [(UMO, "p1", "c-new-1")]
    assert conv_mgr.switch_calls == [(UMO, "c-new-1")]


async def test_h1_persona_get_conversation_raise_treated_as_missing():
    """get_conversation 本身抛异常：与返回 None 同等处理（重建路径）。"""
    state = _StateH1(stream_mode="on", persona_id="p1", conv_map={"p1": "c-dead"})
    conv_mgr = _ConvMgr(curr_cid="c-curr",
                        get_conv_error=RuntimeError("存储抖动"))
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert state.forget_conv_calls == [(UMO, "p1")]
    assert conv_mgr.new_calls == [(UMO, "aiocqhttp")]
    assert conv_mgr.switch_calls == [(UMO, "c-new-1")]


# ── F2：换卡不重置 quill_rounds（M3.0，BASELINE §8.2 F2）────────────
#
# 现象与根因（BASELINE §8.2 F2）：quill_rounds 键为 UMO（聊天会话级），
# 换角色卡（新建隔离对话）后计数延续（实测跨三张卡累到第 9 轮）→ 新卡
# 首轮即 skip_constants，而新对话无历史可承载被跳过的 Layer 1 常驻
# （WR/WB 常驻内容缺失）。
#
# 修复设计（PLAN §M3.0）：ensure_persona_conversation 在**新建独立对话**
# 成功后与**切换到不同对话**成功后调用
# ``state_manager.reset_quill_rounds(target_id)``；同卡快路径（已绑定且
# 已在目标对话）与首次启用接管当前对话（不新建）**不**重置；重置失败只
# 记 warning、不阻断对话隔离主流程（quill/services/character.py）。


async def test_f2_new_card_conversation_resets_quill_rounds_once():
    """换新卡 → 新建独立对话成功后 reset_quill_rounds(UMO) 恰好**一次**
    （新建后必然紧随切换，去重旗标防止同一轮双重重置）。"""
    state = _StateH1(stream_mode="on", persona_id="p-new",
                     conv_map={"p-old": "c-old"})
    conv_mgr = _ConvMgr(curr_cid="c-old", conversations={"c-old": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息", platform_id="aiocqhttp")

    await _run_h1(host, ev)

    assert conv_mgr.new_calls == [(UMO, "aiocqhttp")]
    assert conv_mgr.switch_calls == [(UMO, "c-new-1")]
    assert state.reset_rounds_calls == [UMO]      # 恰好一次
    assert ev.extras.get("enable_streaming") is True


async def test_f2_switch_to_different_conversation_resets_quill_rounds():
    """切回旧卡（目标对话存在且与当前不同）→ switch 成功后 reset 一次。"""
    state = _StateH1(stream_mode="on", persona_id="p1",
                     conv_map={"p1": "c-p1", "p2": "c-p2"})
    conv_mgr = _ConvMgr(curr_cid="c-p2", conversations={"c-p1": object(),
                                                        "c-p2": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.get_conv_calls == ["c-p1"]    # 先校验存在性
    assert conv_mgr.switch_calls == [(UMO, "c-p1")]
    assert state.reset_rounds_calls == [UMO]
    assert ev.extras.get("enable_streaming") is True


async def test_f2_fast_path_does_not_reset_quill_rounds():
    """同卡快路径（已绑定且已在目标对话）：不新建、不切换、**不重置**
    ——连续激活的轮次计数不能被清掉。"""
    state = _StateH1(stream_mode="on", persona_id="p1",
                     conv_map={"p1": "c-curr"})
    conv_mgr = _ConvMgr(curr_cid="c-curr")
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.get_conv_calls == []
    assert conv_mgr.switch_calls == []
    assert state.set_conv_calls == []
    assert state.reset_rounds_calls == []
    assert ev.extras.get("enable_streaming") is True


async def test_f2_first_enable_takeover_does_not_reset_quill_rounds():
    """首次启用隔离（映射空 → 接管当前对话，不新建）：**不**重置
    ——历史对话延续，轮次语义与接管前保持一致。"""
    state = _StateH1(stream_mode="on", persona_id="p1", conv_map={})
    conv_mgr = _ConvMgr(curr_cid="c-curr")
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息")

    await _run_h1(host, ev)

    assert conv_mgr.new_calls == []               # 接管而非新建
    assert conv_mgr.switch_calls == []            # 本来就在该对话
    assert state.reset_rounds_calls == []
    assert ev.extras.get("enable_streaming") is True


async def test_f2_reset_failure_does_not_break_isolation_flow():
    """reset_quill_rounds 抛异常：只记 warning，不阻断主流程——新建/
    切换/登记照常完成，H1 后续流式分支照常执行（该函数既有容错风格）。"""
    state = _StateH1(stream_mode="on", persona_id="p-new",
                     conv_map={"p-old": "c-old"})

    async def _boom(user_id):
        state.reset_rounds_calls.append(user_id)
        raise RuntimeError("injected reset failure")

    state.reset_quill_rounds = _boom              # 实例属性遮蔽，注入失败
    conv_mgr = _ConvMgr(curr_cid="c-old", conversations={"c-old": object()})
    host = _mk_h1_host(state=state, conv_mgr=conv_mgr)
    ev = _EvH1(text="普通消息", platform_id="aiocqhttp")

    await _run_h1(host, ev)                       # 不应抛

    assert state.reset_rounds_calls == [UMO]      # 重置被尝试过
    assert conv_mgr.new_calls == [(UMO, "aiocqhttp")]
    assert conv_mgr.switch_calls == [(UMO, "c-new-1")]
    assert state.set_conv_calls == [(UMO, "p-new", "c-new-1")]
    assert ev.extras.get("enable_streaming") is True


# ════════════════════════════════════════════════════════════════════
# H2（on_using_llm_tool）行为快照（M2.2 第三轮）
#
# 行为要点（BASELINE §2 H2 行）：
#   ① 三重闸门：非 send_message_to_user 工具 / `_quill_activated` 未置位 /
#      空 tool_args → 原样放行（不修改 tool_args）；
#   ② telegram/tg 平台剥离 plain 段 Markdown（未知平台不剥离，避免破坏
#      原生 Markdown 渲染）；
#   ③ 状态栏：开启时首条 plain 走六级链提取渲染（handled 后 set
#      `_quill_status_handled`，后续消息只清残留**不**二次渲染）；
#      关闭时整套 `_strip_status_artifacts`；
#   ④ 注入报告追加到最后一条 plain（仅一次，show_inject_report 开），
#      追加在剥离之后且带 rstrip；
#   ⑤ tool_args.messages 为 JSON 字符串时先解析、末尾 ensure_ascii=False
#      序列化回写（解析失败 → 立即放行，后续一切不跑）；
#   ⑥ 拒绝模式补充扫描（只扫首条 plain 文本，命中 mark_refusal 一次，
#      只扫不改）。
#   降级语义（BASELINE §2 H2 行）：单一顶层 try/except → error 日志
#   （「拦截异常，已降级放行」）+ 放行（不修改 tool_args）——该层位在
#   注册桩内保留（与 H6 同形态），interfaces 实现内不重复。
#
# 注入点约定（同 H6/H1）：用例一律经注册桩
# ``QuillPlugin.on_using_llm_tool(host, event, tool, tool_args)`` 进入——
# 搬移前逻辑在 main.py 方法体内，搬移后桩一行委托 interfaces，入口不变，
# 两个世界命中同一用例。顶层异常注入 patch QuillPlugin._handle_status_bar
# （H2 前后都以 self/plugin._handle_status_bar 动态分发，同一 patch 点）；
# error 日志断言 patch main 模块 logger（顶层降级日志的所在模块，搬移
# 前后在 main.py 内——先在钩子体、后在注册桩，同一 patch 点）。
# ════════════════════════════════════════════════════════════════════


class _ToolH2:
    """FunctionTool 桩：H2 只读 .name。"""

    def __init__(self, name="send_message_to_user"):
        self.name = name


class _EvH2:
    """H2 事件桩：unified_msg_origin 为字符串属性（真机 property 形状，
    _get_target_id 取 UMO）；extras 精确建模 get_extra/set_extra。

    platform_name → platform_meta.name（钩子探测的第一优先路径）；
    platform_meta 为 None 时走 get_platform_name() 回退路径。
    """

    def __init__(self, umo=UMO, platform="aiocqhttp", activated=True,
                 platform_name=None, extras=None):
        self.unified_msg_origin = umo
        self.extras: dict = dict(extras or {})
        if activated:
            self.extras["_quill_activated"] = True
        self._platform = platform
        self.platform_meta = (
            types.SimpleNamespace(name=platform_name) if platform_name else None
        )

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_name(self):
        return self._platform


class _StateH2:
    """state_manager 桩：状态栏模式/会话变量/拒绝标记，全部调用留痕。"""

    def __init__(self, mode="auto", session_vars=None):
        self._mode = mode
        self._vars = dict(session_vars or {})
        self.get_mode_calls: list = []
        self.get_vars_calls: list = []
        self.update_vars_calls: list = []
        self.refusal_calls: list = []

    async def get_status_bar_mode(self, tid):
        self.get_mode_calls.append(tid)
        return self._mode

    async def get_session_vars(self, tid):
        self.get_vars_calls.append(tid)
        return dict(self._vars)

    async def update_session_vars(self, tid, updates):
        self.update_vars_calls.append((tid, dict(updates)))
        self._vars.update(updates)

    async def mark_refusal(self, tid):
        self.refusal_calls.append(tid)


def _mk_h2_host(enabled=True, mode="auto", refusal=True,
                patterns=("我不能", "我无法"), show_report=False,
                reports=None):
    """轻量 QuillPlugin 宿主（t26 手法）：只挂 H2 触碰的协作对象。

    - props（SimpleNamespace）：H2 自身经 self.props 读 love_fields /
      refusal_* / show_inject_report / status_bar_enabled；
    - Mixin 读路径（statusbar parsers/render 裸读 self.<attr>）：关键项
      同时 setattr 实例属性（实例字典遮蔽，与 t26/_mk_host 双建模一致）；
    - status_bar_plain_platforms 用真机默认（含 aiocqhttp → 纯文本模板）；
    - show_delta 关（去掉变化标注噪声，渲染断言更稳）；
    - config.status_bar_llm_extract=False（L6 默认关闭）；
    - health_tracker 用真实 HealthTracker（六级链逐级写入点）；
    - reports 非空时预置 `_inject_reports` 缓存（注入报告数据源）。
    """
    h = object.__new__(M.QuillPlugin)
    h.props = types.SimpleNamespace(
        love_fields=list(M._DEFAULT_LOVE_FIELDS_RAW),
        status_bar_enabled=enabled,
        refusal_enabled=refusal,
        refusal_patterns=list(patterns),
        show_inject_report=show_report,
    )
    h.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    h.status_bar_enabled = enabled
    h.status_bar_default_placeholder = "未设置"
    h.status_bar_show_delta = False
    h.status_bar_format_template = "**状态栏**\n```\n{content}\n```"
    h.status_bar_format_plain = "───── 状态栏 ─────\n{content}\n────────────────"
    h.status_bar_plain_platforms = ["aiocqhttp", "qq_official"]
    h.config = types.SimpleNamespace(status_bar_llm_extract=False)
    h.health_tracker = M.HealthTracker()
    h.state_manager = _StateH2(mode)
    if reports is not None:
        h._inject_reports = {UMO: dict(reports)}
    return h


def _plain(text):
    """tool_args.messages 的 plain 段构造（H2 逐 dict 处理，非 Plain 组件）。"""
    return {"type": "plain", "text": text}


async def _run_h2(host, event, tool, tool_args):
    """经注册桩调用（搬移前后都是 QuillPlugin.on_using_llm_tool）。"""
    await M.QuillPlugin.on_using_llm_tool(host, event, tool, tool_args)


# ── H2：三重闸门（原样放行，tool_args 不动）─────────────────────────


async def test_h2_non_smt_tool_passthrough():
    """非 send_message_to_user 工具：第一重闸门直接放行，tool_args 原样，
    且不触达任何状态查询（闸门顺序快照：非 SMT 判定在最前）。"""
    host = _mk_h2_host()
    ev = _EvH2()
    tool_args = {"messages": [_plain(DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(name="search_web"), tool_args)

    assert tool_args["messages"][0]["text"] == DIRTY_LOVE   # 未被处理
    assert host.state_manager.get_mode_calls == []          # 后续一切未跑


@pytest.mark.parametrize("extras", [{}, {"_quill_activated": False}],
                         ids=["missing", "explicit_false"])
async def test_h2_not_activated_passthrough(extras):
    """`_quill_activated` 缺失/False：第二重闸门放行，tool_args 原样。"""
    host = _mk_h2_host()
    ev = _EvH2(activated=False, extras=extras)
    tool_args = {"messages": [_plain(DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert tool_args["messages"][0]["text"] == DIRTY_LOVE
    assert host.state_manager.get_mode_calls == []


async def test_h2_empty_tool_args_passthrough():
    """tool_args None/空 dict：第三重闸门放行（空 dict 也不补 messages 键）。"""
    host = _mk_h2_host()
    ev = _EvH2()

    await _run_h2(host, ev, _ToolH2(), None)          # 不应抛

    empty: dict = {}
    await _run_h2(host, ev, _ToolH2(), empty)

    assert empty == {}
    assert host.state_manager.get_mode_calls == []


# ── H2：状态栏两档（开启提取渲染 / 关闭整套剥离）────────────────────


async def test_h2_sb_on_first_plain_rendered_followups_stripped():
    """状态栏开启：首条 plain 的 [LOVE_DATA] 走六级链提取并渲染
    （aiocqhttp 命中真机默认 plain_platforms → 纯文本模板），handled 后
    set `_quill_status_handled`、取值经 `_persist_status_vars` 落
    session_vars；后续 plain 只清残留标记，**不**二次渲染（P2-3 语义）。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": [
        _plain("开头正文\n" + DIRTY_LOVE),
        _plain("后续段落\n" + DIRTY_LOVE),   # 残留：应被清，不应再次渲染
    ]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    msgs = tool_args["messages"]
    first, second = msgs[0]["text"], msgs[1]["text"]
    # 首条：标记被渲染（纯文本模板），原始标记不再存在
    assert "[LOVE_DATA]" not in first
    assert "───── 状态栏 ─────" in first
    assert "好感度：88/100（爱意）" in first
    assert "开头正文" in first
    # handled → extra 置位；取值落库（键与值进入 session_vars 更新）
    assert ev.extras.get("_quill_status_handled") is True
    assert host.state_manager.update_vars_calls
    tid, updates = host.state_manager.update_vars_calls[0]
    assert tid == UMO
    assert updates.get("好感度") == "88/100（爱意）"
    # 后续：残留标记被清，但没有第二个模板栏（只减法）
    assert "[LOVE_DATA]" not in second
    assert "后续段落" in second
    assert "───── 状态栏 ─────" not in second


async def test_h2_sb_off_strips_artifacts():
    """状态栏关闭：plain 段走 _strip_status_artifacts（渲染产物也清），
    不走提取链（无 `_quill_status_handled` 置位、无 session_vars 落库）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    tool_args = {"messages": [_plain("正文\n" + RENDERED), _plain(BARE_FIELDS)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    msgs = tool_args["messages"]
    assert msgs[0]["text"].strip() == "正文"       # 渲染栏整段被清
    assert msgs[1]["text"] == "正文。"             # 裸字段行被剥（t26 同款样本）
    assert ev.extras.get("_quill_status_handled") is None
    assert host.state_manager.update_vars_calls == []


# ── H2：注入报告追加到最后一条 plain ────────────────────────────────


async def test_h2_inject_report_appended_to_last_plain():
    """show_inject_report 开：报告行追加到最后一条 plain（且仅一次），
    set `_quill_report_added`；追加前 rstrip（尾部空白被清理）。"""
    host = _mk_h2_host(enabled=False, show_report=True, reports={"wb": 2})
    ev = _EvH2()
    tool_args = {"messages": [_plain("第一条"), _plain("最后一条  ")]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    msgs = tool_args["messages"]
    assert msgs[0]["text"] == "第一条"
    assert msgs[1]["text"] == "最后一条\n\n〔注入〕世界书×2"
    assert ev.extras.get("_quill_report_added") is True


async def test_h2_inject_report_off_or_no_cache_stays_silent():
    """show_inject_report 关 / 报告缓存为空：正文零改动（报告行不出现）。"""
    host = _mk_h2_host(enabled=False, show_report=False, reports={"wb": 2})
    ev = _EvH2()
    tool_args = {"messages": [_plain("第一条"), _plain("最后一条")]}
    await _run_h2(host, ev, _ToolH2(), tool_args)
    assert tool_args["messages"][1]["text"] == "最后一条"
    assert "_quill_report_added" not in ev.extras

    host2 = _mk_h2_host(enabled=False, show_report=True)   # 无缓存 → 报告沉默
    ev2 = _EvH2()
    tool_args2 = {"messages": [_plain("最后一条")]}
    await _run_h2(host2, ev2, _ToolH2(), tool_args2)
    assert tool_args2["messages"][0]["text"] == "最后一条"


async def test_h2_inject_report_skipped_when_last_msg_not_plain():
    """最后一条不是 plain（如图片段）：报告无处可附，整轮静默（历史行为）。"""
    host = _mk_h2_host(enabled=False, show_report=True, reports={"wb": 2})
    ev = _EvH2()
    tool_args = {"messages": [_plain("正文"), {"type": "image", "url": "x"}]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert tool_args["messages"][0]["text"] == "正文"
    assert "_quill_report_added" not in ev.extras


# ── H2：JSON 字符串解析-修改-回写 ───────────────────────────────────


async def test_h2_json_string_parse_modify_writeback():
    """tool_args.messages 为 JSON 字符串：解析 → 处理 → ensure_ascii=False
    序列化回写（中文原样、不退化为 \\u 转义）。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": json.dumps(
        [_plain(DIRTY_LOVE), _plain("后续\n" + DIRTY_LOVE)], ensure_ascii=False)}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert isinstance(tool_args["messages"], str)
    assert "───── 状态栏 ─────" in tool_args["messages"]   # ensure_ascii=False
    msgs = json.loads(tool_args["messages"])
    assert "[LOVE_DATA]" not in msgs[0]["text"]
    assert "───── 状态栏 ─────" in msgs[0]["text"]
    assert "[LOVE_DATA]" not in msgs[1]["text"]
    assert "后续" in msgs[1]["text"]


async def test_h2_json_string_parse_failure_passthrough():
    """JSON 解析失败：立即放行——tool_args 原样，状态栏链/落库/报告/扫描
    全都不跑（早退 return 的位置快照）。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": "{这不是JSON"}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert tool_args["messages"] == "{这不是JSON"
    assert host.state_manager.get_mode_calls == []
    assert host.state_manager.refusal_calls == []


# ── H2：telegram/tg 平台 Markdown 剥离 ──────────────────────────────


@pytest.mark.parametrize(
    ("platform_name", "expect_stripped"),
    [("Telegram", True), ("tg", True), ("aiocqhttp", False)],
    ids=["telegram", "tg", "other_platform_keeps_md"],
)
async def test_h2_telegram_markdown_strip_by_platform(platform_name, expect_stripped):
    """telegram/tg 平台剥离 plain 段 Markdown；其他平台原样保留
    （未知平台不剥离，避免破坏原生 Markdown 渲染）。"""
    host = _mk_h2_host(enabled=False)   # 关状态栏，隔离 Markdown 剥离行为
    ev = _EvH2(platform_name=platform_name)
    md_text = "**剧情**与`代码`"
    tool_args = {"messages": [_plain(md_text)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    text = tool_args["messages"][0]["text"]
    if expect_stripped:
        assert text == "剧情与代码"
    else:
        assert text == md_text


# ── H2：拒绝模式补充扫描（S3-2）─────────────────────────────────────


async def test_h2_refusal_scan_hit_marks_once():
    """拒绝模式命中（tool_args 通道补充扫描）：mark_refusal(target_id)
    恰一次；扫描只读不改（文本保持原样）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    tool_args = {"messages": [_plain("我不能继续了"), _plain("第二段")]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert host.state_manager.refusal_calls == [UMO]
    assert tool_args["messages"][0]["text"] == "我不能继续了"


async def test_h2_refusal_scan_only_first_plain():
    """只扫首条 plain 文本：拒绝模式仅出现在后续 plain → 不命中（历史怪癖）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    tool_args = {"messages": [_plain("正常内容"), _plain("我无法照做")]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert host.state_manager.refusal_calls == []


async def test_h2_refusal_scan_disabled():
    """refusal_enabled=False：扫描整体跳过（即使首条 plain 命中模式）。"""
    host = _mk_h2_host(enabled=False, refusal=False)
    ev = _EvH2()
    tool_args = {"messages": [_plain("我不能继续了")]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert host.state_manager.refusal_calls == []


# ── H2：顶层异常 → error 日志 + 原样放行（降级语义）─────────────────


class _LoggerRec:
    """main 模块 logger 替身：记录 error 调用（顶层降级日志断言）。"""

    def __init__(self):
        self.errors: list = []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def warning(self, msg, *a, **k):
        pass

    def info(self, msg, *a, **k):
        pass

    def debug(self, msg, *a, **k):
        pass


async def test_h2_top_level_exception_degrades(monkeypatch):
    """状态栏链中途抛异常 → 顶层 except 吞掉 + error 日志（「拦截异常，
    已降级放行」）→ tool_args 原样放行（不中断 agent loop）。

    patch 点 QuillPlugin._handle_status_bar（搬移前后 H2 都经
    self/plugin._handle_status_bar 动态分发）；calls 非空防注入假绿。
    """
    calls: list = []

    async def _boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected status-bar failure")

    monkeypatch.setattr(M.QuillPlugin, "_handle_status_bar", _boom)
    rec = _LoggerRec()
    monkeypatch.setattr(M, "logger", rec)

    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": [_plain(DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)   # 不应抛

    assert calls, "异常注入未命中实际调用路径（假绿）"
    assert tool_args["messages"][0]["text"] == DIRTY_LOVE   # 原样放行
    assert any("on_using_llm_tool 拦截异常" in e for e in rec.errors)


# ════════════════════════════════════════════════════════════════════
# H4（on_llm_response）行为快照（M2.2 第四轮）
#
# 行为要点（BASELINE §2 H4 行 + §2.1 不对称点）：
#   ① 前置清洗：[B:...] Base64 解密安全网（解密失败保留标记原样）；
#      用户中断系统标记擦除（replace + strip）；
#   ② **状态栏段不受 `_quill_activated` gate 限制**（BASELINE §2.1：
#      状态栏处理在 gate 之前执行，始终处理——刻意怪癖，钉住）：
#      - 开启 + `_quill_status_handled` 已置位（H2 工具钩子已处理）→
#        只剥 completion_text 中的残留标记，**不**二次渲染（"已剥离"
#        日志路径；F1 重复回复链路一环，M3.0 独立设计，此处不修）；
#      - 开启 + 未置位 → 六级链提取渲染（`_handle_status_bar`）+
#        session_vars 统一持久化；无栏（handled=False）且有 persona 时
#        追加兜底栏（`_build_default_love_data`），无 persona 不补；
#      - 关闭 → 整套 `_strip_status_artifacts`；
#   ③ 注入报告追加（show_inject_report + `_quill_report_added` 去重，
#      H2 工具路径与本路径都会跑，标记防两行报告）；
#   ④ gate：未激活 return（②③在 gate 之前照跑，④之后的段落全部跳过）；
#   ⑤ 助手回复落 chat_logs（直接文本流路径；`rag_enable_chat_logging`
#      开关 + retriever/memory_store 存在性判断；`_quill_assistant_logged`
#      标记防 H4/H5 双写；`_spawn` 后台任务）；
#   ⑥ 拒绝模式扫描（refusal_enabled 开 → 命中 mark_refusal 一次即 break）。
#   降级语义（BASELINE §2 H4 行）：单一顶层 try/except → error 日志
#   （「后处理遭遇未捕获异常，已降级放行」）+ 放行，**resp 保持已改到
#   一半的状态**——该层位在注册桩内保留（与 H2/H6 同形态），interfaces
#   实现内不重复。
#
# 注入点约定（同 H6/H1/H2）：用例一律经注册桩
# ``QuillPlugin.on_llm_response(host, event, resp)`` 进入——搬移前逻辑在
# main.py 方法体内，搬移后桩一行委托 interfaces，入口不变，两个世界命中
# 同一用例。异常注入 patch QuillPlugin._handle_status_bar（H4 前后都以
# self/plugin._handle_status_bar 动态分发，同一 patch 点）；error 日志
# 断言 patch main 模块 logger（顶层降级日志搬移前后都在 main.py——先在
# 钩子体、后在注册桩，同一 patch 点）。唯一例外：`_quill_status_handled`
# 残留剥离分支的 info 日志（"已剥离"）随实现搬到 interfaces——该用例同时
# patch main 与 interfaces 两个模块的 logger（搬移前前者命中、搬移后后者
# 命中，共用同一留痕替身），两个世界断言不变。
# ════════════════════════════════════════════════════════════════════

from astrbot_plugin_quillplus.interfaces import astrbot_hooks as _hooks
from astrbot_plugin_quillplus.encryption import b64_wrap

# 与 main.py H4 体 内联常量逐字相同的样本（快照 fixture，非共享常量）
_INTERRUPT_MSG = (
    "[SYSTEM: User actively interrupted the response generation. "
    "Partial output before interruption is preserved.]"
)


class _RespH4:
    """LLMResponse 桩：H4 只读写 completion_text（stub 模式下
    astrbot.api.provider.LLMResponse 为 object，无法实例化）。"""

    def __init__(self, text=""):
        self.completion_text = text


class _EvH4:
    """H4 事件桩：unified_msg_origin 为字符串属性（真机 property 形状，
    _get_target_id 取 UMO）；extras 精确建模 get_extra/set_extra。

    platform_meta 为 None 时走 get_platform_name() 回退路径
    （aiocqhttp → 命中真机默认 plain_platforms → 纯文本模板）。
    """

    def __init__(self, umo=UMO, platform="aiocqhttp", extras=None):
        self.unified_msg_origin = umo
        self.extras: dict = dict(extras or {})
        self._platform = platform
        self.platform_meta = None

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return "10000"

    def get_platform_name(self):
        return self._platform


class _StateH4:
    """state_manager 桩：状态栏模式/会话变量/persona/拒绝标记，全留痕。"""

    def __init__(self, mode="auto", persona_id="p1", session_vars=None):
        self._mode = mode
        self._persona_id = persona_id
        self._vars = dict(session_vars or {})
        self.get_mode_calls: list = []
        self.get_vars_calls: list = []
        self.update_vars_calls: list = []
        self.persona_calls: list = []
        self.refusal_calls: list = []

    async def get_status_bar_mode(self, tid):
        self.get_mode_calls.append(tid)
        return self._mode

    async def get_session_vars(self, tid):
        self.get_vars_calls.append(tid)
        return dict(self._vars)

    async def update_session_vars(self, tid, updates):
        self.update_vars_calls.append((tid, dict(updates)))
        self._vars.update(updates)

    async def get_persona_id(self, tid):
        self.persona_calls.append(tid)
        return self._persona_id

    async def mark_refusal(self, tid):
        self.refusal_calls.append(tid)


class _RagH4:
    """rag_retriever 桩：memory_store 只需真值（H4 只判存在），落日志留痕。"""

    def __init__(self):
        self.memory_store = object()
        self.log_calls: list = []

    async def log_chat_message(self, session_id, role, text):
        self.log_calls.append((session_id, role, text))


def _mk_h4_host(enabled=True, mode="auto", refusal=True,
                patterns=("我不能", "我无法"), show_report=False,
                reports=None, chat_logging=True, rag=True, state=None):
    """轻量 QuillPlugin 宿主（t26 手法）：只挂 H4 触碰的协作对象。

    - props（SimpleNamespace）：H4 自身经 self.props 读 love_fields /
      refusal_* / show_inject_report；
    - Mixin 读路径（statusbar parsers/render 裸读 self.<attr>）：关键项
      同时 setattr 实例属性（与 _mk_h2_host 双建模一致）；
    - status_bar_plain_platforms 用真机默认（aiocqhttp → 纯文本模板）；
    - show_delta 关；config.status_bar_llm_extract=False（L6 默认关闭）；
    - config.rag_enable_chat_logging 可配（H4 直接读 self.config）；
    - health_tracker 用真实 HealthTracker（六级链写入点）；
    - _spawn 捕获后台协程不调度（用例内显式 await 执行，确定性断言）；
    - reports 非空时预置 `_inject_reports` 缓存（注入报告数据源）。
    """
    h = object.__new__(M.QuillPlugin)
    h.props = types.SimpleNamespace(
        love_fields=list(M._DEFAULT_LOVE_FIELDS_RAW),
        status_bar_enabled=enabled,
        refusal_enabled=refusal,
        refusal_patterns=list(patterns),
        show_inject_report=show_report,
    )
    h.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    h.status_bar_enabled = enabled
    h.status_bar_default_placeholder = "未设置"
    h.status_bar_show_delta = False
    h.status_bar_format_template = "**状态栏**\n```\n{content}\n```"
    h.status_bar_format_plain = "───── 状态栏 ─────\n{content}\n────────────────"
    h.status_bar_plain_platforms = ["aiocqhttp", "qq_official"]
    h.status_bar_plot_paths = ["继续当前话题", "换个话题"]
    h.config = types.SimpleNamespace(
        status_bar_llm_extract=False,
        rag_enable_chat_logging=chat_logging,
    )
    h.health_tracker = M.HealthTracker()
    h.state_manager = state if state is not None else _StateH4()
    h.rag_retriever = _RagH4() if rag else None
    h._spawned: list = []
    h._spawn = h._spawned.append      # 捕获协程，用例内手动 await 执行
    if reports is not None:
        h._inject_reports = {UMO: dict(reports)}
    return h


async def _run_h4(host, event, resp):
    """经注册桩调用（搬移前后都是 QuillPlugin.on_llm_response）。"""
    await M.QuillPlugin.on_llm_response(host, event, resp)


class _LoggerRecH4:
    """logger 替身：error/info 分级留痕（顶层降级与"已剥离"日志断言）。"""

    def __init__(self):
        self.errors: list = []
        self.infos: list = []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def warning(self, msg, *a, **k):
        pass

    def info(self, msg, *a, **k):
        self.infos.append(str(msg))

    def debug(self, msg, *a, **k):
        pass


# ── H4：状态栏段不受 gate 限制（§2.1 不对称点，钉住）────────────────


async def test_h4_status_bar_zone_runs_without_activation_gate():
    """`_quill_activated` 缺失（未激活）：状态栏段与注入报告段**照常执行**
    （二者都在 gate 之前），gate 之后的段落全部跳过——不落 chat_logs、
    不置防双写标记、不扫拒绝。钉住这个怪癖。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state, show_report=True,
                       reports={"wb": 2})
    ev = _EvH4()                       # extras 无 _quill_activated
    resp = _RespH4("正文\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)

    # gate 之前①：[LOVE_DATA] 已提取渲染（纯文本模板）
    assert "[LOVE_DATA]" not in resp.completion_text
    assert "───── 状态栏 ─────" in resp.completion_text
    assert "好感度：88/100（爱意）" in resp.completion_text
    assert "正文" in resp.completion_text
    # gate 之前②：注入报告照常追加 + 置位
    assert "〔注入〕世界书×2" in resp.completion_text
    assert ev.extras.get("_quill_report_added") is True
    # gate：未激活 → 记忆/日志/拒绝段全部未跑
    assert host.rag_retriever.log_calls == []
    assert "_quill_assistant_logged" not in ev.extras
    assert state.refusal_calls == []


# ── H4：状态栏开启（提取渲染 + 持久化 / 兜底栏）─────────────────────


async def test_h4_sb_on_renders_and_persists_session_vars():
    """状态栏开启（工具钩子未处理）：[LOVE_DATA] 走六级链提取渲染，
    取值经 `_handle_status_bar` 统一持久化进 session_vars。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4()
    resp = _RespH4("剧情开头\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)

    assert "[LOVE_DATA]" not in resp.completion_text
    assert "───── 状态栏 ─────" in resp.completion_text
    assert "好感度：88/100（爱意）" in resp.completion_text
    assert "剧情开头" in resp.completion_text
    assert state.update_vars_calls
    tid, updates = state.update_vars_calls[0]
    assert tid == UMO
    assert updates.get("好感度") == "88/100（爱意）"
    assert updates.get("关系阶段") == "亲密恋人"


async def test_h4_sb_on_missing_bar_fallback_default_appended():
    """状态栏开启但 LLM 未输出状态栏（handled=False）且有 persona：
    兜底栏追加在正文之后（\\n 连接）——历史值缺省用占位符 + 剧情走向
    选项块；兜底路径不写 session_vars（updates 空 + 未 handled）。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4()
    resp = _RespH4("只有剧情正文，没有状态栏")

    await _run_h4(host, ev, resp)

    assert resp.completion_text.startswith("只有剧情正文，没有状态栏\n")
    assert "───── 状态栏 ─────" in resp.completion_text
    assert "好感度：未设置" in resp.completion_text
    assert ">>> 剧情走向 <<<" in resp.completion_text
    assert state.update_vars_calls == []


async def test_h4_sb_on_no_persona_skips_fallback():
    """handled=False 但无 persona（get_persona_id 空）：不补兜底栏，
    正文原样——`if persona_id:` 分支快照。"""
    state = _StateH4(persona_id="")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4()
    resp = _RespH4("只有剧情正文")

    await _run_h4(host, ev, resp)

    assert resp.completion_text == "只有剧情正文"


# ── H4：状态栏关闭 / 已处理标记下的残留剥离 ─────────────────────────


async def test_h4_sb_off_strips_all_artifacts():
    """状态栏关闭：completion_text 走整套 `_strip_status_artifacts`——
    渲染栏、裸字段、原始标记全部擦除，正文保留，且不新增模板栏。"""
    host = _mk_h4_host(enabled=False)
    ev = _EvH4()
    resp = _RespH4("前文\n" + RENDERED + "\n" + DIRTY_LOVE + "\n后文")

    await _run_h4(host, ev, resp)

    assert "[LOVE_DATA]" not in resp.completion_text
    assert "好感度" not in resp.completion_text
    assert "关系阶段" not in resp.completion_text
    assert "剧情走向" not in resp.completion_text
    assert "───── 状态栏 ─────" not in resp.completion_text
    assert "前文" in resp.completion_text and "后文" in resp.completion_text


async def test_h4_sb_handled_only_strips_residual_no_rerender(monkeypatch):
    """状态栏开启 + `_quill_status_handled` 已置位（H2 工具钩子已处理）：
    只剥离 completion_text 中的残留标记（"已剥离"日志路径），**不走**
    六级链、不二次渲染。"""
    calls: list = []

    async def _spy(*args, **kwargs):
        calls.append(args)
        return "若本桩被调用则此文本会进入结果（不应发生）", {}, False

    monkeypatch.setattr(M.QuillPlugin, "_handle_status_bar", _spy)
    rec = _LoggerRecH4()
    # "已剥离" info 日志：搬移前在 main.py，搬移后在 interfaces——
    # 两个模块的 logger 同 patch 一个替身，两世界断言不变
    monkeypatch.setattr(M, "logger", rec)
    monkeypatch.setattr(_hooks, "logger", rec)

    host = _mk_h4_host(enabled=True)
    ev = _EvH4(extras={"_quill_status_handled": True})
    resp = _RespH4("剧情前段\n" + DIRTY_LOVE + "\n剧情后段")

    await _run_h4(host, ev, resp)

    assert calls == []                 # 六级链未触达（不二次渲染）
    assert "[LOVE_DATA]" not in resp.completion_text
    assert "剧情前段" in resp.completion_text
    assert "剧情后段" in resp.completion_text
    assert "───── 状态栏 ─────" not in resp.completion_text
    assert any("已剥离" in m for m in rec.infos)


# ── H4：注入报告（去重标记）─────────────────────────────────────────


async def test_h4_inject_report_appended_once_with_marker():
    """show_inject_report 开：报告行追加到 completion_text（rstrip 后
    \\n\\n 连接）并 set `_quill_report_added`；标记已置位 / 正文为空时
    不追加（H2 工具路径已追加过的去重语义）。"""
    host = _mk_h4_host(enabled=False, chat_logging=False, show_report=True,
                       reports={"wb": 2})
    ev = _EvH4(extras={"_quill_activated": True})
    resp = _RespH4("第一轮回复")

    await _run_h4(host, ev, resp)

    assert resp.completion_text == "第一轮回复\n\n〔注入〕世界书×2"
    assert ev.extras.get("_quill_report_added") is True

    # 标记已置位：不再追加
    ev2 = _EvH4(extras={"_quill_activated": True, "_quill_report_added": True})
    resp2 = _RespH4("第二条回复")
    await _run_h4(host, ev2, resp2)
    assert resp2.completion_text == "第二条回复"

    # 正文为空：不追加、不置位
    ev3 = _EvH4()
    resp3 = _RespH4("")
    await _run_h4(host, ev3, resp3)
    assert resp3.completion_text == ""
    assert "_quill_report_added" not in ev3.extras


# ── H4：gate 后——助手回复落 chat_logs（防双写）─────────────────────


async def test_h4_activated_assistant_reply_logged_with_marker():
    """激活 + rag_enable_chat_logging 开：completion_text（strip 后）按
    assistant 角色落 chat_logs（后台任务），session id 为
    target_id::persona_id，并 set `_quill_assistant_logged` 防双写标记。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=False, state=state)
    ev = _EvH4(extras={"_quill_activated": True})
    resp = _RespH4("直接文本流回复\n")

    await _run_h4(host, ev, resp)
    for coro in host._spawned:         # _spawn 捕获的后台协程，显式执行
        await coro

    assert host.rag_retriever.log_calls == [
        (UMO + "::p1", "assistant", "直接文本流回复")
    ]
    assert ev.extras.get("_quill_assistant_logged") is True


async def test_h4_chat_logging_off_skips_log():
    """rag_enable_chat_logging 关：不落库、不置标记（开关判断在标记与
    retriever 判断之前，短路快照）。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=False, state=state, chat_logging=False)
    ev = _EvH4(extras={"_quill_activated": True})
    resp = _RespH4("直接文本流回复")

    await _run_h4(host, ev, resp)

    assert host.rag_retriever.log_calls == []
    assert "_quill_assistant_logged" not in ev.extras


async def test_h4_assistant_logged_marker_prevents_double_write():
    """`_quill_assistant_logged` 已置位（H5 工具路径已落库）：直接文本流
    路径不再落库——防双写标记语义（记忆/日志段原样保留的怪癖）。"""
    host = _mk_h4_host(enabled=False)
    ev = _EvH4(extras={"_quill_activated": True,
                       "_quill_assistant_logged": True})
    resp = _RespH4("已被工具路径落库的回复")

    await _run_h4(host, ev, resp)
    for coro in host._spawned:
        await coro

    assert host.rag_retriever.log_calls == []


# ── H4：拒绝模式扫描 ────────────────────────────────────────────────


async def test_h4_refusal_pattern_hit_marks_once():
    """激活 + refusal_enabled 开：completion_text 命中任一模式 →
    mark_refusal(target_id) 一次即 break。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=False, state=state, chat_logging=False)
    ev = _EvH4(extras={"_quill_activated": True})
    resp = _RespH4("抱歉，我不能继续这个话题")

    await _run_h4(host, ev, resp)

    assert state.refusal_calls == [UMO]


async def test_h4_refusal_disabled_skips_scan():
    """refusal_enabled 关：扫描整体跳过（即使正文含模式，不标记）。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=False, state=state, chat_logging=False,
                       refusal=False)
    ev = _EvH4(extras={"_quill_activated": True})
    resp = _RespH4("我不能继续")

    await _run_h4(host, ev, resp)

    assert state.refusal_calls == []


# ── H4：前置清洗（解密安全网 / 中断标记擦除）────────────────────────


async def test_h4_base64_decrypt_safety_net():
    """[B:...] Base64 解密安全网：completion_text 中的标记被解码为原文
    （encrypt_output 的逆变换），后续剥离路径不再改动。"""
    host = _mk_h4_host(enabled=False)
    ev = _EvH4()
    resp = _RespH4(b64_wrap("机密尾部文本"))

    await _run_h4(host, ev, resp)

    assert resp.completion_text == "机密尾部文本"


async def test_h4_interrupt_system_marker_removed():
    """用户中断系统标记：被 replace 擦除并 strip（中断前的部分输出保留）。"""
    host = _mk_h4_host(enabled=False)
    ev = _EvH4()
    resp = _RespH4("前半段" + _INTERRUPT_MSG + "后半段")

    await _run_h4(host, ev, resp)

    assert resp.completion_text == "前半段后半段"


# ── H4：顶层异常 → error 日志 + 放行（降级语义）─────────────────────


async def test_h4_top_level_exception_degrades(monkeypatch):
    """状态栏链中途抛异常 → 顶层 except 吞掉 + error 日志（「后处理遭遇
    未捕获异常，已降级放行」）→ resp 保持已改到一半的状态放行（不清空、
    不中断框架响应流程）。

    patch 点 QuillPlugin._handle_status_bar（搬移前后 H4 都经
    self/plugin._handle_status_bar 动态分发）；calls 非空防注入假绿。
    """
    calls: list = []

    async def _boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected status-bar failure")

    monkeypatch.setattr(M.QuillPlugin, "_handle_status_bar", _boom)
    rec = _LoggerRecH4()
    monkeypatch.setattr(M, "logger", rec)

    host = _mk_h4_host(enabled=True)
    ev = _EvH4()
    resp = _RespH4("尚未被清空的响应正文")

    await _run_h4(host, ev, resp)   # 不应抛

    assert calls, "异常注入未命中实际调用路径（假绿）"
    # 前置清洗（解密/中断标记）对该文本均为 no-op → 异常点 resp 未被改动
    assert resp.completion_text == "尚未被清空的响应正文"
    assert any("on_llm_response 后处理遭遇未捕获异常" in e for e in rec.errors)


# ═══ F1：SMT 回声置空（M3.0，BASELINE §8.2 F1）═══════════════════════
#
# 现象与根因（BASELINE §8.2 F1）：AstrBot 4.28.x 的 send_message_to_user
# 直接发送消息并把**已发送纯文本**记入
# ``_send_message_to_user_current_session_plain_texts``（message_tools.py
# :349-361，值经 strip()）；respond.stage 以 ``result.get_plain_text()
# .strip()`` 与已发列表做**精确成员匹配**去重（respond/stage.py:189-207）。
# 羽笔流程打破匹配：H2 把状态栏**渲染后**随工具文本发出（列表里是渲染
# 版），H4 把 completion 回声里的原始标记**剥离**（completion 变体）——
# 两者不等 → 框架去重失效 → 用户收到两条（实测间隔 25s）。
#
# 修复设计（PLAN §M3.0）：H4 在状态栏段之后、注入报告段之前，把本轮
# completion 与已发列表做**双向归一**比对（双方各经注入报告行抹除 +
# ``_strip_status_artifacts`` 剥离状态栏变体后比较正文），判定为回声则
# **置空** completion——框架侧不再产出可发文本（runner 对空 completion
# 不 yield llm_result；result_chain 存在时 respond.stage 的
# ``_is_empty_message_chain`` 走空链早退），从根上消除第二条消息。
# 宁漏勿误：已发列表缺失/非 list/正文为空/归一后不等 → 一律放行原路径；
# 判定自身异常也只放行，不影响 H4 其余段落。

# 框架已发记录的 extra 键（message_tools.py:352 逐字）
_SMT_SENT_KEY = "_send_message_to_user_current_session_plain_texts"

# H2 工具路径实际发出的形态：正文 + 渲染后的状态栏（无原始标记）
_SENT_RENDERED = "剧情正文。\n\n" + RENDERED
# 模型回声形态：同正文 + 原始 [LOVE_DATA] 标记（模型照契约输出自己的
# 原始参数，不会带上 H2 渲染后的栏）
_ECHO_RAW = "剧情正文。\n" + DIRTY_LOVE


async def test_f1_echo_of_sent_tool_text_is_cleared():
    """工具已发渲染版 + completion 回声（原始标记变体）→ 判定回声 →
    completion 置空（respond.stage 不再发第二条）；下游 chat_logs / 防双写
    / 拒绝扫描对空文本天然短路。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={
        "_quill_activated": True,
        "_quill_status_handled": True,
        _SMT_SENT_KEY: [_SENT_RENDERED],
    })
    resp = _RespH4(_ECHO_RAW)

    await _run_h4(host, ev, resp)
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert resp.completion_text == ""
    assert host.rag_retriever.log_calls == []     # 空文本不落库
    assert "_quill_assistant_logged" not in ev.extras
    assert state.refusal_calls == []              # 空文本不扫拒绝


async def test_f1_echo_matches_sent_text_with_inject_report_line():
    """已发文本带注入报告行（H2 追加、模型不会回声插件后加的行）→
    归一抹除报告行后命中 → 置空（报告行抹除是确定性行锚正则，非模糊匹配）。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={
        "_quill_activated": True,
        "_quill_status_handled": True,
        _SMT_SENT_KEY: [_SENT_RENDERED + "\n\n〔注入〕世界书×2"],
    })
    resp = _RespH4(_ECHO_RAW)

    await _run_h4(host, ev, resp)

    assert resp.completion_text == ""


async def test_f1_new_reply_not_cleared():
    """已发列表非空 + completion 是**不同内容**的新回复 → 归一后正文不等
    → 不置空（防误杀）：残留标记照常剥离，正文原样保留。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={
        "_quill_activated": True,
        "_quill_status_handled": True,
        _SMT_SENT_KEY: [_SENT_RENDERED],
    })
    resp = _RespH4("全新的后续剧情，与已发内容不同。\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert resp.completion_text == "全新的后续剧情，与已发内容不同。"


async def test_f1_no_sent_texts_pure_text_flow_unchanged():
    """模型直接纯文本输出（未走工具，已发列表 extra 缺失）→ 判定不触发，
    剥离/渲染行为与现状完全一致（六级链照常提取渲染，不置空）。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={"_quill_activated": True})   # 无 _SMT_SENT_KEY
    resp = _RespH4("剧情开头\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert "[LOVE_DATA]" not in resp.completion_text
    assert "───── 状态栏 ─────" in resp.completion_text
    assert "好感度：88/100（爱意）" in resp.completion_text
    assert "剧情开头" in resp.completion_text
    assert state.update_vars_calls                  # 持久化照常


async def test_f1_empty_sent_list_pure_text_flow_unchanged():
    """已发列表为空列表（extra 键存在但工具未发过消息）→ 同上，判定不
    触发，行为与现状完全一致。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={"_quill_activated": True, _SMT_SENT_KEY: []})
    resp = _RespH4("剧情开头\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert "───── 状态栏 ─────" in resp.completion_text
    assert "剧情开头" in resp.completion_text


async def test_f1_sent_list_type_anomaly_passes_through():
    """已发列表类型异常（非 list）：判定跳过、放行原路径——剥离照常、
    不置空、不中断。"""
    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={
        "_quill_activated": True,
        "_quill_status_handled": True,
        _SMT_SENT_KEY: "not-a-list",
    })
    resp = _RespH4("剧情正文。\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert resp.completion_text == "剧情正文。"    # 剥离后原样放行


async def test_f1_echo_check_exception_passes_through(monkeypatch):
    """回声判定自身异常：只放行原路径（剥离结果保留、不置空），不影响
    H4 其余段落、不触发顶层降级。"""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected echo-check failure")

    monkeypatch.setattr(M.QuillPlugin, "_scrub_inject_report", _boom)
    rec = _LoggerRecH4()
    monkeypatch.setattr(M, "logger", rec)
    monkeypatch.setattr(_hooks, "logger", rec)

    state = _StateH4(persona_id="p1")
    host = _mk_h4_host(enabled=True, state=state)
    ev = _EvH4(extras={
        "_quill_activated": True,
        "_quill_status_handled": True,
        _SMT_SENT_KEY: [_SENT_RENDERED],
    })
    resp = _RespH4("剧情正文。\n" + DIRTY_LOVE)

    await _run_h4(host, ev, resp)                 # 不应抛
    for coro in host._spawned:                    # 后台协程显式执行
        await coro

    assert calls, "异常注入未命中实际调用路径（假绿）"
    # 判定失败 → 放行剥离后的原路径文本（不置空）
    assert resp.completion_text == "剧情正文。"
    # 未触发顶层降级（H4 其余段落未被打断）
    assert not any("on_llm_response 后处理遭遇未捕获异常" in e
                   for e in rec.errors)


# ═══ H5：on_llm_tool_respond（第五轮）═══════════════════════════════
#
# H5 是全插件最特殊的钩子（BASELINE §2 H5 行）：**无钩子级顶层 try**——
# 三个早退 gate 之间与记忆存储块之外的异常原样上抛框架；仅"记忆存储调度"
# 与"反思调度"两个内层块各自 warning 吞掉。本节除行为外还钉住两个不对称点
# （BASELINE §2.1）：
#   1. 去重用 `_quill_memorized` 专用标记，**不**把 `_quill_activated` 置
#      False（历史 bug，原 main.py L2746-2757 注释随实现搬移）；
#   2. 反思调度阈值常量（REFLECTION_TURN_THRESHOLD=4 / RECENT_LOG_LIMIT=8 /
#      MIN_LOGS_FOR_SUMMARY=2）的边界行为——常量随下沉迁至
#      quill/services/memory.py（main.py 类属性 re-export），此处按行为钉值。


class _StateH5:
    """state_manager 桩：persona 读取 + 反思轮次计数（increment 返回自增
    后的轮次，与 state.py 语义一致），全部留痕；可注入异常。"""

    def __init__(self, persona_id="p1", count=0):
        self._persona_id = persona_id
        self._count = count
        self.persona_calls: list = []
        self.increment_calls: list = []
        self.reset_calls: list = []
        self.persona_error: Exception | None = None
        self.increment_error: Exception | None = None

    async def get_persona_id(self, tid):
        self.persona_calls.append(tid)
        if self.persona_error:
            raise self.persona_error
        return self._persona_id

    async def increment_unsummarized_turns(self, tid):
        self.increment_calls.append(tid)
        if self.increment_error:
            raise self.increment_error
        self._count += 1
        return self._count

    async def reset_unsummarized_turns(self, tid):
        self.reset_calls.append(tid)
        self._count = 0


class _MemStoreH5:
    """memory_store 桩：最近日志读取/修剪/清理全留痕。"""

    def __init__(self, recent=None):
        self._recent = list(recent or [])
        self.recent_calls: list = []
        self.prune_calls: list = []
        self.cleanup_calls: list = []

    async def get_recent_chat_logs(self, session_id, limit=8):
        self.recent_calls.append((session_id, limit))
        return list(self._recent)

    async def prune_memories(self):
        self.prune_calls.append(True)
        return 0

    async def cleanup_chat_logs(self, retention_days):
        self.cleanup_calls.append(retention_days)
        return 0


class _RagH5:
    """rag_retriever 桩：enable_memory/memory_store 存在性建模 + 落日志/
    总结留痕（summarize_contexts 挂在 retriever 而非 store，与线上一致）。

    memory_store 用哨兵区分「未指定」（补默认桩）与「显式 None」（建模
    memory_store 缺失的外层 gate 分支）。"""

    _DEFAULT = object()

    def __init__(self, enable_memory=True, memory_store=_DEFAULT):
        self.enable_memory = enable_memory
        self.memory_store = _MemStoreH5() if memory_store is _RagH5._DEFAULT \
            else memory_store
        self.log_calls: list = []
        self.summarize_calls: list = []

    async def log_chat_message(self, session_id, role, text):
        self.log_calls.append((session_id, role, text))

    async def summarize_contexts(self, session_id, contexts=None):
        self.summarize_calls.append((session_id, list(contexts or [])))
        return "ok"


class _SpawnToken:
    """_spawn 捕获壳：真实 _spawn 返回 asyncio.Task（add_done_callback 可用）。
    测试不真跑事件循环任务，用本壳承接协程与回调注册——用例内显式 await
    执行协程；阈值路径的 ``sum_task.add_done_callback(...)`` 依赖本壳提供
    该契约（若 _spawn 只返回 None，阈值分支会在回调注册处炸进内层 except，
    prune/cleanup 断言失效）。"""

    def __init__(self, coro):
        self.coro = coro
        self.callbacks: list = []
        self.drained = False

    def add_done_callback(self, cb):
        self.callbacks.append(cb)


class _EvH5(_EvH4):
    """H5 事件桩：复用 _EvH4 的 UMO/extras 建模，补 message_str（H5 以
    ``getattr(event, 'message_str', '')`` 读取，仅落日志侧取值，不落库）。"""

    def __init__(self, extras=None, message_str=""):
        super().__init__(extras=extras)
        self.message_str = message_str


def _smt_tool():
    """send_message_to_user 工具桩（H5 只读 tool.name）。"""
    return types.SimpleNamespace(name="send_message_to_user")


def _mk_h5_host(rag="full", state=None, chat_logging=True, retention=30):
    """轻量 QuillPlugin 宿主（t26 手法）：只挂 H5 触碰的协作对象。

    - rag="full"：enable_memory + memory_store 就绪（记忆/反思全通）；
      "no_memory"：enable_memory=False；"no_store"：memory_store=None；
      None：rag_retriever=None——三种都命中外层存在性 gate 的跳过分支；
    - config 只带 H5 读取的键；retention=None 时**不设**
      rag_chat_log_retention_days（建模 getattr 默认 30 路径）；
    - _get_target_id/_get_memory_session_id 用类上真实现（零实例状态依赖，
      与线上路径一致）；阈值常量同理走 QuillPlugin 类属性；
    - _spawn 捕获协程不调度（_SpawnToken），用例内显式 await 执行。
    """
    h = object.__new__(M.QuillPlugin)
    cfg = {"rag_enable_chat_logging": chat_logging}
    if retention is not None:
        cfg["rag_chat_log_retention_days"] = retention
    h.config = types.SimpleNamespace(**cfg)
    h.state_manager = state if state is not None else _StateH5()
    if rag == "full":
        h.rag_retriever = _RagH5()
    elif rag == "no_memory":
        h.rag_retriever = _RagH5(enable_memory=False)
    elif rag == "no_store":
        h.rag_retriever = _RagH5(memory_store=None)
    else:
        h.rag_retriever = None
    h._spawned: list = []

    def _capture_spawn(coro):
        token = _SpawnToken(coro)
        h._spawned.append(token)
        return token

    h._spawn = _capture_spawn
    return h


async def _run_h5(host, event, tool, tool_args=None, tool_result=None):
    """经注册桩调用（搬移前后都是 QuillPlugin.on_llm_tool_respond）。"""
    await M.QuillPlugin.on_llm_tool_respond(host, event, tool, tool_args, tool_result)


async def _drain_spawned(host):
    """显式执行 _spawn 捕获的后台协程（确定性断言；已执行的 token 跳过，
    同一 host 可重复调用——二轮去重用例靠它避免协程复用）。"""
    for token in host._spawned:
        if not token.drained:
            token.drained = True
            await token.coro


# ── H5：三个早退 gate ────────────────────────────────────────────────


async def test_h5_non_smt_tool_returns_without_touching_state():
    """gate1：非 send_message_to_user 工具直接 return——零状态触碰（不置
    memorized、不落日志、不进反思计数、无后台任务）。"""
    host = _mk_h5_host()
    ev = _EvH5(extras={"_quill_activated": True})
    state, rag = host.state_manager, host.rag_retriever

    await _run_h5(host, ev, types.SimpleNamespace(name="web_search"),
                  {"messages": [{"type": "plain", "text": "工具结果"}]})

    assert "_quill_memorized" not in ev.extras
    assert "_quill_assistant_logged" not in ev.extras
    assert rag.log_calls == []
    assert state.increment_calls == []
    assert host._spawned == []


@pytest.mark.parametrize("activated", [None, False], ids=["missing", "false"])
async def test_h5_not_activated_gate_returns(activated):
    """gate2：`_quill_activated` 缺失/False 都 return——记忆/日志/反思全不
    跑，memorized 也不置位（gate 序在 memorized 之前）。"""
    host = _mk_h5_host()
    extras = {"_quill_activated": activated} if activated is not None else {}
    ev = _EvH5(extras=extras)

    await _run_h5(host, ev, _smt_tool(),
                  {"messages": [{"type": "plain", "text": "工具正文"}]})

    assert "_quill_memorized" not in ev.extras
    assert host.rag_retriever.log_calls == []
    assert host.state_manager.increment_calls == []
    assert host._spawned == []


async def test_h5_memorized_dedup_first_processes_then_skips():
    """gate3：`_quill_memorized` 去重——首轮完整处理（置位 + 落日志 + 反思
    计数），同一 event 二轮早退零重复副作用（agent loop 多次 SMT 调用不
    重复记忆/反思）。"""
    state = _StateH5()
    host = _mk_h5_host(state=state)
    rag = host.rag_retriever
    ev = _EvH5(extras={"_quill_activated": True})
    tool_args = {"messages": [{"type": "plain", "text": "第一段回复"}]}

    await _run_h5(host, ev, _smt_tool(), tool_args)
    await _drain_spawned(host)

    assert ev.extras["_quill_memorized"] is True
    assert rag.log_calls == [(UMO + "::p1", "assistant", "第一段回复")]
    assert state.increment_calls == [UMO]

    await _run_h5(host, ev, _smt_tool(), tool_args)
    await _drain_spawned(host)

    assert rag.log_calls == [(UMO + "::p1", "assistant", "第一段回复")]  # 无第二落
    assert state.increment_calls == [UMO]                                # 无第二计数
    assert len(host._spawned) == 1                                       # 二轮未再 spawn


async def test_h5_activated_not_cleared_after_processing():
    """历史 bug 钉住（BASELINE §2.1，原 main.py L2746-2757 注释）：处理完成
    后 `_quill_activated` **保持 True**——H5 用 `_quill_memorized` 专用标记
    去重，绝不能清总闸门（清了会让同轮后续 SMT 调用绕过状态栏处理与残留
    剥离，裸 [LOVE_DATA] 直达用户）。"""
    host = _mk_h5_host()
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(),
                  {"messages": [{"type": "plain", "text": "回复"}]})
    await _drain_spawned(host)

    assert ev.extras.get("_quill_activated") is True
    assert ev.extras.get("_quill_memorized") is True


# ── H5：助手回复落 chat_logs（防双写，与 H4 互斥）────────────────────


async def test_h5_assistant_reply_logged_with_session_key():
    """tool_args.messages 的 plain 段拼接（每段补 \\n，strip 后）为 AI 回复，
    落 chat_logs（assistant 角色），session 键 = target_id::persona_id；置
    `_quill_assistant_logged` 防双写标记。message_str 不参与落库。"""
    state = _StateH5(persona_id="p9")
    host = _mk_h5_host(state=state)
    ev = _EvH5(extras={"_quill_activated": True},
               message_str="用户输入（不由本钩子落库）")
    tool_args = {"messages": [
        {"type": "plain", "text": "第一段"},
        {"type": "plain", "text": "第二段"},
    ]}

    await _run_h5(host, ev, _smt_tool(), tool_args)
    await _drain_spawned(host)

    assert host.rag_retriever.log_calls == [
        (UMO + "::p9", "assistant", "第一段\n第二段")
    ]
    assert ev.extras.get("_quill_assistant_logged") is True


async def test_h5_json_string_messages_parsed_and_logged():
    """tool_args.messages 为 JSON 字符串（agent loop 常见形态）：先解析再
    提取落库；解析失败降级为空文本（不落库、不上抛）。"""
    host = _mk_h5_host()
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), {
        "messages": json.dumps([{"type": "plain", "text": "字符串形态"}],
                               ensure_ascii=False),
    })
    await _drain_spawned(host)
    assert host.rag_retriever.log_calls == [(UMO + "::p1", "assistant", "字符串形态")]

    ev_bad = _EvH5(extras={"_quill_activated": True})
    await _run_h5(host, ev_bad, _smt_tool(), {"messages": "不是JSON{{{"})
    await _drain_spawned(host)
    assert len(host.rag_retriever.log_calls) == 1          # 坏 JSON 未追加落库
    assert "_quill_assistant_logged" not in ev_bad.extras


async def test_h5_assistant_logged_marker_prevents_double_write():
    """`_quill_assistant_logged` 已置位（H4 直接文本流路径已落库）：H5 不
    重复落——两路径防双写互斥契约（BASELINE §4 写入侧）；反思计数不受影响。"""
    state = _StateH5()
    host = _mk_h5_host(state=state)
    ev = _EvH5(extras={"_quill_activated": True, "_quill_assistant_logged": True})

    await _run_h5(host, ev, _smt_tool(),
                  {"messages": [{"type": "plain", "text": "已被 H4 落库的回复"}]})
    await _drain_spawned(host)

    assert host.rag_retriever.log_calls == []
    assert state.increment_calls == [UMO]


async def test_h5_chat_logging_off_skips_log():
    """`rag_enable_chat_logging=False`（H5 直读 self.config）：不落库、
    **不置**防双写标记（开关判断在标记置位之前）。"""
    host = _mk_h5_host(chat_logging=False)
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(),
                  {"messages": [{"type": "plain", "text": "回复"}]})

    assert host.rag_retriever.log_calls == []
    assert "_quill_assistant_logged" not in ev.extras


@pytest.mark.parametrize("tool_args", [
    None,
    {},
    {"messages": []},
    {"messages": [{"type": "plain", "text": "   "}]},
], ids=["no_args", "empty_dict", "empty_messages", "blank_text"])
async def test_h5_blank_tool_text_not_logged(tool_args):
    """AI 回复为空（无 tool_args / 无 messages / 全空白）：不落库也不置
    防双写标记；记忆块其余部分（反思计数）照常执行。"""
    state = _StateH5()
    host = _mk_h5_host(state=state)
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), tool_args)

    assert host.rag_retriever.log_calls == []
    assert "_quill_assistant_logged" not in ev.extras
    assert state.increment_calls == [UMO]


# ── H5：N 轮反思调度（阈值常量随下沉迁 quill/services/memory.py）──────


async def test_h5_reflection_threshold_reached_triggers_once():
    """第 4 轮（>= REFLECTION_TURN_THRESHOLD=4）：reset 轮次 + 读最近日志
    （limit=RECENT_LOG_LIMIT=8）+ 三个后台任务调度（摘要/修剪/清理）；摘要
    任务注册 done 回调（S1-1 修复：保留 task 引用）。本例工具文本为空，
    隔离出纯反思调度（无落库任务，spawn 序 = 摘要→修剪→清理）。"""
    state = _StateH5(count=3)          # increment → 4 == 阈值
    store = _MemStoreH5(recent=[{"role": "user", "text": f"m{i}"} for i in range(5)])
    host = _mk_h5_host(state=state)
    host.rag_retriever.memory_store = store
    rag = host.rag_retriever
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), {"messages": []})

    assert state.increment_calls == [UMO]
    assert state.reset_calls == [UMO]
    assert store.recent_calls == [(UMO + "::p1", 8)]
    assert len(host._spawned) == 3     # 摘要 + 修剪 + 清理
    await _drain_spawned(host)
    assert rag.summarize_calls == [(UMO + "::p1", store._recent)]
    assert store.prune_calls == [True]
    assert store.cleanup_calls == [30]
    # 摘要任务（首个 spawn）注册了异常记日志的 done 回调
    assert len(host._spawned[0].callbacks) == 1


async def test_h5_reflection_below_threshold_no_trigger():
    """第 3 轮（< 阈值）：只累计——不 reset、不读日志、零后台任务。"""
    state = _StateH5(count=2)          # increment → 3 < 4
    store = _MemStoreH5(recent=[{"text": "x"}])
    host = _mk_h5_host(state=state)
    host.rag_retriever.memory_store = store
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), {"messages": []})

    assert state.increment_calls == [UMO]
    assert state.reset_calls == []
    assert store.recent_calls == []
    assert host._spawned == []


@pytest.mark.parametrize("n_logs,expect_summary", [(1, False), (2, True)],
                         ids=["below_min_1", "at_min_2"])
async def test_h5_min_logs_boundary(n_logs, expect_summary):
    """MIN_LOGS_FOR_SUMMARY=2 边界：最近日志 <2 条不生成摘要，>=2 条生成；
    修剪/清理不受日志条数影响（阈值分支内无条件调度）。"""
    state = _StateH5(count=3)
    store = _MemStoreH5(recent=[{"text": f"m{i}"} for i in range(n_logs)])
    host = _mk_h5_host(state=state)
    host.rag_retriever.memory_store = store
    rag = host.rag_retriever
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), {"messages": []})
    await _drain_spawned(host)

    assert bool(rag.summarize_calls) is expect_summary
    assert store.prune_calls == [True]
    assert store.cleanup_calls == [30]


async def test_h5_cleanup_retention_default_30_when_config_missing():
    """清理保留天数：config 无 rag_chat_log_retention_days 属性时 getattr
    默认 30（原样保真——H5 直读 self.config 而非 props）。"""
    state = _StateH5(count=3)
    store = _MemStoreH5(recent=[{"text": "m0"}, {"text": "m1"}])
    host = _mk_h5_host(state=state, retention=None)
    host.rag_retriever.memory_store = store
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(), {"messages": []})
    await _drain_spawned(host)

    assert store.cleanup_calls == [30]


# ── H5：外层存在性 gate 与内层降级 ───────────────────────────────────


@pytest.mark.parametrize("rag", ["no_memory", "no_store", "none"],
                         ids=["enable_memory_off", "memory_store_none",
                              "retriever_none"])
async def test_h5_memory_gate_skips_storage_but_marks_memorized(rag):
    """外层存在性 gate（retriever/enable_memory/memory_store 任一缺失）：
    跳过记忆存储与反思调度；`_quill_memorized` 已在该 gate 之前置位
    （标记先于 gate——去重不依赖记忆功能开关）。"""
    host = _mk_h5_host(rag=rag)
    ev = _EvH5(extras={"_quill_activated": True})

    await _run_h5(host, ev, _smt_tool(),
                  {"messages": [{"type": "plain", "text": "回复"}]})

    assert ev.extras.get("_quill_memorized") is True
    assert host._spawned == []
    assert host.state_manager.increment_calls == []


async def test_h5_persona_read_failure_swallowed_by_storage_block(caplog):
    """内层块一（记忆存储调度）：get_persona_id 抛异常 → 该块 except
    warning 吞掉（不上抛框架），落库/反思全未执行。"""
    state = _StateH5()
    state.persona_error = RuntimeError("persona 读取失败")
    host = _mk_h5_host(state=state)
    ev = _EvH5(extras={"_quill_activated": True})

    with caplog.at_level(logging.WARNING):
        await _run_h5(host, ev, _smt_tool(),
                      {"messages": [{"type": "plain", "text": "回复"}]})

    assert any("记忆存储调度失败" in r.getMessage() for r in caplog.records)
    assert state.increment_calls == []
    assert host._spawned == []


async def test_h5_reflection_failure_swallowed(caplog):
    """内层块二（反思调度）：increment_unsummarized_turns 抛异常 → 反思块
    自身 except warning 吞掉（不上抛框架；与块一降级相互独立）。"""
    state = _StateH5()
    state.increment_error = RuntimeError("轮次存储损坏")
    host = _mk_h5_host(state=state)
    ev = _EvH5(extras={"_quill_activated": True})

    with caplog.at_level(logging.WARNING):
        await _run_h5(host, ev, _smt_tool(), {"messages": []})

    assert any("反思调度失败" in r.getMessage() for r in caplog.records)
    assert state.reset_calls == []
    assert host._spawned == []


async def test_h5_exception_between_gates_propagates_no_top_try():
    """降级怪癖钉住（BASELINE §2 H5 行，全插件唯一）：**无钩子级顶层 try**
    ——gate 之后、记忆存储 try 之前的异常**原样上抛框架**。本例构造外层
    存在性 gate（rag_retriever.enable_memory）求值异常；防后续"顺手"补
    try 改变降级位置（H1 轮同款用例）。"""
    host = _mk_h5_host()
    state = host.state_manager

    class _BoomRag:
        @property
        def enable_memory(self):
            raise RuntimeError("retriever 状态损坏")
        memory_store = None

    host.rag_retriever = _BoomRag()
    ev = _EvH5(extras={"_quill_activated": True})

    with pytest.raises(RuntimeError, match="retriever 状态损坏"):
        await _run_h5(host, ev, _smt_tool(),
                      {"messages": [{"type": "plain", "text": "回复"}]})

    # 异常点在三个 gate（含 memorized 置位）之后、内层 try 之前
    assert ev.extras.get("_quill_memorized") is True
    assert state.increment_calls == []
# ═══ H3：on_llm_request（第六轮，全插件最大钩子）══════════════════════
#
# H3 是 22 步注入编排（BASELINE §4，顺序即行为）的实现本体。本轮快照按
# 步骤逐面钉住：
#   步 1  SMT 还原（无条件、最先）+ 步 13 改写（仅激活路径、无卡跳过）
#   步 2  垫回（contexts 空/≤1 且开关开 → get_recent_chat_logs 前插 8 条；
#         开关关/已有上下文/retriever 缺失 → 不动）+ 注入报告行抹除
#   步 3  状态栏关闭时清洗历史已渲染状态栏（开启方向不动）
#   步 4  角色卡注入 + [%None] 切断原生人格 + 开场白首插
#   步 5  用户消息落 chat_logs（gate 之前——未激活也落）
#   步 6  核心记忆自然语言（@记住）改写 + 后台写库 + 群聊权限拦截
#   步 7  最近 12 条存 _quill_recent_msgs extra
#   步 8  多轮 context_text 拼接
#   步 9-12  激活 gate（未激活 → reset_quill_rounds + 不注入 +
#         `_quill_activated` 不置位）、worldbook_always_activate 强制、
#         quill_rounds>1 → skip_constants、WR 关键词激活（匹配异常内部吞）
#   步 14-19  emergency/extra_info → build_system_prompt（总闸×分闸）→
#         RAG（doc→mem→core 无条件→format 调用序列）→ 触发日志 →
#         注入报告缓存 → inject_prompt（injection_position 透传）
#   步 20  tail message（开：契约提醒 / 关：禁止文案；幂等；空 prompt）
#   步 21  set_extra("_quill_activated", True)（全部注入成功之后——顺序断言）
#   步 22  update_activity / clear_refusal
#   降级语义（BASELINE §2 H3 行）：顶层 try/except → error 日志
#   （「致命错误，Prompt 装配失败，降级放行」+ _sanitize_extra 脱敏摘要）
#   吞掉放行——该层位在注册桩内保留（预初始化 + _sanitize_extra 随桩）。
#
# 注入点约定（同 H6/H1/H2/H4/H5）：用例一律经注册桩
# ``QuillPlugin.on_llm_request(host, event, req)`` 进入——搬移前逻辑在
# main.py 方法体内，搬移后桩一行委托 interfaces，入口不变，两个世界命中
# 同一用例。既有协作方法（_restore_smt_tool/_check_activation/
# _run_rag_retrieval/_prompt_builder_for_request 等未搬移，仍住 main.py
# 类上，经 plugin/self 动态分发）直接用真实现 + 桩协作对象；搬移体内的
# 方法打点（_rewrite_smt_tool_description/_remember_inject_report/
# _restore_smt_tool）patch QuillPlugin 类属性。error 日志断言 patch main
# 模块 logger（顶层降级日志搬移前后都在 main.py 注册桩）。
# ════════════════════════════════════════════════════════════════════

from astrbot.core.platform.message_type import MessageType

_SMT_ORIG_DESC = "原始的 send_message_to_user 描述。"


class _EvH3(_EvH4):
    """H3 事件桩：复用 _EvH4 的 UMO/extras 建模，补共享顺序留痕（set_extra
    打点——步 21 的 `_quill_activated` 时序断言用）与 get_message_type
    （核心记忆自然语言块的群聊权限校验消费）。"""

    def __init__(self, umo=UMO, extras=None, order=None, message_type="friend"):
        super().__init__(umo=umo, extras=extras)
        self._order = order if order is not None else []
        self._mtype = message_type

    def set_extra(self, key, value):
        self.extras[key] = value
        self._order.append(("set_extra", key))

    def get_message_type(self):
        return MessageType.FRIEND_MESSAGE if self._mtype == "friend" \
            else MessageType.GROUP_MESSAGE


class _StateH3:
    """state_manager 桩：H3 触碰的全部状态面（persona/轮次/emergency/
    会话变量/活动/拒绝），全留痕并打进共享顺序表。"""

    def __init__(self, persona_id="p1", mode="auto", rounds=0, emergency=False,
                 session_vars=None, first_message_injected=False, order=None):
        self._persona_id = persona_id
        self._mode = mode
        self._rounds = rounds
        self._emergency = emergency
        self._vars = dict(session_vars or {"好感度": "88"})
        self._fm_injected = first_message_injected
        self._order = order if order is not None else []
        self.persona_calls: list = []
        self.reset_rounds_calls: list = []
        self.increment_calls: list = []
        self.activity_calls: list = []
        self.refusal_calls: list = []
        self.mark_fm_calls: list = []
        self.get_state_calls: list = []

    async def get_persona_id(self, tid):
        self.persona_calls.append(tid)
        self._order.append("get_persona_id")
        return self._persona_id

    async def get_status_bar_mode(self, tid):
        self._order.append("get_status_bar_mode")
        return self._mode

    async def get_session_vars(self, tid):
        self._order.append("get_session_vars")
        return dict(self._vars)

    async def should_inject_emergency(self, tid):
        self._order.append("should_inject_emergency")
        return self._emergency

    async def reset_quill_rounds(self, uid):
        self.reset_rounds_calls.append(uid)

    async def increment_quill_rounds(self, tid):
        self._order.append("increment_rounds")
        self.increment_calls.append(tid)
        self._rounds += 1
        return self._rounds

    async def update_activity(self, tid):
        self._order.append("update_activity")
        self.activity_calls.append(tid)

    async def clear_refusal(self, tid):
        self._order.append("clear_refusal")
        self.refusal_calls.append(tid)

    async def get_state(self, tid):
        self.get_state_calls.append(tid)
        return types.SimpleNamespace(first_message_injected=self._fm_injected)

    async def mark_first_message_injected(self, tid):
        self.mark_fm_calls.append(tid)


class _DetectorH3:
    """激活检测器桩：should_activate / check_brackets 双打点（步 9 的
    _check_activation 内一次 + 步 9 后直读一次，共两次 check_brackets）。"""

    def __init__(self, activated=False, bracket=False, order=None):
        self._activated = activated
        self._bracket = bracket
        self._order = order if order is not None else []

    def should_activate(self, text):
        self._order.append("should_activate")
        return self._activated

    def check_brackets(self, text):
        self._order.append("check_brackets")
        return self._bracket


class _WRH3:
    """WR 管理器桩：match 异步留痕（可注入异常——步 9 的内部吞语义）。"""

    def __init__(self, matched=None, error=None, order=None):
        self._matched = list(matched if matched is not None else
                             [{"entry_id": "e1", "category": "场景",
                               "match_score": 0.9, "keywords": ["雨"]}])
        self._error = error
        self._order = order if order is not None else []
        self.match_calls: list = []

    async def match(self, context_text, top_k=3, log_match=False):
        self.match_calls.append((context_text, top_k, log_match))
        if self._error is not None:
            raise self._error
        return list(self._matched)


class _MemStoreH3:
    """memory_store 桩：get_recent_chat_logs 留痕（垫回数据源）。"""

    def __init__(self, recent=None, order=None):
        self._recent = list(recent if recent is not None else
                            [{"role": "user", "content": f"历史消息{i}"}
                             for i in range(8)])
        self._order = order if order is not None else []
        self.recent_calls: list = []

    async def get_recent_chat_logs(self, session_id, limit=8):
        self._order.append("recent_logs")
        self.recent_calls.append((session_id, limit))
        return [dict(c) for c in self._recent[:limit]]


class _RagH3:
    """rag_retriever 桩：embedding/memory_store 存在性 + doc/mem/core/
    format 调用序列留痕（步 17 顺序断言）+ 落日志留痕。"""

    def __init__(self, order=None, recent=None, docs=2, mems=1, cores=1,
                 memory_store="default"):
        self.embedding = object()
        self.enable_memory = True
        self._order = order if order is not None else []
        self._docs_n, self._mems_n, self._cores_n = docs, mems, cores
        self.log_calls: list = []
        self.doc_calls: list = []
        self.mem_calls: list = []
        self.core_calls: list = []
        self.format_calls: list = []
        if memory_store == "default":
            self.memory_store = _MemStoreH3(recent=recent, order=self._order)
        else:
            self.memory_store = memory_store      # None 建模缺失

    async def log_chat_message(self, session_id, role, text):
        self.log_calls.append((session_id, role, text))

    async def search_documents(self, user_input, allowed_sources=None):
        self._order.append("search_documents")
        self.doc_calls.append((user_input, allowed_sources))
        return [{"source": "魔女手册", "text": f"段{i}"}
                for i in range(self._docs_n)]

    async def search_memories(self, session_id, user_input):
        self._order.append("search_memories")
        self.mem_calls.append((session_id, user_input))
        return [{"source": "记忆", "text": f"记{i}"}
                for i in range(self._mems_n)]

    async def get_core_memories(self, session_id):
        self._order.append("get_core_memories")
        self.core_calls.append(session_id)
        return [{"text": f"核心{i}"} for i in range(self._cores_n)]

    def format_for_prompt(self, docs, mems, cores):
        self._order.append("format_for_prompt")
        self.format_calls.append((len(docs), len(mems), len(cores)))
        return "RAG-CONTEXT"


class _PersonaMgrH3:
    """persona_manager 桩：get_persona 留痕（返回预设角色卡 dict/None）。"""

    def __init__(self, persona=None, order=None):
        self._persona = persona
        self._order = order if order is not None else []
        self.calls: list = []

    async def get_persona(self, pid):
        self._order.append("get_persona")
        self.calls.append(pid)
        return self._persona


class _WbH3:
    """世界书管理器桩：get_trigger_log 为同步方法（与线上一致，步 18）。"""

    def __init__(self, log=None):
        self._log = list(log or [])

    def get_trigger_log(self):
        return list(self._log)


class _CoreMemStoreH3:
    """rag_memory_store 桩：update_core_memory 留痕（核心记忆后台写库）。"""

    def __init__(self, order=None):
        self._order = order if order is not None else []
        self.update_calls: list = []

    async def update_core_memory(self, session_id, content, content2):
        self._order.append("update_core_memory")
        self.update_calls.append((session_id, content, content2))


# PromptBuilder 桩。_prompt_builder_for_request 会 copy.copy 出浅拷贝实例，
# 方法调用可能落在拷贝上——记录进模块级共享表 _PB3_REC 才能在用例侧观测
# 「实际使用的是哪个实例」（浅拷贝断言的核心）。
_PB3_REC: dict = {}


class _PBH3:
    """PromptBuilder 桩：H3 编排的装配末端（步 15/16/19/20）。

    inject_prompt 按真实现默认序（stable → original → dynamic）拼接，使
    req.system_prompt 断言可读。
    """

    def __init__(self, sb_enabled=False, order=None):
        self.status_bar_enabled = sb_enabled
        self._order = order if order is not None else []

    async def build_system_prompt(self, wr_manager, wb_manager, extra_info=None,
                                  emergency=False, stats=None):
        self._order.append("build_system_prompt")
        _PB3_REC["build"] = {
            "self": self, "wr": wr_manager, "wb": wb_manager,
            "extra": dict(extra_info or {}), "emergency": emergency,
            "stats": stats,
        }
        return "STABLE_PROMPT", "DYNAMIC_PROMPT"

    def build_status_reminder(self):
        self._order.append("build_status_reminder")
        _PB3_REC["reminder_self"] = self
        return "TAIL-REMINDER-MARKER"

    def inject_prompt(self, original_prompt, stable_prompt, dynamic_prompt="",
                      injection_position="system_end"):
        self._order.append("inject_prompt")
        _PB3_REC["inject"] = {
            "self": self, "original": original_prompt, "stable": stable_prompt,
            "dynamic": dynamic_prompt, "injection_position": injection_position,
        }
        parts = [p for p in (stable_prompt, original_prompt, dynamic_prompt) if p]
        return "\n\n".join(parts)


def _persona_h3(**overrides):
    """默认角色卡（master 用）：rag_mode=custom 走 doc 检索路径。"""
    base = {
        "name": "Layla",
        "core_prompts": {},
        "quill_extensions": {"rag_mode": "custom",
                             "bound_rag_docs": ["魔女手册"]},
    }
    base.update(overrides)
    return base


class _ToolsH3:
    """func_tool 桩：含 send_message_to_user 工具（SMT 还原/改写断言面）。"""

    def __init__(self, desc=_SMT_ORIG_DESC):
        self._smt = types.SimpleNamespace(description=desc)

    def empty(self):
        return False

    def get_tool(self, name):
        return self._smt if name == "send_message_to_user" else None


class _ReqH3:
    """ProviderRequest 桩（真机形状：contexts list / prompt str /
    conversation 对象 / system_prompt / func_tool）。"""

    def __init__(self, prompt="你好，今天天气怎么样", contexts=None,
                 system_prompt="", func_tool=None, conversation=None):
        self.prompt = prompt
        self.contexts = list(contexts) if contexts is not None else []
        self.system_prompt = system_prompt
        self.func_tool = func_tool
        self.conversation = conversation


def _mk_h3_host(*, order=None, state=None, persona_id="p1", mode="auto",
                sb_panel=True, activated=True, bracket=False,
                chat_logging=True, always_activate=False,
                worldbook_enabled=True, show_log=False, wr_manager=None,
                wb_manager=None, rounds=0, emergency=False, session_vars=None,
                persona="default", rag="full", pb=None,
                injection_pos="system_end", detector=None):
    """轻量 QuillPlugin 宿主（t26 手法）：只挂 H3 触碰的协作对象。

    默认值对齐 master 快通路（激活路径 + 状态栏开 + doc 检索 custom 模式）；
    - persona="default"：_persona_h3() 默认卡；None → persona_manager 桩
      返回 None（无卡路径）；其余值原样作为角色卡 dict；
    - rag="full"：retriever 就绪；"no_store"：memory_store=None；
      "no_retriever"：rag_retriever=None；
    - _get_target_id/_get_memory_session_id/_check_activation/_run_rag_retrieval/
      _inject_persona_and_first_message/_prompt_builder_for_request/
      _restore_smt_tool/_rewrite_smt_tool_description/_remember_inject_report/
      _scrub_inject_report 均用 QuillPlugin 类上真实现（与线上动态分发路径
      一致）；
    - _spawn 捕获协程不调度（_SpawnToken），用例内 _drain_spawned 执行。
    """
    h = object.__new__(M.QuillPlugin)
    order = order if order is not None else []
    h.props = types.SimpleNamespace(
        love_fields=list(M._DEFAULT_LOVE_FIELDS_RAW),
        status_bar_enabled=sb_panel,
        wr_max_entries=12,
        wr_fallback_top_count=3,
        wb_max_entries=5,
        debug=False,
        prompt_builder=pb if pb is not None else _PBH3(sb_enabled=sb_panel,
                                                       order=order),
    )
    # Mixin 裸读 self.<attr> 的双建模路径（_effective_status_bar_enabled）
    h.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    h.status_bar_enabled = sb_panel
    h.status_bar_default_placeholder = "未设置"
    h.config = types.SimpleNamespace(
        rag_enable_chat_logging=chat_logging,
        worldbook_always_activate=always_activate,
        worldbook_enabled=worldbook_enabled,
        worldbook_show_log=show_log,
        worldbook_injection_pos=injection_pos,
        worldbook_sensitivity=60,
        worldbook_max_token=2000,
    )
    h.state_manager = state if state is not None else _StateH3(
        persona_id=persona_id, mode=mode, rounds=rounds, emergency=emergency,
        session_vars=session_vars, order=order)
    h.activation_detector = detector if detector is not None else _DetectorH3(
        activated=activated, bracket=bracket, order=order)
    h.wr_manager = wr_manager
    h.wb_manager = wb_manager
    if persona == "default":
        persona = _persona_h3()
    h.persona_manager = _PersonaMgrH3(persona=persona, order=order)
    if rag == "full":
        h.rag_retriever = _RagH3(order=order)
    elif rag == "no_store":
        h.rag_retriever = _RagH3(order=order, memory_store=None)
    else:
        h.rag_retriever = None
    h.rag_memory_store = _CoreMemStoreH3(order=order)
    h.health_tracker = M.HealthTracker()
    h._spawned: list = []

    def _capture_spawn(coro):
        token = _SpawnToken(coro)
        h._spawned.append(token)
        return token

    h._spawn = _capture_spawn
    return h


async def _run_h3(host, event, req):
    """经注册桩调用（搬移前后都是 QuillPlugin.on_llm_request）。"""
    await M.QuillPlugin.on_llm_request(host, event, req)


# H3 master 全序（与 BASELINE §4 的 22 步一一对应；重复出现的打点为
# 真实现内的多次调用：get_persona_id ×3、check_brackets ×2）
_H3_MASTER_ORDER = [
    "restore_smt",                        # 步 1（patch 打点）
    "get_persona_id",                     # 步 2 前置：mem_session_id
    "recent_logs",                        # 步 2 垫回
    "get_status_bar_mode",                # 步 3 前置：_sb_effective
    "get_persona_id",                     # 步 4 _inject_persona_and_first_message
    "get_persona",                        # 步 4
    ("set_extra", "_quill_recent_msgs"),  # 步 7
    "should_activate",                    # 步 9 _check_activation
    "check_brackets",
    "check_brackets",                     # 步 9 后直读
    "increment_rounds",                   # 步 12
    "rewrite_smt",                        # 步 13（patch 打点）
    "should_inject_emergency",            # 步 14
    "get_session_vars",                   # 步 14
    "build_system_prompt",                # 步 16（世界书+WR 注入点）
    "get_persona_id",                     # 步 17 _run_rag_retrieval 内部
    "search_documents",                   # 步 17（doc 检索）
    "search_memories",                    # 步 17（memory 检索）
    "get_core_memories",                  # 步 17（核心记忆无条件）
    "format_for_prompt",                  # 步 17
    "remember_report",                    # 步 17 后（patch 打点）
    "inject_prompt",                      # 步 19
    "build_status_reminder",              # 步 20（tail 契约提醒）
    ("set_extra", "_quill_activated"),    # 步 21（注入全部成功之后）
    "update_activity",                    # 步 22
    "clear_refusal",                      # 步 22
]


async def test_h3_master_full_sequence_happy_path(monkeypatch):
    """master 快通路：激活路径 + 状态栏开 + custom doc 检索——22 步全序
    断言（BASELINE §4 顺序即行为）+ 终态断言。

    `_quill_activated` 的时序单独断言：严格晚于全部注入步（format_for_prompt
    / inject_prompt / build_status_reminder）、严格早于 update_activity。
    """
    order: list = []
    state = _StateH3(persona_id="p1", mode="auto", order=order)
    wb_sentinel = _WbH3()
    host = _mk_h3_host(order=order, state=state, sb_panel=True,
                       activated=True, wb_manager=wb_sentinel)
    ev = _EvH3(order=order)
    req = _ReqH3(func_tool=_ToolsH3(),
                 conversation=types.SimpleNamespace(persona_id="orig-persona"))

    # 打点搬移体内方法调用（patch QuillPlugin 类属性——搬移前后都是
    # self/plugin 动态分发，同一 patch 点；其余打点由桩协作对象承担）
    orig_restore = M.QuillPlugin._restore_smt_tool
    orig_rewrite = M.QuillPlugin._rewrite_smt_tool_description
    orig_remember = M.QuillPlugin._remember_inject_report

    def _restore_spy(hst, r):
        order.append("restore_smt")
        return orig_restore(hst, r)

    async def _rewrite_spy(hst, r, pid=""):
        order.append("rewrite_smt")
        return await orig_rewrite(hst, r, pid)

    def _remember_spy(hst, tid, stats):
        order.append("remember_report")
        return orig_remember(hst, tid, stats)

    monkeypatch.setattr(M.QuillPlugin, "_restore_smt_tool", _restore_spy)
    monkeypatch.setattr(M.QuillPlugin, "_rewrite_smt_tool_description",
                        _rewrite_spy)
    monkeypatch.setattr(M.QuillPlugin, "_remember_inject_report", _remember_spy)

    await _run_h3(host, ev, req)
    await _drain_spawned(host)

    assert order == _H3_MASTER_ORDER, f"22 步顺序漂移:\n{order}"

    # 步 21 时序（显式断言，防后续在注入前置位闸门）
    i_activated = order.index(("set_extra", "_quill_activated"))
    assert i_activated > order.index("format_for_prompt")
    assert i_activated > order.index("inject_prompt")
    assert i_activated > order.index("build_status_reminder")
    assert i_activated < order.index("update_activity")

    # 步 2：垫回 8 条 + 会话键 target_id::persona_id
    assert host.rag_retriever.memory_store.recent_calls == [(UMO + "::p1", 8)]
    assert len(req.contexts) == 8
    assert req.contexts[0] == {"role": "user", "content": "历史消息0"}
    # 步 4：切断原生人格
    assert req.conversation.persona_id == "[%None]"
    # 步 7：最近 12 条 extra（8 条垫回全保留）
    assert ev.extras.get("_quill_recent_msgs") == [
        {"role": "user", "content": f"历史消息{i}"} for i in range(8)]
    # 步 12/14：extra_info 形状（skip_constants/映射自 props+config）；
    # emergency 是 build_system_prompt 的独立 kwargs（不在 extra_info 内）
    build = _PB3_REC["build"]
    assert build["extra"]["skip_constants"] is False
    assert build["emergency"] is False
    assert build["extra"]["persona_id"] == "p1"
    assert build["extra"]["user_id"] == UMO
    assert build["extra"]["session_vars"] == {"好感度": "88"}
    assert build["extra"]["wr_max_entries"] == 12
    assert build["extra"]["wr_fallback_top_count"] == 3
    assert build["extra"]["wb_max_entries"] == 5
    assert build["extra"]["wb_sensitivity"] == 60
    assert build["extra"]["wb_max_token"] == 2000
    # 步 13：SMT 改写（原描述存 req 对象属性，防并发覆盖；改写文本不含
    # marker——marker 是「系统强制描述」的识别键，只用于还原判定）
    tool = req.func_tool.get_tool("send_message_to_user")
    assert "THIS IS THE ONLY TOOL" in tool.description
    assert getattr(req, M.QuillPlugin._QUILL_ORIG_DESC_KEY) == _SMT_ORIG_DESC
    # 步 16：世界书总闸开 → 真实 wb_manager 传入；WR 侧 None
    assert build["wr"] is None
    assert build["wb"] is wb_sentinel
    # 步 17：RAG 调用序列参数
    rag = host.rag_retriever
    assert rag.doc_calls == [("你好，今天天气怎么样", ["魔女手册"])]
    assert rag.mem_calls == [(UMO + "::p1", "你好，今天天气怎么样")]
    assert rag.core_calls == [UMO + "::p1"]
    assert rag.format_calls == [(2, 1, 1)]
    # 注入报告缓存（步 17 stats 回填 + 步 17 后缓存合并标准键）
    assert host._get_inject_report(UMO) == {
        "wb": 0, "mem": 1, "wr": 0, "doc": 2, "core_mem": 1,
        "doc_sources": ["魔女手册"],
    }
    # 步 19：inject_prompt 透传（original 为原 system_prompt，此处空）
    inject = _PB3_REC["inject"]
    assert inject["stable"] == "STABLE_PROMPT"
    assert inject["dynamic"] == "DYNAMIC_PROMPT\n\nRAG-CONTEXT"
    assert inject["injection_position"] == "system_end"
    assert req.system_prompt == "STABLE_PROMPT\n\nDYNAMIC_PROMPT\n\nRAG-CONTEXT"
    # 步 20：tail 契约提醒追加
    assert req.prompt.endswith("\n\n[System] TAIL-REMINDER-MARKER")
    # 步 22
    assert state.activity_calls == [UMO]
    assert state.refusal_calls == [UMO]
    # 步 5：用户消息落 chat_logs（后台任务，drain 后可见）
    assert rag.log_calls == [(UMO + "::p1", "user", "你好，今天天气怎么样")]


async def test_h3_smt_restore_first_and_actually_restores(monkeypatch):
    """步 1：SMT 还原**先于一切**（打点序首）且真实生效——上一轮改写的
    描述（含 marker）从 req 对象属性恢复原状并删属性。

    用未激活路径（gate 后 return）隔离还原效果：还原之后、改写之前没有任何
    别的步骤会再碰工具描述，终态即还原结果。"""
    order: list = []
    orig_restore = M.QuillPlugin._restore_smt_tool

    def _restore_spy(hst, r):
        order.append("restore_smt")
        return orig_restore(hst, r)

    monkeypatch.setattr(M.QuillPlugin, "_restore_smt_tool", _restore_spy)

    host = _mk_h3_host(order=order, activated=False)
    ev = _EvH3(order=order)
    tool = _ToolsH3(desc=M.QuillPlugin._QUILL_SMT_DESC_MARKER + "（改写版）")
    req = _ReqH3(func_tool=tool)
    setattr(req, M.QuillPlugin._QUILL_ORIG_DESC_KEY, _SMT_ORIG_DESC)

    await _run_h3(host, ev, req)

    assert order[0] == "restore_smt", "SMT 还原必须先于一切"
    # 还原真实生效（防打点假绿）
    smt = req.func_tool.get_tool("send_message_to_user")
    assert smt.description == _SMT_ORIG_DESC
    assert not hasattr(req, M.QuillPlugin._QUILL_ORIG_DESC_KEY)


async def test_h3_smt_rewrite_skipped_without_persona():
    """步 13 无卡守卫：persona_id 空 → SMT 不改写（防 Agent 死循环）、
    tail message 不追加（`if persona_id:` 整段跳过）。"""
    order: list = []
    state = _StateH3(persona_id="", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state, persona_id="")
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="普通消息", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    tool = req.func_tool.get_tool("send_message_to_user")
    assert tool.description == _SMT_ORIG_DESC          # 未改写
    assert not hasattr(req, M.QuillPlugin._QUILL_ORIG_DESC_KEY)
    assert req.prompt == "普通消息"                     # tail 未追加


async def test_h3_context_restoration_prepends_recent_logs():
    """步 2：contexts 空 + 开关开（默认）+ retriever/store 就绪 →
    get_recent_chat_logs(mem_session_id, limit=8) 结果**前插**。"""
    host = _mk_h3_host()
    ev = _EvH3()
    req = _ReqH3(contexts=[])

    await _run_h3(host, ev, req)

    assert host.rag_retriever.memory_store.recent_calls == [(UMO + "::p1", 8)]
    assert [c["content"] for c in req.contexts] == [f"历史消息{i}" for i in range(8)]


async def test_h3_context_restoration_single_context_counts_as_fresh():
    """步 2 边界：contexts 恰 1 条也算 fresh（`<= 1`）→ 照样垫回（8+1）。"""
    host = _mk_h3_host()
    ev = _EvH3()
    req = _ReqH3(contexts=[{"role": "user", "content": "仅存的一条"}])

    await _run_h3(host, ev, req)

    assert len(req.contexts) == 9
    assert req.contexts[-1] == {"role": "user", "content": "仅存的一条"}


@pytest.mark.parametrize("kwargs,ctx", [
    ({"rag": "full", "chat_logging": False},
     [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]),
    ({"rag": "no_store"},
     [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]),
    ({"rag": "no_retriever"},
     [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]),
], ids=["logging_off", "no_store", "no_retriever"])
async def test_h3_context_restoration_gates(kwargs, ctx):
    """步 2 闸门：开关关 / memory_store 缺失 / retriever 缺失 → 不读日志、
    req.contexts 原样（已有上下文的分支由 direct 断言覆盖）。"""
    host = _mk_h3_host(**kwargs)
    ev = _EvH3()
    req = _ReqH3(contexts=[dict(c) for c in ctx])

    await _run_h3(host, ev, req)

    if host.rag_retriever is not None and host.rag_retriever.memory_store:
        assert host.rag_retriever.memory_store.recent_calls == []
    assert req.contexts == ctx


async def test_h3_context_restoration_skipped_when_has_contexts():
    """步 2 闸门（已有上下文）：≥2 条 → 视为非 fresh，不读日志、原样。"""
    host = _mk_h3_host()
    ev = _EvH3()
    ctx = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    req = _ReqH3(contexts=[dict(c) for c in ctx])

    await _run_h3(host, ev, req)

    assert host.rag_retriever.memory_store.recent_calls == []
    assert req.contexts == ctx


async def test_h3_inject_report_scrubbed_from_history():
    """步 2 前置：历史 contexts 中的〔注入〕报告行被抹除（防模型模仿回显）；
    非 dict 项与非 str content 原样保留。"""
    host = _mk_h3_host(chat_logging=False)
    ev = _EvH3()
    raw = {"role": "user", "content": 123}
    req = _ReqH3(contexts=[
        {"role": "assistant",
         "content": "第一轮回复\n\n〔注入〕世界书×2\n\n\n\n第二轮"},
        raw,
        "裸字符串项",
    ])

    await _run_h3(host, ev, req)

    assert req.contexts[0]["content"] == "第一轮回复\n\n第二轮"
    assert req.contexts[1] is raw
    assert req.contexts[2] == "裸字符串项"


async def test_h3_status_bar_off_scrubs_history_artifacts():
    """步 3：状态栏关闭 → 历史 contexts 中已渲染栏被整套剥离（恢复来的
    chat_logs 同样携带状态栏，必须在垫回之后清）；开启方向历史不动。"""
    bar_ctx = [{"role": "assistant", "content": "前文\n" + RENDERED}]

    # 关闭（面板 off + 会话 auto）→ 清洗
    host = _mk_h3_host(sb_panel=False, chat_logging=False)
    ev = _EvH3()
    req = _ReqH3(contexts=[dict(c) for c in bar_ctx])
    await _run_h3(host, ev, req)
    assert "状态栏" not in req.contexts[0]["content"]
    assert "好感度" not in req.contexts[0]["content"]
    assert req.contexts[0]["content"].startswith("前文")

    # 开启 → 原样保留（渲染产物是合法历史）
    host_on = _mk_h3_host(sb_panel=True, chat_logging=False)
    ev_on = _EvH3()
    req_on = _ReqH3(contexts=[dict(c) for c in bar_ctx])
    await _run_h3(host_on, ev_on, req_on)
    assert req_on.contexts[0]["content"] == "前文\n" + RENDERED


async def test_h3_not_activated_resets_rounds_and_returns_untouched():
    """步 11 gate：未激活（无激活词/无括号/WR 未匹配）→ reset_quill_rounds
    + return——不注入（system_prompt/prompt 不变）、SMT 不改写、`_quill_
    activated` 不置位、increment 不跑。**步 5 的用户消息落库照常发生**
    （在 gate 之前——断点续传语义）。"""
    order: list = []
    state = _StateH3(persona_id="p1", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state, activated=False)
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="普通闲聊", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)
    await _drain_spawned(host)

    assert state.reset_rounds_calls == [UMO]
    assert state.increment_calls == []
    assert "_quill_activated" not in ev.extras
    assert req.system_prompt == ""
    assert req.prompt == "普通闲聊"
    tool = req.func_tool.get_tool("send_message_to_user")
    assert tool.description == _SMT_ORIG_DESC
    assert "build_system_prompt" not in order
    assert "increment_rounds" not in order
    # gate 之前的落库照常
    assert host.rag_retriever.log_calls == [(UMO + "::p1", "user", "普通闲聊")]


async def test_h3_worldbook_always_activate_forces_activation():
    """步 10：worldbook_always_activate=True 强制 activated=True 且
    wr_activated=False——即使 WR 管理器与 debug 全开也不触发 WR 调试匹配，
    gate 照常通过（不 reset）。"""
    order: list = []
    wr = _WRH3(order=order)
    state = _StateH3(persona_id="p1", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state, activated=False,
                       always_activate=True, wr_manager=wr)
    host.props.debug = True
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="普通闲聊", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert state.reset_rounds_calls == []
    assert state.increment_calls == [UMO]
    assert ev.extras.get("_quill_activated") is True
    # wr_activated 被强制 False → WR 调试匹配（步 13 后的 debug 块）不触发
    assert wr.match_calls == []
    assert "build_system_prompt" in order


async def test_h3_wr_keyword_activation():
    """步 9 WR 关键词路径：激活词/括号未命中 → wr_manager.match(context_text,
    top_k=3) 命中 → wr_activated=True 进激活路径；context_text 含多轮拼接
    （末 4 条 + 本轮）。"""
    order: list = []
    wr = _WRH3(order=order)
    state = _StateH3(persona_id="p1", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state, activated=False,
                       wr_manager=wr,
                       persona=_persona_h3(quill_extensions={
                           "wr_mode": "auto", "rag_mode": "disabled"}))
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="外面下雨了", contexts=[
        {"role": "user", "content": "前一条"},
        {"role": "assistant", "content": "前一条回复"},
        {"role": "user", "content": "再前一条"},
        {"role": "assistant", "content": "再前一条回复"},
        {"role": "user", "content": "更早（截出窗口）"},
    ], func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert state.reset_rounds_calls == []
    assert state.increment_calls == [UMO]
    # _check_activation 的匹配：top_k=3（无绑定分类）、context_text = 末 4 条 + 本轮
    assert len(wr.match_calls) == 1
    ctx_text, top_k, log_match = wr.match_calls[0]
    assert top_k == 3 and log_match is False
    # 步 8 多轮拼接：末 4 条（「前一条」被截出窗口）+ 本轮在首
    assert ctx_text.split("\n") == [
        "外面下雨了", "前一条回复", "再前一条", "再前一条回复", "更早（截出窗口）"]


async def test_h3_wr_match_failure_swallowed_gate_return():
    """步 9 怪癖（BASELINE §4 步 9：WR 匹配异常内部吞）：match 抛异常 →
    _check_activation 内 warning 吞掉 → wr_activated=False → gate →
    reset_quill_rounds + return（不上抛、不降级日志）。"""
    order: list = []
    wr = _WRH3(error=RuntimeError("WR 索引损坏"), order=order)
    state = _StateH3(persona_id="p1", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state, activated=False,
                       wr_manager=wr,
                       persona=_persona_h3(quill_extensions={
                           "wr_mode": "auto", "rag_mode": "disabled"}))
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="外面下雨了")

    await _run_h3(host, ev, req)   # 不应抛

    assert state.reset_rounds_calls == [UMO]
    assert state.increment_calls == []
    assert "_quill_activated" not in ev.extras


async def test_h3_skip_constants_flag_after_first_round():
    """步 12：quill_rounds>1 → skip_constants 置位进 extra_info（跳过
    Layer 1 常驻的信号）；计数走 increment 返回值。"""
    order: list = []
    state = _StateH3(persona_id="p1", mode="auto", rounds=1, order=order)
    host = _mk_h3_host(order=order, state=state)
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="继续", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert state.increment_calls == [UMO]
    assert _PB3_REC["build"]["extra"]["skip_constants"] is True


def _pb_copy_host(mode):
    """浅拷贝断言用宿主：面板关 + 会话级 mode（on=强制开）。"""
    order: list = []
    pb = _PBH3(sb_enabled=False, order=order)
    state = _StateH3(persona_id="p1", mode=mode, order=order)
    host = _mk_h3_host(order=order, state=state, sb_panel=False, pb=pb)
    return host, pb, _EvH3(order=order)


async def test_h3_prompt_builder_shallow_copy_on_session_override():
    """步 15：`_prompt_builder_for_request` 浅拷贝——会话级覆盖（on）与
    面板全局（False）不一致时，build_system_prompt / build_status_reminder
    用**拷贝实例**（开关翻正），共享实例的开关**绝不落回**（并发防污染）。"""
    host, pb, ev = _pb_copy_host("on")
    req = _ReqH3(prompt="继续", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    build = _PB3_REC["build"]
    used = build["self"]
    assert used is not pb                      # 用的是浅拷贝
    assert used.status_bar_enabled is True     # 拷贝上的开关已对齐会话覆盖
    assert pb.status_bar_enabled is False      # 共享实例未被污染
    assert _PB3_REC["reminder_self"] is used   # tail 与装配用同一拷贝


async def test_h3_prompt_builder_same_object_when_aligned():
    """步 15 对齐分支：开关一致（面板开 + auto 跟随）→ 直接用共享实例
    （不拷贝）。"""
    order: list = []
    pb = _PBH3(sb_enabled=True, order=order)
    host = _mk_h3_host(order=order, sb_panel=True, pb=pb)
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="继续", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert _PB3_REC["build"]["self"] is pb


async def test_h3_rag_disabled_mode_skips_doc_but_memory_core_run():
    """步 17：rag_mode=disabled → 跳过 doc 检索；memory 检索与**核心记忆
    无条件注入**照常；stats 相应为 0（注入报告可分辨「没命中」与「没生效」）。"""
    host = _mk_h3_host(
        persona=_persona_h3(quill_extensions={"rag_mode": "disabled"}))
    ev = _EvH3()
    req = _ReqH3(prompt="继续", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    rag = host.rag_retriever
    assert rag.doc_calls == []
    assert len(rag.mem_calls) == 1
    assert len(rag.core_calls) == 1
    assert rag.format_calls == [(0, 1, 1)]
    report = host._get_inject_report(UMO)
    assert report["doc"] == 0 and report["doc_sources"] == []
    assert report["mem"] == 1 and report["core_mem"] == 1


async def test_h3_rag_retriever_missing_degrades_gracefully():
    """步 17 缺 retriever：`_run_rag_retrieval` 提前 return（warning），
    编排继续——build_system_prompt 照跑、inject_prompt 的 dynamic 无 RAG
    上下文；垫回与落库一并跳过（同一存在性判断面）。"""
    order: list = []
    host = _mk_h3_host(order=order, rag="no_retriever")
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="继续", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert "build_system_prompt" in order
    assert _PB3_REC["inject"]["dynamic"] == "DYNAMIC_PROMPT"
    assert req.system_prompt == "STABLE_PROMPT\n\nDYNAMIC_PROMPT"
    assert ev.extras.get("_quill_activated") is True


async def test_h3_trigger_log_appended_when_enabled():
    """步 18：worldbook_show_log 开 + wb 有 get_trigger_log → 触发日志块
    追加进 dynamic_prompt（同步方法，不 await）。"""
    order: list = []
    wb = _WbH3(log=[{"worldbook": "witchi", "title": "暴雨",
                     "matched_keys": ["雨", "雷"]}])
    host = _mk_h3_host(order=order, show_log=True, wb_manager=wb)
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="外面下雨了", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    dynamic = _PB3_REC["inject"]["dynamic"]
    assert dynamic.startswith("DYNAMIC_PROMPT\n\nRAG-CONTEXT\n\n[触发日志]")
    assert "witchi/暴雨 ← 雨,雷" in dynamic


async def test_h3_worldbook_dead_switch_forces_none_manager():
    """步 16 死开关接线：worldbook_enabled=False → wb_manager 传 None
    （PromptBuilder 跳过全部世界书逻辑——「改了不生效」死开关的唯一消费点）；
    injection_position 原样透传 inject_prompt。"""
    order: list = []
    wb = _WbH3(log=[{"worldbook": "witchi", "title": "暴雨",
                     "matched_keys": ["雨"]}])
    host = _mk_h3_host(order=order, worldbook_enabled=False, wb_manager=wb,
                       injection_pos="user_prefix")
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="外面下雨了", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)

    assert _PB3_REC["build"]["wb"] is None
    assert _PB3_REC["inject"]["injection_position"] == "user_prefix"
    # 总闸关 → 触发日志块同样跳过（_wb_for_request 为 None）
    assert "[触发日志]" not in _PB3_REC["inject"]["dynamic"]


async def test_h3_tail_message_variants_and_idempotence():
    """步 20：开启 → 契约提醒 tail；关闭 → 禁止状态栏文案 tail；空 prompt
    → 整体成为 prompt；已含 tail → 不重复；无 persona → 整段跳过。"""
    # 开启
    host = _mk_h3_host(sb_panel=True)
    ev = _EvH3()
    req = _ReqH3(prompt="剧情正文", func_tool=_ToolsH3())
    await _run_h3(host, ev, req)
    assert req.prompt == "剧情正文\n\n[System] TAIL-REMINDER-MARKER"

    # 已含 tail → 不重复追加
    host2 = _mk_h3_host(sb_panel=True)
    ev2 = _EvH3()
    req2 = _ReqH3(prompt="剧情正文\n\n[System] TAIL-REMINDER-MARKER",
                  func_tool=_ToolsH3())
    await _run_h3(host2, ev2, req2)
    assert req2.prompt == "剧情正文\n\n[System] TAIL-REMINDER-MARKER"

    # 空 prompt → tail 即全部
    host3 = _mk_h3_host(sb_panel=True)
    ev3 = _EvH3()
    req3 = _ReqH3(prompt="", func_tool=_ToolsH3())
    await _run_h3(host3, ev3, req3)
    assert req3.prompt == "\n\n[System] TAIL-REMINDER-MARKER"

    # 关闭 → 禁止文案
    host4 = _mk_h3_host(sb_panel=False, chat_logging=False)
    ev4 = _EvH3()
    req4 = _ReqH3(prompt="剧情正文", func_tool=_ToolsH3())
    await _run_h3(host4, ev4, req4)
    assert "禁止输出任何格式的状态栏" in req4.prompt
    assert "[LOVE_DATA]" in req4.prompt


async def test_h3_user_message_logging_gates():
    """步 5：用户消息落 chat_logs 的三重闸门（persona 绑定 / 非斜杠指令 /
    rag_enable_chat_logging）——各自缺一即不落。

    注意 persona_id 来自 state_manager（会话绑定的卡 id），不是
    persona_manager 的返回值——persona_manager 查不到卡只影响 persona_data，
    不影响落库闸门（未绑会话用 persona_id="" 建模）。"""
    # 斜杠指令不落
    host = _mk_h3_host()
    ev = _EvH3()
    req = _ReqH3(prompt="/help", func_tool=_ToolsH3())
    await _run_h3(host, ev, req)
    await _drain_spawned(host)
    assert host.rag_retriever.log_calls == []

    # persona 未绑定（state 会话无卡）不落，session 键退化为 target_id
    order: list = []
    state = _StateH3(persona_id="", mode="auto", order=order)
    host2 = _mk_h3_host(order=order, state=state, persona_id="")
    ev2 = _EvH3(order=order)
    req2 = _ReqH3(prompt="你好", func_tool=_ToolsH3())
    await _run_h3(host2, ev2, req2)
    await _drain_spawned(host2)
    assert host2.rag_retriever.log_calls == []

    # 开关关不落
    host3 = _mk_h3_host(chat_logging=False)
    ev3 = _EvH3()
    req3 = _ReqH3(prompt="你好", func_tool=_ToolsH3())
    await _run_h3(host3, ev3, req3)
    await _drain_spawned(host3)
    assert host3.rag_retriever.log_calls == []

    # persona_manager 查不到卡（persona_data=None）不影响落库闸门
    host4 = _mk_h3_host(persona=None)
    ev4 = _EvH3()
    req4 = _ReqH3(prompt="你好", func_tool=_ToolsH3())
    await _run_h3(host4, ev4, req4)
    await _drain_spawned(host4)
    assert host4.rag_retriever.log_calls == [(UMO + "::p1", "user", "你好")]


async def test_h3_core_memory_nl_rewrites_prompt_and_stores():
    """步 6：`@记住：` 前缀 → 捕获组为核心记忆内容 + 后台写库（session 键
    = target_id::persona_id）。prompt 改写为匹配**之后**的剩余文本——
    单行输入剩余为空 → prompt 保持原文（不 rewrite 出空 prompt）；多行
    输入第二行起成为新 prompt。落库内容始终按原文 user_input。"""
    # 多行：改写生效
    host = _mk_h3_host()
    ev = _EvH3()
    req = _ReqH3(prompt="@记住：她喜欢茉莉花茶\n今天天气如何",
                 func_tool=_ToolsH3())
    await _run_h3(host, ev, req)
    await _drain_spawned(host)
    assert req.prompt == "今天天气如何\n\n[System] TAIL-REMINDER-MARKER"
    assert host.rag_memory_store.update_calls == [
        (UMO + "::p1", "她喜欢茉莉花茶", "她喜欢茉莉花茶")]
    # 落库按**原文**（改写前读取的 user_input）
    assert host.rag_retriever.log_calls == [
        (UMO + "::p1", "user", "@记住：她喜欢茉莉花茶\n今天天气如何")]

    # 单行：剩余为空 → prompt 不改写（避免空 prompt 发给 LLM），写库照常
    host2 = _mk_h3_host()
    ev2 = _EvH3()
    req2 = _ReqH3(prompt="@记住：她喜欢茉莉花茶", func_tool=_ToolsH3())
    await _run_h3(host2, ev2, req2)
    await _drain_spawned(host2)
    assert req2.prompt == "@记住：她喜欢茉莉花茶\n\n[System] TAIL-REMINDER-MARKER"
    assert host2.rag_memory_store.update_calls == [
        (UMO + "::p1", "她喜欢茉莉花茶", "她喜欢茉莉花茶")]


async def test_h3_core_memory_nl_permission_denied_group():
    """步 6 权限面：群聊 + admin_users 未配置（fail-close）→ 不改写
    prompt、不写核心记忆（自然语言注入被拦截；tail 仍照常追加）。"""
    host = _mk_h3_host()
    ev = _EvH3(message_type="group")
    req = _ReqH3(prompt="记住：她喜欢茉莉花茶", func_tool=_ToolsH3())

    await _run_h3(host, ev, req)
    await _drain_spawned(host)

    assert req.prompt == "记住：她喜欢茉莉花茶\n\n[System] TAIL-REMINDER-MARKER"
    assert host.rag_memory_store.update_calls == []


class _LoggerRecH3:
    """logger 替身：error 分级留痕（顶层降级日志断言）。"""

    def __init__(self):
        self.errors: list = []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def warning(self, msg, *a, **k):
        pass

    def info(self, msg, *a, **k):
        pass

    def debug(self, msg, *a, **k):
        pass


@pytest.mark.parametrize("inject_at", ["restore", "check_activation"],
                         ids=["first_step", "mid_flow"])
async def test_h3_top_level_exception_degrades(monkeypatch, inject_at):
    """降级语义（BASELINE §2 H3 行）：任意步异常 → 顶层 except 吞掉 +
    error 日志（「致命错误，Prompt 装配失败，降级放行」+ `extra_summary=`
    脱敏摘要——persona_id/长度字段/skip_constants，**不含** user_input/
    context_text 原文）→ 放行（不注入、`_quill_activated` 不置位、
    gate 前异常不 reset）。

    patch 点 QuillPlugin 类属性（搬移前后 H3 都经 self/plugin 动态分发，
    同一 patch 点命中）；calls 非空防注入假绿。脱敏摘要只断言键存在——
    emergency/extra_info 的**中间值**随搬移后的降级层位固定为预初始化值，
    不是快照面（报告见 main.py 注册桩 docstring）。
    """
    calls: list = []
    order: list = []

    if inject_at == "restore":
        def _boom(hst, r):
            calls.append(True)
            raise RuntimeError("injected restore failure")

        monkeypatch.setattr(M.QuillPlugin, "_restore_smt_tool", _boom)
    else:
        async def _boom(hst, *a, **k):
            calls.append(True)
            raise RuntimeError("injected activation failure")

        monkeypatch.setattr(M.QuillPlugin, "_check_activation", _boom)

    rec = _LoggerRecH3()
    monkeypatch.setattr(M, "logger", rec)

    state = _StateH3(persona_id="p1", mode="auto", order=order)
    host = _mk_h3_host(order=order, state=state)
    ev = _EvH3(order=order)
    req = _ReqH3(prompt="你好", func_tool=_ToolsH3(),
                 contexts=[{"role": "user", "content": "旧消息"}])

    await _run_h3(host, ev, req)   # 不应抛（抛了会打断框架请求流程）

    assert calls, "异常注入未命中实际调用路径（假绿）"
    assert any("致命错误，Prompt 装配失败，降级放行" in e for e in rec.errors)
    assert any("injected " in e for e in rec.errors)       # 原始异常消息保留
    assert any("extra_summary=" in e for e in rec.errors)
    assert any("user_input_len" in e for e in rec.errors)  # 只有长度，无原文
    assert not any("你好" in e.split("extra_summary=")[-1]
                   for e in rec.errors if "extra_summary=" in e)
    assert "_quill_activated" not in ev.extras
    assert state.reset_rounds_calls == []                  # gate 前异常不 reset


# ════════════════════════════════════════════════════════════════════
# F4/F5（M3.0b，BASELINE §8.2）行为快照 —— 有意行为变更，red→green
#
# F4 同回合 SMT 循环调用拦截：真机日志实证同一回合内模型反复调用
# send_message_to_user（00:06:36 发正文+状态栏 → 00:06:49 又单独发一遍
# 状态栏 → 00:07:22/00:07:32 再发两遍变体正文），插件照单全发，用户被迫
# 手动停止 agent。框架证据（message_tools.py SendMessageToUserTool.call）：
# messages 为空列表或 plain text 为空 → 直接返回
# "error: messages parameter is empty or invalid."，**不向用户发送任何
# 内容**——因此 H2 改写 tool_args["messages"] = [] 即「拒绝本次发送」：
# 模型收到 error 结果，用户侧零副作用。三规则（首个命中即拦截）：
# 精确/子串重复、重复状态栏（bar-only 且 `_quill_status_handled` 已置位）、
# 发送预算（`_SMT_MAX_SENDS_PER_TURN = 2`）；含媒体段放行、守卫异常放行
# （宁漏勿误）。放行路径在函数末尾登记已发正文与次数，与 F1 回声比对
# 共用同一归一函数（`_normalized_reply_body`），保证两侧对称。
#
# F5 剧情分支标记渲染割裂：`>>> 剧情走向 <<<` 在 webchat 等 Markdown
# 渲染器里行首 >>> 被解析为嵌套引用块渲染成三条竖线（<<< 无此语义保持
# 字面），观感割裂。方案：输出侧归一为全角（H2/H6 发送前统一转换，
# >>>→＞＞＞ / <<<→＜＜＜），提示词模板与渲染模板保持 ASCII 不动
# （零解析回归风险）；解析侧三处加全角容忍（模型模仿已归一历史时仍要
# 能解析）。
#
# 注入点约定（同 H2/H6 节）：用例一律经注册桩
# QuillPlugin.on_using_llm_tool / on_decorating_result 进入；F4 判定经
# `_normalized_reply_body`（经 plugin._scrub_inject_report /
# plugin._strip_status_artifacts 动态分发），宿主照 _mk_h2_host / _mk_host
# 造法（真实 QuillPlugin 宿主，动态分发路径天然可用）。
# ════════════════════════════════════════════════════════════════════


# ── F4：同回合 SMT 循环调用拦截 ─────────────────────────────────────


async def test_f4_first_call_passes_and_records_body_and_count():
    """首调用放行：处理后的归一正文登记进 `_quill_smt_sent_bodies`、
    `_quill_smt_send_count` 计 1（记录的是 H2 各段处理**之后**的文本，
    与 F1 回声比对共用同一归一函数，两侧对称）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    tool_args = {"messages": [_plain("剧情正文A。")]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert tool_args["messages"][0]["text"] == "剧情正文A。"
    assert ev.extras.get("_quill_smt_send_count") == 1
    assert ev.extras.get("_quill_smt_sent_bodies") == ["剧情正文A。"]


async def test_f4_exact_duplicate_blocked_and_postprocessing_skipped():
    """精确重复拦截：messages 置 []（框架对空 messages 返回 error、不向
    用户发送）；拦截发生在 JSON 解析之后、其余处理之前——状态栏链/拒绝
    扫描/记录段全部不跑（计数保持首次的 1、无 `_quill_status_handled`
    置位、状态栏开关查询停在首次）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    first = {"messages": [_plain("剧情正文A。")]}
    await _run_h2(host, ev, _ToolH2(), first)
    assert ev.extras.get("_quill_smt_send_count") == 1

    second = {"messages": [_plain("剧情正文A。")]}
    await _run_h2(host, ev, _ToolH2(), second)

    assert second["messages"] == []
    assert ev.extras.get("_quill_status_handled") is None
    assert ev.extras.get("_quill_smt_send_count") == 1
    assert ev.extras.get("_quill_smt_sent_bodies") == ["剧情正文A。"]
    assert len(host.state_manager.get_mode_calls) == 1


async def test_f4_exact_duplicate_blocked_json_string_writeback():
    """was_string 形态：拦截后回写 "[]"（保持 JSON 字符串类型，参数类型
    不漂移；模型经框架 error 得知被拒）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    first = {"messages": json.dumps([_plain("剧情正文A。")], ensure_ascii=False)}
    await _run_h2(host, ev, _ToolH2(), first)

    second = {"messages": json.dumps([_plain("剧情正文A。")], ensure_ascii=False)}
    await _run_h2(host, ev, _ToolH2(), second)

    assert second["messages"] == "[]"
    assert ev.extras.get("_quill_smt_send_count") == 1


async def test_f4_substring_of_sent_body_blocked():
    """子串拦截：候选正文（≥ _SMT_SUBSTR_MIN_LEN）是已发正文的子串
    （状态栏残尾/分段重发）→ 拦截；反方向（新正文**包含**已发正文）不拦
    ——只防更短的重发，宁漏勿误。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    first = {"messages": [_plain(
        "开头正文。她转过身看向窗外，雨还没停，屋檐滴水声一下一下敲着安静。\n"
        "结尾是一大段状态栏残尾混在正文里没有清理干净的部分。"
    )]}
    await _run_h2(host, ev, _ToolH2(), first)

    second = {"messages": [_plain(
        "结尾是一大段状态栏残尾混在正文里没有清理干净的部分。"
    )]}
    await _run_h2(host, ev, _ToolH2(), second)
    assert second["messages"] == []

    # 反方向：更长的正文包含已发正文 → 放行（判定只有 body in b 方向）
    third = {"messages": [_plain(
        "开头正文。她转过身看向窗外，雨还没停，屋檐滴水声一下一下敲着安静。\n"
        "结尾是一大段状态栏残尾混在正文里没有清理干净的部分。\n新增后续剧情。"
    )]}
    await _run_h2(host, ev, _ToolH2(), third)
    assert third["messages"][0]["text"].endswith("新增后续剧情。")


async def test_f4_short_body_substring_of_sent_passes():
    """短正文（< _SMT_SUBSTR_MIN_LEN）即使是已发正文的子串也放行——
    一句短对话偶然含于已发长文属合法新消息，宁漏勿误。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    first = {"messages": [_plain(
        "门口的对话还在继续。好，转身走向厨房，她把湿伞收了起来。"
    )]}
    await _run_h2(host, ev, _ToolH2(), first)

    short = {"messages": [_plain("好，转身走向厨房")]}
    await _run_h2(host, ev, _ToolH2(), short)
    assert short["messages"] != []
    assert short["messages"][0]["text"] == "好，转身走向厨房"


async def test_f4_bar_only_repeat_blocked_when_status_handled():
    """重复状态栏：整段全是状态栏痕迹（归一后正文为空）且
    `_quill_status_handled` 已置位 → 拦截（真机 00:06:49 单独重发状态栏
    的形态）。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2(extras={"_quill_status_handled": True})
    tool_args = {"messages": [_plain(DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert tool_args["messages"] == []
    assert host.state_manager.get_mode_calls == []   # 拦截在状态栏链之前


async def test_f4_bar_only_first_round_without_handled_passes():
    """`_quill_status_handled` 未置位（首轮）：整段状态栏痕迹放行——那是
    历史上「正文一段、状态栏单独一段」的合法分割模式；状态栏链照常提取
    渲染，计数 +1。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": [_plain(DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    assert "───── 状态栏 ─────" in tool_args["messages"][0]["text"]
    assert ev.extras.get("_quill_status_handled") is True
    assert ev.extras.get("_quill_smt_send_count") == 1
    assert len(ev.extras.get("_quill_smt_sent_bodies")) == 1


async def test_f4_budget_blocks_third_distinct_call():
    """发送预算（`_SMT_MAX_SENDS_PER_TURN = 2`）：三条互不重复的正文，
    第三条仍被拦截——合法分割两条、循环失败实测 3-4 条；拦截调用不计数。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    for text in ("第一条正文。", "第二条正文。"):
        ta = {"messages": [_plain(text)]}
        await _run_h2(host, ev, _ToolH2(), ta)
    assert ev.extras.get("_quill_smt_send_count") == 2
    assert ev.extras.get("_quill_smt_sent_bodies") == ["第一条正文。", "第二条正文。"]

    third = {"messages": [_plain("第三条正文。")]}
    await _run_h2(host, ev, _ToolH2(), third)

    assert third["messages"] == []
    assert ev.extras.get("_quill_smt_send_count") == 2


async def test_f4_media_call_not_blocked_even_if_text_duplicates():
    """含媒体段（image + plain 重复文本）：`_has_media` 为真跳过全部拦截
    规则直接放行（媒体消息无法凭正文比对判重）；计数恒 +1、正文照常登记
    （媒体调用同样消耗本轮预算）。"""
    host = _mk_h2_host(enabled=False)
    ev = _EvH2()
    first = {"messages": [_plain("剧情正文A。")]}
    await _run_h2(host, ev, _ToolH2(), first)

    second = {"messages": [{"type": "image", "url": "x"}, _plain("剧情正文A。")]}
    await _run_h2(host, ev, _ToolH2(), second)

    assert second["messages"][0] == {"type": "image", "url": "x"}   # 原样放行
    assert second["messages"][1]["text"] == "剧情正文A。"
    assert ev.extras.get("_quill_smt_send_count") == 2
    assert ev.extras.get("_quill_smt_sent_bodies") == ["剧情正文A。", "剧情正文A。"]


async def test_f4_guard_exception_passes_through_original_path(monkeypatch):
    """守卫自身异常（注入 `_normalized_reply_body` 抛出）：debug 放行原路径
    ——H2 其余处理照常（状态栏链提取渲染），记录段同样吞掉（计数/正文
    均不置位），顶层降级不触发。"""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected F4 guard failure")

    monkeypatch.setattr(_hooks, "_normalized_reply_body", _boom)
    rec = _LoggerRec()
    monkeypatch.setattr(_hooks, "logger", rec)

    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    tool_args = {"messages": [_plain("开头正文\n" + DIRTY_LOVE)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)   # 不应抛

    assert calls, "异常注入未命中实际调用路径（假绿）"
    assert "───── 状态栏 ─────" in tool_args["messages"][0]["text"]
    assert ev.extras.get("_quill_status_handled") is True
    assert "_quill_smt_send_count" not in ev.extras
    assert "_quill_smt_sent_bodies" not in ev.extras


# ── F5：剧情分支标记全角归一 ────────────────────────────────────────


async def test_f5_h2_plain_plot_markers_normalized():
    """H2 工具路径：plain 段的 ASCII 剧情分支箭头在发送前统一转全角
    （含 `<<< 请选择 >>>` 行内尾部 >>>）；正文与选项内容原样保留。"""
    host = _mk_h2_host(enabled=True)
    ev = _EvH2()
    text = "剧情正文。\n\n>>> 剧情走向 <<<\n1. 继续当前话题\n<<< 请选择 >>>"
    tool_args = {"messages": [_plain(text)]}

    await _run_h2(host, ev, _ToolH2(), tool_args)

    out = tool_args["messages"][0]["text"]
    assert "＞＞＞ 剧情走向 ＜＜＜" in out
    assert "＜＜＜ 请选择 ＞＞＞" in out
    assert ">>>" not in out
    assert "<<<" not in out
    assert "剧情正文。" in out
    assert "1. 继续当前话题" in out


async def test_f5_h6_chain_plain_plot_markers_normalized():
    """H6 兜底路径：result.chain 的 Plain 组件在发送前同样归一（剥离之后、
    赋回 comp.text 之前），箭头归一计入 cleaned。

    例外（有意收窄，见接口 docstring）：剥离后仍含渲染签名
    （``**状态栏**`` + ``` 围栏）的段**不**归一——
    tests/legacy/test_status_bar_parsers.py 兜底钩子节（断言逐字保留
    铁律）把「开启时已渲染状态栏原样保留」连同 ASCII 箭头逐字钉死；
    该契约由上节 test_h6_enabled_never_eats_rendered_bar_or_custom_
    template 继续逐字锁定，渲染栏的归一由 H2 工具路径（正常产生路径）
    承担。"""
    host = _mk_host(enabled=True)
    ev = _Ev(_Result(["剧情正文。\n\n>>> 剧情走向 <<<\n1. 选项一\n<<< 请选择 >>>"]))

    await _run_hook(host, ev)

    out = ev.get_result().chain[0].text
    assert "＞＞＞ 剧情走向 ＜＜＜" in out
    assert "＜＜＜ 请选择 ＞＞＞" in out
    assert ">>>" not in out
    assert "<<<" not in out


def test_f5_normalize_plot_markers_unit():
    """`normalize_plot_markers` 单元：≥3 连续箭头整组映射（>>>> → ＞＞＞＞）、
    1-2 连箭头与普通正文无 collateral、空串原样。"""
    from astrbot_plugin_quillplus.quill.services import response as _resp

    assert _resp.normalize_plot_markers(">>>>") == "＞＞＞＞"
    assert _resp.normalize_plot_markers(">>> 剧情走向 <<<") == "＞＞＞ 剧情走向 ＜＜＜"
    assert _resp.normalize_plot_markers("a > b") == "a > b"
    assert _resp.normalize_plot_markers("a >> b") == "a >> b"
    assert _resp.normalize_plot_markers(
        "普通的剧情正文，没有标记。") == "普通的剧情正文，没有标记。"
    assert _resp.normalize_plot_markers("") == ""


def test_f5_plot_path_re_tolerates_fullwidth():
    """解析器全角容忍：`_PLOT_PATH_RE` 匹配全角变体（含混合宽度）；
    ASCII 语义不变——普通 `>>> 引用 <<<` 仍不误判（legacy t6 语义）。"""
    cn_fw = ("正文\n\n＞＞＞ 剧情走向 ＜＜＜\n1. 继续当前话题\n"
             "2. 转换场景\n＜＜＜ 请选择 ＞＞＞")
    m = M._PLOT_PATH_RE.search(cn_fw)
    assert m is not None
    assert "继续当前话题" in m.group(1)

    mixed = "正文\n\n>>> 剧情走向 ＜＜＜\n1. 选项一\n<<< 请选择 ＞＞＞"
    assert M._PLOT_PATH_RE.search(mixed) is not None

    # ASCII 语义不变（对应 legacy t6：普通引用块不误判）
    assert M._PLOT_PATH_RE.search(">>> 这里是普通引用块 <<<") is None


def test_f5_lenient_parse_status_stops_at_fullwidth_marker():
    """`_lenient_parse_status` 行首前瞻容忍全角 ＞＞＞：字段值在下一行为
    全角箭头标记时正确终止（不把标记行吞进值里）。"""
    host = _mk_h2_host()
    text = "好感度：88\n位置：海滩\n＞＞＞ 剧情走向 ＞＞＞\n1. 选项一"
    updates = host._lenient_parse_status(text, list(M._DEFAULT_LOVE_FIELDS_RAW))
    assert updates.get("好感度") == "88"
    assert updates.get("位置") == "海滩"


def test_f5_parse_status_block_skips_fullwidth_marker_lines():
    """`_parse_status_block` 跳过含全角箭头标记的行（模型模仿已归一历史时
    可能写出全角标记 + 冒号，不跳过会把标记当成字段读进 session_vars）；
    ASCII 跳过语义不变。"""
    host = _mk_h2_host()
    updates = host._parse_status_block(
        "好感度：65\n＞＞＞ 剧情走向：海边约会 ＜＜＜\n心情：开心"
    )
    assert updates == {"好感度": "65", "心情": "开心"}

    # ASCII 语义不变（legacy 同款样本）
    assert host._parse_status_block("好感度：65\n>>> 剧情走向 <<<\n1. 继续") == {
        "好感度": "65"}
