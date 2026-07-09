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
    def __init__(self, max_users: int = 0, data_dir: str = "data"):
        self._states: dict[str, UserState] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
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
                except Exception:
                    pass
            logger.info(f"[Quill State] 已恢复 {len(self._states)} 个对话状态")
        except Exception as e:
            logger.error(f"[Quill State] 加载失败: {e}")

    async def _persist(self) -> None:
        data = await asyncio.to_thread(self._serialize)
        await asyncio.to_thread(self._atomic_write, data)

    def _serialize(self) -> str:
        return json.dumps(
            {k: asdict(v) for k, v in self._states.items()},
            ensure_ascii=False, indent=2,
        )

    def _atomic_write(self, text: str) -> None:
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.state_file)

    async def get_state(self, user_id: str) -> UserState:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
                self._dirty = True

            return self._states[user_id]

    async def update_activity(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
            st = self._states[user_id]
            st.last_active = datetime.now(timezone.utc).isoformat()
            st.round_count += 1
            self._dirty = True

    async def persist_all(self) -> None:
        """Force persist all in-memory states to disk (call on shutdown)."""
        await self._persist()

    async def mark_refusal(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
            st = self._states[user_id]
            st.refusal_detected = True
            st.last_refusal_time = datetime.now(timezone.utc).isoformat()
        await self._persist()

    async def clear_refusal(self, user_id: str) -> None:
        async with self._lock:
            if user_id in self._states:
                self._states[user_id].refusal_detected = False
        await self._persist()

    async def should_inject_emergency(self, user_id: str) -> bool:
        async with self._lock:
            return self._states.get(user_id, UserState(user_id=user_id)).refusal_detected

    async def increment_quill_rounds(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
            st.quill_rounds += 1
            return st.quill_rounds

    async def reset_quill_rounds(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                st.quill_rounds = 0

    async def increment_unsummarized_turns(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
            st.unsummarized_turns += 1
            val = st.unsummarized_turns
        await self._persist()
        return val

    async def reset_unsummarized_turns(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                st.unsummarized_turns = 0
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
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st

            st.stream_mode = mode
        await self._persist()

    async def update_session_vars(self, user_id: str, updates: dict) -> dict:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st

            st.session_vars.update(updates)
            result = dict(st.session_vars)
        await self._persist()
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

    async def is_first_message_injected(self, user_id: str) -> bool:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                return False
            return st.first_message_injected

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
        mgr = StateManager(max_users=500, data_dir=_tmp_dir)

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
        mgr2 = StateManager(max_users=10, data_dir=mgr2_dir)
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

        # 8. Persistence round-trip
        await mgr2.set_persona_id("persist_user", "test_persona_123")
        await mgr2.set_stream_mode("persist_user", "off")
        del mgr2
        mgr3 = StateManager(max_users=10, data_dir=mgr2_dir)
        p = await mgr3.get_persona_id("persist_user")
        assert p == "test_persona_123", f"persona_id not persisted, got {p}"
        st = await mgr3.get_state("persist_user")
        assert st.stream_mode == "off", f"stream_mode not persisted, got {st.stream_mode}"
        print("[OK] persistence round-trip")
        shutil.rmtree(mgr2_dir, ignore_errors=True)

        print("All tests passed.")

    asyncio.run(_run_tests())
    shutil.rmtree(_tmp_dir, ignore_errors=True)
