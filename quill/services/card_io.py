# -*- coding: utf-8 -*-
"""角色卡导入的格式识别与字段保真层（M4.1）。

纯逻辑模块：不依赖 AstrBot、不落盘、不访问网络，也不调用 LLM。
输入是已解析好的 JSON 对象或纯文本，输出是「Quill 角色卡字段 + 导入报告」。

为什么要单独成层：导入来源格式多（CCv2 / CCv3 / Tavern V1 / W++ / 纯文本…），
「哪些字段被映射、哪些原样保留、哪些平台特性不支持」必须可解释；把这层从
persona_manager 里拆出来后，M4.1b 加新格式只改映射表，不碰落盘与面板。

严格性约定（M4.1 验收口径）：
- 声明了 spec 的卡必须通过封装校验（spec 已知、data 是对象、spec_version 匹配），
  任何一条不满足都抛 CardFormatError，由路由转成 400 —— 不许把格式错误吞成
  「普通文本导入成功」。
- 只有**根本没有声明任何结构**的普通自然语言文本，才允许低置信回退
  （confidence 0.2~0.4），并在 warnings 里说清楚。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

FORMAT_CCV3 = "ccv3"
FORMAT_CCV2 = "ccv2"
FORMAT_LEGACY_NESTED = "ccv2_legacy"
FORMAT_TAVERN_V1 = "tavern_v1"
FORMAT_QUILL = "quill"
FORMAT_UNKNOWN_JSON = "unknown_json"
FORMAT_WPP = "wpp"
FORMAT_PLAIN_TEXT = "plain_text"
# M4.1b 后批格式：字段名与容器都不属于 CCv2/V3，必须单独识别，不能按 name 猜。
FORMAT_PYGMALION = "pygmalion"
FORMAT_AGNAI = "agnai"
FORMAT_OOBA = "oobabooga_yaml"
FORMAT_SBF = "sbf"

FORMAT_LABELS = {
    FORMAT_CCV3: "CCv3（chara_card_v3）",
    FORMAT_CCV2: "CCv2（chara_card_v2）",
    FORMAT_LEGACY_NESTED: "嵌套 data（无 spec 声明）",
    FORMAT_TAVERN_V1: "Tavern V1（扁平 JSON）",
    FORMAT_QUILL: "Quill 角色卡",
    FORMAT_UNKNOWN_JSON: "未识别 JSON",
    FORMAT_WPP: "W++ / 标签文本",
    FORMAT_PLAIN_TEXT: "纯自然语言文本",
    FORMAT_PYGMALION: "Pygmalion 旧 JSON",
    FORMAT_AGNAI: "Agnai / BYAF JSON",
    FORMAT_OOBA: "oobabooga YAML",
    FORMAT_SBF: "SBF / Boostyle 结构化文本",
}

# spec 值 → 内部格式；只认这两个，其它字符串一律视为「不支持的规格」。
SPEC_FORMATS = {"chara_card_v2": FORMAT_CCV2, "chara_card_v3": FORMAT_CCV3}
# 各 spec 允许的 spec_version 写法（ST 生态里 2 / 2.0 / "2.0" 都见过）。
SPEC_VERSIONS = {FORMAT_CCV2: ("2", "2.0"), FORMAT_CCV3: ("3", "3.0")}

# —— M4.1b：后批格式的字段别名（源字段 → CCv2 规范键）——
# 别名只决定「读哪几个键」，不改写原始 JSON：源键不在 CONSUMED_DATA_KEYS 里，
# 会照常进 preserved，导出时能原样回写。
_PYGMALION_ALIASES = {
    "char_name": "name",
    "char_persona": "personality",
    "char_greeting": "first_mes",
    "world_scenario": "scenario",
    "example_dialogue": "mes_example",
}
_AGNAI_ALIASES = {
    "firstMessage": "first_mes",
    "exampleDialogue": "mes_example",
    "exampleMessages": "mes_example",
    "alternateGreetings": "alternate_greetings",
    "systemPrompt": "system_prompt",
    "postHistoryInstructions": "post_history_instructions",
    "creatorNotes": "creator_notes",
    "characterVersion": "character_version",
}
_ALIASES_BY_FORMAT = {
    FORMAT_PYGMALION: _PYGMALION_ALIASES,
    FORMAT_AGNAI: _AGNAI_ALIASES,
}
_PYGMALION_KEYS = tuple(_PYGMALION_ALIASES)
_AGNAI_KEYS = ("firstMessage", "exampleDialogue", "exampleMessages", "alternateGreetings")


def _apply_aliases(data: dict, fmt: str) -> tuple[dict, dict]:
    """把后批格式的别名键补成规范键；已存在的规范键优先，不覆盖。"""
    aliases = _ALIASES_BY_FORMAT.get(fmt)
    if not aliases:
        return data, {}
    out = dict(data)
    mapped: dict = {}
    for src, dst in aliases.items():
        if src not in out or not _nonempty(out[src]):
            continue
        if _nonempty(out.get(dst)):
            continue
        out[dst] = out[src]
        mapped[src] = DATA_FIELD_TARGETS.get(dst, dst)
    return out, mapped

# 会被消费（映射进 Quill 字段）的 data 键；其余键算「原始字段」，进 card_source。
CONSUMED_DATA_KEYS = frozenset({
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
    "alternate_greetings",
    "extensions",
    "character_book",
})

# 源字段 → Quill 落点。报告里的 mapped_fields 直接引用这张表，面板与 E2E 都能断言。
DATA_FIELD_TARGETS = {
    "name": "name",
    "description": "core_prompts.personality",
    "personality": "core_prompts.personality",
    "scenario": "core_prompts.scenario",
    "first_mes": "core_prompts.first_message",
    "mes_example": "core_prompts.examples_of_dialogue",
    "system_prompt": "system_prompt（受限系统层）",
    "post_history_instructions": "post_history_instructions（近期尾部低权段）",
    "alternate_greetings": "core_prompts.examples_of_dialogue（【备选开场白】段）",
    "character_book": "_character_book（内嵌世界书）",
    "extensions": "quill_extensions（quillplus 命名空间）",
    "creator_notes": "summary（仅当 description 为空时回退）",
    "creator": "card_source.preserved（元数据）",
    "character_version": "card_source.preserved（元数据）",
    "tags": "card_source.preserved（元数据）",
}

# V3 独有、Quill 当前没有运行位置的字段：逐项进 unsupported_features。
# 注意区分语义：preserved = 原值留着可回写；unsupported = 没有对应行为，
# 导入后不会生效（原值仍在 card_source 里，方便用户核对）。
V3_UNSUPPORTED = (
    ("nickname", "V3 昵称（多语言名）：Quill 以 name 为唯一显示名与身份键，昵称不参与运行"),
    ("group_only_greetings", "群聊专用开场白：Quill 没有群聊开场位，仅保留原文"),
    ("assets", "V3 资源清单：导入不下载、不引用外部资源（M4.1 明确不触网）"),
    ("creator_notes_multilingual", "多语言作者说明：按元数据保留，不进提示词"),
    ("source", "卡来源链接：按元数据保留，导入不访问"),
    ("creation_date", "创建日期：按元数据保留，不参与运行"),
    ("modification_date", "修改日期：按元数据保留，不参与运行"),
)

# 单字段 / 总量上限：card_source 存进角色卡 JSON，不能把卡撑爆。
MAX_FIELD_BYTES = 16 * 1024
MAX_PRESERVED_BYTES = 128 * 1024

# <START> 前后可能带换行/空格，统一收成独立的 --- 分隔行（沿用 M2 正则口径）。
_START_RE = re.compile(r"\n?\s*<START>\s*\n?", re.IGNORECASE)


class CardFormatError(ValueError):
    """用户可见的角色卡格式错误（路由转 400，面板直接显示这句话）。"""


def label_for(fmt: str) -> str:
    return FORMAT_LABELS.get(fmt, fmt)


def clean_mes_example(text: str) -> str:
    """ST 的 <START> 分隔符换成 Quill 的 --- 分隔（保留 {{user}} / {{char}} 占位符）。"""
    return _START_RE.sub("\n---\n", text or "").strip()


def _text(value: Any) -> str:
    """把任意 JSON 标量收敛成字符串；容器只在能拼出内容时才拼接。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [_text(item).strip() for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def detect_format(obj: dict) -> str:
    """只看结构，不做校验（校验在 _validate_envelope）。"""
    if "core_prompts" in obj:
        return FORMAT_QUILL
    spec = obj.get("spec")
    if isinstance(spec, str) and spec in SPEC_FORMATS:
        return SPEC_FORMATS[spec]
    if isinstance(obj.get("data"), dict):
        return FORMAT_LEGACY_NESTED
    # 顺序即优先级：带 spec 的永远是权威声明；其次是嵌套 data；再按**特有键**
    # 认后批格式（Pygmalion 的 char_*、Agnai 的 camelCase），最后才是 Tavern V1
    # 的扁平规范键。反过来先判 V1 会把 pygmalion 卡错认成 V1（它也有 name/personality）。
    if any(key in obj for key in _PYGMALION_KEYS):
        return FORMAT_PYGMALION
    if any(key in obj for key in _AGNAI_KEYS):
        return FORMAT_AGNAI
    if any(key in obj for key in ("name", "description", "personality", "scenario", "first_mes", "mes_example")):
        return FORMAT_TAVERN_V1
    return FORMAT_UNKNOWN_JSON


def _validate_envelope(obj: dict) -> None:
    """声明了 spec 就必须对得上；这里抛的错不许在调用方被降级。"""
    if "spec" in obj and obj.get("spec") is not None:
        spec = obj["spec"]
        if not isinstance(spec, str) or spec not in SPEC_FORMATS:
            raise CardFormatError(
                "不支持的角色卡规格：spec=" + repr(spec)
                + "（支持 chara_card_v2 / chara_card_v3）"
            )
        data = obj.get("data")
        if not isinstance(data, dict):
            raise CardFormatError("角色卡声明了 spec=" + spec + "，但 data 字段缺失或不是对象")
        fmt = SPEC_FORMATS[spec]
        version = obj.get("spec_version")
        if version is not None:
            allowed = SPEC_VERSIONS[fmt]
            if str(version).strip() not in allowed:
                raise CardFormatError(
                    "spec_version 与 " + spec + " 不匹配：" + repr(version)
                    + "（期望 " + " / ".join(allowed) + "）"
                )
    elif isinstance(obj.get("data"), dict):
        return
    elif "data" in obj and not isinstance(obj["data"], dict):
        raise CardFormatError("角色卡 data 字段必须是对象 {...}")


def _schema_version(fmt: str, obj: dict) -> str:
    declared = obj.get("spec_version")
    if fmt in (FORMAT_CCV2, FORMAT_CCV3) and declared is not None:
        return str(declared).strip()
    return {
        FORMAT_CCV3: "3.0",
        FORMAT_CCV2: "2.0",
        FORMAT_TAVERN_V1: "1.0",
    }.get(fmt, "")


def _merge_personality(description: str, personality: str) -> str:
    """description 与 personality 合并（M9 起不截断，两个都可能是长文）。"""
    if description and not personality:
        return description
    if description and personality:
        if personality.strip() in description:
            return description
        return description + "\n\n" + personality
    return personality


def _preserve_book(book: dict) -> dict:
    """世界书：保留书级未知字段 + 条目级 extensions 原文（条目正文已走内嵌转换）。"""
    preserved: dict = {}
    for key, value in book.items():
        if key == "entries":
            continue
        if _nonempty(value):
            preserved[key] = value
    entries = book.get("entries")
    if isinstance(entries, dict):
        items = [entries[key] for key in sorted(entries, key=str)]
    elif isinstance(entries, list):
        items = list(entries)
    else:
        items = []
    entry_extensions = {}
    for index, entry in enumerate(items):
        if not isinstance(entry, dict):
            continue
        ext = entry.get("extensions")
        if isinstance(ext, dict) and ext:
            entry_extensions[str(index)] = ext
    if entry_extensions:
        preserved["entry_extensions"] = entry_extensions
    return preserved


def _trim_preserved(preserved: dict, warnings: list) -> dict:
    """超限字段不静默丢：从 preserved 里摘掉，登记到 omitted，并写进 warnings。"""
    omitted = {}
    for scope in ("card", "data"):
        bucket = preserved.get(scope)
        if not isinstance(bucket, dict):
            continue
        for key in list(bucket):
            if _json_bytes(bucket[key]) > MAX_FIELD_BYTES:
                omitted[scope + "." + key] = _json_bytes(bucket[key])
                bucket.pop(key)
    if _json_bytes(preserved) > MAX_PRESERVED_BYTES:
        # 总量还是超：整块 data 级原始字段放弃，只留键名（卡级字段体积小，通常不动）。
        bucket = preserved.get("data") or {}
        for key in list(bucket):
            omitted["data." + key] = _json_bytes(bucket[key])
            bucket.pop(key)
        warnings.append("原始字段总量超过 128KB，已只保留字段名；大字段已记入 omitted")
    if omitted:
        preserved["omitted"] = omitted
        warnings.append("有字段体积过大（>16KB）未随卡保存：" + "、".join(sorted(omitted)))
    return preserved


def _finish(card: dict, fmt: str, schema_version: str, confidence: float,
            mapped: dict, preserved: dict, unsupported: list, warnings: list,
            extensions: dict, via: str) -> dict:
    report = {
        "detected_format": fmt,
        "format_label": label_for(fmt),
        "schema_version": schema_version,
        "confidence": round(float(confidence), 2),
        "mapped_fields": mapped,
        "preserved_fields": sorted(
            ["card." + key for key in (preserved.get("card") or {})]
            + ["data." + key for key in (preserved.get("data") or {})]
            + (["book"] if preserved.get("book") else [])
        ),
        "unsupported_features": unsupported,
        "warnings": warnings,
        "extensions": extensions,
    }
    card["_import_report"] = report
    card["card_source"] = {
        "format": fmt,
        "format_label": report["format_label"],
        "schema_version": schema_version,
        "confidence": report["confidence"],
        "imported_at": _utc_now_iso(),
        "via": via,
        "preserved": preserved,
        "unsupported_features": [item["field"] for item in unsupported],
    }
    return card


def _extension_summary(obj: dict, data: dict) -> dict:
    summary = {"card": [], "book": [], "entries": 0}
    # 卡级 extensions 在 V2/V3 里位于 data.extensions；V1 扁平卡则直接在根上。
    card_ext = data.get("extensions")
    if not isinstance(card_ext, dict):
        card_ext = obj.get("extensions")
    if isinstance(card_ext, dict):
        summary["card"] = sorted(str(key) for key in card_ext)
    book = data.get("character_book")
    if isinstance(book, dict):
        book_ext = book.get("extensions")
        if isinstance(book_ext, dict):
            summary["book"] = sorted(str(key) for key in book_ext)
        entries = book.get("entries")
        items = list(entries.values()) if isinstance(entries, dict) else (entries if isinstance(entries, list) else [])
        summary["entries"] = sum(
            1 for entry in items
            if isinstance(entry, dict) and isinstance(entry.get("extensions"), dict) and entry.get("extensions")
        )
    return summary


def _parse_quill(obj: dict, via: str) -> dict:
    """已经是 Quill 结构（带 core_prompts）：原样收敛，字段一个不丢。"""
    core = obj.get("core_prompts") if isinstance(obj.get("core_prompts"), dict) else {}
    card = {
        "name": _text(obj.get("name")).strip() or "未命名角色",
        "summary": _text(obj.get("summary")),
        "core_prompts": {
            "personality": _text(core.get("personality")),
            "scenario": _text(core.get("scenario")),
            "examples_of_dialogue": _text(core.get("examples_of_dialogue")),
            "first_message": _text(core.get("first_message")),
        },
        "system_prompt": _text(
            obj.get("system_prompt", core.get("system_prompt"))
        ).strip(),
        "post_history_instructions": _text(
            obj.get("post_history_instructions", core.get("post_history_instructions"))
        ).strip(),
    }
    if isinstance(obj.get("id"), str) and obj["id"].strip():
        card["id"] = obj["id"].strip()
    if isinstance(obj.get("quill_extensions"), dict):
        card["quill_extensions"] = obj["quill_extensions"]
    preserved = {"card": {}, "data": {}}
    known = {
        "id", "name", "summary", "core_prompts", "quill_extensions",
        "avatar_path", "card_source", "system_prompt", "post_history_instructions",
    }
    for key, value in obj.items():
        if key not in known and _nonempty(value):
            preserved["card"][key] = value
    mapped = {
        "core_prompts.personality": "core_prompts.personality",
        "core_prompts.scenario": "core_prompts.scenario",
        "core_prompts.first_message": "core_prompts.first_message",
        "core_prompts.examples_of_dialogue": "core_prompts.examples_of_dialogue",
    }
    warnings: list = []
    preserved = _trim_preserved(preserved, warnings)
    return _finish(
        card, FORMAT_QUILL, "", 1.0, mapped, preserved, [],
        warnings, {"card": [], "book": [], "entries": 0}, via,
    )


def parse_json_card(obj: Any, via: str = "json") -> dict:
    """JSON 角色卡 → Quill 字段 + _import_report + card_source。

    via 只用于记录导入入口（json / image / text），不影响解析口径。
    """
    if not isinstance(obj, dict):
        raise CardFormatError("角色卡 JSON 根必须是对象 {...}")
    _validate_envelope(obj)
    fmt = detect_format(obj)
    if fmt == FORMAT_QUILL:
        return _parse_quill(obj, via)

    warnings: list = []
    alias_mapped: dict = {}
    if fmt == FORMAT_UNKNOWN_JSON:
        name_probe = _text(obj.get("name")).strip()
        body_probe = "".join(
            _text(obj.get(key)) for key in ("description", "personality", "scenario", "first_mes", "mes_example")
        )
        if not name_probe and not body_probe.strip():
            raise CardFormatError(
                "未识别的角色卡 JSON：没有 spec / data，也找不到角色名与设定文本"
            )
        data = obj
        confidence = 0.5
        warnings.append("未识别的 JSON 结构：按扁平 V1 字段尽力导入，请核对结果")
    elif fmt == FORMAT_LEGACY_NESTED:
        data = obj["data"]
        confidence = 0.85
        warnings.append("缺少 spec 声明：按嵌套 data 的 V2 结构导入")
    elif fmt in (FORMAT_PYGMALION, FORMAT_AGNAI):
        data = obj
        confidence = 0.75 if fmt == FORMAT_PYGMALION else 0.8
        warnings.append(
            "按 " + label_for(fmt) + " 的字段别名映射导入；未映射字段已原样保留"
        )
    elif fmt == FORMAT_TAVERN_V1:
        data = obj
        confidence = 0.8
    else:
        data = obj.get("data") or {}
        confidence = 1.0

    if not isinstance(data, dict):
        raise CardFormatError("角色卡 data 字段必须是对象 {...}")
    data, alias_mapped = _apply_aliases(data, fmt)

    name = _text(data.get("name")).strip() or "未命名角色"
    description = _text(data.get("description")).strip()
    personality = _merge_personality(description, _text(data.get("personality")).strip())
    scenario = _text(data.get("scenario")).strip()
    first_message = _text(data.get("first_mes")).strip()
    mes_example = clean_mes_example(_text(data.get("mes_example")))

    system_prompt = _text(data.get("system_prompt")).strip()
    post_history = _text(data.get("post_history_instructions")).strip()
    greetings = data.get("alternate_greetings")
    alt_list = [_text(item).strip() for item in greetings] if isinstance(greetings, list) else []
    alt_list = [item for item in alt_list if item]
    if alt_list:
        mes_example = (mes_example + "\n\n【备选开场白】\n" + "\n\n".join(alt_list)).strip()

    summary = description
    if not summary:
        summary = _text(data.get("creator_notes")).strip()
    if not summary:
        summary = personality[:500]

    extensions = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    quillplus = extensions.get("quillplus") if isinstance(extensions.get("quillplus"), dict) else {}
    quill_ext = quillplus.get("quill_extensions") if isinstance(quillplus.get("quill_extensions"), dict) else None

    card: dict = {
        "name": name,
        "summary": summary,
        "core_prompts": {
            "personality": personality,
            "scenario": scenario,
            "first_message": first_message,
            "examples_of_dialogue": mes_example,
        },
        "system_prompt": system_prompt,
        "post_history_instructions": post_history,
    }
    if isinstance(quillplus.get("id"), str) and quillplus["id"].strip():
        card["id"] = quillplus["id"].strip()
    if quill_ext:
        card["quill_extensions"] = quill_ext
    book = data.get("character_book")
    if isinstance(book, dict) and _nonempty(book.get("entries")):
        card["_character_book"] = book

    # —— 报告：映射 / 保留 / 不支持 ——
    prefix = "data." if fmt in (FORMAT_CCV2, FORMAT_CCV3, FORMAT_LEGACY_NESTED) else ""
    mapped: dict = dict(alias_mapped)
    for key in CONSUMED_DATA_KEYS:
        if key not in data or not _nonempty(data[key]):
            continue
        if key == "extensions":
            if quillplus:
                mapped[key] = DATA_FIELD_TARGETS[key]
            continue
        if key == "description" and not description:
            continue
        # 报告里的字段名不带 data. 前缀：这是「哪个字段被映射」的用户视角，
        # 而 preserved_fields 用的是 JSON 路径（card.X / data.X），两者语义不同。
        mapped[key] = DATA_FIELD_TARGETS[key]
    if not description and _text(data.get("creator_notes")).strip():
        mapped["creator_notes"] = DATA_FIELD_TARGETS["creator_notes"]

    preserved: dict = {"card": {}, "data": {}}
    for key, value in data.items():
        if key in CONSUMED_DATA_KEYS:
            continue
        if _nonempty(value):
            preserved["data"][key] = value
    # M4.1b：非 quillplus 的扩展命名空间（RisuAI / Agnai 等）原样保留。
    # 旧实现把整个 extensions 当「已消费」跳过，等于静默丢掉 RisuAI 的角色扩展。
    extra_ext = {k: v for k, v in extensions.items() if k != "quillplus" and _nonempty(v)}
    if extra_ext:
        preserved["data"]["extensions"] = extra_ext
        if any(str(k).lower() == "risuai" for k in extra_ext):
            warnings.append("已原样保留 RisuAI 扩展；其触发器与自定义脚本不会执行")
    if prefix:
        for key, value in obj.items():
            if key in ("spec", "spec_version", "data"):
                continue
            if _nonempty(value):
                preserved["card"][key] = value
    if isinstance(book, dict):
        book_copy = _preserve_book(book)
        if book_copy:
            preserved["book"] = book_copy
    preserved = _trim_preserved(preserved, warnings)

    unsupported = []
    for field, reason in V3_UNSUPPORTED:
        if _nonempty(data.get(field)):
            unsupported.append({"field": field, "reason": reason})

    return _finish(
        card, fmt, _schema_version(fmt, obj), confidence, mapped, preserved, unsupported,
        warnings, _extension_summary(obj, data), via,
    )


# —— 文本导入：JSON 优先，其后是 W++ 标签文本，最后才是纯自然语言回退 ——

# 标签正则直接沿用 M2 的实现（FUNC-P1-2 修过交替分支捕获组与块终止前瞻）。
# 本层只把结果接进统一报告，不改识别行为——改识别等于改兼容面，必须有 E2E 覆盖。
_WPP_NAME_RE = re.compile(r'Name\s*=\s*["\']?([^"\'\n]+)["\']?', re.IGNORECASE)


def _wpp_name(text: str) -> str:
    match = _WPP_NAME_RE.search(text)
    return (match.group(1) if match else "").strip()


def _wpp_block(label_regex: str, source_text: str) -> str:
    """取标签后的内容，直到下一个「大写标签 + =/:」或文本结束。"""
    pattern = (
        rf"(?:{label_regex})\s*[=:]?\s*\n?(.*?)"
        rf"(?=\n\s*[A-Z][\w ]*?[=:]|\Z)"
    )
    match = re.search(pattern, source_text, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else "").strip()


# —— M4.1b：oobabooga YAML ——
# 只在出现 context / greeting / example_dialogue 这类**oobabooga 专有键**时才接管；
# 否则 W++ 文本里的「Name: / Personality:」会被 safe_load 当成普通字典吞掉。
_YAML_MAX_BYTES = 256 * 1024
_YAML_MAX_DEPTH = 12
_YAML_MAX_ALIASES = 20
_YAML_KEY_RE = re.compile(r"(?m)^\s{0,8}(context|greeting|example_dialogue)\s*:")
_YAML_ALIAS_RE = re.compile(r"(?:^|[\s\[{,])([&*][A-Za-z0-9_]+)")


def _yaml_depth(value: Any, depth: int = 0) -> int:
    if depth >= _YAML_MAX_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([_yaml_depth(item, depth + 1) for item in value.values()] or [depth])
    if isinstance(value, (list, tuple)):
        return max([_yaml_depth(item, depth + 1) for item in value] or [depth])
    return depth


def _try_yaml_card(raw: str) -> dict | None:
    """解析 oobabooga YAML；不像 YAML 或解析失败都返回 None（继续走后面的分支）。

    安全边界：限大小、限嵌套深度、限锚点/别名数量；只用 safe_load，
    不允许自定义对象构造（yaml.load 的 Loader 参数永远不传）。
    """
    if len(raw.encode("utf-8")) > _YAML_MAX_BYTES or not _YAML_KEY_RE.search(raw):
        return None
    try:
        import yaml
    except ImportError:
        return None
    if len(_YAML_ALIAS_RE.findall(raw)) > _YAML_MAX_ALIASES:
        raise CardFormatError("YAML 角色卡使用了过多锚点/别名，已拒绝解析（上限 20 个）")
    try:
        parsed = yaml.safe_load(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if _yaml_depth(parsed) > _YAML_MAX_DEPTH:
        raise CardFormatError("YAML 角色卡嵌套过深（上限 12 层），已拒绝解析")
    return parsed


def _parse_yaml_card(obj: dict) -> dict:
    name = _text(obj.get("name")).strip() or "未命名角色"
    personality = _text(obj.get("context")).strip() or _text(obj.get("description")).strip()
    scenario = _text(obj.get("scenario")).strip()
    first_message = _text(obj.get("greeting")).strip()
    examples = clean_mes_example(_text(obj.get("example_dialogue") or obj.get("example_dialogs")))
    card = {
        "name": name,
        "summary": personality[:500],
        "core_prompts": {
            "personality": personality,
            "scenario": scenario,
            "first_message": first_message,
            "examples_of_dialogue": examples,
        },
    }
    mapped: dict = {}
    for source, value, target in (
        ("name", name, "name"),
        ("context", personality, "core_prompts.personality"),
        ("greeting", first_message, "core_prompts.first_message"),
        ("example_dialogue", examples, "core_prompts.examples_of_dialogue"),
    ):
        if value:
            mapped[source] = target
    preserved = {"card": {}, "data": {}}
    for key, value in obj.items():
        if key in ("name", "context", "description", "scenario", "greeting", "example_dialogue", "example_dialogs"):
            continue
        if _nonempty(value):
            preserved["data"][key] = value
    warnings = ["按 oobabooga YAML 解析；未映射字段已作为原始字段保留"]
    return _finish(
        card, FORMAT_OOBA, "", 0.7, mapped, _trim_preserved(preserved, warnings), [],
        warnings, {"card": [], "book": [], "entries": 0}, "text",
    )


# —— M4.1b：SBF / Boostyle 结构化文本 ——
# SBF 常见形态：[character("Name"){ "personality": "…", "description": "…" }]，
# Boostyle 则是带方括号的 key: "value"。两者都没有统一 schema，只提取明确成对的
# 字段；提取不到就返回 None，让 W++ / 纯文本分支接手，绝不硬猜。
_SBF_PAIR_RE = re.compile(
    r'["\']?(name|description|personality|scenario|first_mes|first_message|mes_example|example_dialogue|greeting)["\']?'
    r"\s*[:=]\s*[\"']((?:[^\"'\\\n]|\\.)*)[\"']",
    re.IGNORECASE,
)
_SBF_CHAR_RE = re.compile(r'character\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _sbf_fields(raw: str) -> dict:
    fields: dict = {}
    for match in _SBF_PAIR_RE.finditer(raw):
        key = match.group(1).lower()
        if key in fields:
            continue
        try:
            value = json.loads('"' + match.group(2) + '"')
        except Exception:
            value = match.group(2)
        if _nonempty(value):
            fields[key] = value
    char = _SBF_CHAR_RE.search(raw)
    if char and not _nonempty(fields.get("name")):
        fields["name"] = char.group(1)
    return fields


def _parse_sbf_card(raw: str) -> dict | None:
    # 先看标记：W++ / Raw Text 里的 Name = "X" 也会被键值对正则命中，
    # 但那是 W++ 的地盘。只有带方括号 / 花括号 / character(...) 的文本才按 SBF 处理。
    if not (_SBF_CHAR_RE.search(raw) or ("[" in raw and "]" in raw) or raw.lstrip().startswith("{")):
        return None
    fields = _sbf_fields(raw)
    if not any(_text(value).strip() for value in fields.values()):
        return None
    name = _text(fields.get("name")).strip() or "未命名角色 (需手动修改)"
    personality = _merge_personality(
        _text(fields.get("description")).strip(),
        _text(fields.get("personality")).strip(),
    )
    if not personality:
        # 拆不出人格正文时整段原文进设定：宁可多带标记，也不静默丢内容。
        personality = raw
    scenario = _text(fields.get("scenario")).strip()
    first_message = _text(fields.get("first_mes") or fields.get("first_message") or fields.get("greeting")).strip()
    examples = clean_mes_example(_text(fields.get("mes_example") or fields.get("example_dialogue")))
    card = {
        "name": name,
        "summary": personality[:500],
        "core_prompts": {
            "personality": personality,
            "scenario": scenario,
            "first_message": first_message,
            "examples_of_dialogue": examples,
        },
    }
    mapped: dict = {}
    for source, value, target in (
        ("name", name, "name"),
        ("description", _text(fields.get("description")).strip(), "core_prompts.personality"),
        ("personality", _text(fields.get("personality")).strip(), "core_prompts.personality"),
        ("scenario", scenario, "core_prompts.scenario"),
        ("first_mes", first_message, "core_prompts.first_message"),
        ("mes_example", examples, "core_prompts.examples_of_dialogue"),
    ):
        if value:
            mapped[source] = target
    warnings = ["按 SBF / Boostyle 结构化文本解析；无法拆解的内容整段并入角色设定，导入后请核对"]
    return _finish(
        card, FORMAT_SBF, "", 0.5, mapped, {"card": {}, "data": {}}, [],
        warnings, {"card": [], "book": [], "entries": 0}, "text",
    )


def parse_text_card(text: str) -> dict:
    """文本导入：JSON → oobabooga YAML → SBF → W++ → 纯文本，逐级降置信。

    YAML 分支只在出现 context/greeting 这类 oobabooga 专有键时才接管，
    否则 W++ 的「Name: / Personality:」会被 safe_load 当成普通字典吞掉。
    """
    raw = (text or "").strip()
    if not raw:
        raise CardFormatError("导入文本为空")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if parsed is not None:
        if not isinstance(parsed, dict):
            raise CardFormatError("剪贴板 JSON 根不是对象：JSON 必须是对象才能作为角色卡导入")
        return parse_json_card(parsed, via="text")

    yaml_card = _try_yaml_card(raw)
    if yaml_card is not None:
        return _parse_yaml_card(yaml_card)
    sbf_card = _parse_sbf_card(raw)
    if sbf_card is not None:
        return sbf_card

    name = _wpp_name(raw)
    personality = _wpp_block(r"Personality", raw)
    scenario = _wpp_block(r"Scenario", raw)
    first_message = _wpp_block(r"First\s*Message|First_mes|FirstMes", raw)
    examples = _wpp_block(r"Example\s*Dialogs|Example_Dialog|mes_example|Examples", raw)
    description = _wpp_block(r"Description|Summary", raw)
    has_personality = bool(personality)
    if not personality and description:
        personality = description

    if not any((name, personality, scenario, first_message, examples)):
        # 一个标签都没有 = 普通自然语言：整段进角色设定，低置信 + 明确警告，
        # 但仍然是「成功导入」——这是 M4.1 允许的唯一降级路径。
        first_line = raw.splitlines()[0].strip()
        card = {
            "name": first_line[:40] or "未命名角色",
            "summary": raw[:500],
            "core_prompts": {
                "personality": raw,
                "scenario": "",
                "first_message": "",
                "examples_of_dialogue": "",
            },
        }
        return _finish(
            card, FORMAT_PLAIN_TEXT, "", 0.25,
            {"text": "core_prompts.personality"}, {"card": {}, "data": {}},
            [], ["未识别到结构化格式，已按纯文本导入：全部内容作为角色设定，请核对"],
            {"card": [], "book": [], "entries": 0}, "text",
        )

    card = {
        "name": name or "未命名角色 (需手动修改)",
        "summary": (description or personality)[:500],
        "core_prompts": {
            "personality": personality,
            "scenario": scenario,
            "first_message": first_message,
            "examples_of_dialogue": clean_mes_example(examples),
        },
    }
    mapped = {}
    for label, value, target in (
        ("Name", name, "name"),
        ("Personality", personality if has_personality else "", "core_prompts.personality"),
        ("Scenario", scenario, "core_prompts.scenario"),
        ("First Message", first_message, "core_prompts.first_message"),
        ("Example Dialogs", examples, "core_prompts.examples_of_dialogue"),
        ("Description", description, "core_prompts.personality"),
    ):
        if value:
            mapped[label] = target
    return _finish(
        card, FORMAT_WPP, "", 0.55, mapped, {"card": {}, "data": {}}, [],
        ["按标签文本（W++ 一类）解析；标签之外的内容不保留，导入后请核对"],
        {"card": [], "book": [], "entries": 0}, "text",
    )
