# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""路径安全设施（v5.3.0 M3.1，自 v6 重构版 ``core/paths.py`` 摘取移植）。

为什么收敛到这里
----------------
用户提供的文件名/标识符此前散落多处手工检查（``web_routes.py`` 的
``'..' in filename`` 三连、``persona_manager._sanitize_id``、
``worldbook._validate_name``），各处强度不一且互相抄写。本模块提供两个
纯函数作为标准通道：

* :func:`sanitize_name` —— 任意字符串 → 单个安全路径段；
* :func:`resolve_safe`  —— 用户名 → ``base`` 目录下的安全落点。

依赖规则：本文件属 ``quill/core``（最底层），**禁止 import astrbot 与
quill.services**（tests/arch/test_layering.py 机械校验），只依赖标准库。
M3.2 落地 ``QuillError`` 体系后，:func:`resolve_safe` 的异常可换族；
当前用 ``ValueError``（调用方按"参数不合法"处理，正对应 400 语义）。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 允许出现在文件名里的字符之外一律替换成 ``_``（含路径分隔符与 Windows
#: 文件名非法字符、全部 C0 控制字符）。保留中日韩字符、连字符与常见符号。
#: 注意：参考实现此处是普通字符串逐字符 replace，``\x00-\x1f`` 写法在普通
#: 字符串里只是「NUL、减号、单元分隔符」三个字符——既漏掉其余控制字符、
#: 又误伤连字符；移植时改为正则字符类，让区间语义成立（对齐 docstring，
#: 连字符是合法文件名字符，persona 头像文件名里真实存在）。
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 保留设备名（不分大小写、带不带扩展名都保留）。
#: 这类名字做文件名会让 create/open 行为异常（打开的是设备而非文件）。
_WINDOWS_RESERVED = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def sanitize_name(name: str, *, fallback: str = "unnamed", max_length: int = 120) -> str:
    """把任意字符串规整成安全的单段文件名。

    保留中日韩字符与常见符号，替换掉路径分隔符、控制字符与 Windows 保留名。
    结果**一定**是单个路径段（不含 ``/`` 或 ``\\``），因此天然免疫目录穿越。

    Args:
        name: 用户输入。``None``/空白/纯 ``.`` 段一律落到 *fallback*。
        fallback: 输入为空时的兜底名。
        max_length: 结果最大长度（超长截断，截断后剥离尾部悬挂的 ``.`` 与空格）。

    Returns:
        规整后的单段文件名（可能 ≠ 输入：危险字符被替换为 ``_``）。
    """
    text = str(name or "").strip()
    text = _UNSAFE_CHARS_RE.sub("_", text)
    # 目录穿越的两个核心字符：斜杠与 ..（单独出现时也已无意义，但显式清掉更清晰）
    text = text.replace("/", "_").replace("\\", "_")
    text = text.strip(" .")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    if not text:
        return fallback
    # Windows 保留设备名会让 create/open 行为异常。
    stem = text.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text


def resolve_safe(base: Path, name: str, *, suffix: str = "") -> Path:
    """把用户提供的 *name* 安全地解析到 *base* 目录下。

    新代码里"用户输入 → 磁盘路径"必须走这里，不得再手写
    ``os.path.join(base, name)`` + 逐字符检查：

    * 目录分隔符与 ``..`` 一律被 :func:`sanitize_name` 清掉；
    * *suffix* 不匹配时自动补齐（大小写不敏感判断）；
    * 解析后再用 ``relative_to`` 断言最终路径确实落在 *base* 内
      （纵深防御：防符号链接/大小写不敏感文件系统上的逃逸——即便
      :func:`sanitize_name` 被改坏，这里仍会拦截）。

    Raises:
        ValueError: 最终路径逃逸出 *base*（name 不合法）。M3.2 收编
            ``QuillError`` 体系后换族；调用方按 400 语义处理。
    """
    safe = sanitize_name(name)
    if suffix and not safe.lower().endswith(suffix.lower()):
        safe += suffix
    candidate = (base / safe).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"文件名不合法: {name!r}") from exc
    return candidate


__all__ = [
    "_WINDOWS_RESERVED",
    "resolve_safe",
    "sanitize_name",
]
