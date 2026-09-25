# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配置访问器覆盖完整性守卫（QuillConfig 字段 → QuillConfigProperties）。

M2.3 变更说明（原为「配置投影完整性守卫」）
------------------------------------------
v5.2.5 的反模式：__init__ 把 config 字段复制成插件实例属性，
`save_plugin_configs()` 手工同步/回滚这批投影（`_PROJECTED_ATTRS`），
漏登记 = 「不重启不生效」。旧版本测试看守那套投影登记。

M2.3 已把投影消解（债务 D2）：配置唯一真源是 `self.config`（QuillConfig
对象），运行期经 `self.props.<attr>`（props.py 的 QuillConfigProperties，
逐属性实时读）访问；保存 = 整体替换 config 引用，访问器自动读到新值，
无需任何同步代码。

本测试随之改看守**新形态**的完整性：

  t1  QuillConfig 每个字段都有归宿：props 同名访问器 / 改名访问器 /
      显式豁免（运行期直读 self.config 或由子对象持有）——防「加配置项
      忘加访问器」的新形态漂移；
  t2  投影确已消亡：main.py 无 `_PROJECTED_ATTRS`、__init__ 与
      save_plugin_configs 无 `self.X = self.config.Y` 式同步赋值、
      无裸 `self.<19 属性名>` 读取（一律 self.props.*）；
  t3  mixin 委托面完整：quill/services/statusbar/ 的 Mixin（方法体零
      改动纪律）以裸 `self.<attr>` 读的那批属性，QuillPlugin 上都有
      只读委托 property 且逐个转发到同名 props 访问器；
  t4  save_plugin_configs 新语义：config 引用快照在 try 外建立、成功
      路径重建 config、失败路径恢复旧引用 + Retriever 三字段回滚，
      且不再有任何投影残留。

用 AST 解析源码，不硬编码行号，重构位移不会误报。
运行方式见 test_status_bar_parsers.py 顶部说明（同一套 PYTHONPATH）。
"""

from __future__ import annotations

import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容两处位置：仓库根 tests/（M1 之前）与 tests/legacy/（M1 起）
_PLUGIN_DIR = os.path.dirname(_HERE) if os.path.exists(
    os.path.join(os.path.dirname(_HERE), "main.py")
) else os.path.dirname(os.path.dirname(_HERE))
_MAIN_PY = os.path.join(_PLUGIN_DIR, "main.py")
_CONFIG_PY = os.path.join(_PLUGIN_DIR, "config.py")
_PROPS_PY = os.path.join(_PLUGIN_DIR, "props.py")
_STATUSBAR_DIR = os.path.join(_PLUGIN_DIR, "quill", "services", "statusbar")

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


def _find_class(tree: ast.AST, name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _class_properties(cls_node: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    """取类体内带 @property 装饰器的方法（名 → 节点）。"""
    props: dict[str, ast.FunctionDef] = {}
    for item in cls_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in item.decorator_list:
            dec_name = dec.id if isinstance(dec, ast.Name) else (
                dec.attr if isinstance(dec, ast.Attribute) else ""
            )
            if dec_name == "property":
                props[item.name] = item
    return props


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


def _self_attr_loads(tree: ast.AST) -> set[str]:
    """取树内所有**读取**形态的 `self.X` 的 X（self.props.X 不算——其
    Attribute 节点的 value 是 `self.props` 而非裸 `self`）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Load)
        ):
            names.add(node.attr)
        # getattr(self, "x") / getattr(self, "x", default) 形态（防御式读取）
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def _projection_assignments(func: ast.AST) -> list[str]:
    """取函数内 `self.X = self.config.Y` / `self.X = getattr(self.config, ...)` 式
    投影赋值的目标名（投影反模式的机械特征）。"""
    names: list[str] = []

    def _is_config_read(node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute):
            return (
                isinstance(node.value, ast.Name) and node.value.id == "config"
            ) or _is_config_read(node.value)
        if isinstance(node, ast.Call):
            if node.args and _is_config_read(node.args[0]):
                return True
            return False
        return False

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                    and _is_config_read(node.value)
                ):
                    names.append(t.attr)
    return names


# ──────────────────────────────────────────────────────────────────
# 显式豁免：这些配置字段**有意**不设 props 访问器，运行期直接读
# self.config.X（或由子对象持有）。
#
# 每个条目都必须写明理由，新增时必须显式追加。
# ──────────────────────────────────────────────────────────────────
_LIVE_READ_FIELDS = {
    # RAG 组件在 initialize() 时按这些值构建检索器/嵌入器；改这些值需要
    # 重建组件，由 save 流程里的 embedding 变更分支单独处理，不是属性级
    # 热更。top_k/enable_memory 两项由 Retriever 热更新分支直接同步。
    "rag_llm_provider_id", "rag_embedding_provider_id", "rag_rerank_provider_id",
    "rag_enable_local_embedding", "rag_chunk_size", "rag_chunk_overlap",
    "rag_top_k", "rag_dense_top_k", "rag_enable_memory",
    "rag_enable_autonomous_reflection",
    # 世界书/素材库的装配参数在请求路径上经 self.config 现读
    "worldbook_enabled", "worldbook_max_token",
    "worldbook_sensitivity", "worldbook_injection_pos", "worldbook_show_log",
    "wr_enabled", "wr_dedup_limit",
    # 输出长度限制由 PromptBuilder 构造时快照持有；prompt_builder 访问器
    # 按 config 代自动重建，这些值随之更新（见 props.prompt_builder）
    "max_prompt_length", "min_output_length", "max_output_length",
    # 状态栏协议文本与提取器配置由 PromptBuilder / 提取方法按需读取
    "status_bar_llm_extract", "status_bar_llm_provider_id",
    # 群聊写权限白名单：每次指令都经 _check_group_permission 现读
    # plugin.config.admin_users（commands.py），无需访问器
    "admin_users",
}

# 配置字段名与 props 访问器名不一致的（改名是为了可读性，需显式登记）
_ALIASES = {
    "status_bar_format": "status_bar_format_template",
    "status_bar_fields": "love_fields",
    "worldbook_max_dynamic": "wb_max_entries",
    "wr_fallback_top": "wr_fallback_top_count",
    "debug_enabled": "debug",
}

# props 访问器中不直接对应某个 config 字段的特殊项（各自有明确机制）
_SPECIAL_PROPS = {
    # PromptBuilder 按 config 代缓存的访问器（见 props.py docstring）
    "prompt_builder",
}

# v5.2.5 投影清单的 19 项——M2.3 后全部成为 props 访问器（数量下限看守）
_EXPECTED_ACCESSOR_COUNT = 19


def t1_accessor_coverage() -> None:
    print("\n== 1. QuillConfig 每个字段都被访问器层覆盖（同名 / 改名 / 显式豁免）==")

    fields = _config_fields(_read(_CONFIG_PY))
    _assert(len(fields) > 30, f"解析到 QuillConfig 字段（{len(fields)} 个）")

    props_tree = ast.parse(_read(_PROPS_PY))
    props_cls = _find_class(props_tree, "QuillConfigProperties")
    _assert(props_cls is not None, "props.py 中找到 QuillConfigProperties")
    if props_cls is None:
        return
    accessors = _class_properties(props_cls)
    _assert(
        len(accessors) >= _EXPECTED_ACCESSOR_COUNT,
        f"访问器数量符合投影清单量级（{len(accessors)} >= {_EXPECTED_ACCESSOR_COUNT}）",
    )

    uncovered = []
    for f in fields:
        if f in accessors:                      # 同名访问器
            continue
        if _ALIASES.get(f) in accessors:        # 改名访问器
            continue
        if f in _LIVE_READ_FIELDS:              # 显式豁免
            continue
        uncovered.append(f)

    _assert(
        not uncovered,
        "每个配置字段都有归宿（同名访问器 / 改名访问器 / 显式豁免）",
        f"未覆盖: {uncovered}\n"
        f"         → 若该字段需要面板热更新，在 props.py 加 @property 并更新本表；\n"
        f"           若确实不需要，加进本文件的 _LIVE_READ_FIELDS 并写明理由。",
    )

    # 反向：每个访问器都要有出处（同名字段 / 别名目标 / 特殊机制），
    # 防止留下读不到任何配置的悬空访问器
    field_set = set(fields)
    dangling = []
    for name in accessors:
        if name in field_set or name in _SPECIAL_PROPS:
            continue
        if name in _ALIASES.values():
            continue
        dangling.append(name)
    _assert(
        not dangling,
        "每个 props 访问器都有配置字段出处",
        f"悬空访问器: {dangling}",
    )

    # 豁免/别名表不得含已不存在的字段（防重构后留下误导性条目）
    stale = sorted(f for f in _LIVE_READ_FIELDS if f not in field_set)
    _assert(not stale, "_LIVE_READ_FIELDS 无失效条目", f"已不存在: {stale}")

    stale_alias = sorted(k for k in _ALIASES if k not in field_set)
    _assert(not stale_alias, "_ALIASES 无失效条目", f"已不存在: {stale_alias}")


def t2_projection_eliminated() -> None:
    print("\n== 2. 投影反模式确已消亡（main.py）==")
    src = _read(_MAIN_PY)
    tree = ast.parse(src)

    _assert("_PROJECTED_ATTRS" not in src, "_PROJECTED_ATTRS 清单已删除")
    _assert(
        "projected_previous" not in src and "_MISSING = object()" not in src,
        "投影快照/哨兵机制已删除",
    )

    init = _find_func(_find_class(tree, "QuillPlugin"), "__init__")
    _assert(init is not None, "找到 QuillPlugin.__init__")
    if init is not None:
        proj = _projection_assignments(init)
        _assert(
            not proj,
            "__init__ 不再把 config 复制到实例属性",
            f"残留投影赋值: {proj}",
        )
        has_props = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name) and t.value.id == "self"
                and t.attr == "props"
                for t in node.targets
            )
            for node in ast.walk(init)
        )
        _assert(has_props, "__init__ 创建了 self.props（访问器入口）")

    save_fn = _find_func(tree, "save_plugin_configs")
    _assert(save_fn is not None, "找到 save_plugin_configs()")
    if save_fn is None:
        return
    proj = _projection_assignments(save_fn)
    _assert(
        not proj,
        "save_plugin_configs 不再逐属性同步投影",
        f"残留投影赋值: {proj}",
    )

    # 全文件：19 个旧投影名不得再以裸 self.X 形态被读取（一律 self.props.X）
    accessor_names = set(_ALIASES.values()) | {
        "wr_max_entries", "refusal_enabled", "refusal_patterns",
        "status_bar_enabled", "status_bar_format_plain", "status_bar_plain_platforms",
        "status_bar_plot_paths", "status_bar_default_placeholder",
        "status_bar_show_delta", "show_inject_report",
        "rag_enable_chat_logging", "rag_chat_log_retention_days",
        "worldbook_always_activate",
    }
    bare = sorted(_self_attr_loads(tree) & accessor_names)
    _assert(
        not bare,
        "main.py 无裸 self.<投影名> 读取（一律经 self.props.*）",
        f"裸读取: {bare}",
    )


def _class_frozenset_literal(cls_node: ast.ClassDef, attr_name: str) -> set[str]:
    """取类体内 `attr = frozenset({...})` / `attr = {...}` 字面量的字符串集合。"""
    for item in cls_node.body:
        if not isinstance(item, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == attr_name for t in item.targets
        ):
            continue
        value = item.value
        if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "frozenset":
            value = value.args[0] if value.args else None
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return {
                e.value for e in value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    return set()


def t3_mixin_delegate_surface() -> None:
    print("\n== 3. 状态栏 Mixin 的属性消费面有 __getattr__ 实时委托兜底 ==")

    # Mixin（方法体零改动纪律）以裸 self.<attr> 读的投影名集合
    mixin_attrs: set[str] = set()
    for fn in sorted(os.listdir(_STATUSBAR_DIR)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(_read(os.path.join(_STATUSBAR_DIR, fn)))
        mixin_attrs |= _self_attr_loads(tree)
    accessor_names = set(_ALIASES.values()) | {
        "wr_max_entries", "refusal_enabled", "refusal_patterns",
        "status_bar_enabled", "status_bar_format_plain", "status_bar_plain_platforms",
        "status_bar_plot_paths", "status_bar_default_placeholder",
        "status_bar_show_delta", "show_inject_report",
        "rag_enable_chat_logging", "rag_chat_log_retention_days",
        "worldbook_always_activate",
    }
    consumed = mixin_attrs & accessor_names
    _assert(
        len(consumed) > 0,
        f"状态栏 Mixin 消费到投影名（{sorted(consumed)}）",
    )

    props_tree = ast.parse(_read(_PROPS_PY))
    props_cls = _find_class(props_tree, "QuillConfigProperties")
    props_accessors = set(_class_properties(props_cls)) if props_cls else set()

    tree = ast.parse(_read(_MAIN_PY))
    plugin_cls = _find_class(tree, "QuillPlugin")
    _assert(plugin_cls is not None, "找到 QuillPlugin 类")
    if plugin_cls is None:
        return

    delegated = _class_frozenset_literal(plugin_cls, "_MIXIN_CONFIG_ATTRS")
    _assert(
        bool(delegated),
        "QuillPlugin 声明了 _MIXIN_CONFIG_ATTRS 委托清单",
    )

    missing = sorted(consumed - delegated)
    _assert(
        not missing,
        "Mixin 消费到的每个投影名都在委托清单里",
        f"缺失: {missing}",
    )

    extra = sorted(delegated - props_accessors)
    _assert(
        not extra,
        "委托清单里的每个名字都是 props 访问器（转发目标有效，不留第二真源）",
        f"悬空: {extra}",
    )

    # __getattr__ 方法体必须经实例字典上的 props 转发（且防 self.props 递归）
    getattr_fn = None
    for item in plugin_cls.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__getattr__":
            getattr_fn = item
    _assert(getattr_fn is not None, "QuillPlugin 定义了 __getattr__ 兜底")
    if getattr_fn is not None:
        # ast.unparse 统一用单引号，归一后再做文本断言
        body_src = ast.unparse(getattr_fn).replace("'", '"')
        _assert(
            'self.__dict__.get("props")' in body_src,
            "__getattr__ 经实例字典取 props（防 self.props 递归）",
        )
        _assert(
            'getattr(props, name)' in body_src,
            "__getattr__ 把名字转发到同名 props 访问器",
        )


def t4_save_rollback_semantics() -> None:
    print("\n== 4. save_plugin_configs 新语义（引用替换 + Retriever 回滚）==")

    tree = ast.parse(_read(_MAIN_PY))
    save_fn = _find_func(tree, "save_plugin_configs")
    _assert(save_fn is not None, "找到 save_plugin_configs()")
    if save_fn is None:
        return

    # 内层 try（带 except Exception 的那个）
    inner_try = None
    for node in ast.walk(save_fn):
        if isinstance(node, ast.Try) and node.handlers:
            inner_try = node
    _assert(inner_try is not None, "找到内层 try（保存事务体）")
    if inner_try is None:
        return

    # 快照必须建在 try 之外（引用回滚在 try 内任何一步失败时都要用到）
    snap_line = None
    for node in ast.walk(save_fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "previous_config" for t in node.targets):
            continue
        # 值形如 self.config（Attribute），而非普通 Name
        if isinstance(node.value, ast.Attribute) and node.value.attr == "config":
            snap_line = node.lineno
    _assert(
        snap_line is not None and snap_line < inner_try.lineno,
        "旧 config 引用快照（previous_config = self.config）建在内层 try 之外",
    )
    prev_retr = None
    for node in ast.walk(save_fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_retriever_prev" for t in node.targets
        ):
            prev_retr = node.lineno
            break
    _assert(
        prev_retr is not None and prev_retr < inner_try.lineno,
        "_retriever_prev 预置在 try 之外（修复旧版在早期失败时 except 内 NameError）",
    )

    # 成功路径：整体替换 config 引用（props 实时读自动生效）
    rebuild = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute)
            and isinstance(t.value, ast.Name) and t.value.id == "self"
            and t.attr == "config"
            for t in node.targets
        )
        and isinstance(node.value, ast.Call)
        for node in ast.walk(inner_try)
    )
    _assert(rebuild, "成功路径重建 self.config（QuillConfig 整体替换）")

    # 失败路径：恢复旧引用（而非重建），Retriever 三字段回滚保留
    handler = inner_try.handlers[0]
    handler_src = ast.unparse(handler)
    _assert(
        "self.config = previous_config" in handler_src,
        "回滚恢复旧 config 引用（不重建）",
    )
    _assert(
        all(
            s in handler_src
            for s in ("rag_retriever.top_k", "rag_retriever.enable_memory", "rag_retriever.config")
        ),
        "Retriever 三字段（top_k/enable_memory/config）回滚保留",
    )
    _assert("raise" in handler_src, "回滚后 re-raise（外层统一脱敏返回）")

    # 落盘调用保留
    body_src = ast.unparse(inner_try)
    _assert("save_config()" in body_src, "AstrBotConfig.save_config() 落盘调用保留")
    _assert("_spawn_rag_reinit" in body_src, "embedding 变更触发 RAG 重初始化保留")


def main() -> int:
    print("=== 配置访问器覆盖完整性守卫（M2.3） ===")
    t1_accessor_coverage()
    t2_projection_eliminated()
    t3_mixin_delegate_surface()
    t4_save_rollback_semantics()

    print(f"\n=== 结果: {_PASSED} passed, {_FAILED} failed ===")
    if _FAILURES:
        print("\n失败项：")
        for f in _FAILURES:
            print("  - " + f)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
