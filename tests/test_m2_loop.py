"""M2 主循环集成测试：心境接入、决策器接入、重要度调节（任务书 M2-D/E）。"""

import asyncio
from datetime import datetime
from typing import Any

from core.activities import ActivityContext, ActivityOutcome
from core.decider import Decision
from core.living_loop import LivingLoop

NOW = datetime(2026, 9, 8, 14, 0, 0)

BASE_CONFIG = {
    "decision": {
        "daily_impulse_limit": 3,
        "activity_probability": 1.0,
        "impulse_check_interval_minutes": 45,
        "max_run_seconds": 300,
        "decision_mode": "hybrid",
    },
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": ""},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


class FakeGate:
    def __init__(self):
        self.started = 0
        self.finished = 0

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        self.started += 1

    async def note_activity_finished(self, now=None):
        self.finished += 1

    async def close(self):
        pass


class FakeMemory:
    def __init__(self):
        self.added = []

    async def add(self, content, importance=0.5, metadata=None):
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class RecordingMood:
    """记录 record_activity 调用的心境替身；valence 可预设。"""

    def __init__(self, valence=0.2, energy=0.8):
        self.valence = valence
        self.energy = energy
        self.records = []

    async def record_activity(self, activity_name, ok, topic=None, **kwargs):
        self.records.append((activity_name, ok, topic))

    def digest(self):
        return "测试心境"


class ScriptedActivity:
    def __init__(self, name, fail=False):
        self.name = name
        self.description = f"{name} 的描述"
        self.fail = fail
        self.runs = 0
        self.last_ctx = None

    async def run(self, ctx):
        self.runs += 1
        self.last_ctx = ctx
        if self.fail:
            raise RuntimeError("故意失败")
        return ActivityOutcome(
            name=self.name,
            summary=f"{self.name} 摘要",
            memory_content=f"{self.name} 的记忆",
            importance=0.5,
        )


class ScriptedDecider:
    """固定返回指定活动与参数的决策器替身。"""

    def __init__(self, activity, params=None, error=False):
        self._activity = activity
        self._params = params or {}
        self._error = error

    async def decide(self, now=None):
        if self._error:
            raise RuntimeError("decider boom")
        return Decision(self._activity, self._params, "hybrid")


def make_loop(activities, gate=None, memory=None, mood=None, decider=None):
    memory = memory if memory is not None else FakeMemory()
    return LivingLoop(
        gate=gate or FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        activities=activities,
        mood=mood,
        decider=decider,
    )


def test_decider_choice_reaches_activity_context():
    """决策器选的活动与参数原样进入活动上下文（hybrid 参数注入链路）。"""
    act = ScriptedActivity("surf")
    decider = ScriptedDecider(act, params={"topic": "深海生物"})
    loop = make_loop([act], decider=decider)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert act.runs == 1
    assert act.last_ctx.params == {"topic": "深海生物"}


def test_mood_recorded_with_topic_after_cycle():
    """活动周期结束后心境被记录：成功 + 主题词。"""
    act = ScriptedActivity("surf")
    mood = RecordingMood()
    loop = make_loop([act], mood=mood, decider=ScriptedDecider(act, {"topic": "咖啡"}))

    asyncio.run(loop.run_activity_cycle(NOW))
    assert mood.records == [("surf", True, "咖啡")]


def test_mood_records_failure_for_failed_activity():
    act = ScriptedActivity("game", fail=True)
    mood = RecordingMood()
    loop = make_loop([act], mood=mood, decider=ScriptedDecider(act))

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    assert mood.records == [("game", False, None)]


def test_low_valence_success_boosts_memory_importance():
    """低谷时的小确幸记得更牢：valence<0 且活动成功 → 重要度 +0.1。"""
    act = ScriptedActivity("surf")
    mood = RecordingMood(valence=-0.5)
    memory = FakeMemory()
    loop = make_loop([act], memory=memory, mood=mood, decider=ScriptedDecider(act))

    asyncio.run(loop.run_activity_cycle(NOW))
    assert memory.added[0][1] == 0.6  # 0.5 + 0.1，且被钳制在 [0,1]


def test_positive_valence_no_importance_boost():
    act = ScriptedActivity("surf")
    mood = RecordingMood(valence=0.5)
    memory = FakeMemory()
    loop = make_loop([act], memory=memory, mood=mood, decider=ScriptedDecider(act))

    asyncio.run(loop.run_activity_cycle(NOW))
    assert memory.added[0][1] == 0.5  # 心情好时正常记


def test_low_valence_failure_no_boost():
    """重要度加成只给成功活动：失败不加。"""
    act = ScriptedActivity("game", fail=True)
    mood = RecordingMood(valence=-0.5)
    memory = FakeMemory()
    loop = make_loop([act], memory=memory, mood=mood, decider=ScriptedDecider(act))

    asyncio.run(loop.run_activity_cycle(NOW))
    # 失败记忆基础重要度 0.2，失败不加成
    assert memory.added[0][1] == 0.2


def test_importance_clamped_to_one():
    """加成后不超过 1.0：outcome.importance=0.95 + 0.1 → 1.0。"""
    act = ScriptedActivity("surf")
    act_outcome = ActivityOutcome(
        name="surf", summary="s", memory_content="m", importance=0.95
    )

    class FixedAct(ScriptedActivity):
        async def run(self, ctx):
            self.runs += 1
            self.last_ctx = ctx
            return act_outcome

    fixed = FixedAct("surf")
    mood = RecordingMood(valence=-0.5)
    memory = FakeMemory()
    loop = make_loop([fixed], memory=memory, mood=mood, decider=ScriptedDecider(fixed))

    asyncio.run(loop.run_activity_cycle(NOW))
    assert memory.added[0][1] == 1.0


def test_mood_update_failure_does_not_break_cycle():
    """心境后端写挂：活动照常完成、记忆照写。"""
    act = ScriptedActivity("surf")

    class BoomMood:
        valence = 0.2
        energy = 0.8

        async def record_activity(self, *a, **kw):
            raise RuntimeError("mood db gone")

        def digest(self):
            return ""

    memory = FakeMemory()
    loop = make_loop([act], memory=memory, mood=BoomMood(),
                     decider=ScriptedDecider(act))
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert len(memory.added) == 1


def test_decider_error_falls_back_to_internal_pick():
    """决策器抛异常：退回内部随机选择，周期照常完成。"""
    act = ScriptedActivity("surf")
    loop = make_loop([act], decider=ScriptedDecider(act, error=True))
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert act.runs == 1  # 池里只有它，内部随机也会选它


def test_no_mood_no_decider_back_to_m1_behavior():
    """mood/decider 都不注入：完全退回 M1 行为（向后兼容）。"""
    act = ScriptedActivity("surf")
    loop = make_loop([act])
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert act.last_ctx.params == {}
