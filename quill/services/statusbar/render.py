# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏子系统——渲染侧（自 main.py 原位搬移，M2.1）。

内容（均为原 QuillPlugin 方法，方法体一字不改）：

- ``_format_love_data``：``[LOVE_DATA]`` 单行解析 + 按字段表格式化
  （L2 的产出函数，返回 (updates, formatted, raw_line) 三元组）；
- ``_build_default_love_data``：LLM 未输出状态栏时的兜底栏（历史值 +
  占位符 + 剧情走向选项块，套平台模板）；
- ``_effective_status_bar_enabled``：会话级覆盖 > 面板全局的最终开关；
- ``_resolve_platform_name`` / ``_status_bar_template_for``：平台分治的
  模板选型（纯文本平台 vs Markdown 平台）。

搬移纪律：仅 logger 经 logbridge 桥接（方法体写法不变），其余零改动。
self.* 属性（love_fields、status_bar_*、state_manager）由组合后的
QuillPlugin 提供。
"""

from __future__ import annotations

from ...core.logbridge import logger  # noqa: F401  # 方法体 logger 引用经 logbridge 桥接
from .parsers import _LOVE_DATA_RE


class StatusbarRenderMixin:
    """状态栏渲染侧 Mixin（自 QuillPlugin 原位搬移）。

    解析侧见 parsers.StatusbarParsersMixin；两者都只承载方法，状态由
    QuillPlugin 提供（MRO：QuillPlugin → StatusbarParsersMixin → 本类
    → Star）。
    """

    async def _effective_status_bar_enabled(self, target_id: str) -> bool:
        """解析状态栏的最终开关：会话级覆盖 > 面板全局。

        `/quill statusbar on|off|auto` 写的是会话级覆盖值，面板开关是全局默认。
        两者语义一致（都是「是否启用状态栏」），只是粒度不同：
          auto —— 跟随面板全局（默认）
          on   —— 本会话强制开（即使面板关着）
          off  —— 本会话强制关（即使面板开着）

        每次都现读 state（内存读 + 锁，成本可忽略），不做缓存：用户刚
        敲完指令的下一轮就要生效，缓存会引入「改了不生效」的窗口。
        """
        mode = "auto"
        try:
            mode = await self.state_manager.get_status_bar_mode(target_id)
        except Exception:
            logger.debug("[Quill] 读取会话级状态栏开关失败", exc_info=True)
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self.status_bar_enabled

    # ── 状态栏模板选型（平台分治）───────────────────────────────
    # 状态栏最终是「模板 + 内容」拼出来的，而模板默认是 Markdown
    # （`**状态栏**\n```\n{content}\n```）。不渲染 Markdown 的平台上，
    # `**` 和围栏会原样显示给用户，所以这些平台改用纯文本模板。
    #
    # 平台名在每条消息上才拿得到（event.platform_meta），而模板拼接发生在
    # _handle_status_bar 内部，因此把选好的模板作为参数传进去，而不是在
    # 渲染处再回头去问 event。
    @staticmethod
    def _resolve_platform_name(event) -> str:
        """取平台适配器名（小写）。取不到返回空串。"""
        try:
            pm = getattr(event, "platform_meta", None)
            if pm is not None:
                name = (getattr(pm, "name", "") or "").strip().lower()
                if name:
                    return name
        except Exception:
            logger.debug("[Quill] platform_meta.name 获取失败", exc_info=True)
        try:
            return (event.get_platform_name() or "").strip().lower()
        except Exception:
            logger.debug("[Quill] get_platform_name() 获取失败", exc_info=True)
        return ""

    def _status_bar_template_for(self, platform: str) -> str:
        """按平台返回该用的状态栏模板。

        纯文本平台走 status_bar_format_plain，其余（含未知平台）走
        status_bar_format。未知平台保持原行为是刻意的：它可能是支持
        Markdown 的新适配器，贸然改成纯文本反而破坏渲染。
        """
        plain = [p for p in (getattr(self, "status_bar_plain_platforms", None) or []) if p]
        if platform and plain and platform in plain:
            return self.status_bar_format_plain
        return self.status_bar_format_template

    def _format_love_data(self, content: str) -> tuple:
        """Parse [LOVE_DATA] line, return (updates_dict, formatted_text, raw_line) or (None, None, None)."""
        m = _LOVE_DATA_RE.search(content)
        if not m:
            return None, None, None
        raw_data = m.group(1).strip()
        parts = [p.strip() for p in raw_data.split("|")]
        updates = {}
        formatted_lines = []
        for i, field_name in enumerate(self.love_fields):
            val = parts[i] if i < len(parts) else ""
            updates[field_name] = val
            formatted_lines.append(f"{field_name}：{val}")
        formatted = "\n".join(formatted_lines)
        return updates, formatted, m.group(0)

    async def _build_default_love_data(
        self, target_id: str, bar_template: str | None = None
    ) -> str:
        """构建默认状态栏（当 LLM 未输出状态栏时兜底）。

        bar_template：与 _handle_status_bar 同源的模板参数——兜底栏也要按平台
        分治，否则 QQ 上正常轮次是纯文本、兜底轮次却冒出 ``` 围栏。
        """
        template = bar_template or self.status_bar_format_template
        vars = await self.state_manager.get_session_vars(target_id)
        parts = []
        for field_name in self.love_fields:
            val = vars.get(field_name, "")
            parts.append(val if val else self.status_bar_default_placeholder)
        love_section = "\n".join(f"{f}：{v}" for f, v in zip(self.love_fields, parts))
        plot_section = "\n\n>>> 剧情走向 <<<\n" + "\n".join(
            f"{i+1}. {p}" for i, p in enumerate(self.status_bar_plot_paths)
        ) + "\n<<< 请选择 >>>"
        full_content = love_section + plot_section
        return template.replace("{content}", full_content)
