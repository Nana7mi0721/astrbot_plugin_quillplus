# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏痕迹剥离器（v5.3.0 M2.2 自 main.py 原位下沉）。

剥离器是「纯逻辑」：正则文本处理，零 self 依赖（原实现即 QuillPlugin 的
类属性 + classmethod，且方法体只引用类级常量），因此整体转成模块级常量
与纯函数，**正则/文本处理逐字未改**。main.py 的 QuillPlugin 保留同名
类属性/类方法薄转发（M2.0 搬移期约定）——tests/legacy（t7/t8/t15/t26）
与 probe 脚本经 ``QuillPlugin.<name>`` 的旧访问面不变，转发类属性与
本模块常量是**同一对象**（缓存 dict 亦然），任何一侧清缓存都作用于
同一份状态，与搬移前单一代码路径等价。

两档强度（调用方语义，供速查；完整理由见两个函数的 docstring）：

- ``_strip_status_artifacts`` —— **关闭状态栏**用的完整剥离（含渲染产物
  与裸字段行）；H3 历史清洗、H4 dedup、H6 关闭档共用。
- ``_strip_raw_markers`` —— **状态栏开启**时发送前兜底（H6）专用，只擦
  ``[LOVE_DATA]`` / ``[STATUS]`` 两种绝无歧义的原始标记。
"""

from __future__ import annotations

import re

from .parsers import _DEFAULT_LOVE_FIELDS_RAW, _build_raw_status_re
from .tokens import LOVE_DATA_TAG, STATUS_END_TAG, STATUS_TAG

# 聚合所有状态栏变体的剥离正则（disabled 模式 + dedup 清理用）
# 前 4 条与字段名无关（靠标签/标记识别），字段名只出现在 _strip_bare_fields 里，
# 由 _strip_status_artifacts 按 love_fields 动态构建后拼在后面。
_STRIP_PATTERNS: list = [
    (re.compile(r'\*\*状态栏\*\*[\s\S]*?```[\s\S]*?```'), ''),
    (re.compile(re.escape(LOVE_DATA_TAG) + r'\s*.+'), ''),
    (re.compile(re.escape(STATUS_TAG) + r'[\s\S]*?' + re.escape(STATUS_END_TAG)), ''),
    (re.compile(
        r'[>|]{2,}\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*[|<]{2,}\s*.+?\s*[|<]{2,}\s*(?:Select|请选择|选择)\s*[>|]{2,}',
        re.DOTALL | re.IGNORECASE
    ), ''),
    (re.compile(r'\[状态栏\][\s\S]*?\[/状态栏\]'), ''),
    # B1：本条的**唯一**目的是擦掉「裸写的状态栏标题行」，但旧写法
    #   re.compile(r'状态栏[：:][\s\S]*?(?=\n\n|\Z)')
    # 会从 `状态栏：` 一路吃到下一个空行/文末。于是任何正文里出现
    # `状态栏：`/`状态栏:` 的**用户可见文本**都会被从该处截断——`/quill debug`
    # 的字段表（第 4 行恰好是 `  状态栏: 启用|关闭`）在状态栏关闭档整段消失；
    # 更严重的是本剥离器同时是 H3 历史清洗的执行体（history_scrub.py），
    # 被截断的是**回放给模型的历史消息**，等于每轮从上下文里永久删正文。
    # 收紧为「行首、整行」：只删这一行标题，标题之下的裸字段行仍由
    # _strip_bare_fields_re 按字段表处理，擦除能力不下降。
    (re.compile(r'(?m)^[ \t]*状态栏[：:][^\n]*(?:\n|$)'), ''),
]

# 字段名 → 正则的缓存。键是字段元组，值同 _build_raw_status_re。
# 原因：字段名来自面板配置（每次保存都会重建 love_fields 列表），而
# 剥离是每条消息都要跑的热路径，不能每次重新 compile。
_strip_field_re_cache: dict = {}

# 「只擦原始标记」用的两条 —— 供发送前兜底钩子在**状态栏开启**时使用。
# 刻意不放在 _STRIP_PATTERNS 里：那套是「关闭状态栏」用的完整剥离，
# 含匹配 `**状态栏**...``` ``` 的模式，用在开启时会把正常渲染的栏删掉。
# 也刻意**不含**裸字段行模式——理由见 _strip_raw_markers 的说明。
_STRIP_LOVE_DATA_RE = re.compile(re.escape(LOVE_DATA_TAG) + r'\s*.+')
_STRIP_LEGACY_STATUS_RE = re.compile(re.escape(STATUS_TAG) + r'[\s\S]*?' + re.escape(STATUS_END_TAG))


def _strip_bare_fields_re(fields: list) -> re.Pattern:
    """按字段名取（或建）剥离用正则——值不设长度上限，见 _build_raw_status_re。"""
    key = tuple(fields) if fields else ()
    cached = _strip_field_re_cache.get(key)
    if cached is None:
        cached = _build_raw_status_re(list(fields), max_value_len=None)
        # 配置字段数有限，缓存不会无界增长；仍设上限兜底异常调用方
        if len(_strip_field_re_cache) > 32:
            _strip_field_re_cache.clear()
        _strip_field_re_cache[key] = cached
    return cached


def _strip_status_artifacts(text: str, fields: list | None = None) -> str:
    """移除文本中所有状态栏相关痕迹（禁用模式 + dedup 清理）。

    fields 传入当前生效的字段表（调用方传 self.props.love_fields）。此前这里用
    硬编码的 8 个字段名，而解析侧 L4 用动态字段——用户改字段名后（插件自己
    的协议文本就建议改成「催眠度/信赖度」），关闭状态栏时裸字段行擦不掉，
    会原样漏到屏幕上。现改为与解析侧共用同一字段来源。
    fields=None 时退回默认字段表，保证旧调用点仍可用。
    """
    if not text:
        return text
    for pattern, replacement in _STRIP_PATTERNS:
        text = pattern.sub(replacement, text)
    # 字段名相关的裸字段行：与解析侧同源，保证「能解析就必能擦除」
    bare_re = _strip_bare_fields_re(
        fields or _DEFAULT_LOVE_FIELDS_RAW
    )
    text = bare_re.sub('', text)
    return text.strip()


def _strip_raw_markers(text: str, fields: list | None = None) -> str:
    """只擦**原始标记**，保留已渲染的状态栏 —— 发送前兜底专用。

    与 `_strip_status_artifacts` 的区别就是「要不要连渲染产物一起擦」：

    `_strip_status_artifacts` 是给「状态栏已关闭」用的，那时
    `**状态栏**...\\`\\`\\`...\\`\\`\\`` 属于该被清掉的痕迹，所以它第一条模式
    就把它整段匹配掉。而在状态栏**开启**时，同样的文本正是 L1/L2 的
    **正常产出**——拿整套剥离器去擦会把状态栏从回复里删掉。

    **这里只擦两种绝无歧义的原始标记**：`[LOVE_DATA]` 行与
    `[STATUS]...[/STATUS]` 块。它们无论如何都不该出现在最终消息里
    （渲染后的形态是模板产出，不含这两个标记本身）。

    **刻意不擦裸字段行**——因为「裸字段行」与「渲染后的状态栏内容」
    在文本上**完全同形**（渲染出来本来就是 `好感度：88` 这样的行）。
    想区分只能去认模板外壳，而模板是用户可自定义的
    （`format_template` / `format_template_plain` 都能改），
    任何白名单都会在自定义模板下失效并误删正文——
    这个坑实测踩过：用 `[[CUSTOMTPL]]` 这种自定义模板时，
    按「行首裸字段」擦会把栏里内容整段掏空，只剩一个空壳。

    权衡的依据：实测抓到的**全部**泄漏样本都是模型直接输出的
    `[LOVE_DATA]` 行（模型照契约走，会带标记）。裸字段块那种偏离契约的
    输出，常规路径上的 `on_using_llm_tool` 已在处理；为了兜住它而
    引入「可能误删用户自定义模板内容」的风险不划算。
    """
    if not text:
        return text
    text = _STRIP_LOVE_DATA_RE.sub('', text)
    text = _STRIP_LEGACY_STATUS_RE.sub('', text)
    return text.strip()
