# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏子系统（v5.3.0 M2.1 自 main.py 拆出）。

公共入口 re-export：
- ``StatusbarParsersMixin`` / ``StatusbarRenderMixin``：QuillPlugin 的两个
  Mixin 基类（解析侧六级降级链 / 渲染侧模板与兜底栏）；
- ``LOVE_DATA_TAG`` / ``STATUS_TAG`` / ``STATUS_END_TAG``：协议标记常量；
- 解析侧模块级正则与纯函数（带下划线前缀，供 main.py 保持旧 import 面与
  tests/legacy、probe 脚本兼容——M2.0 约定：搬移期旧位置保留 re-export）。
"""

from .tokens import LOVE_DATA_TAG, STATUS_END_TAG, STATUS_TAG
from .parsers import (
    _DEFAULT_LOVE_FIELDS_RAW,
    _DELTA_MARK_RE,
    _LOVE_DATA_RE,
    _PLOT_PATH_RE,
    _STATUS_BLOCK_RE,
    _STATUS_RE,
    _StatusLevelContext,
    _StatusLevelResult,
    _annotate_changes,
    _build_raw_status_re,
    _extract_numeric,
    _format_delta,
    _normalize_status_value,
    StatusbarParsersMixin,
)
from .render import StatusbarRenderMixin
from .strip import (
    _STRIP_LEGACY_STATUS_RE,
    _STRIP_LOVE_DATA_RE,
    _STRIP_PATTERNS,
    _strip_bare_fields_re,
    _strip_field_re_cache,
    _strip_raw_markers,
    _strip_status_artifacts,
)

# 注意：_SB_LEVELS 是 StatusbarParsersMixin 的类属性（不再是模块级名字），
# 经 `StatusbarParsersMixin._SB_LEVELS` 或 QuillPlugin 实例访问。

__all__ = [
    "LOVE_DATA_TAG",
    "STATUS_END_TAG",
    "STATUS_TAG",
    "StatusbarParsersMixin",
    "StatusbarRenderMixin",
    "_DEFAULT_LOVE_FIELDS_RAW",
    "_DELTA_MARK_RE",
    "_LOVE_DATA_RE",
    "_PLOT_PATH_RE",
    "_STATUS_BLOCK_RE",
    "_STATUS_RE",
    "_STRIP_LEGACY_STATUS_RE",
    "_STRIP_LOVE_DATA_RE",
    "_STRIP_PATTERNS",
    "_StatusLevelContext",
    "_StatusLevelResult",
    "_annotate_changes",
    "_build_raw_status_re",
    "_extract_numeric",
    "_format_delta",
    "_normalize_status_value",
    "_strip_bare_fields_re",
    "_strip_field_re_cache",
    "_strip_raw_markers",
    "_strip_status_artifacts",
]
