# -*- coding: utf-8 -*-
"""探针 D：会话内 on → off 的**中途切换**（不清上下文）。

为什么单独写这一条：探针 B 走的是 `auto → on → auto`，**从未真正切到 off**；
探针 C 全程开着，中途也没关过。而「从开到关」恰好是两个 P0 改动的落点：

  * P0-1 关闭时不再注入 `session_vars`（`prompt_builder.py:215` 的
    `if session_vars and self.status_bar_enabled`）；
  * P1-4 关闭时剥离历史 `req.contexts` 里已渲染的状态栏
    （`main.py:2065`，且必须在上下文恢复之后执行）。

不清上下文是关键——上下文里留着前几轮带状态栏的 assistant 消息，
模型有强烈的模仿倾向；如果那两个门控没生效，切换后状态栏会「阴魂不散」。

断言四条：
  A. on 阶段：回复里有状态栏（基线，证明开着时确实在写）
  B. off 之后第一轮：回复里**没有**状态栏字段、没有围栏、没有 [LOVE_DATA]
  C. 连聊三轮：不能出现「迟到的」状态栏（模型模仿历史需要一轮以上才显现）
  D. 全程 config md5 不变（会话覆盖存在 state 文件，不写面板配置）

安全：`finally` 无条件把会话复位 `auto` 并还原面板开关，再校验 md5。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_session_on_off.py
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

CFG = r"D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json"
BASE_MD5 = "403fee103fe2c8c82d7166a98c538854"

# 状态栏痕迹：字段行、[LOVE_DATA] 标记、代码块围栏、剧情走向块
FIELD_RE = re.compile(
    r"(?:好感度|关系阶段|心情|位置|穿着|当前想法)\s*[：:=]\s*\S"
)
MARKER_RE = re.compile(r"\[LOVE_DATA\]|\[STATUS\]|\*\*状态栏\*\*")
PLOT_RE = re.compile(r"剧情走向|请选择")


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def _trace(reply: str) -> dict:
    """一条回复里有哪些状态栏痕迹。"""
    return {
        "字段行": bool(FIELD_RE.search(reply)),
        "标记": bool(MARKER_RE.search(reply)),
        "围栏": "```" in reply,
        "剧情块": bool(PLOT_RE.search(reply)),
    }


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
        # 记下「翻转后」的 md5：本探针自己把 enabled 改成了 True，所以运行期间
        # 配置**本来就不该**等于基线。真正要断言的是「会话覆盖不写面板配置」——
        # 即中途再也没有任何写入，md5 一直停在这个翻转后的值上。
        flipped_md5 = md5()
        print("flipped md5 =", flipped_md5, "（运行期间应保持不变）")

        wc.send("/char 1", timeout=120)

        # ── 阶段 A：开着聊两轮，确认状态栏确实在（基线）──
        print()
        print("=== 阶段 A：status_bar 开（建立历史示范）===")
        a_replies = []
        for i in (1, 2):
            resp = wc.send(
                f"【测试】第{i}次：她抬头看你，似乎在等你说点什么。", timeout=240
            )
            rep = resp.get("reply") or ""
            a_replies.append(rep)
            tr = _trace(rep)
            print(f"  第{i}轮 {tr}")
        a_has = any(_trace(x)["字段行"] for x in a_replies)
        checks.append(("A. on 阶段确实有状态栏（基线）", a_has))
        if not a_has:
            notes.append("on 阶段就没出现状态栏，后续 off 断言失去对照意义。")

        # 确认此时上下文里确实带了状态栏（不清上下文的前提成立）
        time.sleep(0.5)
        checks.append(("A2. on 阶段历史示范已建立（供 off 阶段对照）",
                       any(_trace(x)["字段行"] for x in a_replies)))

        # ── 阶段 B：会话内切 off（不动面板、不清上下文）──
        print()
        print("=== 阶段 B：/quill statusbar off（面板仍开着，不清上下文）===")
        r_off = wc.send("/quill statusbar off", timeout=60)
        print("  ", (r_off.get("reply") or "").replace("\n", " ")[:130])
        checks.append(("B0. off 写入成功", "强制关闭" in (r_off.get("reply") or "")))

        r_q = wc.send("/quill statusbar", timeout=60)
        t_q = r_q.get("reply") or ""
        print("  ", t_q.replace("\n", " ")[:150])
        checks.append(("B0b. 报告生效值=关闭", "本轮实际生效: 关闭" in t_q))

        # ── 阶段 B/C：切 off 后连聊三轮 ──
        print()
        print("=== 阶段 C：off 后连聊三轮（上下文未清，历史里仍有示范）===")
        off_replies = []
        for i in (1, 2, 3):
            mark = tail.mark()
            resp = wc.send(
                f"【测试】第{i}次：她换了个坐姿，继续刚才的话题。", timeout=240
            )
            rep = resp.get("reply") or ""
            off_replies.append(rep)
            time.sleep(0.6)
            block = tail.since_plugin(mark)
            parsed = re.search(r"\[Quill\] 状态栏(已处理|解析失败|兜底注入)", block)
            tr = _trace(rep)
            print(f"  第{i}轮 {tr}")
            if parsed:
                print(f"        ⚠ 日志出现了状态栏处理行: {parsed.group(0)}")
            print(f"        尾部 120 字: {rep[-120:]!r}")

        # B: 第一轮就该干净
        t1 = _trace(off_replies[0])
        checks.append(("B. off 后第 1 轮：无字段行", not t1["字段行"]))
        checks.append(("B. off 后第 1 轮：无标记/围栏", not t1["标记"] and not t1["围栏"]))

        # C: 三轮都不能有（模仿历史通常要一两轮才显现）
        for i, rep in enumerate(off_replies, 1):
            tr = _trace(rep)
            checks.append((f"C. 第 {i} 轮仍无状态栏痕迹", not any(tr.values())))

        if any(any(_trace(x).values()) for x in off_replies):
            notes.append(
                "关闭后仍看到状态栏痕迹 —— 说明 prompt 尾部禁令或历史剥离没兜住。"
                "这是真缺陷，请贴出上面那段尾部 120 字。"
            )

        # ── 阶段 D：切回 on，确认还能恢复（不是单向失效）──
        print()
        print("=== 阶段 D：切回 on，确认可恢复（非单向失效）===")
        wc.send("/quill statusbar on", timeout=60)
        resp_on = wc.send("【测试】她笑着凑近，眼神亮亮的。", timeout=240)
        rep_on = resp_on.get("reply") or ""
        tr_on = _trace(rep_on)
        print("  ", tr_on)
        checks.append(("D. 切回 on 后状态栏恢复", tr_on["字段行"]))

        # ── 配置 md5：会话覆盖全程不写面板配置 ──
        now = md5()
        print()
        print("config md5 =", now)
        print("  flipped  =", flipped_md5, "（进入时）")
        print("  说明：本探针把 enabled 改成了 true，所以运行期间不等于基线是对的；")
        print("        要断言的是「/quill statusbar on|off 不写面板配置」→ md5 恒定。")
        checks.append(("E. 会话覆盖全程未写面板配置（md5 恒定）", now == flipped_md5))

    finally:
        print()
        print("=== 还原 ===")
        try:
            r_auto = wc.send("/quill statusbar auto", timeout=60)
            print("  会话复位:", (r_auto.get("reply") or "").replace("\n", " ")[:90])
        except Exception as exc:
            print("  会话复位失败（可忽略）:", type(exc).__name__)

        r2 = c.plugin(
            "POST",
            "config/save_batch",
            json={"updates": [
                {"group": "status_bar", "key": "enabled", "value": False},
            ]},
            timeout=30.0,
        )
        print("  restore:", (r2 or {}).get("status"))
        time.sleep(2.5)
        now = md5()
        print("  md5 =", now, "| OK" if now == BASE_MD5 else "| !!! MISMATCH !!!")
        if now != BASE_MD5:
            ok = False

        # 清残留
        try:
            wc.send("/char 1", timeout=30)
            rep = wc.send("/quill reset", timeout=60)
            print("  reset:", (rep.get("reply") or "").replace("\n", " ")[:100])
        except Exception as exc:
            print("  reset 失败（可忽略）:", type(exc).__name__)

    print()
    print("=== 判定 ===")
    all_ok = True
    for label, good in checks:
        print(f"  [{'PASS' if good else 'FAIL'}] {label}")
        all_ok = all_ok and good
    if notes:
        print()
        print("  NOTE:")
        for n in notes:
            print("   -", n)
    if all_ok:
        ok = True
    print()
    print(">>> 探针 D:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
