# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""架构守护测试（M1.3，防腐层）。

目标结构（PLAN §2.2，可机械校验的依赖规则）：

    interfaces/ ──import──> quill/          （单向）
    quill/core/ ←──import── quill/services/ （core 不依赖 services）
    quill/ 内部禁止 import astrbot.*        （含函数内延迟 import）

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


def _scan_imports(path: str, module: str) -> list[_ImportSite]:
    """收集文件内全部 import，包括函数体内的延迟 import。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    sites: list[_ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                sites.append(_ImportSite(module, path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                sites.append(_ImportSite(module, path, node.lineno, node.module))
            elif node.level > 0:
                # 相对 import：还原目标模块名
                base_parts = module.split(".")
                # level=1 → 当前包；逐级上跳
                up = base_parts[: len(base_parts) - (node.level - 1)] if len(base_parts) >= node.level else []
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
    """规则 3（软）：quill/config 与 quill/core 之间方向检查。

    允许：config → core（config 用 core 的工具）；
    禁止：core → config（core 是最底层，不得反向依赖配置投影层）。
    """
    violations = [
        s for s in _collect()
        if s.module.startswith("quill.core") and s.target == "quill.config"
    ]
    assert not violations, (
        "quill/core/ 不应依赖 quill/config（方向反了）：\n"
        + "\n".join(f"  {s.module} ({s.file}:{s.line}) -> {s.target}"
                    for s in violations)
    )
