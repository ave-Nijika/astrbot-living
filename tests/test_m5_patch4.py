"""M5-补丁4 测试：日程感知作息——约定提取、压力竞争、锚定、睡过头认知。

验收 3-7 走真实 SleepManager/MoodState/LivingGate 状态机（rng 注入固定值）；
LivingGate/MoodState 持有 aiosqlite 连接——多步流程包进单个 asyncio.run。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.mood import MoodState
from core.schedule import (
    ScheduleManager,
    build_extract_prompt,
    hit_trigger_words,
)
from core.sleep import SleepManager

NOW = datetime(2026, 9, 22, 22, 0, 0)
WORKDIR = Path(__file__).resolve().parents[1]


def _config(**overrides):
    cfg = {
        "decision": {"decision_mode": "rules"},
        "sleep": {
            "sleep_mode": "autonomous",
            "min_awake_minutes": 0,
            "sleepiness_threshold": 0.6,
            "sleepiness_jitter": 0.0,  # 固定 rng 时抖动为 0
            "circadian_hint": "23:00-07:00",
            "fatigue_rate_per_hour": 4.0,
        },
    }
    cfg["sleep"].update(overrides)
    return cfg


def _gate(config, tmp_path):
    return LivingGate(
        config_getter=lambda: config,
        db_path=str(tmp_path / "gate.db"),
        rng=lambda: 0.5,
    )


def _schedule(config, gate, llm=None):
    return ScheduleManager(
        config_getter=lambda: config, gate=gate, llm_call=llm,
        now_provider=lambda: NOW,
    )


async def _add_wake(gate, target: datetime, now=None):
    """注入约定：expires 取约定当日 23:59（与生产 _expires_at 语义一致，
    保证晚睡场景下自然醒时刻不会先于约定过期）。"""
    await gate.add_commitment({
        "type": "wake",
        "target_time": target.isoformat(timespec="minutes"),
        "content": "测试约定",
        "created_at": (now or NOW).isoformat(timespec="minutes"),
        "expires_at": datetime.combine(
            target.date(), datetime.min.time()
        ).replace(hour=23, minute=59).isoformat(timespec="minutes"),
    })


class _Mood:
    """睡意公式测试的最小心境（arousal 可控）。"""

    def __init__(self, energy=0.5, sleep_debt=0.0, arousal=0.5):
        self.energy = energy
        self.sleep_debt = sleep_debt
        self.arousal = arousal
        self.interests = {}

    def interest_weight(self, topic, repeat_count=0):
        return 0.3

    def apply_grouchiness(self, enabled):
        return bool(enabled)

    def add_sleep_debt(self, amount):
        self.sleep_debt = max(0.0, min(100.0, self.sleep_debt + max(amount, 0.0)))

    def recent_topics_list(self):
        return []


# ---------------------------------------------------------------------------
# 验收 1：预筛——未命中词表 → LLM 调用次数 0
# ---------------------------------------------------------------------------
def test_prefilter_zero_llm_cost(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        calls = {"n": 0}

        async def llm(prompt, system=None):
            calls["n"] += 1
            return '{"is_wake_commitment": true, "target_time": "2026-09-23T08:00", "confidence": 0.9}'

        sm = _schedule(config, gate, llm)
        await sm.maybe_extract("今天天气真不错，适合散步。", NOW)
        await sm.maybe_extract("", NOW)
        await gate.close()
        return calls["n"]

    assert asyncio.run(flow()) == 0


# ---------------------------------------------------------------------------
# 验收 2：严格度门槛（confidence >= strictness 才记录）
# ---------------------------------------------------------------------------
def test_strictness_gate(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        payload = '{"is_wake_commitment": true, "target_time": "%s", "confidence": %s}'

        async def llm_low(prompt, system=None):
            return payload % ("2026-09-23T08:00", "0.6")

        async def llm_high(prompt, system=None):
            return payload % ("2026-09-23T08:00", "0.8")

        sm = _schedule(config, gate, llm_low)
        result = await sm.maybe_extract("明早 8 点起床跑步", NOW)
        assert result is None  # 0.6 < 0.7 → 不记录

        sm2 = _schedule(config, gate, llm_high)
        result = await sm2.maybe_extract("明早 8 点起床跑步", NOW)
        assert result is not None  # 0.8 ≥ 0.7 → 记录
        stored = await gate.get_commitments(NOW)
        await gate.close()
        return result, stored

    result, stored = asyncio.run(flow())
    assert result["target_time"] == "2026-09-23T08:00"
    assert len(stored) == 1 and stored[0]["type"] == "wake"


def test_extract_prompt_contains_message_and_json_spec():
    prompt = build_extract_prompt("明早 8 点起床", NOW)
    assert "明早 8 点起床" in prompt
    assert "is_wake_commitment" in prompt and "target_time" in prompt
    assert NOW.isoformat(timespec="minutes") in prompt


def test_hit_trigger_words():
    assert hit_trigger_words("明早 8 点起床跑步") is True
    assert hit_trigger_words("Wake up at 8") is True
    assert hit_trigger_words("今天天气不错") is False


# ---------------------------------------------------------------------------
# 验收 3/4/5：睡意公式——压力爬升 / discipline / 沉浸竞争（真实 SleepManager）
# ---------------------------------------------------------------------------
def test_pressure_rises_with_upcoming_commitment(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5, schedule=sm)
        mood = _Mood(energy=0.5, sleep_debt=0.0, arousal=0.0)  # arousal=0 消除竞争项

        no_commitment, _ = await manager.sleepiness(mood, NOW)

        # 约定在 1 小时后 → pressure = 1 - 1/3 ≈ 0.667
        await _add_wake(gate, NOW + timedelta(hours=1))
        with_commitment, detail = await manager.sleepiness(mood, NOW)
        await gate.close()
        return no_commitment, with_commitment, detail

    base, risen, detail = asyncio.run(flow())
    assert risen > base  # 验收 3：有约定且临近 → 睡意单调上升
    # 精确值：+w_schedule(0.25) × pressure(2/3) × discipline(0.6) = +0.1
    assert risen - base == pytest.approx(0.25 * (2 / 3) * 0.6, abs=1e-6)
    assert detail["schedule"] == pytest.approx(0.1, abs=1e-3)


def test_discipline_knob_scales_commitment_pressure(tmp_path):
    async def flow():
        results = {}
        for discipline in (0.0, 1.0):
            config = _config(schedule_discipline=discipline)
            gate = _gate(config, tmp_path)
            sm = _schedule(config, gate)
            manager = SleepManager(config_getter=lambda: config, gate=gate,
                                   rng=lambda: 0.5, schedule=sm)
            mood = _Mood(energy=0.5, sleep_debt=0.0, arousal=0.0)
            await _add_wake(gate, NOW + timedelta(hours=1))
            value, _ = await manager.sleepiness(mood, NOW)
            results[discipline] = value
            await gate.close()
        return results

    results = asyncio.run(flow())
    base = 0.35 * 0.5 + 0.3 * 0.1  # e+d+c（22:00 昼夜基底 0.1），arousal=0
    assert results[0.0] == pytest.approx(base, abs=1e-6)  # discipline=0 → 压力项 0
    assert results[1.0] == pytest.approx(
        base + 0.25 * (2 / 3) * 1.0, abs=1e-6)  # =1 全额


def test_arousal_competes_against_commitment_pressure(tmp_path):
    """验收 5：有约定 + arousal 高 → 睡意低于同条件低 arousal（沉浸竞争）。"""

    async def flow():
        highs, lows = {}, {}
        for arousal in (1.0, 0.0):
            config = _config()
            gate = _gate(config, tmp_path)
            sm = _schedule(config, gate)
            manager = SleepManager(config_getter=lambda: config, gate=gate,
                                   rng=lambda: 0.5, schedule=sm)
            mood = _Mood(energy=0.5, sleep_debt=0.0, arousal=arousal)
            await _add_wake(gate, NOW + timedelta(hours=1))
            value, _ = await manager.sleepiness(mood, NOW)
            (highs if arousal == 1.0 else lows)[arousal] = value
            await gate.close()
        return highs[1.0], lows[0.0]

    high, low = asyncio.run(flow())
    assert high < low  # 验收 5：兴头上压得住约定压力
    assert low - high == pytest.approx(0.2 * 1.0 * 0.5, abs=1e-6)  # w_arousal×arousal×0.5


# ---------------------------------------------------------------------------
# 验收 6（核心）：早睡链——锚定至约定前 15 分钟
# ---------------------------------------------------------------------------
def test_early_sleep_anchored_before_commitment(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        target = datetime(2026, 9, 23, 8, 0)  # 约定明早 8:00
        await _add_wake(gate, target)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.75, schedule=sm)  # 自然时长 9.5h
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.2
        mood.sleep_debt = 80.0  # 债高 → 自然时长 10.05h（晚于锚点 07:45）
        mood.arousal = 0.3

        result = await manager.begin_autonomous_sleep(mood, NOW.replace(hour=23))
        state = gate.sleep_state(NOW.replace(hour=23))
        await mood.close()
        await gate.close()
        return result, state

    result, state = asyncio.run(flow())
    # 自然时长 10.05h → 自然 until=09:03；锚定 min(09:03, 07:45) = 07:45
    assert result["until"] == datetime(2026, 9, 23, 7, 45)
    assert result["duration_h"] == pytest.approx(8.75)  # 23:00 → 07:45
    assert state["until"] == datetime(2026, 9, 23, 7, 45)
    assert state["kind"] == "long"


# ---------------------------------------------------------------------------
# 验收 7/8（核心）：熬夜链——不锚定、睡满、睡过头认知（含身份注入与幂等）
# ---------------------------------------------------------------------------
def test_oversleep_chain_produces_self_awareness_note(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        target = datetime(2026, 9, 23, 8, 0)
        await _add_wake(gate, target)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.75, schedule=sm)  # 自然时长 9.5h

        class FakeMemory:
            def __init__(self):
                self.added = []

            async def search(self, query, k=5, **kwargs):
                return []

            async def add(self, content, importance=0.5, metadata=None, **kwargs):
                self.added.append((content, metadata))
                return len(self.added)

        memory = FakeMemory()
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.2
        mood.sleep_debt = 80.0
        mood.arousal = 0.3
        loop = LivingLoop(
            gate=gate, memory_getter=lambda: asyncio.sleep(0, result=memory),
            config_getter=lambda: config, sleep_manager=manager, mood=mood,
            rng=lambda: 0.5,  # 分享掷点 0.5 → 不分享（认知写入与分享解耦）
            schedule=sm,
        )

        async def _identity():
            return {"identity_key": "aiocqhttp:10001", "is_bot": True}

        async def _none():
            return None
        loop._bot_identity = _identity
        loop._persona_id = _none
        loop._session_id = lambda event: "living_test"

        # 02:00 才入睡（晚于熬夜线 01:00）→ 不锚定
        result = await manager.begin_autonomous_sleep(mood, datetime(2026, 9, 23, 2, 0))
        unanchored_until = result["until"]
        # 睡满自然时长后到点自然醒 → 结算 + 睡过头认知
        await loop._autonomous_sleep_tick(unanchored_until)
        stored_after = await gate.get_commitments(unanchored_until)
        note_count = len(memory.added)
        note_content, note_metadata = memory.added[0] if memory.added else ("", {})

        # 幂等：再次结算（约定已消费）→ 不重复写
        await loop._autonomous_sleep_tick(unanchored_until + timedelta(minutes=5))
        note_count_after = len(memory.added)

        await mood.close()
        await gate.close()
        return unanchored_until, stored_after, note_count, note_content, note_metadata, note_count_after

    (unanchored_until, stored_after, note_count,
     note_content, note_metadata, note_count_after) = asyncio.run(flow())
    assert unanchored_until == datetime(2026, 9, 23, 12, 3)  # 不锚定，睡满 10.05h
    assert unanchored_until > datetime(2026, 9, 23, 8, 0)  # 验收 7：睡过头
    assert note_count == 1  # 验收 8：一条认知
    assert "睡过头" in note_content and "08:00" in note_content
    assert note_metadata["topics"] == ["睡过头"]
    assert note_metadata["participant_identities"][0]["identity_key"] == "aiocqhttp:10001"
    assert stored_after == []  # C3：结算即从存储清除
    assert note_count_after == 1  # 幂等：再结算不重复写


# ---------------------------------------------------------------------------
# 验收 9：催醒起床气——过期未兑现约定 + force → 概率 ×2
# ---------------------------------------------------------------------------
def test_forced_wake_grouchiness_doubled_with_overdue(tmp_path):
    async def flow():
        config = _config(grouchiness_percent=20)
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.3, schedule=sm)  # rng 0.3：0.2≤0.3<0.4
        mood = _Mood()
        # 无过期约定：rng 0.3 ≥ 0.2 → 不气
        normal = await manager.apply_woken_from_autonomous(mood, 1.0, 8.0, kind="long")
        # 注入已过期未兑现约定：概率 ×2（40%）→ rng 0.3 < 0.4 → 气
        await _add_wake(gate, NOW - timedelta(hours=1))
        boosted = await manager.apply_woken_from_autonomous(
            mood, 1.0, 8.0, kind="long", grouchy_boost=True
        )
        await gate.close()
        return normal["grouchy"], boosted["grouchy"]

    normal_grouchy, boosted_grouchy = asyncio.run(flow())
    assert normal_grouchy is False
    assert boosted_grouchy is True


# ---------------------------------------------------------------------------
# 验收 10：总开关关闭 → 预筛/LLM/压力/锚定全链路零生效
# ---------------------------------------------------------------------------
def test_master_switch_off_disables_everything(tmp_path):
    async def flow():
        config = _config(schedule_reminder_enabled=False)
        gate = _gate(config, tmp_path)
        calls = {"n": 0}

        async def llm(prompt, system=None):
            calls["n"] += 1
            return '{"is_wake_commitment": true, "target_time": "2026-09-23T08:00", "confidence": 0.9}'

        sm = _schedule(config, gate, llm)
        # 命中词表也不产生 LLM 调用
        assert await sm.maybe_extract("明早 8 点起床", NOW) is None
        assert calls["n"] == 0
        # 手工塞一条约定 → 压力仍为 0（查询端同样被开关拦住）
        await _add_wake(gate, NOW + timedelta(hours=1))
        assert await sm.schedule_pressure(NOW) == 0.0
        assert await sm.earliest_future_wake(NOW) is None

        # 锚定不生效：begin_autonomous_sleep 按纯自然时长
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.75, schedule=sm)
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.2
        mood.sleep_debt = 50.0
        mood.arousal = 0.3
        result = await manager.begin_autonomous_sleep(mood, NOW.replace(hour=23))
        await mood.close()
        await gate.close()
        return result["until"]

        natural_until = asyncio.run(flow())
        assert natural_until == datetime(2026, 9, 23, 7, 15)  # 23:00 + 8.25h（debt 0.5 折算 3h + 抖动 0.25h），未锚定


# ---------------------------------------------------------------------------
# 验收 11：过期清除（惰性）→ 压力归零
# ---------------------------------------------------------------------------
def test_expired_commitment_lazily_cleared(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        # 已过期约定（expires_at 已过）
        await gate.add_commitment({
            "type": "wake",
            "target_time": (NOW - timedelta(hours=1)).isoformat(timespec="minutes"),
            "content": "过期约定",
            "created_at": (NOW - timedelta(hours=25)).isoformat(timespec="minutes"),
            "expires_at": (NOW - timedelta(minutes=30)).isoformat(timespec="minutes"),
        })
        stored = await gate.get_commitments(NOW)  # 读取时惰性清除
        pressure = await sm.schedule_pressure(NOW)
        remaining = await gate.get_commitments(NOW)
        await gate.close()
        return stored, pressure, remaining

    stored, pressure, remaining = asyncio.run(flow())
    assert stored == []  # 惰性清除
    assert pressure == 0.0  # 压力归零
    assert remaining == []  # 清除已落库


# ---------------------------------------------------------------------------
# A4：同 target_time 幂等（更新而非新增）
# ---------------------------------------------------------------------------
def test_same_target_time_updates_not_appends(tmp_path):
    async def flow():
        config = _config()
        gate = _gate(config, tmp_path)
        sm = _schedule(config, gate)
        target = datetime(2026, 9, 23, 8, 0)
        await sm.record(target, "第一次", NOW)
        await sm.record(target, "第二次补充", NOW + timedelta(minutes=5))
        stored = await gate.get_commitments(NOW)
        await gate.close()
        return stored

    stored = asyncio.run(flow())
    assert len(stored) == 1
    assert stored[0]["content"] == "第二次补充"
