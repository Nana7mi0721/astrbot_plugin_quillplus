# -*- coding: utf-8 -*-
"""探针 Z（专项）：修复后，裸 [LOVE_DATA] 不再送达用户。

对应 VERIFICATION_PLAN.md 的「发现 1」。修复前实测 DB 泄漏 3/3 轮、SSE 3/3 轮；
修复点是 `main.py` 里 `on_llm_tool_respond` 不再把总闸门 `_quill_activated`
置 False（改用专用标记 `_quill_memorized`），使后续工具调用仍被处理。

两个通道都看：
  * DB（platform_message_history）= 真正送达用户的内容 → 必须干净；
  * SSE 的 plain 帧 = 流式推送帧 → 也应干净。

安全：`finally` 还原面板开关 + 校验 md5；结束清测试残留。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_no_leak.py
"""

from __future__ import annotations

import hashlib
import io
import json
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
ROUNDS = 6

# 「泄漏」的判据必须严格：**裸状态栏行真的发给用户**。
# 不能只搜 `[LOVE_DATA]` 子串——模型会在正文里**提到**这个标记名
# （实测抓到一条 254 字的英文说明：「I noticed I split the reply into two
# messages ... using the `[LOVE_DATA]` and `>>> <<<` markers」），
# 那是模型的自我修正说明，不是泄漏的状态栏。只按子串判会假报。
#
# 真正的泄漏长这样：以 [LOVE_DATA] 开头，且值是 `a | b | c` 竖线分隔的字段串。
_LEAK_RE = re.compile(
    r"^\s*\[LOVE_DATA\]\s*\S.*\|",   # 行首标记 + 至少一个竖线分隔值
    re.MULTILINE,
)
# 兜底：`[LOVE_DATA]` 后紧跟数字/斜杠这类典型取值开头（行首、无竖线也算）
_LEAK_HEAD_RE = re.compile(r"^\s*\[LOVE_DATA\]\s*[\d０-９]", re.MULTILINE)


def _is_leak(text: str) -> bool:
    """文本里是否有**真正的**裸状态栏行。"""
    return bool(_LEAK_RE.search(text) or _LEAK_HEAD_RE.search(text))


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def max_id(session_id: str) -> int:
    try:
        conn = paths.connect_ro(paths.DATA_V4_DB)
        r = conn.execute(
            "SELECT MAX(id) m FROM platform_message_history "
            "WHERE platform_id='webchat' AND user_id=?",
            (session_id,),
        ).fetchone()
        conn.close()
        return int(r["m"] or 0)
    except Exception:
        return 0


def rows_since(session_id: str, after: int):
    conn = paths.connect_ro(paths.DATA_V4_DB)
    rows = conn.execute(
        """
        SELECT id, sender_id,
               (SELECT group_concat(json_extract(p.value,'$.text'),'')
                FROM json_each(json_extract(h.content,'$.message')) AS p
                WHERE json_extract(p.value,'$.type')='plain') AS text
        FROM platform_message_history h
        WHERE platform_id='webchat' AND user_id=? AND id > ?
        ORDER BY id ASC
        """,
        (session_id, after),
    ).fetchall()
    conn.close()
    return rows


def main() -> int:
    if md5() != BASE_MD5:
        print("!! 配置基线不符，中止")
        print("   now      =", md5())
        print("   expected =", BASE_MD5)
        return 2

    c = auth.DashboardClient()
    wc = chat.WebChat()
    tail = logs_mod.LogTail()
    db_leaks = 0
    sse_leaks = 0
    multi_call_rounds = 0

    print("UMO:", wc.umo[-44:])

    try:
        r = c.plugin(
            "POST", "config/save_batch",
            json={"updates": [
                {"group": "status_bar", "key": "enabled", "value": True},
            ]},
            timeout=30.0,
        )
        print("save:", (r or {}).get("status"), (r or {}).get("message"))
        if (r or {}).get("status") != "ok":
            return 2
        time.sleep(1.5)
        wc.send("/char 1", timeout=120)

        for i in range(1, ROUNDS + 1):
            wc.send("/quill reset", timeout=60)
            time.sleep(0.4)
            mark = tail.mark()
            before = max_id(wc.session_id)
            resp = wc.send(f"【测试】第{i}次：她看着你，等你开口。", timeout=240)
            time.sleep(1.0)
            blk = tail.since_plugin(mark)

            rows = rows_since(wc.session_id, before)
            bot_rows = [r for r in rows if r["sender_id"] == "bot"]
            db_text = "\n".join(str(r["text"] or "") for r in bot_rows)
            sse_text = resp.get("reply") or ""

            d_leak = _is_leak(db_text)
            s_leak = _is_leak(sse_text)
            db_leaks += d_leak
            sse_leaks += s_leak

            # 统计模型是否把回复拆成多次工具调用（旧实现下就必然泄漏）
            calls = sum(
                1 for ev in (resp.get("events") or [])
                if ev.get("type") == "plain" and isinstance(ev.get("data"), str)
                and '"name": "send_message_to_user"' in ev["data"]
            )
            if calls > 1:
                multi_call_rounds += 1

            handled = blk.count("状态栏已处理")
            print()
            print(f"--- 第 {i} 轮 ---")
            print(f"  bot 落库 {len(bot_rows)} 行 | 工具调用 {calls} 次 | "
                  f"状态栏已处理 {handled} 次")
            print(f"  DB 裸标记={d_leak} | SSE 裸标记={s_leak}  (都应 False)")
            if d_leak:
                for r2 in bot_rows:
                    t = str(r2["text"] or "")
                    if _is_leak(t):
                        print(f"    ⚠ DB 泄漏行 id={r2['id']}: {t[:150]!r}")

        print()
        print("=== 判定 ===")
        print(f"  多工具调用轮次: {multi_call_rounds}/{ROUNDS}"
              f"（说明这个触发条件确实常见）")
        ok_db = db_leaks == 0
        ok_sse = sse_leaks == 0
        print(f"  [{'PASS' if ok_db else 'FAIL'}] DB 送达文本无裸 [LOVE_DATA] "
              f"（泄漏 {db_leaks}/{ROUNDS}）")
        print(f"  [{'PASS' if ok_sse else 'FAIL'}] SSE 帧无裸 [LOVE_DATA] "
              f"（泄漏 {sse_leaks}/{ROUNDS}）")
        ok = ok_db and ok_sse

    finally:
        r2 = c.plugin(
            "POST", "config/save_batch",
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

        print()
        print("=== 清理测试残留 ===")
        try:
            wc.send("/char 1", timeout=30)
            rep = wc.send("/quill reset", timeout=60)
            print("  reset:", (rep.get("reply") or "").replace("\n", " ")[:90])
        except Exception as exc:
            print("  reset 失败（可忽略）:", type(exc).__name__)
        try:
            db = paths.db_path_memory()
            con = paths.connect_ro(db)
            sids = [str(r["session_id"]) for r in con.execute(
                "SELECT DISTINCT session_id FROM memories WHERE session_id LIKE ?",
                (wc.umo + "%",))]
            con.close()
            for sid in sids:
                resp = c.plugin("POST", "memory/delete",
                                json={"session_id": sid}, timeout=20.0)
                d = (resp or {}).get("data") or {}
                if d.get("deleted"):
                    print("  已删记忆", d["deleted"], "条")
        except Exception as exc:
            print("  记忆清理失败（可忽略）:", type(exc).__name__)

    print()
    print(">>> 探针 Z:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
