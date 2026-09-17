# -*- coding: utf-8 -*-
"""探针 C：真实多轮聊天（走 harness 已验证的 WebChat 通道）。

覆盖 fixture 覆盖不到的三件事：

  1. **多轮下状态栏每轮都在、字段随剧情演进**（不是复读陈旧值）；
  2. **裸字段行不漏到用户可见文本**——这是 L4 单命中剥离的线上对应。
     做法：让模型单独输出一行裸字段（不走 [LOVE_DATA]），然后断言这行
     没有出现在渲染出的状态栏之外。同时看日志里有没有
     `[Quill] L4 单命中` —— 有它就证明新分支在线上真的跑到了。
  3. **标注不回流**：session_vars 里不得出现 ↑/↓ 标注（逐轮累积污染）。

顺带统计 L1-L6 各走了哪一级，作为「L2 是主力路径」结论的线上佐证。

**对第 2 点的诚实说明**：模型是否照做由它自己决定（system prompt 里
写着要用 [LOVE_DATA]，而本探针的指令是反着的）。所以两种结果分开对待：
  * 模型照做了 → 硬断言「裸行不漏」，并报告是否命中 L4 单命中分支；
  * 模型没照做 → 标 NOTE（未覆盖），**不算 FAIL**，并说明原因。
把它记成 FAIL 会让探针随机红，把它当成 PASS 又是粉饰。

安全：改配置前断言基线 md5；`finally` 无条件还原 + 校验回到基线；
结束清测试残留（先 /quill reset，再按完整 session_id 删 memories）。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_live_chat.py
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import auth  # noqa: E402
import chat  # noqa: E402
import logs as logs_mod  # noqa: E402
import paths  # noqa: E402

CFG = r"D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json"
BASE_MD5 = "403fee103fe2c8c82d7166a98c538854"

FIELD_NAMES = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法"]
BARE_FIELD_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:" + "|".join(FIELD_NAMES) + r")\s*[：:=]\s*\S",
    re.M,
)
ANNOT_RE = re.compile(r"[↑↓]\s*\d+")
LEVEL_RE = re.compile(r"\[Quill\] 状态栏已处理 \(([^)]+)\)")
L4_SINGLE_RE = re.compile(r"\[Quill\] L4 单命中")


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def _hide_rendered_bar(text: str) -> str:
    """把「渲染出来的状态栏块」从文本里摘掉，只留模型自由正文。

    只摘模板产出的那几段（围栏块 / 纯文本边线块 / **状态栏** 标题），
    这样剩下的部分才代表「会漏给用户看的裸字段行」。
    """
    out = re.sub(r"```[\s\S]*?```", "", text)
    out = re.sub(r"─+ 状态栏 ─+[\s\S]*?─{4,}", "", out)
    out = re.sub(r"\*\*状态栏\*\*", "", out)
    return out


def _report_bar_shape(reply: str) -> None:
    print("  围栏 ``` :", "```" in reply, "| 标题 **状态栏** :", "**状态栏**" in reply)


def main() -> int:
    if md5() != BASE_MD5:
        print("!! 配置基线不符，中止")
        print("   now      =", md5())
        print("   expected =", BASE_MD5)
        return 2

    c = auth.DashboardClient()
    wc = chat.WebChat()
    tail = logs_mod.LogTail()
    checks: list[tuple[str, bool]] = []
    notes: list[str] = []
    levels: list[str] = []
    saw_l4_single = False
    ok = False

    print("UMO:", wc.umo[-46:])

    try:
        r = c.plugin(
            "POST",
            "config/save_batch",
            json={"updates": [
                {"group": "status_bar", "key": "enabled", "value": True},
            ]},
            timeout=30.0,
        )
        print("save:", (r or {}).get("status"), (r or {}).get("message"))
        if (r or {}).get("status") != "ok":
            print("!! 保存失败，中止")
            return 2
        time.sleep(1.5)

        wc.send("/char 1", timeout=120)

        # ── 三轮真实对话，观察状态栏是否每轮都在、值是否演进 ──
        turns = [
            "【测试】傍晚，她把画板支在窗边，转头问你今天过得怎么样。",
            "【测试】你说起今天遇到的一件小事，她听得入神，凑近了些。",
            "【测试】她忽然笑出声，承认刚才其实一直在偷偷看你。",
        ]
        replies: list[str] = []
        for i, text in enumerate(turns, 1):
            mark = tail.mark()
            resp = wc.send(text, timeout=240)
            reply = resp.get("reply") or ""
            replies.append(reply)
            time.sleep(0.6)
            block = tail.since_plugin(mark)
            lv = LEVEL_RE.search(block)
            if lv:
                levels.append(lv.group(1))
            if L4_SINGLE_RE.search(block):
                saw_l4_single = True

            has_bar = ("好感度" in reply) or ("关系阶段" in reply)
            leak = BARE_FIELD_RE.search(_hide_rendered_bar(reply))
            print()
            print(f"--- 第 {i} 轮 ---")
            _report_bar_shape(reply)
            print("  含状态栏字段 :", has_bar, "(应 True)")
            print("  栏外裸字段行 :", (leak.group(0).strip() if leak else "无"), "(应「无」)")
            print("  命中级别     :", lv.group(1) if lv else "(日志里没有已处理行)")
            checks.append((f"第{i}轮 状态栏出现", has_bar))
            checks.append((f"第{i}轮 栏外无裸字段行", leak is None))

        # ── 字段确实在演进（三轮不能完全一样）──
        def _field_of(reply: str, name: str) -> str:
            m = re.search(rf"{name}\s*[：:=]\s*(.+)", reply)
            return (m.group(1).strip() if m else "")

        seq_mood = [_field_of(r, "心情") for r in replies]
        seq_loc = [_field_of(r, "位置") for r in replies]
        print()
        print("=== 演进观察 ===")
        print("  心情序列:", seq_mood)
        print("  位置序列:", seq_loc)
        evolved = len(set(seq_mood + seq_loc)) > 1
        checks.append(("状态字段随剧情演进（非复读）", evolved))

        # ── 裸字段行专项：明确要求模型只输出一行裸字段 ──
        print()
        print("--- 第 4 轮：要求只输出一行裸字段（L4 单命中专项）---")
        mark = tail.mark()
        resp = wc.send(
            "（测试指令）这一轮请忽略状态栏格式要求，只在回复最末尾"
            "单独另起一行输出：心情：愉快。不要输出任何方括号标记。",
            timeout=240,
        )
        reply4 = resp.get("reply") or ""
        time.sleep(0.6)
        block = tail.since_plugin(mark)
        lv4 = LEVEL_RE.search(block)
        if lv4:
            levels.append(lv4.group(1))
        hit_l4s = bool(L4_SINGLE_RE.search(block))
        saw_l4_single = saw_l4_single or hit_l4s

        leak4 = BARE_FIELD_RE.search(_hide_rendered_bar(reply4))
        print("  栏外裸字段行 :", (leak4.group(0).strip() if leak4 else "无"), "(应「无」)")
        print("  命中级别     :", lv4.group(1) if lv4 else "(无已处理行)")
        print("  走到 L4 单命中分支 :", hit_l4s)
        _report_bar_shape(reply4)
        # 硬断言与「模型是否配合」分开：不漏是硬要求；走到哪一级不由测试决定
        checks.append(("第4轮 栏外无裸字段行", leak4 is None))
        if hit_l4s:
            checks.append(("第4轮 命中 L4 单命中分支（线上证据）", True))
        else:
            notes.append(
                "L4 单命中分支本轮未跑到：模型仍按 system prompt 走了 "
                f"[LOVE_DATA]（命中级别={lv4.group(1) if lv4 else '无'}）。"
                "该分支的行为由 fixture t20 锁定，线上未覆盖不算失败。"
            )

        # ── session_vars 检查：落盘 + 无标注回流 ──
        time.sleep(6.0)  # state 自动落盘 5s，必须等
        entry = paths.read_state().get(wc.umo) or {}
        sv = entry.get("session_vars") or {}
        print()
        print("=== session_vars（等待落盘后）===")
        print("  keys    :", list(sv.keys()))
        print("  content :", {k: str(v)[:28] for k, v in sv.items()})
        checks.append(("session_vars 已落盘", bool(sv)))
        dirty = {k: v for k, v in sv.items() if isinstance(v, str) and ANNOT_RE.search(v)}
        print("  含 ↑/↓ 标注的键 :", dirty if dirty else "无")
        checks.append(("session_vars 无标注回流", not dirty))

        print()
        print("=== L1-L6 命中分布（本轮实测）===")
        for lv, n in sorted((x, levels.count(x)) for x in set(levels)):
            print(f"  {lv}: {n}")
        if levels:
            top = max(set(levels), key=levels.count)
            print(f"  主力: {top}")
            notes.append(f"本轮 {len(levels)} 次解析里，{top} 命中 {levels.count(top)} 次。")

        print()
        print("=== 结果 ===")
        all_ok = True
        for label, good in checks:
            print(f"  [{'PASS' if good else 'FAIL'}] {label}")
            all_ok = all_ok and good
        if notes:
            print()
            print("  NOTE:")
            for n in notes:
                print("   -", n)
        ok = all_ok

    finally:
        r2 = c.plugin(
            "POST",
            "config/save_batch",
            json={"updates": [
                {"group": "status_bar", "key": "enabled", "value": False},
            ]},
            timeout=30.0,
        )
        print()
        print("restore:", (r2 or {}).get("status"))
        time.sleep(2.5)
        now = md5()
        print("md5 =", now, "| OK" if now == BASE_MD5 else "| !!! MISMATCH !!!")
        if now != BASE_MD5:
            ok = False

        # ── 清残留：先 /quill reset（按当前角色卡），再按完整 session_id 删记忆 ──
        print()
        print("=== 清理测试残留 ===")
        umo = wc.umo
        try:
            keys = list(paths.persona_conv_map(umo).keys())
        except Exception:
            keys = []
        try:
            conn = paths.connect_ro(paths.db_path_memory())
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM chat_logs WHERE session_id LIKE ?",
                (umo + "::%",),
            ).fetchall()
            conn.close()
            for row in rows:
                tailkey = str(row["session_id"])[len(umo) + 2:]
                if tailkey and tailkey not in keys:
                    keys.append(tailkey)
        except Exception:
            pass
        if not keys:
            keys = [""]
        for key in keys:
            try:
                wc.send(f"/char {key}" if key else "/char unset", timeout=30.0)
                rep = wc.send("/quill reset", timeout=60.0)
                line = (rep.get("reply") or "").replace("\n", " ")
                print("  reset", key or "(未绑定)", "→", line[:100])
            except Exception as exc:
                print("  reset 失败（可忽略）:", type(exc).__name__)
        try:
            conn = paths.connect_ro(paths.db_path_memory())
            rows = conn.execute(
                "SELECT DISTINCT session_id FROM memories WHERE session_id LIKE ?",
                (umo + "%",),
            ).fetchall()
            conn.close()
            for row in rows:
                sid = str(row["session_id"])
                resp = c.plugin(
                    "POST", "memory/delete", json={"session_id": sid}, timeout=20.0
                )
                d = (resp or {}).get("data") or {}
                if d.get("deleted"):
                    print("  已删记忆", d["deleted"], "条 (…" + sid[-20:] + ")")
        except Exception as exc:
            print("  记忆清理失败（可忽略）:", type(exc).__name__)

    print()
    print(">>> 探针 C:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
