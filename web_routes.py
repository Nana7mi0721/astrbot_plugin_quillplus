# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quill Web API routes — AstrBot v4.26+ register_web_api 模式。

所有 handler 使用 astrbot.api.web 的请求/响应抽象，
与 FastAPI/Quart/Starlette 底层实现解耦。

_route_core.py 提供纯 async handler（零 HTTP 依赖），
本文件只做 HTTP ↔ 核心逻辑的适配。
"""

_ALLOWED_CONFIG_KEYS: set = {
    # rag
    ("rag", "embedding_provider_id"), ("rag", "rerank_provider_id"),
    ("rag", "llm_provider_id"), ("rag", "enable_local_embedding"),
    ("rag", "chunk_size"), ("rag", "chunk_overlap"),
    ("rag", "top_k"), ("rag", "dense_top_k"),
    ("rag", "enable_memory"), ("rag", "enable_chat_logging"),
    ("rag", "enable_autonomous_reflection"),
    ("rag", "chat_log_retention_days"),
    ("rag", "astrbot_kb_enabled"),
    # worldbook
    ("worldbook", "enabled"), ("worldbook", "max_dynamic_entries"),
    ("worldbook", "max_token_limit"), ("worldbook", "match_sensitivity"),
    ("worldbook", "injection_position"), ("worldbook", "show_trigger_log"),
    ("worldbook", "always_activate"), ("worldbook", "card_embed_worldbook"),
    # writing_resource
    ("writing_resource", "enabled"), ("writing_resource", "max_entries"),
    ("writing_resource", "fallback_top_count"), ("writing_resource", "category_dedup_limit"),
    # performance
    ("performance", "max_prompt_length"), ("performance", "min_output_length"),
    ("performance", "max_output_length"),
    # status_bar
    ("status_bar", "enabled"), ("status_bar", "fields"),
    ("status_bar", "format_template"), ("status_bar", "plot_paths"),
    ("status_bar", "llm_extract"), ("status_bar", "llm_provider_id"),
    ("status_bar", "default_placeholder"), ("status_bar", "show_delta"),
    ("status_bar", "format_template_plain"), ("status_bar", "plain_platforms"),
    # status_bar / Decision 决策模式（结构化判定：推荐选择度 + 分支路由）
    ("status_bar", "decision_enabled"), ("status_bar", "decision_base_url"),
    ("status_bar", "decision_api_key"), ("status_bar", "decision_model"),
    ("status_bar", "decision_api_format"), ("status_bar", "decision_confidence_floor"),
    # debug
    ("debug", "enabled"), ("debug", "show_inject_report"),
    # trace（M1a 注入诊断）
    ("trace", "enabled"), ("trace", "buffer_per_session"),
    # M2 用量/缓存
    ("usage", "enabled"),
    ("usage", "display_currency"), ("usage", "usd_cny_rate"),
    ("compression", "enabled"), ("compression", "max_tokens"), ("compression", "provider_id"),
    ("usage", "retention_days"),
    ("usage", "max_records"),
    ("usage", "prices_json"),
    ("cache", "enabled"),
    ("cache", "dynamic_to_user_tail"),
    ("cache", "stable_sampling"),
    ("cache", "sampling_window"),
    # permissions
    ("permissions", "admin_users"),
}

import asyncio
import os
import tempfile
from functools import wraps
from pathlib import Path
from urllib.parse import quote


# 文件名 URL 编码（safe='' 确保 / 等字符也被编码，避免注入下载头）
def _urlquote(name: str) -> str:
    return quote(str(name), safe='')

from astrbot.api.web import (
    error_response,
    json_response,
    request,
)

# 字节流响应（file_response 仅支持文件路径，不适用内存字节）
from starlette.responses import Response

from astrbot.api import logger

from ._backup_util import (
    BackupTooLarge,
    build_backup_zip,
    is_sqlite_bytes,
    remove_sidecars,
)
from ._paths import backup_sources, resolve_archive_dest
from .quill.core.errors import QuillError, StorageError
from .quill.core.storage_stats import note_storage_error

from ._route_core import (
    error_text,
    handle_wr_list,
    handle_wr_get,
    handle_wr_create,
    handle_wr_update,
    handle_wr_delete,
    handle_wr_toggle,
    handle_wr_export,
    handle_wr_import,
    handle_wr_test,
    handle_wr_categories,
    handle_wr_batch_delete,
    handle_wr_batch_toggle,
    handle_wb_list,
    handle_wb_get,
    handle_wb_create,
    handle_wb_delete,
    handle_wb_entry_create,
    handle_wb_entry_update,
    handle_wb_entry_delete,
    handle_wb_import_st,
    handle_wb_export_st,
    handle_rag_upload,
    handle_rag_documents,
    handle_rag_kb_documents,
    handle_rag_group_action,
    handle_rag_delete,
    handle_rag_search,
    handle_rag_config,
    handle_memory_list,
    handle_memory_delete,
    handle_memory_list_all,
    handle_memory_stats,
    handle_memory_get,
    handle_memory_prune,
    handle_provider_list,
    handle_memory_export,
    handle_memory_import,
    handle_chat_log_list,
    handle_chat_log_export,
)

# M3.1 统一上传通道：文件读取 / base64 消歧 / 大小上限 / 扩展名集合的唯一实现
from .interfaces.web.upload import (
    BINARY_EXTS,
    CARD_IMAGE_EXTS,
    CARD_IMPORT_EXTS,
    CARD_LIMIT,
    DEFAULT_LIMIT,
    UploadError,
    UploadTooLarge,
    _declared_length,
    decode_base64,
    read_upload,
)

# AstrBot 人格 → 角色卡（只读人格、用摘要模型做语义切分，见 persona_import.py）
from .persona_import import (
    CARD_NAME_MAX,
    AstrBotPersonaSource,
    PersonaCardConverter,
    PersonaImportError,
)

PLUGIN_NAME = "astrbot_plugin_quillplus"


def _clip_name(name: str, limit: int = CARD_NAME_MAX) -> str:
    """角色名截断（与 persona_import.CARD_NAME_MAX 同口径）。"""
    name = (name or "").strip()
    return name if len(name) <= limit else name[:limit]


#: 备份恢复包体积上限（M6.2）。备份只含插件数据根（DB + JSON + 头像），
#: 512MB 已远超正常规模；上限存在的意义是挡住「用一个高压缩比 zip 撑爆内存
#: 与事件循环」的构造请求。
_MAX_RESTORE_BYTES = 512 * 1024 * 1024

#: M6 修复：备份恢复的流式拷贝块大小。1MB 在「不整条进内存」与「系统调用次数」
#: 之间取平衡；单独提出来是为了让上限检查的粒度与块大小解耦。
_RESTORE_CHUNK_BYTES = 1024 * 1024


class _BytesUpload:
    """bytes → upload-like 适配：handle_rag_upload 只需 ``async read()``。

    （原 rag_upload / rag_upload_base64 内各自定义的 _BytesUpload/DummyFile
    收敛为此处唯一实现。）
    """

    def __init__(self, content: bytes) -> None:
        self._content = content

    async def read(self) -> bytes:
        return self._content


async def _json_body() -> dict:
    """安全地读取 JSON 请求体，保证返回 dict。

    request.json(default={}) 只在「解析失败」时回退默认值：如果请求体本身是合法
    JSON 但顶层不是对象（字符串 / 数字 / 数组，例如直接 POST 一个 "abc"），它会
    原样返回，随后 data.get(...) 就抛 AttributeError，被 _api_handler 兜成 500。
    这里统一收敛成 dict，非对象一律当空对象处理（后续的必填校验会给出 400）。
    """
    data = await request.json(default={})
    return data if isinstance(data, dict) else {}


#: H2 修复：会改变插件状态的 handler 名单（写/删/导入/恢复/开关）。
#:
#: 为什么用名单而不是按 HTTP 方法判定：路由注册表里的方法（GET/POST）与实际语义
#: 并不对应——`wb/get`、`wr/get`、`memory/vector_search`、`wr/test` 都是 POST 但只读，
#: 而 `backup/export` 是 GET。按名字点名才是唯一可靠的判据。
#: 新增写/删类 handler 时必须同时登记到这里，否则该路由不设防。
_MUTATING_ROUTES: frozenset = frozenset({
    "config_save", "config_save_batch",
    "stream_set_all", "state_cleanup",
    "rag_upload", "rag_delete", "rag_groups",
    "memory_import", "memory_prune", "memory_delete", "memory_pin",
    "wr_create", "wr_update", "wr_delete", "wr_toggle", "wr_import",
    "wr_batch_delete", "wr_batch_toggle",
    "wb_create", "wb_delete", "wb_entry_create", "wb_entry_update",
    "wb_entry_delete", "wb_import_st", "wb_import_json", "wb_reload",
    "persona_create", "persona_update", "persona_delete", "upload_avatar",
    "persona_import", "persona_import_text", "persona_astrbot_import",
    "backup_restore", "backup_restore_base64",
    # Q1：连通性测试**会真实发出站外请求**（可能携带已保存的判定密钥），
    # 是一等一的「借用户之手打内网 / 外带凭据」的入口，必须与写操作同级设防。
    "decision_probe",
    "task_list", "task_detail", "task_cancel", "scope_resolve", "schema_status",
    "memory_progress", "memory_summarize_task", "memory_import_task",
    "groups_list", "groups_preview", "groups_save", "groups_archive", "groups_recent", "groups_actors",
    "usage_stats", "usage_clear",  # 全会话元数据也需要管理身份。
})

# 新增写入口默认要求管理身份；只有这些既有 POST 查询明确按只读处理。
_READ_ONLY_POST_ROUTES = frozenset({'wr_get','wr_test','wb_get','rag_search','memory_vector_search','trace_simulate'})

#: H2 修复：AstrBot 用该前缀把「API Key 调用」与「Dashboard 登录会话」区分开
#: （框架 `astrbot.core.workspace.API_KEY_USERNAME_PREFIX`，由
#: `dashboard/api/auth.py::_require_api_key_scope` 拼进 AuthContext.username）。
#: 这里复制字面量而不 import `astrbot.core.*`，是为了不新增合规债（见 C1）；
#: 下方 `_request_carries_api_key_marker()` 同时按请求头识别，即使该前缀将来变动
#: 也不会静默放行 API Key。
_API_KEY_USERNAME_PREFIX = "api_key:"


def _request_carries_api_key_marker() -> bool:
    """请求是否自带 API Key 凭据（与框架 `_extract_raw_api_key` 同判据）。

    只认「只有 API Key 才会出现」的形态：`Authorization: ApiKey`、`X-API-Key`、
    `?api_key=`、`?key=`。`Authorization: Bearer` 不算——面板降级路径正是用
    Bearer 传 Dashboard JWT，那属于登录会话。
    """
    try:
        headers = request.headers
        if (headers.get("Authorization", "") or "").strip().startswith("ApiKey "):
            return True
        if (headers.get("X-API-Key", "") or "").strip():
            return True
        query = request.query
        getter = getattr(query, "get", None)
        if callable(getter):
            if (getter("api_key") or "").strip():
                return True
            if (getter("key") or "").strip():
                return True
    except Exception:
        # 取不到请求元数据时按「不是 API Key」处理：身份判定仍由 username 兜底，
        # 这里只是加固，不该因为它失败而拒绝正常面板请求。
        return False
    return False


def _api_handler(handler):
    """统一的 handler 异常捕获装饰器。

    未捕获异常转为 500 error_response，避免向前端泄露堆栈。

    D11：`except QuillError` 分支让受控异常体系真正生效（此前 `status_code`
    与 `to_payload` 零调用，所有失败一律 500——参数/冲突类错误也报成服务端故障，
    还陪一条 logger.exception 噪音）。这里只回 `message`（类文档约定它是对用户
    可见的中文摘要，`detail` 才是不外泄的内部上下文）。

    H2：写/删类 handler 在进入业务逻辑之前先过管理校验（见 `_MUTATING_ROUTES`）。
    放在这一层是因为 73 条路由**全部**经过它，是唯一没有漏网可能的收口点。
    """
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        name = getattr(handler, "__name__", "")
        if name in _MUTATING_ROUTES or (request.method.upper() not in ('GET','HEAD','OPTIONS') and name not in _READ_ONLY_POST_ROUTES):
            owner = args[0] if args else None
            guard = getattr(owner, "_require_admin_request", None)
            if not callable(guard):
                # 理论上不可达（这些 handler 都是 QuillRoutes 的方法）。真出现
                # 说明装饰器被用在了别处，宁可拒绝也不放行。
                return error_response("拒绝访问：写操作缺少鉴权入口。", status_code=403)
            denied = await guard(handler.__name__)
            if denied:
                return error_response(denied, status_code=403)
        try:
            return await handler(*args, **kwargs)
        except QuillError as e:
            logger.warning(
                "[Quill Web] handler 受控失败 (%s): %s",
                getattr(handler, "__name__", "?"), e,
            )
            return error_response(e.message, status_code=e.status_code)
        except Exception as e:
            # P1-1 修复：记录完整异常日志，前端仅返回通用错误，避免泄漏内部信息
            logger.exception("[Quill Web] handler 异常 (%s): %s", getattr(handler, "__name__", "?"), e)
            return error_response("服务器内部错误，请查看服务端日志", status_code=500)
    return wrapper


class QuillRoutes:
    """Quill 插件后端 Web API 路由注册器。

    用法::

        QuillRoutes(wr_manager, wb_manager, context).register_all()
    """

    def __init__(self, wr_manager, wb_manager, context, config=None,
                 rag_components=None, plugin=None, persona_manager=None):
        self.wr_manager = wr_manager
        self.wb_manager = wb_manager
        self.context = context
        self.config = config
        self.plugin = plugin  # 直接引用插件实例，避免反射查找
        self.persona_manager = persona_manager
        # rag_components: dict with keys: embedding, vector_store, reranker, memory_store, summarizer
        self.rag = rag_components or {}
        # 备份导出与恢复互斥：此前两个并发恢复会各自解压/重建组件，互相覆盖文件
        # 与路由引用，没有任何保护。
        self._maintenance_lock = asyncio.Lock()

    # ── H2：Web 层管理校验 ────────────────────────────────────

    def _admin_users(self) -> list:
        """读取当前 admin_users 白名单（与群聊指令同一份配置）。

        `QuillConfig` 已把原始文本解析成 list（config.py 的 `admin_users`），
        这里只在它仍是字符串/其他形态时兜底，不做二次正则解析。
        """
        plugin = getattr(self, "plugin", None)
        cfg = getattr(plugin, "config", None) or getattr(self, "config", None)
        raw = getattr(cfg, "admin_users", None)
        if isinstance(raw, (list, tuple, set)):
            return [str(u).strip() for u in raw if str(u).strip()]
        if isinstance(raw, str):
            parts = []
            for chunk in raw.replace("|", ",").replace("\n", ",").split(","):
                chunk = chunk.strip()
                if chunk:
                    parts.append(chunk)
            return parts
        return []

    async def _require_admin_request(self, route: str) -> str | None:
        """H2：写/删类 Web 路由的管理校验。返回 None 放行，否则返回拒绝文案。

        背景：插件的 73 条路由此前零插件级校验。面板走的 `/api/plug/*` 那条要求
        Dashboard JWT（安全），但 `/api/v1/plugins/extensions/*` 只要求 `plugin`
        作用域，而框架 `_require_api_key_scope` 只比对 key hash 与 scopes、**从不
        校验密码** —— 一枚 `plugin` 作用域的 API Key 就能无密码覆盖全部 SQLite
        （`backup/restore_base64`）、改写含权限表的配置（`config/save_batch`）。

        判据：框架已经把身份注入了 `astrbot.api.web.request.username` ——
        Dashboard 登录会话是登录用户名，API Key 调用是 `api_key:<key_id>`
        （`dashboard/api/auth.py` 拼 `API_KEY_USERNAME_PREFIX`）。因此：

        - 身份缺失 → 拒绝（`/api/plug/*` 与 `/api/v1/*` 都必然带凭据，取不到
          说明分派链路异常，不能默认放行）。
        - API Key 调用 → **默认拒绝**（fail-close）。要放行必须把该 Key 的完整
          身份串 `api_key:<key_id>` 显式填进 `admin_users`，属于知情选择。
        - Dashboard 登录会话 → 放行。它等同于「已通过 Dashboard 密码认证」，
          与框架其余管理面同一信任级；这里再要求它出现在 `admin_users` 会误伤
          只配了聊天平台 UID 的用户，把面板写操作全部锁死。
        """
        try:
            username = (getattr(request, "username", None) or "").strip()
        except Exception:
            username = ""
        if not username:
            logger.warning("[Quill Web] 拒绝 %s：无法确认请求身份（无 Dashboard 会话，也无 API Key）", route)
            return "拒绝访问：无法确认请求身份，请从面板登录后操作。"
        is_api_key = username.startswith(_API_KEY_USERNAME_PREFIX)
        if not is_api_key and _request_carries_api_key_marker():
            # 前缀变了（框架改过常量）但请求确实带 API Key → 仍按 API Key 处理。
            is_api_key = True
        if not is_api_key:
            return None
        allowed = self._admin_users()
        if username in allowed:
            return None
        if not allowed:
            logger.warning(
                "[Quill Web] 拒绝 %s：API Key 调用且 admin_users 未配置（fail-close）。"
                "如需放行，请在插件配置面板 → 权限 → admin_users 中填入该 Key 的身份串。",
                route,
            )
            return (
                "拒绝访问：API Key 无权执行写操作。\n"
                "当前 admin_users 未配置。如确需用 API Key 调用，请在插件配置面板 → "
                "权限 → admin_users 中填入该 Key 的身份串（可在 Dashboard 的 API Key "
                "列表查看其 ID，形如 api_key:<ID>）。"
            )
        logger.warning("[Quill Web] 拒绝 %s：API Key 身份不在 admin_users 白名单内", route)
        return "拒绝访问：该 API Key 不在 admin_users 白名单内。"

    # ── 路由注册入口 ──────────────────────────────────────────

    def register_all(self):
        """一次性注册全部 Web API 路由（包含前端 panel 调用的全部端点）。"""
        _r = self.context.register_web_api

        # ── WR 写作素材库 (11 个) ──
        _r(f"/{PLUGIN_NAME}/wr/list",         self.wr_list,        ["GET"],    "列出写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/get",          self.wr_get,         ["POST"],   "获取单个写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/create",       self.wr_create,      ["POST"],   "创建写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/update",       self.wr_update,      ["POST"],   "更新写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/delete",       self.wr_delete,      ["POST"],   "删除写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/toggle",       self.wr_toggle,      ["POST"],   "启用/禁用写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/export",       self.wr_export,      ["GET"],    "导出写作素材库")
        _r(f"/{PLUGIN_NAME}/wr/import",       self.wr_import,      ["POST"],   "导入写作素材库")
        _r(f"/{PLUGIN_NAME}/wr/test",         self.wr_test,        ["POST"],   "测试写作素材库匹配")
        _r(f"/{PLUGIN_NAME}/wr/categories",   self.wr_categories,  ["GET"],    "列出写作素材库分类")
        _r(f"/{PLUGIN_NAME}/wr/batch_delete", self.wr_batch_delete,["POST"],   "批量删除写作素材库条目")
        _r(f"/{PLUGIN_NAME}/wr/batch_toggle", self.wr_batch_toggle,["POST"],   "批量启用/禁用写作素材库条目")

        # ── WB 世界书 (13 个：含 reload) ──
        _r(f"/{PLUGIN_NAME}/wb/list",         self.wb_list,        ["GET"],    "列出世界书")
        _r(f"/{PLUGIN_NAME}/wb/get",          self.wb_get,         ["POST"],   "获取世界书详情")
        _r(f"/{PLUGIN_NAME}/wb/create",       self.wb_create,      ["POST"],   "创建世界书")
        _r(f"/{PLUGIN_NAME}/wb/delete",       self.wb_delete,      ["POST"],   "删除世界书")
        # P3-3 修复：delete_book 与 delete 为同一逻辑（历史兼容端点），复用同一 handler
        _r(f"/{PLUGIN_NAME}/wb/delete_book",  self.wb_delete,      ["POST"],   "删除整本世界书")
        _r(f"/{PLUGIN_NAME}/wb/reload",       self.wb_reload,      ["POST"],   "重新加载世界书")
        _r(f"/{PLUGIN_NAME}/wb/entry/create", self.wb_entry_create,["POST"],   "创建世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/entry/update", self.wb_entry_update,["POST"],   "更新世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/entry/delete", self.wb_entry_delete,["POST"],   "删除世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/import_st",    self.wb_import_st,   ["POST"],   "导入 ST 格式世界书")
        _r(f"/{PLUGIN_NAME}/wb/import_json",  self.wb_import_json, ["POST"],   "导入 JSON(原生/ST)")
        _r(f"/{PLUGIN_NAME}/wb/export_st",    self.wb_export_st,   ["GET"],    "导出 ST 格式世界书")

        # ── Persona 角色卡 (独立 Quill 管理 + V2 兼容) ──
        _r(f"/{PLUGIN_NAME}/persona/list",          self.persona_list,   ["GET"],    "列出角色卡")
        _r(f"/{PLUGIN_NAME}/persona/avatar",        self.persona_avatar, ["GET"],    "获取角色卡头像(懒加载)")
        _r(f"/{PLUGIN_NAME}/persona/create",        self.persona_create, ["POST"],   "创建角色卡")
        _r(f"/{PLUGIN_NAME}/persona/update",        self.persona_update, ["POST"],   "更新角色卡")
        _r(f"/{PLUGIN_NAME}/persona/delete",        self.persona_delete, ["POST"],   "删除角色卡")
        _r(f"/{PLUGIN_NAME}/upload_avatar",         self.upload_avatar,  ["POST"],   "上传头像(文件)")
        # M3.1：base64 变体与正身共用同一 handler（同上）
        _r(f"/{PLUGIN_NAME}/upload_avatar_base64",  self.upload_avatar,  ["POST"],   "上传头像(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/import",        self.persona_import, ["POST"],   "导入V2角色卡(文件)")
        _r(f"/{PLUGIN_NAME}/persona/import_base64", self.persona_import, ["POST"],   "导入V2角色卡(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/export",        self.persona_export, ["GET"],    "导出V2角色卡")
        _r(f"/{PLUGIN_NAME}/persona/export_base64",     self.persona_export_base64, ["POST"], "导出V2角色卡(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/import_text",        self.persona_import_text, ["POST"], "文本导入角色卡")
        # M3.1：base64 变体与正身共用同一 handler（同上）
        _r(f"/{PLUGIN_NAME}/persona/import_text_base64", self.persona_import_text, ["POST"], "文本导入角色卡(Base64)")
        # AstrBot 人格导入：list 供面板选人，import 走摘要模型转换后落卡
        _r(f"/{PLUGIN_NAME}/persona/astrbot_list",       self.persona_astrbot_list, ["GET"], "列出 AstrBot 人格")
        _r(f"/{PLUGIN_NAME}/persona/astrbot_import",     self.persona_astrbot_import, ["POST"], "由 AstrBot 人格导入角色卡")
        # L7：此前还注册了一条 `/avatar/<path:filename>` 静态头像路由，但面板只用
        # `/persona/avatar?id=`（返回 base64 data URL），全仓零调用点。它同时是本
        # 插件路径匹配面最宽的一条（`<path:...>` 吞掉后续所有斜杠），保留只会扩大
        # 未鉴权可达面，故直接删除该注册与对应 handler。

        # ── Info (世界书列表 + 触发日志) ──
        _r(f"/{PLUGIN_NAME}/info",             self.info,           ["GET"],    "插件状态信息")

        # ── 配置持久化 ──
        _r(f"/{PLUGIN_NAME}/config/save",      self.config_save,    ["POST"],   "保存配置项")
        _r(f"/{PLUGIN_NAME}/config/save_batch", self.config_save_batch, ["POST"], "批量保存配置项")
        _r(f"/{PLUGIN_NAME}/config/all",       self.config_all,     ["GET"],    "获取全量配置")

        # ── RAG 文档知识库 (5 个) ──
        _r(f"/{PLUGIN_NAME}/rag/upload",       self.rag_upload,     ["POST"],   "上传文档")
        # M3.1：base64 变体与正身共用同一 handler（通道消歧收敛于 upload.read_upload）
        _r(f"/{PLUGIN_NAME}/rag/upload_base64", self.rag_upload,    ["POST"],   "上传文档(Base64)")
        _r(f"/{PLUGIN_NAME}/rag/documents",    self.rag_documents,  ["GET"],    "列出已上传文档")
        _r(f"/{PLUGIN_NAME}/rag/kb_documents", self.rag_kb_documents, ["GET"],  "列出内置库的文档"),
        _r(f"/{PLUGIN_NAME}/rag/groups",       self.rag_groups,     ["POST"],   "插件文档分组操作"),
        _r(f"/{PLUGIN_NAME}/rag/delete",       self.rag_delete,     ["POST"],   "删除文档")
        _r(f"/{PLUGIN_NAME}/rag/search",       self.rag_search,     ["POST"],   "语义检索测试")
        _r(f"/{PLUGIN_NAME}/rag/config",       self.rag_config,     ["GET"],    "RAG 配置状态")

        # ── 动态记忆 (5 个) ──
        _r(f"/{PLUGIN_NAME}/memory/list",      self.memory_list,    ["GET"],    "列出记忆")
        _r(f"/{PLUGIN_NAME}/memory/list_all",  self.memory_list_all,["GET"],    "列出全部记忆(倒序)")
        _r(f"/{PLUGIN_NAME}/memory/sessions",  self.memory_sessions,["GET"],    "列出所有会话")
        _r(f"/{PLUGIN_NAME}/memory/stats",     self.memory_stats,   ["GET"],    "记忆存储统计")
        _r(f"/{PLUGIN_NAME}/memory/get",       self.memory_get,     ["GET"],    "获取单条记忆详情")
        _r(f"/{PLUGIN_NAME}/memory/delete",    self.memory_delete,  ["POST"],   "删除记忆")
        _r(f"/{PLUGIN_NAME}/memory/pin",       self.memory_pin,     ["POST"],   "钉住/取消钉住核心记忆")
        _r(f"/{PLUGIN_NAME}/memory/vector_search", self.memory_vector_search, ["POST"], "向量检索(Debug)")

        # ── Provider 列表 (配置面板下拉) ──
        _r(f"/{PLUGIN_NAME}/provider/list",    self.provider_list,  ["GET"],    "列出可用模型提供商")

        # ── 记忆导入导出 ──
        _r(f"/{PLUGIN_NAME}/memory/export",    self.memory_export, ["GET"],    "导出记忆(JSON)")
        _r(f"/{PLUGIN_NAME}/memory/import",    self.memory_import, ["POST"],   "导入记忆(JSON)")
        _r(f"/{PLUGIN_NAME}/memory/prune",     self.memory_prune,  ["POST"],   "修剪过期记忆")

        # ── 对话日志 ──
        _r(f"/{PLUGIN_NAME}/chatlog/list",     self.chat_log_list,  ["GET"],    "列出对话日志")
        _r(f"/{PLUGIN_NAME}/chatlog/export",   self.chat_log_export, ["GET"],   "导出对话日志")

        _r(f"/{PLUGIN_NAME}/tasks/list", self.task_list, ["GET"], "后台任务列表（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/list", self.groups_list, ["GET"], "会话组与已观测入口（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/preview", self.groups_preview, ["POST"], "预览共享范围与历史（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/save", self.groups_save, ["POST"], "保存会话组（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/archive", self.groups_archive, ["POST"], "解散并保留管理档案（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/recent", self.groups_recent, ["GET"], "组内近期消息（管理员）")
        _r(f"/{PLUGIN_NAME}/groups/actors", self.groups_actors, ["POST"], "显式参与者关联（管理员）")
        _r(f"/{PLUGIN_NAME}/tasks/detail", self.task_detail, ["GET"], "后台任务详情（管理员）")
        _r(f"/{PLUGIN_NAME}/tasks/cancel", self.task_cancel, ["POST"], "请求安全点取消（管理员）")
        _r(f"/{PLUGIN_NAME}/scope/resolve", self.scope_resolve, ["GET"], "身份与角色桶诊断（管理员）")
        _r(f"/{PLUGIN_NAME}/storage/status", self.schema_status, ["GET"], "存储版本与恢复状态（管理员）")
        _r(f"/{PLUGIN_NAME}/memory/progress", self.memory_progress, ["GET"], "持久总结水位（管理员）")
        _r(f"/{PLUGIN_NAME}/memory/summarize_task", self.memory_summarize_task, ["POST"], "增量总结后台任务")
        _r(f"/{PLUGIN_NAME}/memory/import_task", self.memory_import_task, ["POST"], "记忆导入后台任务")

        _r(f"/{PLUGIN_NAME}/usage/stats", self.usage_stats, ["GET"], "用量元数据（管理员）")
        _r(f"/{PLUGIN_NAME}/usage/clear", self.usage_clear, ["POST"], "清空用量台账（管理员）")

        # ── M1a 注入追踪（只读诊断；写名单 _MUTATING_ROUTES 不涉及）──
        _r(f"/{PLUGIN_NAME}/trace/recent",     self.trace_recent,   ["GET"],    "最近注入追踪(按会话)")
        _r(f"/{PLUGIN_NAME}/trace/detail",     self.trace_detail,   ["GET"],    "单轮注入追踪详情")

        # ── M1b 注入实验室（simulate 只读沙盒；SSE 流；体检全只读）──
        _r(f"/{PLUGIN_NAME}/trace/simulate",   self.trace_simulate, ["POST"],   "模拟检索沙盒(零写入)")
        _r(f"/{PLUGIN_NAME}/trace/events",     self.trace_events,   ["GET"],    "追踪SSE实时流")
        _r(f"/{PLUGIN_NAME}/worldbook/lint",   self.worldbook_lint, ["GET"],    "世界书/角色卡静态体检")
        # 决策模型连通性测试：真发一次判定调用（零写入），面板「测试」按钮用
        _r(f"/{PLUGIN_NAME}/decision/probe",   self.decision_probe, ["POST"],   "决策模型连通性测试")

        # ── Panel 主题 / 界面状态：已随配置项一并移除 ──
        # 主题改由 AstrBot 面板注入 <html data-theme>（前端 app.js 读它），
        # 界面折叠偏好则整体取消（面板已无折叠交互，无偏好需要跨刷新记住）。
        # 两对端点此前仅为「旧客户端平滑过渡」保留，现前端已确认无调用方，
        # 连同 debug.panel_theme / debug.panel_ui_state 两个配置键一起清掉。

        # ── 流式模式批量控制 ──
        _r(f"/{PLUGIN_NAME}/stream/stats",     self.stream_stats,     ["GET"],   "流式模式统计")
        _r(f"/{PLUGIN_NAME}/stream/all",       self.stream_set_all,   ["POST"],  "批量设置流式模式")

        # ── 会话状态清理（管理） ──
        _r(f"/{PLUGIN_NAME}/state/cleanup",    self.state_cleanup,    ["POST"],  "按 UMO 前缀清理会话状态(默认 dry_run)")

        # ── 全量备份导出/恢复 ──
        _r(f"/{PLUGIN_NAME}/backup/export",   self.backup_export,  ["GET"],    "全量备份导出")
        _r(f"/{PLUGIN_NAME}/backup/export_base64", self.backup_export_base64, ["GET"], "全量备份导出(Base64)")
        _r(f"/{PLUGIN_NAME}/backup/restore",  self.backup_restore, ["POST"],   "从备份 zip 恢复(二进制)")
        _r(f"/{PLUGIN_NAME}/backup/restore_base64", self.backup_restore_base64, ["POST"], "从备份 zip 恢复(Base64)")

    # ── Info ──────────────────────────────────────────────────

    @_api_handler
    async def info(self):
        """返回插件状态：可用世界书、触发日志。"""
        from ._route_core import handle_info
        show_log = False
        if self.config is not None:
            show_log = getattr(self.config, 'worldbook_show_log', False)
        pc = 0
        if self.persona_manager:
            pc = await self.persona_manager.get_persona_count()
        return await handle_info(
            self.wr_manager, self.wb_manager,
            persona_count=pc,
            show_trigger_log=show_log,
            health_tracker=self.plugin.health_tracker if self.plugin else None,
        )

    @_api_handler
    async def task_list(self):
        return json_response({"items":self.plugin.task_registry.list(),"retention_seconds":self.plugin.task_registry.retention_seconds})

    async def _groups_call(self,work):
        from .interfaces.groups import GroupBusy, StalePreview
        try:
            self.plugin.shared_scopes.require_ready()
            return json_response(await work())
        except (GroupBusy, StalePreview) as exc:
            return error_response(str(exc),status_code=409)
        except ValueError as exc:
            return error_response(str(exc),status_code=400)

    @_api_handler
    async def groups_list(self):
        async def read():
            service=self.plugin.shared_scopes
            await service.refresh()
            from .interfaces.prompt_cache import _host_profile, last_capability
            return {**service.index,"groups":[{**g,'busy':service.busy(g['scope_id'])} for g in service.index['groups']],"history_policy":"from_now_unless_selected",
                "exit_policy":"管理员在此面板移除入口后恢复该入口原有独立数据；组档案仅管理员可读",
                "capability":"共享纯文本；需通过宿主装配、保存契约与实际序列化能力检查；仅本地 runner 的已审阅 OpenAI/Anthropic 路径",
                "host_capability":_host_profile(),
                "origin_capabilities":{o['origin_key']:last_capability(o['umo']) for o in service.index['origins']},
                "instance_scope":"single_astrbot_instance"}
        return await self._groups_call(read)

    @_api_handler
    async def groups_preview(self):
        data=await _json_body()
        return await self._groups_call(lambda:self.plugin.shared_scopes.preview(data))

    @_api_handler
    async def groups_save(self):
        data=await _json_body()
        return await self._groups_call(lambda:self.plugin.shared_scopes.save(data))

    @_api_handler
    async def groups_archive(self):
        data=await _json_body()
        scope=data.get('scope_id');revision=data.get('revision')
        if not isinstance(scope,str) or not scope.startswith('scope:') or not isinstance(revision,int) or data.get('confirm')!='ARCHIVE':
            return error_response('需要会话组 ID、当前版本和解散确认',status_code=400)
        return await self._groups_call(lambda:self.plugin.shared_scopes.archive(scope,revision))

    @_api_handler
    async def groups_recent(self):
        scope=request.query.get('scope_id','')
        persona=request.query.get('persona_id','')
        if len(scope)>200 or len(persona)>200:
            return error_response('会话组或角色 ID 过长',status_code=400)
        async def read():
            if not any(g['scope_id']==scope for g in self.plugin.shared_scopes.index['groups']):
                raise ValueError('会话组不存在')
            return {'items':await self.plugin.shared_scopes.store.recent(scope,persona)}
        return await self._groups_call(read)

    @_api_handler
    async def groups_actors(self):
        from uuid import uuid4
        data=await _json_body()
        actors=data.get('actors')
        if not isinstance(actors,list) or not 1<=len(actors)<=50 or any(not isinstance(a,str) for a in actors):
            return error_response('请选择 1–50 个已观测参与者',status_code=400)
        async def link():
            service=self.plugin.shared_scopes
            async with service.admin_lock:
                # 只锁受关联身份影响的组；其他组生成不应阻塞此操作。
                for scope in await service.store.actor_scopes(actors):
                    service.guard_idle(scope)
                canonical=None if data.get('unlink') is True else 'actorlink:'+uuid4().hex
                await service.store.link_actors(actors,canonical)
                await service.refresh()
                return {'actors':actors,'canonical_id':canonical,'permission_changed':False}
        return await self._groups_call(link)

    @_api_handler
    async def task_detail(self):
        record = self.plugin.task_registry.get(str(request.query.get("id", "")))
        if record is None:
            return error_response("任务不存在或已过保留期",status_code=404)
        return json_response({"status":"ok","data":record.to_dict()})

    @_api_handler
    async def task_cancel(self):
        data = await _json_body()
        result = self.plugin.task_registry.cancel(str(data.get("task_id", "")))
        return json_response({"status":"ok","data":result}) if result is not None else error_response("任务不存在或已过保留期",status_code=404)

    @_api_handler
    async def scope_resolve(self):
        from .quill.services.scope import PhysicalOrigin
        umo = str(request.query.get("umo", ""))
        if not umo or len(umo)>2048:
            return error_response("需要有效 umo",status_code=400)
        selected = await self.plugin.state_manager.get_selected_persona_id(umo)
        persona = str(request.query.get("persona_id", selected))
        if len(persona)>200:
            return error_response("角色 ID 过长",status_code=400)
        parts=umo.split(":",2)
        observed=next((o for o in self.plugin.shared_scopes.index['origins'] if o['umo']==umo),{})
        origin=PhysicalOrigin(umo,str(request.query.get("platform_instance_id",observed.get('platform_id',parts[0] if len(parts)==3 else ""))),
                              observed.get('message_type',parts[1] if len(parts)==3 else ""),str(request.query.get("sender_id","")))
        turn=self.plugin.scope_resolver.resolve(origin,persona)
        if 'persona_id' in request.query:
            from dataclasses import replace
            turn=replace(turn,persona_id=persona)
        values=await self.plugin.state_manager.get_session_vars(umo,persona_id=persona,claim_legacy=False)
        return json_response({"identity":turn.to_dict(),"memory_session_id":turn.memory_session_id,
                              "selected_persona_id":selected,"session_vars":values,"diagnostic_only":True})

    @_api_handler
    async def schema_status(self):
        stores={"memory":getattr(self.plugin,"rag_memory_store",None),
                "documents":getattr(self.plugin,"rag_vector_store",None),"writing":self.plugin.wr_manager}
        result={}
        for name,store in stores.items():
            status=dict(getattr(store,"schema_status",{}) or (getattr(self.plugin,"_wr_storage_status",{}) if name == "writing" else getattr(self.plugin,"_rag_storage_status",{}).get(name,{})) or {})
            if status:
                status["backup_verified"]=bool(status.pop("backup",None))
            result[name]={"available":store is not None and getattr(store,"_conn",None) is not None,**status}
            if store is not None and name == 'memory':
                result[name]['retention'] = getattr(store, 'retention_status', {})
        memory=stores["memory"]
        pending=await memory.pending_operations() if memory and getattr(memory,"_writable",False) else []
        return json_response({"stores":result,"pending_operations":[{**{k:v for k,v in row.items() if k!='payload'},
            'failure_count':row['payload'].get('failure_count',0),'error_class':row['payload'].get('error_class'),
            'retry_after':row['payload'].get('retry_after')} for row in pending],
                              "state_read_only":bool(getattr(self.plugin.state_manager,"_read_only_reason",None))})

    @_api_handler
    async def memory_progress(self):
        session_id=str(request.query.get("session_id",""))
        store=self.rag.get("memory_store")
        if not session_id or len(session_id)>2048:
            return error_response("需要有效 session_id",status_code=400)
        if not store or not getattr(store,"_writable",False):
            return error_response("记忆库暂不可用",status_code=503)
        turn=self.plugin.scope_resolver.from_session_key(session_id,self.plugin.state_manager.known_origins())
        return json_response({**await store.get_summary_progress(session_id),"scope_id":turn.scope_id,"persona_id":turn.persona_id})

    @_api_handler
    async def memory_summarize_task(self):
        data=await _json_body()
        session_id=str(data.get("session_id", ""))
        if not session_id or len(session_id)>2048:
            return error_response("需要有效 session_id",status_code=400)
        store=self.rag.get("memory_store")
        if not store or not getattr(store,"_writable",False):
            return error_response("记忆库暂不可用",status_code=503)
        if not self.plugin.rag_retriever or not self.plugin.rag_retriever.enable_memory:
            return error_response("动态记忆功能未启用",status_code=400)
        turn=self.plugin.scope_resolver.from_session_key(session_id,self.plugin.state_manager.known_origins())
        legacy=await self.plugin.state_manager.get_last_learned_id(turn.scope_id,persona_id=turn.persona_id)
        record,reused=self.plugin._start_summary_task(session_id,legacy_last_id=legacy)
        return json_response({"task_id":record.task_id,"reused":reused,"task":record.to_dict()},status_code=202)

    @_api_handler
    async def memory_import_task(self):
        import hashlib,json
        from ._route_core import handle_memory_import
        data=await _json_body()
        memories=data.get("memories")
        if not isinstance(memories,list) or len(memories)>10000 or any(not isinstance(row,dict) for row in memories):
            return error_response("memories 必须为最多 10000 条对象的数组",status_code=400)
        store=self.rag.get("memory_store")
        if not store or not getattr(store,"_writable",False):
            return error_response("记忆库暂不可用",status_code=503)
        key=hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        async def work(handle):
            result=await handle_memory_import(store,self.rag.get("embedding"),data,handle=handle)
            if result.get("status")!='ok':
                raise ValueError("记忆导入失败")
            handle.record.result=result.get("data")
            return result.get("data")
        record,reused=self.plugin._start_task("memory_import",key,work,total=len(memories))
        return json_response({"task_id":record.task_id,"reused":reused,"task":record.to_dict()},status_code=202)

    @_api_handler
    async def usage_stats(self):
        recorder = getattr(self.plugin, "_usage", None) if self.plugin else None
        if recorder is None or not recorder.ready:
            return error_response("用量台账暂不可用，对话不受影响", status_code=503)
        params = request.query
        filters = {}
        try:
            for key, default, low, high in (("days", 7, 1, 366), ("limit", 50, 1, 200),
                                            ("offset", 0, 0, 100000)):
                filters[key] = int(params.get(key, default))
                if not low <= filters[key] <= high:
                    raise ValueError()
            for key in ("scope_id", "source_umo", "persona_id", "provider_id", "model", "purpose"):
                filters[key] = str(params.get(key, ""))
                if len(filters[key]) > 512:
                    raise ValueError()
        except (ValueError, TypeError):
            return error_response("用量筛选参数无效", status_code=400)
        try:
            data = await recorder.query(**filters)
            import hashlib
            labels = {}
            groups = getattr(self.plugin, 'shared_scopes', None)
            if groups:
                for group in groups.index['groups']:
                    labels[hashlib.sha256(group['scope_id'].encode()).hexdigest()[:16]] = '会话组：' + group['name']
                for origin in groups.index['origins']:
                    labels.setdefault(hashlib.sha256(origin['umo'].encode()).hexdigest()[:16], origin.get('label') or origin['umo'])
            for dimension, key in (('sessions','scope_id'), ('origins','source_umo')):
                for row in data.get(dimension,[]):
                    row['label'] = labels.get(row[key], row[key])
            data['context_compression'] = {'enabled':self.plugin.props.compression_enabled,'limit':self.plugin.props.compression_max_tokens,'recent':list(self.plugin.context_compactor.status.values())[-20:]}
            return json_response(data)
        except Exception as exc:
            recorder.error = type(exc).__name__
            return error_response("用量台账暂不可用，对话不受影响", status_code=503)

    @_api_handler
    async def usage_clear(self):
        data = await _json_body()
        if data.get("confirm") != "CLEAR_USAGE":
            return error_response("需明确确认清空用量台账", status_code=400)
        recorder = getattr(self.plugin, "_usage", None) if self.plugin else None
        if recorder is None or not recorder.ready:
            return error_response("用量台账暂不可用", status_code=503)
        try:
            await recorder.clear()
        except Exception as exc:
            recorder.error = type(exc).__name__
            return error_response("用量台账暂不可用", status_code=503)
        return json_response({"cleared": True})

    # ── Config Save ───────────────────────────────────────────

    @_api_handler
    async def config_save(self):
        """保存配置项。

        H2：不再依赖「面板已由 AstrBot 鉴权保护」这一前提——同一条路由在
        `/api/v1/plugins/extensions/*` 上只需 `plugin` 作用域的 API Key 即可调用，
        而框架校验 API Key 时从不校验密码。鉴权改由 `_api_handler` 统一收口
        （本 handler 已登记进 `_MUTATING_ROUTES`）。
        """
        data = await _json_body()
        group = data.get("group", "")
        key = data.get("key", "")
        value = data.get("value")

        if not group or not key:
            return error_response("缺少 group 或 key 参数", status_code=400)

        if (group, key) not in _ALLOWED_CONFIG_KEYS:
            return error_response(f"不支持的配置项: {group}.{key}", status_code=400)

        from .quill.services.usage import validate_usage_setting
        message = validate_usage_setting(group, key, value)
        if message:
            return error_response(message, status_code=400)
        if (group, key) == ("usage", "prices_json"):
            from .quill.services.usage import validate_prices
            message = validate_prices(value)
            if message:
                return error_response(message, status_code=400)

        plugin = self.plugin
        if plugin is None:
            return error_response("插件实例不可用", status_code=500)

        ok = plugin.save_plugin_config(group, key, value)
        if ok:
            return json_response({"status": "ok", "message": f"已保存 {group}.{key}"})
        return error_response("保存失败", status_code=500)

    @_api_handler
    async def config_save_batch(self):
        """Persist all changed fields in one transaction and one hot reload."""
        data = await _json_body()
        updates = data.get("updates")
        if not isinstance(updates, list) or not updates:
            return error_response("缺少 updates 数组", status_code=400)
        if len(updates) > 64:
            return error_response("单次最多保存 64 项配置", status_code=400)

        seen: set[tuple[str, str]] = set()
        for item in updates:
            if not isinstance(item, dict):
                return error_response("配置更新格式无效", status_code=400)
            group = str(item.get("group", "")).strip()
            key = str(item.get("key", "")).strip()
            pair = (group, key)
            if pair not in _ALLOWED_CONFIG_KEYS:
                return error_response(
                    f"不支持的配置项: {group}.{key}", status_code=400
                )
            from .quill.services.usage import validate_usage_setting
            message = validate_usage_setting(*pair, item.get("value"))
            if message:
                return error_response(message, status_code=400)
            if pair == ("usage", "prices_json"):
                from .quill.services.usage import validate_prices
                message = validate_prices(item.get("value"))
                if message:
                    return error_response(message, status_code=400)
            if pair in seen:
                return error_response(
                    f"重复的配置项: {group}.{key}", status_code=400
                )
            seen.add(pair)

        plugin = self.plugin
        if plugin is None:
            return error_response("插件实例不可用", status_code=500)
        saved, message = plugin.save_plugin_configs(updates)
        if saved:
            return json_response({"status": "ok", "message": message})
        return error_response(f"保存失败: {message}", status_code=500)

    @_api_handler
    async def config_all(self):
        """获取全量 config 供前端渲染（经 _ALLOWED_CONFIG_KEYS 白名单过滤，不下发未授权字段）。"""
        if not self.config:
            return json_response({})
        raw = self.config.get_raw()
        if not isinstance(raw, dict):
            return json_response({})
        safe = {}
        for group, group_dict in raw.items():
            if not isinstance(group_dict, dict):
                continue
            allowed = {
                key: value for key, value in group_dict.items()
                if (group, key) in _ALLOWED_CONFIG_KEYS
            }
            if allowed:
                safe[group] = allowed
        return json_response(safe)

    # ── 流式模式批量控制 ───────────────────────────────────────

    @_api_handler
    async def stream_stats(self):
        """返回所有 session 的流式模式统计。"""
        plugin = self.plugin
        if plugin is None or plugin.state_manager is None:
            return json_response({"auto": 0, "on": 0, "off": 0, "total": 0})
        stats = await plugin.state_manager.get_stream_mode_stats()
        return json_response(stats)

    @_api_handler
    async def stream_set_all(self):
        """批量设置所有 session 的流式模式。"""
        plugin = self.plugin
        if plugin is None or plugin.state_manager is None:
            return error_response("插件实例不可用", status_code=500)
        data = await _json_body()
        mode = (data.get("mode") or "").strip().lower()
        if mode not in ("auto", "on", "off"):
            return error_response("无效模式，可选: auto/on/off", status_code=400)
        count = await plugin.state_manager.set_stream_mode_all(mode)
        mode_names = {"auto": "自动", "on": "强制流式", "off": "强制关闭"}
        return json_response({
            "status": "ok",
            "data": {
                "affected": count,
                "mode": mode,
                "message": f"已将 {count} 个会话的流式模式设为: {mode_names[mode]}"
            }
        })

    # ── 会话状态清理（管理） ──────────────────────────────────────

    @staticmethod
    def _validate_cleanup_prefix(prefix: str) -> str | None:
        """校验 state/cleanup 的 session_prefix；返回错误信息，None 表示通过。

        M7 修复：原闸门只有「≥8 字符且含 '!'」，而 UMO 形如
        ``平台:事件:平台ID!用户ID!会话``——只写 ``webchat:FriendMessage:webchat!``
        就满足条件，却会命中**该平台下所有用户**的状态键（单次上限 500 条）。
        用户以为在清自己那一条，实际清掉的是别人的。

        现在只接受「精确到一个会话」的前缀：必须含 '!'（用户边界）**且**以 '!'
        结尾（会话边界），即 ``…!用户ID!``。多一层 ``!`` 就多排除一个用户，
        因此结尾的 '!' 之后不允许再有内容，也不允许用裸用户前缀批量清人。
        """
        if not prefix:
            return "缺少 session_prefix"
        if any(ord(c) < 32 or c == "\x7f" for c in prefix):
            return "session_prefix 含非法控制字符"
        if "!" not in prefix or not prefix.endswith("!"):
            return (
                "session_prefix 过宽：只允许精确到单个会话的前缀，必须以 '!' 结尾"
                "（形如 webchat:FriendMessage:webchat!quilltest!）。"
                "若只想清理自己那个会话，请到面板会话列表按当前 UMO 清理。"
            )
        # 会话段本身不能为空（`…!user!` 合法，`webchat:x!user!!` 这种空段拒绝）。
        if prefix.endswith("!!") or prefix.startswith("!"):
            return "session_prefix 格式无效：存在空的用户/会话段"
        return None

    @_api_handler
    async def state_cleanup(self):
        """按 UMO 前缀清理 quill_state.json 里的会话状态键（管理端点）。

        请求体 ``{"session_prefix": "...", "dry_run": true, "confirm": "DELETE"}``：
        - **dry_run 默认 true**：只返回 matched/deleted=0/sample，不改动任何数据；
        - M7：真删（``dry_run=false``）额外要求 ``confirm="DELETE"`` 显式确认，
          并把命中数回显在返回体里——此前只有 dry_run 一个开关，一次误触就是
          不可逆删除；
        - 前缀必须精确到单个会话（见 `_validate_cleanup_prefix`），
          再叠加 StateManager 单次 500 上限；
        - 删除走 StateManager 锁内路径并置脏，由 autoflush 正常落盘。
        """
        plugin = self.plugin
        if plugin is None or getattr(plugin, "state_manager", None) is None:
            return error_response("状态管理器未加载", status_code=500)
        data = await _json_body()
        prefix = str(data.get("session_prefix") or "").strip()
        dry_run = data.get("dry_run", True)
        # 显式收 bool：JSON 里 "false" 是真值，字符串误传会把 dry_run 变成真删。
        if not isinstance(dry_run, bool):
            return error_response("dry_run 必须是布尔值", status_code=400)
        err_msg = self._validate_cleanup_prefix(prefix)
        if err_msg:
            return error_response(err_msg, status_code=400)
        if not dry_run and str(data.get("confirm") or "").strip() != "DELETE":
            return error_response(
                "真删需要显式确认：请把 confirm 设为 \"DELETE\"。"
                "建议先用 dry_run=true 查看命中数量与样例。",
                status_code=400,
            )
        try:
            result = await plugin.state_manager.cleanup_states_by_prefix(
                prefix, dry_run=dry_run
            )
        except ValueError as e:
            return error_response(str(e), status_code=400)
        if isinstance(result, dict):
            # M7：把「命中/已删多少」明确回显，前端与调用方都能看到影响面。
            result.setdefault("matched", 0)
            result["dry_run"] = dry_run
        return json_response({"status": "ok", "data": result})

    # ── RAG ───────────────────────────────────────────────────

    @_api_handler
    async def rag_upload(self):
        """上传文档（multipart 文件 / base64 双通道统一入口，M3.1 收敛）。

        ``/rag/upload``（multipart）与 ``/rag/upload_base64``（JSON+b64_data，
        面板 bridge 通道）共用同一 handler：通道消歧、50MB 上限、base64
        解码全部收敛于 upload.read_upload。原 base64 变体的 ``source``
        字段既是文件名（扩展名黑名单检查对象）也是文档名。
        """
        try:
            payload = await read_upload(
                request,
                keys=("file",),
                limit=DEFAULT_LIMIT,
                filename_field="source",
                default_filename="unknown",
            )
        except UploadTooLarge:
            return error_response("文档文件过大（最大 50MB）", status_code=413)
        except UploadError as e:
            return error_response(error_text("Base64 解码失败", e), status_code=400)
        if payload is None:
            return error_response("未收到文件", status_code=400)
        # 文件类型检测：仅支持纯文本文件（黑名单常量唯一来源：upload.BINARY_EXTS）
        ext = os.path.splitext(payload.filename.lower())[1]
        if ext in BINARY_EXTS:
            return error_response(f"不支持的文件格式：{ext}。目前仅支持纯文本文件（.txt/.md/.csv/.json 等）。PDF/Word 请先转换为纯文本。", status_code=400)
        source = payload.fields.get("source") or payload.filename
        chunk_size = self.config.rag_chunk_size if self.config else 500
        chunk_overlap = self.config.rag_chunk_overlap if self.config else 50
        embedding = self.rag.get('embedding')
        vector_store = self.rag.get('vector_store')
        if embedding is None or vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(
            await handle_rag_upload(vector_store, embedding, _BytesUpload(payload.data), source, chunk_size, chunk_overlap,
                                    group_store=self.rag.get('doc_groups'),
                                    group=payload.fields.get("group", ""))
        )

    @_api_handler
    async def rag_documents(self):
        """列出已上传文档 + 插件文档分组 + AstrBot 内置知识库。"""
        vector_store = self.rag.get('vector_store')
        kb_source = self.rag.get('kb_source')
        kb_ready = bool(kb_source and getattr(kb_source, "available", False))
        if vector_store is None and not kb_ready:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(await handle_rag_documents(
            vector_store, kb_source=kb_source, group_store=self.rag.get('doc_groups')
        ))

    @_api_handler
    async def rag_groups(self):
        """插件文档分组（子知识库）的增删改与归组。

        body: ``{"action": "create|rename|delete|assign", "name": ..., "new_name": ...,
        "sources": [...], "group": ...}``

        H2：`action` 里 create/rename/delete/assign 全部是写操作（面板同一入口
        按 action 分派），因此整个 handler 都登记为写类——只读的列表由
        `rag/documents` 承担。
        """
        data = await _json_body()
        return json_response(await handle_rag_group_action(
            self.rag.get('doc_groups'),
            data.get("action", ""),
            name=data.get("name", ""),
            new_name=data.get("new_name", ""),
            sources=data.get("sources") or [],
            group=data.get("group", ""),
        ))

    @_api_handler
    async def rag_kb_documents(self):
        """列出某个 AstrBot 内置知识库里的文档（面板「子知识库组」展开懒加载）。"""
        kb_name = request.query.get("kb_name", "")
        limit = request.query.get("limit", 200)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 200
        return json_response(
            await handle_rag_kb_documents(self.rag.get('kb_source'), kb_name, limit=limit)
        )

    @_api_handler
    async def rag_delete(self):
        """删除文档。"""
        data = await _json_body()
        source = data.get("source", "")
        if not source:
            return error_response("缺少 source 参数", status_code=400)
        vector_store = self.rag.get('vector_store')
        if vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(await handle_rag_delete(
            vector_store, source, group_store=self.rag.get('doc_groups')
        ))

    @_api_handler
    async def rag_search(self):
        """语义检索测试。"""
        data = await _json_body()
        query = data.get("query", "")
        top_k = data.get("top_k", self.config.rag_top_k if self.config else 3)
        if not query:
            return error_response("缺少 query 参数", status_code=400)
        embedding = self.rag.get('embedding')
        vector_store = self.rag.get('vector_store')
        reranker = self.rag.get('reranker')
        kb_source = self.rag.get('kb_source')
        kb_ready = bool(kb_source and getattr(kb_source, "available", False))
        if (embedding is None or vector_store is None) and not kb_ready:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(
            await handle_rag_search(vector_store, embedding, reranker, query, top_k, kb_source=kb_source)
        )

    @_api_handler
    async def rag_config(self):
        """返回 RAG 配置状态（包含 LLM 提供商）。"""
        embedding = self.rag.get('embedding')
        reranker = self.rag.get('reranker')
        resp = await handle_rag_config(embedding, reranker)
        # 补充 LLM 提供商 ID
        if resp.get('status') == 'ok' and self.config:
            resp['data']['llm_provider_id'] = getattr(self.config, 'rag_llm_provider_id', '')
        return json_response(resp)

    # ── Memory ─────────────────────────────────────────────────

    @_api_handler
    async def memory_list(self):
        """列出记忆。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        session_id = request.query.get("session_id")
        return json_response(await handle_memory_list(memory_store, session_id))

    @_api_handler
    async def memory_list_all(self):
        """列出全部记忆（跨 session），按创建时间倒序，支持分页。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        page = max(1, request.query.get("page", 1, type=int) or 1)
        per_page = min(max(1, request.query.get("per_page", 50, type=int) or 50), 200)
        return json_response(await handle_memory_list_all(memory_store, page=page, per_page=per_page))

    @_api_handler
    async def memory_sessions(self):
        """列出有记忆或待总结日志的会话。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        sessions = await memory_store.list_sessions()
        return json_response({"status": "ok", "data": {"sessions": sessions}})

    @_api_handler
    async def memory_stats(self):
        """B4 修复：记忆存储统计（总数 / 活跃会话 / 今日新增）。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        return json_response(await handle_memory_stats(memory_store))

    @_api_handler
    async def memory_get(self):
        """获取单条记忆完整详情。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        memory_id = request.query.get("id", type=int)
        if not memory_id:
            return error_response("缺少 id 参数", status_code=400)
        result = await handle_memory_get(memory_store, memory_id=memory_id)
        if result.get('status') == 'ok':
            mem = result['data']
            turn = self.plugin.scope_resolver.from_session_key(mem['session_id'],self.plugin.state_manager.known_origins())
            group = self.plugin.scope_resolver.groups.get(turn.scope_id)
            if group:
                mem['shared_scope'] = {'scope_id':turn.scope_id,'revision':group['revision'],'name':group['name'],
                    'affected':[o['umo'] for o in self.plugin.shared_scopes.index['origins'] if o['scope_id']==turn.scope_id]}
        return json_response(result)

    @_api_handler
    async def memory_vector_search(self):
        """向量检索 Debug — 对输入文本做 embedding 后全局搜索。"""
        data = await _json_body()
        query = data.get("query", "")
        top_k = data.get("top_k", 5)
        if not query:
            return error_response("缺少 query 参数", status_code=400)
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        embedding = self.rag.get('embedding')
        if embedding is None:
            return error_response("Embedding 未初始化", status_code=500)
        try:
            vectors = await embedding.embed([query])
            vector = vectors[0] if vectors else None
            if not vector:
                return error_response("embedding 向量化失败", status_code=500)
            # 将 NumPy 全表扫描 + 矩阵计算放入线程池，防止阻塞事件循环
            results = await memory_store.search_all(vector, top_k=top_k)
            return json_response({"results": results, "query": query})
        except Exception as e:
            return error_response(error_text("检索失败", e), status_code=500)

    @_api_handler
    async def memory_export(self):
        """导出全部记忆为 JSON。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        return json_response(await handle_memory_export(memory_store))

    @_api_handler
    async def memory_import(self):
        """从上传的 JSON 文件导入记忆（异步，重新生成向量）。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        embedding = self.rag.get('embedding')
        try:
            data = await _json_body()
        except Exception:
            return error_response("请求体不是有效 JSON", status_code=400)
        result = await handle_memory_import(memory_store, embedding, data)
        if result.get("status") == "error":
            return error_response(result.get("message", "导入失败"), status_code=400)
        return json_response(result)

    @_api_handler
    async def memory_prune(self):
        """一键清理低价值/过期记忆。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        return json_response(await handle_memory_prune(memory_store))

    @_api_handler
    async def memory_delete(self):
        """删除记忆。"""
        data = await _json_body()
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        session_id = data.get('session_id')
        if data.get('memory_id'):
            memory = await memory_store.get_memory_by_id(data['memory_id'])
            session_id = memory['session_id'] if memory else None
        turn = self.plugin.scope_resolver.from_session_key(str(session_id or ''),self.plugin.state_manager.known_origins())
        group = self.plugin.scope_resolver.groups.get(turn.scope_id)
        if group:
            service = self.plugin.shared_scopes
            async with service.admin_lock:
                latest = self.plugin.scope_resolver.groups.get(turn.scope_id)
                if not latest or data.get('scope_revision') != latest['revision'] or data.get('confirm_scope') != turn.scope_id:
                    return error_response('共享范围已变化或未确认，请重新查看影响入口后删除',status_code=409)
                try:
                    service.guard_idle(turn.scope_id)
                except ValueError as exc:
                    return error_response(str(exc),status_code=409)
                return json_response(await handle_memory_delete(memory_store,data.get('memory_id'),data.get('session_id')))
        return json_response(await handle_memory_delete(memory_store, data.get("memory_id"), data.get("session_id")))

    @_api_handler
    async def memory_pin(self):
        """钉住/取消钉住核心记忆。is_core=1 的记忆不参与 Top-K 竞争，直接注入 prompt。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        data = await _json_body()
        memory_id = data.get("memory_id")
        is_core = bool(data.get("is_core", False))
        if not memory_id:
            return error_response("缺少 memory_id", status_code=400)
        # L7：非数字 memory_id 此前抛 ValueError 被 _api_handler 兜成 500（服务端
        # 错误），其实是调用方参数不合法 → 400。同文件 chat_log_list 已用 type=int。
        try:
            memory_id_int = int(memory_id)
        except (TypeError, ValueError):
            return error_response("memory_id 必须是整数", status_code=400)
        ok = await memory_store.set_core(memory_id_int, is_core)
        if not ok:
            return error_response("记忆不存在或更新失败", status_code=404)
        return json_response({
            "status": "ok",
            "data": {"memory_id": memory_id, "is_core": is_core}
        })

    # ── Chat Log ──────────────────────────────────────────────

    @_api_handler
    async def chat_log_list(self):
        """列出对话日志。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        session_id = request.query.get("session_id")
        # L7：非数字（?limit=abc）回落到默认值。不用 request.query.get(..., type=int)：
        # 真机 PluginMultiDict 支持 type 参数，但显式解析在两种实现下行为一致。
        try:
            raw_limit = int(request.query.get("limit") or 200)
        except (TypeError, ValueError):
            raw_limit = 200
        limit = min(max(1, raw_limit), 1000)
        return json_response(await handle_chat_log_list(memory_store, session_id, limit))

    @_api_handler
    async def chat_log_export(self):
        """导出对话日志。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆暂不可用，请检查存储状态", status_code=503)
        session_id = request.query.get("session_id")
        fmt = request.query.get("format", "markdown")
        result = await handle_chat_log_export(memory_store, session_id, fmt)
        if result.get("status") == "error":
            return error_response(result.get("message", "导出失败"), status_code=400)
        return json_response(result)

    # ── M1a 注入追踪（只读；数据在内存 ring buffer，重启即清）──

    @_api_handler
    async def trace_recent(self):
        """最近注入追踪轮次。

        ``?target=<16位hash>`` → 该会话最近 N 轮（最新在前，limit 1-50 默认 20）；
        缺省 → 有记录的会话摘要（面板会话下拉）。只认哈希会话键，原始
        target_id 不出内存（trace 红线）。
        """
        from ._route_core import handle_trace_recent
        return json_response(await handle_trace_recent(
            self.plugin,
            target=request.query.get("target", ""),
            limit=request.query.get("limit", 20),
        ))

    @_api_handler
    async def trace_detail(self):
        """单轮追踪详情（``?id=<32位trace_id>``）。

        id 即 /quill debug「Trace: …」锚点对应的完整轮次——两处同源，
        结论必然一致（规划 §四 M1a 的硬验收项）。
        """
        from ._route_core import handle_trace_detail
        result = await handle_trace_detail(
            self.plugin, trace_id=request.query.get("id", "")
        )
        if result.get("status") == "error":
            return error_response(result.get("message", "查询失败"), status_code=404)
        return json_response(result)

    # ── M1b 注入实验室（只读）──────────────────────────────────

    @_api_handler
    async def trace_simulate(self):
        """模拟检索沙盒（POST，零写入 TraceStore/state/chat_logs）。

        入参 ``{text, persona_id?, target_id_hash?, rag?, umo?,
        session_vars?, include_recent_context?}``。``rag`` 缺省关——
        零 provider 请求；面板侧拿不到原始 UMO，记忆/文档检索只有
        API/指令侧带 ``umo`` 时可用（面板粘贴多轮文本即上下文）。
        """
        from ._route_core import handle_trace_simulate
        data = await _json_body()
        result = await handle_trace_simulate(
            self.plugin,
            text=data.get("text", ""),
            persona_id=data.get("persona_id", ""),
            target_id_hash=data.get("target_id_hash", ""),
            rag=bool(data.get("rag", False)),
            umo=data.get("umo", ""),
            session_vars=data.get("session_vars"),
            include_recent_context=bool(data.get("include_recent_context", False)),
        )
        if result.get("status") == "error":
            return error_response(result.get("message", "模拟失败"), status_code=400)
        return json_response(result)

    @_api_handler
    async def worldbook_lint(self):
        """世界书/角色卡静态体检（纯本地分析，零模型调用）。"""
        from ._route_core import handle_worldbook_lint
        return json_response(await handle_worldbook_lint(self.plugin))

    @_api_handler
    async def decision_probe(self):
        """决策模型连通性测试（POST，真发一次判定调用，零写入）。

        入参 ``{base_url?, api_key?, model?, api_format?}``，全部可选——
        面板只把「用户改过但还没保存」的字段传上来，其余回落服务端配置。
        与 ``/trace/simulate`` 不同，这个端点**会真实发出网络请求**（也就
        是它存在的意义），但仍然零写入：不碰 state / chat_logs / TraceStore。
        """
        from ._route_core import handle_decision_probe
        data = await _json_body()
        return json_response(await handle_decision_probe(
            self.plugin,
            base_url=data.get("base_url"),
            api_key=data.get("api_key"),
            model=data.get("model"),
            api_format=data.get("api_format"),
        ))

    @_api_handler
    async def trace_events(self):
        """追踪 SSE 实时流（M1b 模块 7）。

        宿主桥 ``subscribeSSE`` 经 ``EventSource('/api/plug/…/trace/events')``
        连接；直接返回 Starlette StreamingResponse（经宿主
        ``_coerce_view_result`` 原样透传——不能返回 Quart 响应，会被整体
        缓冲）。事件为 ``data: {trace.to_dict()}\n\n``；15s 心跳注释行保活，
        10 分钟无断开则自动收线（防僵尸连接占住队列）。每个连接注册一个
        TraceStore 监听者，断开时移除（finally 兜底）。
        """
        import json as _json
        from starlette.responses import StreamingResponse
        plugin = self.plugin
        store = getattr(plugin, "_trace_store", None) if plugin is not None else None
        if store is None:
            return error_response("追踪存储未初始化", status_code=503)
        if not getattr(getattr(plugin, "props", None), "trace_enabled", False):
            return error_response("注入追踪已关闭（配置 → 调试与维护）", status_code=503)

        queue: asyncio.Queue = asyncio.Queue(maxsize=20)

        def _on_commit(payload: dict):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 慢消费者：丢最旧不如丢本轮——SSE 是「实时瞥一眼」通道

        store.add_listener(_on_commit)

        async def _gen():
            try:
                yield "retry: 5000\n\n"
                idle = 0
                while idle < 600:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                        idle = 0
                        yield f"data: {_json.dumps(item, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        idle += 15
                        yield ": heartbeat\n\n"
                yield "event: bye\ndata: {}\n\n"
            finally:
                store.remove_listener(_on_commit)

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Provider List ─────────────────────────────────────────

    @_api_handler
    async def provider_list(self):
        """列出 AstrBot 中已配置的 Embedding / Rerank 提供商。"""
        return json_response(await handle_provider_list(self.context))

    # ── WR ────────────────────────────────────────────────────

    @_api_handler
    async def wr_list(self):
        category = request.query.get("category")
        search = request.query.get("search")
        page = max(1, request.query.get("page", 1, type=int) or 1)
        per_page = min(max(1, request.query.get("per_page", 20, type=int) or 20), 100)
        is_constant_str = request.query.get("is_constant")
        is_constant = (is_constant_str.lower() == 'true') if is_constant_str else None
        return json_response(
            await handle_wr_list(self.wr_manager, category, search, page, per_page, is_constant)
        )

    @_api_handler
    async def wr_get(self):
        data = await _json_body()
        return json_response(
            await handle_wr_get(self.wr_manager, data.get("entry_id"))
        )

    @_api_handler
    async def wr_create(self):
        return json_response(
            await handle_wr_create(self.wr_manager, await _json_body())
        )

    @_api_handler
    async def wr_update(self):
        return json_response(
            await handle_wr_update(self.wr_manager, await _json_body())
        )

    @_api_handler
    async def wr_delete(self):
        data = await _json_body()
        return json_response(
            await handle_wr_delete(self.wr_manager, data.get("entry_id"))
        )

    @_api_handler
    async def wr_toggle(self):
        data = await _json_body()
        return json_response(
            await handle_wr_toggle(
                self.wr_manager,
                data.get("entry_id"),
                data.get("enabled", True),
            )
        )

    @_api_handler
    async def wr_export(self):
        return json_response(await handle_wr_export(self.wr_manager))

    @_api_handler
    async def wr_import(self):
        data = await _json_body()
        return json_response(
            await handle_wr_import(self.wr_manager, data.get("entries", []))
        )

    @_api_handler
    async def wr_test(self):
        data = await _json_body()
        return json_response(
            await handle_wr_test(self.wr_manager, data.get("text"))
        )

    @_api_handler
    async def wr_categories(self):
        return json_response(await handle_wr_categories(self.wr_manager))

    @_api_handler
    async def wr_batch_delete(self):
        data = await _json_body()
        entry_ids = data.get("entry_ids", [])
        return json_response(await handle_wr_batch_delete(self.wr_manager, entry_ids))

    @_api_handler
    async def wr_batch_toggle(self):
        data = await _json_body()
        entry_ids = data.get("entry_ids", [])
        enabled = bool(data.get("enabled", True))
        return json_response(await handle_wr_batch_toggle(self.wr_manager, entry_ids, enabled))

    # ── WB ────────────────────────────────────────────────────

    @_api_handler
    async def wb_list(self):
        return json_response(await handle_wb_list(self.wb_manager))

    @_api_handler
    async def wb_get(self):
        data = await _json_body()
        return json_response(
            await handle_wb_get(self.wb_manager, data.get("name"))
        )

    @_api_handler
    async def wb_create(self):
        data = await _json_body()
        return json_response(
            await handle_wb_create(
                self.wb_manager,
                data.get("name"),
                data.get("description", ""),
            )
        )

    @_api_handler
    async def wb_delete(self):
        data = await _json_body()
        return json_response(
            await handle_wb_delete(self.wb_manager, data.get("name"))
        )

    @_api_handler
    async def wb_entry_create(self):
        data = await _json_body()
        return json_response(
            await handle_wb_entry_create(
                self.wb_manager,
                data.get("name"),
                data.get("entry"),
            )
        )

    @_api_handler
    async def wb_entry_update(self):
        data = await _json_body()
        entry_id = data.get("entry_id", data.get("id"))
        return json_response(
            await handle_wb_entry_update(
                self.wb_manager,
                data.get("name"),
                entry_id,
                data.get("entry"),
            )
        )

    @_api_handler
    async def wb_entry_delete(self):
        data = await _json_body()
        # 安全防御：兼容前端传 id 或 entry_id 的情况
        entry_id = data.get("entry_id", data.get("id"))
        return json_response(
            await handle_wb_entry_delete(
                self.wb_manager,
                data.get("name"),
                entry_id,
            )
        )

    @_api_handler
    async def wb_import_st(self):
        """上传 ST 格式 lorebook 文件并导入。"""
        # M6.2：此前是 limit=None（无大小上限）——请求体会被**全量读进内存**
        # 再落盘到 imports/，一次性上传即可放大内存占用。世界书是纯文本设定集，
        # 50MB（= RAG 文档通道的 DEFAULT_LIMIT）已远超任何真实用例，故收敛一致。
        try:
            payload = await read_upload(request, keys=("file",), limit=DEFAULT_LIMIT)
        except UploadTooLarge:
            return error_response("世界书文件过大（最大 50MB）", status_code=413)
        except UploadError as e:
            return error_response(error_text("Base64 解码失败", e), status_code=400)
        if payload is None:
            return error_response("未收到文件", status_code=400)

        form = await request.form()
        name = form.get("name")
        if not name:
            return error_response("缺少世界书名称", status_code=400)

        # S1-8 修复：校验 name 防止路径遍历（../、/、\ 等）
        from .worldbook import _validate_name
        if not _validate_name(name):
            return error_response("无效的世界书名称", status_code=400)

        if not payload.data:
            return error_response("上传的文件为空", status_code=400)

        if self.plugin is None or not getattr(self.plugin, "paths", None):
            return error_response("插件实例不可用", status_code=500)
        # 写入导入暂存目录（数据根下的 imports/，不再写进插件目录）。
        # 落盘走线程池：同步 write_bytes 在事件循环上会阻塞整台 bot 的聊天管道。
        target_dir = Path(self.plugin.paths["imports_dir"])
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((target_dir / f"{name}.json").write_bytes, payload.data)

        return json_response(
            await handle_wb_import_st(self.wb_manager, name, payload.data)
        )

    @_api_handler
    async def wb_import_json(self):
        """接收 JSON 文本数据并导入世界书（绕过沙盒 FormData 限制）。"""
        data = await _json_body()
        # 显式传 null 时 .get 的默认值不生效（键存在、值为 None），
        # 直接 .strip() 会抛 AttributeError 被兜成 500。
        name = (data.get("name") or "").strip()
        file_data = data.get("data", "")
        if not name:
            return error_response("缺少世界书名称", status_code=400)
        if not file_data:
            return error_response("未收到文件数据", status_code=400)
        # L7：data 字段是数组/对象/数字时，下面 f.write 会抛 TypeError →
        # 被 _api_handler 兜成 500。参数问题应当是 400。
        if not isinstance(file_data, str):
            return error_response("data 字段必须是世界书 JSON 文本", status_code=400)
        # 写入临时文件供 import_from_st 解析
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(file_data)
            if not await asyncio.to_thread(self.wb_manager.import_from_st, tmp_path, name):
                return error_response("导入世界书失败", status_code=400)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return json_response({"name": name, "message": "Worldbook imported"})

    @_api_handler
    async def wb_export_st(self):
        name = request.query.get("name")
        return json_response(
            await handle_wb_export_st(self.wb_manager, name)
        )

    # ── WB Reload ─────────────────────────────────────────────

    @_api_handler
    async def wb_reload(self):
        """重新从磁盘加载所有世界书后返回列表（供面板刷新按钮用）。"""
        try:
            # reload_all 是同步方法（加锁后重新读盘），不能 await。
            # 它内部做文件 IO，放到线程里避免阻塞事件循环。
            await asyncio.to_thread(self.wb_manager.reload_all)
        except Exception:
            logger.warning("[Quill] 世界书重载失败，返回当前缓存列表", exc_info=True)
        return json_response(await handle_wb_list(self.wb_manager))

    # ── Persona 角色卡 (Quill 独立管理) ──────────────────────────

    @_api_handler
    async def persona_list(self):
        """返回所有角色卡（不含头像数据，前端通过 /persona/avatar 懒加载）。"""
        if not self.persona_manager:
            return json_response([])
        personas = await self.persona_manager.load_all()
        for p in personas:
            avatar_path = p.get("avatar_path", "")
            p["has_avatar"] = bool(avatar_path and avatar_path.startswith("quill_avatars/"))
            p.pop("avatar_url", None)  # 不内联 Base64，前端按需懒加载
        return json_response(personas)

    @_api_handler
    async def persona_avatar(self):
        """单独获取某个角色卡的头像（Base64 data URL），供前端懒加载。"""
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        persona_id = (request.query.get("id") or "").strip()
        if not persona_id:
            return error_response("缺少角色 ID", status_code=400)
        persona = await self.persona_manager.get_persona(persona_id)
        if not persona:
            return error_response("角色卡不存在", status_code=404)
        avatar_path = persona.get("avatar_path", "")
        if not avatar_path or not avatar_path.startswith("quill_avatars/"):
            return json_response({"avatar_url": ""})
        fname = avatar_path[len("quill_avatars/"):]
        data = await self.persona_manager.read_avatar(fname)
        if not data:
            return json_response({"avatar_url": ""})
        import base64
        ext = os.path.splitext(fname)[1].lower()
        mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
        mime = mime_map.get(ext, 'image/png')
        return json_response({"avatar_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"})

    @_api_handler
    async def persona_create(self):
        """创建角色卡。"""
        data = await _json_body()
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        try:
            result = await self.persona_manager.create_persona(data)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)

    @_api_handler
    async def persona_update(self):
        """更新角色卡（支持部分更新）。"""
        data = await _json_body()
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return error_response("缺少 id 参数", status_code=400)
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        try:
            result = await self.persona_manager.update_persona(persona_id, data)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)

    @_api_handler
    async def persona_delete(self):
        """删除角色卡。"""
        data = await _json_body()
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return error_response("缺少 id 参数", status_code=400)
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        try:
            await self.persona_manager.delete_persona(persona_id)
            return json_response({"id": persona_id, "message": "Persona deleted"})
        except ValueError as e:
            return error_response(str(e), status_code=404)

    # ── Persona 扩展功能：头像上传 / V2 导入导出 ──────────────────

    @_api_handler
    async def upload_avatar(self):
        """上传头像图片（multipart 文件 / base64 双通道统一入口，M3.1 收敛）。

        ``/upload_avatar``（multipart）与 ``/upload_avatar_base64``
        （JSON+b64_data）共用同一 handler；5MB 上限与通道消歧收敛于
        upload.read_upload。
        """
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)

        try:
            payload = await read_upload(
                request, keys=("file",), limit=CARD_LIMIT, default_filename="avatar.png"
            )
        except UploadTooLarge:
            return error_response("图片文件过大（最大 5MB）", status_code=413)
        except UploadError as e:
            return error_response(error_text("Base64 解码失败", e), status_code=400)
        if payload is None:
            return error_response("未收到文件", status_code=400)

        try:
            rel_path = await self.persona_manager.save_avatar(payload.filename, payload.data)
            url = f"/{PLUGIN_NAME}/avatar/{os.path.basename(rel_path)}"
            return json_response({"url": url, "path": rel_path, "message": "Avatar uploaded"})
        except Exception as e:
            return error_response(error_text("保存失败", e), status_code=500)

    @_api_handler
    async def persona_import(self):
        """导入 V2 角色卡（multipart 文件 / base64 双通道统一入口，M3.1 收敛）。

        扩展名白名单为原两通道并集（含 .webp，BASELINE §8.1 决策）；
        5MB 上限与通道消歧收敛于 upload.read_upload。
        """
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)

        try:
            payload = await read_upload(
                request, keys=("file",), limit=CARD_LIMIT, default_filename="card.png"
            )
        except UploadTooLarge:
            return error_response("文件过大（最大 5MB）", status_code=413)
        except UploadError as e:
            return error_response(error_text("Base64 解码失败", e), status_code=400)
        if payload is None:
            return error_response("未收到文件", status_code=400)

        filename = payload.filename.lower()
        ext = os.path.splitext(filename)[1].lower()
        # 处理 .card.png 等特殊扩展名
        if filename.endswith('.card.png'):
            ext = '.png'
        elif ext not in CARD_IMPORT_EXTS:
            return error_response("不支持的文件格式（支持 PNG/JPG/WebP/JSON）", status_code=400)

        try:
            is_image = ext in CARD_IMAGE_EXTS
            persona_data = await asyncio.to_thread(self.persona_manager.parse_v2_card, payload.data, is_image)

            # Issue #1：内嵌世界书（character_book）自动落盘并绑定（配置可关）
            wb_info = await self.persona_manager.attach_embedded_worldbook(
                persona_data, self.wb_manager,
                enabled=getattr(self.config, "worldbook_card_embed_import", True),
            )

            # 如果是图片，保存为头像（头像保存失败不影响角色卡导入）
            if is_image:
                try:
                    avatar_filename = f"{persona_data['name']}{ext}"
                    avatar_path = await self.persona_manager.save_avatar(avatar_filename, payload.data)
                    persona_data["avatar_path"] = avatar_path
                except Exception as av_e:
                    logger.warning(f"[Quill] 头像保存失败（角色卡仍会导入）: {av_e}")

            result = await self.persona_manager.create_persona(persona_data)
            if wb_info:
                result["embedded_worldbook"] = wb_info
            return json_response(result)

        except ImportError as e:
            return error_response(str(e), status_code=501)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(error_text("导入失败", e), status_code=500)

    @_api_handler
    async def persona_export(self):
        """导出 V2 角色卡。"""
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)

        persona_id = (request.query.get("id") or "").strip()
        if not persona_id:
            return error_response("缺少角色 ID", status_code=400)

        persona = await self.persona_manager.get_persona(persona_id)
        if not persona:
            return error_response("角色卡不存在", status_code=404)

        try:
            # 尝试读取头像
            avatar_data = None
            avatar_path = persona.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                avatar_filename = os.path.basename(avatar_path)
                avatar_data = await self.persona_manager.read_avatar(avatar_filename)

            # 导出为 V2（PIL 图片编码为同步 CPU 操作，放线程池避免阻塞事件循环）
            export_data = await asyncio.to_thread(
                self.persona_manager.export_v2_card, persona, avatar_data
            )

            # 返回文件下载（P3-5 修复：file_response 仅支持路径，bytes 需用 Response + 下载头）
            if avatar_data:
                return Response(
                    export_data,
                    media_type="image/png",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(persona['name'])}_v2.png"}
                )
            else:
                return Response(
                    export_data,
                    media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(persona['name'])}_v2.json"}
                )

        except ImportError as e:
            return error_response(str(e), status_code=501)
        except Exception as e:
            return error_response(error_text("导出失败", e), status_code=500)

    @_api_handler
    async def persona_import_text(self):
        """从剪贴板文本导入角色卡（text 直传 / b64_text 绕沙箱，M3.1 收敛）。

        ``/persona/import_text``（text 字段）与 ``/persona/import_text_base64``
        （b64_text 字段，面板 bridge 通道）共用同一 handler。
        """
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        data = await _json_body()
        text = (data.get("text") or "").strip()
        if not text:
            # 沙箱 bridge 通道：文本以 Base64 编码放 b64_text 字段传输
            b64_text = (data.get("b64_text") or "").strip()
            if b64_text:
                try:
                    text = decode_base64(b64_text).decode('utf-8')
                except Exception as e:
                    return error_response(error_text("Base64 解码失败", e), status_code=400)
        if not text:
            return error_response("缺少 text 参数", status_code=400)
        try:
            # L10 修复：`parse_clipboard_text` 是纯同步函数，里面有一次
            # `json.loads` 加多条正则匹配；SillyTavern 角色卡正文动辄几万字，
            # 直接在这里调用会**阻塞事件循环**（同一进程内的群聊消息、SSE
            # 流式输出、其它面板请求全部卡住）。丢到线程池执行。
            persona_data = await asyncio.to_thread(
                self.persona_manager.parse_clipboard_text, text
            )
            # Issue #1：V2/V3 JSON 文本同样可能携带内嵌世界书
            wb_info = await self.persona_manager.attach_embedded_worldbook(
                persona_data, self.wb_manager,
                enabled=getattr(self.config, "worldbook_card_embed_import", True),
            )
            result = await self.persona_manager.create_persona(persona_data)
            if wb_info:
                result["embedded_worldbook"] = wb_info
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(error_text("解析失败", e), status_code=400)

    # ── AstrBot 人格 → 角色卡（M7） ───────────────────────────

    def _summary_provider_id(self) -> str:
        """「用于生成摘要的模型」= 面板 RAG 页配置的 LLM 提供商。

        与 QuillSummarizer 取同一个配置项（config.rag_llm_provider_id）：用户已经
        为摘要配过模型，不该再为导入配第二次；也没必要多一个配置项。
        """
        if self.config is None:
            return ""
        return str(getattr(self.config, "rag_llm_provider_id", "") or "").strip()

    def _persona_source(self) -> AstrBotPersonaSource:
        return AstrBotPersonaSource(self.context)

    async def _card_names(self) -> set:
        """现有角色卡名集合（重名预检用）。读取失败按空集合处理，不阻断导入。"""
        if not self.persona_manager:
            return set()
        try:
            personas = await self.persona_manager.load_all()
        except Exception as e:
            logger.warning("[Quill Persona] 读取角色卡列表失败: %s", e)
            return set()
        return {str(p.get("name") or "") for p in personas or []}

    async def _create_card_with_unique_name(self, card: dict) -> tuple:
        """落卡；重名 / 文件名冲突时自动加后缀重试。

        重试而不是直接报错：转换已经花掉一次 LLM 调用，让用户为了一个重名再点一次
        「导入」既慢又费 token。加的后缀通过 ``renamed_from`` 如实回报给面板 toast，
        不是静默改名；后缀全撞满仍失败才抛错（调用方转 400）。
        """
        base = str(card.get("name") or "").strip()
        for i in range(6):
            if i == 0:
                candidate = _clip_name(base)
            else:
                tail = "（导入）" if i == 1 else f"（导入{i}）"
                candidate = _clip_name(base, CARD_NAME_MAX - 8) + tail
            try:
                created = await self.persona_manager.create_persona({**card, "name": candidate})
                return created, ("" if i == 0 else base)
            except ValueError as e:
                msg = str(e)
                if "已存在" in msg or "文件名冲突" in msg:
                    continue
                raise
        raise ValueError(f"角色名 '{base}' 及其导入变体都已存在，请手动指定一个名称")

    @_api_handler
    async def persona_astrbot_list(self):
        """列出 AstrBot 面板里已配置的人格（「由人格导入」的选择列表）。"""
        source = self._persona_source()
        converter = PersonaCardConverter(self.context, provider_id=self._summary_provider_id(),
                                         usage_recorder=getattr(self.plugin, "_usage", None))
        personas = await source.list_personas()
        existing = await self._card_names()
        for item in personas:
            # persona_id 就是 AstrBot 里的显示名，多半会被直接当角色卡名用：先标出来，
            # 面板可以提前提示，避免转换跑完才撞重名。
            item["name_taken"] = item.get("persona_id") in existing
        return json_response({
            "available": source.available,
            "reason": "" if source.available else
                      "当前 AstrBot 未向插件提供人格管理器（persona_manager），无法读取人格",
            "personas": personas,
            "count": len(personas),
            "provider_id": converter.provider_id,
            "provider_ready": converter.ready,
        })

    @_api_handler
    async def persona_astrbot_import(self):
        """把选中的 AstrBot 人格转成角色卡并落卡。

        请求体 ``{persona_id, name?}``（``name`` 为空时由模型决定角色名）。
        返回 ``{persona, renamed_from, source_persona, provider_id}``。
        """
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        data = await _json_body()
        persona_id = str(data.get("persona_id") or "").strip()
        if not persona_id:
            return error_response("缺少 persona_id 参数", status_code=400)
        name = str(data.get("name") or "").strip()

        source = self._persona_source()
        if not source.available:
            return error_response(
                "当前 AstrBot 未向插件提供人格管理器，无法导入人格", status_code=400
            )
        raw = await source.get_persona(persona_id)
        if raw is None:
            return error_response(f"人格不存在：{persona_id}", status_code=404)
        if name and name in await self._card_names():
            # 用户显式指定的名字撞重名：先报错，别白跑一次 LLM
            return error_response(f"角色名 '{name}' 已存在", status_code=400)

        converter = PersonaCardConverter(self.context, provider_id=self._summary_provider_id(),
                                         usage_recorder=getattr(self.plugin, "_usage", None))
        try:
            card = await converter.convert(raw, name_override=name)
        except PersonaImportError as e:
            return error_response(str(e), status_code=400)
        try:
            created, renamed_from = await self._create_card_with_unique_name(card)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        return json_response({
            "persona": created,
            "renamed_from": renamed_from,
            "source_persona": persona_id,
            "provider_id": converter.provider_id,
        })

    @_api_handler
    async def persona_export_base64(self):
        """导出 V2 角色卡（Base64 编码，突破沙盒下载限制）。"""
        if not self.persona_manager:
            return error_response("角色卡管理器未加载", status_code=500)
        data = await _json_body()
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return error_response("缺少角色 ID", status_code=400)
        persona = await self.persona_manager.get_persona(persona_id)
        if not persona:
            return error_response("角色卡不存在", status_code=404)
        try:
            import os
            import base64
            avatar_data = None
            avatar_path = persona.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                avatar_filename = os.path.basename(avatar_path)
                avatar_data = await self.persona_manager.read_avatar(avatar_filename)
            export_data = await asyncio.to_thread(
                self.persona_manager.export_v2_card, persona, avatar_data
            )
            filename = f"{persona['name']}_v2.png" if avatar_data else f"{persona['name']}_v2.json"
            b64_str = base64.b64encode(export_data).decode('ascii')
            return json_response({"filename": filename, "b64_data": b64_str})
        except Exception as e:
            return error_response(error_text("导出失败", e), status_code=500)

    async def _build_backup_zip(self) -> tuple[bytes, str, int, list[str]]:
        """Build one consistent zip snapshot from plugin data directories.

        数据库文件走 SQLite 在线备份 API 生成一致快照（WAL 中未 checkpoint 的
        已提交事务也包含在内），而不是裸拷主文件——否则热备会静默丢掉最近提交
        （实测出现过「备份里 0 条记忆，实际 2 条」）。快照失败时退回裸拷并把原因
        记入 warnings，交由调用方透出，不静默降级。

        归档范围由 _paths.backup_sources 决定：数据根就位后整根打包
        （knowledge/、worldbooks/、状态文件落在顶层），旧布局则维持
        data/、knowledge/、worldbooks/ 三个目录名的历史格式。
        """
        import io
        import datetime as _dt

        plugin_root = os.path.dirname(os.path.abspath(__file__))
        paths = getattr(self.plugin, "paths", None) or {}
        sources = backup_sources(paths, plugin_root)
        if not sources:
            raise FileNotFoundError("数据目录不存在")

        buf = io.BytesIO()
        try:
            # M6：整个归档构建在内存 BytesIO 里，导出侧必须与恢复侧同一个上限，
            # 否则「备份做得出、恢复装不回」——数据根膨胀时备份本身还会吃光内存。
            zip_count, warnings = await asyncio.to_thread(
                build_backup_zip, sources, buf, _MAX_RESTORE_BYTES
            )
        except BackupTooLarge as e:
            # M6：超限是「用户可自行解决」的失败（清数据再试），文案直接透出，
            # 不落进下面的通用 500 分支。
            note_storage_error("backup", e)
            raise StorageError(str(e), detail=f"_build_backup_zip: {e}") from e
        except Exception as e:
            # M3.2 D4：备份失败抛 StorageError（六类高频路径之一：backup），
            # 由上层 @_api_handler 转为 500 信封——与改前未捕获异常路径一致，
            # 但现在可观测（计数 + 异常链）。
            note_storage_error("backup", e)
            raise StorageError(
                "生成备份归档失败",
                detail=f"_build_backup_zip: {e}",
            ) from e
        for w in warnings:
            logger.warning("[Quill] 备份快照降级: %s", w)
        fname = (
            f"quill_backup_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        return buf.getvalue(), fname, zip_count, warnings

    @_api_handler
    async def backup_export(self):
        """Export all plugin data as a binary zip download."""
        if self._maintenance_lock.locked():
            return error_response("备份/恢复正在进行中，请稍后再试", status_code=409)
        async with self._maintenance_lock:
            try:
                raw, fname, zip_count, warnings = await self._build_backup_zip()
            except FileNotFoundError as e:
                return error_response(str(e), status_code=404)
        logger.info(f"[Quill] 全量备份导出: {zip_count} 个文件")
        return Response(
            raw,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )

    @_api_handler
    async def backup_export_base64(self):
        """Return a zip snapshot as JSON for sandboxed Plugin Page downloads."""
        import base64
        if self._maintenance_lock.locked():
            return error_response("备份/恢复正在进行中，请稍后再试", status_code=409)
        async with self._maintenance_lock:
            try:
                raw, filename, count, warnings = await self._build_backup_zip()
            except FileNotFoundError as e:
                return error_response(str(e), status_code=404)
        logger.info("[Quill] 全量备份导出(Base64): %d 个文件", count)
        payload = {
            "filename": filename,
            "b64_data": base64.b64encode(raw).decode("ascii"),
            "size": len(raw),
        }
        if warnings:
            payload["warnings"] = warnings
        return json_response(payload)

    @_api_handler
    async def backup_restore(self):
        """P2-3: 从备份 zip 恢复数据（原始二进制上传）。

        审查修复版：
        - zip slip 防护统一为"白名单前缀 + normpath 边界校验 + 以校验后的
          dest 为落点手写文件"（此前校验 dest 却用 zf.extract 按原名解压，
          两套路径逻辑不一致）；目录白名单限定 data/ knowledge/ worldbooks/，
          防止覆盖插件源码。
        - 恢复前先由 plugin._reload_after_restore 关闭旧 DB 句柄并停 autoflush
          （否则 Windows 下覆盖运行中的 SQLite 会读到错乱页，且旧内存态会把
          恢复的 quill_state.json 反向覆盖回去）。
        - 逐文件容错：单个文件失败不中断整体恢复，失败数如实返回。
        """
        # 先按 Content-Length 拒一次，避免为了报「过大」而先把整个请求体读进内存。
        declared = _declared_length(request)
        if declared is not None and declared > _MAX_RESTORE_BYTES:
            return error_response(
                f"备份文件过大（最大 {_MAX_RESTORE_BYTES // (1024 * 1024)}MB）",
                status_code=413,
            )
        raw = await request.body()
        if not raw:
            return error_response("请上传备份文件", status_code=400)
        if self._maintenance_lock.locked():
            return error_response("备份/恢复正在进行中，请稍后再试", status_code=409)
        async with self._maintenance_lock:
            return await self._do_restore_bytes(raw)

    @_api_handler
    async def backup_restore_base64(self):
        """P2-3: 从备份 zip 恢复数据（Base64 JSON 模式）。

        面板 iframe 是无 allow-same-origin 的受限沙箱，无法携带 Dashboard
        认证直连 fetch——前端统一走 bridge（JSON）上传，此端点为此而设。
        """
        import base64
        data = await _json_body()
        b64 = (data.get("b64_data") or "").strip()
        if not b64:
            return error_response("缺少 b64_data 字段", status_code=400)
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return error_response("Base64 解码失败", status_code=400)
        if self._maintenance_lock.locked():
            return error_response("备份/恢复正在进行中，请稍后再试", status_code=409)
        async with self._maintenance_lock:
            return await self._do_restore_bytes(raw)

    async def _do_restore_bytes(self, raw: bytes):
        import io
        import zipfile
        plugin_root = os.path.dirname(os.path.abspath(__file__))
        paths = getattr(self.plugin, "paths", None) or {}

        # M6.2：恢复包上限。此前 body/base64 无任何体积约束，配合下面的
        # 全量 CRC 校验，一个高压缩比的小包就能撑爆内存 + 冻结事件循环。
        if len(raw) > _MAX_RESTORE_BYTES:
            return error_response(
                f"备份文件过大（最大 {_MAX_RESTORE_BYTES // (1024 * 1024)}MB）",
                status_code=413,
            )

        # Fully validate the archive before touching live databases or
        # stopping autoflush. A corrupt/irrelevant zip must not evict runtime
        # state or overwrite existing data.
        #
        # M6.2：校验（testzip 会对整包逐条 CRC 解压 + 每个 .db 读头嗅探）是
        # CPU/IO 密集操作，此前直接在事件循环上跑 —— 校验期间台 bot 的所有
        # 聊天管道与面板请求一起卡住。这里整体丢进线程池；函数只读 raw 与本
        # 地文件，不碰任何共享状态，线程安全。
        def _validate() -> list:
            candidates: list = []
            bad_db: list[str] = []
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                bad_file = zf.testzip()
                if bad_file:
                    raise ValueError(f"备份文件损坏: {bad_file}")
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # 归档条目名 -> 数据根下的绝对落点（同时过滤路径穿越与非法前缀）
                    dest = resolve_archive_dest(paths, plugin_root, info.filename)
                    if dest is None:
                        continue
                    candidates.append((info, dest))
                    # 覆盖在线库前先确认归档里确实是 SQLite 文件：把非数据库
                    # 字节写进 quill_wr.db 会直接毁掉现有数据。只读头部若干字节，
                    # 不必把整个条目解压进内存。
                    if dest.endswith(".db"):
                        with zf.open(info) as fh:
                            if not is_sqlite_bytes(fh.read(16)):
                                bad_db.append(info.filename)
            if bad_db:
                raise ValueError(
                    "备份中的数据库文件已损坏，拒绝恢复: " + ", ".join(bad_db)
                )
            return candidates

        try:
            candidates = await asyncio.to_thread(_validate)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except (zipfile.BadZipFile, OSError) as e:
            return error_response(error_text("无效的备份文件", e), status_code=400)
        if not candidates:
            return error_response("备份中没有可恢复的数据文件", status_code=400)

        extracted_count = 0
        skipped_count = 0
        failed_files: list[str] = []
        stale_sidecars: list[str] = []

        def _extract():
            nonlocal extracted_count, skipped_count
            # M6 修复：解压总量上限。请求体上限（_MAX_RESTORE_BYTES）管的是
            # **压缩包**大小，而 zip 的压缩比可以做到 1000:1 —— 一个 1MB 的
            # 包能解出 1GB。此前用 `f.write(zf.read(info))` 把每条一次性读进
            # 内存，再叠加「解压总量无上限」，等于给了一个内存耗尽的入口。
            # 这里两件事一起改：流式拷贝（不再整条进内存）+ 累计解压字节上限。
            total_written = 0
            with zipfile.ZipFile(io.BytesIO(raw), 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    dest = resolve_archive_dest(paths, plugin_root, info.filename)
                    # 非数据条目（例如归档里混入 .py）一律跳过，绝不写插件代码
                    if dest is None:
                        skipped_count += 1
                        continue
                    name = info.filename.replace("\\", "/")
                    if total_written + info.file_size > _MAX_RESTORE_BYTES:
                        raise ValueError(
                            "备份解压后体积过大（超过 "
                            f"{_MAX_RESTORE_BYTES // (1024 * 1024)}MB），已中止恢复"
                        )
                    try:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        # 覆盖数据库前先删掉上一次运行留下的 -wal/-shm/-journal：
                        # 旧 WAL 记录的是**旧库**的页，SQLite 重开恢复后的主文件时
                        # 会把它当成自己的日志重放，静默回退/损坏刚恢复的数据。
                        if dest.endswith(".db"):
                            stale_sidecars.extend(remove_sidecars(dest))
                        # 以校验后的 dest 为落点手写（不使用 zf.extract 的自有路径逻辑）
                        written = 0
                        with zf.open(info) as src, open(dest, "wb") as f:
                            while True:
                                chunk = src.read(_RESTORE_CHUNK_BYTES)
                                if not chunk:
                                    break
                                written += len(chunk)
                                # 以实际写出的字节为准（info.file_size 是压缩包里
                                # 声明的值，不能作为唯一依据）。
                                if total_written + written > _MAX_RESTORE_BYTES:
                                    raise ValueError(
                                        "备份解压后体积过大（超过 "
                                        f"{_MAX_RESTORE_BYTES // (1024 * 1024)}MB），"
                                        "已中止恢复"
                                    )
                                f.write(chunk)
                        total_written += written
                        extracted_count += 1
                    except OSError as e:
                        failed_files.append(name)
                        logger.warning("[Quill] 备份恢复: 写入 %s 失败: %s", name, e)

        # 顺序关键：先停 autoflush + 关闭持有 DB 句柄的组件，再覆盖文件
        plugin = self.plugin
        if plugin is not None and hasattr(plugin, "_prepare_for_restore"):
            try:
                await plugin._prepare_for_restore()
            except Exception as e:
                note_storage_error("restore", e)
                logger.warning("[Quill] 备份恢复前组件关闭失败: %s", e, exc_info=True)

        # 解压失败（zip 条目 CRC 损坏 / 加密等）也必须走到重建：_prepare_for_restore
        # 已经把 wr_manager 置 None，若在此中断，组件会一直停在「已准备」状态，
        # 面板与聊天路径直到重启都不可用。
        extract_error = None
        try:
            await asyncio.to_thread(_extract)
        except Exception as e:
            extract_error = e
            note_storage_error("restore", e)
            logger.warning("[Quill] 备份恢复: 解压中断: %s", e, exc_info=True)
        logger.info(
            f"[Quill] 备份恢复: 解压 {extracted_count} 个文件, "
            f"跳过 {skipped_count} 个, 失败 {len(failed_files)} 个"
        )
        if stale_sidecars:
            logger.info(
                "[Quill] 备份恢复: 已清理 %d 个陈旧数据库 sidecar", len(stale_sidecars)
            )

        # 解压完成 → 全量重建数据组件 → 刷新 Web 路由引用
        plugin = self.plugin
        reload_ok = False
        if plugin is not None and hasattr(plugin, "_reload_after_restore"):
            try:
                await plugin._reload_after_restore()
                reload_ok = True
            except Exception as e:
                note_storage_error("restore", e)
                logger.warning("[Quill] 备份恢复后组件重建失败: %s", e, exc_info=True)

        msg = f"已恢复 {extracted_count} 个文件（跳过 {skipped_count} 个）"
        if failed_files:
            msg += f"；{len(failed_files)} 个文件写入失败（详见服务端日志）"
        if extract_error is not None:
            msg += f"；解压中断（{type(extract_error).__name__}），数据可能不完整，建议重新恢复"
        if not reload_ok:
            msg += "；组件热重载失败，建议重启 AstrBot 或重载插件"
        return json_response({
            "status": "ok",
            "data": {
                "extracted": extracted_count,
                "skipped": skipped_count,
                "failed": len(failed_files),
                "reload_ok": reload_ok,
                "message": msg,
            }
        })
