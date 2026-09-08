"""M3 补丁 II 测试：清醒待机（滑动窗）、唤醒确认/入睡告别消息、状态切换日志。"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.sleep import SleepManager

# 休眠窗 02:00-06:00；T0=凌晨 3:00（窗内）
T0 = datetime(2026, 9, 9, 3, 0, 0)
DAY = datetime(2026, 9, 9, 12, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 45, "max_run_seconds": 300,
                 "decision_mode": "rules"},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {
        "sleep_window": "02:00-06:00",
        "wake_n_messages": 3,
        "wake_window_minutes": 10,
        "grouchiness_percent": 0,  # 默认不起床气，隔离变量
        "awake_standby_minutes": 30,
        "wake_ack_message": "醒了，怎么了？",
        "sleep_farewell_message": "",
        "sleep_mute_replies": True,
        "wake_source": "all",
        "fatigue_rate_per_hour": 4.0,
    },
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


# ---------------------------------------------------------------------------
# 带待机状态的真实闸门 + 管理器
# ---------------------------------------------------------------------------
def make_gate(config=None):
    return LivingGate(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        db_path=":memory:",
        rng=lambda: 0.0,  # 概率恒命中
    )


def make_manager(gate, config=None, mood=None):
    return SleepManager(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        gate=gate,
        mood=mood,
    )


class FakeSender:
    def __init__(self, ok=True):
        self.sent = []  # (session, text) 按发送顺序
        self.ok = ok

    async def send(self, session, text):
        if not self.ok:
            return False
        self.sent.append((session, text))
        return True


class FakeMemory:
    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)

    async def search(self, query, k=5):
        return self.rows[:k]

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="surf", order=None):
        self.name = name
        self.order = order  # 与 sender 共享的时序列表
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        if self.order is not None:
            self.order.append("activity")
        from core.activities import ActivityOutcome

        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


# ---------------------------------------------------------------------------
# 一：清醒待机（LivingGate）
# ---------------------------------------------------------------------------
def test_standby_skips_sleeping_verdict():
    """待机期内 should_wake 不返回 sleeping（窗内 + awake_until 未过期）。"""
    gate = make_gate()
    asyncio.run(gate.refresh_awake_until(30, now=T0))
    allow, reason = asyncio.run(gate.should_wake(T0))
    assert allow is True
    assert reason != "sleeping"


def test_standby_force_does_not_trigger_wake_settlement():
    """待机期内 force 也不触发 woken_from_sleep（人已经醒了，不存在吵醒）。"""
    gate = make_gate()
    asyncio.run(gate.refresh_awake_until(30, now=T0))
    allow, reason = asyncio.run(gate.should_wake(T0, force=True))
    assert allow is True
    assert reason == "ok"  # 非 woken_from_sleep：不会重复扣睡眠债


def test_standby_expiry_restores_sleeping():
    """待机结束 + 仍在休眠窗内 → sleeping 恢复。"""
    gate = make_gate()
    asyncio.run(gate.refresh_awake_until(30, now=T0))  # 待机至 3:30
    allow, _ = asyncio.run(gate.should_wake(T0 + timedelta(minutes=31)))
    assert allow is False  # 3:31 仍在窗内 → 回去睡


def test_standby_active_mute_exempt():
    """待机期内 should_mute_message 不拦截（gate 同步判定路径）。"""
    gate = make_gate()
    manager = make_manager(gate)
    asyncio.run(gate.refresh_awake_until(30, now=T0))
    assert manager.should_mute_message(T0, "有人说话") is False


def test_awake_until_persists_across_restart(tmp_path):
    """awake_until 持久化：新 gate 实例 load_state 后恢复待机（跨重启）。

    注意用真实文件库：:memory: 每个连接是独立数据库，模拟不了跨实例。
    """
    path = str(tmp_path / "gate.db")
    gate1 = LivingGate(config_getter=lambda: BASE_CONFIG, db_path=path, rng=lambda: 0.5)
    asyncio.run(gate1.refresh_awake_until(30, now=T0))

    gate2 = LivingGate(config_getter=lambda: BASE_CONFIG, db_path=path, rng=lambda: 0.5)
    asyncio.run(gate2.load_state())
    assert gate2.awake_standby_active(T0) is True
    # 过期后不再待机
    assert gate2.awake_standby_active(T0 + timedelta(minutes=31)) is False


def test_consume_standby_expiry_transitions():
    """刚过期 → True 并清除；未设置/未过期 → False。"""
    gate = make_gate()
    assert asyncio.run(gate.consume_standby_expiry(T0)) is False  # 从未设置

    asyncio.run(gate.refresh_awake_until(30, now=T0))
    assert asyncio.run(gate.consume_standby_expiry(T0 + timedelta(minutes=10))) is False
    assert asyncio.run(gate.consume_standby_expiry(T0 + timedelta(minutes=31))) is True
    assert gate.awake_standby_active(T0) is False  # 已清除


def test_standby_does_not_bypass_daily_limit():
    """待机只是豁免 sleeping，每日上限照拦。"""
    gate = make_gate()

    async def flow():
        for _ in range(3):
            await gate.note_activity_started(DAY)
        await gate.refresh_awake_until(30, now=T0)
        return await gate.should_wake(T0)

    allow, reason = asyncio.run(flow())
    assert allow is False and reason == "daily_limit"


# ---------------------------------------------------------------------------
# 一：SleepManager 的滑动窗刷新
# ---------------------------------------------------------------------------
def test_refresh_standby_sliding_window():
    """滑动窗验证：3:00 唤醒/30min → 3:20 发消息 → 3:50 才过期。"""
    gate = make_gate()
    manager = make_manager(gate)
    asyncio.run(manager.begin_standby(T0))  # 3:00 唤醒 → 待机至 3:30
    assert gate.awake_standby_active(datetime(2026, 9, 9, 3, 20)) is True

    refreshed = asyncio.run(
        manager.refresh_standby(datetime(2026, 9, 9, 3, 20))  # 3:20 主人发消息
    )
    assert refreshed is True
    # 3:49 还在待机（3:20+30=3:50 才过期）
    assert gate.awake_standby_active(datetime(2026, 9, 9, 3, 49)) is True
    assert gate.awake_standby_active(datetime(2026, 9, 9, 3, 51)) is False


def test_refresh_standby_false_when_not_in_standby():
    """不在待机期：refresh_standby 返回 False（调用方走吵醒计数）。"""
    manager = make_manager(make_gate())
    assert asyncio.run(manager.refresh_standby(T0)) is False


def test_register_message_records_wake_session():
    """触发吵醒时记录来源会话（唤醒确认消息的发往地）。"""
    gate = make_gate()
    manager = make_manager(gate)
    for i in range(3):
        wake, _ = manager.register_message(
            datetime(2026, 9, 9, 3, 0, i), sender_id="u1",
            session="aiocqhttp:GroupMessage:42",
        )
    assert wake is True
    assert manager.last_wake_session == "aiocqhttp:GroupMessage:42"


def test_refresh_standby_records_active_session():
    manager = make_manager(make_gate())
    asyncio.run(manager.begin_standby(T0))
    asyncio.run(
        manager.refresh_standby(T0 + timedelta(minutes=1),
                                session="aiocqhttp:GroupMessage:42")
    )
    assert manager.last_active_session == "aiocqhttp:GroupMessage:42"


# ---------------------------------------------------------------------------
# 二/三：唤醒确认 + 入睡告别（loop 级）
# ---------------------------------------------------------------------------
class StandbyGate:
    """真实语义的待机闸门替身（固定在休眠窗内）。"""

    def __init__(self):
        self.awake_until: datetime | None = None

    def in_sleep_window(self, now=None):
        return True

    def awake_standby_active(self, now=None):
        now = now or datetime.now()
        return self.awake_until is not None and now < self.awake_until

    async def refresh_awake_until(self, minutes, now=None):
        now = now or datetime.now()
        self.awake_until = now + timedelta(minutes=minutes)

    async def clear_awake_until(self):
        self.awake_until = None

    async def consume_standby_expiry(self, now=None):
        now = now or datetime.now()
        if self.awake_until is not None and now >= self.awake_until:
            self.awake_until = None
            return True
        return False

    async def should_wake(self, now=None, force=False):
        now = now or datetime.now()
        if self.awake_standby_active(now):
            return True, "ok"
        if force:
            return True, "woken_from_sleep"
        return False, "sleeping"

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


def make_loop_with_standby(gate, sender, config=None, order=None):
    memory = FakeMemory()
    activity = ScriptedActivity(order=order)
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG if config is None else config,
        activities=[activity],
        sender=sender,
        sleep_manager=make_manager(gate, config=config),
        rng=None,
    )
    return loop, activity, memory


def test_woken_sleep_sends_ack_before_activity():
    """吵醒 → 确认消息在活动周期之前发出（mock sender 验证顺序）。"""
    gate = StandbyGate()
    order = []
    sender = FakeSender()
    sender.sent = order  # 直接共享时序列表
    loop, activity, _memory = make_loop_with_standby(gate, sender, order=order)
    # 真实链路里 last_wake_session 由消息监听在吵醒触发时写入（有专项测试）
    loop._sleep_manager.last_wake_session = "aiocqhttp:GroupMessage:42"

    asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    assert sender.sent and sender.sent[0] == ("aiocqhttp:GroupMessage:42", "醒了，怎么了？")
    # 时序：ack（元组）在 activity 标记之前 = 确认消息先于活动周期
    assert order[1] == "activity" and len(order) == 2
    # 进入待机
    assert gate.awake_standby_active(T0) is True


def test_ack_empty_config_not_sent():
    config = {
        **BASE_CONFIG,
        "sleep": {**BASE_CONFIG["sleep"], "wake_ack_message": ""},
    }
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender, config=config)

    asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    assert sender.sent == []  # 留空 = 不发送


def test_ack_send_failure_is_silent():
    """确认消息发送失败：记 WARNING，活动周期照常。"""
    gate = StandbyGate()
    sender = FakeSender(ok=False)
    loop, activity, _memory = make_loop_with_standby(gate, sender)

    result = asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    assert result[0] is True  # 唤醒成功
    assert activity.runs == 1  # 活动照常执行


def test_standby_expiry_in_window_sends_farewell_and_restores_sleep():
    """待机结束 + 仍在休眠窗内 → 恢复静默 + 告别消息（配置后）。"""
    config = {
        **BASE_CONFIG,
        "sleep": {**BASE_CONFIG["sleep"],
                  "sleep_farewell_message": "我先去睡了，晚安。"},
    }
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender, config=config)
    loop._sleep_manager.last_active_session = "aiocqhttp:GroupMessage:42"

    asyncio.run(loop.heartbeat_once_detailed(T0, force=True))  # 唤醒+待机
    asyncio.run(loop.heartbeat_once_detailed(
        datetime(2026, 9, 9, 3, 20), force=False))  # 待机期内心跳
    # 3:20 没有新消息 → 3:30 待机过期
    result = asyncio.run(loop.heartbeat_once_detailed(
        datetime(2026, 9, 9, 3, 31), force=False))  # 待机过期 + 仍在窗内
    awake, reason, _act = result
    assert awake is False and reason == "sleeping"  # 恢复睡眠
    assert gate.awake_standby_active(datetime(2026, 9, 9, 3, 32)) is False
    assert any(t == "我先去睡了，晚安。" for _s, t in sender.sent)


def test_farewell_default_empty_not_sent():
    """告别消息默认空 = 安静入睡。"""
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender)

    asyncio.run(loop.heartbeat_once_detailed(T0, force=True))  # 唤醒+待机
    asyncio.run(loop.heartbeat_once_detailed(
        datetime(2026, 9, 9, 3, 31), force=False))  # 待机过期
    assert all(t != "我先去睡了，晚安。" for _s, t in sender.sent)


def test_farewell_send_failure_is_silent():
    config = {
        **BASE_CONFIG,
        "sleep": {**BASE_CONFIG["sleep"],
                  "sleep_farewell_message": "晚安。"},
    }
    gate = StandbyGate()
    sender = FakeSender(ok=False)
    loop, _activity, _memory = make_loop_with_standby(gate, sender, config=config)

    # 手动制造"待机已过期"状态后心跳
    asyncio.run(gate.refresh_awake_until(1, now=T0))
    asyncio.run(loop.heartbeat_once_detailed(T0 + timedelta(minutes=2), force=False))
    assert sender.sent == []  # 发送失败被吞，无异常抛出


def test_standby_expiry_outside_window_no_farewell():
    """待机过期 + 已出休眠窗 → 正常活动，不发告别。"""
    config = {
        **BASE_CONFIG,
        "sleep": {**BASE_CONFIG["sleep"],
                  "sleep_farewell_message": "晚安。"},
    }
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender, config=config)
    # 把闸门切到窗外
    gate.in_sleep_window = lambda now=None: False

    asyncio.run(gate.refresh_awake_until(1, now=T0))
    asyncio.run(loop.heartbeat_once_detailed(T0 + timedelta(minutes=2), force=False))
    assert sender.sent == []


# ---------------------------------------------------------------------------
# 附加：状态切换日志（只在翻转时打）
# ---------------------------------------------------------------------------
def test_sleep_state_transition_logs_once():
    """进窗打一条"进入休眠"、出窗打一条"休眠结束"、中间不重复。"""
    gate = StandbyGate()
    gate.in_sleep_window = lambda now=None: getattr(gate, "window_open", True)
    gate.window_open = False
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender)

    infos = []
    import core.living_loop as llm_mod

    class RecordingLogger:
        def info(self, msg, *a, **kw):
            infos.append(str(msg))

        def warning(self, msg, *a, **kw):
            pass

        def debug(self, msg, *a, **kw):
            pass

        def error(self, msg, *a, **kw):
            pass

        def exception(self, msg, *a, **kw):
            pass

    original_logger = llm_mod.logger
    llm_mod.logger = RecordingLogger()
    try:
        gate.window_open = True
        asyncio.run(loop.heartbeat_once_detailed(T0))  # 进窗 → 进入休眠
        asyncio.run(loop.heartbeat_once_detailed(T0 + timedelta(minutes=1)))
        asyncio.run(loop.heartbeat_once_detailed(T0 + timedelta(minutes=2)))
        gate.window_open = False
        asyncio.run(loop.heartbeat_once_detailed(DAY))  # 出窗 → 休眠结束
    finally:
        llm_mod.logger = original_logger

    entries = [m for m in infos if "进入休眠" in m]
    exits = [m for m in infos if "休眠结束" in m]
    assert len(entries) == 1 and "静默至 06:00" in entries[0]
    assert len(exits) == 1


def test_standby_entry_logs_wake_message():
    """被吵醒进待机 → "被连续消息唤醒，进入清醒待机" INFO 一条。"""
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender)

    infos = []
    import core.living_loop as llm_mod

    class RecordingLogger:
        def info(self, msg, *a, **kw):
            infos.append(str(msg))

        def warning(self, msg, *a, **kw):
            pass

        def debug(self, msg, *a, **kw):
            pass

        def error(self, msg, *a, **kw):
            pass

        def exception(self, msg, *a, **kw):
            pass

    original_logger = llm_mod.logger
    llm_mod.logger = RecordingLogger()
    try:
        asyncio.run(loop.heartbeat_once_detailed(T0, force=True))
    finally:
        llm_mod.logger = original_logger

    assert any("进入清醒待机 30 分钟" in m for m in infos)


def test_standby_period_no_double_sleep_debt():
    """红线：待机期内主人连发消息不重复扣睡眠债/起床气。"""
    mood = types_namespace(valence=0.2, energy=0.8, sleep_debt=0.0,
                           grouchy_calls=[], debt_adds=[])
    gate = StandbyGate()
    sender = FakeSender()
    loop, _activity, _memory = make_loop_with_standby(gate, sender)

    manager = loop._sleep_manager
    manager._mood = mood

    asyncio.run(loop.heartbeat_once_detailed(T0, force=True))  # 吵醒结算一次
    debt_after_wake = mood.sleep_debt
    # 待机期内再"发消息"（refresh_standby 路径）——不触发结算
    for i in range(3):
        asyncio.run(
            manager.refresh_standby(T0 + timedelta(minutes=1 + i),
                                    session="aiocqhttp:GroupMessage:42")
        )
    assert mood.sleep_debt == debt_after_wake
    assert manager.should_mute_message(T0 + timedelta(minutes=2), "聊天") is False


# ---- 小工具 ----
def types_namespace(**kw):
    return type("NS", (), kw)()
