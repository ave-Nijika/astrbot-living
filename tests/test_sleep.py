"""M3-B 休眠测试：吵醒滑动窗、起床气、睡眠债、静默拦截判定。"""

import asyncio
from collections import deque
from datetime import datetime
from typing import Any

import pytest

from core.living_state import LivingGate
from core.sleep import SleepManager

# 用一个明确不在默认休眠窗（00:30-08:00）之外的窗口避免混淆：
# 本文件统一用窗口 02:00-06:00，白天时间 12:00 = 窗外，04:00 = 窗内
CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 0.8},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {
        "sleep_window": "02:00-06:00",
        "wake_n_messages": 3,
        "wake_window_minutes": 10,
        "grouchiness_percent": 20,
        "wake_source": "all",
        "owner_id": "",
        "sleep_mute_replies": True,
        "fatigue_rate_per_hour": 4.0,
        "sleep_debt_decay_per_day": 30.0,
        "dream_probability": 0.3,
    },
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}

IN_WINDOW = datetime(2026, 9, 8, 4, 0, 0)   # 凌晨 4 点：窗内
OUT_WINDOW = datetime(2026, 9, 8, 12, 0, 0)  # 中午：窗外


def make_gate():
    return LivingGate(config_getter=lambda: CONFIG, db_path=":memory:",
                      rng=lambda: 0.5)


def make_manager(mood=None, rng=None, config=None, gate=None):
    return SleepManager(
        config_getter=lambda: CONFIG if config is None else config,
        gate=gate or make_gate(),
        mood=mood,
        rng=rng or (lambda: 0.5),
        now_provider=lambda: IN_WINDOW,
    )


class FakeMood:
    def __init__(self, valence=0.2, energy=0.8):
        self.valence = valence
        self.energy = energy
        self.sleep_debt = 0.0
        self.grouchy_calls = []

    def apply_grouchiness(self, enabled):
        self.grouchy_calls.append(enabled)
        if enabled:
            self.valence -= 0.15
            self.energy -= 0.1
        return enabled

    def add_sleep_debt(self, amount):
        self.sleep_debt += amount

    async def save(self):
        pass


# ---------------------------------------------------------------------------
# 吵醒计数（B3）
# ---------------------------------------------------------------------------
def test_wake_after_threshold_messages():
    """窗内 3 条消息（默认阈值）→ 第 3 条触发吵醒。"""
    manager = make_manager()
    times = [datetime(2026, 9, 8, 4, 0, i) for i in (0, 1, 2)]
    results = [manager.register_message(t) for t in times]
    assert [r[0] for r in results] == [False, False, True]


def test_wake_only_counts_inside_sleep_window():
    """窗外的消息不参与吵醒计数。"""
    manager = make_manager()
    for i in range(5):
        wake, _ = manager.register_message(datetime(2026, 9, 8, 12, 0, i))
        assert wake is False


def test_sliding_window_prunes_old_messages():
    """滑动窗：超过窗口时长的旧消息不再计数。"""
    manager = make_manager()
    # 第 1、2 条在 04:00/04:01；第 3 条在 04:15（超出 10 分钟窗，前两条已过期）
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 1, 0))
    wake, count = manager.register_message(datetime(2026, 9, 8, 4, 15, 0))
    assert wake is False
    assert count == 1  # 只剩自己


def test_wake_triggers_once_per_burst():
    """同一波消息只吵醒一次：触发后冷却一个窗口时长。"""
    manager = make_manager()
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 0, 30))
    wake1, _ = manager.register_message(datetime(2026, 9, 8, 4, 1, 0))
    # 触发后清窗，同窗内再来 3 条（04:02-04:03，在冷却期内）不应再次触发
    manager.register_message(datetime(2026, 9, 8, 4, 2, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 2, 30))
    wake2, _ = manager.register_message(datetime(2026, 9, 8, 4, 3, 0))
    assert wake1 is True
    assert wake2 is False


def test_wake_source_owner_only_filters_strangers():
    """owner_only：陌生人的消息不计入吵醒，主人的算。"""
    config = {
        **CONFIG,
        "sleep": {**CONFIG["sleep"], "wake_source": "owner_only",
                  "owner_id": "master001"},
    }
    manager = make_manager(config=config)
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0), "stranger")
    manager.register_message(datetime(2026, 9, 8, 4, 0, 30), "stranger2")
    wake_stranger, _ = manager.register_message(datetime(2026, 9, 8, 4, 1, 0), "stranger3")
    assert wake_stranger is False
    # 主人连发 3 条（窗内前两条陌生消息不算数，但同在滑动窗里——
    # 主人 3 条达标：04:02/04:03/04:04）
    manager.register_message(datetime(2026, 9, 8, 4, 2, 0), "master001")
    manager.register_message(datetime(2026, 9, 8, 4, 3, 0), "master001")
    wake_owner, _ = manager.register_message(datetime(2026, 9, 8, 4, 4, 0), "master001")
    # 注意：陌生消息也在窗内（counts_toward_wake 只影响触发判定，不删除计数）——
    # 此时窗内总数 >= 3，但陌生人消息不参与触发判定，主人 3 条已达标
    assert wake_owner is True


def test_owner_only_without_owner_id_falls_back_to_all():
    """配了 owner_only 没填 owner_id：退回 all（吵醒不能因配置残缺失灵）。"""
    config = {
        **CONFIG,
        "sleep": {**CONFIG["sleep"], "wake_source": "owner_only", "owner_id": ""},
    }
    manager = make_manager(config=config)
    assert manager.counts_toward_wake("anyone") is True


# ---------------------------------------------------------------------------
# 吵醒结算：起床气 + 睡眠债（B3）
# ---------------------------------------------------------------------------
def test_grouchiness_roll_hit_and_miss():
    """起床气按 grouchiness_percent 概率触发（mock rng）。"""
    mood = FakeMood()
    manager = make_manager(mood=mood, rng=lambda: 0.01)  # 必中（< 0.2）
    result = asyncio.run(manager.apply_woken_in_sleep(IN_WINDOW))
    assert result["grouchy"] is True
    assert mood.grouchy_calls == [True]
    assert mood.valence == pytest.approx(0.2 - 0.15)
    assert mood.energy == pytest.approx(0.8 - 0.1)

    mood2 = FakeMood()
    manager2 = make_manager(mood=mood2, rng=lambda: 0.99)  # 必不中
    result2 = asyncio.run(manager2.apply_woken_in_sleep(IN_WINDOW))
    assert result2["grouchy"] is False
    assert mood2.grouchy_calls == [False]
    assert mood2.valence == pytest.approx(0.2)


def test_sleep_debt_proportional_to_remaining_sleep():
    """睡眠债按剩余睡眠占整个窗口的比例累积（任务书 B3）。"""
    mood = FakeMood()
    manager = make_manager(mood=mood)
    # 04:00 醒，窗口 02:00-06:00：剩余 2h/总 4h = 50 债
    result = asyncio.run(manager.apply_woken_in_sleep(IN_WINDOW))
    assert result["debt_added"] == pytest.approx(50.0)
    assert mood.sleep_debt == pytest.approx(50.0)
    assert result["remaining_minutes"] == pytest.approx(120.0)


def test_debt_zero_when_woken_at_window_end():
    """快到自然醒点才醒：几乎不欠债。"""
    mood = FakeMood()
    manager = make_manager(mood=mood)
    result = asyncio.run(
        manager.apply_woken_in_sleep(datetime(2026, 9, 8, 5, 54, 0))
    )
    assert result["debt_added"] == pytest.approx(2.5)  # 剩 6 分钟 / 总 240 分钟 = 2.5%


def test_debt_zero_outside_window():
    mood = FakeMood()
    manager = make_manager(mood=mood)
    result = asyncio.run(manager.apply_woken_in_sleep(OUT_WINDOW))
    assert result == {"grouchy": False, "debt_added": 0.0, "remaining_minutes": 0.0}


# ---------------------------------------------------------------------------
# 静默拦截判定（B4）
# ---------------------------------------------------------------------------
def test_mute_blocks_in_window_when_enabled():
    manager = make_manager()
    assert manager.should_mute_message(IN_WINDOW, "有人说话") is True


def test_mute_disabled_by_config():
    """sleep_mute_replies=false：完全不拦（红线要求）。"""
    config = {
        **CONFIG,
        "sleep": {**CONFIG["sleep"], "sleep_mute_replies": False},
    }
    manager = make_manager(config=config)
    assert manager.should_mute_message(IN_WINDOW, "有人说话") is False


def test_mute_ignores_own_commands():
    """本插件命令不拦（任务书 B4 例外）。"""
    manager = make_manager()
    assert manager.should_mute_message(IN_WINDOW, "/living_wake") is False


def test_mute_ignores_outside_window():
    manager = make_manager()
    assert manager.should_mute_message(OUT_WINDOW, "有人说话") is False


# ---------------------------------------------------------------------------
# LivingGate 的 force 语义与睡眠窗工具（A2/B3 支撑）
# ---------------------------------------------------------------------------
def test_gate_force_in_sleep_window_returns_woken():
    """force + 休眠窗内 → (True, woken_from_sleep)，交给调用方做吵醒结算。"""
    gate = make_gate()
    allow, reason = asyncio.run(gate.should_wake(IN_WINDOW, force=True))
    assert allow is True
    assert reason == "woken_from_sleep"


def test_gate_force_bypasses_probability():
    """force 豁免概率掷点：rng 恒 0.99（普通判定会 rolled_off）。"""
    gate = LivingGate(
        config_getter=lambda: CONFIG, db_path=":memory:", rng=lambda: 0.99
    )
    allow_normal, reason_normal = asyncio.run(gate.should_wake(OUT_WINDOW))
    allow_force, reason_force = asyncio.run(gate.should_wake(OUT_WINDOW, force=True))
    assert (allow_normal, reason_normal) == (False, "rolled_off")
    assert (allow_force, reason_force) == (True, "ok")


def test_gate_force_still_respects_daily_limit():
    """force 仍受每日上限约束（不是无限豁免）。"""
    gate = make_gate()

    async def fill():
        for _ in range(3):
            await gate.note_activity_started(
                datetime(2026, 9, 8, 1, 0, 0)  # 窗外时间记账，避免混入睡眠逻辑
            )
        return await gate.should_wake(OUT_WINDOW, force=True)

    allow, reason = asyncio.run(fill())
    assert allow is False and reason == "daily_limit"


def test_gate_sleep_window_span_cross_midnight():
    """跨午夜窗口的总时长与剩余时间计算。"""
    config = {
        **CONFIG,
        "sleep": {**CONFIG["sleep"], "sleep_window": "23:00-07:00"},
    }
    gate = LivingGate(config_getter=lambda: config, db_path=":memory:",
                      rng=lambda: 0.5)
    # 凌晨 3 点：窗头在昨天 23:00，窗尾今天 07:00 → 剩 4h，总 8h
    span = gate.sleep_window_span(datetime(2026, 9, 8, 3, 0, 0))
    assert span == pytest.approx((480.0, 240.0))
    assert gate.sleep_window_span(OUT_WINDOW) is None


def test_gate_in_sleep_window_helper():
    gate = make_gate()
    assert gate.in_sleep_window(IN_WINDOW) is True
    assert gate.in_sleep_window(OUT_WINDOW) is False
