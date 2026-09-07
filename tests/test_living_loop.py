"""LivingLoop 主循环测试（任务书 M1-C/E）。

全部用 fake 注入（gate/记忆/能力/sender/sleep），不依赖真实时钟与网络。
"""

import asyncio
import random
from datetime import datetime
from typing import Any

import pytest

from core.living_loop import LivingLoop


NOW = datetime(2026, 9, 7, 14, 0, 0)

BASE_CONFIG = {
    "decision": {
        "daily_impulse_limit": 3,
        "activity_probability": 0.8,
        "impulse_check_interval_minutes": 45,
        "max_run_seconds": 300,
    },
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": ""},
    "output_gate": {
        "daily_message_limit": 10,
        "message_min_interval_minutes": 30,
        "target_sessions": "",
        "quiet_hours": "",
    },
}


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeGate:
    def __init__(self, allow=True, reason="ok"):
        self.allow = allow
        self.reason = reason
        self.started = 0
        self.finished = 0
        self.message_sends = 0
        self.message_verdicts = []

    async def should_wake(self, now=None):
        return self.allow, self.reason

    async def should_send_message(self, now=None):
        self.message_verdicts.append(self.allow)
        return self.allow, "ok" if self.allow else "blocked"

    async def note_activity_started(self, now=None):
        self.started += 1

    async def note_activity_finished(self, now=None):
        self.finished += 1

    async def note_message_sent(self, now=None):
        self.message_sends += 1

    async def close(self):
        pass


class FakeMemory:
    def __init__(self, error=None):
        self.added = []
        self.error = error

    async def add(self, content, importance=0.5, metadata=None):
        if self.error:
            raise self.error
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class FakeSender:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send(self, session, text):
        if self.ok:
            self.sent.append((session, text))
            return True
        return False


class ScriptedActivity:
    """可编排结果的活动替身。"""

    def __init__(self, name, behavior=None, fail=False, sleep_time=None):
        self.name = name
        self.behavior = behavior  # 返回 (summary, memory)
        self.fail = fail
        self.sleep_time = sleep_time
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        if self.sleep_time:
            await asyncio.sleep(self.sleep_time)
        if self.fail:
            raise RuntimeError("故意失败")
        return _outcome(self.name, self.behavior)


def _outcome(name, behavior):
    from core.activities import ActivityOutcome

    summary, memory = behavior if behavior else (f"{name} 的摘要", f"{name} 的记忆")
    return ActivityOutcome(name=name, summary=summary, memory_content=memory)


class ScriptedRng:
    """脚本化的 choice：按队列吐活动，绕开随机性。"""

    def __init__(self, picks):
        self.picks = list(picks)

    def choice(self, seq):
        target = self.picks.pop(0) if self.picks else seq[0]
        for item in seq:
            if item.name == target:
                return item
        return seq[0]


def make_loop(gate=None, memory=None, activities=None, config=None, sender=None,
              picks=None, abilities=None, rng=None):
    memory = memory if memory is not None else FakeMemory()
    return LivingLoop(
        gate=gate or FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG if config is None else config,
        abilities=abilities or {},
        activities=activities
        if activities is not None
        else [ScriptedActivity("a1"), ScriptedActivity("a2")],
        sender=sender,
        rng=rng if rng is not None else ScriptedRng(picks or []),
    )


# ---------------------------------------------------------------------------
# 生命周期（幂等）
# ---------------------------------------------------------------------------
def test_start_stop_idempotent():
    """重复 start/stop 均安全；stop 后 running=False。"""

    async def flow():
        loop = make_loop()
        blocking = asyncio.Event()
        loop._sleep = blocking.wait  # 挂住心跳，避免真睡 45 分钟
        await loop.start()
        task1 = loop._task
        await loop.start()  # 幂等
        assert loop._task is task1
        assert loop.running
        await loop.stop()
        await loop.stop()  # 幂等
        assert not loop.running

    asyncio.run(flow())


def test_heartbeat_gate_blocks_no_activity():
    """闸门不放行 → 零活动、零记忆、零记账。"""
    gate = FakeGate(allow=False, reason="sleeping")
    memory = FakeMemory()
    acts = [ScriptedActivity("a1")]
    loop = make_loop(gate=gate, memory=memory, activities=acts)

    ran = asyncio.run(loop.heartbeat_once(NOW))
    assert ran is False
    assert acts[0].runs == 0
    assert memory.added == []
    assert gate.started == 0 and gate.finished == 0


def test_heartbeat_gate_passes_runs_cycle():
    """闸门放行 → 活动、记忆、起止记账都发生。"""
    gate = FakeGate(allow=True)
    memory = FakeMemory()
    acts = [ScriptedActivity("a1", ("摘要", "9月7日我干了件事"))]
    loop = make_loop(gate=gate, memory=memory, activities=acts, picks=["a1"])

    ran = asyncio.run(loop.heartbeat_once(NOW))
    assert ran is True
    assert acts[0].runs == 1
    assert gate.started == 1 and gate.finished == 1
    assert memory.added and memory.added[0][0] == "9月7日我干了件事"


def test_activity_failure_still_writes_memory_and_loop_survives():
    """活动抛异常 → 记失败记忆，且下一次心跳照常工作。"""
    gate = FakeGate(allow=True)
    memory = FakeMemory()
    bad = ScriptedActivity("bad", fail=True)
    good = ScriptedActivity("good", ("没问题", "9月7日成功了"))
    loop = make_loop(
        gate=gate, memory=memory, activities=[bad, good], picks=["bad", "good"]
    )

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    assert "故意失败" in result["error"]
    assert len(memory.added) == 1  # 失败记忆也写了
    assert "没成" in memory.added[0][0]

    # 主循环没死：下一次心跳还能正常跑成功活动
    result2 = asyncio.run(loop.run_activity_cycle(NOW))
    assert result2["ok"] is True
    assert good.runs == 1
    assert any("9月7日成功了" == c for c, _ in memory.added)


def test_activity_timeout_killed_and_memory_written():
    """超时强杀：卡死的活动被 wait_for 掐掉，记忆照写。"""
    memory = FakeMemory()
    stuck = ScriptedActivity("stuck", sleep_time=30)
    config = {**BASE_CONFIG, "decision": {**BASE_CONFIG["decision"], "max_run_seconds": 0.1}}
    loop = make_loop(memory=memory, activities=[stuck], config=config, picks=["stuck"])

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    assert "超时" in result["error"]
    assert len(memory.added) == 1


def test_memory_write_failure_only_warns():
    """记忆后端写挂了：活动仍算完成，不向上抛。"""
    memory = FakeMemory(error=RuntimeError("db gone"))
    acts = [ScriptedActivity("a1", ("摘要", "记忆内容"))]
    loop = make_loop(memory=memory, activities=acts, picks=["a1"])
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True  # 活动本身成功
    assert memory.added == []


def test_memory_getter_failure_aborts_cycle_without_quota():
    """记忆后端拿不到：整轮放弃，不消耗活动配额（note_started 不被调用）。"""
    gate = FakeGate(allow=True)

    async def broken_getter():
        raise RuntimeError("no backend")

    loop = LivingLoop(
        gate=gate,
        memory_getter=broken_getter,
        config_getter=lambda: BASE_CONFIG,
        activities=[ScriptedActivity("a1")],
        rng=ScriptedRng([]),
    )
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["error"] == "memory_unavailable"
    assert gate.started == 0


def test_avoid_consecutive_same_activity():
    """避免连续两次同活动。"""
    acts = [ScriptedActivity("a1"), ScriptedActivity("a2"), ScriptedActivity("a3")]
    # 脚本连续想选 a1：第二次实际选池里会剔掉 a1
    rng = ScriptedRng(["a1", "a1"])
    loop = make_loop(activities=acts, rng=rng)
    first = loop._pick_activity()
    second = loop._pick_activity()
    assert first.name == "a1"
    assert second.name != "a1"


# ---------------------------------------------------------------------------
# 分享链路（任务书 E）
# ---------------------------------------------------------------------------
def test_share_without_targets_never_sends():
    """默认未配置 target_sessions：零发送（安静是默认态）。"""
    sender = FakeSender()
    gate = FakeGate(allow=True)
    loop = make_loop(gate=gate, sender=sender)
    asyncio.run(loop._maybe_share("今天看了个有意思的东西", NOW))
    assert sender.sent == []
    assert gate.message_sends == 0


def test_share_with_targets_and_gate_open_sends():
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123\n"
    sender = FakeSender()
    gate = FakeGate(allow=True)
    loop = make_loop(gate=gate, sender=sender, config=config)
    asyncio.run(loop._maybe_share("今天看了个有意思的东西", NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123", "今天看了个有意思的东西")]
    assert gate.message_sends == 1  # 发出去才记账


def test_share_blocked_by_message_gate():
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"
    sender = FakeSender()
    gate = FakeGate(allow=False)
    loop = make_loop(gate=gate, sender=sender, config=config)
    asyncio.run(loop._maybe_share("想说话", NOW))
    assert sender.sent == []
    assert gate.message_sends == 0


def test_share_send_failure_does_not_count():
    """send 返回 False（无匹配平台）：不消耗消息配额。"""
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"
    sender = FakeSender(ok=False)
    gate = FakeGate(allow=True)
    loop = make_loop(gate=gate, sender=sender, config=config)
    asyncio.run(loop._maybe_share("想说话", NOW))
    assert gate.message_sends == 0


def test_cycle_shares_activity_summary():
    """完整活动周期后，活动摘要进入分享链路（闸门放行 → sender 收到）。"""
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"
    sender = FakeSender()
    acts = [ScriptedActivity("a1", ("今天冲浪看到好东西", "记忆"))]
    loop = make_loop(
        gate=FakeGate(allow=True), sender=sender, config=config,
        activities=acts, picks=["a1"],
    )
    asyncio.run(loop.run_activity_cycle(NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123", "今天冲浪看到好东西")]
