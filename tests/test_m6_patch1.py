"""M6-补丁1 测试：工具轮数放开（预算兜底）+ fixed 移除后的行为验收。

验收 1-3 走 drive_agent_steps 真实步进路径；验收 6/7/8 走真实
LivingGate/SleepManager 状态机（enter_autonomous_sleep 触发在睡）。
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.agent_loop import drive_agent_steps
from core.living_state import LivingGate
from core.sleep import SleepManager

NOW = datetime(2026, 9, 22, 5, 50, 0)
WORKDIR = Path(__file__).resolve().parents[1]


class FakeRunner:
    """鸭子类型 runner：按预算/步数持续步进，语义与真实 runner 一致。"""

    def __init__(self, total_steps, tokens_per_step=1000, fail_after=None):
        self._total = total_steps
        self._per = tokens_per_step
        self._fail_after = fail_after
        self._steps_taken = 0
        self.stats = type("S", (), {})()
        self.stats.token_usage = type("T", (), {})()
        self.stats.token_usage.total = 0

    def done(self):
        # fail_after 模拟"任务提前完成"（步数耗尽即 done）
        if self._fail_after is not None:
            return self._steps_taken >= self._fail_after
        return self._steps_taken >= self._total


    async def step_until_done(self, max_steps):
        while not self.done() and self._steps_taken < max_steps:
            self._steps_taken += 1
            self.stats.token_usage.total += self._per
            yield self._steps_taken

    def request_stop(self):
        # 真实 runner 在检查点优雅退出：这里等价于步进停止
        self._total = self._steps_taken
        self._fail_after = self._steps_taken

    def get_final_llm_resp(self):
        return type("F", (), {"completion_text": "玩好了"})()


# ---------------------------------------------------------------------------
# 验收 1：轮数=0（不限）→ 持续运行直至预算耗尽中断
# ---------------------------------------------------------------------------
def test_unlimited_steps_stopped_by_token_budget():
    """max_tool_rounds=0 → 映射 10**9（不限）；20000 预算兜底中断。"""
    # 500 步 × 1000 token = 500000 >> 20000 预算：轮数不限时由预算闸截停
    runner = FakeRunner(total_steps=500, tokens_per_step=1000)
    steps, exceeded, llm_calls = asyncio.run(
        drive_agent_steps(runner, budget=20000, max_steps=10**9)
    )
    assert exceeded is True
    assert steps < 500  # 远未到"总步数"就被预算截停
    assert runner.stats.token_usage.total >= 20000
    assert llm_calls == steps


# ---------------------------------------------------------------------------
# 验收 2：轮数=5 → 5 轮后自然停止（capped_at_max_steps 语义）
# ---------------------------------------------------------------------------
def test_positive_step_cap_preserved():
    runner = FakeRunner(total_steps=50, tokens_per_step=10)
    steps, exceeded, _ = asyncio.run(
        drive_agent_steps(runner, budget=20000, max_steps=5)
    )
    assert steps == 5
    assert exceeded is False
    result_like = {"steps_used": steps, "max_steps": 5}
    # capped_at_max_steps 语义：steps_used >= max_steps 且未触预算
    assert result_like["steps_used"] >= result_like["max_steps"]


# ---------------------------------------------------------------------------
# 验收 3：预算兜底行为与现状逐位一致（默认 20000；budget>0 检查不变）
# ---------------------------------------------------------------------------
def test_budget_gate_unchanged():
    runner = FakeRunner(total_steps=100, tokens_per_step=5000)
    steps, exceeded, _ = asyncio.run(
        drive_agent_steps(runner, budget=20000, max_steps=10**9)
    )
    # 每步 5000：第 4 步累计 20000 达预算 → 中断
    assert steps == 4
    assert exceeded is True
    assert runner.stats.token_usage.total == 20000


def test_zero_budget_semantics_unchanged():
    """budget=0 = 不设预算（既有语义，红线 2：其他限制不动）。"""
    runner = FakeRunner(total_steps=3, tokens_per_step=5000)
    steps, exceeded, _ = asyncio.run(
        drive_agent_steps(runner, budget=0, max_steps=10**9)
    )
    assert steps == 3 and exceeded is False


# ---------------------------------------------------------------------------
# 验收 5/8：autonomous 静默链（真实 gate 状态；原 M5-补丁3 用例的延续）
# ---------------------------------------------------------------------------
def _gate(tmp_path, config=None):
    config = config or {
        "decision": {"daily_impulse_limit": 3},
        "sleep": {"min_awake_minutes": 0, "circadian_hint": "23:00-07:00"},
    }
    return LivingGate(
        config_getter=lambda: config,
        db_path=str(tmp_path / "gate.db"),
        rng=lambda: 0.5,
    )


def test_autonomous_sleep_chain(tmp_path):
    """在睡 → sleeping；force → woken_from_sleep；自然醒 → 恢复。"""

    async def flow(tmp_path):
        gate = _gate(tmp_path)
        now = NOW
        await gate.enter_autonomous_sleep(now + timedelta(hours=5), "long", now)
        sleeping = await gate.should_wake(now + timedelta(minutes=20))
        forced = await gate.should_wake(now + timedelta(minutes=20), force=True)
        # 跨过残留 fixed 窗口结束（08:00）但 until（10:50）未到 → 仍 sleeping
        across = await gate.should_wake(now + timedelta(hours=4))
        # 到点自然醒结算
        now2 = now + timedelta(hours=5)
        state = gate.sleep_state(now2)
        assert state["asleep"] is False  # expired（M5-补丁2 语义）
        await gate.exit_autonomous_sleep(now2)
        awake = await gate.should_wake(now2 + timedelta(minutes=1))
        await gate.close()
        return sleeping, forced, across, awake

    sleeping, forced, across, awake = asyncio.run(flow(tmp_path))
    assert sleeping == (False, "sleeping")
    assert forced == (True, "woken_from_sleep")
    assert across == (False, "sleeping")  # 验收 8：跨窗瞬间不放行
    assert awake[0] is True  # 自然醒后活动链恢复


# ---------------------------------------------------------------------------
# 验收 6：配置迁移——残留键的老配置加载后行为 = autonomous 且无报错
# ---------------------------------------------------------------------------
def test_legacy_config_residue_is_autonomous(tmp_path):
    config = {
        "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0},
        "capabilities": {"cooldown_between_activities_hours": 0.0},
        "sleep": {
            # 残留键：AstrBot check_config_integrity 会静默清理（配置存储层）；
            # 代码层也不再读取——行为必须等同 autonomous
            "sleep_mode": "fixed",
            "sleep_window": "00:30-08:00",
            "wake_n_messages": 3,
            "sleep_mute_replies": True,
        },
    }
    gate = _gate(tmp_path, config)

    async def flow():
        now = NOW
        await gate.enter_autonomous_sleep(now + timedelta(hours=5), "long", now)
        sleeping = await gate.should_wake(now + timedelta(minutes=20))
        # 残留的 sleep_window（00:30-08:00）不得产生 fixed 拦截：
        # 09:20 已出窗但仍在睡（until 10:50 未到）→ sleeping
        across = await gate.should_wake(now + timedelta(hours=3, minutes=30))
        await gate.exit_autonomous_sleep(now + timedelta(hours=5))
        awake = await gate.should_wake(now + timedelta(hours=5, minutes=1))
        await gate.close()
        return sleeping, across, awake

    sleeping, across, awake = asyncio.run(flow())
    assert sleeping == (False, "sleeping")
    assert across == (False, "sleeping")
    assert awake[0] is True


# ---------------------------------------------------------------------------
# 验收 7：告别消息迁移（长睡 enter 触发一次；小睡不触发；未配置不发送）
# ---------------------------------------------------------------------------
class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5, **kwargs):
        return []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)


class RecordingGate(LivingGate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_kinds = []

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self.entered_kinds.append(kind)
        await super().enter_autonomous_sleep(until, kind, now)


async def _loop(tmp_path, config, gate, sender):
    from core.living_loop import LivingLoop
    from core.mood import MoodState

    mood = MoodState(db_path=str(tmp_path / "mood.db"))
    await mood.load()
    mood.energy = 0.05
    manager = SleepManager(config_getter=lambda: config, gate=gate,
                           mood=mood, rng=lambda: 0.5)
    loop = LivingLoop(
        gate=gate, memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
        config_getter=lambda: config, sleep_manager=manager, mood=mood,
        sender=sender, rng=lambda: 0.0,
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop, mood


def test_farewell_sent_on_long_sleep_enter(tmp_path):
    config = {
        "decision": {"daily_impulse_limit": 3},
        "sleep": {"sleepiness_threshold": 0.0, "sleepiness_jitter": 0.0,
                  "min_awake_minutes": 0, "circadian_hint": "23:00-07:00",
                  "sleep_farewell_message": "我先去睡了，晚安。"},
    }
    gate = RecordingGate(config_getter=lambda: config,
                         db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
    sender = FakeSender()

    async def flow():
        loop, mood = await _loop(tmp_path, config, gate, sender)
        loop._sleep_manager.last_active_session = "aiocqhttp:GroupMessage:42"
        await loop._autonomous_sleep_tick(NOW)  # 长睡 enter
        sent_after_long = list(sender.sent)
        kinds = list(gate.entered_kinds)
        # 小睡场景（白天、睡意不达标 → nap enter）：不发送告别
        await gate.exit_autonomous_sleep(NOW + timedelta(hours=8, minutes=10))
        gate.entered_kinds.clear()
        config["sleep"]["sleepiness_threshold"] = 0.6  # 热读：睡意需达标才长睡
        mood.energy = 0.05
        mood.sleep_debt = 0.0
        await loop._autonomous_sleep_tick(NOW.replace(hour=14))
        sent_after_nap = list(sender.sent)
        nap_kinds = list(gate.entered_kinds)
        await mood.close()
        await gate.close()
        return kinds, sent_after_long, nap_kinds, sent_after_nap

    kinds, sent_long, nap_kinds, sent_nap = asyncio.run(flow())
    assert "long" in kinds
    assert sent_long == [("aiocqhttp:GroupMessage:42", "我先去睡了，晚安。")] or \
        any("我先去睡了" in t for _s, t in sent_long)
    assert "nap" in nap_kinds
    assert len(sent_nap) == len(sent_long)  # 小睡 enter 未追加告别


def test_farewell_empty_config_not_sent(tmp_path):
    """未配置告别（空串默认）→ 长睡 enter 也不发送。"""
    config = {
        "decision": {"daily_impulse_limit": 3},
        "sleep": {"sleepiness_threshold": 0.0, "sleepiness_jitter": 0.0,
                  "min_awake_minutes": 0, "circadian_hint": "23:00-07:00",
                  "sleep_farewell_message": ""},
    }
    gate = RecordingGate(config_getter=lambda: config,
                         db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
    sender = FakeSender()

    async def flow():
        loop, mood = await _loop(tmp_path, config, gate, sender)
        await loop._autonomous_sleep_tick(NOW)
        await mood.close()
        await gate.close()
        return sender.sent

    assert asyncio.run(flow()) == []
