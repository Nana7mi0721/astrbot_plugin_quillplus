"""Async-safe per-user state manager with LRU eviction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


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


class StateManager:
    def __init__(self, max_users: int = 500):
        self._states: dict[str, UserState] = {}
        self._lock = asyncio.Lock()
        self._max_users = max_users

    async def get_state(self, user_id: str) -> UserState:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
                self._evict()
            return self._states[user_id]

    async def update_activity(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
            st = self._states[user_id]
            st.last_active = datetime.now(timezone.utc).isoformat()
            st.round_count += 1
            self._evict()

    async def mark_refusal(self, user_id: str) -> None:
        async with self._lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id=user_id)
            st = self._states[user_id]
            st.refusal_detected = True
            st.last_refusal_time = datetime.now(timezone.utc).isoformat()
            self._evict()

    async def clear_refusal(self, user_id: str) -> None:
        async with self._lock:
            if user_id in self._states:
                self._states[user_id].refusal_detected = False

    async def should_inject_emergency(self, user_id: str) -> bool:
        async with self._lock:
            return self._states.get(user_id, UserState(user_id=user_id)).refusal_detected

    async def increment_quill_rounds(self, user_id: str) -> int:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
                self._evict()
            st.quill_rounds += 1
            return st.quill_rounds

    async def reset_quill_rounds(self, user_id: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is not None:
                st.quill_rounds = 0

    async def set_stream_mode(self, user_id: str, mode: str) -> None:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
                self._evict()
            st.stream_mode = mode

    async def update_session_vars(self, user_id: str, updates: dict) -> dict:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                st = UserState(user_id=user_id)
                self._states[user_id] = st
                self._evict()
            st.session_vars.update(updates)
            return dict(st.session_vars)

    async def get_session_vars(self, user_id: str) -> dict:
        async with self._lock:
            st = self._states.get(user_id)
            if st is None:
                return {}
            return dict(st.session_vars)

    def _evict(self) -> None:
        """Drop oldest entries when capacity exceeded. Caller must hold lock."""
        while len(self._states) > self._max_users:
            oldest_id = min(self._states, key=lambda k: self._states[k].last_active)
            del self._states[oldest_id]


if __name__ == "__main__":
    import sys

    async def _run_tests():
        mgr = StateManager(max_users=500)
        errors = []

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

        # 3. LRU eviction — add 501 users total (1 + 100 + 400 more)
        for i in range(400):
            await mgr.update_activity(f"evict_{i}")
        assert len(mgr._states) == 500, f"eviction failed: {len(mgr._states)} users"

        # 4. Refusal lifecycle
        await mgr.mark_refusal("lifecycle_user")
        assert (await mgr.should_inject_emergency("lifecycle_user")) is True
        await mgr.clear_refusal("lifecycle_user")
        assert (await mgr.should_inject_emergency("lifecycle_user")) is False

        # 5. Eviction direction — fresh entries created via mark_refusal must
        # NOT be evicted before stale entries (regression for H2/H3 bug).
        small = StateManager(max_users=2)
        await small.update_activity("old_a")
        await asyncio.sleep(0.01)
        await small.update_activity("old_b")
        await asyncio.sleep(0.01)
        await small.mark_refusal("fresh")  # triggers eviction at cap=2 → +1
        assert "fresh" in small._states, "fresh entry was wrongly evicted"
        assert len(small._states) == 2, f"cap exceeded: {len(small._states)}"
        # The evicted one should be old_a (oldest last_active), not "fresh"
        assert "old_a" not in small._states, "oldest entry not evicted first"

        # 6. quill_rounds tracking
        mgr2 = StateManager(max_users=10)
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

        print("All tests passed.")

    asyncio.run(_run_tests())
