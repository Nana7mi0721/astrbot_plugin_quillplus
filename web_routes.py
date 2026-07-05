# -*- coding: utf-8 -*-
"""Quill Web API routes — AstrBot v4.26+ register_web_api 模式。

所有 handler 使用 astrbot.api.web 的请求/响应抽象，
与 FastAPI/Quart/Starlette 底层实现解耦。

_route_core.py 提供纯 async handler（零 HTTP 依赖），
本文件只做 HTTP ↔ 核心逻辑的适配。
"""

import asyncio
import os
import tempfile
from functools import wraps
from pathlib import Path

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from ._route_core import (
    handle_kb_list,
    handle_kb_get,
    handle_kb_create,
    handle_kb_update,
    handle_kb_delete,
    handle_kb_toggle,
    handle_kb_export,
    handle_kb_import,
    handle_kb_test,
    handle_kb_categories,
    handle_wb_list,
    handle_wb_get,
    handle_wb_create,
    handle_wb_delete,
    handle_wb_entry_create,
    handle_wb_entry_update,
    handle_wb_entry_delete,
    handle_wb_import_st,
    handle_wb_export_st,
    handle_wb_bindings,
    handle_wb_bind,
    handle_wb_unbind,
    handle_rag_upload,
    handle_rag_documents,
    handle_rag_delete,
    handle_rag_search,
    handle_rag_config,
    handle_memory_list,
    handle_memory_delete,
    handle_memory_list_all,
    handle_provider_list,
    handle_memory_export,
    handle_memory_import,
)

PLUGIN_NAME = "astrbot_plugin_quillplus"


def _api_handler(handler):
    """统一的 handler 异常捕获装饰器。

    任何未捕获异常转为 500 error_response，避免向前端泄露堆栈。
    """
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except Exception as e:
            return error_response(str(e), status_code=500)
    return wrapper


class QuillRoutes:
    """Quill 插件后端 Web API 路由注册器。

    用法::

        QuillRoutes(kb_manager, wb_manager, context).register_all()
    """

    def __init__(self, kb_manager, wb_manager, context, config=None,
                 rag_components=None, plugin=None, persona_manager=None):
        self.kb_manager = kb_manager
        self.wb_manager = wb_manager
        self.context = context
        self.config = config
        self.plugin = plugin  # 直接引用插件实例，避免反射查找
        self.persona_manager = persona_manager
        # rag_components: dict with keys: embedding, vector_store, reranker, memory_store, summarizer
        self.rag = rag_components or {}

    # ── 路由注册入口 ──────────────────────────────────────────

    def register_all(self):
        """一次性注册全部 Web API 路由（包含前端 panel 调用的全部端点）。"""
        _r = self.context.register_web_api

        # Panel HTML 页面
        _r(f"/{PLUGIN_NAME}/panel", self.serve_panel, ["GET"], "Quill 插件面板")

        # ── KB 写作素材库 (11 个) ──
        _r(f"/{PLUGIN_NAME}/kb/list",         self.kb_list,        ["GET"],    "列出写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/get",          self.kb_get,         ["POST"],   "获取单个写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/create",       self.kb_create,      ["POST"],   "创建写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/update",       self.kb_update,      ["POST"],   "更新写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/delete",       self.kb_delete,      ["POST"],   "删除写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/toggle",       self.kb_toggle,      ["POST"],   "启用/禁用写作素材库条目")
        _r(f"/{PLUGIN_NAME}/kb/export",       self.kb_export,      ["GET"],    "导出写作素材库")
        _r(f"/{PLUGIN_NAME}/kb/import",       self.kb_import,      ["POST"],   "导入写作素材库")
        _r(f"/{PLUGIN_NAME}/kb/test",         self.kb_test,        ["POST"],   "测试写作素材库匹配")
        _r(f"/{PLUGIN_NAME}/kb/categories",   self.kb_categories,  ["GET"],    "列出写作素材库分类")

        # ── WB 世界书 (13 个：含 reload) ──
        _r(f"/{PLUGIN_NAME}/wb/list",         self.wb_list,        ["GET"],    "列出世界书")
        _r(f"/{PLUGIN_NAME}/wb/get",          self.wb_get,         ["POST"],   "获取世界书详情")
        _r(f"/{PLUGIN_NAME}/wb/create",       self.wb_create,      ["POST"],   "创建世界书")
        _r(f"/{PLUGIN_NAME}/wb/delete",       self.wb_delete,      ["POST"],   "删除世界书")
        _r(f"/{PLUGIN_NAME}/wb/delete_book",  self.wb_delete_book, ["POST"],   "删除整本世界书")
        _r(f"/{PLUGIN_NAME}/wb/reload",       self.wb_reload,      ["POST"],   "重新加载世界书")
        _r(f"/{PLUGIN_NAME}/wb/entry/create", self.wb_entry_create,["POST"],   "创建世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/entry/update", self.wb_entry_update,["POST"],   "更新世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/entry/delete", self.wb_entry_delete,["POST"],   "删除世界书条目")
        _r(f"/{PLUGIN_NAME}/wb/import_st",    self.wb_import_st,   ["POST"],   "导入 ST 格式世界书")
        _r(f"/{PLUGIN_NAME}/wb/import_json",  self.wb_import_json, ["POST"],   "导入 JSON(原生/ST)")
        _r(f"/{PLUGIN_NAME}/wb/export_st",    self.wb_export_st,   ["GET"],    "导出 ST 格式世界书")
        _r(f"/{PLUGIN_NAME}/wb/bindings",     self.wb_bindings,    ["GET"],    "获取世界书绑定")
        _r(f"/{PLUGIN_NAME}/wb/bind",         self.wb_bind,        ["POST"],   "绑定世界书")
        _r(f"/{PLUGIN_NAME}/wb/unbind",       self.wb_unbind,      ["POST"],   "解绑世界书")

        # ── Persona 角色卡 (独立 Quill 管理 + V2 兼容) ──
        _r(f"/{PLUGIN_NAME}/persona/list",          self.persona_list,   ["GET"],    "列出角色卡")
        _r(f"/{PLUGIN_NAME}/persona/create",        self.persona_create, ["POST"],   "创建角色卡")
        _r(f"/{PLUGIN_NAME}/persona/update",        self.persona_update, ["POST"],   "更新角色卡")
        _r(f"/{PLUGIN_NAME}/persona/delete",        self.persona_delete, ["POST"],   "删除角色卡")
        _r(f"/{PLUGIN_NAME}/upload_avatar",         self.upload_avatar,  ["POST"],   "上传头像(文件)")
        _r(f"/{PLUGIN_NAME}/upload_avatar_base64",  self.upload_avatar_base64, ["POST"], "上传头像(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/import",        self.persona_import, ["POST"],   "导入V2角色卡(文件)")
        _r(f"/{PLUGIN_NAME}/persona/import_base64", self.persona_import_base64, ["POST"], "导入V2角色卡(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/export",        self.persona_export, ["GET"],    "导出V2角色卡")
        _r(f"/{PLUGIN_NAME}/persona/export_base64",     self.persona_export_base64, ["POST"], "导出V2角色卡(Base64)")
        _r(f"/{PLUGIN_NAME}/persona/import_text",        self.persona_import_text, ["POST"], "文本导入角色卡")
        _r(f"/{PLUGIN_NAME}/persona/import_text_base64", self.persona_import_text_base64, ["POST"], "文本导入角色卡(Base64)")
        _r(f"/{PLUGIN_NAME}/avatar/<path:filename>", self.serve_avatar,  ["GET"],    "获取头像文件")

        # ── Info (世界书列表 + 触发日志) ──
        _r(f"/{PLUGIN_NAME}/info",             self.info,           ["GET"],    "插件状态信息")

        # ── 配置持久化 ──
        _r(f"/{PLUGIN_NAME}/config/save",      self.config_save,    ["POST"],   "保存配置项")
        _r(f"/{PLUGIN_NAME}/config/all",       self.config_all,     ["GET"],    "获取全量配置")

        # ── RAG 文档知识库 (5 个) ──
        _r(f"/{PLUGIN_NAME}/rag/upload",       self.rag_upload,     ["POST"],   "上传文档")
        _r(f"/{PLUGIN_NAME}/rag/upload_base64", self.rag_upload_base64, ["POST"], "上传文档(Base64)")
        _r(f"/{PLUGIN_NAME}/rag/documents",    self.rag_documents,  ["GET"],    "列出已上传文档")
        _r(f"/{PLUGIN_NAME}/rag/delete",       self.rag_delete,     ["POST"],   "删除文档")
        _r(f"/{PLUGIN_NAME}/rag/search",       self.rag_search,     ["POST"],   "语义检索测试")
        _r(f"/{PLUGIN_NAME}/rag/config",       self.rag_config,     ["GET"],    "RAG 配置状态")

        # ── 动态记忆 (4 个) ──
        _r(f"/{PLUGIN_NAME}/memory/list",      self.memory_list,    ["GET"],    "列出记忆")
        _r(f"/{PLUGIN_NAME}/memory/list_all",  self.memory_list_all,["GET"],    "列出全部记忆(倒序)")
        _r(f"/{PLUGIN_NAME}/memory/delete",    self.memory_delete,  ["POST"],   "删除记忆")
        _r(f"/{PLUGIN_NAME}/memory/vector_search", self.memory_vector_search, ["POST"], "向量检索(Debug)")

        # ── Provider 列表 (配置面板下拉) ──
        _r(f"/{PLUGIN_NAME}/provider/list",    self.provider_list,  ["GET"],    "列出可用模型提供商")

        # ── 记忆导入导出 ──
        _r(f"/{PLUGIN_NAME}/memory/export",    self.memory_export, ["GET"],    "导出记忆(JSON)")
        _r(f"/{PLUGIN_NAME}/memory/import",    self.memory_import, ["POST"],   "导入记忆(JSON)")

    # ── Info ──────────────────────────────────────────────────

    @_api_handler
    async def info(self):
        """返回插件状态：可用世界书、活跃列表、触发日志。"""
        from ._route_core import handle_info
        active = None
        show_log = False
        if self.config is not None:
            active = getattr(self.config, 'worldbook_active', None)
            show_log = getattr(self.config, 'worldbook_show_log', False)
        pc = 0
        if self.persona_manager:
            pc = await self.persona_manager.get_persona_count()
        return await handle_info(
            self.kb_manager, self.wb_manager,
            persona_count=pc,
            active_worldbooks=active,
            show_trigger_log=show_log,
        )

    # ── Config Save ───────────────────────────────────────────

    @_api_handler
    async def config_save(self):
        """保存配置项（支持 rag/worldbook 分组）。直接使用注入的插件实例。"""
        data = await request.json(default={})
        group = data.get("group", "")
        key = data.get("key", "")
        value = data.get("value")

        if not group or not key:
            return error_response("缺少 group 或 key 参数", status_code=400)

        # 直接使用注入的插件实例（由 main.py 传入 self）
        plugin = self.plugin
        if plugin is None:
            return error_response("插件实例不可用", status_code=500)

        ok = plugin.save_plugin_config(group, key, value)
        if ok:
            return json_response({"status": "ok", "message": f"已保存 {group}.{key}"})
        return error_response("保存失败", status_code=500)

    @_api_handler
    async def config_all(self):
        """获取底层全量 config 字典供前端渲染"""
        if not self.config:
            return json_response({})
        return json_response(self.config.get_raw())

    # ── Panel ─────────────────────────────────────────────────

    @_api_handler
    async def serve_panel(self):
        """提供旧版 web_panel 静态 HTML (向后兼容)。

        新面板请直接使用 pages/panel/index.html (Plugin Pages 系统)。
        """
        html_path = os.path.join(
            os.path.dirname(__file__), "web_panel", "static", "index.html"
        )
        if not os.path.isfile(html_path):
            return error_response("Frontend not built", status_code=501)
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()

    # ── RAG ───────────────────────────────────────────────────

    @_api_handler
    async def rag_upload(self):
        """上传文档（multipart/form-data）。"""
        from astrbot.api.web import PluginUploadFile
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("未收到文件", status_code=400)
        form = await request.form()
        source = form.get("source", "") or getattr(upload, 'name', 'unknown')
        chunk_size = self.config.rag_chunk_size if self.config else 500
        chunk_overlap = self.config.rag_chunk_overlap if self.config else 50
        embedding = self.rag.get('embedding')
        vector_store = self.rag.get('vector_store')
        if embedding is None or vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(
            await handle_rag_upload(vector_store, embedding, upload, source, chunk_size, chunk_overlap)
        )

    @_api_handler
    async def rag_upload_base64(self):
        """接收前端发来的 Base64 JSON，完美绕过沙盒 FormData 拦截。"""
        import base64
        data = await request.json(default={})
        source = data.get("source", "unknown")
        b64_data = data.get("b64_data", "")
        if not b64_data:
            return error_response("未收到文件数据", status_code=400)
        try:
            file_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return error_response(f"Base64 解码失败: {e}", status_code=400)
        chunk_size = self.config.rag_chunk_size if self.config else 500
        chunk_overlap = self.config.rag_chunk_overlap if self.config else 50
        embedding = self.rag.get('embedding')
        vector_store = self.rag.get('vector_store')
        if embedding is None or vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        class DummyFile:
            def __init__(self, content):
                self._content = content
            async def read(self):
                return self._content
        upload_file = DummyFile(file_bytes)
        return json_response(
            await handle_rag_upload(vector_store, embedding, upload_file, source, chunk_size, chunk_overlap)
        )

    @_api_handler
    async def rag_documents(self):
        """列出已上传文档。"""
        vector_store = self.rag.get('vector_store')
        if vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(await handle_rag_documents(vector_store))

    @_api_handler
    async def rag_delete(self):
        """删除文档。"""
        data = await request.json(default={})
        source = data.get("source", "")
        if not source:
            return error_response("缺少 source 参数", status_code=400)
        vector_store = self.rag.get('vector_store')
        if vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(await handle_rag_delete(vector_store, source))

    @_api_handler
    async def rag_search(self):
        """语义检索测试。"""
        data = await request.json(default={})
        query = data.get("query", "")
        top_k = data.get("top_k", self.config.rag_top_k if self.config else 3)
        if not query:
            return error_response("缺少 query 参数", status_code=400)
        embedding = self.rag.get('embedding')
        vector_store = self.rag.get('vector_store')
        reranker = self.rag.get('reranker')
        if embedding is None or vector_store is None:
            return error_response("RAG 未初始化", status_code=500)
        return json_response(
            await handle_rag_search(vector_store, embedding, reranker, query, top_k)
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
            return error_response("记忆未初始化", status_code=500)
        session_id = request.query.get("session_id")
        return json_response(await handle_memory_list(memory_store, session_id))

    @_api_handler
    async def memory_list_all(self):
        """列出全部记忆（跨 session），按创建时间倒序。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆未初始化", status_code=500)
        limit = request.query.get("limit", 200, type=int)
        return json_response(await handle_memory_list_all(memory_store, limit=limit))

    @_api_handler
    async def memory_vector_search(self):
        """向量检索 Debug — 对输入文本做 embedding 后全局搜索。"""
        data = await request.json(default={})
        query = data.get("query", "")
        top_k = data.get("top_k", 5)
        if not query:
            return error_response("缺少 query 参数", status_code=400)
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆未初始化", status_code=500)
        embedding = self.rag.get('embedding')
        if embedding is None:
            return error_response("Embedding 未初始化", status_code=500)
        try:
            vectors = await embedding.embed([query])
            vector = vectors[0] if vectors else None
            if not vector:
                return error_response("embedding 向量化失败", status_code=500)
            # 将 NumPy 全表扫描 + 矩阵计算放入线程池，防止阻塞事件循环
            results = await asyncio.to_thread(memory_store.search_all, vector, top_k=top_k)
            return json_response({"results": results, "query": query})
        except Exception as e:
            return error_response(f"检索失败: {e}", status_code=500)

    @_api_handler
    async def memory_export(self):
        """导出全部记忆为 JSON。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆未初始化", status_code=500)
        return json_response(handle_memory_export(memory_store))

    @_api_handler
    async def memory_import(self):
        """从上传的 JSON 文件导入记忆（异步，重新生成向量）。"""
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆未初始化", status_code=500)
        embedding = self.rag.get('embedding')
        try:
            data = await request.json(default={})
        except Exception:
            return error_response("请求体不是有效 JSON", status_code=400)
        result = await handle_memory_import(memory_store, embedding, data)
        if result.get("status") == "error":
            return error_response(result.get("message", "导入失败"), status_code=400)
        return json_response(result)

    @_api_handler
    async def memory_delete(self):
        """删除记忆。"""
        data = await request.json(default={})
        memory_store = self.rag.get('memory_store')
        if memory_store is None:
            return error_response("记忆未初始化", status_code=500)
        return json_response(
            await handle_memory_delete(memory_store, data.get("memory_id"), data.get("session_id"))
        )

    # ── Provider List ─────────────────────────────────────────

    @_api_handler
    async def provider_list(self):
        """列出 AstrBot 中已配置的 Embedding / Rerank 提供商。"""
        return json_response(await handle_provider_list(self.context))

    # ── KB ────────────────────────────────────────────────────

    @_api_handler
    async def kb_list(self):
        category = request.query.get("category")
        search = request.query.get("search")
        page = request.query.get("page", 1, type=int)
        per_page = min(request.query.get("per_page", 20, type=int), 100)
        is_constant_str = request.query.get("is_constant")
        is_constant = (is_constant_str.lower() == 'true') if is_constant_str else None
        return json_response(
            await handle_kb_list(self.kb_manager, category, search, page, per_page, is_constant)
        )

    @_api_handler
    async def kb_get(self):
        data = await request.json(default={})
        return json_response(
            await handle_kb_get(self.kb_manager, data.get("entry_id"))
        )

    @_api_handler
    async def kb_create(self):
        return json_response(
            await handle_kb_create(self.kb_manager, await request.json(default={}))
        )

    @_api_handler
    async def kb_update(self):
        return json_response(
            await handle_kb_update(self.kb_manager, await request.json(default={}))
        )

    @_api_handler
    async def kb_delete(self):
        data = await request.json(default={})
        return json_response(
            await handle_kb_delete(self.kb_manager, data.get("entry_id"))
        )

    @_api_handler
    async def kb_toggle(self):
        data = await request.json(default={})
        return json_response(
            await handle_kb_toggle(
                self.kb_manager,
                data.get("entry_id"),
                data.get("enabled", True),
            )
        )

    @_api_handler
    async def kb_export(self):
        return json_response(await handle_kb_export(self.kb_manager))

    @_api_handler
    async def kb_import(self):
        data = await request.json(default={})
        return json_response(
            await handle_kb_import(self.kb_manager, data.get("entries", []))
        )

    @_api_handler
    async def kb_test(self):
        data = await request.json(default={})
        return json_response(
            await handle_kb_test(self.kb_manager, data.get("text"))
        )

    @_api_handler
    async def kb_categories(self):
        return json_response(await handle_kb_categories(self.kb_manager))

    # ── WB ────────────────────────────────────────────────────

    @_api_handler
    async def wb_list(self):
        return json_response(await handle_wb_list(self.wb_manager))

    @_api_handler
    async def wb_get(self):
        data = await request.json(default={})
        return json_response(
            await handle_wb_get(self.wb_manager, data.get("name"))
        )

    @_api_handler
    async def wb_create(self):
        data = await request.json(default={})
        return json_response(
            await handle_wb_create(
                self.wb_manager,
                data.get("name"),
                data.get("description", ""),
            )
        )

    @_api_handler
    async def wb_delete(self):
        data = await request.json(default={})
        return json_response(
            await handle_wb_delete(self.wb_manager, data.get("name"))
        )

    @_api_handler
    async def wb_delete_book(self):
        data = await request.json(default={})
        return json_response(
            await handle_wb_delete(self.wb_manager, data.get("name"))
        )

    @_api_handler
    async def wb_entry_create(self):
        data = await request.json(default={})
        return json_response(
            await handle_wb_entry_create(
                self.wb_manager,
                data.get("name"),
                data.get("entry"),
            )
        )

    @_api_handler
    async def wb_entry_update(self):
        data = await request.json(default={})
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
        data = await request.json(default={})
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
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("No file uploaded", status_code=400)

        form = await request.form()
        name = form.get("name")
        if not name:
            return error_response("Worldbook name required", status_code=400)

        # 必须先读取数据再 save（save 后文件指针在末尾，read() 返回空）
        data = await upload.read()
        if not data:
            return error_response("Uploaded file is empty", status_code=400)

        # 写入插件数据目录备份
        target_dir = (
            Path(get_astrbot_plugin_data_path())
            / PLUGIN_NAME
            / "imports"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        await upload.save(target_dir / f"{name}.json")

        return json_response(
            await handle_wb_import_st(self.wb_manager, name, data)
        )

    @_api_handler
    async def wb_import_json(self):
        """接收 JSON 文本数据并导入世界书（绕过沙盒 FormData 限制）。"""
        data = await request.json(default={})
        name = data.get("name", "").strip()
        file_data = data.get("data", "")
        if not name:
            return error_response("Worldbook name required", status_code=400)
        if not file_data:
            return error_response("No file data provided", status_code=400)
        # 写入临时文件供 import_from_st 解析
        import tempfile, os
        from pathlib import Path
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(file_data)
            if not self.wb_manager.import_from_st(tmp_path, name):
                return error_response("Failed to import worldbook", status_code=400)
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

    @_api_handler
    async def wb_bindings(self):
        return json_response(await handle_wb_bindings(self.wb_manager))

    @_api_handler
    async def wb_bind(self):
        data = await request.json(default={})
        name = data.get("name")
        bind_type = data.get("type", "persona")
        target_id = data.get("persona_id") if bind_type == "persona" else data.get("user_id")
        return json_response(
            await handle_wb_bind(self.wb_manager, bind_type, target_id, name)
        )

    @_api_handler
    async def wb_unbind(self):
        data = await request.json(default={})
        name = data.get("name")
        unbind_type = data.get("type", "persona")
        target_id = data.get("persona_id") if unbind_type == "persona" else data.get("user_id")
        return json_response(
            await handle_wb_unbind(self.wb_manager, unbind_type, target_id, name)
        )

    # ── WB Reload ─────────────────────────────────────────────

    @_api_handler
    async def wb_reload(self):
        """重新从磁盘加载所有世界书后返回列表（供面板刷新按钮用）。"""
        try:
            if hasattr(self.wb_manager, "reload_all"):
                self.wb_manager.reload_all()
            else:
                self.wb_manager._load_all()
        except Exception:
            pass  # 即使 reload 失败也返回当前列表
        return json_response(await handle_wb_list(self.wb_manager))

    # ── Persona 角色卡 (Quill 独立管理) ──────────────────────────

    @_api_handler
    async def persona_list(self):
        """返回所有角色卡（完整 JSON 结构，含头像 base64 data URL）。"""
        if not self.persona_manager:
            return json_response([])
        personas = await self.persona_manager.load_all()
        # 为每个有头像的角色卡嵌入 base64 data URL，避免 <img> 跨域请求
        for p in personas:
            avatar_path = p.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                fname = avatar_path[len("quill_avatars/"):]
                data = await self.persona_manager.read_avatar(fname)
                if data:
                    import base64
                    ext = os.path.splitext(fname)[1].lower()
                    mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
                    mime = mime_map.get(ext, 'image/png')
                    p["avatar_url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                else:
                    p["avatar_url"] = ""
            else:
                p["avatar_url"] = ""
        return json_response(personas)

    @_api_handler
    async def persona_create(self):
        """创建角色卡。"""
        data = await request.json(default={})
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        try:
            result = await self.persona_manager.create_persona(data)
            # 同步世界书绑定
            if self.wb_manager:
                persona_id = result.get("id")
                bound_wbs = data.get("quill_extensions", {}).get("bound_worldbooks", [])
                for wb in bound_wbs:
                    self.wb_manager.bind_persona(persona_id, wb)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)

    @_api_handler
    async def persona_update(self):
        """更新角色卡（支持部分更新）。"""
        data = await request.json(default={})
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return error_response("id is required", status_code=400)
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        try:
            result = await self.persona_manager.update_persona(persona_id, data)
            # 同步世界书绑定
            if self.wb_manager:
                bound_wbs = data.get("quill_extensions", {}).get("bound_worldbooks", [])
                for wb in self.wb_manager.list_worldbooks():
                    if wb in bound_wbs:
                        self.wb_manager.bind_persona(persona_id, wb)
                    else:
                        self.wb_manager.unbind_persona(persona_id, wb)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)

    @_api_handler
    async def persona_delete(self):
        """删除角色卡。"""
        data = await request.json(default={})
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return error_response("id is required", status_code=400)
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        try:
            await self.persona_manager.delete_persona(persona_id)
            return json_response({"id": persona_id, "message": "Persona deleted"})
        except ValueError as e:
            return error_response(str(e), status_code=404)

    # ── Persona 扩展功能：头像上传 / V2 导入导出 ──────────────────

    @_api_handler
    async def upload_avatar(self):
        """上传头像图片（multipart/form-data）。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)

        from astrbot.api.web import PluginUploadFile
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("未收到文件", status_code=400)

        data = await upload.read()
        filename = getattr(upload, 'name', 'avatar.png')

        # 验证文件大小 (最大 10MB)
        if len(data) > 5 * 1024 * 1024:
            return error_response("图片文件过大（最大 5MB）", status_code=413)

        try:
            rel_path = await self.persona_manager.save_avatar(filename, data)
            url = f"/{PLUGIN_NAME}/avatar/{os.path.basename(rel_path)}"
            return json_response({"url": url, "path": rel_path, "message": "Avatar uploaded"})
        except Exception as e:
            return error_response(f"保存失败: {e}", status_code=500)

    @_api_handler
    async def upload_avatar_base64(self):
        """上传头像图片（Base64 模式，绕过沙盒 FormData 拦截）。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)

        import base64
        data = await request.json(default={})
        filename = (data.get("filename") or "avatar.png").strip()
        b64_data = (data.get("b64_data") or "").strip()

        if not b64_data:
            return error_response("未收到文件数据", status_code=400)

        try:
            file_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return error_response(f"Base64 解码失败: {e}", status_code=400)

        # 验证文件大小 (最大 10MB，Base64 膨胀约 33%)
        if len(file_bytes) > 10 * 1024 * 1024:
            return error_response("图片文件过大（最大 10MB）", status_code=413)

        try:
            rel_path = await self.persona_manager.save_avatar(filename, file_bytes)
            url = f"/{PLUGIN_NAME}/avatar/{os.path.basename(rel_path)}"
            return json_response({"url": url, "path": rel_path, "message": "Avatar uploaded"})
        except Exception as e:
            return error_response(f"保存失败: {e}", status_code=500)

    @_api_handler
    async def persona_import(self):
        """导入 V2 角色卡（multipart/form-data，支持 PNG/JPG/JSON）。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)

        from astrbot.api.web import PluginUploadFile
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("未收到文件", status_code=400)

        file_data = await upload.read()
        filename = getattr(upload, 'name', 'card.png').lower()
        ext = os.path.splitext(filename)[1].lower()
        # 处理 .card.png 等特殊扩展名
        if filename.lower().endswith('.card.png'):
            ext = '.png'
        elif ext not in ('.png', '.jpg', '.jpeg', '.json'):
            return error_response("不支持的文件格式（支持 PNG/JPG/JSON）", status_code=400)

        if len(file_data) > 5 * 1024 * 1024:
            return error_response("文件过大（最大 5MB）", status_code=413)

        try:
            is_image = ext in ('.png', '.jpg', '.jpeg')
            persona_data = self.persona_manager.parse_v2_card(file_data, is_image)

            # 如果是图片，保存为头像
            if is_image:
                avatar_filename = f"{persona_data['name']}{ext}"
                avatar_path = await self.persona_manager.save_avatar(avatar_filename, file_data)
                persona_data["avatar_path"] = avatar_path

            result = await self.persona_manager.create_persona(persona_data)
            return json_response(result)

        except ImportError as e:
            return error_response(str(e), status_code=501)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"导入失败: {e}", status_code=500)

    @_api_handler
    async def persona_import_base64(self):
        """导入 V2 角色卡（Base64 模式，绕过沙盒 FormData 拦截）。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)

        import base64
        data = await request.json(default={})
        filename = (data.get("filename") or "card.png").strip().lower()
        b64_data = (data.get("b64_data") or "").strip()

        if not b64_data:
            return error_response("未收到文件数据", status_code=400)

        try:
            file_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return error_response(f"Base64 解码失败: {e}", status_code=400)

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.json'):
            return error_response("不支持的文件格式（支持 PNG/JPG/JSON）", status_code=400)

        if len(file_bytes) > 5 * 1024 * 1024:
            return error_response("文件过大（最大 5MB）", status_code=413)

        try:
            is_image = ext in ('.png', '.jpg', '.jpeg')
            persona_data = self.persona_manager.parse_v2_card(file_bytes, is_image)

            # 如果是图片，保存为头像
            if is_image:
                avatar_filename = f"{persona_data['name']}{ext}"
                avatar_path = await self.persona_manager.save_avatar(avatar_filename, file_bytes)
                persona_data["avatar_path"] = avatar_path

            result = await self.persona_manager.create_persona(persona_data)
            return json_response(result)

        except ImportError as e:
            return error_response(str(e), status_code=501)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"导入失败: {e}", status_code=500)

    @_api_handler
    async def persona_export(self):
        """导出 V2 角色卡。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)

        persona_id = request.args.get("id", "").strip()
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

            # 导出为 V2
            export_data = self.persona_manager.export_v2_card(persona, avatar_data)

            # 返回文件下载
            if avatar_data:
                from astrbot.api.web import file_response
                return file_response(
                    export_data,
                    filename=f"{persona['name']}_v2.png",
                    content_type="image/png"
                )
            else:
                from astrbot.api.web import file_response
                return file_response(
                    export_data,
                    filename=f"{persona['name']}_v2.json",
                    content_type="application/json"
                )

        except ImportError as e:
            return error_response(str(e), status_code=501)
        except Exception as e:
            return error_response(f"导出失败: {e}", status_code=500)

    async def serve_avatar(self, filename: str):
        """提供头像文件静态服务。"""
        if not self.persona_manager:
            return error_response("Not available", status_code=500)

        # 安全检查
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return error_response("Invalid filename", status_code=400)

        data = await self.persona_manager.read_avatar(filename)
        if not data:
            return error_response("File not found", status_code=404)

        # 根据扩展名设置 MIME 类型
        ext = os.path.splitext(filename)[1].lower()
        mime_map = {
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp', '.gif': 'image/gif'
        }
        mime = mime_map.get(ext, 'application/octet-stream')

        from starlette.responses import Response
        return Response(
            data,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=86400"}
        )

    @_api_handler
    async def persona_import_text(self):
        """从剪贴板文本导入角色卡"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        data = await request.json(default={})
        text = (data.get("text") or "").strip()
        if not text:
            return error_response("text is required", status_code=400)
        try:
            persona_data = self.persona_manager.parse_clipboard_text(text)
            result = await self.persona_manager.create_persona(persona_data)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"解析失败: {e}", status_code=400)

    @_api_handler
    async def persona_import_text_base64(self):
        """从剪贴板文本导入角色卡（Base64 绕过沙盒）"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        data = await request.json(default={})
        b64_text = (data.get("b64_text") or "").strip()
        if not b64_text:
            return error_response("b64_text is required", status_code=400)
        import base64
        try:
            text = base64.b64decode(b64_text).decode('utf-8')
        except Exception as e:
            return error_response(f"Base64 解码失败: {e}", status_code=400)
        try:
            persona_data = self.persona_manager.parse_clipboard_text(text)
            result = await self.persona_manager.create_persona(persona_data)
            return json_response(result)
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"解析失败: {e}", status_code=400)

    @_api_handler
    async def persona_export_base64(self):
        """导出 V2 角色卡（Base64 编码，突破沙盒下载限制）。"""
        if not self.persona_manager:
            return error_response("Persona manager not available", status_code=500)
        data = await request.json(default={})
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
            export_data = self.persona_manager.export_v2_card(persona, avatar_data)
            filename = f"{persona['name']}_v2.png" if avatar_data else f"{persona['name']}_v2.json"
            b64_str = base64.b64encode(export_data).decode('ascii')
            return json_response({"filename": filename, "b64_data": b64_str})
        except Exception as e:
            return error_response(f"导出失败: {e}", status_code=500)
