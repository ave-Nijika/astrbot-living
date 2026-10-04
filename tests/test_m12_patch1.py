"""M12-补丁1 测试：分享链路的完整聊天上下文（同源于真实聊天）。

主链路：_maybe_share → _load_chat_contexts（ConversationManager 的
history 末尾 N 条，只取 role/content）→ rewrite(..., contexts=…) →
决策 LLM 包装透传 context.llm_generate(contexts=…)。contexts=None 时
模板与调用形态均为 M7 版逐字（对照基线）。
"""

import asyncio
import copy
import json
import types

from core.living_loop import LivingLoop
from core.share_rewriter import (
    CONTEXT_FRAME_TEXT,
    MATERIAL_BEGIN,
    ShareRewriter,
)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "share_context_messages": 12},
    "output_gate": {
        "daily_message_limit": 10,
        "target_sessions": "aiocqhttp:FriendMessage:10001",
        "share_rewrite_enabled": True,
        "share_rewrite_prompt": "",
        "share_max_length": 500,
    },
}

REPORT = "## 文章总结\n\n**主题**：深海热泉生态，讲了化能合成……"
UMO = "aiocqhttp:FriendMessage:10001"

# M7 版（无 {context_block}）模板渲染快照：用于"现状逐字"对照
M7_TEMPLATE = (
    "你刚完成了自己的活动，正准备随手跟主人聊一句。\n"
    "\n"
    "下面是你的活动材料，仅供你自己参考，主人看不到这些：\n"
    "--- 活动材料开始 ---\n"
    "{report}\n"
    "--- 结束 ---\n"
    "\n"
    "这是你主动想跟他说的话，不是回答他的问题——绝不能出现"
    "‘你说’、‘发过来’、‘给我’这类回应式措辞，也不要问主人要任何东西。\n"
    "\n"
    "要求：\n"
    "- 像朋友间随口聊天，不是汇报；不要标题、不要列表、不要 Markdown 格式\n"
    "- 用你自己的口吻，可以带一点当天心情（你现在的状态：{mood}）\n"
    "- 只输出要说的话本身，不要任何前缀和引号"
)


class FakeLLM:
    """改写 LLM 替身：记录 prompt 与 contexts kwargs。"""

    def __init__(self, text="今天翻了翻深海热泉的资料，想起你上次说的化能合成"):
        self.calls = []
        self._text = text

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt,
                           "kwargs": kwargs})
        return self._text


class FakeConvMgr:
    """ConversationManager 替身：记录调用、返回预设 history。"""

    def __init__(self, history=None, cid="conv-1", conv=None, cid_result=True):
        self._history = history
        self._cid = cid if cid_result else None
        self._conv = conv
        self.calls = []

    async def get_curr_conversation_id(self, umo):
        self.calls.append(("cid", umo))
        return self._cid

    async def get_conversation(self, umo, cid, **kwargs):
        self.calls.append(("conv", umo, cid))
        if self._conv is not None:
            return self._conv
        if self._history is None:
            return None
        return types.SimpleNamespace(
            history=json.dumps(self._history, ensure_ascii=False)
        )


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeGate:
    def __init__(self):
        self.message_sends = 0

    async def should_send_message(self, now=None):
        return True, "ok"

    async def note_message_sent(self, now=None):
        self.message_sends += 1


class FakeMood:
    def digest(self):
        return "心情不错；精力充沛"


def _history(n=20):
    """真实聊天形态的历史：user/assistant 交替的 dict 列表。"""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append({"role": "user", "content": f"消息{i}"})
        else:
            out.append({"role": "assistant", "content": f"回复{i}"})
    return out


def make_loop(config=None, llm=None, mgr=None, target=UMO):
    # 深拷贝：内层 dict 若共享会跨测试污染模块级 BASE_CONFIG
    cfg = copy.deepcopy(BASE_CONFIG)
    if config:
        for group, kv in config.items():
            cfg.setdefault(group, {}).update(kv)
    if target is not None:
        cfg["output_gate"]["target_sessions"] = target
    sender = FakeSender()
    rewriter = ShareRewriter(
        llm_call=llm if llm is not None else FakeLLM(),
        config_getter=lambda: cfg,
        persona_getter=None,
        life_extra_getter=None,
        mood=FakeMood(),
    )
    loop = LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=object()),
        config_getter=lambda: cfg,
        activities=[],
        sender=sender,
        share_rewriter=rewriter,
        conversation_manager=mgr,
    )
    return loop, sender


# ---------------------------------------------------------------------------
# rewriter 级：模板与 contexts 透传（D1/D6）
# ---------------------------------------------------------------------------
def test_rewrite_with_contexts_frame_and_passthrough():
    """D1/验收2：带 contexts → prompt 含逐字框定文案 + kwargs 透传原列表。"""
    llm = FakeLLM()
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    ctx = [{"role": "user", "content": "在干嘛"},
           {"role": "assistant", "content": "在琢磨下一步看什么"}]
    result = asyncio.run(rw.rewrite(REPORT, "心情不错", contexts=ctx))
    assert result  # 正常产出
    call = llm.calls[0]
    # 框定文案逐字在 prompt 中
    assert CONTEXT_FRAME_TEXT in call["prompt"]
    assert call["prompt"].index(CONTEXT_FRAME_TEXT) < call["prompt"].index(MATERIAL_BEGIN)
    # 材料段与上下文段边界清晰（B5）：材料仍在分隔行里
    assert MATERIAL_BEGIN in call["prompt"] and REPORT in call["prompt"]
    # contexts 原样透传（顺序与 role 不动）
    assert call["kwargs"]["contexts"] == ctx


def test_rewrite_without_contexts_verbatim_baseline():
    """D1/验收1：contexts=None → prompt 与 M7 版渲染逐字一致、零额外 kwargs。"""
    llm = FakeLLM()
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    asyncio.run(rw.rewrite(REPORT, "心情不错"))
    call = llm.calls[0]
    expected = (
        M7_TEMPLATE.replace("{report}", REPORT)
        .replace("{mood}", "心情不错")
    )
    assert call["prompt"] == expected
    assert call["kwargs"] == {}  # 调用形态与 M12 之前一致


def test_rewrite_custom_template_without_placeholder_unchanged():
    """用户自定义模板不含 {context_block} 占位符时行为不变（M7 兼容）。"""
    llm = FakeLLM()
    cfg = {"output_gate": {"share_rewrite_enabled": True,
                           "share_rewrite_prompt": "材料：{report} 心情：{mood}",
                           "share_max_length": 500}}
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: cfg)
    asyncio.run(rw.rewrite(REPORT, "心情不错", contexts=_history(3)))
    assert llm.calls[0]["prompt"] == f"材料：{REPORT} 心情：心情不错"


def test_responsive_output_still_filtered_with_contexts():
    """D6/验收7：带 contexts 且产物回应式 → None（M7 过滤不回退）。"""
    llm = FakeLLM(text="你说我在忙什么？我记得的，你上次让我看的那个")
    rw = ShareRewriter(llm_call=llm, config_getter=lambda: BASE_CONFIG)
    result = asyncio.run(rw.rewrite(REPORT, "心情不错", contexts=_history(4)))
    assert result is None


# ---------------------------------------------------------------------------
# loop 级：上下文获取（D2-D5）
# ---------------------------------------------------------------------------
def test_tail_n_messages_order_and_roles_intact():
    """D2/验收3：20 条 history、N=5 → 最后 5 条、顺序与 role 不变。"""
    mgr = FakeConvMgr(history=_history(20))
    llm = FakeLLM()
    loop, _ = make_loop(config={"decision": {"share_context_messages": 5}},
                        llm=llm, mgr=mgr)
    asyncio.run(loop._maybe_share(REPORT, None))
    ctx = llm.calls[0]["kwargs"]["contexts"]
    assert ctx == _history(20)[-5:]
    # 末尾 5 条 = 索引 15-19（user/assistant 起始交替），顺序原样
    assert [m["role"] for m in ctx] == [
        "assistant", "user", "assistant", "user", "assistant",
    ]


def test_only_role_content_fields_kept():
    """红线 7：history 条目的其余字段（元数据等）不带入注入列表。"""
    polluted = [
        {"role": "user", "content": "在干嘛", "tool_call_id": "x",
         "metadata": {"internal": "stuff"}},
    ] * 3
    mgr = FakeConvMgr(history=polluted)
    llm = FakeLLM()
    loop, _ = make_loop(config={"decision": {"share_context_messages": 5}},
                        llm=llm, mgr=mgr)
    asyncio.run(loop._maybe_share(REPORT, None))
    ctx = llm.calls[0]["kwargs"]["contexts"]
    assert ctx == [{"role": "user", "content": "在干嘛"}] * 3


def test_missing_conversation_id_degrades_quietly():
    """D3/验收4：cid 为 None → 无上下文、分享照发、零异常。"""
    mgr = FakeConvMgr(history=_history(6), cid_result=False)
    llm = FakeLLM()
    loop, sender = make_loop(llm=llm, mgr=mgr)
    asyncio.run(loop._maybe_share(REPORT, None))
    assert llm.calls[0]["kwargs"] == {}
    assert CONTEXT_FRAME_TEXT not in llm.calls[0]["prompt"]
    assert sender.sent and sender.sent[0][0] == UMO


def test_bad_history_json_degrades_quietly():
    """D4/验收5：history 非法 JSON → 静默降级无上下文，分享照常。"""
    conv = types.SimpleNamespace(history="not-json{{{{{")
    mgr = FakeConvMgr(conv=conv)
    llm = FakeLLM()
    loop, sender = make_loop(llm=llm, mgr=mgr)
    asyncio.run(loop._maybe_share(REPORT, None))
    assert llm.calls[0]["kwargs"] == {}
    assert sender.sent


def test_switch_off_zero_messages_no_mgr_calls():
    """D5/验收6：share_context_messages=0 → mgr 零调用、prompt 同现状。

    M16-补丁1 适配：本测试只锁"改写上下文读路径"零调用——落库写路径
    （A1 发送成功后 _write_speech_to_stores 也会经 mgr 写上下文）在这里
    显式关掉，由 test_m16_patch1.py 专门覆盖。"""
    mgr = FakeConvMgr(history=_history(6))
    llm = FakeLLM()
    loop, _ = make_loop(
        config={"decision": {"share_context_messages": 0,
                             "activity_context_write": False}},
        llm=llm, mgr=mgr)
    asyncio.run(loop._maybe_share(REPORT, None))
    assert mgr.calls == []
    assert CONTEXT_FRAME_TEXT not in llm.calls[0]["prompt"]


def test_first_session_used_when_multiple():
    """A1：多会话时以 target_sessions 第一行为准。"""
    mgr = FakeConvMgr(history=_history(4))
    llm = FakeLLM()
    loop, sender = make_loop(
        llm=llm, mgr=mgr,
        target="aiocqhttp:FriendMessage:111\naiocqhttp:FriendMessage:222",
    )
    asyncio.run(loop._maybe_share(REPORT, None))
    assert mgr.calls[0] == ("cid", "aiocqhttp:FriendMessage:111")
    # 两个会话照常都发送
    assert {s for s, _ in sender.sent} == {
        "aiocqhttp:FriendMessage:111", "aiocqhttp:FriendMessage:222"
    }


def test_config_read_failure_falls_back_to_default():
    """配置读取失败回落默认 12（任务书 2.2：配置坏了不中断分享）。"""
    history = _history(20)

    class _BrokenConfig:
        def __call__(self, *a, **kw):
            raise RuntimeError("config boom")

    async def flow():
        mgr = FakeConvMgr(history=history)
        llm = FakeLLM()
        sender = FakeSender()
        rewriter = ShareRewriter(
            llm_call=llm, config_getter=_BrokenConfig(),
            persona_getter=None, life_extra_getter=None, mood=None,
        )
        loop = LivingLoop(
            gate=FakeGate(),
            memory_getter=lambda: asyncio.sleep(0, result=object()),
            config_getter=_BrokenConfig(),
            activities=[],
            sender=sender,
            share_rewriter=rewriter,
            conversation_manager=mgr,
        )
        # 直接走 _load_chat_contexts（config_getter 抛异常时 _maybe_share
        # 的其他环节也会失败，这里只验证读取层回落 12）
        ctx = await loop._load_chat_contexts([UMO])
        return ctx

    ctx = asyncio.run(flow())
    assert len(ctx) == 12
    assert ctx[-1] == {"role": "assistant", "content": "回复19"}


def test_conversation_manager_not_injected_is_noop():
    """未注入 mgr（None，旧装配形态）→ 直接按无上下文处理。"""
    llm = FakeLLM()
    loop, sender = make_loop(llm=llm, mgr=None)
    asyncio.run(loop._maybe_share(REPORT, None))
    assert llm.calls[0]["kwargs"] == {}
    assert sender.sent
