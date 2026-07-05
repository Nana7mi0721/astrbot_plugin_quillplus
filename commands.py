"""Business helpers for QuillPlugin commands.

命令入口（@filter.command 装饰的函数）必须定义在 Star 子类所在的模块
（main.py）里，否则 AstrBot 的 handler 扫描会把它们登记到错误的模块，
导致指令不被分派。这里只放无装饰器的业务函数，由 main.py 调用。
"""

from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageEventResult


# ================================================================
# /wb — 世界书
# ================================================================

async def wb_dispatch(plugin, event: AstrMessageEvent, arg1: str, arg2: str):
    """
    /wb              列表（带序号）
    /wb <序号|名字>  绑定
    /wb off          解绑
    /wb info <序号|名字>  详情
    """
    if not plugin.wb_manager:
        event.set_result(MessageEventResult().message("世界书系统未加载"))
        return

    sub = (arg1 or "").strip().lower()

    if not arg1:
        await _wb_list(plugin, event)
        return

    if sub == "off":
        user_id = str(event.get_sender_id())
        plugin.wb_manager.unbind_user(user_id)
        event.set_result(MessageEventResult().message("已解绑所有世界书"))
        return

    if sub == "info":
        if not arg2:
            event.set_result(MessageEventResult().message("用法: /wb info <序号|名字>"))
            return
        resolved = _resolve_wb_name(plugin, arg2)
        await _wb_info(plugin, event, resolved or arg2)
        return

    if sub == "list":
        await _wb_list(plugin, event)
        return

    # /wb <序号|名字> → 绑定（最常用）
    name = _resolve_wb_name(plugin, arg1) or arg1.strip()
    user_id = str(event.get_sender_id())
    if plugin.wb_manager.bind_user(user_id, name):
        wb = plugin.wb_manager.get_worldbook(name)
        entry_count = len(wb.get("entries", [])) if wb else 0
        event.set_result(MessageEventResult().message(
            f"已绑定世界书: {name} ({entry_count} 条条目)"
        ))
    else:
        books = plugin.wb_manager.list_worldbooks()
        lines = [f"世界书不存在: {arg1}", "", "可用世界书："]
        for i, n in enumerate(books, 1):
            lines.append(f"  {i}. {n}")
        lines.append("")
        lines.append("使用序号或完整名称: /wb 1  或  /wb 名字")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


def _resolve_wb_name(plugin, arg: str) -> str | None:
    """把 '1' / '2' 解析为世界书名。若 arg 是纯数字序号（1-based）则返回对应名字，
    否则返回 None（调用方继续按字符串名字处理）。"""
    if not arg:
        return None
    s = arg.strip()
    if not s.isdigit():
        return None
    idx = int(s) - 1
    books = plugin.wb_manager.list_worldbooks()
    if 0 <= idx < len(books):
        return books[idx]
    return None


async def _wb_list(plugin, event: AstrMessageEvent):
    books = plugin.wb_manager.list_worldbooks()
    if not books:
        event.set_result(MessageEventResult().message("没有可用的世界书"))
        return
    user_id = str(event.get_sender_id())
    bound = set(
        plugin.wb_manager.bindings.get("user_bindings", {}).get(user_id, [])
    )
    lines = ["可用世界书（✓ 已绑定）："]
    for i, name in enumerate(books, 1):
        wb = plugin.wb_manager.get_worldbook(name)
        desc = wb.get("description", "")[:40] if wb else ""
        entry_count = len(wb.get("entries", [])) if wb else 0
        mark = "✓" if name in bound else " "
        lines.append(f"  {mark} {i}. {name} ({entry_count} 条) - {desc}")
    lines.append("")
    lines.append("使用: /wb <序号>   快速绑定（如 /wb 1）")
    lines.append("     /wb <名字>   按名字绑定")
    lines.append("     /wb off      解绑全部")
    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def _wb_info(plugin, event: AstrMessageEvent, name: str):
    wb = plugin.wb_manager.get_worldbook(name)
    if not wb:
        event.set_result(MessageEventResult().message(f"世界书不存在: {name}"))
        return
    lines = [f"世界书: {name}"]
    lines.append(f"描述: {wb.get('description', '')}")
    lines.append(f"条目数: {len(wb.get('entries', []))}")
    for entry in wb.get("entries", []):
        status = "常驻" if entry.get("is_constant") else "触发"
        enabled = "开" if entry.get("enabled", True) else "关"
        keys = ", ".join(entry.get("keys", [])) if entry.get("keys") else "无"
        lines.append(f"  [{status}|{enabled}] {entry.get('title', '')} (keys: {keys})")
    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


# ================================================================
# /char — 角色卡
# ================================================================

async def char_dispatch(plugin, event: AstrMessageEvent, arg: str):
    """
    /char             列表（带序号）
    /char <序号|名字> 切换
    /char unset       取消角色卡
    """
    sub = (arg or "").strip().lower()

    if not arg:
        personas = plugin.context.persona_manager.personas
        if not personas:
            event.set_result(MessageEventResult().message("没有可用的角色卡"))
            return
        lines = ["可用角色卡："]
        for i, p in enumerate(personas, 1):
            lines.append(f"  {i}. {p.persona_id}")
        lines.append("")
        lines.append("使用: /char <序号>   快速切换（如 /char 1）")
        lines.append("     /char <名字>   按名字切换")
        lines.append("     /char unset    取消")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
        return

    if sub == "unset":
        umo = event.unified_msg_origin
        await plugin.context.conversation_manager.update_conversation_persona_id(
            umo, "[%None]"
        )
        event.set_result(MessageEventResult().message("已取消角色卡，将使用默认人设。"))
        return

    if sub == "list":
        await char_dispatch(plugin, event, "")
        return

    # /char <序号|名字> → 切换
    personas = plugin.context.persona_manager.personas
    name = arg.strip()
    # 序号解析
    if name.isdigit():
        idx = int(name) - 1
        if 0 <= idx < len(personas):
            name = personas[idx].persona_id
        else:
            event.set_result(MessageEventResult().message(
                f"序号超出范围: {arg}\n发送 /char 查看列表"
            ))
            return
    if not any(p.persona_id == name for p in personas):
        event.set_result(MessageEventResult().message(
            f"角色卡不存在: {arg}\n发送 /char 查看列表"
        ))
        return
    umo = event.unified_msg_origin
    await plugin.context.conversation_manager.update_conversation_persona_id(umo, name)
    event.set_result(MessageEventResult().message(
        f"已切换到: {name}\n建议使用 /reset 清空上下文，防止旧对话影响新角色。"
    ))


# ================================================================
# /quill — 状态 / 匹配测试
# ================================================================

async def quill_status(plugin, event: AstrMessageEvent):
    user_id = str(event.get_sender_id())
    lines = ["[Quill 状态]"]

    state = await plugin.state_manager.get_state(user_id)
    lines.append(f"  对话轮次: {state.round_count}")
    lines.append(f"  拒绝标记: {'是 (下次将注入应急协议)' if state.refusal_detected else '否'}")
    if state.last_refusal_time:
        lines.append(f"  最近拒绝: {state.last_refusal_time}")

    if plugin.kb_manager:
        try:
            stats = await plugin.kb_manager.get_stats()
            lines.append(
                f"  写作素材库: {stats['total_entries']} 条 "
                f"({stats['enabled_entries']} 启用, {stats['disabled_entries']} 禁用)"
            )
            logs = await plugin.kb_manager.get_match_logs(limit=5)
            if logs:
                lines.append("  最近匹配:")
                for log in logs[:3]:
                    inp = (log.get("user_input") or "")[:40]
                    cnt = log.get("match_count", 0)
                    lines.append(f'    "{inp}" → {cnt} 条')
            if stats["total_logs"] > 5:
                lines.append(f"    ... 共 {stats['total_logs']} 条日志")
        except Exception as e:
            lines.append(f"  写作素材库: 查询失败 ({e})")
    else:
        lines.append("  写作素材库: 未加载")

    if plugin.wb_manager:
        try:
            persona_id = None
            if hasattr(event, "get_persona_id"):
                persona_id = event.get_persona_id()
            active = plugin.wb_manager.get_active_worldbooks(
                persona_id=persona_id, user_id=user_id
            )
            if active:
                names = [w.get("name", "?") for w in active]
                lines.append(f"  世界书: {', '.join(names)}")
            else:
                lines.append("  世界书: 未绑定")
        except Exception:
            pass

    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def quill_test(plugin, event: AstrMessageEvent, text: str):
    if not plugin.kb_manager:
        event.set_result(MessageEventResult().message("写作素材库未加载，无法测试"))
        return
    try:
        # log_match=False: 测试不应污染 match_count 统计
        results = await plugin.kb_manager.match(text, top_k=5, log_match=False)
        if not results:
            event.set_result(MessageEventResult().message(
                f"未匹配到任何条目\n输入: {text[:80]}"
            ))
            return
        lines = [f"匹配到 {len(results)} 条:"]
        for r in results:
            name = r.get("name") or r.get("entry_id", "?")
            score = r.get("match_score", 0)
            kw = r.get("matched_keywords", [])
            kw_str = ", ".join(str(k) for k in (kw or []))
            lines.append(f"  [{score:.1f}] {name}")
            if kw_str:
                lines.append(f"       关键词: {kw_str}")
        lines.append(f"输入: {text[:60]}")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
    except Exception as e:
        event.set_result(MessageEventResult().message(f"匹配失败: {e}"))


# ================================================================
# /stream — 流式控制
# ================================================================

_MODE_MAP = {
    "on": "on", "off": "off", "auto": "auto",
    "开": "on", "关": "off", "自动": "auto",
}


async def stream_dispatch(plugin, event: AstrMessageEvent, arg: str):
    """/stream on|off|auto — 控制流式模式"""
    user_id = str(event.get_sender_id())
    arg = (arg or "").strip().lower()

    if arg not in _MODE_MAP:
        state = await plugin.state_manager.get_state(user_id)
        event.set_result(MessageEventResult().message(
            f"当前流式模式: {state.stream_mode}\n"
            "用法: /stream on|off|auto"
        ))
        return

    new_mode = _MODE_MAP[arg]
    await plugin.state_manager.set_stream_mode(user_id, new_mode)
    mode_names = {"on": "开启（强制流式）", "off": "关闭（强制无流式）", "auto": "自动（默认）"}
    event.set_result(MessageEventResult().message(
        f"流式模式已设为: {mode_names[new_mode]}"
    ))


# ================================================================
# /reinject — 强制重置注入状态
# ================================================================

async def reinject_dispatch(plugin, event: AstrMessageEvent):
    """/reinject — 重置 quill_rounds，下次激活重新注入全部常驻内容"""
    user_id = str(event.get_sender_id())
    await plugin.state_manager.reset_quill_rounds(user_id)
    event.set_result(MessageEventResult().message(
        "已重置注入状态。下次触发 Quill 时将重新注入全部常驻素材。"
    ))
