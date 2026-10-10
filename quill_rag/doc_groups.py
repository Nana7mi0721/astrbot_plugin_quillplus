# -*- coding: utf-8 -*-
"""DocGroupStore — 插件自有 RAG 文档的「子知识库」分组。

背景
----
插件文档原先是一个平铺列表（``FaissVectorStore.list_documents()`` 按 source
聚合）。用户希望能在插件内像 AstrBot 那样建「子知识库」：新建分组、把文档
上传进指定分组、之后还能改组（含移出到「未分组」）。

设计约束
--------
- **分组是元数据，不是向量数据**：所有插件文档共用同一套 embedding / FAISS
  索引（``rag.embedding_provider_id`` 是全局配置），所以分组只记在
  ``<data_root>/rag_doc_groups.json`` 里，**不动 chunks 表结构**——不需要
  SQLite 迁移，旧库/旧版本回滚都能读。
- **未分组是合法状态**：``assign`` 里没有条目的文档就是「未分组」，不写空串
  记录（文件更干净，也不会因为一次误写而把文档钉死在空组里）。
- **绑定标识**：``grp:<组名>``，与 ``kb:<库名>`` 同级，都是「一次绑定一整组」；
  ``split_bound_ex`` 通过 :meth:`DocGroupStore.resolve` 把它展开成文档 source
  列表，因此检索层完全不需要知道分组的存在。
- 文件损坏 / 数据目录不可写 → 退化为「没有分组」，绝不阻塞插件启动
  （与 ``state.py`` 的容错口径一致）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from typing import Iterable, Optional

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件自测：先把 AstrBot 加入可导入路径
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _astrbot_bootstrap import ensure_astrbot_importable

    ensure_astrbot_importable()
    from astrbot.api import logger

# 绑定前缀：bound_rag_docs 中插件文档分组的标识格式。
GROUP_SOURCE_PREFIX = "grp:"

# 分组名长度上限（面板输入框与后端共用同一口径）。
MAX_GROUP_NAME_LEN = 40

# 面板里「不属于任何分组」那一栏的显示名。同名分组被保留，避免出现两个
# 「未分组」让人无法区分。
UNGROUPED_LABEL = "未分组"

# 分组数量上限：每个分组在面板上都是一个卡片，几百个分组既没人用也会把
# 前端渲染拖垮。
MAX_GROUPS = 100

_FILE_VERSION = 1


def is_group_source(source: object) -> bool:
    """绑定标识是否指向插件文档分组。"""
    return isinstance(source, str) and source.startswith(GROUP_SOURCE_PREFIX)


def parse_group_source(source: object) -> str:
    """``grp:组名`` → ``组名``；非分组标识返回空串。"""
    if not is_group_source(source):
        return ""
    return str(source)[len(GROUP_SOURCE_PREFIX):].strip()


def group_source(name: str) -> str:
    """组出分组绑定标识 —— 格式的唯一来源（面板/指令/测试都走这里）。"""
    return f"{GROUP_SOURCE_PREFIX}{str(name or '').strip()}"


def normalize_group_name(name: object) -> str:
    """校验并规整分组名；非法输入抛 :class:`ValueError`。"""
    s = str(name or "").strip()
    if not s:
        raise ValueError("分组名不能为空")
    if len(s) > MAX_GROUP_NAME_LEN:
        raise ValueError(f"分组名过长（最多 {MAX_GROUP_NAME_LEN} 个字符）")
    if any(ch in s for ch in "\r\n\t"):
        raise ValueError("分组名不能包含换行或制表符")
    if s == UNGROUPED_LABEL:
        raise ValueError(f"「{UNGROUPED_LABEL}」是保留名称，请换一个")
    if is_group_source(s) or s.startswith("kb:"):
        raise ValueError("分组名不能以 kb: / grp: 开头（那是绑定标识前缀）")
    return s


class DocGroupStore:
    """插件文档分组的持久化存储（JSON + 原子替换）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._groups: list[str] = []
        self._assign: dict[str, str] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # ── 读写 ───────────────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _read_file(self) -> tuple[list[str], dict[str, str]]:
        """同步读取（解析失败 → 留档改名 + 当作空数据）。"""
        if not self.path or not os.path.isfile(self.path):
            return [], {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001 — 任何损坏都只能是「没有分组」
            logger.warning("[Quill RAG] 分组文件解析失败（将重建）: %s", exc)
            self._quarantine()
            return [], {}
        if not isinstance(raw, dict):
            logger.warning("[Quill RAG] 分组文件结构异常（将重建）: %r", type(raw))
            self._quarantine()
            return [], {}

        groups: list[str] = []
        for item in raw.get("groups") or []:
            try:
                name = normalize_group_name(item)
            except ValueError:
                continue
            if name not in groups:
                groups.append(name)

        assign: dict[str, str] = {}
        raw_assign = raw.get("assign")
        if isinstance(raw_assign, dict):
            for src, grp in raw_assign.items():
                key = str(src or "").strip()
                if not key:
                    continue
                # 指向已不存在分组的条目直接丢弃：分组被删时文档应回到「未分组」，
                # 而不是挂在一个看不见的名字上。
                if str(grp or "").strip() in groups:
                    assign[key] = str(grp).strip()
        return groups, assign

    def _quarantine(self) -> None:
        """把读不动的分组文件改名留档，别让用户的数据无声消失。"""
        try:
            os.replace(self.path, f"{self.path}.corrupt-{int(time.time())}")
        except OSError:
            pass

    def _ensure_loaded_sync(self) -> None:
        if self._loaded:
            return
        self._groups, self._assign = self._read_file()
        self._loaded = True

    def _write_file(self) -> None:
        """原子写：先写 .tmp 再 os.replace，避免半截文件把分组全弄丢。"""
        payload = {
            "version": _FILE_VERSION,
            "groups": list(self._groups),
            "assign": dict(self._assign),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = f"{self.path}.tmp"
        dirname = os.path.dirname(self.path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)

    async def _persist(self) -> None:
        try:
            await asyncio.to_thread(self._write_file)
        except OSError as exc:
            logger.warning("[Quill RAG] 分组文件写入失败: %s", exc)

    async def load(self) -> None:
        """懒加载（幂等）。"""
        async with self._lock:
            if self._loaded:
                return
            await asyncio.to_thread(self._ensure_loaded_sync)

    # ── 查询 ───────────────────────────────────────────────────

    async def list_groups(self) -> list[str]:
        await self.load()
        async with self._lock:
            return list(self._groups)

    async def snapshot(self) -> dict:
        """``{"groups": [...], "assign": {...}}``（深拷贝，调用方随便改）。"""
        await self.load()
        async with self._lock:
            return {"groups": list(self._groups), "assign": copy.deepcopy(self._assign)}

    async def group_of(self, source: str) -> str:
        await self.load()
        async with self._lock:
            return self._assign.get(str(source or "").strip(), "")

    def resolve(self, name: str) -> Optional[list[str]]:
        """把分组名解析成组内文档 source 列表（供 ``split_bound_ex`` 同步调用）。

        返回 ``None`` 表示「没有这个分组」——调用方应忽略该绑定条目；
        返回 ``[]`` 表示分组存在但为空（**是一次有效但空手而归的绑定**）。
        """
        self._ensure_loaded_sync()
        key = str(name or "").strip()
        if key not in self._groups:
            return None
        return [src for src, grp in self._assign.items() if grp == key]

    def sources_in(self, name: str) -> list[str]:
        """:meth:`resolve` 的「不存在当空」版本。"""
        return list(self.resolve(name) or [])

    # ── 变更 ───────────────────────────────────────────────────

    async def create(self, name: str) -> list[str]:
        clean = normalize_group_name(name)
        await self.load()
        async with self._lock:
            if clean in self._groups:
                raise ValueError(f"分组「{clean}」已存在")
            if len(self._groups) >= MAX_GROUPS:
                raise ValueError(f"分组数量已达上限（{MAX_GROUPS} 个）")
            self._groups.append(clean)
            await self._persist()
            return list(self._groups)

    async def rename(self, name: str, new_name: str) -> int:
        old = normalize_group_name(name)
        new = normalize_group_name(new_name)
        await self.load()
        async with self._lock:
            if old not in self._groups:
                raise ValueError(f"分组「{old}」不存在")
            if old == new:
                return 0
            if new in self._groups:
                raise ValueError(f"分组「{new}」已存在")
            self._groups[self._groups.index(old)] = new
            moved = 0
            for src, grp in list(self._assign.items()):
                if grp == old:
                    self._assign[src] = new
                    moved += 1
            await self._persist()
            return moved

    async def delete(self, name: str) -> int:
        clean = normalize_group_name(name)
        await self.load()
        async with self._lock:
            if clean not in self._groups:
                raise ValueError(f"分组「{clean}」不存在")
            self._groups.remove(clean)
            # 组内文档回到「未分组」，而不是跟着分组一起消失。
            freed = [src for src, grp in self._assign.items() if grp == clean]
            for src in freed:
                self._assign.pop(src, None)
            await self._persist()
            return len(freed)

    async def assign(self, sources: Iterable[str], group: str = "") -> int:
        """把若干文档移入分组；``group`` 为空串 = 移出到「未分组」。"""
        clean = str(group or "").strip()
        if clean:
            clean = normalize_group_name(clean)
        await self.load()
        async with self._lock:
            if clean and clean not in self._groups:
                raise ValueError(f"分组「{clean}」不存在")
            changed = 0
            for raw in sources or []:
                src = str(raw or "").strip()
                if not src:
                    continue
                if clean:
                    if self._assign.get(src) != clean:
                        self._assign[src] = clean
                        changed += 1
                elif self._assign.pop(src, None) is not None:
                    changed += 1
            if changed:
                await self._persist()
            return changed

    async def prune(self, existing: Iterable[str]) -> int:
        """丢掉指向已删除文档的条目（文档删了，分组里不该留幽灵）。"""
        alive = {str(s or "").strip() for s in existing or []}
        alive.discard("")
        await self.load()
        async with self._lock:
            stale = [src for src in self._assign if src not in alive]
            for src in stale:
                self._assign.pop(src, None)
            if stale:
                await self._persist()
            return len(stale)

    async def attach_documents(self, docs: list[dict]) -> list[dict]:
        """给 ``list_documents()`` 的结果补上 ``group`` 字段（保序、只增字段）。"""
        await self.load()
        async with self._lock:
            out = []
            for doc in docs or []:
                item = dict(doc)
                item["group"] = self._assign.get(str(doc.get("source") or "").strip(), "")
                out.append(item)
            return out


def _self_test() -> None:  # pragma: no cover — 手工自检（python -m quill_rag.doc_groups）
    import asyncio
    import tempfile

    async def main() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DocGroupStore(os.path.join(tmp, "rag_doc_groups.json"))
            assert await store.list_groups() == []
            assert store.resolve("没有这个组") is None

            await store.create("剧情设定")
            await store.create("世界观")
            assert await store.list_groups() == ["剧情设定", "世界观"]
            try:
                await store.create("剧情设定")
                raise AssertionError("重名应报错")
            except ValueError:
                pass
            for bad in ("", "   ", "未分组", "kb:库", "grp:x", "a" * (MAX_GROUP_NAME_LEN + 1)):
                try:
                    await store.create(bad)
                    raise AssertionError(f"非法名应报错: {bad!r}")
                except ValueError:
                    pass

            assert await store.assign(["a.txt", "b.txt"], "剧情设定") == 2
            assert await store.assign(["b.txt"], "世界观") == 1
            assert store.resolve("剧情设定") == ["a.txt"]
            assert store.resolve("世界观") == ["b.txt"]
            docs = await store.attach_documents(
                [{"source": "a.txt"}, {"source": "c.txt"}]
            )
            assert [d["group"] for d in docs] == ["剧情设定", ""], docs

            assert await store.rename("世界观", "设定集") == 1
            assert store.resolve("设定集") == ["b.txt"] and store.resolve("世界观") is None

            assert await store.delete("设定集") == 1
            assert await store.attach_documents([{"source": "b.txt"}]) == [
                {"source": "b.txt", "group": ""}
            ], "删组后文档应回到未分组"

            await store.assign(["a.txt"], "")
            assert store.resolve("剧情设定") == []
            assert await store.assign(["a.txt"], "剧情设定") == 1
            assert await store.assign(["a.txt", "ghost.txt"], "剧情设定") == 1
            assert await store.prune(["a.txt"]) == 1
            assert store.resolve("剧情设定") == ["a.txt"]

            # 落盘 → 新实例读回
            again = DocGroupStore(store.path)
            assert await again.list_groups() == ["剧情设定"]
            assert again.resolve("剧情设定") == ["a.txt"]

            # 绑定标识解析
            assert is_group_source("grp:剧情设定") and not is_group_source("kb:库")
            assert parse_group_source("grp:剧情设定") == "剧情设定"
            assert parse_group_source(" 设定.txt ") == ""
            assert group_source("剧情设定") == "grp:剧情设定"

            # 损坏文件 → 留档 + 空数据，不抛
            bad = DocGroupStore(os.path.join(tmp, "broken.json"))
            with open(bad.path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            assert await bad.list_groups() == []
            assert any(
                f.startswith("broken.json.corrupt-") for f in os.listdir(tmp)
            ), os.listdir(tmp)

        print("doc_groups 自测通过")

    asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    _self_test()
