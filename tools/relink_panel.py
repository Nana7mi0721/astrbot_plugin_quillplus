# -*- coding: utf-8 -*-
"""把 index.html 里的内联 <style> / <script> 换成外链（一次性工具）。

产出：
  * <style>…</style>            → 12 个 <link rel="stylesheet">
  * <script>…</script>（主体）  → <script type="module" src="js/app.js">
  * <head> 里的主题引导脚本保持内联（必须在 CSS 之前跑，见下）

顺序敏感：CSS 的层叠依赖原有先后顺序，link 的书写顺序必须与拆分前
`<style>` 内的段落顺序一致，否则同权重规则可能翻转。
"""

from __future__ import annotations

import io
import re
import sys

SRC = "pages/panel/index.html"

CSS_ORDER = [
    "css/tokens.css",
    "css/base.css",
    "css/components.css",
    "css/layout.css",
    "css/overlays.css",
    "css/pages/writing.css",
    "css/pages/worldbook.css",
    "css/pages/persona.css",
    "css/pages/rag.css",
    "css/pages/memory.css",
    "css/pages/config.css",
    "css/responsive.css",
]


def main() -> int:
    text = io.open(SRC, encoding="utf-8").read()

    # ── 1. 替换 <style> 块 ──
    # 用正则匹配整块（DOTALL），而不是按行号 —— 行号会随后续编辑漂移。
    style_re = re.compile(r"<style>.*?</style>", re.DOTALL)
    styles = style_re.findall(text)
    if len(styles) != 1:
        print(f"✗ 预期 1 个 <style> 块，实际 {len(styles)} 个", file=sys.stderr)
        return 1

    links = "\n".join(
        f'<link rel="stylesheet" href="{p}">' for p in CSS_ORDER
    )
    # 每行前加两个空格，保持与原缩进风格一致
    links_block = "\n".join("  " + ln for ln in links.split("\n"))
    text = style_re.sub(links_block, text, count=1)

    # ── 2. 替换 JS 主 <script> 块 ──
    # 只替换**内容为面板脚本**的那个（特征：含 quillInit）。head 里的主题
    # 引导脚本必须保持内联：它要在 CSS 生效前设置 data-theme，否则首帧闪白。
    # 注意别用 (?!...) 负向断言去区分 —— 惰性匹配 + DOTALL 下它会一路扫到
    # 文末再回溯，判据完全错位（实测表现为「找不到主脚本块」）。
    # 直接枚举所有 <script>…</script>，按内容挑选即可。
    js_re = re.compile(r"<script>(.*?)</script>", re.DOTALL)
    target = None
    for m in js_re.finditer(text):
        if "quillInit" in m.group(1):
            target = m
            break
    if target is None:
        print("✗ 未找到面板主脚本块", file=sys.stderr)
        return 1
    # 确认拆分产物存在（否则外链会指向不存在的文件，面板白屏）
    import os
    for need in ("pages/panel/js/app.js", "pages/panel/js/state.js",
                 "pages/panel/css/tokens.css"):
        if not os.path.isfile(need):
            print(f"✗ 缺少拆分产物 {need}（请先跑 split_panel.py）", file=sys.stderr)
            return 1

    text = text[: target.start()] + '<script type="module" src="js/app.js"></script>' + text[target.end():]

    io.open(SRC, "w", encoding="utf-8", newline="\n").write(text)

    # ── 3. 汇总 ──
    new_lines = text.count("\n") + 1
    print(f"✓ index.html 重写完成：{new_lines} 行")
    print(f"  外链 CSS: {len(CSS_ORDER)} 个")
    print(f"  入口模块: js/app.js")
    # 残留检查
    left = text.count("<style>")
    inline_js = len(re.findall(r"<script>(?!.*?data-theme)", text, re.DOTALL))
    print(f"  残留内联 <style>: {left}（应为 0）")
    print(f"  保留的内联 <script>: {len(re.findall(r'<script>', text))} 个（应为 1，head 主题引导）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
