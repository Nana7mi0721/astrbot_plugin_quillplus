# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 按语义预算与段稳定性诊断；不声称知道供应商最终 token 前缀。"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CacheBlock:
    name: str
    content: str
    priority: int = 10  # 与旧 PromptSection 一样越大越先裁；这里由 protected 显式保护，0 不自带保护。
    protected: bool = False
    sensitive: bool = False


def fit_blocks(blocks: list[CacheBlock], budget: int) -> tuple[list[CacheBlock], list[dict]]:
    """只裁可丢的数据；数值越大越先丢，受保护段不裁；不按物理尾部盲切核心。"""
    kept = [block for block in blocks if block.content]
    events = []
    size = lambda values: sum(len(b.content) + 2 for b in values)
    while size(kept) > max(0, budget):
        candidates = [(b.priority, i) for i, b in enumerate(kept) if not b.protected]
        if not candidates:
            events.append({"where": "cache_budget", "kind": "protected_floor_reached",
                           "remaining_overflow_chars": size(kept) - max(0, budget),
                           "core_memory_protected": any(b.name == "rag_core_memory" for b in kept)})
            break
        _, index = max(candidates)
        block = kept[index]
        # 检索段优先裁末尾完整条目；单条过长才裁正文，始终保留外层 XML/辅助包装闭合。
        match = re.fullmatch(r'([\s\S]*?<(documents|memories)>\n)([\s\S]*?)(\n</\2>[\s\S]*)',block.content) if block.name in ('rag_documents','rag_memories') else None
        if match:
            target = len(block.content) - (size(kept) - max(0,budget))
            head, body, tail = match[1], match[3], match[4]
            room = target - len(head) - len(tail)
            units = re.split(r'(?m)(?=^\s*\[\d+\]\s+\[)',body)
            units = [unit for unit in units if unit.strip()]
            while len(units)>1 and len(''.join(units))>room:
                units.pop()
            shortened = ''.join(units).rstrip()
            marker = '\n[此条检索正文因预算截断]'
            if len(shortened)>room and room >= 100:
                shortened = shortened[:room-len(marker)].rstrip()+marker
            if shortened and len(shortened)<=room:
                kept[index] = replace(block,content=head+shortened+tail)
                events.append({'where':'cache_budget','kind':'section_trimmed','section':block.name,
                               'dropped_chars':len(block.content)-len(kept[index].content),
                               'retained_items':len(re.findall(r'(?m)^\s*\[\d+\]\s+\[',shortened))})
                continue
        # 数据整段丢弃，保护 XML 与上下文包装的完整性。
        kept.pop(index)
        events.append({"where": "cache_budget", "kind": "section_dropped", "section": block.name,
                       "dropped_chars": len(block.content), "core_memory_protected": any(b.name == "rag_core_memory" for b in kept)})
    return kept, events


def stable_seed(scope: str, persona: str, round_number: int, window: int) -> str:
    bucket = max(0, round_number - 1) // max(1, window)
    return hashlib.sha256(f"{scope}\0{persona}\0{bucket}".encode()).hexdigest()


class SegmentProfiler:
    def __init__(self, limit=250):
        self.limit = limit
        self._previous = OrderedDict()

    def observe(self, identity: str, blocks: list[CacheBlock]) -> dict:
        now = [{"name": block.name, "chars": len(block.content),
                "sha256": hashlib.sha256(block.content.encode()).hexdigest()}
               for block in blocks if block.content]
        previous = self._previous.pop(identity, None)
        self._previous[identity] = now
        while len(self._previous) > self.limit:
            self._previous.popitem(last=False)
        old = {item["name"]: item for item in previous or []}
        changes = []
        for item in now:
            prior = old.pop(item["name"], None)
            changes.append({"name": item["name"], "chars": item["chars"],
                            "previous_chars": prior['chars'] if prior else None,
                            "change": "added" if prior is None else
                            ("unchanged" if prior["sha256"] == item["sha256"] else "changed")})
        changes.extend({"name": name, "chars": 0, "change": "removed"} for name in old)
        return {"measurement": "plugin_segments", "has_previous": previous is not None,
                "order_changed": previous is not None and [x["name"] for x in previous] != [x["name"] for x in now],
                "segments": changes,
                "note": "比较插件装配段的长度/摘要，不是最终 payload、真实 token 前缀或缓存命中率。"}
