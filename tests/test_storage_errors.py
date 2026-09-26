# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""统一异常体系 / 存储失败可观测 / 激活 fail-close 测试（v5.3.0 M3.2，D4 + D4b）。

三层覆盖
--------
1. ``quill/core/errors.py``        —— QuillError 族类层次、message/detail/context
   结构、to_payload 不含 detail、``__str__`` 携带 detail（面板 error_text
   信封无需改动即可同时带上中文摘要与异常类型）；
2. D4（六类高频路径）              —— add / search / prune / delete / backup /
   restore 各至少一条完整链：**底层抛 StorageError → 调用方按既有 except
   降级 → 用户可见行为不变 → 失败计数 +1**（quill/core/storage_stats，
   进程内线程安全计数，/info ``storage_errors`` 字段的数据源）；
3. D4b（激活判定 fail-close）      —— 检测异常 → False（红→绿：改前
   ``_check_activation`` 无 try，异常直接上抛 H3 顶层）；加载失败 → 激活词
   通道降级不命中（load_ok=False），括号通道不受影响；正常激活/未激活回归。

行为红线对照（M3.2）：所有降级断言都钉「降级形态不变」——该返回空的返回空、
该 err 信封的还是 err 信封、该放行的放行；差异仅限日志更详细与失败可计数。

双模式（纯 stub / ASTRBOT_APP 真机）均可运行；响应解包沿用
tests/test_upload_channel.py 的 ``_unwrap`` 手法。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_quillplus.quill.core.errors import (
    ConflictError,
    DependencyMissingError,
    MigrationError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    QuillError,
    StorageError,
    UnsupportedFormatError,
    ValidationError,
)
from astrbot_plugin_quillplus.quill.core.storage_stats import (
    CATEGORIES,
    note_storage_error,
    reset_storage_error_counts,
    storage_error_snapshot,
)
from astrbot_plugin_quillplus.quill_rag.memory_store import MemoryStore
from astrbot_plugin_quillplus.quill_rag.vector_store import FaissVectorStore
from astrbot_plugin_quillplus.kb import WritingResourceManager
from astrbot_plugin_quillplus.quill_rag.retrieval import QuillRetriever, rag_ok
from astrbot_plugin_quillplus import _route_core
from astrbot_plugin_quillplus import web_routes as web_routes_module
from astrbot_plugin_quillplus.web_routes import QuillRoutes
from astrbot_plugin_quillplus.activation import ActivationDetector
from astrbot_plugin_quillplus import main as M


# ── 公共假件 ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_counters():
    """每个用例从零计数（生产进程不重置，仅测试隔离用）。"""
    reset_storage_error_counts()
    yield


def _unwrap(resp):
    """双模式取 (负载, 状态码)，见 tests/test_upload_channel.py 同名函数。"""
    if isinstance(resp, dict):
        if set(resp) == {"json"}:
            return resp["json"], 200
        return resp, resp.get("status_code", 400)
    return json.loads(resp.body), getattr(resp, "status_code", 200)


class _BrokenConn:
    """让 kb.add_entry/delete_entry 走到底层异常的最小假连接。

    execute 必须是**同步抛**（aiosqlite 的 ``async with conn.execute(...)``
    用法要求调用即失败才可被 except 捕获，async def 会先返回协程对象）。
    """

    def __init__(self, exc: Exception):
        self._exc = exc

    def execute(self, *a, **k):
        raise self._exc


class _FakeFaissIndex:
    """search 必炸 / add 必炸的假 FAISS 索引（避免 CI 依赖真 faiss）。"""

    def __init__(self, ntotal: int = 10, exc: Exception | None = None):
        self.ntotal = ntotal
        self._exc = exc or RuntimeError("injected faiss failure")

    def search(self, *a, **k):
        raise self._exc

    def add_with_ids(self, *a, **k):
        raise self._exc


async def _make_memory_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "mem_test.db"))
    await store.initialize()
    return store


def _make_detector(tmp_path: Path, words=True) -> ActivationDetector:
    import os

    yaml_path = tmp_path / "activation_triggers.yaml"
    if words:
        yaml_path.write_text(
            "activation_words:\n  - 插入\n  - 测试\nexact_match_words:\n  - love\n",
            encoding="utf-8",
        )
    return ActivationDetector(str(yaml_path))


# ── 1. QuillError 族 ─────────────────────────────────────────────


def test_quill_error_family_hierarchy():
    """单一基类：全部受控异常可被 ``except QuillError`` 兜住。"""
    for cls in (
        ValidationError,
        NotFoundError,
        ConflictError,
        PermissionDeniedError,
        DependencyMissingError,
        StorageError,
        MigrationError,
        ProviderError,
        UnsupportedFormatError,
    ):
        assert issubclass(cls, QuillError)
    assert issubclass(UnsupportedFormatError, ValidationError)
    assert issubclass(StorageError, QuillError)


def test_quill_error_attributes_and_str():
    exc = StorageError("写入记忆失败", detail="no such table", context={"session_id": "s1"})
    assert exc.message == "写入记忆失败"
    assert exc.detail == "no such table"
    assert exc.context == {"session_id": "s1"}
    assert exc.status_code == 500
    assert exc.code == "storage_error"
    # __str__ 携带 detail：面板 error_text(prefix, e) 无需改动即可见摘要+类型
    assert "写入记忆失败" in str(exc) and "no such table" in str(exc)
    plain = ValidationError("参数不合法")
    assert str(plain) == "参数不合法"
    assert plain.status_code == 400 and plain.code == "validation_error"


def test_quill_error_to_payload_hides_detail():
    exc = StorageError("删除失败", detail="C:/secret/path.db locked")
    payload = exc.to_payload()
    assert payload == {"status": "error", "code": "storage_error", "message": "删除失败"}
    assert "C:/secret" not in json.dumps(payload)


def test_storage_error_exception_chain():
    """D4 统一写法：底层异常经 ``from exc`` 可追溯。"""
    cause = sqlite3.OperationalError("disk I/O error")
    try:
        try:
            raise cause
        except Exception as e:
            raise StorageError("写入记忆失败", detail=str(e)) from e
    except StorageError as exc:
        assert exc.__cause__ is cause


# ── 2. storage_stats 计数器 ──────────────────────────────────────


def test_storage_stats_categories_complete():
    assert CATEGORIES == ("add", "search", "prune", "delete", "backup", "restore")


def test_storage_stats_note_and_snapshot():
    assert storage_error_snapshot() == {c: 0 for c in CATEGORIES}
    note_storage_error("add")
    note_storage_error("add")
    note_storage_error("search")
    snap = storage_error_snapshot()
    assert snap["add"] == 2 and snap["search"] == 1
    # 快照是拷贝：改它不影响内部计数
    snap["add"] = 999
    assert storage_error_snapshot()["add"] == 2


def test_storage_stats_unknown_category_rejected():
    with pytest.raises(ValueError):
        note_storage_error("retrive")  # 拼错类别名必须在测试期暴露


def test_storage_stats_thread_safety():
    def _bump(n):
        for _ in range(n):
            note_storage_error("delete")

    threads = [threading.Thread(target=_bump, args=(200,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert storage_error_snapshot()["delete"] == 1600


# ── 3. D4 六类路径 ───────────────────────────────────────────────
# 每类至少一条完整链：底层抛 StorageError → 调用方降级 → 用户可见行为
# 不变 → 失败计数 +1。


# ── 3.1 add ──────────────────────────────────────────────────────


async def test_d4_add_memory_store_raises_and_counts(tmp_path, monkeypatch):
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_exec_write", _boom)
    before = storage_error_snapshot()["add"]
    with pytest.raises(StorageError) as ei:
        await store.add("s1", "记忆", [0.1] * 8)
    assert ei.value.__cause__ is not None          # 异常链保留
    assert storage_error_snapshot()["add"] == before + 1
    await store.close()


async def test_d4_add_chat_log_caller_degrades_silently(tmp_path, monkeypatch):
    """聊天链路降级保持：log_message 失败（经 log_chat_message 吞掉）不连累
    本轮对话——用户可见行为与改前一致（消息照发），只是计数 +1。"""
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_exec_write", _boom)
    retriever = QuillRetriever(embedding_provider=None, memory_store=store, enable_memory=True)
    before = storage_error_snapshot()["add"]
    await retriever.log_chat_message("s1", "user", "你好")   # 不抛
    assert storage_error_snapshot()["add"] == before + 1
    await store.close()


# ── 3.2 search ───────────────────────────────────────────────────


async def test_d4_search_vector_store_raises_and_counts(tmp_path):
    store = FaissVectorStore(str(tmp_path / "v.db"), str(tmp_path / "idx"))
    store._index = _FakeFaissIndex()               # 绕开 faiss 依赖，直接注入故障
    store.dim = 8                                  # M3.3 F1：注入索引需声明维度，过 dim 门
    before = storage_error_snapshot()["search"]
    with pytest.raises(StorageError):
        await store.search([0.1] * 8, top_k=3)
    assert storage_error_snapshot()["search"] == before + 1


async def test_d4_search_memory_search_all_raises_and_counts(tmp_path, monkeypatch):
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(store, "_exec_fetchall", _boom)
    before = storage_error_snapshot()["search"]
    with pytest.raises(StorageError):
        await store.search_all([0.1] * 8, top_k=3)
    assert storage_error_snapshot()["search"] == before + 1
    await store.close()


async def test_d4_search_caller_degrades_to_rag_failed(tmp_path):
    """检索调用方降级保持：search_documents 返回带失败标记的**空列表**——
    注入侧拿到的仍是空结果（该返回空返回空），健康度可记失败。"""
    store = FaissVectorStore(str(tmp_path / "v.db"), str(tmp_path / "idx"))
    store._index = _FakeFaissIndex()
    store.dim = 8                                  # M3.3 F1：注入索引需声明维度，过 dim 门

    class _Embedding:
        async def embed(self, texts):
            return [[0.1] * 8 for _ in texts]

    retriever = QuillRetriever(embedding_provider=_Embedding(), vector_store=store, top_k=3)
    results = await retriever.search_documents("查询")
    assert results == []
    assert rag_ok(results) is False                # 失败可区分于「真没找到」
    assert storage_error_snapshot()["search"] == 1


# ── 3.3 prune ────────────────────────────────────────────────────


async def test_d4_prune_memory_store_raises_and_counts(tmp_path, monkeypatch):
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_exec_write", _boom)
    before = storage_error_snapshot()["prune"]
    with pytest.raises(StorageError):
        await store.prune_memories()
    assert storage_error_snapshot()["prune"] == before + 1
    await store.close()


async def test_d4_prune_caller_degrades_in_reflection_schedule(tmp_path, monkeypatch):
    """反思调度降级保持：spawn 出的 prune 任务失败只进日志回调，
    schedule_reflection 本身不抛（H5 聊天主流程不受影响）。"""
    from astrbot_plugin_quillplus.quill.services.memory import schedule_reflection

    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_exec_write", _boom)

    tasks: list = []

    def _spawn(coro):
        t = asyncio.create_task(coro)
        tasks.append(t)
        return t

    state_manager = SimpleNamespace(
        increment_unsummarized_turns=_mk_async(5),
        reset_unsummarized_turns=_mk_async(None),
    )
    retriever = SimpleNamespace(
        memory_store=store,
        summarize_contexts=_mk_async("摘要"),
        get_recent_chat_logs=None,
    )
    # get_recent_chat_logs 由 memory_store 提供（真实 store，返回空列表即可）
    retriever = SimpleNamespace(memory_store=store, summarize_contexts=_mk_async("摘要"))
    config = SimpleNamespace(rag_chat_log_retention_days=30)

    before = storage_error_snapshot()["prune"]
    await schedule_reflection(state_manager, retriever, _spawn, config, "t1", "s1")
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert any(isinstance(r, StorageError) for r in results)   # 任务失败被看见
    # prune_memories 与 cleanup_chat_logs 同属 prune 类别且都失败（同一故障注入）
    assert storage_error_snapshot()["prune"] == before + 2
    await store.close()


def _mk_async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ── 3.4 delete ───────────────────────────────────────────────────


async def test_d4_delete_memory_store_raises_and_counts(tmp_path, monkeypatch):
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_exec_write", _boom)
    before = storage_error_snapshot()["delete"]
    with pytest.raises(StorageError):
        await store.delete_session_memories("s1")
    with pytest.raises(StorageError):
        await store.delete_session_chat_logs("s1")
    assert storage_error_snapshot()["delete"] == before + 2
    await store.close()


async def test_d4_delete_panel_envelope_shape_unchanged(tmp_path, monkeypatch):
    """面板降级保持：/memory/delete 失败仍走既有 err 信封（前缀「删除失败」），
    与改前 except Exception 分支形态一致；失败计数 +1。"""
    store = await _make_memory_store(tmp_path)

    async def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_exec_write", _boom)
    before = storage_error_snapshot()["delete"]
    resp = await _route_core.handle_memory_delete(store, memory_id=1)
    body, status = _unwrap_resp(resp)
    assert status == 200 and body["status"] == "error"
    assert "删除失败" in body["message"]
    assert storage_error_snapshot()["delete"] == before + 1
    await store.close()


def _unwrap_resp(resp):
    """_route_core 返回纯 dict（无 HTTP 依赖），直接断言。"""
    assert isinstance(resp, dict)
    return resp, 200


# ── 3.5 backup ───────────────────────────────────────────────────


async def test_d4_backup_raises_and_counts(tmp_path, monkeypatch):
    routes = QuillRoutes(
        None, None, None,
        plugin=SimpleNamespace(paths={"data_root": str(tmp_path), "legacy": False}),
    )

    def _boom(sources, buf):
        raise OSError("disk full")

    monkeypatch.setattr(web_routes_module, "build_backup_zip", _boom)
    before = storage_error_snapshot()["backup"]
    with pytest.raises(StorageError):
        await routes._build_backup_zip()
    assert storage_error_snapshot()["backup"] == before + 1


async def test_d4_backup_route_degrades_to_500_envelope(tmp_path, monkeypatch):
    """面板降级保持：导出失败经既有 @_api_handler 转 500 错误信封（改前未捕获
    异常同样落此分支），失败可计数、日志带异常链。"""
    routes = QuillRoutes(
        None, None, None,
        plugin=SimpleNamespace(paths={"data_root": str(tmp_path), "legacy": False}),
    )

    def _boom(sources, buf):
        raise OSError("disk full")

    monkeypatch.setattr(web_routes_module, "build_backup_zip", _boom)
    resp = await routes.backup_export()
    body, status = _unwrap(resp)
    assert status == 500
    assert body.get("status") == "error" or body.get("message")
    assert storage_error_snapshot()["backup"] == 1


# ── 3.6 restore ──────────────────────────────────────────────────


def _make_backup_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("quill_state.json", json.dumps({"ok": True}))
    return buf.getvalue()


async def test_d4_restore_reload_failure_counts_and_message_unchanged(tmp_path, monkeypatch):
    """恢复链路降级保持：组件重建失败 → 响应消息含「组件热重载失败」
    （改前同文案）且 reload_ok=False，失败计数 +1。"""
    calls: list = []

    async def _prepare():
        calls.append("prepare")

    async def _reload():
        raise RuntimeError("rebuild failed")

    plugin = SimpleNamespace(
        paths={"data_root": str(tmp_path), "legacy": False},
        _prepare_for_restore=_prepare,
        _reload_after_restore=_reload,
    )
    routes = QuillRoutes(None, None, None, plugin=plugin)
    before = storage_error_snapshot()["restore"]
    resp = await routes._do_restore_bytes(_make_backup_zip())
    body, status = _unwrap(resp)
    data = body["data"]
    assert status == 200
    assert data["extracted"] == 1 and data["reload_ok"] is False
    assert "组件热重载失败" in data["message"]
    assert calls == ["prepare"]                    # 前置步骤正常执行
    assert storage_error_snapshot()["restore"] == before + 1


async def test_d4_restore_prepare_failure_counts(tmp_path):
    async def _prepare():
        raise RuntimeError("close failed")

    plugin = SimpleNamespace(
        paths={"data_root": str(tmp_path), "legacy": False},
        _prepare_for_restore=_prepare,
        _reload_after_restore=_mk_async(None),
    )
    routes = QuillRoutes(None, None, None, plugin=plugin)
    before = storage_error_snapshot()["restore"]
    resp = await routes._do_restore_bytes(_make_backup_zip())
    body, _ = _unwrap(resp)
    assert body["data"]["extracted"] == 1          # 恢复照常完成（降级保持）
    assert storage_error_snapshot()["restore"] == before + 1


# ── 3.7 /info storage_errors 字段 ────────────────────────────────


async def test_info_exposes_storage_errors_and_stays_backward_compatible():
    note_storage_error("backup", RuntimeError("x"))
    resp = await _route_core.handle_info(None, None)
    assert resp["status"] == "ok"
    data = resp["data"]
    # 新字段
    assert data["storage_errors"]["backup"] == 1
    assert set(data["storage_errors"]) == set(CATEGORIES)
    # 既有字段一个不少（只增不改）
    for key in ("wr_count", "wb_count", "persona_count", "categories", "version",
                "available_worldbooks", "trigger_log", "health", "wr_index"):
        assert key in data


# ── 4. D4b 激活判定 fail-close ───────────────────────────────────


class _RaisingDetector:
    """模拟检测器内部异常（load 失败之外的运行期故障）。"""

    def should_activate(self, message):
        raise RuntimeError("injected detector failure")

    def check_brackets(self, message):
        raise RuntimeError("injected detector failure")


def _mk_activation_host(detector) -> M.QuillPlugin:
    """轻量宿主（同 test_hook_snapshots 手法）：object.__new__ + setattr。"""
    host = object.__new__(M.QuillPlugin)
    host.activation_detector = detector
    host.wr_manager = None
    return host


async def test_d4b_check_activation_exception_returns_false():
    """红→绿：检测异常 → (False, False)（不注入）。

    改前 `_check_activation` 无 try，异常原样上抛 H3 顶层（靠顶层降级兜底，
    无 fail-close 契约、无专门日志）；本用例在改前必然以 RuntimeError 失败。
    """
    host = _mk_activation_host(_RaisingDetector())
    activated, wr_activated = await M.QuillPlugin._check_activation(host, "你好", "", None)
    assert activated is False
    assert wr_activated is False


async def test_d4b_detector_load_failure_degrades_word_channel(tmp_path):
    """加载失败 → load_ok=False，激活词通道不命中（不注入），括号通道照常。"""
    import os

    missing = ActivationDetector(str(tmp_path / "no_such.yaml"))
    assert missing.load_ok is False
    assert missing.should_activate("请插入剧情") is False

    corrupt = tmp_path / "broken.yaml"
    corrupt.write_text("activation_words: [ {unclosed", encoding="utf-8")
    det = ActivationDetector(str(corrupt))
    assert det.load_ok is False
    assert det.should_activate("请插入剧情") is False
    assert det.check_brackets("【继续】") is True   # 括号通道不依赖触发词文件


async def test_d4b_detector_runtime_exception_returns_false(tmp_path):
    """检测过程异常（非加载期）→ False（fail-close 契约在检测器内部兜底）。"""
    det = _make_detector(tmp_path)
    monkey_target = det
    orig_lower = str.lower

    def _boom():
        raise RuntimeError("injected")

    # 直接触发内部 except：让 lower() 抛异常的对象
    class _Weird(str):
        def lower(self):
            raise RuntimeError("injected")

    assert det.should_activate(_Weird("插入")) is False
    assert det.should_activate("请插入") is True    # 正常词命中不受影响


async def test_d4b_normal_activation_paths_regression(tmp_path):
    """正常判定逻辑回归：激活词 / 精确词 / 括号 / 未激活，与 v5.2.5 一致。"""
    det = _make_detector(tmp_path)
    assert det.load_ok is True
    host = _mk_activation_host(det)

    activated, wr_activated = await M.QuillPlugin._check_activation(host, "请插入剧情", "", None)
    assert (activated, wr_activated) == (True, False)

    activated, wr_activated = await M.QuillPlugin._check_activation(host, "I love you", "", None)
    assert (activated, wr_activated) == (True, False)

    activated, wr_activated = await M.QuillPlugin._check_activation(host, "【继续】", "", None)
    assert (activated, wr_activated) == (False, False)   # 括号在 hooks 步 9 后直读

    activated, wr_activated = await M.QuillPlugin._check_activation(host, "今天天气不错", "", None)
    assert (activated, wr_activated) == (False, False)


async def test_d4b_wr_disabled_skips_match(tmp_path):
    """WR 模式 disabled：不发起素材检索（正常路径逐字保持）。"""
    det = _make_detector(tmp_path)
    host = _mk_activation_host(det)

    match_calls: list = []

    class _WR:
        async def match(self, text, top_k=3, log_match=False):
            match_calls.append(text)
            return []

    host.wr_manager = _WR()
    persona = {"quill_extensions": {"wr_mode": "disabled"}}
    activated, wr_activated = await M.QuillPlugin._check_activation(
        host, "你好", "上下文", persona
    )
    assert (activated, wr_activated) == (False, False)
    assert match_calls == []


# ── 5. kb（WritingResourceManager）add / delete 直测 ─────────────


def _mk_wr_broken(exc: Exception) -> WritingResourceManager:
    wr = WritingResourceManager(":memory:")
    wr._conn = _BrokenConn(exc)
    return wr


async def test_d4_kb_add_entry_raises_storage_error():
    wr = _mk_wr_broken(sqlite3.OperationalError("no such table"))
    before = storage_error_snapshot()["add"]
    with pytest.raises(StorageError):
        await wr.add_entry(category="场景", entry_id="e1", keywords=["雨"], content="文本")
    assert storage_error_snapshot()["add"] == before + 1


async def test_d4_kb_add_entry_duplicate_still_false():
    """IntegrityError（entry_id 重复）不是存储失败：维持返回 False 的既有语义。"""
    import aiosqlite

    wr = _mk_wr_broken(aiosqlite.IntegrityError("UNIQUE constraint failed"))
    assert await wr.add_entry(category="场景", entry_id="e1", keywords=[], content="文本") is False
    assert storage_error_snapshot()["add"] == 0


async def test_d4_kb_delete_entry_raises_and_panel_envelope():
    wr = _mk_wr_broken(sqlite3.OperationalError("no such table"))
    before = storage_error_snapshot()["delete"]
    with pytest.raises(StorageError):
        await wr.delete_entry("e1")
    assert storage_error_snapshot()["delete"] == before + 1

    resp = await _route_core.handle_wr_delete(wr, entry_id="e1")
    assert resp["status"] == "error"
    assert "删除条目失败" in resp["message"]


async def test_d4_kb_match_fallback_scan_raises():
    """match 兜底扫描失败 → StorageError（search 类别）；
    _check_activation 的既有 except 降级为 wr_activated=False。"""
    wr = _mk_wr_broken(sqlite3.OperationalError("no such table"))
    before = storage_error_snapshot()["search"]
    with pytest.raises(StorageError):
        await wr.match("雨中的街道", top_k=3, log_match=False)
    assert storage_error_snapshot()["search"] == before + 1


async def test_d4_kb_wr_create_route_degrades():
    """面板降级保持：wr/create 存储失败仍走 err 信封（改前被吞成
    「创建条目失败（ID 可能已存在）」的误导文案，现 detail 可见真实原因）。"""
    wr = _mk_wr_broken(sqlite3.OperationalError("no such table"))
    resp = await _route_core.handle_wr_create(
        wr,
        {"category": "场景", "entry_id": "e1", "keywords": ["雨"], "content": "文本"},
    )
    assert resp["status"] == "error"
    assert "创建条目失败" in resp["message"]
    assert storage_error_snapshot()["add"] == 1
