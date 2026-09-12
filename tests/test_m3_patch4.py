"""M3 补丁 IV 测试：无聊曲线、并发锁、participant_identities、energy 保底、
兴趣清理、敏感信息脱敏、wait-task 泄漏修复。"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from core.llm_failover import build_provider_chain  # noqa: F401（确保模块可导入）
from core.living_loop import LivingLoop
from core.living_state import LivingGate, boredom_probability
from core.mood import ENERGY_FLOOR, INTEREST_PRUNE_THRESHOLD, MoodState
from core.secrets_redact import redact_secrets

T0 = datetime(2026, 9, 12, 12, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 0.8,
                 "activity_probability_min": 0.1,
                 "activity_probability_ramp_minutes": 60,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


def make_gate(config=None, rng=lambda: 0.0):
    return LivingGate(
        config_getter=lambda: BASE_CONFIG if config is None else config,
        db_path=":memory:",
        rng=rng,
    )


# ---------------------------------------------------------------------------
# 部分 A：无聊曲线
# ---------------------------------------------------------------------------
def test_curve_cold_end_is_min_prob():
    """冷却刚结束（elapsed == cooldown）→ t=0 → 概率 = min_prob。"""
    last = T0 - timedelta(hours=2)  # 冷却 2h 刚好结束
    p = boredom_probability(
        base_prob=0.8, min_prob=0.1, ramp_minutes=60,
        cooldown_hours=2.0, last_activity_at=last, now=T0,
    )
    assert p == pytest.approx(0.1)


def test_curve_mid_ramp_is_linear():
    """爬升中点 → 线性插值：(0.1+0.8)/2 = 0.45。"""
    last = T0 - timedelta(hours=2, minutes=30)  # 冷却后 30 分钟 / 60 分钟爬升
    p = boredom_probability(
        base_prob=0.8, min_prob=0.1, ramp_minutes=60,
        cooldown_hours=2.0, last_activity_at=last, now=T0,
    )
    assert p == pytest.approx(0.45)


def test_curve_past_ramp_caps_at_base():
    """过了爬升时间（含 2 倍）→ 恒为 base_prob（封顶）。"""
    for extra_minutes in (60, 120):
        last = T0 - timedelta(hours=2, minutes=extra_minutes)
        p = boredom_probability(
            base_prob=0.8, min_prob=0.1, ramp_minutes=60,
            cooldown_hours=2.0, last_activity_at=last, now=T0,
        )
        assert p == pytest.approx(0.8)


def test_curve_no_last_activity_is_base():
    """无 last_activity_at（重启后无账本）→ 视为已经很闲 → base_prob。"""
    p = boredom_probability(
        base_prob=0.8, min_prob=0.1, ramp_minutes=60,
        cooldown_hours=2.0, last_activity_at=None, now=T0,
    )
    assert p == pytest.approx(0.8)


def test_curve_zero_ramp_is_base():
    """ramp=0：不爬升，直接 base（任务书：0 = 不用曲线）。"""
    last = T0 - timedelta(hours=2, minutes=1)
    p = boredom_probability(
        base_prob=0.8, min_prob=0.1, ramp_minutes=0,
        cooldown_hours=2.0, last_activity_at=last, now=T0,
    )
    assert p == pytest.approx(0.8)


def test_curve_min_gt_base_clamps():
    """配置错乱（min > base）时收敛到 base，不会出现概率倒挂。"""
    last = T0 - timedelta(hours=2)
    p = boredom_probability(
        base_prob=0.3, min_prob=0.9, ramp_minutes=60,
        cooldown_hours=2.0, last_activity_at=last, now=T0,
    )
    assert p == pytest.approx(0.3)


def test_should_wake_uses_curve(monkeypatch):
    """should_wake 集成：冷却刚过 + rng 落在 (0.1, 0.8) 区间 → rolled_off；
    空闲很久后同一 rng → 通过。"""
    gate = make_gate()
    monkeypatch.setattr(gate, "_rng", lambda: 0.5)

    async def flow(idle_minutes):
        await gate.note_activity_started(T0 - timedelta(hours=2))
        # 修正 note 的时间戳为指定的空闲起点
        await gate._set_raw(
            "last_activity_at",
            (T0 - timedelta(hours=2, minutes=idle_minutes)).isoformat(),
        )
        result = await gate.should_wake(datetime(2026, 9, 12, 12, 0, 0))
        await gate.close()
        return result

    # 冷却刚结束（空闲 121 分钟，冷却 120）：概率 ≈ 0.11，rng 0.5 → 拒
    allow_early, reason_early = asyncio.run(flow(1))
    assert (allow_early, reason_early) == (False, "rolled_off")
    # 空闲远超爬升期（181 分钟）：概率 0.8，rng 0.5 → 通过
    allow_late, reason_late = asyncio.run(flow(61))
    assert (allow_late, reason_late) == (True, "ok")


# ---------------------------------------------------------------------------
# B1：并发锁
# ---------------------------------------------------------------------------
class TracingActivity:
    """记录 enter/exit 顺序的活动（并发时会出现 enter-enter 交错）。"""

    def __init__(self, name="surf", order=None, delay=0.05):
        self.name = name
        self.order = order
        self.delay = delay
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        self.order.append(f"enter-{self.runs}")
        await asyncio.sleep(self.delay)
        self.order.append(f"exit-{self.runs}")
        from core.activities import ActivityOutcome

        return ActivityOutcome(name=self.name, summary="s", memory_content="m")


class OkGate:
    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def daily_limit_info(self, now=None):
        return False, 0, 3

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


class SilentMemory:
    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        return 1

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


def make_loop(activity):
    return LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=SilentMemory()),
        config_getter=lambda: BASE_CONFIG,
        activities=[activity],
    )


def test_concurrent_cycles_serialized_by_lock():
    """B1：并发 run_activity_cycle 被 Lock 排队（enter/exit 不交错）。"""
    order = []
    activity = TracingActivity(order=order, delay=0.05)
    loop = make_loop(activity)

    async def flow():
        tasks = [
            asyncio.create_task(loop.run_activity_cycle(T0)),
            asyncio.create_task(loop.run_activity_cycle(T0)),
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(flow())
    assert all(r["ok"] for r in results)
    # 串行执行：每个周期完整跑完才开始下一个
    assert order == ["enter-1", "exit-1", "enter-2", "exit-2"]


# ---------------------------------------------------------------------------
# B2：participant_identities
# ---------------------------------------------------------------------------
class RecordingMemory:
    def __init__(self):
        self.calls = []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.calls.append({"content": content, "importance": importance,
                           "metadata": metadata})
        return len(self.calls)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


def test_write_memory_carries_participant_identities(tmp_path):
    """B2：metadata 携带 bot 的 participant_identities（图谱桥接原料）。"""
    identity = {
        "identity_key": "aiocqhttp:12345",
        "sender_id": "12345",
        "platform": "aiocqhttp",
        "display_name": "小凛",
        "is_bot": True,
    }
    loop = LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=RecordingMemory()),
        config_getter=lambda: BASE_CONFIG,
        activities=[TracingActivity(order=None, delay=0)],
        bot_identity_getter=lambda: asyncio.sleep(0, result=identity),
    )
    # 劫持记忆以捕获 metadata
    memory = RecordingMemory()
    loop._get_memory = lambda: asyncio.sleep(0, result=memory)

    asyncio.run(loop.run_activity_cycle(T0))
    metadata = memory.calls[0]["metadata"]
    assert metadata["participant_identities"] == [identity]
    assert metadata["participant_identities"][0]["is_bot"] is True


def test_write_memory_without_identity_has_no_participants(tmp_path):
    """未注入身份 getter：metadata 无 participant_identities（记忆照写）。"""
    loop = LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=RecordingMemory()),
        config_getter=lambda: BASE_CONFIG,
        activities=[TracingActivity(order=None, delay=0)],
    )
    memory = RecordingMemory()
    loop._get_memory = lambda: asyncio.sleep(0, result=memory)

    asyncio.run(loop.run_activity_cycle(T0))
    assert "participant_identities" not in memory.calls[0]["metadata"]


# ---------------------------------------------------------------------------
# B3/B4：energy 保底与兴趣清理
# ---------------------------------------------------------------------------
def test_energy_floor_after_repeated_failures():
    """B3：连续失败把 energy 压到保底 0.05，绝不触底为 0。"""
    mood = MoodState(db_path=":memory:")
    asyncio.run(mood.load())
    for _ in range(30):
        asyncio.run(mood.record_activity("game", ok=False))
    assert mood.energy == pytest.approx(ENERGY_FLOOR)
    asyncio.run(mood.close())


def test_grouchiness_respects_energy_floor():
    mood = MoodState(db_path=":memory:")
    asyncio.run(mood.load())
    mood.energy = 0.06
    mood.apply_grouchiness(True)  # -0.1 会击穿 0 → 钳在保底
    assert mood.energy == pytest.approx(ENERGY_FLOOR)
    asyncio.run(mood.close())


def test_decay_prunes_tiny_interests():
    """B4：衰减后低于阈值的兴趣条目被删除，字典不无限膨胀。"""
    mood = MoodState(db_path=":memory:")
    asyncio.run(mood.load())
    mood.interests = {"还热乎的": 0.5, "快忘了的": 0.0105, "几乎没了的": 0.009}
    mood.decay_interests(0.9)
    assert "还热乎的" in mood.interests  # 0.45 保留
    assert "快忘了的" not in mood.interests  # 0.00945 < 0.01 → 删除
    assert "几乎没了的" not in mood.interests
    asyncio.run(mood.close())


# ---------------------------------------------------------------------------
# 独立审计项 4：敏感信息脱敏
# ---------------------------------------------------------------------------
def test_redact_secrets_patterns():
    assert "sk-" not in redact_secrets("连接失败 sk-abcdef1234567890 请检查")
    assert "Bearer" not in redact_secrets(
        "Authorization: Bearer abcdefghijklmnop"
    ).lower() or "[REDACTED]" in redact_secrets(
        "Authorization: Bearer abcdefghijklmnop"
    )
    assert "secret12345678" not in redact_secrets("api_key=secret12345678")
    redacted = redact_secrets("hash " + "a" * 32 + " 结束")
    assert "[REDACTED]" in redacted
    # 正常文本不受影响
    assert redact_secrets("今天看了《三体》") == "今天看了《三体》"


def test_failure_memory_redacts_secrets(tmp_path):
    """失败详情写记忆前脱敏：异常串里的密钥形态不能进记忆库。"""
    from core.living_loop import LivingLoop as _  # noqa（确保同环境）

    class FailingActivity:
        name = "surf"

        async def run(self, ctx):
            raise RuntimeError(
                "request failed with api key sk-abcdef1234567890 at endpoint"
            )

    class CapMemory(SilentMemory):
        def __init__(self):
            self.added = []

        async def add(self, content, importance=0.5, metadata=None, **kwargs):
            self.added.append(content)
            return 1

    memory = CapMemory()
    loop = LivingLoop(
        gate=OkGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        activities=[FailingActivity()],
    )
    result = asyncio.run(loop.run_activity_cycle(T0))
    assert result["ok"] is False
    assert memory.added, "失败记忆照写"
    assert "sk-abcdef1234567890" not in memory.added[0]
    assert "[REDACTED]" in memory.added[0]


# ---------------------------------------------------------------------------
# 独立审计项 3：stop() 时 wait 内层 task 不泄漏
# ---------------------------------------------------------------------------
def test_stop_cancels_pending_wait_tasks():
    """stop() 取消 _run 后，config/wake 两个 wait task 必须被收尾，不能
    以 pending 状态残留到事件循环关闭（补丁 IV 独立审计项 3）。"""
    gate = OkGate()

    async def flow():
        loop = make_loop(gate)
        await loop.start()
        await asyncio.sleep(0.05)  # 让 _run 进入双事件等待
        await loop.stop()
        # 当前 task 之外不应有残留的 pending 任务
        current = asyncio.current_task()
        pending = [
            t for t in asyncio.all_tasks()
            if t is not current and not t.done()
        ]
        return pending

    pending = asyncio.run(flow())
    assert pending == []


def test_config_watcher_task_also_cleaned():
    async def flow():
        loop = make_loop(TracingActivity(order=None, delay=0))
        await loop.start()
        await asyncio.sleep(0.05)
        watcher = loop._watcher_task
        await loop.stop()
        return watcher

    watcher = asyncio.run(flow())
    assert watcher is not None and watcher.done()  # stop 后不残留 pending
