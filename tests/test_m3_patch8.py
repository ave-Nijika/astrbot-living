"""M3 补丁 VIII 测试：分享角色化改写（提示词可配置/降级/截断/占位符）。"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from core.living_loop import LivingLoop
from core.share_rewriter import ShareRewriter, truncate_at_sentence

NOW = datetime(2026, 9, 16, 15, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {
        "daily_message_limit": 10, "message_min_interval_minutes": 30,
        "target_sessions": "", "quiet_hours": "",
        "share_rewrite_enabled": True,
        "share_rewrite_prompt": "把活动记录改写成聊天：{report}（心情：{mood}）",
        "share_max_length": 120,
    },
}

REPORT = "## 文章总结\n\n**主题**：《程序生成做游戏》，讲了不少技术细节……"


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeGate:
    def __init__(self, allow=True):
        self.allow = allow

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_send_message(self, now=None):
        return self.allow, "ok" if self.allow else "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def note_message_sent(self, now=None):
        pass

    async def close(self):
        pass


class FakeMood:
    def digest(self):
        return "心情不错；精力充沛"

    def recent_topics_list(self):
        return []

    def record_recent_topics(self, topics, window=6):
        pass


class FakeActivity:
    def __init__(self, name="surf", summary=REPORT):
        self.name = name
        self.summary = summary
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        from core.activities import ActivityOutcome

        return ActivityOutcome(name=self.name, summary=self.summary,
                               memory_content="m")


class FakeLLM:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def __call__(self, prompt, system_prompt=None):
        self.calls.append((prompt, system_prompt))
        if self.error:
            raise self.error
        return self.text


def make_loop(config=None, sender=None, llm=None, rewriter=True):
    sender = sender if sender is not None else FakeSender()
    config = dict(BASE_CONFIG if config is None else config)
    rewriter_obj = None
    if rewriter:
        from core.share_rewriter import ShareRewriter

        rewriter_obj = ShareRewriter(
            llm_call=llm if llm is not None else FakeLLM(),
            config_getter=lambda: config,
            persona_getter=None,
            life_extra_getter=None,
            mood=FakeMood(),
        )
    loop = LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=Any),
        config_getter=lambda: config,
        activities=[FakeActivity()],
        sender=sender,
        share_rewriter=rewriter_obj,
    )
    loop._test_config = config  # 测试在启动后修改配置用
    loop._get_memory = lambda: asyncio.sleep(0, result=type(
        "M", (), {
            "add": staticmethod(asyncio.sleep),
            "search": staticmethod(lambda q, k=5: []),
            "close": staticmethod(lambda: asyncio.sleep(0)),
        },
    )) if False else None
    # 简化：loop 的记忆写入走 _get_memory；这里给一个最小后端
    class M:
        async def add(self, content, importance=0.5, metadata=None, **kw):
            return 1
        async def search(self, query, k=5):
            return []
        async def close(self):
            pass
    loop._get_memory = lambda: asyncio.sleep(0, result=M())
    return loop, sender


# ---------------------------------------------------------------------------
# ShareRewriter 单元
# ---------------------------------------------------------------------------
def test_rewrite_produces_chat_text_with_placeholders():
    """占位符替换：{report}/{mood} 正确注入。"""
    captured = {}

    async def llm(prompt, system_prompt=None):
        captured["prompt"] = prompt
        captured["system"] = system_prompt
        return "今天看了篇讲程序生成的文章，挺有意思！"

    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: BASE_CONFIG,
        persona_getter=None, life_extra_getter=None, mood=FakeMood(),
    )
    text = asyncio.run(rewriter.rewrite(REPORT, mood_digest="心情不错"))
    assert text == "今天看了篇讲程序生成的文章，挺有意思！"
    assert REPORT.splitlines()[0] in captured["prompt"]  # 原始汇报进了 prompt
    assert "心情不错" in captured["prompt"]  # 心境摘要注入
    assert "程序生成" not in captured["system"]  # 人设不含活动内容


def test_rewrite_disabled_returns_none_without_llm_call():
    """开关关闭：不调 LLM，返回 None（调用方直发原文）。"""
    config = {**BASE_CONFIG, "output_gate": {
        **BASE_CONFIG["output_gate"], "share_rewrite_enabled": False}}
    llm = FakeLLM(text="不该被调用")
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: config, mood=FakeMood(),
    )
    assert asyncio.run(rewriter.rewrite(REPORT)) is None
    assert llm.calls == []


def test_rewrite_llm_error_returns_none():
    """LLM 抛错 → None（调用方降级原文）。"""
    llm = FakeLLM(error=RuntimeError("provider down"))
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: BASE_CONFIG, mood=FakeMood(),
    )
    assert asyncio.run(rewriter.rewrite(REPORT)) is None


def test_rewrite_empty_output_returns_none():
    llm = FakeLLM(text="   ")
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: BASE_CONFIG, mood=FakeMood(),
    )
    assert asyncio.run(rewriter.rewrite(REPORT)) is None


def test_rewrite_wrapping_quotes_stripped():
    llm = FakeLLM(text='"今天看了篇好文章"')
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: BASE_CONFIG, mood=FakeMood(),
    )
    assert asyncio.run(rewriter.rewrite(REPORT)) == "今天看了篇好文章"


def test_rewrite_identity_passthrough_when_llm_lazy():
    """LLM 偷懒原样返回：照发（返回原文，调用方不阻塞）。"""
    llm = FakeLLM(text=REPORT)
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: BASE_CONFIG, mood=FakeMood(),
    )
    assert asyncio.run(rewriter.rewrite(REPORT)) == REPORT


def test_truncate_at_sentence_boundary():
    """超长按句子边界截断：截到句末标点处。"""
    text = "第一句话讲了一件事。第二句话又讲了一件事！第三句还没说完就"
    truncated = truncate_at_sentence(text, 20)
    assert truncated.endswith("。")
    assert len(truncated) <= 20
    # 无句末标点 → 硬截
    hard = truncate_at_sentence("没有任何标点的超长文本" * 20, 15)
    assert len(hard) <= 15
    # 未超长原样返回
    assert truncate_at_sentence("短句。", 100) == "短句。"


def test_rewrite_long_output_truncated(tmp_path):
    """改写结果超 share_max_length → 截断。"""
    config = {**BASE_CONFIG, "output_gate": {
        **BASE_CONFIG["output_gate"], "share_max_length": 30}}
    llm = FakeLLM(text="第一句是完整的。第二句很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长。")
    rewriter = ShareRewriter(
        llm_call=llm, config_getter=lambda: config, mood=FakeMood(),
    )
    text = asyncio.run(rewriter.rewrite(REPORT))
    assert len(text) <= 30
    assert text.endswith("。")


# ---------------------------------------------------------------------------
# loop 集成：_maybe_share 改写链路
# ---------------------------------------------------------------------------
def test_loop_sends_rewritten_text():
    """端到端：闸门通过 → 改写 → 发送的是改写文本而非原始报告。"""
    sender = FakeSender()
    llm = FakeLLM(text="今天看了篇讲程序生成的文章，挺有意思！")
    loop, sender2 = make_loop(sender=sender, llm=llm)
    loop._test_config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"

    asyncio.run(loop._maybe_share(REPORT, NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123",
                            "今天看了篇讲程序生成的文章，挺有意思！")]


def test_loop_falls_back_to_original_on_rewrite_failure():
    """改写失败 → 降级发送原始汇报（有内容总比没有强）。"""
    sender = FakeSender()
    llm = FakeLLM(error=RuntimeError("down"))
    loop, sender2 = make_loop(sender=sender, llm=llm)
    loop._test_config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"

    asyncio.run(loop._maybe_share(REPORT, NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123", REPORT)]


def test_loop_rewrite_disabled_sends_original_without_llm():
    """开关关闭：不调改写 LLM，直发原文（向后兼容）。"""
    config = {**BASE_CONFIG, "output_gate": {
        **BASE_CONFIG["output_gate"], "share_rewrite_enabled": False,
        "target_sessions": "aiocqhttp:GroupMessage:123"}}
    sender = FakeSender()
    llm = FakeLLM(text="不该被调用")
    loop, sender2 = make_loop(config=config, sender=sender, llm=llm, rewriter=True)
    # rewriter 注入了但开关关—— rewrite 返回 None，loop 降级原文

    asyncio.run(loop._maybe_share(REPORT, NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123", REPORT)]
    assert llm.calls == []


def test_loop_no_rewriter_sends_original(tmp_path):
    """未注入 rewriter（旧装配）：行为与补丁 VIII 之前完全一致。"""
    sender = FakeSender()
    loop, sender2 = make_loop(sender=sender, rewriter=False)
    loop._test_config["output_gate"]["target_sessions"] = "aiocqhttp:GroupMessage:123"

    asyncio.run(loop._maybe_share(REPORT, NOW))
    assert sender.sent == [("aiocqhttp:GroupMessage:123", REPORT)]
