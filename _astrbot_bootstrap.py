# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""独立运行时的 AstrBot 路径引导（不引入内置 logging）。

为什么需要它
------------
插件所有模块的 logger **必须**来自 `astrbot.api`（上架规范：严禁使用 Python
内置 logging 模块）。插件由 AstrBot 加载时 `astrbot` 必然可导入，没有问题；
但 `python kb.py`、`python state.py` 这类**直接跑自测**的场合，解释器不知道
AstrBot 在哪，`from astrbot.api import logger` 会以 ModuleNotFoundError 失败。

本模块用最小代价解决：只有当 `astrbot` 真的导入不到时，才把 AstrBot 的
`backend/app` 追加到 sys.path 再试一次。它是有意的窄承诺——
**不做** mock、**不做** 日志降级、不吞任何其它异常；带不到 AstrBot 就让
错误照原样抛出，避免掩盖真实的配置问题。

用法（在自测入口或模块顶部调用一次）::

    try:
        from astrbot.api import logger
    except ModuleNotFoundError:
        from ._astrbot_bootstrap import ensure_astrbot_importable
        ensure_astrbot_importable()
        from astrbot.api import logger
"""

from __future__ import annotations

import os
import sys

# 常见的 AstrBot 安装位置，按优先级排列
_CANDIDATES = (
    os.environ.get("ASTRBOT_APP", ""),
    r"D:\Program\AstrBot\backend\app",
    "/opt/AstrBot/backend/app",
)


def ensure_astrbot_importable() -> str:
    """确保 `astrbot` 可导入，返回实际被追加的路径（已可导入时返回空串）。"""
    try:
        import astrbot  # noqa: F401

        return ""
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    # 插件目录通常位于 <AstrBot data>/data/plugins/<plugin>，
    # 往上三级常见地能到达 AstrBot 根目录。
    guess: list[str] = []
    cur = here
    for _ in range(4):
        cur = os.path.dirname(cur)
        guess.append(os.path.join(cur, "backend", "app"))

    for path in (*_CANDIDATES, *guess):
        if not path or not os.path.isdir(path):
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import astrbot  # noqa: F401

            return path
        except ImportError:
            continue
    return ""
