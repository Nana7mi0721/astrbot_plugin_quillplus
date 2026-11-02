# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M7 有界、纯文本近期视图；不把工具半对或控制消息复制到其他入口。

串台修复（M7/来源可见）：同一组内的私聊与群聊原先只在「用户行」前贴一段 JSON，
助手自己的回复不带任何来源，入口又回落到裸 UMO、参与者是哈希，模型读不出这是两个地方，
于是在私聊里按群聊说话、到群里接着私聊的话题。现在每条消息都带可读的来源标记
（用户侧「来自 X｜说话人 Y」，助手侧「发往 X」），入口与说话人都取人类可读的名字。
视图仍只走临时通道（`_no_save`），不写回宿主历史。
"""
import re

_KIND={"FriendMessage":"私聊","GroupMessage":"群聊","TempMessage":"临时会话",
       "GuildMessage":"频道","GroupAtMessage":"群聊","C2CMessage":"私聊"}
_MAX_LABEL=60


def _short_session(platform, session):
    """WebChat 的会话键是 `webchat!<用户名>!<会话 id>`：同名用户可能同时开多个会话，
    只取用户名会让两个入口显示成同一个名字，所以补一段会话尾缀区分。
    尾缀只对 uuid 形态截断——测试/自建会话常是 `m7fix-a` 这种可读短名，
    一律取前 6 位会把 `m7fix-a`/`m7fix-b` 截成同一个 `m7fix-`，入口名反而撞车。"""
    if session.startswith("webchat!"):
        parts=session.split("!")
        if len(parts)>2 and parts[1] and parts[2]:
            tail=parts[2]
            if len(tail)>12 and "-" in tail[8:]:
                tail=tail[:6]
            return "%s#%s"%(parts[1],tail[:12])
        if len(parts)>1 and parts[1]:
            return parts[1]
    return session


def entry_label(source, umo=""):
    """入口的可读称呼；优先库里写入的 label，否则由 UMO 拼出来，永不返回空串。"""
    source=source or {}
    label=str(source.get("label") or "").strip()
    if label:
        return label[:_MAX_LABEL]
    text=str(source.get("umo") or umo or "").strip()
    if not text:
        return "历史来源"
    platform,_,rest=text.partition(":")
    kind,_,session=rest.partition(":")
    parts=[platform,_KIND.get(kind,kind),_short_session(platform,session)]
    return (" ".join(p for p in parts if p) or text)[:_MAX_LABEL]


def actor_names(actors):
    """actor_id / canonical_id → 可读称呼；轮次行里存的是关联后的 canonical id。"""
    names={}
    for actor in actors or ():
        if not isinstance(actor,dict):
            continue
        name=str(actor.get("label") or "").strip() or str(actor.get("sender_id") or "").strip()
        if not name:
            continue
        for key in (actor.get("actor_id"),actor.get("canonical_id")):
            if key and not names.get(str(key)):
                names[str(key)]=name
    return names


def row_prefix(row, origins, names, current=None):
    """每条消息前的来源标记：用户侧「来自 X｜说话人 Y」，助手侧「发往 X」。

    `current` 是本轮入口的 origin_key：给了就再标一层「本入口 / 别处」。
    实测（隔离实例 6186，两组 webchat 入口）只标入口名**不足以**阻止串台——
    模型在 B 入口问「我现在在哪」时，把 A 入口里用户说的「我在便利店值夜班、
    柜台上有把红伞」认领成自己的现场并报出扫描枪。原因是两组前缀读起来都在
    「讲同一场戏」，模型无从判断哪条属于此刻。用「别处」两个字把归属写死。
    """
    key=str(row.get("origin_key") or "")
    label=entry_label(origins.get(key))
    where=""
    if current:
        where="（本入口）" if key==str(current) else "（别处）"
    if row.get("role")=="user":
        who=names.get(str(row.get("actor_id") or "")) or "未关联"
        return "〔来自：%s｜说话人：%s%s〕\n"%(label,who,where)
    return "〔发往：%s%s〕\n"%(label,where)


# 模型会把行前缀当成自己的输出格式模仿（实测入口 B 的回复正文第一行原样打出了
# `〔发往：webchat 私聊 Reisena#m7fix-b〕`）。提示词里已明令禁止，这里再兜一层：
# 发送前擦掉**行首、整行、形如〔来自/发往：…〕** 的标记行。取法与状态栏剥离器
# 一致——只删整行标记，绝不做「从标记一路吃到某处」的跨度匹配，避免吞正文。
_MARKER_LEAK = re.compile(r'(?m)^[ \t]*〔(?:来自|发往)：[^〕\n]*〕[ \t]*(?:\r?\n|$)')


def strip_view_markers(text):
    """擦掉回复里漏出的〔来自/发往：…〕来源标记行。"""
    return _MARKER_LEAK.sub('', text or '')


def history_view(rows, origins, budget=16000, actors=None, decorate=True, current=None):
    """`decorate=False` 给只做文本扫描的调用方（激活词判定）：入口标签里带「群聊/私聊」
    这类词，混进关键词匹配会造成误激活。

    `current` 是本轮入口的 origin_key，用于给每条前缀补「本入口 / 别处」（见 row_prefix）。
    """
    names=actor_names(actors)
    selected=[]
    used=0
    for row in reversed(rows):
        prefix=row_prefix(row,origins,names,current) if decorate else ""
        text=prefix+row["content"]
        if used+len(text)>budget:
            if selected:
                break
            # 单条就超预算时优先保留来源标记，只截正文；否则模型会看到半截标记。
            keep=max(0,budget-len(prefix))
            text=prefix+row["content"][-keep:] if keep else row["content"][-budget:]
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