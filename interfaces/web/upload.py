# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""统一上传通道（v5.3.0 M3.1，PLAN D3/F2）。

为什么需要它
------------
v5.2.5 的每个上传端点都有两个几乎相同的 handler（multipart 正身 +
``*_base64`` 变体），六组共 12 个函数里散落着同一批逻辑的抄写：文件读取、
base64 解码、大小上限、扩展名黑名单。只要一处改了另一处忘改，两条通道
行为就开始漂移（persona_import 的扩展名白名单已实际漂移——BASELINE
§6.2 组2、§8.1）。本模块把"从请求里拿一个上传文件"收敛为唯一入口
:func:`read_upload`。

三通道消歧（按序探测，先命中先生效）
------------------------------------
1. **multipart 文件通道**：``await request.files()`` 按 *keys* 找文件，
   ``await file.read()`` 异步读——真机 ``PluginUploadFile.read`` 是
   async（BASELINE §1.1）；
2. **表单 base64 通道**：``await request.form()`` 文本字段携带
   ``b64_data``——部分沙箱环境的 FormData workaround；
3. **JSON base64 通道**：``await request.json()`` 携带 ``b64_data``——
   面板 bridge 通道拒绝 FormData/Blob，**实际主力路径**（BASELINE §6.2：
   面板只调 base64/JSON 变体）。

此即原各 ``*_base64`` handler 的消歧逻辑（FormData 沙箱限制 workaround）
的唯一实现；M3.1 后四组双份 handler 正身与 base64 路由共用同一函数。

错误语义（与原 handler 逐点对齐）
----------------------------------
* base64 解码失败 → :class:`UploadError`（ValueError 子类）——原为
  ``error_response(error_text("Base64 解码失败", e), 400)``，调用方照旧；
* 超过 *limit* → :class:`UploadTooLarge` ——原为 413 + 各端点文案，
  文案由调用方保留；
* 无任何上传 → 返回 ``None`` ——原为 400「未收到文件」，调用方照旧。

本模块不 import astrbot：``request`` 经参数传入（真机代理或测试假件
皆可），json/form/files/body 一律 ``await``（M1 stub 形状契约）。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

#: multipart 文件通道的候选字段名（按序探测）。
DEFAULT_FILE_KEYS = ("file", "upload", "card", "avatar")

#: base64 数据字段名（各 ``*_base64`` handler 的历史契约字段）。
B64_FIELD = "b64_data"

#: 默认大小上限（50MB，原 RAG 文档通道抄写的值）。
DEFAULT_LIMIT = 50 * 1024 * 1024

#: 头像 / V2 角色卡通道的大小上限（原各 handler 抄写的 5MB）。
CARD_LIMIT = 5 * 1024 * 1024

#: 二进制扩展名黑名单——仅此一处定义（原先 rag_upload / rag_upload_base64
#: 两处抄写）。命中即拒绝：RAG 通道只收纯文本。
BINARY_EXTS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".zip", ".rar", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    }
)

#: V2 角色卡导入接受的扩展名 = 原 multipart 侧 {png,jpg,jpeg,json} ∪
#: base64 侧 {png,jpg,jpeg,webp,json}（BASELINE §8.1 决策：统一取并集，
#: 两通道接受同一集合，兼容面最宽；M3.1 起两通道本就是同一 handler）。
CARD_IMPORT_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".json"})

#: :data:`CARD_IMPORT_EXTS` 中按图片处理的子集（parse_v2_card 图像分支 +
#: 存头像）。
CARD_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class UploadError(ValueError):
    """上传通道错误（base64 解码失败等）。

    继承 ``ValueError``：调用方的 ``except ValueError`` 兜底（如
    persona_import 的 400 分支）天然覆盖；M3.2 收编 QuillError 体系。
    """


class UploadTooLarge(UploadError):
    """上传内容超过大小上限（原 413 语义）。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"文件超过 {max(1, limit // (1024 * 1024))}MB 上限")


@dataclass
class UploadPayload:
    """一次成功读取的上传内容。

    Attributes:
        filename: 文件名。multipart 通道取自文件对象；base64 通道取自
            *filename_field* 指定的文本字段；都缺失时落到调用方给的
            default。
        data: 文件字节。
        content_type: MIME 类型（base64 通道为空串）。
        fields: 随文件一起提交的文本字段（form/JSON 里的非 b64 标量），
            供调用方取 ``source`` 之类附加参数，避免再次手工消歧通道。
    """

    filename: str
    data: bytes
    content_type: str = ""
    fields: dict[str, str] = field(default_factory=dict)


def decode_base64(raw: object) -> bytes:
    """base64 → bytes（原各 ``*_base64`` handler 抄写的解码逻辑）。

    Raises:
        UploadError: 解码失败（原语义：调用方报 400「Base64 解码失败」）。
    """
    text = str(raw or "").strip()
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError) as exc:
        raise UploadError(str(exc) or type(exc).__name__) from exc


def _scalar_fields(source: object) -> dict[str, str]:
    """提取 form/JSON dict 里的字符串字段（剔除 b64 数据字段本体）。"""
    if not isinstance(source, dict):
        return {}
    return {
        str(key): value
        for key, value in source.items()
        if key != B64_FIELD and isinstance(value, str)
    }


def _upload_filename(file: object) -> str:
    """读上传文件对象的文件名。

    真机 ``PluginUploadFile`` 暴露 ``.filename``（``.name`` 不存在，经
    ``__getattr__`` 委托给 starlette UploadFile 也不一定有）；测试假件
    两者都有。两个字段都试，取第一个非空值。
    """
    for attr in ("filename", "name"):
        value = str(getattr(file, attr, "") or "").strip()
        if value:
            return value
    return ""


async def read_upload(
    request,
    *,
    keys: tuple[str, ...] = DEFAULT_FILE_KEYS,
    limit: int | None = DEFAULT_LIMIT,
    filename_field: str = "filename",
    default_filename: str = "unnamed",
) -> UploadPayload | None:
    """从请求读取一个上传文件（multipart / base64 三通道统一入口）。

    Args:
        request: astrbot.api.web 的请求代理（真机 ``request`` 单例或测试
            假件）。``json/form/files`` 按 async 契约调用。
        keys: multipart 文件通道的候选字段名，按序探测。
        limit: 字节上限；``None`` 表示不限（原 wb_import_st 无上限语义）。
            超限抛 :class:`UploadTooLarge`（调用方映射为各自的 413 文案）。
        filename_field: base64 通道里携带文件名的文本字段
            （rag 通道历史字段是 ``source``）。
        default_filename: 文件名完全缺失时的兜底。

    Returns:
        :class:`UploadPayload`；请求里没有任何上传数据时 ``None``
        （调用方报 400「未收到文件」）。

    Raises:
        UploadTooLarge: 内容超过 *limit*。
        UploadError: base64 解码失败。
    """
    # ── ① multipart 文件通道 ──
    files = await request.files()
    for key in keys:
        file = files.get(key) if files else None
        if file is None:
            continue
        data = await file.read()
        if limit is not None and len(data) > limit:
            raise UploadTooLarge(limit)
        form = await request.form()
        return UploadPayload(
            filename=_upload_filename(file) or default_filename,
            data=bytes(data),
            content_type=str(getattr(file, "content_type", "") or ""),
            fields=_scalar_fields(form),
        )

    # ── ② 表单 base64 通道 / ③ JSON base64 通道（按序探测）──
    candidates: list[dict] = []
    form = await request.form()
    if form:
        candidates.append(form)
    body = await request.json(default={})
    if isinstance(body, dict) and body:
        candidates.append(body)
    for source in candidates:
        b64_raw = source.get(B64_FIELD)
        if not (isinstance(b64_raw, (str, bytes)) and str(b64_raw).strip()):
            continue
        data = decode_base64(b64_raw)
        if limit is not None and len(data) > limit:
            raise UploadTooLarge(limit)
        fields = _scalar_fields(source)
        filename = str(
            fields.get(filename_field) or fields.get("filename") or ""
        ).strip()
        return UploadPayload(
            filename=filename or default_filename,
            data=data,
            content_type="",
            fields=fields,
        )
    return None


__all__ = [
    "B64_FIELD",
    "BINARY_EXTS",
    "CARD_IMAGE_EXTS",
    "CARD_IMPORT_EXTS",
    "CARD_LIMIT",
    "DEFAULT_FILE_KEYS",
    "DEFAULT_LIMIT",
    "UploadError",
    "UploadPayload",
    "UploadTooLarge",
    "decode_base64",
    "read_upload",
]
