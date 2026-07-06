"""Business helpers for QuillPlugin commands.

命令入口（@filter.command 装饰的函数）必须定义在 Star 子类所在的模块
（main.py）里，否则 AstrBot 的 handler 扫描会把它们登记到错误的模块，
导致指令不被分派。这里只放无装饰器的业务函数，由 main.py 调用。
"""

import json

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
    /wb reload       重载全部世界书
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

    if sub == "reload":
        try:
            await plugin.wb_manager.reload_all()
            count = len(plugin.wb_manager.list_worldbooks())
            event.set_result(MessageEventResult().message(f"已重载全部世界书 ({count} 本)"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"重载失败: {e}"))
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
    user_bound = set(
        plugin.wb_manager.bindings.get("user_bindings", {}).get(user_id, [])
    )

    # 获取角色自带的世界书
    persona_bound: set = set()
    persona_name = ""
    if hasattr(plugin.state_manager, "get_persona_id"):
        pid = await plugin.state_manager.get_persona_id(user_id)
        if pid:
            persona_bound = set(
                plugin.wb_manager.bindings.get("persona_bindings", {}).get(pid, [])
            )
            if plugin.persona_manager:
                pdata = await plugin.persona_manager.get_persona(pid)
                persona_name = pdata.get("name", pid) if pdata else pid

    lines = ["可用世界书（✓ 已绑定）："]
    for i, name in enumerate(books, 1):
        wb = plugin.wb_manager.get_worldbook(name)
        desc = wb.get("description", "")[:40] if wb else ""
        entry_count = len(wb.get("entries", [])) if wb else 0

        mark = "✓" if name in user_bound else " "
        tag = ""
        if name in persona_bound and name in user_bound:
            tag = " [角色+玩家]"
        elif name in persona_bound:
            tag = " [角色自带]"
        elif name in user_bound:
            tag = " [玩家挂载]"

        lines.append(f"  {mark} {i}. {name} ({entry_count} 条){tag} - {desc}")

    if persona_name:
        lines.append(f"\n当前角色自带者: {persona_name}（[角色自带] 强制生效，无需手动绑定）")

    lines.append("")
    lines.append("使用: /wb <序号>   快速绑定")
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
    /char info [序号|名字]  查看详情
    /char export [序号|名字]  导出 JSON
    /char import <JSON>  从 JSON 导入角色卡
    """
    sub_args = (arg or "").strip()
    parts = sub_args.split(None, 1) if sub_args else []
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if not parts:
        await _char_list(plugin, event)
        return

    if sub == "list":
        await _char_list(plugin, event)
        return

    if sub == "unset":
        user_id = str(event.get_sender_id())
        await plugin.state_manager.set_persona_id(user_id, "")
        event.set_result(MessageEventResult().message("已取消角色卡，将使用默认人设。"))
        return

    if sub == "info":
        if not rest:
            # 查看当前角色
            user_id = str(event.get_sender_id())
            pid = await plugin.state_manager.get_persona_id(user_id)
            if not pid:
                event.set_result(MessageEventResult().message("未绑定角色卡。使用 /char 查看列表。"))
                return
            await _char_info(plugin, event, pid)
        else:
            resolved = await _resolve_persona_id(plugin, rest, event)
            if resolved is None:
                return
            await _char_info(plugin, event, resolved)
        return

    if sub == "export":
        if not rest:
            # 导出当前角色卡
            user_id = str(event.get_sender_id())
            pid = await plugin.state_manager.get_persona_id(user_id)
            if not pid:
                event.set_result(MessageEventResult().message("未绑定角色卡，无法导出。"))
                return
            await _char_export(plugin, event, pid)
        else:
            resolved = await _resolve_persona_id(plugin, rest, event)
            if resolved is None:
                return
            await _char_export(plugin, event, resolved)
        return

    if sub == "import":
        if not rest:
            event.set_result(MessageEventResult().message("用法: /char import <角色卡 JSON>"))
            return
        await _char_import(plugin, event, rest)
        return

    # /char <序号|名字> → 切换
    resolved = await _resolve_persona_id(plugin, sub_args, event)
    if resolved is None:
        return

    personas = await plugin.persona_manager.load_all()
    matched = None
    for p in personas:
        if p.get("id") == resolved:
            matched = p
            break
    if not matched:
        event.set_result(MessageEventResult().message(
            f"角色卡不存在: {sub_args}\n发送 /char 查看列表"
        ))
        return

    user_id = str(event.get_sender_id())
    pid = matched.get("id", resolved)
    await plugin.state_manager.set_persona_id(user_id, pid)

    # 统计联动信息
    wbs = []
    rag_docs = []
    if plugin.wb_manager:
        active = plugin.wb_manager.get_active_worldbooks(persona_id=pid)
        wbs = [w.get("name", "?") for w in active]
    if matched.get("quill_extensions"):
        rag_docs = matched["quill_extensions"].get("bound_rag_docs", [])

    msg_parts = [f"已切换到: {matched.get('name', pid)}"]
    link_info = []
    if wbs:
        link_info.append(f"{len(wbs)} 本世界书: {', '.join(wbs)}")
    if rag_docs:
        link_info.append(f"{len(rag_docs)} 个文档知识库: {', '.join(rag_docs)}")
    if link_info:
        msg_parts.append("已自动挂载：")
        msg_parts.append("；".join(link_info))
    msg_parts.append("\n建议使用 /reset 清空上下文，防止旧对话影响新角色。")

    event.set_result(MessageEventResult().message("\n".join(msg_parts)))


async def _char_list(plugin, event: AstrMessageEvent):
    personas = await plugin.persona_manager.load_all()
    if not personas:
        event.set_result(MessageEventResult().message("没有可用的角色卡"))
        return

    user_id = str(event.get_sender_id())
    current_pid = await plugin.state_manager.get_persona_id(user_id)

    lines = ["可用角色卡："]
    for i, p in enumerate(personas, 1):
        name = p.get("name", p.get("id", "?"))
        summary = p.get("summary", "")
        pid = p.get("id", "")
        mark = " ← 当前" if pid == current_pid and current_pid else ""
        suffix = f" — {summary[:30]}" if summary else ""
        lines.append(f"  {i}. {name}{suffix}{mark}")
    lines.append("")
    lines.append("使用: /char <序号>   快速切换")
    lines.append("     /char info     查看当前角色卡详情")
    lines.append("     /char export   导出当前角色卡 JSON")
    lines.append("     /char unset    取消")
    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def _char_info(plugin, event: AstrMessageEvent, persona_id: str):
    pdata = await plugin.persona_manager.get_persona(persona_id)
    if not pdata:
        event.set_result(MessageEventResult().message(f"角色卡不存在: {persona_id}"))
        return

    name = pdata.get("name", persona_id)
    summary = pdata.get("summary", "")
    cp = pdata.get("core_prompts", {})
    personality = cp.get("personality", "")
    first_msg = cp.get("first_message", "")
    scenario = cp.get("scenario", "")
    examples = cp.get("examples_of_dialogue", "")

    lines = [f"【角色卡】{name}"]
    if summary:
        lines.append(f"简介：{summary}")
    if personality:
        pers_short = personality[:80] + "..." if len(personality) > 80 else personality
        lines.append(f"人设：{pers_short}")
    if first_msg:
        fm_short = first_msg[:60] + "..." if len(first_msg) > 60 else first_msg
        lines.append(f"开场白：{fm_short}")
    if scenario:
        sc_short = scenario[:60] + "..." if len(scenario) > 60 else scenario
        lines.append(f"场景：{sc_short}")
    if examples:
        ex_short = examples[:60] + "..." if len(examples) > 60 else examples
        lines.append(f"对话示例：{ex_short}")

    # 联动信息
    ext = pdata.get("quill_extensions", {})
    wb_names = ext.get("bound_worldbooks", []) if ext else []
    rag_docs = ext.get("bound_rag_docs", []) if ext else []

    if wb_names or rag_docs:
        lines.append("\n[联动拓展]")
        if wb_names:
            lines.append(f"专属世界书：{', '.join(wb_names)}")
        if rag_docs:
            lines.append(f"专属文档知识库：{', '.join(rag_docs)}")

    # 活跃世界书（包含全局 + 角色自带）
    if plugin.wb_manager:
        active = plugin.wb_manager.get_active_worldbooks(persona_id=persona_id)
        if active:
            active_names = [w.get("name", "?") for w in active]
            lines.append(f"当前生效世界书：{', '.join(active_names)}")

    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def _char_export(plugin, event: AstrMessageEvent, persona_id: str):
    pdata = await plugin.persona_manager.get_persona(persona_id)
    if not pdata:
        event.set_result(MessageEventResult().message(f"角色卡不存在: {persona_id}"))
        return

    # 导出为可复制的 JSON（清理内部字段）
    export_data = {
        "name": pdata.get("name", ""),
        "summary": pdata.get("summary", ""),
        "core_prompts": pdata.get("core_prompts", {}),
        "quill_extensions": pdata.get("quill_extensions", {}),
    }
    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)

    lines = [
        f"【{pdata.get('name', persona_id)}】角色卡 JSON（复制下方内容后用 /char import 导入）：",
        "```json",
        json_str,
        "```",
    ]
    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def _char_import(plugin, event: AstrMessageEvent, json_text: str):
    # 清理可能的 markdown 代码块包裹
    cleaned = json_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # 从文本中提取 JSON（可能混有其他文字）
    # 尝试找到第一个 { 到最后一个 }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        event.set_result(MessageEventResult().message("未在输入中找到有效的 JSON 对象。"))
        return
    json_str = cleaned[start:end + 1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        event.set_result(MessageEventResult().message(f"JSON 解析失败: {e}"))
        return

    if not data.get("name"):
        data["name"] = data.get("name", "导入的角色")

    try:
        result = await plugin.persona_manager.create_persona(data)
        name = result.get("name", data["name"])
        event.set_result(MessageEventResult().message(
            f"角色卡导入成功！名称: {name}\n使用 /char {name} 切换到新角色。"
        ))
    except Exception as e:
        event.set_result(MessageEventResult().message(f"导入失败: {e}"))


async def _resolve_persona_id(plugin, arg: str, event: AstrMessageEvent) -> str | None:
    """解析序号或名字为 persona_id。解析失败时直接发送错误消息并返回 None。"""
    personas = await plugin.persona_manager.load_all()
    name = arg.strip()

    # 序号
    if name.isdigit():
        idx = int(name) - 1
        if 0 <= idx < len(personas):
            return personas[idx].get("id", personas[idx].get("name", ""))
        event.set_result(MessageEventResult().message(
            f"序号超出范围: {arg}\n发送 /char 查看列表"
        ))
        return None

    # 按 id 或 name 匹配
    for p in personas:
        if p.get("id") == name or p.get("name") == name:
            return p.get("id", name)

    event.set_result(MessageEventResult().message(
        f"角色卡不存在: {arg}\n发送 /char 查看列表"
    ))
    return None


# ================================================================
# /quill — 状态总览 / 多系统测试
# ================================================================

async def quill_status(plugin, event: AstrMessageEvent):
    """Quill 系统总览 — 覆盖五大系统状态"""
    user_id = str(event.get_sender_id())
    lines = ["[Quill 运行状态]"]

    # 角色卡
    persona_id = await plugin.state_manager.get_persona_id(user_id) if hasattr(plugin.state_manager, "get_persona_id") else ""
    persona_name = ""
    wb_count = 0
    if persona_id and plugin.persona_manager:
        pdata = await plugin.persona_manager.get_persona(persona_id)
        persona_name = pdata.get("name", persona_id) if pdata else persona_id
        if plugin.wb_manager:
            wb_count = len(plugin.wb_manager.get_active_worldbooks(persona_id=persona_id))

    if persona_name:
        lines.append(f"  角色：{persona_name} (已绑 {wb_count} 本世界书)")
    else:
        lines.append("  角色：默认")

    # 流式模式
    state = await plugin.state_manager.get_state(user_id)
    lines.append(f"  流式模式：{state.stream_mode}")

    # 写作素材库
    if plugin.kb_manager:
        try:
            stats = await plugin.kb_manager.get_stats()
            ext_info = ""
            if persona_id and plugin.persona_manager and pdata:
                ext = pdata.get("quill_extensions", {})
                kb_cats = ext.get("bound_knowledge_base", []) if ext else []
                if kb_cats:
                    ext_info = f" (仅限: {', '.join(kb_cats)})"
            lines.append(f"  写作素材库: {stats['total_entries']} 条启用{ext_info}")
        except Exception:
            lines.append("  写作素材库: 查询失败")
    else:
        lines.append("  写作素材库: 未加载")

    # 动态记忆
    if plugin.rag_memory_store:
        try:
            mem_stats = plugin.rag_memory_store.get_stats()
            total_mem = mem_stats.get("total_memories", 0)
            total_sessions = mem_stats.get("total_sessions", 0)
            lines.append(f"  动态记忆: {total_mem} 条 ({total_sessions} 个会话)")
        except Exception:
            lines.append("  动态记忆: 查询失败")
    else:
        lines.append("  动态记忆: 未加载")

    # Doc RAG
    if plugin.rag_vector_store:
        try:
            vs_stats = plugin.rag_vector_store.get_stats()
            doc_count = vs_stats.get("total_documents", 0)
            lines.append(f"  Doc RAG: {doc_count} 条向量")
        except Exception:
            lines.append("  Doc RAG: 查询失败")
    else:
        lines.append("  Doc RAG: 未加载")

    event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))


async def quill_test(plugin, event: AstrMessageEvent, system: str, text: str):
    """
    /quill test kb <文本>  — 测试素材库命中
    /quill test wb <文本>  — 测试世界书命中
    /quill test mem <文本> — 测试记忆检索
    """
    system = (system or "").strip().lower()
    text = (text or "").strip()

    if not text:
        event.set_result(MessageEventResult().message("用法: /quill test <kb|wb|mem> <文字>"))
        return

    if system == "kb":
        await _test_kb(plugin, event, text)
    elif system == "wb":
        await _test_wb(plugin, event, text)
    elif system == "mem":
        await _test_mem(plugin, event, text)
    else:
        event.set_result(MessageEventResult().message("用法: /quill test <kb|wb|mem> <文字>"))


async def _test_kb(plugin, event: AstrMessageEvent, text: str):
    if not plugin.kb_manager:
        event.set_result(MessageEventResult().message("写作素材库未加载，无法测试"))
        return
    try:
        results = await plugin.kb_manager.match(text, top_k=5, log_match=False)
        if not results:
            event.set_result(MessageEventResult().message(
                f"KB 未匹配到任何条目\n输入: {text[:80]}"
            ))
            return
        lines = [f"[KB 测试] 匹配到 {len(results)} 条:"]
        for r in results:
            name = r.get("name") or r.get("entry_id", "?")
            score = r.get("match_score", 0)
            kw = r.get("matched_keywords", [])
            kw_str = ", ".join(str(k) for k in (kw or []))
            cat = r.get("category", "")
            cat_str = f" | 分类: {cat}" if cat else ""
            lines.append(f"  [{score:.1f}] {name}{cat_str}")
            if kw_str:
                lines.append(f"       关键词: {kw_str}")
        lines.append(f"输入: {text[:60]}")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
    except Exception as e:
        event.set_result(MessageEventResult().message(f"KB 匹配失败: {e}"))


async def _test_wb(plugin, event: AstrMessageEvent, text: str):
    if not plugin.wb_manager:
        event.set_result(MessageEventResult().message("世界书系统未加载，无法测试"))
        return
    try:
        user_id = str(event.get_sender_id())
        persona_id = await plugin.state_manager.get_persona_id(user_id) if hasattr(plugin.state_manager, "get_persona_id") else None
        results = plugin.wb_manager.match_entries(text, persona_id=persona_id, user_id=user_id, top_k=5)
        if not results:
            event.set_result(MessageEventResult().message(
                f"WB 未匹配到任何条目\n输入: {text[:80]}"
            ))
            return
        trigger_log = plugin.wb_manager.get_trigger_log()
        lines = [f"[WB 测试] 匹配到 {len(results)} 条:"]
        for log in trigger_log[:5]:
            title = log.get("title", "?")
            score = log.get("score", 0)
            wb_name = log.get("worldbook", "")
            matched = log.get("matched_keys", [])
            keys_str = ", ".join(matched) if matched else ""
            lines.append(f"  [{score}] {title} (来自: {wb_name})")
            if keys_str:
                lines.append(f"       触发词: {keys_str}")
        lines.append(f"输入: {text[:60]}")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
    except Exception as e:
        event.set_result(MessageEventResult().message(f"WB 匹配失败: {e}"))


async def _test_mem(plugin, event: AstrMessageEvent, text: str):
    if not plugin.rag_retriever or not plugin.rag_retriever.memory_store:
        event.set_result(MessageEventResult().message("动态记忆未加载，无法测试"))
        return
    if not plugin.rag_retriever.enable_memory:
        event.set_result(MessageEventResult().message("动态记忆功能未启用。"))
        return
    try:
        session_id = str(event.unified_msg_origin) if hasattr(event, 'unified_msg_origin') else str(event.get_sender_id())
        results = await plugin.rag_retriever.search_memories(session_id, text)
        if not results:
            event.set_result(MessageEventResult().message(
                f"Mem 未检索到相关记忆\n输入: {text[:80]}\n会话: {session_id}"
            ))
            return
        lines = [f"[Mem 测试] 检索到 {len(results)} 条记忆:"]
        for r in results:
            summary = r.get("summary", r.get("content", "?"))[:80]
            score = r.get("score", 0)
            lines.append(f"  [{score:.2f}] {summary}")
        lines.append(f"输入: {text[:60]}")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
    except Exception as e:
        event.set_result(MessageEventResult().message(f"记忆检索失败: {e}"))


# ================================================================
# /memory — 动态记忆管理
# ================================================================

async def memory_dispatch(plugin, event: AstrMessageEvent, arg1: str, arg2: str):
    """
    /memory                    当前记忆统计
    /memory list [页码]        列出记忆（5条/页）
    /memory del <序号>         删除指定记忆
    /memory clear              清空当前会话所有记忆
    /memory learn <内容>       手动添加一条记忆
    /memory search <关键词>    关键词搜索记忆
    """
    if not plugin.rag_memory_store:
        event.set_result(MessageEventResult().message("动态记忆系统未加载"))
        return

    sub = (arg1 or "").strip().lower()
    user_id = str(event.get_sender_id())
    session_id = str(event.unified_msg_origin) if hasattr(event, 'unified_msg_origin') else user_id

    if not arg1:
        stats = plugin.rag_memory_store.get_stats()
        lines = [
            f"[记忆统计]",
            f"  总记忆数: {stats.get('total_memories', 0)}",
            f"  总会话数: {stats.get('total_sessions', 0)}",
            f"\n使用: /memory list 查看列表",
            f"     /memory del <序号> 删除记忆",
            f"     /memory clear 清空当前会话",
            f"     /memory learn <内容> 添加记忆",
        ]
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
        return

    if sub == "list":
        page_str = (arg2 or "1").strip()
        try:
            page = max(1, int(page_str))
        except ValueError:
            page = 1
        page_size = 5
        offset = (page - 1) * page_size
        try:
            all_memories = plugin.rag_memory_store.list_memories(session_id, limit=offset + page_size)
        except Exception:
            all_memories = []

        if not all_memories:
            event.set_result(MessageEventResult().message(f"当前会话没有记忆。\n会话: {session_id}"))
            return

        page_items = all_memories[offset:offset + page_size]
        total = len(all_memories)
        total_pages = max(1, (total + page_size - 1) // page_size)

        lines = [f"[记忆列表] 第 {page}/{total_pages} 页 (共 {total} 条)"]
        for idx, m in enumerate(page_items, offset + 1):
            mid = m.get("id", "?")
            summary = m.get("summary", "?")[:60]
            ts = m.get("timestamp", "")
            lines.append(f"  #{idx} [{mid}] {summary}")
            if ts:
                lines.append(f"        {ts}")
        lines.append(f"\n使用: /memory del <序号> 删除")
        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
        return

    if sub == "del":
        idx_str = (arg2 or "").strip()
        if not idx_str or not idx_str.isdigit():
            event.set_result(MessageEventResult().message("用法: /memory del <序号>（使用 /memory list 查看序号）"))
            return
        idx = int(idx_str)
        try:
            all_memories = plugin.rag_memory_store.list_memories(session_id, limit=max(idx, 50))
            if 0 < idx <= len(all_memories):
                mid = all_memories[idx - 1].get("id")
                if mid and plugin.rag_memory_store.delete_memory(mid):
                    event.set_result(MessageEventResult().message(f"已删除记忆 #{idx}"))
                else:
                    event.set_result(MessageEventResult().message(f"删除失败"))
            else:
                event.set_result(MessageEventResult().message(f"序号超出范围: {idx}"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"删除失败: {e}"))
        return

    if sub == "clear":
        try:
            deleted = plugin.rag_memory_store.delete_session_memories(session_id)
            event.set_result(MessageEventResult().message(f"已清空当前会话 {deleted} 条记忆"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"清空失败: {e}"))
        return

    if sub == "learn":
        content = (arg2 or "").strip()
        if not content:
            event.set_result(MessageEventResult().message("用法: /memory learn <内容>"))
            return
        if not plugin.rag_retriever or not plugin.rag_retriever.enable_memory:
            event.set_result(MessageEventResult().message(
                "动态记忆功能未启用，无法学习。\n"
                "请在对话中自然产生内容，系统会自动提取和存储。"
            ))
            return
        try:
            await plugin.rag_retriever.store_memory_direct(session_id, content)
            event.set_result(MessageEventResult().message(f"已学习: {content[:50]}..."))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"学习失败: {e}"))
        return

    if sub == "search":
        query = (arg2 or "").strip()
        if not query:
            event.set_result(MessageEventResult().message("用法: /memory search <关键词>"))
            return
        try:
            if plugin.rag_retriever:
                results = await plugin.rag_retriever.search_memories(session_id, query)
                if not results:
                    event.set_result(MessageEventResult().message("未找到匹配的记忆。"))
                    return
                lines = [f"[记忆搜索] \"{query}\" → {len(results)} 条:"]
                for r in results:
                    summary = r.get("summary", "?")[:60]
                    score = r.get("score", 0)
                    lines.append(f"  [{score:.2f}] {summary}")
                event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
            else:
                event.set_result(MessageEventResult().message("检索器未就绪"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"搜索失败: {e}"))
        return

    event.set_result(MessageEventResult().message(
        "未知子命令。\n用法: /memory [list|del|clear|learn|search]"
    ))


# ================================================================
# /doc — 外部文档 (Doc RAG)
# ================================================================

async def doc_dispatch(plugin, event: AstrMessageEvent, arg1: str, arg2: str):
    """
    /doc list             列出已加载的外部文档
    /doc search <关键词>  RAG 检索返回原文片段
    """
    if not plugin.rag_vector_store:
        event.set_result(MessageEventResult().message("Doc RAG 系统未加载"))
        return

    sub = (arg1 or "").strip().lower()

    if not arg1 or sub == "list":
        try:
            docs = plugin.rag_vector_store.list_documents()
            if not docs:
                event.set_result(MessageEventResult().message("没有已加载的外部文档。"))
                return
            lines = [f"已加载文档 ({len(docs)} 个):"]
            for i, d in enumerate(docs, 1):
                name = d.get("source", d.get("doc_id", "?"))
                chunks = d.get("chunk_count", "")
                chunk_str = f" ({chunks} 段)" if chunks else ""
                lines.append(f"  {i}. {name}{chunk_str}")
            event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"查询失败: {e}"))
        return

    if sub == "search":
        query = (arg2 or "").strip()
        if not query:
            event.set_result(MessageEventResult().message("用法: /doc search <关键词>"))
            return
        try:
            if plugin.rag_retriever:
                results = await plugin.rag_retriever.search_documents(query)
                if not results:
                    event.set_result(MessageEventResult().message("未找到匹配的文档。"))
                    return
                lines = [f"[Doc 检索] \"{query}\" → {len(results)} 段:"]
                for r in results:
                    content = r.get("content", "?")[:100]
                    score = r.get("score", 0)
                    source = r.get("source", "")
                    source_str = f" ({source})" if source else ""
                    lines.append(f"  [{score:.2f}]{source_str} {content}")
                event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
            else:
                event.set_result(MessageEventResult().message("检索器未就绪"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"搜索失败: {e}"))
        return

    event.set_result(MessageEventResult().message(
        "未知子命令。\n用法: /doc [list|search <关键词>]"
    ))


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
