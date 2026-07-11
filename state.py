# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async-safe per-session state manager with JSON persistence."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class UserState:
    user_id: str
    last_active: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    round_count: int = 0
    refusal_detected: bool = False
    last_refusal_time: str = ""
    quill_rounds: int = 0
    stream_mode: str = "auto"
    session_vars: dict = field(default_factory=dict)
    persona_id: str = ""
    first_message_injected: bool = False
    unsummarized_turns: int = 0
    last_learned_id: int = 0


class StateManager:
    def __init__(self, data_dir: str = "data", max_users: int = 10000):
        self._states: dict[str, UserState] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._max_users = max_users
        self._autoflush_task: asyncio.Task | None = None
        self.state_file = os.path.join(data_dir, "quill_state.json")
        os.makedirs(data_dir, exist_ok=True)
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key, val in raw.items():
                try:
                    self._states[key] = UserState(**val)
                except Exception as e:
                    # S3-15: 补日志，便于发现损坏的状态条目
                    logger.warning("[Quill State] 跳过损坏的状态条目 %s: %s", key, e)
            self._evict_if_needed()
            logger.info(f"[Quill State] 已恢复 {len(self._states)} 个对话状态")
        except Exception as e:
            logger.error(f"[Quill State] 加载失败: {e}")

    def _evict_if_needed(self) -> None:
        """Evict oldest sessions beyond max_users limit (LRU by last_active)."""
        if len(self._states) <= self._max_users:
            return
        sorted_sessions = sorted(
            self._states.items(),
            key=lambda x: x[1].last_active,
        )
        to_remove = len(self._states) - self._max_users
        for i in range(to_remove):
            del self._states[sorted_sessions[i][0]]
            self._dirty = True
        logger.info("[Quill State] LRU 淘汰: %d 个最旧会话", to_remove)

    async def _persist(self) -> None:
        """立即落盘：锁内序列化快照，锁外写盘（F6 修复：减少持锁时间）

        S2-1 修复：写盘成功后才清脏，失败时恢复 dirty 供 autoflush 重试。
        """
        async with self._lock:
            snapshot = self._serialize()
            self._dirty = False
        try:
            await asyncio.to_thread(self._atomic_write, snapshot)
        except Exception:
            # 写盘失败：恢复脏标记，让 autoflush 下轮重试
            async with self._lock:
                self._dirty = True
            raise

    def _serialize(self) -> str:
        return json.dumps(
            {k: asdict(v) for k, v in self._states.items()},
            ensure_ascii=False, indent=2,
        )

    def _atomic_write(self, text: str) -> None:
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    async def get_state(self, user_id: str) -> UserState:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
                self._evict_if_needed()
                self._mark_dirty()
            return self._states[user_id]

    async def update_activity(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
                self._evict_if_needed()
            st = self._states[user_id]
            st.last_active = datetime.now(timezone.utc).isoformat()
            st.round_count += 1
            self._mark_dirty()

    async def persist_all(self) -> None:
        """Force persist all in-memory states to disk (call on shutdown)."""
        await self._persist()

    # ── Autoflush ──────────────────────────────────────────────────

    def start_autoflush(self, interval: float = 5.0) -> None:
        """Start background task that periodically flushes dirty state."""
        if self._autoflush_task is not None:
            return
        self._autoflush_task = asyncio.ensure_future(self._autoflush_loop(interval))
        logger.info("[Quill State] 自动落盘已启动 (interval=%.0fs)", interval)

    async def _autoflush_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                # F6 修复：锁内只做序列化+清脏，锁外写盘，避免阻塞其他状态读写
                async with self._lock:
                    if not self._dirty:
                        continue
                    snapshot = self._serialize()
                    self._dirty = False
                # S1-2 修复：内层捕获写盘异常，恢复 dirty 供下轮重试，避免循环死亡
                try:
                    await asyncio.to_thread(self._atomic_write, snapshot)
                    logger.debug("[Quill State] 自动落盘")
                except Exception as e:
                    logger.error(f"[Quill State] 自动落盘失败，将在下个周期重试: {e}")
                    async with self._lock:
                        self._dirty = True  # 恢复脏标记以便重试
        except asyncio.CancelledError:
            pass

    async def stop_autoflush(self) -> None:
        """Cancel the background autoflush task."""
        if self._autoflush_task is not None:
            self._autoflush_task.cancel()
            try:
                await self._autoflush_task
            except asyncio.CancelledError:
                pass
            self._autoflush_task = None

    async def shutdown(self) -> None:
        """Convenience: stop autoflush then persist all."""
        await self.stop_autoflush()
        await self.persist_all()

    # Internal: mark dirty
    def _mark_dirty(self) -> None:
        self._dirty = True

    # ── Public state API ───────────────────────────────────────────

    async def mark_refusal(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
            st = self._states[user_id]
            st.refusal_detected = True
            st.last_refusal_time = datetime.now(timezone.utc).isoformat()
        await self._persist()

    async def clear_refusal(self, user_id: str) -> None:
        # F13 降级：clear 是幂等操作，丢失可由下次 mark_refusal 覆盖；on_llm_response 每轮调，写放大严重
        async with self._lock:
            if user_id in self._states:
                self._states[user_id].refusal_detected = False
            self._mark_dirty()

    async def should_inject_emergency(self, user_id: str) -> bool:
        async with self._lock:
            return self._states.get(user_id, UserState(user_id=user_id)).refusal_detected

    async def increment_quill_rounds(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._evict_if_needed()
                self._states[user_id] = st

            st.quill_rounds += 1
            self._mark_dirty()
            return st.quill_rounds

    async def reset_quill_rounds(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                st.quill_rounds = 0
                self._mark_dirty()

    async def increment_unsummarized_turns(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._evict_if_needed()
                self._states[user_id] = st
            st.unsummarized_turns += 1
            val = st.unsummarized_turns
            self._mark_dirty()
        return val

    async def reset_unsummarized_turns(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                st.unsummarized_turns = 0
                # S3-1: 补 _mark_dirty()，与同类写操作一致；persist 失败时 autoflush 可重试
                self._mark_dirty()
        await self._persist()

    async def update_last_learned_id(self, user_id: str, last_id: int) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
            st.last_learned_id = last_id
        await self._persist()

    async def get_last_learned_id(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                return st.last_learned_id
            return 0

    async def set_stream_mode(self, user_id: str, mode: str) -> None:
        # F13 降级：运行时态，重启可重算
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st

            st.stream_mode = mode
            self._mark_dirty()

    async def update_session_vars(self, user_id: str, updates: dict) -> dict:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st

            st.session_vars.update(updates)
            result = dict(st.session_vars)
            self._mark_dirty()
        return result

    async def get_session_vars(self, user_id: str) -> dict:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                return {}
            return dict(st.session_vars)

    async def set_persona_id(self, user_id: str, persona_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
            st.persona_id = persona_id
            st.first_message_injected = False  # 切换角色时重置注入标记
        await self._persist()

    async def get_persona_id(self, user_id: str) -> str:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                return ""
            return st.persona_id

    async def mark_first_message_injected(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
            st.first_message_injected = True
        await self._persist()

if __name__ == "__main__":
    import shutil
    import tempfile

    _tmp_dir = tempfile.mkdtemp(prefix="quill_state_test_")

    async def _run_tests():
        mgr = StateManager(data_dir=_tmp_dir)

        # 1. Basic get/update/mark/clear cycle
        s = await mgr.get_state("u1")
        assert s.user_id == "u1" and s.round_count == 0, "initial state wrong"
        await mgr.update_activity("u1")
        s = await mgr.get_state("u1")
        assert s.round_count == 1 and s.last_active != "", "update_activity failed"

        await mgr.mark_refusal("u1")
        assert (await mgr.should_inject_emergency("u1")) is True, "refusal not detected"
        await mgr.clear_refusal("u1")
        assert (await mgr.should_inject_emergency("u1")) is False, "refusal not cleared"

        # 2. Concurrent access — 100 coroutines
        await asyncio.gather(*[mgr.update_activity(f"user_{i}") for i in range(100)])
        total = sum(1 for _ in mgr._states)
        assert total == 101, f"expected 101 users after concurrent adds, got {total}"

        # 3. All users retained — add 501 users total (1 + 100 + 400 more)
        for i in range(400):
            await mgr.update_activity(f"evict_{i}")
        assert len(mgr._states) == 501, f"expected 501 users, got {len(mgr._states)}"

        # 4. Refusal lifecycle
        await mgr.mark_refusal("lifecycle_user")
        assert (await mgr.should_inject_emergency("lifecycle_user")) is True
        await mgr.clear_refusal("lifecycle_user")
        assert (await mgr.should_inject_emergency("lifecycle_user")) is False

        # 5. All entries retained — no eviction even beyond old cap
        small_dir = tempfile.mkdtemp(prefix="quill_small_")
        small = StateManager(data_dir=small_dir)
        await small.update_activity("old_a")
        await asyncio.sleep(0.01)
        await small.update_activity("old_b")
        await asyncio.sleep(0.01)
        await small.mark_refusal("fresh")
        assert "fresh" in small._states, "fresh entry was wrongly removed"
        assert "old_a" in small._states, "old_a should be retained"
        assert "old_b" in small._states, "old_b should be retained"
        assert len(small._states) == 3, f"expected 3, got {len(small._states)}"
        shutil.rmtree(small_dir, ignore_errors=True)

        # 6. quill_rounds tracking
        mgr2_dir = tempfile.mkdtemp(prefix="quill_mgr2_")
        mgr2 = StateManager(data_dir=mgr2_dir)
        r = await mgr2.increment_quill_rounds("u_quill")
        assert r == 1, f"first increment should be 1, got {r}"
        r = await mgr2.increment_quill_rounds("u_quill")
        assert r == 2, f"second increment should be 2, got {r}"
        s = await mgr2.get_state("u_quill")
        assert s.quill_rounds == 2, "UserState.quill_rounds should match"
        await mgr2.reset_quill_rounds("u_quill")
        s = await mgr2.get_state("u_quill")
        assert s.quill_rounds == 0, "after reset should be 0"
        print("[OK] quill_rounds tracking")

        # 7. stream_mode
        await mgr2.set_stream_mode("u_stream", "off")
        s = await mgr2.get_state("u_stream")
        assert s.stream_mode == "off", f"stream_mode should be 'off', got {s.stream_mode}"
        await mgr2.set_stream_mode("u_stream", "on")
        s = await mgr2.get_state("u_stream")
        assert s.stream_mode == "on", f"stream_mode should be 'on', got {s.stream_mode}"
        await mgr2.set_stream_mode("u_stream", "auto")
        s = await mgr2.get_state("u_stream")
        assert s.stream_mode == "auto", f"stream_mode should be 'auto', got {s.stream_mode}"
        s_default = await mgr2.get_state("fresh_user")
        assert s_default.stream_mode == "auto", "default stream_mode should be 'auto'"
        print("[OK] stream_mode")

        # 8. Persistence round-trip（set_stream_mode 已降级为 dirty-only，需显式 persist）
        await mgr2.set_persona_id("persist_user", "test_persona_123")
        await mgr2.set_stream_mode("persist_user", "off")
        await mgr2.persist_all()  # 显式落盘（set_stream_mode 不再立即写）
        del mgr2
        mgr3 = StateManager(data_dir=mgr2_dir)
        p = await mgr3.get_persona_id("persist_user")
        assert p == "test_persona_123", f"persona_id not persisted, got {p}"
        st = await mgr3.get_state("persist_user")
        assert st.stream_mode == "off", f"stream_mode not persisted, got {st.stream_mode}"
        print("[OK] persistence round-trip")
        shutil.rmtree(mgr2_dir, ignore_errors=True)

        print("All tests passed.")

    asyncio.run(_run_tests())
    shutil.rmtree(_tmp_dir, ignore_errors=True)
