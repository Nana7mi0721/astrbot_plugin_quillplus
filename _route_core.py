# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared route handler logic for Quill management API.

Used by web_routes.py (AstrBot mode).
Contains zero HTTP/framework dependencies — pure async handler functions
that receive already-parsed data and return plain dicts.

Each handler returns {"status": "ok", "data": ...} or {"status": "error", "message": "..."}.
"""

import asyncio
import os
import re
import tempfile
import time
import uuid
from typing import Any

from astrbot.api import logger

from .quill.core.errors import StorageError
from .quill.core.storage_stats import storage_error_snapshot


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

    try:
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
    except StorageError as e:
        # M3.2 D4：底层存储失败上抛后在路由层降级——信封形状不变
        # （此前存储失败被吞成 False，误报为「ID 可能已存在」）。
        return err(error_text("创建条目失败", e))
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
    try:
        deleted = await wr_manager.delete_entry(entry_id)
    except StorageError as e:
        # M3.2 D4：存储失败与「条目不存在」区分（信封形状不变）。
        return err(error_text("删除条目失败", e))
    if not deleted:
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
            # P1-d：条件字段随 ST 导出落盘（原生 ST 工具会忽略未知字段，
            # 本插件导出的卡 re-import 时条件不丢）
            "condition": entry.get("condition", ""),
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
    # M3.2 D4：存储层六类高频路径（add/search/prune/delete/backup/restore）
    # 的底层失败累计计数（进程内，重启清零）。结构见
    # quill/core/storage_stats.py 模块 docstring；只增字段，响应向后兼容。
    storage_errors = storage_error_snapshot()
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
        "storage_errors": storage_errors,
    })


# ── RAG Handlers ────────────────────────────────────────────────

async def handle_rag_upload(vector_store, embedding_provider, upload_file, source_name, chunk_size=500, chunk_overlap=50,
                            group_store=None, group=""):
    """处理文档上传：提取文本 → 分块 → 向量化 → 存储（可选归入某个分组）。

    ``group`` 非空时把新文档归入该分组（子知识库）。归组失败不影响入库结果
    ——文档已经花钱算完向量了，不能因为分组名过期就把它丢掉；改为在响应里
    带回 ``group_error`` 让面板提示。
    """
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
        # add() 现在返回实际入库条数、失败则抛异常。0 表示文本进了 SQLite
        # 但 FAISS 不可用（暂不可检索）——这不能当成成功上报，否则界面显示
        # 「上传成功」而用户检索不到，是比报错更糟的静默失败。
        stored = await vector_store.add(chunks, embeddings, source_name)
        if stored == 0:
            return err(
                "文档已保存，但向量索引不可用（faiss 未安装或初始化失败），"
                "暂无法检索。请检查依赖后重新上传。"
            )
        # 归组（可选）：面板上传时选的「目标分组」
        group_error = ""
        target_group = str(group or "").strip()
        if target_group and group_store is not None:
            try:
                await group_store.assign([source_name], target_group)
            except Exception as e:  # noqa: BLE001 — 归组失败不能吞掉已入库的文档
                group_error = str(e) or "归组失败"
                logger.warning("[Quill] 上传后归组失败: %s", e)

        payload = {
            "source": source_name,
            "chunk_count": stored,
            "dim": len(embeddings[0]) if embeddings else 0,
        }
        if group_error:
            payload["group_error"] = group_error
        return ok(payload)
    except Exception as e:
        return err(error_text("上传失败", e))

async def handle_rag_documents(vector_store, kb_source=None, group_store=None):
    """列出已上传文档 + 插件文档分组 + AstrBot 内置知识库（只读展示）。

    vector_store 允许为 None（本地 RAG 未初始化、仅内置库可用的场景），
    此时 documents 为空列表。
    """
    try:
        docs = await vector_store.list_documents() if vector_store is not None else []
        # M3.3 F1：附带向量索引状态（真实维度 / 维度不一致标记 / 最近重建），
        # 面板 RAG 页据此感知「索引将按新维度重建」类状态。只增字段，向后兼容。
        index_status = None
        get_stats = getattr(vector_store, "get_stats", None) if vector_store is not None else None
        if get_stats is not None:
            try:
                index_status = await get_stats()
            except Exception as e:
                logger.warning("[Quill] 获取向量索引状态失败: %s", e, exc_info=True)
        # AstrBot 内置知识库列表（S-兼容新增，只读——管理入口在 AstrBot 面板）。
        # kb_source 不可达时返回空列表，旧前端忽略该字段不受影响。
        astrbot_kbs: list = []
        if kb_source is not None and getattr(kb_source, "available", False):
            try:
                astrbot_kbs = await kb_source.list_kbs()
            except Exception as e:
                logger.warning("[Quill] 列举内置知识库失败: %s", e, exc_info=True)
        # 插件文档分组（子知识库）与每个文档的 group 字段。分组存储不可用时
        # 只回空分组列表，旧前端与「只有内置库」的场景都不受影响。
        groups: list = []
        if group_store is not None:
            try:
                # 文档可能已被删除，先清掉指向幽灵文档的分组条目
                await group_store.prune(
                    [str(d.get("source") or "") for d in docs]
                )
                docs = await group_store.attach_documents(docs)
                groups = await group_store.list_groups()
            except Exception as e:
                logger.warning("[Quill] 文档分组信息获取失败: %s", e, exc_info=True)
        return ok({"documents": docs, "groups": groups,
                   "index_status": index_status, "astrbot_kbs": astrbot_kbs})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_rag_kb_documents(kb_source, kb_name, limit: int = 200):
    """列出某个 AstrBot 内置知识库里的文档（面板子知识库组展开时懒加载）。

    kb_source 不可达 / 库不存在 → documents 为空列表，同时用 available
    告诉前端是「功能不可达」还是「这个库确实没有文档」。
    """
    if not kb_name or not str(kb_name).strip():
        return err("缺少 kb_name 参数")
    try:
        available = bool(kb_source is not None and getattr(kb_source, "available", False))
        docs: list = []
        if available:
            docs = await kb_source.list_documents(str(kb_name).strip(), limit=int(limit) or 200)
        return ok({"kb_name": str(kb_name).strip(), "available": available, "documents": docs})
    except Exception as e:
        return err(error_text("查询失败", e))


async def handle_rag_group_action(group_store, action, name="", new_name="", sources=None, group=""):
    """插件文档分组的增删改与归组（面板「知识库」页与角色卡绑定共用）。

    ``action``：

    - ``create``：新建分组（``name``）；
    - ``rename``：改名（``name`` → ``new_name``），组内文档跟着走；
    - ``delete``：删除分组（``name``），组内文档回到「未分组」；
    - ``assign``：把 ``sources`` 移入 ``group``（``group`` 空串 = 移出到未分组）。
      只允许操作插件内上传的文档（``sources`` 就是插件的 doc source）。

    失败一律返回可读的 ``message``（分组名非法/重名/不存在都是用户输入问题，
    不该在日志里刷 traceback）。
    """
    if group_store is None:
        return err("文档分组功能不可用（本地 RAG 组件未初始化）")
    from .quill_rag.doc_groups import normalize_group_name
    act = str(action or "").strip().lower()
    try:
        if act == "create":
            groups = await group_store.create(name)
            return ok({"action": act, "group": normalize_group_name(name), "groups": groups})
        if act == "rename":
            moved = await group_store.rename(name, new_name)
            return ok({"action": act, "group": normalize_group_name(new_name),
                       "moved": moved, "groups": await group_store.list_groups()})
        if act == "delete":
            freed = await group_store.delete(name)
            return ok({"action": act, "group": normalize_group_name(name),
                       "freed": freed, "groups": await group_store.list_groups()})
        if act == "assign":
            srcs = [str(s).strip() for s in (sources or []) if str(s or "").strip()]
            if not srcs:
                return err("缺少要移动的文档（sources）")
            target = str(group or "").strip()
            changed = await group_store.assign(srcs, target)
            return ok({"action": act, "group": target, "changed": changed,
                       "sources": srcs})
        return err(f"未知的分组操作: {action}")
    except ValueError as e:
        return err(str(e))
    except Exception as e:
        return err(error_text("分组操作失败", e))


async def handle_rag_delete(vector_store, source, group_store=None):
    """删除文档（顺手清掉它的分组记录）。"""
    try:
        deleted = await vector_store.delete_by_source(source)
        if group_store is not None:
            try:
                # 移出分组＝删除 assign 条目（等价于回到「未分组」）
                await group_store.assign([source], "")
            except Exception as e:  # noqa: BLE001 — 文档已删，清分组失败只是残留
                logger.warning("[Quill] 删除文档后清理分组记录失败: %s", e)
        return ok({"deleted": deleted, "source": source})
    except Exception as e:
        return err(error_text("删除失败", e))


async def handle_rag_search(vector_store, embedding_provider, reranker, query, top_k=3, kb_source=None):
    """语义检索测试（本地 FAISS + AstrBot 内置知识库两路合并）。"""
    try:
        from .quill_rag.retrieval import QuillRetriever
        results: list = []
        # 本地路：embedding/vector_store 任一缺失就跳过（组件为 None 时
        # search_documents 会走异常路径，与其空跑不如不发请求）
        if vector_store is not None and embedding_provider is not None:
            retriever = QuillRetriever(
                embedding_provider=embedding_provider,
                vector_store=vector_store,
                reranker=reranker,
                top_k=top_k,
            )
            results.extend(await retriever.search_documents(query))
        # 内置库路：不依赖本地组件，embedding/reranker 未配置也能检索
        if kb_source is not None and getattr(kb_source, "available", False):
            try:
                kb_results = await kb_source.search(query)
                results.extend(kb_results)
            except Exception as e:
                logger.warning("[Quill] 内置知识库检索测试失败: %s", e, exc_info=True)
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


async def handle_memory_list_all(memory_store, page=1, per_page=50):
    """列出全部记忆（跨 session），按创建时间倒序，支持分页。

    返回 total 为数据库中记忆的真实总数（不受分页限制），
    前端据此修正"总览"和"列表"之间的数量脱节问题。
    D7：删掉从未被使用的 ``limit`` 形参（唯一调用方只传 page/per_page）。
    """
    try:
        per_page = min(max(1, per_page), 200)
        page = max(1, page)
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


# ── Trace handlers (M1a 注入诊断) ─────────────────────────────────

#: 会话键/trace id 的形制守卫：hash_target_id 取 SHA-256 前 16 位、
#: trace_id 是 uuid4().hex（32 位）。fullmatch 挡掉拼接注入与超长串。
_TRACE_TARGET_RE = re.compile(r"[0-9a-f]{16}")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")


def _trace_store_of(plugin):
    """安全取插件实例上的 TraceStore（无插件/未初始化 → None）。"""
    return getattr(plugin, "_trace_store", None) if plugin is not None else None


def _trace_enabled_of(plugin) -> bool:
    props = getattr(plugin, "props", None) if plugin is not None else None
    return bool(getattr(props, "trace_enabled", False))


async def handle_trace_recent(plugin, target=None, limit=20):
    """M1a：最近追踪轮次。``target`` 缺省 → 有记录的会话摘要（面板下拉）；
    给定 16 位 hash → 该会话最近 N 轮（最新在前）。

    ``enabled`` 字段诚实区分「追踪关闭」与「暂无记录」——沿用注入报告
    「采集过但零命中 ≠ 从没采集过」的口径（见 main._remember_inject_report）。
    """
    store = _trace_store_of(plugin)
    if store is None:
        return err("追踪存储未初始化")
    enabled = _trace_enabled_of(plugin)
    if not target:
        return ok({"enabled": enabled, "sessions": store.sessions_summary()})
    target = str(target).strip().lower()
    if not _TRACE_TARGET_RE.fullmatch(target):
        return err("target 参数须为 16 位十六进制会话哈希")
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    # store.recent 返回旧→新；面板要最新在前，倒一下
    items = [t.to_dict() for t in reversed(store.recent(target, limit))]
    return ok({"enabled": enabled, "target_id_hash": target, "items": items})


async def handle_trace_detail(plugin, trace_id=None):
    """M1a：单轮追踪详情（含模块行/段序/token 估算/状态栏级别/inject_stats）。

    M1b 模块 4：TraceStore 查不到时兜底查模拟缓存——``/quill test`` 与
    trace/simulate 的产物不进 TraceStore（零写入红线），锚点 id 经由此处
    与真实轮次用同一查询口。
    """
    store = _trace_store_of(plugin)
    if store is None:
        return err("追踪存储未初始化")
    trace_id = str(trace_id or "").strip().lower()
    if not _TRACE_ID_RE.fullmatch(trace_id):
        return err("id 参数须为 32 位十六进制 trace id")
    trace = store.get(trace_id)
    simulated = False
    if trace is None:
        trace = _simulate_cache_of(plugin).get(trace_id)
        simulated = trace is not None
    if trace is None:
        return err("未找到该追踪记录（可能已被淘汰，或实例已重启清空）")
    data = trace.to_dict()
    if simulated:
        # 与 trace/simulate 响应同构：模拟轮补 simulate 标记（面板/命令
        # 锚点凭它区分「真实轮」与「只读沙盒产物」）
        data["simulate"] = True
    return ok(data)


# ── M1b 注入实验室（模拟检索 / 静态体检）─────────────────────────────

#: 模拟结果缓存上限（/quill test 锚点 + 面板重复展开用；零写入红线的
#: 例外是这块纯内存临时缓存——它不进 TraceStore、不落盘、重启即清）
_SIMULATE_CACHE_LIMIT = 10


def _simulate_cache_of(plugin) -> dict:
    """按需取插件的模拟结果缓存（普通 dict + 手动淘汰最旧键）。"""
    cache = getattr(plugin, "_quill_simulate_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(plugin, "_quill_simulate_cache", cache)
        except Exception:
            return {}
    return cache


def _persona_ext(persona_data) -> dict:
    return (persona_data or {}).get("quill_extensions", {}) or {}


async def handle_trace_simulate(plugin, text=None, persona_id=None,
                                target_id_hash=None, rag=False, umo=None,
                                session_vars=None, include_recent_context=False):
    """M1b 模块 4：模拟检索沙盒（只读，零写入 TraceStore/state/chat_logs）。

    入参 ``{text, persona_id?, target_id_hash?, rag?, umo?, session_vars?,
    include_recent_context?}``。流程镜像 22 步的检索侧（激活 → 世界书
    （含条件）→ WR → 可选 RAG/记忆 → 预算画像），但：

    * ``rag`` 缺省 False——零 provider 请求（E2E 断言点）；显式开启且
      提供 ``umo`` 才跑文档/记忆检索（记忆直连 memory_store.search，
      绕过 search_memories 的 mark_memories_used 写入）；
    * WR 检索 ``log_match=False``（不累计 match_count、不写匹配日志）；
    * 产出临时 ``ContextTrace``（``simulate=True`` 标记），存
      ``_quill_simulate_cache``（上限 10 条）供 trace/detail 锚点查询。

    ``session_vars`` 显式覆盖 > ``umo`` 现场读取 > 空 dict（条件条目
    fail-close，与主链路同一语义）。
    """
    from .quill.services import trace as _trace_mod

    text = str(text or "").strip()
    if not text:
        return err("缺少 text 参数（要模拟的用户输入）")
    persona_id = str(persona_id or "").strip() or None

    persona_data = None
    if persona_id and getattr(plugin, "persona_manager", None):
        try:
            persona_data = await plugin.persona_manager.get_persona(persona_id)
        except Exception as e:
            return err(error_text("读取角色卡失败", e))
    ext = _persona_ext(persona_data)

    # session_vars：显式覆盖 > umo 现场读 > 空（条件 fail-close）
    svars = session_vars if isinstance(session_vars, dict) else None
    if svars is None and umo:
        try:
            svars = await plugin.state_manager.get_session_vars(str(umo))
        except Exception:
            svars = None
    svars = svars or {}

    # 匹配文本：面板粘贴的多轮文本即 context；API 侧可选拼最近日志
    matching_text = text
    if include_recent_context and umo:
        retriever = getattr(plugin, "rag_retriever", None)
        if retriever and retriever.memory_store:
            try:
                mem_sid = plugin._get_memory_session_id(str(umo), persona_id or "")
                logs = await retriever.memory_store.get_recent_chat_logs(mem_sid, limit=8)
                if logs:
                    ctx = "\n".join(
                        f"{lg.get('role', '?')}: {str(lg.get('content', ''))[:200]}"
                        for lg in logs if isinstance(lg, dict)
                    )
                    matching_text = f"{ctx}\nuser: {text}"
            except Exception as exc:
                logger.warning("[Quill Trace] 拼接最近上下文失败（跳过）: %s", exc)

    trace = _trace_mod.ContextTrace(
        target_id_hash=str(target_id_hash or "")[:16],
        persona_id=persona_id or "",
        persona_name_at_turn=str((persona_data or {}).get("name", "") or ""),
    )
    trace.warnings.append("模拟结果（只读沙盒，未写入追踪列表）")
    rows: list = []
    _t0 = time.perf_counter()

    # ── 激活判定（复用 detector / WR 组件，fail-close 同主链路）──
    activated = False
    activation_reason = None
    detector = getattr(plugin, "activation_detector", None)
    if detector is not None:
        try:
            if detector.should_activate(text):
                activated, activation_reason = True, "激活词"
            elif detector.check_brackets(text):
                activated, activation_reason = True, "括号"
        except Exception:
            activated, activation_reason = False, None
    if not activated and getattr(plugin, "wr_manager", None) \
            and ext.get("wr_mode", "disabled") != "disabled":
        try:
            # M1b-Fix P1-b：WR 激活匹配面与主链路对齐——主链路
            # _check_activation 的 WR 路按 context_text（最近 4 轮拼串）
            # 判关键词，simulate 此前只用裸 text，多轮语境触发在模拟里
            # 永远报未激活。这里同样用 matching_text（面板粘贴的多轮文本
            # / include_recent_context 拼串），custom 模式同样过滤绑定分类。
            act_bound = ext.get("bound_writing_resource", []) \
                if ext.get("wr_mode") == "custom" else None
            act_matched = await plugin.wr_manager.match(
                matching_text, top_k=7 if act_bound is not None else 3, log_match=False)
            if act_bound is not None:
                act_matched = [m for m in act_matched if m.get("category") in act_bound]
            if act_matched:
                activated, activation_reason = True, "WR关键词"
        except Exception as exc:
            logger.warning("[Quill Trace] simulate WR 激活匹配失败（按未激活处理）: %s", exc)
    # M1b-Fix P1-b：全局常驻模式（worldbook_always_activate）主链路在步 5
    # 直接 activated=True——simulate 此前漏这一档，常驻模式下模拟永远报
    # 「未激活」，与真实轮相反。
    if getattr(plugin.config, "worldbook_always_activate", False):
        activated = True
        activation_reason = "常驻模式"
    trace.activated = activated
    trace.activation_reason = activation_reason

    wb_manager = getattr(plugin, "wb_manager", None)
    wb_mode = ext.get("wb_mode", "disabled")
    bound_worldbooks = ext.get("bound_worldbooks", []) if wb_mode == "custom" else None
    wb_constants: list = []
    sim_chars = 0

    # ── 世界书（含条件条目；总闸关 = disabled）──
    if wb_manager and wb_mode != "disabled" and getattr(plugin.config, "worldbook_enabled", True):
        try:
            wb_funnel: dict = {}
            wb_matched = await asyncio.to_thread(
                wb_manager.match_entries, matching_text,
                bound_worldbooks=bound_worldbooks,
                top_k=int(getattr(plugin.props, "wb_max_entries", 4) or 4),
                sensitivity=float(getattr(plugin.config, "worldbook_sensitivity", 0.7) or 0.7),
                session_vars=svars, funnel=wb_funnel,
                # M1b-Fix P1-b：只读沙盒不覆写真实会话的触发日志
                # （/quill test wb 读的就是 _last_trigger_log）。
                write_trigger_log=False,
            )
            wb_constants = await asyncio.to_thread(
                wb_manager.get_constant_entries,
                bound_worldbooks=bound_worldbooks, session_vars=svars,
                # M1b-Fix P1-d：条件不成立的常驻条目记 condition_unmet 近失
                # （在 match_entries 之后调用，只 append 不覆写 filtered）。
                funnel=wb_funnel,
            )
            items = [
                {"source_ref": f"{e.get('_source_worldbook', '?')}/{e.get('title', '?')}",
                 "score_or_kw": ",".join(k for k in (e.get("_matched_keys") or [])[:4])}
                for e in wb_matched[:10]
            ] + _trace_mod.funnel_filtered_items(wb_funnel)
            rows.append(_trace_mod.TraceModuleResult(
                module="worldbook", ok=True,
                hits=len(wb_matched) + len(wb_constants),
                candidates=int(wb_funnel.get("candidates", 0) or 0),
                stage_ms=int((time.perf_counter() - _t0) * 1000),
                items=items,
            ))
            sim_chars += sum(len(str(e.get("content", "") or "")) for e in wb_matched)
            sim_chars += sum(len(str(e.get("content", "") or "")) for e in wb_constants)
        except Exception as e:
            rows.append(_trace_mod.TraceModuleResult(
                module="worldbook", ok=False, error="simulate_error",
                items=[{"filtered_reason": type(e).__name__}],
            ))
    else:
        rows.append(_trace_mod.TraceModuleResult(
            module="worldbook", ok=True,
            degraded="disabled" if wb_mode == "disabled" or not getattr(
                plugin.config, "worldbook_enabled", True) else None,
        ))

    # ── WR 素材（log_match=False：零写入）──
    wr_manager = getattr(plugin, "wr_manager", None)
    wr_mode = ext.get("wr_mode", "disabled")
    bound_wrs = ext.get("bound_writing_resource", []) if wr_mode == "custom" else None
    if wr_manager and wr_mode != "disabled":
        try:
            wr_funnel: dict = {}
            fetch = int(getattr(plugin.props, "wr_max_entries", 5) or 5)
            wr_matched = await wr_manager.match(
                matching_text, top_k=fetch, log_match=False, funnel=wr_funnel)
            if bound_wrs is not None:
                wr_matched = [e for e in wr_matched if e.get("category") in bound_wrs]
            items = [
                {"source_ref": str(e.get("name") or e.get("entry_id") or "?")[:40],
                 **({"score_or_kw": f"{float(e['match_score']):.1f}"}
                    if isinstance(e.get("match_score"), (int, float)) else {}),
                 **({"score_or_kw": ",".join(str(k) for k in (e.get("matched_keywords") or [])[:4])}
                    if not isinstance(e.get("match_score"), (int, float)) and e.get("matched_keywords") else {})}
                for e in wr_matched[:10]
            ] + _trace_mod.funnel_filtered_items(wr_funnel)
            if wr_funnel.get("fallback_pool"):
                items.append({"source_ref": "fallback_pool",
                              "filtered_reason": "fallback_pool(命中0，保底注入)"})
            rows.append(_trace_mod.TraceModuleResult(
                module="wr", ok=True, hits=len(wr_matched),
                candidates=int(wr_funnel.get("candidates", 0) or 0),
                stage_ms=int((time.perf_counter() - _t0) * 1000),
                items=items,
            ))
            sim_chars += sum(len(str(e.get("content", "") or "")) for e in wr_matched)
        except Exception as e:
            rows.append(_trace_mod.TraceModuleResult(
                module="wr", ok=False, error="simulate_error",
                items=[{"filtered_reason": type(e).__name__}],
            ))
    else:
        rows.append(_trace_mod.TraceModuleResult(module="wr", ok=True, degraded="disabled"))

    # ── RAG / 记忆（rag 显式开启 + umo 才跑；记忆直连 search 避开写入）──
    retriever = getattr(plugin, "rag_retriever", None)
    rag_docs: list = []
    mem_results: list = []
    core_mems: list = []
    if rag:
        if umo and retriever:
            rag_funnel: dict = {}
            mem_funnel: dict = {}
            # M1b-Fix Bug3（核验报告 §9.2#3）：文档路与主链 _run_rag_retrieval
            # 对齐——按角色卡 rag_mode 判定 + custom 绑定展开（kb:/grp: 前缀、
            # 插件文档源过滤）+ AstrBot 内置知识库路。此前无脑全库检索：
            # custom 绑定的卡在模拟里会看到绑外内容，与真实轮相反。
            rag_mode = ext.get("rag_mode", "disabled")
            if rag_mode != "disabled":
                plugin_sources: list | None = None
                kb_names: list | None = None
                kb_doc_filters: dict = {}
                if rag_mode == "custom":
                    try:
                        from .quill_rag.astrbot_kb import split_bound_ex
                        group_store = getattr(plugin, "rag_doc_groups", None)
                        plugin_sources, kb_names, kb_doc_filters = split_bound_ex(
                            ext.get("bound_rag_docs", []),
                            group_resolver=(group_store.resolve if group_store else None),
                        )
                    except Exception:
                        plugin_sources, kb_names, kb_doc_filters = None, None, {}
                try:
                    if retriever.embedding:
                        rag_docs = await retriever.search_documents(
                            matching_text, allowed_sources=plugin_sources, funnel=rag_funnel)
                    # 内置库路（custom 模式仅绑定的 kb: 源；kb_source 解析
                    # 顺序与主链一致：retriever 句柄优先，插件引用兜底）
                    kb_source = getattr(retriever, "kb_source", None) \
                        or getattr(plugin, "rag_kb_source", None)
                    if kb_source and getattr(kb_source, "available", False) \
                            and (rag_mode != "custom" or kb_names):
                        kb_docs = await kb_source.search(
                            matching_text,
                            kb_names=kb_names if rag_mode == "custom" else None,
                            doc_filters=kb_doc_filters if rag_mode == "custom" else None,
                        )
                        rag_docs.extend(kb_docs or [])
                except Exception:
                    rows.append(_trace_mod.TraceModuleResult(
                        module="rag_doc", ok=False, error="simulate_error"))
                else:
                    rows.append(_trace_mod.TraceModuleResult(
                        module="rag_doc", ok=True, hits=len(rag_docs),
                        candidates=int(rag_funnel.get("candidates", 0) or 0),
                        items=[
                            {"source_ref": (str(r.get("source", "") or "").strip() or "unknown")[:40],
                             **({"score_or_kw": round(float(r["score"]), 3)}
                                if isinstance(r.get("score"), (int, float)) else {})}
                            for r in rag_docs[:10]
                        ] + _trace_mod.funnel_filtered_items(rag_funnel),
                    ))
            else:
                rows.append(_trace_mod.TraceModuleResult(
                    module="rag_doc", ok=True, degraded="disabled",
                    items=[{"filtered_reason": "角色卡 rag_mode=disabled"}]))
            try:
                mem_sid = plugin._get_memory_session_id(str(umo), persona_id or "")
                if retriever.memory_store and retriever.embedding:
                    emb = await retriever.embedding.embed([matching_text])
                    if emb:
                        mem_results = await retriever.memory_store.search(
                            mem_sid, emb[0], retriever.top_k,
                            query_text=matching_text, funnel=mem_funnel)
                core_mems = await retriever.get_core_memories(mem_sid)
                rows.append(_trace_mod.TraceModuleResult(
                    module="memory", ok=True, hits=len(mem_results),
                    candidates=int(mem_funnel.get("candidates", 0) or 0),
                    # M1b-Fix Bug3：补留存记忆命中明细（只记 id 与 rrf 分，
                    # 不携带原文——与主链 main._run_rag_retrieval 同口径）
                    items=[
                        {"source_ref": f"mem#{r.get('id', '?')}",
                         **({"score_or_kw": f"rrf {float(r['rrf_score']):.4f}"}
                            if isinstance(r.get("rrf_score"), (int, float)) else {})}
                        for r in mem_results[:10]
                    ] + _trace_mod.funnel_filtered_items(mem_funnel),
                ))
                rows.append(_trace_mod.TraceModuleResult(
                    module="core_memory", ok=True, hits=len(core_mems)))
            except Exception:
                rows.append(_trace_mod.TraceModuleResult(
                    module="memory", ok=False, error="simulate_error"))
                rows.append(_trace_mod.TraceModuleResult(
                    module="core_memory", ok=True, degraded="skipped"))
            sim_chars += sum(len(str(r.get("content", "") or "")) for r in rag_docs)
            sim_chars += sum(len(str(r.get("summary", "") or "")) for r in mem_results)
            sim_chars += sum(len(str(r.get("summary", "") or "")) for r in core_mems)
        else:
            reason = "未提供 umo" if not umo else "检索栈未初始化"
            rows.append(_trace_mod.TraceModuleResult(
                module="rag_doc", ok=True, degraded="skipped",
                items=[{"filtered_reason": reason}]))
            rows.append(_trace_mod.TraceModuleResult(
                module="memory", ok=True, degraded="skipped",
                items=[{"filtered_reason": reason}]))
            rows.append(_trace_mod.TraceModuleResult(
                module="core_memory", ok=True, degraded="skipped",
                items=[{"filtered_reason": reason}]))
    else:
        # 缺省关：零 provider 请求（E2E 断言）——embedding 也不发
        for mod in ("rag_doc", "memory", "core_memory"):
            rows.append(_trace_mod.TraceModuleResult(
                module=mod, ok=True, degraded="skipped",
                items=[{"filtered_reason": "模拟默认不跑 RAG（勾选 rag 且提供 umo 才跑）"}]))

    # ── 预算画像（量级参考：不含角色卡正文——那是宿主 original prompt，
    #    模拟侧不可得；对照 max_prompt_length 给出超限预警）──
    budget = int(getattr(plugin.config, "max_prompt_length", 0) or 0)
    trace.token_estimate = {
        "simulated_chars": sim_chars,
        "est_tokens": sim_chars // 2,
        "max_prompt_length": budget,
        "over_budget": bool(budget > 0 and sim_chars > budget),
    }
    if budget > 0 and sim_chars > budget:
        trace.truncation_events.append({
            "where": "simulate", "kind": "budget_projection",
            "dropped_chars": sim_chars - budget,
            "detail": "模拟内容量已超 max_prompt_length——真实链路步 18.5 守卫会先撤触发日志、再裁 RAG 尾部",
        })

    trace.module_results = rows
    trace.duration_ms = int((time.perf_counter() - _t0) * 1000)
    cache = _simulate_cache_of(plugin)
    if cache is not None:
        cache[trace.trace_id] = trace
        while len(cache) > _SIMULATE_CACHE_LIMIT:
            cache.pop(next(iter(cache)), None)
    data = trace.to_dict()
    data["simulate"] = True
    return ok(data)


async def handle_decision_probe(plugin, base_url=None, api_key=None,
                                model=None, api_format=None) -> dict:
    """决策模型连通性测试（读配置为准，入参只用于「改了还没保存」的场景）。

    面板上填了新地址/新模型 ID 就想先点一下测试、此时还没点保存——入参优先，
    缺省回落 ``self.config`` 的当前值。API 密钥**不回传给前端**，也不接受
    空串覆盖：``api_key`` 传 ``None`` 或空值时用配置里那把（否则测试永远
    401，用户会误以为地址错了）。

    与生产路径的差别：``decision_evaluate`` 任何失败都吞成 None（fail-open），
    这里把失败阶段（config/network/http/format）与原始错误片段一并带回，
    因为用户要据此判断该改地址、改模型还是改网络。
    """
    from .quill.services.statusbar import decision_client as dc

    base = (base_url if base_url is not None
            else getattr(plugin.config, "status_bar_decision_base_url", ""))
    key = (api_key if api_key else
           getattr(plugin.config, "status_bar_decision_api_key", ""))
    mid = (model if model is not None
           else getattr(plugin.config, "status_bar_decision_model", ""))
    fmt = (api_format if api_format is not None
           else getattr(plugin.config, "status_bar_decision_api_format", "auto"))

    result = await dc.decision_probe(
        str(base or ""), str(key or ""), str(mid or ""), str(fmt or "auto")
    )
    # 成功时也把「这次实际打到哪个端点、用了哪族格式」说清楚：auto 模式下
    # 用户看不到它猜了哪族，出问题无从下手。
    result["configured_base_url"] = str(base or "").strip()
    result["configured_model"] = str(mid or "").strip()
    if not result["ok"]:
        logger.info(
            "[Quill] 决策模型连通性测试失败 | stage=%s url=%s model=%s | %s",
            result.get("stage"), result.get("url"), result.get("configured_model"),
            result.get("message"),
        )
    return ok(result)


async def handle_worldbook_lint(plugin) -> dict:
    """M1b 模块 5：世界书/角色卡静态体检（纯本地分析，零模型调用）。

    数据全部从既有管理器**只读**读取（vector_store.list_documents 不经
    handle_rag_documents——那会触发 group_store.prune 的清理写入）。
    返回 ``{findings, summary}``；summary 给面板徽标（按 check 计数）。
    """
    from .quill.services import lint as _lint_mod

    findings: list = []
    try:
        wb_manager = getattr(plugin, "wb_manager", None)
        worldbooks: list = []
        if wb_manager:
            def _collect():
                out = []
                for n in wb_manager.list_worldbooks():
                    wb = wb_manager.get_worldbook(n)
                    if wb:
                        wb.setdefault("name", n)
                        out.append(wb)
                return out
            worldbooks = await asyncio.to_thread(_collect)

        sensitivity = float(getattr(plugin.config, "worldbook_sensitivity", 0.7) or 0.7)
        findings += _lint_mod.lint_worldbooks(worldbooks, sensitivity=sensitivity)

        # 幽灵绑定：全部角色卡的绑定对照现存资源名
        persona_manager = getattr(plugin, "persona_manager", None)
        wr_manager = getattr(plugin, "wr_manager", None)
        personas: list = []
        if persona_manager:
            wb_names = {str(wb.get("name", "")) for wb in worldbooks}
            wr_cats: set = set()
            if wr_manager:
                try:
                    wr_cats = {str(c) for c in (await wr_manager.get_categories())}
                except Exception:
                    wr_cats = set()
            rag_sources: set = set()
            vector_store = getattr(getattr(plugin, "rag_retriever", None), "vector_store", None)
            if vector_store is not None:
                try:
                    docs = await vector_store.list_documents()
                    rag_sources = {str(d.get("source", "")).removeprefix("grp:")
                                   for d in (docs or [])}
                except Exception:
                    rag_sources = set()
            personas = await persona_manager.load_all() or []
            for p in (personas or []):
                findings += _lint_mod.lint_persona_bindings(
                    p, worldbook_names=wb_names,
                    wr_categories=wr_cats, rag_sources=rag_sources)

        # 常驻预算水位（素材常驻字符量由 kb 侧汇总）
        wr_constant_chars = 0
        if wr_manager:
            try:
                constants = await wr_manager.get_constant_entries()
                wr_constant_chars = sum(len(str(e.get("content", "") or "")) for e in constants)
            except Exception:
                wr_constant_chars = 0
        # M1b-Fix Bug5：按角色卡绑定集合算单卡峰值——(卡名, 绑定书 | None=auto
        # 全量 | []=disabled, core_prompts 字符量)。core_prompts 只计进 system
        # prompt 的三段（first_message 走聊天首条消息，不占常驻预算）。
        persona_bindings: list = []
        for p in personas or []:
            pext = p.get("quill_extensions", {}) or {}
            if pext.get("wb_mode") == "custom":
                books = list(pext.get("bound_worldbooks", []) or [])
            elif pext.get("wb_mode") == "disabled":
                books = []
            else:
                books = None  # auto：全量书
            cp = p.get("core_prompts", {}) or {}
            core_chars = sum(
                len(str(v) or "")
                for k, v in cp.items() if k != "first_message"
            )
            persona_bindings.append((
                str(p.get("name", "") or p.get("id", "?")), books, core_chars))
        findings += _lint_mod.lint_constant_budget(
            worldbooks, wr_constant_chars,
            int(getattr(plugin.config, "max_prompt_length", 0) or 0),
            persona_bindings=persona_bindings)

        summary: dict = {}
        for f in findings:
            summary[f["check"]] = summary.get(f["check"], 0) + 1
        return ok({"findings": findings, "summary": summary})
    except Exception as e:
        return err(error_text("体检失败", e))
