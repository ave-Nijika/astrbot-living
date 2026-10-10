"""M32+M33-补丁1：质检系统完整升级（分两批）。

第一批 A 组：睡过头交代（side=oversleep）/ 醒来补回复（side=
  pending_reply）纳入主动产出质检——"任何输出给用户看的（模型生成的）
  都要过一遍质检"；固定文案两处（唤醒确认/晚安概率档）按拍板不接。
第一批 B 组：用户在聊避让——目标会话用户说话后 avoid_after_user_minutes
  窗口内，搭话与分享都不出声（接入 should_send_message 第 0 关单点，
  core/initiative.py 保持零改动）；活动照做、经历照写、睡眠侧不变。
第二批 C/D 组：双模型协商 + 档位并存（见文件下半部分）。

测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import json
import logging
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.living_loop import LivingLoop
from core.living_state import LivingGate

WORKDIR = Path(__file__).resolve().parents[1]
MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份
OTHER = "aiocqhttp:GroupMessage:20002"

NOW = datetime(2026, 10, 10, 21, 0)


# ---------------------------------------------------------------------------
# 通用替身（形态沿用 test_m16/m17/m31 先例）
# ---------------------------------------------------------------------------
class FakeSender:
    def __init__(self, ok=True):
        self.sent = []
        self._ok = ok

    async def send(self, session, text):
        self.sent.append((session, text))
        return self._ok


class FakeMemory:
    def __init__(self):
        self.added = []

    async def add(self, text, **kwargs):
        self.added.append(text)


def _qc_capture(result=None):
    calls = []

    async def qc(text, side, umo=""):
        calls.append((text, side, umo))
        if callable(result):
            return result(text, side, umo)
        return result if result is not None else text

    qc.calls = calls
    return qc


class _BareGate:
    """should_send 恒过的最小 gate（m31 FakeGate 同款；awake_standby_active
    是同步判定，__getattr__ 的 async noop 兜不住——显式给 False）。"""

    async def should_send_message(self, now):
        return True, ""

    async def note_message_sent(self, now):
        return None

    def awake_standby_active(self, now=None):
        return False

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return None

        return _noop


def make_loop(cfg=None, sender=None, **attrs):
    loop = LivingLoop(
        gate=_BareGate(),
        memory_getter=lambda: asyncio.sleep(0, result=None),
        config_getter=lambda: cfg or {},
        activities=[],
        sender=sender,
        dream_llm_call=attrs.pop("dream_llm_call", None),
        persona_id_getter=lambda: "default",
        bot_identity_getter=lambda: {},
        proactive_qc=attrs.pop("proactive_qc", None),
    )
    for key, value in attrs.items():
        setattr(loop, key, value)
    return loop


def make_gate(tmp_path, avoid=30, config=None):
    cfg = {
        "decision": {"daily_impulse_limit": 3, "activity_probability": 0.8,
                     "impulse_check_interval_minutes": 45},
        "capabilities": {"cooldown_between_activities_hours": 2.0},
        "sleep": {},
        "output_gate": {"daily_message_limit": 10,
                        "message_min_interval_minutes": 30,
                        "target_sessions": MASTER},
        "initiative": {"avoid_after_user_minutes": avoid},
    }
    if config:
        for group, kv in config.items():
            cfg.setdefault(group, {}).update(kv)
    return LivingGate(
        config_getter=lambda: cfg,
        db_path=str(tmp_path / "state.db"),
        rng=lambda: 0.5,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 第一批 A 组：质检覆盖面（验收 1/2/3 + 总闸 7）
# ---------------------------------------------------------------------------
def test_a1_oversleep_note_goes_through_qc():
    """验收 1：睡过头交代（全链 _handle_oversleep_commitment）经 _maybe_share
    送检，side=oversleep；质检改写生效（rewrite 文本替换原文）。"""
    qc = _qc_capture(lambda text, side, umo: "呀，一觉睡到九点半，对不起嘛")
    sender = FakeSender()
    loop = make_loop(
        cfg={"output_gate": {"target_sessions": MASTER}},
        sender=sender,
        proactive_qc=qc,
    )
    mem = FakeMemory()
    loop._get_memory = lambda: asyncio.sleep(0, result=mem)
    loop._rng = lambda: 0.0  # roll < 0.5 → 主动交代
    loop._bot_identity = lambda: asyncio.sleep(0, result={})
    loop._persona_id = lambda: asyncio.sleep(0, result="default")
    loop._session_id = lambda event: "living_test"
    loop._write_speech_to_stores = lambda *a, **k: asyncio.sleep(0, result=None)

    async def consume(wake_time):
        return {"target_time": "2026-10-10T08:00:00"}

    loop._schedule = types.SimpleNamespace(consume_due_wake=consume)

    assert run(loop._handle_oversleep_commitment(NOW)) is True
    # 送检：side=oversleep、umo=目标会话、对象是改写前的原文（note）
    assert len(qc.calls) == 1
    text, side, umo = qc.calls[0]
    assert side == "oversleep"
    assert umo == MASTER
    assert "睡过头" in text
    # 质检产物替换原文出站
    assert sender.sent == [(MASTER, "呀，一觉睡到九点半，对不起嘛")]
    # 认知记忆路径原样（改前行为）：睡过头认知照写
    assert any("睡过头" in c for c in mem.added)


def test_a1_oversleep_qc_failure_passes_original():
    """验收 3（oversleep 形态）：质检抛异常 → 原文照常发送，一句话不丢。"""
    async def boom(text, side, umo=""):
        raise RuntimeError("judge down")

    sender = FakeSender()
    loop = make_loop(
        cfg={"output_gate": {"target_sessions": MASTER}},
        sender=sender,
        proactive_qc=boom,
    )
    loop._get_memory = lambda: asyncio.sleep(0, result=FakeMemory())
    loop._rng = lambda: 0.0
    loop._bot_identity = lambda: asyncio.sleep(0, result={})
    loop._persona_id = lambda: asyncio.sleep(0, result="default")
    loop._session_id = lambda event: "living_test"
    loop._write_speech_to_stores = lambda *a, **k: asyncio.sleep(0, result=None)
    loop._schedule = types.SimpleNamespace(
        consume_due_wake=lambda wake_time: asyncio.sleep(
            0, result={"target_time": "2026-10-10T08:00:00"}
        )
    )
    assert run(loop._handle_oversleep_commitment(NOW)) is True
    assert len(sender.sent) == 1
    assert "睡过头" in sender.sent[0][1]  # 原文（未质检替换）


class _PendingManager:
    def __init__(self, messages):
        self._messages = list(messages)

    async def take_pending_messages(self):
        return list(self._messages)


def _reply_llm(text):
    async def call(prompt, system_prompt=None):
        return f"REPLY\n{text}"

    return call


def test_a2_pending_reply_goes_through_qc():
    """验收 2：醒来补回复发送前送检，side=pending_reply、umo=最后消息会话。"""
    qc = _qc_capture(lambda text, side, umo: "质检后的话")
    sender = FakeSender()
    loop = make_loop(
        cfg={"sleep": {"pending_reply_enabled": True},
             "output_gate": {"target_sessions": MASTER}},
        sender=sender,
        dream_llm_call=_reply_llm("刚看到，昨晚睡得沉"),
        proactive_qc=qc,
    )
    loop._sleep_manager = _PendingManager([
        {"session": MASTER, "text": "睡了？", "at": NOW.isoformat()},
    ])
    loop._write_speech_to_stores = lambda *a, **k: asyncio.sleep(0, result=None)

    run(loop._settle_pending_replies(NOW, actual_h=7.2))
    assert qc.calls == [("刚看到，昨晚睡得沉", "pending_reply", MASTER)]
    assert sender.sent == [(MASTER, "质检后的话")]


def test_a2_pending_reply_qc_failure_passes_original():
    """验收 3（pending_reply 形态）：质检超时/异常 → 原文照发。"""
    async def boom(text, side, umo=""):
        raise TimeoutError("judge timeout")

    sender = FakeSender()
    loop = make_loop(
        cfg={"sleep": {"pending_reply_enabled": True},
             "output_gate": {"target_sessions": MASTER}},
        sender=sender,
        dream_llm_call=_reply_llm("糊弄版回复"),
        proactive_qc=boom,
    )
    loop._sleep_manager = _PendingManager([
        {"session": MASTER, "text": "在吗", "at": NOW.isoformat()},
    ])
    loop._write_speech_to_stores = lambda *a, **k: asyncio.sleep(0, result=None)

    run(loop._settle_pending_replies(NOW, actual_h=7.2))
    assert sender.sent == [(MASTER, "糊弄版回复")]


def test_a2_pending_reply_no_qc_injection_backward_compatible():
    """旧装配（proactive_qc=None）→ 补回复直发（既有测试/旧装配零影响）。"""
    sender = FakeSender()
    loop = make_loop(
        cfg={"sleep": {"pending_reply_enabled": True},
             "output_gate": {"target_sessions": MASTER}},
        sender=sender,
        dream_llm_call=_reply_llm("直发的话"),
        proactive_qc=None,
    )
    loop._sleep_manager = _PendingManager([
        {"session": MASTER, "text": "在吗", "at": NOW.isoformat()},
    ])
    loop._write_speech_to_stores = lambda *a, **k: asyncio.sleep(0, result=None)

    run(loop._settle_pending_replies(NOW, actual_h=7.2))
    assert sender.sent == [(MASTER, "直发的话")]


def test_v7_master_off_zero_judge_calls(tmp_path):
    """验收 7：judge.mode=off → 新增出口零 judge 调用（原文直发）。
    （_proactive_output_qc 是两新出口共用的质检回调，mode=off 在此单点拦。）"""
    from core.judge import OutputJudge

    calls = []

    async def llm(prompt, system_prompt=None):
        calls.append(prompt)
        return '{"ok": true, "note": ""}'

    judge = OutputJudge(
        config_getter=lambda: {"advanced": {"judge": {"mode": "off"}}},
        llm_call=llm,
        records_path=None,
    )
    plugin, main_module = _make_plugin(tmp_path)
    plugin._judge = judge
    for side in ("oversleep", "pending_reply"):
        out = run(plugin._proactive_output_qc("它要说的话", side, MASTER))
        assert out == "它要说的话"  # 原文放行
    assert calls == []  # 零模型调用


# ---------------------------------------------------------------------------
# 第一批 B 组：用户在聊避让（验收 4/5/6 + 配置与持久化）
# ---------------------------------------------------------------------------
def test_b1_gate_standby_avoid_window(tmp_path):
    """B1 判定本体：目标会话用户说话后 avoid_after_user_minutes（默认 30）
    窗口内 should_send_message 第 0 关拦下（reason=standby_avoid）；
    窗口外放行；无记录不避让；0=关闭。"""
    gate = make_gate(tmp_path)
    try:
        # 无记录 → 不避让
        assert gate.standby_avoid_active(NOW) is False
        allow, reason = run(gate.should_send_message(NOW))
        assert (allow, reason) == (True, "ok")

        run(gate.note_user_message(NOW))
        # 窗口内（NOW+10min）→ 避让
        assert gate.standby_avoid_active(NOW + timedelta(minutes=10)) is True
        allow, reason = run(gate.should_send_message(NOW + timedelta(minutes=10)))
        assert (allow, reason) == (False, "standby_avoid")
        # 恰好到界（30 分钟整）→ 不避让（半开区间）
        assert gate.standby_avoid_active(NOW + timedelta(minutes=30)) is False
        # 窗口外（31 分钟）→ 放行
        allow, reason = run(gate.should_send_message(NOW + timedelta(minutes=31)))
        assert (allow, reason) == (True, "ok")
    finally:
        run(gate.close())

    # 0 = 关闭避让（"不想要的人"可关）
    gate0 = make_gate(tmp_path, avoid=0)
    try:
        run(gate0.note_user_message(NOW))
        assert gate0.standby_avoid_active(NOW + timedelta(minutes=1)) is False
        allow, reason = run(gate0.should_send_message(NOW + timedelta(minutes=1)))
        assert (allow, reason) == (True, "ok")
    finally:
        run(gate0.close())


def test_b1_avoid_window_persists_across_restart(tmp_path):
    """避让锚点持久化：note 后重开 gate（load_state）窗口仍在。"""
    db = str(tmp_path / "state.db")
    cfg_getter = lambda: {"initiative": {"avoid_after_user_minutes": 30},
                          "output_gate": {}, "decision": {}, "capabilities": {},
                          "sleep": {}}
    gate = LivingGate(config_getter=cfg_getter, db_path=db, rng=lambda: 0.5)
    run(gate.note_user_message(NOW))
    run(gate.close())

    gate2 = LivingGate(config_getter=cfg_getter, db_path=db, rng=lambda: 0.5)
    run(gate2.load_state())
    try:
        assert gate2.standby_avoid_active(NOW + timedelta(minutes=5)) is True
    finally:
        run(gate2.close())


def test_b1_initiative_tick_avoided_by_user_activity(tmp_path):
    """验收 4：搭话避让——用户刚说过话 → tick 不发送、reason 可见
    （gate:standby_avoid）；窗口外照常发。initiative.py 零改动（红线）。"""
    from core.initiative import InitiativeEngine

    async def line_llm(prompt, system_prompt=None):
        return "突然想你说说话"  # 纯台词行（不带补回复的 REPLY 协议头）

    gate = make_gate(tmp_path)
    sender = FakeSender()
    engine = InitiativeEngine(
        config_getter=lambda: {"initiative": {"enabled": True,
                                              "base_probability": 1.0,
                                              "sources": "random_miss"}},
        gate=gate,
        llm_call=line_llm,
        mood=None,
        sender=sender,
        persona_getter=lambda: None,
        session_getter=lambda: MASTER,
        contexts_getter=lambda: None,
        speech_writer=None,
        rng=lambda: 0.0,  # 掷点必过
    )
    engine._loaded = True

    # 用户 5 分钟前说过话 → 避让（预检在生成前，token 未花）
    run(gate.note_user_message(NOW - timedelta(minutes=5)))
    result = run(engine.tick(NOW))
    assert result["sent"] is False
    assert "standby_avoid" in result["reason"]
    assert sender.sent == []

    # 窗口外（31 分钟前说的）→ 照常搭话
    gate2 = make_gate(tmp_path)
    try:
        run(gate2.note_user_message(NOW - timedelta(minutes=31)))
        engine._gate = gate2
        result = run(engine.tick(NOW))
        assert result["sent"] is True
        assert sender.sent == [(MASTER, "突然想你说说话")]
    finally:
        run(gate2.close())
        run(gate.close())


def test_b2_share_avoided_by_user_activity(tmp_path):
    """验收 5：分享避让——用户刚说过话 → _maybe_share 不出声；窗口外照常。"""
    gate = make_gate(tmp_path)
    sender = FakeSender()
    writes = []
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=None),
        config_getter=lambda: {"output_gate": {"target_sessions": MASTER}},
        activities=[],
        sender=sender,
        proactive_qc=None,
    )
    loop._write_speech_to_stores = (
        lambda *a, **k: (writes.append(a[0]), asyncio.sleep(0, result=None))[1]
    )

    # 用户 10 分钟前说过话 → 分享被拦（reason=standby_avoid 可见）
    run(gate.note_user_message(NOW - timedelta(minutes=10)))
    run(loop._maybe_share("我刚把那本书翻完了", NOW))
    assert sender.sent == []
    assert writes == []  # 没说出口的话不落话语库

    # 窗口外 → 照常分享
    run(loop._maybe_share("我刚把那本书翻完了", NOW + timedelta(minutes=31)))
    assert sender.sent == [(MASTER, "我刚把那本书翻完了")]
    assert writes == ["我刚把那本书翻完了"]


def test_b4_activities_and_sleep_unaffected(tmp_path):
    """验收 6：避让不误伤——活动判定链（should_wake/无聊曲线）不经过避让
    关；睡眠侧 standby_blocks_sleep 仍只看睡眠侧待机（与避让窗口无关）。"""
    gate = make_gate(tmp_path)
    try:
        base = run(gate.should_wake(NOW))
        run(gate.note_user_message(NOW))
        after = run(gate.should_wake(NOW))
        assert base == after  # 活动判定链不受"用户在聊"影响
    finally:
        run(gate.close())

    # 睡眠侧：_standby_blocks_sleep 只看 gate.awake_standby_active（睡眠侧
    # 待机），用户消息时刻不进入该判定
    loop = make_loop(cfg={"sleep": {"standby_blocks_sleep": True}})
    gate2 = make_gate(tmp_path)
    try:
        run(gate2.note_user_message(NOW))
        loop._gate = gate2
        assert loop._standby_blocks_sleep(NOW) is False  # 无睡眠侧待机 → 不拦睡
        run(gate2.refresh_awake_until(30, NOW))  # 睡眠侧待机（吵醒后）在场
        assert loop._standby_blocks_sleep(NOW) is True  # 既有行为不变
    finally:
        run(gate2.close())


def test_b5_main_records_user_message_from_target_sessions(tmp_path):
    """B 接线：on_any_message 只对目标会话的用户消息记避让锚点——
    她要说的话的受众说话才算"在聊"；别的会话不触发。"""
    plugin, _ = _make_plugin(tmp_path)
    gate = make_gate(tmp_path)
    plugin.gate = gate
    plugin.loop = types.SimpleNamespace(
        _resolve_target_sessions=lambda: ([MASTER], "test"),
        initiative=None,
    )
    try:
        event = types.SimpleNamespace(
            unified_msg_origin=MASTER, get_sender_id=lambda: "10001"
        )
        run(plugin.on_any_message(event))
        assert gate._last_user_message_at is not None

        gate._last_user_message_at = None
        other = types.SimpleNamespace(
            unified_msg_origin=OTHER, get_sender_id=lambda: "20002"
        )
        run(plugin.on_any_message(other))
        assert gate._last_user_message_at is None  # 非目标会话不记
    finally:
        run(gate.close())


def test_b6_schema_key_visible_with_hint():
    """B5/通用：新键 initiative.avoid_after_user_minutes 在 schema（面板
    驱动）可见：默认 30、int、hint 语义完整、独立于睡眠侧键。"""
    schema = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
    item = schema["advanced"]["items"]["initiative"]["items"][
        "avoid_after_user_minutes"
    ]
    assert item["type"] == "int"
    assert item["default"] == 30
    assert item.get("hint")
    assert item.get("section") == ["D", "D3", 8]
    # 睡眠侧待机键语义独立（不复用、不联动）
    sleep_side = schema["advanced"]["items"]["sleep"]["items"][
        "awake_standby_minutes"
    ]
    assert sleep_side is not item


# ---------------------------------------------------------------------------
# 合成插件（m31 make_plugin 同款，供 on_any_message / _proactive_output_qc）
# ---------------------------------------------------------------------------
def _make_plugin(tmp_path):
    import sys

    pkg_name = "living_plugin_under_test_m32"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(WORKDIR)]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    import importlib

    main_module = importlib.import_module(f"{pkg_name}.main")
    plugin = object.__new__(main_module.LivingPlugin)
    seed = {"preset": {}, "advanced": {}}
    cfg_path = Path(tmp_path) / "astrbot_plugin_living_config.json"
    cfg_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    plugin._plugin_config_path = lambda: str(cfg_path)
    plugin.config = seed
    plugin.context = types.SimpleNamespace()
    plugin._judge = None
    plugin._judge_tasks = set()
    plugin._chat_prefix_cache = {}
    plugin.sender = types.SimpleNamespace(send=None)
    plugin.loop = None
    plugin.gate = None
    plugin.sleep_manager = None
    return plugin, main_module
