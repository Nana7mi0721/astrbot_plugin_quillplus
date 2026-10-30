# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""宿主未公开绝对相关度时的保守文字依据；不是语义相似度或分数校准。"""
import re


def has_text_support(query,content):
    query=str(query).lower().strip();text=str(content).lower()
    if query and query in text:
        return True
    keys=set(re.findall(r'[a-z0-9_]{2,}',query))
    for run in re.findall(r'[\u3400-\u9fff]+',query):
        keys.update(run[i:i+2] for i in range(min(len(run)-1,128)))
    return bool(keys) and sum(k in text for k in keys)>=min(3,max(1,len(keys)//3))
