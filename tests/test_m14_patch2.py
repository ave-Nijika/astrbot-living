"""M14-补丁2：复用优先——删固定时窗/独立间隔/独立上限 + 收敛不归零
+ 面板 bridge 化 + 面板双档暴露。

T1 概率仅由基础 × 心境 × 收敛构成（无时窗因子）；
T2 共享闸门拦截念头、发送消耗共享配额（与活动分享同池）；
T3 无独立间隔（initiative_interval 理由码不存在，间隔只由共享闸门决定）；
T4 收敛衰减带下限 0.1，streak 再大概率也 > 0；
T5 streak=6 不再硬静默（backoff_silent 移除）；
T6 streak 跨日衰减：每天至多 -1，同日多次评估只减一次；
T7 面板请求层源码断言：moodRequest bridge 优先 + fetch 回退，endpoint
   为插件内相对路径（不含 /api/v1 前缀）；
T8 死代码扫描：已删标识符在 core/ 与 schema 中 0 命中；
F5 面板双档暴露源码断言：GROUP_LABELS 中文标签、新手卡键名、档位映射。
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.initiative import (
    STATE_KEY_PENDING_SETTLE,
    InitiativeEngine,
    mood_energy_factor,
)

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")

NOW = datetime(2026, 10, 3, 20, 0, 0)
MASTER_UMO = "aiocqhttp:FriendMessage:10001"  # 假号（测试先例），非真实 QQ 号
LINE = "今天路过那家店，想起你说想喝它家的奶茶。"


class ScriptedRng:
    def __init__(self, values=()):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)

    def choice(self, seq):
        v = self.values.pop(0)
        return seq[int(v * len(seq)) % len(seq)]


class FakeGate:
    def __init__(self, allow_message=True, gate_reason="ok", asleep=False):
        self.allow_message = allow_message
        self.gate_reason = gate_reason
        self.asleep = asleep
        self.message_sends = 0
        self.should_send_calls = 0

    def is_asleep_now(self, now=None):
        return self.asleep

    async def should_send_message(self, now=None):
        self.should_send_calls += 1
        if not self.allow_message:
            return False, self.gate_reason
        return True, "ok"

    async def note_message_sent(self, now=None):
        self.message_sends += 1

    async def state_get(self, key):
        return None

    async def state_set(self, key, value):
        pass


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeLLM:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    async def __call__(self, prompt, system=None, **kwargs):
        self.calls.append((prompt, system))
        return self.outputs.pop(0) if self.outputs else ""


class FakeMood:
    def __init__(self, energy=0.5):  # 0.5 → 心境因子 1.0，裸看概率构成
        self.energy = energy

    def digest(self):
        return "心情平静"


class FakeWriter:
    def __init__(self):
        self.written = []

    async def __call__(self, text, dedup_key):
        self.written.append((text, dedup_key))


def make_engine(config=None, gate=None, rng=None, llm=None, mood=None,
                sender=None, session=MASTER_UMO, writer=None):
    engine = InitiativeEngine(
        config_getter=lambda: config if config is not None else {
            "initiative": {
                "enabled": True,
                "base_probability": 0.18,
                "unanswered_backoff": True,
                "final_review_enabled": True,
                "sources": "random_miss,open_topic",
            },
            "output_gate": {
                "daily_message_limit": 10,
                "message_min_interval_minutes": 30,
                "target_sessions": MASTER_UMO,
            },
        },
        gate=gate if gate is not None else FakeGate(),
        llm_call=llm if llm is not None else FakeLLM([LINE]),
        mood=mood if mood is not None else FakeMood(),
        sender=sender if sender is not None else FakeSender(),
        persona_getter=lambda: "你是小澄。",
        session_getter=lambda: session,
        speech_writer=writer if writer is not None else FakeWriter(),
        rng=rng if rng is not None else ScriptedRng([0.0, 0.0]),
    )
    engine._loaded = True  # 直测内存状态；持久化路径由补丁1文件覆盖
    return engine


# ---------------------------------------------------------------------------
# T1：概率构成（无时窗）
# ---------------------------------------------------------------------------
def test_probability_is_base_times_mood_only():
    engine = make_engine(mood=FakeMood(0.8))  # ×1.2
    assert engine._probability(engine._cfg()) == pytest.approx(0.18 * 1.2)
    # 与时刻无关：不存在时窗因子（连签名带 now 的入口都没有）
    import inspect

    params = inspect.signature(InitiativeEngine._probability).parameters
    assert set(params) == {"self", "cfg"}


def test_mood_energy_factor_unchanged():
    """D3 心境调制原样（红线 1）：≥0.7 ×1.2 / ≤0.3 ×0.5 / 其余 ×1.0。"""
    assert mood_energy_factor(FakeMood(0.8)) == 1.2
    assert mood_energy_factor(FakeMood(0.2)) == 0.5
    assert mood_energy_factor(FakeMood(0.5)) == 1.0


# ---------------------------------------------------------------------------
# T2：共享闸门（每日配额/最小间隔的唯一来源）
# ---------------------------------------------------------------------------
def test_shared_gate_blocks_initiative_before_generation():
    gate = FakeGate(allow_message=False, gate_reason="msg_interval")
    sender = FakeSender()
    writer = FakeWriter()
    llm = FakeLLM()
    engine = make_engine(gate=gate, sender=sender, writer=writer, llm=llm,
                         rng=ScriptedRng([0.0]))  # 掷点已过，预检拦下
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "gate:msg_interval"
    assert sender.sent == [] and writer.written == []
    assert gate.message_sends == 0
    assert llm.calls == []  # 预检在生成之前，不浪费 token


def test_send_consumes_shared_quota():
    gate = FakeGate()
    engine = make_engine(gate=gate, rng=ScriptedRng([0.0, 0.0]))
    result = asyncio.run(engine.tick(NOW))
    assert result["sent"] is True
    assert gate.message_sends == 1  # note_message_sent 记账（共享 last_message_at）


# ---------------------------------------------------------------------------
# T3：无独立间隔
# ---------------------------------------------------------------------------
def test_no_independent_interval_between_initiatives():
    """两次念头间隔只由共享闸门决定：替身闸门放行 → 连续两次发送都成立。"""
    gate = FakeGate()
    sender = FakeSender()
    engine = make_engine(
        gate=gate, sender=sender, llm=FakeLLM([LINE, LINE]),
        rng=ScriptedRng([0.0, 0.0, 0.0, 0.0]),
    )
    first = asyncio.run(engine.tick(NOW))
    second = asyncio.run(engine.tick(NOW + timedelta(minutes=1)))
    assert first["sent"] is True and second["sent"] is True
    assert len(sender.sent) == 2  # 无 150 分钟独立间隔拦截
    # 第二次发送前：上一念头已结算为未回应（streak 1），共享闸门放行
    assert engine._streak == 1
    assert gate.should_send_calls >= 4  # 每次 tick 预检 + 正式检查


# ---------------------------------------------------------------------------
# T4/T5：收敛不归零
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("streak,factor", [
    (3, 0.5), (6, 0.25), (9, 0.125), (30, 0.1), (99, 0.1),
])
def test_backoff_floor_keeps_probability_alive(streak, factor):
    """T4：×0.5^floor(streak/3)，下限 0.1（封顶）——全部 > 0。"""
    engine = make_engine(mood=FakeMood(0.5))  # 心境因子 1.0
    engine._streak = streak
    p = engine._probability(engine._cfg())
    assert p == pytest.approx(0.18 * factor)
    assert p > 0


def test_no_hard_silence_at_high_streak():
    """T5：streak=6 仍正常评估（backoff_silent 已移除，永不硬静默）。"""
    llm = FakeLLM()
    engine = make_engine(llm=llm, rng=ScriptedRng([0.99]))
    engine._streak = 6
    result = asyncio.run(engine.tick(NOW))
    assert result["reason"] == "no_roll"  # 只是概率更低，评估照常


# ---------------------------------------------------------------------------
# T6：streak 跨日衰减
# ---------------------------------------------------------------------------
def test_streak_decays_once_per_day():
    engine = make_engine(rng=ScriptedRng([0.99, 0.99, 0.99]))
    engine._streak = 5
    engine._streak_date = (NOW - timedelta(days=1)).date()
    asyncio.run(engine.tick(NOW))
    assert engine._streak == 4  # 跨日 -1
    assert engine._streak_date == NOW.date()
    asyncio.run(engine.tick(NOW + timedelta(minutes=5)))
    assert engine._streak == 4  # 同日多次评估只减一次
    asyncio.run(engine.tick(NOW + timedelta(days=1)))
    assert engine._streak == 3  # 次日再 -1


def test_streak_decay_floors_at_zero_and_skips_unknown_date():
    engine = make_engine(rng=ScriptedRng([0.99]))
    engine._streak = 1
    engine._streak_date = (NOW - timedelta(days=3)).date()
    asyncio.run(engine.tick(NOW))
    assert engine._streak == 0  # 不为负
    engine2 = make_engine(rng=ScriptedRng([0.99]))
    engine2._streak = 5
    engine2._streak_date = None  # 旧状态无日期键：从现在起跟踪，不追溯惩罚
    asyncio.run(engine2.tick(NOW))
    assert engine2._streak == 5


def test_owner_message_maintains_streak_date():
    engine = make_engine()
    asyncio.run(engine.note_owner_message(NOW))
    assert engine._streak_date == NOW.date()


# ---------------------------------------------------------------------------
# T7：面板请求层（app.js 源码断言）
# ---------------------------------------------------------------------------
def test_mood_request_has_bridge_first_and_fetch_fallback():
    # bridge 优先：apiFn.call(page, endpoint, ...) 且 GET/POST 两形态齐备
    assert "apiFn.call(page, endpoint" in APP_JS
    assert "page && page.apiPost" in APP_JS and "page && page.apiGet" in APP_JS
    # fetch 回退保留（PLUGIN_API_BASE 直连仍存在）
    assert "fetch(`${PLUGIN_API_BASE}${path}`" in APP_JS
    # bridge 失败回退的注释存在（两分支结构可见）
    assert "fetch 回退" in APP_JS


def test_mood_request_endpoint_is_plugin_relative():
    # endpoint 归一化：剥掉前导斜杠，桥接调用不含 /api/v1 前缀
    assert 'replace(/^\\//, "")' in APP_JS
    assert not re.search(r"api(?:Get|Post)\(\s*[`\"']?/api/v1", APP_JS)
    assert not re.search(r"api(?:Get|Post)\([^)]*PLUGIN_API_BASE", APP_JS)
    # E5：历史错误结论注释已更正（不再声称"绕过 bridge"）
    assert "绕过 bridge，直接同源 fetch" not in APP_JS
    assert "allow-same-origin" in APP_JS  # 新注释引用了硬证据


def test_mood_request_error_copy_shared():
    # E2：401 专用文案与网络错误文案仍在一处（fetch 分支），两路共用
    assert "登录已过期，请重新登录 dashboard" in APP_JS
    assert "网络错误：无法连接 dashboard" in APP_JS


# ---------------------------------------------------------------------------
# T8：死代码扫描（core/ 与 schema 中 0 命中）
# ---------------------------------------------------------------------------
DEAD_PLAIN = [
    "hourly_weights", "DEFAULT_HOURLY_WEIGHTS", "parse_hourly_weights",
    "hour_weight", "daily_max", "_sent_today", "_sent_log",
    "STATE_KEY_SENT_LOG", "SENT_LOG_MAX", "_last_sent_at",
    "STATE_KEY_LAST_SENT", "initiative_interval", "backoff_silent",
]


def test_dead_identifiers_absent_from_core_and_schema():
    """T8：min_interval_minutes 需引号锚定——共享闸门的
    message_min_interval_minutes 含其子串，属合法存在，不得误报。"""
    targets = list(WORKDIR.glob("core/*.py"))
    targets.append(WORKDIR / "_conf_schema.json")
    for target in targets:
        text = target.read_text(encoding="utf-8")
        for name in DEAD_PLAIN:
            assert name not in text, f"{target.name} 残留 {name}"
        assert '"min_interval_minutes"' not in text, (
            f"{target.name} 残留 initiative.min_interval_minutes"
        )


def test_schema_initiative_group_shrunk_to_five_keys():
    """C5/A3/B3 + F4：3 键随 schema 删除自动消失，剩 5 键。
    M19-补丁1 D2：prompt_open_topic / prompt_line 两个提示词键入组（7 键），
    其余业务键集合不变。"""
    items = SCHEMA["advanced"]["items"]["initiative"]["items"]
    assert set(items) == {
        "enabled", "base_probability", "unanswered_backoff",
        "final_review_enabled", "sources",
        "prompt_open_topic", "prompt_line",  # M19-补丁1 D2
    }
    # F2：专家面板键文案审查——每键有 description 与 hint
    for key, item in items.items():
        assert item.get("description"), f"{key} 缺 description"
        assert item.get("hint"), f"{key} 缺 hint"


# ---------------------------------------------------------------------------
# F5：面板双档暴露（app.js 源码断言）
# ---------------------------------------------------------------------------
def test_group_labels_has_initiative_chinese_label():
    assert re.search(r'initiative:\s*"主动搭话"', APP_JS)


def test_novice_initiative_card_exists_and_writes_right_keys():
    assert "function initiativeCard()" in APP_JS
    for key in ("ini.enabled", "ini.base_probability", "ini.unanswered_backoff"):
        assert key in APP_JS, f"新手卡未读写 {key}"
    # 卡位置：renderNovice 里 initiativeCard() 在 scheduleCard() 之前
    idx_novice = APP_JS.index("function renderNovice()")
    idx_card = APP_JS.index("grid.appendChild(initiativeCard());", idx_novice)
    idx_schedule = APP_JS.index("grid.appendChild(scheduleCard());", idx_novice)
    assert idx_card < idx_schedule
    # 总开关关闭时隐藏明细
    assert 'detail.classList.toggle("hidden", value === false)' in APP_JS


def test_initiative_level_mapping_pure_function_boundaries():
    """F3：档位→概率映射（0.08/0.18/0.35）与未知档位回落 0.18。"""
    block = re.search(
        r"const INITIATIVE_LEVELS = \[(.*?)\];", APP_JS, re.S
    )
    assert block, "缺 INITIATIVE_LEVELS 映射表"
    pairs = re.findall(r'\["(\w+)",\s*"[^"]*",\s*(0\.\d+)\]', block.group(1))
    assert pairs == [("quiet", "0.08"), ("moderate", "0.18"), ("active", "0.35")]
    fn = re.search(
        r"function initiativeLevelToProbability\(level\) \{.*?\n\}",
        APP_JS, re.S,
    )
    assert fn, "缺 initiativeLevelToProbability 纯函数"
    assert "return 0.18" in fn.group(0)  # 未知档位回落默认


def test_novice_backoff_copy_uses_master_wording():
    assert "你不理它时，它会慢慢安静下来（不会完全不理你）" in APP_JS
