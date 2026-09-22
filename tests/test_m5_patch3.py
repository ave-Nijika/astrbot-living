"""M5-补丁3 测试：睡眠模式互斥——fixed/autonomous 双轨冲突修复。

验收 1-5、7 全部走真实 LivingGate 状态（enter_autonomous_sleep 构造
_sleep_until），不测纯函数层。LivingGate/MoodState 持有 aiosqlite 连接，
跨 asyncio.run 复用会死锁——所有多步流程包进单个 asyncio.run。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.mood import MoodState
from core.sleep import SleepManager

NOW = datetime(2026, 9, 22, 5, 50, 0)  # VM 现场时刻：长睡入睡点
WORKDIR = Path(__file__).resolve().parents[1]


def _config(mode, **overrides):
    cfg = {
        "decision": {
            "decision_mode": "rules",
            "daily_impulse_limit": 3,
            "activity_probability": 1.0,
            "activity_probability_min": 1.0,
            "activity_probability_ramp_minutes": 0,
        },
        "capabilities": {"cooldown_between_activities_hours": 0.0},
        "sleep": {
            "sleep_mode": mode,
            "sleep_window": "00:30-08:00",  # VM 现场窗口
            "min_awake_minutes": 0,
            "sleepiness_threshold": 0.6,
            "sleepiness_jitter": 0.0,
            "nap_enabled": True,
            "circadian_hint": "23:00-07:00",
            "fatigue_rate_per_hour": 4.0,
        },
        "output_gate": {"daily_message_limit": 10},
    }
    cfg["sleep"].update(overrides)
    return cfg


class _Mood:
    def __init__(self, energy=0.05, sleep_debt=0.0):
        self.energy = energy
        self.sleep_debt = sleep_debt

    def apply_grouchiness(self, enabled):
        return False


def _gate(config, tmp_path):
    return LivingGate(
        config_getter=lambda: config,
        db_path=str(tmp_path / "gate.db"),
        rng=lambda: 0.5,
    )


def _manager(config, gate):
    return SleepManager(config_getter=lambda: config, gate=gate, rng=lambda: 0.5)


# ---------------------------------------------------------------------------
# 验收 1-3：autonomous 在睡（真实 _sleep_until 状态）
# ---------------------------------------------------------------------------
def test_autonomous_asleep_inside_window(tmp_path):
    async def flow():
        config = _config("autonomous")
        gate = _gate(config, tmp_path)
        # 真实长睡：05:50 入睡，预计 10:50 自然醒
        await gate.enter_autonomous_sleep(
            NOW + timedelta(hours=5), "long", NOW
        )
        inside = await gate.should_wake(NOW + timedelta(minutes=20))  # 06:10 窗口内
        await gate.close()
        return inside

    allow, reason = asyncio.run(flow())
    assert (allow, reason) == (False, "sleeping")  # 验收 1


def test_autonomous_asleep_force_wakes(tmp_path):
    async def flow():
        config = _config("autonomous")
        gate = _gate(config, tmp_path)
        await gate.enter_autonomous_sleep(NOW + timedelta(hours=5), "long", NOW)
        result = await gate.should_wake(
            NOW + timedelta(minutes=20), force=True
        )
        await gate.close()
        return result

    allow, reason = asyncio.run(flow())
    assert (allow, reason) == (True, "woken_from_sleep")  # 验收 2


def test_autonomous_asleep_outside_window_still_sleeping(tmp_path):
    """验收 3（本次核心）：窗口外（08:00-10:50 场景）在睡仍拦截——
    修复前第 1 关放行，活动链会在自主睡眠中执行。"""

    async def flow():
        config = _config("autonomous")
        gate = _gate(config, tmp_path)
        await gate.enter_autonomous_sleep(NOW + timedelta(hours=5), "long", NOW)
        # 09:00：已跨过窗口结束（08:00），但 until（10:50）未到
        result = await gate.should_wake(NOW + timedelta(hours=3, minutes=10))
        await gate.close()
        return result

    allow, reason = asyncio.run(flow())
    assert (allow, reason) == (False, "sleeping")


# ---------------------------------------------------------------------------
# 验收 4：autonomous 醒着 + 窗口内 → 跳过窗口判定，按 2-4 关放行
# ---------------------------------------------------------------------------
def test_autonomous_awake_inside_window_not_blocked(tmp_path):
    async def flow():
        config = _config("autonomous")
        gate = _gate(config, tmp_path)  # 不 enter：醒着
        result = await gate.should_wake(NOW + timedelta(minutes=20))
        await gate.close()
        return result

    allow, reason = asyncio.run(flow())
    # 概率 1.0 → ok；不低于 rolled_off 语义（关键是不被 sleeping 拦）
    assert allow is True
    assert reason in ("ok", "rolled_off")


# ---------------------------------------------------------------------------
# 验收 5：fixed 行为零变化（窗口内拦 / 窗口外放行）
# ---------------------------------------------------------------------------


def _loop_with_mocks(config, gate, manager, mood, memory):
    loop = LivingLoop(
        gate=gate, memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: config, sleep_manager=manager, mood=mood,
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop




class FakeMemory:
    """可记录写入的最小记忆替身（多测试共用）。"""

    def __init__(self):
        self.added = []

    async def search(self, query, k=5, **kwargs):
        return []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append((content, metadata))
        return len(self.added)


def test_review_triggered_by_long_sleep_enter_only(tmp_path):
    async def flow():
        config = _config("autonomous")
        gate = RecordingGate3(config_getter=lambda: config,
                              db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)

        async def make_mood(energy, debt):
            m = MoodState(db_path=str(tmp_path / "mood.db"))
            await m.load()
            m.energy = energy
            m.sleep_debt = debt
            return m

        mood = await make_mood(0.05, 80.0)  # 睡意达标 → 长睡
        memory = FakeMemory()
        loop = _loop_with_mocks(config, gate, manager, mood, memory)
        await loop._autonomous_sleep_tick(NOW.replace(hour=15))
        long_reviews = len(memory.added)
        long_entered = [k for k in gate.entered_kinds if k == "long"]

        # 小睡路径：债清零精力极低 → 睡意不达标 → 小睡 enter，不写回顾
        await gate.exit_autonomous_sleep(NOW.replace(hour=16))
        mood.sleep_debt = 0.0
        mood.energy = 0.05
        await loop._autonomous_sleep_tick(NOW.replace(hour=16, minute=10))
        nap_reviews = len(memory.added) - long_reviews
        nap_entered = [k for k in gate.entered_kinds if k == "nap"]

        await mood.close()
        await gate.close()
        return long_entered, long_reviews, nap_entered, nap_reviews

    long_entered, long_reviews, nap_entered, nap_reviews = asyncio.run(flow())
    assert long_entered and long_reviews == 1  # 长睡 enter 触发一次回顾
    assert nap_entered and nap_reviews == 0  # 小睡不触发


class RecordingGate3(LivingGate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_kinds = []

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self.entered_kinds.append(kind)
        await super().enter_autonomous_sleep(until, kind, now)






class _InWindowRealGate(LivingGate):
    """fixed 翻转测试替身：恒在窗口内且不在待机（其余走真实 gate）。"""

    def in_sleep_window(self, now=None):
        return True

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_wake(self, now=None, force=False):
        return False, "sleeping"

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass


# ---------------------------------------------------------------------------
# 验收 7（重点）：时序回归——窗口内长睡跨过窗口结束时刻
# ---------------------------------------------------------------------------
def test_sequential_no_silent_to_open_flip_across_window_end(tmp_path):
    """VM 现场复刻：05:50 窗口内长睡（until 10:50）→
    - 06:10（窗内）应 sleeping；
    - 09:00（跨过窗口结束 08:00，仍未自然醒）仍 sleeping——不允许跨窗
      瞬间由静默变放行（修复前此处放行）；
    - 10:50 到点自然醒结算后 → 活动链恢复（放行）。"""

    async def flow():
        config = _config("autonomous")
        gate = _gate(config, tmp_path)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.05
        mood.sleep_debt = 0.0  # VM 现场：债 0，仅靠夜昼项达标
        loop = _loop_with_mocks(config, gate, manager, mood, FakeMemory())

        # 05:50（窗内）长睡入睡——真实 tick 路径（circadian 1.0 → 睡意 0.633）
        await loop._autonomous_sleep_tick(NOW)
        entered = gate.sleep_state()

        r_0610 = await gate.should_wake(NOW + timedelta(minutes=20))
        r_0900 = await gate.should_wake(NOW + timedelta(hours=3, minutes=10))

        # 10:50 到点自然醒（真实结算路径）→ 之后活动链恢复
        wake_time = NOW + timedelta(hours=5)
        await loop._autonomous_sleep_tick(wake_time)
        r_1055 = await gate.should_wake(wake_time + timedelta(minutes=5))

        final = gate.sleep_state()
        await mood.close()
        await gate.close()
        return entered, r_0610, r_0900, r_1055, final

    entered, r_0610, r_0900, r_1055, final = asyncio.run(flow())
    assert entered["asleep"] is True and entered["kind"] == "long"
    assert (r_0610[0], r_0610[1]) == (False, "sleeping")  # 窗内在睡
    assert (r_0900[0], r_0900[1]) == (False, "sleeping")  # 跨窗仍在睡（核心）
    assert r_1055[0] is True  # 自然醒后活动链恢复
    assert final["asleep"] is False


# ---------------------------------------------------------------------------
# 红线：fixed 模式 tick 全链路零变化（autonomous 分支不参与）
# ---------------------------------------------------------------------------

