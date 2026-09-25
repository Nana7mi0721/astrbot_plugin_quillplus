# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""v5.3.0 M1 pytest 基建：环境引导 + 收集控制。

优先级（与 tests/legacy/ 脚本自引导一致）：
  1. 环境里已能 import astrbot（真机或 ASTRBOT_APP）→ 用真实框架；
  2. 否则安装 tests/infra/framework_stubs.py 的假件（CI 环境）。

ASTRBOT_TEST_STRICT=1 时要求必须是 stub 环境（CI 断言没有真机依赖）。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)

# 插件以包形式导入（astrbot_plugin_quillplus），父目录必须在 path 上；
# ASTRBOT_APP 指向真机 app 目录时优先使用真实框架（本地开发）。
for _p in (os.environ.get("ASTRBOT_APP") or "", _PLUGIN_PARENT):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from tests.infra import framework_stubs  # noqa: E402


def _can_import_astrbot() -> bool:
    try:
        import astrbot  # noqa: F401

        return True
    except Exception:
        return False


_HAS_REAL_ASTRBOT = _can_import_astrbot()

if not _HAS_REAL_ASTRBOT:
    framework_stubs.install()
if os.environ.get("ASTRBOT_TEST_STRICT") == "1":
    assert not _HAS_REAL_ASTRBOT, (
        "ASTRBOT_TEST_STRICT=1 要求纯 stub 环境，但检测到真实 astrbot 可导入"
    )

# legacy/ 是手写 self-test 脚本（函数名 t1_/t2_… 不符合 pytest 约定，
# 且模块级 main() 有副作用）；由 tests/test_legacy_*.py 包装执行。
collect_ignore = [os.path.join(_HERE, "legacy")]
