# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillPlugin — 羽笔 v5.0 多维沉浸式 RP 增强插件

五合一沉浸式 RP 注入系统：世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆。

核心架构：
- 平行宇宙双轴隔离 (target_id::persona_id)：彻底根治群聊切卡串戏
- JSON 原子化状态机：tmp+fsync+os.replace 四连防数据损坏
- 全链路纯异步防阻塞：asyncio.to_thread 包裹所有 IO/DB 操作
- 无损对话日志归档：Context Restoration 断点续传，重启零失忆
- FAISS + SQLite 事务一致性：row_id 映射 + L2 归一化 + 幽灵向量回收
- 4 层 Prompt 装配 + 强制 Tool Description 重写

v5.0 变化:
- 配置由 AstrBot 通过 _conf_schema.json 注入（不再读 config.yaml）
- 全部行为由 QuillConfig 控制
- admin_users 收窄为仅群聊写指令权限控制，Web 面板信任 AstrBot 鉴权
"""

import asyncio
import copy
import json
import os
import re

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.star.register import register_command
from astrbot.core.star.filter.command import GreedyStr

from astrbot.api import logger

from .config import QuillConfig
from ._paths import resolve_data_layout
from .activation import ActivationDetector
from .state import StateManager
from .kb import WritingResourceManager
from .worldbook import WorldbookManager
from .props import QuillConfigProperties
# PromptBuilder 本体已由 props.py 直接构造（M2.3）；此处保留 re-export 是
# M2.0 搬移期约定——tests/legacy 与 probe 脚本经 main 模块取旧导入面。
from .prompt_builder import PromptBuilder  # noqa: F401  (legacy/probe 导入面)
from . import commands as _cmds
from .web_routes import QuillRoutes
# M2.2 第四轮：H4 的 [B:...] 解密消费方已随钩子实现迁至
# interfaces/astrbot_hooks.py（实现侧自行 import）；此处保留旧导入面
# （M2.0 搬移期约定，同 strip_markdown 的 re-export 处理）。
from .encryption import decrypt_output  # noqa: F401  (legacy/probe 导入面)
from .persona_manager import QuillPersonaManager
from .quill.core import logbridge
# M2.2 剥离器下沉：实现住 quill/services/statusbar/strip.py，
# QuillPlugin 类体内保留同名薄转发（见「状态栏解析共享方法」段）。
from .quill.services.statusbar import strip as _strip_mod
# M2.2 第二轮：角色卡→对话隔离下沉 quill/services/character.py
# （_ensure_persona_conversation 保留同名薄转发，见彼处）。
from .quill.services import character as _character_mod
# M2.2 第五轮：H5 反思调度下沉 quill/services/memory.py；阈值常量在类属性
# 处 re-export（QuillPlugin.REFLECTION_* 旧访问面）。
from .quill.services import memory as _memory_mod
# M2.2 第三轮：H2 的 telegram Markdown 剥离下沉 quill/services/response.py
# （_MD_PATTERNS/strip_markdown 在原 main.py 即为零状态模块级纯函数，唯一
# 消费方是 H2 on_using_llm_tool；此处 re-export 维持 main 模块旧导入面，
# M2.0 搬移期约定）。实现与完整设计理由（Telegram 无 parse_mode 为何要擦）
# 见 response.py。
from .quill.services.response import _MD_PATTERNS, strip_markdown  # noqa: F401
from .quill.services.statusbar import (
    LOVE_DATA_TAG,
    STATUS_END_TAG,
    STATUS_TAG,
    StatusbarParsersMixin,
    StatusbarRenderMixin,
    # ── 搬移期 re-export（M2.0 约定）：状态栏解析侧常量/纯函数已搬至
    # quill/services/statusbar/parsers.py，这里保留旧模块级名字，
    # 供 tests/legacy、probe 脚本的旧 import 面使用。
    # （剥离器已于 M2.2 下沉 strip.py，不再从本文件取这些名字。）
    _DEFAULT_LOVE_FIELDS_RAW,
    _LOVE_DATA_RE,
    _PLOT_PATH_RE,
    _STATUS_BLOCK_RE,
    _STATUS_RE,
    _StatusLevelContext,
    _StatusLevelResult,
    _annotate_changes,
    _build_raw_status_re,
    _extract_numeric,
    _format_delta,
    _normalize_status_value,
)
# M2.2 钩子薄化：实现函数住 interfaces/astrbot_hooks.py（BASELINE §1.2
# 架构修订——注册桩必须留在本类体，桩体一行委托）。
from .interfaces import astrbot_hooks as _quill_hooks


# ── 指令参数切分 ───────────────────────────────────────────────────
# AstrBot 的 CommandFilter.init_handler_md 把「有默认值的形参」记为**默认值**
# 本身（而非注解），于是 `rest: GreedyStr = ""` 里的 GreedyStr 标记被丢掉，
# 该参数退化成普通 str，只吃到一个 token，其余全部丢弃。
# 实测证据：`/quill test wb 女巫` 解析成 rest='wb'（"女巫" 消失），
# 导致 /quill test 永远回落到 wr、`/char <含空格的名字>` 必然失败、
# `/char import <JSON>` 只拿到 JSON 的第一个 token。
# 因此入口一律改用具名 GreedyStr 形参（无默认值，框架才会按整体剩余文本处理），
# 再在这里自行按「首个 token + 其余」切分。


def _split2(text: str) -> tuple[str, str]:
    """把「子命令 + 其余参数」切成两段，等价于原来的 arg1/arg2。"""
    parts = (text or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


# P1-8: 核心记忆自然语言注入前缀匹配
# 支持: @记住：内容 | 记住：内容 | 核心记忆：内容 | @remember: content
_CORE_MEMORY_NL_RE = re.compile(
    r'(?:@记住\s*[：:]|记住\s*[：:]|核心记忆\s*[：:]|@remember\s*[:：])\s*(.+)',
    re.IGNORECASE
)
# 默认字段名（用于 _build_raw_status_re 兜底）与 L4 动态正则已搬至
# quill/services/statusbar/parsers.py（M2.1），此处经顶部 import 保持旧名可用。


# ── 状态栏数值变化标注（_DELTA_MARK_RE/_normalize_status_value/_extract_numeric/
# _format_delta/_annotate_changes）已搬至 quill/services/statusbar/parsers.py（M2.1）。

# 注入报告行（详见 QuillPlugin._format_inject_report / _scrub_inject_report）
_INJECT_REPORT_LINE_RE = re.compile(r"^[ \t]*〔注入〕.*$", re.MULTILINE)


class HealthTracker:
    """P1-4: 轻量级健康度追踪器 — 滑动窗口记录最近 N 次事件的成功/失败。

    线程安全：所有操作在事件循环线程内完成，无需加锁（单线程异步模型）。
    持久化：仅内存，重启后清零（符合"最近健康度"语义）。
    """

    def __init__(self, window_size: int = 20):
        self._window_size = window_size
        self._rag_results: list[bool] = []      # True=成功, False=失败
        self._status_results: list[bool] = []
        # 六级降级链各自命中次数（级别名 → 次数），仅供观测，不做判定
        self._status_levels: dict[str, int] = {}

    def record_rag(self, success: bool) -> None:
        self._rag_results.append(success)
        if len(self._rag_results) > self._window_size:
            self._rag_results.pop(0)

    def record_status(self, success: bool) -> None:
        self._status_results.append(success)
        if len(self._status_results) > self._window_size:
            self._status_results.pop(0)

    def record_status_level(self, level: str) -> None:
        """记一次「某一级命中」。

        为什么要单独统计：此前只能从日志文本 grep 出「这轮走了 L2」，既没法
        看比例、也没法在面板上呈现。级别名与日志里的括号内容一致，便于对照。
        与 record_status 分开：record_status 统计「整栏最终有没有成功产出」，
        这里统计「哪一级产出的」——L4 单命中会记这里但不记成功（它只清理痕迹，
        整栏要靠后续级或兜底补）。
        """
        self._status_levels[level] = self._status_levels.get(level, 0) + 1

    def stats(self) -> dict:
        def _rate(lst):
            if not lst:
                return None
            return round(sum(lst) / len(lst) * 100, 1)
        return {
            "rag": {
                "total": len(self._rag_results),
                "success": sum(self._rag_results),
                "rate": _rate(self._rag_results),
            },
            "status_bar": {
                "total": len(self._status_results),
                "success": sum(self._status_results),
                "rate": _rate(self._status_results),
                # 逐级命中分布（降序）：用于判断主力路径是否需要优化
                "levels": dict(
                    sorted(self._status_levels.items(),
                           key=lambda kv: kv[1], reverse=True)
                ),
            },
            "window_size": self._window_size,
        }


# _StatusLevelResult / _StatusLevelContext 数据类已搬至
# quill/services/statusbar/parsers.py（M2.1），经顶部 import 保持旧名可用。
# strip_markdown/_MD_PATTERNS 已下沉 quill/services/response.py（M2.2 第三轮），
# 经顶部 import 保持旧名可用。


@register(
    "astrbot_plugin_quillplus",
    "Nana7mi0721 & Gemini & GLM & DeepSeek",
    "羽笔 v5.0 — 世界书+写作素材库+角色卡+文档RAG+动态记忆 五合一沉浸式 RP 增强插件",
    "5.0.6",
    "https://github.com/Nana7mi0721/astrbot_plugin_quillplus",
)
class QuillPlugin(StatusbarParsersMixin, StatusbarRenderMixin, Star):
    """羽笔 — v5.0 多维沉浸式 RP 增强插件

    五合一沉浸式 RP 注入系统：世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆。

    核心特性：
    - 平行宇宙双轴隔离 (target_id::persona_id)
    - JSON 原子化状态机（拔电源级防损坏）
    - 全链路纯异步防阻塞
    - 无损对话日志归档与断点续传
    - FAISS + SQLite 事务一致性
    - 4 层 Prompt 装配 + 状态栏降级解析

    Mixin 基类（M2.1 状态栏域搬移）：
    - StatusbarParsersMixin —— 状态栏六级降级链解析侧（quill/services/statusbar/parsers.py）
    - StatusbarRenderMixin —— 状态栏渲染侧：模板选型/兜底栏（quill/services/statusbar/render.py）
    继承顺序保证 MRO：QuillPlugin → Parsers → Render → Star（Star 必须最后）。
    """

    # S3-13: 反思/总结相关阈值常量
    # M2.2 第五轮：反思阈值常量随调度下沉 quill/services/memory.py，
    # 类属性 re-export 维持 QuillPlugin.REFLECTION_* 旧访问面
    REFLECTION_TURN_THRESHOLD = _memory_mod.REFLECTION_TURN_THRESHOLD
    RECENT_LOG_LIMIT = _memory_mod.RECENT_LOG_LIMIT
    MIN_LOGS_FOR_SUMMARY = _memory_mod.MIN_LOGS_FOR_SUMMARY

    def __init__(self, context: Context, config: dict | None = None):
        # quill/ 包日志桥：最早处把宿主 logger 注入，包内方法体的 logger
        # 引用经 logbridge 代理转发到这个对象（quill/ 内禁止 import astrbot）。
        logbridge.set_logger(logger)
        super().__init__(context)
        self._raw_config = config  # AstrBotConfig 实例（支持 save_config）
        self.config = QuillConfig(config)
        # M2.3 配置投影消解（D2）：不再把 config 字段复制成实例属性。
        # props 持本插件引用，19 个原投影属性一律经 self.props.<attr>
        # 实时读 self.config（见 props.py；save_plugin_configs 重建
        # config 后自动生效，无需手工同步）。
        self.props = QuillConfigProperties(self)
        self.plugin_dir = os.path.dirname(__file__)
        # F5 修复：保留后台 task 引用，防止被 GC 中断
        self._bg_tasks: set = set()
        # RAG 重建串行化：避免连续保存配置时并发重建互相踩（见 _reinit_rag_...）
        self._rag_reinit_lock = asyncio.Lock()
        self._rag_reinit_task: asyncio.Task | None = None
        # P1-4: 健康度追踪器（内存滑动窗口，重启清零）
        self.health_tracker = HealthTracker(window_size=20)

        # --- 运行数据位置 ---
        # 数据库/世界书/状态原先放在插件目录内（knowledge/、worldbooks/、data/），
        # 而插件开着长连接，Windows 下更新器删不掉这些文件，更新会中途失败并把
        # 安装目录留成半新半旧。这里统一迁到 AstrBot 约定的
        # data/plugin_data/<插件名>/（更新器不碰），并做一次性搬迁。
        self.paths = resolve_data_layout(self.plugin_dir)
        if self.paths.get("legacy"):
            logger.warning(
                "[Quill] 无法使用外部数据目录，仍在插件目录内读写数据；"
                "插件更新时请先停用插件，否则会因文件占用而失败。"
            )

        # --- Activation ---
        # 随插件分发的只读配置，留在插件目录（更新时会被新版覆盖，符合预期）
        activation_path = os.path.join(self.plugin_dir, "activation_triggers.yaml")
        self.activation_detector = ActivationDetector(activation_path)

        # --- State ---
        data_dir = self.paths["state_dir"]
        os.makedirs(data_dir, exist_ok=True)
        self.state_manager = StateManager(data_dir=data_dir)

        # --- Writing Resource (deferred to initialize()) ---
        # WR 注入参数（wr_max_entries 等）经 self.props 实时读，见 props.py
        self.wr_manager = None

        # --- Worldbook ---
        wb_dir = self.paths["worldbooks_dir"]
        try:
            self.wb_manager = WorldbookManager(wb_dir)
            wb_names = self.wb_manager.list_worldbooks()
            logger.info(f"[Quill] 世界书已加载: {len(wb_names)} 个 - {wb_names}")
        except Exception as e:
            self.wb_manager = None
            logger.warning(f"[Quill] 世界书加载失败: {e}")

        # --- Persona Manager (独立 JSON 角色卡) ---
        self.persona_manager = QuillPersonaManager(
            self.paths["personas_dir"], avatar_dir=self.paths["avatars_dir"]
        )

        # --- Prompt builder / refusal / status bar / debug ---
        # v5.2.5 在此逐属性复制 config（prompt_builder 也在此构造）；
        # M2.3 起统一走 self.props（prompt_builder 为按 config 代缓存的
        # 访问器），运行期读法见各消费点。该段实例属性赋值已删除。

        # --- RAG 组件（延迟到 initialize() 初始化）---
        self.rag_embedding = None
        self.rag_vector_store = None
        self.rag_reranker = None
        self.rag_memory_store = None
        self.rag_summarizer = None
        self.rag_retriever = None

        logger.info(f"[Quill] 插件构造完成 | {self.config}")

    # ================================================================
    # 配置属性访问器（mixin 兼容面，M2.3）
    # ================================================================
    # quill/services/statusbar/ 的两个 Mixin（M2.1 搬移，方法体零改动纪律）
    # 仍以 `self.love_fields` / `self.status_bar_*` 读配置——这些方法组合进
    # 本类后 self 就是插件实例。此处用 __getattr__ 兜底把该批旧名字实时
    # 转发到 self.props.<attr>（后者实时读 self.config），不再是「__init__
    # 复制 + 保存时手工同步」的实例属性快照。main.py / commands.py 自身
    # 代码一律走 self.props.*。
    #
    # 为什么用 __getattr__ 兜底而不是 @property：property 是数据描述符，
    # 会挡掉实例属性赋值——tests/legacy/test_status_bar_parsers.py 的轻量
    # 宿主（object.__new__(QuillPlugin) 后直接 setattr，见 t24/t25/t26）
    # 与既有外部脚本都会因此失效。__getattr__ 只在**常规
    # 属性查找失败**时触发：实例字典里已有的同名属性照常生效（与 v5.2.5
    # 的可写实例属性行为完全一致），默认路径实时读 props，不留第二真源。
    # 运行期自身代码没有任何对这些名字的赋值点
    # （tests/legacy/test_config_projection.py 看守）。
    _MIXIN_CONFIG_ATTRS = frozenset({
        "love_fields",
        "status_bar_enabled",
        "status_bar_format_template",
        "status_bar_format_plain",
        "status_bar_plain_platforms",
        "status_bar_plot_paths",
        "status_bar_default_placeholder",
        "status_bar_show_delta",
    })

    def __getattr__(self, name: str):
        # 只兜底状态栏 Mixin 消费的配置属性名。注意 self.props 自身缺失也
        # 会走到这里，必须查实例字典而不是属性访问，避免无限递归。
        if name in QuillPlugin._MIXIN_CONFIG_ATTRS:
            props = self.__dict__.get("props")
            if props is not None:
                return getattr(props, name)
        raise AttributeError(
            f"{type(self).__name__} object has no attribute {name!r}"
        )

    # ================================================================
    # Lifecycle
    # ================================================================

    @staticmethod
    def _is_valid_reflection(reflection: dict) -> bool:
        """校验反思结果是否可直接用于写入。

        `reflect_on_logs` 只保证「返回 dict」，不保证字段齐全——LLM 少了某个
        字段时它照样返回。而 `update_core_memory` 会把取到的值**覆写**进核心
        记忆行，所以缺字段 = 用空串顶掉已有核心设定，且日志随后被清理、
        无法回滚。因此写入前必须确认关键字段存在且非空。
        """
        if not isinstance(reflection, dict):
            return False
        traits = reflection.get("new_core_traits")
        facts = reflection.get("crucial_facts")
        trivials = reflection.get("trivial_summaries")
        # traits/facts 至少一个有实质内容，否则这次反思没有产出，不写不删
        has_core = bool(isinstance(traits, str) and traits.strip()) or bool(
            isinstance(facts, str) and facts.strip()
        )
        if not has_core:
            return False
        # trivial_summaries 允许为空（没有闲聊可提纯），但给了就必须是列表
        if trivials is not None and not isinstance(trivials, list):
            return False
        return True

    async def _reflection_loop(self):
        """Phase 4: 全自动自迭代反思守护进程 (Idle Detection)"""
        import asyncio
        from datetime import datetime, timezone
        from astrbot.api.all import logger
        
        # 初始延迟，避免启动时抢占资源
        await asyncio.sleep(60)
        
        while True:
            try:
                # 每隔 1 小时检查一次
                await asyncio.sleep(3600)
                if not getattr(self.config, 'rag_enable_chat_logging', True) or not getattr(self.config, 'rag_enable_autonomous_reflection', True):
                    continue
                if not self.rag_retriever or not self.rag_retriever.memory_store:
                    continue
                    
                # 寻找空闲的 Session (超过1小时没说话，且有大量日志)
                # 由于这是后台任务，我们可以直接查询 SQLite
                store = self.rag_retriever.memory_store
                rows = await store._exec_fetchall("SELECT session_id, MAX(timestamp), COUNT(*) FROM chat_logs GROUP BY session_id")
                
                now_utc = datetime.now(timezone.utc)
                for row in rows:
                    session_id, last_ts_str, count = row[0], row[1], row[2]
                    # 空闲判定：最后一条日志距今超过 1 小时才反思，避免删除活跃会话的日志打断对话。
                    # SQLite CURRENT_TIMESTAMP 为 naive UTC 字符串（"YYYY-MM-DD HH:MM:SS"）。
                    try:
                        last_active = datetime.fromisoformat(str(last_ts_str))
                    except (TypeError, ValueError):
                        logger.debug(f"[Quill Reflection] 会话 {session_id} 时间戳无法解析: {last_ts_str!r}，跳过")
                        continue
                    if last_active.tzinfo is None:
                        last_active = last_active.replace(tzinfo=timezone.utc)
                    if (now_utc - last_active).total_seconds() < 3600:
                        continue
                    # 粗略判断：如果日志条数 > 30 条，进行反思
                    if count > 30:
                        logger.info(f"[Quill Reflection] 开始对 {session_id} 进行闲时反思归纳...")
                        logs = await store.get_recent_chat_logs(session_id, limit=200)
                        if not logs:
                            continue
                        # 记录本次实际参与摘要的日志 id 批次：删除只能针对这批，
                        # 不能按「保留最新 2 条」重算——LLM 与 embedding 的等待
                        # 窗口里会话可能重新活跃并写入新日志，重算会把它们误删。
                        batch_ids = [lg.get("id") for lg in logs if lg.get("id") is not None]
                        combined = []
                        for log in logs:
                            role = "User" if log.get("role") == "user" else "AI"
                            combined.append(f"{role}: {log.get('content', '')}")
                        combined_text = "\n".join(combined)

                        reflection = await self.rag_retriever.summarizer.reflect_on_logs(combined_text)
                        if reflection:
                            # 结构校验：reflect_on_logs 只保证返回 dict，不保证字段
                            # 齐全。字段缺失时 .get(..., "") 会拿到空串，而
                            # update_core_memory 会把它**覆写**进核心记忆行 ——
                            # 等于用空白顶掉已有核心设定，且紧接着日志被删，
                            # 无法回滚。这里必须显式校验后再放行。
                            if not self._is_valid_reflection(reflection):
                                logger.warning(
                                    f"[Quill Reflection] {session_id} 反思结果字段不全，"
                                    f"已跳过本次写入与日志清理（keys={sorted(reflection.keys())}）"
                                )
                                continue

                            traits = reflection.get("new_core_traits", "")
                            facts = reflection.get("crucial_facts", "")
                            trivials = reflection.get("trivial_summaries", [])

                            # 顺序：先写记忆，确认落库后再清日志。
                            # 反过来的话，写记忆失败 = 日志已删、摘要也没留下，
                            # 这段对话永久丢失。
                            try:
                                await store.update_core_memory(session_id, traits, facts)
                                for t in trivials:
                                    if isinstance(t, str) and t.strip():
                                        await self.rag_retriever.store_memory_direct(session_id, t)
                            except Exception as e:
                                logger.warning(
                                    f"[Quill Reflection] {session_id} 记忆写入失败，"
                                    f"保留原始日志待下轮重试: {e}"
                                )
                                continue

                            # 只删本次实际处理过的那批 id
                            removed = await store.delete_chat_logs_by_ids(session_id, batch_ids)
                            logger.info(
                                f"[Quill Reflection] 成功完成 {session_id} 的记忆反思提纯"
                                f"（清理本批 {removed}/{len(batch_ids)} 条日志）。"
                            )
                            
                            # 控制速率，防止 API 频率过高
                            await asyncio.sleep(10)
                            
            except Exception as e:
                logger.warning(f"[Quill Reflection] 守护进程异常: {e}")


    async def initialize(self) -> None:
        """异步初始化：WR 载入、Web 路由注册、RAG 索引构建、过期日志清理。

        生命周期阶段一。完成以下工作：
        - 写作素材库（WR）懒加载
        - Web 面板 API 路由注册
        - RAG 检索器与 FAISS 索引初始化
        - 过期对话日志清理与低价值记忆修剪
        """
        if self.config.wr_enabled:
            wr_path = os.path.join(self.paths["knowledge_dir"], "quill_wr.db")
            # 迁移旧数据库文件名（同一目录内；_paths 已把整个目录搬到外部数据根）
            old_kb_path = os.path.join(self.paths["knowledge_dir"], "quill_kb.db")
            if not os.path.exists(wr_path) and os.path.exists(old_kb_path):
                try:
                    # os.replace 而非 rename：若并发/外部已创建目标文件，Windows 下
                    # rename 直接抛 FileExistsError 回退到旧路径，replace 则原子覆盖。
                    os.replace(old_kb_path, wr_path)
                    # sidecar 必须跟随主文件一起改名：SQLite 按「主文件名 + -wal」
                    # 配对，留在旧名字下的 WAL 不会被新库读取，其中已提交但未
                    # checkpoint 的事务会静默丢失。
                    for suffix in ("-wal", "-shm", "-journal"):
                        old_side = old_kb_path + suffix
                        if os.path.exists(old_side):
                            try:
                                os.replace(old_side, wr_path + suffix)
                            except OSError as se:
                                logger.warning(
                                    f"[Quill] 数据库迁移: sidecar {suffix} 搬迁失败: {se}"
                                )
                    logger.info("[Quill] 写作素材库数据库已迁移: quill_kb.db → quill_wr.db")
                except Exception as e:
                    logger.warning(f"[Quill] 数据库迁移失败，使用旧文件: {e}")
                    wr_path = old_kb_path
            try:
                category_dedup_limit = self.config.wr_dedup_limit
                self.wr_manager = WritingResourceManager(wr_path, category_dedup_limit=category_dedup_limit)
                await self.wr_manager.initialize()
                stats = await self.wr_manager.get_stats()
                logger.info(
                    f"[Quill] 写作素材库已加载: {stats['total_entries']} 条 "
                    f"(启用 {stats['enabled_entries']} 条)"
                )
                logger.info(f"[Quill] 最大注入: {self.props.wr_max_entries} 条")
            except Exception as e:
                self.wr_manager = None
                logger.warning(f"[Quill] 写作素材库初始化失败: {e}")
        else:
            logger.info("[Quill] 写作素材库已禁用")

        # --- RAG 初始化 ---
        await self._init_rag()

        # --- Web routes (AstrBot v4.26+ register_web_api 模式) ---
        rag_components = {
            'embedding': self.rag_embedding,
            'vector_store': self.rag_vector_store,
            'reranker': self.rag_reranker,
            'memory_store': self.rag_memory_store,
            'summarizer': self.rag_summarizer,
        }
        try:
            # 审查修复：保存 routes 实例引用——_init_rag/备份恢复重建组件后，
            # 需要把最新组件同步进已注册的路由对象（否则面板 API 一直操作旧连接）
            self._quill_routes = QuillRoutes(self.wr_manager, self.wb_manager, self.context, self.config,
                        rag_components=rag_components, plugin=self,
                        persona_manager=self.persona_manager)
            self._quill_routes.register_all()
            logger.info("[Quill] 已注册全部 Web API 路由 (register_web_api)")
        except Exception as e:
            logger.warning(f"[Quill] Web 路由注册失败: {e}")

        # ── 插件面板（Plugin Pages 系统）──
        pages_index = os.path.join(self.plugin_dir, "pages", "panel", "index.html")
        if os.path.isfile(pages_index):
            logger.info("[Quill] Plugin Pages 面板已就绪: pages/panel/index.html")
        else:
            logger.warning(
                "[Quill] Plugin Pages 面板未找到 (pages/panel/index.html)，"
                "面板 UI 不可用，但 API 路由仍正常工作。"
            )

        # 启动时清理过期对话日志
        if self.rag_retriever and self.rag_retriever.memory_store:
            retention_days = getattr(self.config, 'rag_chat_log_retention_days', 30)
            cleaned = await self.rag_retriever.memory_store.cleanup_chat_logs(retention_days)
            if cleaned:
                logger.info(f"[Quill ChatLog] 清理了 {cleaned} 条过期日志（保留 {retention_days} 天）")

        # 启动时修剪过期低价值记忆
        if self.rag_retriever and self.rag_retriever.memory_store:
            pruned = await self.rag_retriever.memory_store.prune_memories()
            if pruned:
                logger.info(f"[Quill Memory] 启动修剪: 清理了 {pruned} 条低价值记忆")

        # 启动 state 自动落盘（分级落盘：关键字段即时，高频字段 5s 批量刷洗）
        self.state_manager.start_autoflush()

        # P2-1 修复：启动闲时反思守护进程（此前 _reflection_loop 为死代码，从未被调用）
        if getattr(self.config, 'rag_enable_autonomous_reflection', True) \
                and self.rag_retriever and self.rag_retriever.memory_store:
            self._spawn(self._reflection_loop())
            logger.info("[Quill Reflection] 闲时反思守护进程已启动")

        logger.info(
            f"[Quill] 插件初始化完成 | 激活词: {self.activation_detector.get_word_count()} 个"
        )

    async def _init_rag(self):
        """初始化 RAG 组件（Embedding、向量库、重排、记忆、摘要）。"""
        try:
            from .quill_rag.embedding import QuillEmbeddingProvider
            from .quill_rag.vector_store import FaissVectorStore
            from .quill_rag.memory_store import MemoryStore
            from .quill_rag.reranker import QuillReranker
            from .quill_rag.llm_summarizer import QuillSummarizer
            from .quill_rag.retrieval import QuillRetriever

            # Embedding Provider
            self.rag_embedding = QuillEmbeddingProvider(
                self.context,
                provider_id=self.config.rag_embedding_provider_id,
                enable_local=self.config.rag_enable_local_embedding,
            )

            # Doc RAG 向量库（FAISS + SQLite）
            rag_db = os.path.join(self.paths["knowledge_dir"], "quill_rag.db")
            rag_idx = os.path.join(self.paths["knowledge_dir"], "quill_rag.index")
            # S2-10: 传入 embedding_provider，切换 provider 时自动重建索引
            self.rag_vector_store = FaissVectorStore(
                rag_db, rag_idx, embedding_provider=self.rag_embedding
            )
            await self.rag_vector_store.initialize()

            # Reranker
            self.rag_reranker = QuillReranker(
                self.context,
                rerank_provider_id=self.config.rag_rerank_provider_id,
                fallback_llm_id=self.config.rag_llm_provider_id,
            )

            # 动态记忆存储（SQLite BLOB）
            mem_db = os.path.join(self.paths["knowledge_dir"], "quill_memory.db")
            self.rag_memory_store = MemoryStore(mem_db)
            await self.rag_memory_store.initialize()

            # LLM 摘要器
            self.rag_summarizer = QuillSummarizer(
                self.context,
                provider_id=self.config.rag_llm_provider_id,
            )

            # 统一检索器
            self.rag_retriever = QuillRetriever(
                embedding_provider=self.rag_embedding,
                vector_store=self.rag_vector_store,
                reranker=self.rag_reranker,
                memory_store=self.rag_memory_store,
                summarizer=self.rag_summarizer,
                top_k=self.config.rag_top_k,
                enable_memory=self.config.rag_enable_memory,
                config=self.config,
            )

            logger.info("[Quill RAG] 组件初始化完成 | embedding=%s | memory=%s",
                        self.config.rag_embedding_provider_id or "local",
                        "on" if self.config.rag_enable_memory else "off")
        except Exception as e:
            logger.warning(f"[Quill RAG] 初始化失败（RAG 功能不可用）: {e}")

    # ================================================================
    # 审查修复：组件生命周期管理（重初始化 / 备份恢复共用）
    # ================================================================

    async def _close_rag_components(self):
        """安全关闭当前 RAG 组件连接。重初始化前必须调用，否则旧 aiosqlite/FAISS
        句柄泄漏，且（Windows 下）恢复解压覆盖运行中的 DB 文件会读到错乱页。

        关闭前**等待 QuillRetriever 自己的在途后台任务退出**：它持有独立的
        _bg_tasks（记忆落库 / 有用性统计等），插件的 _bg_tasks 管不到。
        不等待的话，重建过程中这些任务仍会往刚被 close 的连接里写，
        报 "Connection closed" 或更糟——静默丢数据。
        """
        if self.rag_retriever:
            await self._drain_retriever_tasks()
            for comp, name in (
                (getattr(self.rag_retriever, "memory_store", None), "memory_store"),
                (getattr(self.rag_retriever, "vector_store", None), "vector_store"),
            ):
                if comp:
                    try:
                        await comp.close()
                    except Exception as e:
                        logger.debug(f"[Quill] 关闭旧 {name} 失败（忽略）: {e}")
        self.rag_retriever = None
        self.rag_memory_store = None
        self.rag_vector_store = None
        self.rag_embedding = None
        self.rag_reranker = None
        self.rag_summarizer = None

    async def _drain_retriever_tasks(self, timeout: float = 10.0) -> int:
        """等待 retriever 在途后台任务结束，返回等待到的任务数。

        尽力而为：超时后直接返回，不阻塞重建（否则一个卡死的 embedding 请求
        会把整个重建永久挂起）。异常一律吞掉——这里的目的是「尽量不打断
        正在写库的任务」，不是保证它们都成功。
        """
        tasks = getattr(self.rag_retriever, "_bg_tasks", None)
        if not tasks:
            return 0
        pending = [t for t in list(tasks) if not t.done()]
        if not pending:
            return 0
        logger.info("[Quill] 等待 %d 个 RAG 在途任务结束再关闭组件", len(pending))
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "[Quill] %d 个 RAG 在途任务在 %.0fs 内未结束，强制继续关闭",
                len(pending), timeout,
            )
        except Exception as e:
            logger.debug(f"[Quill] 等待 RAG 在途任务异常（忽略）: {e}")
        return len(pending)

    def _refresh_routes_refs(self):
        """把最新的管理器/RAG 组件引用同步到已注册的 QuillRoutes 实例。"""
        routes = getattr(self, "_quill_routes", None)
        if routes is None:
            return
        routes.wr_manager = self.wr_manager
        routes.wb_manager = self.wb_manager
        routes.persona_manager = self.persona_manager
        routes.config = self.config
        routes.plugin = self
        routes.rag = {
            'embedding': self.rag_embedding,
            'vector_store': self.rag_vector_store,
            'reranker': self.rag_reranker,
            'memory_store': self.rag_memory_store,
            'summarizer': self.rag_summarizer,
        }

    async def _reinit_rag_and_refresh_routes(self):
        """关闭旧 RAG 组件 → 重建 → 刷新 Web 路由引用（Embedding 切换等场景）。

        用 _rag_reinit_lock 串行化：连续保存 embedding 配置会各自 spawn 一个
        重建，并发执行时后者可能在前者 close 了一半的连接时开始初始化，
        结果是两个半成品互相踩、路由引用指向已关闭的组件。
        """
        # 已有一个重建在跑且未结束 → 直接复用它的结果，不叠加第二个。
        # 用 create_task + 共享 await 的方式去重：后来的调用者等待同一次重建。
        if self._rag_reinit_lock.locked():
            logger.info("[Quill] RAG 重建已在进行中，复用本次结果，不重复触发")
            if self._rag_reinit_task is not None and not self._rag_reinit_task.done():
                try:
                    await asyncio.shield(self._rag_reinit_task)
                except Exception:
                    pass
                return
        async with self._rag_reinit_lock:
            try:
                await self._close_rag_components()
                await self._init_rag()
            finally:
                # 无论成功失败都刷新：失败时组件可能部分初始化，路由引用
                # 必须反映真实状态，否则面板/聊天会握着已关闭的连接。
                self._refresh_routes_refs()

    def _spawn_rag_reinit(self):
        """触发一次 RAG 重建（供配置保存路径调用），可安全重复调用。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[Quill] 无运行中的事件循环，RAG 重建将在插件重载后生效")
            return
        if self._rag_reinit_task is not None and not self._rag_reinit_task.done():
            logger.info("[Quill] RAG 重建任务已在运行，忽略重复触发")
            return

        async def _run():
            try:
                await self._reinit_rag_and_refresh_routes()
            finally:
                self._rag_reinit_task = None

        self._rag_reinit_task = loop.create_task(_run())
        # 也纳入插件自己的任务集合，terminate 时能被统一取消/等待
        self._bg_tasks.add(self._rag_reinit_task)

    async def _prepare_for_restore(self):
        """备份恢复前的准备：停 autoflush（不 flush）+ 关闭持有 DB 句柄的组件。

        必须在解压覆盖文件**之前**调用——否则 Windows 下覆盖运行中的 SQLite
        会让旧连接读到错乱页，且旧内存脏状态会被 autoflush 反向写回、
        覆盖刚恢复的 quill_state.json。

        关闭后立即把 wr_manager 置 None 并刷新路由引用：解压是 to_thread 执行
        的，事件循环全程可并发服务，而此时旧 manager 已是「已关闭但非 None」的
        真值对象 —— 路由只做 `if not wr_manager` 判断，会走进已关闭连接抛
        AssertionError（面板显示 500），聊天路径也会静默丢注入。置 None 后
        两边都干净降级为「未加载」。
        """
        try:
            await self.state_manager.stop_autoflush()
        except Exception as e:
            logger.debug(f"[Quill] 恢复前停止 autoflush 失败（忽略）: {e}")
        await self._close_rag_components()
        if self.wr_manager:
            try:
                await self.wr_manager.close()
            except Exception as e:
                logger.debug(f"[Quill] 恢复前关闭写作素材库失败（忽略）: {e}")
        self.wr_manager = None
        self._refresh_routes_refs()

    async def _reload_after_restore(self):
        """备份解压完成后的全量重建：丢弃旧内存态与缓存，从恢复的磁盘数据重新加载。

        前置条件：已调用 _prepare_for_restore() 且数据文件已被覆盖到位。
        无论中途哪一步抛错，都在 finally 里刷新路由引用——否则路由会一直握着
        旧的（已关闭）引用直到进程重启，面板与聊天路径都不可用。
        """
        autoflush_ready = False
        try:
            # 1) 重建 StateManager（从恢复后的 quill_state.json 重新加载）
            data_dir = self.paths["state_dir"]
            self.state_manager = StateManager(data_dir=data_dir)
            autoflush_ready = True
            # 2) 重建写作素材库连接
            self.wr_manager = None
            wr_path = os.path.join(self.paths["knowledge_dir"], "quill_wr.db")
            try:
                from .kb import WritingResourceManager
                self.wr_manager = WritingResourceManager(
                    wr_path, category_dedup_limit=self.config.wr_dedup_limit
                )
                await self.wr_manager.initialize()
            except Exception as e:
                self.wr_manager = None
                logger.warning(f"[Quill] 恢复后写作素材库重建失败: {e}")
            # 3) 世界书重载（无文件句柄，直接重读 JSON；读盘放到线程避免阻塞事件循环）
            if self.wb_manager:
                try:
                    await asyncio.to_thread(self.wb_manager._load_all)
                except Exception as e:
                    logger.warning(f"[Quill] 恢复后世界书重载失败: {e}")
            # 4) 角色卡缓存失效（重建实例，重新扫描 personas 目录）
            try:
                from .persona_manager import QuillPersonaManager
                self.persona_manager = QuillPersonaManager(
                    self.paths["personas_dir"], avatar_dir=self.paths["avatars_dir"]
                )
            except Exception as e:
                logger.warning(f"[Quill] 恢复后角色卡管理器重建失败: {e}")
            # 5) RAG 组件重建
            await self._init_rag()
        finally:
            # 6) 无条件刷新路由引用 + 恢复 autoflush（StateManager 建好才启动）
            self._refresh_routes_refs()
            if autoflush_ready:
                self.state_manager.start_autoflush()
        logger.info("[Quill] 备份恢复后的组件重建完成")

    async def terminate(self) -> None:
        """生命周期终止：取消所有后台任务并关闭数据库连接，防止资源泄漏。"""
        # S2-4 修复：先取消并等待所有后台任务，防止退出时悬挂/资源泄漏
        if self._bg_tasks:
            bg_count = len(self._bg_tasks)
            for t in list(self._bg_tasks):
                if not t.done():
                    t.cancel()
            for t in list(self._bg_tasks):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            self._bg_tasks.clear()
            logger.info(f"[Quill] 已清理 {bg_count} 个后台任务")
        if self.state_manager:
            try:
                await self.state_manager.shutdown()
                logger.info("[Quill] 状态已持久化")
            except Exception as e:
                logger.warning(f"[Quill] 状态持久化失败: {e}")
        if self.wr_manager:
            try:
                await self.wr_manager.close()
            except Exception as e:
                logger.warning(f"[Quill] 写作素材库关闭失败: {e}")
        if self.rag_retriever and self.rag_retriever.memory_store:
            await self.rag_retriever.memory_store.close()
        if self.rag_retriever and self.rag_retriever.vector_store:
            await self.rag_retriever.vector_store.close()
        logger.info("[Quill] 插件已停用")

    def _spawn(self, coro):
        """F5 修复：启动后台任务并保留引用，防止被 GC 中断。完成后自动从集合移除。"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)

        def _on_done(task: asyncio.Task):
            self._bg_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.warning(f"[Quill] 后台任务 {task.get_coro()} 异常退出: {task.exception()}")

        t.add_done_callback(_on_done)
        return t

    # ================================================================
    # 配置持久化
    # ================================================================

    def save_plugin_configs(self, updates: list[dict]) -> tuple[bool, str]:
        """Atomically persist a batch of config updates.

        The Web panel sends all edited fields in one request. Saving them one
        by one exposed transient config states and could start overlapping RAG
        rebuilds when the embedding provider changed.

        M2.3（投影消解）后的职责只剩四件事：写 raw dict → 重建 QuillConfig →
        Retriever 热更新三字段 → 落盘；失败时回滚 raw dict + config 引用 +
        Retriever 三字段。运行期配置的生效不再依赖本方法做任何属性同步：
        self.props.* 实时读 self.config，引用一换即全部生效。
        """
        if self._raw_config is None:
            return False, "插件配置不可用"

        normalized: list[tuple[str, str, object]] = []
        try:
            for item in updates:
                if not isinstance(item, dict):
                    return False, "配置更新格式无效"
                group = str(item.get("group", "")).strip()
                key = str(item.get("key", "")).strip()
                if not group or not key:
                    return False, "配置更新缺少 group 或 key"
                normalized.append((group, key, item.get("value")))
            if not normalized:
                return True, "没有需要保存的修改"

            previous = {
                (group, key): (
                    group in self._raw_config
                    and isinstance(self._raw_config.get(group), dict)
                    and key in self._raw_config[group],
                    (
                        self._raw_config.get(group, {}).get(key)
                        if isinstance(self._raw_config.get(group), dict)
                        else None
                    ),
                )
                for group, key, _ in normalized
            }
            # M2.3 投影消解：这里不再有任何「把 config 摊平到实例属性」的
            # 同步代码——运行期一律经 self.props.<attr> 实时读 self.config，
            # 重建 config 对象即天然生效；保存失败也只需恢复旧 config 引用。
            # 旧 config 引用快照必须建在内层 try 之外：回滚分支在 try 内部
            # 任何一步（包括第一行）抛异常时都会用到它，建在内部会有
            # 未赋值的风险（v5.2.5 的 _retriever_prev 正是犯了这个错，
            # 见下）。
            previous_config = self.config
            # Retriever 热更新旧值同样先置 None：旧实现把它放在内层 try 内
            # 赋值、except 里引用——若 try 早期（如 QuillConfig 构造）失败，
            # 回滚分支自身会 NameError 并掩盖原始异常。这里一并修正。
            _retriever_prev = None
            try:
                for group, key, value in normalized:
                    if (
                        group not in self._raw_config
                        or not isinstance(self._raw_config[group], dict)
                    ):
                        self._raw_config[group] = {}
                    self._raw_config[group][key] = value

                self.config = QuillConfig(self._raw_config)
                self._refresh_routes_refs()

                # ── RAG 运行期参数热更新（无需重载插件）──
                # 这几个值被 QuillRetriever 持有为**普通属性**，构造后不再变化。
                # 此前 save 流程只重建 self.config，没人把它们同步过去，于是
                # 面板上改 `enable_memory` / `top_k` 当轮不生效、要重载才生效
                # （实测：关闭后仍检索到 2 条）。这里补上同步。
                #
                # 为什么不用 _reinit_rag_and_refresh_routes()：那会 close 掉
                # memory_store / vector_store（SQLite + FAISS 句柄），代价与风险
                # 都远高于改两个属性，而这两个属性本来就不依赖连接。真正需要
                # 重建的是 embedding/reranker 换 provider，那条路径已单独处理。
                if self.rag_retriever is not None:
                    _retriever_prev = (
                        self.rag_retriever.top_k,
                        self.rag_retriever.enable_memory,
                        self.rag_retriever.config,
                    )
                    self.rag_retriever.top_k = self.config.rag_top_k
                    self.rag_retriever.enable_memory = self.config.rag_enable_memory
                    # 换掉 retriever 持有的 config 引用，保证它读到的
                    # `config._raw`（rag_dense_top_k 走这条路）也是新的
                    self.rag_retriever.config = self.config

                if hasattr(self._raw_config, "save_config") and callable(
                    self._raw_config.save_config
                ):
                    self._raw_config.save_config()
                elif hasattr(self.context, "save_config"):
                    self.context.save_config()
                else:
                    raise RuntimeError("AstrBot 配置对象不支持持久化")

                changed_keys = {(group, key) for group, key, _ in normalized}
                if ("rag", "embedding_provider_id") in changed_keys:
                    # 走 _spawn_rag_reinit 而非直接 _spawn：连续保存会各起一个
                    # 重建，并发执行时后一个可能在前一个 close 到一半时开始
                    # 初始化。这里做去重 + 串行化（内部已处理「无事件循环」）。
                    self._spawn_rag_reinit()
                    logger.info("[Quill] Embedding 提供商已变更，触发 RAG 重初始化")
            except Exception:
                # Best-effort rollback of the in-memory dict. Disk writes are
                # atomic inside AstrBotConfig, so a failed write never exposes
                # a partial JSON file.
                for (group, key), (existed, old_value) in previous.items():
                    if not isinstance(self._raw_config.get(group), dict):
                        self._raw_config[group] = {}
                    if existed:
                        self._raw_config[group][key] = old_value
                    else:
                        self._raw_config[group].pop(key, None)
                # 恢复旧 config **引用**（而非重建）：QuillConfig 是 raw dict
                # 的解析快照，raw 回滚后旧对象的解析结果与磁盘内容重新一致；
                # props 逐属性实时读 self.config，引用一换即全部复原，
                # prompt_builder 的按 config 代缓存也随之自动回到旧代实例。
                self.config = previous_config
                self._refresh_routes_refs()
                # 回滚 Retriever 的热更新字段：不还原就会出现「面板提示保存
                # 失败，但记忆开关/检索条数已按新值运行」的不一致状态。
                if _retriever_prev is not None and self.rag_retriever is not None:
                    self.rag_retriever.top_k = _retriever_prev[0]
                    self.rag_retriever.enable_memory = _retriever_prev[1]
                    self.rag_retriever.config = _retriever_prev[2]
                raise
        except Exception as e:
            logger.warning("[Quill] 配置批量保存失败: %s", e, exc_info=True)
            # 这条 message 会经 web_routes.config_update 直接下发给面板，不能带原始
            # 异常文本（OSError 会带出配置文件的绝对路径）。
            return False, f"配置保存失败（{type(e).__name__}），详情见服务端日志"

        summary = ", ".join(f"{group}.{key}" for group, key, _ in normalized)
        logger.info("[Quill] 配置已保存 (%d 项): %s", len(normalized), summary)
        return True, f"已保存 {len(normalized)} 项配置"

    def save_plugin_config(self, group: str, key: str, value) -> bool:
        """Compatibility wrapper for callers that still save one field."""
        ok, _ = self.save_plugin_configs(
            [{"group": group, "key": key, "value": value}]
        )
        return ok

    # ================================================================
    # LLM Hooks
    # ================================================================

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """在流式决策前切换角色卡专属对话并控制流式模式。

        注册桩（M2.2 第二轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_waiting_llm_request``（完整
        设计理由见彼处 docstring；行为快照见 tests/test_hook_snapshots.py
        H1 节）。行为契约：

        * 先切角色卡专属对话（必须早于框架 ``_get_session_conv()``，
          下一事件才切换就要晚一轮生效），实现见
          quill/services/character.py；
        * 拦截字面 ``/reinject`` / ``/重新注入``（按 **sender_id** 非
          target_id 重置 quill_rounds 并回执，不继续流式决策）；
        * 按 ``state.stream_mode`` off/on/auto 设置 ``enable_streaming``
          extra（auto 且激活词/【】括号时关流式）。

        降级怪癖（BASELINE §2 H1 行，与 H6 相反，刻意保留）：**无钩子级
        顶层 try**——``state_manager.get_state`` 抛出会**上抛框架**；仅
        "取 message_str/_get_target_id"的内层小块 except: return 静默。
        桩体因此不做任何 try 包裹。
        """
        await _quill_hooks.handle_waiting_llm_request(self, event)

    # ── 状态栏解析共享方法 ──────────────────────────────────────

    # M2.2 剥离器下沉：实现（剥离正则常量 + 纯函数）已原位搬至
    # quill/services/statusbar/strip.py，正则/文本处理一字未改，仅由
    # 类属性/classmethod 转为模块级常量/函数（原实现本就零 self 依赖）。
    # 此处保留同名类属性/类方法薄转发（M2.0 搬移期约定）：
    #   - tests/legacy（t7/t8/t15/t26）与 probe 脚本经
    #     QuillPlugin.<name> 的旧访问面不变；
    #   - 钩子/服务侧经 self._strip_* 的动态分发路径与搬移前一致。
    # 下面的类属性与 strip 模块常量是**同一对象**的别名（缓存 dict 亦然）：
    # 任何一侧清缓存/改表都作用于同一份状态，与搬移前单一代码路径等价。
    # 实现与完整设计理由（两档强度为何分设、为何不擦裸字段行）见 strip.py。

    # 聚合所有状态栏变体的剥离正则（disabled 模式 + dedup 清理用）
    _STRIP_PATTERNS = _strip_mod._STRIP_PATTERNS
    _strip_field_re_cache = _strip_mod._strip_field_re_cache
    _STRIP_LOVE_DATA_RE = _strip_mod._STRIP_LOVE_DATA_RE
    _STRIP_LEGACY_STATUS_RE = _strip_mod._STRIP_LEGACY_STATUS_RE

    @classmethod
    def _strip_bare_fields_re(cls, fields: list) -> re.Pattern:
        """转发 strip 模块（字段缓存语义见彼处）。"""
        return _strip_mod._strip_bare_fields_re(fields)

    @classmethod
    def _strip_status_artifacts(cls, text: str, fields: list | None = None) -> str:
        """转发 strip 模块（完整剥离：关闭状态栏 / dedup 清理用）。"""
        return _strip_mod._strip_status_artifacts(text, fields)

    @classmethod
    def _strip_raw_markers(cls, text: str, fields: list | None = None) -> str:
        """转发 strip 模块（只擦原始标记：发送前兜底专用）。"""
        return _strip_mod._strip_raw_markers(text, fields)

    # _lenient_parse_status 已搬至 quill/services/statusbar/parsers.py
    # （StatusbarParsersMixin，M2.1）。

    # _effective_status_bar_enabled / _resolve_platform_name /
    # _status_bar_template_for 已搬至 quill/services/statusbar/render.py
    # （StatusbarRenderMixin，M2.1）。

    def _prompt_builder_for_request(self, status_bar_enabled: bool):
        """按本轮的最终开关，取一个 PromptBuilder（浅拷贝，必要时覆盖开关）。

        为什么不直接改 self.props.prompt_builder.status_bar_enabled：它是共享实例，
        并发请求会互相踩（A 会话设 on 会污染 B 会话）。浅拷贝只复制属性引用，
        PromptBuilder 不持有连接/任务，拷贝成本可忽略，且绝不落回共享实例。

        为什么不用给 build_system_prompt 加参数：契约文案分散在
        build_status_bar_guide / build_send_message_guide / build_safety_wrapper
        三处读取该开关，加参数就得把签名一路改到底；拷贝一次把这四个读取点
        一次性对齐，改动面最小。
        """
        if status_bar_enabled == self.props.prompt_builder.status_bar_enabled:
            return self.props.prompt_builder
        pb = copy.copy(self.props.prompt_builder)
        pb.status_bar_enabled = status_bar_enabled
        return pb

    # _resolve_platform_name / _status_bar_template_for / _handle_status_bar /
    # _SB_LEVELS / _sb_l1.._sb_l6 / _persist_status_vars 已搬至
    # quill/services/statusbar/{parsers,render}.py（M2.1，StatusbarParsersMixin
    # 与 StatusbarRenderMixin，经 QuillPlugin 的 Mixin 基类提供）。

    # ── 本轮注入报告 ────────────────────────────────────────────
    # 设计：统计**始终**采集并缓存（容量见 _INJECT_REPORT_MAX），回复文本里则
    # 只在 debug 开启时附一行。这样调灵敏度/top_k 时有据可依（否则所有相关
    # 配置项都只能凭感觉调），同时不给普通用户的每条消息都加噪声。
    # /quill debug 无论开关状态都能读到缓存，用于事后排查。
    _INJECT_REPORT_MAX = 64

    def _remember_inject_report(self, target_id: str, stats: dict) -> None:
        """缓存本轮注入构成，供回复渲染与 /quill debug 读取。

        写入前补齐标准键（缺的记 0）：这样「采集过但一条没命中」会得到
        `{'wb':0,...}` → 渲染成「〔注入〕无命中」，而「从没采集过」（缓存里
        查不到该 target_id，`_get_inject_report` 返回 `{}`）仍然沉默。
        这条区分是实测踩出来的：RAG 未初始化时 `_run_rag_retrieval` 会提前
        return，一个键都不填，报告行随之整个消失 —— 用户无法分辨
        「确实没命中」与「开关没生效」，而开这个开关的全部意义就在于分辨它。
        """
        cache = getattr(self, "_inject_reports", None)
        if cache is None:
            cache = {}
            self._inject_reports = cache
        merged = {"wb": 0, "mem": 0, "wr": 0, "doc": 0, "core_mem": 0}
        merged.update(stats or {})
        # 重新赋值以更新插入顺序，使淘汰按「最近使用」而非「最早创建」
        cache.pop(target_id, None)
        cache[target_id] = merged
        while len(cache) > self._INJECT_REPORT_MAX:
            cache.pop(next(iter(cache)), None)

    def _get_inject_report(self, target_id: str) -> dict:
        return (getattr(self, "_inject_reports", None) or {}).get(target_id) or {}

    def _append_inject_report(self, text: str, target_id: str) -> str:
        """在消息末尾追加注入报告行（仅 debug 开启时）。

        追加在状态栏代码块**之外**：报告行若落进 ``` 内会被 _parse_status_block
        当成字段读走并写进 session_vars，进而注入 system prompt 污染模型输入。
        """
        if not self.props.show_inject_report or not text:
            return text
        line = self._format_inject_report(self._get_inject_report(target_id))
        if not line:
            return text
        return text.rstrip() + "\n\n" + line

    @staticmethod
    def _scrub_inject_report(text: str) -> str:
        """从对话历史里抹掉上一轮的注入报告行。

        报告只该出现在用户看到的那一条消息里。它作为 assistant 历史回显时会
        被模型模仿（下一轮自己写一行「〔注入〕…」），且对本轮推理毫无价值，
        因此在注入前统一清除。
        """
        if not text or _INJECT_REPORT_LINE_RE.search(text) is None:
            return text
        cleaned = _INJECT_REPORT_LINE_RE.sub("", text)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    @staticmethod
    def _format_inject_report(stats: dict) -> str:
        """把统计渲染成一行人类可读文本。

        无命中时也返回一行（「〔注入〕无命中」）而不是空串：用户开这个开关
        就是为了判断「调了参数之后到底有没有召回」。若没命中就不显示，
        他会分不清「开关没生效」和「确实什么都没命中」。

        文档来源名一并列出（引用溯源），最多 3 个，其余折叠为「等 N 份」。
        """
        if not stats:
            return ""
        parts = []
        if stats.get("wb"):
            parts.append(f"世界书×{stats['wb']}")
        if stats.get("mem"):
            parts.append(f"记忆×{stats['mem']}")
        if stats.get("core_mem"):
            parts.append(f"核心记忆×{stats['core_mem']}")
        if stats.get("wr"):
            parts.append(f"素材×{stats['wr']}")
        if stats.get("doc"):
            srcs = stats.get("doc_sources") or []
            label = f"文档×{stats['doc']}"
            if srcs:
                shown = "、".join(srcs[:3])
                if len(srcs) > 3:
                    shown += f" 等 {len(srcs)} 份"
                label += f"（{shown}）"
            parts.append(label)
        if not parts:
            return "〔注入〕无命中"
        return "〔注入〕" + " · ".join(parts)

    # _format_love_data / _parse_legacy_status / _build_default_love_data 已搬至
    # quill/services/statusbar/{render,parsers}.py（M2.1）。

    async def _llm_extract_status(self, text: str, target_id: str) -> dict | None:
        """方案C: LLM 智能提取状态栏字段 — 当 L1-L5 全部失败时，调用轻量 LLM 做结构化提取。

        风险控制:
        - 超时 3s，失败则放弃（不影响主流程）
        - JSON 解析失败则放弃
        - 字段名白名单校验（仅保留 self.props.love_fields 中的）
        - 启发式判断：文本中必须包含 ≥2 个字段关键词才触发
        """
        # 启发式：检查文本是否疑似包含状态信息
        keyword_hits = sum(1 for f in self.props.love_fields if f in text)
        if keyword_hits < 2:
            return None

        # P0-2: 模型路由 — 优先使用状态栏独立 provider，留空回退到 RAG summarizer
        provider_id = getattr(self.config, 'status_bar_llm_provider_id', '') or ''
        if not provider_id:
            provider_id = getattr(self.rag_summarizer, 'provider_id', '') if self.rag_summarizer else ''
        if not provider_id or not self.context:
            return None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if not provider:
            return None

        fields_json = json.dumps(self.props.love_fields, ensure_ascii=False)
        prompt = (
            "从以下角色扮演文本中提取角色状态字段，输出严格 JSON。\n"
            f"已知字段：{fields_json}\n"
            "规则：\n"
            "- 若某字段在文本中存在，提取其值（纯文本，去除 Markdown 标记）\n"
            "- 若某字段不存在，填 null\n"
            "- 仅输出 JSON 对象，不要任何额外文字\n\n"
            f"文本：\n{text[:1500]}"
        )

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt, session_id=None, contexts=[],
                    image_urls=[], system_prompt="你是 JSON 提取助手，仅输出 JSON。"
                ),
                timeout=3.0
            )
            raw = (resp.completion_text or "").strip()
            # 提取 JSON（兼容 ```json 包裹）
            if raw.startswith("```"):
                raw = re.sub(r'^```(?:json)?\s*', '', raw)
                raw = re.sub(r'\s*```$', '', raw)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            # 字段名白名单校验
            result = {}
            for f in self.props.love_fields:
                val = data.get(f)
                if val and isinstance(val, str) and val.strip():
                    result[f] = val.strip()
            return result if result else None
        except asyncio.TimeoutError:
            logger.debug("[Quill] LLM 状态栏提取超时（3s），放弃")
            return None
        except (json.JSONDecodeError, Exception) as e:
            logger.debug(f"[Quill] LLM 状态栏提取失败: {e}")
            return None

    # _parse_status_block 已搬至 quill/services/statusbar/parsers.py（M2.1）。

    # ── FunctionTool 描述泄漏防护 ─────────────────────────────
    _QUILL_SMT_DESC_MARKER = "MUST use this tool to send ALL reply text"
    _QUILL_ORIG_DESC_KEY = "_quill_orig_desc_saved"

    def _restore_smt_tool(self, req: ProviderRequest) -> None:
        """若 send_message_to_user 描述被 Quill 改写过，恢复原件。

        将原始描述存储在 req 对象上而非共享的 FunctionTool 实例，
        防止多请求并发时互相覆盖。"""
        if not req or not req.func_tool or req.func_tool.empty():
            return
        tool = req.func_tool.get_tool("send_message_to_user")
        if tool is None:
            return
        if self._QUILL_SMT_DESC_MARKER not in (tool.description or ""):
            return
        orig = getattr(req, self._QUILL_ORIG_DESC_KEY, None)
        if orig is not None:
            tool.description = orig
            try:
                delattr(req, self._QUILL_ORIG_DESC_KEY)
            except AttributeError:
                pass

    def _get_target_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "unified_msg_origin") and event.unified_msg_origin:
            return str(event.unified_msg_origin)
        return str(event.get_sender_id())

    def _get_memory_session_id(self, target_id: str, persona_id: str) -> str:
        return f"{target_id}::{persona_id}" if persona_id else target_id

    # ── 角色卡 → 对话隔离 ────────────────────────────────────────

    async def _ensure_persona_conversation(self, event: AstrMessageEvent) -> None:
        """把当前角色卡切到它自己的 AstrBot 对话上，实现对话历史隔离。

        M2.2 第二轮下沉薄转发：实现已整体搬至
        quill/services/character.py（``ensure_persona_conversation``，
        完整设计理由——为什么必须挂在 on_waiting_llm_request、快路径、
        死对话重建、首次接管、全量 try 放行——见彼处 docstring）。

        本方法保留以维持旧访问面（H1 委托链、/quill reset 的文档引用）
        与搬移前 ``self._ensure_persona_conversation`` 的动态分发路径
        逐字等价；宿主状态（context / state_manager / _get_target_id）
        以显式参数注入服务层，quill/ 侧零 astrbot 依赖。
        """
        await _character_mod.ensure_persona_conversation(
            self.context, self.state_manager, event, self._get_target_id,
        )

    @filter.on_using_llm_tool(priority=200)
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None
    ):
        """工具调用前拦截 — 在 Telegram 平台剥离 Markdown 标记，全平台格式化/擦除状态栏。

        注册桩（M2.2 第三轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_using_llm_tool``（完整设计理由与
        下沉决策见彼处 docstring；行为快照见 tests/test_hook_snapshots.py
        H2 节）。行为契约：

        * 三重闸门原样放行（不修改 tool_args）：非 send_message_to_user
          工具 → 未激活（``_quill_activated``）→ 空 tool_args；
        * telegram/tg 平台剥离 plain 段 Markdown（下沉
          quill/services/response.py；未知平台不剥离）；
        * 状态栏（全平台）：开启时首条 plain 走六级链提取渲染
          （handled 后 set ``_quill_status_handled``，后续只清残留），关闭时
          整套 ``_strip_status_artifacts``；注入报告追加到最后一条 plain；
        * messages 为 JSON 字符串时解析-修改-回写（解析失败原样放行）；
        * 拒绝模式补充扫描（S3-2，只扫首条 plain）。

        顶层降级留在桩内（与原 H2 同层，S2-3 语义）：任何异常吞掉 +
        error 日志放行——工具参数钩子抛出会打断 agent loop，降级语义 =
        不修改 tool_args。
        """
        try:
            await _quill_hooks.handle_using_llm_tool(self, event, tool, tool_args)
        except Exception as e:
            logger.error(f"[Quill] on_using_llm_tool 拦截异常，已降级放行: {e}", exc_info=True)

    async def _inject_persona_and_first_message(self, req: ProviderRequest, event: AstrMessageEvent, target_id: str) -> tuple:
        """获取角色卡数据并注入开场白。返回 (persona_id, persona_data)。"""
        persona_id = await self.state_manager.get_persona_id(target_id)
        # 切断 AstrBot 原生人格注入，防止双重 Prompt 污染
        if req.conversation:
            req.conversation.persona_id = "[%None]"

        persona_data = None
        if persona_id and self.persona_manager:
            persona_data = await self.persona_manager.get_persona(persona_id)
            if persona_data:
                # .get 的默认值只在键缺失时生效；键在但值为 null 时返回 None，
                # .strip() 会抛 AttributeError 把整个请求打成降级路径。
                fm = (persona_data.get("core_prompts", {}).get("first_message") or "").strip()
                if fm:
                    state = await self.state_manager.get_state(target_id)
                    if not state.first_message_injected:
                        # 上下文为空（无恢复记录）才判定为"初次见面"
                        is_truly_empty = not req.contexts or len(req.contexts) <= 1
                        if is_truly_empty:
                            if hasattr(req, 'contexts') and isinstance(req.contexts, list):
                                req.contexts.insert(0, {"role": "assistant", "content": fm})
                                logger.info(f"[Quill] 已注入 {persona_data.get('name', persona_id)} 的开场白 (first_message)")
                        await self.state_manager.mark_first_message_injected(target_id)
        return persona_id, persona_data

    async def _check_activation(self, user_input: str, context_text: str, persona_data) -> tuple:
        """检查激活状态（激活词 / 括号 / WR关键词）。返回 (activated, wr_activated)。"""
        activated = self.activation_detector.should_activate(user_input)
        has_bracket = self.activation_detector.check_brackets(user_input)

        wr_activated = False
        if not (activated or has_bracket) and self.wr_manager:
            try:
                ext = persona_data.get("quill_extensions", {}) if persona_data else {}
                wr_mode = ext.get("wr_mode", "disabled")
                bound_wrs = ext.get("bound_writing_resource", []) if wr_mode == "custom" else None

                logger.info(f"[Quill] 写作素材库模式: {wr_mode}，绑定分类: {bound_wrs if bound_wrs is not None else 'Auto (全局匹配)'}")

                if wr_mode != "disabled":
                    fetch_count = 7 if bound_wrs is not None else 3
                    matched = await self.wr_manager.match(context_text, top_k=fetch_count, log_match=False)

                    if bound_wrs is not None:
                        matched = [m for m in matched if m.get("category") in bound_wrs]

                    wr_activated = len(matched) > 0
                    if wr_activated:
                        logger.info(f"[Quill] 写作素材库关键词触发激活: {len(matched)} 条匹配 (context)")
                    else:
                        logger.info(f"[Quill] 写作素材库未匹配到内容")
                else:
                    logger.info(f"[Quill] 写作素材库模式: disabled，跳过素材检索")
            except Exception as e:
                logger.warning(f"[Quill] WR 匹配失败: {e}")

        return activated, wr_activated

    async def _run_rag_retrieval(self, event: AstrMessageEvent, req: ProviderRequest, user_input: str, persona_data, dynamic_prompt: str, stats: dict | None = None) -> str:
        """执行 RAG 检索（Doc + Memory），返回更新后的 dynamic_prompt。

        `stats` 为可选出参：回填 doc/mem 的命中条数与文档来源名（注入报告用）。
        """
        if not (self.rag_retriever and self.rag_retriever.embedding):
            logger.warning(f"[Quill RAG] 文档系统未初始化")
            return dynamic_prompt

        try:
            target_id = self._get_target_id(event)
            persona_id = await self.state_manager.get_persona_id(target_id)
            mem_session_id = self._get_memory_session_id(target_id, persona_id)

            doc_results = []
            ext = persona_data.get("quill_extensions", {}) if persona_data else {}
            rag_mode = ext.get("rag_mode", "disabled")

            logger.info(f"[Quill RAG] 模式: {rag_mode}，绑定文档: {ext.get('bound_rag_docs', []) if rag_mode == 'custom' else 'Auto (全库)'}")

            if rag_mode != "disabled":
                bound_rag_docs = ext.get("bound_rag_docs", []) if rag_mode == "custom" else None
                doc_results = await self.rag_retriever.search_documents(user_input, allowed_sources=bound_rag_docs)
                logger.info(f"[Quill RAG] 文档检索结果: {len(doc_results)} 段")
            else:
                logger.info(f"[Quill RAG] 模式: disabled，跳过文档检索")

            mem_results = await self.rag_retriever.search_memories(mem_session_id, user_input)
            logger.info(f"[Quill RAG] 记忆检索: {len(mem_results)} 条 (Session: {mem_session_id})")

            # 核心记忆：无条件注入，不参与 Top-K 竞争
            core_mems = await self.rag_retriever.get_core_memories(mem_session_id)
            if core_mems:
                logger.info(f"[Quill RAG] 核心记忆: {len(core_mems)} 条 (Session: {mem_session_id})")

            rag_context = self.rag_retriever.format_for_prompt(doc_results, mem_results, core_mems)
            if rag_context:
                dynamic_prompt += "\n\n" + rag_context
                logger.info(f"[Quill RAG] 注入上下文: {len(rag_context)} 字符")
            if stats is not None:
                stats["doc"] = len(doc_results)
                # 去重保序：同一份文档常有多段命中，来源名只列一次
                seen_src: list[str] = []
                for r in doc_results:
                    src = str(r.get("source", "") or "").strip()
                    if src and src not in seen_src:
                        seen_src.append(src)
                stats["doc_sources"] = seen_src
                stats["mem"] = len(mem_results)
                stats["core_mem"] = len(core_mems)
            # P1-4: 记录 RAG 检索结果。此前检索器吞异常返回 []，这里统一记 True，
            # 于是 embedding/索引故障在健康度里表现为 100% 成功。现在按 rag_ok
            # 判定：空结果算成功（确实没找到），只有真出错才算失败。
            from .quill_rag.retrieval import rag_ok as _rag_ok
            doc_ok = _rag_ok(doc_results)
            mem_ok = _rag_ok(mem_results)
            if not doc_ok or not mem_ok:
                logger.warning(
                    "[Quill RAG] 检索降级: doc=%s mem=%s",
                    getattr(doc_results, "_rag_error", "ok"),
                    getattr(mem_results, "_rag_error", "ok"),
                )
            self.health_tracker.record_rag(doc_ok and mem_ok)
        except Exception as e:
            logger.warning(f"[Quill RAG] 检索失败: {e}")
            # P1-4: 记录 RAG 检索失败
            self.health_tracker.record_rag(False)

        return dynamic_prompt

    async def _rewrite_smt_tool_description(self, req: ProviderRequest, persona_id: str = "") -> None:
        """改写 send_message_to_user 工具描述。状态栏指令通过 system prompt + tail message 注入。

        无角色卡时不重写：原始描述不会强制 "MUST call IMMEDIATELY"，
        避免 LLM 在无人设约束时进入 Agent 死循环（连续调用工具）。
        """
        if not persona_id:
            # 无角色卡时跳过重写，避免 LLM 失控循环
            return
        if not (req.func_tool and not req.func_tool.empty()):
            return
        smt_tool = req.func_tool.get_tool("send_message_to_user")
        if smt_tool and self._QUILL_SMT_DESC_MARKER not in (smt_tool.description or ""):
            setattr(req, self._QUILL_ORIG_DESC_KEY, smt_tool.description)
            smt_tool.description = (
                "THIS IS THE ONLY TOOL for sending replies. Output text DIRECTLY in your response will be DISCARDED. "
                "You MUST call this tool to send ANY reply text — do NOT output text in the content field. "
                "Call this tool IMMEDIATELY as your first action — do not call any other tools before sending your message."
            )
            logger.info(f"[Quill] 已重写 send_message_to_user 描述 (persona={persona_id})")

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求拦截：触发平行宇宙隔离，执行状态栏降级解析与多维 Prompt 组装注入。

        核心职责：
        - 平行宇宙双轴隔离 (target_id::persona_id)：按群+角色切分独立状态
        - 激活检测：决定本次请求是否进入 RP 模式
        - Context Restoration：req.contexts 为空时从 chat_logs 捞取最近 N 条垫入
        - First Message 智能抑制：避免重启后突兀复读开场白
        - 4 层 Prompt 装配：系统/角色/世界书/WR/RAG 多源注入
        - 状态栏降级解析：5 级兜底（STATUS 块→LOVE_DATA→legacy→RAW→lenient）
        """
        try:
            # P1-3 修复：提前初始化，避免 except 块引用未定义变量掩盖原始异常
            emergency = False
            extra_info = {}
            self._restore_smt_tool(req)

            user_input = req.prompt or ""
            target_id = self._get_target_id(event)

            # ── 上下文恢复（重启/滑动窗口切断后无缝续传）──
            mem_session_id = self._get_memory_session_id(
                target_id,
                await self.state_manager.get_persona_id(target_id)
            )
            # 防御性类型守卫：AstrBot 框架契约保证 contexts 为 list，但防止异常值导致崩溃
            if not isinstance(req.contexts, list):
                req.contexts = []
            # 抹掉历史里的注入报告行：它只该出现在用户看到的那条消息里，
            # 回显进上下文会被模型模仿（下一轮自己写一行），且对本轮推理无价值。
            req.contexts = [
                ({**c, "content": self._scrub_inject_report(c.get("content", ""))}
                 if isinstance(c, dict) and isinstance(c.get("content"), str) else c)
                for c in req.contexts
            ]
            contexts_is_fresh = not req.contexts or len(req.contexts) <= 1
            if contexts_is_fresh \
                    and getattr(self.config, 'rag_enable_chat_logging', True) \
                    and self.rag_retriever and self.rag_retriever.memory_store:
                recent_logs = await self.rag_retriever.memory_store.get_recent_chat_logs(mem_session_id, limit=8)
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
            _sb_effective = await self._effective_status_bar_enabled(target_id)
            if not _sb_effective and req.contexts:
                _scrubbed = []
                for c in req.contexts:
                    if isinstance(c, dict) and isinstance(c.get("content"), str):
                        clean = self._strip_status_artifacts(
                            c["content"], self.props.love_fields
                        )
                        _scrubbed.append({**c, "content": clean} if clean != c["content"] else c)
                    else:
                        _scrubbed.append(c)
                req.contexts = _scrubbed

            persona_id, persona_data = await self._inject_persona_and_first_message(req, event, target_id)

            # 记录用户消息（仅已绑定角色卡且非指令时）
            if persona_id and user_input and not user_input.strip().startswith("/") \
                    and getattr(self.config, 'rag_enable_chat_logging', True) \
                    and self.rag_retriever:
                self._spawn(self.rag_retriever.log_chat_message(
                    mem_session_id, "user", user_input
                ))

            # P1-8: 自然语言核心记忆注入 — 检测 @记住 / 核心记忆 / @remember 前缀
            # 审查修复：切片统一以 stripped 文本为基准（此前 strip 后匹配、原文切片，
            # 带前导空白时 prompt 残留尾部字符）；剥离后为空则保留原文（避免空 prompt
            # 仍发给 LLM）；群聊写入需通过 admin 权限校验（与 /memory core 对齐）。
            _core_nl = None
            if persona_id and user_input and self.rag_retriever and self.rag_retriever.memory_store:
                _stripped = user_input.strip()
                _core_match = _CORE_MEMORY_NL_RE.match(_stripped)
                if _core_match:
                    _perm_err = _cmds._check_group_permission(self, event)
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
                self._spawn(self.rag_memory_store.update_core_memory(
                    mem_session_id, _core_nl, _core_nl
                ))

            # 存储最近 6 轮对话，供 /memory learn 自动总结
            if hasattr(req, 'contexts') and isinstance(req.contexts, list):
                # P3-5 修复：深拷贝切片，避免后续 req.contexts 被修改（如上下文恢复）后引用失效
                recent_msgs = [dict(c) for c in req.contexts[-12:] if c.get("role") in ("user", "assistant")]
                event.set_extra("_quill_recent_msgs", recent_msgs)

            # Build multi-turn context for WR matching
            context_text = user_input
            if req.contexts and isinstance(req.contexts, list):
                recent = req.contexts[-4:]
                parts = [user_input]
                for msg in recent:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "assistant") and isinstance(content, str):
                        parts.append(content)
                context_text = "\n".join(parts)

            activated, wr_activated = await self._check_activation(user_input, context_text, persona_data)
            has_bracket = self.activation_detector.check_brackets(user_input)

            # 全局常驻模式：跳过激活检测
            always_activate = getattr(self.config, "worldbook_always_activate", False)
            if always_activate:
                activated = True
                wr_activated = False

            if not (activated or has_bracket or wr_activated):
                await self.state_manager.reset_quill_rounds(target_id)
                return

            quill_rounds = await self.state_manager.increment_quill_rounds(target_id)
            skip_constants = quill_rounds > 1
            if skip_constants:
                logger.info(f"[Quill] 连续第 {quill_rounds} 轮激活，跳过 Layer 1 常驻")

            # 改写 send_message_to_user 描述（含状态栏强制要求）
            # 无角色卡时跳过：避免 LLM 在无人设约束时进入 Agent 死循环
            await self._rewrite_smt_tool_description(req, persona_id)

            if wr_activated and self.wr_manager and self.props.debug:
                try:
                    debug_match = await self.wr_manager.match(context_text, top_k=10, log_match=False)
                    for e in debug_match:
                        logger.info(
                            f"[Quill] WR 匹配: {e.get('entry_id','')} "
                            f"(score={e.get('match_score',0)}, "
                            f"kw={e.get('keywords',[])})"
                        )
                except Exception:
                    logger.debug("[Quill] 调试 WR 匹配失败", exc_info=True)

            emergency = await self.state_manager.should_inject_emergency(target_id)

            extra_info = {
                "user_input": user_input,
                "context_text": context_text,
                "persona_id": persona_id,
                "persona_data": persona_data,
                "user_id": target_id,
                "wr_max_entries": self.props.wr_max_entries,
                "wr_fallback_top_count": self.props.wr_fallback_top_count,
                "wb_max_entries": self.props.wb_max_entries,
                "wb_sensitivity": self.config.worldbook_sensitivity,
                "wb_max_token": self.config.worldbook_max_token,
                "skip_constants": skip_constants,
                "session_vars": await self.state_manager.get_session_vars(target_id),
            }

            # 注入报告统计（本轮各来源命中条数）。始终采集——即使 debug 关闭，
            # 本轮最终开关：会话级覆盖 > 面板全局。上面（历史 contexts 清理）与
            # 下面（system prompt 契约、tail message）必须用同一个值，否则会出现
            # 「system prompt 说别输出、tail 说必须输出」的自相矛盾。
            _pb = self._prompt_builder_for_request(_sb_effective)

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
            _wb_for_request = self.wb_manager if getattr(
                self.config, "worldbook_enabled", True
            ) else None
            stable_prompt, dynamic_prompt = await _pb.build_system_prompt(
                self.wr_manager, _wb_for_request, extra_info, emergency=emergency,
                stats=inject_stats,
            )

            # ── RAG 检索（Doc RAG + 动态记忆）──
            dynamic_prompt = await self._run_rag_retrieval(
                event, req, user_input, persona_data, dynamic_prompt, inject_stats
            )

            # 触发日志注入（show_trigger_log 开启时）
            if (self.config.worldbook_show_log and _wb_for_request
                    and hasattr(_wb_for_request, 'get_trigger_log')):
                # get_trigger_log 是同步方法（加锁读一次列表），不能 await
                trigger_log = _wb_for_request.get_trigger_log()
                if trigger_log:
                    log_lines = ["[触发日志]"]
                    for t in trigger_log[:10]:
                        log_lines.append(f"  {t['worldbook']}/{t['title']} ← {','.join(t['matched_keys'])}")
                    dynamic_prompt += "\n\n" + "\n".join(log_lines)

            self._remember_inject_report(target_id, inject_stats)

            req.system_prompt = self.props.prompt_builder.inject_prompt(
                req.system_prompt or "", stable_prompt, dynamic_prompt,
                injection_position=self.config.worldbook_injection_pos
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

            await self.state_manager.update_activity(target_id)
            await self.state_manager.clear_refusal(target_id)

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
        except Exception as e:
            # 记录脱敏摘要，避免泄露 user_input、context_text 等敏感字段
            def _sanitize_extra(info: dict) -> dict:
                return {
                    "persona_id": info.get("persona_id"),
                    "user_id_len": len(str(info.get("user_id", ""))),
                    "user_input_len": len(str(info.get("user_input", ""))),
                    "context_text_len": len(str(info.get("context_text", ""))),
                    "skip_constants": info.get("skip_constants"),
                    "emergency": emergency,
                }

            logger.error(
                f"[Quill] 致命错误，Prompt 装配失败，降级放行: {e} | "
                f"extra_summary={_sanitize_extra(extra_info)}",
                exc_info=True,
            )

    @filter.on_llm_response(priority=10)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 响应拦截：前置清洗、状态栏提取渲染、注入报告、落日志与拒绝扫描。

        注册桩（M2.2 第四轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_llm_response``（完整设计理由、
        段序与下沉决策见彼处 docstring；行为快照见
        tests/test_hook_snapshots.py H4 节）。行为契约：

        * 前置清洗：``[B:...]`` Base64 解密安全网、用户中断标记擦除；
        * **状态栏段不受 ``_quill_activated`` gate 限制**（BASELINE §2.1
          不对称点，快照钉住）——开启时 ``_quill_status_handled`` 已置位
          则只剥残留（不二次渲染），否则六级链提取渲染 + 无栏兜底；
          关闭时整套剥离；随后注入报告追加（``_quill_report_added``
          去重，H2 工具路径与本路径共用）；
        * gate 后：未激活 return；助手回复落 chat_logs（
          ``rag_enable_chat_logging`` 开关 + ``_quill_assistant_logged``
          防双写标记原样保留）；拒绝模式扫描命中 ``mark_refusal``。

        顶层降级留在桩内（与原 H4 同层）：任何异常吞掉 + error 日志放行，
        resp 保持已改到一半的状态（降级语义 = 放行当前响应）。
        """
        try:
            await _quill_hooks.handle_llm_response(self, event, resp)
        except Exception as e:
            logger.error(f"[Quill] on_llm_response 后处理遭遇未捕获异常，已降级放行: {e}", exc_info=True)

    @filter.on_llm_tool_respond(priority=10)
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None, tool_result
    ):
        """工具调用后拦截：Agent Loop 终止信号、动态记忆存储与多轮反思调度。

        注册桩（M2.2 第五轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_llm_tool_respond``。核心职责：

        - 检测 send_message_to_user 调用，终止 agent loop
        - AI 回复异步写入对话日志（供断点续传使用）
        - N 轮反思触发：攒够阈值后生成上下文摘要
        - 记忆修剪调度（分档遗忘）
        - 过期对话日志无人值守清理（避免长期运行日志膨胀）

        **降级怪癖（全插件唯一，刻意保真）**：本钩子**没有顶层 try**——
        gate 之间与 ``logger.info`` 等处的异常原样上抛框架；唯一异常处理
        是 interfaces 实现内两个内层块各自的 warning 吞掉（记忆存储调度 /
        反思调度，后者已下沉 quill/services/memory.py）。快照
        test_h5_exception_between_gates_propagates_no_top_try 钉住此行为，
        防后续误补 try。

        行为契约（legacy t26 源码窗口断言引用的标记原文，实际置位发生在
        interfaces 实现内，此处逐字保留以锁定泄漏修复语义——记忆去重
        标记与总闸门必须分离）：
        ``event.set_extra("_quill_memorized", True)``；
        总闸门永不在工具回调中被清除（不得出现
        set_extra("_quill_activated", False) 调用）。
        """
        await _quill_hooks.handle_llm_tool_respond(self, event, tool, tool_args, tool_result)


    # ================================================================
    # 最后一道防线：发送前擦除残留状态栏（M2.2 薄化：注册桩 + 委托）
    # ================================================================

    @filter.on_decorating_result(priority=100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息**发送前**的最后一次清洗——擦掉漏网的状态栏残留。

        注册桩（M2.2）：装饰器/签名/priority 不变（框架以 ``__module__``
        精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_decorating_result``（完整设计
        理由见彼处 docstring）。行为契约：

        * 无 ``_quill_activated`` gate，始终执行；**只减法不补栏**；
        * 两档强度——状态栏开启只擦原始标记（``_strip_raw_markers``），
          绝不能用整套剥离器（会把 L1/L2 正常渲染的栏整段删掉）；
          关闭时整套剥离（``_strip_status_artifacts``）；
        * 顶层降级留在桩内（与原 H6 同层）：任何异常吞掉放行——发送前
          钩子绝不能抛，抛了会中断整条回复的发送。
        """
        try:
            await _quill_hooks.handle_decorating_result(self, event)
        except Exception:
            # 发送前钩子绝不能抛：抛了会中断整条回复的发送
            logger.warning("[Quill] 发送前清理异常，已放行", exc_info=True)

    # ================================================================
    # 用户指令
    # ================================================================

    @filter.command("wb")
    async def cmd_wb(self, event: AstrMessageEvent, args: GreedyStr):
        """世界书管理。用法：/wb | /wb bind <序号|名字> | /wb unbind <序号|名字> | /wb info <序号|名字> | /wb reload"""
        arg1, arg2 = _split2(args)
        await _cmds.wb_dispatch(self, event, arg1, arg2)

    @filter.command("char")
    async def cmd_char(self, event: AstrMessageEvent, args: GreedyStr):
        """角色卡管理。用法：/char | /char <序号|名字> | /char unset | /char info [序号|名字] | /char export [序号|名字] | /char import <JSON>"""
        await _cmds.char_dispatch(self, event, args)

    @filter.command("quill")
    async def cmd_quill(self, event: AstrMessageEvent, args: GreedyStr):
        """Quill 系统总览与测试。用法：/quill | /quill help | /quill reset | /quill debug | /quill statusbar [on|off|auto] | /quill test <wr|wb|mem> <文字>"""
        arg1, rest = _split2(args)
        arg1_lower = (arg1 or "").strip().lower()
        if arg1_lower == "help":
            await _cmds.quill_help(event)
            return
        if arg1_lower == "reset":
            await _cmds.quill_reset(self, event)
            return
        if arg1_lower == "debug":
            await _cmds.quill_debug(self, event)
            return
        if arg1_lower == "statusbar":
            await _cmds.statusbar_dispatch(self, event, rest)
            return
        if arg1_lower == "test":
            text = (rest or "").strip()
            # 解析: /quill test wr <文字> 或 /quill test <文字>
            parts = text.split(None, 1) if text else []
            if len(parts) >= 2 and parts[0].lower() in ("wr", "wb", "mem"):
                system = parts[0]
                test_text = parts[1]
            else:
                system = "wr"
                test_text = text
            if not test_text:
                from astrbot.core.message.message_event_result import MessageEventResult
                event.set_result(MessageEventResult().message("用法: /quill test <wr|wb|mem> <文字>"))
                return
            await _cmds.quill_test(self, event, system, test_text)
            return
        await _cmds.quill_status(self, event)

    @filter.command("memory")
    async def cmd_memory(self, event: AstrMessageEvent, args: GreedyStr):
        """动态记忆管理。用法：/memory | /memory list [页码] | /memory del <序号> | /memory clear | /memory learn [内容] | /memory search <关键词> | /memory pin <序号> [on|off] | /memory core <内容>"""
        arg1, arg2 = _split2(args)
        await _cmds.memory_dispatch(self, event, arg1, arg2)

    @filter.command("doc")
    async def cmd_doc(self, event: AstrMessageEvent, args: GreedyStr):
        """外部文档 RAG 管理。用法：/doc list | /doc bind <序号> | /doc unbind <序号> | /doc search <关键词> | /doc reload"""
        arg1, arg2 = _split2(args)
        await _cmds.doc_dispatch(self, event, arg1, arg2)

    @filter.command("stream")
    async def cmd_stream(self, event: AstrMessageEvent, arg: str = ""):
        """流式模式控制。用法：/stream on|off|auto"""
        await _cmds.stream_dispatch(self, event, arg)

    @register_command("reinject", alias={"重新注入"})
    async def cmd_reinject(self, event: AstrMessageEvent):
        """强制重置注入状态，下次激活重新注入全部常驻素材。用法：/reinject"""
        await _cmds.reinject_dispatch(self, event)
