"""M5-补丁2 测试：睡眠失控修复（时序回归为重点）与记忆质检。

验收 10 条全部走真实调用路径（LivingLoop._autonomous_sleep_tick /
SleepManager / LivingGate / MoodState 真实实例），不测策略函数空壳。

注意：LivingGate/MoodState 持有 aiosqlite 连接（绑定创建时的事件循环），
跨 asyncio.run 复用实例会死锁——所有多步流程必须包进**单个** asyncio.run。
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.activities import ActivityContext, TOPIC_POOL
from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.mood import MoodState, apply_nap_effects
from core.sleep import SleepManager
from core.topic_fingerprint import topic_fingerprint

NOW = datetime(2026, 9, 21, 15, 0, 0)
WORKDIR = Path(__file__).resolve().parents[1]
# rng 固定 0.5 时小睡时长 = 20 + (90-20)*0.5 = 55 分钟
NAP_DURATION_MIN = 55


def _config(**overrides):
    cfg = {
        "decision": {"decision_mode": "rules", "interest_daily_decay": 0.9},
        "sleep": {
            "sleep_mode": "autonomous",
            "min_awake_minutes": 0,  # 默认关闭，便于单项隔离
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
    """should_nap 用的最小心境（energy 可控）。"""

    def __init__(self, energy=0.05, sleep_debt=0.0):
        self.energy = energy
        self.sleep_debt = sleep_debt

    def interest_weight(self, topic, repeat_count=0):
        return 0.3


# ---------------------------------------------------------------------------
# 验收 1：小睡冷却（A1）——经真实 enter/exit 路径记账
# ---------------------------------------------------------------------------
def test_nap_cooldown_blocks(tmp_path):
    async def flow():
        config = _config(min_awake_minutes=0)  # 隔离 A7，专验 A1 冷却
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)
        mood = _Mood()
        # 一次真实小睡（enter → 到点 exit 记账；时长 55 分钟）
        await gate.enter_autonomous_sleep(
            NOW + timedelta(minutes=NAP_DURATION_MIN), "nap", NOW
        )
        ended_at = NOW + timedelta(minutes=NAP_DURATION_MIN)
        await gate.exit_autonomous_sleep(ended_at)
        # 冷却 240 分钟内（结束后 30 分钟）→ False
        blocked, _ = manager.should_nap(mood, ended_at + timedelta(minutes=30))
        # 冷却过后 → True
        allowed, _ = manager.should_nap(mood, ended_at + timedelta(minutes=241))
        await gate.close()
        return blocked, allowed

    blocked, allowed = asyncio.run(flow())
    assert blocked is False
    assert allowed is True


# ---------------------------------------------------------------------------
# 验收 2：每日上限（A2，含跨日恢复）
# ---------------------------------------------------------------------------
def test_nap_daily_limit_and_cross_day(tmp_path):
    async def flow():
        config = _config(min_awake_minutes=0, nap_cooldown_minutes=0)  # 隔离 A1
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)
        mood = _Mood()
        for i in range(2):  # 当日两次小睡
            start = NOW + timedelta(hours=i * 2)
            await gate.enter_autonomous_sleep(
                start + timedelta(minutes=NAP_DURATION_MIN), "nap", start
            )
            await gate.exit_autonomous_sleep(start + timedelta(minutes=NAP_DURATION_MIN))
        blocked, _ = manager.should_nap(mood, NOW + timedelta(hours=5))
        # 跨日：昨天的一次不带入今天
        gate2 = LivingGate(config_getter=lambda: config,
                           db_path=str(tmp_path / "gate2.db"), rng=lambda: 0.5)
        manager2 = SleepManager(config_getter=lambda: config, gate=gate2,
                                rng=lambda: 0.5)
        yesterday = NOW - timedelta(days=1)
        await gate2.enter_autonomous_sleep(
            yesterday + timedelta(minutes=NAP_DURATION_MIN), "nap", yesterday
        )
        await gate2.exit_autonomous_sleep(yesterday + timedelta(minutes=NAP_DURATION_MIN))
        allowed_today, _ = manager2.should_nap(mood, NOW)
        await gate.close()
        await gate2.close()
        return blocked, allowed_today

    blocked, allowed_today = asyncio.run(flow())
    assert blocked is False  # 上限 2 已满
    assert allowed_today is True  # 跨日恢复


# ---------------------------------------------------------------------------
# 验收 3：小睡不清债（A3）
# ---------------------------------------------------------------------------
def test_nap_does_not_reduce_debt(tmp_path):
    async def flow():
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.05
        mood.sleep_debt = 50.0
        detail = await apply_nap_effects(mood, 45.0)
        debt_after = mood.sleep_debt
        await mood.close()
        return debt_after, detail

    debt, detail = asyncio.run(flow())
    assert debt == 50.0  # 债不变（A3）
    assert detail["energy"] == pytest.approx(0.35)  # 精力照常恢复


# ---------------------------------------------------------------------------
# 验收 4（重点）：长睡优先——energy 低、债高、睡意达阈值 → 长睡而非小睡
# ---------------------------------------------------------------------------
def test_long_sleep_takes_priority_over_nap(tmp_path):
    async def flow():
        config = _config(min_awake_minutes=0)
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.energy = 0.05
        mood.sleep_debt = 80.0  # 睡意 0.3325+0.28+circadian ≥ 阈值
        loop = LivingLoop(
            gate=gate, memory_getter=lambda: None,
            config_getter=lambda: config, sleep_manager=manager, mood=mood,
        )
        now = NOW.replace(hour=15)  # 白天：旧实现会先走小睡分支
        await loop._autonomous_sleep_tick(now)
        state = gate.sleep_state(now)
        await mood.close()
        await gate.close()
        return state["kind"], state["asleep"]

    kind, asleep = asyncio.run(flow())
    assert asleep is True
    assert kind == "long"  # 长睡，不是小睡（A5 顺序反转）


# ---------------------------------------------------------------------------
# 验收 5：夜间禁小睡（A6）
# ---------------------------------------------------------------------------
def test_no_nap_during_circadian_window(tmp_path):
    config = _config(min_awake_minutes=0, nap_cooldown_minutes=0)
    gate = LivingGate(config_getter=lambda: config,
                      db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
    manager = SleepManager(config_getter=lambda: config, gate=gate, rng=lambda: 0.5)
    mood = _Mood(energy=0.05)  # 精力条件满足
    night = NOW.replace(hour=23, minute=30)  # 23:00-07:00 窗内
    allowed, _ = manager.should_nap(mood, night)
    assert allowed is False


# ---------------------------------------------------------------------------
# 验收 6/7：睡前回顾——过滤回声（B1）、幂等（B2）、无日期前缀（B3）
# ---------------------------------------------------------------------------
class FakeMemory:
    def __init__(self):
        self.added = []
        self.results = {}  # query → rows

    async def search(self, query, k=5, **kwargs):
        return list(self.results.get(query, []))[:k]

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append((content, metadata))
        return len(self.added)


def _bare_loop(memory, config=None):
    loop = LivingLoop(
        gate=LivingGate(config_getter=lambda: config or _config(),
                        db_path=":memory:", rng=lambda: 0.5),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: config or _config(),
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop


def test_bedtime_review_filters_own_echo(tmp_path):
    memory = FakeMemory()
    echo_row = {
        "content": "9月21日睡前想了想今天：9月21日睡前想了想今天：……",
        "metadata": {"topics": ["睡前回顾"]},
    }
    real_row = {"content": "我想 read 来着，没成（活动超时）。", "metadata": {}}
    memory.results["9月21日"] = [echo_row, real_row]
    loop = _bare_loop(memory)
    asyncio.run(loop._write_bedtime_review(NOW))
    assert len(memory.added) == 1
    content, metadata = memory.added[0]
    # B1：回声文本不进新回顾
    assert "想了想今天：" not in content.split("今天想了想：", 1)[1]
    assert "没成" in content  # 真实内容保留
    # B3：正文不再以日期字符串开头
    assert not content.startswith("9月21日")
    assert content.startswith("今天想了想：")
    # B2 依赖：metadata 带当天日期标记
    assert metadata["review_date"] == NOW.date().isoformat()
    assert "睡前回顾" in metadata["topics"]


def test_bedtime_review_idempotent_same_day(tmp_path):
    memory = FakeMemory()
    loop = _bare_loop(memory)

    async def flow():
        await loop._write_bedtime_review(NOW)
        first_count = len(memory.added)
        # 第一次写入的回顾进入检索结果（生产上经"今天想了想"可召回）
        content, metadata = memory.added[0]
        memory.results["今天想了想"] = [{"content": content, "metadata": metadata}]
        for _ in range(2):  # 同一天再调 2 次 → 跳过
            await loop._write_bedtime_review(NOW + timedelta(hours=1))
        return first_count, len(memory.added)

    first_count, total = asyncio.run(flow())
    assert first_count == 1
    assert total == 1  # 只新增 1 条


# ---------------------------------------------------------------------------
# 验收 8：指纹排除——coffee 变体互相排除
# ---------------------------------------------------------------------------
def _pick_ctx(recent, rng):
    return ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=None, gate=None,
        event=None, rng=rng, recent_topics=list(recent),
    )


def test_fingerprint_excludes_topic_family():
    import random

    rng = random.Random(7)
    ctx = _pick_ctx(
        ["coffee extraction science", "DIY home cold brew optimization",
         "v60 paper filter chemical trea"], rng,
    )
    picks = {ctx.pick_topic() for _ in range(100)}
    assert "咖啡" not in picks  # coffee 家族候选被指纹排除
    # 无近期话题时咖啡可选（排除只针对命中指纹）
    rng2 = random.Random(7)
    ctx2 = _pick_ctx([], rng2)
    picks2 = {ctx2.pick_topic() for _ in range(100)}
    assert "咖啡" in picks2


def test_topic_fingerprint_variants():
    assert topic_fingerprint("coffee extraction science") == "coffee"
    assert topic_fingerprint("DIY home cold brew optimization") == "coffee"
    assert topic_fingerprint("v60 paper filter chemical trea") == "coffee"
    assert topic_fingerprint("咖啡萃取") == "coffee"
    assert topic_fingerprint("memory palace techniques") == "memory-technique"
    # 兜底：非规则家族取首实词
    assert topic_fingerprint("medieval maritime history") == "medieval"
    assert topic_fingerprint("") == "unknown"


# ---------------------------------------------------------------------------
# 验收 9：兴趣按经过时长衰减（C3）
# ---------------------------------------------------------------------------
def test_interest_decay_by_elapsed_hours(tmp_path):
    async def flow():
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        mood.interests = {"x": 0.9}
        now = NOW
        await mood.decay_interests_elapsed(now, daily_decay=0.9)  # 首次：落戳
        # 把时间戳拨回 24 小时前 → 再调 → 恰好一天 ×0.9
        await mood._set_raw(
            "interests_decay_at", repr((now - timedelta(hours=24)).timestamp())
        )
        await mood.decay_interests_elapsed(now, daily_decay=0.9)
        value = mood.interests["x"]
        await mood.close()
        return value

    assert asyncio.run(flow()) == pytest.approx(0.81, abs=1e-6)


def test_interest_decay_small_gap_noop(tmp_path):
    """不足 1 小时不衰减（防高频心跳浮点噪声）。"""

    async def flow():
        mood = MoodState(db_path=str(tmp_path / "m.db"))
        await mood.load()
        mood.interests = {"x": 0.9}
        now = NOW
        await mood.decay_interests_elapsed(now, daily_decay=0.9)
        await mood._set_raw(
            "interests_decay_at", repr((now - timedelta(minutes=30)).timestamp())
        )
        await mood.decay_interests_elapsed(now, daily_decay=0.9)
        value = mood.interests["x"]
        await mood.close()
        return value

    assert asyncio.run(flow()) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 验收 10（重点）：时序回归——真实状态机连续 12 轮心跳
# ---------------------------------------------------------------------------
class RecordingGate(LivingGate):
    """记录每次入睡 kind 的 gate（其余行为与真 gate 完全一致）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_kinds = []

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self.entered_kinds.append(kind)
        await super().enter_autonomous_sleep(until, kind, now)


def test_sequential_heartbeats_long_sleep_eventually_wins(tmp_path):
    """模拟连续 12 轮心跳（energy 低、债 0 起始）：
    - 至少发生 1 次长睡（旧实现为 0 次——死循环）；
    - 小睡次数 ≤ nap_max_per_day。
    走真实 _autonomous_sleep_tick → SleepManager → gate 状态机路径。"""
    config = _config(min_awake_minutes=0, nap_cooldown_minutes=240,
                     nap_max_per_day=2)
    gate = RecordingGate(config_getter=lambda: config,
                         db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
    manager = SleepManager(config_getter=lambda: config, gate=gate,
                           rng=lambda: 0.5)

    async def flow():
        mood = MoodState(db_path=str(tmp_path / "mood.db"))
        await mood.load()
        loop = LivingLoop(
            gate=gate, memory_getter=lambda: None,
            config_getter=lambda: config, sleep_manager=manager, mood=mood,
        )
        # 15:00 起，每轮 +45 分钟：小睡1(15:00) → 冷却拦若干轮 → 小睡2
        # (21:00) → 上限拦 → 23:15 入夜长睡。单事件循环内跑完。
        now = NOW.replace(hour=15)
        for _ in range(12):
            mood.energy = 0.05  # 每轮活动把精力耗回保底（真实动力学）
            await loop._autonomous_sleep_tick(now)
            now += timedelta(minutes=45)
        final_state = gate.sleep_state(now)
        await mood.close()
        await gate.close()
        return gate.entered_kinds, final_state, mood.sleep_debt

    kinds, final_state, debt = asyncio.run(flow())
    naps = [k for k in kinds if k == "nap"]
    longs = [k for k in kinds if k == "long"]
    assert len(naps) <= 2, f"小睡次数超上限：{kinds}"
    assert len(longs) >= 1, f"12 轮心跳内没有发生长睡：{kinds}"
    assert final_state["kind"] == "long"  # 最终停在长睡中
    assert debt > 0  # A4：清醒期债在累积


# ---------------------------------------------------------------------------
# A8：小睡状态跨重启存活
# ---------------------------------------------------------------------------
def test_nap_state_survives_restart(tmp_path):
    async def flow():
        config = _config(min_awake_minutes=0)
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        await gate.enter_autonomous_sleep(NOW + timedelta(minutes=55), "nap", NOW)
        await gate.exit_autonomous_sleep(NOW + timedelta(minutes=55))
        await gate.close()
        # 模拟重启：全新实例 load_state
        gate2 = LivingGate(config_getter=lambda: config,
                           db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        await gate2.load_state()
        result = (
            gate2.last_nap_ended_at(),
            gate2.nap_count_today(NOW + timedelta(hours=1)),
        )
        await gate2.close()
        return result

    last_ended, count = asyncio.run(flow())
    assert last_ended is not None
    assert count == 1


# ---------------------------------------------------------------------------
# D4：老配置缺新键时回落默认值
# ---------------------------------------------------------------------------
def test_missing_config_keys_fall_back_to_defaults(tmp_path):
    async def flow():
        config = _config(min_awake_minutes=0)
        config["sleep"].pop("nap_cooldown_minutes", None)
        config["sleep"].pop("nap_max_per_day", None)
        gate = LivingGate(config_getter=lambda: config,
                          db_path=str(tmp_path / "gate.db"), rng=lambda: 0.5)
        manager = SleepManager(config_getter=lambda: config, gate=gate,
                               rng=lambda: 0.5)
        mood = _Mood()
        # 默认冷却 240：刚结束（55 分钟小睡后 30 分钟）仍被拦
        await gate.enter_autonomous_sleep(
            NOW + timedelta(minutes=NAP_DURATION_MIN), "nap", NOW
        )
        ended = NOW + timedelta(minutes=NAP_DURATION_MIN)
        await gate.exit_autonomous_sleep(ended)
        cooldown_blocked, _ = manager.should_nap(mood, ended + timedelta(minutes=30))
        # 默认上限 2：当日两次后拦下
        for i in range(2):
            start = NOW + timedelta(hours=1 + i * 2)
            await gate.enter_autonomous_sleep(
                start + timedelta(minutes=NAP_DURATION_MIN), "nap", start
            )
            await gate.exit_autonomous_sleep(start + timedelta(minutes=NAP_DURATION_MIN))
        limit_blocked, _ = manager.should_nap(mood, NOW + timedelta(hours=6))
        await gate.close()
        return cooldown_blocked, limit_blocked

    cooldown_blocked, limit_blocked = asyncio.run(flow())
    assert cooldown_blocked is False
    assert limit_blocked is False


# ---------------------------------------------------------------------------
# D1：schema 两新键存在且 invisible
# ---------------------------------------------------------------------------
def test_schema_declares_nap_guard_keys():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    sleep_items = schema["advanced"]["items"]["sleep"]["items"]
    cooldown = sleep_items["nap_cooldown_minutes"]
    limit = sleep_items["nap_max_per_day"]
    assert cooldown["type"] == "int" and cooldown["default"] == 240
    assert cooldown.get("invisible") is True
    assert limit["type"] == "int" and limit["default"] == 2
    assert limit.get("invisible") is True


# ---------------------------------------------------------------------------
# D2：新键进面板（GET payload 含两键，可经 POST 保存）
# ---------------------------------------------------------------------------
def test_panel_exposes_new_sleep_keys():
    from core.panel_api import apply_panel_save, build_config_payload, load_schema

    schema = load_schema(WORKDIR)
    config = {"preset": {}, "advanced": {}}
    payload = build_config_payload(config, schema)
    items = payload["schema"]["advanced"]["items"]["sleep"]["items"]
    assert "nap_cooldown_minutes" in items and "nap_max_per_day" in items
    result = apply_panel_save(
        config, schema,
        {"advanced": {"sleep": {"nap_cooldown_minutes": 120}}},
    )
    assert result["count"] == 1
    assert config["advanced"]["sleep"]["nap_cooldown_minutes"] == 120


# ---------------------------------------------------------------------------
# 红线：fixed 模式行为零变化（autonomous tick 在 fixed 下短路）
# ---------------------------------------------------------------------------
