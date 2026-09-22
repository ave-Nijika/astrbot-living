"""M3-B 休眠测试：吵醒滑动窗、起床气、睡眠债、静默拦截判定。"""

import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Any

import pytest

from core.living_state import LivingGate
from core.sleep import SleepManager

# 用一个明确不在默认休眠窗（00:30-08:00）之外的窗口避免混淆：
# 本文件统一用窗口 02:00-06:00，白天时间 12:00 = 窗外，04:00 = 窗内
# M6-补丁1：fixed 机制移除——"在睡"由 enter_autonomous_sleep 触发
# （autonomous 语义），IN_WINDOW/OUT_WINDOW 仅作为虚拟时钟使用
CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 0.8},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {
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


def asleep_gate(enter_at=None, hours=4.0):
    """处于"在睡"状态的 gate（autonomous 语义：enter_autonomous_sleep 触发，
    睡到 enter_at + hours）。吵醒计数/静默拦截的判定来源。"""
    gate = make_gate()
    start = enter_at or IN_WINDOW

    async def _enter():
        await gate.enter_autonomous_sleep(
            start + timedelta(hours=hours), "long", start
        )
    asyncio.run(_enter())
    return gate


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
    """在睡 3 条消息（默认阈值）→ 第 3 条触发吵醒。"""
    gate = asleep_gate(hours=8.0)
    manager = make_manager(gate=gate)
    times = [datetime(2026, 9, 8, 4, 0, i) for i in (0, 1, 2)]
    results = [manager.register_message(t) for t in times]
    asyncio.run(gate.close())  # M8-补丁1：enter 开的连接必须显式关闭
    assert [r[0] for r in results] == [False, False, True]


def test_wake_only_counts_while_asleep():
    """醒着的消息不参与吵醒计数（M6-补丁1：原"窗外"语义 = 醒着）。"""
    manager = make_manager()
    for i in range(5):
        wake, _ = manager.register_message(datetime(2026, 9, 8, 12, 0, i))
        assert wake is False


def test_sliding_window_prunes_old_messages():
    """滑动窗：超过窗口时长的旧消息不再计数。"""
    gate = asleep_gate(hours=8.0)
    manager = make_manager(gate=gate)
    # 第 1、2 条在 04:00/04:01；第 3 条在 04:15（超出 10 分钟窗，前两条已过期）
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 1, 0))
    wake, count = manager.register_message(datetime(2026, 9, 8, 4, 15, 0))
    asyncio.run(gate.close())
    assert wake is False
    assert count == 1  # 只剩自己


def test_wake_triggers_once_per_burst():
    """同一波消息只吵醒一次：触发后冷却一个窗口时长。"""
    gate = asleep_gate(hours=8.0)
    manager = make_manager(gate=gate)
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 0, 30))
    wake1, _ = manager.register_message(datetime(2026, 9, 8, 4, 1, 0))
    # 触发后清窗，同窗内再来 3 条（04:02-04:03，在冷却期内）不应再次触发
    manager.register_message(datetime(2026, 9, 8, 4, 2, 0))
    manager.register_message(datetime(2026, 9, 8, 4, 2, 30))
    wake2, _ = manager.register_message(datetime(2026, 9, 8, 4, 3, 0))
    asyncio.run(gate.close())
    assert wake1 is True
    assert wake2 is False


def test_wake_source_owner_only_filters_strangers():
    """owner_only：陌生人的消息不计入吵醒，主人的算。"""
    config = {
        **CONFIG,
        "sleep": {**CONFIG["sleep"], "wake_source": "owner_only",
                  "owner_id": "master001"},
    }
    gate = asleep_gate(hours=8.0)
    manager = make_manager(config=config, gate=gate)
    manager.register_message(datetime(2026, 9, 8, 4, 0, 0), "stranger")
    manager.register_message(datetime(2026, 9, 8, 4, 0, 30), "stranger2")
    wake_stranger, _ = manager.register_message(datetime(2026, 9, 8, 4, 1, 0), "stranger3")
    assert wake_stranger is False
    # 主人连发 3 条（窗内前两条陌生消息不算数，但同在滑动窗里——
    # 主人 3 条达标：04:02/04:03/04:04）
    manager.register_message(datetime(2026, 9, 8, 4, 2, 0), "master001")
    manager.register_message(datetime(2026, 9, 8, 4, 3, 0), "master001")
    wake_owner, _ = manager.register_message(datetime(2026, 9, 8, 4, 4, 0), "master001")
    asyncio.run(gate.close())
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
    """起床气按 grouchiness_percent 概率触发（mock rng；M6-补丁1 起
    走 autonomous 结算路径，实睡=预计时不欠债）。"""
    mood = FakeMood()
    manager = make_manager(mood=mood, rng=lambda: 0.01)  # 必中（< 0.2）
    result = asyncio.run(
        manager.apply_woken_from_autonomous(mood, 1.0, 8.0, kind="long")
    )
    assert result["grouchy"] is True
    assert mood.grouchy_calls == [True]
    assert mood.valence == pytest.approx(0.2 - 0.15)
    assert mood.energy == pytest.approx(0.8 - 0.1)

    mood2 = FakeMood()
    manager2 = make_manager(mood=mood2, rng=lambda: 0.99)  # 必不中
    result2 = asyncio.run(
        manager2.apply_woken_from_autonomous(mood2, 1.0, 8.0, kind="long")
    )
    assert result2["grouchy"] is False
    assert mood2.grouchy_calls == [False]
    assert mood2.valence == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# 静默拦截判定（B4）
# ---------------------------------------------------------------------------
def test_mute_blocks_when_asleep():
    gate = asleep_gate()
    manager = make_manager(gate=gate)
    result = manager.should_mute_message(IN_WINDOW, "有人说话")
    asyncio.run(gate.close())
    assert result is True


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


def test_mute_ignores_when_awake():
    manager = make_manager()
    assert manager.should_mute_message(OUT_WINDOW, "有人说话") is False


# ---------------------------------------------------------------------------
# LivingGate 的 force 语义与睡眠窗工具（A2/B3 支撑）
# ---------------------------------------------------------------------------
def test_gate_force_while_asleep_returns_woken():
    """force + 在睡 → (True, woken_from_sleep)，交给调用方做吵醒结算。"""
    gate = make_gate()

    async def flow():
        # M8-补丁1：enter 与判定收拢进单次 run（连接不跨 loop）
        start = IN_WINDOW - timedelta(hours=4)
        await gate.enter_autonomous_sleep(IN_WINDOW + timedelta(hours=4), "long", start)
        result = await gate.should_wake(IN_WINDOW, force=True)
        await gate.close()
        return result

    allow, reason = asyncio.run(flow())
    assert allow is True
    assert reason == "woken_from_sleep"


def test_gate_force_bypasses_probability():
    """force 豁免概率掷点：rng 恒 0.99（普通判定会 rolled_off）。"""
    gate = LivingGate(
        config_getter=lambda: CONFIG, db_path=":memory:", rng=lambda: 0.99
    )

    async def flow():
        # M8-补丁1：两次判定收拢进单次 run（连接不跨 loop）
        allow_normal = await gate.should_wake(OUT_WINDOW)
        allow_force = await gate.should_wake(OUT_WINDOW, force=True)
        await gate.close()
        return allow_normal, allow_force

    allow_normal, allow_force = asyncio.run(flow())
    assert (allow_normal[0], allow_normal[1]) == (False, "rolled_off")
    assert (allow_force[0], allow_force[1]) == (True, "ok")


def test_gate_force_still_respects_daily_limit():
    """force 仍受每日上限约束（不是无限豁免）。"""
    gate = make_gate()

    async def fill():
        for _ in range(3):
            await gate.note_activity_started(
                datetime(2026, 9, 8, 1, 0, 0)  # 窗外时间记账，避免混入睡眠逻辑
            )
        result = await gate.should_wake(OUT_WINDOW, force=True)
        await gate.close()
        return result

    allow, reason = asyncio.run(fill())
    assert allow is False and reason == "daily_limit"


def test_gate_is_asleep_now_helper():
    """is_asleep_now（原 in_sleep_window）：在睡 True / 醒着 False。"""
    gate = asleep_gate()
    asleep_result = (gate.is_asleep_now(IN_WINDOW), gate.is_asleep_now(OUT_WINDOW))
    asyncio.run(gate.close())
    awake_gate = make_gate()  # 仅构造，未开连接
    assert asleep_result == (True, False)
    assert awake_gate.is_asleep_now(IN_WINDOW) is False
