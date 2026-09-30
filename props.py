# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillConfigProperties — 配置属性访问器层（M2.3，债务 D2 消解）。

v5.2.5 的反模式（BASELINE D2）：__init__ 把一批 config 字段复制成插件实例
属性（`self.status_bar_enabled = self.config.status_bar_enabled` …），
`save_plugin_configs()` 保存成功后逐属性手工同步、失败后逐属性手工回滚
（约 174 行，其中大半是为这套投影存在的）。加新配置项必须记得登记投影，
漏登记不报错，只有「不重启就不生效」一种症状，定位成本极高。

v5.3.0 的目标形态（PLAN §M2.3）：**属性访问器 + 唯一真源**。

- 本类持有**插件实例引用**（不是 config 快照）：每个属性实时读
  ``self._plugin.config.X``。``save_plugin_configs()`` 整体替换
  ``plugin.config`` 后，下一次属性读自动拿到新值——同步逻辑从「人肉
  逐属性抄写」变成「引用替换天然生效」。
- 读 dict 属性成本可忽略，默认全部实时读、不加缓存；实测发现瓶颈再
  引入缓存 + 显式 refresh（PLAN §5.2 的既定决策）。唯一例外是
  ``prompt_builder``（见该 property 的 docstring）。
- 守卫：``tests/legacy/test_config_projection.py`` 改造后看守「访问器
  覆盖完整性」——QuillConfig 每个字段必须有同名/别名 property 或显式
  豁免理由，防止「加了配置项忘了加访问器」的新形态漂移。

放置位置（插件根而非 quill/ 包）：访问器消费方是 main.py / commands.py /
web_routes.py 等宿主侧模块，而架构守卫只约束 quill/ 包；本文件属于
接口/宿主侧，不是与框架解耦的核心逻辑。
"""

from __future__ import annotations

from .prompt_builder import PromptBuilder


class QuillConfigProperties:
    """QuillPlugin 的配置访问器（v5.2.5 的 19 个投影实例属性的替代物）。

    用法（QuillPlugin.__init__）::

        self.props = QuillConfigProperties(self)
        # 之后运行期一律 self.props.status_bar_enabled 而非 self.status_bar_enabled
    """

    def __init__(self, plugin):
        # 持插件引用而非 config 对象：config 在 save_plugin_configs 里被
        # **整体替换**（self.config = QuillConfig(...)），持有旧对象会变成
        # 快照。逐属性实时经 self._plugin.config 取值，永远读到当前生效者。
        self._plugin = plugin
        # prompt_builder 的按代缓存状态（见 prompt_builder property docstring）
        self._prompt_builder: PromptBuilder | None = None
        self._prompt_builder_config = None

    # ── writing_resource ──────────────────────────────────────────

    @property
    def wr_max_entries(self) -> int:
        """WR 最大注入条数（config.writing_resource.max_entries）。"""
        return self._plugin.config.wr_max_entries

    @property
    def wr_fallback_top_count(self) -> int:
        """WR 匹配不足时的兜底条数（config.writing_resource.fallback_top_count）。"""
        return self._plugin.config.wr_fallback_top

    # ── worldbook ─────────────────────────────────────────────────

    @property
    def wb_max_entries(self) -> int:
        """世界书动态注入上限（config.worldbook.max_dynamic_entries）。"""
        return self._plugin.config.worldbook_max_dynamic

    @property
    def worldbook_always_activate(self) -> bool:
        """全局常驻模式开关（config.worldbook.always_activate）。

        消费方（on_llm_request 的激活判定）经本访问器或 self.config 直读，
        面板改值后当轮生效。
        """
        return self._plugin.config.worldbook_always_activate

    # ── prompt ────────────────────────────────────────────────────

    @property
    def prompt_builder(self) -> PromptBuilder:
        """PromptBuilder 访问器（按 config 代缓存）。

        为什么不能像其他属性那样每次现读：PromptBuilder 在 __init__ 时把
        config 的若干字段快照进实例（max_prompt_length、status_bar_enabled、
        love_fields、status_bar_plot_paths），且 ``_prompt_builder_for_request``
        依赖「同一 config 代内拿到同一实例」来做浅拷贝与身份比较。

        缓存策略：按 config **对象身份**判代——save_plugin_configs 整体
        替换 plugin.config 后首次访问自动重建（等价旧「保存成功重建」），
        保存失败回滚恢复旧 config 引用时自动复用旧实例（等价旧「失败
        回滚」），save 流程因此无需再手工重建它。
        """
        config = self._plugin.config
        if self._prompt_builder is None or self._prompt_builder_config is not config:
            self._prompt_builder = PromptBuilder(config)
            self._prompt_builder_config = config
        return self._prompt_builder

    # ── refusal ───────────────────────────────────────────────────

    @property
    def refusal_enabled(self) -> bool:
        """拒绝语扫描总开关（config.refusal.enabled）。"""
        return self._plugin.config.refusal_enabled

    @property
    def refusal_patterns(self) -> list:
        """拒绝语模式列表（config.refusal.patterns）。"""
        return self._plugin.config.refusal_patterns

    # ── status_bar ────────────────────────────────────────────────

    @property
    def status_bar_enabled(self) -> bool:
        """状态栏全局开关（config.status_bar.enabled）。

        注意：运行期最终开关是「会话级覆盖 > 全局」，见
        StatusbarRenderMixin._effective_status_bar_enabled。
        """
        return self._plugin.config.status_bar_enabled

    @property
    def status_bar_format_template(self) -> str:
        """Markdown 平台的状态栏模板（config.status_bar.format_template）。"""
        return self._plugin.config.status_bar_format

    @property
    def status_bar_format_plain(self) -> str:
        """纯文本平台的状态栏模板（config.status_bar.format_template_plain）。"""
        return self._plugin.config.status_bar_format_plain

    @property
    def status_bar_plain_platforms(self) -> list:
        """按纯文本模板渲染的平台适配器名列表（config.status_bar.plain_platforms）。"""
        return self._plugin.config.status_bar_plain_platforms

    @property
    def love_fields(self) -> list:
        """状态栏字段表（config.status_bar.fields）。

        命名沿革：投影时代为可读性从 config.status_bar_fields 改名而来，
        状态栏解析/渲染/剥离全链路以此为唯一字段来源。
        """
        return self._plugin.config.status_bar_fields

    @property
    def status_bar_plot_paths(self) -> list:
        """剧情走向选项表（config.status_bar.plot_paths）。"""
        return self._plugin.config.status_bar_plot_paths

    @property
    def status_bar_jev_enabled(self) -> bool:
        """JEV 模式总开关（config.status_bar.jev_enabled）。"""
        return self._plugin.config.status_bar_jev_enabled

    @property
    def status_bar_jev_provider_id(self) -> str:
        """JEV 判定模型对应的 AstrBot 提供商 id（config.status_bar.jev_provider_id）。"""
        return self._plugin.config.status_bar_jev_provider_id

    @property
    def status_bar_jev_confidence_floor(self) -> float:
        """JEV 推荐选择度显示/分支路由的置信度下限（config.status_bar.jev_confidence_floor）。"""
        return self._plugin.config.status_bar_jev_confidence_floor

    @property
    def status_bar_default_placeholder(self) -> str:
        """字段缺值时的占位符（config.status_bar.default_placeholder）。"""
        return self._plugin.config.status_bar_default_placeholder

    @property
    def status_bar_show_delta(self) -> bool:
        """数值变化标注开关（config.status_bar.show_delta）。"""
        return self._plugin.config.status_bar_show_delta

    # ── debug ─────────────────────────────────────────────────────

    @property
    def debug(self) -> bool:
        """控制台 debug 日志开关（config.debug.enabled）。"""
        return self._plugin.config.debug_enabled

    @property
    def show_inject_report(self) -> bool:
        """聊天内注入报告开关（config.debug.show_inject_report）。

        与 debug 相互独立（受众与副作用不同，见 config.py 中的注释）。
        """
        return self._plugin.config.show_inject_report

    # ── rag（运行期日志/修剪参数）──────────────────────────────────

    @property
    def rag_enable_chat_logging(self) -> bool:
        """对话日志记录开关（config.rag.enable_chat_logging）。

        v5.2.5 的投影清单里它只有赋值没有读者（消费方全部直接读
        self.config）；保留访问器以维持 19 项访问器覆盖的完整性，
        也给未来的属性级消费一个统一入口。
        """
        return self._plugin.config.rag_enable_chat_logging

    @property
    def rag_chat_log_retention_days(self) -> int:
        """对话日志保留天数（config.rag.chat_log_retention_days）。

        同上：v5.2.5 投影清单中的「只写不读」项，访问器保留理由同上。
        """
        return self._plugin.config.rag_chat_log_retention_days
