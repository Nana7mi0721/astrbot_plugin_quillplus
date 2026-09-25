# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AstrBot 事件钩子的实现函数（v5.3.0 M2.2 钩子薄化）。

架构修订（BASELINE §1.2，真机 4.28.1 源码实证）：钩子/指令函数的
``__module__`` 必须与插件注册路径（``data.plugins.<目录>.main``）精确
相等才会被框架绑定与分发——因此**注册桩**（装饰器 + 签名 + priority）
留在 main.py 类体，桩体一行委托到本模块的实现函数；业务逻辑逐字下沉
于此。M2.2 第一轮 H6（on_decorating_result）、第二轮 H1
（on_waiting_llm_request）、第三轮 H2（on_using_llm_tool），其余三钩子
随后续轮次按同一形态迁入。

降级语义分层：顶层 try/except 留在 main.py 注册桩内（与原 H6 的
"顶层吞掉放行"同层，不因委托而改变降级位置）；本模块实现体内**不再**
重复包裹——桩内已保证任何异常都不会外抛中断发送。

**例外——H1（第二轮起）**：原 H1 本就**无钩子级顶层 try**（BASELINE §2
H1 行的降级怪癖：`state_manager.get_state` 抛出会上抛框架）。其注册桩
因此**不做**任何 try 包裹，本模块的 ``handle_waiting_llm_request`` 也
只保留原有的"内层取值 except: return"小块——降级位置原样保真。
"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import FunctionTool

from ..quill.services import response as _response_mod


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
        if stripped != text:
            comp.text = stripped
            cleaned += 1
    if cleaned:
        logger.info(
            f"[Quill] 发送前擦除 {cleaned} 段状态栏残留"
            f"（{'原始标记' if enabled else '全套剥离'}）"
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
    3. telegram/tg 平台剥离 plain 段 Markdown（逐字下沉
       quill/services/response.py，调用点一行）；
    4. 状态栏（全平台）：开启时首条 plain 走六级链
       （``plugin._handle_status_bar``，M2.1 Mixin，MRO 动态分发），
       handled 后 set ``_quill_status_handled``，后续 plain 只清残留；
       关闭时整套 ``_strip_status_artifacts``；注入报告追加到最后一条
       plain（``_append_inject_report``）；
    5. JSON 回写（was_string 时序列化回去）；
    6. 拒绝模式补充扫描（S3-2：completion_text 为空时拒绝内容藏于
       tool_args.messages，只扫首条 plain，命中 mark_refusal）。

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
