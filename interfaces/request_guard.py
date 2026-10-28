# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""请求装配幂等：记录属于事件，不把已装配标记放进会被宿主浅拷贝的请求。"""

import hashlib


def _signature(req):
    # 摘要只在事件生命周期内比较，不入日志或存储；上下文重置也应触发重装配。
    return hashlib.sha256(repr((req.prompt, req.system_prompt, req.contexts,
                               req.extra_user_content_parts)).encode()).digest()


def begin_request(event, req):
    records = event.get_extra('_quill_prepared_requests') or []
    signature = _signature(req)
    for record in records:
        if record['request'] is req and record['signature'] == signature:
            return None
    # 宿主 copy.copy 会保留部分已修改字段、重建其余字段；只撤回我们确实认识的改动。
    for record in reversed(records):
        if req.prompt == record['after_prompt']:
            req.prompt = record['before_prompt']
            break
    for record in reversed(records):
        if req.system_prompt == record['after_system']:
            req.system_prompt = record['before_system']
            break
        before, after = record['before_system'] or '', record['after_system'] or ''
        incoming = req.system_prompt or ''
        if before and after.count(before) == 1:
            prefix, suffix = after.split(before, 1)
            # 宿主可能在 hook 之后继续追加工具/运行器协议；仅撤回已知 Quill 增量。
            changed = False
            if prefix and prefix in incoming:
                incoming = incoming.replace(prefix, '', 1)
                changed = True
            if suffix and suffix in incoming:
                incoming = incoming.replace(suffix, '', 1)
                changed = True
            if changed:
                req.system_prompt = incoming
                break
        elif after and after in incoming:
            req.system_prompt = incoming.replace(after, before, 1)
            break
    added = {id(part) for record in records for part in record['added_parts']}
    req.extra_user_content_parts = [part for part in (req.extra_user_content_parts or []) if id(part) not in added]
    return {'request': req, 'before_prompt': req.prompt, 'before_system': req.system_prompt,
            'before_parts': {id(part) for part in req.extra_user_content_parts}}


def finish_request(event, req, record):
    before_parts = record.pop('before_parts')
    record.update(signature=_signature(req), after_prompt=req.prompt, after_system=req.system_prompt,
                  added_parts=[part for part in (req.extra_user_content_parts or []) if id(part) not in before_parts])
    records = event.get_extra('_quill_prepared_requests') or []
    records = [old for old in records if old['request'] is not req]
    records.append(record)
    event.set_extra('_quill_prepared_requests', records[-32:])
