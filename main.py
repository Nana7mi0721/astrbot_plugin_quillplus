# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillPlugin — 羽笔 v5.3 多维沉浸式 RP 增强插件

六合一沉浸式 RP 注入系统：世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆 + 状态栏。

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
- admin_users 同时用于群聊写指令权限与 Web 面板写/删路由校验
"""

import asyncio
import copy
import json
import os
import re

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
# C1：FunctionTool / register_command 都有公开导出面，不再从 astrbot.core.* 取，
# 避免内部模块改名时跟随断裂（astrbot.api 是框架承诺稳定的那一层）。
from astrbot.api import FunctionTool
from astrbot.api.event.filter import command as register_command
# GreedyStr 例外保留 core 路径：框架在 `validate_and_convert_params` 里用
# **身份比较**（`param_type_or_default_val is GreedyStr`）判定「吃掉剩余文本」，
# 公开面没有导出它，自造同名类不会生效。此处依赖的是该标记类本身而非内部实现。
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
# M2.0 搬移期约定——probe 脚本与外部旧脚本经 main 模块取旧导入面
# （原 tests/legacy 已归档，见 .backup/tests_v5.3.2_unit_regression/）。
from .prompt_builder import PromptBuilder  # noqa: F401  (legacy/probe 导入面)
from . import commands as _cmds
from .web_routes import QuillRoutes
from .persona_manager import QuillPersonaManager
from .quill import __version__
from .quill.core import logbridge
from .quill.core.errors import StorageError
# M2.2 剥离器下沉：实现住 quill/services/statusbar/strip.py，
# QuillPlugin 类体内保留同名薄转发（见「状态栏解析共享方法」段）。
from .quill.services.statusbar import strip as _strip_mod
# M2.2 第二轮：角色卡→对话隔离下沉 quill/services/character.py
# （_ensure_persona_conversation 保留同名薄转发，见彼处）。
from .quill.services import character as _character_mod
# M1a：注入上下文 trace（ring buffer + 数据结构，R1 规范全见彼处 docstring）。
from .quill.services import trace as _trace_mod
# M2.2 第五轮：H5 反思调度下沉 quill/services/memory.py；阈值常量在类属性
# 处 re-export（QuillPlugin.REFLECTION_* 旧访问面）。
from .quill.services import memory as _memory_mod
# M2.2 第三轮：H2 的 telegram Markdown 剥离下沉 quill/services/response.py。
# 唯一的实现消费方在 astrbot_hooks（经 response_mod 调用），故本模块**不再**
# re-export `_MD_PATTERNS`/`strip_markdown`——那三个名字全仓库零引用，只是
# M2.0 搬移期留下的空导入面，已随 D1/D2 一并清理。
from .quill.services.statusbar import (
    StatusbarParsersMixin,
    StatusbarRenderMixin,
    # ── 搬移期 re-export（M2.0 约定）：状态栏解析侧常量/纯函数已搬至
    # quill/services/statusbar/parsers.py，这里保留旧模块级名字，
    # 供 probe 脚本与外部旧脚本的旧 import 面使用（原 tests/legacy 的
    # t8 与各处 M.<name> 断言随该套件一并归档）。
    # 标签常量（LOVE_DATA_TAG/STATUS_TAG/STATUS_END_TAG）与剥离器侧常量
    # （_STRIP_LOVE_DATA_RE 等）无任何外部引用，故不在本列表内。
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
from .interfaces.usage import UsageRecorder
from .interfaces.scopes import freeze_turn, event_turn
from .quill.services.scope import ScopeResolver, bind_turn
from .quill.services.tasks import TaskRegistry
from .quill_rag.usage_store import UsageStore
from .quill.services.prompt_cache import SegmentProfiler


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


# 未唤醒场景的 /指令 兜底（群聊，或配置档把唤醒词清空时）。
# wake_prefix 列表为空时 core/pipeline/waking_check/stage.py 的
# `for wake_prefix in wake_prefixes` 整个循环不进入；群聊消息又没有 @/回复时
# is_wake 始终为假，该阶段末尾 `if is_wake: ... else: event.stop_event()` 会把
# 整条消息丢弃——指令处理器连被匹配的机会都没有（CommandFilter.filter 首行就是
# `if not event.is_at_or_wake_command: return False`）。
# RegexFilter 是全框架唯一不检查唤醒状态的过滤器（core/star/filter/regex.py），
# 且它通过这一事实本身会让唤醒检查把 is_wake 置真、事件得以继续流转，故用它兜底。
# 只认「消息以 /指令名 开头」，命中后复用同一套 dispatch：参数解析、权限门、
# 输出格式全部不变。命中时 is_at_or_wake_command 仍为假，ProcessStage 不会接着
# 调 LLM，所以 /char 不会被当成普通聊天再答一遍。
_SLASH_CMD_RE = re.compile(r"^/(wb|char|quill|memory|doc|stream|reinject)(?=\s|$)")


class _UnwokenSlashFilter(filter.CustomFilter):
    """未唤醒时的 /指令 兜底门。

    只在 `is_at_or_wake_command` 为假时放行：该标志为真说明正常唤醒路径已经
    接管（私聊、群聊 @ 机器人、或唤醒词命中），此时再放行会让同一条指令被派发
    两次——StarRequestSubStage 逐个执行 activated_handlers，每个 handler 各自
    走到发送阶段，用户会收到两条一模一样的回复。

    保留 `raise_error` 形参：register_custom_filter 会以位置参数实例化本类
    （`custom_filter(raise_error)`），签名不符会直接注册失败。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        if event.is_at_or_wake_command:
            return False
        return bool(_SLASH_CMD_RE.match((event.get_message_str() or "").strip()))


class _AlreadyAwakeFilter(filter.CustomFilter):
    """M7 能力门只检查原本已唤醒的消息，不改变群聊唤醒条件。"""
    def __init__(self,raise_error=False):
        pass

    def filter(self,event,cfg):
        return bool(event.is_at_or_wake_command)


async def _quill_dispatch(plugin, event: AstrMessageEvent, args: str) -> None:
    """/quill 的子命令分发（三条入口共用：/quill、/quill 兜底）。

    抽成模块级而非留在方法里：正常入口与未唤醒兜底走的是同一份分发，
    避免两处各写一遍后逐渐漂移（此前 /quill test 的 system 白名单就只在一处）。
    """
    arg1, rest = _split2(args)
    arg1_lower = (arg1 or "").strip().lower()
    if arg1_lower == "help":
        await _cmds.quill_help(event)
        return
    if arg1_lower == "compact":
        await _cmds.quill_compact(plugin, event)
        return
    if arg1_lower == "reset":
        await _cmds.quill_reset(plugin, event,rest)
        return
    if arg1_lower == "debug":
        await _cmds.quill_debug(plugin, event)
        return
    if arg1_lower == "statusbar":
        await _cmds.statusbar_dispatch(plugin, event, rest)
        return
    if arg1_lower == "test":
        text = (rest or "").strip()
        # 解析: /quill test wr <文字> 或 /quill test <文字>
        # M1b-Fix P1-e：kb 分支（AstrBot 内置知识库）并入 system 白名单
        parts = text.split(None, 1) if text else []
        if len(parts) >= 2 and parts[0].lower() in ("wr", "wb", "mem", "kb"):
            system = parts[0]
            test_text = parts[1]
        else:
            system = "wr"
            test_text = text
        if not test_text:
            from astrbot.api.event import MessageEventResult
            event.set_result(
                MessageEventResult().message("用法: /quill test <wr|wb|mem|kb> <文字>")
            )
            return
        await _cmds.quill_test(plugin, event, system, test_text)
        return
    await _cmds.quill_status(plugin, event)


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


def _sanitize_extra(info: dict) -> dict:
    """H3 降级日志的 extra 摘要：只记 persona_id/长度字段，不泄用户原文。

    M1a 自 on_llm_request 注册桩的 except 闭包提升为模块级（语义逐字不变）：
    trace 侧（target_id 脱敏键等）与降级日志共用同一「只记长度不记内容」
    口径，防平行造第二套脱敏（红线 7）。键集合保持原样——增删键等于
    改日志契约，须与 R1 一起评审。
    """
    return {
        "persona_id": info.get("persona_id"),
        "user_id_len": len(str(info.get("user_id", ""))),
        "user_input_len": len(str(info.get("user_input", ""))),
        "context_text_len": len(str(info.get("context_text", ""))),
        "skip_constants": info.get("skip_constants"),
    }


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
# 本模块不再 re-export（零引用，见 D2）。


@register(
    "astrbot_plugin_quillplus",
    # 与 metadata.yaml 的 author 逐字一致：AstrBot 的 plugin_id 由 author/name
    # 派生（star.py::plugin_id → f"{author}/{name}"），author 是**身份值**而不是
    # 展示名。多作者展示串会让 plugin_id 变成一长串，与市场登记对不上；协作者
    # 的致谢放在 README 鸣谢节。yaml 优先，这里只是 subclass 注册路径的兜底。
    "Nana7mi0721",
    # 描述与 metadata.yaml desc 逐字一致（M4.1 单源化，原先此处是 v5.0 时代
    # 的五合一旧文案，与六合一漂移）；版本号取 quill/__init__.py 唯一真源，
    # 由 tools/check_version.py 静态对拍。
    "世界书+写作素材库+角色卡+文档RAG+动态记忆+状态栏 六合一沉浸式 RP 核心引擎 | 六级降级解析兜底 | 平行宇宙双轴隔离 | JSON 原子化状态机 | 无损断点续传",
    __version__,
    "https://github.com/Nana7mi0721/astrbot_plugin_quillplus",
)
class QuillPlugin(StatusbarParsersMixin, StatusbarRenderMixin, Star):
    """羽笔 — v5.3 多维沉浸式 RP 增强插件

    六合一沉浸式 RP 注入系统：世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆 + 状态栏。

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

    # M2.2 第六轮：核心记忆自然语言前缀正则的定义留在本模块（H3 支撑面，
    # 消费方已迁 interfaces），经类属性 re-export 供
    # interfaces.astrbot_hooks.handle_llm_request 以 plugin._CORE_MEMORY_NL_RE
    # 访问（H5 REFLECTION_* 同款先例；模块级旧名照常可用）
    _CORE_MEMORY_NL_RE = _CORE_MEMORY_NL_RE

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
        # M1a：注入上下文 trace ring buffer（纯内存，重启即清；每会话 20 轮、
        # 全局 200，R1 规范与红线见 quill/services/trace.py）。采集点在
        # interfaces/astrbot_hooks.py 的 22 步编排内就地附带；读取方（/quill
        # debug、面板 trace 端点）一律经 _get_inject_report / _trace_latest
        # 访问。读取处仍需 getattr 防御（AGENT.md 运行期硬约束——__init__
        # 极端中断时属性可能缺失）。
        self._trace_store = _trace_mod.TraceStore()
        # Decision 剧情走向轮次缓存（内存态，按 target_id 一轮一条，5 分钟过期自动清）：
        # H4 请求钩子写入（推荐选择度分布 + argmax 分支），H6 装饰（parsers/render
        # 追加「▸ N%」）与 /quill debug 读取。不落盘——百分比只服务当前轮回复，
        # 持久化反而会把过期分布带进下一轮。
        self._decision_round_cache: dict[str, dict] = {}

        # H1：框架 send_message_to_user 的原文描述快照。必须在 __init__ 就赋初值——
        # AGENT.md 运行期约束要求「读可能未初始化的属性必须 getattr(self, ...)」，
        # 但 _restore_smt_tool 在每次 on_llm_request 开头都会跑，靠 getattr 兜底
        # 会让「initialize 尚未执行」这段时间的还原静默失效；显式初始化更直白。
        self._smt_orig_desc: str | None = None

        # --- 运行数据位置 ---
        # 数据库/世界书/状态原先放在插件目录内（knowledge/、worldbooks/、data/），
        # 而插件开着长连接，Windows 下更新器删不掉这些文件，更新会中途失败并把
        # 安装目录留成半新半旧。这里统一迁到 AstrBot 约定的
        # data/plugin_data/<插件名>/（更新器不碰），并做一次性搬迁。
        self.paths = resolve_data_layout(self.plugin_dir)
        from .interfaces.context_compact import ContextCompactor
        self.context_compactor = ContextCompactor(self)

        self.scope_resolver = ScopeResolver()
        self.task_registry = TaskRegistry()
        self._usage = UsageRecorder(
            self.context,
            UsageStore(os.path.join(self.paths["data_root"] or self.paths["state_dir"], "quill_usage.db")),
            lambda: self.config,
            session_resolver=lambda value: self.scope_resolver.from_session_key(
                value, self.state_manager.known_origins()),
        )
        self._cache_profiler = SegmentProfiler()
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
        from .interfaces.groups import SharedScopes
        self.shared_scopes = SharedScopes(self)
        self.shared_scopes.bind_state()

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

        # --- Prompt builder / status bar / debug ---
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
        self.rag_kb_source = None  # AstrBot 内置知识库源（无自建句柄，仅引用）
        self.rag_doc_groups = None  # 插件文档分组的元数据存储（JSON）

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
    # 会挡掉实例属性赋值——外部脚本用 object.__new__(QuillPlugin) 造轻量
    # 宿主后直接 setattr 的用法（原 tests/legacy 的 t24/t25/t26 即此类）
    # 会因此失效。__getattr__ 只在**常规
    # 属性查找失败**时触发：实例字典里已有的同名属性照常生效（与 v5.2.5
    # 的可写实例属性行为完全一致），默认路径实时读 props，不留第二真源。
    # 运行期自身代码没有任何对这些名字的赋值点
    # （现由 docs/probe_config_effects.py 的真机键效应探测 +
    # .build/harness/quilltest/run.py --api 的 api_config_write_roundtrip 看守）。
    _MIXIN_CONFIG_ATTRS = frozenset({
        "love_fields",
        "status_bar_enabled",
        "status_bar_format_template",
        "status_bar_format_plain",
        "status_bar_plain_platforms",
        "status_bar_plot_paths",
        "status_bar_decision_enabled",
        "status_bar_decision_confidence_floor",
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
        # 完整空结构是成功零产出；缺字段/类型错是失败，不能因此推进水位。
        return (isinstance(reflection,dict)
                and isinstance(reflection.get("new_core_traits"),str)
                and isinstance(reflection.get("crucial_facts"),str)
                and isinstance(reflection.get("trivial_summaries"),list)
                and all(isinstance(item,str) for item in reflection["trivial_summaries"]))

    async def _run_idle_reflection(self, session_id, handle):
        store=self.rag_memory_store
        retriever=self.rag_retriever
        progress=await store.get_reflection_progress(session_id)
        logs=await store.get_chat_logs_after(session_id,progress['last_log_id'],limit=50)
        if not logs:
            return {"processed":0,"stored":0}
        await handle.checkpoint(0,total=len(logs),message="正在准备闲时反思")
        combined="\n".join(f"{row['role']}: {row['content']}" for row in logs)
        while len(combined)>24000 and len(logs)>1:
            logs=logs[:-1]
            combined="\n".join(f"{row['role']}: {row['content']}" for row in logs)
        if len(combined)>24000:
            raise ValueError("单条日志过长，反思水位未推进")
        reflection=await retriever.summarizer.reflect_on_logs(combined,session_id=session_id)
        if not self._is_valid_reflection(reflection):
            raise ValueError("反思结果字段不完整，水位未推进")
        summaries=[text.strip()[:200] for text in reflection['trivial_summaries'] if text.strip()]
        if len(summaries)>50:
            raise ValueError("反思条目过多，未提交")
        await handle.checkpoint(message="正在准备反思记忆向量")
        notes=[]
        if summaries:
            vectors=await retriever.embedding.embed(summaries,session_id=session_id)
            if not vectors or len(vectors)!=len(summaries) or any(not vector for vector in vectors):
                raise ValueError("反思向量化失败，水位未推进")
            notes=list(zip(summaries,vectors))
        await handle.checkpoint(message="正在原子提交反思与水位")
        result=await store.commit_reflection_batch(session_id,progress,max(row['id'] for row in logs),reflection,notes)
        result['processed']=len(logs)
        handle.record.progress=len(logs)
        handle.record.total=len(logs)
        handle.record.result=result
        return result

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
                rows = await store._exec_fetchall("""SELECT l.session_id,MAX(l.timestamp),COUNT(*) FROM chat_logs l
                    LEFT JOIN reflection_watermarks w ON w.session_id=l.session_id
                    WHERE l.id>COALESCE(w.last_log_id,0) GROUP BY l.session_id""")
                
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
                        turn=self.scope_resolver.from_session_key(session_id,self.state_manager.known_origins())
                        async def reflect(handle, frozen=turn, sid=session_id):
                            with self._usage.bind({**frozen.to_dict(),"source_umo":frozen.origin_umo,
                                                   "turn_kind":"background","_turn_context":frozen}):
                                return await self._run_idle_reflection(sid,handle)
                        record,reused=self._start_task("idle_reflection",session_id,reflect)
                        try:
                            await asyncio.shield(record.task)
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                        except Exception as exc:
                            logger.warning("[Quill Reflection] 闲时反思失败（%s），保留水位与日志",type(exc).__name__)

                        # 控制速率，防止 API 频率过高。
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
        await self._usage.initialize()
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
                self._wr_storage_status = {}
                stats = await self.wr_manager.get_stats()
                logger.info(
                    f"[Quill] 写作素材库已加载: {stats['total_entries']} 条 "
                    f"(启用 {stats['enabled_entries']} 条)"
                )
                logger.info(f"[Quill] 最大注入: {self.props.wr_max_entries} 条")
            except Exception as e:
                self._wr_storage_status = {"error":type(e).__name__,"available":False}
                if self.wr_manager is not None:
                    try:
                        await self.wr_manager.close()
                    except Exception as close_error:
                        logger.error("[Quill] 素材库失败连接关闭异常: %s", type(close_error).__name__)
                self.wr_manager = None
                logger.error(f"[Quill] 写作素材库初始化失败: {e}")
        else:
            logger.info("[Quill] 写作素材库已禁用")

        # --- RAG 初始化 ---
        await self._init_rag()
        from .interfaces.operations import recover_operations
        try:
            await recover_operations(self)
        except Exception as exc:
            logger.warning("[Quill Operation] 存在待恢复操作（%s），相关会话写入前会再次恢复",type(exc).__name__)

        # --- Web routes (AstrBot v4.26+ register_web_api 模式) ---
        rag_components = {
            'embedding': self.rag_embedding,
            'vector_store': self.rag_vector_store,
            'reranker': self.rag_reranker,
            'memory_store': self.rag_memory_store,
            'summarizer': self.rag_summarizer,
            'kb_source': self.rag_kb_source,
            'doc_groups': self.rag_doc_groups,
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

        # 能力源码核对在启动期预热，首次聊天不承担同步 inspect/AST 开销。
        from .interfaces.prompt_cache import _host_profile
        await asyncio.to_thread(_host_profile)

        # 启动时清理过期对话日志
        if self.rag_retriever and self.rag_retriever.memory_store:
            retention_days = getattr(self.config, 'rag_chat_log_retention_days', 30)
            try:
                cleaned = await self.rag_retriever.memory_store.cleanup_chat_logs(
                    retention_days, summary_enabled=self.config.rag_enable_memory,
                    reflection_enabled=getattr(self.config, 'rag_enable_autonomous_reflection', True))
                if cleaned:
                    logger.info(f"[Quill ChatLog] 清理了 {cleaned} 条过期日志（保留 {retention_days} 天）")
            except StorageError as e:
                # M3.2 D4：底层失败上抛后由这里降级——启动继续（与改前吞错误
                # 时「启动照常、清理没跑」的行为一致），日志带方法名与异常链。
                logger.warning("[Quill] 启动清理过期对话日志失败: %s", e, exc_info=True)

        # 启动时修剪过期低价值记忆
        if self.rag_retriever and self.rag_retriever.memory_store:
            try:
                pruned = await self.rag_retriever.memory_store.prune_memories()
                if pruned:
                    logger.info(f"[Quill Memory] 启动修剪: 清理了 {pruned} 条低价值记忆")
            except StorageError as e:
                # 同上：修剪失败不阻断插件启动（M3.2 D4 调用方降级点）。
                logger.warning("[Quill] 启动修剪低价值记忆失败: %s", e, exc_info=True)

        # 启动 state 自动落盘（分级落盘：关键字段即时，高频字段 5s 批量刷洗）
        self.state_manager.start_autoflush()

        # P2-1 修复：启动闲时反思守护进程（此前 _reflection_loop 为死代码，从未被调用）
        if getattr(self.config, 'rag_enable_autonomous_reflection', True) \
                and self.rag_retriever and self.rag_retriever.memory_store:
            self._spawn(self._reflection_loop())
            logger.info("[Quill Reflection] 闲时反思守护进程已启动")

        # H1：复位进程级 send_message_to_user 单例描述并快照框架原文。
        # 必须在这里（而非首次改写时）做：热重载会复用同一个 singletons，
        # 旧版本写下的 Quill 版描述若不主动复位，新代码会继承旧污染。
        self._reset_smt_tool_singleton()

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
                usage_recorder=self._usage,
            )

            # Doc RAG 向量库（FAISS + SQLite）
            rag_db = os.path.join(self.paths["knowledge_dir"], "quill_rag.db")
            rag_idx = os.path.join(self.paths["knowledge_dir"], "quill_rag.index")
            # S2-10: 传入 embedding_provider，切换 provider 时自动重建索引
            self._rag_storage_status = {}
            self.rag_vector_store = None
            vector_store = None
            try:
                vector_store = FaissVectorStore(rag_db, rag_idx, embedding_provider=self.rag_embedding)
                await vector_store.initialize()
                self.rag_vector_store = vector_store
            except Exception as exc:
                self._rag_storage_status['documents'] = getattr(vector_store, 'schema_status', {}) or {'available': False, 'error': type(exc).__name__}
                logger.error("[Quill RAG] 文档库不可用，其他检索组件继续初始化: %s", type(exc).__name__)

            # 插件文档分组（子知识库）元数据：所有插件文档共用同一套
            # embedding/FAISS 索引，分组只是 source → 组名 的映射，不进 chunks 表。
            # 独立 try：分组存储出问题不该连累整条 RAG 栈。
            self.rag_doc_groups = None
            try:
                from .quill_rag.doc_groups import DocGroupStore
                self.rag_doc_groups = DocGroupStore(
                    os.path.join(self.paths["state_dir"], "rag_doc_groups.json")
                )
                await self.rag_doc_groups.load()
            except Exception as e:
                logger.warning(f"[Quill RAG] 文档分组存储初始化失败（分组功能不可用）: {e}")

            # Reranker
            self.rag_reranker = QuillReranker(
                self.context,
                rerank_provider_id=self.config.rag_rerank_provider_id,
                fallback_llm_id=self.config.rag_llm_provider_id,
                usage_recorder=self._usage,
            )

            # 动态记忆存储（SQLite BLOB）
            mem_db = os.path.join(self.paths["knowledge_dir"], "quill_memory.db")
            self.rag_memory_store = None
            memory_store = None
            try:
                memory_store = MemoryStore(mem_db)
                await memory_store.initialize()
                self.rag_memory_store = memory_store
            except Exception as exc:
                self._rag_storage_status['memory'] = getattr(memory_store,'schema_status',{}) or {'available':False,'error':type(exc).__name__}
                logger.error("[Quill RAG] 记忆库不可用，文档检索继续初始化: %s", type(exc).__name__)
            if self.rag_memory_store is not None:
                try:
                    await self.shared_scopes.initialize(self.rag_memory_store)
                except Exception as exc:
                    logger.error("[Quill Groups] 共享服务初始化失败: %s", type(exc).__name__)

            # LLM 摘要器
            self.rag_summarizer = QuillSummarizer(
                self.context,
                provider_id=self.config.rag_llm_provider_id,
                usage_recorder=self._usage,
            )

            # AstrBot 内置知识库源（kb: 前缀可绑定源）。独立 try：本地栈
            # 任何一步失败都不能连累 KB 路——KB 检索走 AstrBot 自己的
            # embedding，本地 embedding 未配置时它仍可独立工作。
            self.rag_kb_source = None
            try:
                from .quill_rag.astrbot_kb import AstrBotKBSource
                self.rag_kb_source = AstrBotKBSource(
                    self.context,
                    enabled=self.config.rag_astrbot_kb_enabled,
                    top_k=self.config.rag_top_k,
                )
                if self.rag_kb_source.available:
                    logger.info("[Quill RAG] AstrBot 内置知识库源已启用")
            except Exception as e:
                logger.warning(f"[Quill RAG] AstrBot 内置知识库源初始化失败（KB 检索不可用）: {e}")

            # 统一检索器
            self.rag_retriever = QuillRetriever(
                embedding_provider=self.rag_embedding,
                vector_store=self.rag_vector_store,
                reranker=self.rag_reranker,
                memory_store=self.rag_memory_store,
                summarizer=self.rag_summarizer,
                top_k=self.config.rag_top_k,
                enable_memory=self.config.rag_enable_memory and self.rag_memory_store is not None,
                config=self.config,
                kb_source=self.rag_kb_source,
            )

            logger.info("[Quill RAG] 组件初始化完成 | embedding=%s | memory=%s | 内置KB=%s",
                        self.config.rag_embedding_provider_id or "local",
                        "on" if self.config.rag_enable_memory else "off",
                        "on" if (self.rag_kb_source and self.rag_kb_source.available) else "off")
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
        registry=getattr(self,"task_registry",None)
        if registry:
            for record in list(registry.records.values()):
                if record.kind in ("memory_summary","idle_reflection","memory_import") and record.finished_at is None:
                    registry.cancel(record.task_id)
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
        # KB 源无自建句柄（检索全程用 AstrBot 侧的 kb_manager），无需 close
        self.rag_kb_source = None
        self.rag_doc_groups = None

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
            'kb_source': self.rag_kb_source,
            'doc_groups': self.rag_doc_groups,
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

        self._rag_reinit_task = self._spawn(_run(),kind="rag_reinitialize",key="global")
        # 也纳入插件自己的任务集合，terminate 时能被统一取消/等待。
        # L3：与 _spawn 一样必须挂 done 回调把任务从集合里摘掉，否则每次
        # embedding provider 变更都会在 _bg_tasks 留下一个已完成的 Task 对象
        # （长期运行无界增长，且 terminate 会去 await 一堆死任务）。
        self._bg_tasks.add(self._rag_reinit_task)

        def _on_reinit_done(task: asyncio.Task):
            self._bg_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.warning(f"[Quill] RAG 重建任务异常退出: {task.exception()}")

        self._rag_reinit_task.add_done_callback(_on_reinit_done)

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
            self.shared_scopes.bind_state()
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
            # B4-fix：等待要带超时，并记下非取消类异常。旧写法
            # `except (asyncio.CancelledError, Exception): pass` 既吞掉了自身被取消的
            # 信号，也让卡死/自屏蔽取消的任务把 terminate 永久挂住。
            pending = [t for t in list(self._bg_tasks)]
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning("[Quill] 退出时仍有 %d 个后台任务未结束（已超时放弃等待）", len(pending))
            else:
                for task, res in zip(pending, results):
                    if isinstance(res, BaseException) and not isinstance(
                        res, asyncio.CancelledError
                    ):
                        logger.warning(
                            f"[Quill] 后台任务 {task.get_coro()} 异常退出: {res}"
                        )
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
        # B4 修复（两处）：
        # 1) 关连接前先排空 retriever 自己的在途任务（_close_rag_components 做了，
        #    terminate 之前漏了）——否则在途的 mark_memories_used 会往刚关掉的
        #    aiosqlite 连接里写，静默丢一轮统计。
        # 2) 关闭必须覆盖**顶层引用**，不能只走 rag_retriever。_init_rag 是
        #    「先 vector_store.initialize()，后 MemoryStore.initialize()」且整段
        #    只有一个 except 记日志；中途失败时 rag_retriever 仍为 None，而
        #    vector_store 已经打开了 quill_rag.db/.index —— 旧写法下这些句柄
        #    再无人关闭，Windows 上会锁住 DB 文件，插件更新/重载直接失败。
        #    （与 _close_rag_components 的收尾口径保持一致。）
        if self.rag_retriever:
            await self._drain_retriever_tasks()
        for comp, name in (
            (self.rag_memory_store, "memory_store"),
            (self.rag_vector_store, "vector_store"),
        ):
            if comp is None:
                continue
            try:
                await comp.close()
            except Exception as e:
                logger.warning(f"[Quill] RAG {name} 关闭失败: {e}")
        usage = getattr(self, "_usage", None)
        if usage is not None:
            try:
                await usage.close()
            except Exception as exc:
                logger.warning("[Quill Usage] 关闭台账失败（%s）", type(exc).__name__)
        logger.info("[Quill] 插件已停用")

    def _track_task(self, task):
        if task in self._bg_tasks:
            return task
        self._bg_tasks.add(task)
        def done(completed):
            self._bg_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.warning("[Quill Task] 后台任务失败（%s）", type(completed.exception()).__name__)
        task.add_done_callback(done)
        return task

    def _start_task(self, kind, key, factory, *, total=None):
        record, reused = self.task_registry.submit(kind,key,factory,total=total)
        self._track_task(record.task)
        return record, reused

    def _start_summary_task(self, session_id, *, legacy_last_id=0):
        from .quill.services.scope import current_turn
        current = current_turn()
        turn = current if current and current.memory_session_id == session_id else self.scope_resolver.from_session_key(
            session_id,self.state_manager.known_origins())
        async def summarize(handle):
            identity = {**turn.to_dict(), "source_umo":turn.origin_umo,
                        "turn_kind":"user" if current else "background", "_turn_context":turn}
            with self._usage.bind(identity):
                result = await self.rag_retriever.summarize_incremental(
                    session_id,legacy_last_id=legacy_last_id,handle=handle)
            handle.record.result = {k:v for k,v in result.items() if k != "summary"}
            return result
        return self._start_task("memory_summary",session_id,summarize)

    def _spawn(self, coro, *, kind=None, key=None):
        if kind is None:
            # 短日志写入沿用原跟踪集合，不挤占长任务的完成记录保留空间。
            try:
                return self._track_task(asyncio.create_task(coro))
            except BaseException:
                if hasattr(coro,"close"):
                    coro.close()
                raise
        # 无业务 key 的短任务不能拒重，否则两条日志可能被误当成同一任务。
        import uuid
        task_kind = kind or getattr(getattr(coro,"cr_code",None),"co_name","background")
        started = False
        async def work(handle):
            nonlocal started
            started = True
            return await coro
        try:
            record, reused = self._start_task(task_kind, key if key is not None else uuid.uuid4().hex, work)
        except BaseException:
            if hasattr(coro,"close"):
                coro.close()
            raise
        if reused and hasattr(coro,"close"):
            coro.close()
        elif hasattr(coro,"close"):
            def close_unstarted(completed):
                if not started:
                    coro.close()
            record.task.add_done_callback(close_unstarted)
        return record.task

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
                if (group, key) == ("usage", "prices_json"):
                    from .quill.services.usage import validate_prices
                    price_error = validate_prices(item.get("value"))
                    if price_error:
                        return False, price_error
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

                # KB 源的 enabled/top_k 也是构造期快照，随面板保存实时同步
                #（available 是属性，读 self.context.kb_manager + enabled，
                # 所以关掉开关下一轮检索立即失效，无需重建）。
                kb_src = getattr(self, "rag_kb_source", None)
                if kb_src is not None:
                    kb_src.enabled = self.config.rag_astrbot_kb_enabled
                    kb_src.top_k = self.config.rag_top_k

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

    @filter.custom_filter(_AlreadyAwakeFilter,priority=1000)
    async def shared_scope_capability_guard(self,event:AstrMessageEvent):
        await self.shared_scopes.observe(event)
        if event.get_extra('_quill_origin_error'):
            return
        text=(event.message_str or '').strip()
        first=text.split(maxsplit=1)[0].lstrip('/') if text else ''
        if text.startswith('/') or first in ('quill','char','wb','doc','memory','stream','reinject'):
            return
        from .interfaces.scopes import physical_origin
        origin=physical_origin(event)
        scope=self.scope_resolver.scope_for(origin.umo)
        if scope!=origin.umo:
            turn=self.scope_resolver.resolve(origin,await self.state_manager.get_selected_persona_id(origin.umo))
            await self.shared_scopes.waiting_supported(event,turn)
        try:
            await _quill_hooks.prepare_output_flags(self,event)
        except Exception as exc:
            logger.warning('[Quill] 提前设置输出模式失败（%s），保留宿主模式',type(exc).__name__)

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """切换角色卡专属对话；输出旗标已在 AdapterMessage 阶段准备。

        注册桩（M2.2 第二轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_waiting_llm_request``（完整
        设计理由见彼处 docstring；验证走 E2E
        .build/harness/quilltest/run.py --tier2）。行为契约：

        * 先切角色卡专属对话（必须早于框架 ``_get_session_conv()``，
          下一事件才切换就要晚一轮生效），实现见
          quill/services/character.py；**F2（M3.0，有意行为变更）**：
          新建独立对话 / 切换到不同对话成功后重置 ``quill_rounds``，
          消除换卡后首轮跳 Layer 1 常驻（BASELINE §8.2 F2；同卡快路径
          与首次接管当前对话不重置，重置失败只 warning）；
        * 拦截字面 ``/reinject`` / ``/重新注入``，按 target_id 重置轮次并回执；
        * 输出旗标由前置 AdapterMessage 钩子准备，本钩子不再声称能修改本轮局部流式值。

        角色对话隔离自带异常降级；取事件身份失败则退出，轮次重置异常
        交由宿主记录。输出旗标不在这个阶段重新决策。
        """
        try:
            await self.shared_scopes.observe(event)
            if event.get_extra('_quill_origin_error'):
                return
            await self.shared_scopes.acquire(event)
        except Exception as exc:
            logger.warning('[Quill Scope] 来源或会话组不可用（%s）',type(exc).__name__)
            event.set_result(event.plain_result('会话组来源暂不可用，本轮已停止。请查看存储状态。'))
            event.stop_event()
            return
        try:
            turn = await freeze_turn(self, event)
        except StorageError:
            return  # freeze_turn 已给出恢复受阻的原因并停止本轮。
        if not await self.shared_scopes.waiting_supported(event,turn):
            return
        with bind_turn(turn):
            await _quill_hooks.handle_waiting_llm_request(self, event)

    # ── 状态栏解析共享方法 ──────────────────────────────────────

    # M2.2 剥离器下沉：实现（剥离正则常量 + 纯函数）已原位搬至
    # quill/services/statusbar/strip.py，正则/文本处理一字未改，仅由
    # 类属性/classmethod 转为模块级常量/函数（原实现本就零 self 依赖）。
    # 此处保留同名类属性/类方法薄转发（M2.0 搬移期约定）：
    #   - probe 脚本与外部旧脚本经
    #     QuillPlugin.<name> 的旧访问面不变（原 tests/legacy 已归档）；
    #   - 钩子/服务侧经 self._strip_* 的动态分发路径与搬移前一致。
    # 下面的类属性与 strip 模块常量是**同一对象**的别名：任何一侧改表都作用
    # 于同一份状态，与搬移前单一代码路径等价。
    # D4：`_strip_field_re_cache` / `_STRIP_LOVE_DATA_RE` / `_STRIP_LEGACY_STATUS_RE`
    # 三个别名零引用（私有缓存由 strip 模块内部持有，标签正则只在模块内用），已删。
    # 实现与完整设计理由（两档强度为何分设、为何不擦裸字段行）见 strip.py。

    # 聚合所有状态栏变体的剥离正则（disabled 模式 + dedup 清理用）
    _STRIP_PATTERNS = _strip_mod._STRIP_PATTERNS

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

        M1a 同源锚点：合并后的 stats **同时**镜像进 trace store 的进行中
        trace（`inject_stats` 字段），`_get_inject_report` 优先从 trace 取数
        ——/quill debug、回复末尾报告行、面板 trace 三处同一份数据。
        trace 关闭（trace.enabled=false）或无进行中 trace 时回落旧 dict，
        行为与 M1a 之前逐字一致。
        """
        cache = getattr(self, "_inject_reports", None)
        if cache is None:
            cache = {}
            self._inject_reports = cache
        merged = {"wb": 0, "mem": 0, "wr": 0, "doc": 0, "core_mem": 0}
        # 核验报告 P0（§4.1/§9.4）：stats 里的下划线键（_funnel_*、
        # _sections_*、_section_texts…）是 prompt_builder 的内部诊断产物，
        # 其中 _section_texts 携带各段**完整正文**。merged 会镜像进进行中
        # trace 的 inject_stats（见下），经 /trace/recent、/quill debug、
        # 面板三路对外可见——不滤掉就是正文泄露面。下游消费不受影响：
        # hooks 步 16/18 读的是 prompt_builder 的**本地** stats dict
        # （inject_stats.get("_funnel_wb")），不经此镜像；读侧合成键
        # "_unactivated" 在 _get_inject_report 里现造，也不依赖这里透传。
        merged.update({
            k: v for k, v in (stats or {}).items()
            if not str(k).startswith("_")
        })
        # 重新赋值以更新插入顺序，使淘汰按「最近使用」而非「最早创建」
        cache.pop(target_id, None)
        cache[target_id] = merged
        while len(cache) > self._INJECT_REPORT_MAX:
            cache.pop(next(iter(cache)), None)
        # 镜像进进行中 trace（采集故障隔离：任何异常只记 warning，R1）
        try:
            store = getattr(self, "_trace_store", None)
            trace = store.inflight(_trace_mod.hash_target_id(target_id)) \
                if store is not None else None
            if trace is not None:
                trace.inject_stats = dict(merged)
        except Exception as e:  # noqa: BLE001 - R1 故障隔离：采集不影响主链路
            logger.warning(f"[Quill Trace] inject_stats 镜像失败（已忽略）: {e}")

    def _get_inject_report(self, target_id: str) -> dict:
        """读取某会话最近一轮注入统计（trace 同源优先，旧 dict 兜底）。

        M1a-Fix 1.1：取数改走 ``latest_for_report``——inflight 已有 stats
        （H2 实时报告行）时取本轮而非 queue[-1] 上一轮；返回的 trace 未激活
        时给 ``{"_unactivated": True}`` 标记（渲染为「本轮未激活」）而不是
        回退上一轮的陈旧缓存——注入行与 Trace 行永远同一条 trace。
        """
        try:
            store = getattr(self, "_trace_store", None)
            if store is not None and getattr(self.props, "trace_enabled", True):
                trace = store.latest_for_report(
                    _trace_mod.hash_target_id(target_id)
                )
                if trace is not None:
                    if not trace.activated:
                        return {"_unactivated": True}
                    if trace.inject_stats:
                        return trace.inject_stats
        except Exception as e:  # noqa: BLE001 - R1 故障隔离
            logger.warning(f"[Quill Trace] 读注入统计失败（回落旧缓存）: {e}")
        return (getattr(self, "_inject_reports", None) or {}).get(target_id) or {}

    def _get_last_trace_id(self, target_id: str) -> str:
        """最近一轮 trace 的 id（无记录/trace 关闭 → 空串）。

        /quill debug 用它标注注入构成的来源轮次——面板「诊断」页展示同一
        trace_id 的完整结构（段序/模块行/耗时），三处同源即由此锚定。
        M1a-Fix 1.1：与 ``_get_inject_report`` 同用 ``latest_for_report``，
        两行永远指向同一条 trace（未激活轮后不再跨轮错位）。
        """
        try:
            store = getattr(self, "_trace_store", None)
            if store is not None and getattr(self.props, "trace_enabled", True):
                trace = store.latest_for_report(
                    _trace_mod.hash_target_id(target_id)
                )
                if trace is not None:
                    return trace.trace_id
        except Exception as e:  # noqa: BLE001 - R1 故障隔离
            logger.warning(f"[Quill Trace] 读 trace_id 失败: {e}")
        return ""

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

    # 注入报告行正则的类属性别名（M2.0 re-export 约定；模块级定义见本文件
    # 顶部）。唯一消费者：M3.4 增量清洗游标的世代指纹
    # （quill/services/history_scrub.py:_inputs_fingerprint）——指纹须覆盖
    # 清洗函数的全部外部输入（报告行格式），格式一变指纹即变 → 游标整体
    # 失效回退全量重洗。钩子/服务侧的清洗调用路径
    # （plugin._scrub_inject_report）不经此属性，纯属指纹读取面。
    _INJECT_REPORT_LINE_RE = _INJECT_REPORT_LINE_RE

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
        if stats.get("_unactivated"):
            # M1a-Fix 1.1：最近一轮未激活（步 11 早退）——显式说明而不是
            # 回退渲染上一轮的陈旧构成，否则用户会拿旧数据调参。
            return "〔注入〕本轮未激活"
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
        capability = stats.get('_temporary_capability')
        if capability:
            parts.append('临时通道：' + ('可用' if capability.get('supported') else capability.get('reason','未知')))
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
                self._usage.generate(
                    provider_id=provider_id, purpose="status_extract", timeout=3.0,
                    prompt=prompt, contexts=[], image_urls=[],
                    system_prompt="你是 JSON 提取助手，仅输出 JSON。"
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

    # ── FunctionTool 描述泄漏防护（H1）─────────────────────────
    # 旧实现有两个致命缺陷，二者叠加导致「还原」是永不可达的死代码、
    # 而「改写」每轮都重复执行（线上 backend.log 里同一行出现 366 次）：
    #   1. marker 写的是一句**从未被写入过**的文案（"MUST use this tool to
    #      send ALL reply text"），既不在框架原文里、也不在 Quill 改写版里，
    #      于是 "marker in description" 恒为 False —— 守卫恒真，还原永不触发；
    #   2. 原件存在**每请求新建**的 req 上，而被改写的是
    #      FunctionToolManager 里注册的**进程级缓存单例**
    #      （func_tool_manager.get_builtin_tool 命中 builtin_func_list 直接
    #      返回同一对象，且 ToolSet 只持引用不复制），所以改写会跨请求、
    #      跨会话、跨并发永久沉淀；等下一个请求拿到同一个单例时，
    #      req 上那份「原件」早已随上一个 req 一起被丢弃。
    # 新契约：**永不修改共享单例**。改写走 copy.copy 出来的请求级副本，
    # 只把副本塞回本请求的 ToolSet（ToolSet.add_tool 同名覆盖）。
    # 这里仍保留一个复位函数，用于兜住两种残留：热重载前遗留的脏单例，
    # 以及 func_tool 与别的请求共享 ToolSet 时被塞进来的旧改写副本。
    _QUILL_SMT_DESC_MARKER = "THIS IS THE ONLY TOOL for sending replies"
    _QUILL_SMT_TOOL_NAME = "send_message_to_user"

    def _pristine_smt_description(self) -> str | None:
        """取框架原文描述：优先用 initialize() 快照，退回复位后的单例。

        返回 None 表示两条路都拿不到（框架改版/单例尚未注册），
        调用方按「宁可不还原也不要写坏」处理。
        """
        snap = getattr(self, "_smt_orig_desc", None)
        if isinstance(snap, str) and snap:
            return snap
        return None

    def _reset_smt_tool_singleton(self) -> None:
        """把进程级 SMT 单例复位成框架原文，并快照原文备用。

        为什么在 initialize() 做：旧版本的改写直接落在共享单例上，
        插件热重载（面板「重载插件」/开发期自动重载）时单例仍是脏的，
        不主动复位就会让新代码继承旧污染。原文从 dataclass 字段默认值取回
        （message_tools.SendMessageToUserTool 的 description 默认值即框架原文），
        这样无需新增 astrbot.core.* 导入（合规 C1）。
        """
        try:
            ctx = getattr(self, "context", None)
            if ctx is None:
                return
            mgr = ctx.get_llm_tool_manager()
            tool = mgr.get_builtin_tool(self._QUILL_SMT_TOOL_NAME)
        except Exception as e:
            # 组件初始化失败不阻断插件加载（AGENT.md 运行期约束）
            logger.warning("[Quill] 获取 send_message_to_user 单例失败: %s", e)
            return
        try:
            desc = getattr(tool, "description", "") or ""
            if self._QUILL_SMT_DESC_MARKER in desc:
                field = getattr(type(tool), "__dataclass_fields__", {}).get("description")
                default = getattr(field, "default", None)
                if not (isinstance(default, str) and default):
                    logger.warning(
                        "[Quill] send_message_to_user 描述已被写成 Quill 版，"
                        "但取不回框架原文，暂不复位（H1）"
                    )
                    return
                tool.description = default
                desc = default
                logger.info("[Quill] 已复位 send_message_to_user 工具描述（H1，进程级单例）")
            self._smt_orig_desc = desc
        except Exception as e:
            logger.warning("[Quill] 复位 send_message_to_user 描述失败: %s", e)

    def _restore_smt_tool(self, req: ProviderRequest) -> None:
        """若本请求工具集里的 send_message_to_user 是 Quill 改写版，换回原文副本。

        只替换 req.func_tool 里的那一个引用，绝不就地改共享单例的属性
        （就地改正是 H1 的成因）。取不到原文时保持原样：宁可描述被改写，
        也不能把描述写成空串让模型失去唯一的发送出口。
        """
        if not req or not req.func_tool or req.func_tool.empty():
            return
        tool = req.func_tool.get_tool(self._QUILL_SMT_TOOL_NAME)
        if tool is None:
            return
        if self._QUILL_SMT_DESC_MARKER not in (getattr(tool, "description", "") or ""):
            return
        orig = self._pristine_smt_description()
        if orig is None:
            return
        try:
            fresh = copy.copy(tool)
            fresh.description = orig
            req.func_tool.add_tool(fresh)
        except Exception as e:
            logger.warning("[Quill] 还原 send_message_to_user 描述失败: %s", e)

    def _get_target_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "unified_msg_origin") and event.unified_msg_origin:
            return str(event.unified_msg_origin)
        return str(event.get_sender_id())

    def _get_memory_session_id(self, target_id: str, persona_id: str) -> str:
        from .quill.services.scope import current_turn, memory_session_key
        turn=current_turn()
        scope=turn.scope_id if turn and target_id in (turn.origin_umo,turn.scope_id) else self.scope_resolver.scope_for(target_id)
        return memory_session_key(scope,persona_id)

    # ── 角色卡 → 对话隔离 ────────────────────────────────────────

    async def _ensure_persona_conversation(self, event: AstrMessageEvent) -> None:
        """把当前角色卡切到它自己的 AstrBot 对话上，实现对话历史隔离。

        M2.2 第二轮下沉薄转发：实现已整体搬至
        quill/services/character.py（``ensure_persona_conversation``，
        完整设计理由——为什么必须挂在 on_waiting_llm_request、快路径、
        死对话重建、首次接管、全量 try 放行——见彼处 docstring；M3.0 F2
        的 quill_rounds 重置挂点同样住彼处，语义与容错风格见
        ``_reset_quill_rounds_safe``）。

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
        下沉决策见彼处 docstring；验证走 E2E
        .build/harness/quilltest/run.py --tier2）。行为契约：

        * 三重闸门原样放行（不修改 tool_args）：非 send_message_to_user
          工具 → 未激活（``_quill_activated``）→ 空 tool_args；
        * telegram/tg 平台剥离 plain 段 Markdown（下沉
          quill/services/response.py；未知平台不剥离）；
        * 状态栏（全平台）：开启时首条 plain 走六级链提取渲染
          （handled 后 set ``_quill_status_handled``，后续只清残留），关闭时
          整套 ``_strip_status_artifacts``；注入报告追加到最后一条 plain；
        * messages 为 JSON 字符串时解析-修改-回写（解析失败原样放行）；
        * F4 同回合循环拦截：重复正文/状态栏、发送预算命中后设置拒绝标记；
          请求级工具包装器返回 None 结束本轮，不伪造空参数错误。
          只有实际发送成功才登记正文和次数，初次失败仍可重试。


        顶层降级留在桩内（与原 H2 同层，S2-3 语义）：任何异常吞掉 +
        error 日志放行——工具参数钩子抛出会打断 agent loop，降级语义 =
        不修改 tool_args。
        """
        try:
            if getattr(tool,'name','')==self._QUILL_SMT_TOOL_NAME and not event.get_extra("_quill_smt_terminated") and not await self.shared_scopes.response_current(event):
                if isinstance(tool_args,dict):
                    tool_args['messages']='[]' if isinstance(tool_args.get('messages'),str) else []
                event.stop_event()
                return
            with self._usage.bind(event.get_extra("_quill_usage_identity") or {}):
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
        """检查激活状态（激活词 / 括号 / WR关键词）。返回 (activated, wr_activated)。

        D4b（M3.2，本版本唯一有意行为变更的落点）：**激活判定 fail-close**。
        检测过程异常时返回 (False, False)（不注入），绝不允许异常把判定顶成
        True（全量注入）。理由（PLAN §M3.2）：fail-open 的故障形态是「每轮
        注入全部设定」——最贵行为、用户可感为刷屏注入报告；fail-close 的故障
        形态是「该注入时没注入」——下一轮检测恢复即自愈，用户可感为偶发设定
        丢失。后者更安全。

        与重构参考版 ``interfaces/astrbot/plugin.py`` `_should_activate` 的
        ``if detector is None: return True`` / ``except Exception: return True``
        相反——移植该层时（M3.3+）不得把 fail-open 带回来。

        正常判定逻辑（``should_activate`` / ``check_brackets`` / WR match）
        一行未动：异常路径之外的行为与 v5.2.5 逐字相同（由
        .build/harness/quilltest/run.py --tier2 的真实注入看守）。
        """
        try:
            activated = self.activation_detector.should_activate(user_input)
            has_bracket = self.activation_detector.check_brackets(user_input)
        except Exception as e:
            # 检测器异常（正则/输入形态等理论不可达路径）→ fail-close：
            # 本轮不注入，warning 带异常链，等待下一轮自然恢复。
            logger.warning(
                "[Quill] 激活检测异常，本轮按未激活处理（fail-close）: %s", e, exc_info=True
            )
            return False, False

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
                # WR 匹配失败（含底层 StorageError）→ wr_activated 保持 False：
                # 语义上同属激活判定的 fail-close（不因素材库故障全量注入）。
                logger.warning("[Quill] _check_activation WR 匹配失败: %s", e, exc_info=True)

        return activated, wr_activated

    def _trace_rag_modules(self, event: AstrMessageEvent, rows: list) -> None:
        """M1a trace：把 RAG 段模块行写进本会话 inflight trace（R1 故障隔离）。

        采集只是「就地读已有数据」：任何异常只 warning，不影响注入主链路；
        error/degraded 只记类别（local_error / kb_error / mem_error /
        not_initialized / rag_crash），不落原始异常文本与检索原文。
        """
        try:
            props = getattr(self, "props", None)
            store = getattr(self, "_trace_store", None)
            if props is None or store is None or not props.trace_enabled:
                return
            key = _trace_mod.hash_target_id(self._get_target_id(event))
            t = store.inflight(key)
            if t is not None:
                t.module_results.extend(rows)
        except Exception as e:
            logger.warning(f"[Quill Trace] RAG 段采集失败（已忽略）: {e}")

    async def _run_rag_retrieval(self, event: AstrMessageEvent, req: ProviderRequest, user_input: str, persona_data, dynamic_prompt: str, stats: dict | None = None) -> str:
        """执行 RAG 检索（Doc + Memory），返回更新后的 dynamic_prompt。

        `stats` 为可选出参：回填 doc/mem 的命中条数与文档来源名（注入报告用）。
        """
        # 文档注入有两条路：本地 FAISS Doc RAG 与 AstrBot 内置知识库（kb: 源）。
        # KB 路用 AstrBot 自己的 embedding，本地栈未初始化时仍可独立工作，
        # 所以两条路都不可用才跳过（此前只看本地栈）。
        # getattr 防御：快照测试以裸对象构造插件实例（不走 __init__），
        # 插件 __getattr__ 对未知属性会抛 AttributeError。
        kb_source = (getattr(self.rag_retriever, "kb_source", None) if self.rag_retriever else None) \
            or getattr(self, "rag_kb_source", None)
        kb_ready = bool(kb_source and kb_source.available)
        if not (self.rag_retriever and self.rag_retriever.embedding) and not kb_ready:
            logger.warning("[Quill RAG] 文档系统未初始化（本地 RAG 与内置知识库均不可用）")
            self._trace_rag_modules(event, [
                _trace_mod.TraceModuleResult(module="rag_doc", ok=True, degraded="not_initialized"),
                # M1a-Fix 1.4：早退时 memory/core_memory 未执行，补 skipped 行
                # ——面板模块表此前这两行缺席，与「执行了但零命中」无法区分。
                _trace_mod.TraceModuleResult(module="memory", ok=True, degraded="skipped"),
                _trace_mod.TraceModuleResult(module="core_memory", ok=True, degraded="skipped"),
            ])
            return dynamic_prompt

        try:
            target_id = self._get_target_id(event)
            persona_id = await self.state_manager.get_persona_id(target_id)
            mem_session_id = self._get_memory_session_id(target_id, persona_id)

            # D9：用 RAG_ERROR_ATTR 常量而不是再写一遍字面量 "_rag_error"——
            # 否则改了常量名这里会静默失配（读不到错误原因，只报 "ok"）。
            # P1-4：空结果算成功（确实没找到），只有真出错才算失败。
            from .quill_rag.retrieval import RAG_ERROR_ATTR, format_for_prompt, rag_ok as _rag_ok
            from .quill_rag.astrbot_kb import split_bound_ex
            from .quill_rag.doc_groups import is_group_source, parse_group_source

            doc_results: list = []   # 合并后的注入列表（本地在前，内置库在后）
            ext = persona_data.get("quill_extensions", {}) if persona_data else {}
            rag_mode = ext.get("rag_mode", "disabled")

            logger.info(f"[Quill RAG] 模式: {rag_mode}，绑定文档: {ext.get('bound_rag_docs', []) if rag_mode == 'custom' else 'Auto (全库)'}")

            # 合并 list 会丢掉 RagResult 子类的失败标记，按路分别取 ok/原因
            local_ok = kb_ok = True
            doc_errors: list[str] = []

            # M1b 模块 1：RAG 漏斗出参——本地路粗召回/重排近失由
            # search_documents 回填；内置库路（kb_source.search）无出参支持，
            # 其贡献只进 hits，candidates 口径以本地路为准（面板如实展示）。
            rag_funnel: dict = {}
            mem_funnel: dict = {}

            if rag_mode != "disabled":
                bound_rag_docs = ext.get("bound_rag_docs", []) if rag_mode == "custom" else None
                kb_doc_filters: dict = {}
                if rag_mode == "custom":
                    # kb: 前缀 = 内置库绑定（可细到 kb:库名/文档名），
                    # grp: 前缀 = 插件文档分组（展开成组内全部 source），
                    # 其余按插件文档 source 精确过滤
                    group_store = getattr(self, "rag_doc_groups", None)
                    plugin_sources, kb_names, kb_doc_filters = split_bound_ex(
                        bound_rag_docs,
                        group_resolver=(group_store.resolve if group_store else None),
                    )
                    doc_level = {k: v for k, v in kb_doc_filters.items() if v is not None}
                    if doc_level:
                        logger.info(f"[Quill RAG] 文档级绑定: {doc_level}")
                    group_bound = [parse_group_source(s) for s in (bound_rag_docs or [])
                                   if is_group_source(s)]
                    if group_bound:
                        logger.info(f"[Quill RAG] 分组绑定: {group_bound} → {len(plugin_sources)} 篇插件文档")
                else:
                    # auto：本地全库 + 全部非空内置库
                    plugin_sources, kb_names = None, None

                # 路 1：本地 FAISS Doc RAG
                if self.rag_retriever and self.rag_retriever.embedding:
                    local_results = await self.rag_retriever.search_documents(
                        user_input, allowed_sources=plugin_sources, funnel=rag_funnel
                    )
                    local_ok = _rag_ok(local_results)
                    if not local_ok:
                        doc_errors.append(f"本地: {getattr(local_results, RAG_ERROR_ATTR, '?')}")
                    doc_results.extend(local_results)
                    logger.info(f"[Quill RAG] 本地文档检索: {len(local_results)} 段")

                # 路 2：AstrBot 内置知识库（custom 模式仅绑定的 kb: 源，
                # 一个都没绑就不发空跑请求）
                if kb_ready and (rag_mode != "custom" or kb_names):
                    kb_results = await kb_source.search(
                        user_input,
                        kb_names=kb_names if rag_mode == "custom" else None,
                        doc_filters=kb_doc_filters if rag_mode == "custom" else None,
                    )
                    kb_ok = _rag_ok(kb_results)
                    if not kb_ok:
                        doc_errors.append(f"内置库: {getattr(kb_results, RAG_ERROR_ATTR, '?')}")
                    doc_results.extend(kb_results)
                    logger.info(f"[Quill RAG] 内置知识库检索: {len(kb_results)} 段")
            else:
                logger.info(f"[Quill RAG] 模式: disabled，跳过文档检索")

            mem_results = []
            core_mems = []
            if self.rag_retriever:
                mem_results = await self.rag_retriever.search_memories(
                    mem_session_id, user_input, funnel=mem_funnel
                )
                logger.info(f"[Quill RAG] 记忆检索: {len(mem_results)} 条 (Session: {mem_session_id})")

                # 核心记忆：无条件注入，不参与 Top-K 竞争
                core_mems = await self.rag_retriever.get_core_memories(mem_session_id)
                if core_mems:
                    logger.info(f"[Quill RAG] 核心记忆: {len(core_mems)} 条 (Session: {mem_session_id})")

            # format_for_prompt 优先走 retriever 实例方法（H3 快照钉住的调用
            # 契约：步 17 在 retriever 上发生）；模块级纯函数仅为 KB-only 模式
            # （本地栈未初始化、rag_retriever 为 None）兜底。
            if self.rag_retriever:
                rag_context = self.rag_retriever.format_for_prompt(doc_results, mem_results, core_mems)
            else:
                rag_context = format_for_prompt(doc_results, mem_results, core_mems)
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
            # P1-4: 记录 RAG 检索结果，按 rag_ok 判定（见上）。文档路现在有
            # 本地/内置库两段，失败标记在合并前分别取（合并 list 会丢标记）。
            doc_ok = local_ok and kb_ok
            mem_ok = _rag_ok(mem_results)
            if not doc_ok or not mem_ok:
                logger.warning(
                    "[Quill RAG] 检索降级: doc=%s mem=%s",
                    "; ".join(doc_errors) if doc_errors else "ok",
                    getattr(mem_results, RAG_ERROR_ATTR, "ok"),
                )
            self.health_tracker.record_rag(doc_ok and mem_ok)
            # M1a trace：RAG 段三行（doc/memory/core_memory）。doc_ok/mem_ok
            # 在上面刚判定完；items 只放文档名（source_ref，截 40 字符）与
            # 分值，绝不放检索命中的原文片段（红线：来源原文不进 trace）。
            rag_rows = [
                _trace_mod.TraceModuleResult(
                    module="rag_doc", ok=doc_ok,
                    degraded=("disabled" if rag_mode == "disabled" else None),
                    error=(
                        "+".join(e for e, bad in
                                 (("local_error", not local_ok), ("kb_error", not kb_ok))
                                 if bad) or None
                    ),
                    hits=len(doc_results),
                    candidates=int(rag_funnel.get("candidates", 0) or 0),
                    items=[
                        {
                            "source_ref": (str(r.get("source", "") or "").strip() or "unknown")[:40],
                            **({"score_or_kw": round(float(r["score"]), 3)}
                               if isinstance(r.get("score"), (int, float)) else {}),
                        }
                        for r in doc_results[:10]
                    ] + [
                        # M1b 模块 1 漏斗：重排/截帽挤掉的粗召回段（只记来源名）
                        {"source_ref": str(f.get("source_ref", "?"))[:40],
                         "filtered_reason": str(f.get("reason", ""))}
                        for f in (rag_funnel.get("filtered") or [])[:10]
                    ],
                ),
                _trace_mod.TraceModuleResult(
                    module="memory", ok=mem_ok,
                    error=None if mem_ok else "mem_error",
                    hits=len(mem_results),
                    candidates=int(mem_funnel.get("candidates", 0) or 0),
                    items=[
                        # 留存记忆只记 id 与综合分（rrf），不携带记忆原文（红线 7）
                        {"source_ref": f"mem#{r.get('id', '?')}",
                         **({"score_or_kw": f"rrf {float(r['rrf_score']):.4f}"}
                            if isinstance(r.get("rrf_score"), (int, float)) else {})}
                        for r in mem_results[:10]
                    ] + [
                        {"source_ref": f"mem#名次{f.get('rank', '?')}",
                         "filtered_reason": str(f.get("reason", ""))}
                        for f in (mem_funnel.get("filtered") or [])
                    ],
                ),
                _trace_mod.TraceModuleResult(
                    module="core_memory", hits=len(core_mems),
                ),
            ]
            self._trace_rag_modules(event, rag_rows)
        except Exception as e:
            logger.warning(f"[Quill RAG] 检索失败: {e}")
            # P1-4: 记录 RAG 检索失败
            self.health_tracker.record_rag(False)
            self._trace_rag_modules(event, [
                _trace_mod.TraceModuleResult(module="rag_doc", ok=False, error="rag_crash"),
                # M1a-Fix 1.4：同早退分支——崩溃后 memory/core_memory 状态未知，
                # 但本轮确定未注入，标 skipped 保持三行齐全。
                _trace_mod.TraceModuleResult(module="memory", ok=True, degraded="skipped"),
                _trace_mod.TraceModuleResult(module="core_memory", ok=True, degraded="skipped"),
            ])

        return dynamic_prompt

    async def _rewrite_smt_tool_description(self, req: ProviderRequest, persona_id: str = "") -> None:
        """改写本请求工具集里的 send_message_to_user 描述。状态栏指令通过 system prompt + tail message 注入。

        无角色卡时不重写：原始描述不会强制 "MUST call IMMEDIATELY"，
        避免 LLM 在无人设约束时进入 Agent 死循环（连续调用工具）。

        H1：改写**只落在 copy.copy 出来的请求级副本**上，原始单例一个字节都不动。
        这样每次请求都从框架原文出发重新改写（幂等，不再需要 marker 守卫），
        并发请求之间也互不干扰——旧实现直接改共享单例，晚到的请求会读到
        早先请求写下的文案，而它自己 req 上那份"原件"又是另一个对象的，
        还原逻辑永远够不着被改的那个。
        """
        if not (req.func_tool and not req.func_tool.empty()):
            return
        smt_tool = req.func_tool.get_tool(self._QUILL_SMT_TOOL_NAME)
        if smt_tool is None:
            return
        try:
            fresh = copy.copy(smt_tool)
            _quill_hooks.guard_smt_tool(self,fresh)
            if persona_id:
                fresh.description = (
                    "THIS IS THE ONLY TOOL for sending replies. Output text DIRECTLY in your response will be DISCARDED. "
                    "You MUST call this tool to send ANY reply text — do NOT output text in the content field. "
                    "Call this tool IMMEDIATELY as your first action — do not call any other tools before sending your message."
                    # F4（M3.0b）源头减压：单次调用契约 + 明示重发会被拒绝
                    " Send the COMPLETE reply — all story segments AND the status bar — in ONE single call. "
                    "NEVER call this tool more than once per reply; repeated calls are rejected."
                    " History may contain status blocks formatted by the renderer, not written by you. "
                    "Do not apologize for historical formatting or discuss the protocol. "
                    "Use the requested raw status markers only in this call; send story text, not analysis."
                )
            req.func_tool.add_tool(fresh)
        except Exception as e:
            # 改写失败不阻断本轮：模型仍能用框架原文调用工具
            logger.warning("[Quill] 改写 send_message_to_user 描述失败（H1）: %s", e)
            return
        # 每请求只该记一次：旧实现每轮无条件重写，日志里同一行刷了 366 次。
        # 这里用 req 上的标记做去重，同一 req 被多条 hook 路径处理时不重复刷。
        if not getattr(req, "_quill_smt_rewritten", False):
            try:
                setattr(req, "_quill_smt_rewritten", True)
            except Exception:
                pass
            logger.debug(f"[Quill] 已改写 send_message_to_user 描述 (persona={persona_id})")

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求拦截：触发平行宇宙隔离，执行状态栏降级解析与多维 Prompt 组装注入。

        注册桩（M2.2 第六轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_llm_request``（22 步注入编排
        本体，BASELINE §4——顺序即行为；完整设计理由、段序与下沉决策见
        彼处 docstring；验证走 E2E：.build/harness/quilltest/run.py --tier2）。
        核心职责：

        - 平行宇宙双轴隔离 (target_id::persona_id)：按群+角色切分独立状态
        - 激活检测：决定本次请求是否进入 RP 模式
        - Context Restoration：req.contexts 为空时从 chat_logs 捞取最近 N 条垫入
        - First Message 智能抑制：避免重启后突兀复读开场白
        - 4 层 Prompt 装配：系统/角色/世界书/WR/RAG 多源注入
        - 状态栏降级解析：5 级兜底（STATUS 块→LOVE_DATA→legacy→RAW→lenient）

        顶层降级留在桩内（与原 H3 同层，BASELINE §2 H3 行）：预初始化
        （P1-3，防 except 块引用未定义变量掩盖原始异常）+ 任何异常吞掉 +
        error 日志（「致命错误，Prompt 装配失败，降级放行」+
        ``_sanitize_extra`` 脱敏摘要——只记 persona_id/长度字段/
        skip_constants，不泄 user_input/context_text 原文）+ 放行。

        搬移注记（降级日志保真度）：委托后降级日志中的 extra_summary
        字段恒为预初始化值（原实现记录失败时刻的中间值）；
        异常消息与堆栈不受影响，行为语义（不注入、闸门不置位、放行）
        逐字保真——快照 test_h3_top_level_exception_degrades 钉住。

        行为契约（legacy t25 源码窗口断言引用的标记原文，实际置位发生在
        interfaces 实现内步 21，此处逐字保留以锁定闸门语义——置位点在
        22 步全部注入成功之后，全插件仅此一处）：
        ``event.set_extra("_quill_activated", True)``。
        """
        extra_info = {}
        try:
            # M2：同一请求重复经过插件时不重记日志、不开第二个摘要、不叠加提示。
            from .interfaces.request_guard import begin_request, finish_request
            prepared_record = begin_request(event, req)
            if prepared_record is None:
                return
            identity = {}
            await self.shared_scopes.acquire(event)
            turn = await freeze_turn(self, event)
            try:
                if not await self.shared_scopes.prepare(event,req,turn):
                    return
            except Exception as exc:
                if turn.scope_id!=turn.origin_umo:
                    logger.warning('[Quill Scope] 共享视图准备失败（%s），停止本轮',type(exc).__name__)
                    event.set_result(event.plain_result('共享上下文暂不可用，本轮未发送给模型。请稍后重试。'))
                    event.stop_event()
                    return
                raise
            try:
                identity = await self._usage.identity_for(event, turn.persona_id, new_request=True)
                await self._usage.note_provider(identity, req, event)
            except Exception as exc:
                logger.warning("[Quill Usage] 请求身份采集失败（%s），继续装配", type(exc).__name__)
            with self._usage.bind(identity), bind_turn(turn):
                await _quill_hooks.handle_llm_request(self, event, req)
                # 压缩只作用于已完成角色/共享恢复的历史；保留系统和本轮输入。
                try:
                    result = await self.context_compactor.apply(event, req, turn)
                    event.set_extra('_quill_compaction', result)
                except Exception as exc:
                    logger.warning('[Quill Context] 压缩失败（%s），保留当前历史', type(exc).__name__)
            if event.get_extra('_quill_shared_context'):
                # 包括开场白在内的共享旧消息一律不回灌到物理原生历史。
                for message in req.contexts:
                    if isinstance(message,dict): message['_no_save']=True
            finish_request(event, req, prepared_record)
        except Exception as e:
            if event.get_extra('_quill_shared_context'):
                event.set_result(event.plain_result('共享会话装配失败，本轮未继续生成，请检查存储状态。'))
                event.stop_event()
            # 记录脱敏摘要，避免泄露 user_input、context_text 等敏感字段
            # （M1a 起摘要函数在模块级 _sanitize_extra——trace 侧共用同一
            # 「只记长度不记内容」口径，语义与闭包版逐字一致）
            logger.error(
                f"[Quill] 致命错误，Prompt 装配失败，降级放行: {e} | "
                f"extra_summary={_sanitize_extra(extra_info)}",
                exc_info=True,
            )

    @filter.on_llm_response(priority=10)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 响应拦截：前置清洗、状态栏提取渲染、注入报告与落日志。

        注册桩（M2.2 第四轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_llm_response``（完整设计理由、
        段序与下沉决策见彼处 docstring；验证走 E2E
        .build/harness/quilltest/run.py --tier2）。行为契约：

        * 前置清洗：用户中断标记擦除；
        * **状态栏段不受 ``_quill_activated`` gate 限制**（BASELINE §2.1
          不对称点，快照钉住）——开启时 ``_quill_status_handled`` 已置位
          则只剥残留（不二次渲染），否则六级链提取渲染 + 无栏兜底；
          关闭时整套剥离；
        * **F1 回声置空（M3.0，有意行为变更）**：状态栏段之后、注入报告
          段之前，completion 与框架已发记录
          （``_send_message_to_user_current_session_plain_texts``）做双向
          归一比对（报告行抹除 + 状态栏变体剥离后正文全等），命中则置空
          completion 让 respond 阶段走空链——消除 SMT 回声重复回复
          （BASELINE §8.2 F1；宁漏勿误，判定异常/列表异常一律放行原路径）；
        * **F6 直出丢弃（M3.0c，有意行为变更）**：F1 段之后，
          ``_quill_smt_send_count`` ≥1（本轮已用工具发过消息）且
          completion 非空 → 置空——工具描述契约"直出文本不送达"成为真
          行为，消除元叙述旁白与变体复述（BASELINE §8.2 F6；直接文本流
          路径 count 缺失/0 不受影响）；
          随后注入报告追加（``_quill_report_added``
          去重，H2 工具路径与本路径共用）；
        * gate 后：未激活 return；助手回复落 chat_logs（
          ``rag_enable_chat_logging`` 开关 + ``_quill_assistant_logged``
          防双写标记原样保留）。

        顶层降级留在桩内（与原 H4 同层）：任何异常吞掉 + error 日志放行，
        resp 保持已改到一半的状态（降级语义 = 放行当前响应）。
        """
        try:
            # 先采原始响应，状态栏/回声清洗不得改变计费数据。
            try:
                await self._usage.main_response(event, resp)
            except Exception as exc:
                logger.warning("[Quill Usage] 主响应采集失败（%s），继续处理", type(exc).__name__)
            if not await self.shared_scopes.response_current(event):
                event.set_result(event.plain_result('共享会话任务已中断，旧结果未写入。请重新发起本轮。'))
                event.stop_event()
                return
            identity = event.get_extra("_quill_usage_identity") or {}
            with self._usage.bind(identity), bind_turn(event_turn(event)):
                await _quill_hooks.handle_llm_response(self, event, resp)
                await self.shared_scopes.finish(event,resp)
        except Exception as e:
            logger.error(f"[Quill] on_llm_response 后处理遭遇未捕获异常，已降级放行: {e}", exc_info=True)

    @filter.on_llm_tool_respond(priority=10)
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None, tool_result
    ):
        """工具调用后处理：动态记忆存储与多轮反思调度。

        注册桩（M2.2 第五轮）：装饰器/签名/priority 不变（框架以
        ``__module__`` 精确匹配绑定，BASELINE §1.2），实现委托
        ``interfaces.astrbot_hooks.handle_llm_tool_respond``。核心职责：

        - 处理 send_message_to_user 后的记忆；拒重终止由请求级工具包装器负责
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
        with self._usage.bind(event.get_extra("_quill_usage_identity") or {}):
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
            with bind_turn(event_turn(event)):
                await _quill_hooks.handle_decorating_result(self, event)
        except Exception:
            # 发送前钩子绝不能抛：抛了会中断整条回复的发送
            logger.warning("[Quill] 发送前清理异常，已放行", exc_info=True)

    # ================================================================
    # 用户指令
    # ================================================================
    #
    # 斜杠别名（每个指令都挂一个 "/同名"）：
    # AstrBot 的指令匹配只在 wake_prefix 命中时才剥掉开头的唤醒词
    # （core/pipeline/waking_check/stage.py：`for wake_prefix in wake_prefixes`
    # 在列表为空时整个循环不进入），随后 core/star/filter/command.py 用
    # `message_str.startswith(full_cmd + " ") or == full_cmd` 做匹配。
    # 于是唤醒词被清空的配置档下，消息原文的 "/" 原样留着，"/char" 永远
    # 匹配不上 "char"——指令静默失效、消息被当成普通聊天交给 LLM，而
    # 本插件的 /quill help 与全部文档写的都是带斜杠的形式。
    # 补别名让匹配层同时接受两种写法：唤醒前缀正常剥离时 "char" 命中，
    # 未剥离时 "/char" 命中别名。只影响名字匹配，不改参数解析、不改
    # 权限判断、不碰消息原文。

    @filter.command("wb", alias={"/wb"})
    async def cmd_wb(self, event: AstrMessageEvent, args: GreedyStr):
        """世界书管理。用法：/wb | /wb bind <序号|名字> | /wb unbind <序号|名字> | /wb info <序号|名字> | /wb reload"""
        arg1, arg2 = _split2(args)
        with bind_turn(await freeze_turn(self, event)):
            await _cmds.wb_dispatch(self, event, arg1, arg2)

    @filter.command("char", alias={"/char"})
    async def cmd_char(self, event: AstrMessageEvent, args: GreedyStr):
        """角色卡管理。用法：/char | /char <序号|名字> | /char unset | /char info [序号|名字] | /char export [序号|名字] | /char import <JSON>"""
        await self.shared_scopes.observe(event)
        try:
            with bind_turn(await freeze_turn(self,event)):
                await _cmds.char_dispatch(self, event, args)
        except ValueError as exc:
            event.set_result(event.plain_result(str(exc)))

    @filter.command("quill", alias={"/quill"})
    async def cmd_quill(self, event: AstrMessageEvent, args: GreedyStr):
        """Quill 系统总览与测试。用法：/quill | /quill help | /quill reset | /quill debug | /quill statusbar [on|off|auto] | /quill test <wr|wb|mem|kb> <文字>"""
        await self.shared_scopes.observe(event)
        with bind_turn(await freeze_turn(self, event, recover=str(args).strip().lower() != 'debug')):
            await _quill_dispatch(self, event, args)

    @filter.command("memory", alias={"/memory"})
    async def cmd_memory(self, event: AstrMessageEvent, args: GreedyStr):
        """动态记忆管理。用法：/memory | /memory list [页码] | /memory del <序号> | /memory clear | /memory learn [内容] | /memory search <关键词> | /memory pin <序号> [on|off] | /memory core <内容>"""
        arg1, arg2 = _split2(args)
        with bind_turn(await freeze_turn(self, event)):
            await _cmds.memory_dispatch(self, event, arg1, arg2)

    @filter.command("doc", alias={"/doc"})
    async def cmd_doc(self, event: AstrMessageEvent, args: GreedyStr):
        """外部文档 RAG 管理。用法：/doc list | /doc bind <序号> | /doc unbind <序号> | /doc search <关键词> | /doc reload"""
        arg1, arg2 = _split2(args)
        with bind_turn(await freeze_turn(self, event)):
            await _cmds.doc_dispatch(self, event, arg1, arg2)

    @filter.command("stream", alias={"/stream"})
    async def cmd_stream(self, event: AstrMessageEvent, arg: str = ""):
        """流式模式控制。用法：/stream on|off|auto"""
        with bind_turn(await freeze_turn(self,event)):
            await _cmds.stream_dispatch(self, event, arg)

    @register_command("reinject", alias={"重新注入", "/reinject"})
    async def cmd_reinject(self, event: AstrMessageEvent):
        """重置注入轮次；常驻内容仍随每次激活请求发送。用法：/reinject"""
        with bind_turn(await freeze_turn(self,event)):
            await _cmds.reinject_dispatch(self, event)

    # ================================================================
    # 未唤醒时的 /指令 兜底
    # ================================================================
    #
    # 与上面 7 个 @filter.command 是**同一套 dispatch 的第二条入口**，不是新指令：
    # 参数解析、权限门（_check_group_permission）、输出格式全部复用，差别只在
    # 「怎么被唤醒」。上面那批由 CommandFilter 匹配，而 CommandFilter 首行
    # 就要求 is_at_or_wake_command 为真；本处理器用 CustomFilter 自己判条件，
    # 因此在唤醒检查把事件丢弃之前就能拿到它（见 _UnwokenSlashFilter 的注释）。
    #
    # 参数只能自己从消息原文里切：CustomFilter 不参与框架的参数解析
    # （那是 CommandFilter 的职责），event.get_message_str() 就是「/char 1」本身。

    @filter.custom_filter(_UnwokenSlashFilter)
    async def cmd_slash_fallback(self, event: AstrMessageEvent):
        """未唤醒时接管 /wb /char /quill /memory /doc /stream /reinject。"""
        text = (event.get_message_str() or "").strip()
        name, args = _split2(text[1:])  # 去掉开头的 "/"
        try:
            handlers = {'wb':self.cmd_wb,'char':self.cmd_char,'quill':self.cmd_quill,
                        'memory':self.cmd_memory,'doc':self.cmd_doc,'stream':self.cmd_stream}
            if name == 'reinject':
                await self.cmd_reinject(event)
            elif name in handlers:
                await handlers[name](event,args)
        except Exception:
            # 兜底入口失败不能让整条消息无声无息地消失（正常情况下事件已被
            # 唤醒检查丢弃，用户本来什么都收不到），至少回一句让用户知道。
            logger.error("[Quill] 未唤醒指令兜底执行失败", exc_info=True)
            from astrbot.api.event import MessageEventResult
            event.set_result(
                MessageEventResult().message("指令执行出错，详情见 AstrBot 日志。")
                .use_markdown(False)
            )
