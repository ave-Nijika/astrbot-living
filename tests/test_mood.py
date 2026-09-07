"""MoodState 心境状态机测试（任务书 M2-A）。"""

import asyncio
from datetime import datetime

import pytest

from core.mood import MoodState


def make_mood(tmp_path, now=None):
    return MoodState(
        db_path=str(tmp_path / "mood.db"),
        now_provider=(lambda: now) if now else None,
    )


def test_defaults_on_empty_db(tmp_path):
    """首次加载：valence=0.2 / arousal=0.5 / energy=0.8 / interests 空。"""

    async def flow():
        mood = make_mood(tmp_path)
        await mood.load()
        return mood.valence, mood.arousal, mood.energy, mood.get_interests()

    valence, arousal, energy, interests = asyncio.run(flow())
    assert valence == pytest.approx(0.2)
    assert arousal == pytest.approx(0.5)
    assert energy == pytest.approx(0.8)
    assert interests == {}


def test_success_updates_valence_energy_and_topic_interest(tmp_path):
    """冲浪成功：valence +0.05，energy -0.1，主题兴趣 +0.15，并持久化。"""
    d1 = datetime(2026, 9, 8, 12, 0, 0)

    async def flow():
        mood = make_mood(tmp_path, now=d1)
        await mood.load()
        await mood.record_activity("surf", ok=True, topic="深海生物")
        # 新实例验证持久化
        mood2 = make_mood(tmp_path, now=d1)
        await mood2.load()
        return mood2

    mood = asyncio.run(flow())
    assert mood.valence == pytest.approx(0.25)
    assert mood.energy == pytest.approx(0.7)
    assert mood.get_interests()["深海生物"] == pytest.approx(0.15)


def test_failure_updates_valence_energy_without_interest(tmp_path):
    mood = make_mood(tmp_path)

    async def flow():
        await mood.load()
        await mood.record_activity("read", ok=False, topic="咖啡")
        return mood.valence, mood.energy, mood.get_interests()

    valence, energy, interests = asyncio.run(flow())
    assert valence == pytest.approx(0.2 - 0.08)
    assert energy == pytest.approx(0.8 - 0.05)
    assert "咖啡" not in interests  # 失败不长兴趣


def test_reminisce_success_bumps_memory_interest(tmp_path):
    """reminisce 成功：'记忆' 兴趣 +0.1（回味本身也是兴趣）。"""
    mood = make_mood(tmp_path)

    async def flow():
        await mood.load()
        await mood.record_activity("reminisce", ok=True, topic=None)
        return mood.get_interests()

    assert asyncio.run(flow()).get("记忆") == pytest.approx(0.1)


def test_clamps_on_repeated_updates(tmp_path):
    """边界钳制：连续失败 valence ≥ -1；连续成功 energy ≥ 0；兴趣 ≤ 1。"""
    mood = make_mood(tmp_path)

    async def flow():
        await mood.load()
        for _ in range(50):
            await mood.record_activity("game", ok=False)
        low_valence, low_energy = mood.valence, mood.energy
        for _ in range(50):
            mood.bump_interest("咖啡", 0.3)
        await mood.record_activity("game", ok=True, topic="独立游戏")
        return low_valence, low_energy, mood.get_interests()

    low_valence, low_energy, interests = asyncio.run(flow())
    assert low_valence == pytest.approx(-1.0)
    assert low_energy == 0.0
    assert interests["咖啡"] == pytest.approx(1.0)


def test_interest_weight_neutral_default(tmp_path):
    """无记录主题返回 0.3 中性——没接触过的东西也值得一试。"""
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    assert mood.interest_weight("从没见过的话题") == pytest.approx(0.3)
    mood.bump_interest("咖啡", 0.5)
    assert mood.interest_weight("咖啡") == pytest.approx(0.5)


def test_daily_interest_decay_on_date_rollover(tmp_path):
    """每日兴趣衰减 ×0.9：跨日 load 触发一次，同日重复 load 不衰减。"""
    d1 = datetime(2026, 9, 8, 23, 0, 0)
    d2 = datetime(2026, 9, 9, 8, 0, 0)

    async def first_day():
        mood = make_mood(tmp_path, now=d1)
        await mood.load()
        mood.bump_interest("宇宙探索", 1.0)
        await mood.save()
        await mood.close()
        # 同日再 load 不应衰减
        again = make_mood(tmp_path, now=d1)
        await again.load()
        value = again.get_interests()["宇宙探索"]
        await again.close()
        return value

    async def next_day():
        mood = make_mood(tmp_path, now=d2)
        await mood.load()
        value = mood.get_interests()["宇宙探索"]
        await mood.close()
        return value

    same_day = asyncio.run(first_day())
    assert same_day == pytest.approx(1.0)
    next_day_value = asyncio.run(next_day())
    assert next_day_value == pytest.approx(0.9)


def test_digest_is_human_readable(tmp_path):
    """心境摘要给决策 LLM 看的应该是人话，包含状态词与兴趣。"""
    mood = make_mood(tmp_path)

    async def flow():
        await mood.load()
        mood.valence = -0.6
        mood.energy = 0.2
        mood.bump_interest("咖啡", 0.8)
        return mood.digest()

    text = asyncio.run(flow())
    assert "低落" in text
    assert "累了" in text
    assert "咖啡" in text


def test_corrupt_db_values_fall_back_to_defaults(tmp_path):
    """脏数据兜底：手写坏值进库后 load 不崩溃、回默认值。"""

    async def flow():
        mood = make_mood(tmp_path)
        await mood._get_db()
        await mood._set_raw("mood_valence", "不是数字")
        await mood._set_raw("interests", "{bad json")
        await mood.close()
        mood2 = make_mood(tmp_path)
        await mood2.load()
        await mood2.close()
        return mood2

    mood = asyncio.run(flow())
    assert mood.valence == pytest.approx(0.2)
    assert mood.get_interests() == {}
