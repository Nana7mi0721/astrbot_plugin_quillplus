# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AstrBot 事件钩子的实现函数（v5.3.0 M2.2 钩子薄化）。

架构修订（BASELINE §1.2，真机 4.28.1 源码实证）：钩子/指令函数的
``__module__`` 必须与插件注册路径（``data.plugins.<目录>.main``）精确
相等才会被框架绑定与分发——因此**注册桩**（装饰器 + 签名 + priority）
留在 main.py 类体，桩体一行委托到本模块的实现函数；业务逻辑逐字下沉
于此。M2.2 第一轮 H6（on_decorating_result）、第二轮 H1
（on_waiting_llm_request）、第三轮 H2（on_using_llm_tool）、第四轮 H4
（on_llm_response）、第五轮 H5（on_llm_tool_respond）、第六轮 H3
（on_llm_request，全插件最大函数、22 步注入编排本体，BASELINE §4）。
至此六钩子全部迁毕。

M3.0 真机实测修复（BASELINE §8.2，**有意行为变更**里程碑）：H4 新增
F1 回声置空段（``_normalized_reply_body`` + ``handle_llm_response``
2.5 段，消除 SMT 回声重复回复）；F2 的 quill_rounds 重置挂点住
quill/services/character.py（H1 委托链上）。M3.0b：H2 新增 F4 同回合
SMT 循环调用拦截与放行记录段、F5 剧情分支箭头全角归一（H6 发送前
同步归一），均为有意行为变更。

降级语义分层：顶层 try/except 留在 main.py 注册桩内（与原 H6 的
"顶层吞掉放行"同层，不因委托而改变降级位置）；本模块实现体内**不再**
重复包裹——桩内已保证任何异常都不会外抛中断发送。

**例外——无顶层 try 的钩子（H1 / H5，BASELINE §2 各行降级怪癖）**：原
H1 与 H5 都**没有**钩子级顶层 try（gate 间异常会上抛框架）。两者的注册
桩因此**不做**任何 try 包裹，本模块对应实现也只保留原有的内层小块异常
处理（H1：取值 ``except: return``；H5：记忆存储调度/反思调度两个内层
块的 warning 吞掉）——降级位置原样保真，防后续"顺手"补 try。
"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.tool import FunctionTool

from ..commands import _check_group_permission
from ..encryption import decrypt_output
from ..quill.services import memory as _memory_mod
from ..quill.services import prompt as _prompt_mod
from ..quill.services import response as _response_mod

# F1（M3.0，BASELINE §8.2 F1）：框架 send_message_to_user 把已发送纯文本
# 记入本 extra 键（message_tools.py:349-361，值经 strip()），respond.stage
# 以它与最终 result 做精确匹配去重（respond/stage.py:189-207）。
_SMT_SENT_TEXTS_KEY = "_send_message_to_user_current_session_plain_texts"

# F4（M3.0b，BASELINE §8.2 F4）：同回合 SMT 循环拦截的登记键。
_SMT_SENT_BODIES_KEY = "_quill_smt_sent_bodies"
_SMT_SEND_COUNT_KEY = "_quill_smt_send_count"
# 发送预算：合法的分割发送是两条（历史上「正文一段、状态栏单独一段」的
# 模式，实测 7 轮里 3 轮如此）；同回合循环失败实测连发 3-4 条——预算取 2，
# 再多的调用几乎必然是循环失败（拦截方式：messages 置 []，框架对空
# messages 直接返回 error 且不发送任何内容）。
_SMT_MAX_SENDS_PER_TURN = 2
# 子串判重（规则 1）的最小候选长度：短正文（一句短对话）偶然是已发长文
# 的子串属合法新消息，不拦；真实的残尾/分段重发形态远长于此。
_SMT_SUBSTR_MIN_LEN = 20


def _normalized_reply_body(plugin, text: str) -> str:
    """F1 回声比对的**双向归一**：注入报告行抹除 + 状态栏变体剥离后取正文。

    completion 侧与已发记录侧走同一函数，保证比对对称。两个变换都是
    确定性的（报告行是行锚正则、剥离器是既有状态栏变体正则组），不含
    任何模糊/相似度匹配——正文不完全相等即不判回声（宁漏勿误）。

    经 ``plugin._scrub_inject_report`` / ``plugin._strip_status_artifacts``
    动态分发而非直接 import：与 H4 其余段落对 main.py 辅助方法的访问
    路径一致（前者在 main.py、后者转发 quill/services/statusbar/strip.py）。
    """
    body = plugin._scrub_inject_report(text or "")
    return plugin._strip_status_artifacts(
        body, plugin.props.love_fields
    ).strip()


async def handle_decorating_result(plugin, event: AstrMessageEvent) -> None:
    """消息**发送前**的最后一次清洗——擦掉漏网的状态栏残留。

    （H6 业务逻辑，自 main.py 逐字搬移，M2.2；``self`` → ``plugin``。）

    为什么需要这一道（这不是重复劳动，覆盖的是别的钩子够不到的情况）：

    `on_using_llm_tool` 只能改写**工具参数**（`send_message_to_user` 的
    messages）。但 agent loop 每一轮迭代都会**先** `yield` 该轮的
    `llm_resp.result_chain`（`tool_loop_agent_runner.py:917`），**然后**才走
    `_handle_function_tools`（同文件 `:982`）触发工具钩子。也就是说：
    模型在**不调用工具**的那一轮直接输出的文本，会先于任何工具钩子被推送，
    插件根本没机会处理它。

    实测（`docs/probe_no_leak.py`）：一轮里模型被纠正后连发了 18 次
    `send_message_to_user`，其中 17 次都被正常处理，唯独夹在中间那次
    「直接输出一行裸 `[LOVE_DATA]`」的迭代绕过了全部钩子，直达用户。

    这一钩子在 `result_decorate` 阶段、**真正发送之前**触发
    （`core/pipeline/result_decorate/stage.py:158`），拿到的是最终
    MessageChain，因此能兜住任何来源的残留。

    **两档强度，取决于本轮状态栏是否启用**（这一点是踩过坑才分清的）：

    * 启用时——只擦**原始标记**（`[LOVE_DATA]`、`[STATUS]`、裸字段行）。
      **绝不能**用整套 `_strip_status_artifacts`：它第一条模式就匹配
      `**状态栏**...\\`\\`\\`...\\`\\`\\``，那是 L1/L2 **正常渲染**的产物，
      整段擦掉等于把状态栏从回复里删掉（第一版就是这么把 A/C 两项测挂的）。
    * 关闭时——用整套剥离器。此时渲染过的状态栏**本就不该出现**
      （历史上下文那侧也在同步清理），擦掉正是期望行为。

    另外**不做**「补栏」：此刻正文已定型，补栏会与前面已发出的分段重复；
    发送前只做减法。

    F5（M3.0b，BASELINE §8.2 F5）：剥离结果在赋回 ``comp.text`` 前追加
    ``_response_mod.normalize_plot_markers``（ASCII 剧情分支箭头 → 全角）
    ——本钩子是发送前最后一道，提示词/渲染模板保持 ASCII，模型直出的
    箭头在此归一（webchat 等 Markdown 渲染器把行首 >>> 解析为嵌套引用块
    渲染成三条竖线）；归一变化并入 cleaned 计数。**例外**：剥离后仍含
    L1/L2 渲染签名（``**状态栏**`` + ``` 围栏）的段不归一——
    tests/legacy/test_status_bar_parsers.py 兜底钩子节（铁律：断言逐字
    保留）把「开启时已渲染状态栏原样保留」连同 ASCII 箭头逐字钉死为本
    钩子契约；渲染栏的正常产生路径在 H2 工具链（彼处对每个 plain 段
    无条件归一），H6 只对非渲染栏段兜底归一。

    剥离器经 ``plugin._strip_*`` 调用而非直接 import：与原 ``self._strip_*``
    的动态分发路径逐字等价（转发链最终落在
    quill/services/statusbar/strip.py 的纯函数），并让行为快照测试
    （tests/test_hook_snapshots.py）在搬移前后命中同一注入点。
    """
    # 会话级覆盖 > 面板全局：关闭方向必须清，开启方向只清原始标记
    enabled = await plugin._effective_status_bar_enabled(
        plugin._get_target_id(event)
    )

    result = event.get_result()
    if result is None:
        return
    chain = getattr(result, "chain", None)
    if not chain:
        return

    from astrbot.core.message.components import Plain

    cleaned = 0
    for comp in chain:
        if not isinstance(comp, Plain):
            continue
        text = getattr(comp, "text", "") or ""
        if not text:
            continue
        if enabled:
            stripped = plugin._strip_raw_markers(text, plugin.props.love_fields)
        else:
            stripped = plugin._strip_status_artifacts(text, plugin.props.love_fields)
        # F5（M3.0b）：ASCII 剧情分支箭头 → 全角（webchat 等 Markdown
        # 渲染器把行首 >>> 解析为嵌套引用块渲染成三条竖线；输出侧归一，
        # 提示词/渲染模板保持 ASCII 不动）。归一变化并入 cleaned 计数
        # （stripped != text 判定在其之后）。
        # 例外：剥离后仍含渲染签名（**状态栏** + ``` 围栏）的段不归一——
        # legacy 兜底钩子节（断言逐字保留铁律）把「开启时已渲染状态栏
        # 原样保留」连同 ASCII 箭头逐字钉死为本钩子契约；渲染栏的正常
        # 产生路径在 H2 工具链（彼处无条件归一），此处只对非渲染栏段
        # 兜底归一。签名判定用 stripped（关闭档整套剥离后栏已不存在，
        # 剩余正文照常归一）。
        if not ("**状态栏**" in stripped and "```" in stripped):
            stripped = _response_mod.normalize_plot_markers(stripped)
        if stripped != text:
            comp.text = stripped
            cleaned += 1
    if cleaned:
        logger.info(
            f"[Quill] 发送前清洗 {cleaned} 段（状态栏残留/剧情标记，"
            f"{'原始标记' if enabled else '全套剥离'}）"
        )


async def handle_waiting_llm_request(plugin, event: AstrMessageEvent) -> None:
    """LLM 请求等待期：切换角色卡专属对话并控制流式模式（H1）。

    （业务逻辑自 main.py 逐字搬移，M2.2 第二轮；``self`` → ``plugin``。
    行为快照见 tests/test_hook_snapshots.py H1 节，行为契约与降级怪癖
    见 main.py 注册桩 docstring 与 BASELINE §2 H1 行。）

    与 H6 的关键差异（降级怪癖，刻意保真）：本函数**没有**钩子级顶层
    try——``plugin.state_manager.get_state`` 抛出会原样上抛框架；唯一
    的异常处理是"取 message_str/_get_target_id"的内层小块
    ``except: return``。角色卡对话隔离步骤（
    ``plugin._ensure_persona_conversation``）自带全量 try/except，隔离
    失败只记日志放行，不影响后续流式决策——经 plugin 的薄转发调用
    （转发最终落在 quill/services/character.py），与原
    ``self._ensure_persona_conversation`` 动态分发路径逐字等价。
    """
    # 必须最先执行：本事件早于 AstrBot 的 _get_session_conv()，
    # 在这里切换对话才能对本轮生效（详见 quill/services/character.py）。
    await plugin._ensure_persona_conversation(event)

    try:
        user_input = event.message_str or ""
        target_id = plugin._get_target_id(event)
    except Exception:
        return

    # 拦截 /reinject 和 /重新注入（个人行为，仍用 sender_id）
    if user_input.strip() in ("/reinject", "/重新注入"):
        sender_id = str(event.get_sender_id())
        await plugin.state_manager.reset_quill_rounds(sender_id)
        logger.info("[Quill] /reinject 已重置 quill_rounds")
        from astrbot.core.message.message_event_result import MessageEventResult
        event.set_result(MessageEventResult().message(
            "已重置注入状态。下次触发 Quill 时将重新注入全部常驻素材。"
        ))
        return

    # 读取对话维度流式偏好
    state = await plugin.state_manager.get_state(target_id)

    if state.stream_mode == "off":
        event.set_extra("enable_streaming", False)
        return
    if state.stream_mode == "on":
        event.set_extra("enable_streaming", True)
        return

    # auto 模式：激活时关闭流式
    activated = plugin.activation_detector.should_activate(user_input)
    has_bracket = plugin.activation_detector.check_brackets(user_input)

    if activated or has_bracket:
        event.set_extra("enable_streaming", False)
        logger.info("[Quill] 已关闭流式输出")


async def handle_using_llm_tool(
    plugin, event: AstrMessageEvent, tool: FunctionTool, tool_args: dict | None
) -> None:
    """工具调用前拦截：改写 send_message_to_user 的工具参数（H2）。

    （业务逻辑自 main.py 逐字搬移，M2.2 第三轮；``self`` → ``plugin``。
    行为快照见 tests/test_hook_snapshots.py H2 节，行为契约与顶层降级
    语义见 main.py 注册桩 docstring 与 BASELINE §2 H2 行。顶层
    try/except **不在本函数内**——降级层位在注册桩，与 H6 同形态。）

    处理链（三重闸门通过后）：

    1. 平台探测：``platform_meta.name`` 优先，``get_platform_name()``
       回退（内联双 try）。与 StatusbarRenderMixin._resolve_platform_name
       近似但**不等价**（无 strip、空名不短路继续求值）——历史实现，
       刻意逐字保留，不借搬移"顺手统一"；
    2. messages 为 JSON 字符串时先解析（失败 → 立即 return，后续一切
       不跑、tool_args 原样）；
    3. F4 同回合循环拦截（M3.0b 新增，BASELINE §8.2 F4）：守卫段——
       已发正文精确/子串重复、重复状态栏（bar-only 且
       ``_quill_status_handled`` 已置位）、发送预算
       （``_SMT_MAX_SENDS_PER_TURN = 2``）任一命中 → messages 置 []
       （was_string 回写 "[]"）并 return，后续一切不跑；
    4. telegram/tg 平台剥离 plain 段 Markdown（逐字下沉
       quill/services/response.py，调用点一行）；
    5. 状态栏（全平台）：开启时首条 plain 走六级链
       （``plugin._handle_status_bar``，M2.1 Mixin，MRO 动态分发），
       handled 后 set ``_quill_status_handled``，后续 plain 只清残留；
       关闭时整套 ``_strip_status_artifacts``；注入报告追加到最后一条
       plain（``_append_inject_report``）；每个 plain 段追加 F5 剧情分支
       箭头全角归一（M3.0b，``_response_mod.normalize_plot_markers``）；
    6. JSON 回写（was_string 时序列化回去）；
    7. 拒绝模式补充扫描（S3-2：completion_text 为空时拒绝内容藏于
       tool_args.messages，只扫首条 plain，命中 mark_refusal）；
    8. F4 记录段（M3.0b 新增）：放行路径登记已发正文（重算处理后的
       plain concat 经 ``_normalized_reply_body`` 归一，与 F1 回声比对
       共用同一函数）与发送次数（恒 +1，含媒体调用）。

    下沉决策（本轮评估记录）：telegram 剥离（``strip_markdown`` 正则组与
    逐段套用循环）在原 main.py 即为零 self 依赖的模块级纯函数/自由段，
    已下沉 quill/services/response.py；**JSON 解析-回写与拒绝扫描留在本
    函数**——前者与控制流交织（解析失败的早退 return 卡在解析与回写
    之间），后者在守卫内重算 target_id（提取成服务函数需要改变求值
    时序或传参形态），两段强搬都会把「逐字搬移」变成「重写」，违背本轮
    "不增加行为风险"的准绳。
    """
    if tool.name != "send_message_to_user":
        return
    if not event.get_extra("_quill_activated"):
        return
    if not tool_args:
        return

    platform = ""
    try:
        pm = getattr(event, "platform_meta", None)
        if pm is not None:
            platform = (getattr(pm, "name", "") or "").lower()
    except Exception:
        logger.debug("[Quill] platform_meta.name 获取失败", exc_info=True)
    if not platform:
        try:
            platform = (event.get_platform_name() or "").lower()
        except Exception:
            logger.debug("[Quill] get_platform_name() 获取失败", exc_info=True)

    # 记录原始类型以便正确回写
    messages_raw = tool_args.get("messages", [])
    was_string = isinstance(messages_raw, str)
    if was_string:
        try:
            messages = json.loads(messages_raw)
        except (json.JSONDecodeError, TypeError):
            return
    else:
        messages = messages_raw

    # ── F4（M3.0b，BASELINE §8.2 F4）：同回合 SMT 循环调用拦截 ────────
    # 真机实证：同一回合内模型反复调用 send_message_to_user（正文+状态栏
    # → 单独状态栏 → 两遍变体正文），插件照单全发，用户被迫手动停止
    # agent。框架 message_tools.py 对空 messages 直接返回
    # "error: messages parameter is empty or invalid." 且**不发送任何内容**，
    # 故把 tool_args["messages"] 置 [] 即等于「拒绝本次发送」：模型收到
    # error 结果，用户侧零副作用。
    #
    # 守卫位于 JSON 解析之后、其余全部处理之前：拦截时 H2 后续（Markdown
    # 剥离/状态栏链/报告/扫描/记录）一律不跑。仅当 messages 是 list 时
    # 执行；整体 try/except——判定自身异常只 debug 放行原路径（宁漏勿误，
    # 不吞 H2 其他处理）。与 F1 回声比对共用 _normalized_reply_body（经
    # plugin 动态分发 _scrub_inject_report / _strip_status_artifacts），
    # 保证判定两侧归一对称。
    if isinstance(messages, list):
        try:
            _plain_concat = "\n".join(
                m.get("text") for m in messages
                if isinstance(m, dict) and m.get("type") == "plain"
                and isinstance(m.get("text"), str)
            )
            # 存在非 plain 段（image/record/video/file 等）→ 媒体调用：
            # 无法凭正文比对判重，跳过全部拦截规则直接放行
            _has_media = any(
                isinstance(m, dict) and m.get("type") != "plain"
                for m in messages
            )
            _body = _normalized_reply_body(plugin, _plain_concat)
            _sent = event.get_extra(_SMT_SENT_BODIES_KEY)
            if not (isinstance(_sent, list)
                    and all(isinstance(s, str) for s in _sent)):
                _sent = []
            _count = event.get_extra(_SMT_SEND_COUNT_KEY)
            _count = _count if isinstance(_count, int) else 0

            _reason = ""
            if not _has_media:
                if _body:
                    # 规则 1：精确重复；或候选（≥ _SMT_SUBSTR_MIN_LEN）是
                    # 已发正文的子串（如状态栏残尾/分段重发）。反方向
                    # （新正文包含已发正文）不拦；短正文不做子串判定——
                    # 一句短对话偶然含于已发长文属合法新消息，宁漏勿误。
                    for _b in _sent:
                        if _b == _body or (
                            len(_body) >= _SMT_SUBSTR_MIN_LEN and _body in _b
                        ):
                            _reason = "重复正文"
                            break
                elif _plain_concat.strip() and event.get_extra(
                        "_quill_status_handled"):
                    # 规则 2：整段全是状态栏痕迹（归一后正文为空）且本轮
                    # 状态栏已处理过 → 重复状态栏；未置位时放行——那是
                    # 历史上「正文一段、状态栏单独一段」的合法分割模式
                    _reason = "重复状态栏"
                if not _reason and _count >= _SMT_MAX_SENDS_PER_TURN:
                    # 规则 3：发送预算（合法分割两条、循环失败实测 3-4 条）
                    _reason = "发送预算"
            if _reason:
                tool_args["messages"] = (
                    json.dumps([], ensure_ascii=False) if was_string else []
                )
                logger.info(
                    f"[Quill] F4 已拦截第 {_count + 1} 次 "
                    f"send_message_to_user（{_reason}），本次调用不发送"
                )
                return
        except Exception as e:
            # 宁漏勿误：判定自身失败只放行原路径，不吞掉 H2 其余处理
            logger.debug(f"[Quill] F4 守卫异常，放行原路径: {e}", exc_info=True)

    # 仅对特定平台执行 Markdown 清理（未知平台不剥离，避免破坏原生 Markdown 渲染）
    _response_mod.strip_markdown_in_plain_messages(messages, platform)

    # 状态栏处理（全平台执行）
    # 本轮最终开关 = 会话级覆盖 > 面板全局（见 _effective_status_bar_enabled）
    target_id = plugin._get_target_id(event)
    _sb_on = await plugin._effective_status_bar_enabled(target_id)
    _bar_tpl = plugin._status_bar_template_for(platform)
    if isinstance(messages, list):
        report_done = False
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                # 首条 plain 消息：执行状态栏提取；后续消息：仅清理残留状态栏标记
                if idx == 0 or not event.get_extra("_quill_status_handled"):
                    if _sb_on:
                        new_text, _, handled = await plugin._handle_status_bar(
                            msg["text"], target_id, _bar_tpl
                        )
                        msg["text"] = new_text
                        if handled:
                            event.set_extra("_quill_status_handled", True)
                    else:
                        msg["text"] = plugin._strip_status_artifacts(
                            msg["text"], plugin.props.love_fields
                        )
                else:
                    # P2-3 修复：首条之后的 plain 消息也清理残留的状态栏标记，
                    # 避免 LLM 多段输出时后续段落的 [LOVE_DATA]/状态栏代码块被原样发给用户
                    msg["text"] = plugin._strip_status_artifacts(
                        msg["text"], plugin.props.love_fields
                    )
                # 注入报告追加到最后一条 plain 消息上（仅一次）
                if not report_done and idx == len(messages) - 1:
                    before = msg["text"]
                    msg["text"] = plugin._append_inject_report(msg["text"], target_id)
                    if msg["text"] != before:
                        event.set_extra("_quill_report_added", True)
                    report_done = True
                # F5（M3.0b）：ASCII 剧情分支箭头 → 全角。webchat 等
                # Markdown 渲染器把行首 >>> 解析为嵌套引用块渲染成三条
                # 竖线（<<< 无此语义），观感割裂；输出侧统一归一，提示词
                # 模板与渲染模板保持 ASCII 不动。F4 拦截分支已在此前
                # return，不受影响。
                msg["text"] = _response_mod.normalize_plot_markers(msg["text"])

    # JSON 回写：如果原始类型是字符串，序列化回去
    if was_string:
        tool_args["messages"] = json.dumps(messages, ensure_ascii=False)

    # S3-2: Agent 模式下 LLM 输出可能经由 tool_args.messages 传递，
    # completion_text 为空时拒绝内容藏于此，需在此补充扫描。
    if plugin.props.refusal_enabled and isinstance(messages, list):
        target_id = plugin._get_target_id(event)
        for msg in messages:
            if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                scan_text = msg.get("text") or ""
                if not scan_text:
                    continue
                for pattern in plugin.props.refusal_patterns:
                    if pattern in scan_text:
                        await plugin.state_manager.mark_refusal(target_id)
                        logger.info(f"[Quill] (tool_args) 检测到拒绝模式 '{pattern}' (target={target_id})")
                        break
                break  # 只扫首条 plain 文本

    # ── F4 记录段（M3.0b）：放行路径的已发正文/次数登记 ────────────────
    # 重算**处理后**的 plain concat（本函数已在各段改写 msg["text"]），
    # 经 _normalized_reply_body 归一后非空才登记；发送次数恒 +1（含媒体
    # 调用——媒体调用放行同样消耗本轮预算）。与 F1 回声比对
    # （handle_llm_response）共用同一归一函数，保证两侧对称。
    # 整体 try/except：记录自身异常只 debug 放行，不影响本钩子语义。
    try:
        _bodies = event.get_extra(_SMT_SENT_BODIES_KEY)
        if not (isinstance(_bodies, list)
                and all(isinstance(s, str) for s in _bodies)):
            _bodies = []
        _concat = ""
        if isinstance(messages, list):
            _concat = "\n".join(
                m.get("text") for m in messages
                if isinstance(m, dict) and m.get("type") == "plain"
                and isinstance(m.get("text"), str)
            )
        _body = _normalized_reply_body(plugin, _concat)
        if _body:
            _bodies.append(_body)
            event.set_extra(_SMT_SENT_BODIES_KEY, _bodies)
        _count = event.get_extra(_SMT_SEND_COUNT_KEY)
        _count = _count if isinstance(_count, int) else 0
        event.set_extra(_SMT_SEND_COUNT_KEY, _count + 1)
    except Exception as e:
        logger.debug(f"[Quill] F4 已发记录异常，放行: {e}", exc_info=True)


async def handle_llm_response(
    plugin, event: AstrMessageEvent, resp: LLMResponse
) -> None:
    """LLM 响应拦截：前置清洗、状态栏提取渲染、注入报告、落日志与拒绝扫描（H4）。

    （业务逻辑自 main.py 逐字搬移，M2.2 第四轮；``self`` → ``plugin``。
    行为快照见 tests/test_hook_snapshots.py H4 节，行为契约与顶层降级
    语义见 main.py 注册桩 docstring 与 BASELINE §2 H4 行。顶层
    try/except **不在本函数内**——降级层位在注册桩，与 H2/H6 同形态：
    任何异常吞掉 + error 日志放行，resp 保持已改到一半的状态。）

    段序（搬移前后一致，不得重排——顺序即行为；M3.0 在段 2 与段 3 之间
    新增 F1 回声置空段，为**有意行为变更**，BASELINE §8.2 F1 / PLAN §M3.0）：

    1. 前置清洗：``[B:...]`` Base64 解密安全网（agent loop 续写尾部文本
       的混淆层的逆变换，``decrypt_output``）；用户中断系统标记擦除；
    2. **状态栏段不受 ``_quill_activated`` gate 限制**（BASELINE §2.1
       不对称点：本段在 gate 检查之前执行，未激活也始终处理——刻意
       怪癖，快照钉住，勿"顺手"收紧）：
       - 开启 + ``_quill_status_handled`` 已置位（H2 工具钩子已处理）→
         只剥离 completion_text 中的残留标记，**不**二次渲染（F1 重复
         回复链路的一环——M3.0 有独立设计，本轮只做等价搬移不修）；
       - 开启 + 未置位 → 六级链提取渲染（``plugin._handle_status_bar``，
         M2.1 Mixin，MRO 动态分发）+ session_vars 统一持久化；无栏
         （handled=False）且有 persona 时追加兜底栏
         （``plugin._build_default_love_data``）；
       - 关闭 → 整套 ``plugin._strip_status_artifacts`` 擦除一切痕迹；
    2.5. **F1：SMT 回声置空**（M3.0 新增，位于状态栏段之后、注入报告
       段之前）。现象与根因（BASELINE §8.2 F1）：AstrBot 4.28.x 的
       ``send_message_to_user`` 直接发送并把**已发送纯文本**记入
       ``_send_message_to_user_current_session_plain_texts``（框架
       message_tools.py:349-361，值经 strip()）；respond.stage 以
       ``result.get_plain_text().strip()`` 与已发列表做**精确成员匹配**
       去重（respond/stage.py:189-207）。羽笔流程打破匹配：H2 把状态栏
       **渲染后**随工具文本发出（列表里是渲染版），本钩子把 completion
       回声里的原始标记**剥离**（completion 变体）——两者不等 → 框架
       去重失效 → 用户收到两条（实测：工具直发后 25s respond 再发剥离版）。

       处置：对 completion 与已发列表做**双向归一**比对（双方各经
       ``_normalized_reply_body``：``plugin._scrub_inject_report`` 抹除
       注入报告行 + ``plugin._strip_status_artifacts`` 剥离状态栏变体，
       比较正文），判定为已发内容的回声则**置空** ``resp.completion_text``
       ——H4 对 resp 的实际操作对象历来只有 completion_text（经
       LLMResponse 的 property 语义与 result_chain 互转），置空后框架侧
       不再产出可发文本：runner 对空 completion 不 yield llm_result
       （result_chain 为 None 时），result_chain 存在时链上只剩空 Plain、
       respond.stage 的 ``_is_empty_message_chain`` 走空链早退——从根上
       消除第二条消息，而非依赖框架那份注定失配的去重。

       宁漏勿误（只拦高置信回声，禁止模糊匹配）：已发 extra 缺失/非
       list/空列表/剥离后正文为空/归一后不等 → 一律放行原路径；"模型
       直接纯文本输出（未走工具）"场景下 extra 无记录，判定天然不触发，
       行为与修复前完全一致；比对自身异常只 debug 记日志放行，不影响
       本钩子其余段落。归一中抹除注入报告行的理由：报告行是 H2 追加在
       **已发侧**的插件产物、模型不会回声它，不抹则开启
       show_inject_report 时回声必然漏判；两侧对称抹除，仍是正文全等
       比对，无新增误杀面。
    3. 注入报告追加（``show_inject_report`` 开 + ``_quill_report_added``
       未置位 + 正文非空）——H2 工具路径与本路径都会跑到本函数，标记
       防两行报告；
    4. gate：未激活 return（步骤 2/3 在此之前，照跑）；
    5. 助手回复落 chat_logs（直接文本流路径——H5 只覆盖工具调用路径；
       ``rag_enable_chat_logging`` 开关 + retriever/memory_store 存在性
       前置判断 + ``_quill_assistant_logged`` 防双写标记**原样保留**；
       ``plugin._spawn`` 后台任务不阻塞响应）；
    6. 拒绝模式扫描：``refusal_enabled`` 开且正文命中任一模式 →
       ``mark_refusal`` 一次即 break。

    下沉决策（本轮评估记录）：**无新增下沉**。状态栏六级链/剥离器/兜底
    栏/会话开关解析均已在 M2.1 住 quill/services/statusbar/（经
    ``plugin._*`` 动态分发，与搬移前 ``self._*`` 同一路径）；其余各段——
    前置清洗、分支派发、chat_logs 落库判断、拒绝扫描——是 resp /
    event extra / config / retriever 上的框架对象胶水与控制流：拆成
    服务函数需要把 event extra 读写、防双写标记置位与短路求值时序一并
    拆出调用点（或改传参形态），强搬会把「逐字搬移」变成「重写」，违背
    本轮"不增加行为风险"的准绳（同 H2 轮对 JSON 回写/拒绝扫描的裁定）。
    """
    # [B:...] Base64 解码——安全网
    text = resp.completion_text or ""
    if text:
        decrypted = decrypt_output(text)
        if decrypted != text:
            resp.completion_text = decrypted
            logger.info(f"[Quill] 解密 [B:...]: {len(text)} -> {len(decrypted)}")

    content = resp.completion_text or ""
    sys_msg = "[SYSTEM: User actively interrupted the response generation. Partial output before interruption is preserved.]"
    if sys_msg in content:
        content = content.replace(sys_msg, "").strip()
        resp.completion_text = content

    # 状态栏处理
    target_id = plugin._get_target_id(event)

    # 会话级最终开关（与请求侧同一个解析函数，保证前后一致）
    _sb_effective = await plugin._effective_status_bar_enabled(target_id)
    _bar_tpl = plugin._status_bar_template_for(plugin._resolve_platform_name(event))

    if _sb_effective:

        if event.get_extra("_quill_status_handled"):
            # 工具钩子已处理完毕 — 仅剥离 resp.completion_text 中的
            # 原始状态栏残留（LLM 可能同时在 content 字段也输出了）
            content = resp.completion_text or ""
            stripped = plugin._strip_status_artifacts(content, plugin.props.love_fields)
            if stripped != content:
                resp.completion_text = stripped
                logger.info("[Quill] 已剥离 resp.completion_text 中的状态栏残留")
        else:
            # 工具钩子未命中 — 在此处作为最终安全网处理
            content = resp.completion_text or ""
            new_text, _, handled = await plugin._handle_status_bar(
                content, target_id, _bar_tpl
            )
            if not handled:
                persona_id = await plugin.state_manager.get_persona_id(target_id)
                if persona_id:
                    default_bar = await plugin._build_default_love_data(
                        target_id, _bar_tpl
                    )
                    new_text = (new_text or "") + "\n" + default_bar
                    logger.info("[Quill] 状态栏兜底注入")
            resp.completion_text = new_text

    else:
        # 禁用模式：彻底擦除所有状态栏痕迹
        content = resp.completion_text or ""
        resp.completion_text = plugin._strip_status_artifacts(
            content, plugin.props.love_fields
        )

    # ── F1（M3.0，BASELINE §8.2 F1）：SMT 回声置空 ────────────────────
    # 框架 respond.stage 对"工具直发文本的回声"只做精确匹配去重
    # （result.get_plain_text().strip() 与已发列表逐条比对）。羽笔流程
    # 打破匹配：H2 发出的是状态栏**渲染后**文本，而本钩子剥离的是
    # completion 回声里的**原始标记**变体——两个变体不等 → 框架去重失效
    # → 用户收到两条（实测间隔 25s）。
    #
    # 处置：在状态栏段产出之后做**双向归一**比对（双方各经
    # _normalized_reply_body：注入报告行抹除 + `_strip_status_artifacts`
    # 剥离状态栏变体），判定为已发内容的回声则**置空** completion——
    # 框架侧不再产出可发文本（runner 对空 completion 不 yield
    # llm_result；result_chain 存在时 respond.stage 的
    # `_is_empty_message_chain` 走空链早退），从根上消除第二条消息。
    #
    # 宁漏勿误（只拦高置信回声）：已发列表缺失/非 list/无记录/正文为空/
    # 归一后不等 → 一律放行原路径。位置在状态栏段之后、注入报告段之前：
    # 置空后报告段对空文本天然跳过，gate 后的落库/拒绝扫描同样短路。
    # 本会话工具未发过消息（模型直接纯文本输出，extra 无记录）时判定
    # 天然不触发，行为与本段加入前完全一致。
    try:
        sent_texts = event.get_extra(_SMT_SENT_TEXTS_KEY)
        if isinstance(sent_texts, list) and sent_texts:
            echo_body = _normalized_reply_body(
                plugin, resp.completion_text or ""
            )
            if echo_body:
                for sent_text in sent_texts:
                    if not isinstance(sent_text, str):
                        continue
                    if _normalized_reply_body(plugin, sent_text) == echo_body:
                        resp.completion_text = ""
                        logger.info(
                            "[Quill] completion 为已发工具消息的回声"
                            "（状态栏渲染/剥离变体归一后命中），已置空避免重复回复"
                        )
                        break
    except Exception as e:
        # 宁漏勿误：判定自身失败只放行原路径，不吞掉整个 H4
        logger.debug(f"[Quill] 回声判定异常，放行原路径: {e}", exc_info=True)

    # 注入报告（仅开关开启时）。工具路径已在 on_llm_tool_respond 里
    # 追加过，用标记去重——两条路径都会跑到本函数，否则会出现两行报告。
    if (plugin.props.show_inject_report and not event.get_extra("_quill_report_added")
            and (resp.completion_text or "").strip()):
        resp.completion_text = plugin._append_inject_report(
            resp.completion_text, target_id
        )
        event.set_extra("_quill_report_added", True)

    if not event.get_extra("_quill_activated"):
        return

    # ── 助手回复落日志（直接文本流路径）──
    # on_llm_tool_respond 仅覆盖 send_message_to_user 工具调用路径；
    # 模型直接输出文本时 completion_text 在此落日志，否则 chat_logs
    # 只有用户侧，断点续传与反思调度都缺半边对话。
    # _quill_assistant_logged 标记防止两条路径双写。
    if (not event.get_extra("_quill_assistant_logged")
            and (resp.completion_text or "").strip()
            and getattr(plugin.config, 'rag_enable_chat_logging', True)
            and plugin.rag_retriever and plugin.rag_retriever.memory_store):
        resp_pid = await plugin.state_manager.get_persona_id(target_id)
        plugin._spawn(plugin.rag_retriever.log_chat_message(
            plugin._get_memory_session_id(target_id, resp_pid),
            "assistant", (resp.completion_text or "").strip()
        ))
        event.set_extra("_quill_assistant_logged", True)

    if not plugin.props.refusal_enabled:
        return

    scan_text = resp.completion_text or ""
    if not scan_text:
        return

    for pattern in plugin.props.refusal_patterns:
        if pattern in scan_text:
            await plugin.state_manager.mark_refusal(target_id)
            logger.info(f"[Quill] 检测到拒绝模式 '{pattern}' (target={target_id})")
            break


async def handle_llm_tool_respond(
    plugin, event: AstrMessageEvent, tool: FunctionTool,
    tool_args: dict | None, tool_result
) -> None:
    """工具调用后拦截：Agent Loop 终止信号、动态记忆存储与多轮反思调度（H5）。

    （业务逻辑自 main.py 逐字搬移，M2.2 第五轮；``self`` → ``plugin``。
    行为快照见 tests/test_hook_snapshots.py H5 节，行为契约与降级怪癖见
    main.py 注册桩 docstring 与 BASELINE §2 H5 行。）

    **降级怪癖（全插件唯一，与 H1 同款"无 try 保留"形态，刻意保真）**：
    本函数与原 H5 一样**没有钩子级顶层 try**——三个早退 gate 之间、
    ``logger.info``、``_quill_memorized`` 置位与记忆块存在性 gate 求值的
    异常都会**原样上抛框架**；main.py 注册桩因此同样不做 try 包裹（快照
    用例 test_h5_exception_between_gates_propagates_no_top_try 钉住，防
    后续误补）。唯一的异常处理是两个内层块各自的 warning 吞掉：

    * 记忆存储调度（「记忆存储调度失败」）——覆盖 persona 读取、落库
      调度与整个反思调度；
    * 反思调度（「反思调度失败」）——随下沉整块迁入
      quill/services/memory.py（``schedule_reflection``）。

    处理链（顺序即行为，不得重排）：

    1. gate1 非 send_message_to_user 工具 → return；gate2
       ``_quill_activated`` 未置位 → return；gate3 ``_quill_memorized``
       去重已置位 → return（**不**把 ``_quill_activated`` 置 False——
       历史 bug，下方注释逐字保留，BASELINE §2.1）；
    2. ``_quill_memorized`` 置位（先于记忆块存在性 gate——去重不依赖
       记忆功能开关）；
    3. 记忆块（retriever + enable_memory + memory_store 全真值）：
       从 tool_args.messages 提取 AI 回复（JSON 字符串先解析，失败降级
       空文本；plain 段每段补 \\n 拼接），落 chat_logs（
       ``rag_enable_chat_logging`` 开关 + ``_quill_assistant_logged``
       防双写——与 H4 直接文本流路径互斥，BASELINE §4 写入侧）；
    4. N 轮反思调度：轮次判断 + 摘要/修剪/清理调度序列。

    下沉决策（本轮评估记录）：反思调度的"轮次判断 + 摘要/修剪/清理调度
    序列"依赖面规整（state_manager 轮次计数、retriever 的
    memory_store/summarize_contexts、``_spawn`` 后台任务、config 保留
    天数），以显式参数**成块下沉** quill/services/memory.py——内层
    warning 吞掉语义、阈值常量（随迁为模块常量，main.py 类属性
    re-export）、调用顺序逐字保真；``plugin.state_manager`` 等参数求值
    随之移到调用点（仍在外层记忆块 try 内），与原"内层 try 内求值
    self.state_manager"的差异仅在宿主属性缺失这种运行期不可达路径上。
    落库段（messages 提取 + event extra 防双写标记 + ``_spawn`` 调度）
    是框架对象胶水，与 H4 轮对落库段的裁定一致，留在本函数。
    """
    if tool.name != "send_message_to_user":
        return

    if not event.get_extra("_quill_activated"):
        return

    logger.info("[Quill] send_message_to_user 已调用")

    # 记忆/反思只做一次 —— 但**不能用总闸门来兼职**。
    #
    # 这里此前写的是 `event.set_extra("_quill_activated", False)`，而
    # `_quill_activated` 是本轮的**总闸门**，被 on_using_llm_tool(1771) 与
    # on_llm_response(2341) 读取。清掉它等于宣布「本轮插件下班」：此后
    # 所有工具调用都不再经过插件，状态栏不处理、残留不剥离。
    # 而模型在 agent 模式下会**多次**调用 send_message_to_user（正文一段、
    # 状态栏单独一段；实测 7 轮里 3 轮如此），第 2 次之后的内容就带着裸
    # [LOVE_DATA] 直达用户，看起来像「漏处理」。
    #
    # 拆成专用标记后语义单一：只保证记忆存储与反思调度不重复执行，
    # 不影响后续工具调用继续被处理。
    if event.get_extra("_quill_memorized"):
        return
    event.set_extra("_quill_memorized", True)

    # ── 动态记忆存储（异步后台任务，不阻塞响应）──
    if (plugin.rag_retriever and plugin.rag_retriever.enable_memory
            and plugin.rag_retriever.memory_store):
        try:
            user_input = getattr(event, 'message_str', '') or ""

            # 安全提取工具发出的文本内容（resp 不在当前函数签名中）
            ai_response = ""
            if tool_args and "messages" in tool_args:
                msgs = tool_args.get("messages", [])
                if isinstance(msgs, str):
                    try:
                        msgs = json.loads(msgs)
                    except Exception:
                        logger.debug("[Quill] tool messages JSON 解析失败，原样作为文本处理", exc_info=True)
                        msgs = []
                if isinstance(msgs, list):
                    for m in msgs:
                        if isinstance(m, dict) and m.get("type") == "plain" and "text" in m:
                            ai_response += m["text"] + "\n"

            # 存入记忆库（后台任务，异常在done回调中捕获）
            target_id = plugin._get_target_id(event)
            persona_id = await plugin.state_manager.get_persona_id(target_id)
            mem_session_id = plugin._get_memory_session_id(target_id, persona_id)

            # 记录 AI 回复到对话日志（始终保留，供断点续传使用；
            # 直接文本流已在 on_llm_response 落库时跳过，防双写）
            if ai_response.strip() and not event.get_extra("_quill_assistant_logged") \
                    and getattr(plugin.config, 'rag_enable_chat_logging', True):
                event.set_extra("_quill_assistant_logged", True)
                plugin._spawn(plugin.rag_retriever.log_chat_message(
                    mem_session_id, "assistant", ai_response.strip()
                ))

            # N 轮反思触发：攒够 N 轮对话后生成摘要（M2.2 第五轮下沉
            # quill/services/memory.py：内层 warning 吞掉语义、阈值常量、
            # 调用顺序逐字保真）
            await _memory_mod.schedule_reflection(
                plugin.state_manager, plugin.rag_retriever, plugin._spawn,
                plugin.config, target_id, mem_session_id,
            )

        except Exception as e:
            logger.warning(f"[Quill Memory] 记忆存储调度失败: {e}")

async def handle_llm_request(
    plugin, event: AstrMessageEvent, req: ProviderRequest
) -> None:
    """LLM 请求拦截：22 步注入编排本体（H3，BASELINE §4，顺序即行为）。

    （业务逻辑自 main.py 逐字搬移，M2.2 第六轮；``self`` → ``plugin``。
    行为快照见 tests/test_hook_snapshots.py H3 节，行为契约与顶层降级
    语义见 main.py 注册桩 docstring 与 BASELINE §2 H3 行。顶层
    try/except **不在本函数内**——降级层位在注册桩：``emergency``/
    ``extra_info`` 预初始化与 ``_sanitize_extra`` 脱敏摘要是降级语义的
    组成部分，随桩留在 main.py，任何异常由桩吞掉 + error 日志放行。）

    核心职责（原 H3 docstring 逐字保留）：
    - 平行宇宙双轴隔离 (target_id::persona_id)：按群+角色切分独立状态
    - 激活检测：决定本次请求是否进入 RP 模式
    - Context Restoration：req.contexts 为空时从 chat_logs 捞取最近 N 条垫入
    - First Message 智能抑制：避免重启后突兀复读开场白
    - 4 层 Prompt 装配：系统/角色/世界书/WR/RAG 多源注入
    - 状态栏降级解析：5 级兜底（STATUS 块→LOVE_DATA→legacy→RAW→lenient）

    22 步编排（BASELINE §4 一一对应，搬移前后顺序逐字一致，不得重排）：

    1.  ``_restore_smt_tool``（无条件、最先——SMT 请求级还原，§4.1）；
    2.  Context Restoration 垫回：类型守卫 + 注入报告行抹除 →
        contexts 空/≤1 且 ``rag_enable_chat_logging``（默认 True）且
        retriever.memory_store 存在 → ``get_recent_chat_logs`` 前插 8 条；
    3.  状态栏关闭时清洗历史 contexts 已渲染状态栏（``_sb_effective``
        在此求值，供步 15/20 复用——跨步存活值，不下沉的原因之一）；
    4.  ``_inject_persona_and_first_message``（[%None] 切断原生人格 +
        开场白首插）；
    5.  用户消息落 chat_logs（仅绑卡、非 ``/`` 指令——**在激活 gate 之前**，
        未激活也落，断点续传语义）；
    6.  自然语言核心记忆（``@记住：`` 前缀，``plugin._CORE_MEMORY_NL_RE``
        ——模块级正则经类属性 re-export 访问，H5 REFLECTION_* 先例）；
        改写 req.prompt + 群聊经 ``_check_group_permission`` 权限拦截 +
        后台写库；
    7.  最近 12 条存 ``_quill_recent_msgs`` extra；
    8.  拼多轮 context_text（末 4 条——纯函数段，本轮下沉
        quill/services/prompt.py ``build_context_text``，见下沉决策）；
    9.  ``_check_activation``（激活词/【】括号/WR 关键词；WR 匹配异常内部吞）；
    10. ``worldbook_always_activate`` 强制 ``activated=True, wr_activated=False``；
    11. **未激活 → reset_quill_rounds + return**（gate 本体，编排控制流）；
    12. ``increment_quill_rounds``；quill_rounds>1 → ``skip_constants``；
    13. ``_rewrite_smt_tool_description``（仅激活路径；无角色卡跳过）；
    14. emergency 检查 + extra_info（含 session_vars）；
    15. ``_prompt_builder_for_request(_sb_effective)``（浅拷贝对齐状态栏开关）；
    16. ``build_system_prompt``——世界书+WR 注入点（``worldbook_enabled``
        死开关的唯一消费点：关→传 None 整段跳过）；
    17. ``_run_rag_retrieval``——RAG 注入点（doc 检索→memory 检索→核心
        记忆无条件注入→format_for_prompt 追加 dynamic）；
    18. 世界书触发日志注入（worldbook_show_log，同步 get_trigger_log）；
    19. ``inject_prompt`` 合并（injection_position 透传）；
    20. tail message 追加 req.prompt（开：``build_status_reminder`` 契约
        提醒；关：禁止状态栏文案；幂等）；
    21. **``event.set_extra("_quill_activated", True)``**（闸门唯一点位，
        全部注入成功之后——快照 master 用例钉住其相对时序）；
    22. ``update_activity`` / ``clear_refusal``。

    下沉决策（本轮评估记录，宁可少下沉不可重排顺序）：

    * **步 8 context_text 拼接** → quill/services/prompt.py
      ``build_context_text``：22 步中唯一零 async、零插件实例状态、零控制
      流交织的纯函数段，显式参数即完整依赖面，调用点原位一行替换（块内
      对 req.contexts 的 isinstance 守卫随迁，求值时序不变）。
    * **步 2-3 垫回/历史清洗块不下沉**：块内求值的 ``mem_session_id``（供
      步 5/6 使用）与 ``_sb_effective``（供步 15/20 使用）是跨步存活值，
      下沉需以返回值/出参形态交还编排层，传参形态与求值时序都要改；且
      块内三个动态分发点（``_scrub_inject_report`` /
      ``_effective_status_bar_enabled`` / ``_strip_status_artifacts``）使
      服务函数要么收 plugin（伪解耦的代码搬家）要么改传参——强搬=重写。
    * **步 9-12 gate 块不下沉**：步 11 是早退 return（编排控制流本体），
      下沉需要哨兵返回值改变控制流形状；顺序即行为的核心段。
    * **步 6 核心记忆块不下沉**：权限校验经根包 commands 模块函数
      （``_check_group_permission``，本模块顶部直接 import——同
      ``..encryption`` 先例；quill/services 依赖根包会反转分层，故整块
      留此）；步 1/4/9/13/17 的既有方法仍住 main.py 类上（经 plugin
      动态分发，与搬移前 self.* 同一路径）。
    * **步 20 tail 块不下沉**：req.prompt 的幂等拼接 + 空 prompt 分支是
      req 对象胶水；契约文本已由 PromptBuilder 单一来源生成（步 20 无
      重复逻辑可收敛）。

    其余各段（extra_info 组装、触发日志、终态日志等）是 event/req/config
    上的框架对象胶水，同 H2/H4 轮裁定，留在本编排函数内。
    """
    plugin._restore_smt_tool(req)

    user_input = req.prompt or ""
    target_id = plugin._get_target_id(event)

    # ── 上下文恢复（重启/滑动窗口切断后无缝续传）──
    mem_session_id = plugin._get_memory_session_id(
        target_id,
        await plugin.state_manager.get_persona_id(target_id)
    )
    # 防御性类型守卫：AstrBot 框架契约保证 contexts 为 list，但防止异常值导致崩溃
    if not isinstance(req.contexts, list):
        req.contexts = []
    # 抹掉历史里的注入报告行：它只该出现在用户看到的那条消息里，
    # 回显进上下文会被模型模仿（下一轮自己写一行），且对本轮推理无价值。
    req.contexts = [
        ({**c, "content": plugin._scrub_inject_report(c.get("content", ""))}
         if isinstance(c, dict) and isinstance(c.get("content"), str) else c)
        for c in req.contexts
    ]
    contexts_is_fresh = not req.contexts or len(req.contexts) <= 1
    if contexts_is_fresh \
            and getattr(plugin.config, 'rag_enable_chat_logging', True) \
            and plugin.rag_retriever and plugin.rag_retriever.memory_store:
        recent_logs = await plugin.rag_retriever.memory_store.get_recent_chat_logs(mem_session_id, limit=8)
        if recent_logs:
            req.contexts = recent_logs + req.contexts
            logger.info(f"[Quill Context] 恢复 {len(recent_logs)} 条上下文（Session: {mem_session_id}）")

    # 关闭状态栏时，抹掉回灌上下文里已渲染的历史状态栏。
    # 不清掉就是一边用 tail message 明令「禁止输出好感度、关系阶段、心情」，
    # 一边在历史里给模型看几轮「好感度：85」的示范，属于自己和自己拉锯：
    # 模型倾向于模仿历史（可见性由读侧剥离兜住，但说服力被白白消耗）。
    # 必须放在上下文恢复之后：恢复来的 chat_logs 同样带着状态栏。
    # 开启方向不处理——历史里本来就没有栏，tail message 会教它写。
    # 用会话级最终开关判断：/quill statusbar off 之后同样要清历史示范。
    _sb_effective = await plugin._effective_status_bar_enabled(target_id)
    if not _sb_effective and req.contexts:
        _scrubbed = []
        for c in req.contexts:
            if isinstance(c, dict) and isinstance(c.get("content"), str):
                clean = plugin._strip_status_artifacts(
                    c["content"], plugin.props.love_fields
                )
                _scrubbed.append({**c, "content": clean} if clean != c["content"] else c)
            else:
                _scrubbed.append(c)
        req.contexts = _scrubbed

    persona_id, persona_data = await plugin._inject_persona_and_first_message(req, event, target_id)

    # 记录用户消息（仅已绑定角色卡且非指令时）
    if persona_id and user_input and not user_input.strip().startswith("/") \
            and getattr(plugin.config, 'rag_enable_chat_logging', True) \
            and plugin.rag_retriever:
        plugin._spawn(plugin.rag_retriever.log_chat_message(
            mem_session_id, "user", user_input
        ))

    # P1-8: 自然语言核心记忆注入 — 检测 @记住 / 核心记忆 / @remember 前缀
    # 审查修复：切片统一以 stripped 文本为基准（此前 strip 后匹配、原文切片，
    # 带前导空白时 prompt 残留尾部字符）；剥离后为空则保留原文（避免空 prompt
    # 仍发给 LLM）；群聊写入需通过 admin 权限校验（与 /memory core 对齐）。
    _core_nl = None
    if persona_id and user_input and plugin.rag_retriever and plugin.rag_retriever.memory_store:
        _stripped = user_input.strip()
        _core_match = plugin._CORE_MEMORY_NL_RE.match(_stripped)
        if _core_match:
            _perm_err = _check_group_permission(plugin, event)
            if _perm_err:
                logger.info("[Quill] 核心记忆自然语言注入被权限拦截（群聊非 admin）")
            else:
                _core_nl = _core_match.group(1).strip()
                _rest = _stripped[_core_match.end():].strip()
                if _rest:
                    req.prompt = _rest
                logger.info(f"[Quill] 检测到核心记忆自然语言注入: {_core_nl[:80]}...")
    # 异步写入核心记忆（不阻塞请求流程）
    if _core_nl:
        plugin._spawn(plugin.rag_memory_store.update_core_memory(
            mem_session_id, _core_nl, _core_nl
        ))

    # 存储最近 6 轮对话，供 /memory learn 自动总结
    if hasattr(req, 'contexts') and isinstance(req.contexts, list):
        # P3-5 修复：深拷贝切片，避免后续 req.contexts 被修改（如上下文恢复）后引用失效
        recent_msgs = [dict(c) for c in req.contexts[-12:] if c.get("role") in ("user", "assistant")]
        event.set_extra("_quill_recent_msgs", recent_msgs)

    # Build multi-turn context for WR matching
    # （M2.2 第六轮下沉：纯函数段迁 quill/services/prompt.py，见下沉决策）
    context_text = _prompt_mod.build_context_text(user_input, req.contexts)

    activated, wr_activated = await plugin._check_activation(user_input, context_text, persona_data)
    has_bracket = plugin.activation_detector.check_brackets(user_input)

    # 全局常驻模式：跳过激活检测
    always_activate = getattr(plugin.config, "worldbook_always_activate", False)
    if always_activate:
        activated = True
        wr_activated = False

    if not (activated or has_bracket or wr_activated):
        await plugin.state_manager.reset_quill_rounds(target_id)
        return

    quill_rounds = await plugin.state_manager.increment_quill_rounds(target_id)
    skip_constants = quill_rounds > 1
    if skip_constants:
        logger.info(f"[Quill] 连续第 {quill_rounds} 轮激活，跳过 Layer 1 常驻")

    # 改写 send_message_to_user 描述（含状态栏强制要求）
    # 无角色卡时跳过：避免 LLM 在无人设约束时进入 Agent 死循环
    await plugin._rewrite_smt_tool_description(req, persona_id)

    if wr_activated and plugin.wr_manager and plugin.props.debug:
        try:
            debug_match = await plugin.wr_manager.match(context_text, top_k=10, log_match=False)
            for e in debug_match:
                logger.info(
                    f"[Quill] WR 匹配: {e.get('entry_id','')} "
                    f"(score={e.get('match_score',0)}, "
                    f"kw={e.get('keywords',[])})"
                )
        except Exception:
            logger.debug("[Quill] 调试 WR 匹配失败", exc_info=True)

    emergency = await plugin.state_manager.should_inject_emergency(target_id)

    extra_info = {
        "user_input": user_input,
        "context_text": context_text,
        "persona_id": persona_id,
        "persona_data": persona_data,
        "user_id": target_id,
        "wr_max_entries": plugin.props.wr_max_entries,
        "wr_fallback_top_count": plugin.props.wr_fallback_top_count,
        "wb_max_entries": plugin.props.wb_max_entries,
        "wb_sensitivity": plugin.config.worldbook_sensitivity,
        "wb_max_token": plugin.config.worldbook_max_token,
        "skip_constants": skip_constants,
        "session_vars": await plugin.state_manager.get_session_vars(target_id),
    }

    # 注入报告统计（本轮各来源命中条数）。始终采集——即使 debug 关闭，
    # 本轮最终开关：会话级覆盖 > 面板全局。上面（历史 contexts 清理）与
    # 下面（system prompt 契约、tail message）必须用同一个值，否则会出现
    # 「system prompt 说别输出、tail 说必须输出」的自相矛盾。
    _pb = plugin._prompt_builder_for_request(_sb_effective)

    # /quill debug 也要能查上一轮，见 _last_inject_report。
    inject_stats: dict = {}
    # 世界书总开关（默认 True）。此前 `worldbook.enabled` 只在 config.py
    # 解析与 __repr__ 里出现，运行期**没有任何消费者**——面板上关掉它
    # 世界书照样注入，属「改了不生效」的死开关。这里把它接到唯一的
    # 注入点上：关掉就传 None，让 PromptBuilder 跳过全部世界书逻辑
    # （常驻+关键词匹配）。传 None 而不是加新参数，是因为
    # `build_system_prompt` 各处判断的都是 `if wb_manager`，
    # 置空即可整段跳过，且不改变函数签名（prompt_builder 自检里
    # 就有 `build_system_prompt(None, None, {})` 的用法）。
    # 按角色卡绑定的 wb_mode 仍在其上层生效：两者是「总闸 × 分闸」。
    _wb_for_request = plugin.wb_manager if getattr(
        plugin.config, "worldbook_enabled", True
    ) else None
    stable_prompt, dynamic_prompt = await _pb.build_system_prompt(
        plugin.wr_manager, _wb_for_request, extra_info, emergency=emergency,
        stats=inject_stats,
    )

    # ── RAG 检索（Doc RAG + 动态记忆）──
    dynamic_prompt = await plugin._run_rag_retrieval(
        event, req, user_input, persona_data, dynamic_prompt, inject_stats
    )

    # 触发日志注入（show_trigger_log 开启时）
    if (plugin.config.worldbook_show_log and _wb_for_request
            and hasattr(_wb_for_request, 'get_trigger_log')):
        # get_trigger_log 是同步方法（加锁读一次列表），不能 await
        trigger_log = _wb_for_request.get_trigger_log()
        if trigger_log:
            log_lines = ["[触发日志]"]
            for t in trigger_log[:10]:
                log_lines.append(f"  {t['worldbook']}/{t['title']} ← {','.join(t['matched_keys'])}")
            dynamic_prompt += "\n\n" + "\n".join(log_lines)

    plugin._remember_inject_report(target_id, inject_stats)

    req.system_prompt = plugin.props.prompt_builder.inject_prompt(
        req.system_prompt or "", stable_prompt, dynamic_prompt,
        injection_position=plugin.config.worldbook_injection_pos
    )

    if persona_id:
        if _sb_effective:
            # 契约文本由 PromptBuilder 单一来源生成（格式行/示例/选项块），
            # 此处不再手抄示例——此前四处各写一份，字段名或顺序一变就漂移。
            tail = "\n\n[System] " + _pb.build_status_reminder()
        else:
            tail = (
                "\n\n[System] 禁止输出任何格式的状态栏、[LOVE_DATA]、"
                "[STATUS]、好感度数值、关系阶段、心情标签、穿着描述、"
                "位置信息、剧情走向选项等内容。请仅输出纯剧情正文。"
            )
        if req.prompt and tail not in req.prompt:
            req.prompt += tail
        elif not req.prompt:
            req.prompt = tail

    event.set_extra("_quill_activated", True)

    await plugin.state_manager.update_activity(target_id)
    await plugin.state_manager.clear_refusal(target_id)

    trigger = "激活词" if activated else ("括号" if has_bracket else "WR关键词")
    _es = event.get_extra("enable_streaming")
    if _es is True:
        streaming_status = "强制流式"
    elif _es is False:
        streaming_status = "已关"
    else:
        streaming_status = "默认"
    logger.info(
        f"[Quill] 触发:{trigger} | 流式:{streaming_status} | "
        f"prompt_len={len(req.system_prompt)} | emergency={emergency}"
    )
