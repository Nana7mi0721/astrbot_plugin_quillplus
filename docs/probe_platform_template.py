# -*- coding: utf-8 -*-
"""探针 A：平台双模板真的生效（含 L2 套模板的端到端验证）。

为什么需要它：fixture 只能证明「函数行为对」，证明不了「改了配置之后
线上真的变了」。这个探针把 webchat 临时当作纯文本平台、给纯文本模板塞一个
可识别标记，然后发一条真实消息，检查回复里：
  * 有那个标记（模板被套用了）
  * 没有 ``` 围栏、没有 ** 粗体（确实是纯文本模板而非默认 Markdown 模板）

这条同时验证 L2 修复 —— L2（[LOVE_DATA] 单行）是实际最常命中的解析路径
（实测 40 次解析里 36 次走 L2），而它此前不套模板。修复前本探针会失败。

安全：改配置前断言基线 md5；`finally` 无条件还原并校验回到基线。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_platform_template.py
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
MARK = "[[PLAINTPL]]"
PLAIN_DEFAULT = "───── 状态栏 ─────\n{content}\n────────────────"


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def main() -> int:
    if md5() != BASE_MD5:
        print("!! 配置基线不符，中止（先查是不是有别的进程改了配置）")
        print("   now     =", md5())
        print("   expected =", BASE_MD5)
        return 2

    c = auth.DashboardClient()
    ok = False
    try:
        r = c.plugin(
            "POST",
            "config/save_batch",
            json={
                "updates": [
                    {"group": "status_bar", "key": "enabled", "value": True},
                    {"group": "status_bar", "key": "plain_platforms", "value": "webchat"},
                    {"group": "status_bar", "key": "format_template_plain",
                     "value": MARK + "\n{content}"},
                ]
            },
            timeout=30.0,
        )
        print("save:", (r or {}).get("status"), (r or {}).get("message"))
        if (r or {}).get("status") != "ok":
            print("!! 保存失败，中止")
            return 2
        time.sleep(1.5)

        wc = chat.WebChat()
        wc.send("/char 1", timeout=120)
        resp = wc.send("【测试】她看着你，心跳加快了。", timeout=240)
        out = resp.get("reply") or ""

        has_mark = MARK in out
        has_fence = "```" in out
        has_bold = "**" in out

        print()
        print("=== 判定 ===")
        print(f"含纯文本标记 {MARK} :", has_mark, "(应 True)")
        print("含 Markdown 围栏 ```   :", has_fence, "(应 False)")
        print("含 ** 粗体标记         :", has_bold, "(应 False)")
        print()
        print("回复尾部 300 字：")
        print(out[-300:])

        ok = has_mark and not has_fence and not has_bold
        print()
        print(">>> 探针 A:", "PASS" if ok else "FAIL")
    finally:
        r2 = c.plugin(
            "POST",
            "config/save_batch",
            json={
                "updates": [
                    {"group": "status_bar", "key": "enabled", "value": False},
                    {"group": "status_bar", "key": "plain_platforms", "value": ""},
                    {"group": "status_bar", "key": "format_template_plain",
                     "value": PLAIN_DEFAULT},
                ]
            },
            timeout=30.0,
        )
        print()
        print("restore:", (r2 or {}).get("status"))
        time.sleep(2.5)
        now = md5()
        print("md5 =", now, "| OK" if now == BASE_MD5 else "| !!! MISMATCH !!!")
        if now != BASE_MD5:
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
