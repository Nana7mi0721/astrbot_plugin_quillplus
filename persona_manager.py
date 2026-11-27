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
import posixpath
import re
import tempfile
import time
import zipfile
from collections import defaultdict
from io import BytesIO
from typing import Any, Optional
from uuid import uuid4

from astrbot.api import logger

# M4.1：格式识别与字段保真层（纯逻辑，无 astrbot 依赖）。persona_manager 只保留
# 「图片/文本 → JSON 入口」，识别与映射统一委托 card_io，加新格式不用碰落盘逻辑。
try:  # 生产路径按包加载，与 main.py / _route_core.py 的 .quill 相对导入保持一致
    from .quill.services import card_io, card_export
except ImportError:  # 直接 python persona_manager.py 自测时没有包上下文
    from quill.services import card_io, card_export


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

    async def _will_create_conflict(self, data: dict) -> bool:
        """create_persona 是否**必然**因重名/重 ID/文件名冲突而拒绝这份数据。

        Q3 修复用：只在「注定失败」时返回 True——判据逐条对齐 create_persona
        的四个拒绝点（空名、重名、显式 ID 已存在、_sanitize_id 归一后文件名
        已被占用），避免预检比建卡更严而误挡合法导入。空名也算冲突：建卡同样
        会以 `角色名称 (name) 为必填项` 抛错，此时落盘的内嵌世界书会退化成一
        本绑定不到任何卡的书（书名固定为「角色卡_内嵌」）。
        """
        name = (data.get("name") or "").strip()
        if not name or await self._name_exists(name):
            return True
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            # 缺省 ID 由 create_persona 现场生成 12 位 hex，不会与既有卡撞车。
            return False
        if self._cache_loaded and persona_id in self._cache:
            return True
        return await asyncio.to_thread(
            os.path.isfile, self._persona_path(persona_id)
        )

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
                # FUNC-P1-1：.get(k, {}) 的默认值只在键缺失时生效，外部/手改的卡
                # 文件可能是 "core_prompts": null —— 二段 .get 直接 AttributeError。
                # 统一 (x.get(k) or {}) 兜底：null 与缺失同样回落默认。
                "core_prompts": {
                    "personality": ((data.get("core_prompts") or {}).get("personality") or "").strip(),
                    "scenario": ((data.get("core_prompts") or {}).get("scenario") or "").strip(),
                    "examples_of_dialogue": ((data.get("core_prompts") or {}).get("examples_of_dialogue") or "").strip(),
                    "first_message": ((data.get("core_prompts") or {}).get("first_message") or "").strip(),
                },
                # M5.3：系统提示与后置指令独立保存，不再硬拼进 personality。
                "system_prompt": (data.get("system_prompt") or "").strip(),
                "post_history_instructions": (
                    data.get("post_history_instructions") or ""
                ).strip(),
                "quill_extensions": {
                    "wb_mode": self._normalize_mode((data.get("quill_extensions") or {}).get("wb_mode", "disabled")),
                    "bound_worldbooks": (data.get("quill_extensions") or {}).get("bound_worldbooks", []),
                    "rag_mode": self._normalize_mode((data.get("quill_extensions") or {}).get("rag_mode", "disabled")),
                    "bound_rag_docs": (data.get("quill_extensions") or {}).get("bound_rag_docs", []),
                    "wr_mode": self._normalize_mode((data.get("quill_extensions") or {}).get("wr_mode", "disabled")),
                    "bound_writing_resource": (data.get("quill_extensions") or {}).get("bound_writing_resource", []),
                    "wr_preset_id": str(
                        (data.get("quill_extensions") or {}).get("wr_preset_id") or ""
                    ).strip(),
                },
            }
            # M4.1：导入保真快照（未映射的原始字段 / 未识别 extensions）随卡落库。
            # 只有导入路径会带该键；手建卡没有，不写空对象（避免每张卡多一段噪音）。
            card_source = data.get("card_source")
            if isinstance(card_source, dict) and card_source:
                persona["card_source"] = card_source
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

            for field in ("system_prompt", "post_history_instructions"):
                if field in data:
                    existing[field] = (data[field] or "").strip()

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
                if "wr_preset_id" in data["quill_extensions"]:
                    qe["wr_preset_id"] = str(
                        data["quill_extensions"]["wr_preset_id"] or ""
                    ).strip()

            # M4.1：重新导入同一张卡时可整块替换保真快照；不传则原样保留。
            # 编辑设定（core_prompts）不触碰 card_source —— 「编辑只覆写对应位置」。
            if isinstance(data.get("card_source"), dict):
                existing["card_source"] = data["card_source"]

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
        # P2-14：撤销 S3-3 的孤儿锁 pop。pop 后等待中的调用者仍持旧锁引用，
        # 新来者经 defaultdict 拿到新锁，同一 id 的两个操作失去互斥——极端
        # 时序下旧 update 会把已删的卡重新写回（无头像的「复活卡」，正是 B9
        # 想防的问题）。锁数量等于历史卡数，量级可接受，保留锁即是修复。
        return True

    async def get_persona_count(self) -> int:
        """返回角色卡数量。"""
        return len(await self.load_all())

    # ── V2 角色卡标准兼容 ─────────────────────────────────────

    @staticmethod
    def parse_v2_card(raw_data: bytes, is_image: bool = False) -> dict:
        """解析角色卡数据（PNG/JPG 内嵌元数据或纯 JSON）→ Quill 角色数据。

        M4.1：格式识别、严格 spec 校验、字段映射与「映射 / 保留 / 不支持」报告
        全部委托 quill/services/card_io.py，本方法只保留「图片 → JSON」这段加载。
        返回 dict 额外带 card_source（保真快照，随卡落库）与 _import_report
        （导入报告，由路由 pop 出来回传面板，不落库）。
        """
        if is_image:
            v2_json = QuillPersonaManager._extract_v2_from_image(raw_data)
        elif raw_data[:4] == b"PK\x03\x04":
            # CHARX（ZIP 容器）：根目录 card.json + embeded:// 本地资产。
            v2_json = QuillPersonaManager._extract_from_charx(raw_data)
        else:
            # 非图：M4.1 起不再用 json.loads 之外的宽松入口；错误由 card_io 统一
            # 归一成中文格式错误（值错误一律冒到路由的 400）。
            v2_json = json.loads(raw_data.decode('utf-8'))
        via = "image" if is_image else ("charx" if raw_data[:4] == b"PK\x03\x04" else "json")
        return card_io.parse_json_card(v2_json, via=via)

    #: CHARX 解压安全上限（防 Zip Bomb / 路径穿越 / 符号链接）。
    _CHARX_MAX_ENTRIES = 1024
    _CHARX_MAX_TOTAL_BYTES = 64 * 1024 * 1024
    _CHARX_MAX_FILE_BYTES = 32 * 1024 * 1024
    _CHARX_MAX_RATIO = 1000

    @staticmethod
    def _charx_member_is_safe(info: zipfile.ZipInfo) -> bool:
        """拒绝路径穿越、绝对路径与符号链接条目。"""
        name = info.filename
        if not name or name.endswith("/"):
            return False
        if "\x00" in name:
            return False
        text = name.replace("\\", "/")
        # 盘符（C:/...）、UNC / 绝对路径一律拒绝；Linux 上 os.path.isabs
        # 不认识 "C:\\a"，必须显式匹配盘符。
        if re.match(r"^[A-Za-z]:", text) or text.startswith("/") or text.startswith("//"):
            return False
        # POSIX 归一化（与运行平台无关），禁止跳出根目录。
        norm = posixpath.normpath(text)
        if norm == ".." or norm.startswith("../") or "/../" in "/" + norm + "/":
            return False
        # 符号链接：external_attr 高 16 位是 Unix mode，S_IFLNK = 0xA000。
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            return False
        return True

    @staticmethod
    def _extract_from_charx(raw_data: bytes) -> dict:
        """从 CHARX（ZIP）容器提取根目录 card.json。

        ``embeded://``（规范拼写为单 d）本地资产只在卡内引用层面保留，
        远程 http/https 一律不下载；Risu/第三方脚本字段原样保留但不执行。
        """
        try:
            archive = zipfile.ZipFile(BytesIO(raw_data))
        except zipfile.BadZipFile as exc:
            raise ValueError("CHARX 文件不是合法的 ZIP 容器") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > QuillPersonaManager._CHARX_MAX_ENTRIES:
                raise ValueError(
                    f"CHARX 条目数超过上限（{QuillPersonaManager._CHARX_MAX_ENTRIES}）"
                )
            total_uncompressed = 0
            total_compressed = 0
            for info in infos:
                if info.is_dir():
                    continue
                if not QuillPersonaManager._charx_member_is_safe(info):
                    raise ValueError(f"CHARX 含不安全的条目名：{info.filename!r}")
                if info.file_size > QuillPersonaManager._CHARX_MAX_FILE_BYTES:
                    raise ValueError(f"CHARX 单文件过大：{info.filename!r}")
                total_uncompressed += max(0, info.file_size)
                total_compressed += max(1, info.compress_size)
            if total_uncompressed > QuillPersonaManager._CHARX_MAX_TOTAL_BYTES:
                raise ValueError("CHARX 解压总量超过上限（64MB），疑似 Zip Bomb")
            if total_compressed > 0 and total_uncompressed / total_compressed > QuillPersonaManager._CHARX_MAX_RATIO:
                raise ValueError("CHARX 压缩比异常（疑似 Zip Bomb），已拒绝解析")

            # 根目录 card.json 优先（大小写不敏感）。
            target = None
            for info in infos:
                if info.is_dir():
                    continue
                norm = posixpath.normpath(info.filename.replace("\\", "/"))
                if norm.lower() == "card.json":
                    target = info
                    break
            if target is None:
                raise ValueError("CHARX 缺少根目录 card.json")
            try:
                # 流式限读：info.file_size 来自压缩包声明，不可信；多读 1 字节
                # 用于判断是否超限，避免伪造头部导致 OOM。
                limit = QuillPersonaManager._CHARX_MAX_FILE_BYTES
                with archive.open(target) as fh:
                    raw = fh.read(limit + 1)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError("CHARX 的 card.json 无法读取") from exc
            if len(raw) > limit:
                raise ValueError("CHARX 的 card.json 超过单文件大小上限")
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("CHARX 的 card.json 不是合法 UTF-8 JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("CHARX 的 card.json 根必须是对象 {...}")
            return parsed

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

        # 统一上下文管理器释放文件句柄，并按实际尺寸拒绝超大图。
        # 不使用 Image.MAX_IMAGE_PIXELS 全局变量：它是进程级状态，修改会
        # 污染同进程其他模块，且并发下不稳定。
        with Image.open(BytesIO(image_data)) as _img:
            if int(_img.width) * int(_img.height) > 64_000_000:
                raise ValueError("图片像素超过安全上限（64MP）")
            _info = dict(_img.info or {})
            try:
                _exif = _img.getexif()
            except Exception:
                _exif = None

        # 1. 检查 img.info（标准 tEXt 块）— 多 key 兼容
        # Issue #1：V3 PNG 把完整卡 JSON 放在 ccv3 键（chara 里是 V2 兼容副本，
        # 缺 V3 专属字段），优先取 ccv3。
        for key in ('ccv3', 'chara', 'TavernAI', 'sillytavern', 'SillyTavern',
                    'character', 'comment', 'description', 'title', 'data'):
            val = _info.get(key, '')
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
            exif = _exif
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
        """清理对话示例中的 <START> 标记（委托 card_io：正则口径只保留一份）。"""
        return card_io.clean_mes_example(text)

    def parse_clipboard_text(self, text: str) -> dict:
        """解析剪贴板文本（JSON / W++ 标签文本 / 纯文本）→ Quill 角色数据。

        M4.1：本方法只做入口委托，识别与字段映射在 card_io.parse_text_card。
        保留既有口径（FUNC-P1-3）：文本是 JSON 但卡格式不合法时，CardFormatError
        必须原样上抛（路由转 400），不许降级成标签分支产出一张空卡；只有根本
        不是 JSON 的普通文本才走标签 / 纯文本解析，且纯文本回退带
        confidence=0.25 与明确警告。
        """
        return card_io.parse_text_card(text)

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

        Q3 修复（数据破坏）：落盘与建卡是两步，而**重名/重 ID 只会在第二步被
        拒绝**。于是「导入一张与库中同名的卡」会先覆盖掉旧卡的同名内嵌世界书
        （用户可能已手工微调过），再被 `create_persona` 以 400 打回——导入失败
        了，旧书却已被毁。这里在落盘前先跑一遍与 create_persona 完全同判据的
        预检（空名 / 重名 / 显式 ID 冲突 / 文件名冲突），注定失败的导入绝不
        提前写盘。判据必须与 create_persona 保持一致，否则会出现「预检放过、
        建卡拒绝」的同一坑，或反过来让本来能成功的导入被误挡。
        """
        book = persona_data.pop("_character_book", None)
        if not enabled or wb_manager is None or not isinstance(book, dict):
            return None
        if await self._will_create_conflict(persona_data):
            # 不记录名字本身：日志会进用户可读的后端日志，卡名属内容。
            logger.info(
                "[QuillPersona] 目标角色卡与既有卡冲突，跳过内嵌世界书转换"
                "（避免覆盖已绑定的世界书）"
            )
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
        cp = persona.get("core_prompts") or {}
        personality = cp.get("personality") or ""
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
                "scenario": (persona.get("core_prompts") or {}).get("scenario") or "",
                "first_mes": (persona.get("core_prompts") or {}).get("first_message") or "",
                "mes_example": (persona.get("core_prompts") or {}).get("examples_of_dialogue") or "",
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

    def export_card(self, persona: dict, version: str = "v2", books: list = None,
                    include_id: bool = True, avatar_data: bytes = None,
                    container: str = "auto") -> dict:
        """导出 V2 / V3 角色卡（M4.2）。返回 {data, filename, warnings, payload}。

        真正的字段组装在 quill/services/card_export.py（纯函数），这里只负责
        「要不要包进 PNG」以及文件名。container:
          - json：永远给 JSON，哪怕有头像
          - png：必须有头像，否则报错（不静默降级成 JSON，用户选的是 PNG）
          - auto：有头像给 PNG，没有给 JSON
        """
        container = (container or "auto").lower()
        if container not in ("auto", "json", "png"):
            raise ValueError("载体只支持 auto / json / png")
        if container == "png" and not avatar_data:
            raise ValueError("该角色卡没有头像，无法导出 PNG；请改选 JSON")
        payload, warnings = card_export.build_card(persona, version, books, include_id)
        use_png = bool(avatar_data) and container != "json"
        if use_png:
            data = self._embed_card_in_image(avatar_data, payload)
        else:
            data = card_export.dump(payload).encode("utf-8")
        return {
            "data": data,
            "filename": card_export.filename(persona, version, "png" if use_png else "json"),
            "warnings": warnings,
            "payload": payload,
            "media_type": "image/png" if use_png else "application/json",
        }

    @staticmethod
    def _embed_card_in_image(image_data: bytes, payload: dict) -> bytes:
        """把卡负载嵌入 PNG 的 tEXt 元数据。

        V3 卡同时写 chara 与 ccv3 两个键：SillyTavern 导出 V3 时就是这么写的，
        只写 ccv3 的卡在只认 chara 的旧工具里会「有图无卡」。
        """
        try:
            from PIL import Image
            from PIL.PngImagePlugin import PngInfo
        except ImportError:
            raise ImportError(
                "需要安装 Pillow 库才能导出图片格式角色卡: pip install Pillow"
            )

        json_str = json.dumps(payload, ensure_ascii=False)
        card_b64 = base64.b64encode(json_str.encode('utf-8')).decode('ascii')

        output = BytesIO()
        with Image.open(BytesIO(image_data)) as src:
            if int(src.width) * int(src.height) > 64_000_000:
                raise ValueError("图片像素超过安全上限（64MP）")
            img = src.convert('RGBA' if src.mode in ('RGBA', 'LA', 'P') else 'RGB')
        info = PngInfo()
        if payload.get("spec") == "chara_card_v3":
            # V3 载体的规范形态：ccv3 放完整 V3，chara 放 V2 兼容副本，
            # 供只认 chara 的旧工具读取。
            compat = QuillPersonaManager._v2_compat_payload(payload)
            compat_b64 = base64.b64encode(
                json.dumps(compat, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            info.add_text("chara", compat_b64)
            info.add_text("ccv3", card_b64)
        else:
            info.add_text("chara", card_b64)
        img.save(output, format="PNG", pnginfo=info)

        return output.getvalue()

    @staticmethod
    def _v2_compat_payload(payload: dict) -> dict:
        """把 V3 载荷裁成 V2 兼容副本（chara 键用），不修改原对象。"""
        compat = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        compat["spec"] = "chara_card_v2"
        compat["spec_version"] = "2.0"
        data = compat.get("data")
        if isinstance(data, dict):
            for key in (
                "nickname", "group_only_greetings", "assets",
                "creator_notes_multilingual", "source",
                "creation_date", "modification_date",
            ):
                data.pop(key, None)
        return compat

    @staticmethod
    def _embed_v2_in_image(image_data: bytes, v2_data: dict) -> bytes:
        """兼容旧调用点：V2 负载等价于 _embed_card_in_image。"""
        return QuillPersonaManager._embed_card_in_image(image_data, v2_data)
