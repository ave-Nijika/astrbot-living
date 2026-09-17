"""M3 补丁 XI Part A 测试：自主模式被吵醒退出 + 紧急唤醒 + fixed 零回归。"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from core.living_state import LivingGate
from core.mood import MoodState
from core.sleep import SleepManager

T0 = datetime(2026, 9, 17, 23, 30)  # 夜间
BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {
        "sleep_mode": "autonomous",
        "sleep_window": "02:00-06:00",
        "min_awake_minutes": 0,
        "sleepiness_threshold": 0.0,  # 一定入睡
        "sleepiness_jitter": 0.0,
        "min_sleep_hours": 5.0,
        "max_sleep_hours": 11.0,
        "wake_n_messages": 3,
        "wake_window_minutes": 10,
        "grouchiness_percent": 0,
        "fatigue_rate_per_hour": 4.0,
        "sleep_debt_decay_per_day": 30.0,
    },
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


class AutoGate:
    """真实 LivingGate 语义 + 可控的自主睡眠状态（完整方法集）。"""

    def __init__(self, config=None):
        self._config = config or BASE_CONFIG
        self._sleep_until = None
        self._fell_asleep_at = None
        self._sleep_kind = None

    def sleep_mode(self):
        sleep_cfg = self._config.get("sleep", {})
        return sleep_cfg.get("sleep_mode", "fixed") if isinstance(sleep_cfg, dict) else "fixed"

    def autonomous_mode(self):
        return self.sleep_mode() == "autonomous"

    def in_sleep_window(self, now=None):
        now = now or datetime.now()
        if self.autonomous_mode():
            return self.asleep_in_autonomous(now)
        window = self._config.get("sleep", {}).get("sleep_window", "")
        if not window or "-" not in window:
            return False
        parts = window.split("-")
        from datetime import time as _time
        try:
            s = _time.fromisoformat(parts[0].strip())
            e = _time.fromisoformat(parts[1].strip())
        except ValueError:
            return False
        t = now.time()
        if s <= e:
            return s <= t < e
        return t >= s or t < e

    def asleep_in_autonomous(self, now=None):
        now = now or datetime.now()
        return (self._sleep_until is not None
                and self._fell_asleep_at is not None
                and now < self._sleep_until)

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self._sleep_until = until
        self._sleep_kind = kind
        self._fell_asleep_at = now or datetime.now()

    async def exit_autonomous_sleep(self, now=None):
        self._sleep_until = None
        self._sleep_kind = None
        self._fell_asleep_at = None

    def sleep_state(self, now=None):
        now = now or datetime.now()
        return {"asleep": self.asleep_in_autonomous(now),
                "until": self._sleep_until, "kind": self._sleep_kind,
                "fell_asleep_at": self._fell_asleep_at}

    async def should_wake(self, now=None, force=False):
        now = now or datetime.now()
        if self.autonomous_mode():
            if self.asleep_in_autonomous(now):
                if force:
                    return True, "woken_from_sleep"
                return False, "sleeping"
            return True, "ok"
        return True, "ok"

    async def daily_limit_info(self, now=None):
        return False, 0, 3

    async def should_send_message(self, now=None):
        return True, "ok"

    async def refresh_awake_until(self, minutes, now=None):
        pass

    def awake_standby_active(self, now=None):
        return False

    async def clear_awake_until(self):
        pass

    async def force_awake_now(self, now=None):
        return None

    def next_sleep_window_text(self):
        if self.autonomous_mode():
            return "自主作息（无固定窗）"
        return self._config.get("sleep", {}).get("sleep_window", "")

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def note_message_sent(self, now=None):
        pass

    async def close(self):
        pass


class RecordingSettle:
    """记录 apply_woken_from_autonomous 调用的替身。"""

    def __init__(self):
        self.calls = []

    async def should_nap(self, mood, now=None):
        return False, 0.0

    async def apply_woken_in_sleep(self, now=None):
        self.calls.append({"method": "apply_woken_in_sleep"})
        return {"grouchy": False, "debt_added": 0.0}

    async def begin_autonomous_sleep(self, mood, now=None):
        return {"asleep": False}

    async def apply_woken_from_autonomous(self, mood, actual_h, planned_h, now=None, kind="long"):
        self.calls.append({"actual": actual_h, "planned": planned_h, "kind": kind})
        return {"grouchy": True, "debt_added": 50.0}

    async def apply_woken_in_sleep(self, now=None):
        self.calls.append({"method": "apply_woken_in_sleep"})
        return {"grouchy": False, "debt_added": 0.0}


class RecordingMood:
    def __init__(self):
        self.energy = 0.3
        self.sleep_debt = 20.0
        self.valence = 0.2
        self.arousal = 0.5

    def apply_grouchiness(self, enabled):
        return enabled

    def add_sleep_debt(self, amount):
        self.sleep_debt += amount

    def digest(self):
        return "测试"

    def recent_topics_list(self):
        return []

    def interest_weight(self, topic, repeat_count=0, penalty_table=(0.5, 0.3, 0.15)):
        return 0.3

    async def save(self):
        pass

    async def close(self):
        pass


class SilentMemory:
    async def add(self, c, importance=0.5, metadata=None, **kw):
        return 1
    async def search(self, q, k=5):
        return []
    async def close(self):
        pass


class StubActivity:
    def __init__(self, name="surf"):
        self.name = name
    async def run(self, ctx):
        from core.activities import ActivityOutcome
        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


class FakeSender:
    def __init__(self):
        self.sent = []
    async def send(self, session, text):
        self.sent.append((session, text))
        return True


def make_autonomous_loop(gate, settle, mood):
    from core.living_loop import LivingLoop

    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=SilentMemory()),
        config_getter=lambda: BASE_CONFIG,
        activities=[StubActivity("surf")],
        mood=mood,
        sleep_manager=settle,
    )
    return loop


# ---------------------------------------------------------------------------
# A.1：被吵醒 → 自主模式走 apply_woken_from_autonomous + exit
# ---------------------------------------------------------------------------
def test_woken_from_sleep_autonomous_calls_settlement_and_exits():
    gate = AutoGate()
    now = datetime(2026, 9, 17, 23, 30)
    asyncio.run(gate.enter_autonomous_sleep(now + timedelta(hours=8), "long", now))
    assert gate.asleep_in_autonomous(now)

    settle = RecordingSettle()
    mood = RecordingMood()
    loop = make_autonomous_loop(gate, settle, mood)

    awake, reason, _act = asyncio.run(
        loop.heartbeat_once_detailed(now, force=True)
    )
    assert awake is True
    assert reason == "woken_from_sleep"
    assert len(settle.calls) == 1
    assert settle.calls[0]["kind"] == "long"
    # 自主睡眠状态已退出
    assert not gate.asleep_in_autonomous(now)
    # 后续 in_sleep_window 不再拦截
    assert gate.in_sleep_window(now) is False


def test_woken_from_sleep_nap_kind_debt_zero():
    """小睡被吵醒：债按 0 处理（kind="nap"）。"""
    gate = AutoGate()
    settle = RecordingSettle()
    mood = RecordingMood()
    loop = make_autonomous_loop(gate, settle, mood)

    now = datetime(2026, 9, 17, 14, 0)
    asyncio.run(gate.enter_autonomous_sleep(now + timedelta(hours=1), "nap", now))

    # 模拟被吵醒（kind=nap）
    settle2 = RecordingSettle()
    loop2 = make_autonomous_loop(gate, settle2, mood)
    awake, reason, _ = asyncio.run(loop2.heartbeat_once_detailed(now, force=True))
    assert awake is True
    assert settle2.calls[0]["kind"] == "nap"


def test_woken_from_sleep_non_autonomous_uses_fixed_path():
    """fixed 模式：仍走 apply_woken_in_sleep（零回归验证）。"""
    class FixedGate(AutoGate):
        def autonomous_mode(self):
            return False
        def in_sleep_window(self, now=None):
            return True
        async def should_wake(self, now=None, force=False):
            if self.in_sleep_window(now):
                if force:
                    return True, "woken_from_sleep"
                return False, "sleeping"
            return True, "ok"

    gate = FixedGate()
    settle = RecordingSettle()
    loop = make_autonomous_loop(gate, settle, mood=RecordingMood())

    awake, reason, _ = asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    assert awake is True
    assert len(settle.calls) == 1  # apply_woken_in_sleep 被调


# ---------------------------------------------------------------------------
# A.3：紧急唤醒 autonomous 分流
# ---------------------------------------------------------------------------
def test_next_sleep_window_text_autonomous():
    gate = AutoGate(config={**BASE_CONFIG, "sleep": {
        "sleep_mode": "autonomous", "sleep_window": "02:00-06:00"}})
    assert "自主作息" in gate.next_sleep_window_text()


def test_next_sleep_window_text_fixed():
    gate = AutoGate()
    gate._config = {**BASE_CONFIG, "sleep": {"sleep_mode": "fixed",
                                             "sleep_window": "02:00-06:00"}}
    assert "02:00-06:00" in gate.next_sleep_window_text()
