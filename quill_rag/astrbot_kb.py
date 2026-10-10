# -*- coding: utf-8 -*-
"""AstrBotKBSource — AstrBot 内置知识库（kb_manager）的检索适配层。

把 ``self.context.kb_manager``（astrbot.core.knowledge_base.KnowledgeBaseManager）
包装成插件 Doc RAG 体系的一个「可绑定源」：

- 绑定标识统一为 ``kb:<库名>``（``bound_rag_docs`` 里与插件自有文档 source
  混存，前缀区分，见 :func:`split_bound`）；
- 检索结果映射为与 ``FaissVectorStore.search`` 相同的 dict 形状
  （``content / source / chunk_index / score``），下游
  ``format_for_prompt`` / 注入报告零改动；
- 失败语义走 ``retrieval.RagResult`` 协议（``_rag_ok`` / ``_rag_error``），
  与本地 FAISS 路一致。

设计约束（S-兼容）：
- AstrBot 版本过旧（无 ``kb_manager``）或测试 stub 环境（Context 是裸
  object）→ ``available`` 为 False，功能自然隐藏，不抛错；
- kb_manager 自身的检索（embedding/重排）由 AstrBot 侧完成，本组件
  **不持有任何自建句柄**，无 close 语义，不进 ``_close_rag_components`` 的
  资源释放列表。
"""

from __future__ import annotations

from typing import Optional

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件自测：先把 AstrBot 加入可导入路径
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _astrbot_bootstrap import ensure_astrbot_importable
    ensure_astrbot_importable()
    from astrbot.api import logger

# 插件文档分组标识（grp:组名）：分组元数据只存在插件侧，这里只负责拆解绑定。
from .doc_groups import is_group_source, parse_group_source

# 绑定前缀：bound_rag_docs 中 AstrBot 内置知识库条目的标识格式。
# 上传文档的 source 来自文件名，Windows 文件名不允许':'，实际不会冲突。
KB_SOURCE_PREFIX = "kb:"

# 文档级绑定分隔符：``kb:库名/文档名``。解析取**第一个** '/'，所以
# 文档名里带 '/' 也能还原（``kb:库/docs/a.md`` → 库名``库``、文档``docs/a.md``）；
# 代价是库名本身不能含 '/'（AstrBot 未禁止，但实际命名几乎不会遇到）。
KB_DOC_SEP = "/"

# 文档级绑定时向 kb_manager 索取候选池的下限：要先拿到足够多的候选，
# 再按文档名过滤，否则绑定的小文档很容易被同库其它文档挤出 top_m_final。
KB_DOC_FILTER_FETCH = 30

try:
    from .retrieval import RagResult, _rag_failed
except ImportError:  # 直接运行本文件自测时无父包
    from retrieval import RagResult, _rag_failed


def is_kb_source(source: str) -> bool:
    """绑定标识是否指向 AstrBot 内置知识库。"""
    return isinstance(source, str) and source.startswith(KB_SOURCE_PREFIX)


def kb_source_name(source: str) -> str:
    """从绑定标识提取 AstrBot 知识库名（``kb:xxx`` → ``xxx``）。

    文档级标识（``kb:库名/文档名``）同样只取第一段库名——需要区分
    「整库」与「单文档」时用 :func:`parse_kb_source`。
    """
    return parse_kb_source(source)[0]


def parse_kb_source(source: str) -> tuple[str, str]:
    """拆解内置库绑定标识 → ``(库名, 文档名)``。

    整库绑定（``kb:库名``）的文档名为空串；非内置库标识返回 ``("", "")``。
    """
    if not is_kb_source(source):
        return "", ""
    body = source[len(KB_SOURCE_PREFIX):]
    name, sep, doc = body.partition(KB_DOC_SEP)
    return name.strip(), (doc.strip() if sep else "")


def kb_doc_source(kb_name: str, doc_name: str) -> str:
    """组出文档级绑定标识 —— 格式的唯一来源（面板/指令/测试都走这里）。"""
    return f"{KB_SOURCE_PREFIX}{kb_name}{KB_DOC_SEP}{doc_name}"


def split_bound_ex(bound: Optional[list[str]], group_resolver=None) -> tuple[list[str], list[str], dict]:
    """把人设的 ``bound_rag_docs`` 拆成三路（含文档级与分组绑定）。

    Args:
        bound: ``bound_rag_docs`` 原始条目列表。
        group_resolver: ``(组名) -> list[str] | None``，把 ``grp:<组名>`` 展开成
            组内文档 source 列表（:meth:`DocGroupStore.resolve`）。返回 ``None``
            表示分组不存在，该条目被忽略；不传则在检索层忽略所有 ``grp:`` 条目
            （分组元数据只存在于插件侧，不该被当成一个文档名去匹配）。

    Returns:
        ``(plugin_sources, kb_names, kb_doc_filters)``：

        - ``plugin_sources``：插件自有文档 source（精确匹配 ``allowed_sources``），
          含 ``grp:`` 展开出来的成员；
        - ``kb_names``：内置库名（保序去重）；
        - ``kb_doc_filters``：``{库名: None | [文档名, ...]}``，``None`` 表示整库。
          同一库既有整库条目又有文档条目时，整库优先（语义更宽，且不会因为
          漏掉一条文档而少注入）。

    无法识别的条目按插件文档处理（与旧行为一致——``allowed_sources``
    精确匹配，匹配不到就静默不命中）。
    """
    plugin_sources: list[str] = []
    kb_names: list[str] = []
    kb_doc_filters: dict = {}
    for item in bound or []:
        s = str(item or "").strip()
        if not s:
            continue
        if is_kb_source(s):
            name, doc = parse_kb_source(s)
            if not name:
                continue
            if name not in kb_names:
                kb_names.append(name)
            if not doc:
                kb_doc_filters[name] = None          # 整库绑定，覆盖文档级
                continue
            cur = kb_doc_filters.get(name, "")
            if cur is None:                          # 已是整库：文档条目被吸收
                continue
            if not cur:
                kb_doc_filters[name] = [doc]
            elif doc not in cur:
                cur.append(doc)
        elif is_group_source(s):
            gname = parse_group_source(s)
            if not gname or group_resolver is None:
                continue
            # 分组不存在 → 忽略（等价于该组为空）；组存在但没文档 → 展开成空，
            # 于是 plugin_sources 里没有这一组的成员（而不是"不过滤＝全都注入"）。
            for src in group_resolver(gname) or []:
                if src not in plugin_sources:
                    plugin_sources.append(src)
        elif s not in plugin_sources:
            plugin_sources.append(s)
    return plugin_sources, kb_names, kb_doc_filters


def split_bound(bound: Optional[list[str]]) -> tuple[list[str], list[str]]:
    """``split_bound_ex`` 的旧签名（只要两路的调用点保持不动）。"""
    plugin_sources, kb_names, _ = split_bound_ex(bound)
    return plugin_sources, kb_names


class AstrBotKBSource:
    """AstrBot 内置知识库检索源。

    Usage::

        kbs = AstrBotKBSource(self.context, top_k=3)
        if kbs.available:
            result = await kbs.search("一些问题")          # RagResult[list[dict]]
            infos = await kbs.list_kbs()                  # 面板 / /doc list 用
    """

    def __init__(self, context, enabled: bool = True, top_k: int = 3):
        self.context = context
        self.enabled = bool(enabled)
        self.top_k = int(top_k) if top_k and int(top_k) > 0 else 3

    @property
    def available(self) -> bool:
        """kb_manager 是否可达（旧版 AstrBot / stub 环境为 False）。"""
        return bool(
            self.enabled
            and self.context is not None
            and getattr(self.context, "kb_manager", None) is not None
        )

    async def list_kbs(self) -> list[dict]:
        """列出全部内置知识库（面板展示 / auto 模式取全量名单）。

        返回项：``{source, kb_id, kb_name, description, emoji, doc_count,
        chunk_count, init_error}``。kb_manager 不可达返回 []（不是失败——
        「没有可用的内置库」与「功能未启用」对调用方行为一致）。
        """
        if not self.available:
            return []
        kb_manager = getattr(self.context, "kb_manager", None)
        try:
            records = await kb_manager.list_kbs()
        except Exception as e:
            logger.warning("[Quill KB] 列举 AstrBot 内置知识库失败: %s", e)
            return []
        infos: list[dict] = []
        for rec in records or []:
            kb_name = getattr(rec, "kb_name", "") or ""
            if not kb_name:
                continue
            infos.append({
                "source": KB_SOURCE_PREFIX + kb_name,
                # kb_id 给面板做文档级懒加载用（库名可能重名，kb_id 才是主键）
                "kb_id": str(getattr(rec, "kb_id", "") or ""),
                "kb_name": kb_name,
                "description": getattr(rec, "description", "") or "",
                "emoji": getattr(rec, "emoji", "") or "",
                "doc_count": int(getattr(rec, "doc_count", 0) or 0),
                "chunk_count": int(getattr(rec, "chunk_count", 0) or 0),
                # init_error 挂在 KBHelper 上而不是 KnowledgeBase 模型上，
                # list_kbs 返回的是 helper.kb，所以这里大概率取不到——保留
                # 字段让面板展示逻辑统一，准确性由 search 时的 retrieve 报错兜底。
                "init_error": str(getattr(rec, "init_error", "") or ""),
            })
        return infos

    async def list_documents(self, kb_name: str, limit: int = 200) -> list[dict]:
        """列出某个内置库里的文档（面板「子知识库组」展开时懒加载）。

        走 ``kb_manager.get_kb_by_name`` → ``KBHelper.list_documents``，
        两者都是 AstrBot 的公开面（插件不碰 vec_db / 索引内部）。

        返回项：``{doc_id, doc_name, file_type, file_size, chunk_count,
        created_at}``。库不存在 / 不可达 / 查询失败一律返回 [] 并记日志——
        面板按「这个组现在展不开」处理，不影响整页渲染。
        """
        if not kb_name or not self.available:
            return []
        kb_manager = getattr(self.context, "kb_manager", None)
        try:
            helper = await kb_manager.get_kb_by_name(kb_name)
        except Exception as e:
            logger.warning("[Quill KB] 定位内置知识库 %s 失败: %s", kb_name, e)
            return []
        if helper is None:
            return []
        try:
            docs = await helper.list_documents(limit=int(limit) or 200)
        except Exception as e:
            logger.warning("[Quill KB] 列举内置知识库 %s 的文档失败: %s", kb_name, e, exc_info=True)
            return []
        out: list[dict] = []
        for d in docs or []:
            doc_name = str(getattr(d, "doc_name", "") or "")
            if not doc_name:
                continue
            created = getattr(d, "created_at", None)
            out.append({
                "doc_id": str(getattr(d, "doc_id", "") or ""),
                "doc_name": doc_name,
                "file_type": str(getattr(d, "file_type", "") or ""),
                "file_size": int(getattr(d, "file_size", 0) or 0),
                "chunk_count": int(getattr(d, "chunk_count", 0) or 0),
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
            })
        return out

    async def _auto_kb_names(self) -> list[str]:
        """auto 模式的库名单：全部**非空**且可检索的库。

        空库（chunk_count=0）跳过——retrieve 空跑一遍纯属浪费；init_error
        非空的库 retrieve 会整体 ValueError，这里提前剔除。
        """
        names: list[str] = []
        for info in await self.list_kbs():
            if info.get("init_error"):
                continue
            if int(info.get("chunk_count", 0) or 0) <= 0:
                continue
            names.append(info["kb_name"])
        return names

    async def search(self, query: str, kb_names: Optional[list[str]] = None,
                     top_k: Optional[int] = None,
                     doc_filters: Optional[dict] = None) -> RagResult:
        """检索内置知识库，结果映射为插件 Doc RAG 的 dict 形状。

        Args:
            query: 查询文本
            kb_names: 指定库名列表；None = auto（全部非空库）
            top_k: 本次注入条数；None = 构造时的 top_k
            doc_filters: 文档级过滤 ``{库名: None(整库) | [文档名, ...]}``。
                来自 :func:`split_bound_ex`。kb_manager 的检索接口只按
                「库」召回，没有文档级过滤参数，所以这里把候选池放大到
                ``KB_DOC_FILTER_FETCH`` 再按 ``doc_name`` 过滤，最后截回
                ``top_k``。任意库带文档级过滤时才放大候选池（整库绑定
                保持原有开销）。

        Returns:
            RagResult: 空结果可能是「真没找到」也可能是「功能不可达」，
            通过 ``rag_ok(result)`` 区分（不可达按失败计，进健康度）。
        """
        if not query or not query.strip():
            return RagResult([])
        if not self.available:
            return _rag_failed("AstrBot 内置知识库不可用（kb_manager 缺失或功能未启用）")

        kb_manager = getattr(self.context, "kb_manager", None)
        names = list(kb_names) if kb_names is not None else await self._auto_kb_names()
        if not names:
            return RagResult([])

        final_k = int(top_k) if top_k else self.top_k
        filters: dict = {}
        for name, docs in (doc_filters or {}).items():
            if docs is None:
                filters[str(name)] = None
            else:
                filters[str(name)] = {str(d) for d in docs}
        need_filter = any(v is not None for v in filters.values())
        fetch_k = max(final_k, KB_DOC_FILTER_FETCH) if need_filter else final_k

        try:
            resp = await kb_manager.retrieve(
                query=query,
                kb_names=names,
                top_k_fusion=fetch_k,
                top_m_final=fetch_k,
            )
        except ValueError as e:
            # kb_manager 语义：请求的库全部不可用（init_error）。对注入侧
            # 等价于「本轮无内置库内容」，但按失败计（与本地 embedding 故障
            # 同口径），让健康度能观察到。
            logger.warning("[Quill KB] 内置知识库全部不可用: %s", e)
            return _rag_failed(str(e))
        except Exception as e:
            logger.warning("[Quill KB] 内置知识库检索失败: %s", e, exc_info=True)
            return _rag_failed(str(e))

        # None=检索无结果；{}=库名一个都没匹配到。都算「成功但没找到」。
        if not resp:
            return RagResult([])

        mapped: list[dict] = []
        for r in resp.get("results") or []:
            kb_name = str(r.get("kb_name", "") or "")
            content = str(r.get("content", "") or "")
            if not content:
                continue
            mapped.append({
                "content": content,
                "source": KB_SOURCE_PREFIX + kb_name,
                "chunk_index": int(r.get("chunk_index", 0) or 0),
                "score": float(r.get("score", 0.0) or 0.0),
                # 附加信息：面板检索测试 / 注入报告可选用，注入格式化器忽略
                "kb_name": kb_name,
                "doc_name": str(r.get("doc_name", "") or ""),
            })

        if need_filter:
            # 未出现在 filters 里的库（理论上不会有）按整库保留；显式整库
            # （None）同样全保留，只有列了文档名的库才逐条比对。
            kept: list[dict] = []
            for r in mapped:
                allowed = filters.get(r["kb_name"])
                if allowed is None or r["doc_name"] in allowed:
                    kept.append(r)
            mapped = kept
        if len(mapped) > final_k:
            mapped = mapped[:final_k]
        return RagResult(mapped)


# 直接运行自测：python quill_rag/astrbot_kb.py
# 用一个假 kb_manager 走通 list_kbs / search / split_bound 的映射逻辑，
# 不依赖真实 AstrBot 实例。
if __name__ == "__main__":
    import asyncio

    class _Rec:
        def __init__(self, kb_name, chunk_count=3, doc_count=1):
            self.kb_name = kb_name
            self.kb_id = f"id-{kb_name}"
            self.chunk_count = chunk_count
            self.doc_count = doc_count
            self.description = "测试库"
            self.emoji = "📚"

    class _Doc:
        def __init__(self, doc_name, chunk_count=2):
            self.doc_id = f"doc-{doc_name}"
            self.doc_name = doc_name
            self.file_type = "md"
            self.file_size = 100
            self.chunk_count = chunk_count
            self.created_at = None

    class _Helper:
        async def list_documents(self, offset=0, limit=100, search=None):
            return [_Doc("甲.md"), _Doc("乙.md", 5)]

    class _FakeKBManager:
        def __init__(self):
            self.last_kwargs = {}

        async def list_kbs(self):
            return [_Rec("剧情设定"), _Rec("空库", chunk_count=0)]

        async def get_kb_by_name(self, kb_name):
            return _Helper() if kb_name == "剧情设定" else None

        async def retrieve(self, query, kb_names, top_k_fusion=20, top_m_final=5):
            # 复刻真实语义：请求的库名一个都不存在 → 返回 {}
            self.last_kwargs = {"top_k_fusion": top_k_fusion, "top_m_final": top_m_final}
            existing = [n for n in kb_names if n == "剧情设定"]
            if not existing:
                return {}
            return {
                "context_text": "...",
                "results": [
                    {"kb_name": existing[0], "doc_name": "设定集.md", "chunk_index": 2,
                     "content": "主角喜欢甜豆腐脑。", "score": 0.87, "char_count": 9},
                    {"kb_name": existing[0], "doc_name": "番外.md", "chunk_index": 0,
                     "content": "番外内容。", "score": 0.5, "char_count": 5},
                ],
            }

    class _Ctx:
        kb_manager = _FakeKBManager()

    async def _main():
        src = AstrBotKBSource(_Ctx(), top_k=3)
        assert src.available, "fake context 应可用"
        infos = await src.list_kbs()
        assert [i["source"] for i in infos] == ["kb:剧情设定", "kb:空库"]
        assert infos[0]["kb_id"] == "id-剧情设定", infos[0]

        r = await src.search("豆腐脑")
        assert r.ok and r[0]["source"] == "kb:剧情设定" and r[0]["doc_name"] == "设定集.md"
        assert len(r) == 2, "整库绑定不放大也不截断（fake 返回 2 条）"
        r2 = await src.search("豆腐脑", kb_names=["不存在的库"])
        assert r2.ok and list(r2) == [], "未匹配到库名应为成功空结果"

        # 文档级过滤：只保留绑定文档，且候选池被放大
        r3 = await src.search("豆腐脑", kb_names=["剧情设定"],
                              doc_filters={"剧情设定": ["番外.md"]})
        assert [x["doc_name"] for x in r3] == ["番外.md"], list(r3)
        assert src.context.kb_manager.last_kwargs["top_m_final"] == KB_DOC_FILTER_FETCH

        docs = await src.list_documents("剧情设定")
        assert [d["doc_name"] for d in docs] == ["甲.md", "乙.md"] and docs[1]["chunk_count"] == 5
        assert await src.list_documents("不存在的库") == []

        bare = AstrBotKBSource(object())  # stub 环境：Context 无 kb_manager
        assert not bare.available
        failed = await bare.search("x")
        assert not failed.ok, "不可达应带失败标记"
        assert await bare.list_documents("剧情设定") == []

        p, k = split_bound(["设定.txt", "kb:剧情设定", " kb:空库 ", "", "kb:剧情设定"])
        assert p == ["设定.txt"] and k == ["剧情设定", "空库"], (p, k)

        # 三路拆分：整库 / 文档级 / 混绑（整库优先吸收文档条目）
        p2, k2, f2 = split_bound_ex([
            "设定.txt", "kb:外传", "kb:外传/甲.md", "kb:正传/甲.md", "kb:正传/乙.md",
            "kb:正传/甲.md", "kb:外传/乙.md", "",
        ])
        assert p2 == ["设定.txt"], p2
        assert k2 == ["外传", "正传"], k2
        assert f2["外传"] is None, f2          # 整库条目吸收同库文档条目
        assert f2["正传"] == ["甲.md", "乙.md"], f2
        assert kb_doc_source("正传", "甲.md") == "kb:正传/甲.md"
        assert parse_kb_source("kb:正传/甲.md") == ("正传", "甲.md")
        assert parse_kb_source("kb:正传") == ("正传", "")
        assert parse_kb_source("设定.txt") == ("", "")
        assert parse_kb_source("kb:库/docs/a.md") == ("库", "docs/a.md")

        print("astrbot_kb 自测通过")

    asyncio.run(_main())
