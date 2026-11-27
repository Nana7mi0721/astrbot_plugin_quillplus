# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worldbook manager — JSON file storage with persona/user bindings.

Ported from intimate_send v5.0 with three bug fixes:
  #1: get_worldbook() returns copy.deepcopy() (was direct reference)
  #2: _validate_name() rejects path-traversal / invalid characters
  #3: import_from_st() coerces keys to list (was trusting ST format)
"""

import copy
import json
import os
import re
import tempfile
import threading
from typing import Dict, List, Optional


# ── 原子写入辅助函数（纠偏1: os.replace(tmp_path, path)）──
def _save_json_atomic(path: str, data: dict):
    """原子写入 JSON 文件：tmp + fsync + os.replace，防止写入中断导致文件损坏。

    S2-8 修复：补充 fsync 确保数据落盘，异常时清理 tmp 文件。
    复用 persona_manager._sync_write_file 的模式。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件做自测：先把 AstrBot 加入可导入路径
    # 包内加载走相对导入；`python <file>` 时无父包，退回顶层导入
    try:
        from ._astrbot_bootstrap import ensure_astrbot_importable
    except ImportError:
        from _astrbot_bootstrap import ensure_astrbot_importable
    ensure_astrbot_importable()
    from astrbot.api import logger

# M1b 模块 6：条目条件解锁的安全求值器（纯数据入出，无 astrbot 依赖，
# 与 logger 的引导无关，两种运行模式都可直接导入）
try:
    from .quill.services import condition as _condition_mod
    from .quill.services import decorators as _decorators_mod
    from .quill.services import regex_safety as _regex_mod
except ImportError:  # `python worldbook.py` 自测：无父包
    from quill.services import condition as _condition_mod
    from quill.services import decorators as _decorators_mod
    from quill.services import regex_safety as _regex_mod

# M5.1：规则状态与匹配逻辑分离。状态模块只依赖标准库，便于单文件自测。
try:
    from .quill.services.worldbook_rules import WorldbookRuleStore
except ImportError:  # `python worldbook.py` 自测：无父包
    from quill.services.worldbook_rules import WorldbookRuleStore

# ── Name validation ──────────────────────────────────────────────────────────

_VALID_NAME_RE = re.compile(r'[a-zA-Z0-9_\-\u4e00-\u9fff]')

#: Windows 保留设备名（L5）。纯字母名会通过 _VALID_NAME_RE，但 CON.json /
#: NUL.json 这类路径在 Windows 上指向设备而非文件：写入静默失败，
#: 而内存缓存里那本世界书还在 → 重启后凭空消失。quill/core/paths.sanitize_name
#: 已有同样一份名单，这里补上以保持两个入口口径一致。
_WIN_RESERVED_NAMES = frozenset(
    {'con', 'prn', 'aux', 'nul'}
    | {f'com{i}' for i in range(1, 10)}
    | {f'lpt{i}' for i in range(1, 10)}
)


def _validate_name(name: str) -> bool:
    """Return True if *name* is a safe worldbook identifier.

    Rejects empty names, path traversal fragments (``..``, ``/``, ``\\``),
    Windows reserved device names, and any character outside
    [a-zA-Z0-9_\\-\\u4e00-\u9fff].
    """
    if not name:
        return False
    if '..' in name or '/' in name or '\\' in name:
        return False
    if name.strip().lower() in _WIN_RESERVED_NAMES:
        return False
    # After removing allowed chars, nothing should remain
    return not _VALID_NAME_RE.sub('', name)


# ── Manager ──────────────────────────────────────────────────────────────────

class WorldbookManager:
    #: M15 灵敏度分档边界，与 `_conf_schema.json` 的滑块说明一一对应。
    #: <0.3 严格、0.3–0.7 平衡（默认 0.7）、>0.7 宽松。
    _SENS_STRICT_MAX = 0.3
    _SENS_LOOSE_MIN = 0.7
    #: 宽松档里「多词关键词」的切分符（中英混排时都可能出现）
    _KEY_SEG_SPLIT_RE = re.compile(r'[\s,，、/|;；:：·\-—]+')

    def __init__(self, worldbooks_dir: str, rules_path: Optional[str] = None):
        self.worldbooks_dir: str = worldbooks_dir
        os.makedirs(worldbooks_dir, exist_ok=True)
        self.worldbooks: Dict[str, dict] = {}
        # 最近一次注入的触发日志（show_trigger_log 开启时使用）
        self._last_trigger_log: List[dict] = []
        self._lock = threading.Lock()
        # M5.1：定时效应状态落插件数据目录；测试/旧调用不传则纯内存，
        # 新字段缺省时仍按旧行为不启用定时规则。
        self.rules = WorldbookRuleStore(rules_path)
        # M5.1：递归扫描的默认关闭；由 main.save_plugin_configs 按配置热更新。
        # 这里保留普通属性而不是直接读 config，保证 quill/ 纯服务层零宿主依赖。
        self.recursion_enabled = False
        self.recursion_max_steps = 3
        self.recursion_max_tokens = 2000
        self._load_all()

    # ── persistence ──────────────────────────────────────────────────────

    def _load_all(self):
        """Load every ``*.json`` from *worldbooks_dir*."""
        self.worldbooks.clear()
        for f in os.listdir(self.worldbooks_dir):
            if not f.endswith('.json'):
                continue
            path = os.path.join(self.worldbooks_dir, f)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    wb = json.load(fh)
                # M10：此前只校验 name，文件里任何结构异常都留到后面某处才炸。
                # 典型：JSON 根是数组（手改/导出工具产物）→ `wb.get` 直接
                # AttributeError，该世界书在本进程内**整体不可用**，且报错
                # 点在 `_load_all` 之外时连日志都没有。这里统一做结构归一：
                # 根必须是对象、entries 必须是列表，缺则补空。
                if not isinstance(wb, dict):
                    logger.warning(
                        "[WorldbookManager] 跳过 %s：JSON 根应为对象，实际为 %s",
                        f, type(wb).__name__,
                    )
                    continue
                if wb.get('entries') is None:
                    wb['entries'] = []
                elif not isinstance(wb['entries'], list):
                    # 个别导出工具会把 entries 写成「下标 → 条目」的对象形态
                    if isinstance(wb['entries'], dict):
                        wb['entries'] = [v for v in wb['entries'].values() if isinstance(v, dict)]
                    else:
                        logger.warning(
                            "[WorldbookManager] %s 的 entries 类型异常（%s），按空处理",
                            f, type(wb['entries']).__name__,
                        )
                        wb['entries'] = []
                name = wb.get('name', f.replace('.json', ''))
                # M10：文件里的 name 也可能是数字/null（手改产物）。不归一的话
                # `_validate_name` 里的 `'..' in name` 会对 int 抛 TypeError，
                # 整本在这一轮启动里消失。
                if not isinstance(name, str):
                    name = f.replace('.json', '')
                # 拒绝包含路径穿越/非法字符的名字
                if not _validate_name(name):
                    logger.warning("[WorldbookManager] Skipping %s: invalid name %r", f, name)
                    continue
                # L10：以文件内的 name 为键时，两本世界书可能撞同一个键（同名文件
                # 后者静默覆盖前者），文件 a.json 里写 name:"b" 也会让运行时行为
                # 与文件名脱节（delete "b" 删的是 b.json，a.json 下次启动又把它带
                # 回来）。这里至少把撞键事实记下来，便于排查「世界书不见了」。
                if name in self.worldbooks:
                    logger.warning(
                        "[WorldbookManager] %s 与已加载的世界书同名（name=%r），已覆盖；"
                        "建议把文件内的 name 改成唯一值",
                        f, name,
                    )
                self.worldbooks[name] = wb
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[WorldbookManager] 读取失败 %s: %s", f, exc)
            except Exception as exc:
                logger.error("[WorldbookManager] 加载 %s 时发生未知错误: %s", f, exc, exc_info=True)

    def reload_all(self):
        """重新从磁盘加载全部世界书。供 /wb reload 使用。"""
        with self._lock:
            self._load_all()

    # ── query ────────────────────────────────────────────────────────────

    def get_available_worldbooks(self) -> List[str]:
        """返回所有已加载的世界书名称（供前端多选下拉使用）。"""
        with self._lock:
            return sorted(self.worldbooks.keys())

    def get_trigger_log(self) -> List[dict]:
        """返回最近一次注入的触发日志。"""
        with self._lock:
            return list(self._last_trigger_log)

    def list_worldbooks(self) -> List[str]:
        with self._lock:
            return list(self.worldbooks.keys())

    def get_worldbook(self, name: str) -> Optional[dict]:
        """Return a **deep copy** of the worldbook (bug-fix #1)."""
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return None
            return copy.deepcopy(wb)

    def reload_worldbook(self, name: str) -> bool:
        """Re-read a single worldbook file from disk. Returns True on success."""
        if not _validate_name(name):
            return False
        for f in os.listdir(self.worldbooks_dir):
            if not f.endswith('.json'):
                continue
            path = os.path.join(self.worldbooks_dir, f)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    wb = json.load(fh)
                # M10：reload 走的是与 _load_all 不同的读取路径，同样要挡
                # 「根不是对象」——否则 `wb.get` 抛 AttributeError，reload 静默
                # 失败而缓存里仍留着旧内容，用户以为已重新加载。
                if not isinstance(wb, dict):
                    logger.warning(
                        "[WorldbookManager] reload 跳过 %s：JSON 根应为对象，实际为 %s",
                        f, type(wb).__name__,
                    )
                    continue
                wb_name = wb.get('name', f.replace('.json', ''))
                if wb_name == name:
                    # 与 _load_all 同一套归一，避免 reload 后 entries 缺失/类型错
                    if wb.get('entries') is None:
                        wb['entries'] = []
                    elif isinstance(wb['entries'], dict):
                        wb['entries'] = [v for v in wb['entries'].values() if isinstance(v, dict)]
                    elif not isinstance(wb['entries'], list):
                        wb['entries'] = []
                    with self._lock:
                        self.worldbooks[name] = wb
                    return True
            except (OSError, json.JSONDecodeError) as exc:
                # S3-4: 裸 except pass 会吞掉 JSON 损坏/IO 错误，改为日志便于排查
                logger.warning("[WorldbookManager] reload 读取失败 %s: %s", f, exc)
            except Exception as exc:
                logger.error("[WorldbookManager] reload 加载 %s 时发生未知错误: %s", f, exc, exc_info=True)
        return False

    # ── active / matching entries ────────────────────────────────────────

    @classmethod
    def _min_match_required(cls, sensitivity: float, key_count: int) -> int:
        """按灵敏度算出「至少命中几个关键词」，M15 的三档实现。

        ``key_count`` 是条目**实际可用**的关键词个数（已剔除非字符串/空串），
        因此不会出现「条目只有 1 个关键词却要求命中 2 个 → 永远不触发」的
        死条目：关键词不足时把门槛降到该条目能达到的上限。
        """
        if key_count <= 0:
            return 0
        if sensitivity < cls._SENS_STRICT_MAX:
            # 严格档：关键词少就 2 个，堆到 4 个以上按半数递增（向上取整），
            # 这样「写更多关键词」不再变成「更容易被误触发」。
            need = max(2, (key_count + 1) // 2) if key_count >= 4 else 2
        else:
            need = 1
        return min(need, key_count)

    @classmethod
    def _key_segments(cls, key: str) -> List[str]:
        """把多词关键词切成片段，供宽松档做局部命中。

        只切长度 ≥2 的片段（单字片段在中文里几乎必然误命中，等于把宽松档
        变成「凡事都注入」）。整条 key 本身不重复返回——主匹配路径已经查过。
        """
        low = key.lower()
        out: List[str] = []
        for seg in cls._KEY_SEG_SPLIT_RE.split(low):
            seg = seg.strip()
            if len(seg) >= 2 and seg != low and seg not in out:
                out.append(seg)
        return out

    def get_active_worldbooks(self, bound_worldbooks: Optional[List[str]] = None) -> List[dict]:
        """获取活跃世界书

        Args:
            bound_worldbooks: 来自角色卡的绑定列表
                None = Auto 模式（全库）
                [] = Custom 模式，空列表（什么都不启用）
                ["wb1", "wb2"] = Custom 模式，只启用指定的世界书

        Returns:
            活跃世界书列表
        """
        with self._lock:
            if bound_worldbooks is None:
                # Auto 模式：返回所有世界书
                logger.debug("[Worldbook] Auto 模式：加载所有世界书（%d 个）", len(self.worldbooks))
                # 与 get_worldbook 保持一致：返回深拷贝，防止调用方修改污染内部缓存
                return copy.deepcopy(list(self.worldbooks.values()))
            else:
                # Custom 模式：返回指定的世界书（空列表返回空）
                logger.debug("[Worldbook] Custom 模式：绑定世界书: %s", bound_worldbooks)
                valid_wbs = [n for n in bound_worldbooks if n in self.worldbooks]
                if len(valid_wbs) != len(bound_worldbooks):
                    invalid_wbs = set(bound_worldbooks) - set(valid_wbs)
                    logger.warning("[Worldbook] 以下世界书不存在，已跳过: %s", invalid_wbs)
                return copy.deepcopy([self.worldbooks[n] for n in valid_wbs])

    def get_constant_entries(self, bound_worldbooks: Optional[List[str]] = None,
                             session_vars: Optional[dict] = None,
                             funnel: Optional[dict] = None) -> List[dict]:
        """常驻条目（M1b 模块 6 起：带 ``condition`` 字段的常驻条目变为
        「条件型常驻」——条件成立才注入，条件求值器见 quill/services/
        condition.py；无字段的存量条目行为不变）。

        M1b-Fix P1-d：``funnel`` 传入 dict 时，条件不成立的常驻条目**追加**
        进 ``funnel["filtered"]``（reason 以 condition_unmet 开头）——此前
        静默跳过，面板只能看到常驻数变少不知道是条件拦的。只 append 不覆
        写 candidates/matched（那些是 match_entries 的口径），也不整表替换
        filtered（match_entries 会整体赋值，先跑的一方会被清掉——调用方
        注意调用顺序或各用各的 dict 再合并）。
        """
        entries: List[dict] = []
        for wb in self.get_active_worldbooks(bound_worldbooks):
            for entry in wb.get('entries', []):
                if not (entry.get('enabled', True) and entry.get('is_constant', False)):
                    continue
                cond = entry.get('condition')
                if cond:
                    ok, _detail = _condition_mod.evaluate(str(cond), session_vars)
                    if not ok:
                        if funnel is not None:
                            funnel.setdefault("filtered", []).append({
                                "worldbook": wb.get('name', ''),
                                "title": entry.get('title', ''),
                                "reason": f"condition_unmet({_detail})",
                            })
                        continue
                # M5.3：常驻条目同样支持 `@@depth` / `@@role` 深度注入；
                # 面板上直接选「深度位」（inject_position=3）也走同一分支。
                item = dict(entry)
                deco = _decorators_mod.parse(item.get("content", ""))
                if deco.is_depth_injection:
                    item["content"] = deco.body
                depth_position = self._normalize_inject_position(item)
                if deco.is_depth_injection or depth_position == 3:
                    item["_rule_inject_position"] = 3
                    item["_rule_depth"] = (
                        max(0, int(deco.depth)) if deco.is_depth_injection
                        else self._entry_nonneg_alias(item, "depth", default=4, cap=9999)
                    )
                    item["_rule_role"] = (
                        deco.role if deco.role in ("system", "user", "assistant")
                        else str(item.get("role") or "system")
                    )
                entries.append(item)
        return entries

    # ── M5.1 rule helpers ────────────────────────────────────────────────

    @staticmethod
    def _entry_nonneg(entry: dict, key: str, cap: int = 9999) -> int:
        try:
            value = int(entry.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, min(cap, value))

    @staticmethod
    def _entry_alias(entry: dict, *keys, default=None):
        for key in keys:
            if key in entry and entry.get(key) is not None:
                return entry.get(key)
        return default

    @classmethod
    def _entry_nonneg_alias(cls, entry: dict, *keys, default: int = 0,
                            cap: int = 9999) -> int:
        value = cls._entry_alias(entry, *keys, default=default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, min(cap, number))

    @classmethod
    def _entry_bool_alias(cls, entry: dict, *keys, default: bool = False) -> bool:
        value = cls._entry_alias(entry, *keys, default=default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() not in (
                "", "0", "false", "no", "off", "none", "null"
            )
        return bool(value)

    @staticmethod
    def _entry_rule_id(wb_name: str, entry: dict) -> str:
        return f"{wb_name}:{entry.get('id', '')}"

    @staticmethod
    def _normalize_selective_logic(entry: dict, selective: bool,
                                   has_secondary: bool) -> str:
        raw = str(
            entry.get("selective_logic")
            or entry.get("selectiveLogic")
            or entry.get("key_mode")
            or ""
        ).strip().upper().replace("-", "_")
        legacy = {
            "ANY": "LEGACY_ANY",
            "ALL": "AND_ALL",
            "PRIMARY_AND_SECONDARY": "AND_ANY",
            "PRIMARY_OR_SECONDARY": "LEGACY_OR",
            "SECONDARY_ONLY": "LEGACY_SECONDARY",
        }
        mode = legacy.get(raw, raw)
        if mode not in ("AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL",
                        "LEGACY_ANY", "LEGACY_OR", "LEGACY_SECONDARY"):
            mode = "AND_ANY"
        if not selective or not has_secondary:
            return "LEGACY_ANY"
        return mode

    @staticmethod
    def _normalize_inject_position(entry: dict) -> int:
        raw = entry.get("inject_position", entry.get("position", 2))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            text = str(raw or "").strip().lower()
            if text in ("before_char", "after_char", "constant"):
                return 1
            return 2
        return value if value in (1, 2, 3) else 2

    @staticmethod
    def _estimate_rule_tokens(text: str) -> int:
        # 与 PromptBuilder 的量级口径一致（CJK 约 1 token/字，ASCII 约 0.25）。
        return max(1, (len(text or "") + 1) // 2)

    def _match_keys(self, entry: dict, text: str, sensitivity: float,
                    loose_mode: bool) -> tuple:
        """按条目选择性规则返回 (ok, matched_count, matched_keys, required, error)。

        兼容两套字段：M5 规范字段（secondary_keys / selective /
        selective_logic）与 v5.3.5 过渡字段（keys_secondary / key_mode）。
        ``selective=False`` 时完全忽略次关键词，保持旧世界书行为。
        """
        low = (text or "").lower()
        primary = self._coerce_str_list(
            self._entry_alias(entry, "keys", "key", default=[])
        )
        secondary = self._coerce_str_list(
            self._entry_alias(entry, "secondary_keys", "keys_secondary",
                              "keysecondary", default=[])
        )
        selective = self._entry_bool_alias(entry, "selective", default=False)
        legacy_mode = str(entry.get("key_mode") or "").strip().lower()
        if legacy_mode in ("primary_and_secondary", "primary_or_secondary",
                           "secondary_only", "all"):
            selective = True
        mode = self._normalize_selective_logic(entry, selective, bool(secondary))
        use_regex = self._entry_bool_alias(entry, "use_regex", default=False)
        regex_error = ""

        def _hits(keys: List[str], apply_loose: bool = True) -> List[str]:
            hit: List[str] = []
            for key in keys:
                if use_regex:
                    ok, reason = _regex_mod.search(key, text)
                    if reason:
                        regex_error = reason
                        continue
                    if ok:
                        hit.append(key)
                    continue
                if key.lower() in low:
                    hit.append(key)
                    continue
                if apply_loose:
                    for seg in self._key_segments(key):
                        if seg in low:
                            hit.append(key)
                            break
            return hit

        primary_hits = _hits(primary)
        secondary_hits = _hits(secondary)
        need = self._min_match_required(sensitivity, len(primary))
        # 宽松档标题命中：标题是作者写的主题词，仅补主关键词的缺口。
        if (not use_regex and loose_mode and primary
                and len(primary_hits) < need):
            title = entry.get("title", "")
            if (isinstance(title, str) and len(title.strip()) >= 2
                    and title.strip().lower() in low):
                primary_hits.append(title.strip())
        p_ok = bool(primary) and len(primary_hits) >= need
        s_any = bool(secondary_hits)
        s_all = bool(secondary) and len(secondary_hits) == len(secondary)

        if mode == "AND_ALL":
            ok = p_ok and s_all
            required = need + len(secondary)
        elif mode == "NOT_ANY":
            ok = p_ok and not s_any
            required = need
        elif mode == "NOT_ALL":
            ok = p_ok and not s_all
            required = need
        elif mode == "LEGACY_OR":
            ok = p_ok or s_any
            required = need if primary else 1
        elif mode == "LEGACY_SECONDARY":
            ok = s_any
            required = 1
        elif mode == "LEGACY_ANY":
            ok = p_ok or (not primary and s_any)
            required = need if primary else (1 if secondary else 0)
        else:  # AND_ANY
            ok = p_ok and s_any
            required = need + 1

        matched_keys = primary_hits + [
            k for k in secondary_hits if k not in primary_hits
        ]
        return ok, len(matched_keys), matched_keys, required, regex_error

    def _rule_gate(self, wb_name: str, entry: dict, round_no: int,
                   scope_id: str, persona_id: str, simulate: bool = False):
        """定时效应闸门：返回 (state, rule_id, block_reason)。

        只读；推进只发生在 confirm_rule_injection。没有 scope/persona 或
        没有轮次时返回 (None, rule_id, None)，新字段保持旧行为。
        """
        rule_id = self._entry_rule_id(wb_name, entry)
        if not (scope_id or persona_id) or round_no <= 0:
            return None, rule_id, None
        # simulate 只读：不能为了看结果给条目补 first_seen 并落盘。
        state = self.rules.entry_state(scope_id, persona_id, rule_id, round_no,
                                       create=not simulate)
        if state is None:
            return None, rule_id, None
        delay = self._entry_nonneg_alias(
            entry, "delay", "delay_rounds", default=0, cap=999
        )
        if delay and round_no < delay:
            return state, rule_id, f"delay({delay - round_no}轮)"
        # 粘性期内必须放行：cooldown_until 是从「粘性结束」起算的（见
        # mark_injected），若先判冷却，粘性轮会被自己的冷却整段挡掉，
        # 表现为「粘性从未生效、冷却却已消耗」。
        sticky_until = int(state.get("sticky_until", 0) or 0)
        if round_no > 0 and round_no <= sticky_until:
            return state, rule_id, None
        cooldown = int(state.get("cooldown_until", 0) or 0)
        if cooldown >= round_no:
            return state, rule_id, f"cooldown({cooldown - round_no + 1}轮)"
        return state, rule_id, None

    def confirm_rule_injection(self, entries: List[dict]) -> int:
        """条目实际进入 prompt 后提交粘性/冷却；模拟与预算丢弃不调用。"""
        committed = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            scope_id = str(entry.get("_rule_scope") or "")
            persona_id = str(entry.get("_rule_persona") or "")
            rule_id = str(entry.get("_rule_id") or "")
            if not (scope_id or persona_id) or not rule_id:
                continue
            self.rules.mark_injected(
                scope_id, persona_id, rule_id,
                int(entry.get("_rule_round", 0) or 0),
                int(entry.get("_rule_sticky_rounds", 0) or 0),
                int(entry.get("_rule_cooldown_rounds", 0) or 0),
                carry=(entry.get("_rule_reason") == "sticky"),
            )
            committed += 1
        return committed

    def rule_status(self, scope_id: str, persona_id: str,
                    worldbook: Optional[str] = None) -> dict:
        """给面板/诊断返回当前轮次与各规则剩余轮数（只读）。"""
        data = self.rules.snapshot(scope_id or "", persona_id or "")
        if worldbook:
            prefix = f"{worldbook}:"
            data["rules"] = {
                key: value for key, value in data.get("rules", {}).items()
                if key.startswith(prefix)
            }
        return data

    def match_entries(self, user_input: str,
                      bound_worldbooks: Optional[List[str]] = None,
                      top_k: int = 0,
                      sensitivity: float = 0.7,
                      session_vars: Optional[dict] = None,
                      funnel: Optional[dict] = None,
                      write_trigger_log: bool = True,
                      scope_id: str = "",
                      persona_id: str = "",
                      turn_token: str = "",
                      simulate: bool = False,
                      recent_messages: Optional[List[str]] = None,
                      current_input: str = "",
                      recursion_enabled: Optional[bool] = None,
                      recursion_max_steps: Optional[int] = None,
                      recursion_max_tokens: Optional[int] = None,
                      trigger_log_out: Optional[list] = None) -> List[dict]:
        """Return keyword-matched entries (substring, case-insensitive).

        Entries already marked ``is_constant`` are skipped — those come from
        ``get_constant_entries()`` instead.

        M15：灵敏度此前**只有两档**（`<0.3` 要 2 个命中，其余全走同一分支），
        于是面板上把滑块从 0.3 拖到 0.7 再到 1.0 注入结果完全一样 ——
        而配置 schema 的说明明确承诺「0.0 = 极其严格，1.0 = 极其宽松」。
        现在按 schema 承诺落成三档真实行为：
        - 严格（<0.3）：至少 2 个关键词命中；关键词多（≥4）的条目按半数递增，
          避免「关键词堆得越多越容易被触发」。
        - 平衡（0.3–0.7，含默认 0.7）：单个**完整**关键词子串命中即可，
          与旧行为逐字一致（默认档的行为不变，升级不改变既有世界书的表现）。
        - 宽松（>0.7）：在平衡之上再放开两点 —— 多词关键词允许按词片段命中
          （`"魔法 学院"` 命中 `"学院"` 也算），以及条目标题命中。

        M1b 模块 1/6：``session_vars`` 供条目 ``condition`` 字段判定（条件
        不成立的命中条目不注入）；``funnel`` 传入 dict 时回填三阶漏斗诊断
        ——``candidates``（参与匹配的启用量）/``matched``（截帽前命中数）/
        ``filtered``（近失明细：``sensitivity_unmet`` 命中数不足、
        ``condition_unmet`` 条件不成立、``max_entries_cap`` 被 top_k 截帽）。
        两个参数缺省时与旧签名行为逐字一致。

        Args:
            user_input: text to match keywords against
            bound_worldbooks: optional worldbook filter (None = Auto, [] = zero, [...] = Custom)
            top_k: max entries to return (0 = no limit)
            sensitivity: 0.0-1.0，匹配灵敏度。0.0 极其严格，1.0 极其宽松，默认 0.7
            session_vars: 会话变量快照（条件求值用；None 时带条件的条目一律不注入）
            funnel: 可选出参，回填漏斗诊断（None 时零开销）
            write_trigger_log: 是否覆写 ``_last_trigger_log``（默认 True =
                主链路行为；M1b-Fix P1-b：simulate 等只读调用方传 False，
                不再顶掉真实会话的触发日志）
        """
        entries: List[dict] = []
        seen_ids: set = set()
        trigger_log: List[dict] = []
        _candidates = 0
        _filtered: List[dict] = []

        # 获取活跃世界书快照（get_active_worldbooks 内部已加锁）
        active_wbs = self.get_active_worldbooks(bound_worldbooks)

        try:
            sens = float(sensitivity)
        except (TypeError, ValueError):
            sens = 0.7
        # 配置面板滑块是 0.1 步进；越界值按边界档处理而不是当成异常
        if sens < 0.0:
            sens = 0.0
        elif sens > 1.0:
            sens = 1.0
        loose_mode = sens > self._SENS_LOOSE_MIN

        # M5.1：逻辑轮次绑定 scope::persona，不按入口次数。simulate 只读当前
        # 轮；真实链路用 turn_token 幂等推进，避免同一轮被多入口重复计数。
        _scope_id = str(scope_id or "")
        _persona_id = str(persona_id or "")
        round_no = 0
        if _scope_id or _persona_id:
            if simulate:
                round_no = self.rules.current_round(_scope_id, _persona_id)
            elif turn_token:
                round_no = self.rules.begin_turn(_scope_id, _persona_id, str(turn_token))
            else:
                round_no = self.rules.current_round(_scope_id, _persona_id)
        recent_texts = [str(m) for m in (recent_messages or []) if str(m).strip()]
        rec_enabled = (
            self.recursion_enabled if recursion_enabled is None
            else bool(recursion_enabled)
        )
        rec_steps = max(0, min(10, int(
            self.recursion_max_steps if recursion_max_steps is None
            else recursion_max_steps
        )))
        rec_tokens = max(0, int(
            self.recursion_max_tokens if recursion_max_tokens is None
            else recursion_max_tokens
        ))

        def _drop(entry: dict, wb_name: str, reason: str) -> None:
            _filtered.append({
                "worldbook": wb_name,
                "title": entry.get("title", ""),
                "reason": reason,
            })

        def _clear_sticky(entry: dict, wb_name: str) -> None:
            if simulate:
                return
            rule_id = self._entry_rule_id(wb_name, entry)
            try:
                if self.rules.entry_state(
                        _scope_id, _persona_id, rule_id, round_no, create=False):
                    self.rules.clear_sticky(_scope_id, _persona_id, rule_id)
            except Exception:
                logger.debug("[WorldbookManager] 清理粘性失败", exc_info=True)

        def _enrich(result_entry: dict, entry: dict, wb_name: str,
                    rule_id: str, matched_count: int, matched_keys: list,
                    reason: str, depth_override=None) -> dict:
            # M5.3：`@@depth` / `@@role` 装饰器决定「按消息深度注入」。从正文
            # 里剥离控制行，避免它们被拼进 system prompt；未知 @@ 指令原样保留。
            deco = _decorators_mod.parse(result_entry.get("content", ""))
            if deco.is_depth_injection:
                depth_override = deco.depth
                result_entry["content"] = deco.body
            inject_position = self._normalize_inject_position(entry)
            if depth_override is not None:
                inject_position = 3
                depth = max(0, int(depth_override))
            else:
                depth = self._entry_nonneg_alias(
                    entry, "depth", default=4, cap=9999
                )
            role = str(deco.role or entry.get("role") or "system").strip().lower()
            if role not in ("system", "user", "assistant"):
                role = "system"
            result_entry["_match_score"] = matched_count
            result_entry["_matched_keys"] = matched_keys
            result_entry["_source_worldbook"] = wb_name
            result_entry["_rule_id"] = rule_id
            result_entry["_rule_scope"] = _scope_id
            result_entry["_rule_persona"] = _persona_id
            result_entry["_rule_round"] = round_no
            result_entry["_rule_sticky_rounds"] = self._entry_nonneg_alias(
                entry, "sticky", "sticky_rounds", default=0, cap=999
            )
            result_entry["_rule_cooldown_rounds"] = self._entry_nonneg_alias(
                entry, "cooldown", "cooldown_rounds", default=0, cap=999
            )
            result_entry["_rule_delay_rounds"] = self._entry_nonneg_alias(
                entry, "delay", "delay_rounds", default=0, cap=999
            )
            result_entry["_rule_group"] = str(entry.get("group") or "")
            result_entry["_rule_group_max"] = self._entry_nonneg_alias(
                entry, "group_max", default=1, cap=999
            )
            result_entry["_rule_group_weight"] = self._entry_nonneg_alias(
                entry, "group_weight", default=100, cap=9999
            )
            result_entry["_rule_priority"] = self._entry_nonneg_alias(
                entry, "priority", "order", default=100, cap=9999
            )
            result_entry["_rule_recursive"] = self._entry_bool_alias(
                entry, "recursive", default=False
            )
            result_entry["_rule_exclude_recursion"] = self._entry_bool_alias(
                entry, "exclude_recursion", default=False
            )
            result_entry["_rule_prevent_recursion"] = self._entry_bool_alias(
                entry, "prevent_recursion", default=False
            )
            result_entry["_rule_reason"] = reason
            result_entry["_rule_inject_position"] = inject_position
            result_entry["_rule_depth"] = depth
            result_entry["_rule_role"] = role
            result_entry["_rule_regex"] = self._entry_bool_alias(
                entry, "use_regex", default=False
            )
            return result_entry

        for wb in active_wbs:
            wb_name = wb.get('name', '')
            for entry in wb.get('entries', []):
                rule_id = self._entry_rule_id(wb_name, entry)
                enabled = self._entry_bool_alias(entry, "enabled", default=True)
                if not enabled:
                    _clear_sticky(entry, wb_name)
                    continue
                if entry.get('is_constant', False):
                    continue
                primary_keys = self._coerce_str_list(
                    self._entry_alias(entry, "keys", "key", default=[])
                )
                secondary_keys = self._coerce_str_list(
                    self._entry_alias(entry, "secondary_keys", "keys_secondary",
                                      "keysecondary", default=[])
                )
                if not primary_keys and not secondary_keys:
                    continue
                _candidates += 1

                state, rule_id, block_reason = self._rule_gate(
                    wb_name, entry, round_no, _scope_id, _persona_id, simulate)
                if block_reason:
                    _drop(entry, wb_name, block_reason)
                    continue
                sticky_until = int((state or {}).get('sticky_until', 0) or 0)
                sticky_active = bool(state and round_no > 0 and round_no <= sticky_until)

                # 扫描窗口：0/缺省维持旧行为（调用方传入的 context_text，
                # 即最近 4 条）；>0 时改为「本轮原文 + 最近 N 条」。
                scan_window = self._entry_nonneg_alias(
                    entry, "scan_depth", "scan_window", default=0, cap=999
                )
                match_text = user_input
                if scan_window and recent_texts:
                    match_text = (current_input or user_input) + "\n" + "\n".join(
                        recent_texts[-scan_window:])

                (matched_ok, matched_count, matched_keys,
                 min_match_required, regex_error) = self._match_keys(
                    entry, match_text, sens, loose_mode
                )
                if regex_error:
                    _drop(entry, wb_name, regex_error)
                    continue
                if not matched_ok and not sticky_active:
                    # M1b 漏斗：近失（碰到了关键词但不够门槛）——此前静默丢弃，
                    # 面板只能看到「没命中」不知道差多少。零命中不算近失。
                    if matched_count > 0:
                        _drop(
                            entry, wb_name,
                            f"sensitivity_unmet(命中{matched_count}/需{min_match_required})",
                        )
                    continue
                # M1b 模块 6：条件解锁——关键词过了还要条件成立；粘性也不绕条件。
                cond = entry.get('condition')
                if cond:
                    cond_ok, cond_detail = _condition_mod.evaluate(str(cond), session_vars)
                    if not cond_ok:
                        _clear_sticky(entry, wb_name)
                        _drop(entry, wb_name, f"condition_unmet({cond_detail})")
                        continue
                entry_id = entry.get('id', '')
                dup_key = f"{wb_name}:{entry_id}"
                if dup_key not in seen_ids:
                    seen_ids.add(dup_key)
                    reason = "sticky" if not matched_ok else "keyword"
                    result_entry = _enrich(
                        entry.copy(), entry, wb_name, rule_id,
                        matched_count, matched_keys, reason,
                    )
                    entries.append(result_entry)
                    trigger_log.append({
                        "worldbook": wb_name,
                        "entry_id": entry_id,
                        "title": entry.get('title', ''),
                        "matched_keys": matched_keys,
                        "score": matched_count,
                        "sticky": reason == "sticky",
                    })

        # M5.1 互斥组先于递归选择：被组规则淘汰的条目不应再作为递归父节点
        # 扩散（P2：此前先递归后分组，落选条目仍能在递归带出命中）。
        def _apply_group_limits(items: List[dict]) -> List[dict]:
            grouped_local: dict = {}
            ungrouped_local: List[dict] = []
            for item in items:
                group = str(item.get('_rule_group') or '')
                if group:
                    grouped_local.setdefault(group, []).append(item)
                else:
                    ungrouped_local.append(item)
            selected_local: List[dict] = []
            for group, members in grouped_local.items():
                limit = max([
                    self._entry_nonneg(m, '_rule_group_max', 999) for m in members
                ] + [1])
                members.sort(key=lambda x: (
                    -int(x.get('_match_score', 0) or 0),
                    -int(x.get('_rule_priority', 0) or 0),
                    -int(x.get('_rule_group_weight', 0) or 0),
                    str(x.get('_rule_id', '')),
                ))
                winner = members[0].get('_rule_id', '') if members else ''
                if limit > 0:
                    for dropped in members[limit:]:
                        _filtered.append({
                            "worldbook": dropped.get('_source_worldbook', ''),
                            "title": dropped.get('title', ''),
                            "reason": f"group_lost({group}; winner={winner})",
                        })
                    members = members[:limit]
                selected_local.extend(members)
            return selected_local + ungrouped_local

        entries = _apply_group_limits(entries)

        # M5.1 有界递归：默认关闭；仅把被选条目的正文当作额外匹配文本，
        # 受 max_steps / max_tokens / visited 三重约束，路径写进 _recursion_path。
        if entries and rec_enabled and rec_steps > 0 and rec_tokens > 0:
            seen_ids = {f"{e.get('_source_worldbook', '')}:{e.get('id', '')}" for e in entries}
            queue = [
                (e, 1) for e in entries
                if not e.get('_rule_prevent_recursion')
            ]
            scan_budget = 64
            token_budget = rec_tokens
            while queue and scan_budget > 0 and token_budget > 0:
                parent, depth = queue.pop(0)
                if depth > rec_steps:
                    continue
                content = str(parent.get('content') or '')[:4000]
                if not content:
                    continue
                for wb in active_wbs:
                    wb_name = wb.get('name', '')
                    for entry in wb.get('entries', []):
                        if scan_budget <= 0:
                            break
                        if not entry.get('enabled', True) or entry.get('is_constant', False):
                            continue
                        if self._entry_bool_alias(
                                entry, "exclude_recursion", default=False):
                            continue
                        rule_id = self._entry_rule_id(wb_name, entry)
                        # 只用「已入选」集合防环：此前把未命中的候选也写进
                        # 跨父共享的 visited，导致父条目 A 没匹配上的 C 被永久
                        # 拉黑，父条目 B 即使含 C 的关键词也永远触发不到。
                        if rule_id in seen_ids:
                            continue
                        if not (
                            self._coerce_str_list(
                                self._entry_alias(entry, "keys", "key", default=[]))
                            or self._coerce_str_list(
                                self._entry_alias(
                                    entry, "secondary_keys", "keys_secondary",
                                    "keysecondary", default=[]))
                        ):
                            continue
                        scan_budget -= 1
                        state, rule_id, block_reason = self._rule_gate(
                            wb_name, entry, round_no, _scope_id, _persona_id, simulate)
                        if block_reason:
                            continue
                        (matched_ok, matched_count, matched_keys,
                         _need, regex_error) = self._match_keys(
                            entry, content, sens, loose_mode
                        )
                        if regex_error:
                            _drop(entry, wb_name, regex_error)
                            continue
                        if not matched_ok:
                            continue
                        cond = entry.get('condition')
                        if cond:
                            cond_ok, _cond_detail = _condition_mod.evaluate(str(cond), session_vars)
                            if not cond_ok:
                                continue
                        tokens = self._estimate_rule_tokens(
                            str(entry.get('content') or '')
                        )
                        if tokens > token_budget:
                            _drop(entry, wb_name, "recursion_limit(tokens)")
                            token_budget = 0
                            break
                        token_budget -= tokens
                        result_entry = _enrich(
                            entry.copy(), entry, wb_name, rule_id,
                            matched_count, matched_keys, "recursion",
                        )
                        result_entry['_recursion_path'] = list(parent.get('_recursion_path') or [parent.get('_rule_id', '')]) + [rule_id]
                        entries.append(result_entry)
                        seen_ids.add(rule_id)
                        trigger_log.append({
                            "worldbook": wb_name,
                            "entry_id": entry.get('id', ''),
                            "title": entry.get('title', ''),
                            "matched_keys": matched_keys,
                            "score": matched_count,
                            "recursion": True,
                        })
                        if (not self._entry_bool_alias(
                                entry, "prevent_recursion", default=False)
                                and depth < rec_steps):
                            queue.append((result_entry, depth + 1))
                    if scan_budget <= 0:
                        break

        # 递归可能带出同组新条目，入选后再执行一次互斥选择，保证最终
        # 注入集合仍满足 group_max。
        entries = _apply_group_limits(entries)

        # 稳定排序：score 降序、priority 降序、group_weight 降序、rule_id 升序。
        entries.sort(key=lambda x: (
            -int(x.get('_match_score', 0) or 0),
            -int(x.get('_rule_priority', 0) or 0),
            -int(x.get('_rule_group_weight', 0) or 0),
            str(x.get('_rule_id', '')),
        ))
        _matched = len(entries)
        if top_k > 0 and len(entries) > top_k:
            # M1b 漏斗：截帽掉的命中条目同样进 filtered（它们「命中了但没挤进
            # 名额」，与近失是两种不同的失败）。
            for dropped in entries[top_k:]:
                _filtered.append({
                    "worldbook": dropped.get('_source_worldbook', ''),
                    "title": dropped.get('title', ''),
                    "reason": "max_entries_cap",
                })
            entries = entries[:top_k]

        # M5.1：触发日志只留最终入选条目。组互斥/截帽掉的条目已进 filtered，
        # 再写进日志会让 /quill 与 trace 都显示「命中了却没注入」。
        if trigger_log:
            _kept = {(e.get('_source_worldbook', ''), str(e.get('id', '')))
                     for e in entries}
            trigger_log = [t for t in trigger_log
                           if (t.get('worldbook', ''), str(t.get('entry_id', ''))) in _kept]

        # 更新触发日志（show_trigger_log 开启时可供查询，加锁保护）。
        # P1-b：只读调用方（simulate）传 write_trigger_log=False——不顶掉
        # 真实会话刚写的触发日志（/quill test wb 读的就是它）。
        if write_trigger_log:
            with self._lock:
                self._last_trigger_log = trigger_log
        if trigger_log_out is not None:
            trigger_log_out.extend(trigger_log)
        if funnel is not None:
            funnel["candidates"] = _candidates
            funnel["matched"] = _matched
            funnel["filtered"] = _filtered
            funnel["trigger_log"] = trigger_log
        return entries

    # ── CRUD ─────────────────────────────────────────────────────────────

    def create_worldbook(self, name: str, description: str = "") -> bool:
        """Create a new worldbook JSON file. Returns False if name is invalid."""
        if not _validate_name(name):
            return False
        wb = {
            "name": name,
            "description": description,
            "version": "1.0",
            "entries": [],
        }
        path = os.path.join(self.worldbooks_dir, name + ".json")
        _save_json_atomic(path, wb)
        with self._lock:
            self.worldbooks[name] = wb
        return True

    def delete_worldbook(self, name: str) -> bool:
        """Delete a worldbook and clean up all bindings. Thread-safe."""
        if not _validate_name(name):
            return False
        with self._lock:
            if name not in self.worldbooks:
                return False
            path = os.path.join(self.worldbooks_dir, name + ".json")
            if os.path.exists(path):
                os.remove(path)
            del self.worldbooks[name]
        return True

    # ── Atomic entry operations (F1 fix: read-modify-write under single lock) ──

    def add_entry_to_worldbook(self, name: str, entry: dict) -> bool:
        """Atomically add an entry to a worldbook. Thread-safe.

        B6：改「副本 → 落盘 → 换缓存」。此前就地改缓存再落盘，落盘抛错
        （磁盘满、Windows 上文件被 AV/备份占用）时缓存已经变了 —— 面板显示
        改成功、重启后消失。副本失败则缓存保持原样，内存与磁盘不会再分叉。
        """
        if not _validate_name(name):
            return False
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return False
            updated = copy.deepcopy(wb)
            # M10：面板「增条目」曾直接 KeyError: 'entries'——手改过/旧版导出的
            # 世界书文件可以没有 entries 键，而增删改三条路径全都直接下标。
            # _load_all 现在会归一，这里再兜一次，保证运行期任何来源的 wb
            # 都不会把一次「加条目」变成 500。
            updated.setdefault("entries", [])
            updated["entries"].append(entry)
            path = os.path.join(self.worldbooks_dir, name + ".json")
            _save_json_atomic(path, updated)
            self.worldbooks[name] = updated
        return True

    def update_entry_in_worldbook(self, name: str, entry_id: str, patch: dict) -> bool:
        """Atomically update an entry in a worldbook. Thread-safe."""
        if not _validate_name(name):
            return False
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return False
            updated = copy.deepcopy(wb)
            # M10：同 add，缺 entries 键时按空列表处理而不是 KeyError
            updated.setdefault("entries", [])
            for i, item in enumerate(updated["entries"]):
                if not isinstance(item, dict):
                    continue
                if item.get("id") == entry_id:
                    updated["entries"][i].update(patch)
                    # M1b-Fix（核验报告 §9.2#4）：前端条目表单恒传 condition，
                    # 空串语义 = 清除条件。dict.update 做不到删键（旧条件会
                    # 一直残留），这里显式 pop——「清空输入框保存」才真正生效。
                    if "condition" in patch and not str(patch["condition"] or "").strip():
                        updated["entries"][i].pop("condition", None)
                    path = os.path.join(self.worldbooks_dir, name + ".json")
                    _save_json_atomic(path, updated)
                    self.worldbooks[name] = updated
                    return True
            return False

    def delete_entry_from_worldbook(self, name: str, entry_id: str) -> bool:
        """Atomically delete an entry from a worldbook. Thread-safe."""
        if not _validate_name(name):
            return False
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return False
            updated = copy.deepcopy(wb)
            # M10：同 add/update，缺 entries 键时按空列表处理（结果自然是 False）
            updated.setdefault("entries", [])
            before = len(updated["entries"])
            updated["entries"] = [e for e in updated["entries"] if not isinstance(e, dict) or e.get("id") != entry_id]
            if len(updated["entries"]) == before:
                return False
            path = os.path.join(self.worldbooks_dir, name + ".json")
            _save_json_atomic(path, updated)
            self.worldbooks[name] = updated
        return True

    # ── ST Lorebook import ───────────────────────────────────────────────

    @staticmethod
    def _coerce_str_list(raw) -> List[str]:
        """把世界书里「键集合」的各种形态归一成字符串列表。

        H4：ST 生态里 keys/key/keys 的实际类型五花八门（list、逗号分隔字符串、
        单个字符串、null、嵌套 list）。旧实现只判 list/str 两支，其余一律丢成
        空键集——条目还在但永远匹配不上，等于静默失效。这里补上嵌套与
        非字符串元素的处理：数字键转成字符串（ST 里 `"key": 2024` 很常见）。
        """
        out: List[str] = []
        if isinstance(raw, str):
            return [k.strip() for k in raw.split(',') if k.strip()]
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, str):
                    if item.strip():
                        out.append(item.strip())
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    out.append(str(item))
                elif isinstance(item, (list, tuple)):
                    # 嵌套一层的形态（部分导出工具会把主/次键套在一起）
                    out.extend(WorldbookManager._coerce_str_list(item))
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            out.append(str(raw))
        return out

    @staticmethod
    def _safe_inject_position(raw, default: int = 1) -> int:
        """归一注入位置为 1（常驻）/ 2（按需）/ 3（深度）。

        H4：旧写法 `1 if e.get('position', 2) <= 1 else 2` 直接把 JSON 值和
        整数比大小。ST 导出里有 `"position": "0"` 这类字符串取值（也有
        `"before_char"` 这类枚举名），字符串与 int 比较会抛 TypeError，
        **整本**世界书跟着导入失败。这里统一走数值归一，认不出来就退回默认值。
        """
        try:
            value = int(raw)
            return value if value in (1, 2, 3) else default
        except (TypeError, ValueError):
            # 枚举名形态：ST 的 before_char/after_char 都是常驻位
            if isinstance(raw, str) and raw.strip().lower() in ("before_char", "after_char"):
                return default
            return default

    @staticmethod
    def _safe_bool(value, default: bool = False) -> bool:
        """字符串 ``"false"`` / ``"0"`` / ``"off"`` 必须判 False。"""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() not in (
                "", "0", "false", "no", "off", "none", "null"
            )
        return bool(value)

    @classmethod
    def _rule_fields(cls, e: dict) -> dict:
        """M5.1 可选规则的统一落库形态；缺省值严格等价旧行为。"""
        secondary = cls._coerce_str_list(
            e.get("secondary_keys", e.get("keysecondary", []))
        )
        selective = cls._safe_bool(e.get("selective"), bool(secondary))
        logic = str(
            e.get("selective_logic") or e.get("selectiveLogic")
            or e.get("key_mode") or "AND_ANY"
        ).strip().upper().replace("-", "_")
        legacy = {
            "ANY": "AND_ANY",
            "ALL": "AND_ALL",
            "PRIMARY_AND_SECONDARY": "AND_ANY",
            "PRIMARY_OR_SECONDARY": "AND_ANY",
            "SECONDARY_ONLY": "AND_ANY",
        }
        logic = legacy.get(logic, logic)
        if logic not in ("AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL"):
            logic = "AND_ANY"
        position = cls._safe_inject_position(
            e.get("inject_position", e.get("position", 2)), default=2
        )
        role = str(e.get("role") or "system").strip().lower()
        if role not in ("system", "user", "assistant"):
            role = "system"
        return {
            "secondary_keys": secondary,
            "selective": selective,
            "selective_logic": logic,
            "sticky": cls._entry_nonneg_alias(e, "sticky", "sticky_rounds", default=0, cap=999),
            "cooldown": cls._entry_nonneg_alias(e, "cooldown", "cooldown_rounds", default=0, cap=999),
            "delay": cls._entry_nonneg_alias(e, "delay", "delay_rounds", default=0, cap=999),
            "group": str(e.get("group") or "")[:100],
            "group_max": cls._entry_nonneg_alias(e, "group_max", default=1, cap=999),
            "group_weight": cls._entry_nonneg_alias(e, "group_weight", default=100, cap=9999),
            "priority": cls._entry_nonneg_alias(e, "priority", "order", default=100, cap=9999),
            "scan_depth": (
                None if e.get("scan_depth") is None
                else cls._entry_nonneg_alias(e, "scan_depth", "scan_window", default=0, cap=999)
            ),
            "exclude_recursion": cls._safe_bool(e.get("exclude_recursion"), False),
            # 旧 `recursive=true` 语义是「允许递归」，与 `prevent_recursion`
            # 相反。只有条目显式带 `recursive` 且未声明 `prevent_recursion`
            # 时才取反；否则默认 False（允许递归，但仍受全局开关约束）。
            "prevent_recursion": cls._safe_bool(
                e.get("prevent_recursion"),
                (not cls._safe_bool(e.get("recursive"), False))
                if e.get("recursive") is not None else False,
            ),
            "use_regex": cls._safe_bool(e.get("use_regex"), False),
            "inject_position": position,
            "depth": cls._entry_nonneg_alias(e, "depth", default=4, cap=9999),
            "role": role,
        }

    def _map_quill_entry(self, e: dict, index: int) -> dict:
        """Quill 原生条目 → 内部条目。字段缺省或类型异常都不抛，按兜底值走。"""
        mapped = {
            # L8：id 可能是数字（round-trip 过 ST 工具/hand-edit 后很常见），
            # 直接 [:50] 会 TypeError；而异常被外层宽 except 吞掉 ⇒ 不是
            # 跳过这一条，而是**整本**世界书导入失败。
            "id": str(e.get('id') or f"entry_{index}")[:50],
            "title": str(e.get('title', '') or ''),
            "content": str(e.get('content', '') or ''),
            "is_constant": self._safe_bool(e.get('is_constant'), False),
            "inject_position": self._safe_inject_position(e.get('inject_position', 1)),
            "keys": self._coerce_str_list(e.get('keys', [])),
            "enabled": self._safe_bool(e.get('enabled'), True),
        }
        mapped.update(self._rule_fields(e))
        # P1-d：condition 只在有值时落键（空串等价于无条件，与存量条目
        # 的键形态保持一致）。
        cond = str(e.get('condition', '') or '').strip()
        if cond:
            mapped["condition"] = cond
        return mapped

    def _map_st_entry(self, e: dict, index: int) -> dict:
        """SillyTavern 条目 → 内部条目。

        H4：三个历史崩溃点都在这里——`e.get('comment','')[:50]`（comment 是数字
        时 `'int' object is not subscriptable`）、`e.get('position',2) <= 1`
        （position 是字符串时 `'<=' not supported between 'str' and 'int'`）、
        以及 keys 拼接。三者都会把异常抛到外层宽 except，导致**一条畸形条目
        废掉整本世界书**。现在统一先归一类型再取值。
        """
        keys_raw = self._coerce_str_list(e.get('key') or e.get('keys', []))
        secondary_raw = self._coerce_str_list(
            e.get('keysecondary') or e.get('secondary_keys') or []
        )

        # comment/uid 都可能是数字；title 与 id 都以字符串形态落库
        comment = e.get('comment', '')
        title = '' if comment is None else str(comment)
        uid = e.get('uid', '')
        # 先定 id：comment 优先，退回 uid（数字 uid 也要 str 化再截断），
        # 两者都没有才用序号兜底（序号用 index 而非 len(entries)，
        # 避免同一本里出现重复 id）
        eid_source = title or ('' if uid is None else str(uid))
        entry_id = eid_source[:50] if eid_source else f"st_import_{index}"

        mapped = {
            "id": entry_id,
            "title": title,
            "content": str(e.get('content', '') or ''),
            "is_constant": self._safe_bool(e.get('constant'), False),
            "inject_position": self._safe_inject_position(e.get('position', 2), default=2),
            "keys": keys_raw,
            "enabled": not self._safe_bool(e.get('disable'), False),
        }
        mapped.update(self._rule_fields({**e, "secondary_keys": secondary_raw}))
        # P1-d：ST 导出（handle_wb_export_st）会把 condition 写进条目，
        # 本插件导出的 ST 卡 re-import 时条件不丢；原生 ST 无此字段，
        # 缺省即无条件，不受影响。
        cond = str(e.get('condition', '') or '').strip()
        if cond:
            mapped["condition"] = cond
        return mapped

    def import_from_st(self, st_json_path: str, worldbook_name: str) -> bool:
        """Import a worldbook JSON file. Auto-detects format:
        - Quill native format: {"entries": [{id, title, content, keys, is_constant, ...}]}
        - SillyTavern format: {"entries": {"0": {key, content, constant, ...}}}
        """
        if not _validate_name(worldbook_name):
            return False
        try:
            with open(st_json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # H4：根不是对象就直接判失败。此前 `data.get` 会在 JSON 根是数组
            # （ST 工具有导成裸 entries 数组的形态）时抛 AttributeError，
            # 被外层宽 except 吞成「Import 时发生未知错误」，用户只看到一句
            # 无信息量的失败——现在拿得到准确的失败原因。
            if not isinstance(data, dict):
                logger.warning(
                    "[WorldbookManager] Import 失败: JSON 根应为对象，实际为 %s",
                    type(data).__name__,
                )
                return False

            raw_entries = data.get('entries', [])
            entries: List[dict] = []

            # 检测格式：如果第一个条目包含 Quill 特征字段（content + keys），按 Quill 原生处理
            # L9：dict 形态的 entries 要转成 list 再判类型——此前 `items_list` 是
            # dict_values，`isinstance(..., list)` 恒为 False，Quill 原生格式被误判成
            # ST 格式，is_constant / inject_position 随之丢失（常驻条目不再常驻）。
            if isinstance(raw_entries, dict):
                items_list = list(raw_entries.values())
            elif isinstance(raw_entries, list):
                items_list = raw_entries
            else:
                items_list = []
            if items_list:
                first = items_list[0] if isinstance(items_list[0], dict) else {}
                is_quill_native = 'content' in first and ('keys' in first or 'is_constant' in first)
            else:
                is_quill_native = False

            if is_quill_native:
                # Quill 原生格式 → 直接映射字段
                for e in items_list:
                    # H4：把「这一条能不能用」与「整本能不能进」解耦。此前任何一条
                    # 触发异常（字段类型不对、取值越界）都会冒泡到外层宽 except，
                    # 结果是**整本**世界书导入失败——一条畸形条目废掉一本是用户
                    # 无法接受的失败放大。现在逐条 try：坏条目跳过并留日志，
                    # 好条目照常进，最后若一条都没成就返回 False。
                    if not isinstance(e, dict) or not e.get('content'):
                        continue
                    try:
                        entry = self._map_quill_entry(e, len(entries))
                    except Exception as exc:
                        logger.warning(
                            "[WorldbookManager] 跳过无法解析的 Quill 条目 #%d: %s",
                            len(entries), exc,
                        )
                        continue
                    entries.append(entry)
            else:
                # SillyTavern 格式
                items = raw_entries.values() if isinstance(raw_entries, dict) else raw_entries if isinstance(raw_entries, list) else []
                for e in items:
                    if not isinstance(e, dict):
                        continue
                    # H4：无正文的条目没有任何可注入内容，落库只会变成
                    # 「匹配得上但什么都不注入」的幽灵条目。与 Quill 原生分支
                    # 保持同一判据（那边一直有 content 判空）。
                    if not e.get('content'):
                        continue
                    # H4：同上的逐条隔离。ST 导出工具的字段类型很不统一
                    # （comment 可能是数字、position 可能是字符串 "0"、
                    # key 可能是嵌套结构），旧写法直接下标/比较，一崩就是整本。
                    try:
                        entry = self._map_st_entry(e, len(entries))
                    except Exception as exc:
                        logger.warning(
                            "[WorldbookManager] 跳过无法解析的 ST 条目 #%d: %s",
                            len(entries), exc,
                        )
                        continue
                    entries.append(entry)

            if not entries:
                return False

            # P2-14：重复 entry_id 会让匹配阶段按 wb:id 去重时静默丢条目。
            # 导入/导出往返（尤其 ST 的 comment 当 id）必须保序追加 #k。
            seen_ids: set = set()
            for entry in entries:
                eid = str(entry.get("id") or "")
                if eid in seen_ids:
                    k = 2
                    while f"{eid}#{k}" in seen_ids:
                        k += 1
                    eid = f"{eid}#{k}"
                    entry["id"] = eid
                seen_ids.add(eid)

            # description：优先取 JSON 中的 description，否则根据格式自动生成
            description = data.get('description', '') or ("Imported from ST Lorebook" if not is_quill_native else "Imported worldbook")

            wb = {
                "name": worldbook_name,
                "description": description,
                "version": "1.0",
                "entries": entries,
            }
            path = os.path.join(self.worldbooks_dir, worldbook_name + ".json")
            with self._lock:
                _save_json_atomic(path, wb)
                self.worldbooks[worldbook_name] = wb
            return True
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[WorldbookManager] Import 失败: %s", exc)
            return False
        except Exception as exc:
            logger.error("[WorldbookManager] Import 时发生未知错误: %s", exc, exc_info=True)
            return False

    # ── 角色卡内嵌世界书（Issue #1） ─────────────────────────────────────

    @classmethod
    def _embedded_to_st_entry(cls, e: dict) -> dict:
        """character_book 条目（V2/V3 spec 形态）→ ST 条目形态。

        spec 与 ST 编辑器导出的字段名不同：keys/key、secondary_keys/keysecondary、
        标题在 name 或 comment、enabled 与 disable 语义相反。归一后复用
        _map_st_entry 的类型兜底，不另写一套映射。spec 的注入位在
        extensions.position（ST 导出在顶层），一并提升。
        """
        st = dict(e)
        if "key" not in st and "keys" in st:
            st["key"] = st["keys"]
        if "keysecondary" not in st and "secondary_keys" in st:
            st["keysecondary"] = st["secondary_keys"]
        if not str(st.get("comment") or "").strip():
            st["comment"] = st.get("name") or ""
        # Q4-1 修复：spec 的 `enabled` 与 ST 的 `disable` 语义相反，而 V2/V3 导出
        # 常常**同时**带这两个键（`enabled: false` 是规范字段，`disable: false`
        # 是 ST 编辑器残留的默认值）。旧写法 `if "disable" not in st` 一见 disable
        # 就跳过 enabled，把作者刻意禁用的条目（剧透 / 作者备注）强行启用并注入
        # ——静默泄内容，比报错难查得多。
        #
        # 现在按「任一为禁用即禁用」（OR）：两个字段都只在表达「这条要不要注入」，
        # 冲突时保守取禁用。`enabled is None` 视为未表态，不参与判定。
        if "enabled" in st and st["enabled"] is not None:
            st["disable"] = (
                cls._safe_bool(st.get("disable"))
                or (not cls._safe_bool(st["enabled"], True))
            )
        else:
            # 只有 disable（原生 ST 形态）时统一成 bool：`"false"` 这类字符串脏值
            # 走 `not bool(...)` 会被当成 True，这里一次性收干净。
            st["disable"] = cls._safe_bool(st.get("disable"))
        ext = st.get("extensions")
        if isinstance(ext, dict) and "position" in ext and "position" not in st:
            st["position"] = ext.get("position")
        return st

    def save_embedded_worldbook(self, card_name: str, book) -> Optional[dict]:
        """角色卡内嵌世界书（data.character_book）→ 落盘为羽笔世界书。

        与 import_from_st 共用条目映射（先归一成 ST 形态再走 _map_st_entry）；
        「constant 或无关键词」的条目按常驻位落库（类脑生态用空键条目承载
        必设设定）。书名 = 卡名清洗 + "_内嵌"——`·` 是关键词条的保留分割符，
        世界书名不允许使用；同名覆盖（与 import_from_st 一致，重导入幂等）。
        返回 {"name": 落盘名, "entries": 条数}（覆盖既有同名书时另带
        "replaced_existing": True）；无可用条目返回 None。
        """
        if not isinstance(book, dict):
            return None
        raw_entries = book.get("entries")
        if isinstance(raw_entries, dict):
            items = list(raw_entries.values())
        elif isinstance(raw_entries, list):
            items = raw_entries
        else:
            items = []

        entries: List[dict] = []
        seen_ids: set = set()
        for e in items:
            # 与 import_from_st 同判据：无正文的条目落库只会变成幽灵条目
            if not isinstance(e, dict) or not str(e.get("content") or "").strip():
                continue
            try:
                entry = self._map_st_entry(self._embedded_to_st_entry(e), len(entries))
            except Exception as exc:
                logger.warning("[WorldbookManager] 跳过无法解析的内嵌条目 #%d: %s",
                               len(entries), exc)
                continue
            if entry.get("is_constant") or not entry.get("keys"):
                entry["is_constant"] = True
                entry["inject_position"] = 1
            eid = str(entry.get("id") or f"embedded_{len(entries)}")
            if eid in seen_ids:
                k = 2
                while f"{eid}#{k}" in seen_ids:
                    k += 1
                eid = f"{eid}#{k}"
            entry["id"] = eid
            seen_ids.add(eid)
            entries.append(entry)
        if not entries:
            return None

        raw_name = str(card_name or "").strip() or "角色卡"
        safe = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", raw_name).strip("_")[:60] or "角色卡"
        wb_name = f"{safe}_内嵌"
        wb = {
            "name": wb_name,
            "description": f"由角色卡《{raw_name}》内嵌世界书自动转换（{len(entries)} 条）",
            "version": "1.0",
            "entries": entries,
        }
        path = os.path.join(self.worldbooks_dir, wb_name + ".json")
        replaced = wb_name in self.worldbooks or os.path.exists(path)
        if replaced:
            # 重导入幂等是有意设计；但两张**不同**卡同名时，后导入会连带改写
            # 先导入卡仍绑定的这本书——日志点名，回执带 replaced_existing。
            logger.warning(
                "[WorldbookManager] 内嵌世界书《%s》已存在，本次导入将覆盖原内容"
                "（不同角色卡同名时，先导入卡的绑定会被连带改写）", wb_name)
        with self._lock:
            _save_json_atomic(path, wb)
            self.worldbooks[wb_name] = wb
        info = {"name": wb_name, "entries": len(entries)}
        if replaced:
            info["replaced_existing"] = True
        return info


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    passed = 0
    failed = 0

    def _assert(condition: bool, label: str):
        global passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {label}")
        else:
            failed += 1
            print(f"  [FAIL] {label}")

    with tempfile.TemporaryDirectory(prefix="wb_test_") as tmpdir:
        mgr = WorldbookManager(tmpdir)

        # ── 1. create / list / get ───────────────────────────────────────
        print("\n== CRUD ==")
        _assert(mgr.create_worldbook("test_wb", "A test worldbook"),
                "create_worldbook returns True")
        _assert("test_wb" in mgr.list_worldbooks(), "list_worldbooks contains test_wb")

        wb = mgr.get_worldbook("test_wb")
        _assert(wb is not None, "get_worldbook returns non-None")
        assert wb is not None  # for type checker
        _assert(wb["name"] == "test_wb", "worldbook name matches")

        # ── 2. Bug-fix #1: deep copy ────────────────────────────────────
        print("\n== Bug-fix #1: deep copy ==")
        wb_copy = mgr.get_worldbook("test_wb")
        assert wb_copy is not None
        wb_copy["entries"].append({"id": "hacked", "content": "should not leak"})
        wb_again = mgr.get_worldbook("test_wb")
        assert wb_again is not None
        _assert(len(wb_again["entries"]) == 0,
                "mutating get_worldbook result does NOT affect internal state")

        # ── 3. Bug-fix #2: path-traversal / invalid names ───────────────
        print("\n== Bug-fix #2: name validation ==")
        _assert(not mgr.create_worldbook("../evil", "traversal"),
                "reject ../evil")
        _assert(not mgr.create_worldbook("foo/bar", "slash"),
                "reject foo/bar")
        _assert(not mgr.create_worldbook("bad name!", "special chars"),
                "reject name with !")
        _assert(not mgr.create_worldbook("", "empty"),
                "reject empty name")
        _assert(mgr.create_worldbook("中文书", "chinese name ok"),
                "accept CJK name")
        _assert(mgr.create_worldbook("my-book_v2", "valid ascii"),
                "accept alphanumeric + dash + underscore")

        # ── 4. entries + match ───────────────────────────────────────────
        print("\n== entries & matching ==")
        wb_obj = mgr.worldbooks["test_wb"]
        wb_obj["entries"] = [
            {"id": "e1", "title": "magic", "content": "Magic rule",
             "keys": ["magic", "spell"], "enabled": True, "is_constant": False},
            {"id": "e2", "title": "const", "content": "Always here",
             "keys": [], "enabled": True, "is_constant": True},
            {"id": "e3", "title": "sword", "content": "Sword lore",
             "keys": ["sword", "blade"], "enabled": True, "is_constant": False},
            {"id": "e4", "title": "disabled", "content": "Should not match",
             "keys": ["magic"], "enabled": False, "is_constant": False},
        ]
        # Save to disk so reload works
        path = os.path.join(tmpdir, "test_wb.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(wb_obj, f, ensure_ascii=False, indent=2)

        # Test with Custom mode (bound_worldbooks)
        matches = mgr.match_entries("I cast a magic spell", bound_worldbooks=["test_wb"])
        _assert(len(matches) == 1, f"match_entries returns 1 (got {len(matches)})")
        _assert(matches[0]["id"] == "e1", "matched entry is e1")
        _assert(matches[0]["_match_score"] == 2, "match score is 2 (magic+spell)")

        constants = mgr.get_constant_entries(bound_worldbooks=["test_wb"])
        _assert(len(constants) == 1, f"constant entries = 1 (got {len(constants)})")
        _assert(constants[0]["id"] == "e2", "constant entry is e2")

        # Test Auto mode (bound_worldbooks=None, returns all)
        auto_wbs = mgr.get_active_worldbooks(bound_worldbooks=None)
        _assert(len(auto_wbs) == 3, f"Auto mode returns all worldbooks (got {len(auto_wbs)})")

        # Test Custom mode with empty list (zero injection)
        empty_wbs = mgr.get_active_worldbooks(bound_worldbooks=[])
        _assert(len(empty_wbs) == 0, f"Custom mode with empty list returns zero (got {len(empty_wbs)})")

        # ── 5. reload ────────────────────────────────────────────────────
        print("\n== reload ==")
        _assert(mgr.reload_worldbook("test_wb"), "reload existing returns True")
        _assert(not mgr.reload_worldbook("nonexistent"), "reload missing returns False")
        _assert(not mgr.reload_worldbook("../evil"), "reload traversal returns False")

        # ── 6. delete ────────────────────────────────────────────────────
        print("\n== delete ==")
        _assert(mgr.delete_worldbook("中文书"), "delete CJK worldbook succeeds")
        _assert("中文书" not in mgr.list_worldbooks(), "deleted wb not in list")

        # ── 7. Bug-fix #3: ST import keys coercion ──────────────────────
        print("\n== Bug-fix #3: ST import keys ==")
        st_path = os.path.join(tmpdir, "st_lore.json")
        st_data = {
            "entries": {
                "0": {
                    "comment": "test entry",
                    "content": "some lore",
                    "key": "fire,flame",   # string, NOT list
                    "constant": False,
                    "disable": False,
                    "position": 2,
                    "uid": 0,
                }
            }
        }
        with open(st_path, 'w', encoding='utf-8') as f:
            json.dump(st_data, f)

        _assert(mgr.import_from_st(st_path, "st_import"),
                "import_from_st succeeds")
        imported = mgr.get_worldbook("st_import")
        _assert(imported is not None, "imported worldbook exists")
        assert imported is not None  # for type checker
        imported_keys = imported["entries"][0]["keys"]
        _assert(isinstance(imported_keys, list),
                f"keys is list (got {type(imported_keys).__name__})")
        _assert(imported_keys == ["fire", "flame"],
                f"keys parsed correctly: {imported_keys}")

        _assert(not mgr.import_from_st(st_path, "../evil"),
                "import rejects path-traversal name")

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
