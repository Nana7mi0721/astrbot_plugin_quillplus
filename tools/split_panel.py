# -*- coding: utf-8 -*-
"""把 pages/panel/index.html 的 JS 主体拆成 ES 模块（一次性工具，跑完即弃）。

为什么可以安全自动化
--------------------
* 原脚本已声明 `"use strict"`，与 ES module 的严格模式语义一致 ——
  不会因为改 module 而冒出「未声明变量赋值」之类的新错误。
* 已扫描确认：`with` / `arguments.callee` / `delete 变量` / 八进制字面量
  全部不存在。
* 已扫描确认：31 个被反复赋值的顶层名**没有任何局部遮蔽**，因此整词替换
  `_dirty` → `S._dirty` 是安全的。

两处必须改写（否则模块化后行为会变）
------------------------------------
1. **可变状态外置**：ESM 的 import 绑定只读，`_dirty = true`（原为同文件内
   自由变量赋值）在模块里会直接抛 TypeError。统一搬进 `state.js` 的 `S`。
2. **顶层 const 的函数也要 export**，供其它模块 import。

其余一律逐行拷贝，不做重排、不改缩进 —— 以便用「原行段 vs 新文件正文」
做逐行等价校验（脚本自带该校验）。
"""

from __future__ import annotations

import io
import os
import re
import sys

SRC = "pages/panel/index.html"
OUTDIR = "pages/panel/js"

# ── 模块划分（行号取原文件 1-based，左闭右开） ──
MODULES = [
    ("utils.js",                  3578, 3708, "工具函数"),
    ("api.js",                    3708, 3820, "API 通信层"),
    ("components/feedback.js",    3820, 4013, "提示横幅与对话框"),
    ("components/modal.js",       4013, 4123, "模态框 / 溢出菜单 / 空状态"),
    ("components/taginput.js",    4123, 4175, "标签输入"),
    ("pages/writing.js",          4175, 4606, "写作素材库"),
    ("pages/worldbook.js",        4606, 4947, "世界书"),
    ("pages/persona.js",          4947, 5429, "角色卡"),
    ("components/cropper.js",     5429, 5609, "头像裁剪"),
    ("pages/rag.js",              5609, 5727, "文档知识库"),
    ("pages/memory.js",           5727, 6008, "动态记忆"),
    ("components/backup.js",      6008, 6051, "全量备份"),
    ("pages/config.js",           6051, 6468, "配置页"),
    ("components/listkeys.js",    6468, 6569, "列表方向键导航"),
    ("components/select.js",      6975, 7431, "自绘下拉"),
    ("components/contextmenu.js", 7431, 7661, "右键上下文菜单"),
    # app.js 由两段拼成：动作分发表+全局事件绑定（6569-6974）、初始化（7661-末尾）
    #
    # 第二段的结束边界必须是 **7723**（`</script>` 所在行，左闭右开即取到 7722）。
    # 早先写成 7722，把倒数第二行 `else quillInit();` 漏掉了 —— 那是「DOM 已就绪
    # 时直接初始化」的分支，模块（defer 语义）下 DOM 必然已就绪，走的正是这一支。
    # 漏掉它的后果是面板完全不初始化：页面能画出来，但所有交互失效、动态列表全空，
    # 且**控制台一个错都不报**（函数定义了没人调用）。这类缺陷只有真实浏览器
    # 端到端才看得出来。
    ("app.js",                    6569, 6975, "动作分发表 + 全局事件绑定"),
    ("app.js",                    7661, 7723, "初始化入口"),
]

# 被反复赋值的顶层名 → 搬进 state.js（含初值，从原声明行提取）
STATE_RE = re.compile(r'^(let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?);\s*(?://.*)?$')


def _restore_template_interpolations(original: str, stripped: str) -> str:
    """把模板字面量 `${...}` 里的真实代码从原文挖回抹白后的文本。

    两份文本等长（抹白是一对一替换），因此下标可直接互通：
    在 original 上定位 `${`，配平花括号，把区间内容原样写回 stripped。
    """
    out: list[str] = []
    i, n = 0, len(stripped)
    while i < n:
        if original[i] == "$" and i + 1 < n and original[i + 1] == "{":
            depth, j = 0, i + 1
            while j < n:
                ch = original[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == "\n":
                    break            # 未闭合时不要吞掉后续行
                j += 1
            if j < n and original[j] == "}":
                out.append("${")
                out.append(original[i + 2:j])
                out.append("}")
                i = j + 1
                continue
        out.append(stripped[i])
        i += 1
    return "".join(out)


def strip_comments_strings(src: str) -> str:
    """注释与字符串内容抹成等长空白（保留换行），避免误匹配标识符。

    **但模板字面量里的 `${...}` 插值必须保留**：那是真实代码，里面调用的
    函数（如 `${esc(x)}`）是必须被识别出来的跨模块依赖。早先把整个模板串
    一并抹白，导致 feedback.js 里 5 处 `${esc(...)}` 没被算进依赖，
    生成的模块漏了 `import { esc }`，浏览器运行时报
    `ReferenceError: esc is not defined`。
    """
    original = src
    out, i, n, st = [], 0, len(src), None
    while i < n:
        c, two = src[i], src[i:i + 2]
        if st is None:
            if two == "//":
                st = "//"; out.append("  "); i += 2; continue
            if two == "/*":
                st = "/*"; out.append("  "); i += 2; continue
            if c in "'\"`":
                st = c; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if st == "//":
            if c == "\n":
                st = None; out.append("\n")
            else:
                out.append(" ")
            i += 1; continue
        if st == "/*":
            if two == "*/":
                st = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if c == "\\":
            out.append("  "); i += 2; continue
        if c == st:
            st = None; out.append(" "); i += 1; continue
        out.append("\n" if c == "\n" else " "); i += 1
    src = "".join(out)
    # 正则字面量：上面的状态机不识别它，字面量里的 `sub=` 会残留成看似标识符的
    # 片段（导致误报「未解析标识符 sub」）。这里补一遍替换：只在前一个非空白
    # 字符明显处于「值位置」时才当正则，避免把除法 `a / b` 误伤。
    src = re.sub(
        r'([\(,=:\[!&|?{};]\s*|^)(/(?![*/])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n])+/[gimsuy]*)',
        lambda m: m.group(1) + " " * len(m.group(2)),
        src,
        flags=re.MULTILINE,
    )

    # 模板字面量插值 `${...}` 还原。
    #
    # 上面的状态机把整个反引号串（含插值）都抹白了。但插值里是**真实代码**：
    # `${esc(msg)}` 这种调用必须被依赖扫描看见，否则不会生成 import，运行期
    # 报 ReferenceError。
    #
    # 注意必须**同时**用 original 与 src 两份文本判定：
    #   - 「哪里是 ${」只能从 original 找（src 里那三字符已被抹成空白）；
    #   - 「哪里是真代码位置」仍由 src 的长度对齐保证。
    # 早期版本只在 src 上找 "${"，因为字符已被抹白，永远找不到，插值从未还原。
    src = _restore_template_interpolations(original, src)
    return src


def main() -> int:
    raw = io.open(SRC, encoding="utf-8").read()
    lines = raw.split("\n")
    assert lines[3556].strip() == "<script>", repr(lines[3556])
    assert lines[7722].strip() == "</script>", repr(lines[7722])

    # ── 1. 逐模块收集原始行段 ──
    order: list[str] = []
    segs: dict[str, list[tuple[int, int]]] = {}
    for rel, a, b, _t in MODULES:
        if rel not in segs:
            segs[rel] = []; order.append(rel)
        segs[rel].append((a, b))

    # ── 2. 顶层符号表 + 可变状态识别 ──
    # 标识符必须用 [A-Za-z_$][\w$]* 而不是 \w+ —— `$` 与 `$$`（DOM 查询别名）
    # 不在 \w 里，用 \w+ 会把它们整个漏掉，导致这两个高频工具函数没有 export。
    decl_re = re.compile(
        r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=|^class\s+([A-Za-z_$][\w$]*)'
    )
    owner: dict[str, str] = {}
    decl_line: dict[str, int] = {}
    for rel, a, b, _t in MODULES:
        for ln in range(a, b):
            l = lines[ln - 1]
            if not l or l[0] in " \t/*":
                continue
            m = decl_re.match(l)
            if m:
                name = m.group(1) or m.group(2) or m.group(3)
                owner.setdefault(name, rel)
                decl_line.setdefault(name, ln)

    # 被裸赋值 → 必须进 state.js
    state_names: dict[str, str] = {}     # name → 初值表达式
    for name, ln in sorted(decl_line.items(), key=lambda x: x[1]):
        src_line = lines[ln - 1].strip()
        if src_line.startswith("function") or src_line.startswith("async function"):
            continue
        m = STATE_RE.match(src_line)
        if not m or m.group(1) not in ("let", "var"):
            continue
        reassigned = False
        for j, l in enumerate(lines):
            if j == ln - 1:
                continue
            s = strip_comments_strings(l)
            if re.search(r'(?<![\w.$])' + re.escape(name) + r'\s*(?:=(?!=)|\+=|-=|\*=)', s):
                reassigned = True
                break
        if reassigned:
            state_names[name] = m.group(3).strip()

    # window.X = function(...) 形式的定义：既挂全局（保持原行为），也要能被
    # 别的模块 import。必须在算依赖**之前**登记，否则先写出的模块看不到它们。
    # 原脚本里这类函数（sandboxConfirm / sandboxPrompt）在调用点是裸写名字的，
    # 同作用域时没问题，拆模块后必须靠 import 才能解析。
    all_clean = strip_comments_strings(
        "\n".join("\n".join(lines[a - 1:b - 1]) for _rel, a, b, _t in MODULES)
    )
    # 注意用 ^[ \t]* 而不是 ^：这些赋值在源码里是有缩进的（之前用 ^ 直接匹配
    # 行首，结果一个都没找到，window_fns 恒为空）。
    window_fn_re = re.compile(
        r'^[ \t]*window\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function',
        re.MULTILINE,
    )
    window_fns = set(window_fn_re.findall(all_clean))
    window_fn_owner: dict[str, str] = {}
    for rel, a, b, _t in MODULES:
        seg = strip_comments_strings("\n".join(lines[a - 1:b - 1]))
        for n in window_fn_re.findall(seg):
            window_fn_owner.setdefault(n, rel)
    for n, rel in window_fn_owner.items():
        owner.setdefault(n, rel)
    print(f"window.X = function 形式（挂全局 + 供 import）: {sorted(window_fns)}")

    print(f"可变状态（进 state.js）: {len(state_names)} 个")

    # ── 3. 生成 state.js ──
    outdir = OUTDIR
    os.makedirs(os.path.join(outdir, "components"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "pages"), exist_ok=True)

    st_lines = [
        "/* state.js — 全局可变状态（由 index.html 拆分生成）",
        " *",
        " * 为什么需要这个文件：ES module 的 import 绑定是**只读**的，",
        " * 而原脚本里这些顶层变量会被反复赋值（`_dirty = true` 之类）。",
        " * 放进 S 对象后，各模块 `S._dirty = true` 即可跨模块共享同一份状态。",
        " */",
        "",
        "export const S = {",
    ]
    for n, init in sorted(state_names.items()):
        st_lines.append(f"  {n}: {init},")
    st_lines.append("};")
    st_lines.append("")
    io.open(os.path.join(outdir, "state.js"), "w", encoding="utf-8", newline="\n").write(
        "\n".join(st_lines)
    )
    print(f"  写出 {outdir}/state.js")

    # ── 4. 逐模块改写正文 ──
    ident_alt = sorted(state_names, key=len, reverse=True)
    state_re = re.compile(
        r'(?<![\w.$])(' + "|".join(re.escape(n) for n in ident_alt) + r')(?![\w$])'
    ) if ident_alt else None
    # 状态量的原声明行号（这些行要被删除，因为初值已搬进 state.js）
    state_decl_lines = {decl_line[n] for n in state_names if n in decl_line}

    # `window.X = function (args) {` … `};` → `function X(args) {` … `}` + `window.X = X;`
    #
    # 为什么必须改写：这类函数在原脚本里既挂全局、又在**同一作用域**里被裸名调用。
    # 拆成模块后，`export { sandboxConfirm }` 要求它是个真实绑定 —— 而
    # `window.sandboxConfirm = function...` 只是给 window 加了属性，本模块内
    # 并没有同名绑定，Node 会直接报 "Export 'sandboxConfirm' is not defined"。
    # 改写成函数声明后既满足 export，又靠末尾一行保住全局可见性。
    #
    # 注意：不能用 decl_line 找行号 —— `window.X =` 不是 const/let/var/function
    # 声明，压根不在 decl_line 里（早先这么写导致 win_convert 恒为空，
    # export 照发、绑定却没有，模块语法直接不通过）。这里直接扫原始行。
    # 键是**原文件行号**、值是函数名：rewrite() 逐行遍历时手上只有行号，
    # 用行号反查才有意义。（早先建成 name → 行号，再用 `get(src_ln)` 查，
    # 永远命中不了，conv 恒为 None，函数没被改写但 export 照发 ——
    # 模块语法直接报 "Export 'sandboxConfirm' is not defined"。）
    win_convert: dict[int, str] = {}
    for n in window_fn_owner:
        for ln in range(1, len(lines) + 1):
            if re.search(
                r'^[ \t]*window\.' + re.escape(n) + r'\s*=\s*(?:async\s+)?function',
                lines[ln - 1],
            ):
                win_convert[ln] = n
                break

    def rewrite(body_lines: list[str], src_start: int) -> list[str]:
        """把可变状态引用改写为 S.xxx。

        `src_start` 为该段在原文件中的起始行号（1-based），用于识别
        「本行是不是某个状态量的声明行」—— 声明行必须**整行删掉**：
        其初值已搬进 state.js，留着会变成非法的 `let S._dirty = false;`。
        """
        out = []
        win_tail: list[str] = []
        for off, l in enumerate(body_lines):
            src_ln = src_start + off
            # 状态量的原声明行 → 删除（初值已搬进 state.js）
            if src_ln in state_decl_lines:
                out.append("")
                continue
            # window.X = function (a) {  →  function X(a) {
            conv = win_convert.get(src_ln)
            if conv is not None:
                newline = re.sub(
                    r'^(\s*)window\.' + re.escape(conv) + r'\s*=\s*(?:async\s+)?'
                    r'function\s*\(([^)]*)\)\s*\{',
                    lambda m: f"{m.group(1)}function {conv}({m.group(2)}) {{",
                    l,
                )
                if newline != l:
                    out.append(newline)
                    win_tail.append(f"window.{conv} = {conv};")
                    continue
            if state_re is None:
                out.append(l)
                continue
            code = strip_comments_strings(l)
            if state_re.search(code) is None:
                out.append(l)
                continue
            res, pos = [], 0
            for m in state_re.finditer(code):
                res.append(l[pos:m.start(1)])
                res.append("S." + m.group(1))
                pos = m.end(1)
            res.append(l[pos:])
            out.append("".join(res))
        if win_tail:
            out.append("")
            out.extend(win_tail)
        return out

    written: dict[str, int] = {}
    for rel in order:
        body: list[str] = []
        seg_start: int | None = None
        starts: list[tuple[int, int]] = []
        for a, b in segs[rel]:
            starts.append((len(body), a))
            body.extend(lines[a - 1:b - 1])
        while body and not body[0].strip():
            body.pop(0)
            starts = [(p - 1, a) for p, a in starts]
        while body and not body[-1].strip():
            body.pop()

        # 按段分别改写（每段有自己的起始行号）
        pre_body = list(body)          # 改写前快照（用于判定 S 的引用）
        rewritten: list[str] = []
        for idx, (off, a) in enumerate(starts):
            end = starts[idx + 1][0] if idx + 1 < len(starts) else len(body)
            rewritten.extend(rewrite(body[off:end], a))
        body = rewritten
        clean = strip_comments_strings("\n".join(body))
        # **改写前**的干净文本：用于判定本模块是否引用了可变状态。
        # 必须用改写前的，因为改写已把 `_dirty` 变成 `S._dirty`，原始名再也
        # 匹配不上 state_names，uses_state 恒为 False → 不生成 `import { S }`
        # → 运行期 "S is not defined"（实测：配置页整个打不开）。
        clean_pre = strip_comments_strings("\n".join(pre_body))

        # 本模块声明了什么（替换后名字仍可识别：声明行本身也被改写，故用原始声明判断）
        own = {n for n, o in owner.items() if o == rel}
        # 声明行里若被 STATE 化，S.xxx 形式不再算本模块声明
        own_exportable = {n for n in own if n not in state_names}
        exports: list[str] = []
        for n in sorted(own_exportable):
            # 只导出模块顶层声明的（排除缩进内同名）
            if decl_line.get(n) is None:
                continue
            exports.append(n)
        # window.X = function 形式的名字也要 export（挂全局的同时供 import）
        for name in sorted(n for n, o in window_fn_owner.items() if o == rel):
            if name not in exports:
                exports.append(name)

        # 依赖：正文里引用到的、属于其它模块的名字
        refs = set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)(?![\w$])', clean))
        local = set(re.findall(r'(?<![\w.$])(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)', clean))
        deps: dict[str, set[str]] = {}
        # 可变状态的引用判定走改写前文本（见 clean_pre 的注释）
        pre_refs = set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)(?![\w$])', clean_pre))
        uses_state = bool(pre_refs & set(state_names))
        if uses_state:
            deps.pop("state.js", None)
        for r in refs:
            if r in own or r in local:
                continue
            if r in state_names:
                uses_state = True
                continue
            o = owner.get(r)
            if o and o != rel:
                deps.setdefault(o, set()).add(r)

        # window.X = function 形式的名字也要 export（挂全局的同时供 import）。
        # 判定用前面已建好的 window_fn_owner，不要在这里另起一个正则 ——
        # 之前留了一份行首锚定的副本，与正确定义重名，覆盖掉了正确结果，
        # 导致 sandboxConfirm 始终没被导出。
        for name in sorted(n for n, o in window_fn_owner.items() if o == rel):
            if name not in exports:
                exports.append(name)

        depth = rel.count("/")
        # import 路径必须以 ./ 或 ../ 开头，不能用裸说明符。
        #
        # 浏览器不认识裸说明符（那是 Node / 打包器的解析规则），AstrBot 的
        # 资源重写也只处理以 ./ ../ / 开头的说明符
        # （`is_js_relative_module_specifier`）。早期版本在 js/ 根目录下
        # 生成的是 "components/backup.js"，服务端不重写、浏览器也不认，
        # 面板会整片白屏。
        up = "../" * depth if depth else "./"
        head = [f"/* {rel} — 由 index.html 拆分生成（可读性优先，逻辑与拆分前逐行一致） */"]
        if uses_state:
            head.append(f'import {{ S }} from "{up}state.js";')
        for o, names in sorted(deps.items()):
            head.append(f'import {{ {", ".join(sorted(names))} }} from "{up}{o}";')
        if exports:
            head.append("")
            head.append(f"export {{ {', '.join(exports)} }};")
        head.append("")

        io.open(os.path.join(outdir, rel), "w", encoding="utf-8", newline="\n").write(
            "\n".join(head + body) + "\n"
        )
        written[rel] = len(body)

    print()
    print("=== 写出模块 ===")
    for rel in order:
        print(f"  {rel:30s} {written[rel]:5d} 行正文")

    # ── 5.5 覆盖性校验：所有模块正文合起来必须覆盖原脚本的每一行代码 ──
    # 这是防「漏掉某一行」的底线检查。漏行的后果可能完全不报错
    # （例如丢掉 `else quillInit();` → 面板永不初始化，控制台干干净净），
    # 只靠语法检查根本发现不了。
    covered: set[int] = set()
    for rel, seglist in segs.items():
        for a, b in seglist:
            covered.update(range(a, b))
    script_range = set(range(3558, 7723))     # JS 主体：<script> 之后到 </script> 之前
    missing = sorted(script_range - covered)  # 允许的缺口另行白名单
    # 允许缺口：空行、注释行、以及块注释内部（各节标题与目录）。
    # 用括号深度跟踪判断「是否处在 /* … */ 块内」，比逐行前缀匹配可靠 ——
    # 脚本开头那段章节目录（A. 工具函数 / B. API 通信层 …）每行都不以
    # /* 或 * 开头，只靠前缀会被误判成「未覆盖的代码」。
    in_block = False
    block_lines: set[int] = set()
    for i in script_range:
        s = lines[i - 1].strip()
        if in_block:
            block_lines.add(i)
            if "*/" in s:
                in_block = False
            continue
        if "/*" in s and "*/" not in s:
            in_block = True
            block_lines.add(i)
            continue
        if "/*" in s and "*/" in s:
            block_lines.add(i)

    def _is_trivial(ln: int) -> bool:
        s = lines[ln - 1].strip()
        return (not s) or s.startswith("//") or ln in block_lines

    real_missing = [
        ln for ln in missing
        if not _is_trivial(ln) and lines[ln - 1].strip() != '"use strict";'
    ]
    print()
    print("=== 覆盖性校验（防漏行）===")
    if real_missing:
        print(f"  ✗ 有 {len(real_missing)} 行代码未被任何模块覆盖：")
        for ln in real_missing[:15]:
            print(f"      {ln:5d}  {lines[ln - 1][:90]}")
        rc_cover = 1
    else:
        print(f"  ✓ JS 主体每一行都已覆盖（跳过 {len(missing)} 行空行/注释）")
        rc_cover = 0
    # 关键哨兵：初始化分支必须还在，否则面板不初始化且不报错
    app_text = io.open(os.path.join(outdir, "app.js"), encoding="utf-8").read()
    if "else quillInit();" not in app_text:
        print("  ✗ app.js 缺少 `else quillInit();` —— 面板将完全不初始化")
        rc_cover = 1
    else:
        print("  ✓ app.js 保留 `else quillInit();` 初始化分支")

    # ── 5. 汇总校验：所有导入的名字都真实存在于目标模块的 export 里 ──
    print()
    print("=== 导出/导入一致性校验 ===")
    export_map: dict[str, set[str]] = {}
    import_re = re.compile(r'^import \{ ([^}]+) \} from "(.+)";$', re.MULTILINE)
    for rel in order + ["state.js"]:
        p = os.path.join(outdir, rel)
        text = io.open(p, encoding="utf-8").read()
        m = re.search(r'^export \{ ([^}]+) \};$', text, re.MULTILINE)
        export_map[rel] = set(x.strip() for x in m.group(1).split(",")) if m else set()
        if rel == "state.js":
            export_map[rel] = {"S"}

    bad = 0
    for rel in order:
        p = os.path.join(outdir, rel)
        text = io.open(p, encoding="utf-8").read()
        for m in import_re.finditer(text):
            names = [x.strip() for x in m.group(1).split(",")]
            spec = m.group(2)
            target = os.path.normpath(os.path.join(os.path.dirname(rel), spec))
            target = target.replace("\\", "/")
            if target not in export_map:
                print(f"  ✗ {rel}: 找不到模块 {target}")
                bad += 1
                continue
            missing = [n for n in names if n not in export_map[target]]
            if missing:
                print(f"  ✗ {rel} ← {target}: 未导出 {missing}")
                bad += 1
    print(f"  {'✓ 全部通过' if bad == 0 else f'✗ {bad} 处问题'}")

    return rc_cover or (1 if bad else 0)


if __name__ == "__main__":
    sys.exit(main())
