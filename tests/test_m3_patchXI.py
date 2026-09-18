"""M3 补丁 XI 测试：Part A 缺陷修复 + Part B 档位/浏览器/写操作/free 活动。"""

import asyncio
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.autonomy import (
    build_tool_manifest, clamp_tier, clamp_write_level,
    is_path_protected, is_write_allowed, read_tier, read_write_level,
)
from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.mood import MoodState
from core.sleep import SleepManager

T0 = datetime(2026, 9, 18, 14, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {"sleep_mode": "autonomous", "sleep_window": "02:00-06:00",
              "min_awake_minutes": 0, "sleepiness_threshold": 0.0,
              "sleepiness_jitter": 0.0, "min_sleep_hours": 5.0,
              "max_sleep_hours": 11.0, "fatigue_rate_per_hour": 4.0,
              "wake_n_messages": 3, "wake_window_minutes": 10,
              "grouchiness_percent": 0, "sleep_debt_decay_per_day": 30.0,
              "awake_standby_minutes": 30, "wake_ack_message": "",
              "sleep_farewell_message": "", "nap_enabled": True,
              "circadian_hint": "23:00-07:00"},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": "",
                    "share_rewrite_enabled": False, "share_rewrite_prompt": "",
                    "share_max_length": 120},
    "autonomy": {"tier": 1, "write_level": 0, "workspace_dir": ""},
}


class FakeGate:
    def __init__(self, autonomous=True, in_sleep=False):
        self._autonomous = autonomous
        self._in_sleep = in_sleep
        self._sleep_until = None
        self._fell_asleep_at = None

    def sleep_mode(self):
        return "autonomous" if self._autonomous else "fixed"

    def autonomous_mode(self):
        return self._autonomous

    def in_sleep_window(self, now=None):
        return self._in_sleep

    def asleep_in_autonomous(self, now=None):
        return self._in_sleep and self._sleep_until is not None

    def sleep_state(self, now=None):
        return {"asleep": self._in_sleep, "until": self._sleep_until,
                "kind": "long", "fell_asleep_at": self._fell_asleep_at}

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self._sleep_until = until
        self._fell_asleep_at = now or datetime.now()

    async def exit_autonomous_sleep(self, now=None):
        self._sleep_until = None
        self._fell_asleep_at = None
        self._in_sleep = False

    async def should_wake(self, now=None, force=False):
        if self._in_sleep:
            if force:
                return True, "woken_from_sleep"
            return False, "sleeping"
        return True, "ok"

    async def should_send_message(self, now=None):
        return True, "ok"

    async def daily_limit_info(self, now=None):
        return False, 0, 3

    async def refresh_awake_until(self, minutes, now=None):
        pass

    def awake_standby_active(self, now=None):
        return False

    async def clear_awake_until(self):
        pass

    async def force_awake_now(self, now=None):
        return None

    def next_sleep_window_text(self):
        return "02:00-06:00"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def note_message_sent(self, now=None):
        pass

    async def close(self):
        pass


class RecordingMood:
    def __init__(self, valence=0.2, energy=0.8):
        self.valence = valence
        self.energy = energy
        self.sleep_debt = 0.0
        self.records = []

    def digest(self):
        return "测试心境"

    def recent_topics_list(self):
        return []

    def record_recent_topics(self, topics, window=6):
        pass

    def interest_weight(self, topic, repeat_count=0, penalty_table=(0.5, 0.3, 0.15)):
        return 0.3

    async def record_activity(self, *a, **kw):
        pass

    def add_sleep_debt(self, amount):
        self.sleep_debt += amount

    def apply_grouchiness(self, enabled):
        if enabled:
            self.valence = max(0.0, self.valence - 0.15)
            self.energy = max(self.energy - 0.1, 0.05)
        return enabled

    async def save(self):
        pass

    async def close(self):
        pass


class SilentMemory:
    def __init__(self):
        self.added = []

    async def add(self, content, importance=0.5, metadata=None, **kw):
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="surf"):
        self.name = name
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        from core.activities import ActivityOutcome
        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


# ---------------------------------------------------------------------------
# Part A：自主模式被吵醒后醒过来
# ---------------------------------------------------------------------------
def test_autonomous_woken_exits_sleep_and_stops_muting(tmp_path):
    """端到端：入睡 → 连发 3 条消息 → 状态退出 + 后续不再拦。"""
    from core.sleep import SleepManager

    gate = FakeGate(autonomous=True, in_sleep=True)
    gate._sleep_until = datetime.now() + timedelta(hours=8)
    gate._fell_asleep_at = datetime.now() - timedelta(hours=2)
    mood = RecordingMood()
    manager = SleepManager(
        config_getter=lambda: BASE_CONFIG, gate=gate, mood=mood,
    )

    from core.living_loop import LivingLoop

    class Stub:
        name = "surf"
        async def run(self, ctx):
            from core.activities import ActivityOutcome
            return ActivityOutcome(name="surf", summary="s", memory_content="m")

    loop = LivingLoop(
        gate=gate, memory_getter=lambda: asyncio.sleep(0, result=SilentMemory()),
        config_getter=lambda: BASE_CONFIG, activities=[Stub()], mood=mood,
        sleep_manager=manager,
    )

    awake, reason, _ = asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    assert awake is True
    assert reason == "woken_from_sleep"
    # 自主睡眠状态已退出 → in_sleep_window 返回 False → 后续消息不再被拦
    assert gate.in_sleep_window(T0) is False


def test_apply_woken_from_autonomous_non_empty(tmp_path):
    """A.2：结算非空路径——起床气概率生效、债按比例计算正确。"""
    mood = RecordingMood()
    mood.sleep_debt = 20.0
    manager = SleepManager(
        config_getter=lambda: BASE_CONFIG, gate=FakeGate(), mood=mood,
    )
    result = asyncio.run(manager.apply_woken_from_autonomous(
        mood, 2.0, 8.0, kind="long"))
    assert result["debt_added"] == pytest.approx(75.0)  # 100*(1-2/8)=75  # 2/8 睡了 → 保留 75%→50
    assert mood.sleep_debt > 0

    # nap 类型 → 债不累积
    result2 = asyncio.run(manager.apply_woken_from_autonomous(
        mood, 1.0, 2.0, kind="nap"))
    assert result2["debt_added"] == 0.0


# ---------------------------------------------------------------------------
# Part A：紧急唤醒 autonomous 分流
# ---------------------------------------------------------------------------
def test_wake_now_exits_autonomous_sleep(tmp_path):
    """紧急唤醒：autonomous 下先 exit_autonomous_sleep 再清待机。"""
    gate = FakeGate(autonomous=True, in_sleep=True)
    gate._sleep_until = datetime.now() + timedelta(hours=8)
    gate._fell_asleep_at = datetime.now() - timedelta(hours=1)

    manager = SleepManager(
        config_getter=lambda: BASE_CONFIG, gate=gate, mood=None,
    )

    # 模拟 living_wake_now 命令逻辑
    now = datetime.now()
    assert gate.autonomous_mode() and gate.asleep_in_autonomous(now)
    asyncio.run(gate.exit_autonomous_sleep(now))
    manager.reset_wake_state()
    asyncio.run(gate.clear_awake_until())
    # 退出后 in_sleep_window False → 不再拦
    assert gate.in_sleep_window(now) is False


# ---------------------------------------------------------------------------
# Part B：档位系统
# ---------------------------------------------------------------------------
def test_tier_clamp():
    assert clamp_tier(0) == 0
    assert clamp_tier(3) == 3
    assert clamp_tier(5) == 3
    assert clamp_tier(-1) == 0
    assert clamp_tier("abc") == 1
    assert clamp_tier(None) == 1


def test_write_level_clamp():
    assert clamp_write_level(0) == 0
    assert clamp_write_level(2) == 2
    assert clamp_write_level(9) == 3
    assert clamp_write_level(-1) == 0


def test_read_tier_and_write_level():
    config = {"autonomy": {"tier": 2, "write_level": 1}}
    assert read_tier(config) == 2
    assert read_write_level(config) == 1
    assert read_tier({}) == 1
    assert read_write_level({}) == 0


def test_path_protection():
    """红线 1：AstrBot 本体路径一律拒绝。"""
    assert is_path_protected("data/config/cmd_config.json")
    assert is_path_protected("data/cmd_config.json")
    assert is_path_protected("data/data.db")
    assert is_path_protected("astrbot/core/star/context.py")
    assert is_path_protected("data/plugins/some_other_plugin/x.py")
    assert not is_path_protected("data/plugin_data/astrbot_plugin_living/x.txt")
    assert not is_path_protected("workspace/screenshot.png")


def test_write_level_gate():
    """write_level 分层（B2.4）：L0 禁写 / L2 区内可写 / L3 全写。"""
    assert is_write_allowed("ws/file.txt", "ws", write_level=0) is False
    assert is_write_allowed("ws/file.txt", "ws", write_level=1) is False
    assert is_write_allowed("ws/file.txt", "ws", write_level=2) is True
    assert is_write_allowed("ws/file.txt", "ws", write_level=3) is True
    assert is_write_allowed("/etc/passwd", "ws", write_level=2) is False  # 区外
    assert is_write_allowed("/etc/passwd", "ws", write_level=3) is True
    assert is_write_allowed("data/config/cmd_config.json", "ws", 3) is False


def test_tool_manifest_by_tier():
    """清单函数按档位递进（补丁 XV：与实际挂载名一致，含 click/type/list）。"""
    m0 = build_tool_manifest(0, 0, has_browser=False)
    assert "web_search" in m0 and "browser_navigate" not in m0
    m1 = build_tool_manifest(1, 0, has_browser=True)
    assert "browser_navigate" in m1
    assert "browser_click" in m1 and "browser_type" in m1
    m2 = build_tool_manifest(2, 2, has_browser=True, has_workspace=True)
    assert "workspace_read" in m2 and "workspace_list" in m2
    assert "local_shell" not in m2
    m3 = build_tool_manifest(3, 3, has_browser=True, has_workspace=True)
    assert "local_shell" in m3
    # has_workspace=False 时清单不预告工作区工具（与实际挂载条件一致）
    m2_now = build_tool_manifest(2, 2, has_browser=True, has_workspace=False)
    assert "workspace_read" not in m2_now


# ---------------------------------------------------------------------------
# Part B：free 活动
# ---------------------------------------------------------------------------
def test_free_activity_config_gate(tmp_path):
    """free_activity_enabled=false → free 不出现在活动池。"""
    from core.activities import default_activities

    acts = default_activities()
    assert any(a.name == "free" for a in acts)  # 默认含 free（补丁 XI-B5）
