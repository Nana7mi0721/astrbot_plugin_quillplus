# -*- coding: utf-8 -*-
"""FaissVectorStore — FAISS 向量存储 + SQLite 元数据。仅用于 Doc RAG（全局文档检索）。

索引维度自适应（M3.3 F1）
------------------------
索引维度由**事实**决定，不由**推断**决定（自重构参考版
``infrastructure/vector/faiss_index.py`` 摘取改造）：

1. 磁盘上已有索引 → 维度 = 索引文件的维度；
2. 磁盘上没有索引 → 不预建（旧版在这里按 ``embedding_provider.get_dim()``
   的推断值建索引，而 embedding 未跑过时该值是硬编码 512 的猜测——真机
   SiliconFlow/bge-m3 实测 1024 维，索引维度错、上传全被维度校验拒绝），
   延迟到 :meth:`add` 用**首批真实向量**的维度建索引；
3. 维度不匹配（embedding 切换）→ 重建空索引 + 复位 SQLite 孤儿 faiss_id，
   文本仍在 chunks 表，后续 add 慢慢回填。**不自动重烧 API 配额**重嵌入。

持久化格式：自定义文件头（魔数 + 版本号 + 维度）+ faiss 序列化负载，见
:meth:`_save_index`；旧版无头文件按旧规则兼容读取，首次保存自动升级。
"""

from __future__ import annotations

import os
import struct
import asyncio
import aiosqlite

import numpy as np

from astrbot.api import logger

try:
    from .._schema import prepare_database, finish_database
    from ..quill.core.errors import StorageError
    from ..quill.core.locks import ReentrantLock
    from ..quill.core.storage_stats import note_storage_error
except ImportError:  # 直接运行本文件时无父包
    from _schema import prepare_database, finish_database
    from quill.core.errors import StorageError
    from quill.core.locks import ReentrantLock
    from quill.core.storage_stats import note_storage_error

# ── 索引文件头（v1）─────────────────────────────────────────────
# 布局：魔数(4) + 版本号(4, LE uint32) + 维度(4, LE uint32) + faiss 序列化负载。
# 旧版文件是纯 faiss.write_index 产物，头 4 字节是 faiss 自身的 fourcc
# （"Ix…" 开头），不可能等于 QPVI —— 以此区分有无文件头。
INDEX_MAGIC = b"QPVI"
INDEX_FORMAT_VERSION = 1
_HEADER_STRUCT = struct.Struct("<4sII")


class FaissVectorStore:
    """FAISS 向量存储 + SQLite 元数据。

    仅用于 Doc RAG（全局文档检索），动态记忆使用 MemoryStore。
    """

    def __init__(self, db_path: str, index_path: str, dim: int = 0, embedding_provider=None):
        """初始化向量存储。

        Args:
            dim: 维度提示。仅当**无 embedding_provider 且调用方显式传入**时
                参与"加载即重建"判定；默认 0（未知）。F1 之后维度一律以
                磁盘索引 / 首批真实向量为准，这里的值只是提示。
            embedding_provider: 用于在 ``get_dim()`` 可给出确定值时参与
                加载期维度校验（S2-10 语义保留：切换 embedding provider
                时重建索引）。
        """
        self.db_path = db_path
        self.index_path = index_path
        # 维度提示：provider 可给就取 provider 的（0 = 未知），否则用传入值。
        # 注意 get_dim() 在未成功 embed 过且 provider 不报维度时返回 0（M3.3
        # 已去除硬编码 512），因此启动期这里通常是 0 → 加载期不轻易重建。
        self.expected_dim = int(embedding_provider.get_dim()) if embedding_provider else int(dim)
        # 索引的真实维度：由已加载索引或首批真实向量决定；0 = 未知/未建。
        self.dim = 0
        self._index = None
        self._conn = None
        # 面板可观测（M3.3）：dim_mismatch = 索引维度与当前查询维度不一致
        # （等下一次上传触发重建）；last_rebuild = 最近一次重建的说明。
        self._dim_mismatch = False
        self._last_rebuild = None
        # M3.3 D5：可重入锁，SQLite 与 FAISS 操作共用（同任务重入安全）。
        self._lock = ReentrantLock()

    # F4 修复：SQLite 共享连接必须串行化，与 FAISS 共用同一把锁
    async def initialize(self):
        self._conn = await aiosqlite.connect(self.db_path, timeout=10.0)
        try:
            self.schema_status = await prepare_database(self._conn, self.db_path, "documents")
            await self._init_db()
            self.schema_status["previous_version"]=self.schema_status["version"]
            self.schema_status["version"]=await finish_database(self._conn,kind="documents")
            await self._load_index()
        except BaseException:
            await self._conn.close()
            self._conn = None
            raise

    async def _exec_write(self, sql: str, params=()) -> aiosqlite.Cursor:
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cur

    async def _exec_fetchall(self, sql: str, params=()) -> list:
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchall()

    async def _exec_fetchone(self, sql: str, params=()):
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return await cur.fetchone()

    async def _init_db(self):
        """初始化 SQLite 表（复用长连接）。"""
        async with self._lock:
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    faiss_id INTEGER DEFAULT -1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id)")
            await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
            await self._conn.commit()

    async def close(self):
        """关闭数据库连接（插件卸载时调用）。"""
        try:
            if getattr(self, '_conn', None):
                await self._conn.close()
        except Exception as e:
            logger.debug("[Quill RAG] vector_store close 失败: %s", e)

    async def _load_index(self):
        """加载 FAISS 索引（如存在）。维度判定规则见模块 docstring。

        磁盘无索引时**不预建**（F1：维度等首批真实向量决定）；有索引时以
        索引维度为准，仅当 expected_dim 已知（>0）且不一致才立即重建空索引
        （embedding 已切换的确定信号）。expected_dim 未知时，维度不一致留待
        首次 add/search 时发现——同样会触发重建/降级，不会用错维度的索引。
        """
        try:
            import faiss
        except ImportError:
            logger.warning("[Quill RAG] faiss 未安装，向量检索不可用")
            return

        if not os.path.exists(self.index_path):
            self._index=None;self.dim=0
            await self._exec_write('UPDATE chunks SET faiss_id=-1 WHERE faiss_id>=0')
            logger.info("[Quill RAG] 无既有 FAISS 索引，维度将由首批真实向量决定")
            return
        try:
            # 文件读取/反序列化为同步 IO，放入线程池避免阻塞事件循环
            index, header_dim = await asyncio.to_thread(self._read_index_file)
            loaded_dim = int(getattr(index, "d", 0) or 0)
            if header_dim is not None and header_dim != loaded_dim:
                # 冗余校验位失配：以索引真实维度为准（faiss 负载是事实）。
                logger.warning(
                    "[Quill RAG] 索引文件头维度 %d 与索引实际维度 %d 不一致，以索引为准",
                    header_dim, loaded_dim,
                )
            if self.expected_dim and loaded_dim != self.expected_dim:
                # S2-10 语义保留（provider 明确报出的维度才有资格触发）：
                # 切换了 embedding provider，维度不匹配，重建空索引。
                logger.warning(
                    f"[Quill RAG] FAISS 索引 dim={loaded_dim} 与期望 dim={self.expected_dim} 不一致，"
                    f"重建空索引（文本保留，可在面板确认重建向量，不自动重嵌入）。"
                )
                self._index = None
                await asyncio.to_thread(self._remove_index_file)
                # 清空 SQLite 中的孤儿 faiss_id（指向已失效的索引）
                await self._exec_write(
                    "UPDATE chunks SET faiss_id = -1 WHERE faiss_id >= 0"
                )
                self._create_index(self.expected_dim)
                self._dim_mismatch = False
                self._last_rebuild = {
                    "reason": "load_dim_mismatch",
                    "from": loaded_dim,
                    "to": self.expected_dim,
                }
            else:
                self._index = index
                self.dim = loaded_dim
                self._dim_mismatch = False
                logger.info(f"[Quill RAG] FAISS 索引已加载: {self.index_path} (dim={self.dim})")
        except Exception as e:
            logger.warning("[Quill RAG] _load_index FAISS 索引加载失败: %s", e, exc_info=True)
            self._index = None
            self.dim = 0
            # 损坏/不认识的索引文件是派生数据，移除后等 add() 按首批向量重建
            await asyncio.to_thread(self._remove_index_file)
            # M3：这个兜底分支删了索引文件却**没有复位 SQLite 里的 faiss_id**，
            # 而上面 dim 不一致的分支（:171）复位了。结果是所有 chunk 都留着
            # 指向一个已经不存在的索引的 id：search 里 `row_map.get(faiss_id)`
            # 全部落空、get_stats().faiss_vectors 虚高、而 add() 又以为这些
            # chunk 已在索引里（faiss_id >= 0）不再补写 —— 文档永久检索不到，
            # 只能靠删库重传。删文件与清映射必须成对出现。
            try:
                await self._exec_write(
                    "UPDATE chunks SET faiss_id = -1 WHERE faiss_id >= 0"
                )
            except Exception as ue:
                logger.warning("[Quill RAG] 复位 chunks.faiss_id 失败: %s", ue)

    def _read_index_file(self):
        """读取索引文件，返回 ``(index, header_dim_or_None)``。

        新格式（QPVI 头）：校验魔数与版本号，反序列化 faiss 负载，头内维度
        仅作冗余校验位。旧格式（无头）：头 4 字节是 faiss fourcc，按旧规则
        直接整文件反序列化（faiss.write_index 与 serialize_index 的流格式
        一致），返回 header_dim=None 由调用方按索引自身 .d 推断维度。
        """
        import faiss
        with open(self.index_path, "rb") as f:
            blob = f.read()
        if len(blob) >= _HEADER_STRUCT.size and blob[:4] == INDEX_MAGIC:
            _, version, header_dim = _HEADER_STRUCT.unpack(blob[:_HEADER_STRUCT.size])
            if version != INDEX_FORMAT_VERSION:
                raise ValueError(
                    f"不支持的索引文件版本: {version}（当前支持 {INDEX_FORMAT_VERSION}）"
                )
            payload = np.frombuffer(blob[_HEADER_STRUCT.size:], dtype=np.uint8)
            return faiss.deserialize_index(payload), int(header_dim)
        # 旧格式：无文件头（纯 faiss 序列流）——按旧规则兼容读取
        return faiss.deserialize_index(np.frombuffer(blob, dtype=np.uint8)), None

    def _remove_index_file(self):
        try:
            os.remove(self.index_path)
        except OSError as e:
            logger.debug("[Quill RAG] 删除旧索引文件失败（可忽略）: %s", e)

    def _create_index(self, dim: int):
        """创建新的 FAISS 索引（IndexFlatIP + IDMap，L2 归一化后内积即余弦）。

        dim 必须来自事实（已加载索引或首批真实向量），不接受推断值——F1
        之前这里用启动期推断的 expected_dim（往往是硬编码 512 的猜测），
        对维度不同的 provider 会建出错误维度的索引。
        """
        try:
            import faiss
            dim = int(dim)
            if dim <= 0:
                logger.warning(f"[Quill RAG] _create_index 维度未知({dim})，推迟到首批向量")
                return None
            base = faiss.IndexFlatIP(dim)
            self._index = faiss.IndexIDMap(base)
            self.dim = dim
            logger.info(f"[Quill RAG] 新建 FAISS 索引 (dim={dim})")
        except ImportError:
            # faiss 不可用：_index 保持 None，add() 走「文本入库暂不可检索」分支
            pass
        return self._index

    def _save_index(self):
        """持久化 FAISS 索引到磁盘（QPVI 文件头 + faiss 序列化负载）。

        失败必须**抛出**而非只记日志：调用方（add）据此回滚 SQLite，
        上层据此向用户报错。此前吞掉异常会导致「界面提示上传成功、
        但磁盘上没有索引，重启后数据消失」——当前进程能检索只是因为
        索引还活着在内存里。

        文件格式（v1）::

            bytes 0-3   魔数 b"QPVI"
            bytes 4-7   版本号（当前 1，little-endian uint32）
            bytes 8-11  维度（冗余校验位，读取时与索引真实 .d 对拍）
            bytes 12-   faiss.serialize_index 负载

        旧版无头文件在下次保存时自动升级为本格式（单文件原子替换，
        头与负载同生共死，不存在两文件不同步的问题）。
        """
        if self._index is None:
            return
        import faiss
        dir_path = os.path.dirname(self.index_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        # 先写临时文件再 os.replace：避免写一半崩溃留下损坏的索引文件
        # （损坏的索引会在下次 load 时整体失败，比丢失更糟）
        tmp = f"{self.index_path}.tmp"
        try:
            header = _HEADER_STRUCT.pack(INDEX_MAGIC, INDEX_FORMAT_VERSION, int(self._index.d))
            payload = faiss.serialize_index(self._index)
            with open(tmp, "wb") as f:
                f.write(header)
                f.write(payload.tobytes())
            os.replace(tmp, self.index_path)
        except Exception:
            try:
                os.remove(tmp)
            except (OSError, FileNotFoundError):
                pass
            raise

    async def _rebuild_for_dim_change(self, new_dim: int):
        """维度变化时重建：丢弃旧索引文件、按新维度建空索引、复位孤儿 faiss_id。

        **不自动重烧 API 配额**（PLAN M3.3 决策）：重建只丢「派生数据」
        （FAISS 索引），chunks 表的文本原样保留，后续 add 慢慢回填；需要
        立即全量恢复的用户可在面板确认重建向量。面板可感知：
        :meth:`get_stats` 的 ``last_rebuild`` / ``dim`` 字段 + 本条 WARNING 日志。
        """
        old_dim = self.dim
        logger.warning(
            "[Quill RAG] FAISS 索引维度 %d 与当前 embedding 维度 %d 不一致，"
            "重建空索引（旧向量作废，文档文本仍在库中，随上传回填）",
            old_dim, new_dim,
        )
        self._index = None
        await asyncio.to_thread(self._remove_index_file)
        await self._exec_write(
            "UPDATE chunks SET faiss_id = -1 WHERE faiss_id >= 0"
        )
        self._dim_mismatch = False
        self._last_rebuild = {"reason": "dim_change", "from": old_dim, "to": int(new_dim)}
        self._create_index(new_dim)

    async def add(self, texts: list[str], embeddings: list[list[float]], source: str, doc_id: str = ""):
        # 同时保护维度变更、pending写入与索引发布，避免重建提交在三步之间插入。
        async with self._lock:
            return await self._add(texts,embeddings,source,doc_id)

    async def _add(self, texts, embeddings, source, doc_id):
        """F11 修复：SQLite 先写 pending 行（faiss_id=-1）拿 rowid → FAISS 写入 →
        失败则回滚 SQLite → 成功则回填 faiss_id。保证两库一致性。

        S1-3 修复：FAISS ID 直接用 SQLite row_id（AUTOINCREMENT 单调递增、全局唯一），
        彻底删除基于 ntotal 的 ID 生成逻辑（删除后 ntotal 下降会撞库）。
        S1-4 修复：IndexFlatIP 计算内积，add/search 前必须 L2 归一化，否则非真余弦相似度。

        返回成功写入的 chunk 数；**失败一律抛 :class:`StorageError`**（M3.2 D4
        六类高频路径之一：add，失败计数经 quill.core.storage_stats）。此前失败
        时静默 return，上层 `await add()` 不抛就当作成功，于是出现「上传成功
        但数据未入库」。
        """
        if not texts or not embeddings:
            return 0
        if doc_id == "":
            doc_id = source

        # ── 维度自适应（M3.3 F1）──
        # 批内一致性：一个批次必须同维度（异维度是调用方缺陷，直接拒绝）。
        first_dim = len(embeddings[0])
        if first_dim <= 0:
            raise ValueError("Embedding dimension must be > 0")
        for i, emb in enumerate(embeddings):
            if len(emb) != first_dim:
                raise ValueError(
                    f"Embedding dimension mismatch inside batch at chunk {i}: "
                    f"got {len(emb)}, first={first_dim}"
                )

        if self.dim <= 0:
            # 索引尚未建立：维度由**首批真实向量**决定（不再信启动期推断值）。
            # faiss 不可用时 _create_index 返回 None，走下方「文本入库暂不可
            # 检索」分支；provider 报告维度与实际向量不一致时以向量为准。
            if self.expected_dim and self.expected_dim != first_dim:
                logger.warning(
                    "[Quill RAG] provider 报告维度 %d 与实际向量维度 %d 不一致，以实际向量为准",
                    self.expected_dim, first_dim,
                )
            self._create_index(first_dim)
        elif first_dim != self.dim:
            # 维度已确定且与本批不一致 → embedding 已切换：重建空索引后照常
            # 写入本批（旧向量作废，文本仍在 chunks 表，随后续上传回填）。
            await self._rebuild_for_dim_change(first_dim)

        # 1. SQLite 单条插入，精确拿每行 rowid（faiss_id=-1 标记 pending）
        row_ids = []
        try:
            async with self._lock:
                for i, text in enumerate(texts):
                    cur = await self._conn.execute(
                        "INSERT INTO chunks (doc_id, source, chunk_index, content, faiss_id) "
                        "VALUES (?, ?, ?, ?, -1)",
                        (doc_id, source, i, text)
                    )
                    row_ids.append(cur.lastrowid)
                await self._conn.commit()
        except Exception as e:
            # L11：失败必须回滚。此前中途抛错时前几条 INSERT 留在**未提交事务**里，
            # 共享连接上的下一次任意写（别的 doc 的 add/delete）会把它们一起提交，
            # 留下永久 faiss_id=-1 的幽灵行，污染 get_stats/list_documents 的计数
            # （搜索侧有 F11 过滤，所以只是计数噪音，但会一直累积）。
            try:
                await self._conn.rollback()
            except Exception as rb_err:  # noqa: BLE001 - 回滚失败只能降级为日志
                logger.warning("[Quill RAG] add 失败后回滚未成功: %s", rb_err)
            note_storage_error("add", e)
            raise StorageError(
                "写入文档块失败",
                detail=f"FaissVectorStore.add SQLite 阶段: {e}",
                context={"source": source, "doc_id": doc_id},
            ) from e

        # 2. FAISS 写入
        if self._index is not None:
            try:
                # S1-3: 用 row_ids 作为 FAISS ID（全局唯一，不会因删除而撞库）
                ids = np.array(row_ids, dtype=np.int64)
                # S1-4: L2 归一化，使 IndexFlatIP 等价于余弦相似度
                emb_array = np.array(embeddings, dtype=np.float32)
                norms = np.linalg.norm(emb_array, axis=1, keepdims=True)
                norms[norms == 0] = 1.0  # 防止除零
                emb_array = emb_array / norms
                async with self._lock:
                    # P2-2 修复：FAISS 同步 CPU 密集操作放入线程池，避免阻塞事件循环
                    def _faiss_add():
                        self._index.add_with_ids(emb_array, ids)
                        self._save_index()
                    await asyncio.to_thread(_faiss_add)
            except Exception as e:
                # 3. FAISS 失败：回滚 SQLite（用精确 row_ids 删除 pending 行）
                note_storage_error("add", e)
                # M2：回滚必须**同时**覆盖 FAISS 侧。旧实现只 DELETE SQLite 行，
                # 而 `add_with_ids` 可能已经成功、只有 `_save_index()` 落盘失败
                # （磁盘满、文件被占用）—— 此时向量已经进了**内存索引**，
                # SQLite 却把对应行删了。于是这些 id 变成永久幽灵：search 里
                # `row_map.get(faiss_id)` 过滤掉它们，却照样占掉
                # `recall_k = min(top_k*3, ntotal)` 的名额，真实结果被挤出；
                # get_stats().faiss_vectors 也一直虚高。反过来 add_with_ids
                # 自身失败时 remove_ids 是安全的空操作。
                if self._index is not None and row_ids:
                    try:
                        async with self._lock:
                            def _faiss_rollback():
                                self._index.remove_ids(np.array(row_ids, dtype=np.int64))
                                # 把「已回滚」的状态落盘，否则下次启动从旧文件
                                # 加载回来又会带回这批幽灵 id。
                                self._save_index()
                            await asyncio.to_thread(_faiss_rollback)
                    except Exception as fe:
                        # 回滚索引失败只能降级为日志：绝不能因此掩盖原始错误，
                        # 但也必须让用户知道索引里可能残留了无效 id。
                        logger.warning(
                            "[Quill RAG] FAISS 回滚 remove_ids 失败（索引可能残留 %d 个无效 id，"
                            "重建索引可清除）: %s",
                            len(row_ids), fe,
                        )
                if not row_ids:
                    logger.warning(
                        "[Quill RAG] FaissVectorStore.add FAISS 写入失败且无 row_ids 可回滚: %s",
                        e, exc_info=True,
                    )
                    raise StorageError(
                        "文档向量写入失败（无可回滚数据）",
                        detail=f"FaissVectorStore.add FAISS 阶段: {e}",
                        context={"source": source, "doc_id": doc_id},
                    ) from e
                async with self._lock:
                    placeholders = ",".join("?" for _ in row_ids)
                    await self._conn.execute(
                        f"DELETE FROM chunks WHERE id IN ({placeholders})",
                        row_ids
                    )
                    await self._conn.commit()
                logger.warning(
                    "[Quill RAG] FaissVectorStore.add FAISS 写入失败，已回滚 %d 行 SQLite: %s",
                    len(row_ids), e, exc_info=True,
                )
                # 回滚完成也必须向上报错：数据没进去就是没进去，不能让上层
                # 以为入库成功（此前这里 return，界面显示「上传成功」）。
                raise StorageError(
                    "文档向量写入失败（已回滚数据库写入）",
                    detail=f"FaissVectorStore.add FAISS 阶段: {e}",
                    context={"source": source, "doc_id": doc_id, "rolled_back": len(row_ids)},
                ) from e
            # 4. 回填 faiss_id（S1-3 后 faiss_id == row_id，但仍写入以保持一致性和 search 性能）
            async with self._lock:
                for rid in row_ids:
                    await self._conn.execute(
                        "UPDATE chunks SET faiss_id = ? WHERE id = ?",
                        (rid, rid)
                    )
                await self._conn.commit()
            # 落库完成：返回实际入库条数供上层核对
            return len(row_ids)
        else:
            # FAISS 不可用（未安装 faiss）。文本已写进 SQLite 且可随索引重建恢复，
            # 不是丢数据，但**当前检索不到**。返回 0 让上层提示降级而非谎报成功。
            logger.warning(
                "[Quill RAG] FAISS 未初始化（faiss 不可用？），%d 条文本已入库但暂不可检索",
                len(row_ids),
            )
            return 0

    async def search(self, query_embedding: list[float], top_k: int = 9, allowed_sources: list[str] = None) -> list[dict]:
        """FAISS 检索，支持通过 allowed_sources 按文档 source 过滤。

        S1-4 修复：query 向量也需 L2 归一化。
        M3.3 F1：查询维度与索引维度不一致（embedding 已切换但尚未经 add
        触发重建）时，明确降级为空结果——这是配置状态而非存储故障，不再
        让 FAISS 断言炸成 StorageError；面板经 get_stats().dim_mismatch 可见。
        """
        if self._index is None or self.dim <= 0 or self._index.ntotal == 0:
            if (await self._exec_fetchone('SELECT COUNT(*) FROM chunks'))[0]:
                raise StorageError('文档向量索引为空，已有文本待重建',detail='document_index_pending')
            return []
        try:
            # S1-4: L2 归一化 query
            query = np.array([query_embedding], dtype=np.float32)
            if query.shape[1] != self.dim:
                self._dim_mismatch = True
                logger.warning(
                    "[Quill RAG] 查询维度 %d 与索引维度 %d 不一致，跳过本次向量检索"
                    "（下次上传文档时将按新维度重建索引）",
                    query.shape[1], self.dim,
                )
                raise StorageError('查询与文档索引维度不一致，请重建索引',detail='document_index_dimension_mismatch')
            qnorm = np.linalg.norm(query)
            if qnorm > 0:
                query = query / qnorm

            # 多召回一些以补偿幽灵向量。如果有文档过滤限制，大幅放大召回池以防被过滤空。
            recall_k = min(top_k * 3, self._index.ntotal)
            if allowed_sources is not None:
                recall_k = min(max(top_k * 10, 100), self._index.ntotal)

            async with self._lock:
                # P2-2 修复：FAISS search 为同步 CPU 密集操作，放入线程池
                def _faiss_search():
                    return self._index.search(query, recall_k)
                scores, ids = await asyncio.to_thread(_faiss_search)

            results = []
            try:
                # 收集有效的 faiss_id 及对应 score
                valid_pairs = []  # [(faiss_id, score), ...]
                for score, idx in zip(scores[0], ids[0]):
                    if idx < 0:
                        continue
                    valid_pairs.append((int(idx), float(score)))

                if not valid_pairs:
                    return results

                # 批量查询（单次 SQL 替代 N 次）
                faiss_ids = [p[0] for p in valid_pairs]
                placeholders = ",".join("?" * len(faiss_ids))
                rows = await self._exec_fetchall(
                    f"SELECT content, source, chunk_index, faiss_id FROM chunks WHERE faiss_id IN ({placeholders})",
                    faiss_ids
                )
                # 构建 faiss_id → row 映射
                row_map = {}
                for row in rows:
                    row_map[row[3]] = row

                # 按 score 排序遍历
                for faiss_id, score in valid_pairs:
                    row = row_map.get(faiss_id)
                    if row:
                        # F11 修复：过滤 faiss_id 无效的孤儿行（pending 行 faiss_id=-1）
                        if row[3] is not None and row[3] < 0:
                            continue
                        doc_source = row[1]
                        if allowed_sources is not None and doc_source not in allowed_sources:
                            continue

                        results.append({
                            "content": row[0],
                            "source": doc_source,
                            "chunk_index": row[2],
                            "score": score,
                        })

                        if len(results) >= top_k:
                            break
            except Exception as e:
                # 内层：元数据回表失败。保留已映射的部分结果（设计降级，不改语义），
                # 但日志带方法名与异常链（M3.2 D4 未改造路径统一格式）。
                logger.warning("[Quill RAG] FaissVectorStore.search SQLite 回表失败: %s", e, exc_info=True)
            return results
        except Exception as e:
            # D4（M3.2）：FAISS 检索失败抛 StorageError（六类高频路径之一：search），
            # 调用方（retrieval.search_documents）降级为带失败标记的空结果——
            # 此前静默返回 []，与「确实没找到」不可区分。
            note_storage_error("search", e)
            raise StorageError(
                "文档向量检索失败",
                detail=f"FaissVectorStore.search: {e}",
            ) from e

    async def delete_by_source(self, source: str) -> int:
        # 与全量重建发布互斥，不能在删完FAISS但尚未删映射时发布旧快照。
        async with self._lock:
            return await self._delete_by_source(source)

    async def _delete_by_source(self, source: str) -> int:
        """删除某文档的所有块，并尝试从 FAISS 索引中移除对应向量。

        顺序：先查 faiss_id → 再删 FAISS 向量 → 成功后删 SQLite。
        若 FAISS 删除失败，保留 SQLite 记录并标记需要重建，避免产生无法检索的
        幽灵向量（设计降级，保持不变）。SQLite 删除失败则抛
        :class:`StorageError`（M3.2 D4 六类高频路径之一：delete）——此前裸
        except 吞掉后返回 0，面板看到「deleted: 0」却不知道删除根本没执行。
        """
        try:
            rows = await self._exec_fetchall(
                "SELECT faiss_id FROM chunks WHERE source = ? AND faiss_id >= 0", (source,)
            )
            to_remove = np.array([r[0] for r in rows], dtype=np.int64)
        except Exception as e:
            logger.warning(
                "[Quill RAG] delete_by_source 查询 faiss_id 失败: %s", e, exc_info=True
            )
            to_remove = np.array([], dtype=np.int64)

        # 先尝试从 FAISS 移除向量，成功后再删 SQLite
        if len(to_remove) > 0 and self._index is not None:
            try:
                async with self._lock:
                    # P2-2 修复：FAISS remove_ids + 索引落盘放入线程池
                    def _faiss_remove():
                        self._index.remove_ids(to_remove)
                        self._save_index()
                    await asyncio.to_thread(_faiss_remove)
                logger.info(f"[Quill RAG] 已从 FAISS 索引移除 {len(to_remove)} 条向量 (source={source})")
            except Exception as e:
                logger.error(
                    f"[Quill RAG] FAISS remove_ids 失败 (source={source})，保留 SQLite 数据等待重建: {e}"
                )
                return 0

        try:
            cursor = await self._exec_write("DELETE FROM chunks WHERE source = ?", (source,))
            deleted = cursor.rowcount
        except Exception as e:
            note_storage_error("delete", e)
            raise StorageError(
                "删除文档数据失败",
                detail=f"delete_by_source SQLite 阶段: {e}",
                context={"source": source},
            ) from e

        return deleted

    async def list_documents(self) -> list[dict]:
        """列出所有已上传文档。"""
        try:
            rows = await self._exec_fetchall(
                "SELECT source, doc_id, COUNT(*) as chunk_count, MIN(created_at) as created_at "
                "FROM chunks GROUP BY source, doc_id"
            )
            return [
                {"source": r[0], "doc_id": r[1], "chunk_count": r[2], "created_at": r[3]}
                for r in rows
            ]
        except Exception as e:
            logger.warning("[Quill RAG] list_documents 失败: %s", e, exc_info=True)
            return []

    async def get_stats(self) -> dict:
        """返回存储统计。

        M3.3 面板可观测（只增字段）：``dim`` 现在是索引**真实**维度
        （0 = 尚未建立，此前是启动期推断值）；``dim_mismatch`` = 检索发现
        查询维度与索引不一致（等下次上传触发重建）；``last_rebuild`` =
        最近一次维度重建的 ``{"reason","from","to"}`` 或 None。
        """
        try:
            total_chunks = (await self._exec_fetchone("SELECT COUNT(*) FROM chunks"))[0]
            total_docs = (await self._exec_fetchone("SELECT COUNT(DISTINCT source) FROM chunks"))[0]
            pending = (await self._exec_fetchone('SELECT COUNT(*) FROM chunks WHERE faiss_id<0'))[0]
        except Exception as e:
            logger.warning("[Quill RAG] get_stats 失败: %s", e, exc_info=True)
            raise StorageError('无法读取文档索引状态',detail='document_stats_unavailable') from e
        return {
            "total_chunks": total_chunks,
            "total_docs": total_docs,
            "faiss_vectors": self._index.ntotal if self._index else 0,
            "dim": self.dim,
            "dim_mismatch": self._dim_mismatch,
            "last_rebuild": self._last_rebuild,
            "pending_chunks": max(pending,total_chunks-(self._index.ntotal if self._index else 0)),
            "available": bool(self._index is not None and self._index.ntotal>0 and not self._dim_mismatch),
        }

    async def reindex(self, embedding_provider, handle, *, validate=None):
        """P4：显式付费恢复已有文本；模型阶段不动现有索引，提交前复核全部文本。"""
        import faiss
        import shutil
        from pathlib import Path
        from uuid import uuid4
        rows=await self._exec_fetchall('SELECT id,content FROM chunks ORDER BY id')
        snapshot=[(r[0],r[1]) for r in rows]
        if not rows:
            handle.record.result={'chunks':0};return handle.record.result
        candidate=None;dimension=0
        for start in range(0,len(rows),32):
            await handle.checkpoint(start,total=len(rows),message='重新嵌入已有文档（会计入用量）')
            if validate and not validate():
                raise StorageError('嵌入模型或文档组件已切换，请重试')
            batch=rows[start:start+32]
            vectors=await embedding_provider.embed([r[1] for r in batch])
            array=np.asarray(vectors,dtype=np.float32)
            if array.ndim!=2 or len(array)!=len(batch) or array.shape[1]<=0 or not np.isfinite(array).all():
                raise StorageError('嵌入结果数量或数值无效，旧索引保留')
            if candidate is None:
                dimension=array.shape[1];candidate=faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
            if array.shape[1]!=dimension:
                raise StorageError('重建过程中嵌入维度改变，旧索引保留')
            norms=np.linalg.norm(array,axis=1,keepdims=True)
            if np.any(norms==0):
                raise StorageError('嵌入模型返回零向量，旧索引保留')
            candidate.add_with_ids(array/norms,np.asarray([r[0] for r in batch],dtype=np.int64))
        await handle.checkpoint(len(rows),total=len(rows),message='核对来源并提交索引')
        path=Path(self.index_path);name=uuid4().hex
        temporary=path.with_name(path.name+'.'+name+'.tmp');backup=path.with_name(path.name+'.'+name+'.bak')
        def prepare_file():
            with temporary.open('xb') as f:
                os.chmod(temporary,0o600)
                f.write(_HEADER_STRUCT.pack(INDEX_MAGIC,INDEX_FORMAT_VERSION,dimension));f.write(faiss.serialize_index(candidate).tobytes());f.flush();os.fsync(f.fileno())
            if path.exists():
                shutil.copyfile(path,backup);os.chmod(backup,0o600)
        committed=False
        try:
            async with self._lock:
                if validate and not validate():
                    raise StorageError('嵌入模型或文档组件已切换，旧索引保留')
                current=await self._exec_fetchall('SELECT id,content FROM chunks ORDER BY id')
                if [(r[0],r[1]) for r in current]!=snapshot:
                    raise StorageError('文档在重建期间发生变更，请重试；现有索引保留')
                # 提交区不再响应用户取消，避免文件与映射各提交一半；任务取消仍在模型批次生效。
                work=asyncio.create_task(asyncio.to_thread(prepare_file))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    await asyncio.gather(work,return_exceptions=True)
                    raise
                existed=path.exists()
                try:
                    await self._conn.execute('BEGIN IMMEDIATE')
                    await self._conn.execute('UPDATE chunks SET faiss_id=id')
                    os.replace(temporary,path)
                    await self._conn.commit()
                except BaseException:
                    await self._conn.rollback()
                    if backup.exists():os.replace(backup,path)
                    elif not existed:path.unlink(missing_ok=True)
                    raise
                self._index=candidate;self.dim=dimension;self._dim_mismatch=False
                self._last_rebuild={'reason':'manual_reindex','chunks':len(rows),'to':dimension}
                committed=True
        finally:
            temporary.unlink(missing_ok=True)
            if committed:backup.unlink(missing_ok=True)
        handle.record.result={'chunks':len(rows),'dim':dimension}
        return handle.record.result

    async def load_index(self):
        """重新加载 FAISS 索引（供 /doc reload 调用）。

        此前漏了 await（协程从未执行，reload 实际无效）——修复保留。
        锁已可重入（M3.3 D5），持锁调用本方法不再有死锁风险；
        调用方保持无锁调用不变。
        """
        async with self._lock:
            await self._load_index()
