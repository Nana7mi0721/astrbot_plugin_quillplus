# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AstrBot 事件钩子的实现函数（v5.3.0 M2.2 钩子薄化）。

架构修订（BASELINE §1.2，真机 4.28.1 源码实证）：钩子/指令函数的
``__module__`` 必须与插件注册路径（``data.plugins.<目录>.main``）精确
相等才会被框架绑定与分发——因此**注册桩**（装饰器 + 签名 + priority）
留在 main.py 类体，桩体一行委托到本模块的实现函数；业务逻辑逐字下沉
于此。M2.2 第一轮仅 H6（on_decorating_result），其余五钩子随后续轮次
按同一形态迁入。

降级语义分层：顶层 try/except 留在 main.py 注册桩内（与原 H6 的
"顶层吞掉放行"同层，不因委托而改变降级位置）；本模块实现体内**不再**
重复包裹——桩内已保证任何异常都不会外抛中断发送。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


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
