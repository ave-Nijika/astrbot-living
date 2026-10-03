"""M14-补丁1：主动搭话念头系统（initiative）。

A 组：InitiativeEngine 心跳 tick 驱动、全依赖可注入；主流程
掷概率 → 选来源 → 生成（含人格终审）→ 通用闸门 → 发送 → 双写 → 记账，
任一环节失败静默降级。
B/C 组：random_miss / open_topic 两来源；单次 LLM 调用生成 + SKIP 终审；
C3 长度与异常形态防线；C4 final_review_enabled 关闭时 SKIP 指令移除。
D 组：概率 = 基础 × 时段权重 × 心境调制 × 收敛倍率；念头独立间隔/每日
上限与通用闸门取更严者。
E 组：双写复用 M13-补丁1 抽象（E1 重构后活动侧行为逐字不变）。
F 组：未回应收敛——每念头一次结算（F3 口径）、主人消息即时清零（F2）、
streak≥3 概率减半（F4）、streak≥6 安静（F5）、状态持久化（F1）。
G/I 组：INFO 审计（台词只留 20 字预览）；心跳接入与睡眠静默。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import pytest

from core.initiative import (
    DEFAULT_HOURLY_WEIGHTS,
    STATE_KEY_LAST_SENT,
    STATE_KEY_SENT_LOG,
    STATE_KEY_STREAK,
    InitiativeEngine,
    hour_weight,
    mood_energy_factor,
    parse_hourly_weights,
)
from core.living_loop import LivingLoop

NOW = datetime(2026, 10, 3, 20, 0, 0)  # 18-23 段，权重 1.0
# 假号（测试先例 10001），非真实 QQ 号
MASTER_UMO = "aiocqhttp:FriendMessage:10001"
LINE = "今天路过那家店，想起你说想喝它家的奶茶。"

BASE_CONFIG = {
    "initiative": {},
    "output_gate": {
        "daily_message_limit": 10,
        "message_min_interval_minutes": 30,
        "target_sessions": MASTER_UMO,
        "quiet_hours": "",
    },
}


def _config(**overrides):
    cfg = {
        "initiative": {
            "enabled": True,
            "daily_max": 3,
            "min_interval_minutes": 150,
            "base_probability": 0.18,
            "hourly_weights": "",
            "unanswered_backoff": True,
            "final_review_enabled": True,
            "sources": "random_miss,open_topic",
        },
        "output_gate": BASE_CONFIG["output_gate"],
    }
    cfg["initiative"].update(overrides)
    return cfg


class ScriptedRng:
    """确定性骰子：按脚本吐 random() 与 choice() 的值，并留观测记录。"""

    def __init__(self, values=()):
        self.values = list(values)
        self.calls = []

    def random(self):
        v = self.values.pop(0)
        self.calls.append(("random", v))
        return v

    def choice(self, seq):
        v = self.values.pop(0)
        self.calls.append(("choice", v))
        return seq[int(v * len(seq)) % len(seq)]


class FakeGate:
    def __init__(self, allow_message=True, gate_reason="ok", asleep=False):
        self.allow_message = allow_message
        self.gate_reason = gate_reason
        self.asleep = asleep
        self.message_sends = 0
        self.store: dict = {}

    def is_asleep_now(self, now=None):
        return self.asleep

    async def should_send_message(self, now=None):
        return self.allow_message, self.gate_reason if not self.allow_message else "ok"

    async def note_message_sent(self, now=None):
        self.message_sends += 1

    async def state_get(self, key):
        return self.store.get(key)

    async def state_set(self, key, value):
        self.store[key] = value


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return self.ok


class FakeLLM:
    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.calls = []  # (prompt, system)

    async def __call__(self, prompt, system=None, **kwargs):
        self.calls.append((prompt, system))
        if self.error:
            raise self.error
        return self.outputs.pop(0) if self.outputs else ""


class FakeMood:
    def __init__(self, energy=0.8):
        self.energy = energy

    def digest(self):
        return "心情不错，精力充沛"


class FakeWriter:
    def __init__(self):
        self.written = []

    async def __call__(self, text, dedup_key):
        self.written.append((text, dedup_key))


def make_engine(config=None, gate=None, rng=None, llm=None, mood=None,
                sender=None, session=MASTER_UMO, contexts=None, writer=None,
                persona=None):
    gate = gate if gate is not None else FakeGate()
    rng = rng if rng is not None else ScriptedRng([0.0, 0.0])
    llm = llm if llm is not None else FakeLLM([LINE])
    mood = mood if mood is not None else FakeMood()
    sender = sender if sender is not None else FakeSender()
    engine = InitiativeEngine(
        config_getter=lambda: config if config is not None else _config(),
        gate=gate,
        llm_call=llm,
        mood=mood,
        sender=sender,
        persona_getter=(lambda: "你是小凛，温和有点慢热。") if persona is None
        else persona,
        session_getter=(lambda: session) if not callable(session) else session,
        contexts_getter=(lambda: contexts) if not callable(contexts)
        else contexts,
        speech_writer=writer if writer is not None else FakeWriter(),
        rng=rng,
    )
    engine._loaded = True  # 直接测内存状态；持久化恢复有专门用例
    return engine


# ---------------------------------------------------------------------------
# J1：概率与系数
# ---------------------------------------------------------------------------
def test_hour_weight_default_table():
    cases = {7: 0.5, 9: 0.9, 10: 0.9, 12: 0.8, 13: 0.8, 14: 0.6, 17: 0.6,
             18: 1.0, 22: 1.0, 23: 0.0, 2: 0.0, 5: 0.0, 6: 0.5}
    for hour, expected in cases.items():
        assert hour_weight(
            DEFAULT_HOURLY_WEIGHTS, datetime(2026, 10, 3, hour, 0)
        ) == expected


def test_parse_hourly_weights_custom_override_and_fallback():
    assert parse_hourly_weights("10-12=0.25") == ((10, 12, 0.25),)
    # 跨午夜段合法；解析失败/空串 → None（回落内置表）
    assert parse_hourly_weights("06-09=0.5,23-06=0") == (
        (6, 9, 0.5), (23, 6, 0.0),
    )
    assert parse_hourly_weights("坏数据") is None
    assert parse_hourly_weights("10-12=负数") is None
    assert parse_hourly_weights("") is None
    assert parse_hourly_weights(None) is None


def test_mood_energy_factor_bands():
    assert mood_energy_factor(FakeMood(0.8)) == 1.2
    assert mood_energy_factor(FakeMood(0.7)) == 1.2
    assert mood_energy_factor(FakeMood(0.3)) == 0.5
    assert mood_energy_factor(FakeMood(0.2)) == 0.5
    assert mood_energy_factor(FakeMood(0.5)) == 1.0
    assert mood_energy_factor(None) == 1.0

    class NoEnergy:
        pass

    assert mood_energy_factor(NoEnergy()) == 1.0


def test_probability_composition_j1():
    """基础 0.18 × 晚间 1.0 × 心境 1.2 = 0.216；固定 rng 命中与未命中。"""
    engine = make_engine(rng=ScriptedRng([0.2]))
    prob = engine._probability(engine._cfg(), NOW)
    assert prob == pytest.approx(0.18 * 1.0 * 1.2)
    low_hour = NOW.replace(hour=15)  # 14-18 → 0.6
    assert engine._probability(engine._cfg(), low_hour) == pytest.approx(
        0.18 * 0.6 * 1.2
    )
    # 时段权重表配置覆盖（H5）
    engine2 = make_engine(
        config=_config(hourly_weights="14-18=0.1"), rng=ScriptedRng([0.2])
    )
    assert engine2._probability(engine2._cfg(), low_hour) == pytest.approx(
        0.18 * 0.1 * 1.2
    )


def test_backoff_multiplier_halves_probability():
    """F4：streak ≥ 3 → ×0.5^floor(streak/3)；streak 归零恢复。"""
    engine = make_engine(rng=ScriptedRng([0.2]))
    base = engine._probability(engine._cfg(), NOW)
    engine._streak = 3
    assert engine._probability(engine._cfg(), NOW) == pytest.approx(base * 0.5)
    engine._streak = 7  # 若不被 F5 拦，倍率 0.25（公式一般化）
    assert engine._probability(engine._cfg(), NOW) == pytest.approx(base * 0.25)
    engine._streak = 0
    assert engine._probability(engine._cfg(), NOW) == pytest.approx(base)


# ---------------------------------------------------------------------------
# J2：闸门与前置分支
# ---------------------------------------------------------------------------
def test_disabled_is_fully_silent():
    llm = FakeLLM()
    engine = make_engine(config=_config(enabled=False), llm=llm)
    result = asyncio.run(engine.tick(NOW))
    assert result == {"sent": False, "reason": "disabled"}
    assert llm.calls == []


def test_sleeping_gate_blocks_before_anything():
    llm = FakeLLM()
    engine = make_engine(gate=FakeGate(asleep=True), llm=llm)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "sleeping"
    assert llm.calls == []


def test_daily_max_blocks_independent_count():
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]))
    engine._sent_log = [NOW - timedelta(days=1), NOW - timedelta(minutes=200),
                        NOW - timedelta(minutes=300)]
    result = asyncio.run(engine.tick(NOW))  # 昨日 1 条不计，今日 2 条 < 3 → 放行掷点
    assert result["sent"] is True
    llm = engine._llm_call
    calls_after_first = len(llm.calls)
    engine._sent_log.append(NOW - timedelta(minutes=100))
    result = asyncio.run(engine.tick(NOW.replace(hour=21)))
    assert result["reason"] == "daily_max"
    assert len(llm.calls) == calls_after_first  # 拦下时不产生新的生成调用


def test_initiative_interval_blocks_and_opens():
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]))
    engine._last_sent_at = NOW - timedelta(minutes=10)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "initiative_interval"
    engine._last_sent_at = NOW - timedelta(minutes=151)
    llm = engine._llm_call
    asyncio.run(engine.tick(NOW))  # 间隔已过 → 走到生成
    assert len(llm.calls) == 1


def test_backoff_silent_at_streak_six_and_toggle():
    engine = make_engine(rng=ScriptedRng([0.0]))
    engine._streak = 6
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "backoff_silent"
    # H6 关闭：收敛机制整体不生效
    engine2 = make_engine(
        config=_config(unanswered_backoff=False),
        rng=ScriptedRng([0.0, 0.0]),
    )
    engine2._streak = 6
    result = asyncio.run(engine2.tick(NOW))
    assert result["sent"] is True


def test_no_roll_when_dice_misses():
    llm = FakeLLM()
    engine = make_engine(rng=ScriptedRng([0.99]), llm=llm)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "no_roll"
    assert llm.calls == []


# ---------------------------------------------------------------------------
# J3：SKIP / 空输出 / 异常 / 异常形态
# ---------------------------------------------------------------------------
def _skip_case_engine(llm):
    return make_engine(rng=ScriptedRng([0.0, 0.0]), llm=llm)  # 命中掷点 + random_miss


def test_llm_outputs_skip():
    engine = _skip_case_engine(FakeLLM(["SKIP"]))
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "llm_skip"
    assert result["sent"] is False


def test_llm_empty_output():
    engine = _skip_case_engine(FakeLLM(["  "]))
    assert asyncio.run(engine.tick(NOW))["reason"] == "llm_skip"


def test_llm_exception_is_llm_skip():
    engine = _skip_case_engine(FakeLLM(error=RuntimeError("模型挂了")))
    assert asyncio.run(engine.tick(NOW))["reason"] == "llm_skip"


@pytest.mark.parametrize("bad", ["看这个 https://example.com/x", "```python\nx=1```"])
def test_ooc_forms_are_skipped(bad):
    engine = _skip_case_engine(FakeLLM([bad]))
    assert asyncio.run(engine.tick(NOW))["reason"] == "llm_skip"


def test_final_review_off_removes_skip_instruction():
    llm = FakeLLM(["今晚上要不要一起看那部剧？"])
    engine = make_engine(
        config=_config(final_review_enabled=False),
        rng=ScriptedRng([0.0, 0.0]),
        llm=llm,
    )
    result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True
    prompt, _system = llm.calls[0]
    assert "SKIP" not in prompt


def test_line_truncated_to_120():
    long_line = "啊" * 150
    engine = _skip_case_engine(FakeLLM([long_line]))
    result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True
    assert len(result["text"]) == 120


# ---------------------------------------------------------------------------
# 端到端：来源选择 / 通用闸门 / 记账
# ---------------------------------------------------------------------------
def test_send_success_random_miss_end_to_end():
    gate = FakeGate()
    sender = FakeSender()
    writer = FakeWriter()
    engine = make_engine(
        rng=ScriptedRng([0.0, 0.0]),  # 命中 + 选 random_miss
        gate=gate, sender=sender, writer=writer,
    )
    result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True and result["text"] == LINE
    assert sender.sent == [(MASTER_UMO, LINE)]
    # E2 双写：台词交给共享落库，键带念头前缀与来源
    assert writer.written == [(LINE, "#initiative:20261003_200000:random_miss")]
    # D5 记账：通用闸门配额消费 + F1 念头账本
    assert gate.message_sends == 1
    assert engine._last_sent_at == NOW
    assert engine._pending_settle_at == NOW
    assert len(engine._sent_log) == 1


def test_send_success_open_topic_uses_extraction():
    contexts = [
        {"role": "user", "content": "最近在看那部时间循环的剧"},
        {"role": "assistant", "content": "对啊我也在想剧情走向"},
    ]
    llm = FakeLLM(["那部时间循环的剧", "那部剧你看到第几集了？"])
    engine = make_engine(
        rng=ScriptedRng([0.0, 0.99]),  # 命中 + 选 open_topic
        llm=llm, contexts=contexts,
    )
    result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True
    assert len(llm.calls) == 2  # B2 提取 + C1 生成
    extract_prompt, _ = llm.calls[0]
    assert "时间循环的剧" in extract_prompt
    gen_prompt, _ = llm.calls[1]
    assert "那部时间循环的剧" in gen_prompt  # 话题材料进生成 prompt


def test_open_topic_extraction_failure_no_fallback_to_random_miss():
    contexts = [{"role": "user", "content": "最近在看那部时间循环的剧"}]
    llm = FakeLLM(["NONE"])
    engine = make_engine(rng=ScriptedRng([0.0, 0.99]), llm=llm, contexts=contexts)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "no_source"
    assert len(llm.calls) == 1  # 只有提取调用，不降级生成 random_miss
    # 无上下文时 open_topic 同样不可用（零 LLM 调用即判定）
    engine2 = make_engine(rng=ScriptedRng([0.0, 0.99]), contexts=None)
    assert asyncio.run(engine2.tick(NOW))["reason"] == "no_source"


def test_common_gate_blocks_after_generation():
    gate = FakeGate(allow_message=False, gate_reason="msg_daily_limit")
    sender = FakeSender()
    engine = make_engine(
        rng=ScriptedRng([0.0, 0.0]), gate=gate, sender=sender
    )
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "gate:msg_daily_limit"
    assert sender.sent == []
    assert gate.message_sends == 0


def test_no_target_session_skips_send():
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]), session=None)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "no_target"


def test_send_failure_consumes_no_quota():
    gate = FakeGate()
    sender = FakeSender(ok=False)
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]), gate=gate, sender=sender)
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "send_failed"
    assert gate.message_sends == 0
    assert engine._last_sent_at is None  # 未发出不记账


def test_audit_line_has_roll_prob_and_20char_preview(caplog):
    long_line = "这句话特别长" * 10  # 60 字
    engine = _skip_case_engine(FakeLLM([long_line]))
    with caplog.at_level(logging.INFO, logger="astrbot"):
        result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True
    audit = [r.getMessage() for r in caplog.records if "[Initiative]" in r.getMessage()]
    assert audit, "缺 [Initiative] 审计行"
    line = audit[-1]
    assert "掷点=0.00" in line and "概率=0.22" in line
    assert "来源=random_miss" in line and "闸门=通过" in line and "终审=通过" in line
    assert f"已发送({len(long_line)}字)" in line
    assert long_line[:20] in line
    assert long_line[20:] not in line  # G2：全文不进日志


# ---------------------------------------------------------------------------
# J4：未回应收敛
# ---------------------------------------------------------------------------
def _sent_engine(now=NOW):
    """发出一条念头后的引擎（已记账）。"""
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]))
    asyncio.run(engine.tick(now))
    return engine


def test_unanswered_initiative_settles_once_per_initiative():
    engine = _sent_engine()
    # 第一次评估：无回应 → streak 1（该念头计为未回应）
    asyncio.run(engine.tick(NOW + timedelta(minutes=5)))
    assert engine._streak == 1
    # 后续评估不再重复结算（一个念头只记一次）
    asyncio.run(engine.tick(NOW + timedelta(minutes=10)))
    assert engine._streak == 1


def test_owner_message_clears_streak_immediately():
    engine = _sent_engine()
    asyncio.run(engine.tick(NOW + timedelta(minutes=5)))
    assert engine._streak == 1
    asyncio.run(engine.note_owner_message(NOW + timedelta(minutes=8)))
    assert engine._streak == 0
    assert engine._pending_settle_at is None
    # 回应后再评估：不重复结算、不回涨
    asyncio.run(engine.tick(NOW + timedelta(minutes=10)))
    assert engine._streak == 0


def test_owner_message_before_next_settle_prevents_increment():
    """F2 即时清零路径：回应落在发出后、下次结算前。"""
    engine = _sent_engine()
    asyncio.run(engine.note_owner_message(NOW + timedelta(minutes=2)))
    asyncio.run(engine.tick(NOW + timedelta(minutes=5)))
    assert engine._streak == 0


def test_state_persisted_and_restored_across_engines():
    """F1：发送记录/间隔/收敛计数持久化到 gate 状态库，跨引擎（重启）恢复。"""
    gate = FakeGate()
    engine = make_engine(rng=ScriptedRng([0.0, 0.0]), gate=gate)
    engine._loaded = False  # 走真实加载/持久化路径
    asyncio.run(engine.tick(NOW))
    assert json.loads(gate.store[STATE_KEY_SENT_LOG]) == [NOW.isoformat()]
    assert gate.store[STATE_KEY_LAST_SENT] == NOW.isoformat()
    assert gate.store[STATE_KEY_STREAK] == "0"

    # 新引擎（模拟重启）：状态恢复后 150 分钟间隔立即生效
    engine2 = make_engine(rng=ScriptedRng([0.0, 0.0]), gate=gate)
    engine2._loaded = False
    result = asyncio.run(engine2.tick(NOW + timedelta(minutes=5)))
    assert result["reason"] == "initiative_interval"


# ---------------------------------------------------------------------------
# E1：共享落库重构——活动侧行为逐字不变 + 念头落库
# ---------------------------------------------------------------------------
class FakeCtxMgr:
    def __init__(self):
        self.cid = "cid-1"
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return self.cid

    async def new_conversation(self, umo):
        return self.cid

    async def add_message_pair(self, cid, user_msg, assistant_msg):
        self.pairs.append((cid, user_msg, assistant_msg))


class FakeLmMgr:
    def __init__(self):
        self.calls = []

    async def add_message(self, **kwargs):
        self.calls.append(kwargs)


class ZeroMemory:
    async def add(self, content, **kwargs):
        raise AssertionError("活动路径不得直塞记忆（M13-补丁1 C1）")

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class FakeActivity:
    def __init__(self, name="surf"):
        self.name = name
        self.description = f"{name} 的描述"


def make_loop_with_stores():
    mgr = FakeCtxMgr()
    lm = FakeLmMgr()
    loop = LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=ZeroMemory()),
        config_getter=lambda: {
            "decision": {"daily_impulse_limit": 3},
            "output_gate": {"target_sessions": MASTER_UMO},
        },
        activities=[],
        sender=FakeSender(),
        conversation_manager=mgr,
        lm_conversation_manager_getter=lambda: asyncio.sleep(0, result=lm),
    )
    return loop, mgr, lm


def test_shared_store_activity_side_verbatim():
    """E1 重构后活动侧幂等键与占位形态逐字不变（红线 4）。"""
    loop, mgr, lm = make_loop_with_stores()
    asyncio.run(loop._write_activity_experience(
        FakeActivity("surf"), "10月3日我读了媒介理论", "20261003_120000"
    ))
    assert mgr.pairs[-1][1]["content"] == "(自主活动：surf)"
    assert mgr.pairs[-1][2]["content"] == "10月3日我读了媒介理论"
    assert lm.calls[-1]["session_id"] == MASTER_UMO
    # 幂等：同键重放不重复写
    asyncio.run(loop._write_activity_experience(
        FakeActivity("surf"), "10月3日我读了媒介理论", "20261003_120000"
    ))
    assert len(mgr.pairs) == 1 and len(lm.calls) == 1
    # 同秒不同活动 = 两次经历，互不顶掉
    asyncio.run(loop._write_activity_experience(
        FakeActivity("read"), "10月3日我读了另一篇", "20261003_120000"
    ))
    assert len(mgr.pairs) == 2 and len(lm.calls) == 2


def test_shared_store_initiative_side_writes_placeholder():
    """E2：念头台词经共享落库写入双存储，占位与标签独立于活动侧。"""
    loop, mgr, lm = make_loop_with_stores()
    asyncio.run(loop._write_speech_to_stores(
        "今天路过那家店，想起你了。",
        "#initiative:20261003_200000:random_miss",
        "(主动搭话)",
        label="主动搭话",
    ))
    assert mgr.pairs[-1][1]["content"] == "(主动搭话)"
    assert mgr.pairs[-1][2]["content"] == "今天路过那家店，想起你了。"
    assert lm.calls[-1]["session_id"] == MASTER_UMO
    assert lm.calls[-1]["role"] == "assistant"
    assert lm.calls[-1]["is_bot_message"] is True


def test_shared_store_switch_gates_both_callers():
    """B5 开关（decision.activity_context_write）同时约束活动与念头落库。"""
    loop, mgr, lm = make_loop_with_stores()
    loop._config_getter = lambda: {
        "decision": {"activity_context_write": False},
        "output_gate": {"target_sessions": MASTER_UMO},
    }
    asyncio.run(loop._write_activity_experience(
        FakeActivity("surf"), "自述", "20261003_120000"
    ))
    asyncio.run(loop._write_speech_to_stores("台词", "#initiative:x:random_miss",
                                             "(主动搭话)", label="主动搭话"))
    assert mgr.pairs == [] and lm.calls == []


# ---------------------------------------------------------------------------
# I1：心跳接入（清醒评估 / 睡眠静默 / 异常隔离）
# ---------------------------------------------------------------------------
class InitiativeProbe:
    """tick 观测替身：可注入异常。"""

    def __init__(self, error=None):
        self.ticks = []
        self.error = error

    async def tick(self, now):
        self.ticks.append(now)
        if self.error:
            raise self.error
        return {"sent": False, "reason": "no_roll"}


class LoopGate(FakeGate):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def daily_limit_info(self, now=None):
        return False, 0, 3


class InstantActivity:
    def __init__(self):
        self.name = "surf"
        self.description = "surf"
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1

        class _Outcome:
            summary = ""
            memory_content = "10月3日我冲了浪"

        return _Outcome()


def make_loop(gate, activity, initiative=None):
    return LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=ZeroMemory()),
        config_getter=lambda: {
            "decision": {"daily_impulse_limit": 3, "max_run_seconds": 300},
            "output_gate": {"target_sessions": MASTER_UMO},
        },
        activities=[activity],
        sender=FakeSender(),
        initiative=initiative,
    )


def test_heartbeat_calls_initiative_tick_when_awake():
    activity = InstantActivity()
    probe = InitiativeProbe()
    loop = make_loop(LoopGate(), activity, probe)
    asyncio.run(loop.heartbeat_once_detailed(NOW))
    assert probe.ticks == [NOW]
    assert activity.runs == 1  # 活动主链路照常


def test_heartbeat_skips_initiative_when_asleep():
    probe = InitiativeProbe()
    loop = make_loop(LoopGate(asleep=True), InstantActivity(), probe)
    asyncio.run(loop.heartbeat_once_detailed(NOW))
    assert probe.ticks == []  # 睡眠期零评估（红线 5）


def test_initiative_exception_does_not_break_heartbeat():
    activity = InstantActivity()
    probe = InitiativeProbe(error=RuntimeError("引擎炸了"))
    loop = make_loop(LoopGate(), activity, probe)
    awake, _reason, _name = asyncio.run(loop.heartbeat_once_detailed(NOW))
    assert awake is True and activity.runs == 1  # 主循环照常


def test_loop_without_initiative_keeps_legacy_behavior():
    """未注入引擎（既有测试/旧装配）→ 通路不存在，心跳零变化。"""
    activity = InstantActivity()
    loop = make_loop(LoopGate(), activity)
    awake, _reason, _name = asyncio.run(loop.heartbeat_once_detailed(NOW))
    assert awake is True and activity.runs == 1
