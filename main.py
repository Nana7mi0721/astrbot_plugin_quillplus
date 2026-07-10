# -*- coding: utf-8 -*-
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

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from .config import QuillConfig
from .activation import ActivationDetector
from .state import StateManager
from .kb import KnowledgeBaseManager
from .worldbook import WorldbookManager
from .prompt_builder import PromptBuilder
from . import commands as _cmds
from .web_routes import QuillRoutes
from .encryption import decrypt_output
from .persona_manager import QuillPersonaManager


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
    r'>>>\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*<<<\s*(.+?)\s*<<<\s*(?:Select|请选择|选择)\s*>>>',
    re.DOTALL | re.IGNORECASE
)
_RAW_STATUS_RE = re.compile(
    r'(?:^|\n)\s*(?:[-\*\•]*\s*)?(好感度|关系阶段|心情|位置|穿着|当前想法|服从度|发情度)\s*[：:]\s*(.+?)(?=\n|$)',
    re.MULTILINE
)


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
    "5.0.1",
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

        # --- Activation ---
        activation_path = os.path.join(self.plugin_dir, "activation_triggers.yaml")
        self.activation_detector = ActivationDetector(activation_path)

        # --- State ---
        data_dir = os.path.join(self.plugin_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.state_manager = StateManager(data_dir=data_dir)

        # --- Knowledge Base (deferred to initialize()) ---
        self.kb_manager = None
        self.kb_max_entries = self.config.kb_max_entries
        self.kb_fallback_top_count = self.config.kb_fallback_top

        # --- Worldbook ---
        wb_dir = os.path.join(self.plugin_dir, "worldbooks")
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
            os.path.join(self.plugin_dir, "data", "quill_personas")
        )

        # --- Prompt builder ---
        self.prompt_builder = PromptBuilder(self.config)

        # --- Refusal patterns ---
        self.refusal_enabled = self.config.refusal_enabled
        self.refusal_patterns: List[str] = self.config.refusal_patterns

        # --- Status bar ---
        self.status_bar_enabled = self.config.status_bar_enabled
        self.status_bar_format_template = self.config.status_bar_format
        self.love_fields: List[str] = self.config.status_bar_fields
        self.status_bar_plot_paths: list[str] = getattr(self.config, "status_bar_plot_paths", ["继续当前话题", "转换场景", "结束互动"])

        # --- Debug ---
        self.debug = self.config.debug_enabled

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

    async def initialize(self) -> None:
        """异步初始化：KB 载入、Web 路由注册、RAG 索引构建、过期日志清理。

        生命周期阶段一。完成以下工作：
        - 知识库（KB）懒加载
        - Web 面板 API 路由注册
        - RAG 检索器与 FAISS 索引初始化
        - 过期对话日志清理与低价值记忆修剪
        """
        if self.config.kb_enabled:
            kb_path = os.path.join(self.plugin_dir, "knowledge", "quill_kb.db")
            try:
                category_dedup_limit = self.config.kb_dedup_limit
                self.kb_manager = KnowledgeBaseManager(kb_path, category_dedup_limit=category_dedup_limit)
                await self.kb_manager.initialize()
                stats = await self.kb_manager.get_stats()
                logger.info(
                    f"[Quill] 写作素材库已加载: {stats['total_entries']} 条 "
                    f"(启用 {stats['enabled_entries']} 条)"
                )
                logger.info(f"[Quill] 最大注入: {self.kb_max_entries} 条")
            except Exception as e:
                self.kb_manager = None
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
            QuillRoutes(self.kb_manager, self.wb_manager, self.context, self.config,
                        rag_components=rag_components, plugin=self,
                        persona_manager=self.persona_manager).register_all()
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
            cleaned = await asyncio.to_thread(
                self.rag_retriever.memory_store.cleanup_chat_logs, retention_days
            )
            if cleaned:
                logger.info(f"[Quill ChatLog] 清理了 {cleaned} 条过期日志（保留 {retention_days} 天）")

        # 启动时修剪过期低价值记忆
        if self.rag_retriever and self.rag_retriever.memory_store:
            pruned = await asyncio.to_thread(self.rag_retriever.memory_store.prune_memories)
            if pruned:
                logger.info(f"[Quill Memory] 启动修剪: 清理了 {pruned} 条低价值记忆")

        # 启动 state 自动落盘（分级落盘：关键字段即时，高频字段 5s 批量刷洗）
        self.state_manager.start_autoflush()

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
            rag_db = os.path.join(self.plugin_dir, "knowledge", "quill_rag.db")
            rag_idx = os.path.join(self.plugin_dir, "knowledge", "quill_rag.index")
            # S2-10: 传入 embedding_provider，切换 provider 时自动重建索引
            self.rag_vector_store = FaissVectorStore(
                rag_db, rag_idx, embedding_provider=self.rag_embedding
            )

            # Reranker
            self.rag_reranker = QuillReranker(
                self.context,
                rerank_provider_id=self.config.rag_rerank_provider_id,
                fallback_llm_id=self.config.rag_llm_provider_id,
            )

            # 动态记忆存储（SQLite BLOB）
            mem_db = os.path.join(self.plugin_dir, "knowledge", "quill_memory.db")
            self.rag_memory_store = MemoryStore(mem_db)

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
        if self.kb_manager:
            try:
                await self.kb_manager.close()
            except Exception as e:
                logger.warning(f"[Quill] 写作素材库关闭失败: {e}")
        if self.rag_retriever and self.rag_retriever.memory_store:
            self.rag_retriever.memory_store.close()
        if self.rag_retriever and self.rag_retriever.vector_store:
            self.rag_retriever.vector_store.close()
        logger.info("[Quill] 插件已停用")

    def _spawn(self, coro):
        """F5 修复：启动后台任务并保留引用，防止被 GC 中断。完成后自动从集合移除。"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    # ================================================================
    # 配置持久化
    # ================================================================

    def save_plugin_config(self, group: str, key: str, value) -> bool:
        """保存配置项到 AstrBotConfig 并持久化到磁盘。"""
        if self._raw_config is None:
            return False
        try:
            # 确保分组存在（AstrBotConfig extends dict，支持直接赋值）
            if group not in self._raw_config:
                self._raw_config[group] = {}
            self._raw_config[group][key] = value

            # 热重载：更新内存中所有配置派生属性
            self.config = QuillConfig(self._raw_config)
            self.kb_max_entries = self.config.kb_max_entries
            self.kb_fallback_top_count = self.config.kb_fallback_top
            self.wb_max_entries = self.config.worldbook_max_dynamic
            self.status_bar_enabled = self.config.status_bar_enabled
            self.status_bar_format_template = self.config.status_bar_format
            self.love_fields = self.config.status_bar_fields
            self.refusal_enabled = self.config.refusal_enabled
            self.refusal_patterns = self.config.refusal_patterns
            self.debug = self.config.debug_enabled
            self.prompt_builder = PromptBuilder(self.config)

            # 同步对话日志配置（供运行时检测）
            self.rag_enable_chat_logging = self.config.rag_enable_chat_logging
            self.rag_chat_log_retention_days = self.config.rag_chat_log_retention_days
            self.worldbook_always_activate = self.config.worldbook_always_activate
            self.panel_theme = self.config.panel_theme
            self.status_bar_plot_paths = self.config.status_bar_plot_paths

            # 持久化到磁盘
            if hasattr(self._raw_config, 'save_config') and callable(self._raw_config.save_config):
                self._raw_config.save_config()
            elif hasattr(self.context, 'save_config'):
                self.context.save_config()
            logger.info(f"[Quill] 配置已保存: {group}.{key} = {value}")
            return True
        except Exception as e:
            logger.warning(f"[Quill] 配置保存失败: {e}")
            return False

    # ================================================================
    # LLM Hooks
    # ================================================================

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """在流式决策前控制流式模式。"""
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
    _STRIP_PATTERNS: list = [
        (re.compile(r'\*\*状态栏\*\*[\s\S]*?```[\s\S]*?```'), ''),
        (re.compile(r'\[LOVE_DATA\]\s*.+'), ''),
        (re.compile(r'\[STATUS\][\s\S]*?\[/STATUS\]'), ''),
        (re.compile(
            r'>>>\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*<<<\s*.+?\s*<<<\s*(?:Select|请选择|选择)\s*>>>',
            re.DOTALL | re.IGNORECASE
        ), ''),
        (re.compile(
            r'(?:^|\n)\s*(?:[-\*\•]*\s*)?(好感度|关系阶段|心情|位置|穿着|当前想法|服从度|发情度)\s*[：:]\s*.+?(?=\n|$)',
            re.MULTILINE
        ), ''),
        (re.compile(r'\[状态栏\][\s\S]*?\[/状态栏\]'), ''),
        (re.compile(r'状态栏[：:][\s\S]*?(?=\n\n|\Z)'), ''),
    ]

    @staticmethod
    def _strip_status_artifacts(text: str) -> str:
        """移除文本中所有状态栏相关痕迹（禁用模式 + dedup 清理）。"""
        if not text:
            return text
        for pattern, replacement in QuillPlugin._STRIP_PATTERNS:
            text = pattern.sub(replacement, text)
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

    async def _handle_status_bar(self, text: str, target_id: str) -> tuple:
        """统一状态栏处理入口。

        返回 (formatted_text: str, updates: dict, handled: bool)。
        handled=True 表示文本中已存在有效状态栏并完成了格式化+持久化。
        handled=False 表示未找到状态栏，调用方应注入兜底。
        """
        updates = {}
        new_text = text
        handled = False

        # 1. **状态栏** code block
        m = _STATUS_BLOCK_RE.search(text)
        if m:
            block_content = m.group(1).strip()
            updates = self._parse_status_block(block_content)
            if updates:
                await self._persist_status_vars(updates, target_id)
                handled = True
                new_text = text  # code block 格式保留原样
                logger.info("[Quill] 状态栏已处理 (code block)")

        # 2. [LOVE_DATA] inline
        if not handled:
            love_updates, love_formatted, raw_line = self._format_love_data(text)
            if love_updates:
                updates = love_updates
                await self._persist_status_vars(updates, target_id)
                new_text = text.replace(raw_line, love_formatted)
                handled = True
                logger.info("[Quill] 状态栏已处理 (LOVE_DATA inline)")

        # 3. [STATUS] legacy
        if not handled:
            m = _STATUS_RE.search(text)
            if m:
                status_content = m.group(1).strip()
                updates = self._parse_legacy_status(status_content)
                await self._persist_status_vars(updates, target_id)
                formatted = self.status_bar_format_template.replace("{content}", status_content)
                new_text = _STATUS_RE.sub(formatted, text)
                handled = True
                logger.info("[Quill] 状态栏已处理 (STATUS legacy)")

        # 4. Raw key:value lines
        if not handled:
            raw_matches = _RAW_STATUS_RE.findall(text)
            if raw_matches:
                current_vars = await self.state_manager.get_session_vars(target_id)
                matched_fields = set()
                parsed_lines = []
                for fn, fv in raw_matches:
                    if fn in self.love_fields and fn not in matched_fields:
                        val = fv.strip()
                        updates[fn] = val
                        parsed_lines.append(f"{fn}：{val}")
                        matched_fields.add(fn)
                for f_name in self.love_fields:
                    if f_name not in matched_fields:
                        val = current_vars.get(f_name, "") or "未设置"
                        updates[f_name] = val
                        parsed_lines.append(f"{f_name}：{val}")
                # 从文本中移除已被解析的 raw 行
                for fn in matched_fields:
                    new_text = re.sub(rf'{re.escape(fn)}\s*[：:].*', '', new_text)
                # 剧情走向
                plot_str = ""
                pm = _PLOT_PATH_RE.search(new_text)
                if pm:
                    plot_content = pm.group(1).strip()
                    new_text = new_text.replace(pm.group(0), "").strip()
                    plot_str = f"\n\n>>> 剧情走向 <<<\n{plot_content}\n<<< 请选择 >>>"
                await self._persist_status_vars(updates, target_id)
                block_content = "\n".join(parsed_lines) + plot_str
                beautiful_bar = self.status_bar_format_template.replace("{content}", block_content)
                new_text = new_text.strip() + "\n\n" + beautiful_bar
                handled = True
                logger.info("[Quill] 状态栏已处理 (raw key:value)")

        # 5. Lenient fallback — 严格正则都失败了但可能 LLM 用了非标准格式
        if not handled:
            lenient_updates = self._lenient_parse_status(text, self.love_fields)
            if lenient_updates and len(lenient_updates) >= 2:
                await self._persist_status_vars(lenient_updates, target_id)
                # 重建为标准格式
                lines = []
                for f_name in self.love_fields:
                    val = lenient_updates.get(f_name, "未设置")
                    lines.append(f"{f_name}：{val}")
                bar = self.status_bar_format_template.replace("{content}", "\n".join(lines))
                new_text = text + "\n\n" + bar
                updates = lenient_updates
                handled = True
                logger.info("[Quill] 状态栏已处理 (lenient fallback)")

        return new_text, updates, handled

    async def _persist_status_vars(self, updates: dict, target_id: str) -> None:
        """Persist parsed status fields to session_vars."""
        if updates:
            await self.state_manager.update_session_vars(target_id, updates)

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

    async def _build_default_love_data(self, target_id: str) -> str:
        """构建默认状态栏（当 LLM 未输出状态栏时兜底）。"""
        vars = await self.state_manager.get_session_vars(target_id)
        parts = []
        for field_name in self.love_fields:
            val = vars.get(field_name, "")
            parts.append(val if val else "未设置")
        love_section = "\n".join(f"{f}：{v}" for f, v in zip(self.love_fields, parts))
        plot_section = "\n\n>>> 剧情走向 <<<\n" + "\n".join(
            f"{i+1}. {p}" for i, p in enumerate(self.status_bar_plot_paths)
        ) + "\n<<< 请选择 >>>"
        full_content = love_section + plot_section
        return f"**状态栏**\n```\n{full_content}\n```"

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
                pass
            if not platform:
                try:
                    platform = (event.get_platform_name() or "").lower()
                except Exception:
                    pass

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

            # 仅对特定平台执行 Markdown 清理
            needs_strip = platform in ("", "telegram", "tg")
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
            if isinstance(messages, list):
                target_id = self._get_target_id(event)
                for msg in messages:
                    if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                        if self.status_bar_enabled:
                            new_text, _, handled = await self._handle_status_bar(msg["text"], target_id)
                            msg["text"] = new_text
                            if handled:
                                event.set_extra("_quill_status_handled", True)
                        else:
                            msg["text"] = self._strip_status_artifacts(msg["text"])
                        break

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
                fm = persona_data.get("core_prompts", {}).get("first_message", "").strip()
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
        """检查激活状态（激活词 / 括号 / KB关键词）。返回 (activated, kb_activated)。"""
        activated = self.activation_detector.should_activate(user_input)
        has_bracket = self.activation_detector.check_brackets(user_input)

        kb_activated = False
        if not (activated or has_bracket) and self.kb_manager:
            try:
                ext = persona_data.get("quill_extensions", {}) if persona_data else {}
                kb_mode = ext.get("kb_mode", "disabled")
                bound_kbs = ext.get("bound_knowledge_base", []) if kb_mode == "custom" else None

                logger.info(f"[Quill] 写作素材库模式: {kb_mode}，绑定分类: {bound_kbs if bound_kbs is not None else 'Auto (全局匹配)'}")

                if kb_mode != "disabled":
                    fetch_count = 7 if bound_kbs is not None else 3
                    matched = await self.kb_manager.match(context_text, top_k=fetch_count, log_match=False)

                    if bound_kbs is not None:
                        matched = [m for m in matched if m.get("category") in bound_kbs]

                    kb_activated = len(matched) > 0
                    if kb_activated:
                        logger.info(f"[Quill] 写作素材库关键词触发激活: {len(matched)} 条匹配 (context)")
                    else:
                        logger.info(f"[Quill] 写作素材库未匹配到内容")
                else:
                    logger.info(f"[Quill] 写作素材库模式: disabled，跳过素材检索")
            except Exception as e:
                logger.warning(f"[Quill] KB 匹配失败: {e}")

        return activated, kb_activated

    async def _run_rag_retrieval(self, event: AstrMessageEvent, req: ProviderRequest, user_input: str, persona_data, dynamic_prompt: str) -> str:
        """执行 RAG 检索（Doc + Memory），返回更新后的 dynamic_prompt。"""
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

            rag_context = self.rag_retriever.format_for_prompt(doc_results, mem_results)
            if rag_context:
                dynamic_prompt += "\n\n" + rag_context
                logger.info(f"[Quill RAG] 注入上下文: {len(rag_context)} 字符")
        except Exception as e:
            logger.warning(f"[Quill RAG] 检索失败: {e}")

        return dynamic_prompt

    async def _rewrite_smt_tool_description(self, req: ProviderRequest) -> None:
        """改写 send_message_to_user 工具描述。状态栏指令通过 system prompt + tail message 注入。"""
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
            logger.info(f"[Quill] 已重写 send_message_to_user 描述")

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求拦截：触发平行宇宙隔离，执行状态栏降级解析与多维 Prompt 组装注入。

        核心职责：
        - 平行宇宙双轴隔离 (target_id::persona_id)：按群+角色切分独立状态
        - 激活检测：决定本次请求是否进入 RP 模式
        - Context Restoration：req.contexts 为空时从 chat_logs 捞取最近 N 条垫入
        - First Message 智能抑制：避免重启后突兀复读开场白
        - 4 层 Prompt 装配：系统/角色/世界书/KB/RAG 多源注入
        - 状态栏降级解析：5 级兜底（STATUS 块→LOVE_DATA→legacy→RAW→lenient）
        """
        try:
            self._restore_smt_tool(req)

            user_input = req.prompt or ""
            target_id = self._get_target_id(event)

            # ── 上下文恢复（重启/滑动窗口切断后无缝续传）──
            mem_session_id = self._get_memory_session_id(
                target_id,
                await self.state_manager.get_persona_id(target_id)
            )
            contexts_is_fresh = not req.contexts or len(req.contexts) <= 1
            if contexts_is_fresh \
                    and getattr(self.config, 'rag_enable_chat_logging', True) \
                    and self.rag_retriever and self.rag_retriever.memory_store:
                recent_logs = await asyncio.to_thread(
                    self.rag_retriever.memory_store.get_recent_chat_logs, mem_session_id, limit=8
                )
                if recent_logs and isinstance(req.contexts, list):
                    req.contexts = recent_logs + req.contexts
                    logger.info(f"[Quill Context] 恢复 {len(recent_logs)} 条上下文（Session: {mem_session_id}）")

            persona_id, persona_data = await self._inject_persona_and_first_message(req, event, target_id)

            # 记录用户消息（仅已绑定角色卡且非指令时）
            if persona_id and user_input and not user_input.strip().startswith("/") \
                    and getattr(self.config, 'rag_enable_chat_logging', True) \
                    and self.rag_retriever:
                self._spawn(self.rag_retriever.log_chat_message(
                    mem_session_id, "user", user_input
                ))

            # 存储最近 6 轮对话，供 /memory learn 自动总结
            if hasattr(req, 'contexts') and isinstance(req.contexts, list):
                recent_msgs = [c for c in req.contexts[-12:] if c.get("role") in ("user", "assistant")]
                event.set_extra("_quill_recent_msgs", recent_msgs)

            # Build multi-turn context for KB matching
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

            activated, kb_activated = await self._check_activation(user_input, context_text, persona_data)
            has_bracket = self.activation_detector.check_brackets(user_input)

            # 全局常驻模式：跳过激活检测
            always_activate = getattr(self.config, "worldbook_always_activate", False)
            if always_activate:
                activated = True
                kb_activated = False

            if not (activated or has_bracket or kb_activated):
                await self.state_manager.reset_quill_rounds(target_id)
                return

            quill_rounds = await self.state_manager.increment_quill_rounds(target_id)
            skip_constants = quill_rounds > 1
            if skip_constants:
                logger.info(f"[Quill] 连续第 {quill_rounds} 轮激活，跳过 Layer 1 常驻")

            # 改写 send_message_to_user 描述（含状态栏强制要求）
            await self._rewrite_smt_tool_description(req)

            if kb_activated and self.kb_manager and self.debug:
                try:
                    debug_match = await self.kb_manager.match(context_text, top_k=10, log_match=False)
                    for e in debug_match:
                        logger.info(
                            f"[Quill] KB 匹配: {e.get('entry_id','')} "
                            f"(score={e.get('match_score',0)}, "
                            f"kw={e.get('keywords',[])})"
                        )
                except Exception:
                    pass

            emergency = await self.state_manager.should_inject_emergency(target_id)

            extra_info = {
                "user_input": user_input,
                "context_text": context_text,
                "persona_id": persona_id,
                "persona_data": persona_data,
                "user_id": target_id,
                "kb_max_entries": self.kb_max_entries,
                "kb_fallback_top_count": self.kb_fallback_top_count,
                "wb_max_entries": self.wb_max_entries,
                "wb_sensitivity": self.config.worldbook_sensitivity,
                "wb_max_token": self.config.worldbook_max_token,
                "skip_constants": skip_constants,
                "session_vars": await self.state_manager.get_session_vars(target_id),
            }

            stable_prompt, dynamic_prompt = await self.prompt_builder.build_system_prompt(
                self.kb_manager, self.wb_manager, extra_info, emergency=emergency
            )

            # ── RAG 检索（Doc RAG + 动态记忆）──
            dynamic_prompt = await self._run_rag_retrieval(event, req, user_input, persona_data, dynamic_prompt)

            # 触发日志注入（show_trigger_log 开启时）
            if (self.config.worldbook_show_log and self.wb_manager
                    and hasattr(self.wb_manager, 'get_trigger_log')):
                trigger_log = await asyncio.to_thread(self.wb_manager.get_trigger_log)
                if trigger_log:
                    log_lines = ["[触发日志]"]
                    for t in trigger_log[:10]:
                        log_lines.append(f"  {t['worldbook']}/{t['title']} ← {','.join(t['matched_keys'])}")
                    dynamic_prompt += "\n\n" + "\n".join(log_lines)

            req.system_prompt = self.prompt_builder.inject_prompt(
                req.system_prompt or "", stable_prompt, dynamic_prompt,
                injection_position=self.config.worldbook_injection_pos
            )

            if persona_id:
                if self.status_bar_enabled:
                    fields_format = " | ".join(f"{{{f}}}" for f in self.love_fields)
                    tail = (
                        "\n\n[System] 本轮回复末尾必须严格按以下格式追加状态栏和剧情选项：\n"
                        f"[LOVE_DATA] {fields_format}\n"
                        f"示例：[LOVE_DATA] 55/100（好感说明） | 朋友 | 放松 | 教室 | 校服 | 希望今天也能见到他...\n"
                        "之后输出：\n"
                        ">>> 剧情走向 <<<\n"
                        "1. 继续当前话题\n"
                        "2. 转换场景\n"
                        "3. 结束互动\n"
                        "<<< 请选择 >>>"
                    )
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

            trigger = "激活词" if activated else ("括号" if has_bracket else "KB关键词")
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
            logger.error(f"[Quill] 致命错误，Prompt 装配失败，降级放行: {e}", exc_info=True)

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

            if self.status_bar_enabled:

                if event.get_extra("_quill_status_handled"):
                    # 工具钩子已处理完毕 — 仅剥离 resp.completion_text 中的
                    # 原始状态栏残留（LLM 可能同时在 content 字段也输出了）
                    content = resp.completion_text or ""
                    stripped = self._strip_status_artifacts(content)
                    if stripped != content:
                        resp.completion_text = stripped
                        logger.info("[Quill] 已剥离 resp.completion_text 中的状态栏残留")
                else:
                    # 工具钩子未命中 — 在此处作为最终安全网处理
                    content = resp.completion_text or ""
                    new_text, _, handled = await self._handle_status_bar(content, target_id)
                    if not handled:
                        persona_id = await self.state_manager.get_persona_id(target_id)
                        if persona_id:
                            default_bar = await self._build_default_love_data(target_id)
                            new_text = (new_text or "") + "\n" + default_bar
                            logger.info("[Quill] 状态栏兜底注入")
                    resp.completion_text = new_text

            else:
                # 禁用模式：彻底擦除所有状态栏痕迹
                content = resp.completion_text or ""
                resp.completion_text = self._strip_status_artifacts(content)

            if not event.get_extra("_quill_activated"):
                return

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
        event.set_extra("_quill_activated", False)

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
                            msgs = []
                    if isinstance(msgs, list):
                        for m in msgs:
                            if isinstance(m, dict) and m.get("type") == "plain" and "text" in m:
                                ai_response += m["text"] + "\n"

                # 存入记忆库（后台任务，异常在done回调中捕获）
                target_id = self._get_target_id(event)
                persona_id = await self.state_manager.get_persona_id(target_id)
                mem_session_id = self._get_memory_session_id(target_id, persona_id)

                # 记录 AI 回复到对话日志（始终保留，供断点续传使用）
                if ai_response.strip() and getattr(self.config, 'rag_enable_chat_logging', True):
                    self._spawn(self.rag_retriever.log_chat_message(
                        mem_session_id, "assistant", ai_response.strip()
                    ))

                # N 轮反思触发：攒够 N 轮对话后生成摘要
                try:
                    unsummarized = await self.state_manager.increment_unsummarized_turns(target_id)

                    if unsummarized >= self.REFLECTION_TURN_THRESHOLD:
                        await self.state_manager.reset_unsummarized_turns(target_id)
                        recent_logs = await asyncio.to_thread(
                            self.rag_retriever.memory_store.get_recent_chat_logs, mem_session_id, limit=self.RECENT_LOG_LIMIT
                        )

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

                        # 顺带跑一次记忆修剪（分档遗忘）— 保留 to_thread 包装，prune_memories 是同步方法
                        if self.rag_retriever.memory_store:
                            self._spawn(asyncio.to_thread(self.rag_retriever.memory_store.prune_memories))
                            # 同步清理过期对话日志（无人值守，避免长期运行服务器日志膨胀）
                            self._spawn(asyncio.to_thread(
                                self.rag_retriever.memory_store.cleanup_chat_logs,
                                getattr(self.config, 'rag_chat_log_retention_days', 30)
                            ))
                    else:
                        logger.debug(f"[Quill Memory] 记忆收集进度: {unsummarized}/4 轮")
                except Exception as e:
                    logger.warning(f"[Quill Memory] 反思调度失败: {e}")

            except Exception as e:
                logger.warning(f"[Quill Memory] 记忆存储调度失败: {e}")

    # ================================================================
    # 用户指令
    # ================================================================

    @filter.command("wb")
    async def cmd_wb(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """世界书管理。用法：/wb | /wb <名字> | /wb off | /wb info <名字> | /wb reload"""
        await _cmds.wb_dispatch(self, event, arg1, arg2)

    @filter.command("char")
    async def cmd_char(self, event: AstrMessageEvent, arg: str = ""):
        """角色卡管理。用法：/char | /char <名字> | /char unset | /char info | /char export | /char import"""
        await _cmds.char_dispatch(self, event, arg)

    @filter.command("quill")
    async def cmd_quill(
        self, event: AstrMessageEvent,
        arg1: str = "", rest: GreedyStr = ""
    ):
        """Quill 系统总览与测试。用法：/quill | /quill help | /quill test <kb|wb|mem> <文字>"""
        arg1_lower = (arg1 or "").strip().lower()
        if arg1_lower == "help":
            await _cmds.quill_help(event)
            return
        if arg1_lower == "test":
            text = (rest or "").strip()
            # 解析: /quill test kb <文字> 或 /quill test <文字>
            parts = text.split(None, 1) if text else []
            if len(parts) >= 2 and parts[0].lower() in ("kb", "wb", "mem"):
                system = parts[0]
                test_text = parts[1]
            else:
                system = "kb"
                test_text = text
            if not test_text:
                from astrbot.core.message.message_event_result import MessageEventResult
                event.set_result(MessageEventResult().message("用法: /quill test <kb|wb|mem> <文字>"))
                return
            await _cmds.quill_test(self, event, system, test_text)
            return
        await _cmds.quill_status(self, event)

    @filter.command("memory")
    async def cmd_memory(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """动态记忆管理。用法：/memory | /memory list | /memory del <序号> | /memory clear | /memory learn <内容> | /memory search <关键词>"""
        await _cmds.memory_dispatch(self, event, arg1, arg2)

    @filter.command("doc")
    async def cmd_doc(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """外部文档 RAG 管理。用法：/doc list | /doc search <关键词>"""
        await _cmds.doc_dispatch(self, event, arg1, arg2)

    @filter.command("stream")
    async def cmd_stream(self, event: AstrMessageEvent, arg: str = ""):
        """流式模式控制。用法：/stream on|off|auto"""
        await _cmds.stream_dispatch(self, event, arg)

    @register_command("reinject", alias={"重新注入"})
    async def cmd_reinject(self, event: AstrMessageEvent):
        """强制重置注入状态，下次激活重新注入全部常驻素材。用法：/reinject"""
        await _cmds.reinject_dispatch(self, event)
