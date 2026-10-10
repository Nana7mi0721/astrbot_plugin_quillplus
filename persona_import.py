# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AstrBot「人格」→ Quill 角色卡 导入适配层。

数据面：``self.context.persona_manager``（``astrbot.core.persona_mgr.PersonaManager``
在 AstrBot 4.x 上挂在插件 Context 上，与插件已在用的 ``kb_manager`` 同级）。
本模块**只读**它——列出人格、取单个人格的 ``system_prompt`` / ``begin_dialogs``，
不写不删：人格的增删改一律留在 AstrBot 面板，插件只做「搬进来」。

转换面：人格（一段自由格式的 ``system_prompt`` + 若干 ``begin_dialogs``）→
角色卡四段式（``personality`` / ``scenario`` / ``examples_of_dialogue`` /
``first_message``）+ 名字 + 一句话简介。人格文本没有字段边界，正则拆不出来
（W++ 标签、自然语言小作文、两者混排都常见），故走**用户配置的摘要模型**
（``rag.llm_provider_id``，与 LLM 摘要器同一个 provider）做语义切分。

失败语义：
- AstrBot 版本过旧（Context 无 ``persona_manager``）或测试 stub 环境 →
  :attr:`AstrBotPersonaSource.available` 为 False，面板把入口置灰，不抛错；
- 未配置摘要模型 / provider 解析不到 → :class:`PersonaImportError`（面板原样展示）；
- 模型输出不是 JSON / 关键字段全空 → :class:`PersonaImportError`，**不静默降级**
  （宁可让用户看到「转换失败」，也不要落一张只有名字的空卡）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件自测：先把 AstrBot 加入可导入路径
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _astrbot_bootstrap import ensure_astrbot_importable
    ensure_astrbot_importable()
    from astrbot.api import logger


#: 送进 LLM 的人格正文上限（字符）。人格动辄上万字，全量塞进去既慢又容易顶爆
#: 上下文；超长时截断并在 prompt 里显式标注，让模型知道后面还有内容被砍掉了。
PERSONA_PROMPT_LIMIT = 12000

#: 转换调用的超时（秒）。人格长文 + JSON 输出的生成时间远大于状态栏提取（3s），
#: 但也不能无限等：卡在面板上一个转圈比失败更糟。超时按失败处理。
CONVERT_TIMEOUT = 90.0

#: 角色名 / 简介的长度上限（简介与 persona_manager 的面板截断口径一致）。
CARD_NAME_MAX = 60
CARD_SUMMARY_MAX = 300

#: 单段核心设定（personality 等）的软上限：超过只记日志不截断——模型输出本来
#: 受 max_tokens 约束，真出超长文本说明人格本身就这么大，砍掉反而丢设定。
CORE_PROMPT_WARN = 12000

#: ``begin_dialogs`` 最多取多少条（AstrBot 里通常是 1~3 条，多的按前 N 条算）。
BEGIN_DIALOG_LIMIT = 8


class PersonaImportError(Exception):
    """人格 → 角色卡 转换失败（用户可见的中文消息）。"""


CONVERT_SYSTEM_PROMPT = """你是一个角色卡整理助手，负责把 AstrBot 人格设定整理成结构化的角色卡字段。

规则：
1. 只做整理与切分，**不得编造**原设定里没有的信息；原设定没写的字段留空字符串。
2. 保留原设定的人称、语气与关键细节（称呼、口癖、关系、禁忌），可以合并重复、去掉与角色无关的元信息（如"你是一个AI助手""输出格式要求"这类与扮演无关的说明）。
3. 严格输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码块。

JSON 字段：
- "name": 角色名（尽量短，不要带书名号或引号；原设定没有明确名字时用人格名）
- "summary": 一句话简介，不超过 60 字
- "personality": 性格、说话风格、行为逻辑（核心字段，尽量完整）
- "scenario": 场景 / 世界观 / 关系背景（原设定没有则留空）
- "examples_of_dialogue": 对话示例，**多轮**，每行一条并带说话人前缀（"用户：…" / "角色：…"）；把给出的开场对话整理进来，没有则留空
- "first_message": 角色开场白，**只放角色说的第一句话**（一句话，不要多轮、不要带"用户："前缀）；原设定里没有角色台词时留空

注意 "examples_of_dialogue" 与 "first_message" 是两个不同字段，不要填成同一段内容：
前者是完整的对话样例，后者只是角色在对话开始时的第一句话。
"""


def _dialog_text(item) -> str:
    """把一条开场对话渲染成一行文本。

    AstrBot 存的是纯字符串，但旧版 v3 配置 / 别的调用方可能给
    ``{"role": ..., "content": ...}`` 这类结构，一并容忍。
    """
    if isinstance(item, dict):
        text = item.get("content") or item.get("text") or item.get("message") or ""
        return str(text).strip()
    return str(item).strip()


def _pick(data: dict, *keys: str) -> str:
    """按顺序取第一个非空字符串字段（容忍模型换字段名/换语言）。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # 模型偶尔会把列表塞进字符串字段（如对话示例），拼成多行文本
        if isinstance(value, list):
            joined = "\n".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                return joined
    return ""


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出里提取首个完整 JSON 对象。

    与 ``QuillSummarizer._extract_json`` 同口径：``raw_decode`` 从每个 ``{``
    开始尝试——贪婪正则/整体 ``json.loads`` 在输出带思考过程、被 ``` 包裹或
    尾随说明文字时都会失败，而模型这三种情况都会出。
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _collapse(text: str, limit: int) -> str:
    """压平空白并截断（人格简介预览用）。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class AstrBotPersonaSource:
    """AstrBot 人格的只读数据源。

    Usage::

        src = AstrBotPersonaSource(self.context)
        if src.available:
            items = await src.list_personas()          # 面板列表
            one = await src.get_persona(persona_id)    # 导入前取全文
    """

    def __init__(self, context, enabled: bool = True):
        self.context = context
        self.enabled = bool(enabled)

    @property
    def available(self) -> bool:
        """persona_manager 是否可达（旧版 AstrBot / stub 环境为 False）。"""
        return bool(
            self.enabled
            and self.context is not None
            and getattr(self.context, "persona_manager", None) is not None
        )

    async def _folders(self) -> dict:
        """``{folder_id: 文件夹名}``；取不到就当人格都在根目录。"""
        pm = getattr(self.context, "persona_manager", None)
        getter = getattr(pm, "get_all_folders", None)
        if getter is None:
            return {}
        try:
            folders = await getter()
        except Exception as e:  # 文件夹是锦上添花，失败不影响列人格
            logger.warning("[Quill Persona] 读取人格文件夹失败: %s", e)
            return {}
        out: dict = {}
        for f in folders or []:
            fid = str(getattr(f, "folder_id", "") or "")
            if fid:
                out[fid] = str(getattr(f, "name", "") or "")
        return out

    async def list_personas(self) -> list[dict]:
        """列出全部人格（面板「由人格导入」的选择列表）。

        返回项：``{persona_id, folder, prompt_chars, begin_dialog_count, preview}``。
        不可达 / 查询失败返回 []——「没有人格」与「功能不可用」对面板都是空列表，
        可用性由 :attr:`available` 单独表达。
        """
        if not self.available:
            return []
        pm = getattr(self.context, "persona_manager", None)
        try:
            records = await pm.get_all_personas()
        except Exception as e:
            logger.warning("[Quill Persona] 列举 AstrBot 人格失败: %s", e)
            return []
        folders = await self._folders()
        out: list[dict] = []
        for rec in records or []:
            pid = str(getattr(rec, "persona_id", "") or "").strip()
            if not pid:
                continue
            prompt = str(getattr(rec, "system_prompt", "") or "")
            dialogs = getattr(rec, "begin_dialogs", None) or []
            out.append({
                "persona_id": pid,
                "folder": folders.get(str(getattr(rec, "folder_id", "") or ""), ""),
                "prompt_chars": len(prompt),
                "begin_dialog_count": len(dialogs) if isinstance(dialogs, list) else 0,
                # 预览只取开头，避免面板拿到整篇人格（长文会拖慢列表渲染）
                "preview": _collapse(prompt, 90),
            })
        return out

    async def get_persona(self, persona_id: str) -> Optional[dict]:
        """取单个人格的全文（转换前的原料）。不存在 / 不可达返回 None。"""
        pid = (persona_id or "").strip()
        if not pid or not self.available:
            return None
        pm = getattr(self.context, "persona_manager", None)
        try:
            rec = await pm.get_persona(pid)
        except Exception as e:
            logger.warning("[Quill Persona] 读取人格 %s 失败: %s", pid, e)
            return None
        if rec is None:
            return None
        dialogs = getattr(rec, "begin_dialogs", None) or []
        if not isinstance(dialogs, list):
            dialogs = []
        return {
            "persona_id": str(getattr(rec, "persona_id", "") or pid),
            "system_prompt": str(getattr(rec, "system_prompt", "") or ""),
            "begin_dialogs": [str(d) for d in dialogs[:BEGIN_DIALOG_LIMIT] if str(d).strip()],
        }


class PersonaCardConverter:
    """用「摘要模型」把人格正文整理成角色卡字段。

    ``provider_id`` 来自 ``config.rag_llm_provider_id``（面板「配置 → RAG 与
    动态记忆 → LLM 提供商」），即记忆摘要用的同一个模型——不额外引入一个
    「导入专用模型」配置项，用户已经为摘要配过模型就不该再配第二次。
    """

    def __init__(self, context, provider_id: str = "", timeout: float = CONVERT_TIMEOUT):
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.timeout = float(timeout) if timeout else CONVERT_TIMEOUT

    @property
    def ready(self) -> bool:
        """provider 是否真的解析得到（面板据此提示"先去配置摘要模型"）。"""
        if not self.provider_id or self.context is None:
            return False
        getter = getattr(self.context, "get_provider_by_id", None)
        if getter is None:
            return False
        try:
            return getter(self.provider_id) is not None
        except Exception:
            return False

    def build_prompt(self, persona: dict) -> str:
        """拼转换用的用户侧 prompt（单独暴露给测试与排障）。"""
        name = str(persona.get("persona_id", "") or "")
        prompt = str(persona.get("system_prompt", "") or "")
        truncated = len(prompt) > PERSONA_PROMPT_LIMIT
        body = _clip(prompt, PERSONA_PROMPT_LIMIT)
        parts = [f"人格名：{name}", "", "人格设定：", body]
        if truncated:
            parts.append("（以上设定过长，已截断）")
        dialogs = persona.get("begin_dialogs") or []
        if dialogs:
            parts.append("")
            # AstrBot 的 begin_dialogs 是「纯字符串列表 + 位置决定角色」：偶数位是
            # 用户、奇数位是角色（core/persona_mgr.py 里按 user_turn 交替生成）。
            # 不标出来模型分不清哪句是角色说的，会把整段同时塞进 examples 与
            # first_message。
            parts.append("开场对话（按顺序交替，第一条是用户、第二条是角色，依此类推）：")
            for i, d in enumerate(dialogs):
                parts.append(f"- {'用户' if i % 2 == 0 else '角色'}：{_dialog_text(d)}")
        parts.append("")
        parts.append("请输出 JSON 角色卡：")
        return "\n".join(parts)

    async def convert(self, persona: dict, name_override: str = "") -> dict:
        """人格 dict → ``create_persona`` 可用的角色卡 dict。

        Args:
            persona: :meth:`AstrBotPersonaSource.get_persona` 的返回。
            name_override: 面板里用户显式填的角色卡名（非空则覆盖模型给的名字）。

        Raises:
            PersonaImportError: 模型不可用 / 调用失败 / 输出无法解析 / 关键字段全空。
        """
        if not self.ready:
            if not self.provider_id:
                raise PersonaImportError(
                    "未配置用于生成摘要的模型：请先到「配置 → RAG 与动态记忆」选择 LLM 提供商"
                )
            raise PersonaImportError(
                f"摘要模型 {self.provider_id} 不可用：请检查 AstrBot 的模型提供商配置"
            )

        source_text = str(persona.get("system_prompt", "") or "").strip()
        if not source_text and not (persona.get("begin_dialogs") or []):
            raise PersonaImportError("这个人格没有设定内容（system prompt 为空），无法转换")

        provider = self.context.get_provider_by_id(self.provider_id)
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self.build_prompt(persona),
                    system_prompt=CONVERT_SYSTEM_PROMPT,
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[Quill Persona] 人格转换超时（%ss）: %s", self.timeout, persona.get("persona_id"))
            raise PersonaImportError(f"摘要模型响应超时（{int(self.timeout)} 秒），请稍后重试")
        except Exception as e:
            logger.warning("[Quill Persona] 人格转换调用失败: %s", e, exc_info=True)
            raise PersonaImportError(f"摘要模型调用失败：{e}")

        raw = ""
        if response is not None:
            raw = str(getattr(response, "completion_text", "") or getattr(response, "text", "") or "")
        data = _extract_json(raw)
        if not data:
            logger.warning("[Quill Persona] 转换输出无法解析为 JSON: %r", raw[:200])
            raise PersonaImportError("摘要模型没有返回可解析的 JSON，请重试（或换一个模型）")

        name = (name_override or "").strip() or _pick(data, "name", "角色名", "角色名称")
        name = _clip(name, CARD_NAME_MAX).strip()
        if not name:
            name = str(persona.get("persona_id", "") or "").strip()
        if not name:
            raise PersonaImportError("无法确定角色卡名称：请在上方手动填写名称")

        core = {
            "personality": _pick(data, "personality", "性格", "性格设定"),
            "scenario": _pick(data, "scenario", "场景", "背景", "世界观"),
            "examples_of_dialogue": _pick(
                data, "examples_of_dialogue", "examples", "对话示例", "示例对话"
            ),
            "first_message": _pick(data, "first_message", "first_mes", "开场白", "问候语"),
        }
        if not any(core.values()):
            raise PersonaImportError("摘要模型没有产出任何角色设定字段，请重试（或换一个模型）")

        # 兜底：模型偶尔会漏填开场白/对话示例（实测 32 条开场对话的长人格上就
        # 漏过 first_message）。原始素材就在手里，别让这两个字段空着——
        # AstrBot 的 begin_dialogs 本身就是「开场对话」，奇数位是角色台词。
        dialogs = [d for d in (persona.get("begin_dialogs") or []) if _dialog_text(d)]
        if dialogs:
            if not core["examples_of_dialogue"]:
                core["examples_of_dialogue"] = "\n".join(
                    f"{'用户' if i % 2 == 0 else '角色'}：{_dialog_text(d)}"
                    for i, d in enumerate(dialogs)
                )
                logger.info("[Quill Persona] 模型未产出对话示例，已用开场对话兜底（%d 条）", len(dialogs))
            if not core["first_message"]:
                # 角色说的第一句：优先第 2 条（assistant），只有 1 条时退回第 1 条
                idx = 1 if len(dialogs) > 1 else 0
                core["first_message"] = _dialog_text(dialogs[idx])
                logger.info("[Quill Persona] 模型未产出开场白，已用第 %d 条开场对话兜底", idx + 1)

        for field, value in core.items():
            if len(value) > CORE_PROMPT_WARN:
                logger.info("[Quill Persona] %s 段较长（%d 字），原样保留", field, len(value))

        card = {
            "name": name,
            "summary": _clip(_pick(data, "summary", "简介", "描述"), CARD_SUMMARY_MAX),
            "core_prompts": core,
        }
        logger.info(
            "[Quill Persona] 人格 %s 已转换为角色卡字段（name=%s, 设定 %d 字）",
            persona.get("persona_id"), name, sum(len(v) for v in core.values()),
        )
        return card


# 直接运行自测：python persona_import.py
# 用假 persona_manager + 假 provider 走通 列举/取值/转换/失败 四条路径，
# 不依赖真实 AstrBot 实例，也不发真实网络请求。
if __name__ == "__main__":

    class _Rec:
        def __init__(self, persona_id, system_prompt, dialogs=None, folder_id=None):
            self.persona_id = persona_id
            self.system_prompt = system_prompt
            self.begin_dialogs = dialogs
            self.folder_id = folder_id

    class _Folder:
        def __init__(self, folder_id, name):
            self.folder_id = folder_id
            self.name = name

    class _FakePersonaManager:
        def __init__(self):
            self.records = {
                "宁宁": _Rec("宁宁", "你是绫地宁宁，性格温柔但有点怕生。", ["宁宁: 你好呀。", "我: 你好"], "f1"),
                "空人格": _Rec("空人格", ""),
            }

        async def get_all_personas(self):
            return list(self.records.values())

        async def get_all_folders(self):
            return [_Folder("f1", "GAL")]

        async def get_persona(self, persona_id):
            return self.records.get(persona_id)

    class _Resp:
        def __init__(self, text):
            self.completion_text = text

    class _FakeProvider:
        def __init__(self, payload):
            self.payload = payload
            self.last_kwargs = None

        async def text_chat(self, prompt=None, system_prompt=None, **kw):
            self.last_kwargs = {"prompt": prompt, "system_prompt": system_prompt}
            return _Resp(self.payload)

    class _Ctx:
        def __init__(self, provider=None):
            self.persona_manager = _FakePersonaManager()
            self._provider = provider

        def get_provider_by_id(self, pid):
            return self._provider if pid == "test/llm" else None

    async def _main():
        src = AstrBotPersonaSource(_Ctx())
        assert src.available, "fake context 应可用"
        items = await src.list_personas()
        assert [i["persona_id"] for i in items] == ["宁宁", "空人格"], items
        assert items[0]["folder"] == "GAL" and items[0]["begin_dialog_count"] == 2
        assert items[1]["prompt_chars"] == 0 and items[1]["folder"] == ""
        assert items[0]["preview"].startswith("你是绫地宁宁")

        one = await src.get_persona("宁宁")
        assert one["system_prompt"].startswith("你是绫地宁宁") and len(one["begin_dialogs"]) == 2
        assert await src.get_persona("不存在") is None
        assert (await src.get_persona("")) is None

        # 转换：模型输出带 ```json 包裹 + 尾随说明（真实模型最常见的两种脏输出）
        payload = (
            "好的，整理结果如下：\n```json\n"
            '{"name": "绫地宁宁", "summary": "温柔的邻家女孩", '
            '"personality": "温柔、怕生、说话轻声细语", "scenario": "现代都市", '
            '"examples_of_dialogue": "宁宁: 你好呀。", "first_message": "你好呀。"}\n'
            "```\n希望有帮助。"
        )
        conv = PersonaCardConverter(_Ctx(_FakeProvider(payload)), provider_id="test/llm")
        assert conv.ready
        card = await conv.convert(one)
        assert card["name"] == "绫地宁宁" and card["summary"] == "温柔的邻家女孩"
        assert card["core_prompts"]["personality"].startswith("温柔")
        assert card["core_prompts"]["first_message"] == "你好呀。"
        assert "开场对话" in conv.build_prompt(one)

        # 名称覆盖优先于模型给的名字
        card2 = await conv.convert(one, name_override="宁宁（导入）")
        assert card2["name"] == "宁宁（导入）", card2

        # 超长正文被截断（不炸上下文），且截断有显式标注
        long_persona = {"persona_id": "长人格", "system_prompt": "设" * (PERSONA_PROMPT_LIMIT + 500)}
        prompt = conv.build_prompt(long_persona)
        assert "已截断" in prompt and len(prompt) < PERSONA_PROMPT_LIMIT + 500

        # 未配置摘要模型 / provider 解析不到 → 明确报错
        for ctx, pid, kw in ((_Ctx(_FakeProvider(payload)), "", "未配置"), (_Ctx(None), "test/llm", "不可用")):
            c = PersonaCardConverter(ctx, provider_id=pid)
            assert not c.ready
            try:
                await c.convert(one)
                raise AssertionError("应抛 PersonaImportError")
            except PersonaImportError as e:
                assert kw in str(e), e

        # 空人格 / 非 JSON 输出 → 明确报错，不落空卡
        try:
            await conv.convert(await src.get_persona("空人格"))
            raise AssertionError("空人格应报错")
        except PersonaImportError as e:
            assert "没有设定内容" in str(e), e
        bad = PersonaCardConverter(_Ctx(_FakeProvider("我觉得这个角色很棒！")), provider_id="test/llm")
        try:
            await bad.convert(one)
            raise AssertionError("非 JSON 输出应报错")
        except PersonaImportError as e:
            assert "JSON" in str(e), e
        empty = PersonaCardConverter(
            _Ctx(_FakeProvider('{"name": "x", "summary": "y"}')), provider_id="test/llm"
        )
        try:
            await empty.convert(one)
            raise AssertionError("关键字段全空应报错")
        except PersonaImportError as e:
            assert "角色设定字段" in str(e), e

        # 无 persona_manager（旧版 AstrBot / stub）→ 不可用且返回空列表
        bare = AstrBotPersonaSource(object())
        assert not bare.available
        assert await bare.list_personas() == []
        assert await bare.get_persona("宁宁") is None

        print("persona_import 自测通过")

    asyncio.run(_main())
