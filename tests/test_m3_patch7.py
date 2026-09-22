"""M3 补丁 VII 测试：兴趣饱和、重复惩罚、探索配额、执行层换题、数据降温。"""

import asyncio
import random
from datetime import datetime
from typing import Any

import pytest

from core.activities import ActivityContext, SurfActivity
from core.activities import default_activities
from core.decider import ActivityDecider
from core.mood import ENERGY_FLOOR, MoodState

T0 = datetime(2026, 9, 16, 14, 0, 0)

BASE_CONFIG = {
    "decision": {
        "daily_impulse_limit": 3,
        "activity_probability": 1.0,
        "impulse_check_interval_minutes": 5,
        "max_run_seconds": 300,
        "decision_mode": "rules",
        "interest_daily_decay": 0.9,
        "recent_topic_window": 6,
        "recent_topic_penalty": [0.5, 0.3, 0.15],
        "exploration_window": 4,
        "exploration_trigger": 3,
        "interest_cooldown_threshold": 0.85,
        "interest_cooldown_factor": 0.4,
    },
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


def make_mood(tmp_path, now=T0, decay=0.9):
    return MoodState(
        db_path=str(tmp_path / "mood.db"),
        now_provider=(lambda: now) if now else None,
    )


# ---------------------------------------------------------------------------
# 需求 1：饱和曲线
# ---------------------------------------------------------------------------
def test_bump_saturated_at_one_gains_zero(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.bump_interest("memory palace techniques", 0.15)
    mood.interests["memory palace techniques"] = 1.0
    mood.bump_interest("memory palace techniques", 0.15)
    asyncio.run(mood.close())  # M8-补丁1：连接收尾
    assert mood.interests["memory palace techniques"] == pytest.approx(1.0)


def test_bump_half_interest_gains_half(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.interests["咖啡"] = 0.5
    mood.bump_interest("咖啡", 0.15)
    value = mood.interests["咖啡"]
    asyncio.run(mood.close())  # M8-补丁1：连接收尾
    assert value == pytest.approx(0.575)  # 0.5 + 0.15*0.5


def test_bump_new_topic_gains_full(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.bump_interest("深海生物", 0.15)
    value = mood.interests["深海生物"]
    asyncio.run(mood.close())  # M8-补丁1：连接收尾
    assert value == pytest.approx(0.15)


def test_interest_daily_decay_configurable(tmp_path):
    """M5-补丁2 C3：跨日一次性衰减已移除；衰减系数改由
    decay_interests_elapsed 按经过时长生效且可配置。"""
    from datetime import timedelta

    d1 = datetime(2026, 9, 16, 23, 0, 0)
    d2 = datetime(2026, 9, 17, 8, 0, 0)

    async def flow():
        mood = MoodState(str(tmp_path / "mood.db"), now_provider=lambda: d1)
        await mood.load()
        mood.bump_interest("宇宙探索", 1.0)
        await mood.save()
        await mood.close()
        mood2 = MoodState(str(tmp_path / "mood.db"), now_provider=lambda: d2)
        await mood2.load(interest_daily_decay=0.5)
        rollover_value = mood2.get_interests()["宇宙探索"]  # 跨日 load 不再衰减
        await mood2._set_raw(
            "interests_decay_at", repr((d2 - timedelta(hours=24)).timestamp())
        )
        await mood2.decay_interests_elapsed(d2, daily_decay=0.5)
        after = mood2.get_interests()["宇宙探索"]
        await mood2.close()
        return rollover_value, after

    rollover_value, after = asyncio.run(flow())
    assert rollover_value == pytest.approx(1.0)  # 跨日不衰减（C3）
    assert after == pytest.approx(0.5)  # 24h → ×0.5（可配置系数）


# ---------------------------------------------------------------------------
# 需求 2：近期话题追踪与重复惩罚
# ---------------------------------------------------------------------------
def test_record_recent_topics_window_slides(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    for topic in ["a", "b", "c", "d", "e", "f", "g"]:
        mood.record_recent_topics([topic], window=6)
    assert mood.recent_topics_list() == ["b", "c", "d", "e", "f", "g"]
    asyncio.run(mood.close())


def test_interest_weight_repeat_penalty_table(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.interests["偏执主题"] = 1.0
    assert mood.interest_weight("偏执主题", repeat_count=0) == pytest.approx(1.0)
    assert mood.interest_weight("偏执主题", repeat_count=1) == pytest.approx(0.5)
    assert mood.interest_weight("偏执主题", repeat_count=2) == pytest.approx(0.3)
    assert mood.interest_weight("偏执主题", repeat_count=5) == pytest.approx(0.15)
    asyncio.run(mood.close())  # M8-补丁1：连接收尾


def test_recent_topics_persist(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.record_recent_topics(["冷知识", "咖啡"], window=6)
    await_save = mood.save()
    asyncio.run(await_save)
    mood2 = make_mood(tmp_path)
    asyncio.run(mood2.load())
    topics = mood2.recent_topics_list()
    asyncio.run(mood2.close())
    asyncio.run(mood.close())  # M8-补丁1：连接收尾（mood 本体）
    assert topics == ["冷知识", "咖啡"]


# ---------------------------------------------------------------------------
# 需求 3：探索配额（decider）
# ---------------------------------------------------------------------------
class FakeMood:
    def __init__(self, recent=None, energy=0.8, valence=0.2):
        self._recent = recent or []
        self.energy = energy
        self.valence = valence

    def recent_topics_list(self):
        return list(self._recent)

    def digest(self):
        return "测试心境"

    def interest_weight(self, topic, repeat_count=0, penalty_table=(0.5, 0.3, 0.15)):
        return 0.3


def make_decider(config, mood=None):
    from core.activities import default_activities

    return ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: config,
        rng=random.Random(1),
        llm_call=None,
        mood=mood,
    )


def test_exploration_trigger_boundary():
    """同 topic 最近 4 次窗口内出现 3 次 → 触发；2 次 → 不触发。"""
    config = {
        "decision": {"decision_mode": "rules", "exploration_window": 4,
                     "exploration_trigger": 3},
    }
    mood2 = FakeMood(recent=["memory palace techniques"] * 2 + ["x", "y"])
    decider2 = make_decider(config, mood=mood2)
    triggered2, hot2 = decider2._exploration_needed()
    assert triggered2 is False
    assert hot2 == ""

    mood3 = FakeMood(recent=["memory palace techniques"] * 3 + ["x"])
    decider3 = make_decider(config, mood=mood3)
    triggered3, hot3 = decider3._exploration_needed()
    assert triggered3 is True
    assert hot3 == "memory palace techniques"


def test_exploration_directive_text():
    mood = FakeMood(recent=["memory palace techniques"] * 3)
    decider = make_decider(config=BASE_CONFIG, mood=mood)
    directive = decider._exploration_directive()
    assert "memory palace techniques" in directive
    assert "从没接触过的新话题" in directive
    # 未触发时无指令
    mood2 = FakeMood(recent=[])
    decider2 = make_decider(config=BASE_CONFIG, mood=mood2)
    assert decider2._exploration_directive() == ""


def test_llm_prompt_contains_recent_topics_and_avoidance(monkeypatch):
    """llm 档 prompt 含近期话题清单与避开指示（mock LLM 捕获断言）。"""
    captured = {}

    class FakeMemory:
        async def search(self, query, k=5):
            return [{"content": "旧记忆"}]

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        return '{"activity": "read", "params": {"topic": "火山学"}}'

    async def memory_getter():
        return FakeMemory()

    mood = FakeMood(recent=["memory palace techniques", "memory palace techniques"])
    decider = ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: {**BASE_CONFIG, "decision": {
            **BASE_CONFIG["decision"], "decision_mode": "llm"}},
        rng=random.Random(1),
        llm_call=llm,
        mood=mood,
        memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
    )
    asyncio.run(decider.decide(T0))
    assert "折腾过这些话题" in captured["prompt"]
    assert "避开" in captured["prompt"]


# ---------------------------------------------------------------------------
# 需求 4：执行层换题
# ---------------------------------------------------------------------------
def test_pick_topic_avoids_recent():
    """近期出现过的池内主题被加权压制：近期占满 6/7 时只剩冷门可选。"""
    from core.activities import ActivityContext, TOPIC_POOL

    recent = TOPIC_POOL[:6]  # 6 个主题都刚用过
    rng = random.Random(1)

    class StubMood:
        def interest_weight(self, topic, repeat_count=0, penalty_table=(0.5, 0.3, 0.15)):
            return 0.3

    ctx = ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=None, gate=None,
        event=None, rng=rng, now=T0, recent_topics=recent,
        mood=StubMood(),
    )
    picks = {ctx.pick_topic() for _ in range(50)}
    assert picks.issubset(set(recent) is not None and set(TOPIC_POOL) - set(recent) or set())


def test_pick_topic_recent_penalized_not_chosen_dominantly():
    """近期主题权重大幅降低：多次采样中冷门主题占比应显著更高。"""
    from core.activities import ActivityContext, TOPIC_POOL

    recent = [TOPIC_POOL[0]] * 3  # 某主题连用 3 次 → 惩罚 0.15
    rng = random.Random(42)

    class StubMood:
        def interest_weight(self, topic, repeat_count=0, penalty_table=(0.5, 0.3, 0.15)):
            return 0.3

    ctx = ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=None, gate=None,
        event=None, rng=rng, now=T0, recent_topics=recent,
        interest_penalty_table=(0.5, 0.3, 0.15), mood=StubMood(),
    )
    hot_picks = sum(1 for _ in range(200) if ctx.pick_topic() == TOPIC_POOL[0])
    # 7 选 1 均匀分布下期望约 200/7≈28；被惩罚后应显著低于均匀值
    assert hot_picks < 15


# ---------------------------------------------------------------------------
# 需求 5：数据降温
# ---------------------------------------------------------------------------
def test_cooldown_hot_interests(tmp_path):
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.interests["memory palace techniques"] = 1.0
    mood.interests["咖啡"] = 0.3
    cooled = mood.cooldown_hot_interests(0.85, 0.4)
    assert cooled == ["memory palace techniques"]
    assert mood.interests["memory palace techniques"] == pytest.approx(0.4)
    assert mood.interests["咖啡"] == pytest.approx(0.3)  # <0.85 不动
    asyncio.run(mood.close())


def test_interest_cooldown_once_idempotent(tmp_path):
    from core.selfheal import cooldown_interests_once

    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.interests["memory palace techniques"] = 1.0
    state_path = tmp_path / "selfheal_state.json"

    cooled1 = asyncio.run(
        cooldown_interests_once(mood, state_path, threshold=0.85, factor=0.4)
    )
    assert cooled1 == ["memory palace techniques"]
    assert mood.interests["memory palace techniques"] == pytest.approx(0.4)

    # 手动抬回去：幂等标记后不再降
    mood.interests["memory palace techniques"] = 1.0
    cooled2 = asyncio.run(
        cooldown_interests_once(mood, state_path, threshold=0.85, factor=0.4)
    )
    assert cooled2 == []
    assert mood.interests["memory palace techniques"] == pytest.approx(1.0)
    asyncio.run(mood.close())


def test_interest_cooldown_below_threshold_noop(tmp_path):
    from core.selfheal import cooldown_interests_once

    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.interests["咖啡"] = 0.3
    state_path = tmp_path / "selfheal_state.json"

    cooled = asyncio.run(
        cooldown_interests_once(mood, state_path, threshold=0.85, factor=0.4)
    )
    assert cooled == []
    asyncio.run(mood.close())
