# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""quill.core/ — 最底层设施（错误体系、路径、原子写、锁、日志桥）。

不得 import quill.services/ 与 quill/config（架构守卫 tests/arch/test_layering.py）。
"""
