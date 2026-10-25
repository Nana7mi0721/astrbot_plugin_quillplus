# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuillPersonaManager — 独立的角色卡管理系统。

完全脱离 AstrBot 原生的 data_v4.db 和 context.persona_manager，
改用 JSON 文件存储，每张角色卡对应一个 JSON 文件。
支持 V2 角色卡标准（Character Card V2）的导入导出。
"""

import asyncio
import base64
import copy
import json
import os
import re
import tempfile
import time
from collections import defaultdict
from io import BytesIO
from typing import Any, Optional
from uuid import uuid4

from astrbot.api import logger


class QuillPersonaManager:
    """基于 JSON 文件的结构化角色卡管理器。"""

    def __init__(self, data_dir: str, avatar_dir: str = None):
        self.data_dir = data_dir
        self.avatar_dir = avatar_dir or os.path.join(
            os.path.dirname(data_dir), "quill_avatars"
        )
        self._locks = defaultdict(asyncio.Lock)
        # S2-7 修复：_cache_lock 专门保护 cache 状态转换（load/create/delete），避免 TOCTOU
        self._cache_lock = asyncio.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_loaded: bool = False

    async def _ensure_dir(self):
        await asyncio.to_thread(os.makedirs, self.data_dir, exist_ok=True)

    async def _ensure_avatar_dir(self):
        await asyncio.to_thread(os.makedirs, self.avatar_dir, exist_ok=True)

    def _avatar_path(self, filename: str) -> str:
        return os.path.join(self.avatar_dir, filename)

    async def save_avatar(self, filename: str, data: bytes) -> str:
        """保存头像文件到本地，返回相对路径（如 quill_avatars/xxx.png）。"""
        await self._ensure_avatar_dir()
        safe = self._sanitize_id(os.path.splitext(filename)[0])
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
            ext = '.png'
        # 防重名：加时间戳
        final_name = f"{safe}_{int(time.time())}{ext}"
        path = self._avatar_path(final_name)
        await asyncio.to_thread(self._sync_write_bytes, path, data)
        return f"quill_avatars/{final_name}"

    @staticmethod
    def _sync_write_bytes(path: str, data: bytes):
        """F3 修复：原子写入二进制（头像文件），tmp + fsync + os.replace"""
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _resolve_avatar_file(self, filename: str) -> Optional[str]:
        """把文件名解析为头像目录内的绝对路径；越界返回 None。

        M6.1 安全修复：此前 read/delete 直接用 ``os.path.join(avatar_dir, filename)``，
        而 filename 来自角色卡的 ``avatar_path``（可由面板或 ``/char import`` 写入），
        于是 ``quill_avatars/../../../cmd_config.json`` 能通过调用方的前缀检查，
        被拼到头像目录之外——读取即任意文件读取、删除即任意文件删除。

        这里改用 realpath + 前缀包含性校验（与 ``_paths.resolve_archive_dest`` 同一套
        思路），一次拦住 ``..``、绝对路径、盘符、UNC 与指向外部的符号链接；
        不依赖文件名黑名单，也就不会随命名策略变化而失效。
        """
        if not filename or "\x00" in filename:
            return None
        base = os.path.realpath(self.avatar_dir)
        target = os.path.realpath(os.path.join(base, filename))
        if target != base and not target.startswith(base + os.sep):
            return None
        return target

    def _normalize_avatar_path(self, value: Any) -> str:
        """归一化写入角色卡的 ``avatar_path``，非法一律落空串。

        合法形态只有 :meth:`save_avatar` 的产出 ``quill_avatars/<文件名>``。
        读取侧已做包含性校验，这里再在**写入**侧拒一次：既让越界值不进库
        （不再依赖「每个出口都记得校验」），也让 ``has_avatar`` 之类的判断不会被
        畸形值干扰。
        """
        prefix = "quill_avatars/"
        raw = str(value or "").strip().replace("\\", "/")
        if not raw.startswith(prefix):
            return ""
        name = raw[len(prefix):]
        if not name or name in (".", "..") or name != os.path.basename(name):
            return ""
        return prefix + name if self._resolve_avatar_file(name) else ""

    async def read_avatar(self, filename: str) -> Optional[bytes]:
        """读取头像文件数据。路径越界一律返回 None。"""
        path = self._resolve_avatar_file(filename)
        if not path or not os.path.isfile(path):
            return None
        return await asyncio.to_thread(self._sync_read_bytes, path)

    @staticmethod
    def _sync_read_bytes(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    async def delete_avatar(self, filename: str) -> bool:
        """删除头像文件。路径越界一律返回 False（不删除任何文件）。"""
        path = self._resolve_avatar_file(filename)
        if not path:
            return False
        exists = await asyncio.to_thread(os.path.isfile, path)
        if exists:
            await asyncio.to_thread(os.remove, path)
            return True
        return False

    def _sanitize_id(self, raw: str) -> str:
        """将角色 ID 清理为安全的文件名。"""
        safe = re.sub(r'[^a-zA-Z0-9_\-\u4e00-\u9fff]', '_', raw)
        if not safe:
            return "persona"
        # 过滤 Windows 保留设备名（CON, PRN, AUX, NUL, COM1-9, LPT1-9）
        _WIN_RESERVED = {'con', 'prn', 'aux', 'nul'} | {f'com{i}' for i in range(1, 10)} | {f'lpt{i}' for i in range(1, 10)}
        if safe.lower() in _WIN_RESERVED:
            safe += '_'
        return safe

    def _persona_path(self, persona_id: str) -> str:
        return os.path.join(self.data_dir, f"{self._sanitize_id(persona_id)}.json")

    @staticmethod
    def _normalize_mode(value: Any) -> str:
        """将三态模式值归一化为 auto/custom/disabled，非法值fallback为 disabled。"""
        if value in ("auto", "custom", "disabled"):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("auto", "custom", "disabled"):
                return lowered
        return "disabled"

    @staticmethod
    def _sync_read_file(path: str) -> Optional[dict]:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def _read_file(self, path: str) -> Optional[dict]:
        return await asyncio.to_thread(self._sync_read_file, path)

    @staticmethod
    def _sync_write_file(path: str, data: dict):
        """F3 修复：原子写入 JSON（tmp + fsync + os.replace），防写入中断损坏文件"""
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    async def _write_file(self, path: str, data: dict):
        await asyncio.to_thread(self._sync_write_file, path, data)

    async def load_all(self) -> list[dict]:
        """加载所有角色卡，按 ID 排序返回（优先读缓存）。

        S2-7 修复：用 _cache_lock 保护加载过程，避免多协程同时加载造成 TOCTOU。
        S2-6 修复：返回 deepcopy 列表，防止调用方 mutate 污染缓存。
        """
        async with self._cache_lock:
            return await self._load_all_locked()

    async def _load_all_locked(self) -> list[dict]:
        """load_all 的内部实现，假设调用方已持有 _cache_lock。"""
        if self._cache_loaded:
            personas = [copy.deepcopy(p) for p in self._cache.values()]
            personas.sort(key=lambda p: p.get("id", ""))
            return personas

        await self._ensure_dir()
        self._cache.clear()
        for fname in await asyncio.to_thread(os.listdir, self.data_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.data_dir, fname)
            # B7：单个文件损坏不能让整个子系统瘫掉。load_all 在热路径上
            # （每轮 get_persona、面板列表、create 的重名校验），此前一个截断的
            # JSON 就会 json 解析异常冒泡、循环中断、_cache_loaded 永不置位——
            # 之后每次调用都重新炸。与 worldbook._load_all 对齐，逐文件容错。
            try:
                data = await self._read_file(path)
            except Exception as e:
                logger.warning(f"[QuillPersona] 跳过损坏的角色卡文件 {fname}: {e}")
                continue
            if data and data.get("id"):
                self._cache[data["id"]] = data
        self._cache_loaded = True
        personas = [copy.deepcopy(p) for p in self._cache.values()]
        personas.sort(key=lambda p: p.get("id", ""))
        return personas

    async def get_persona(self, persona_id: str) -> Optional[dict]:
        """根据 ID 获取单张角色卡（优先读缓存）。

        F10 修复：返回深拷贝，防止调用方直接 mutate 缓存导致并发 lost update。
        """
        if not self._cache_loaded:
            await self.load_all()
        persona = self._cache.get(persona_id)
        return copy.deepcopy(persona) if persona else None

    async def _name_exists(self, name: str, exclude_id: str = "") -> bool:
        """检查角色名是否已存在（排除 exclude_id）。

        注意：此方法会调用 load_all（获取 _cache_lock），因此不能在持有 _cache_lock 时调用。
        持锁场景请改用 _name_exists_locked。
        """
        personas = await self.load_all()
        for p in personas:
            if p.get("name") == name and p.get("id") != exclude_id:
                return True
        return False

    async def _name_exists_locked(self, name: str, exclude_id: str = "") -> bool:
        """_name_exists 的持锁版本，假设调用方已持有 _cache_lock。"""
        personas = await self._load_all_locked()
        for p in personas:
            if p.get("name") == name and p.get("id") != exclude_id:
                return True
        return False

    async def create_persona(self, data: dict) -> dict:
        """创建一张新的角色卡。返回保存后的数据。

        S2-7 修复：加 _cache_lock 保护，避免与 load_all/delete_persona 竞争。
        S2-6 修复：缓存与返回值均为 deepcopy，防止调用方 mutate 污染缓存。
        """
        async with self._cache_lock:
            name = (data.get("name") or "").strip()
            if not name:
                raise ValueError("角色名称 (name) 为必填项")
            if await self._name_exists_locked(name):
                raise ValueError(f"角色名 '{name}' 已存在")

            # M0-T1：id 缺省不再取 name——name 会渗入身份键、记忆会话键
            # （_get_memory_session_id）与落盘文件名，改名语义虽不断绑定，但
            # name-id 让"角色名"与"内部标识"耦合（M4 项目化要稳定 ID）。
            # 显式传入的 id 仍优先（跨实例导进保留身份）；缺省生成 12 位 hex
            # 短 UUID：无特殊字符（_sanitize_id 恒等，天然配合 B10 文件名判重），
            # 48bit 碰撞对本插件量级可忽略，且下方缓存/B10 判重兜底。
            persona_id = (data.get("id") or "").strip() or uuid4().hex[:12]
            if self._cache_loaded and persona_id in self._cache:
                raise ValueError(f"角色 ID '{persona_id}' 已存在")
            # B10：不同 id 可能被 _sanitize_id 规整成同一个文件名
            # （"a/b" 与 "a:b" → 都是 a_b.json）。缓存按**原始 id** 判重抓不到，
            # 落盘却是 os.replace → 静默覆盖前一张卡。这里按文件名再判一次。
            if await asyncio.to_thread(os.path.isfile, self._persona_path(persona_id)):
                raise ValueError(
                    f"角色卡文件名冲突：{os.path.basename(self._persona_path(persona_id))} 已被占用"
                )

            persona = {
                "id": persona_id,
                "name": name,
                "avatar_path": self._normalize_avatar_path(data.get("avatar_path")),
                "summary": (data.get("summary") or "").strip(),
                "core_prompts": {
                    "personality": (data.get("core_prompts", {}).get("personality") or "").strip(),
                    "scenario": (data.get("core_prompts", {}).get("scenario") or "").strip(),
                    "examples_of_dialogue": (data.get("core_prompts", {}).get("examples_of_dialogue") or "").strip(),
                    "first_message": (data.get("core_prompts", {}).get("first_message") or "").strip(),
                },
                "quill_extensions": {
                    "wb_mode": self._normalize_mode(data.get("quill_extensions", {}).get("wb_mode", "disabled")),
                    "bound_worldbooks": data.get("quill_extensions", {}).get("bound_worldbooks", []),
                    "rag_mode": self._normalize_mode(data.get("quill_extensions", {}).get("rag_mode", "disabled")),
                    "bound_rag_docs": data.get("quill_extensions", {}).get("bound_rag_docs", []),
                    "wr_mode": self._normalize_mode(data.get("quill_extensions", {}).get("wr_mode", "disabled")),
                    "bound_writing_resource": data.get("quill_extensions", {}).get("bound_writing_resource", []),
                },
            }
            path = self._persona_path(persona_id)
            await self._write_file(path, persona)
            # 缓存存 deepcopy，返回 deepcopy
            self._cache[persona_id] = copy.deepcopy(persona)
            logger.info(f"[QuillPersona] 已创建角色卡: {persona_id}")
            return copy.deepcopy(persona)

    async def update_persona(self, persona_id: str, data: dict) -> dict:
        """更新角色卡（支持部分更新）。

        S2-6 修复：缓存与返回值均为 deepcopy，防止调用方 mutate 污染缓存。
        """
        async with self._locks[persona_id]:
            existing = await self.get_persona(persona_id)
            if not existing:
                raise ValueError(f"角色卡不存在: {persona_id}")

            name = data.get("name")
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("角色名称 (name) 不能为空")
                if await self._name_exists(name, exclude_id=persona_id):
                    raise ValueError(f"角色名 '{name}' 已存在")
                existing["name"] = name

            for field in ("avatar_path", "summary"):
                if field in data:
                    existing[field] = (
                        self._normalize_avatar_path(data[field])
                        if field == "avatar_path" else (data[field] or "").strip()
                    )

            if "core_prompts" in data and isinstance(data["core_prompts"], dict):
                cp = existing.setdefault("core_prompts", {})
                for field in ("personality", "scenario", "examples_of_dialogue", "first_message"):
                    if field in data["core_prompts"]:
                        cp[field] = (data["core_prompts"][field] or "").strip()

            if "quill_extensions" in data and isinstance(data["quill_extensions"], dict):
                qe = existing.setdefault("quill_extensions", {})
                for field in ("wb_mode", "rag_mode", "wr_mode"):
                    if field in data["quill_extensions"]:
                        value = self._normalize_mode(data["quill_extensions"][field])
                        if value != data["quill_extensions"][field]:
                            logger.warning(
                                f"[QuillPersona] 非法 {field}={data['quill_extensions'][field]!r}，"
                                f"已归一化为 disabled"
                            )
                        qe[field] = value
                for field in ("bound_worldbooks", "bound_rag_docs", "bound_writing_resource"):
                    if field in data["quill_extensions"]:
                        qe[field] = data["quill_extensions"][field] or []

            path = self._persona_path(persona_id)
            await self._write_file(path, existing)
            # S2-6: 缓存 deepcopy，返回 deepcopy
            async with self._cache_lock:
                self._cache[persona_id] = copy.deepcopy(existing)
            logger.info(f"[QuillPersona] 已更新角色卡: {persona_id}")
            return copy.deepcopy(existing)

    async def delete_persona(self, persona_id: str) -> bool:
        """删除角色卡及其关联的头像文件。

        S2-7 修复：加 _cache_lock 保护，避免与 load_all/create_persona 竞争。
        B9 修复：与 update_persona 同序取「每卡锁 → 缓存锁」。此前只取缓存锁，
        而 update 在「读到 deepcopy」与「写回 + 回填缓存」之间不持缓存锁——
        先删后写回会让刚删掉的卡复活（头像却已删）。锁序与 update 一致，
        不存在 AB-BA 反转。
        """
        async with self._locks[persona_id]:
            async with self._cache_lock:
                path = self._persona_path(persona_id)
                exists = await asyncio.to_thread(os.path.isfile, path)
                if not exists:
                    raise ValueError(f"角色卡不存在: {persona_id}")
                # 读取角色卡数据以获取头像路径
                try:
                    data = await self._read_file(path)
                    if data:
                        avatar_path = data.get("avatar_path", "")
                        if avatar_path and avatar_path.startswith("quill_avatars/"):
                            avatar_filename = avatar_path[len("quill_avatars/"):]
                            await self.delete_avatar(avatar_filename)
                            logger.info(f"[QuillPersona] 已删除头像: {avatar_filename}")
                except Exception as e:
                    logger.warning(f"[QuillPersona] 读取头像路径失败: {e}")
                await asyncio.to_thread(os.remove, path)
                self._cache.pop(persona_id, None)
                logger.info(f"[QuillPersona] 已删除角色卡: {persona_id}")
        # S3-3: 清理 _locks 中的孤儿锁，防止 defaultdict 无限增长。
        # 必须等锁释放后再 pop——持锁时 pop，并发调用者会拿到另一把新锁，
        # 同一 id 的两个操作就不再互斥。
        self._locks.pop(persona_id, None)
        return True

    async def get_persona_count(self) -> int:
        """返回角色卡数量。"""
        return len(await self.load_all())

    # ── V2 角色卡标准兼容 ─────────────────────────────────────

    @staticmethod
    def parse_v2_card(raw_data: bytes, is_image: bool = False) -> dict:
        """
        解析 V2 角色卡数据，返回 Quill 格式的角色数据。
        支持 PNG/JPG 图片（含元数据）和纯 JSON。
        """
        if is_image:
            v2_json = QuillPersonaManager._extract_v2_from_image(raw_data)
        else:
            v2_json = json.loads(raw_data.decode('utf-8'))

        # L13 修复：JSON 根不是对象时原先直接 `v2_json.get(...)` → 抛
        # `AttributeError: 'list' object has no attribute 'get'`，经面板冒出去
        # 是一句英文 Python 错误串。这里提前归一成明确的中文格式错误，
        # 与下方已有的 `raise ValueError("不是有效的 V2 角色卡格式")` 同一口径。
        if not isinstance(v2_json, dict):
            raise ValueError("不是有效的 V2 角色卡格式：JSON 根必须是对象")

        # 验证 V2 格式（标准格式 + 扁平化格式 + 最小字段集）
        # Issue #1：V3 卡（spec: chara_card_v3）与 V2 同构（嵌套 data），一并放行。
        has_spec = v2_json.get("spec") in ("chara_card_v2", "chara_card_v3")
        has_data = "data" in v2_json
        has_v2_standard_fields = any(k in v2_json for k in ("first_mes", "mes_example", "metadata"))
        has_v2_flat_fields = any(k in v2_json for k in ("name", "description", "personality"))
        if not (has_spec or has_data or has_v2_standard_fields or has_v2_flat_fields):
            raise ValueError("不是有效的 V2 角色卡格式")

        data = v2_json.get("data", v2_json)
        # L13：`data` 允许是扁平化写法里的任意值，仍需是对象才能取键。
        if not isinstance(data, dict):
            raise ValueError("不是有效的 V2 角色卡格式：data 字段必须是对象")

        # V2 → Quill 字段映射
        # L13 修复：name 为数字/列表时 `(data.get("name") or "未命名角色").strip()`
        # 会抛 `int.strip()`/`list.strip()` 的 AttributeError（英文错误串直接
        # 冒到面板）。这里统一先转字符串再 strip，并只在结果非空时采用。
        _raw_name = data.get("name")
        name = (str(_raw_name).strip() if _raw_name is not None else "") or "未命名角色"
        description = data.get("description", "")
        personality = data.get("personality", "")
        scenario = data.get("scenario", "")
        first_mes = data.get("first_mes", "")
        mes_example = data.get("mes_example", "")

        # 智能映射：description 是 V2 的「设定主体」，personality 是「性格摘要」。
        # M9：**不再截断 description**。旧实现 `personality = description[:2000]`
        # 把 2000 字之后的内容永久丢弃（SillyTavern 角色卡普遍远超 2000 字），
        # 而导出时又用 `summary + "\n\n" + personality` 拼回 description，
        # 使前 500 字重复出现（实测 1800 字 → 2302 字）。
        if description and not personality:
            personality = description
        elif description and personality and description != personality:
            # 两个字段都写了：旧实现只把 personality 当人设、description 仅留
            # 前 500 字进 summary —— 角色设定主体整段不会进提示词。这里把
            # description 作为主体、personality 追加在后，两者都不丢；
            # 若 personality 已被包含在 description 里则不重复拼接。
            if personality.strip() not in description:
                personality = f"{description}\n\n{personality}"

        # 清理 mes_example 中的 V2 格式标记
        if mes_example:
            mes_example = QuillPersonaManager._clean_v2_mes_example(mes_example)

        # Issue #1：V3 规范字段此前没有导入路径，而类脑生态的卡常把
        # system_prompt / post_history_instructions 当主要设定载体，丢弃即
        # 「导入后设定全丢」。原字段注入位在对话两端，轻量导入带标注并入
        # personality；alternate_greetings 是开场白变体（不是对话示例），
        # 带标注并入 examples_of_dialogue 保数据，LLM 按标注理解语义。
        _sp = str(data.get("system_prompt") or "").strip()
        _ph = str(data.get("post_history_instructions") or "").strip()
        if _sp:
            personality = f"{personality}\n\n【系统提示】\n{_sp}".strip()
        if _ph:
            personality = f"{personality}\n\n【后置指令】\n{_ph}".strip()
        _alt = [str(x).strip() for x in (data.get("alternate_greetings") or []) if str(x).strip()]
        if _alt:
            _alt_txt = "\n\n".join(_alt)
            mes_example = (f"{mes_example}\n\n【备选开场白】\n{_alt_txt}" if mes_example
                           else f"【备选开场白】\n{_alt_txt}")

        # M0-T1 收口：本插件导出的卡把身份与绑定放在
        # data.extensions.quillplus——优先读回（id 保住 = 记忆会话键/
        # 绑定不断链）。原生 ST 卡没有该块，走既有缺省路径（id 由
        # create_persona 生成）。
        qp: dict = {}
        ext_block = data.get("extensions")
        if isinstance(ext_block, dict) and isinstance(ext_block.get("quillplus"), dict):
            qp = ext_block["quillplus"] or {}

        out = {
            "name": name,
            "summary": description[:500] if description else "",
            "core_prompts": {
                "personality": personality,
                "scenario": scenario,
                "first_message": first_mes,
                "examples_of_dialogue": mes_example,
            }
        }
        _qp_id = str(qp.get("id", "") or "").strip()
        if _qp_id:
            out["id"] = _qp_id
        _qp_ext = qp.get("quill_extensions")
        if isinstance(_qp_ext, dict) and _qp_ext:
            out["quill_extensions"] = _qp_ext
        # Issue #1：内嵌世界书原样透传给调用方（面板导入 / /char import），由
        # attach_embedded_worldbook 落盘并写回绑定。create_persona 按固定键白名单
        # 落库，未接线的调用路径会静默丢弃该键——保持「不认识不落库」。
        _book = data.get("character_book")
        if isinstance(_book, dict) and _book.get("entries"):
            out["_character_book"] = _book
        return out

    @staticmethod
    def _extract_v2_from_image(image_data: bytes) -> dict:
        """从 PNG/JPG 图片中提取 V2 JSON 数据。
        多层递进解析：tEXt多key搜索 → EXIF → 原始字节Base64/JSON搜索。
        """
        try:
            from PIL import Image
        except ImportError:
            raise ImportError(
                "需要安装 Pillow 库才能解析图片中的角色卡数据: pip install Pillow"
            )

        img = Image.open(BytesIO(image_data))

        # 1. 检查 img.info（标准 tEXt 块）— 多 key 兼容
        # Issue #1：V3 PNG 把完整卡 JSON 放在 ccv3 键（chara 里是 V2 兼容副本，
        # 缺 V3 专属字段），优先取 ccv3。
        for key in ('ccv3', 'chara', 'TavernAI', 'sillytavern', 'SillyTavern',
                    'character', 'comment', 'description', 'title', 'data'):
            val = img.info.get(key, '')
            if not val:
                continue
            # L14 修复：原先写 `val.encode('ascii')`——tEXt 块里含中文时直接抛
            # UnicodeEncodeError，此时 `raw` **尚未绑定**；而 except 分支又引用
            # `raw`。循环变量跨迭代存活，所以它不抛 NameError，而是静默拿**上一个
            # key 的字节**去解码（key 顺序固定时等于拿错了数据）。这里先安全地把
            # 值转成 bytes（bytes 原样、str 用 utf-8），保证 raw 一定与当前 val 对应。
            try:
                raw = val if isinstance(val, bytes) else str(val).encode('utf-8')
            except Exception:
                logger.debug("[QuillPersona] V2 tEXt 块 %s 取值无法转为字节，跳过", key, exc_info=True)
                continue
            try:
                decoded = base64.b64decode(raw).decode('utf-8')
                return json.loads(decoded)
            except Exception:
                try:
                    s = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else str(raw)
                    if s.startswith('{'):
                        return json.loads(s)
                except Exception:
                    logger.debug("[QuillPersona] V2 tEXt 块解析失败 (key=%s)", key, exc_info=True)

        # 2. 检查 EXIF tag 37510 (UserComment) 和 270 (ImageDescription)
        try:
            exif = img.getexif()
            if exif:
                for tag in (37510, 270):
                    val = exif.get(tag)
                    if not val:
                        continue
                    if isinstance(val, bytes):
                        if val[:8].startswith(b'ASCII') or val[:8].startswith(b'UNICODE'):
                            val = val[8:]
                        try:
                            decoded = base64.b64decode(val).decode('utf-8')
                            return json.loads(decoded)
                        except Exception:
                            try:
                                decoded = val.decode('utf-8', errors='ignore')
                                if decoded.startswith('{'):
                                    return json.loads(decoded)
                            except Exception:
                                logger.debug("[QuillPersona] V2 EXIF 解析失败", exc_info=True)
                    elif isinstance(val, str):
                        try:
                            decoded = base64.b64decode(val).decode('utf-8')
                            return json.loads(decoded)
                        except Exception:
                            if val.startswith('{'):
                                try:
                                    return json.loads(val)
                                except Exception:
                                    logger.debug("[QuillPersona] V2 EXIF 字符串解析失败", exc_info=True)
        except Exception:
            logger.debug("[QuillPersona] V2 图片元数据提取异常", exc_info=True)

        # 3. 原始字节搜索 — Base64 编码的 JSON（chara\x00 / ccv3\x00 标记）。
        # Issue #1：补 ccv3（V3 PNG 的 tEXt 块 keyword\x00text 形态），优先于 chara。
        for marker in (b'ccv3\x00', b'chara\x00', b'chara\x01'):
            idx = image_data.find(marker)
            if idx >= 0:
                payload_start = idx + len(marker)
                payload_end = payload_start
                while payload_end < len(image_data) and image_data[payload_end] in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r\t ':
                    payload_end += 1
                b64_str = image_data[payload_start:payload_end].decode('ascii', errors='ignore')
                # 修复：trim 到 4 的倍数，防止跨 chunk 多收字符导致 base64 解码失败
                trim_len = len(b64_str) - (len(b64_str) % 4)
                b64_str = b64_str[:trim_len]
                try:
                    decoded = base64.b64decode(b64_str).decode('utf-8')
                    return json.loads(decoded)
                except Exception:
                    logger.debug("[QuillPersona] V2 Base64 原始字节搜索失败", exc_info=True)

        # 4. 原始字节搜索 — 直接的 JSON 结构（无 Base64 编码）
        for marker in (b'"name":', b'"name": ', b'"description":', b'"personality":',
                       b'"spec":', b'"first_mes":', b'"data":{'):
            idx = image_data.find(marker)
            if idx >= 0:
                # 向前找到 {
                brace_start = image_data.rfind(b'{', 0, idx)
                if brace_start < 0:
                    brace_start = idx
                # 向后找到匹配的 }
                depth = 0
                brace_end = brace_start
                for i in range(brace_start, min(brace_start + 5000000, len(image_data))):
                    if image_data[i:i+1] == b'{':
                        depth += 1
                    elif image_data[i:i+1] == b'}':
                        depth -= 1
                        if depth == 0:
                            brace_end = i + 1
                            break
                if depth == 0 and brace_end > brace_start:
                    try:
                        json_str = image_data[brace_start:brace_end].decode('utf-8', errors='ignore')
                        return json.loads(json_str)
                    except Exception:
                        logger.debug("[QuillPersona] V2 JSON 原始字节搜索失败", exc_info=True)

        # 判断是否为 PNG 但已被压缩（无文本块）
        is_png = image_data[:8] == b'\x89PNG\r\n\x1a\n'
        has_text_chunks = b'tEXt' in image_data or b'iTXt' in image_data or b'zTXt' in image_data
        if is_png and not has_text_chunks:
            raise ValueError(
                "PNG 图片已被社交媒体压缩，所有元数据已丢失。请使用「文本粘贴导入」功能，"
                "或从原平台下载原始 PNG 文件。"
            )

        raise ValueError(
            "未找到 V2 角色卡数据（图片可能不是角色卡，或元数据已丢失）"
        )

    @staticmethod
    def _clean_v2_mes_example(text: str) -> str:
        """清理 V2 对话示例中的格式标记。"""
        # 移除 <START> 分隔符
        text = re.sub(r'\n?\s*<START>\s*\n?', '\n---\n', text, flags=re.IGNORECASE)
        # 保留 {{user}} 和 {{char}} 占位符
        return text.strip()

    def parse_clipboard_text(self, text: str) -> dict:
        """解析剪贴板文本（V2 JSON 或纯文本/W++ 格式）→ Quill 角色数据。"""
        text = text.strip()

        # 1. 尝试解析为 V2 JSON
        try:
            data = json.loads(text)
            # L13：JSON 根可能是数组/数字/字符串，`data.get` 会直接抛
            # AttributeError 并穿透 except 元组（只捕 JSONDecodeError/ValueError/
            # UnicodeDecodeError）→ 面板收到英文错误串。先判类型。
            if not isinstance(data, dict):
                raise ValueError("剪贴板 JSON 根不是对象")
            # Issue #1：V3 卡剪贴板导入与文件导入同口径放行
            if data.get("spec") in ("chara_card_v2", "chara_card_v3"):
                return self.parse_v2_card(text.encode('utf-8'), is_image=False)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            logger.debug("[QuillPersona] 剪贴板文本非 V2 JSON，降级为纯文本解析", exc_info=True)

        # 2. 纯文本（W++ / Raw Text）解析
        def extract_block(label_regex: str, source_text: str) -> str:
            """提取标签后的内容直到下一个标签或文本结束"""
            pattern = rf'{label_regex}\s*[=:]?\s*\n?(.*?)(?=\n\s*[A-Z][a-zA-Z_]+\s*[=:]|\Z)'
            match = re.search(pattern, source_text, re.DOTALL | re.IGNORECASE)
            val = match.group(1) if match else ""
            return (val or "").strip()

        # 2.1 提取角色名（安全判空）
        name_match = re.search(r'Name\s*=\s*["\']?([^"\'\n]+)["\']?', text, re.IGNORECASE)
        name_val = name_match.group(1) if name_match else ""
        name = (name_val or "").strip() or "未命名角色 (需手动修改)"

        # 2.2 提取各区块
        personality = extract_block(r'Personality', text)
        scenario = extract_block(r'Scenario', text)
        first_message = extract_block(r'First\s*Message|First_mes|FirstMes', text)
        examples = extract_block(r'Example\s*Dialogs|Example_Dialog|mes_example|Examples', text)
        description = extract_block(r'Description|Summary', text)

        # 2.3 智能合并 description → personality（M9：不截断，理由同 parse_v2_card）
        if not personality and description:
            personality = description

        return {
            "name": name,
            "summary": description[:500] if description else "",
            "core_prompts": {
                "personality": personality,
                "scenario": scenario,
                "first_message": first_message,
                "examples_of_dialogue": examples,
            }
        }

    async def attach_embedded_worldbook(self, persona_data: dict, wb_manager,
                                        enabled: bool = True) -> Optional[dict]:
        """Issue #1：把 parse_v2_card 透传的内嵌世界书（"_character_book"）落盘为
        羽笔世界书，并把绑定写回 persona_data（wb_mode=custom + bound_worldbooks）。

        必须在 create_persona **之前**调用——create_persona 按固定键白名单落库，
        绑定要先写进 quill_extensions 才能存活。开关关闭 / wb_manager 未加载 /
        无内嵌书时仅取出并丢弃 staging 键，返回 None。落盘异常同样降级为警告并
        返回 None（角色卡仍导入，与头像保存失败同一姿态）。create_persona 若失败，
        已落盘的世界书保留不回收：书名由卡名派生，可能与既有同名卡共用同一本，
        自动删除会误伤既有绑定。
        """
        book = persona_data.pop("_character_book", None)
        if not enabled or wb_manager is None or not isinstance(book, dict):
            return None
        try:
            wb_info = await asyncio.to_thread(
                wb_manager.save_embedded_worldbook, str(persona_data.get("name") or ""), book,
            )
        except Exception as exc:
            logger.warning("[Quill] 内嵌世界书转换失败（角色卡仍会导入）: %s", exc)
            return None
        if not wb_info:
            return None
        qext = persona_data.get("quill_extensions")
        if not isinstance(qext, dict):
            qext = {}
            persona_data["quill_extensions"] = qext
        bound = [str(b) for b in (qext.get("bound_worldbooks") or []) if str(b)]
        if wb_info["name"] not in bound:
            bound.append(wb_info["name"])
        qext["bound_worldbooks"] = bound
        # wb_mode 缺省是 disabled，只写 bound_worldbooks 不改 mode 绑定不生效。
        qext["wb_mode"] = "custom"
        return wb_info

    def export_v2_card(self, persona: dict, avatar_data: bytes = None) -> bytes:
        """
        将 Quill 角色卡导出为 V2 格式。
        如果有头像图片，将 JSON 嵌入到图片元数据中；
        如果没有头像图片，返回纯 JSON。
        """
        # 构建 V2 JSON 结构
        # M9：description 只放「角色设定主体」= personality，**不再拼接 summary**。
        # 旧实现把 description 一拆为二（summary=前 500 字、personality=前 2000 字），
        # 导出时再拼回 `summary + "\n\n" + personality`，于是前 500 字重复出现、
        # 2000 字以后的内容直接消失 —— 一张卡在 Quill 里导入再导出就不再是原来
        # 那张卡了（且原文件已被覆盖，不可恢复）。现在 description 与 personality
        # 保持同一份全量文本，summary 仅作为列表用的短摘要留在 Quill 侧，
        # 不再参与 V2 往返。
        cp = persona.get("core_prompts", {})
        personality = cp.get("personality", "")
        summary = persona.get("summary", "")
        if personality and summary:
            if personality.startswith(summary):
                # 本修复后导入的卡：summary 只是 personality 的前缀摘要 →
                # 直接用全量 personality，往返零膨胀、零丢失。
                description = personality
            else:
                # summary 是用户独立填写的简介（面板可单独改）→ 两段都保留。
                description = summary + "\n\n" + personality
        else:
            description = personality or summary
        v2_data = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": persona.get("name", ""),
                "description": description,
                "personality": personality,
                "scenario": persona.get("core_prompts", {}).get("scenario", ""),
                "first_mes": persona.get("core_prompts", {}).get("first_message", ""),
                "mes_example": persona.get("core_prompts", {}).get("examples_of_dialogue", ""),
                "creator": "Quill Plugin",
                "version": "1.0",
                "tags": [],
                "creator_notes": "由 Quill 插件导出的 V2 标准角色卡。",
                "system_prompt": "",
                "post_history_instructions": "",
                "alternate_greetings": [],
                # M0-T1 收口：身份与绑定随卡走。V2 标准的 extensions 命名空间
                # 放本插件私有块（原生 ST 工具忽略未知键），re-import 时
                # _v2_to_quill 优先读这里——不再因导出丢 id 导致导入方重新
                # 生成身份键（记忆会话/世界书绑定随之断链）。
                "extensions": {
                    "quillplus": {
                        "id": persona.get("id", ""),
                        "quill_extensions": persona.get("quill_extensions", {}) or {},
                    }
                },
                "character_book": None,
            }
        }

        # 清理空字段
        v2_data["data"] = {k: v for k, v in v2_data["data"].items() if v not in (None, "", [], {})}

        # 如果有头像图片，嵌入元数据
        if avatar_data:
            return self._embed_v2_in_image(avatar_data, v2_data)

        # 无头像，返回纯 JSON
        return json.dumps(v2_data, ensure_ascii=False, indent=2).encode('utf-8')

    @staticmethod
    def _embed_v2_in_image(image_data: bytes, v2_data: dict) -> bytes:
        """将 V2 JSON 嵌入到 PNG 图片的 tEXt 元数据中。"""
        try:
            from PIL import Image
            from PIL.PngImagePlugin import PngInfo
        except ImportError:
            raise ImportError(
                "需要安装 Pillow 库才能导出图片格式角色卡: pip install Pillow"
            )

        img = Image.open(BytesIO(image_data))

        # 编码为 Base64
        json_str = json.dumps(v2_data, ensure_ascii=False)
        chara_b64 = base64.b64encode(json_str.encode('utf-8')).decode('ascii')

        # 保存为 PNG，嵌入 tEXt
        output = BytesIO()
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
        else:
            img = img.convert('RGB')
        info = PngInfo()
        info.add_text("chara", chara_b64)
        img.save(output, format="PNG", pnginfo=info)

        return output.getvalue()