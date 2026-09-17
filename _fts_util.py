# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FTS5 查询串构造 —— 中文子串检索的公共实现。

**为什么需要这个模块**
    SQLite 内置分词器都不按中文分词：
    - `unicode61` 把一整句连续中文当成**一个** token，于是「生日」这类子串
      查询永远命中不了（实测：整句插入后 MATCH '生日' 得 0 行）；
    - `trigram` 按 3 字符 n-gram 切分，≥3 字的子串查询可命中，
      但 **1-2 字的查询（「猫」「生日」）结构上无法命中** —— 而短词恰恰是
      中文记忆检索里最高频的查询形态。

    因此正确做法是「trigram 快路径 + 短词 LIKE 兜底」两条腿：本模块提供
    这两条路径共用的查询串构造与短词提取，`kb.py`（写作素材库）与
    `quill_rag/memory_store.py`（动态记忆）都复用它，避免两处各写一份
    各自漂移（此前 kb 用 trigram + 自写转义、memory 用 unicode61 且几乎
    不转义，就是分头演化的结果）。

**实测约束（编写本模块时的 sqlite 3.50.4 验证结果）**
    - `MATCH` 里的裸词若含 `-` / `:` 会被当成列名或语法而报
      `no such column`，必须整体加双引号变成字符串字面量；
    - trigram 下 `"生日"` 这种 <3 字的短语不报错但恒不命中，
      所以短词不能靠 FTS 解决；
    - `bm25(别名)` 非法，`别名.rank` 合法（见调用方注释）。
"""

from __future__ import annotations

import re

# 标点 + 空白 → 全部视作分隔符。
#
# 这个集合**必须与 kb.py 原先的转义集合逐字一致**：kb 的写作素材库检索是
# 线上验证过的工作路径，本次只是把它的实现搬进共享模块，不是为了改善它。
# 曾考虑顺带加入 《》「」【】 以覆盖更多书写习惯，但那会造成真实回退 ——
# 查询 `《魔法》`（4 字）原本能作为一个 token 命中字面量，若把书名号当分隔符
# 切掉就只剩 `魔法`（2 字），而 <3 字的片段在 trigram 下无法命中、会被丢弃，
# 该查询反而从「能命中」变成「必然 miss」。要改这个得单独论证，不夹带。
_SPLIT_RE = re.compile(r'[\"\'()*^~{}，。！？；：、—…·\s]+')

# trigram 分词器的最小可命中长度：n-gram 需要 3 个字符才能组成一个 token
TRIGRAM_MIN_LEN = 3


def split_tokens(text: str) -> list[str]:
    """按标点/空白切分，返回非空片段（保留原始长度信息）。"""
    if not text:
        return []
    return [t for t in _SPLIT_RE.split(text) if t]


def escape_trigram(text: str, *, join: str = " OR ") -> str:
    """构造 trigram 分词器可用的 MATCH 查询串。

    只保留 ≥3 字符的片段（短于此的 trigram 无法命中，留在查询里纯属噪声，
    还会让「全短词」的输入退化成必然 miss 的查询）。片段整体加双引号成
    字符串字面量，避免 `-`/`:` 等字符被 FTS5 当语法解析。

    Returns:
        可用的查询串；没有 ≥3 字符片段时返回空串（调用方应改走 LIKE 兜底）。
    """
    tokens = [t for t in split_tokens(text) if len(t) >= TRIGRAM_MIN_LEN]
    if not tokens:
        return ""
    return join.join(f'"{t}"' for t in tokens)


def short_tokens(text: str, *, min_len: int = 1, max_len: int = TRIGRAM_MIN_LEN - 1) -> list[str]:
    """取出无法进 FTS 快路径的短片段（默认 1-2 字符），供 LIKE 兜底使用。

    去重并保持出现顺序；过滤纯空白。
    """
    out: list[str] = []
    for t in split_tokens(text):
        if min_len <= len(t) <= max_len and t not in out:
            out.append(t)
    return out
