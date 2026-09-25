# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""interfaces 层：与 AstrBot 框架握手的薄适配（v5.3.0 M2.2 起）。

本层允许 import astrbot。注意 BASELINE §1.2 的注册契约：钩子/指令函数的
``__module__`` 必须与插件注册路径精确相等——因此**注册桩留在 main.py 类体**，
本层只承载被桩委托的实现函数（取参 → 调服务/剥离器 → 还参），不定义
任何带框架装饰器的函数。
"""
