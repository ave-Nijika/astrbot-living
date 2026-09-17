"""M3 补丁 X 测试：自主作息（睡眠动力学）+ fixed 模式零回归。"""

import asyncio
from datetime import datetime, timedelta

import pytest

import random

import pytest

from core.living_state import LivingGate
from core.mood import MoodState, restore_after_sleep, apply_nap_effects
from core.sleep import SleepManager, circadian_factor


def make_manager(gate, mood, now_provider, config=None):
    return SleepManager(
        config_getter=lambda: config or BASE_CONFIG,
        gate=gate,
        mood=mood,
        now_provider=now_provider,
        rng=random.Random(1),  # Random 实例（.random()/.uniform() 均可用）
    )


def make_gate(config=None):
    return LivingGate(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        db_path=":memory:", rng=lambda: 0.5,
    )


def make_mood(tmp_path=None):
    return MoodState(db_path=":memory:")


AUTONOMOUS_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {
        "sleep_mode": "autonomous",
        "sleep_window": "02:00-06:00",
        "min_sleep_hours": 5.0,
        "max_sleep_hours": 11.0,
        "min_awake_minutes": 240,
        "sleepiness_threshold": 0.45,
        "sleepiness_jitter": 0.0,  # 确定性测试
        "weights": {"energy": 0.35, "debt": 0.35, "circadian": 0.30},
        "nap_enabled": True,
        "nap_min_minutes": 20,
        "nap_max_minutes": 90,
        "circadian_hint": "23:00-07:00",
        "wake_n_messages": 3, "wake_window_minutes": 10,
        "grouchiness_percent": 0, "sleep_mute_replies": True,
        "fatigue_rate_per_hour": 4.0, "sleep_debt_decay_per_day": 30.0,
        "awake_standby_minutes": 30, "wake_ack_message": "",
        "sleep_farewell_message": "",
    },
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": "",
                    "share_rewrite_enabled": False, "share_rewrite_prompt": "",
                    "share_max_length": 120},
}

FIXED_CONFIG = {
    **AUTONOMOUS_CONFIG,
    "sleep": {**AUTONOMOUS_CONFIG["sleep"], "sleep_mode": "fixed"},
}


def set_time(dt):
    return lambda: dt


# ---------------------------------------------------------------------------
# 昼夜节律因子
# ---------------------------------------------------------------------------
def test_circadian_factor_curve():
    hint = "23:00-07:00"
    assert circadian_factor(datetime(2026, 9, 17, 23, 30), hint) == pytest.approx(1.0)
    assert circadian_factor(datetime(2026, 9, 18, 3, 0), hint) == pytest.approx(1.0)
    assert circadian_factor(datetime(2026, 9, 17, 15, 0), hint) == pytest.approx(0.1)
    # 边界 1 小时渐变（22:30 处于窗口前缘 1h 内 → 介于 0.3 与 1.0）
    edge = circadian_factor(datetime(2026, 9, 17, 22, 30), hint)
    assert 0.3 < edge < 1.0


# ---------------------------------------------------------------------------
# 睡意模型
# ---------------------------------------------------------------------------
def test_sleepiness_components_and_determinism(tmp_path):
    config = {
        **AUTONOMOUS_CONFIG,
        "sleep": {**AUTONOMOUS_CONFIG["sleep"], "sleepiness_jitter": 0.0},
    }
    gate = make_gate(config)
    mood = make_mood()
    asyncio.run(mood.load())
    mood.energy = 0.2
    mood.sleep_debt = 40.0
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 23, 30)),
                           config=config)

    asyncio.run(gate.enter_autonomous_sleep(
        datetime(2026, 9, 18, 3, 0), "long",
        datetime(2026, 9, 17, 20, 0)))
    asyncio.run(gate.exit_autonomous_sleep(
        datetime(2026, 9, 17, 20, 30)))  # 醒来 30 分钟

    value, detail = asyncio.run(manager.sleepiness(mood, datetime(2026, 9, 17, 23, 30)))
    # 手算：e=0.35*0.8=0.28, d=0.35*0.4=0.14, c=0.30*1.0=0.30, jitter=0
    assert value == pytest.approx(0.28 + 0.14 + 0.30)
    assert detail["energy"] == 0.2


def test_should_fall_asleep_respects_min_awake(tmp_path):
    gate = make_gate(AUTONOMOUS_CONFIG)
    mood = make_mood()
    asyncio.run(mood.load())
    mood.energy = 0.0
    mood.sleep_debt = 100.0
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 23, 30)))

    # 刚醒 30 分钟 < 240 分钟 → 即使睡意爆表也不睡
    asyncio.run(gate.record_wakeup(datetime(2026, 9, 17, 23, 0)))
    asleep, value, detail = asyncio.run(
        manager.should_fall_asleep(mood, datetime(2026, 9, 17, 23, 30))
    )
    assert asleep is False
    assert "min_awake_wait" in detail

    # 超过 240 分钟 → 入睡
    asleep2, _, _ = asyncio.run(
        manager.should_fall_asleep(mood, datetime(2026, 9, 18, 4, 0))
    )
    assert asleep2 is True


def test_sleep_duration_scales_with_debt(tmp_path):
    gate = make_gate(AUTONOMOUS_CONFIG)
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.sleep_debt = 0.0
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 23, 0)))
    low = manager.sleep_duration_hours(mood, datetime(2026, 9, 17, 23, 0))
    mood.sleep_debt = 100.0
    high = manager.sleep_duration_hours(mood, datetime(2026, 9, 17, 23, 0))
    # 无债 → 趋向 min；满债 → 趋向 max（jitter ±0.5 内）
    assert 4.5 <= low <= 6.0
    assert 10.5 <= high <= 11.5
    assert high > low


# ---------------------------------------------------------------------------
# 入睡 / 醒来结算 / 白天小睡
# ---------------------------------------------------------------------------
def test_begin_autonomous_sleep_enters_gate(tmp_path):
    gate = make_gate(AUTONOMOUS_CONFIG)
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.energy = 0.1
    mood.sleep_debt = 60.0
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 23, 30)))

    now = datetime(2026, 9, 17, 23, 30)
    result = asyncio.run(manager.begin_autonomous_sleep(mood, now))

    assert result["asleep"] is True
    assert gate.asleep_in_autonomous(now + timedelta(hours=1))
    assert not gate.asleep_in_autonomous(now + timedelta(hours=12))
    assert gate.in_sleep_window(now + timedelta(hours=1)) is True  # in_sleep_window 融合


def test_nap_requires_low_energy_and_cooldown(tmp_path):
    gate = make_gate(AUTONOMOUS_CONFIG)
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 14, 0)))

    # energy 高 → 不小睡
    mood.energy = 0.9
    assert manager.should_nap(mood, datetime(2026, 9, 17, 14, 0))[0] is False
    # energy 低但刚醒 100 分钟 < 300 → 不小睡
    mood.energy = 0.1
    asyncio.run(gate.record_wakeup(datetime(2026, 9, 17, 12, 20)))
    assert manager.should_nap(mood, datetime(2026, 9, 17, 14, 0))[0] is False
    # 醒来 5 小时后 → 小睡
    assert manager.should_nap(mood, datetime(2026, 9, 17, 17, 30))[0] is True


def test_woken_from_autonomous_debt_scales(tmp_path):
    gate = make_gate(AUTONOMOUS_CONFIG)
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    mood.sleep_debt = 0.0
    manager = make_manager(gate, mood, set_time(datetime(2026, 9, 17, 14, 0)))

    # 计划 8h 只睡 4h → 保留一半债
    result = asyncio.run(
        manager.apply_woken_from_autonomous(mood, 4.0, 8.0,
                                            datetime(2026, 9, 17, 14, 0))
    )
    assert result["debt_added"] == pytest.approx(50.0)
    assert mood.sleep_debt == pytest.approx(50.0)
    # 睡满 → 无残余
    result2 = asyncio.run(
        manager.apply_woken_from_autonomous(mood, 8.0, 8.0,
                                            datetime(2026, 9, 17, 14, 0))
    )
    assert result2["debt_added"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# fixed 模式零变化（红线 1 的回归证明）
# ---------------------------------------------------------------------------
def test_fixed_mode_ignores_autonomous_state(tmp_path):
    """fixed 模式下：即使 gate 里有残留的自主睡眠状态，也不影响窗口判定。"""
    gate = make_gate(FIXED_CONFIG)
    now = datetime(2026, 9, 17, 23, 30)  # 不在 fixed 窗（02:00-06:00）
    asyncio.run(gate.enter_autonomous_sleep(
        now + timedelta(hours=8), "long", now))
    assert gate.in_sleep_window(now) is False
    asyncio.run(gate.exit_autonomous_sleep(now))


def test_autonomous_mode_ignores_fixed_window(tmp_path):
    """autonomous 模式下：固定窗外/窗内都不影响——在睡只看动力学状态。"""
    gate = make_gate(AUTONOMOUS_CONFIG)
    now = datetime(2026, 9, 17, 14, 0)  # 白天，固定窗外
    asyncio.run(gate.enter_autonomous_sleep(
        now + timedelta(hours=3), "nap", now))
    assert gate.in_sleep_window(now) is True  # 在自主睡眠中 → 视为在睡


# ---------------------------------------------------------------------------
# 7 天作息推演（报告用模拟数据的自动化生成）
# ---------------------------------------------------------------------------
def test_seven_day_autonomous_schedule_simulation(tmp_path):
    """连续 7 天的作息推演：活力活动消耗 energy、夜间动力学入睡、
    醒来恢复——熬夜/睡懒觉/白天补觉应自然涌现（非脚本化时刻表）。"""
    from core.sleep import SleepManager as SM

    config = {
        **AUTONOMOUS_CONFIG,
        "sleep": {
            **AUTONOMOUS_CONFIG["sleep"],
            "sleepiness_jitter": 0.0,  # 推演确定性
            "min_awake_minutes": 240,
        },
    }
    gate = make_gate(config)
    mood = make_mood(tmp_path)
    asyncio.run(mood.load())
    manager = SM(config_getter=lambda: config, gate=gate, mood=mood)

    t = datetime(2026, 9, 10, 8, 0, 0)
    schedule = []
    rng = random.Random(7)
    for day in range(7):
        awake_hours = 0.0
        # 白天：每小时醒来活动（消耗 energy 0.08/小时）
        while awake_hours < 14:
            # 到点自然醒检查
            if gate.asleep_in_autonomous(t):
                state = gate.sleep_state(t)
                if t >= state["until"]:
                    fell = state["fell_asleep_at"]
                    actual_h = (t - fell).total_seconds() / 3600.0
                    planned = (state["until"] - fell).total_seconds() / 3600.0
                    asyncio.run(restore_after_sleep(mood, actual_h, planned))
                    gate.exit_autonomous_sleep(t)
                    schedule.append((t.strftime("%m-%d %H:%M"), "wake",
                                     round(actual_h, 1), round(mood.energy, 2)))
                    break  # 已醒来，开始新一天
                t += timedelta(minutes=30)
                continue
            # 醒着：消耗精力 + 疲劳积累（真实感）
            mood.energy = max(mood.energy - 0.07, 0.05)
            mood.add_fatigue(3.0)
            nap, minutes = manager.should_nap(mood, t)
            if nap:
                t += timedelta(minutes=minutes)
                asyncio.run(apply_nap_effects(mood, minutes))
                schedule.append((t.strftime("%m-%d %H:%M"), "nap",
                                 round(minutes), round(mood.energy, 2)))
            awake_hours += 1
            t += timedelta(hours=1)
        # 夜间：睡意评估 → 入睡
        result = asyncio.run(manager.begin_autonomous_sleep(mood, t))
        duration = result.get("duration_h", 0.0)
        schedule.append((t.strftime("%m-%d %H:%M"), "sleep",
                         round(duration, 1), round(mood.energy, 2)))
        if result.get("asleep"):
            until = result["until"]
            actual_h = duration
            planned = duration
            asyncio.run(restore_after_sleep(mood, actual_h, planned))
            schedule.append((until.strftime("%m-%d %H:%M"), "wake",
                             round(actual_h, 1), round(mood.energy, 2)))
            t = until  # 跳到自然醒

    sleeps = [s for s in schedule if s[1] == "sleep"]
    assert len(sleeps) == 7
    # 睡眠时长应随债务波动（不是每晚都一样）
    durations = [s[2] for s in sleeps]
    assert max(durations) - min(durations) > 0.5
    # 写入报告推演表（供人工查看）
    lines = [f"{row[0]} {row[1]:>6} dur/h={row[2]} energy={row[3]}"
             for row in schedule]
    (tmp_path / "schedule.txt").write_text("\n".join(lines), encoding="utf-8")
