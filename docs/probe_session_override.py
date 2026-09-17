# -*- coding: utf-8 -*-
"""探针 B：会话级开关 /quill statusbar on|off|auto 真的生效。

核心断言：**面板全局关着**（生产基线）时，会话级 `on` 仍能让状态栏出现；
`auto` 复位后消失。这证明了「会话覆盖 > 面板全局」的优先级确实接到了
运行路径上，而不只是 fixture 里的函数逻辑。

安全：本探针**不改任何面板配置**（会话覆盖存在 state 文件里），
所以 md5 全程不变；结束时把该会话复位成 auto，不留残留。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_session_override.py
"""

from __future__ import annotations

import hashlib
import io
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import auth  # noqa: E402
import chat  # noqa: E402

CFG = r"D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json"
BASE_MD5 = "403fee103fe2c8c82d7166a98c538854"


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def main() -> int:
    if md5() != BASE_MD5:
        print("!! 配置基线不符，中止")
        return 2

    wc = chat.WebChat()
    checks: list[tuple[str, bool]] = []

    print("UMO:", wc.umo[-40:])
    wc.send("/char 1", timeout=120)

    # 1) auto：面板关 → 生效值应为关闭
    print()
    print("--- 1. 面板关 + 会话 auto ---")
    r1 = wc.send("/quill statusbar", timeout=60)
    t1 = r1.get("reply") or ""
    print(t1[:220])
    checks.append(("auto 报告生效值=关闭", "本轮实际生效: 关闭" in t1))

    # 2) 切到 on
    print()
    print("--- 2. /quill statusbar on ---")
    r2 = wc.send("/quill statusbar on", timeout=60)
    t2 = r2.get("reply") or ""
    print(t2[:120])
    checks.append(("on 写入成功", "强制开启" in t2))

    # 3) 面板仍关着，但本会话应出现状态栏
    print()
    print("--- 3. 面板关 + 会话 on 发消息（应出现状态栏）---")
    r3 = wc.send("【测试】她笑了，气氛很好。", timeout=240)
    out3 = r3.get("reply") or ""
    has_fence = "```" in out3
    has_fields = ("好感度" in out3) or ("关系阶段" in out3)
    print("含 Markdown 围栏 ``` :", has_fence, "(webchat 是 Markdown 平台，应 True)")
    print("含状态栏字段        :", has_fields, "(应 True)")
    print("尾部 200 字:", repr(out3[-200:]))
    checks.append(("会话 on 覆盖了面板 off", has_fields))

    # 4) 复位 auto
    print()
    print("--- 4. /quill statusbar auto 复位 ---")
    r4 = wc.send("/quill statusbar auto", timeout=60)
    print((r4.get("reply") or "")[:120])
    r5 = wc.send("/quill statusbar", timeout=60)
    t5 = r5.get("reply") or ""
    print(t5[:220])
    checks.append(("复位后生效值=关闭", "本轮实际生效: 关闭" in t5))

    # 5) 配置未被触碰
    now = md5()
    print()
    print("config md5 =", now, "| OK" if now == BASE_MD5 else "| !!! MISMATCH !!!")
    print("（配置不变是预期的：会话覆盖存在 state 文件，不写面板配置）")
    checks.append(("配置 md5 未变", now == BASE_MD5))

    print()
    print("=== 判定 ===")
    all_ok = True
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok
    print()
    print(">>> 探针 B:", "PASS" if all_ok else "FAIL")

    # 该会话已复位 auto（第 4 步），无需额外清理
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
