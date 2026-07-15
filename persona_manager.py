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

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


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

    async def read_avatar(self, filename: str) -> Optional[bytes]:
        """读取头像文件数据。"""
        path = os.path.join(self.avatar_dir, filename)
        if not os.path.isfile(path):
            return None
        return await asyncio.to_thread(self._sync_read_bytes, path)

    @staticmethod
    def _sync_read_bytes(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    async def delete_avatar(self, filename: str) -> bool:
        """删除头像文件。"""
        path = os.path.join(self.avatar_dir, filename)
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
            data = await self._read_file(path)
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

            persona_id = (data.get("id") or "").strip() or name
            if self._cache_loaded and persona_id in self._cache:
                raise ValueError(f"角色 ID '{persona_id}' 已存在")

            persona = {
                "id": persona_id,
                "name": name,
                "avatar_path": (data.get("avatar_path") or "").strip(),
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
                    existing[field] = (data[field] or "").strip()

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
        """
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
            # S3-3: 清理 _locks 中的孤儿锁，防止 defaultdict 无限增长
            self._locks.pop(persona_id, None)
            logger.info(f"[QuillPersona] 已删除角色卡: {persona_id}")
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

        # 验证 V2 格式（标准格式 + 扁平化格式 + 最小字段集）
        has_spec = v2_json.get("spec") == "chara_card_v2"
        has_data = "data" in v2_json
        has_v2_standard_fields = any(k in v2_json for k in ("first_mes", "mes_example", "metadata"))
        has_v2_flat_fields = any(k in v2_json for k in ("name", "description", "personality"))
        if not (has_spec or has_data or has_v2_standard_fields or has_v2_flat_fields):
            raise ValueError("不是有效的 V2 角色卡格式")

        data = v2_json.get("data", v2_json)

        # V2 → Quill 字段映射
        name = (data.get("name") or "未命名角色").strip()
        description = data.get("description", "")
        personality = data.get("personality", "")
        scenario = data.get("scenario", "")
        first_mes = data.get("first_mes", "")
        mes_example = data.get("mes_example", "")

        # 智能映射：如果 description 存在但 personality 为空，尝试拆分
        if description and not personality:
            personality = description[:2000] if len(description) > 2000 else description

        # 清理 mes_example 中的 V2 格式标记
        if mes_example:
            mes_example = QuillPersonaManager._clean_v2_mes_example(mes_example)

        return {
            "name": name,
            "summary": description[:500] if description else "",
            "core_prompts": {
                "personality": personality,
                "scenario": scenario,
                "first_message": first_mes,
                "examples_of_dialogue": mes_example,
            }
        }

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
        for key in ('chara', 'TavernAI', 'sillytavern', 'SillyTavern',
                    'character', 'comment', 'description', 'title', 'data'):
            val = img.info.get(key, '')
            if not val:
                continue
            try:
                raw = val if isinstance(val, bytes) else val.encode('ascii')
                decoded = base64.b64decode(raw).decode('utf-8')
                return json.loads(decoded)
            except Exception:
                try:
                    s = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else str(raw)
                    if s.startswith('{'):
                        return json.loads(s)
                except Exception:
                    logger.debug("[QuillPersona] V2 tEXt 块解析失败", exc_info=True)

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

        # 3. 原始字节搜索 — Base64 编码的 JSON（chara\x00 标记）
        for marker in (b'chara\x00', b'chara\x01'):
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
            if data.get("spec") == "chara_card_v2":
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

        # 2.3 智能合并 description → personality
        if not personality and description:
            personality = description[:2000] if len(description) > 2000 else description

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

    def export_v2_card(self, persona: dict, avatar_data: bytes = None) -> bytes:
        """
        将 Quill 角色卡导出为 V2 格式。
        如果有头像图片，将 JSON 嵌入到图片元数据中；
        如果没有头像图片，返回纯 JSON。
        """
        # 构建 V2 JSON 结构
        v2_data = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": persona.get("name", ""),
                "description": persona.get("summary", ""),
                "personality": persona.get("core_prompts", {}).get("personality", ""),
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
                "extensions": {},
                "character_book": None,
            }
        }

        # 合并 description：如果 personality 和 summary 不同，将 personality 也加入 description
        cp = persona.get("core_prompts", {})
        personality = cp.get("personality", "")
        summary = persona.get("summary", "")
        if personality and personality != summary:
            v2_data["data"]["description"] = summary + "\n\n" + personality if summary else personality

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