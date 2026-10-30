# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""供管理诊断与台账使用的有界错误说明，不保存请求正文或认证头。"""
import re


def safe_error_detail(error):
    text=str(getattr(error,'quill_error_detail','') or getattr(error,'reason','') or type(error).__name__)
    text=re.sub(r'(?i)(?:bearer\s+|sk-)[a-z0-9_.-]+','[已隐藏凭据]',text)
    text=re.sub(r'(?i)(api[_-]?key|token|authorization)(["\s:=]+)[^\s,"}]+',r'\1\2[已隐藏]',text)
    text=re.sub(r'https?://\S+','[端点]',text)
    return text.replace('\n',' ')[:500]
