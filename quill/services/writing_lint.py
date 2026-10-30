# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WR 自动匹配护栏与只读体检；不在素材中执行控制对话流程的命令。"""
import re

BROAD_KEYS={'状态','看看','觉得','感觉','身体','心理','内心','不要'}


def usable_keyword(value):
    return isinstance(value,str) and len(value.strip())>=2 and value.strip() not in BROAD_KEYS


def keyword_matches(keyword,text):
    if not usable_keyword(keyword):
        return False
    keyword=keyword.strip().lower()
    if keyword.isascii() and keyword.replace(' ','').isalnum():
        return re.search(r'(?<![a-z0-9_])'+re.escape(keyword)+r'(?![a-z0-9_])',text) is not None
    return keyword in text


def lint_entries(entries):
    issues=[]
    for row in entries:
        seen=set()
        for key in [*row.get('keywords',[]),*row.get('aliases',[])]:
            if not usable_keyword(key):
                issues.append({'entry_id':row['entry_id'],'code':'broad_keyword','keyword':str(key),'enabled':row.get('enabled',False)})
            elif key.lower() in seen:
                issues.append({'entry_id':row['entry_id'],'code':'duplicate_keyword','keyword':key,'enabled':row.get('enabled',False)})
            seen.add(str(key).lower())
        if not any(usable_keyword(k) for k in row.get('keywords',[])):
            issues.append({'entry_id':row['entry_id'],'code':'empty_effective_keys','enabled':row.get('enabled',False)})
        if str(row.get('content','')).lstrip().startswith(('指令：','指令:','开启助手模式停止生成剧情')):
            issues.append({'entry_id':row['entry_id'],'code':'control_instruction','enabled':row.get('enabled',False)})
    return issues
