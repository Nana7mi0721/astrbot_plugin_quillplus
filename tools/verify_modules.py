# -*- coding: utf-8 -*-
"""模块化后的一致性校验（一次性工具）。

三件事：
  1. **未解析标识符**：某模块引用了既非本地声明、也非 import、也非浏览器内置的
     名字 —— 典型症状是「函数名打错」或「漏写 import」，浏览器里表现为
     `ReferenceError: xxx is not defined`（且只在触发该代码路径时崩）。
  2. **导入/导出对账**：import 的名字必须真在目标模块 export 列表里。
  3. **循环依赖**：列出所有环。函数声明有提升，环通常无害，但必须知情。
"""

from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from split_panel import strip_comments_strings  # noqa: E402

OUTDIR = "pages/panel/js"

BUILTIN = set("""
window document console JSON Math Date Array Object String Number Boolean Promise Map Set WeakMap WeakSet
setTimeout clearTimeout setInterval clearInterval requestAnimationFrame cancelAnimationFrame
fetch Headers Request Response URL URLSearchParams Blob File FileReader FormData Image
localStorage sessionStorage navigator location history Error TypeError RangeError SyntaxError
isNaN parseInt parseFloat encodeURIComponent decodeURIComponent btoa atob unescape escape structuredClone
IntersectionObserver ResizeObserver MutationObserver Event CustomEvent KeyboardEvent MouseEvent
Node NodeList Element HTMLElement HTMLInputElement HTMLCanvasElement CSS
getComputedStyle queueMicrotask crypto Intl RegExp Symbol Proxy Reflect BigInt Infinity NaN
undefined arguments this true false null new typeof instanceof in of delete void
function return if else for while do switch case break continue try catch finally throw class
const let var export import default from as static extends super get set
Int8Array Uint8Array Uint8ClampedArray Int16Array Uint16Array Int32Array Uint32Array
Float32Array Float64Array ArrayBuffer DataView TextEncoder TextDecoder
AbortController AbortSignal DOMException screen performance matchMedia
alert confirm prompt globalThis eval debugger async await
""".split())


def list_modules() -> list[str]:
    mods = []
    for root, _dirs, files in os.walk(OUTDIR):
        for f in sorted(files):
            if f.endswith(".js"):
                rel = os.path.relpath(os.path.join(root, f), OUTDIR)
                mods.append(rel.replace(os.sep, "/"))
    return sorted(mods)


def main() -> int:
    mods = list_modules()
    info: dict[str, dict] = {}
    for rel in mods:
        text = io.open(os.path.join(OUTDIR, rel), encoding="utf-8").read()
        clean = strip_comments_strings(text)
        imports: set[str] = set()
        import_map: dict[str, str] = {}
        # 注意：import 必须从**原始文本**解析。clean 版把字符串内容抹成空白，
        # 模块路径 "utils.js" 也跟着没了，正则匹配不上 —— 这正是前一版
        # 把所有合法导入误报成「未解析标识符」的原因。
        for m in re.finditer(r'^import \{ ([^}]+) \} from "(.+)";$', text, re.MULTILINE):
            names = [x.strip() for x in m.group(1).split(",")]
            imports |= set(names)
            for n in names:
                import_map[n] = m.group(2)
        exports: set[str] = set()
        m = re.search(r"^export \{ ([^}]+) \};$", text, re.MULTILINE)
        if m:
            exports |= {x.strip() for x in m.group(1).split(",")}
        m2 = re.search(r"^export const (\w+)", text, re.MULTILINE)
        if m2:
            exports.add(m2.group(1))
        local = set(re.findall(
            r"^\s*(?:const|let|var|function|async function|class)\s+([A-Za-z_$][\w$]*)",
            clean, re.MULTILINE))
        info[rel] = dict(
            clean=clean, imports=imports, import_map=import_map,
            exports=exports, local=local, text=text,
        )

    rc = 0

    # ── 1. 未解析标识符 ──
    # 只检查「明确的自由变量引用」，不猜函数参数/局部变量名：
    # 参数名在源码里没有独立标记，静态判断极易误报（`e`/`i`/`s` 这类短名
    # 几乎都是参数）。因此这里改用**精确排除法**——把每行里出现过的
    # 参数与局部声明都收集进 known，剩下的可疑项才值得人看。
    print("=== 1. 未解析标识符（疑似漏 import / 拼错）===")

    def collect_locals(clean: str) -> set[str]:
        names: set[str] = set()
        # function 名与参数
        for m in re.finditer(r'function\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)', clean):
            if m.group(1):
                names.add(m.group(1))
            for p in m.group(2).split(","):
                p = p.split("=")[0].strip().lstrip(".")
                if re.fullmatch(r"[A-Za-z_$][\w$]*", p or ""):
                    names.add(p)
        # 箭头函数参数：(a, b) => / a => / ({x}) =>
        for m in re.finditer(r'\(([^()]*)\)\s*=>', clean):
            for p in m.group(1).split(","):
                p = p.split("=")[0].strip().lstrip(".")
                if re.fullmatch(r"[A-Za-z_$][\w$]*", p or ""):
                    names.add(p)
        for m in re.finditer(r'(?<![\w.$])([A-Za-z_$][\w$]*)\s*=>', clean):
            names.add(m.group(1))
        # catch (e)
        for m in re.finditer(r'catch\s*\(\s*([A-Za-z_$][\w$]*)', clean):
            names.add(m.group(1))
        # 任意缩进的 const/let/var/class/function 声明
        for m in re.finditer(
            r'(?<![\w.$])(?:const|let|var|class|function)\s+([A-Za-z_$][\w$]*)', clean
        ):
            names.add(m.group(1))
        # 同一语句里的后续声明符：`const a = 1, b = 2, c;`
        # 只匹配「声明关键字所在行的后续逗号项」，避免把实参、对象字面量误当声明。
        for m in re.finditer(
            r'(?<![\w.$])(?:const|let|var)\s+[A-Za-z_$][\w$]*[^;\n]*?(?=\n|;|$)', clean
        ):
            tail = m.group(0)
            # 去掉首项后，逐个取 , 后面的简单标识符
            parts = tail.split("=", 1)
            rest = parts[1] if len(parts) > 1 else ""
            for mm in re.finditer(r',\s*([A-Za-z_$][\w$]*)\s*(?=[=,;\n]|$)', rest):
                names.add(mm.group(1))
        return names

    suspects_total = 0
    for rel in mods:
        d = info[rel]
        known = d["imports"] | d["exports"] | BUILTIN | collect_locals(d["clean"]) | d["local"]
        refs = set(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)(?![\w$])", d["clean"]))
        unknown = []
        for r in refs:
            if r in known:
                continue
            # 对象字面量 key：  foo:  （排除三元 ? : 需看后面是否紧跟值）
            if re.search(r"(?<![\w.$])" + re.escape(r) + r"\s*:(?!:)", d["clean"]):
                # 但要排除 label/三元误判：若同时以 foo( 形式出现则仍算引用
                if not re.search(r"(?<![\w.$])" + re.escape(r) + r"\s*\(", d["clean"]):
                    continue
            # 纯属性名 .foo
            if re.search(r"\.\s*" + re.escape(r) + r"\b", d["clean"]):
                continue
            if (re.search(r"(?<![\w.$])" + re.escape(r) + r"\s*\(", d["clean"])
                    or re.search(r"(?<![\w.$])" + re.escape(r) + r"\s*=(?!=)", d["clean"])):
                unknown.append(r)
        if unknown:
            suspects_total += len(unknown)
            print(f"  ⚠ {rel}: {sorted(unknown)}")
    if not suspects_total:
        print("  ✓ 没有未解析标识符")
    else:
        rc = 1

    # ── 2. 导入/导出对账 ──
    print()
    print("=== 2. 导入 / 导出对账 ===")
    bad = 0
    for rel in mods:
        d = info[rel]
        for name, spec in sorted(d["import_map"].items()):
            target = os.path.normpath(
                os.path.join(os.path.dirname(rel), spec)
            ).replace(os.sep, "/")
            if target not in info:
                print(f"  ✗ {rel}: 模块不存在 {target}")
                bad += 1
                continue
            if name not in info[target]["exports"]:
                print(f"  ✗ {rel} ← {target}: {name} 未导出")
                bad += 1
    print(f"  {'✓ 全部对账通过' if bad == 0 else f'✗ {bad} 处不匹配'}")
    if bad:
        rc = 1

    # ── 3. 循环依赖 ──
    print()
    print("=== 3. 循环依赖 ===")
    graph: dict[str, set[str]] = {}
    for rel in mods:
        deps = set()
        for spec in info[rel]["import_map"].values():
            t = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
            if t in info:
                deps.add(t)
        graph[rel] = deps

    cycles: list[list[str]] = []
    seen_sig: set[tuple] = set()

    def dfs(node: str, path: list[str], onpath: set[str]) -> None:
        for nxt in sorted(graph.get(node, ())):
            if nxt in onpath:
                i = path.index(nxt)
                cyc = path[i:] + [nxt]
                sig = tuple(sorted(set(cyc)))
                if sig not in seen_sig:
                    seen_sig.add(sig)
                    cycles.append(cyc)
                continue
            dfs(nxt, path + [nxt], onpath | {nxt})

    for m in mods:
        dfs(m, [m], {m})

    if cycles:
        print(f"  发现 {len(cycles)} 个环（函数声明有提升，回调期调用则无害）：")
        for c in cycles:
            print("    " + " → ".join(c))
    else:
        print("  ✓ 无环")

    return rc


if __name__ == "__main__":
    sys.exit(main())
