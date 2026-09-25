# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quill/ — 羽笔与 AstrBot 解耦的核心包（v5.3.0 M2 渐进拆分目标位）。

依赖规则（docs/v5.3/PLAN.md §2.2，tests/arch/test_layering.py 机械校验）：

    interfaces/ ──import──> quill/          （单向）
    quill/core/ ←──import── quill/services/ （core 不依赖 services）
    quill/ 内部禁止 import astrbot.*        （含函数内延迟 import）

日志：本包不 import astrbot，宿主 logger 经 quill/core/logbridge.py 注入
（main.py 在 QuillPlugin.__init__ 最早处 set_logger）。

版本号 __version__ 待 M4.1 版本单源化时落位，此处暂不放。
"""
