"""LivingGate 状态闸门测试（任务书 M1-B）。

所有用例注入确定性的 now 与 rng，不依赖真实时钟与随机数。
"""

import asyncio
from datetime import datetime

import pytest

from core.living_state import LivingGate, in_time_window, parse_time_window


# 默认配置：不在休眠窗（用白天时间即可绕开）、无静默时段
BASE_CONFIG = {
    "decision": {
        "daily_impulse_limit": 3,
        "activity_probability": 0.8,
        "impulse_check_interval_minutes": 45,
        "max_run_seconds": 300,
    },
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "00:30-08:00"},
    "output_gate": {
        "daily_message_limit": 10,
        "message_min_interval_minutes": 30,
        "target_sessions": "",
        "quiet_hours": "",
    },
}


def make_gate(tmp_path, config=None, rng=lambda: 0.5):
    return LivingGate(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        db_path=str(tmp_path / "state.db"),
        rng=rng,
    )


NOON = datetime(2026, 9, 7, 12, 0, 0)


def test_sleep_window_blocks(tmp_path):
    """休眠窗口内 → (False, sleeping)。用假时间注入 03:00。"""
    gate = make_gate(tmp_path)
    allow, reason = asyncio.run(gate.should_wake(datetime(2026, 9, 7, 3, 0, 0)))
    assert allow is False
    assert reason == "sleeping"


def test_daily_limit_blocks(tmp_path):
    """今日活动数达上限 → (False, daily_limit)。"""
    gate = make_gate(tmp_path)

    async def flow():
        # 手动记 3 次活动（默认上限 3）
        for _ in range(3):
            await gate.note_activity_started(NOON)
        return await gate.should_wake(datetime(2026, 9, 7, 18, 0, 0))

    allow, reason = asyncio.run(flow())
    assert allow is False
    assert reason == "daily_limit"


def test_daily_limit_zero_means_unlimited(tmp_path):
    """上限配 0 = 不限制，不应触发 daily_limit。"""
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["decision"]["daily_impulse_limit"] = 0
    gate = make_gate(tmp_path, config)

    async def flow():
        for _ in range(10):
            await gate.note_activity_started(NOON)
        return await gate.should_wake(datetime(2026, 9, 7, 23, 0, 0))

    allow, reason = asyncio.run(flow())
    assert reason != "daily_limit"


def test_cooldown_blocks(tmp_path):
    """距上次活动不足冷却时长 → (False, cooldown)，且优先于概率判定。"""
    gate = make_gate(tmp_path, rng=lambda: 0.0)  # 概率必过，隔离变量

    async def flow():
        await gate.note_activity_finished(datetime(2026, 9, 7, 12, 0, 0))
        # 1.9h 后（冷却 2h 未到）
        return await gate.should_wake(datetime(2026, 9, 7, 13, 54, 0))

    allow, reason = asyncio.run(flow())
    assert allow is False
    assert reason == "cooldown"


def test_cooldown_elapses_allows(tmp_path):
    """冷却已过且概率命中 → (True, ok)。"""
    gate = make_gate(tmp_path, rng=lambda: 0.0)

    async def flow():
        await gate.note_activity_finished(datetime(2026, 9, 7, 12, 0, 0))
        return await gate.should_wake(datetime(2026, 9, 7, 14, 1, 0))

    allow, reason = asyncio.run(flow())
    assert allow is True
    assert reason == "ok"


@pytest.mark.parametrize(
    "rng_value, expect_allow, expect_reason",
    [(0.79, True, "ok"), (0.8, False, "rolled_off"), (0.99, False, "rolled_off")],
)
def test_probability_roll(tmp_path, rng_value, expect_allow, expect_reason):
    """概率边界：rng() < 0.8 通过，否则 rolled_off（mock random）。"""
    gate = make_gate(tmp_path, rng=lambda: rng_value)
    allow, reason = asyncio.run(gate.should_wake(NOON))
    assert allow is expect_allow
    assert reason == expect_reason


def test_cross_day_reset(tmp_path):
    """跨日清零：date 变化后 today_activity_count 归零，daily_limit 不再拦。"""
    gate = make_gate(tmp_path, rng=lambda: 0.0)

    async def flow():
        for _ in range(3):
            await gate.note_activity_started(datetime(2026, 9, 7, 10, 0, 0))
        # 次日（冷却也过了）
        return await gate.should_wake(datetime(2026, 9, 8, 12, 0, 0))

    allow, reason = asyncio.run(flow())
    assert allow is True
    assert reason == "ok"
    state = asyncio.run(gate.get_state(datetime(2026, 9, 8, 12, 0, 1)))
    assert state["activity_count"] == 0


def test_cross_day_keeps_cooldown_timestamp(tmp_path):
    """跨日清计数但保留绝对时间戳：深夜活动后，次日凌晨冷却未过仍要拦。"""
    config = {
        "decision": {"daily_impulse_limit": 1, "activity_probability": 1.0},
        "capabilities": {"cooldown_between_activities_hours": 8.0},
        "sleep": {"sleep_window": ""},
    }
    gate = make_gate(tmp_path, config, rng=lambda: 0.0)

    async def flow():
        await gate.note_activity_started(datetime(2026, 9, 7, 23, 30, 0))
        return await gate.should_wake(datetime(2026, 9, 8, 1, 0, 0))

    allow, reason = asyncio.run(flow())
    assert allow is False
    assert reason == "cooldown"


def test_evaluation_order_sleep_beats_limit(tmp_path):
    """判定链顺序：达上限后进入休眠窗，报 sleeping 而不是 daily_limit。"""
    gate = make_gate(tmp_path)

    async def flow():
        # 00:00 记满 5 次活动（上限 3），05:00 已在休眠窗内（00:30-08:00）
        for _ in range(5):
            await gate.note_activity_started(datetime(2026, 9, 7, 0, 0, 0))
        return await gate.should_wake(datetime(2026, 9, 7, 5, 0, 0))

    allow, reason = asyncio.run(flow())
    assert reason == "sleeping"


# ---------------------------------------------------------------------------
# 消息闸门（任务书 M1-E 的判定部分）
# ---------------------------------------------------------------------------
def test_message_gate_allows_by_default(tmp_path):
    gate = make_gate(tmp_path)
    allow, reason = asyncio.run(gate.should_send_message(NOON))
    assert allow is True and reason == "ok"


def test_message_gate_daily_limit(tmp_path):
    gate = make_gate(tmp_path)

    async def flow():
        for _ in range(10):
            await gate.note_message_sent(NOON)
        return await gate.should_send_message(datetime(2026, 9, 7, 18, 0))

    allow, reason = asyncio.run(flow())
    assert allow is False and reason == "msg_daily_limit"


def test_message_gate_interval(tmp_path):
    gate = make_gate(tmp_path)

    async def flow():
        await gate.note_message_sent(datetime(2026, 9, 7, 12, 0))
        return await gate.should_send_message(datetime(2026, 9, 7, 12, 29))

    allow, reason = asyncio.run(flow())
    assert allow is False and reason == "msg_interval"


def test_message_gate_quiet_hours(tmp_path):
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["quiet_hours"] = "12:00-14:00"
    gate = make_gate(tmp_path, config)
    allow, reason = asyncio.run(gate.should_send_message(datetime(2026, 9, 7, 13, 0)))
    assert allow is False and reason == "quiet_hours"


# ---------------------------------------------------------------------------
# 时间窗口工具
# ---------------------------------------------------------------------------
def test_parse_and_in_window_cross_midnight():
    window = parse_time_window("23:00-07:00")
    assert window is not None
    assert in_time_window(datetime(2026, 9, 7, 3, 0), window)
    assert in_time_window(datetime(2026, 9, 7, 23, 30), window)
    assert not in_time_window(datetime(2026, 9, 7, 12, 0), window)


def test_parse_window_invalid_returns_none():
    assert parse_time_window("") is None
    assert parse_time_window(None) is None
    assert parse_time_window("abc-def") is None
    assert parse_time_window("25:00-08:00") is None


def test_bad_config_values_fall_back_to_defaults(tmp_path):
    """配置值脏（类型错/空）时用默认值兜底，不抛异常。"""
    gate = make_gate(
        tmp_path,
        config={
            "decision": {
                "daily_impulse_limit": "abc",
                "activity_probability": None,
            },
            "capabilities": {"cooldown_between_activities_hours": ""},
            "sleep": {"sleep_window": "不是时间"},
        },
        rng=lambda: 0.0,
    )
    allow, reason = asyncio.run(gate.should_wake(NOON))
    assert allow is True and reason == "ok"
