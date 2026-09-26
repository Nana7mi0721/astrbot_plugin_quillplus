# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FAISS 维度自适应回归测试（v5.3.0 M3.3 F1）。

覆盖（对应 PLAN M3.3 + M3.3.5 的存储层修复）：
1. 维度由**首批真实向量**决定——provider 报告的维度只是提示，不再用于建索引
   （改前启动期 get_dim() 硬编码 512，真机 SiliconFlow/bge-m3 实测 1024 维，
   索引建错维度、上传全被拒）；
2. 旧版无文件头索引的兼容读取（按旧规则 faiss 反序列化推断维度）；
3. QPVI 文件头（魔数 + 版本号 + 维度冗余位）round-trip、未知版本优雅降级；
4. 维度不匹配触发重建：丢旧索引文件、建空索引、复位孤儿 faiss_id，
   **不抛错、不重烧 API 配额**（文本仍在 chunks 表，随上传回填）。

需要真 faiss 的用例经 ``real_faiss`` fixture 跳过守卫（纯 stub CI 环境装了
faiss-cpu 会真跑；本机无 faiss 的解释器自动 skip）。embedding / 响应形状
用例不依赖 faiss，全环境必跑。
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from astrbot_plugin_quillplus import _route_core
from astrbot_plugin_quillplus.quill_rag.vector_store import (
    FaissVectorStore,
    INDEX_FORMAT_VERSION,
    INDEX_MAGIC,
    _HEADER_STRUCT,
)


_opened: list = []


@pytest.fixture(autouse=True)
async def _close_opened_stores():
    """收尾关闭本用例打开的全部 aiosqlite 连接，避免事件循环关闭时
    连接工作线程报 RuntimeError（PytestUnhandledThreadExceptionWarning）。"""
    _opened.clear()
    yield
    for s in _opened:
        try:
            await s.close()
        except Exception:
            pass
    _opened.clear()


@pytest.fixture
def real_faiss():
    """需要真 faiss 的用例用；环境无 faiss 时 skip（不算失败）。"""
    return pytest.importorskip("faiss")


def _write_legacy_index(path, dim: int, n: int = 2):
    """用**旧版方式**（faiss.write_index，无自定义文件头）造一个索引文件。"""
    import faiss

    base = faiss.IndexFlatIP(dim)
    idx = faiss.IndexIDMap(base)
    vecs = np.ones((n, dim), dtype="float32")
    faiss.normalize_L2(vecs)
    idx.add_with_ids(vecs, np.array(list(range(1, n + 1)), dtype="int64"))
    faiss.write_index(idx, str(path))


def _make_store(tmp_path, name="v.db", index="quill_rag.index", **kwargs) -> FaissVectorStore:
    store = FaissVectorStore(str(tmp_path / name), str(tmp_path / index), **kwargs)
    _opened.append(store)
    return store


# ── 1. 维度由首批真实向量决定 ────────────────────────────────────


async def test_no_premature_index_without_file(real_faiss, tmp_path):
    """F1 核心：启动期无索引文件时不再按推断维度预建（dim=0 待定）。"""
    store = _make_store(tmp_path)
    await store.initialize()
    assert store._index is None
    assert store.dim == 0


async def test_provider_hint_does_not_create_premature_index(real_faiss, tmp_path):
    """provider 报 1024（如 bge-m3）也不预建 1024 维索引——等首批向量。"""
    store = _make_store(
        tmp_path, embedding_provider=SimpleNamespace(get_dim=lambda: 1024)
    )
    await store.initialize()
    assert store._index is None
    assert store.dim == 0


async def test_dim_decided_by_first_batch_even_against_hint(real_faiss, tmp_path):
    """首批 8 维真实向量决定维度；provider 提示值不一致时仅告警，以向量为准。"""
    store = _make_store(
        tmp_path, embedding_provider=SimpleNamespace(get_dim=lambda: 1024)
    )
    await store.initialize()
    n = await store.add(["a", "b"], [[0.1] * 8, [0.2] * 8], "docA")
    assert n == 2
    assert store.dim == 8
    assert store._index is not None and store._index.d == 8
    res = await store.search([0.1] * 8, top_k=3)
    assert [r["source"] for r in res] == ["docA", "docA"]


async def test_batch_internal_mixed_dims_rejected(real_faiss, tmp_path):
    """批内维度不一致是调用方缺陷：直接拒绝，不建索引。"""
    store = _make_store(tmp_path)
    await store.initialize()
    with pytest.raises(ValueError):
        await store.add(["a", "b"], [[0.1] * 4, [0.1] * 8], "docA")
    assert store._index is None
    assert store.dim == 0


# ── 2. 旧索引兼容读取 ────────────────────────────────────────────


async def test_legacy_headerless_index_loads_and_upgrades_on_save(real_faiss, tmp_path):
    """旧版无头索引：按旧规则读出、维度推断正确；首次保存自动升级 QPVI 格式。"""
    idx_path = tmp_path / "quill_rag.index"
    await asyncio.to_thread(_write_legacy_index, idx_path, 4)
    store = _make_store(tmp_path)
    await store.initialize()
    assert store.dim == 4
    assert store._index is not None and store._index.ntotal == 2

    n = await store.add(["x"], [[0.5] * 4], "docC")
    assert n == 1                                   # 同维度写入成功
    assert idx_path.read_bytes()[:4] == INDEX_MAGIC  # 保存后升级为新格式

    store2 = _make_store(tmp_path)
    await store2.initialize()
    assert store2.dim == 4                          # 新格式仍可读


# ── 3. 文件头 round-trip / 版本守卫 ─────────────────────────────


async def test_index_header_roundtrip(real_faiss, tmp_path):
    store = _make_store(tmp_path)
    await store.initialize()
    await store.add(["a"], [[0.1] * 6], "docA")
    blob = (tmp_path / "quill_rag.index").read_bytes()
    magic, version, header_dim = _HEADER_STRUCT.unpack(blob[: _HEADER_STRUCT.size])
    assert magic == INDEX_MAGIC
    assert version == INDEX_FORMAT_VERSION
    assert header_dim == store.dim == 6


async def test_unknown_version_and_truncated_file_fail_gracefully(real_faiss, tmp_path):
    """未知版本 / 截断文件：加载不抛错、索引置空、坏文件移除，等待首批向量重建。"""
    idx_path = tmp_path / "quill_rag.index"
    # 未知版本号
    idx_path.write_bytes(
        INDEX_MAGIC + struct.pack("<II", 99, 4) + b"\x00" * 16
    )
    store = _make_store(tmp_path)
    await store.initialize()                       # 不抛
    assert store._index is None and store.dim == 0
    assert not idx_path.exists()                   # 损坏文件已移除
    # 随后首批向量正常重建
    assert await store.add(["x"], [[0.1] * 8], "d") == 1
    assert store.dim == 8

    # 截断文件（只有魔数，不足一个头）
    idx_path.write_bytes(INDEX_MAGIC)
    store2 = _make_store(tmp_path)
    await store2.initialize()                      # 不抛
    assert store2._index is None and store2.dim == 0


# ── 4. 维度不匹配 → 重建（不抛错、不重嵌入）─────────────────────


async def test_dim_change_on_add_rebuilds_and_preserves_text(real_faiss, tmp_path):
    """add 时发现维度变化：丢旧索引、按新维度建空索引、复位孤儿 faiss_id，
    本批照常写入；文本全部保留。"""
    store = _make_store(tmp_path)
    await store.initialize()
    assert await store.add(["a", "b"], [[0.1] * 4, [0.2] * 4], "docA") == 2

    n = await store.add(["c"], [[0.3] * 8], "docB")   # 维度 4 → 8
    assert n == 1
    assert store.dim == 8 and store._index.d == 8

    rows = await store._exec_fetchall("SELECT id, faiss_id, source FROM chunks ORDER BY id")
    assert [r[2] for r in rows] == ["docA", "docA", "docB"]   # 文本一个不少
    assert rows[0][1] == -1 and rows[1][1] == -1               # 旧向量孤儿复位
    assert rows[2][1] == rows[2][0]                            # 新行 faiss_id 回填

    stats = await store.get_stats()
    assert stats["dim"] == 8
    assert stats["last_rebuild"] == {"reason": "dim_change", "from": 4, "to": 8}
    assert stats["dim_mismatch"] is False

    res = await store.search([0.3] * 8, top_k=5)
    assert [r["source"] for r in res] == ["docB"]              # 新维度可检索


async def test_load_time_expected_dim_mismatch_rebuilds(real_faiss, tmp_path):
    """加载期 provider 维度已知且与索引不一致 → 立即重建空索引 + 复位 faiss_id。"""
    store1 = _make_store(tmp_path)
    await store1.initialize()
    assert await store1.add(["a", "b"], [[0.1] * 4, [0.2] * 4], "docA") == 2

    store2 = _make_store(
        tmp_path, embedding_provider=SimpleNamespace(get_dim=lambda: 8),
    )
    await store2.initialize()                     # 4 维索引 vs provider 8 维 → 重建
    assert store2.dim == 8 and store2._index.d == 8
    row = await store2._exec_fetchone("SELECT COUNT(*) FROM chunks WHERE faiss_id >= 0")
    assert row[0] == 0                            # 孤儿 faiss_id 复位
    row = await store2._exec_fetchone("SELECT COUNT(*) FROM chunks")
    assert row[0] == 2                            # 文本保留
    assert store2._last_rebuild == {"reason": "load_dim_mismatch", "from": 4, "to": 8}
    # 重建后本批新维度写入正常
    assert await store2.add(["c"], [[0.3] * 8], "docB") == 1


async def test_search_dim_mismatch_degrades_without_error(real_faiss, tmp_path):
    """查询维度与索引不一致：返回空（不抛 StorageError），面板 dim_mismatch 可见；
    同维度检索不受影响。"""
    store = _make_store(tmp_path)
    await store.initialize()
    assert await store.add(["a"], [[0.1] * 4], "docA") == 1

    res = await store.search([0.1] * 8, top_k=3)   # 8 维查询 vs 4 维索引
    assert res == []
    stats = await store.get_stats()
    assert stats["dim_mismatch"] is True

    res2 = await store.search([0.1] * 4, top_k=3)  # 同维度照常
    assert len(res2) == 1


# ── 5. embedding get_dim 去硬编码 ───────────────────────────────


def test_embedding_get_dim_unknown_is_zero_not_512():
    from astrbot_plugin_quillplus.quill_rag.embedding import QuillEmbeddingProvider

    emb = QuillEmbeddingProvider(context=None, provider_id="", enable_local=False)
    assert emb.get_dim() == 0          # 改前返回硬编码 512
    assert emb.get_status()["dim"] == 0


def test_embedding_get_dim_uses_provider_dim_when_available():
    from astrbot_plugin_quillplus.quill_rag.embedding import QuillEmbeddingProvider

    provider = SimpleNamespace(get_dim=lambda: 1024)
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: provider)
    emb = QuillEmbeddingProvider(context=ctx, provider_id="siliconflow")
    assert emb.get_dim() == 1024


# ── 6. 面板可见性（/rag/documents 响应新增 index_status）────────


async def test_rag_documents_response_includes_index_status(tmp_path):
    """handle_rag_documents 只增字段：index_status（dim/dim_mismatch/last_rebuild）。"""
    store = _make_store(tmp_path)
    await store.initialize()
    store._index = SimpleNamespace(ntotal=7)   # 无 faiss 环境用假索引注入
    store.dim = 8
    resp = await _route_core.handle_rag_documents(store)
    assert resp["status"] == "ok"
    assert resp["data"]["index_status"]["dim"] == 8
    assert resp["data"]["index_status"]["faiss_vectors"] == 7
    assert resp["data"]["index_status"]["dim_mismatch"] is False
    assert resp["data"]["documents"] == []
