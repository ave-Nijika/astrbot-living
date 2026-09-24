"""M7 补丁 1 测试：分享改写器的主动语态修复。

三道修复各有一组测试：
- A 空产物不上桌：rewrite 空 report 不调 LLM；_maybe_share 短文本静默返回；
  run_activity_cycle 空 summary 不进分享（A2 双防线）。
- B prompt 主动语态：默认模板含角色框定/材料边界/主动语态指令，
  不再含"今天的活动记录"字样。
- C 产物回应式过滤：_is_responsive_style 纯函数正反例；rewrite 命中过滤
  返回 None；调用方降级发送原始 report。

全部走真实 ShareRewriter（llm_call 用可控替身），时钟显式传入（凛手改
记录：禁止依赖真实时钟）。
"""

import asyncio
from datetime import datetime

from core.living_loop import LivingLoop, MIN_SHARE_TEXT_LEN
from core.share_rewriter import (
    DEFAULT_PROMPT_TEMPLATE,
    MATERIAL_BEGIN,
    MATERIAL_END,
    ShareRewriter,
    _is_responsive_style,
)

NOW = datetime(2026, 9, 22, 15, 0, 0)  # 测试虚拟时钟

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
        # 默认空串 → _prompt_template() 走 DEFAULT_PROMPT_TEMPLATE
        "share_rewrite_prompt": "",
        "share_max_length": 120,
    },
}

REPORT = "## 文章总结\n\n**主题**：《程序生成做游戏》，讲了不少技术细节……"
RESPONSIVE_OUTPUT = "你说活动记录？你倒是发过来啊，我手上啥都没有。"
NORMAL_OUTPUT = "今天翻了翻合成器的波形文档，挺有意思"


class FakeSender:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send(self, session, text):
        if self.ok:
            self.sent.append((session, text))
            return True
        return False


class FakeGate:
    def __init__(self, allow=True):
        self.allow = allow
        self.message_sends = 0
        self.message_verdicts = []

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_send_message(self, now=None):
        self.message_verdicts.append(self.allow)
        return self.allow, "ok" if self.allow else "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def note_message_sent(self, now=None):
        self.message_sends += 1

    async def close(self):
        pass


class FakeMood:
    def digest(self):
        return "心情不错；精力充沛"

    def recent_topics_list(self):
        return []

    def record_recent_topics(self, topics, window=6):
        pass


class FakeLLM:
    """改写 LLM 替身：记录调用、返回预设文本或抛错。"""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def __call__(self, prompt, system_prompt=None):
        self.calls.append((prompt, system_prompt))
        if self.error:
            raise self.error
        return self.text


class ScriptedActivity:
    """可编排结果的活动替身：behavior 返回 (summary, memory_content)。"""

    def __init__(self, name, behavior=None):
        self.name = name
        self.behavior = behavior
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        from core.activities import ActivityOutcome

        summary, memory = self.behavior if self.behavior else ("", "")
        return ActivityOutcome(name=self.name, summary=summary,
                               memory_content=memory)


class ScriptedRng:
    """脚本化的 choice：按队列吐活动，绕开随机性（同 test_living_loop）。"""

    def __init__(self, picks):
        self.picks = list(picks)

    def choice(self, seq):
        target = self.picks.pop(0) if self.picks else seq[0]
        for item in seq:
            if item.name == target:
                return item
        return seq[0]


def make_loop(config=None, sender=None, llm=None, activities=None,
              gate=None, picks=None):
    """组装真实 LivingLoop + 真实 ShareRewriter（llm 可控）。"""
    sender = sender if sender is not None else FakeSender()
    config = BASE_CONFIG if config is None else config
    gate = gate or FakeGate(allow=True)
    rewriter = ShareRewriter(
        llm_call=llm if llm is not None else FakeLLM(),
        config_getter=lambda: config,
        persona_getter=None,
        life_extra_getter=None,
        mood=FakeMood(),
    )
    acts = activities if activities is not None else [
        ScriptedActivity("a1", (REPORT, "m"))
    ]
    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
        config_getter=lambda: config,
        activities=acts,
        sender=sender,
        share_rewriter=rewriter,
        rng=ScriptedRng(picks or ["a1"]),
    )
    return loop, sender


class FakeMemory:
    async def add(self, content, importance=0.5, metadata=None, **kw):
        return 1

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# A：空产物不上桌
# ---------------------------------------------------------------------------
def test_rewrite_empty_report_skips_llm():
    """D1：rewrite("") / rewrite("   ") → 不调 LLM，返回 None。"""
    llm = FakeLLM(text="不该被调用")
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    assert asyncio.run(rw.rewrite("")) is None
    assert asyncio.run(rw.rewrite("   ")) is None
    assert llm.calls == []


def test_maybe_share_empty_text_no_llm_no_send():
    """D1/验收1：_maybe_share 空壳 → 零改写调用、零发送、零闸门记账。"""
    llm = FakeLLM(text="不该被调用")
    sender = FakeSender()
    gate = FakeGate(allow=True)
    loop, _ = make_loop(sender=sender, llm=llm, gate=gate)
    loop._test_config = dict(BASE_CONFIG)
    loop._test_config["output_gate"]["target_sessions"] = (
        "aiocqhttp:GroupMessage:123"
    )

    for empty in ("", "   ", "abc"):  # 纯空串 / 纯空白 / 3 字占位
        asyncio.run(loop._maybe_share(empty, NOW))
    assert llm.calls == []
    assert sender.sent == []
    assert gate.message_sends == 0
    assert gate.message_verdicts == []  # 闸门掷点都没发生（A1 在最前）


def test_maybe_share_length_boundary():
    """A1 下限语义：len < 4 拦下，len == 4 放行（进闸门链路）。"""
    llm = FakeLLM(text=None)  # 改写返回 None → 降级原文
    sender = FakeSender()
    gate = FakeGate(allow=True)
    loop, _ = make_loop(sender=sender, llm=llm, gate=gate)
    loop._test_config = dict(BASE_CONFIG)
    loop._test_config["output_gate"]["target_sessions"] = (
        "aiocqhttp:GroupMessage:123"
    )

    asyncio.run(loop._maybe_share("abc", NOW))
    assert gate.message_verdicts == []  # 3 字：静默返回

    asyncio.run(loop._maybe_share("abcd", NOW))
    assert gate.message_verdicts == [True]  # 4 字：正常进入闸门
    assert MIN_SHARE_TEXT_LEN == 4


def test_cycle_blank_summary_never_enters_share():
    """A2 双防线：活动产出空/纯空白 summary → 不进分享（无 LLM 无发送）。"""
    llm = FakeLLM(text="不该被调用")
    sender = FakeSender()
    acts = [ScriptedActivity("a1", ("   ", "记忆"))]
    loop, _ = make_loop(sender=sender, llm=llm, activities=acts, picks=["a1"])
    loop._test_config = dict(BASE_CONFIG)
    loop._test_config["output_gate"]["target_sessions"] = (
        "aiocqhttp:GroupMessage:123"
    )

    asyncio.run(loop.run_activity_cycle(NOW))
    assert llm.calls == []
    assert sender.sent == []


# ---------------------------------------------------------------------------
# C：产物回应式过滤
# ---------------------------------------------------------------------------
def test_is_responsive_style_unit():
    """C1 纯函数：各特征正例命中、正常聊天文本不误伤。"""
    # 子串特征
    assert _is_responsive_style("你说活动记录？") is True
    assert _is_responsive_style("你倒是发过来啊") is True
    assert _is_responsive_style("扔过来我再帮你改") is True
    assert _is_responsive_style("发我一份呗") is True
    assert _is_responsive_style("给我发一下") is True
    # "诶"开头且含问号
    assert _is_responsive_style("诶……活动记录呢？") is True
    # 反问句"呢？"结尾
    assert _is_responsive_style("东西呢？") is True
    # 反例：正常分享（宁错杀的边界外）
    assert _is_responsive_style(NORMAL_OUTPUT) is False
    assert _is_responsive_style("我好像做了个梦：梦见会飞的鱼") is False
    assert _is_responsive_style("我睡过头了，晚了二十分钟") is False
    # 空输入安全
    assert _is_responsive_style("") is False
    assert _is_responsive_style("   ") is False


def test_rewrite_filters_responsive_output():
    """D2/验收2：改写产物回应式 → rewrite 返回 None（LLM 已被调 1 次）。"""
    llm = FakeLLM(text=RESPONSIVE_OUTPUT)
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    result = asyncio.run(rw.rewrite(REPORT, "心情不错"))
    assert result is None
    assert len(llm.calls) == 1  # 走到了改写，是产物过滤拦下的


def test_rewrite_passes_normal_output():
    """D2/验收3：正常产物原样返回。"""
    llm = FakeLLM(text=NORMAL_OUTPUT)
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    assert asyncio.run(rw.rewrite(REPORT, "心情不错")) == NORMAL_OUTPUT


def test_loop_skips_share_when_output_filtered():
    """M9-补丁4（主人 2026-09-24 拍板）：产物被过滤 → 整条分享静默跳过，
    不再降级发送原文——原文是工作汇报体，发进聊天框就是 OOC。"""
    llm = FakeLLM(text=RESPONSIVE_OUTPUT)
    sender = FakeSender()
    loop, _ = make_loop(sender=sender, llm=llm)
    loop._test_config = dict(BASE_CONFIG)
    loop._test_config["output_gate"]["target_sessions"] = (
        "aiocqhttp:GroupMessage:123"
    )

    asyncio.run(loop._maybe_share(REPORT, NOW))
    assert sender.sent == []  # 过滤触发 → 不发送（活动记忆里仍留有完整内容）
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# B：prompt 主动语态框架
# ---------------------------------------------------------------------------
def test_default_template_active_voice_assertions():
    """D3/验收4：模板含角色框定/材料边界/主动语态，不含旧误判诱因。"""
    t = DEFAULT_PROMPT_TEMPLATE
    # 材料边界 + 角色框定
    assert "主人看不到" in t
    assert "主动" in t
    assert MATERIAL_BEGIN in t and MATERIAL_END in t
    # 占位符在位（{report} 位于两分隔行之间，{mood} 在口吻段）
    assert t.index(MATERIAL_BEGIN) < t.index("{report}") < t.index(MATERIAL_END)
    assert "{mood}" in t
    # B3：旧误判诱因字样必须消失
    assert "今天的活动记录" not in t
    # 主动语态指令逐字在位
    assert ("这是你主动想跟他说的话，不是回答他的问题" in t)
    assert "也不要问主人要任何东西" in t
    # 口吻/长度要求段保留
    assert "像朋友间随口聊天" in t
    assert "只输出要说的话本身" in t


def test_default_template_used_when_custom_blank():
    """B2 前提：share_rewrite_prompt 为空 → 默认模板生效（自定义不受影响）。"""
    llm = FakeLLM(text=NORMAL_OUTPUT)
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    asyncio.run(rw.rewrite(REPORT, "心情不错"))
    prompt = llm.calls[0][0]
    assert MATERIAL_BEGIN in prompt
    assert REPORT in prompt  # {report} 已被真实材料替换


def test_custom_template_still_respected():
    """B2：用户自定义模板照常生效，不被本次重构破坏。"""
    config = {**BASE_CONFIG, "output_gate": {
        **BASE_CONFIG["output_gate"],
        "share_rewrite_prompt": "自定义改写：{report}（心情：{mood}）",
    }}
    llm = FakeLLM(text=NORMAL_OUTPUT)
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: config)
    assert asyncio.run(rw.rewrite(REPORT, "平静")) == NORMAL_OUTPUT
    prompt = llm.calls[0][0]
    assert prompt.startswith("自定义改写：")
    assert REPORT in prompt and "平静" in prompt
