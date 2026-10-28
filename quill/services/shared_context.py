# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M7 有界、纯文本近期视图；不把工具半对或控制消息复制到其他入口。"""
import json
import re


def history_view(rows, origins, budget=16000):
    selected=[]
    used=0
    for row in reversed(rows):
        text=row["content"]
        if row["role"]=="user":
            source=origins.get(row.get("origin_key"),{})
            header=json.dumps({"入口":source.get("label") or source.get("umo") or row.get("source_ref") or "历史来源",
                               "参与者":row.get("actor_id") or "未关联"},ensure_ascii=False)
            text=header+"\n"+text
        if used+len(text)>budget:
            if not selected:
                text=text[-budget:]
            else:
                break
        selected.append({"role":row["role"],"content":text,"_no_save":True})
        used+=len(text)
    return list(reversed(selected))


def last_options(rows):
    last=next((row['content'] for row in reversed(rows) if row['role']=='assistant'),'')
    block=re.search(r'(?:【|[>＞]{2,})\s*(?:剧情走向|剧情选项|Plot\s*Paths).*?(?:】|[<＜]{2,})(.*?)(?:【\s*(?:请选择|选择)|[>＞]{2,}\s*(?:请选择|选择|Select)|[<＜]{2,}\s*Select)',last,re.S|re.I)
    if not block:
        return []
    options=[]
    for line in block[1].splitlines():
        match=re.match(r'\s*(?:[-*]\s*)?(?:\[(\d+)\]|(\d+)[.、)）:：])\s*(.+)',line)
        bullet=re.match(r'\s*[-*•]\s+(.+)',line)
        if match:
            if int(match[1] or match[2]) != len(options)+1:
                return []  # 跳号不能重编号，否则用户的「3」会指向错误剧情。
            options.append(match[3][:500])
        elif bullet:
            options.append(bullet[1][:500])
    return options if 2<=len(options)<=9 else []
