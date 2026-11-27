# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M6.4 检索评估集、指标与快照 Diff。

评估默认关闭付费 LLM 重排；文档、动态记忆、世界书三路分别计算
Hit Rate / MRR / 平均延迟，避免把不同来源混成一个“总分”。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROUTES = ("doc", "memory", "worldbook")
_PREFIX = {"doc": ("doc:", "kb:", "grp:"), "memory": ("mem:",), "worldbook": ("wb:",)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    return Path(root) / "evals" / "index.json"


def load_cases(root: str | Path) -> list[dict]:
    path = cases_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cases = data.get("cases") if isinstance(data, dict) else data
    return [c for c in (cases or []) if isinstance(c, dict) and c.get("case_id")]


def save_cases(root: str | Path, cases: list[dict]) -> None:
    cleaned = []
    for item in cases or []:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "").strip()
        question = str(item.get("question") or "").strip()
        if not case_id or not question:
            continue
        cleaned.append({
            "case_id": case_id,
            "question": question,
            "scope_id": str(item.get("scope_id") or ""),
            "persona_id": str(item.get("persona_id") or ""),
            "expected_sources": [
                str(src).strip() for src in (item.get("expected_sources") or [])
                if str(src).strip()
            ],
            "tags": [str(tag) for tag in (item.get("tags") or []) if str(tag).strip()],
            "notes": str(item.get("notes") or ""),
            "created_at": str(item.get("created_at") or _now_iso()),
        })
    _atomic_write(cases_path(root), {"cases": cleaned, "updated_at": _now_iso()})


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


def _match(expected: str, actual: str) -> bool:
    expected, actual = str(expected or "").lower(), str(actual or "").lower()
    if not expected or not actual:
        return False
    return expected == actual or expected in actual or actual in expected


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
        except Exception as exc:
            out["doc"].append({"error": type(exc).__name__})
    out["doc_ms"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    if (retriever and getattr(retriever, "memory_store", None)
            and getattr(retriever, "embedding", None) and session_id):
        try:
            emb = await retriever.embedding.embed([question])
            rows = await retriever.memory_store.search(
                session_id, emb[0], top_k=max(3, int(getattr(retriever, "top_k", 3) or 3)),
                query_text=question)
            analysis = temporal_query.analyze(question)
            if rows and (analysis.get("kind") != "none" or analysis.get("entities")):
                rows = temporal_query.rerank(rows, analysis, top_k=len(rows))
            out["memory"] = [
                {"source": str(row.get("record_id") or row.get("id") or ""),
                 "score": row.get("rrf_score")}
                for row in rows
            ]
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
        except Exception as exc:
            out["worldbook"] = [{"error": type(exc).__name__}]
    else:
        out["worldbook"] = []
    out["worldbook_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out


async def _maybe_thread(fn, *args, **kwargs):
    import asyncio
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _route_metric(expected: list[str], actual: list[dict], latency: float) -> dict:
    if not expected:
        return {"hit_rate": None, "mrr": None, "avg_latency_ms": latency,
                "expected": 0}
    sources = [str(row.get("source") or "") for row in actual if not row.get("error")]
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


async def run_eval(retriever, wb_manager, cases: list[dict], *,
                   root: str | Path, keep: int = 5, handle=None,
                   allow_llm_fallback: bool = False,
                   doc_groups=None) -> dict:
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
    snapshots = load_snapshots(root)
    previous = snapshots[-1] if snapshots else None
    payload = {
        "created_at": _now_iso(),
        "case_count": len(run_rows),
        "settings": settings,
        "allow_llm_fallback": bool(allow_llm_fallback),
        "metrics": _summarize(run_rows),
        "cases": run_rows,
    }
    payload["diff"] = _diff(previous, payload)
    _atomic_write(snapshot_dir(root) / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json", payload)
    _rotate_snapshots(root, keep=keep)
    return payload


def snapshot_dir(root: str | Path) -> Path:
    return Path(root) / "evals" / "snapshots"


def load_snapshots(root: str | Path) -> list[dict]:
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


def _rotate_snapshots(root: str | Path, *, keep: int = 5) -> None:
    files = sorted(snapshot_dir(root).glob("*.json"))
    for path in files[:-max(1, int(keep or 5))]:
        try:
            path.unlink()
        except OSError:
            pass
