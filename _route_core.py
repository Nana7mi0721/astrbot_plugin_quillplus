"""Shared route handler logic for Quill management API.

Used by both web_routes.py (AstrBot mode) and quill_desktop.py (standalone).
Contains zero HTTP/framework dependencies — pure async handler functions
that receive already-parsed data and return plain dicts.

Each handler returns {"status": "ok", "data": ...} or {"status": "error", "message": "..."}.
"""

import asyncio
import json
import os
import tempfile
import uuid
from typing import Any, Optional

try:
    from .worldbook import _validate_name  # AstrBot mode (package import)
except ImportError:
    from worldbook import _validate_name  # Desktop mode (standalone)


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


# ── KB handlers ──────────────────────────────────────────────────

async def handle_kb_list(kb_manager, category=None, search=None, page=1, per_page=20, is_constant=None):
    if not kb_manager:
        return err("Knowledge base not available")
    entries = await kb_manager.get_all_entries(enabled_only=False)
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


async def handle_kb_get(kb_manager, entry_id=None):
    if not kb_manager:
        return err("Knowledge base not available")
    if not entry_id:
        return err("entry_id is required")
    entry = await kb_manager.get_entry(entry_id)
    if not entry:
        return err("Entry not found")
    return ok(entry)


async def handle_kb_create(kb_manager, data: dict):
    if not kb_manager:
        return err("Knowledge base not available")

    # 必检字段（keywords 不是必需的，默认为空列表）
    for field in ("category", "entry_id", "content"):
        if field not in data or not data[field]:
            return err(f"Missing required field: {field}")

    # 如果不存在，赋默认空列表
    if "keywords" not in data:
        data["keywords"] = []

    success = await kb_manager.add_entry(
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
        return err("Failed to create entry (ID may already exist)")
    return ok({"entry_id": data["entry_id"]}, message="Entry created")


async def handle_kb_update(kb_manager, data: dict):
    if not kb_manager:
        return err("Knowledge base not available")
    if not data or "entry_id" not in data:
        return err("entry_id is required")
    allowed = [
        "category", "name", "description", "keywords",
        "secondary_keywords", "aliases", "content", "priority", "enabled", "is_constant",
    ]
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return err("No valid fields to update")
    if not await kb_manager.update_entry(data["entry_id"], **updates):
        return err("Failed to update entry")
    return ok({"entry_id": data["entry_id"]}, message="Entry updated")


async def handle_kb_delete(kb_manager, entry_id=None):
    if not kb_manager:
        return err("Knowledge base not available")
    if not entry_id:
        return err("entry_id is required")
    if not await kb_manager.delete_entry(entry_id):
        return err("Failed to delete entry")
    return ok({"entry_id": entry_id}, message="Entry deleted")


async def handle_kb_toggle(kb_manager, entry_id=None, enabled=True):
    if not kb_manager:
        return err("Knowledge base not available")
    if not entry_id:
        return err("entry_id is required")
    if not await kb_manager.enable_entry(entry_id, enabled):
        return err("Failed to toggle entry")
    return ok({"entry_id": entry_id, "enabled": enabled}, message="Entry toggled")


async def handle_kb_export(kb_manager):
    if not kb_manager:
        return err("Knowledge base not available")
    entries = await kb_manager.get_all_entries(enabled_only=False)
    return ok({"entries": entries})


async def handle_kb_import(kb_manager, entries: list):
    if not kb_manager:
        return err("Knowledge base not available")
    imported = 0
    failed = 0
    for entry in entries:
        success = await kb_manager.add_entry(
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
        if success:
            imported += 1
        else:
            failed += 1
    return ok({"imported": imported, "failed": failed},
              message=f"Imported {imported} entries, {failed} failed")


async def handle_kb_test(kb_manager, text=None):
    if not kb_manager:
        return err("Knowledge base not available")
    if not text:
        return err("text is required")
    results = await kb_manager.match(text, top_k=10)
    safe = [
        {"entry_id": r.get("entry_id"), "name": r.get("name"),
         "keywords": r.get("keywords"), "match_score": r.get("match_score")}
        for r in results
    ]
    return ok({"results": safe})


async def handle_kb_categories(kb_manager):
    if not kb_manager:
        return err("Knowledge base not available")
    return ok({"categories": await kb_manager.get_categories()})


# ── WB handlers ──────────────────────────────────────────────────

async def handle_wb_list(wb_manager):
    if not wb_manager:
        return err("Worldbook system not available")
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
    return ok({"worldbooks": result})


async def handle_wb_get(wb_manager, name=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name:
        return err("name is required")
    wb = wb_manager.get_worldbook(name)
    if not wb:
        return err("Worldbook not found")
    return ok(wb)


async def handle_wb_create(wb_manager, name=None, description=""):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name:
        return err("name is required")
    if not await asyncio.to_thread(wb_manager.create_worldbook, name, description):
        return err("Failed to create worldbook")
    return ok({"name": name}, message="Worldbook created")


async def handle_wb_delete(wb_manager, name=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name:
        return err("name is required")
    if not await asyncio.to_thread(wb_manager.delete_worldbook, name):
        return err("Failed to delete worldbook")
    return ok({"name": name}, message="Worldbook deleted")


async def handle_wb_entry_create(wb_manager, name=None, entry=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name:
        return err("name is required")
    entry_data = entry or {}
    if "id" not in entry_data or not entry_data["id"]:
        entry_data["id"] = str(uuid.uuid4())[:8]
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.add_entry_to_worldbook, name, entry_data):
        return err("Worldbook not found")
    return ok({"entry": entry_data}, message="Entry created")


async def handle_wb_entry_update(wb_manager, name=None, entry_id=None, entry=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name or not entry_id:
        return err("name and entry_id are required")
    if not entry:
        return err("entry data is required")
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.update_entry_in_worldbook, name, entry_id, entry):
        return err("Worldbook or entry not found")
    return ok({"entry_id": entry_id}, message="Entry updated")


async def handle_wb_entry_delete(wb_manager, name=None, entry_id=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name or not entry_id:
        return err("name and entry_id are required")
    # Use atomic operation (F1 fix: read-modify-write under single lock)
    if not await asyncio.to_thread(wb_manager.delete_entry_from_worldbook, name, entry_id):
        return err("Worldbook or entry not found")
    return ok({"entry_id": entry_id}, message="Entry deleted")


async def handle_wb_export_st(wb_manager, name=None):
    if not wb_manager:
        return err("Worldbook system not available")
    if not name:
        return err("name parameter is required")
    wb = wb_manager.get_worldbook(name)
    if not wb:
        return err("Worldbook not found")
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
        return err("Worldbook system not available")
    if not upload_data:
        return err("No file data provided")
    if not name:
        return err("Worldbook name required")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_f:
            tmp_f.write(upload_data)
        if not await asyncio.to_thread(wb_manager.import_from_st, tmp_path, name):
            return err("Failed to import ST file")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return ok({"name": name}, message="ST worldbook imported")


async def handle_wb_bindings(wb_manager):
    if not wb_manager:
        return err("Worldbook system not available")
    # 旧绑定系统已移除，现在使用角色卡级别的绑定
    return ok({"bindings": {"global_worldbooks": [], "persona_bindings": {}, "user_bindings": {}}})


async def handle_wb_bind(wb_manager, bind_type="user", target_id=None, worldbook_name=None):
    if not wb_manager:
        return err("Worldbook system not available")
    # 旧绑定系统已移除，现在通过角色卡编辑页面管理世界书绑定
    return err("世界书绑定已迁移到角色卡编辑页面，请在角色卡的高级扩展标签中管理绑定")


async def handle_wb_unbind(wb_manager, bind_type="user", target_id=None, worldbook_name=None):
    if not wb_manager:
        return err("Worldbook system not available")
    # 旧绑定系统已移除，现在通过角色卡编辑页面管理世界书绑定
    return err("世界书绑定已迁移到角色卡编辑页面，请在角色卡的高级扩展标签中管理绑定")


# ── Info handler ─────────────────────────────────────────────────

async def handle_info(kb_manager, wb_manager, persona_count=0,
                       show_trigger_log=False):
    """返回插件状态信息（含可用世界书列表 + 触发日志）。"""
    kb_count = 0
    categories = {}
    if kb_manager:
        entries = await kb_manager.get_all_entries(enabled_only=False)
        kb_count = len(entries)
        for e in entries:
            c = e.get("category", "未分类")
            categories[c] = categories.get(c, 0) + 1
    # 可用世界书列表
    available_wb = []
    trigger_log = []
    if wb_manager:
        try:
            available_wb = await asyncio.to_thread(wb_manager.get_available_worldbooks)
        except Exception:
            pass
        if show_trigger_log and hasattr(wb_manager, 'get_trigger_log'):
            try:
                trigger_log = await asyncio.to_thread(wb_manager.get_trigger_log)
            except Exception:
                pass
    return ok({
        "kb_count": kb_count,
        "wb_count": len(available_wb),
        "persona_count": persona_count,
        "categories": categories,
        "version": "Quill v5.2",
        "available_worldbooks": available_wb,
        "trigger_log": trigger_log,
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
        await asyncio.to_thread(vector_store.add, chunks, embeddings, source_name)
        return ok({
            "source": source_name,
            "chunk_count": len(chunks),
            "dim": len(embeddings[0]) if embeddings else 0,
        })
    except Exception as e:
        return err(f"上传失败: {e}")


async def handle_rag_documents(vector_store):
    """列出已上传文档。"""
    try:
        docs = await asyncio.to_thread(vector_store.list_documents)
        return ok({"documents": docs})
    except Exception as e:
        return err(f"查询失败: {e}")


async def handle_rag_delete(vector_store, source):
    """删除文档。"""
    try:
        deleted = await asyncio.to_thread(vector_store.delete_by_source, source)
        return ok({"deleted": deleted, "source": source})
    except Exception as e:
        return err(f"删除失败: {e}")


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
        return err(f"检索失败: {e}")


async def handle_rag_config(embedding_provider, reranker):
    """返回 RAG 配置状态。"""
    try:
        emb_status = embedding_provider.get_status() if embedding_provider else {}
        rerank_status = reranker.get_status() if reranker else {}
        return ok({"embedding": emb_status, "rerank": rerank_status})
    except Exception as e:
        return err(f"获取配置失败: {e}")


async def handle_memory_list(memory_store, session_id=None):
    """列出记忆（可按 session_id 过滤）。

    当有 session_id 时返回该 session 的记忆；
    无 session_id 时返回全部记忆（跨 session，按时间倒序）。
    """
    try:
        if session_id:
            memories = await asyncio.to_thread(memory_store.list_memories, session_id)
            return ok({"session_id": session_id, "memories": memories})
        # 无 session_id：返回全部记忆（倒序），供记忆浏览表格用
        memories = await asyncio.to_thread(memory_store.list_all_memories, 200)
        return ok({"memories": memories, "sessions": []})
    except Exception as e:
        return err(f"查询失败: {e}")


async def handle_memory_delete(memory_store, memory_id=None, session_id=None):
    """删除记忆。"""
    try:
        if memory_id:
            ok_del = await asyncio.to_thread(memory_store.delete_memory, memory_id)
            return ok({"deleted": ok_del, "memory_id": memory_id}) if ok_del else err("记忆不存在")
        if session_id:
            deleted = await asyncio.to_thread(memory_store.delete_session_memories, session_id)
            return ok({"deleted": deleted, "session_id": session_id})
        return err("需要 memory_id 或 session_id")
    except Exception as e:
        return err(f"删除失败: {e}")


async def handle_memory_list_all(memory_store, limit=200):
    """列出全部记忆（跨 session），按创建时间倒序。

    前端记忆浏览表格调用此接口获取按时间排序的全量列表。
    """
    try:
        memories = await asyncio.to_thread(memory_store.list_all_memories, limit)
        return ok({"memories": memories, "total": len(memories)})
    except Exception as e:
        return err(f"查询失败: {e}")


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
        return err(f"获取提供商列表失败: {e}")


async def handle_memory_export(memory_store):
    """导出全部记忆为 JSON 字符串。"""
    try:
        memories = await asyncio.to_thread(memory_store.list_all_memories, 10000)
        return ok({"memories": memories, "total": len(memories)})
    except Exception as e:
        return err(f"导出失败: {e}")


async def handle_chat_log_list(memory_store, session_id=None, limit=200):
    """列出对话日志"""
    if not session_id:
        return err("需要 session_id")
    try:
        logs = await asyncio.to_thread(memory_store.list_chat_logs, session_id, limit)
        return ok({"session_id": session_id, "logs": logs, "total": len(logs)})
    except Exception as e:
        return err(f"查询失败: {e}")


async def handle_chat_log_export(memory_store, session_id=None, format="markdown"):
    """导出对话日志"""
    if not session_id:
        return err("需要 session_id")
    try:
        text = await asyncio.to_thread(memory_store.export_chat_logs, session_id, format)
        return ok({"session_id": session_id, "format": format, "content": text})
    except Exception as e:
        return err(f"导出失败: {e}")


async def handle_memory_import(memory_store, embedding_provider, data):
    """从 JSON 数据批量导入记忆（异步，重新生成向量）。"""
    try:
        if not data or not isinstance(data, dict):
            return err("无效的数据格式")
        memories = data.get("memories", [])
        if not isinstance(memories, list):
            return err("memories 必须是数组")
        imported = 0
        failed = 0
        for m in memories:
            try:
                summary = m.get("summary", "")
                chat_summary = m.get("chat_summary", "")
                session_id = m.get("session_id", "imported")
                if not summary:
                    failed += 1
                    continue
                # 重新生成向量（必须步骤，否则检索无法命中）
                vector = None
                if embedding_provider:
                    try:
                        vectors = await embedding_provider.embed([summary])
                        if vectors:
                            vector = vectors[0]
                    except Exception as e:
                        logger.warning(f"[Quill Memory] import embed failed: {e}")
                        failed += 1
                        continue
                else:
                    # 没有 embedding provider 时跳过（无法检索）
                    failed += 1
                    continue
                if vector:
                    await asyncio.to_thread(memory_store.add, session_id, summary, vector, chat_summary)
                    imported += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning(f"[Quill Memory] import single failed: {e}")
                failed += 1
                continue
        return ok({"imported": imported, "failed": failed, "message": f"成功导入 {imported}/{len(memories)} 条"})
    except Exception as e:
        return err(f"导入失败: {e}")


# ── Shared helpers ───────────────────────────────────────────────
# (save_worldbook removed — replaced by atomic WorldbookManager methods)
