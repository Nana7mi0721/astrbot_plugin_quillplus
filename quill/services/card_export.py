# -*- coding: utf-8 -*-
"""角色卡导出：把 Quill 角色卡与选定的世界书组装成 CCv2 / CCv3 负载（M4.2）。

这一层是纯函数：不碰文件、不碰 AstrBot、不读头像。路由层负责取 persona、
取世界书与头像字节，再把这里的结果交给 persona_manager 做 PNG 封装。
保持纯函数是为了让「导出预览」（只算字节、不下载）和真正导出走同一条路径——
预览与下载不一致的导出器，比没有预览更糟。
"""

from __future__ import annotations

import json
import re
from typing import Any

SPEC_BY_VERSION = {
    "v2": ("chara_card_v2", "2.0"),
    "v3": ("chara_card_v3", "3.0"),
}

# 导入时能识别、且属于 CCv2/V3 标准元数据的字段：导出时原样回写。
# 其余 preserved.data（例如 Pygmalion 的 char_name）不往规范字段里塞，
# 统一放进 extensions.quillplus.preserved，避免把别名键污染成非法 CCv2 字段。
_RESTORE_KEYS = (
    "creator",
    "character_version",
    "tags",
    "creator_notes",
    "nickname",
    "group_only_greetings",
    "source",
    "creator_notes_multilingual",
    "creation_date",
    "modification_date",
    "assets",
)

_QEXT_KEYS = (
    "wb_mode",
    "bound_worldbooks",
    "rag_mode",
    "bound_rag_docs",
    "wr_mode",
    "bound_writing_resource",
    "wr_preset_id",
)

_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _clean_extensions(raw: Any) -> dict:
    """只保留已知的绑定字段，并丢掉像绝对路径的值。

    导出物会离开这台机器：头像路径、文档路径写进去既没用，也泄露本地目录结构。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in _QEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and not _ABS_PATH_RE.match(value):
                out[key] = value
        elif isinstance(value, list):
            items = []
            for item in value:
                text = _text(item).strip()
                if text and not _ABS_PATH_RE.match(text):
                    items.append(text)
            out[key] = items
    return out


def _section_split_personality(personality: str) -> tuple[str, str, str]:
    """把导入时并入 personality 的【系统提示】/【后置指令】拆回独立字段。

    M4.1 的导入侧为了不丢字段，把这两个字段按固定标记拼进人格正文。导出侧
    不拆回去，往返一次就会把系统提示变成角色设定的一部分——字段在两边都「有」，
    语义已经变了。
    """
    text = personality or ""
    marker = re.compile(r"(?:^|\r?\n)[ \t]*【(系统提示|后置指令)】[ \t]*\r?\n")
    matches = list(marker.finditer(text))
    if not matches:
        return text.strip(), "", ""
    base = text[: matches[0].start()].strip()
    buckets = {"系统提示": "", "后置指令": ""}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[match.end(): end].strip()
        if chunk:
            name = match.group(1)
            buckets[name] = (buckets[name] + "\n\n" + chunk).strip()
    return base, buckets["系统提示"], buckets["后置指令"]


def _section_split_examples(examples: str) -> tuple[str, list[str]]:
    """把【备选开场白】从 mes_example 里拆回 alternate_greetings。

    导入侧用空行连接多个开场白，所以这里也按空行切；单个开场白内部若本来就有
    空行，会被切开——这是旧格式的已知损失，报告里不静默，由 warnings 说明。
    """
    text = examples or ""
    marker = re.compile(r"(?:^|\r?\n)[ \t]*【备选开场白】[ \t]*\r?\n")
    match = marker.search(text)
    if not match:
        return text.strip(), []
    base = text[: match.start()].strip()
    body = text[match.end():]
    greetings = [item.strip() for item in re.split(r"\r?\n\s*\r?\n", body) if item.strip()]
    return base, greetings


def _description(persona: dict, personality: str) -> str:
    """description 与 personality 的关系沿用 M9 的约定。

    导入后 summary 只是 personality 的前缀；如果用户在面板单独改了 summary，
    它是一段独立简介，导出时两段都保留。
    """
    summary = _text(persona.get("summary")).strip()
    body = (personality or "").strip()
    if body and summary:
        return body if body.startswith(summary) else (summary + "\n\n" + body).strip()
    return body or summary


def _entry_payload(entry: dict, index: int, book_name: str) -> dict:
    inject = entry.get("inject_position")
    try:
        inject = int(inject or 0)
    except (TypeError, ValueError):
        inject = 0
    def _int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    def _bool(value, default=False):
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    keys = entry.get("keys")
    secondary = entry.get("secondary_keys", entry.get("keys_secondary"))
    secondary = [str(key) for key in secondary if _text(key).strip()] if isinstance(secondary, list) else []
    priority = _int(entry.get("priority", entry.get("order", 10)), 10)
    depth = _int(entry.get("depth", 4), 4)
    role = str(entry.get("role") or "system").strip().lower()
    if role not in ("system", "user", "assistant"):
        role = "system"
    quill_extra = {
        "is_constant": _bool(entry.get("is_constant")),
        "inject_position": inject,
        "condition": _text(entry.get("condition")),
        "depth": depth,
        "role": role,
        "sticky": _int(entry.get("sticky", entry.get("sticky_rounds", 0)), 0),
        "cooldown": _int(entry.get("cooldown", entry.get("cooldown_rounds", 0)), 0),
        "group": _text(entry.get("group")),
        "group_max": _int(entry.get("group_max", 1), 1),
        "group_weight": _int(entry.get("group_weight", 100), 100),
        "priority": priority,
        "recursive": _bool(entry.get("recursive")),
        "prevent_recursion": _bool(entry.get("prevent_recursion")),
        "scan_depth": _int(entry.get("scan_depth", entry.get("scan_window", 0)), 0),
    }
    return {
        "id": index,
        "keys": [str(key) for key in keys if _text(key).strip()] if isinstance(keys, list) else [],
        "secondary_keys": secondary,
        "selective": bool(secondary),
        "constant": _bool(entry.get("is_constant")),
        "content": _text(entry.get("content")),
        "enabled": _bool(entry.get("enabled", True), True),
        "insertion_order": priority,
        "case_sensitive": False,
        "name": _text(entry.get("title")).strip() or f"条目 {index + 1}",
        "priority": priority,
        "depth": depth,
        "role": role,
        "probability": _int(entry.get("probability", 100), 100),
        "useProbability": _bool(entry.get("use_probability", entry.get("useProbability", True)), True),
        "position": "before_char" if inject == 1 else "after_char",
        "extensions": {
            "quillplus": {
                "source_book": book_name,
                "source_entry_id": _text(entry.get("id")),
                **quill_extra,
            }
        },
    }


def build_character_book(persona_name: str, books: list[dict] | None) -> tuple[dict | None, list[str]]:
    """把选定的世界书合成一个 character_book。

    多本书不合并条目：每本书的来源写进条目扩展，局部 ID 从 0 重排——CCv2 的
    character_book.entries 只有一张表，靠 extensions.quillplus.source_book 仍能
    还原「这条来自哪本书」。同名条目不会被静默去重。
    """
    warnings: list[str] = []
    if not books:
        return None, warnings
    entries: list[dict] = []
    sources: list[str] = []
    seen: dict[str, str] = {}
    seen_books: set[str] = set()
    for book in books:
        book_name = _text(book.get("name")).strip()
        if not book_name or book_name in seen_books:
            continue
        seen_books.add(book_name)
        sources.append(book_name)
        for entry in book.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            content = _text(entry.get("content")).strip()
            if not content:
                continue
            title = _text(entry.get("title")).strip() or f"条目 {len(entries) + 1}"
            if title in seen:
                if seen[title] != content:
                    warnings.append(
                        f"不同世界书都有名为「{title}」的条目且内容不同，已分别保留（未合并）"
                    )
            else:
                seen[title] = content
            entries.append(_entry_payload(entry, len(entries), book_name))
    if not entries:
        warnings.append("选择的世界书里没有可导出的条目（空书或条目内容为空）")
        return None, warnings
    label = persona_name or "角色"
    name = f"{label} 的世界书" if len(sources) == 1 else f"{label} 的世界书（合并 {len(sources)} 本）"
    return {
        "name": name,
        "description": "由 QuillPlus 从世界书导出。",
        "extensions": {"quillplus": {"sources": sources, "entry_count": len(entries)}},
        "entries": entries,
    }, warnings


def build_card(persona: dict, version: str = "v2", books: list[dict] | None = None,
               include_id: bool = True) -> tuple[dict, list[str]]:
    """构建完整的卡负载（含 spec 外壳）；返回 (payload, warnings)。"""
    version = (version or "v2").lower()
    if version not in SPEC_BY_VERSION:
        raise ValueError("版本只支持 v2 / v3")
    spec, spec_version = SPEC_BY_VERSION[version]
    name = _text(persona.get("name")).strip() or "未命名角色"
    core = persona.get("core_prompts") or {}
    personality_raw = _text(core.get("personality"))
    examples_raw = _text(core.get("examples_of_dialogue"))
    personality, system_prompt, post_history = _section_split_personality(personality_raw)
    # M5.3：新卡把 system_prompt / PHI 独立保存；旧卡仍可能把标记拼在
    # personality 里。独立字段优先，标记拆分只作兼容回退。
    system_prompt = _text(persona.get("system_prompt") or core.get("system_prompt")).strip() or system_prompt
    post_history = _text(
        persona.get("post_history_instructions")
        or core.get("post_history_instructions")
    ).strip() or post_history
    examples, alternate_greetings = _section_split_examples(examples_raw)
    warnings: list[str] = []
    if alternate_greetings:
        warnings.append(
            "备选开场白按空行拆分回独立条目；单个开场白内部原本含空行的，会被拆成两条"
        )

    source = persona.get("card_source") if isinstance(persona.get("card_source"), dict) else {}
    preserved = source.get("preserved") if isinstance(source.get("preserved"), dict) else {}
    preserved_data = preserved.get("data") if isinstance(preserved.get("data"), dict) else {}
    meta = {key: preserved_data[key] for key in _RESTORE_KEYS if key in preserved_data}
    extra_extensions = preserved_data.get("extensions")
    if not isinstance(extra_extensions, dict):
        extra_extensions = {}
    extra_extensions = {
        str(key): value for key, value in extra_extensions.items()
        if str(key) != "quillplus" and value not in (None, "", [], {})
    }
    leftovers = {
        key: value for key, value in preserved_data.items()
        if key not in _RESTORE_KEYS and key not in ("extensions",) and value not in (None, "", [], {})
    }

    character_book, book_warnings = build_character_book(name, books)
    warnings.extend(book_warnings)

    quillplus: dict = {}
    if include_id:
        pid = _text(persona.get("id")).strip()
        if pid:
            quillplus["id"] = pid
    quillplus["quill_extensions"] = _clean_extensions(persona.get("quill_extensions"))
    if leftovers:
        quillplus["preserved"] = leftovers

    data: dict = {
        "name": name,
        "description": _description(persona, personality),
        "personality": personality,
        "scenario": _text(core.get("scenario")).strip(),
        "first_mes": _text(core.get("first_message")).strip(),
        "mes_example": examples,
        "creator_notes": _text(meta.get("creator_notes", "")),
        "system_prompt": system_prompt,
        "post_history_instructions": post_history,
        "alternate_greetings": alternate_greetings,
        "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
        "creator": _text(meta.get("creator", "Quill Plugin")),
        "character_version": _text(meta.get("character_version", "1.0")),
        "extensions": {"quillplus": quillplus, **extra_extensions},
    }
    # 没选世界书时整条键都不写：CCv2/v3 里 character_book 是可选的，写成 null
    # 有些导入端会当「有世界书」处理，反而比缺键更容易出岔子。
    if character_book is not None:
        data["character_book"] = character_book
    if version == "v3":
        source_value = meta.get("source", [])
        if isinstance(source_value, str):
            # M6 P1-10：V3 规范 source 是字符串数组；旧卡可能存成单个 URL。
            source_value = [source_value] if source_value.strip() else []
        elif not isinstance(source_value, list):
            source_value = []
        data.update({
            # V3 元数据原样回写：nickname 规范上是字符串，但第三方卡常塞多语言
            # 对象；_text() 会把它变成 "{'zh': ...}" 这种 Python repr。
            "nickname": meta.get("nickname", ""),
            "group_only_greetings": meta.get("group_only_greetings")
            if isinstance(meta.get("group_only_greetings"), list) else [],
            "creator_notes_multilingual": meta.get("creator_notes_multilingual")
            if isinstance(meta.get("creator_notes_multilingual"), dict) else {},
            "source": source_value,
            "assets": meta.get("assets") if isinstance(meta.get("assets"), list) else [],
        })
        # 缺失日期省略而不是填 0：0 会被部分前端渲染成 1970-01-01。
        for key in ("creation_date", "modification_date"):
            value = meta.get(key)
            if value not in (None, "", 0):
                data[key] = value
    return {"spec": spec, "spec_version": spec_version, "data": data}, warnings


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def filename(persona: dict, version: str, container: str) -> str:
    name = _text(persona.get("name")).strip() or "角色卡"
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)[:60].strip(" .") or "角色卡"
    suffix = "png" if (container or "").lower() == "png" else "json"
    return f"{safe}_{(version or 'v2').lower()}.{suffix}"


def conflicts(persona: dict, personas: list[dict]) -> list[dict]:
    """预览用：同 id / 同名会导致什么（导入方看到的东西）。"""
    pid = _text(persona.get("id")).strip()
    name = _text(persona.get("name")).strip()
    hits: list[dict] = []
    if pid:
        for item in personas or []:
            # 导出对象本身也在 load_all() 里：同 id 同名的就是它自己，
            # 报「同身份键已存在」会让每次预览都出现一条假警告。
            if _text(item.get("id")).strip() == pid and _text(item.get("name")).strip() == name:
                continue
            if _text(item.get("id")).strip() == pid:
                hits.append({"type": "same_id", "id": pid, "name": _text(item.get("name"))})
                break
    if name:
        for item in personas or []:
            if _text(item.get("name")).strip() == name and _text(item.get("id")).strip() != pid:
                hits.append({"type": "same_name", "id": _text(item.get("id")), "name": name})
                break
    return hits
