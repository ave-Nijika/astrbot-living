"""M3-A/B 主循环测试：双事件打断睡眠、force 语义、睡前回顾、梦。"""

import asyncio
from datetime import datetime
from typing import Any

from core import living_loop as living_loop_module
from core.living_loop import LivingLoop

NOW = datetime(2026, 9, 8, 14, 0, 0)

BASE_CONFIG = {
    "decision": {
        "daily_impulse_limit": 3,
        "activity_probability": 0.8,
        "impulse_check_interval_minutes": 45,
        "max_run_seconds": 300,
        "decision_mode": "rules",
        "agent_activities": ["surf", "read", "game"],
    },
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.3},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


class FakeGate:
    def __init__(self, allow=True, reason="ok", in_sleep=False):
        self.allow = allow
        self.reason = reason
        self.in_sleep = in_sleep
        self.calls = []  # (now, force)

    async def should_wake(self, now=None, force=False):
        self.calls.append((now, force))
        if self.in_sleep:
            if force:
                return True, "woken_from_sleep"
            return False, "sleeping"
        return self.allow, self.reason

    def in_sleep_window(self, now=None):
        return self.in_sleep

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


class FakeMemory:
    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return self.rows[:k]

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="a1"):
        self.name = name
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        from core.activities import ActivityOutcome

        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


class FakeSleepManager:
    def __init__(self):
        self.woken = 0

    async def apply_woken_in_sleep(self, now=None):
        self.woken += 1
        return {"grouchy": False, "debt_added": 0.0, "remaining_minutes": 30.0}


def make_loop(gate=None, memory=None, activities=None, config=None,
              sleep_manager=None, dream_llm=None, agent_loop=None):
    memory = memory if memory is not None else FakeMemory()
    return LivingLoop(
        gate=gate or FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG if config is None else config,
        activities=activities if activities is not None else [ScriptedActivity()],
        mood=None,
        decider=None,
        sleep_manager=sleep_manager,
        agent_loop=agent_loop,
        dream_llm_call=dream_llm,
    )


# ---------------------------------------------------------------------------
# A1/A5：双事件
# ---------------------------------------------------------------------------
def test_config_event_resets_timer_without_judging():
    """配置变更 → 等待立即结束且**未**执行任何判定（任务书 A5）。"""
    gate = FakeGate()

    async def flow():
        loop = make_loop(gate=gate)
        await loop.start()
        await asyncio.sleep(0.05)  # 让 _run 进入等待
        loop.notify_config_changed()
        await asyncio.sleep(0.1)  # 给 _run 处理事件的时间
        judged = gate.calls
        await loop.stop()
        return judged

    assert asyncio.run(flow()) == []  # 零判定


def test_wake_event_triggers_force_heartbeat():
    """wake_event → force 判定，force 标志传给闸门。"""
    gate = FakeGate(allow=True)

    async def flow():
        loop = make_loop(gate=gate)
        await loop.start()
        await asyncio.sleep(0.05)
        loop.request_wake()
        await asyncio.sleep(0.15)
        calls = list(gate.calls)
        await loop.stop()
        return calls

    calls = asyncio.run(flow())
    assert calls and calls[0][1] is True  # force=True


def test_five_config_changes_zero_activities():
    """连续改配置 5 次 → 定时器重置 5 次、0 活动、0 判定（任务书 A5）。"""
    gate = FakeGate()
    activity = ScriptedActivity()

    async def flow():
        loop = make_loop(gate=gate, activities=[activity])
        await loop.start()
        await asyncio.sleep(0.05)
        for _ in range(5):
            loop.notify_config_changed()
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        runs = activity.runs
        await loop.stop()
        return runs

    assert asyncio.run(flow()) == 0


def test_config_watcher_detects_change(monkeypatch):
    """兜底 watcher：配置序列化哈希变化 → 自动 notify（任务书 A4）。"""
    monkeypatch.setattr(living_loop_module, "CONFIG_POLL_SECONDS", 0.05)
    config = dict(BASE_CONFIG)
    gate = FakeGate()

    async def flow():
        loop = make_loop(gate=gate, config=config)
        notifies = []
        original_notify = loop.notify_config_changed

        def counting_notify():
            notifies.append(1)
            original_notify()

        loop.notify_config_changed = counting_notify
        await loop.start()
        await asyncio.sleep(0.05)
        config["decision"]["activity_probability"] = 0.1  # 原地修改（AstrBot 惯例）
        await asyncio.sleep(0.3)  # 等 watcher 轮询（0.05s 间隔）
        await loop.stop()
        return notifies

    assert asyncio.run(flow()), "watcher 应检测到配置变更并通知"


# ---------------------------------------------------------------------------
# A2/B3：force 语义与吵醒结算
# ---------------------------------------------------------------------------
def test_force_wake_in_sleep_window_triggers_settlement():
    """休眠窗内 force → 闸门给 woken_from_sleep → 吵醒结算被调用。"""
    gate = FakeGate(in_sleep=True)
    sleep_manager = FakeSleepManager()
    loop = make_loop(gate=gate, sleep_manager=sleep_manager)

    awake, reason, _act = asyncio.run(loop.heartbeat_once_detailed(NOW, force=True))
    assert awake is True
    assert reason == "woken_from_sleep"
    assert sleep_manager.woken == 1


def test_force_still_blocked_by_gate_without_sleep():
    """force 不是万能钥匙：非睡眠窗的拦截（上限/冷却）照常生效。"""
    gate = FakeGate(allow=False, reason="daily_limit")
    loop = make_loop(gate=gate)
    awake, reason, _act = asyncio.run(loop.heartbeat_once_detailed(NOW, force=True))
    assert awake is False
    assert reason == "daily_limit"


def test_natural_wake_after_sleep_sets_pending_dream():
    """自然醒（睡眠窗结束后的第一个心跳）→ 掷梦。"""
    gate = FakeGate(in_sleep=True)
    memory = FakeMemory(rows=[{"content": "9月7日我搜了系外行星", "score": 1}])
    dreams = []

    async def dream_llm(prompt, system_prompt=None):
        dreams.append(prompt)
        return "梦见我在一片星海里烤咖啡豆。"

    async def flow():
        loop = make_loop(gate=gate, memory=memory, dream_llm=dream_llm)

        class AlwaysHitRng:
            """random() 恒 0.0：闸门概率与梦概率全命中。"""

            def random(self):
                return 0.0

            def choice(self, seq):
                return seq[0]

        loop._rng = AlwaysHitRng()
        await loop.heartbeat_once_detailed(NOW, force=False)  # 睡眠窗内：入睡+回顾
        gate.in_sleep = False  # 天亮了
        gate.allow = True
        return await loop.heartbeat_once_detailed(NOW, force=False)

    awake, _reason, _act = asyncio.run(flow())
    assert awake is True
    assert dreams, "自然醒应掷梦（概率 mock 恒中）"
    assert any("梦" in c for c, _ in memory.added)


def test_dream_probability_miss_is_silent():
    """概率落空 → 不调 LLM、不写梦。"""
    gate = FakeGate(in_sleep=True)
    memory = FakeMemory(rows=[{"content": "旧记忆", "score": 1}])
    dreams = []

    async def dream_llm(prompt, system_prompt=None):
        dreams.append(prompt)
        return "梦"

    class RigidRng:
        """random() 恒 0.99：0.99 >= 0.3 → 概率落空。"""

        def random(self):
            return 0.99

        def choice(self, seq):
            return seq[0]

    async def flow():
        loop = make_loop(gate=gate, memory=memory, dream_llm=dream_llm)
        loop._rng = RigidRng()
        await loop.heartbeat_once_detailed(NOW, force=True)  # 窗内吵醒→pending_dream
        gate.in_sleep = False
        gate.allow = True
        return await loop.heartbeat_once_detailed(NOW, force=False)

    asyncio.run(flow())
    assert dreams == []  # 概率落空，梦的 LLM 没被调用


def test_bedtime_review_written_on_sleep_onset():
    """入睡（睡眠窗内第一个心跳）→ 写一条睡前回顾记忆（任务书 B2）。"""
    gate = FakeGate(in_sleep=True)
    memory = FakeMemory(rows=[
        {"content": "9月8日我搜了「宇宙探索」", "score": 1},
        {"content": "9月8日我读了《三体》", "score": 1},
    ])
    loop = make_loop(gate=gate, memory=memory)

    asyncio.run(loop.heartbeat_once_detailed(NOW, force=False))
    reviews = [c for c, _ in memory.added if "睡前" in c]
    assert reviews and "9月8日" in reviews[0]
    # 二次心跳不重复写回顾
    asyncio.run(loop.heartbeat_once_detailed(NOW, force=False))
    assert len([c for c, _ in memory.added if "睡前" in c]) == 1
