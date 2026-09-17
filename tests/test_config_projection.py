# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配置投影完整性守卫（QuillConfig 字段 → 插件实例属性）。

要防的陷阱
----------
面板保存配置时走 `save_plugin_configs()`：重建 `QuillConfig`，再把一批配置值
「投影」到插件实例属性上（`_PROJECTED_ATTRS`），因为运行期代码读的是
`self.status_bar_enabled` 这类属性，不是 `self.config.status_bar_enabled`。

这个结构的问题在于：**加新配置项时必须记得登记**。漏登记不会报错——
保存能成功、面板显示已保存、重启后也生效（`__init__` 里读了 config），
只有「运行中改配置」这一条路径上读到旧值。症状是「不重启不生效」，
定位成本极高（要想到对比内存与磁盘）。

本测试把这条隐式约定变成显式断言：用 AST 解析源码，而不是硬编码行号，
所以不会因为重构位移而误报。

运行方式见 test_status_bar_parsers.py 顶部说明（同一套 PYTHONPATH）。
"""

from __future__ import annotations

import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)
_MAIN_PY = os.path.join(_PLUGIN_DIR, "main.py")
_CONFIG_PY = os.path.join(_PLUGIN_DIR, "config.py")

_PASSED = 0
_FAILED = 0
_FAILURES: list = []


def _assert(cond: bool, label: str, detail: str = "") -> None:
    global _PASSED, _FAILED
    if cond:
        _PASSED += 1
        print(f"  [PASS] {label}")
    else:
        _FAILED += 1
        msg = label if not detail else f"{label}\n         {detail}"
        _FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _projected_attrs_literal(func: ast.AST) -> list[str] | None:
    """取函数内 `_PROJECTED_ATTRS = (...)` 的字面量内容。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_PROJECTED_ATTRS" not in targets:
            continue
        value = node.value
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            out = []
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
            return out
    return None


def _self_attr_assigned_in(func: ast.AST) -> set[str]:
    """取函数内所有 `self.X = ...` 的 X（含增强赋值与注解赋值）。"""
    names: set[str] = set()

    def _collect_target(t: ast.AST) -> None:
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                and t.value.id == "self":
            names.add(t.attr)

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _collect_target(t)
        elif isinstance(node, ast.AugAssign):
            _collect_target(node.target)
        elif isinstance(node, ast.AnnAssign):
            _collect_target(node.target)
    return names


def _config_fields(config_src: str) -> list[str]:
    """取 QuillConfig.__init__ 里的 `self.X: T = ...` 字段名（不含私有与内部状态）。"""
    tree = ast.parse(config_src)
    init = _find_func(tree, "__init__")
    fields: list[str] = []
    for node in ast.walk(init):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            name = node.target.attr
            if name.startswith("_"):
                continue
            fields.append(name)
    return fields


# ──────────────────────────────────────────────────────────────────
# 显式豁免：这些配置字段**有意**不投影，运行期直接读 self.config.X
#
# 为什么允许这种形态：投影是为了让热更新生效，而这些字段要么在
# 请求装配时才读（每次都新建 config 感知），要么由子对象持有。
# 每个条目都必须写明理由，新增时必须显式追加。
# ──────────────────────────────────────────────────────────────────
_LIVE_READ_FIELDS = {
    # RAG 组件在 initialize() 时按这些值构建检索器/嵌入器；改这些值需要
    # 重建组件，由 save 流程里的 embedding 变更分支单独处理，不是属性级热更。
    "rag_llm_provider_id", "rag_embedding_provider_id", "rag_rerank_provider_id",
    "rag_enable_local_embedding", "rag_chunk_size", "rag_chunk_overlap",
    "rag_top_k", "rag_dense_top_k", "rag_enable_memory",
    "rag_enable_autonomous_reflection",
    # 世界书/素材库的装配参数在各自 manager 内按需读取
    "worldbook_enabled", "worldbook_max_dynamic", "worldbook_max_token",
    "worldbook_sensitivity", "worldbook_injection_pos", "worldbook_show_log",
    "wr_enabled", "wr_dedup_limit",
    # 输出长度限制由 PromptBuilder 持有（prompt_builder 本身在投影列表里，
    # 重建 PromptBuilder 即带走这些值）
    "max_prompt_length", "min_output_length", "max_output_length",
    # 状态栏协议文本与提取器配置由 PromptBuilder / 提取方法按需读取
    "status_bar_format", "status_bar_llm_extract", "status_bar_llm_provider_id",
    # 群聊写权限白名单：每次指令都经 _check_group_permission 现读
    # plugin.config.admin_users（commands.py），无需投影
    "admin_users",
}

# 投影属性名与配置字段名不一致的（改名是为了可读性，需显式登记）
_ALIASES = {
    "status_bar_format": "status_bar_format_template",
    "status_bar_fields": "love_fields",
    "worldbook_max_dynamic": "wb_max_entries",
    "wr_fallback_top": "wr_fallback_top_count",
    "debug_enabled": "debug",
}


def t1_save_block_self_consistent() -> None:
    print("\n== 1. _PROJECTED_ATTRS 与保存块赋值双向一致 ==")

    tree = ast.parse(_read(_MAIN_PY))
    save_fn = _find_func(tree, "save_plugin_configs")
    _assert(save_fn is not None, "找到 save_plugin_configs()")
    if save_fn is None:
        return

    listed = _projected_attrs_literal(save_fn)
    _assert(listed is not None, "找到 _PROJECTED_ATTRS 字面量")
    if listed is None:
        return

    listed_set = set(listed)
    _assert(len(listed) == len(listed_set), "列表内无重复项",
            f"重复: {sorted({x for x in listed if listed.count(x) > 1})}")

    assigned = _self_attr_assigned_in(save_fn)
    # self.config 是配置对象本身（每次重建），不是投影出来的标量属性
    assigned.discard("config")

    missing_assign = sorted(listed_set - assigned)
    _assert(
        not missing_assign,
        "列表里的每项都真的被赋值（防重命名/删除后残留死条目）",
        f"列了但没赋值: {missing_assign}",
    )

    unregistered = sorted(assigned - listed_set)
    _assert(
        not unregistered,
        "保存块里赋的每个 self.X 都已登记（防「加了赋值忘了登记」→ 热更新失效）",
        f"赋值了但没登记: {unregistered}",
    )


def t2_config_fields_covered() -> None:
    print("\n== 2. QuillConfig 每个字段都被覆盖（投影 / 改名 / 显式豁免）==")

    fields = _config_fields(_read(_CONFIG_PY))
    _assert(len(fields) > 30, f"解析到 QuillConfig 字段（{len(fields)} 个）")

    tree = ast.parse(_read(_MAIN_PY))
    save_fn = _find_func(tree, "save_plugin_configs")
    listed = set(_projected_attrs_literal(save_fn) or [])

    uncovered = []
    for f in fields:
        if f in listed:                      # 同名投影
            continue
        if _ALIASES.get(f) in listed:        # 改名投影
            continue
        if f in _LIVE_READ_FIELDS:           # 显式豁免
            continue
        uncovered.append(f)

    _assert(
        not uncovered,
        "每个配置字段都有归宿（同名投影 / 改名投影 / 显式豁免）",
        f"未覆盖: {uncovered}\n"
        f"         → 若该字段需要热更新，加进 _PROJECTED_ATTRS 并在保存块里赋值；\n"
        f"           若确实不需要，加进本文件的 _LIVE_READ_FIELDS 并写明理由。",
    )

    # 豁免表不得含已不存在的字段（防重构后留下误导性条目）
    stale = sorted(f for f in _LIVE_READ_FIELDS if f not in fields)
    _assert(not stale, "_LIVE_READ_FIELDS 无失效条目", f"已不存在: {stale}")

    stale_alias = sorted(k for k in _ALIASES if k not in fields)
    _assert(not stale_alias, "_ALIASES 无失效条目", f"已不存在: {stale_alias}")


def t3_alias_targets_registered() -> None:
    print("\n== 3. 改名映射的目标在投影列表里 ==")

    tree = ast.parse(_read(_MAIN_PY))
    save_fn = _find_func(tree, "save_plugin_configs")
    listed = set(_projected_attrs_literal(save_fn) or [])

    for src, dst in sorted(_ALIASES.items()):
        _assert(dst in listed, f"{src} → {dst} 目标已登记")


def t4_projection_rolls_back() -> None:
    print("\n== 4. 投影属性在保存失败时回滚（快照覆盖完整）==")

    tree = ast.parse(_read(_MAIN_PY))
    save_fn = _find_func(tree, "save_plugin_configs")
    listed = _projected_attrs_literal(save_fn) or []
    src = _read(_MAIN_PY)

    # 快照必须建在 try 之外：回滚分支在 try 内任何一步抛异常时都会用到它
    _assert(
        "projected_previous = {" in src and "_MISSING" in src,
        "存在投影属性快照与哨兵值",
    )
    _assert(
        "for name in _PROJECTED_ATTRS" in src,
        "快照遍历的是 _PROJECTED_ATTRS（新登记的项自动纳入回滚）",
    )
    _assert(
        "for name, old in projected_previous.items()" in src,
        "回滚分支逐项还原快照",
    )
    _assert(len(listed) >= 18, f"投影项数至少在预期量级（{len(listed)}）")


def main() -> int:
    print("=== 配置投影完整性守卫 ===")
    t1_save_block_self_consistent()
    t2_config_fields_covered()
    t3_alias_targets_registered()
    t4_projection_rolls_back()

    print(f"\n=== 结果: {_PASSED} passed, {_FAILED} failed ===")
    if _FAILURES:
        print("\n失败项：")
        for f in _FAILURES:
            print("  - " + f)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
