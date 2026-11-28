# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M6.4 检索评估集、指标与快照 Diff。

评估默认关闭付费 LLM 重排；文档、动态记忆、世界书三路分别计算
Hit Rate / MRR / 平均延迟，避免把不同来源混成一个“总分”。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROUTES = ("doc", "memory", "worldbook")
_PREFIX = {"doc": ("doc:", "kb:", "grp:"), "memory": ("mem:",), "worldbook": ("wb:",)}

# P2-8：评估索引是单文件 JSON，必须在校验层就限制规模与单字段长度，
# 否则面板可以写入一个几百 MB 的用例问题/证据列表，拖垮读写与快照 Diff。
MAX_CASES = 500
_CASE_ID_MAX = 128
_QUESTION_MAX = 2000
_SCOPE_ID_MAX = 256
_PERSONA_ID_MAX = 256
_EXPECTED_SOURCE_MAX = 512
_EXPECTED_SOURCES_MAX = 50
_TAG_MAX = 64
_TAGS_MAX = 30
_NOTES_MAX = 2000
_CREATED_AT_MAX = 64

# 与 quill_rag.retrieval.RagResult 的失败协议同口径。这里只读属性而不 import
# quill_rag，保持 quill/services 不反向依赖 RAG 组件包。
_RAG_OK_ATTR = "_rag_ok"
_RAG_ERROR_ATTR = "_rag_error"


class EvalRootError(ValueError):
    """评估数据根目录未配置；调用方应返回 503，而不是落到进程 CWD。"""


# 用例与快照各自串行化：读-改-写必须在同一把锁内完成，否则两个面板请求
# 可以互相覆盖；快照锁同时保护「读取上一份 → 写新文件 → 轮转」整段。
_CASES_LOCK = asyncio.Lock()
_SNAPSHOTS_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root_path(root: str | Path) -> Path:
    """校验并返回数据根；空 root 明确失败，禁止 Path("")/CWD 兜底。"""
    if root is None:
        raise EvalRootError("检索评估数据目录未配置")
    try:
        raw = os.fspath(root)
    except TypeError:
        raw = str(root)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    text = str(raw).strip()
    # Path("") == Path(".")：两者都会把评估数据写进进程 CWD，属于同一故障。
    if not text or text == ".":
        raise EvalRootError("检索评估数据目录未配置")
    return Path(text)


def _atomic_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.{time.time_ns()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def cases_path(root: str | Path) -> Path:
    return _root_path(root) / "evals" / "index.json"


def snapshot_dir(root: str | Path) -> Path:
    return _root_path(root) / "evals" / "snapshots"


def _trim(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_str_list(raw, *, item_limit: int, max_items: int) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = _trim(item, item_limit)
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _clean_case(item, *, for_save: bool) -> dict | None:
    if not isinstance(item, dict):
        return None
    case_id = _trim(item.get("case_id"), _CASE_ID_MAX)
    question = _trim(item.get("question"), _QUESTION_MAX)
    if not case_id or not question:
        return None
    created_at = _trim(item.get("created_at"), _CREATED_AT_MAX)
    if for_save and not created_at:
        created_at = _now_iso()
    return {
        "case_id": case_id,
        "question": question,
        "scope_id": _trim(item.get("scope_id"), _SCOPE_ID_MAX),
        "persona_id": _trim(item.get("persona_id"), _PERSONA_ID_MAX),
        "expected_sources": _clean_str_list(
            item.get("expected_sources"),
            item_limit=_EXPECTED_SOURCE_MAX,
            max_items=_EXPECTED_SOURCES_MAX,
        ),
        "tags": _clean_str_list(
            item.get("tags"), item_limit=_TAG_MAX, max_items=_TAGS_MAX),
        "notes": _trim(item.get("notes"), _NOTES_MAX),
        "created_at": created_at,
    }


def _clean_cases(cases) -> list[dict]:
    """规范化并按 case_id 去重；空 question 的条目不计入。"""
    merged: dict[str, dict] = {}
    for item in cases or []:
        cleaned = _clean_case(item, for_save=True)
        if cleaned:
            merged[cleaned["case_id"]] = cleaned
    return list(merged.values())


def _session_id(case: dict) -> str:
    from .scope import memory_session_key
    scope = str(case.get("scope_id") or "").strip()
    persona = str(case.get("persona_id") or "").strip()
    if not scope:
        return persona
    return memory_session_key(scope, persona) if persona else scope


def _expected_for_route(case: dict, route: str, group_resolver=None) -> list[str]:
    out = []
    for src in case.get("expected_sources") or []:
        text = str(src)
        if route == "doc" and text.startswith("grp:") and callable(group_resolver):
            try:
                members = group_resolver(text.split(":", 1)[1])
            except Exception:
                members = None
            out.extend(str(member) for member in (members or []) if str(member).strip())
            continue
        if text.startswith(_PREFIX[route]):
            out.append(text.split(":", 1)[1])
    return out


def _is_word_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char == "_")


def _match(expected: str, actual: str) -> bool:
    """评估来源匹配：只允许 expected 作为 actual 的独立片段。

    双向子串匹配会让 ``"1"`` 命中 ``"10"``、``"10"`` 命中 ``"1"``。这里保留
    「路径/标题包含」的实用性（``foo`` 命中 ``docs/foo.md``），但要求命中片段
    两侧不是字母、数字或下划线，避免编号前缀互相污染。
    """
    expected = str(expected or "").strip().lower()
    actual = str(actual or "").strip().lower()
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    start = 0
    while True:
        index = actual.find(expected, start)
        if index < 0:
            return False
        before = actual[index - 1] if index > 0 else ""
        after_index = index + len(expected)
        after = actual[after_index] if after_index < len(actual) else ""
        if not _is_word_char(before) and not _is_word_char(after):
            return True
        start = index + 1


def _rag_failure(result) -> str:
    """读取 RagResult 的失败标记；普通 list 按成功处理。"""
    if bool(getattr(result, _RAG_OK_ATTR, True)):
        return ""
    return str(getattr(result, _RAG_ERROR_ATTR, "") or "rag_failed")


async def _run_routes(retriever, wb_manager, case: dict, *,
                      allow_llm_fallback: bool = False) -> dict:
    from . import temporal_query
    question = str(case.get("question") or "")
    session_id = _session_id(case)
    out = {}
    started = time.perf_counter()
    out["doc"] = []
    if retriever and getattr(retriever, "embedding", None):
        try:
            docs = await retriever.search_documents(
                question, allow_llm_fallback=allow_llm_fallback)
            out["doc"].extend([
                {"source": str(row.get("source") or ""), "score": row.get("score")}
                for row in docs
            ])
            error = _rag_failure(docs)
            if error:
                out["doc"].append({"error": error})
        except Exception as exc:
            out["doc"].append({"error": type(exc).__name__})
    kb_source = getattr(retriever, "kb_source", None) if retriever else None
    if kb_source is not None and getattr(kb_source, "available", False):
        try:
            kb_rows = await kb_source.search(question)
            out["doc"].extend([
                {"source": str(row.get("source") or ""), "score": row.get("score")}
                for row in kb_rows
            ])
            error = _rag_failure(kb_rows)
            if error:
                out["doc"].append({"error": error})
        except Exception as exc:
            out["doc"].append({"error": type(exc).__name__})
    out["doc_ms"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    if (retriever and getattr(retriever, "memory_store", None)
            and getattr(retriever, "embedding", None) and session_id):
        try:
            emb = await retriever.embedding.embed([question])
            analysis = (
                temporal_query.analyze(question)
                if getattr(getattr(retriever, "config", None),
                           "rag_temporal_recall_enabled", True)
                else {"kind": "none", "entities": []}
            )
            rows = await retriever.memory_store.search(
                session_id, emb[0], top_k=max(3, int(getattr(retriever, "top_k", 3) or 3)),
                query_text=question,
                include_older=temporal_query.requires_older_window(analysis))
            error = _rag_failure(rows)
            if rows and (analysis.get("kind") != "none" or analysis.get("entities")):
                rows = temporal_query.rerank(rows, analysis, top_k=len(rows))
            out["memory"] = [
                {"source": str(row.get("record_id") or row.get("id") or ""),
                 "score": row.get("rrf_score")}
                for row in rows
            ]
            if error:
                out["memory"].append({"error": error})
        except Exception as exc:
            out["memory"] = [{"error": type(exc).__name__}]
    else:
        out["memory"] = []
    out["memory_ms"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    if wb_manager is not None:
        try:
            rows = await _maybe_thread(
                wb_manager.match_entries, question,
                simulate=True, write_trigger_log=False,
                scope_id=str(case.get("scope_id") or ""),
                persona_id=str(case.get("persona_id") or ""),
            )
            out["worldbook"] = [
                {"source": f"{row.get('_source_worldbook', '')}/{row.get('title', '')}",
                 "score": row.get("_match_score")}
                for row in rows
            ]
            error = _rag_failure(rows)
            if error:
                out["worldbook"].append({"error": error})
        except Exception as exc:
            out["worldbook"] = [{"error": type(exc).__name__}]
    else:
        out["worldbook"] = []
    out["worldbook_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out


async def _maybe_thread(fn, *args, **kwargs):
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _route_metric(expected: list[str], actual: list[dict], latency: float) -> dict:
    failures = [
        str(row.get("error"))
        for row in actual
        if isinstance(row, dict) and row.get("error")
    ]
    if failures:
        # P2-7：检索失败不是「0 命中」。指标返回 None 让汇总跳过该用例，
        # 同时把原始失败原因留在快照里供面板排查。
        return {
            "hit_rate": None,
            "mrr": None,
            "avg_latency_ms": latency,
            "expected": len(expected),
            "failed": True,
            "error": "; ".join(failures),
        }
    if not expected:
        return {"hit_rate": None, "mrr": None, "avg_latency_ms": latency,
                "expected": 0}
    sources = [
        str(row.get("source") or "")
        for row in actual
        if isinstance(row, dict) and not row.get("error")
    ]
    ranks = []
    for target in expected:
        rank = next((i + 1 for i, source in enumerate(sources) if _match(target, source)), 0)
        ranks.append(rank)
    hit = sum(1 for rank in ranks if rank > 0)
    mrr = sum((1.0 / rank) for rank in ranks if rank > 0) / len(expected)
    return {"hit_rate": round(hit / len(expected), 4), "mrr": round(mrr, 4),
            "avg_latency_ms": latency, "expected": len(expected)}


def _summarize(rows: list[dict]) -> dict:
    metrics = {}
    for route in ROUTES:
        usable = [r["metrics"][route] for r in rows if r["metrics"][route]["hit_rate"] is not None]
        metrics[route] = {
            "cases": len(usable),
            "hit_rate": round(sum(m["hit_rate"] for m in usable) / len(usable), 4) if usable else None,
            "mrr": round(sum(m["mrr"] for m in usable) / len(usable), 4) if usable else None,
            "avg_latency_ms": round(sum(m["avg_latency_ms"] for m in usable) / len(usable), 2) if usable else None,
        }
    return metrics


def _diff(previous: dict | None, current: dict) -> dict:
    if not previous:
        return {"previous": None, "cases": []}
    prev_cases = {c["case_id"]: c for c in previous.get("cases") or []}
    current_ids = {c.get("case_id") for c in current.get("cases") or []}
    changes = []
    for case in current.get("cases") or []:
        old = prev_cases.get(case["case_id"])
        if not old:
            changes.append({"case_id": case["case_id"], "change": "new"})
            continue
        delta = {}
        for route in ROUTES:
            before = (old.get("results") or {}).get(route) or []
            after = (case.get("results") or {}).get(route) or []
            if before != after:
                delta[route] = {"before": before, "after": after}
        if delta:
            changes.append({"case_id": case["case_id"], "change": "changed", "routes": delta})
    for case_id in prev_cases:
        if case_id not in current_ids:
            changes.append({"case_id": case_id, "change": "removed"})
    return {"previous": previous.get("created_at"), "cases": changes}


async def load_cases(root: str | Path) -> list[dict]:
    """读取用例集；文件 I/O 在线程池中执行并与其他读写串行化。"""
    cases_path(root)  # 先校验 root，空值立即失败
    async with _CASES_LOCK:
        return await asyncio.to_thread(_load_cases_sync, root)


def _load_cases_sync(root: str | Path) -> list[dict]:
    path = cases_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list):
        return []
    out: list[dict] = []
    for item in cases:
        cleaned = _clean_case(item, for_save=False)
        if cleaned:
            out.append(cleaned)
    return out


def _write_cases_sync(root: str | Path, cases) -> list[dict]:
    cleaned = _clean_cases(cases)
    if len(cleaned) > MAX_CASES:
        raise ValueError(f"评估用例最多 {MAX_CASES} 条")
    _atomic_write(cases_path(root), {"cases": cleaned, "updated_at": _now_iso()})
    return cleaned


async def save_cases(root: str | Path, cases: list[dict]) -> list[dict]:
    """覆盖写入用例集；返回实际落盘的合法条目（空 question 已剔除）。"""
    cases_path(root)
    async with _CASES_LOCK:
        return await asyncio.to_thread(_write_cases_sync, root, cases)


def _upsert_cases_sync(root: str | Path, updates: list[dict], max_cases: int) -> list[dict]:
    existing = {case["case_id"]: case for case in _load_cases_sync(root)}
    for item in updates:
        cleaned = _clean_case(item, for_save=True)
        if cleaned:
            existing[cleaned["case_id"]] = cleaned
    merged = list(existing.values())
    if len(merged) > max_cases:
        raise ValueError(
            f"评估用例最多 {max_cases} 条（合并后为 {len(merged)} 条）")
    _atomic_write(cases_path(root), {"cases": merged, "updated_at": _now_iso()})
    return merged


async def upsert_cases(root: str | Path, updates: list[dict], *,
                       max_cases: int = MAX_CASES) -> list[dict]:
    """合并保存用例；整个读-合并-写过程在同一把锁内完成。"""
    cases_path(root)
    if not isinstance(updates, list):
        raise ValueError("cases 必须是数组")
    limit = max(1, min(int(max_cases or MAX_CASES), MAX_CASES))
    async with _CASES_LOCK:
        return await asyncio.to_thread(_upsert_cases_sync, root, updates, limit)


def _delete_case_sync(root: str | Path, case_id: str) -> list[dict]:
    cases = _load_cases_sync(root)
    remaining = [case for case in cases if case.get("case_id") != case_id]
    if len(remaining) != len(cases):
        _atomic_write(cases_path(root), {"cases": remaining, "updated_at": _now_iso()})
    return remaining


async def delete_case(root: str | Path, case_id: str) -> list[dict]:
    cases_path(root)
    async with _CASES_LOCK:
        return await asyncio.to_thread(_delete_case_sync, root, case_id)


async def run_eval(retriever, wb_manager, cases: list[dict], *,
                   root: str | Path, keep: int = 5, handle=None,
                   allow_llm_fallback: bool = False,
                   doc_groups=None) -> dict:
    _root_path(root)  # 空 root 在跑任何检索之前就失败
    cfg = getattr(retriever, "config", None) if retriever else None
    settings = {
        "top_k": int(getattr(retriever, "top_k", 0) or 0) if retriever else 0,
        "dense_top_k": int(getattr(cfg, "rag_dense_top_k", 0) or 0) if cfg else 0,
        "allow_llm_fallback": bool(allow_llm_fallback),
    }
    run_rows = []
    for index, case in enumerate(cases, start=1):
        if handle is not None:
            await handle.checkpoint(index - 1, total=len(cases),
                                    message=f"评估 {case.get('case_id')}")
        started = time.perf_counter()
        actual = await _run_routes(
            retriever, wb_manager, case,
            allow_llm_fallback=allow_llm_fallback)
        results = {}
        metrics = {}
        for route in ROUTES:
            results[route] = actual.get(route) or []
            metrics[route] = _route_metric(
                _expected_for_route(
                    case, route,
                    group_resolver=getattr(doc_groups, "resolve", None)),
                results[route], float(actual.get(f"{route}_ms", 0.0) or 0.0))
        run_rows.append({
            "case_id": case.get("case_id"),
            "question": case.get("question"),
            "tags": case.get("tags") or [],
            "results": results,
            "metrics": metrics,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        })
    payload = {
        "created_at": _now_iso(),
        "case_count": len(run_rows),
        "settings": settings,
        "allow_llm_fallback": bool(allow_llm_fallback),
        "metrics": _summarize(run_rows),
        "cases": run_rows,
    }
    async with _SNAPSHOTS_LOCK:
        await asyncio.to_thread(_persist_snapshot, root, payload, keep)
    return payload


def _load_snapshots_sync(root: str | Path) -> list[dict]:
    out = []
    for path in sorted(snapshot_dir(root).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["_file"] = path.name
                out.append(data)
        except (OSError, ValueError):
            continue
    return out


def _persist_snapshot(root: str | Path, payload: dict, keep: int) -> dict:
    snapshots = _load_snapshots_sync(root)
    previous = snapshots[-1] if snapshots else None
    payload["diff"] = _diff(previous, payload)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    _atomic_write(snapshot_dir(root) / name, payload)
    _rotate_snapshots(root, keep=keep)
    return payload


async def load_snapshots(root: str | Path) -> list[dict]:
    """读取快照；文件 I/O 在线程池中执行并与其他读写串行化。"""
    snapshot_dir(root)
    async with _SNAPSHOTS_LOCK:
        return await asyncio.to_thread(_load_snapshots_sync, root)


def _rotate_snapshots(root: str | Path, *, keep: int = 5) -> None:
    files = sorted(snapshot_dir(root).glob("*.json"))
    for path in files[:-max(1, int(keep or 5))]:
        try:
            path.unlink()
        except OSError:
            pass
