# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H6/H1 行为快照测试（v5.3.0 M2.2 钩子薄化）。

作用
----
M2.2 的"先立保护网再走钢丝"：对钩子现行为建立快照，然后做「业务逻辑
下沉 quill/services/ + 钩子薄化为注册桩+interfaces 委托」，搬移前后本文件
（对应用例）**一字不改**重跑，证明等价（BASELINE §8.2 验收面）。

- 第一轮（commit 958722e）：H6（on_decorating_result）——剥离器两档强度、
  顶层降级、只减法不补栏；下沉 quill/services/statusbar/strip.py。
- 第二轮（本轮）：H1（on_waiting_llm_request）——/reinject 字面拦截、
  stream_mode 三态、内层取值异常静默、`_ensure_persona_conversation`
  对话隔离（含其内部 try/except 吞掉语义）、H1 **无顶层 try** 的降级怪癖
  （BASELINE §2 H1 行：`state_manager.get_state` 抛出会上抛框架，与 H6
  不同，不得补 try）；下沉 quill/services/character.py。

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
