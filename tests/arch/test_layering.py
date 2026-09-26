# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""架构守护测试（M1.3，防腐层）。

目标结构（PLAN §2.2，可机械校验的依赖规则）：

    interfaces/ ──import──> quill/           （单向，规则 4）
    quill/core/ ←──import── quill/services/  （core 不依赖 services，规则 2）
    quill/core/ 不得依赖插件根 config.py      （配置投影层单向，规则 3）
    quill/ 内部禁止 import astrbot.*         （含函数内延迟 import，规则 1）

实现：AST 扫描全部 import（含函数体内的延迟 import），违规即失败。
M2 结束时本测试必须转正通过；M2 之前 quill/ 包尚不存在——此时整体
skip（不是 pass：skip 是显式的"守卫未激活"，避免误以为已被保护）。

规则边界：tests/ 自身（含本文件）不受约束；tools/、docs/ 探针不受约束
（它们按定义就需要 import astrbot）。
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
# M2.1 修正：本文件位于 tests/arch/ 下，插件根在**上两级**。原实现只上跳
# 一级落到 tests/，_QUILL_DIR 指向 tests/quill，守卫永远扫不到真正的
# quill/ 包而恒被 skip（M1 期间 quill/ 尚不存在，两种路径同样 skip，
# bug 不可见；quill/ 落地后即暴露）。
_PLUGIN_DIR = os.path.dirname(os.path.dirname(_HERE))
_QUILL_DIR = os.path.join(_PLUGIN_DIR, "quill")

_HAS_QUILL = os.path.isdir(_QUILL_DIR)

import pytest

pytestmark = pytest.mark.skipif(
    not _HAS_QUILL,
    reason="quill/ 包尚未创建（M2 拆分中）——架构守卫待 M2 结束转正",
)


@dataclass
class _ImportSite:
    module: str          # 相对插件根的模块路径（点分隔）
    file: str
    line: int
    target: str          # import 目标（astrbot.api / quill.services 等）


def _iter_py_files(root: str, prefix: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, _PLUGIN_DIR)
            parts = rel[:-3].split(os.sep)
            parts[0] = prefix if parts[0] == "__init__" else parts[0]
            module = ".".join(p for p in parts if p != "__init__")
            yield full, module or prefix


def _is_package_file(path: str) -> bool:
    return os.path.basename(path) == "__init__.py"


def _scan_imports(path: str, module: str) -> list[_ImportSite]:
    """收集文件内全部 import，包括函数体内的延迟 import。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    # __init__.py 的 module 段已被 _iter_py_files 过滤掉——它本身就是包，
    # 相对 import 的上跳档位因此比普通模块文件少一级。
    is_package = _is_package_file(path)
    sites: list[_ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                sites.append(_ImportSite(module, path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                sites.append(_ImportSite(module, path, node.lineno, node.module))
            elif node.level > 0:
                # 相对 import：还原目标模块名。module 含文件名本体（如
                # quill.core.paths），普通模块文件上跳 level 级、__init__.py
                # 上跳 level-1 级。验收 P1 修正：原实现一律按 level-1 上跳，
                # level>=2 时把 core 文件的 ``from ..services.x`` 还原成
                # quill.core.services.x——规则 2/3 对这种写法失明。
                base_parts = module.split(".")
                trim = node.level - 1 if is_package else node.level
                up = base_parts[: len(base_parts) - trim] if len(base_parts) > trim else []
                target = ".".join(up + ([node.module] if node.module else []))
                sites.append(_ImportSite(module, path, node.lineno, target))
    return sites


def _collect() -> list[_ImportSite]:
    sites: list[_ImportSite] = []
    if os.path.isdir(os.path.join(_PLUGIN_DIR, "quill")):
        sites += _scan_all(os.path.join(_PLUGIN_DIR, "quill"), "quill")
    if os.path.isdir(os.path.join(_PLUGIN_DIR, "interfaces")):
        sites += _scan_all(os.path.join(_PLUGIN_DIR, "interfaces"), "interfaces")
    return sites


def _scan_all(root: str, prefix: str) -> list[_ImportSite]:
    sites: list[_ImportSite] = []
    for full, module in _iter_py_files(root, prefix):
        sites += _scan_imports(full, module)
    return sites


def test_quill_package_forbids_astrbot_imports() -> None:
    """规则 1：quill/ 包内禁止 import astrbot.*（含延迟 import）。"""
    violations = [
        s for s in _collect()
        if s.module.startswith("quill") and s.target.startswith("astrbot")
    ]
    assert not violations, (
        "quill/ 包内发现 astrbot import（违反框架解耦规则）：\n"
        + "\n".join(f"  {s.module} ({s.file}:{s.line}) -> {s.target}"
                    for s in violations)
    )


def test_quill_core_does_not_import_services() -> None:
    """规则 2：quill/core/ 禁止 import quill/services/。"""
    violations = [
        s for s in _collect()
        if s.module.startswith("quill.core") and s.target.startswith("quill.services")
    ]
    assert not violations, (
        "quill/core/ 不应依赖 quill/services/：\n"
        + "\n".join(f"  {s.module} ({s.file}:{s.line}) -> {s.target}"
                    for s in violations)
    )


def test_quill_layers_import_direction() -> None:
    """规则 3（软）：core 不依赖配置投影层。

    允许：config → core（config 用 core 的工具，且根 config.py 不在本
    守卫扫描范围）；禁止：core → config。配置投影层在插件根 ``config.py``，
    core 文件经三级相对 import（``from ...config import x``）触及它时还原
    为 ``config``——验收 P1 修正前该目标恰被错还原成不存在的
    ``quill.config``，而旧断言只认后者：两个缺陷互相掩护、恒绿空转。
    两种拼写一并禁止，防未来在 quill/ 内再造配置层。
    """
    violations = [
        s for s in _collect()
        if s.module.startswith("quill.core")
        and (s.target == "config" or s.target.startswith("config.")
             or s.target == "quill.config" or s.target.startswith("quill.config."))
    ]
    assert not violations, (
        "quill/core/ 不应依赖配置投影层（config）：\n"
        + "\n".join(f"  {s.module} ({s.file}:{s.line}) -> {s.target}"
                    for s in violations)
    )


def test_quill_does_not_import_interfaces() -> None:
    """规则 4：quill/ 禁止 import interfaces/（单向 interfaces → quill）。

    文件头声称的单向关系此前没有任何断言（验收 P1）。本仓代码全用相对
    import（无绝对自包名拼写），按插件相对命名空间检查即可覆盖。
    """
    violations = [
        s for s in _collect()
        if s.module.startswith("quill") and s.target.startswith("interfaces")
    ]
    assert not violations, (
        "quill/ 不应依赖 interfaces/（方向反了，接口层单向依赖服务层）：\n"
        + "\n".join(f"  {s.module} ({s.file}:{s.line}) -> {s.target}"
                    for s in violations)
    )


def test_relative_import_restoration_regular_module(tmp_path) -> None:
    """P1 回归：普通模块文件 level>=2 相对 import 必须还原到正确目标。

    差一缺陷下 ``from ..services.x`` 在 quill.core.offender 里被还原成
    ``quill.core.services.x``——不命中规则 2 的 ``quill.services`` 前缀，
    违规漏检。
    """
    offender = tmp_path / "offender.py"
    offender.write_text("from ..services.x import y\n", encoding="utf-8")
    sites = _scan_imports(str(offender), "quill.core.offender")
    assert len(sites) == 1
    assert sites[0].target == "quill.services.x"


def test_relative_import_restoration_three_levels(tmp_path) -> None:
    """P1 回归：statusbar 式三级上跳（``from ...core.logbridge``）。"""
    offender = tmp_path / "parsers.py"
    offender.write_text("from ...core.logbridge import logger\n", encoding="utf-8")
    sites = _scan_imports(str(offender), "quill.services.statusbar.parsers")
    assert len(sites) == 1
    assert sites[0].target == "quill.core.logbridge"


def test_relative_import_restoration_package_init(tmp_path) -> None:
    """P1 回归：__init__.py 上跳档位比普通文件少一级（module 已滤 __init__）。"""
    pkg = tmp_path / "__init__.py"
    pkg.write_text("from .parsers import parse\n", encoding="utf-8")
    sites = _scan_imports(str(pkg), "quill.services.statusbar")
    assert len(sites) == 1
    assert sites[0].target == "quill.services.statusbar.parsers"
