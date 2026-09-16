"""M3 补丁 IX 测试：休眠拦截可观测性 + 紧急唤醒 + 静默路径日志分级。"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.living_state import LivingGate
from core.sleep import SleepManager


def types_ns(**kw):
    return type("NS", (), kw)()

# 休眠窗 02:00-06:00；T_IN=凌晨 4 点（窗内）
T_IN = datetime(2026, 9, 17, 4, 0, 0)
T_OUT = datetime(2026, 9, 17, 12, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 0.0},
    "sleep": {
        "sleep_window": "02:00-06:00",
        "wake_n_messages": 3,
        "wake_window_minutes": 10,
        "grouchiness_percent": 0,
        "wake_source": "all",
        "owner_id": "",
        "sleep_mute_replies": True,
        "fatigue_rate_per_hour": 4.0,
        "sleep_debt_decay_per_day": 30.0,
        "dream_probability": 0.0,
        "awake_standby_minutes": 30,
        "wake_ack_message": "",
        "sleep_farewell_message": "",
    },
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": "",
                    "share_rewrite_enabled": False,
                    "share_rewrite_prompt": "", "share_max_length": 120},
    "model": {"provider_id": "", "fallback_chain": []},
    "persona": {"life_extra": ""},
}


def make_gate(config=None):
    return LivingGate(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        db_path=":memory:", rng=lambda: 0.0,
    )


def make_manager(gate=None, config=None):
    return SleepManager(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        gate=gate or make_gate(),
    )


class RecordingLogger:
    """替换 main/loop 模块 logger 的记录器（按级别捕获）。"""

    def __init__(self):
        self.infos = []
        self.warnings = []
        self.debugs = []

    def info(self, msg, *a, **kw):
        self.infos.append(str(msg))

    def warning(self, msg, *a, **kw):
        self.warnings.append(str(msg))

    def debug(self, msg, *a, **kw):
        self.debugs.append(str(msg))

    def error(self, msg, *a, **kw):
        pass

    def exception(self, msg, *a, **kw):
        pass


# ---------------------------------------------------------------------------
# 需求 1：拦截上下文 describe_mute
# ---------------------------------------------------------------------------
def test_describe_mute_contains_guidance(tmp_path):
    gate = make_gate()
    manager = make_manager(gate)
    for i in range(1):
        manager.register_message(datetime(2026, 9, 17, 4, 0, i))
    text = manager.describe_mute(datetime(2026, 9, 17, 4, 0, 1), count=1)
    assert "正在休眠（02:00-06:00）" in text
    assert "窗内第 1 条" in text
    assert "再发 2 条可唤醒" in text
    assert "living_wake_now" in text


# ---------------------------------------------------------------------------
# 需求 2：紧急唤醒（force_awake 语义）
# ---------------------------------------------------------------------------
def test_force_awake_disables_sleeping_and_mute(tmp_path):
    """休眠窗内紧急唤醒 → 窗内不再判 sleeping、不再拦截。"""
    gate = make_gate()
    manager = make_manager(gate)
    # 窗内：先确认拦截与 sleeping 生效
    assert manager.should_mute_message(T_IN, "有人说话") is True
    allow, reason = asyncio.run(gate.should_wake(T_IN))
    assert (allow, reason) == (False, "sleeping")

    until = asyncio.run(gate.force_awake_now(T_IN))
    assert until is not None
    # 强醒期内：不拦、不 sleeping
    assert manager.should_mute_message(T_IN, "又一条消息") is False
    allow2, reason2 = asyncio.run(gate.should_wake(T_IN))
    assert allow2 is True
    assert reason2 != "sleeping"
    # 计数器也被重置
    assert manager.last_window_count == 0


def test_force_awake_expires_at_window_end(tmp_path):
    """强醒期 = 当前窗尾：过期后（仍在同窗概念上的下一次窗）恢复拦截。

    force_awake_until 设为窗尾 06:00；06:01 已出窗（本窗结束）；
    下一次进入 02:00-06:00 窗（次日）时强醒已过期，恢复拦截。
    """
    gate = make_gate()
    manager = make_manager(gate)
    until = asyncio.run(gate.force_awake_now(T_IN))
    assert until == datetime(2026, 9, 17, 6, 0, 0)
    # 窗尾前：强醒生效
    assert gate.force_awake_active(datetime(2026, 9, 17, 5, 59)) is True
    assert manager.should_mute_message(datetime(2026, 9, 17, 5, 59), "x") is False
    # 过期后（同一天窗尾之后 + 次日凌晨再进窗）：恢复
    next_day_pre_dawn = datetime(2026, 9, 18, 3, 0, 0)
    assert gate.force_awake_active(next_day_pre_dawn) is False
    assert manager.should_mute_message(next_day_pre_dawn, "x") is True


def test_force_awake_outside_window_is_noop(tmp_path):
    gate = make_gate()
    manager = make_manager(gate)
    assert asyncio.run(gate.force_awake_now(T_OUT)) is None


def test_wake_now_resets_wake_state():
    manager = make_manager(make_gate())
    manager.last_window_count = 2
    manager.reset_wake_state()
    assert manager.last_window_count == 0


# ---------------------------------------------------------------------------
# 需求 3：静默路径日志分级
# ---------------------------------------------------------------------------
def _recording_logger():
    class RL:
        def __init__(self):
            self.infos, self.warnings, self.debugs = [], [], []

        def info(self, m, *a, **kw):
            self.infos.append(str(m))

        def warning(self, m, *a, **kw):
            self.warnings.append(str(m))

        def debug(self, m, *a, **kw):
            self.debugs.append(str(m))

        def error(self, m, *a, **kw):
            pass

        def exception(self, m, *a, **kw):
            pass

    return RL()


class FakeGate:
    """带可配置休眠窗的假 gate（in_sleep_window 按当前时刻返回 True）。"""

    def __init__(self):
        self.in_window = True

    def in_sleep_window(self, now=None):
        return self.in_window

    def awake_standby_active(self, now=None):
        return False

    async def refresh_awake_until(self, minutes, now=None):
        pass

    async def clear_awake_until(self):
        pass

    async def force_awake_now(self, now=None):
        return None

    def force_awake_active(self, now=None):
        return False

    async def should_wake(self, now=None, force=False):
        return False, "sleeping"

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def note_message_sent(self, now=None):
        pass

    async def close(self):
        pass


class FakeEvent:
    def __init__(self, message_str="x"):
        self.message_str = message_str
        self.stopped = False

    def get_sender_id(self):
        return "u1"

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:FriendMessage:u1"

    def stop_event(self):
        self.stopped = True


class FakeLoop:
    def __init__(self):
        self.forced = 0

    async def heartbeat_once_detailed(self, now=None, force=False):
        self.forced += 1
        return True, "ok", "surf"

    def request_wake(self):
        self.forced += 1


def _build_msg_plugin(tmp_path, monkeypatch, gate, main_module, logger):
    """最小装配：只提供 on_any_message 用到的组件（不走真 sqlite）。"""
    from core.sleep import SleepManager

    manager = SleepManager(
        config_getter=lambda: main_module and dict(BASE_CONFIG),
        gate=gate, mood=None,
    )
    rl = logger
    plugin = types_ns(
        sleep_manager=manager,
        loop=types_ns(request_wake=lambda *a, **kw: None),
        config=BASE_CONFIG,
    )
    # 绑定 main.LivingPlugin.on_any_message 到替身上
    bound = main_module.LivingPlugin.on_any_message.__get__(plugin)
    monkeypatch.setattr("living_plugin_under_test.main.logger", logger)
    return bound, manager


def test_mute_log_is_info_with_context(tmp_path, monkeypatch):
    """休眠窗内消息 → INFO 级"已拦截"日志，含计数/剩余/紧急命令提示。"""
    from test_main_wiring import load_plugin_main

    main_module = load_plugin_main()
    rl = _recording_logger()
    handler, manager = _build_msg_plugin(
        tmp_path, monkeypatch, FakeGate(), main_module, rl
    )
    event = FakeEvent("有人在吗")
    asyncio.run(handler(event))

    mute_logs = [m for m in rl.infos if "睡眠期消息已拦截" in m]
    assert mute_logs, "拦截动作必须 INFO 可见（否则主人会误判插件故障）"
    assert "窗内第" in mute_logs[0]
    assert "再发" in mute_logs[0]
    assert "living_wake_now" in mute_logs[0]
    assert event.stopped is True


def test_wake_count_progress_info_per_message(tmp_path, monkeypatch):
    """窗内逐条消息的计数 INFO 递增（1/3、2/3、3/3）。"""
    from test_main_wiring import load_plugin_main

    main_module = load_plugin_main()
    rl = _recording_logger()
    handler, manager = _build_msg_plugin(
        tmp_path, monkeypatch, FakeGate(), main_module, rl
    )

    for i in range(3):
        asyncio.run(handler(FakeEvent(f"第{i}条")))

    progress = [m for m in rl.infos if "休眠计数" in m]
    assert any("1/3" in m for m in progress)
    assert any("2/3" in m for m in progress)


def test_message_count_error_is_warning(tmp_path, monkeypatch):
    """计数异常 → WARNING 级（异常不该静默）。"""
    from test_main_wiring import load_plugin_main

    main_module = load_plugin_main()
    rl = _recording_logger()
    handler, manager = _build_msg_plugin(
        tmp_path, monkeypatch, FakeGate(), main_module, rl
    )

    def boom(now=None, sender_id=None, session=None):
        raise RuntimeError("boom")

    manager.register_message = boom
    asyncio.run(handler(FakeEvent("x")))
    assert any("消息计数异常" in w for w in rl.warnings)


def test_standby_refresh_error_is_warning(tmp_path, monkeypatch):
    """待机刷新异常 → WARNING 级。"""
    from test_main_wiring import load_plugin_main

    main_module = load_plugin_main()
    rl = _recording_logger()
    handler, manager = _build_msg_plugin(
        tmp_path, monkeypatch, FakeGate(), main_module, rl
    )

    async def boom(now=None, session=None):
        raise RuntimeError("standby boom")

    manager.refresh_standby = boom
    asyncio.run(handler(FakeEvent("x")))
    assert any("待机刷新异常" in w for w in rl.warnings)


def _load_main_module():
    from test_main_wiring import load_plugin_main

    return load_plugin_main()
