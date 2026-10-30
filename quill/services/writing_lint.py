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


CONTROL_ENTRY_IDS={'summary_mode','progress_continue','progress_speedup','progress_detail','progress_next'}


def repair_candidates(entries):
    """R2：只生成待确认候选，不把同ID或文字命中当成自动覆写授权。"""
    import hashlib
    import json
    result=[]
    for row in entries:
        if not (row.get('enabled') or row.get('is_constant')):
            continue
        reasons=[]
        if row.get('entry_id') in CONTROL_ENTRY_IDS:
            reasons.append('历史流程控制条目标识；可能已经过用户改写，需核对正文')
        if any(k in str(row.get('content','')) for k in ('开启助手模式','停止生成剧情','【总结完毕】')):
            reasons.append('正文包含切换助手/停止剧情的控制语句')
        if not reasons:
            continue
        # 命中计数不参与版本指纹；检索本身不应令确认失效。正文/关键词/开关必须相同。
        fields={k:row.get(k) for k in ('entry_id','name','category','content','keywords','aliases','enabled','is_constant')}
        fingerprint=hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        result.append({'entry_id':row['entry_id'],'name':row.get('name') or row['entry_id'],
                       'enabled':bool(row.get('enabled')),'is_constant':bool(row.get('is_constant')),
                       'reasons':reasons,'preview':str(row.get('content',''))[:1200],
                       'content_chars':len(str(row.get('content',''))),'fingerprint':fingerprint})
    return sorted(result,key=lambda r:r['entry_id'])
