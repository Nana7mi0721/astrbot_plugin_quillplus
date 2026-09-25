# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏子系统的魔法字符串常量（M2.1 收敛，PLAN §M2.1 第 3 步）。

``[LOVE_DATA]`` / ``[STATUS]`` 是模型输出与解析器之间的**线上协议标记**：
契约文案（prompt_builder.build_status_contract）、解析正则（parsers.py）、
剥离器（main.py 的 _strip_* 系列）引用的都是同一字符串。此前各处手写
字面量，现收敛到这里作为单一来源——改动标记值只需改这一处
（并同步契约文案与 tests/legacy 的全部回归 fixture）。

正则侧不重复手写转义形态（``\\[LOVE_DATA\\]``），而是用
``re.escape(标记常量)`` 从本模块拼装——``re.escape("[LOVE_DATA]")``
恰好等于 ``\\[LOVE_DATA\\]``，编译出的 pattern 与原字面量逐字节一致
（fixture 全量回归守卫）。

注意：``**状态栏**``（Markdown 标题外壳）与 ``[状态栏]``（中文标签变体）
只出现在剥离正则与模板文案里，不属于模型↔解析器的协议标记，暂不收敛。
"""

LOVE_DATA_TAG = "[LOVE_DATA]"
STATUS_TAG = "[STATUS]"
STATUS_END_TAG = "[/STATUS]"
