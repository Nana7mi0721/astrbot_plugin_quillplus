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
