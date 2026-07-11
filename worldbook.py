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
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# ── Name validation ──────────────────────────────────────────────────────────

_VALID_NAME_RE = re.compile(r'[a-zA-Z0-9_\-\u4e00-\u9fff]')


def _validate_name(name: str) -> bool:
    """Return True if *name* is a safe worldbook identifier.

    Rejects empty names, path traversal fragments (``..``, ``/``, ``\\``),
    and any character outside [a-zA-Z0-9_\\-\\u4e00-\u9fff].
    """
    if not name:
        return False
    if '..' in name or '/' in name or '\\' in name:
        return False
    # After removing allowed chars, nothing should remain
    return not _VALID_NAME_RE.sub('', name)


# ── Manager ──────────────────────────────────────────────────────────────────

class WorldbookManager:
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
                name = wb.get('name', f.replace('.json', ''))
                # 拒绝包含路径穿越/非法字符的名字
                if not _validate_name(name):
                    logger.warning("[WorldbookManager] Skipping %s: invalid name %r", f, name)
                    continue
                self.worldbooks[name] = wb
            except Exception as exc:
                logger.warning("[WorldbookManager] Failed to load %s: %s", f, exc)

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
                wb_name = wb.get('name', f.replace('.json', ''))
                if wb_name == name:
                    with self._lock:
                        self.worldbooks[name] = wb
                    return True
            except Exception as exc:
                # S3-4: 裸 except pass 会吞掉 JSON 损坏/IO 错误，改为日志便于排查
                logger.warning("[WorldbookManager] reload 读取失败 %s: %s", f, exc)
        return False

    # ── active / matching entries ────────────────────────────────────────

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
                return list(self.worldbooks.values())
            else:
                # Custom 模式：返回指定的世界书（空列表返回空）
                logger.debug("[Worldbook] Custom 模式：绑定世界书: %s", bound_worldbooks)
                valid_wbs = [n for n in bound_worldbooks if n in self.worldbooks]
                if len(valid_wbs) != len(bound_worldbooks):
                    invalid_wbs = set(bound_worldbooks) - set(valid_wbs)
                    logger.warning("[Worldbook] 以下世界书不存在，已跳过: %s", invalid_wbs)
                return [self.worldbooks[n] for n in valid_wbs]

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

        Args:
            user_input: text to match keywords against
            bound_worldbooks: optional worldbook filter (None = Auto, [] = zero, [...] = Custom)
            top_k: max entries to return (0 = no limit)
            sensitivity: 0.0-1.0, 匹配灵敏度。<0.3 严格(需多关键词命中)；
                0.3-0.7 平衡(子串匹配)；>0.7 宽松(单关键词即可)
        """
        entries: List[dict] = []
        msg_lower = user_input.lower()
        seen_ids: set = set()
        trigger_log: List[dict] = []

        # 获取活跃世界书快照（get_active_worldbooks 内部已加锁）
        active_wbs = self.get_active_worldbooks(bound_worldbooks)

        # 灵敏度门控：最低匹配关键词数
        if sensitivity < 0.3:
            min_match_required = 2  # 严格：至少 2 个关键词命中
        else:
            min_match_required = 1  # 平衡/宽松：1 个即可

        for wb in active_wbs:
            wb_name = wb.get('name', '')
            for entry in wb.get('entries', []):
                if not entry.get('enabled', True):
                    continue
                if entry.get('is_constant', False):
                    continue
                keys = entry.get('keys', [])
                matched_count = 0
                matched_keys: List[str] = []
                for key in keys:
                    # 容错：导入的世界书可能含有非字符串 key（数字、null 等），跳过
                    if not isinstance(key, str):
                        continue
                    if key.lower() in msg_lower:
                        matched_count += 1
                        matched_keys.append(key)
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
        """Atomically add an entry to a worldbook. Thread-safe."""
        if not _validate_name(name):
            return False
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return False
            wb["entries"].append(entry)
            path = os.path.join(self.worldbooks_dir, name + ".json")
            _save_json_atomic(path, wb)
        return True

    def update_entry_in_worldbook(self, name: str, entry_id: str, patch: dict) -> bool:
        """Atomically update an entry in a worldbook. Thread-safe."""
        if not _validate_name(name):
            return False
        with self._lock:
            wb = self.worldbooks.get(name)
            if wb is None:
                return False
            for i, item in enumerate(wb["entries"]):
                if item.get("id") == entry_id:
                    wb["entries"][i].update(patch)
                    path = os.path.join(self.worldbooks_dir, name + ".json")
                    _save_json_atomic(path, wb)
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
            before = len(wb["entries"])
            wb["entries"] = [e for e in wb["entries"] if e.get("id") != entry_id]
            if len(wb["entries"]) == before:
                return False
            path = os.path.join(self.worldbooks_dir, name + ".json")
            _save_json_atomic(path, wb)
        return True

    # ── ST Lorebook import ───────────────────────────────────────────────

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

            raw_entries = data.get('entries', [])
            entries: List[dict] = []

            # 检测格式：如果第一个条目包含 Quill 特征字段（content + keys），按 Quill 原生处理
            items_list = raw_entries.values() if isinstance(raw_entries, dict) else raw_entries
            if items_list and isinstance(items_list, list) and len(items_list) > 0:
                first = items_list[0] if isinstance(items_list[0], dict) else {}
                is_quill_native = 'content' in first and ('keys' in first or 'is_constant' in first)
            else:
                is_quill_native = False

            if is_quill_native:
                # Quill 原生格式 → 直接映射字段
                for e in items_list:
                    if not isinstance(e, dict) or not e.get('content'):
                        continue
                    raw_keys = e.get('keys', [])
                    if isinstance(raw_keys, list):
                        keys_raw = raw_keys
                    elif isinstance(raw_keys, str):
                        keys_raw = [k.strip() for k in raw_keys.split(',') if k.strip()]
                    else:
                        keys_raw = []
                    entries.append({
                        "id": e.get('id', f"entry_{len(entries)}")[:50],
                        "title": e.get('title', ''),
                        "content": e.get('content', ''),
                        "is_constant": e.get('is_constant', False),
                        "inject_position": e.get('inject_position', 1),
                        "keys": keys_raw,
                        "enabled": e.get('enabled', True),
                    })
            else:
                # SillyTavern 格式
                items = raw_entries.values() if isinstance(raw_entries, dict) else raw_entries if isinstance(raw_entries, list) else []
                for e in items:
                    if not isinstance(e, dict):
                        continue
                    raw_keys = e.get('key') or e.get('keys', [])
                    if isinstance(raw_keys, list):
                        keys_raw = raw_keys
                    elif isinstance(raw_keys, str):
                        keys_raw = [k.strip() for k in raw_keys.split(',') if k.strip()]
                    else:
                        keys_raw = []
                    raw_secondary = e.get('keysecondary') or []
                    if isinstance(raw_secondary, list):
                        secondary = raw_secondary
                    elif isinstance(raw_secondary, str):
                        secondary = [k.strip() for k in raw_secondary.split(',') if k.strip()]
                    else:
                        secondary = []
                    keys_raw.extend(secondary)

                    eid_source = e.get('comment', '') or str(e.get('uid', ''))
                    entry_id = eid_source[:50] if eid_source else f"st_import_{len(entries)}"
                    entries.append({
                        "id": entry_id,
                        "title": e.get('comment', ''),
                        "content": e.get('content', ''),
                        "is_constant": e.get('constant', False),
                        "inject_position": 1 if e.get('position', 2) <= 1 else 2,
                        "keys": keys_raw,
                        "enabled": not e.get('disable', False),
                    })

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
        except Exception as exc:
            logger.warning("[WorldbookManager] Import failed: %s", exc)
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
