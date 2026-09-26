#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""版本号单源对拍（v5.3.0 M4.1，docs/v5.3/PLAN.md §M4.1 / §2.4 防再漂移机制）。

背景：v5.2.5 之前版本号三处漂移（main.py @register="5.0.6" ≠ metadata.yaml
= README badge "5.2.5"），@register 的描述还是 v5.0 时代的五合一旧文案。
M4.1 起 ``quill/__init__.py::__version__`` 是唯一真源，本脚本静态解析各
落点并互相对拍——任何不一致即退出码 1（CI 步骤，`.github/workflows/
tests.yml` 在 pytest 之前执行）。

对拍来源（全部**静态解析，不 import 插件**——插件 import 会连带 astrbot）：

1. ``quill/__init__.py`` 的 ``__version__`` —— 唯一真源（AST 解析赋值）；
2. ``main.py`` ``@register(...)`` 的版本参数（AST 解析装饰器调用第 4 个
   位置参数，兼容 version= 关键字）。两种合法形态：
   - ``__version__``（Name 节点）且 main.py 存在 ``from .quill import
     __version__`` —— 单源引用，值按真源取，视为一致（构造上相等）；
   - 字符串字面量 —— 直接与真源比对（退路形态，仍受对拍看守）；
   其余（无法静态解析的表达式）一律判失败。
3. ``metadata.yaml`` 的 ``version:`` 字段（正则行解析，不引入 PyYAML 依赖）。

顺带对拍（同属 PLAN §2.4 漂移表，防止再次漂移）：

- ``@register`` 的描述参数 vs ``metadata.yaml`` 的 ``desc:``（两侧均为
  字面量时才可比，取不到则跳过并注明）；
- ``README.md`` 的版本 badge（``version-X.Y.Z-`` 形态，找到才校验，
  无 badge 不误报）。

用法::

    python tools/check_version.py [仓库根目录]

省略参数时以本脚本所在目录的上一级为仓库根（tests 经参数传入 tmp 样本
目录做对拍样本测试）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

#: 默认仓库根 = tools/ 的上一级（脚本随仓库移动无需改）。
DEFAULT_ROOT = Path(__file__).resolve().parent.parent

_METADATA_VERSION_RE = re.compile(r'^version:\s*["\']?([^"\'#\s]+?)["\']?\s*(?:#.*)?$')
_METADATA_DESC_RE = re.compile(r'^desc:\s*"(.+)"\s*$')
_README_BADGE_RE = re.compile(r"version-([0-9]+(?:\.[0-9]+)*)-")


# ── 各来源静态解析 ────────────────────────────────────────────────────


def _extract_dunder_version(quill_init: Path) -> str:
    """AST 解析 quill/__init__.py 的 ``__version__`` 赋值（唯一真源）。"""
    tree = ast.parse(quill_init.read_text(encoding="utf-8"), filename=str(quill_init))
    found: list[str] = []
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
            and node.value is not None
        ):
            value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.append(value.value)
        elif value is not None:
            raise ValueError("__version__ 赋值不是字符串字面量，无法静态解析")
    if not found:
        raise ValueError("未找到 __version__ 字符串赋值")
    if len(found) > 1:
        raise ValueError(f"__version__ 被赋值 {len(found)} 次，真源不唯一")
    return found[0]


def _extract_register(main_py: Path) -> tuple[object, str | None, bool]:
    """AST 解析 main.py 的 ``@register(...)`` 装饰器调用。

    返回 ``(version_expr, desc, has_single_source_import)``：

    - ``version_expr``：``("literal", "x.y.z")`` 字面量 / ``("name", 名字)``
      Name 引用 / ``None``（没取到版本参数）；
    - ``desc``：描述字面量（非字面量为 ``None``）；
    - ``has_single_source_import``：main.py 是否 ``from .quill import
      __version__``（含 ``from quill import`` 兜底形态）。
    """
    tree = ast.parse(main_py.read_text(encoding="utf-8"), filename=str(main_py))

    has_single_source_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.names:
            module = node.module or ""
            if node.level == 1 and module == "quill":
                has_single_source_import |= any(
                    a.name == "__version__" for a in node.names
                )
            elif node.level == 0 and module == "quill":
                has_single_source_import |= any(
                    a.name == "__version__" for a in node.names
                )

    version_expr: object = None
    desc: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)):
                continue
            func = dec.func
            func_name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            )
            if func_name != "register":
                continue
            # 位置参数：register(name, author, desc, version, repo)
            if len(dec.args) >= 4:
                version_expr = _arg_repr(dec.args[3])
            for kw in dec.keywords:
                if kw.arg == "version":
                    version_expr = _arg_repr(kw.value)
                elif kw.arg == "desc" and desc is None:
                    d = _arg_repr(kw.value)
                    desc = d[1] if isinstance(d, tuple) else None
            if desc is None and len(dec.args) >= 3:
                d = _arg_repr(dec.args[2])
                desc = d[1] if isinstance(d, tuple) else None
            # 只认第一个被 @register 装饰的类
            return version_expr, desc, has_single_source_import
    raise ValueError("main.py 未找到 @register(...) 装饰的插件类")


def _arg_repr(node: ast.expr):
    """装饰器实参 → ('literal', 值) / ('name', 名字) / None（不可解析）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ("literal", node.value)
    if isinstance(node, ast.Name):
        return ("name", node.id)
    return None


def _extract_metadata(metadata: Path) -> tuple[str | None, str | None]:
    """正则行解析 metadata.yaml 的 version / desc（不引入 PyYAML 依赖）。"""
    version: str | None = None
    desc: str | None = None
    for raw in metadata.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if version is None:
            m = _METADATA_VERSION_RE.match(line)
            if m:
                version = m.group(1)
                continue
        if desc is None:
            m = _METADATA_DESC_RE.match(line)
            if m:
                desc = m.group(1)
    return version, desc


def _extract_readme_badge(readme: Path) -> str | None:
    """README 版本 badge（``version-X.Y.Z-`` 形态）；无 badge 返回 None。"""
    m = _README_BADGE_RE.search(readme.read_text(encoding="utf-8"))
    return m.group(1) if m else None


# ── 对拍 ─────────────────────────────────────────────────────────────


def check(root: str | Path = DEFAULT_ROOT) -> tuple[bool, list[str]]:
    """对拍全部版本落点。返回 ``(是否一致, 报告行列表)``。

    解析失败（文件缺失 / 无法静态解析）按不一致处理——宁可误报也不放过
    漂移。
    """
    root = Path(root)
    quill_init = root / "quill" / "__init__.py"
    main_py = root / "main.py"
    metadata = root / "metadata.yaml"
    readme = root / "README.md"

    lines: list[str] = []
    problems: list[str] = []

    # 1. 唯一真源
    try:
        source = _extract_dunder_version(quill_init)
        lines.append(f"OK   quill/__init__.py __version__ = {source}（唯一真源）")
    except Exception as exc:  # noqa: BLE001 —— 解析失败必须显式报告
        lines.append(f"FAIL quill/__init__.py __version__ 解析失败：{exc}")
        problems.append(f"真源解析失败：{exc}")
        source = None

    # 2. main.py @register
    register_version: str | None = None
    register_desc: str | None = None
    register_desc_literal = False
    if source is not None:
        try:
            expr, desc, has_import = _extract_register(main_py)
            if isinstance(expr, tuple) and expr[0] == "name":
                if expr[1] == "__version__" and has_import:
                    register_version = source
                    lines.append(
                        f"OK   main.py @register version = __version__"
                        f"（自 quill 单源导入）→ {source}"
                    )
                else:
                    problems.append(
                        f"main.py @register version 为不可静态解析的引用 "
                        f"{expr[1]!r}"
                        f"（需字面量或 from .quill import __version__）"
                    )
            elif isinstance(expr, tuple) and expr[0] == "literal":
                register_version = expr[1]
                if expr[1] == source:
                    lines.append(f"OK   main.py @register version = {expr[1]}（字面量）")
                else:
                    lines.append(
                        f"FAIL main.py @register version = {expr[1]!r} ≠ 真源 {source!r}"
                    )
                    problems.append(
                        f"main.py @register version = {expr[1]!r} ≠ 真源 {source!r}"
                    )
            else:
                problems.append("main.py @register 未取到 version 参数")
            if isinstance(desc, str):
                register_desc = desc
                register_desc_literal = True
        except Exception as exc:  # noqa: BLE001
            lines.append(f"FAIL main.py @register 解析失败：{exc}")
            problems.append(f"@register 解析失败：{exc}")

    # 3. metadata.yaml
    try:
        meta_version, meta_desc = _extract_metadata(metadata)
        if meta_version is None:
            problems.append("metadata.yaml 未解析到 version 字段")
        elif meta_version == source:
            lines.append(f"OK   metadata.yaml version = {meta_version}")
        else:
            lines.append(
                f"FAIL metadata.yaml version = {meta_version!r} ≠ 真源 {source!r}"
            )
            problems.append(
                f"metadata.yaml version = {meta_version!r} ≠ 真源 {source!r}"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL metadata.yaml 解析失败：{exc}")
        problems.append(f"metadata.yaml 解析失败：{exc}")
        meta_desc = None

    # 4. README badge（找到才校验）
    if readme.exists():
        try:
            badge = _extract_readme_badge(readme)
            if badge is None:
                lines.append("SKIP README 未找到版本 badge（不校验）")
            elif badge == source:
                lines.append(f"OK   README badge version = {badge}")
            else:
                lines.append(
                    f"FAIL README badge version = {badge!r} ≠ 真源 {source!r}"
                )
                problems.append(f"README badge version = {badge!r} ≠ 真源 {source!r}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"FAIL README 解析失败：{exc}")
            problems.append(f"README 解析失败：{exc}")

    # 5. @register 描述 vs metadata desc（两侧均取到才可比）
    if register_desc_literal and meta_desc is not None:
        if register_desc == meta_desc:
            lines.append("OK   main.py @register desc 与 metadata.yaml desc 一致")
        else:
            problems.append("main.py @register desc 与 metadata.yaml desc 不一致")

    ok = not problems
    lines.append(
        f"{'PASS' if ok else 'FAIL'} 版本对拍："
        + ("；".join(problems) if problems else "全部落点一致")
    )
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else DEFAULT_ROOT
    ok, lines = check(root)
    for line in lines:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
