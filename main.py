# -*- coding: utf-8 -*-
"""QuillPlugin — 羽笔 v5.0 世界书+写作素材库+角色卡 RP 注入系统

Integration glue: wires together activation detection, state management,
knowledge base, worldbook, prompt building, commands, and web routes.

v5.0 变化:
- 配置由 AstrBot 通过 _conf_schema.json 注入（不再读 config.yaml）
- 全部行为由 QuillConfig 控制
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


def strip_markdown(text: str) -> str:
    """Remove common Markdown formatting, leaving clean plain text."""
    if not text:
        return text
    for pattern, replacement in _MD_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@register(
    "astrbot_plugin_quillplus",
    "Quill",
    "羽笔 - 世界书+写作素材库+角色卡 RP 注入系统",
    "5.0.0",
    "",
)
class QuillPlugin(Star):
    """羽笔 — 世界书+写作素材库+角色卡三合一 RP 注入系统"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._raw_config = config  # AstrBotConfig 实例（支持 save_config）
        self.config = QuillConfig(config)
        self.plugin_dir = os.path.dirname(__file__)

        # --- Activation ---
        activation_path = os.path.join(self.plugin_dir, "activation_triggers.yaml")
        self.activation_detector = ActivationDetector(activation_path)

        # --- State ---
        self.state_manager = StateManager(max_users=500)

        # --- Knowledge Base (deferred to initialize()) ---
        self.kb_manager = None
        self.kb_max_entries = self.config.kb_max_entries
        self.kb_fallback_top_count = self.config.kb_fallback_top

        # --- Worldbook ---
        wb_dir = os.path.join(self.plugin_dir, "worldbooks")
        try:
            self.wb_manager = WorldbookManager(
                wb_dir,
                active_worldbooks=self.config.worldbook_active
            )
            wb_names = self.wb_manager.list_worldbooks()
            active = self.config.worldbook_active
            logger.info(f"[Quill] 世界书已加载: {len(wb_names)} 个 - {wb_names}"
                        f" (活跃: {active if active else '全部'})")
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
        """Async initialization — KB init + web route registration."""
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
            self.rag_vector_store = FaissVectorStore(rag_db, rag_idx, dim=self.rag_embedding.get_dim())

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
        """Cleanup — close KB connection."""
        if self.kb_manager:
            try:
                await self.kb_manager.close()
            except Exception as e:
                logger.warning(f"[Quill] 写作素材库关闭失败: {e}")
        logger.info("[Quill] 插件已停用")

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
            user_id = str(event.get_sender_id())
        except Exception:
            return

        # 拦截 /reinject 和 /重新注入
        if user_input.strip() in ("/reinject", "/重新注入"):
            await self.state_manager.reset_quill_rounds(user_id)
            logger.info("[Quill] /reinject 已重置 quill_rounds")
            from astrbot.core.message.message_event_result import MessageEventResult
            event.set_result(MessageEventResult().message(
                "已重置注入状态。下次触发 Quill 时将重新注入全部常驻素材。"
            ))
            return

        # 读取用户流式偏好
        state = await self.state_manager.get_state(user_id)

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

    async def _persist_status_vars(self, updates: dict, user_id: str) -> None:
        """Persist parsed status fields to session_vars."""
        if updates:
            await self.state_manager.update_session_vars(user_id, updates)
            logger.info(f"[Quill] 状态栏持久化: {len(updates)} 个字段")

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

    # ── FunctionTool 描述泄漏防护 ─────────────────────────────
    _QUILL_SMT_DESC_MARKER = "MUST use this tool to send ALL reply text"

    @classmethod
    def _restore_smt_tool(cls, req: ProviderRequest) -> None:
        """若 send_message_to_user 描述被 Quill 改写过，恢复原件。"""
        if not req or not req.func_tool or req.func_tool.empty():
            return
        tool = req.func_tool.get_tool("send_message_to_user")
        if tool is None:
            return
        if cls._QUILL_SMT_DESC_MARKER not in (tool.description or ""):
            return
        orig = getattr(tool, "_quill_orig_description", None)
        if orig is not None:
            tool.description = orig
            try:
                delattr(tool, "_quill_orig_description")
            except AttributeError:
                pass

    @filter.on_using_llm_tool(priority=200)
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None
    ):
        """工具调用前拦截 — 在 Telegram 平台剥离 Markdown 标记。"""
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

        needs_strip = platform in ("", "telegram", "tg")
        if not needs_strip:
            return

        logger.info(f"[Quill] >>> send_message_to_user 调用 (platform={platform or '?'}), 清理 Markdown...")

        messages = tool_args.get("messages", [])
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
                tool_args["messages"] = messages
            except (json.JSONDecodeError, TypeError):
                return

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

        # --- 状态栏提取 + 格式化 ---
        if self.status_bar_enabled and isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict) and msg.get("type") == "plain" and "text" in msg:
                    text = msg["text"]
                    user_id = str(event.get_sender_id())
                    love_updates, love_formatted, raw_line = self._format_love_data(text)
                    if love_updates:
                        await self._persist_status_vars(love_updates, user_id)
                        msg["text"] = text.replace(raw_line, love_formatted)
                        break
                    status_match = _STATUS_RE.search(text)
                    if status_match:
                        status_content = status_match.group(1).strip()
                        await self._persist_status_vars(
                            self._parse_legacy_status(status_content), user_id
                        )
                        formatted = self.status_bar_format_template.replace(
                            "{content}", status_content
                        )
                        msg["text"] = _STATUS_RE.sub(formatted, text)
                        break

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求拦截 — 检测激活并注入 System Prompt。"""
        try:
            self._restore_smt_tool(req)

            user_input = req.prompt or ""
            user_id = str(event.get_sender_id())

            # 从 Quill 自身状态读取角色 ID（而非 AstrBot 原生）
            persona_id = await self.state_manager.get_persona_id(user_id)
            # 切断 AstrBot 原生人格注入，防止双重 Prompt 污染
            if req.conversation:
                req.conversation.persona_id = "[%None]"

            # 注入开场白 (first_message) 作为首条伪造历史记录
            persona_data = None
            if persona_id and self.persona_manager:
                persona_data = await self.persona_manager.get_persona(persona_id)
                if persona_data:
                    fm = persona_data.get("core_prompts", {}).get("first_message", "").strip()
                    if fm:
                        state = await self.state_manager.get_state(user_id)
                        if not state.first_message_injected:
                            if hasattr(req, 'contexts') and isinstance(req.contexts, list):
                                req.contexts.insert(0, {"role": "assistant", "content": fm})
                                logger.info(f"[Quill] 已注入 {persona_data.get('name', persona_id)} 的开场白 (first_message)")
                            await self.state_manager.mark_first_message_injected(user_id)

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

            activated = self.activation_detector.should_activate(user_input)
            has_bracket = self.activation_detector.check_brackets(user_input)

            # 写作素材库关键词触发
            kb_activated = False
            if not (activated or has_bracket) and self.kb_manager:
                try:
                    # 获取角色绑定的知识库分类
                    bound_kbs = []
                    if persona_data:
                        bound_kbs = persona_data.get("quill_extensions", {}).get("bound_knowledge_base", [])

                    # 取多一点候选池，防止过滤后不够
                    fetch_count = 15 if bound_kbs else 3
                    matched = await self.kb_manager.match(context_text, top_k=fetch_count, log_match=False)

                    if bound_kbs:
                        matched = [m for m in matched if m.get("category") in bound_kbs]

                    kb_activated = len(matched) > 0
                    if kb_activated:
                        logger.info(f"[Quill] 写作素材库关键词触发激活: {len(matched)} 条匹配 (context)")
                except Exception as e:
                    logger.warning(f"[Quill] KB 匹配失败: {e}")

            if not (activated or has_bracket or kb_activated):
                await self.state_manager.reset_quill_rounds(user_id)
                return

            quill_rounds = await self.state_manager.increment_quill_rounds(user_id)
            skip_constants = quill_rounds > 1
            if skip_constants:
                logger.info(f"[Quill] 连续第 {quill_rounds} 轮激活，跳过 Layer 1 常驻")

            # 改写 send_message_to_user 描述
            if req.func_tool and not req.func_tool.empty():
                smt_tool = req.func_tool.get_tool("send_message_to_user")
                if smt_tool and self._QUILL_SMT_DESC_MARKER not in (smt_tool.description or ""):
                    smt_tool._quill_orig_description = smt_tool.description
                    smt_tool.description = (
                        "Send message to the user. "
                        "IMPORTANT: You MUST use this tool to send ALL reply text. "
                        "Do NOT output text directly in the content field — it will be blocked by the content filter. "
                        "Call this tool IMMEDIATELY — do NOT call any other tools (file_read, shell, etc.) before sending. "
                        "Supports various message types including plain, image, record, video, file, and mention_user."
                    )
                    logger.info(f"[Quill] 已重写 send_message_to_user 描述")

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

            emergency = await self.state_manager.should_inject_emergency(user_id)

            extra_info = {
                "user_input": user_input,
                "context_text": context_text,
                "persona_id": persona_id,
                "persona_data": persona_data,
                "user_id": user_id,
                "kb_max_entries": self.kb_max_entries,
                "kb_fallback_top_count": self.kb_fallback_top_count,
                "wb_max_entries": self.wb_max_entries,
                "wb_sensitivity": self.config.worldbook_sensitivity,
                "wb_max_token": self.config.worldbook_max_token,
                "skip_constants": skip_constants,
                "session_vars": await self.state_manager.get_session_vars(user_id),
            }

            stable_prompt, dynamic_prompt = await self.prompt_builder.build_system_prompt(
                self.kb_manager, self.wb_manager, extra_info, emergency=emergency
            )

            # ── RAG 检索（Doc RAG + 动态记忆）──
            if self.rag_retriever and self.rag_retriever.embedding:
                try:
                    session_id = str(event.unified_msg_origin) if hasattr(event, 'unified_msg_origin') else user_id
                    doc_results = await self.rag_retriever.search_documents(user_input)
                    mem_results = await self.rag_retriever.search_memories(session_id, user_input)
                    rag_context = self.rag_retriever.format_for_prompt(doc_results, mem_results)
                    if rag_context:
                        dynamic_prompt += "\n\n" + rag_context
                        if self.debug:
                            logger.info(f"[Quill RAG] 注入 {len(doc_results)} 文档 + {len(mem_results)} 记忆")
                except Exception as e:
                    logger.warning(f"[Quill RAG] 检索失败: {e}")

            # 触发日志注入（show_trigger_log 开启时）
            if (self.config.worldbook_show_log and self.wb_manager
                    and hasattr(self.wb_manager, 'get_trigger_log')):
                trigger_log = self.wb_manager.get_trigger_log()
                if trigger_log:
                    log_lines = ["[触发日志]"]
                    for t in trigger_log[:10]:
                        log_lines.append(f"  {t['worldbook']}/{t['title']} ← {','.join(t['matched_keys'])}")
                    dynamic_prompt += "\n\n" + "\n".join(log_lines)

            req.system_prompt = self.prompt_builder.inject_prompt(
                req.system_prompt or "", stable_prompt, dynamic_prompt,
                injection_position=self.config.worldbook_injection_pos
            )

            event.set_extra("_quill_activated", True)

            await self.state_manager.update_activity(user_id)
            await self.state_manager.clear_refusal(user_id)

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
        """LLM 响应拦截 — Base64 解密 + 拒绝检测。"""
        try:
            # [B:...] Base64 解码——安全网
            text = resp.completion_text or ""
            if text:
                decrypted = decrypt_output(text)
                if decrypted != text:
                    resp.completion_text = decrypted
                    logger.info(f"[Quill] 解密 [B:...]: {len(text)} -> {len(decrypted)}")

            # 状态栏解析 + 格式化
            if self.status_bar_enabled:
                content = resp.completion_text or ""
                user_id = str(event.get_sender_id())

                love_updates, love_formatted, raw_line = self._format_love_data(content)
                if love_updates:
                    await self._persist_status_vars(love_updates, user_id)
                    resp.completion_text = content.replace(raw_line, love_formatted)
                    logger.info("[Quill] LOVE_DATA 状态栏已格式化")
                else:
                    status_match = _STATUS_RE.search(content)
                    if status_match:
                        status_content = status_match.group(1).strip()
                        await self._persist_status_vars(
                            self._parse_legacy_status(status_content), user_id
                        )
                        formatted = self.status_bar_format_template.replace(
                            "{content}", status_content
                        )
                        resp.completion_text = _STATUS_RE.sub(formatted, content)
                        logger.info(f"[Quill] STATUS 状态栏已格式化: {status_content[:40]}...")

            if not event.get_extra("_quill_activated"):
                return

            if not self.refusal_enabled:
                return

            scan_text = resp.completion_text or ""
            if not scan_text:
                return

            for pattern in self.refusal_patterns:
                if pattern in scan_text:
                    user_id = str(event.get_sender_id())
                    await self.state_manager.mark_refusal(user_id)
                    logger.info(f"[Quill] 检测到拒绝模式 '{pattern}' (user={user_id})")
                    break
        except Exception as e:
            logger.error(f"[Quill] on_llm_response 后处理遭遇未捕获异常，已降级放行: {e}", exc_info=True)

    @filter.on_llm_tool_respond(priority=10)
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool: FunctionTool,
        tool_args: dict | None, tool_result
    ):
        """工具调用后拦截 — 检测 send_message_to_user 调用，停止 agent loop。"""
        if tool.name != "send_message_to_user":
            return

        if not event.get_extra("_quill_activated"):
            return

        logger.info("[Quill] send_message_to_user 已调用")
        event.set_extra("_quill_activated", False)

        # 单次打包模式：无条件强制停止 agent loop（防止第二轮废话）
        event.set_extra("agent_stop_requested", True)
        logger.info("[Quill] 请求停止 agent loop（单次打包模式已强制启用）")

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
                if user_input and ai_response and len(ai_response) > 50:
                    session_id = str(event.unified_msg_origin) if hasattr(event, 'unified_msg_origin') else str(event.get_sender_id())
                    task = asyncio.create_task(
                        self.rag_retriever.store_memory(session_id, user_input, ai_response.strip())
                    )
                    task.add_done_callback(lambda t: t.exception() and logger.warning(f"[Quill Memory] 后台记忆存储异常: {t.exception()}"))
            except Exception as e:
                logger.warning(f"[Quill Memory] 记忆存储调度失败: {e}")

    # ================================================================
    # 用户指令
    # ================================================================

    @filter.command("wb")
    async def cmd_wb(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """世界书管理。用法：/wb | /wb <名字> | /wb off | /wb info <名字>"""
        await _cmds.wb_dispatch(self, event, arg1, arg2)

    @filter.command("char")
    async def cmd_char(self, event: AstrMessageEvent, arg: str = ""):
        """角色卡切换。用法：/char | /char <名字> | /char unset"""
        await _cmds.char_dispatch(self, event, arg)

    @filter.command("quill")
    async def cmd_quill(
        self, event: AstrMessageEvent,
        arg1: str = "", rest: GreedyStr = ""
    ):
        """Quill 状态 / KB 匹配测试。用法：/quill | /quill test <文字>"""
        if (arg1 or "").strip().lower() == "test":
            text = (rest or "").strip()
            if not text:
                from astrbot.core.message.message_event_result import MessageEventResult
                event.set_result(MessageEventResult().message("用法: /quill test <文字>"))
                return
            await _cmds.quill_test(self, event, text)
            return
        await _cmds.quill_status(self, event)

    @filter.command("stream")
    async def cmd_stream(self, event: AstrMessageEvent, arg: str = ""):
        """流式模式控制。用法：/stream on|off|auto"""
        await _cmds.stream_dispatch(self, event, arg)

    @register_command("reinject", alias={"重新注入"})
    async def cmd_reinject(self, event: AstrMessageEvent):
        """强制重置注入状态，下次激活重新注入全部常驻素材。用法：/reinject"""
        await _cmds.reinject_dispatch(self, event)
