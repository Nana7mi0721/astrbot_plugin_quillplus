# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared route handler logic for Quill management API.

Used by web_routes.py (AstrBot mode).
Contains zero HTTP/framework dependencies — pure async handler functions
that receive already-parsed data and return plain dicts.

Each handler returns {"status": "ok", "data": ...} or {"status": "error", "message": "..."}.
"""

import asyncio
import logging
import os
import tempfile
import uuid
from typing import Any

logger = logging.getLogger(__name__)


_PLUGIN_VERSION_CACHE: str | None = None


def _get_plugin_version() -> str:
    """从插件根目录的 metadata.yaml 读取版本号（进程内缓存）。

    修复：此前对 __file__ 做了两次 dirname（跳到 plugins/ 目录），永远找不到
    metadata.yaml，面板标题恒显示硬编码回退值。__file__ 在插件根目录下，
    只需上跳一级。

    另：该函数被 /info 每次请求调用，每次都 open+yaml.safe_load 是事件循环里的
    阻塞磁盘 IO。版本号在进程生命周期内不会变，缓存一次即可。
    """
    global _PLUGIN_VERSION_CACHE
    if _PLUGIN_VERSION_CACHE is not None:
        return _PLUGIN_VERSION_CACHE
    try:
        import yaml
        import os
        _meta_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
        with open(_meta_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f)
        _PLUGIN_VERSION_CACHE = str(meta.get("version", "unknown"))
    except Exception:
        _PLUGIN_VERSION_CACHE = "unknown"
    return _PLUGIN_VERSION_CACHE


# ── Response helpers ─────────────────────────────────────────────

def ok(data: Any = None, **kw) -> dict:
    result: dict = {"status": "ok"}
    if data is not None:
        result["data"] = data
    if kw:
        result.update(kw)
    return result


def err(msg: str) -> dict:
    return {"status": "error", "message": msg}


def error_text(prefix: str, exc: Exception) -> str:
    """把异常转成可下发给前端的提示：服务端记全量，前端只给异常类型。

    原始异常文本会带出内部结构——OSError 带绝对路径、sqlite3 带表/列名、向量库
    带维度细节。此前多处直接把 f"{prefix}: {exc}" 拼进面板提示和群聊回复。
    校验类异常（ValueError 等）由调用方单独捕获、原样透出；走到这里的都是非预期
    错误，前端只需要知道哪一步失败，细节留在服务端日志。
    """
    logger.error("[Quill] %s: %s", prefix, exc, exc_info=True)
    return f"{prefix}（{type(exc).__name__}），详情见服务端日志"


# ── WR handlers ──────────────────────────────────────────────────

async def handle_wr_list(wr_manager, category=None, search=None, page=1, per_page=20, is_constant=None):
    if not wr_manager:
        return err("写作素材库未加载")
    entries = await wr_manager.get_all_entries(enabled_only=False)
    if category:
        entries = [e for e in entries if e.get("category") == category]
    if search:
        s = search.lower()
        entries = [
            e for e in entries
            if s in (e.get("name") or "").lower()
            or s in (e.get("content") or "").lower()
            or s in (e.get("entry_id") or "").lower()
            or any(s in str(k).lower() for k in (e.get("keywords") or []))
        ]
    if is_constant is not None:
        is_c = str(is_constant).lower() == 'true' if isinstance(is_constant, str) else bool(is_constant)
        entries = [e for e in entries if bool(e.get("is_constant")) == is_c]
    total = len(entries)
    items = entries[(page - 1) * per_page: (page - 1) * per_page + per_page]
    return ok({"items": items, "total": total, "page": page, "per_page": per_page})


async def handle_wr_get(wr_manager, entry_id=None):
    if not wr_manager:
        return err("写作素材库未加载")
    if not entry_id:
        return err("缺少 entry_id 参数")
    entry = await wr_manager.get_entry(entry_id)
    if not entry:
        return err("条目未找到")
    return ok(entry)


async def handle_wr_create(wr_manager, data: dict):
    if not wr_manager:
        return err("写作素材库未加载")

    # 必检字段（keywords 不是必需的，默认为空列表）
    for field in ("category", "entry_id", "content"):
        if field not in data or not data[field]:
            return err(f"缺少必填字段: {field}")

    # 如果不存在，赋默认空列表
    if "keywords" not in data:
        data["keywords"] = []

    success = await wr_manager.add_entry(
        category=data["category"],
        entry_id=data["entry_id"],
        keywords=data["keywords"],
        content=data["content"],
        name=data.get("name"),
        description=data.get("description"),
        aliases=data.get("aliases"),
        secondary_keywords=data.get("secondary_keywords"),
        priority=data.get("priority", 5),
        is_constant=bool(data.get("is_constant", False)),
    )
    if not success:
        return err("创建条目失败（ID 可能已存在）")
    return ok({"entry_id": data["entry_id"]}, message="Entry created")


async def handle_wr_update(wr_manager, data: dict):
    if not wr_manager:
        return err("写作素材库未加载")
    if not data or "entry_id" not in data:
        return err("缺少 entry_id 参数")
    allowed = [
        "category", "name", "description", "keywords",
        "secondary_keywords", "aliases", "content", "priority", "enabled", "is_constant",
    ]
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return err("没有可更新的字段")
    if not await wr_manager.update_entry(data["entry_id"], **updates):
        return err("更新条目失败")
    return ok({"entry_id": data["entry_id"]}, message="Entry updated")


async def handle_wr_delete(wr_manager, entry_id=None):
    if not wr_manager:
        return err("写作素材库未加载")
    if not entry_id:
        return err("缺少 entry_id 参数")
    if not await wr_manager.delete_entry(entry_id):
        return err("删除条目失败")
    return ok({"entry_id": entry_id}, message="Entry deleted")


async def handle_wr_toggle(wr_manager, entry_id=None, enabled=True):
    if not wr_manager:
        return err("写作素材库未加载")
    if not entry_id:
        return err("缺少 entry_id 参数")
    if not await wr_manager.enable_entry(entry_id, enabled):
        return err("切换条目状态失败")
    return ok({"entry_id": entry_id, "enabled": enabled}, message="Entry toggled")


async def handle_wr_batch_delete(wr_manager, entry_ids: list = None):
    """P2-4: 批量删除写作素材库条目。"""
    if not wr_manager:
        return err("写作素材库未加载")
    if not entry_ids or not isinstance(entry_ids, list):
        return err("缺少 entry_ids 参数")
    deleted = 0
    failed = 0
    for eid in entry_ids:
        try:
            if await wr_manager.delete_entry(eid):
                deleted += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return ok({"deleted": deleted, "failed": failed}, message=f"批量删除完成: {deleted} 成功, {failed} 失败")


async def handle_wr_batch_toggle(wr_manager, entry_ids: list = None, enabled: bool = True):
    """P2-4: 批量启用/禁用写作素材库条目。"""
    if not wr_manager:
        return err("写作素材库未加载")
    if not entry_ids or not isinstance(entry_ids, list):
        return err("缺少 entry_ids 参数")
    toggled = 0
    failed = 0
    for eid in entry_ids:
        try:
            if await wr_manager.enable_entry(eid, enabled):
                toggled += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return ok({"toggled": toggled, "failed": failed}, message=f"批量操作完成: {toggled} 成功, {failed} 失败")


async def handle_wr_export(wr_manager):
    if not wr_manager:
        return err("写作素材库未加载")
    entries = await wr_manager.get_all_entries(enabled_only=False)
    return ok({"entries": entries})


async def handle_wr_import(wr_manager, entries: list):
    if not wr_manager:
        return err("写作素材库未加载")
    if not isinstance(entries, list):
        return err("entries 必须是数组")
    imported = 0
    failed = 0
    invalid = 0
    for entry in entries:
        # 逐条校验：此前直接 entry["entry_id"]，缺字段的条目会抛 KeyError 被
        # 兜成 HTTP 500，且循环是逐条写入的——前面的条目已经落库，形成「报错
        # 了但导入了一半」的静默副作用。现在坏条目只计失败并跳过。
        if not isinstance(entry, dict) or not entry.get("entry_id"):
            invalid += 1
            failed += 1
            continue
        try:
            success = await wr_manager.add_entry(
                category=entry.get("category", "imported"),
                entry_id=entry["entry_id"],
                keywords=entry.get("keywords", []),
                content=entry.get("content", ""),
                name=entry.get("name"),
                description=entry.get("description"),
                aliases=entry.get("aliases"),
                secondary_keywords=entry.get("secondary_keywords"),
                priority=entry.get("priority", 5),
                is_constant=entry.get("is_constant", False),
            )
        except Exception as e:
            logger.warning("[WR] 导入条目 %s 失败: %s", entry.get("entry_id"), e, exc_info=True)
            success = False
        if success:
            imported += 1
        else:
            failed += 1
    msg = f"Imported {imported} entries, {failed} failed"
    if invalid:
        msg += f" ({invalid} 条缺少 entry_id，已跳过)"
    return ok({"imported": imported, "failed": failed, "invalid": invalid}, message=msg)


async def handle_wr_test(wr_manager, text=None):
    if not wr_manager:
        return err("写作素材库未加载")
    if not text:
        return err("缺少 text 参数")
    results = await wr_manager.match(text, top_k=10)
    safe = [
        {"entry_id": r.get("entry_id"), "name": r.get("name"),
         "keywords": r.get("keywords"), "match_score": r.get("match_score")}
        for r in results
    ]
    return ok({"results": safe})


async def handle_wr_categories(wr_manager):
    if not wr_manager:
        return err("写作素材库未加载")
    return ok({"categories": await wr_manager.get_categories()})


# ── WB handlers ──────────────────────────────────────────────────

def _collect_worldbooks(wb_manager) -> list:
    """同步收集全部世界书（含深拷贝），由 handle_wb_list 丢进线程池执行。"""
    result = []
    for name in wb_manager.list_worldbooks():
        wb = wb_manager.get_worldbook(name)
        if wb:
            # 返回完整 entries 数组，供前端展开时直接渲染
            result.append({
                "name": name,
                "description": wb.get("description", ""),
                "entry_count": len(wb.get("entries", [])),
                "entries": wb.get("entries", []),
            })
        else:
            result.append({"name": name, "description": "", "entry_count": 0, "entries": []})
    return result


async def handle_wb_list(wb_manager):
    if not wb_manager:
        return err("世界书管理器未加载")
    # get_worldbook 对整本书做 deepcopy（同步、持 threading 锁），书大时是事件
    # 循环里的可见停顿；与同文件的写路径一致，放线程执行。
    result = await asyncio.to_thread(_collect_worldbooks, wb_manager)
    return ok({"worldbooks": result})


async def handle_wb_get(wb_manager, name=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name:
        return err("缺少名称")
    wb = await asyncio.to_thread(wb_manager.get_worldbook, name)
    if not wb:
        return err("世界书未找到")
    return ok(wb)


async def handle_wb_create(wb_manager, name=None, description=""):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name:
        return err("缺少名称")
    if not await asyncio.to_thread(wb_manager.create_worldbook, name, description):
        return err("创建世界书失败")
    return ok({"name": name}, message="Worldbook created")


async def handle_wb_delete(wb_manager, name=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name:
        return err("缺少名称")
    if not await asyncio.to_thread(wb_manager.delete_worldbook, name):
        return err("删除世界书失败")
    return ok({"name": name}, message="Worldbook deleted")


async def handle_wb_entry_create(wb_manager, name=None, entry=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name:
        return err("缺少名称")
    entry_data = entry or {}
    if "id" not in entry_data or not entry_data["id"]:
        entry_data["id"] = str(uuid.uuid4())[:8]
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.add_entry_to_worldbook, name, entry_data):
        return err("世界书未找到")
    return ok({"entry": entry_data}, message="Entry created")


async def handle_wb_entry_update(wb_manager, name=None, entry_id=None, entry=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name or not entry_id:
        return err("缺少 name 和 entry_id 参数")
    if not entry:
        return err("缺少 entry 数据")
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.update_entry_in_worldbook, name, entry_id, entry):
        return err("世界书或条目未找到")
    return ok({"entry_id": entry_id}, message="Entry updated")


async def handle_wb_entry_delete(wb_manager, name=None, entry_id=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name or not entry_id:
        return err("缺少 name 和 entry_id 参数")
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.delete_entry_from_worldbook, name, entry_id):
        return err("世界书或条目未找到")
    return ok({"entry_id": entry_id}, message="Entry deleted")


async def handle_wb_export_st(wb_manager, name=None):
    if not wb_manager:
        return err("世界书管理器未加载")
    if not name:
        return err("缺少 name 参数")
    wb = await asyncio.to_thread(wb_manager.get_worldbook, name)
    if not wb:
        return err("世界书未找到")
    st_entries = {}
    for idx, entry in enumerate(wb.get("entries", [])):
        keys = entry.get("keys", [])
        key_str = ",".join(keys) if isinstance(keys, list) else str(keys)
        st_entries[str(idx)] = {
            "uid": idx, "key": key_str, "keysecondary": "",
            "comment": entry.get("title", entry.get("id", "")),
            "content": entry.get("content", ""),
            "constant": entry.get("is_constant", False),
            "selective": False, "selectiveLogic": 0, "addMemo": True,
            "order": entry.get("inject_position", 2),
            "position": entry.get("inject_position", 2),
            "disable": not entry.get("enabled", True),
            "excludeRecursion": False, "preventRecursion": False,
            "delayUntilRecursion": False, "probability": 100,
            "useProbability": True, "depth": 4, "group": "",
            "groupOverride": False, "groupWeight": 100,
            "scanDepth": None, "caseSensitive": None,
            "matchWholeWords": None, "automationId": "",
            "role": None, "vectorized": False, "displayIndex": idx,
        }
    return ok({"entries": st_entries, "name": name})


async def handle_wb_import_st(wb_manager, name=None, upload_data=None):
    """Import ST lorebook from raw bytes data."""
    if not wb_manager:
        return err("世界书管理器未加载")
    if not upload_data:
        return err("未收到文件数据")
    if not name:
        return err("缺少世界书名称")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_f:
            tmp_f.write(upload_data)
        if not await asyncio.to_thread(wb_manager.import_from_st, tmp_path, name):
            return err("导入 ST 文件失败")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return ok({"name": name}, message="ST worldbook imported")


# ── Info handler ─────────────────────────────────────────────────

async def handle_info(wr_manager, wb_manager, persona_count=0,
                       show_trigger_log=False, health_tracker=None):
    """返回插件状态信息（含可用世界书列表 + 触发日志 + 健康度）。"""
    wr_count = 0
    categories = {}
    if wr_manager:
        entries = await wr_manager.get_all_entries(enabled_only=False)
        wr_count = len(entries)
        for e in entries:
            c = e.get("category", "未分类")
            categories[c] = categories.get(c, 0) + 1
    # 可用世界书列表
    available_wb = []
    trigger_log = []
    if wb_manager:
        # get_available_worldbooks / get_trigger_log 都是**同步**方法（内部只有
        # 一次加锁读取，返回 list）。此前写作 await，会抛 TypeError 并被下面的
        # 裸 except 吞掉，于是 available_wb 恒为 []、wb_count 恒为 0——
        # 侧栏世界书徽章因此首屏不显示（要等切到世界书页由 /wb/list 补上）。
        try:
            # get_available_worldbooks 是**同步**方法（加锁读取 + 深拷贝），
            # 放线程避免在事件循环里做整库拷贝。
            available_wb = await asyncio.to_thread(wb_manager.get_available_worldbooks)
        except Exception:
            logger.warning("[Quill] 获取可用世界书列表失败", exc_info=True)
        if show_trigger_log and hasattr(wb_manager, 'get_trigger_log'):
            try:
                trigger_log = await asyncio.to_thread(wb_manager.get_trigger_log)
            except Exception:
                logger.warning("[Quill] 获取触发日志失败", exc_info=True)
    # P1-4: 健康度数据
    health = health_tracker.stats() if health_tracker else None
    # FTS 索引状态：降级为全表扫描时前端必须能看见
    wr_index = None
    if wr_manager and hasattr(wr_manager, "get_index_status"):
        try:
            status = wr_manager.get_index_status()
            status["entries"] = wr_count
            status["degraded"] = status.get("fts_ok") is False
            wr_index = status
        except Exception:
            logger.warning("[Quill] 获取 WR 索引状态失败", exc_info=True)
    return ok({
        "wr_count": wr_count,
        "wb_count": len(available_wb),
        "persona_count": persona_count,
        "categories": categories,
        "version": f"Quill v{_get_plugin_version()}",
        "available_worldbooks": available_wb,
        "trigger_log": trigger_log,
        "health": health,
        "wr_index": wr_index,
    })


# ── RAG Handlers ────────────────────────────────────────────────

async def handle_rag_upload(vector_store, embedding_provider, upload_file, source_name, chunk_size=500, chunk_overlap=50):
    """处理文档上传：提取文本 → 分块 → 向量化 → 存储。"""
    from .quill_rag.chunker import chunk_text
    try:
        content = await upload_file.read()
        # 尝试 UTF-8 解码
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError:
                text = content.decode("utf-8", errors="replace")

        # 分块
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            return err("文档内容为空或无法分块")

        # 向量化
        embeddings = await embedding_provider.embed(chunks)
        if not embeddings:
            return err("向量化失败")

        # 存储（同步 FAISS + SQLite 操作放入线程池，避免阻塞事件循环）
        await vector_store.add(chunks, embeddings, source_name)
        return ok({
            "source": source_name,
            "chunk_count": len(chunks),
            "dim": len(embeddings[0]) if embeddings else 0,
        })
    except Exception as e:
        return err(error_text("上传失败", e))


async def handle_rag_documents(vector_store):
    """列出已上传文档。"""
    try:
        docs = await vector_store.list_documents()
        return ok({"documents": docs})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_rag_delete(vector_store, source):
    """删除文档。"""
    try:
        deleted = await vector_store.delete_by_source(source)
        return ok({"deleted": deleted, "source": source})
    except Exception as e:
        return err(error_text("删除失败", e))


async def handle_rag_search(vector_store, embedding_provider, reranker, query, top_k=3):
    """语义检索测试。"""
    try:
        from .quill_rag.retrieval import QuillRetriever
        retriever = QuillRetriever(
            embedding_provider=embedding_provider,
            vector_store=vector_store,
            reranker=reranker,
            top_k=top_k,
        )
        results = await retriever.search_documents(query)
        return ok({"results": results, "query": query})
    except Exception as e:
        return err(error_text("检索失败", e))


async def handle_rag_config(embedding_provider, reranker):
    """返回 RAG 配置状态。"""
    try:
        emb_status = embedding_provider.get_status() if embedding_provider else {}
        rerank_status = reranker.get_status() if reranker else {}
        return ok({"embedding": emb_status, "rerank": rerank_status})
    except Exception as e:
        return err(error_text("获取配置失败", e))


async def handle_memory_list(memory_store, session_id=None):
    """列出记忆（可按 session_id 过滤）。

    当有 session_id 时返回该 session 的记忆；
    无 session_id 时返回全部记忆（跨 session，按时间倒序）。
    """
    try:
        if session_id:
            memories = await memory_store.list_memories(session_id)
            return ok({"session_id": session_id, "memories": memories})
        # 无 session_id：返回全部记忆（倒序），供记忆浏览表格用
        memories = await memory_store.list_all_memories(200)
        return ok({"memories": memories, "sessions": []})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_memory_delete(memory_store, memory_id=None, session_id=None):
    """删除记忆。"""
    try:
        if memory_id:
            ok_del = await memory_store.delete_memory(memory_id)
            return ok({"deleted": ok_del, "memory_id": memory_id}) if ok_del else err("记忆不存在")
        if session_id:
            deleted = await memory_store.delete_session_memories(session_id)
            return ok({"deleted": deleted, "session_id": session_id})
        return err("需要 memory_id 或 session_id")
    except Exception as e:
        return err(error_text("删除失败", e))


async def handle_memory_list_all(memory_store, limit=200, page=1, per_page=50):
    """列出全部记忆（跨 session），按创建时间倒序，支持分页。

    返回 total 为数据库中记忆的真实总数（不受分页限制），
    前端据此修正"总览"和"列表"之间的数量脱节问题。
    """
    try:
        per_page = min(per_page, 200)
        offset = (page - 1) * per_page
        total = await memory_store.count_all_memories()
        memories = await memory_store.list_all_memories(per_page, offset)
        total_pages = max(1, (total + per_page - 1) // per_page)
        return ok({"memories": memories, "total": total, "page": page, "per_page": per_page, "total_pages": total_pages})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_memory_stats(memory_store):
    """B4 修复：返回记忆存储统计（总数 / 活跃会话 / 今日新增），供总览页使用。"""
    try:
        stats = await memory_store.get_stats()
        return ok(stats)
    except Exception as e:
        return err(error_text("统计失败", e))


async def handle_memory_get(memory_store, memory_id=None):
    """获取单条记忆完整详情。"""
    if not memory_id:
        return err("需要 memory_id")
    try:
        mem = await memory_store.get_memory_by_id(memory_id)
        if mem is None:
            return err("记忆不存在")
        return ok(mem)
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_provider_list(context):
    """列出 AstrBot 中已配置的 Embedding / Rerank / LLM 提供商。

    返回格式: {"embedding": [...], "rerank": [...], "llm": [...]}
    供前端下拉选择使用。
    """
    try:
        pm = context.provider_manager
        providers = pm.providers_config
        embedding = []
        rerank = []
        llm = []
        for p in providers:
            pt = p.get("provider_type", "")
            item = {
                "id": p.get("id", ""),
                "model": p.get("model", ""),
                "type": p.get("type", ""),
            }
            if pt == "embedding":
                embedding.append(item)
            elif pt == "rerank":
                rerank.append(item)
            elif pt in ("chat_completion", "llm", "text_chat") or pt == "":
                # 所有非向量/重排的提供商都视为可用 LLM
                llm.append(item)
        return ok({"embedding": embedding, "rerank": rerank, "llm": llm})
    except Exception as e:
        return err(error_text("获取提供商列表失败", e))


async def handle_memory_export(memory_store):
    """导出全部记忆为 JSON 字符串（分页读取，避免静默截断）。"""
    try:
        page_size = 1000
        max_export = 100000
        memories: list[dict] = []
        offset = 0
        while len(memories) < max_export:
            batch = await memory_store.list_all_memories(page_size, offset)
            if not batch:
                break
            memories.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)

        truncated = len(memories) >= max_export
        result = {"memories": memories, "total": len(memories)}
        if truncated:
            result["truncated"] = True
            result["message"] = f"记忆数量超过 {max_export} 条，本次导出已截断"
        return ok(result)
    except Exception as e:
        return err(error_text("导出失败", e))


async def handle_memory_prune(memory_store):
    """一键清理低价值/过期记忆。"""
    try:
        deleted = await memory_store.prune_memories()
        return ok({"deleted": deleted, "message": f"已清理 {deleted} 条低价值记忆"})
    except Exception as e:
        return err(error_text("修剪失败", e))


async def handle_chat_log_list(memory_store, session_id=None, limit=200):
    """列出对话日志"""
    if not session_id:
        return err("需要 session_id")
    try:
        logs = await memory_store.list_chat_logs(session_id, limit)
        return ok({"session_id": session_id, "logs": logs, "total": len(logs)})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_chat_log_export(memory_store, session_id=None, format="markdown"):
    """导出对话日志"""
    if not session_id:
        return err("需要 session_id")
    try:
        text = await memory_store.export_chat_logs(session_id, format)
        return ok({"session_id": session_id, "format": format, "content": text})
    except Exception as e:
        return err(error_text("导出失败", e))


async def handle_memory_import(memory_store, embedding_provider, data):
    """从 JSON 数据批量导入记忆（异步，重新生成向量）。

    F16 修复：原实现循环内逐条 embed([summary])，100 条=100 次 API 往返。
    现改为先收集所有 summary，一次批量 embed，再循环写 DB。
    """
    try:
        if not data or not isinstance(data, dict):
            return err("无效的数据格式")
        memories = data.get("memories", [])
        if not isinstance(memories, list):
            return err("memories 必须是数组")

        if not memories:
            return ok({"imported": 0, "failed": 0, "message": "无记忆可导入"})

        # 1. 预处理：收集有效条目（有 summary 且有 embedding_provider）
        valid_entries = []
        failed = 0
        for m in memories:
            summary = m.get("summary", "")
            if not summary:
                failed += 1
                continue
            valid_entries.append({
                "summary": summary,
                "chat_summary": m.get("chat_summary", ""),
                "session_id": m.get("session_id", "imported"),
            })

        if not valid_entries:
            return ok({"imported": 0, "failed": failed, "message": "无有效记忆可导入"})

        # 2. 批量向量化（1 次 API 调用）
        all_summaries = [e["summary"] for e in valid_entries]
        if not embedding_provider:
            return ok({"imported": 0, "failed": len(valid_entries) + failed,
                        "message": "embedding_provider 未配置，无法导入"})

        try:
            all_vectors = await embedding_provider.embed(all_summaries)
        except Exception as e:
            logger.warning(f"[Quill Memory] 批量向量化失败: {e}")
            return ok({"imported": 0, "failed": len(valid_entries) + failed,
                        "message": error_text("向量化失败", e)})

        if not all_vectors or len(all_vectors) != len(valid_entries):
            logger.warning(
                f"[Quill Memory] 向量化数量不匹配: 期望 {len(valid_entries)}, 得到 {len(all_vectors) if all_vectors else 0}"
            )
            return ok({"imported": 0, "failed": len(valid_entries) + failed,
                        "message": "向量化数量不匹配"})

        # 3. 批量写 DB（memory_store.add 是 async，直接 await；
        # 此前误用 asyncio.to_thread 包装 async 函数——线程内只会创建协程对象
        # 从不执行，导致导入显示成功但一条都没写库）
        imported = 0
        for entry, vector in zip(valid_entries, all_vectors):
            try:
                if vector:
                    await memory_store.add(
                        entry["session_id"], entry["summary"], vector, entry["chat_summary"]
                    )
                    imported += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning(f"[Quill Memory] import single failed: {e}")
                failed += 1

        return ok({"imported": imported, "failed": failed, "message": f"成功导入 {imported}/{len(memories)} 条"})
    except Exception as e:
        return err(error_text("导入失败", e))


# ── Shared helpers ───────────────────────────────────────────────
# (save_worldbook removed — replaced by atomic WorldbookManager methods)
