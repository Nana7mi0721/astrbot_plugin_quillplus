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
from typing import List

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
from .prompt_builder import PromptBuilder
from . import commands as _cmds
from .web_routes import QuillRoutes
from .encryption import decrypt_output
from .persona_manager import QuillPersonaManager


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


# ── Markdown stripper ──────────────────────────────────────────────
# Telegram 适配器没有设置 parse_mode，Markdown 语法会被原文显示。
# 在 send_message_to_user 执行前用正则擦除标记，让用户看到干净文本。

_MD_PATTERNS = [
    # Inline code (most specific first)
    (re.compile(r'`([^`\n]+)`'), r'\1'),
    # Bold-italic ***text***
    (re.compile(r'\*\*\*(.+?)\*\*\*'), r'\1'),
    (re.compile(r'___(.+?)___'), r'\1'),
    # Bold **text**
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    # Italic *text* (not adjacent to another *, protects **kwargs)
    (re.compile(r'(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)'), r'\1'),
    # Strikethrough ~~text~~
    (re.compile(r'~~(.+?)~~'), r'\1'),
    # Images ![alt](url)
    (re.compile(r'!\[([^\]]*)\]\([^)]+\)'), r'\1'),
    # Links [text](url)
    (re.compile(r'\[([^\]]+)\]\([^)]+\)'), r'\1'),
    # Reference-style links [text][ref]
    (re.compile(r'\[([^\]]+)\]\[[^\]]*\]'), r'\1'),
    # Heading markers at line start
    (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),
    # Blockquotes at line start
    (re.compile(r'^>\s?', re.MULTILINE), ''),
    # Horizontal rules
    (re.compile(r'^[-*_]{3,}[ \t]*$', re.MULTILINE), ''),
]

_STATUS_RE = re.compile(r'\[STATUS\]([\s\S]*?)\[/STATUS\]')
_LOVE_DATA_RE = re.compile(r'\[LOVE_DATA\]\s*(.+)')
_STATUS_BLOCK_RE = re.compile(r'\*\*状态栏\*\*[\s\S]*?```([\s\S]*?)```')
_PLOT_PATH_RE = re.compile(
    r'[>|]{2,}\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*[|<]{2,}\s*(.+?)\s*[|<]{2,}\s*(?:Select|请选择|选择)\s*[>|]{2,}',
    re.DOTALL | re.IGNORECASE
)

# P1-8: 核心记忆自然语言注入前缀匹配
# 支持: @记住：内容 | 记住：内容 | 核心记忆：内容 | @remember: content
_CORE_MEMORY_NL_RE = re.compile(
    r'(?:@记住\s*[：:]|记住\s*[：:]|核心记忆\s*[：:]|@remember\s*[:：])\s*(.+)',
    re.IGNORECASE
)
def _build_raw_status_re(fields: list, max_value_len: int | None = 30) -> re.Pattern:
    """方案A: 动态构建 L4 正则 — 用配置字段名替代硬编码白名单，扩展分隔符。

    分隔符扩展: [：:=→] 覆盖 '好感度→85' 等非标准格式。
    字段名动态: 支持用户自定义字段（如 饥饿度|thirst）。

    设计约束（检测与删除共用本正则，见 _handle_status_bar 分支 4）：
    - 行首锚定 (?:^|\\n) 且消费前导换行符，使 re.sub 能整行干净移除（含列表符号前缀）；
    - 值上限 {1,30}：状态值是短文本；行首的 "字段：长句" 更可能是叙事而非状态栏，
      宁可漏检交给 L5 宽松解析兜底，也不误删正文；
      上限只属于解析侧。剥离侧（_strip_status_artifacts）以 max_value_len=None
      调用，刻意不设限：关闭状态栏时要保证不漏，长值行也必须擦掉。二者的不对称
      是设计，不是遗漏——把长度统一会让长值裸字段行漏到用户屏幕上；
    - 已知裂缝：本正则只漏 1 个字段时 L5 不会兜底（_lenient_parse_status 内部要求
      ≥2），该区间最终走默认状态栏兜底，属可接受的兜底行为；
    - [^\\S\\n] 作空白类：覆盖全角空格等 Unicode 空白，但不跨行。
    """
    # 转义字段名并过滤空值
    valid_fields = [re.escape(f) for f in fields if f and f.strip()]
    if not valid_fields:
        valid_fields = [re.escape(f) for f in _DEFAULT_LOVE_FIELDS_RAW]
    value_pat = (
        r'([^\n]{1,%d}?)' % max_value_len if max_value_len else r'([^\n]+?)'
    )
    pattern = (
        r'(?:^|\n)'
        r'[^\S\n]*'
        r'(?:[-\*\•]*[^\S\n]*)?'
        r'(' + '|'.join(valid_fields) + r')'
        r'[^\S\n]*\**[^\S\n]*[：:=→][^\S\n]*'
        + value_pat +
        r'[^\S\n]*(?=\n|$)'
    )
    return re.compile(pattern, re.MULTILINE)

# 默认字段名（用于 _build_raw_status_re 兜底）
_DEFAULT_LOVE_FIELDS_RAW = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法", "服从度", "发情度"]


# ── 状态栏数值变化标注 ─────────────────────────────────────────────
# 标注形如「好感度：70（↑5）」。之所以用括号包住箭头而不是裸写 `70 ↑`：
#   1. 裸箭头会与合法值混淆 —— 「心情：上升↑」是模型自己可能写出的正常值，
#      用裸箭头做标记就无法区分「值本身」与「我们加的标记」；
#   2. 标注会随 assistant 回复回显进下一轮上下文，模型会模仿。裸箭头一旦
#      被模仿就会在值里逐轮累积（`70 ↑ ↑ ↑`），括号语法则能被下面的
#      _normalize_status_value 精确剥离。
# 该正则同时承担「渲染时匹配字段行」与「解析时剥离标记」两个职责，
# 二者必须对称，否则标记会渗进 session_vars 并被注入提示词。
_DELTA_MARK_RE = re.compile(r"\s*[（(]\s*[↑↓]\s*\d*\s*[）)]\s*$")

# 注入报告行（详见 QuillPlugin._format_inject_report / _scrub_inject_report）
_INJECT_REPORT_LINE_RE = re.compile(r"^[ \t]*〔注入〕.*$", re.MULTILINE)


def _normalize_status_value(value: str) -> str:
    """剥掉值尾部的变化标注，返回干净取值。

    必须在「比对上一轮」与「写入 session_vars」之前调用：标注是我们自己
    渲染进消息文本的，若被下一轮的 L1/L4/L5 解析器当成取值的一部分读回，
    就会同时污染两处——状态值逐轮漂移（`70（↑5）（↑5）`），以及
    session_vars 经 prompt_builder 注入 system prompt 时带上标记。
    """
    if not value:
        return value
    return _DELTA_MARK_RE.sub("", value).strip()


def _extract_numeric(value: str) -> float | None:
    """从状态值里取出可比对的数值；取不到返回 None。

    状态值是自由文本，这里只认**以数字开头**的形态：纯数字（含正负号、
    小数点），以及数字后跟单位/区间/百分号的写法（`65/100`、`3 级`、`80%`）。
    刻意不做「从任意位置抠数字」——`好感度很高`、`心情：很好` 取不到值是对的，
    比错误地把某处的数字当成状态值要安全。取不到时调用方不给标注（见
    `_format_delta` 的设计说明）。
    """
    if not value:
        return None
    m = re.match(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:$|[/、,，%级点分])", value)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _format_delta(old: str, new: str) -> str:
    """生成变化标注：仅在两侧都能取到数值时给出 `（↑5）`，否则不给标注。

    为什么不给文本值标一个只有方向的空箭头（`（↑）`）：状态栏里多数字段是
    自由文本（心情、穿着、当前想法…），它们**每轮都在变**——「当前想法」
    本来就该换。给这类字段挂箭头不传达任何信息，还会让有价值的数值变化
    淹没在噪声里。文本字段的新值本身就摆在眼前，用户读到新值便知变化；
    而数值的「变化幅度」是光看新值拿不到的（`84/100` 看不出是涨了 2 还是 20），
    这才是标注真正要补的信息。
    """
    old_num, new_num = _extract_numeric(old), _extract_numeric(new)
    if old_num is None or new_num is None:
        return ""
    diff = new_num - old_num
    if diff == 0:
        return ""
    arrow = "↑" if diff > 0 else "↓"
    # 整数差值不显示小数点（65→70 显示 ↑5 而不是 ↑5.0）
    magnitude = abs(int(diff)) if float(diff).is_integer() else round(abs(diff), 2)
    return f"（{arrow}{magnitude}）"


def _annotate_changes(content: str, changed: dict) -> str:
    """重写状态栏正文：先剥净旧标注，再给发生变化的字段行追加新标注。

    `content` 为 `字段：值` 逐行文本；`changed` 形如 {字段名: 上一轮值}。

    两个职责合并在一处是刻意的：
    - 剥旧标注：标注会随 assistant 回复回显进下一轮上下文，模型可能模仿着
      再写一遍。不剥就会出现 `70（↑5）（↑5）` 逐轮累积。
    - 加新标注：仅在 `changed` 命中时追加，未变化或取不到旧值的字段保持原样。

    匹配失败不做任何事——标注失败远比标错好。非「字段：值」的行
    （剧情走向等）原样透传。
    """
    if not content:
        return content
    out_lines = []
    for line in content.split("\n"):
        m = re.match(r"^([^：:]{1,20})[：:]\s*(.+)$", line)
        if m:
            field, value = m.group(1).strip(), m.group(2).strip()
            clean = _normalize_status_value(value)
            if clean:
                # 只有出现在 changed 里的字段才谈得上「变化」。字段缺席表示本轮
                # 未检出变化，绝不能落到 _format_delta 的「无旧值」分支——
                # 那会给每个未变化字段都挂上一个空箭头。
                mark = (
                    _format_delta(changed[field], clean)
                    if changed and field in changed
                    else ""
                )
                line = f"{field}：{clean}{mark}"
        out_lines.append(line)
    return "\n".join(out_lines)


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


class _StatusLevelResult:
    """一级降级解析的结果。

    terminal 的语义是这次重构的核心：
      * True  —— 命中即**结束**降级链（这一级产出了可用的状态数据）；
      * False —— 文本已改写但**继续下降**。L4 单命中就是这种：它只擦掉那行
                 可疑的裸字段，不足以重建整栏，所以还要让 L5/L6/兜底接手。

    旧实现把这两种语义藏在 `if not handled:` 的嵌套里（L4 单命中不置 handled
    就落下去），能跑但读不出意图，也无法统计「哪一级真的产出过状态栏」。
    """

    __slots__ = ("new_text", "updates", "terminal")

    def __init__(self, new_text: str, updates: dict | None = None,
                 terminal: bool = True) -> None:
        self.new_text = new_text
        self.updates = updates or {}
        self.terminal = terminal


class _StatusLevelContext:
    """降级链各级共享的输入（避免每级签名拖一长串参数）。

    text      —— 模型原始输出，各级**解析**都用它；
    new_text  —— 当前输出基座，各级**改写**用它。两者必须分开：
                 L4 单命中会把裸字段行从 new_text 里剥掉，而 L5 仍要按原文
                 解析——若改写也从 text 重建，那行裸字段会被重新带回来。
    """

    __slots__ = ("text", "new_text", "template", "prev_vars", "mk_changed",
                 "target_id")

    def __init__(self, text: str, new_text: str, template: str, prev_vars: dict,
                 mk_changed, target_id: str) -> None:
        self.text = text
        self.new_text = new_text
        self.template = template
        self.prev_vars = prev_vars
        self.mk_changed = mk_changed
        self.target_id = target_id


def strip_markdown(text: str) -> str:
    """Remove common Markdown formatting, leaving clean plain text."""
    if not text:
        return text
    for pattern, replacement in _MD_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@register(
    "astrbot_plugin_quillplus",
    "Nana7mi0721 & Gemini & GLM & DeepSeek",
    "羽笔 v5.0 — 世界书+写作素材库+角色卡+文档RAG+动态记忆 五合一沉浸式 RP 增强插件",
    "5.0.6",
    "https://github.com/Nana7mi0721/astrbot_plugin_quillplus",
)
class QuillPlugin(Star):
    """羽笔 — v5.0 多维沉浸式 RP 增强插件

    五合一沉浸式 RP 注入系统：世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆。

    核心特性：
    - 平行宇宙双轴隔离 (target_id::persona_id)
    - JSON 原子化状态机（拔电源级防损坏）
    - 全链路纯异步防阻塞
    - 无损对话日志归档与断点续传
    - FAISS + SQLite 事务一致性
    - 4 层 Prompt 装配 + 状态栏降级解析
    """

    # S3-13: 反思/总结相关阈值常量
    REFLECTION_TURN_THRESHOLD = 4       # 攒够 N 轮触发一次反思摘要
    RECENT_LOG_LIMIT = 8                # 反思时读取的最近日志条数
    MIN_LOGS_FOR_SUMMARY = 2            # 触发总结所需的最少日志条数

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._raw_config = config  # AstrBotConfig 实例（支持 save_config）
        self.config = QuillConfig(config)
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
        self.wr_manager = None
        self.wr_max_entries = self.config.wr_max_entries
        self.wr_fallback_top_count = self.config.wr_fallback_top

        # --- Worldbook ---
        wb_dir = self.paths["worldbooks_dir"]
        try:
            self.wb_manager = WorldbookManager(wb_dir)
            wb_names = self.wb_manager.list_worldbooks()
            logger.info(f"[Quill] 世界书已加载: {len(wb_names)} 个 - {wb_names}")
        except Exception as e:
            self.wb_manager = None
            logger.warning(f"[Quill] 世界书加载失败: {e}")

        self.wb_max_entries = self.config.worldbook_max_dynamic

        # --- Persona Manager (独立 JSON 角色卡) ---
        self.persona_manager = QuillPersonaManager(
            self.paths["personas_dir"], avatar_dir=self.paths["avatars_dir"]
        )

        # --- Prompt builder ---
        self.prompt_builder = PromptBuilder(self.config)

        # --- Refusal patterns ---
        self.refusal_enabled = self.config.refusal_enabled
        self.refusal_patterns: List[str] = self.config.refusal_patterns

        # --- Status bar ---
        self.status_bar_enabled = self.config.status_bar_enabled
        self.status_bar_format_template = self.config.status_bar_format
        self.status_bar_format_plain = self.config.status_bar_format_plain
        self.status_bar_plain_platforms: List[str] = self.config.status_bar_plain_platforms
        self.love_fields: List[str] = self.config.status_bar_fields
        self.status_bar_plot_paths: list[str] = getattr(self.config, "status_bar_plot_paths", ["继续当前话题", "转换场景", "结束互动"])
        self.status_bar_default_placeholder: str = getattr(self.config, "status_bar_default_placeholder", "未设置")
        self.status_bar_show_delta: bool = getattr(self.config, "status_bar_show_delta", True)

        # --- Debug ---
        self.debug = self.config.debug_enabled
        self.show_inject_report: bool = getattr(self.config, "show_inject_report", False)

        # --- RAG 组件（延迟到 initialize() 初始化）---
        self.rag_embedding = None
        self.rag_vector_store = None
        self.rag_reranker = None
        self.rag_memory_store = None
        self.rag_summarizer = None
        self.rag_retriever = None

        logger.info(f"[Quill] 插件构造完成 | {self.config}")

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
                logger.info(f"[Quill] 最大注入: {self.wr_max_entries} 条")
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
            # 下面这一串是把配置值摊平到插件实例属性上的「内存投影」。
            # 一旦后面的落盘失败需要回滚，这些属性也必须跟着回滚，否则
            # _raw_config 回到旧值、实例属性却停在新值，运行期读到的是
            # 「磁盘上没有、内存里生效」的配置，重启后才露出差异。
            # 快照必须建在 try 之外：回滚分支在 try 内部任何一步（包括第一行）
            # 抛异常时都会用到它，建在内部会有未赋值的风险。
            _PROJECTED_ATTRS = (
                "wr_max_entries", "wr_fallback_top_count", "wb_max_entries",
                "status_bar_enabled", "status_bar_format_template",
                "status_bar_format_plain", "status_bar_plain_platforms", "love_fields",
                "refusal_enabled", "refusal_patterns", "debug", "prompt_builder",
                "rag_enable_chat_logging", "rag_chat_log_retention_days",
                "worldbook_always_activate",
                "status_bar_plot_paths", "status_bar_default_placeholder",
                "status_bar_show_delta",
                "show_inject_report",
            )
            _MISSING = object()
            projected_previous = {
                name: getattr(self, name, _MISSING) for name in _PROJECTED_ATTRS
            }
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
                self.wr_max_entries = self.config.wr_max_entries
                self.wr_fallback_top_count = self.config.wr_fallback_top
                self.wb_max_entries = self.config.worldbook_max_dynamic
                self.status_bar_enabled = self.config.status_bar_enabled
                self.status_bar_format_template = self.config.status_bar_format
                self.status_bar_format_plain = self.config.status_bar_format_plain
                self.status_bar_plain_platforms = self.config.status_bar_plain_platforms
                self.love_fields = self.config.status_bar_fields
                self.refusal_enabled = self.config.refusal_enabled
                self.refusal_patterns = self.config.refusal_patterns
                self.debug = self.config.debug_enabled
                self.show_inject_report = getattr(
                    self.config, "show_inject_report", False
                )
                self.prompt_builder = PromptBuilder(self.config)

                self.rag_enable_chat_logging = self.config.rag_enable_chat_logging
                self.rag_chat_log_retention_days = self.config.rag_chat_log_retention_days
                self.worldbook_always_activate = self.config.worldbook_always_activate
                self.status_bar_plot_paths = self.config.status_bar_plot_paths
                self.status_bar_default_placeholder = getattr(
                    self.config, "status_bar_default_placeholder", "未设置"
                )
                self.status_bar_show_delta = getattr(
                    self.config, "status_bar_show_delta", True
                )

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
                # 先存下 retriever 的旧值，供保存失败时回滚（见下方 except）。
                # 这几个属性是热更新的，不属于 _PROJECTED_ATTRS，所以要单独记。
                _retriever_prev = None
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
                self.config = QuillConfig(self._raw_config)
                self._refresh_routes_refs()
                # 同步回滚上面那批内存投影属性：只回滚 _raw_config 会让
                # 内存投影停在「保存失败的那个新值」上，而磁盘还是旧值。
                for name, old in projected_previous.items():
                    if old is _MISSING:
                        # 原先就没有这个属性，回滚时一并移除，避免留下残留
                        self.__dict__.pop(name, None)
                    else:
                        setattr(self, name, old)
                # 回滚 Retriever 的热更新字段。这些属性不在 _PROJECTED_ATTRS 里，
                # 上面的循环覆盖不到：不还原就会出现「面板提示保存失败，但记忆
                # 开关/检索条数已按新值运行」的不一致状态。
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
        """在流式决策前切换角色卡专属对话并控制流式模式。"""
        # 必须最先执行：本事件早于 AstrBot 的 _get_session_conv()，
        # 在这里切换对话才能对本轮生效（详见 _ensure_persona_conversation）。
        await self._ensure_persona_conversation(event)

        try:
            user_input = event.message_str or ""
            target_id = self._get_target_id(event)
        except Exception:
            return

        # 拦截 /reinject 和 /重新注入（个人行为，仍用 sender_id）
        if user_input.strip() in ("/reinject", "/重新注入"):
            sender_id = str(event.get_sender_id())
            await self.state_manager.reset_quill_rounds(sender_id)
            logger.info("[Quill] /reinject 已重置 quill_rounds")
            from astrbot.core.message.message_event_result import MessageEventResult
            event.set_result(MessageEventResult().message(
                "已重置注入状态。下次触发 Quill 时将重新注入全部常驻素材。"
            ))
            return

        # 读取对话维度流式偏好
        state = await self.state_manager.get_state(target_id)

        if state.stream_mode == "off":
            event.set_extra("enable_streaming", False)
            return
        if state.stream_mode == "on":
            event.set_extra("enable_streaming", True)
            return

        # auto 模式：激活时关闭流式
        activated = self.activation_detector.should_activate(user_input)
        has_bracket = self.activation_detector.check_brackets(user_input)

        if activated or has_bracket:
            event.set_extra("enable_streaming", False)
            logger.info("[Quill] 已关闭流式输出")

    # ── 状态栏解析共享方法 ──────────────────────────────────────

    # 聚合所有状态栏变体的剥离正则（disabled 模式 + dedup 清理用）
    # 前 4 条与字段名无关（靠标签/标记识别），字段名只出现在 _strip_bare_fields 里，
    # 由 _strip_status_artifacts 按 love_fields 动态构建后拼在后面。
    _STRIP_PATTERNS: list = [
        (re.compile(r'\*\*状态栏\*\*[\s\S]*?```[\s\S]*?```'), ''),
        (re.compile(r'\[LOVE_DATA\]\s*.+'), ''),
        (re.compile(r'\[STATUS\][\s\S]*?\[/STATUS\]'), ''),
        (re.compile(
            r'[>|]{2,}\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*[|<]{2,}\s*.+?\s*[|<]{2,}\s*(?:Select|请选择|选择)\s*[>|]{2,}',
            re.DOTALL | re.IGNORECASE
        ), ''),
        (re.compile(r'\[状态栏\][\s\S]*?\[/状态栏\]'), ''),
        (re.compile(r'状态栏[：:][\s\S]*?(?=\n\n|\Z)'), ''),
    ]

    # 字段名 → 正则的缓存。键是字段元组，值同 _build_raw_status_re。
    # 原因：字段名来自面板配置（每次保存都会重建 love_fields 列表），而
    # 剥离是每条消息都要跑的热路径，不能每次重新 compile。
    _strip_field_re_cache: dict = {}

    # 「只擦原始标记」用的两条 —— 供发送前兜底钩子在**状态栏开启**时使用。
    # 刻意不放在 _STRIP_PATTERNS 里：那套是「关闭状态栏」用的完整剥离，
    # 含匹配 `**状态栏**...``` ``` 的模式，用在开启时会把正常渲染的栏删掉。
    # 也刻意**不含**裸字段行模式——理由见 _strip_raw_markers 的说明。
    _STRIP_LOVE_DATA_RE = re.compile(r'\[LOVE_DATA\]\s*.+')
    _STRIP_LEGACY_STATUS_RE = re.compile(r'\[STATUS\][\s\S]*?\[/STATUS\]')

    @classmethod
    def _strip_bare_fields_re(cls, fields: list) -> re.Pattern:
        """按字段名取（或建）剥离用正则——值不设长度上限，见 _build_raw_status_re。"""
        key = tuple(fields) if fields else ()
        cached = cls._strip_field_re_cache.get(key)
        if cached is None:
            cached = _build_raw_status_re(list(fields), max_value_len=None)
            # 配置字段数有限，缓存不会无界增长；仍设上限兜底异常调用方
            if len(cls._strip_field_re_cache) > 32:
                cls._strip_field_re_cache.clear()
            cls._strip_field_re_cache[key] = cached
        return cached

    @classmethod
    def _strip_status_artifacts(cls, text: str, fields: list | None = None) -> str:
        """移除文本中所有状态栏相关痕迹（禁用模式 + dedup 清理）。

        fields 传入当前生效的字段表（调用方传 self.love_fields）。此前这里用
        硬编码的 8 个字段名，而解析侧 L4 用动态字段——用户改字段名后（插件自己
        的协议文本就建议改成「催眠度/信赖度」），关闭状态栏时裸字段行擦不掉，
        会原样漏到屏幕上。现改为与解析侧共用同一字段来源。
        fields=None 时退回默认字段表，保证旧调用点仍可用。
        """
        if not text:
            return text
        for pattern, replacement in QuillPlugin._STRIP_PATTERNS:
            text = pattern.sub(replacement, text)
        # 字段名相关的裸字段行：与解析侧同源，保证「能解析就必能擦除」
        bare_re = QuillPlugin._strip_bare_fields_re(
            fields or _DEFAULT_LOVE_FIELDS_RAW
        )
        text = bare_re.sub('', text)
        return text.strip()

    @classmethod
    def _strip_raw_markers(cls, text: str, fields: list | None = None) -> str:
        """只擦**原始标记**，保留已渲染的状态栏 —— 发送前兜底专用。

        与 `_strip_status_artifacts` 的区别就是「要不要连渲染产物一起擦」：

        `_strip_status_artifacts` 是给「状态栏已关闭」用的，那时
        `**状态栏**...\\`\\`\\`...\\`\\`\\`` 属于该被清掉的痕迹，所以它第一条模式
        就把它整段匹配掉。而在状态栏**开启**时，同样的文本正是 L1/L2 的
        **正常产出**——拿整套剥离器去擦会把状态栏从回复里删掉。

        **这里只擦两种绝无歧义的原始标记**：`[LOVE_DATA]` 行与
        `[STATUS]...[/STATUS]` 块。它们无论如何都不该出现在最终消息里
        （渲染后的形态是模板产出，不含这两个标记本身）。

        **刻意不擦裸字段行**——因为「裸字段行」与「渲染后的状态栏内容」
        在文本上**完全同形**（渲染出来本来就是 `好感度：88` 这样的行）。
        想区分只能去认模板外壳，而模板是用户可自定义的
        （`format_template` / `format_template_plain` 都能改），
        任何白名单都会在自定义模板下失效并误删正文——
        这个坑实测踩过：用 `[[CUSTOMTPL]]` 这种自定义模板时，
        按「行首裸字段」擦会把栏里内容整段掏空，只剩一个空壳。

        权衡的依据：实测抓到的**全部**泄漏样本都是模型直接输出的
        `[LOVE_DATA]` 行（模型照契约走，会带标记）。裸字段块那种偏离契约的
        输出，常规路径上的 `on_using_llm_tool` 已在处理；为了兜住它而
        引入「可能误删用户自定义模板内容」的风险不划算。
        """
        if not text:
            return text
        text = cls._STRIP_LOVE_DATA_RE.sub('', text)
        text = cls._STRIP_LEGACY_STATUS_RE.sub('', text)
        return text.strip()

    @staticmethod
    def _lenient_parse_status(text: str, love_fields: list) -> dict:
        """宽松解析：扫描文本中 key=value 或 key：value 的行，匹配 love_fields。
        作为严格正则失败后的回退解析器。"""
        updates = {}
        seen = set()
        field_pattern = re.compile(
            r'(?:^|\n)\s*(?:[-\*\•]*\s*)?'
            r'([^\s：:=]+?)\s*[：:=]\s*(.+?)(?=\n(?:[^\s：:=]+\s*[：:=])|\n\n|\n(?:>>>)|$)',
            re.MULTILINE | re.DOTALL
        )
        for m in field_pattern.finditer(text):
            key = m.group(1).strip()
            val = m.group(2).strip()
            matched_field = None
            for lf in love_fields:
                if lf in key or key in lf:
                    matched_field = lf
                    break
            if matched_field and matched_field not in seen and val:
                seen.add(matched_field)
                updates[matched_field] = val
        return updates if len(updates) >= 2 else {}

    # ── 状态栏模板选型（平台分治）───────────────────────────────
    # 状态栏最终是「模板 + 内容」拼出来的，而模板默认是 Markdown
    # （`**状态栏**\n```\n{content}\n```）。不渲染 Markdown 的平台上，
    # `**` 和围栏会原样显示给用户，所以这些平台改用纯文本模板。
    #
    # 平台名在每条消息上才拿得到（event.platform_meta），而模板拼接发生在
    # _handle_status_bar 内部，因此把选好的模板作为参数传进去，而不是在
    # 渲染处再回头去问 event。
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

    def _prompt_builder_for_request(self, status_bar_enabled: bool):
        """按本轮的最终开关，取一个 PromptBuilder（浅拷贝，必要时覆盖开关）。

        为什么不直接改 self.prompt_builder.status_bar_enabled：它是共享实例，
        并发请求会互相踩（A 会话设 on 会污染 B 会话）。浅拷贝只复制属性引用，
        PromptBuilder 不持有连接/任务，拷贝成本可忽略，且绝不落回共享实例。

        为什么不用给 build_system_prompt 加参数：契约文案分散在
        build_status_bar_guide / build_send_message_guide / build_safety_wrapper
        三处读取该开关，加参数就得把签名一路改到底；拷贝一次把这四个读取点
        一次性对齐，改动面最小。
        """
        if status_bar_enabled == self.prompt_builder.status_bar_enabled:
            return self.prompt_builder
        pb = copy.copy(self.prompt_builder)
        pb.status_bar_enabled = status_bar_enabled
        return pb

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

    async def _handle_status_bar(
        self, text: str, target_id: str, bar_template: str | None = None
    ) -> tuple:
        """统一状态栏处理入口。

        返回 (formatted_text: str, updates: dict, handled: bool)。
        handled=True 表示文本中已存在有效状态栏并完成了格式化+持久化。
        handled=False 表示未找到状态栏，调用方应注入兜底。

        与上一轮取值的比对读一次 `session_vars` 后全程复用（内存操作），
        同时供变化标注（L1-L6）与 L4/L5 的缺失字段补齐使用。

        bar_template：本次渲染用的模板。None 时用默认（Markdown）模板——
        保持既有调用点行为不变。平台分治由调用方经 _status_bar_template_for
        选好传进来。
        """
        updates = {}
        new_text = text
        handled = False
        bar_template = bar_template or self.status_bar_format_template
        prev_vars = await self.state_manager.get_session_vars(target_id)

        def _mk_changed(ups: dict) -> dict:
            """本次取值相对上一轮的变化 {字段: 旧值}；关闭标注时返回空。

            两侧都先归一化：模型有时会照抄上一轮我们渲染的标注
            （`70（↑5）`），不剥掉就会与干净的旧值比较失败、每轮都误报变化。
            """
            if not self.status_bar_show_delta:
                return {}
            changed = {}
            for k, v in ups.items():
                clean_v = _normalize_status_value(v)
                old = _normalize_status_value(prev_vars.get(k, ""))
                if old and clean_v and old != clean_v and clean_v != self.status_bar_default_placeholder:
                    changed[k] = old
            if changed:
                logger.debug(f"[Quill] 状态栏字段变化: {changed}")
            return changed

        # ── 六级降级链：越靠前越严格，命中即停 ──────────────────
        # 每级是一个独立方法（_sb_l1.._sb_l6），返回 _StatusLevelResult 或 None。
        # 拆成注册表而不是一串 `if not handled:` 块，是为了三件事：
        #   1. 逐级命中率可统计（此前只能 grep 日志文本，看不出比例）；
        #   2. 「命中即结束」与「已改写但仍需下降」两种语义显式化
        #      （见 _StatusLevelResult.terminal）；
        #   3. 某一级抛异常时只降级该级、继续往下走，而不是让整链崩掉
        #      （旧写法下任何一级抛异常都会冒泡到调用方，状态栏直接消失）。
        # 顺序即优先级，不要随意调整：越靠前的格式越严格、越可信。
        ctx = _StatusLevelContext(
            text=text,
            new_text=new_text,
            template=bar_template,
            prev_vars=prev_vars,
            mk_changed=_mk_changed,
            target_id=target_id,
        )
        for _lv_name, _lv_attr in self._SB_LEVELS:
            try:
                _res = await getattr(self, _lv_attr)(ctx)
            except Exception:
                logger.warning(
                    f"[Quill] 状态栏降级链「{_lv_name}」级异常，继续下降",
                    exc_info=True,
                )
                continue
            if _res is None:
                continue
            new_text = _res.new_text
            ctx.new_text = new_text
            if _res.updates:
                updates = _res.updates
            self.health_tracker.record_status_level(_lv_name)
            if _res.terminal:
                handled = True
                break

        # P1-1: 所有降级解析均失败时，记录原始文本片段便于调试（不暴露给用户）
        # 注意：本函数只在「本轮最终开关为开」时才被调用（调用方已用
        # _effective_status_bar_enabled 判过），所以这里不再看全局开关——
        # 否则 /quill statusbar on 覆盖全局关时，这两个分支会被错误跳过。
        if not handled:
            preview = (text or "")[:200].replace("\n", "\\n")
            logger.info(f"[Quill] 状态栏解析失败（L1-L5 全部未匹配），使用兜底默认状态栏 | target={target_id} | preview={preview!r}")

        # P1-4: 记录状态栏解析成功率
        self.health_tracker.record_status(handled)

        # P2-4 修复：所有分支统一在此提交一次状态字段，消除多次独立 await 的竞态
        # 审查修复：变化标注仅渲染进消息文本，绝不写入 session_vars——此前会把
        # dict 一并持久化进 quill_state.json，并被 prompt_builder 无白名单遍历注入
        # system prompt（dict repr 污染模型输入）。
        # 落库前统一归一化：模型可能照抄上一轮的标注，不清洗就会随
        # update_session_vars 存进状态并注入提示词，逐轮累积。
        if updates and handled:
            persist_updates = {
                k: _normalize_status_value(v)
                for k, v in updates.items()
                if not k.startswith("_") and isinstance(v, str)
            }
            updates.update(persist_updates)
            await self._persist_status_vars(persist_updates, target_id)

        return new_text, updates, handled

    # ── 降级链各级实现 ─────────────────────────────────────────
    # 级别名会进日志与统计，改名字要同步 docs/STATUS_BAR.md 与 harness 断言。
    _SB_LEVELS: tuple = (
        ("code block", "_sb_l1_code_block"),
        ("LOVE_DATA inline", "_sb_l2_love_data"),
        ("STATUS legacy", "_sb_l3_legacy"),
        ("raw key:value, 动态字段", "_sb_l4_raw"),
        ("lenient + 部分提取", "_sb_l5_lenient"),
        ("LLM 智能提取", "_sb_l6_llm_extract"),
    )

    async def _sb_l1_code_block(self, ctx) -> "_StatusLevelResult | None":
        """L1：`**状态栏** ``` ... ``` ` —— 只替换块内内容，保留外围 Markdown。

        保留外围结构是刻意的：模型常把标题写在外面，整块重渲染会把它吃掉。
        """
        m = _STATUS_BLOCK_RE.search(ctx.text)
        if not m:
            return None
        raw_content = m.group(1)
        updates = self._parse_status_block(raw_content)
        if not updates:
            return None
        # 首尾空白必须原样带回去——group 1 含包裹内容的换行符，丢掉会把
        # ``` 围栏与内容挤到同一行，代码块随之失效。
        lead = raw_content[: len(raw_content) - len(raw_content.lstrip())]
        trail = raw_content[len(raw_content.rstrip()):]
        annotated = _annotate_changes(raw_content.strip(), ctx.mk_changed(updates))
        new_text = (
            ctx.text[: m.start(1)] + lead + annotated + trail + ctx.text[m.end(1):]
        )
        logger.info("[Quill] 状态栏已处理 (code block)")
        return _StatusLevelResult(new_text, updates)

    async def _sb_l2_love_data(self, ctx) -> "_StatusLevelResult | None":
        """L2：`[LOVE_DATA] a | b | c` 单行 —— **实际最常命中的一级**。

        guide 明确要求模型输出这个格式（还把代码块列为错误示例），实测 40 次
        解析里 36 次走这里。此前这一级不套模板、直接把裸字段行替换进正文，
        导致 format_template 配置在整个子系统的主力路径上从未生效。
        """
        love_updates, love_formatted, raw_line = self._format_love_data(ctx.text)
        if not love_updates:
            return None
        annotated = _annotate_changes(love_formatted, ctx.mk_changed(love_updates))
        # 套用本平台模板，与其余五级一致
        new_text = ctx.text.replace(
            raw_line, ctx.template.replace("{content}", annotated)
        )
        logger.info("[Quill] 状态栏已处理 (LOVE_DATA inline)")
        return _StatusLevelResult(new_text, love_updates)

    async def _sb_l3_legacy(self, ctx) -> "_StatusLevelResult | None":
        """L3：`[STATUS]...[/STATUS]` 旧格式。

        注意这一级**不校验解析结果是否为空**：只要标签在，就算认领（旧行为，
        有意保留——标签本身就是「模型在写状态栏」的确证，哪怕内容不合格式）。
        """
        m = _STATUS_RE.search(ctx.text)
        if not m:
            return None
        status_content = m.group(1).strip()
        updates = self._parse_legacy_status(status_content)
        annotated = _annotate_changes(status_content, ctx.mk_changed(updates))
        formatted = ctx.template.replace("{content}", annotated)
        new_text = _STATUS_RE.sub(formatted, ctx.text)
        logger.info("[Quill] 状态栏已处理 (STATUS legacy)")
        return _StatusLevelResult(new_text, updates)

    async def _sb_l4_raw(self, ctx) -> "_StatusLevelResult | None":
        """L4：裸 `字段：值` 多行（动态字段 + 分隔符扩展）。

        阈值取「去重后 ≥2」而非 ≥1：单命中更可能是叙事（「他想起她当时的心情：
        那份悸动」这类行首恰好是字段名的句子），拿一行叙事重建整栏，其余字段
        全靠历史值补齐，等于用一个可疑值造出一整栏陈旧状态。

        但单命中**必须仍然把那行擦掉**——它会原样发给用户，看着像漏处理。
        所以拆成两条路：≥2 重建整栏（terminal），单命中只剥离该行并继续下降
        （non-terminal，交给 L5/L6/兜底补栏）。
        """
        # 预处理：移除 LLM 可能添加的 --- 分隔线干扰（仅用于解析，不影响输出文本）
        clean_text_for_parse = re.sub(
            r'^[-*_]{3,}[ \t]*$', '', ctx.text, flags=re.MULTILINE
        )
        raw_re = _build_raw_status_re(self.love_fields)
        raw_matches = raw_re.findall(clean_text_for_parse)
        _seen = {fn for fn, _fv in raw_matches if fn in self.love_fields}

        if len(_seen) >= 2:
            updates: dict = {}
            matched_fields = set()
            for fn, fv in raw_matches:
                if fn in self.love_fields and fn not in matched_fields:
                    updates[fn] = fv.strip()
                    matched_fields.add(fn)
            changed = ctx.mk_changed(updates)
            # 补齐缺失字段后再整体标注，保证变化字段与非变化字段同样被剥净旧标注
            for f_name in self.love_fields:
                if f_name not in matched_fields:
                    updates[f_name] = (
                        ctx.prev_vars.get(f_name, "")
                        or self.status_bar_default_placeholder
                    )
            parsed_lines = [
                f"{f}：{_normalize_status_value(str(updates.get(f, '')))}"
                for f in self.love_fields
            ]
            # 用与检测完全相同的正则做对称删除——整行移除（含列表符号前缀，
            # 前导换行一并消费，不留空行）。此前按字段名单独构造无锚定模式
            # (rf'{fn}\s*[：:=→].*')，会误删叙事句中间的同名字段到行尾。
            new_text = raw_re.sub('', ctx.new_text)
            # 剧情走向
            plot_str = ""
            pm = _PLOT_PATH_RE.search(new_text)
            if pm:
                plot_content = pm.group(1).strip()
                new_text = new_text.replace(pm.group(0), "").strip()
                plot_str = f"\n\n>>> 剧情走向 <<<\n{plot_content}\n<<< 请选择 >>>"
            block_content = _annotate_changes("\n".join(parsed_lines), changed) + plot_str
            beautiful_bar = ctx.template.replace("{content}", block_content)
            new_text = new_text.strip() + "\n\n" + beautiful_bar
            logger.info("[Quill] 状态栏已处理 (raw key:value, 动态字段)")
            return _StatusLevelResult(new_text, updates)

        if raw_matches:
            _stripped = raw_re.sub('', ctx.new_text)
            if _stripped != ctx.new_text:
                logger.info(
                    "[Quill] L4 单命中（%s），不重建整栏，仅剥离该行",
                    "/".join(sorted(_seen)),
                )
                return _StatusLevelResult(_stripped, None, terminal=False)
        return None

    async def _sb_l5_lenient(self, ctx) -> "_StatusLevelResult | None":
        """L5：宽松解析（key 双向子串匹配）+ 历史值融合。

        阈值 ≥2 由 `_lenient_parse_status` 内部把关，外层不再复述——旧代码在
        这里写了个恒真的 `>= 1`，让读者以为阈值被调过，实际上空 dict 早已被
        `if lenient_updates` 挡掉。
        """
        lenient_updates = self._lenient_parse_status(ctx.text, self.love_fields)
        if not lenient_updates:
            return None
        merged: dict = {}
        for f in self.love_fields:
            new_val = lenient_updates.get(f)
            merged[f] = new_val if new_val else (
                ctx.prev_vars.get(f, "") or self.status_bar_default_placeholder
            )
        lines = [f"{f}：{merged[f]}" for f in self.love_fields]
        bar = ctx.template.replace(
            "{content}", _annotate_changes("\n".join(lines), ctx.mk_changed(merged))
        )
        logger.info(
            f"[Quill] 状态栏已处理 (lenient + 部分提取 "
            f"{len(lenient_updates)}/{len(self.love_fields)} 字段)"
        )
        return _StatusLevelResult(ctx.new_text + "\n\n" + bar, merged)

    async def _sb_l6_llm_extract(self, ctx) -> "_StatusLevelResult | None":
        """L6：LLM 智能提取（可选，默认关闭——额外 token 消耗）。"""
        if not getattr(self.config, "status_bar_llm_extract", False):
            return None
        llm_extracted = await self._llm_extract_status(ctx.text, ctx.target_id)
        if not llm_extracted:
            return None
        merged: dict = {}
        for f in self.love_fields:
            merged[f] = (
                llm_extracted.get(f)
                or ctx.prev_vars.get(f, "")
                or self.status_bar_default_placeholder
            )
        lines = [f"{f}：{merged[f]}" for f in self.love_fields]
        bar = ctx.template.replace(
            "{content}", _annotate_changes("\n".join(lines), ctx.mk_changed(merged))
        )
        logger.info("[Quill] 状态栏已处理 (LLM 智能提取)")
        return _StatusLevelResult(ctx.new_text + "\n\n" + bar, merged)

    async def _persist_status_vars(self, updates: dict, target_id: str) -> None:
        """Persist parsed status fields to session_vars."""
        if updates:
            await self.state_manager.update_session_vars(target_id, updates)

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
        if not self.show_inject_report or not text:
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

    @staticmethod
    def _parse_legacy_status(status_content: str) -> dict:
        """Parse legacy [STATUS] multi-line format into key-value dict."""
        updates = {}
        for line in status_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                updates[k.strip()] = v.strip()
        return updates

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

    async def _llm_extract_status(self, text: str, target_id: str) -> dict | None:
        """方案C: LLM 智能提取状态栏字段 — 当 L1-L5 全部失败时，调用轻量 LLM 做结构化提取。

        风险控制:
        - 超时 3s，失败则放弃（不影响主流程）
        - JSON 解析失败则放弃
        - 字段名白名单校验（仅保留 self.love_fields 中的）
        - 启发式判断：文本中必须包含 ≥2 个字段关键词才触发
        """
        # 启发式：检查文本是否疑似包含状态信息
        keyword_hits = sum(1 for f in self.love_fields if f in text)
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

        fields_json = json.dumps(self.love_fields, ensure_ascii=False)
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
            for f in self.love_fields:
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

    def _parse_status_block(self, block_content: str) -> dict:
        """Parse **状态栏** code block content into key-value dict."""
        updates = {}
        for line in block_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ">>>" in line or "<<<" in line:
                continue
            if "：" in line:
                k, v = line.split("：", 1)
                updates[k.strip()] = v.strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                updates[k.strip()] = v.strip()
            elif "=" in line:
                k, v = line.split("=", 1)
                updates[k.strip()] = v.strip()
        return updates

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

        背景：AstrBot 的 conversation 只按 UMO 切分，**不按角色卡切分**。
        插件的 memories / chat_logs 早已按 `UMO::persona` 隔离，唯独
        AstrBot 侧那段对话历史（即 `req.contexts`）没有隔离，导致切换角色卡后
        新角色仍能读到上一个角色的对话。

        这里给每张角色卡绑定一个独立 conversation：
          * 切到某张卡 → 切到它上次用的对话（切回来仍能看到那段历史）；
          * 该卡首次使用 → 新建一个空对话。

        时序上必须挂在 `on_waiting_llm_request`：该事件在 AstrBot
        `_get_session_conv()` **之前**触发（internal.py:225 vs 239），
        因此这里的切换**对本轮立即生效**；若放到 on_llm_request 则要下一轮才生效。

        一切异常都只记日志并放行：拿不到 conversation_manager 或接口变动时，
        插件退回「不分对话」的原有行为，绝不让隔离逻辑打断正常聊天。
        """
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return
        try:
            umo = self._get_target_id(event)
            persona_id = await self.state_manager.get_persona_id(umo)
            key = persona_id or ""  # 未绑卡时归入空串一档，同样独立
            mapping = await self.state_manager.get_persona_conv_map(umo)
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            target_cid = mapping.get(key)

            if target_cid and target_cid == curr_cid:
                return  # 快路径：已在正确对话，零额外查询

            if target_cid:
                # 对话可能已被 Dashboard 删除；switch 不校验存在性（conversation_mgr.py:126），
                # 不校验会导致每轮都切到一个不存在的 cid 而不断新建/泄漏。
                try:
                    conv = await conv_mgr.get_conversation(umo, target_cid)
                except Exception:
                    conv = None
                if conv is None:
                    logger.info(
                        f"[Quill] 角色卡 {key or '(未绑定)'} 的原对话 {target_cid[:8]} 已不存在，将重建"
                    )
                    await self.state_manager.forget_persona_conv(umo, key)
                    target_cid = None

            if target_cid is None:
                if not mapping and curr_cid:
                    # 首次启用隔离：当前角色卡接管现有对话，历史不断
                    target_cid = curr_cid
                    logger.info(
                        f"[Quill] 角色卡 {key or '(未绑定)'} 接管当前对话 "
                        f"{curr_cid[:8]}（首次启用对话隔离）"
                    )
                else:
                    target_cid = await conv_mgr.new_conversation(
                        umo, event.get_platform_id()
                    )
                    logger.info(
                        f"[Quill] 已为角色卡 {key or '(未绑定)'} 新建独立对话 "
                        f"{str(target_cid)[:8]}（对话历史将相互隔离）"
                    )
                await self.state_manager.set_persona_conv(umo, key, target_cid)

            if curr_cid != target_cid:
                await conv_mgr.switch_conversation(umo, target_cid)
                logger.info(
                    f"[Quill] 对话已切换 → {str(target_cid)[:8]} "
                    f"(角色卡: {key or '(未绑定)'})"
                )
        except Exception as e:
            logger.warning(f"[Quill] 角色卡对话隔离失败，本轮沿用当前对话: {e}")

    @filter.on_using_llm_tool(priority=200)
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None
    ):
        """工具调用前拦截 — 在 Telegram 平台剥离 Markdown 标记，全平台格式化/擦除状态栏。"""
        # S2-3 修复：顶层 try/except，异常时降级放行，与 on_llm_request/response 保持一致
        try:
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
            needs_strip = platform in ("telegram", "tg")
            if needs_strip:
                logger.info(f"[Quill] >>> send_message_to_user 调用 (platform={platform or '?'}), 清理 Markdown...")
                modified = 0
                for msg in messages if isinstance(messages, list) else []:
                    if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                        original = msg["text"]
                        cleaned = strip_markdown(original)
                        if cleaned != original:
                            msg["text"] = cleaned
                            modified += 1
                if modified:
                    logger.info(f"[Quill] 已清理 {modified} 条消息中的 Markdown 标记")

            # 状态栏处理（全平台执行）
            # 本轮最终开关 = 会话级覆盖 > 面板全局（见 _effective_status_bar_enabled）
            target_id = self._get_target_id(event)
            _sb_on = await self._effective_status_bar_enabled(target_id)
            _bar_tpl = self._status_bar_template_for(platform)
            if isinstance(messages, list):
                report_done = False
                for idx, msg in enumerate(messages):
                    if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                        # 首条 plain 消息：执行状态栏提取；后续消息：仅清理残留状态栏标记
                        if idx == 0 or not event.get_extra("_quill_status_handled"):
                            if _sb_on:
                                new_text, _, handled = await self._handle_status_bar(
                                    msg["text"], target_id, _bar_tpl
                                )
                                msg["text"] = new_text
                                if handled:
                                    event.set_extra("_quill_status_handled", True)
                            else:
                                msg["text"] = self._strip_status_artifacts(
                                    msg["text"], self.love_fields
                                )
                        else:
                            # P2-3 修复：首条之后的 plain 消息也清理残留的状态栏标记，
                            # 避免 LLM 多段输出时后续段落的 [LOVE_DATA]/状态栏代码块被原样发给用户
                            msg["text"] = self._strip_status_artifacts(
                                msg["text"], self.love_fields
                            )
                        # 注入报告追加到最后一条 plain 消息上（仅一次）
                        if not report_done and idx == len(messages) - 1:
                            before = msg["text"]
                            msg["text"] = self._append_inject_report(msg["text"], target_id)
                            if msg["text"] != before:
                                event.set_extra("_quill_report_added", True)
                            report_done = True

            # JSON 回写：如果原始类型是字符串，序列化回去
            if was_string:
                tool_args["messages"] = json.dumps(messages, ensure_ascii=False)

            # S3-2: Agent 模式下 LLM 输出可能经由 tool_args.messages 传递，
            # completion_text 为空时拒绝内容藏于此，需在此补充扫描。
            if self.refusal_enabled and isinstance(messages, list):
                target_id = self._get_target_id(event)
                for msg in messages:
                    if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                        scan_text = msg.get("text") or ""
                        if not scan_text:
                            continue
                        for pattern in self.refusal_patterns:
                            if pattern in scan_text:
                                await self.state_manager.mark_refusal(target_id)
                                logger.info(f"[Quill] (tool_args) 检测到拒绝模式 '{pattern}' (target={target_id})")
                                break
                        break  # 只扫首条 plain 文本
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
                            c["content"], self.love_fields
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

            if wr_activated and self.wr_manager and self.debug:
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
                "wr_max_entries": self.wr_max_entries,
                "wr_fallback_top_count": self.wr_fallback_top_count,
                "wb_max_entries": self.wb_max_entries,
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

            req.system_prompt = self.prompt_builder.inject_prompt(
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
        """LLM 响应拦截：Base64 解密、拒绝检测、状态栏提取与剧情分支解析。

        核心职责：
        - Base64 解密输出（若启用）
        - 反拒绝模式检测与降级处理
        - 状态栏提取（多级正则降级）
        - 剧情走向 (Plot Paths) 解析
        """
        try:
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
            target_id = self._get_target_id(event)

            # 会话级最终开关（与请求侧同一个解析函数，保证前后一致）
            _sb_effective = await self._effective_status_bar_enabled(target_id)
            _bar_tpl = self._status_bar_template_for(self._resolve_platform_name(event))

            if _sb_effective:

                if event.get_extra("_quill_status_handled"):
                    # 工具钩子已处理完毕 — 仅剥离 resp.completion_text 中的
                    # 原始状态栏残留（LLM 可能同时在 content 字段也输出了）
                    content = resp.completion_text or ""
                    stripped = self._strip_status_artifacts(content, self.love_fields)
                    if stripped != content:
                        resp.completion_text = stripped
                        logger.info("[Quill] 已剥离 resp.completion_text 中的状态栏残留")
                else:
                    # 工具钩子未命中 — 在此处作为最终安全网处理
                    content = resp.completion_text or ""
                    new_text, _, handled = await self._handle_status_bar(
                        content, target_id, _bar_tpl
                    )
                    if not handled:
                        persona_id = await self.state_manager.get_persona_id(target_id)
                        if persona_id:
                            default_bar = await self._build_default_love_data(
                                target_id, _bar_tpl
                            )
                            new_text = (new_text or "") + "\n" + default_bar
                            logger.info("[Quill] 状态栏兜底注入")
                    resp.completion_text = new_text

            else:
                # 禁用模式：彻底擦除所有状态栏痕迹
                content = resp.completion_text or ""
                resp.completion_text = self._strip_status_artifacts(
                    content, self.love_fields
                )

            # 注入报告（仅开关开启时）。工具路径已在 on_llm_tool_respond 里
            # 追加过，用标记去重——两条路径都会跑到本函数，否则会出现两行报告。
            if (self.show_inject_report and not event.get_extra("_quill_report_added")
                    and (resp.completion_text or "").strip()):
                resp.completion_text = self._append_inject_report(
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
                    and getattr(self.config, 'rag_enable_chat_logging', True)
                    and self.rag_retriever and self.rag_retriever.memory_store):
                resp_pid = await self.state_manager.get_persona_id(target_id)
                self._spawn(self.rag_retriever.log_chat_message(
                    self._get_memory_session_id(target_id, resp_pid),
                    "assistant", (resp.completion_text or "").strip()
                ))
                event.set_extra("_quill_assistant_logged", True)

            if not self.refusal_enabled:
                return

            scan_text = resp.completion_text or ""
            if not scan_text:
                return

            for pattern in self.refusal_patterns:
                if pattern in scan_text:
                    await self.state_manager.mark_refusal(target_id)
                    logger.info(f"[Quill] 检测到拒绝模式 '{pattern}' (target={target_id})")
                    break
        except Exception as e:
            logger.error(f"[Quill] on_llm_response 后处理遭遇未捕获异常，已降级放行: {e}", exc_info=True)

    @filter.on_llm_tool_respond(priority=10)
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None, tool_result
    ):
        """工具调用后拦截：Agent Loop 终止信号、动态记忆存储与多轮反思调度。

        核心职责：
        - 检测 send_message_to_user 调用，终止 agent loop
        - AI 回复异步写入对话日志（供断点续传使用）
        - N 轮反思触发：攒够阈值后生成上下文摘要
        - 记忆修剪调度（分档遗忘）
        - 过期对话日志无人值守清理（避免长期运行日志膨胀）
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
        if (self.rag_retriever and self.rag_retriever.enable_memory
                and self.rag_retriever.memory_store):
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
                target_id = self._get_target_id(event)
                persona_id = await self.state_manager.get_persona_id(target_id)
                mem_session_id = self._get_memory_session_id(target_id, persona_id)

                # 记录 AI 回复到对话日志（始终保留，供断点续传使用；
                # 直接文本流已在 on_llm_response 落库时跳过，防双写）
                if ai_response.strip() and not event.get_extra("_quill_assistant_logged") \
                        and getattr(self.config, 'rag_enable_chat_logging', True):
                    event.set_extra("_quill_assistant_logged", True)
                    self._spawn(self.rag_retriever.log_chat_message(
                        mem_session_id, "assistant", ai_response.strip()
                    ))

                # N 轮反思触发：攒够 N 轮对话后生成摘要
                try:
                    unsummarized = await self.state_manager.increment_unsummarized_turns(target_id)

                    if unsummarized >= self.REFLECTION_TURN_THRESHOLD:
                        await self.state_manager.reset_unsummarized_turns(target_id)
                        recent_logs = await self.rag_retriever.memory_store.get_recent_chat_logs(mem_session_id, limit=self.RECENT_LOG_LIMIT)

                        if len(recent_logs) >= self.MIN_LOGS_FOR_SUMMARY:
                            # S1-1 修复：改用 _spawn 保留 task 引用，防止 GC 中断
                            sum_task = self._spawn(
                                self.rag_retriever.summarize_contexts(mem_session_id, contexts=recent_logs)
                            )
                            def _log_summary_result(t):
                                exp = t.exception()
                                if exp:
                                    logger.warning(f"[Quill Memory] 多轮总结异常: {exp}")
                            sum_task.add_done_callback(_log_summary_result)

                        # 顺带跑一次记忆修剪（分档遗忘）
                        if self.rag_retriever.memory_store:
                            self._spawn(self.rag_retriever.memory_store.prune_memories())
                            # 清理过期对话日志（无人值守，避免长期运行服务器日志膨胀）
                            self._spawn(
                                self.rag_retriever.memory_store.cleanup_chat_logs(
                                    getattr(self.config, 'rag_chat_log_retention_days', 30)
                                )
                            )
                    else:
                        logger.debug(f"[Quill Memory] 记忆收集进度: {unsummarized}/4 轮")
                except Exception as e:
                    logger.warning(f"[Quill Memory] 反思调度失败: {e}")

            except Exception as e:
                logger.warning(f"[Quill Memory] 记忆存储调度失败: {e}")

    # ================================================================
    # 最后一道防线：发送前擦除残留状态栏
    # ================================================================

    @filter.on_decorating_result(priority=100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息**发送前**的最后一次清洗——擦掉漏网的状态栏残留。

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
        """
        try:
            # 会话级覆盖 > 面板全局：关闭方向必须清，开启方向只清原始标记
            enabled = await self._effective_status_bar_enabled(
                self._get_target_id(event)
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
                    stripped = self._strip_raw_markers(text, self.love_fields)
                else:
                    stripped = self._strip_status_artifacts(text, self.love_fields)
                if stripped != text:
                    comp.text = stripped
                    cleaned += 1
            if cleaned:
                logger.info(
                    f"[Quill] 发送前擦除 {cleaned} 段状态栏残留"
                    f"（{'原始标记' if enabled else '全套剥离'}）"
                )
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
