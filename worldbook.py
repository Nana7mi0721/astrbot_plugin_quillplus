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

    def __init__(self, worldbooks_dir: str):
        self.worldbooks_dir: str = worldbooks_dir
        os.makedirs(worldbooks_dir, exist_ok=True)
        self.worldbooks: Dict[str, dict] = {}
        # 最近一次注入的触发日志（show_trigger_log 开启时使用）
        self._last_trigger_log: List[dict] = []
        self._lock = threading.Lock()
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

    def get_constant_entries(self, bound_worldbooks: Optional[List[str]] = None) -> List[dict]:
        entries: List[dict] = []
        for wb in self.get_active_worldbooks(bound_worldbooks):
            for entry in wb.get('entries', []):
                if entry.get('enabled', True) and entry.get('is_constant', False):
                    entries.append(entry)
        return entries

    def match_entries(self, user_input: str,
                      bound_worldbooks: Optional[List[str]] = None,
                      top_k: int = 0,
                      sensitivity: float = 0.7) -> List[dict]:
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

        Args:
            user_input: text to match keywords against
            bound_worldbooks: optional worldbook filter (None = Auto, [] = zero, [...] = Custom)
            top_k: max entries to return (0 = no limit)
            sensitivity: 0.0-1.0，匹配灵敏度。0.0 极其严格，1.0 极其宽松，默认 0.7
        """
        entries: List[dict] = []
        msg_lower = user_input.lower()
        seen_ids: set = set()
        trigger_log: List[dict] = []

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

        for wb in active_wbs:
            wb_name = wb.get('name', '')
            for entry in wb.get('entries', []):
                if not entry.get('enabled', True):
                    continue
                if entry.get('is_constant', False):
                    continue
                keys = entry.get('keys', [])
                if not isinstance(keys, (list, tuple)):
                    keys = [keys]
                str_keys = [k for k in keys if isinstance(k, str) and k.strip()]
                # 灵敏度门控：最低匹配关键词数（在「有几个可用关键词」之后才能算）
                min_match_required = self._min_match_required(sens, len(str_keys))
                if min_match_required <= 0:
                    continue
                matched_count = 0
                matched_keys: List[str] = []
                for key in str_keys:
                    # 容错：导入的世界书可能含有非字符串 key（数字、null 等），跳过
                    if key.lower() in msg_lower:
                        matched_count += 1
                        matched_keys.append(key)
                    elif loose_mode:
                        # 宽松档：多词关键词的任一片段独立命中即可
                        for seg in self._key_segments(key):
                            if seg in msg_lower:
                                matched_count += 1
                                matched_keys.append(key)
                                break
                # 宽松档再补一路「标题命中」：条目标题本身是作者写的主题词，
                # 用户真的聊到该主题时标题往往先于关键词出现在文本里。
                if loose_mode and matched_count < min_match_required:
                    title = entry.get('title', '')
                    if isinstance(title, str) and len(title.strip()) >= 2 and title.strip().lower() in msg_lower:
                        matched_count += 1
                        matched_keys.append(title.strip())
                if matched_count >= min_match_required:
                    entry_id = entry.get('id', '')
                    dup_key = f"{wb_name}:{entry_id}"
                    if dup_key not in seen_ids:
                        seen_ids.add(dup_key)
                        result_entry = entry.copy()
                        result_entry['_match_score'] = matched_count
                        result_entry['_matched_keys'] = matched_keys
                        entries.append(result_entry)
                        trigger_log.append({
                            "worldbook": wb_name,
                            "entry_id": entry_id,
                            "title": entry.get('title', ''),
                            "matched_keys": matched_keys,
                            "score": matched_count,
                        })

        entries.sort(key=lambda x: x.get('_match_score', 0), reverse=True)
        if top_k > 0:
            entries = entries[:top_k]

        # 更新触发日志（show_trigger_log 开启时可供查询，加锁保护）
        with self._lock:
            self._last_trigger_log = trigger_log
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
        """归一注入位置为 1（常驻位置）或 2（按需位置）。

        H4：旧写法 `1 if e.get('position', 2) <= 1 else 2` 直接把 JSON 值和
        整数比大小。ST 导出里有 `"position": "0"` 这类字符串取值（也有
        `"before_char"` 这类枚举名），字符串与 int 比较会抛 TypeError，
        **整本**世界书跟着导入失败。这里统一走数值归一，认不出来就退回默认值。
        """
        try:
            return 1 if int(raw) <= 1 else 2
        except (TypeError, ValueError):
            # 枚举名形态：ST 的 before_char/after_char 都是常驻位
            if isinstance(raw, str) and raw.strip().lower() in ("before_char", "after_char"):
                return 1
            return default

    def _map_quill_entry(self, e: dict, index: int) -> dict:
        """Quill 原生条目 → 内部条目。字段缺省或类型异常都不抛，按兜底值走。"""
        return {
            # L8：id 可能是数字（round-trip 过 ST 工具/hand-edit 后很常见），
            # 直接 [:50] 会 TypeError；而异常被外层宽 except 吞掉 ⇒ 不是
            # 跳过这一条，而是**整本**世界书导入失败。
            "id": str(e.get('id') or f"entry_{index}")[:50],
            "title": str(e.get('title', '') or ''),
            "content": str(e.get('content', '') or ''),
            "is_constant": bool(e.get('is_constant', False)),
            "inject_position": self._safe_inject_position(e.get('inject_position', 1)),
            "keys": self._coerce_str_list(e.get('keys', [])),
            "enabled": bool(e.get('enabled', True)),
        }

    def _map_st_entry(self, e: dict, index: int) -> dict:
        """SillyTavern 条目 → 内部条目。

        H4：三个历史崩溃点都在这里——`e.get('comment','')[:50]`（comment 是数字
        时 `'int' object is not subscriptable`）、`e.get('position',2) <= 1`
        （position 是字符串时 `'<=' not supported between 'str' and 'int'`）、
        以及 keys 拼接。三者都会把异常抛到外层宽 except，导致**一条畸形条目
        废掉整本世界书**。现在统一先归一类型再取值。
        """
        keys_raw = self._coerce_str_list(e.get('key') or e.get('keys', []))
        keys_raw.extend(self._coerce_str_list(e.get('keysecondary') or []))

        # comment/uid 都可能是数字；title 与 id 都以字符串形态落库
        comment = e.get('comment', '')
        title = '' if comment is None else str(comment)
        uid = e.get('uid', '')
        # 先定 id：comment 优先，退回 uid（数字 uid 也要 str 化再截断），
        # 两者都没有才用序号兜底（序号用 index 而非 len(entries)，
        # 避免同一本里出现重复 id）
        eid_source = title or ('' if uid is None else str(uid))
        entry_id = eid_source[:50] if eid_source else f"st_import_{index}"

        return {
            "id": entry_id,
            "title": title,
            "content": str(e.get('content', '') or ''),
            "is_constant": bool(e.get('constant', False)),
            "inject_position": self._safe_inject_position(e.get('position', 2), default=2),
            "keys": keys_raw,
            "enabled": not bool(e.get('disable', False)),
        }

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

            # description：优先取 JSON 中的 description，否则根据格式自动生成
            description = data.get('description', '') or ("Imported from ST Lorebook" if not is_quill_native else "Imported worldbook")

            wb = {
                "name": worldbook_name,
                "description": description,
                "version": "1.0",
                "entries": entries,
            }
            path = os.path.join(self.worldbooks_dir, worldbook_name + ".json")
            _save_json_atomic(path, wb)
            with self._lock:
                self.worldbooks[worldbook_name] = wb
            return True
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[WorldbookManager] Import 失败: %s", exc)
            return False
        except Exception as exc:
            logger.error("[WorldbookManager] Import 时发生未知错误: %s", exc, exc_info=True)
            return False


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
