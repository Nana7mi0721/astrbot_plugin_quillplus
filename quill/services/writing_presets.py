# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作风格预设：显式选择、互斥维度与固定预算。

M5.2 的边界：这里只做「资源绑定 + 受限选择」，不推断文风、不调用模型。
预设按维度各选一条素材（叙述视角 / 对白 / 用词 / 节奏 / 感官），
提示词装配时只注入被选中的固定素材，未选中的同维度素材不再参与常驻
或关键词匹配，从结构上避免「第三人称 + 第一人称」这类相反指令同时出现。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple


# 维度顺序就是固定素材的注入顺序；每个维度只允许选一条。
DIMENSIONS: List[Dict[str, Any]] = [
    {
        "id": "perspective",
        "label": "叙述视角",
        "hint": "第三人称侧写或第一人称内省，二选一",
        "options": ["style_side", "style_pov"],
    },
    {
        "id": "dialogue",
        "label": "对白密度",
        "hint": "对白与动作描写的配比",
        "options": ["style_dialogue"],
    },
    {
        "id": "diction",
        "label": "用词与句式",
        "hint": "词汇、句长与修辞密度",
        "options": ["style_vocab"],
    },
    {
        "id": "pacing",
        "label": "叙事节奏",
        "hint": "段落推进速度与停顿",
        "options": ["style_pacing"],
    },
    {
        "id": "sensory",
        "label": "感官侧重",
        "hint": "优先调动的感官通道",
        "options": ["sensory_touch", "sensory_temp", "sensory_sound", "sensory_scent", "sensory_taste"],
    },
]

_DIM_BY_ID = {d["id"]: d for d in DIMENSIONS}
_OPTION_IDS = {opt for d in DIMENSIONS for opt in d["options"]}


def _preset(name: str, description: str, choices: Dict[str, str],
            max_entries: int = 5, max_chars: int = 2600) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "choices": dict(choices),
        "budget": {"max_entries": int(max_entries), "max_chars": int(max_chars)},
    }


# 内置预设是只读模板；用户只能显式选择，不能在运行时被模型改写。
BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "balanced": _preset(
        "均衡叙事",
        "第三人称、对白与动作均衡、温度感官打底，适合多数长期剧情。",
        {"perspective": "style_side", "dialogue": "style_dialogue",
         "diction": "style_vocab", "pacing": "style_pacing", "sensory": "sensory_temp"},
    ),
    "cinematic": _preset(
        "电影感",
        "第三人称镜头感，声音与空间细节优先，适合场景推进和群像。",
        {"perspective": "style_side", "dialogue": "style_dialogue",
         "diction": "style_vocab", "pacing": "style_pacing", "sensory": "sensory_sound"},
    ),
    "introspective": _preset(
        "内省流",
        "第一人称心理独白，触觉与身体感受优先，适合慢节奏关系戏。",
        {"perspective": "style_pov", "dialogue": "style_dialogue",
         "diction": "style_vocab", "pacing": "style_pacing", "sensory": "sensory_touch"},
    ),
    "restrained": _preset(
        "克制留白",
        "第三人称、气味与余韵收束，少直给，适合含蓄和悬疑氛围。",
        {"perspective": "style_side", "dialogue": "style_dialogue",
         "diction": "style_vocab", "pacing": "style_pacing", "sensory": "sensory_scent"},
    ),
}


def _clean_choice(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class WritingPresetStore:
    """全局预设选择 + 内置模板的只读视图（落盘只为记住显式选择）。"""

    def __init__(self, path: str):
        self.path = path
        self.global_preset = ""
        self.load_error = ""
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        try:
            await asyncio.to_thread(self._load_sync)
        except Exception as exc:  # 读取失败不阻断素材库；面板会报告真实原因
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.global_preset = ""

    def _load_sync(self) -> None:
        if not self.path or self.path == ":memory:":
            return
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("preset file root is not an object")
        selected = _clean_choice(raw.get("global_preset"))
        self.global_preset = selected if selected in BUILTIN_PRESETS else ""
        self.load_error = ""

    async def set_global(self, preset_id: str) -> Dict[str, Any]:
        selected = _clean_choice(preset_id)
        if selected and selected not in BUILTIN_PRESETS:
            raise ValueError("未知写作预设: %s" % selected)
        async with self._lock:
            self.global_preset = selected
            if self.path and self.path != ":memory:":
                await asyncio.to_thread(self._write_sync)
        return self.payload()

    def _write_sync(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "global_preset": self.global_preset}, fh,
                      ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)

    def list_presets(self) -> List[Dict[str, Any]]:
        rows = []
        for pid, preset in BUILTIN_PRESETS.items():
            rows.append({
                "id": pid,
                "name": preset["name"],
                "description": preset["description"],
                "choices": dict(preset["choices"]),
                "budget": dict(preset["budget"]),
            })
        return rows

    def payload(self) -> Dict[str, Any]:
        return {
            "global_preset": self.global_preset,
            "presets": self.list_presets(),
            "dimensions": [dict(d) for d in DIMENSIONS],
            "load_error": self.load_error,
        }

    def resolve(self, persona_choice: str = "") -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """按角色覆盖 > 全局默认解析；返回 (预设, 诊断)。

        空字符串 / inherit 表示跟随全局；__off__ 表示该角色显式关闭预设。
        未知 id 不静默回退到全局，而是返回 degraded 诊断，让调用方报告真实原因。
        """
        choice = _clean_choice(persona_choice)
        if choice == "__off__":
            return None, {"source": "persona", "reason": "persona_off"}
        if choice and choice != "inherit":
            preset = BUILTIN_PRESETS.get(choice)
            if not preset:
                return None, {"source": "persona", "reason": "unknown_preset", "preset_id": choice}
            return self._materialize(choice, preset), {"source": "persona", "preset_id": choice}
        if not self.global_preset:
            return None, {"source": "global", "reason": "global_off"}
        preset = BUILTIN_PRESETS.get(self.global_preset)
        if not preset:
            return None, {"source": "global", "reason": "unknown_preset",
                          "preset_id": self.global_preset}
        return self._materialize(self.global_preset, preset), {
            "source": "global", "preset_id": self.global_preset}

    @staticmethod
    def _materialize(preset_id: str, preset: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": preset_id,
            "name": preset["name"],
            "description": preset["description"],
            "choices": dict(preset["choices"]),
            "budget": dict(preset["budget"]),
            "dimension_options": {
                d["id"]: list(d["options"]) for d in DIMENSIONS
            },
        }


def option_entry_ids() -> set:
    """所有维度候选素材的 id；装配时这些素材只能由预设选中，不能被关键词另塞。"""
    return set(_OPTION_IDS)
