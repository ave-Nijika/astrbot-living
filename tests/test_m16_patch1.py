"""M16-补丁1：话语回读完整性——她说过的每句话都要能被自己读到。

对应任务书 T1-T9（T8 全量基线由本地全量回归保证，不在本文件）。
测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import copy
import hashlib
import logging
import random
import types
from datetime import datetime
from pathlib import Path

import pytest

from core.activities import ActivityContext, SurfActivity
from core.agent_loop import AgentRunResult
from core.living_loop import LivingLoop
from core.share_rewriter import ShareRewriter

MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份
MASTER2 = "aiocqhttp:FriendMessage:10002"
NIGHT = datetime(2026, 9, 22, 5, 50, 0)
MORNING = datetime(2026, 9, 22, 9, 30, 0)
REPORT = "## 文章总结\n\n**主题**：深海热泉生态，讲了化能合成……"
REWRITE_TEXT = "今天试着写了个按键闪避的小游戏，手感带劲"

SHARE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_context_write": True},
    "output_gate": {
        "daily_message_limit": 10,
        "target_sessions": MASTER,
        "share_rewrite_enabled": True,
        "share_rewrite_prompt": "",
        "share_max_length": 500,
    },
}

FAREWELL_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_context_write": True},
    "sleep": {
        "sleep_farewell_message": "我先去睡了，晚安。",
        "farewell_mode": "probability",
        "farewell_probability": 1.0,
        "wake_ack_message": "嗯，我醒了。",
    },
    "output_gate": {"target_sessions": MASTER},
}


# ---------------------------------------------------------------------------
# 复用件
# ---------------------------------------------------------------------------
class FakeSender:
    def __init__(self, ok=True, raises=False):
        self.ok = ok
        self.raises = raises
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        if self.raises:
            raise RuntimeError("platform down")
        return self.ok


class FakeGate:
    def __init__(self):
        self.message_sends = 0

    async def should_send_message(self, now=None):
        return True, "ok"

    async def note_message_sent(self, now=None):
        self.message_sends += 1


class FakeMemory:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.added = []

    async def search(self, query, k=5, **kwargs):
        return self.rows[:k]

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)


class FakeMood:
    def digest(self):
        return "心情不错；精力充沛"


class FakeRewriteLLM:
    def __init__(self, text):
        self._text = text
        self.calls = []

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        return self._text


class FakeConvMgr:
    """ConversationManager 替身：只记 add_message_pair 写入。"""

    def __init__(self):
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return "conv-1"

    async def add_message_pair(self, cid, user, asst):
        self.pairs.append((cid, user, asst))


class FakeLM:
    """livingmemory 会话管理器替身：记 add_message。"""

    def __init__(self):
        self.added = []

    async def add_message(self, **kwargs):
        self.added.append(kwargs)
        return len(self.added)


def _spy_writes(loop):
    """包一层真实 _write_speech_to_stores：记录实参、仍走真实双写。"""
    calls = []
    real = loop._write_speech_to_stores

    async def spy(text, dedup_key, user_msg, label="活动经历"):
        calls.append({"text": text, "dedup_key": dedup_key,
                      "user_msg": user_msg, "label": label})
        await real(text, dedup_key, user_msg, label)

    loop._write_speech_to_stores = spy
    return calls


def make_share_loop(*, rewrite_text=REWRITE_TEXT, target=MASTER, send_ok=True,
                    send_raises=False, lm=None, config=None):
    cfg = copy.deepcopy(SHARE_CONFIG)
    cfg["output_gate"]["target_sessions"] = target
    if config:
        for group, kv in config.items():
            cfg.setdefault(group, {}).update(kv)
    sender = FakeSender(ok=send_ok, raises=send_raises)
    gate = FakeGate()
    mgr = FakeConvMgr()
    rewriter = ShareRewriter(
        llm_call=FakeRewriteLLM(rewrite_text),
        config_getter=lambda: cfg, persona_getter=None,
        life_extra_getter=None, mood=FakeMood(),
    )
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
        config_getter=lambda: cfg, activities=[], sender=sender,
        share_rewriter=rewriter, conversation_manager=mgr,
        lm_conversation_manager_getter=(lambda: lm) if lm is not None else None,
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop, sender, gate, mgr


def make_speech_loop(*, config=None, sender=None, sessions=MASTER):
    """晚安/唤醒路径的最小装配（这几条不经闸门）。"""
    cfg = copy.deepcopy(FAREWELL_CONFIG)
    if config:
        for group, kv in config.items():
            cfg.setdefault(group, {}).update(kv)
    sender = sender or FakeSender()
    loop = LivingLoop(
        gate=types.SimpleNamespace(),
        memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
        config_getter=lambda: cfg, activities=[], sender=sender,
        sleep_manager=types.SimpleNamespace(
            last_active_session=sessions, last_wake_session=sessions),
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop, sender


# ---------------------------------------------------------------------------
# T1：活动分享——发送成功后落 text_to_send（改写后），dedup 内容哈希
# ---------------------------------------------------------------------------
def test_share_writes_speech_after_send():
    loop, sender, gate, mgr = make_share_loop()
    lm = FakeLM()
    loop._lm_conv_mgr_getter = lambda: lm
    calls = _spy_writes(loop)

    asyncio.run(loop._maybe_share(REPORT, None))

    # 发送的是改写后文本；配额照记
    assert sender.sent == [(MASTER, REWRITE_TEXT)]
    assert gate.message_sends == 1
    # 落库实参：内容 == text_to_send（不是 REPORT 原文/summary/narration）
    expected_dedup = "#share:" + hashlib.md5(
        REWRITE_TEXT.encode("utf-8")
    ).hexdigest()[:16]
    assert calls == [{
        "text": REWRITE_TEXT, "dedup_key": expected_dedup,
        "user_msg": "(分享)", "label": "活动分享",
    }]
    # 落点 A：对话上下文追加一对消息（assistant 侧是改写后文本）
    assert mgr.pairs == [(
        "conv-1",
        {"role": "user", "content": "(分享)"},
        {"role": "assistant", "content": REWRITE_TEXT},
    )]
    # 落点 B：livingmemory 会话（主人真实 umo、assistant/bot 形态）
    assert len(lm.added) == 1
    assert lm.added[0]["session_id"] == MASTER
    assert lm.added[0]["role"] == "assistant"
    assert lm.added[0]["content"] == REWRITE_TEXT
    assert lm.added[0]["is_bot_message"] is True


def test_share_multi_session_writes_once():
    """T2：多目标会话都发送，但落库只一次（不因循环落多次）。"""
    target = f"{MASTER}\n{MASTER2}"
    loop, sender, gate, mgr = make_share_loop(target=target)
    lm = FakeLM()
    loop._lm_conv_mgr_getter = lambda: lm
    calls = _spy_writes(loop)

    asyncio.run(loop._maybe_share(REPORT, None))

    assert {s for s, _ in sender.sent} == {MASTER, MASTER2}
    assert gate.message_sends == 2
    assert len(calls) == 1
    assert len(mgr.pairs) == 1
    assert len(lm.added) == 1


def test_share_repeat_trigger_writes_once():
    """红线 2：同一句话重复触发只落一次（内容哈希幂等）；发送行为零变化。"""
    loop, sender, gate, mgr = make_share_loop()
    calls = _spy_writes(loop)

    asyncio.run(loop._maybe_share(REPORT, None))
    asyncio.run(loop._maybe_share(REPORT, None))

    assert len(sender.sent) == 2  # 发送照旧两次
    assert gate.message_sends == 2
    assert len(calls) == 2  # 落库被调两次，但真实写入去重
    assert len(mgr.pairs) == 1
    # M16-补丁2 A2 同步（set→dict），并把恒真的成员断言收紧为
    # "恰好这一个键"——该循环只发生一次话语落库，键值算错或多写都现形
    assert list(loop._experience_written) == [
        "#share:" + hashlib.md5(REWRITE_TEXT.encode("utf-8")).hexdigest()[:16]
    ]


def test_share_send_failure_no_write_no_quota():
    """T3：发送失败（未送达/异常）→ 不落库、不记配额（既有语义保持）。"""
    for kwargs in ({"send_ok": False}, {"send_raises": True}):
        loop, sender, gate, mgr = make_share_loop(**kwargs)
        calls = _spy_writes(loop)
        asyncio.run(loop._maybe_share(REPORT, None))
        assert gate.message_sends == 0
        assert calls == []
        assert mgr.pairs == []


def test_share_write_failure_warning_not_block(caplog):
    """T4：落库抛异常 → 发送与记账已完成、只 WARNING 不阻断。"""
    loop, sender, gate, mgr = make_share_loop()
    calls = _spy_writes(loop)

    async def boom(text, dedup_key, user_msg, label="活动经历"):
        calls.append({"text": text, "dedup_key": dedup_key,
                      "user_msg": user_msg, "label": label})
        raise RuntimeError("store down")

    loop._write_speech_to_stores = boom
    with caplog.at_level(logging.WARNING):
        asyncio.run(loop._maybe_share(REPORT, None))

    assert sender.sent == [(MASTER, REWRITE_TEXT)]  # 发送已成功
    assert gate.message_sends == 1  # 配额照记
    assert len(calls) == 1
    assert "分享话语落库失败" in caplog.text


# ---------------------------------------------------------------------------
# T5：梦话/睡过头交代——经 _maybe_share 单点覆盖，与既有 memory.add 并存
# ---------------------------------------------------------------------------
def test_dream_share_writes_speech_alongside_memory():
    loop, sender, gate, mgr = make_share_loop(
        rewrite_text="我梦见一片会发光的海，还有昨天那个游戏")
    mem = FakeMemory(rows=[{"content": "白天写了会儿游戏"}])
    loop._get_memory = lambda: asyncio.sleep(0, result=mem)
    loop._rng = random.Random(1)  # random()≈0.134 < 0.3 → 做梦
    calls = _spy_writes(loop)

    async def dream_llm(prompt, system=None):
        return "我梦见一片会发光的海"

    loop._dream_llm_call = dream_llm
    asyncio.run(loop._maybe_dream(MORNING))

    # 既有认知记忆路径原样：梦进 memory.add
    assert any("我做了个梦：我梦见一片会发光的海" in c for c in mem.added)
    # M16：发出的梦话（改写后）也落双存储
    assert sender.sent == [(MASTER, "我梦见一片会发光的海，还有昨天那个游戏")]
    assert calls == [{
        "text": "我梦见一片会发光的海，还有昨天那个游戏",
        "dedup_key": "#share:" + hashlib.md5(
            "我梦见一片会发光的海，还有昨天那个游戏".encode("utf-8")
        ).hexdigest()[:16],
        "user_msg": "(分享)", "label": "活动分享",
    }]


def test_oversleep_note_writes_speech_alongside_memory():
    loop, sender, gate, mgr = make_share_loop(
        rewrite_text="呀，一觉睡到九点半，对不起嘛")
    mem = FakeMemory()
    loop._get_memory = lambda: asyncio.sleep(0, result=mem)
    loop._rng = lambda: 0.0  # roll < 0.5 → 主动交代
    calls = _spy_writes(loop)

    async def consume(wake_time):
        return {"target_time": "2026-09-22T08:00:00"}

    loop._schedule = types.SimpleNamespace(consume_due_wake=consume)
    result = asyncio.run(loop._handle_oversleep_commitment(MORNING))

    assert result is True
    # 认知记忆（原样）与话语落库（新增）并存、互不干扰
    assert any("我睡过头了" in c for c in mem.added)
    assert sender.sent == [(MASTER, "呀，一觉睡到九点半，对不起嘛")]
    assert len(calls) == 1
    assert calls[0]["text"] == "呀，一觉睡到九点半，对不起嘛"
    assert calls[0]["dedup_key"].startswith("#share:")
    assert calls[0]["user_msg"] == "(分享)"
    assert calls[0]["label"] == "活动分享"


# ---------------------------------------------------------------------------
# T6：晚安概率档 / 唤醒确认——固定文案落库，dedup 含时间戳
# ---------------------------------------------------------------------------
def test_farewell_probability_writes_speech_with_time_key():
    loop, sender = make_speech_loop()
    loop._rng = lambda: 0.0  # 命中
    calls = _spy_writes(loop)

    asyncio.run(loop._send_sleep_farewell(NIGHT))

    assert sender.sent == [(MASTER, "我先去睡了，晚安。")]
    assert calls == [{
        "text": "我先去睡了，晚安。",
        "dedup_key": f"#farewell:{int(NIGHT.timestamp())}",
        "user_msg": "(晚安)", "label": "晚安",
    }]


def test_farewell_probability_miss_or_fail_no_write():
    # 未命中（掷点 0.9 ≥ 0.5）不发送也不落库
    loop, sender = make_speech_loop(
        config={"sleep": {"farewell_probability": 0.5}})
    loop._rng = lambda: 0.9
    calls = _spy_writes(loop)
    asyncio.run(loop._send_sleep_farewell(NIGHT))
    assert sender.sent == [] and calls == []

    # 命中但发送失败 → 不落库
    sender2 = FakeSender(ok=False)
    loop2, _ = make_speech_loop(sender=sender2)
    loop2._rng = lambda: 0.0
    calls2 = _spy_writes(loop2)
    asyncio.run(loop2._send_sleep_farewell(NIGHT))
    assert sender2.sent and sender2.sent[0][0] == MASTER
    assert calls2 == []


def test_wake_ack_writes_speech_with_time_key():
    loop, sender = make_speech_loop()
    calls = _spy_writes(loop)

    asyncio.run(loop._send_wake_ack(MORNING))

    assert sender.sent == [(MASTER, "嗯，我醒了。")]
    assert calls == [{
        "text": "嗯，我醒了。",
        "dedup_key": f"#wake:{int(MORNING.timestamp())}",
        "user_msg": "(唤醒)", "label": "唤醒确认",
    }]
    # 调用点同步签名（心跳把虚拟时钟传进来）
    src = (Path(__file__).resolve().parents[1] / "core" / "living_loop.py"
           ).read_text(encoding="utf-8")
    assert "await self._send_wake_ack(now)" in src


def test_wake_ack_fail_no_write():
    sender = FakeSender(ok=False)
    loop, _ = make_speech_loop(sender=sender)
    calls = _spy_writes(loop)
    asyncio.run(loop._send_wake_ack(MORNING))
    assert sender.sent and calls == []


# ---------------------------------------------------------------------------
# T7：回归——晚安 LLM 档与 initiative 的双写行为未变
# ---------------------------------------------------------------------------
def test_llm_farewell_double_write_form_unchanged():
    loop, sender = make_speech_loop(
        config={"sleep": {"farewell_mode": "llm"}})
    loop._rng = lambda: 0.0

    async def fake_llm(prompt, system=None):
        return "晚安，今天真的很开心。"

    loop._dream_llm_call = fake_llm
    calls = _spy_writes(loop)
    asyncio.run(loop._send_sleep_farewell(NIGHT))

    assert sender.sent == [(MASTER, "晚安，今天真的很开心。")]
    # 调用形态与 M15-补丁1 逐字一致（占位/标签/键格式都不变）
    assert calls == [{
        "text": "晚安，今天真的很开心。",
        "dedup_key": f"#farewell:{NIGHT.strftime('%Y%m%d_%H%M%S')}",
        "user_msg": "(晚安道别)", "label": "晚安道别",
    }]


def test_initiative_double_write_form_unchanged():
    """initiative 的双写调用形态锁定：占位/标签/键前缀都在原位
    （运行时行为另由 test_m14_patch1 的端到端测试锁定）。"""
    root = Path(__file__).resolve().parents[1]
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert '"(主动搭话)", label="主动搭话"' in main_src
    init_src = (root / "core" / "initiative.py").read_text(encoding="utf-8")
    assert "await self._speech_writer(line, dedup_key)" in init_src


# ---------------------------------------------------------------------------
# T9：C 组——超预算中断经历更饱满、纯中断串退化、正常路径不变
# ---------------------------------------------------------------------------
def _agent_ctx(agent):
    return ActivityContext(
        searcher=types.SimpleNamespace(), fetcher=types.SimpleNamespace(),
        sandbox=types.SimpleNamespace(), memory=types.SimpleNamespace(),
        gate=None, event=None, rng=random.Random(7), agent=agent,
    )


def test_budget_partial_limit_raised_to_200():
    text = "键位闪避手感" * 40  # 240 字

    async def agent(intent):
        return AgentRunResult(ok=False, text=text, budget_exceeded=True,
                              tokens_used=20000, max_steps=8)

    outcome = asyncio.run(SurfActivity().run(_agent_ctx(agent)))
    assert outcome.agent_mode is True
    assert outcome.summary == text[:200]
    assert text[:200] in outcome.memory_content
    assert text[:201] not in outcome.memory_content
    assert outcome.importance == 0.3


@pytest.mark.parametrize("raw", ["Output stopped.", "output stopped",
                                 "  Output stopped.  ", ""])
def test_budget_pure_interruption_degrades_to_full_sentence(raw):
    async def agent(intent):
        return AgentRunResult(ok=False, text=raw, budget_exceeded=True,
                              tokens_used=20000, max_steps=8)

    outcome = asyncio.run(SurfActivity().run(_agent_ctx(agent)))
    # 退化为不含 partial 的完整句式；占位语不进 summary/记忆
    assert outcome.memory_content.endswith("玩到一半被 token 预算叫停了。")
    assert "Output stopped" not in outcome.memory_content
    assert outcome.summary == "玩到一半被 token 预算叫停"


def test_budget_normal_completion_path_unchanged():
    text = "写了个贪吃蛇" * 20  # 120 字，正常完成路径仍截 80

    async def agent(intent):
        return AgentRunResult(ok=True, text=text, tokens_used=1500,
                              steps_used=2, max_steps=8)

    outcome = asyncio.run(SurfActivity().run(_agent_ctx(agent)))
    assert text[:80] in outcome.memory_content
    assert text[:81] not in outcome.memory_content
    assert "玩了很久" not in outcome.memory_content
    assert outcome.summary == text[:80]
