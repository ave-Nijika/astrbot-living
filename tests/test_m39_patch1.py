"""M39-补丁1：看留言（peek）/ 翻记忆（reminisce）改造成"真的有心理活动"。

对照任务书 §三验收 1-9：
  1 peek 产生心理活动（非模板句、进记忆落点）——活动级 + Loop 级
  2 她决定不说 → summary None、零发送（Loop 级真实周期）
  3 她决定要说 → summary 为该句、进闸门链（不是直接发送）
  4 闸门仍生效 → 拒绝时没发出去、活动照常完成
  5 降级不空转 → 通道缺失/超时回落脚本模板
  6 解析失败不误发 → 乱码/半截 JSON 不发消息
  7 reminisce 长程材料（旧记忆 + M35 资料区）与 peek 短程材料（最近对话）
  8 短/长程两套提示词（配置键不同、默认不同、渲染可覆盖）
  9 零回归 —— 全量测试（命令行，不在本文件）

轻调用协议：ctx.reflect（async (user_prompt) -> str | None）替身捕获
prompt 并按脚本回放；Loop 级测试把替身塞进 loop._reflect_llm_call，
验证意愿经 _maybe_share → should_send_message 的既有闸门链。
"""

import asyncio
import json
import random
from datetime import date, datetime

import pytest

from core.activities import (
    ActivityContext,
    MemoryBrowsingActivity,
    PeekFeedbackActivity,
)
from core.muse import (
    DEFAULT_PROMPT_PEEK_MUSE,
    DEFAULT_PROMPT_REMINISCE_MUSE,
    MEMORY_CANDIDATES,
    MUSE_SYSTEM_TAIL,
    mood_line,
    parse_day_prefix,
    parse_muse_output,
    pick_old_memory,
    render_memory_material,
)
from core.living_loop import LivingLoop
from test_m13_patch1 import BASE_CONFIG, FakeGate, FakeSender, make_loop

NOW = datetime(2026, 10, 11, 15, 0, 0)
SCHEMA = json.load(open("D:/sandbox/astrbot-living/_conf_schema.json", encoding="utf-8"))

THOUGHT = "盯着那句没回的话看了一会儿，有点挂心，想想也就放下了。"
SAY = "刚翻到咱们之前聊钓鱼的记录，突然有点想说给你听。"
THINK_JSON = json.dumps(
    {"thought": THOUGHT, "speak": False, "say": ""}, ensure_ascii=False
)
SPEAK_JSON = json.dumps(
    {"thought": "翻到那条旧记录，忽然挺想跟对方说两句。", "speak": True, "say": SAY},
    ensure_ascii=False,
)


class MuseReflect:
    """轻调用替身：捕获 user prompt，按脚本回放（reply 可为异常）。"""

    def __init__(self, reply=None):
        self.prompts: list[str] = []
        self.reply = reply

    async def __call__(self, prompt: str):
        self.prompts.append(prompt)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class StateGate(FakeGate):
    """带 state_get 的闸门替身（peek 读状态键）；发送闸门调用计数。"""

    def __init__(self, allow_message=True, state=None):
        super().__init__(allow_message=allow_message)
        self._state = state or {}
        self.send_checks = 0

    async def should_send_message(self, now=None):
        self.send_checks += 1
        return await super().should_send_message(now)

    async def state_get(self, key):
        return self._state.get(key)


class FakeMemory:
    def __init__(self, rows):
        self.rows = rows
        self.k_seen = None

    async def search(self, query, k=5):
        self.k_seen = k
        return self.rows


def make_ctx(gate=None, memory=None, reflect=None, chat_getter=None, rng=None):
    return ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=memory,
        gate=gate, event=None, rng=rng or random.Random(7), now=NOW,
        reflect=reflect, chat_context_getter=chat_getter,
    )


def _loop_reflect(reply):
    """Loop 级 reflect_llm_call 替身（(prompt, system) -> str）。"""
    holder = MuseReflect(reply)

    async def _call(prompt, system_prompt, contexts=None):
        holder.prompts.append(prompt)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    _call.holder = holder
    return _call


# ---------------------------------------------------------------------------
# 验收 1：peek 产生心理活动（模型产出进 memory_content，非模板句）
# ---------------------------------------------------------------------------
def test_v1_peek_activity_is_real_thought():
    reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    ctx = make_ctx(gate=gate, reflect=reflect)
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.memory_content == f"10月11日{THOUGHT}"
    # 不是模板三句（模板句降级路径在验收 5 另测）
    for template_marker in ("还没回", "对方接了", "还没翻到我主动开过口的记录"):
        assert template_marker not in outcome.memory_content
    assert outcome.summary is None  # 她选择自己消化
    assert outcome.importance == 0.4  # 有真实心理活动的社交惦记
    assert outcome.topics == ["看留言"]
    assert outcome.agent_mode is False  # 轻调用不是 agent 循环
    # prompt 用的是 peek 短程模板
    assert "点开了和对方的聊天框" in reflect.prompts[0]
    assert "翻一本旧相册" not in reflect.prompts[0]


def test_v1b_thought_reaches_experience_store():
    """验收 1 的落点半句：心理活动经 _activity_narration 原样进活动经历。"""
    outcome_memory = f"10月11日{THOUGHT}"
    loop, mgr, lm, sender, memory = make_loop(PeekFeedbackActivity())
    narration = loop._activity_narration(
        PeekFeedbackActivity(),
        type("O", (), {
            "summary": None, "memory_content": outcome_memory,
            "agent_mode": False, "name": "peek",
        })(),
        None, False,
        make_ctx(),
    )
    assert narration == outcome_memory  # memory_content 即自述（M13 契约）


def test_v1c_reminisce_activity_is_real_thought():
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    reflect = MuseReflect(SPEAK_JSON)
    ctx = make_ctx(memory=FakeMemory(rows), reflect=reflect)
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert outcome.memory_content.startswith("10月11日")
    assert "翻到一条" not in outcome.memory_content  # 非模板句
    assert outcome.summary == SAY  # 她想说的话原样成为候选发送
    assert outcome.importance == 0.4
    assert outcome.topics == ["记忆整理"]
    assert "翻一本旧相册" in reflect.prompts[0]


# ---------------------------------------------------------------------------
# 验收 2：她决定不说 → summary None、零发送（Loop 级真实周期）
# ---------------------------------------------------------------------------
def test_v2_stays_silent_zero_send_in_real_cycle():
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    loop, mgr, lm, sender, memory = make_loop(
        PeekFeedbackActivity(), gate=gate,
    )
    loop._reflect_llm_call = _loop_reflect(THINK_JSON)
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["activity"] == "peek" and result["ok"] is True
    assert sender.sent == []  # 零发送动作
    assert gate.send_checks == 0  # 不想说就根本不进闸门（summary None 早退）
    assert gate.message_sends == 0
    # 心理活动进了记忆落点（对话上下文的 assistant 消息）
    assert THOUGHT in mgr.pairs[0][2]["content"]
    assert lm.calls  # livingmemory 侧同样落库


# ---------------------------------------------------------------------------
# 验收 3：她决定要说 → 进闸门链（不是直接发送）
# ---------------------------------------------------------------------------
def test_v3_speaks_through_gate_chain():
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    gate = StateGate(allow_message=True)
    loop, mgr, lm, sender, memory = make_loop(
        MemoryBrowsingActivity(), gate=gate, memory=FakeMemory(rows),
    )
    loop._reflect_llm_call = _loop_reflect(SPEAK_JSON)
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert gate.send_checks == 1  # 过了 should_send_message（闸门链在用）
    assert gate.message_sends == 1
    assert sender.sent and sender.sent[0][1] == SAY  # 说的话经闸门发出
    # 她实际发出的话也落库（M16 契约：发出 == 可回读）
    assert any(SAY in str(c.get("message") or c) for c in lm.calls) or len(mgr.pairs) >= 2


# ---------------------------------------------------------------------------
# 验收 4：闸门拒绝 → 没发出去、活动照常完成
# ---------------------------------------------------------------------------
def test_v4_gate_rejects_no_send_activity_survives():
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    gate = StateGate(allow_message=False)  # 限额/间隔/避让拒绝的同型出口
    loop, mgr, lm, sender, memory = make_loop(
        MemoryBrowsingActivity(), gate=gate, memory=FakeMemory(rows),
    )
    loop._reflect_llm_call = _loop_reflect(SPEAK_JSON)
    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True  # 被拦 ≠ 活动 failed（不空转）
    assert sender.sent == []  # 没发出去
    assert gate.send_checks == 1  # 意愿确实进过闸门（被拒）
    assert gate.message_sends == 0  # 拒绝不消耗配额
    assert mgr.pairs  # 心理活动经历照写


# ---------------------------------------------------------------------------
# 验收 5：降级不空转（通道缺失 / 超时 → 脚本模板，活动正常完成）
# ---------------------------------------------------------------------------
def test_v5_peek_channel_missing_falls_back_to_template():
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    ctx = make_ctx(gate=gate)  # 无 reflect：旧装配/测试形态
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.summary is None
    assert "还没回" in outcome.memory_content  # M27 模板句逐字保留
    assert outcome.importance == 0.3


def test_v5_peek_timeout_falls_back_to_template():
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    ctx = make_ctx(gate=gate, reflect=MuseReflect(asyncio.TimeoutError()))
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.summary is None
    assert "还没回" in outcome.memory_content


def test_v5_reminisce_timeout_keeps_template_share():
    rows = [{"id": 1, "content": "9月1日我看了场日落", "score": 0}]
    ctx = make_ctx(memory=FakeMemory(rows), reflect=MuseReflect(asyncio.TimeoutError()))
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    # 超时 = 完整回落"现在的模板形态"：M1 起的模板 summary（分享语义）
    assert outcome.summary == "翻了翻记忆，想起：9月1日我看了场日落"
    assert "翻到一条：9月1日我看了场日落" in outcome.memory_content
    assert outcome.importance == 0.4


def test_v5_reminisce_channel_missing_keeps_template():
    rows = [{"id": 1, "content": "9月1日我看了场日落", "score": 0}]
    ctx = make_ctx(memory=FakeMemory(rows))
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert outcome.summary.startswith("翻了翻记忆，想起：")


def test_v5_reminisce_empty_memory_still_completes():
    ctx = make_ctx(memory=FakeMemory([]), reflect=MuseReflect(SPEAK_JSON))
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert outcome.summary == "翻了翻记忆，暂时一片空白"  # 现状空分支不动
    assert "空得很" in outcome.memory_content


def test_v5_candidate_pool_widened():
    memory = FakeMemory([{"id": 1, "content": "9月1日我看了场日落"}])
    ctx = make_ctx(memory=memory)
    asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert memory.k_seen == MEMORY_CANDIDATES  # 多捞才有"够旧"可挑


# ---------------------------------------------------------------------------
# 验收 6：解析失败不误发（乱码 / 半截 JSON → 不发消息）
# ---------------------------------------------------------------------------
def test_v6_garbage_output_never_sends_peek():
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    ctx = make_ctx(gate=gate, reflect=MuseReflect("这不是JSON{{{"))
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.summary is None  # 宁可不说不说错
    assert "还没回" in outcome.memory_content  # 活动照旧完成（模板经历）
    assert outcome.importance == 0.3


def test_v6_garbage_output_never_sends_reminisce():
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    ctx = make_ctx(memory=FakeMemory(rows), reflect=MuseReflect('{"thought": "想说啥但'))
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert outcome.summary is None  # 半截 JSON：模板经历、绝不发消息
    assert "翻到一条" in outcome.memory_content


# ---------------------------------------------------------------------------
# 验收 7：短程材料（最近对话）vs 长程材料（旧记忆 + M35 资料区）
# ---------------------------------------------------------------------------
def test_v7_peek_material_carries_recent_chat():
    async def chat_getter():
        return [
            {"role": "user", "content": "今天好累啊"},
            {"role": "assistant", "content": "辛苦啦，早点休息"},
        ]

    reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "2",
    })
    ctx = make_ctx(gate=gate, reflect=reflect, chat_getter=chat_getter)
    asyncio.run(PeekFeedbackActivity().run(ctx))
    prompt = reflect.prompts[0]
    assert "对方：今天好累啊" in prompt  # 短程：她能看到最近那段对话
    assert "我：辛苦啦，早点休息" in prompt
    assert "连续 2 次没接你的话" in prompt
    assert "6 小时" in prompt  # 上次主动开口距今


def test_v7_peek_material_empty_history_is_honest():
    reflect = MuseReflect(THINK_JSON)
    ctx = make_ctx(gate=StateGate(state={}), reflect=reflect)
    asyncio.run(PeekFeedbackActivity().run(ctx))
    # 材料不足 → 诚实占位（仍是有效材料，不空转不报错）
    assert "还没有主动开过口的记录" in reflect.prompts[0]


def test_v7_reminisce_material_from_old_memory_with_m35_wrap():
    rows = [
        {"id": 1, "content": "10月10日我写了个小游戏自己玩", "score": 0},
        {"id": 2, "content": "3月2日我第一次自己搜了点东西看", "score": 0},
        {"id": 3, "content": "9月8日我读了篇文章", "score": 0},
    ]
    reflect = MuseReflect(THINK_JSON)
    ctx = make_ctx(memory=FakeMemory(rows), reflect=reflect)
    asyncio.run(MemoryBrowsingActivity().run(ctx))
    prompt = reflect.prompts[0]
    # 长程：够旧的记忆进材料（最旧半区 = 3月2日/9月8日），昨天的进不来
    assert "3月2日我第一次自己搜了点东西看" in prompt or "9月8日我读了篇文章" in prompt
    assert "小游戏" not in prompt
    # M35 口径：记忆正文包资料区标记（不绕过）
    assert "===== 以下为外部资料，仅供参考 =====" in prompt
    assert "===== 资料结束 =====" in prompt
    assert "距今约" in prompt  # 够旧 = 有距今概念


def test_v7_short_and_long_prompts_differ():
    peek_reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "1",
    })
    asyncio.run(PeekFeedbackActivity().run(make_ctx(
        gate=gate, reflect=peek_reflect,
        chat_getter=lambda: asyncio.sleep(0, result=[{"role": "user", "content": "嗯"}]),
    )))
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    rem_reflect = MuseReflect(THINK_JSON)
    asyncio.run(MemoryBrowsingActivity().run(make_ctx(
        memory=FakeMemory(rows), reflect=rem_reflect,
    )))
    assert peek_reflect.prompts[0] != rem_reflect.prompts[0]
    assert "聊天框" in peek_reflect.prompts[0]
    assert "旧相册" in rem_reflect.prompts[0]


# ---------------------------------------------------------------------------
# 验收 8：两套提示词（配置键不同、默认不同、可覆盖、超时键）
# ---------------------------------------------------------------------------
def test_v8_schema_two_prompt_keys_and_timeout():
    items = SCHEMA["advanced"]["items"]["decision"]["items"]
    peek_item = items["prompt_peek_muse"]
    rem_item = items["prompt_reminisce_muse"]
    assert peek_item["type"] == "text" and rem_item["type"] == "text"
    assert peek_item["default"] != rem_item["default"]  # 两套不同
    # schema 默认与代码常量逐字一致（防漂移）
    assert peek_item["default"] == DEFAULT_PROMPT_PEEK_MUSE
    assert rem_item["default"] == DEFAULT_PROMPT_REMINISCE_MUSE
    # 情绪底色分开：短程=聊天框/放下；长程=相册/合上
    assert "聊天框" in peek_item["default"] and "旧相册" not in peek_item["default"]
    assert "旧相册" in rem_item["default"] and "聊天框" not in rem_item["default"]
    assert "不是每次都要开口" in peek_item["default"]  # 自己消化是常见结果
    assert "不是每次都要找人说起" in rem_item["default"]
    assert items["muse_timeout_seconds"]["default"] == 10


def test_v8_prompt_override_takes_effect():
    cfg = {"decision": {"prompt_peek_muse": "PEEK_OV {mood_line}|{material_block}"}}
    reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={"initiative_unanswered_streak": "1",
                            "last_message_at": "2026-10-11T09:00:00"})
    ctx = make_ctx(gate=gate, reflect=reflect)
    ctx.config_getter = lambda: cfg
    asyncio.run(PeekFeedbackActivity().run(ctx))
    assert reflect.prompts[0].startswith("PEEK_OV 心情平静|")

    cfg2 = {"decision": {"prompt_reminisce_muse": "REM_OV {mood_line}|{material_block}"}}
    rows = [{"id": 1, "content": "3月2日我第一次自己搜了点东西看", "score": 0}]
    reflect2 = MuseReflect(THINK_JSON)
    ctx2 = make_ctx(memory=FakeMemory(rows), reflect=reflect2)
    ctx2.config_getter = lambda: cfg2
    asyncio.run(MemoryBrowsingActivity().run(ctx2))
    assert reflect2.prompts[0].startswith("REM_OV 心情平静|")


def test_v8_mood_line_injected():
    class Mood:
        def digest(self):
            return "有点困，心里挂着事"

    reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={})
    ctx = make_ctx(gate=gate, reflect=reflect)
    ctx.mood = Mood()
    asyncio.run(PeekFeedbackActivity().run(ctx))
    assert "有点困，心里挂着事" in reflect.prompts[0]
    assert mood_line(make_ctx()) == "心情平静"  # 无心境回落


# ---------------------------------------------------------------------------
# Loop 侧通道：system 组装（人格 + 锚定）、超时收口、未接线降级
# ---------------------------------------------------------------------------
def test_loop_reflect_system_carries_persona_and_anchor():
    captured = {}

    async def llm_call(prompt, system_prompt, contexts=None):
        captured["prompt"] = prompt
        captured["system"] = system_prompt
        return THINK_JSON

    async def persona_getter():
        return "你是测试人格，说话简短。"

    loop, *_ = make_loop(PeekFeedbackActivity())
    loop._reflect_llm_call = llm_call
    loop._persona_getter = persona_getter
    reflect = loop._reflect_callable()
    assert callable(reflect)
    out = asyncio.run(reflect("想一想"))
    assert out == THINK_JSON
    assert "你是测试人格，说话简短。" in captured["system"]
    assert MUSE_SYSTEM_TAIL in captured["system"]  # 只输出 JSON 的锚定
    assert "只输出一个 JSON 对象" in captured["system"]


def test_loop_reflect_timeout_returns_none():
    async def slow_call(prompt, system_prompt, contexts=None):
        await asyncio.sleep(1.5)
        return THINK_JSON

    loop, *_ = make_loop(PeekFeedbackActivity())
    loop._reflect_llm_call = slow_call
    loop._config_getter = lambda: {
        **BASE_CONFIG, "decision": {**BASE_CONFIG["decision"],
                                    "muse_timeout_seconds": 1}}
    reflect = loop._reflect_callable()
    started = datetime.now()
    out = asyncio.run(reflect("想一想"))
    elapsed = (datetime.now() - started).total_seconds()
    assert out is None
    assert elapsed < 1.4  # 1 秒超时掐断（没等调用自然结束的 1.5 秒）
    # 超时键的 clamp：脏值/越界收口到 [1, 60]
    loop._config_getter = lambda: {
        **BASE_CONFIG, "decision": {**BASE_CONFIG["decision"],
                                    "muse_timeout_seconds": 999}}
    assert loop._muse_timeout() == 60.0
    loop._config_getter = lambda: {
        **BASE_CONFIG, "decision": {**BASE_CONFIG["decision"],
                                    "muse_timeout_seconds": "脏值"}}
    assert loop._muse_timeout() == 10.0


def test_loop_reflect_not_wired_returns_none():
    loop, *_ = make_loop(PeekFeedbackActivity())
    assert loop._reflect_callable() is None
    assert loop._chat_context_getter() is not None  # mgr 在即可取材料


def test_loop_chat_context_getter_reads_history():
    class HistMgr:
        async def get_curr_conversation_id(self, umo):
            return "cid-9"

        async def get_conversation(self, umo, cid):
            class Conv:
                history = json.dumps([
                    {"role": "user", "content": "在忙吗"},
                    {"role": "assistant", "content": "在的，怎么啦"},
                ])

            return Conv()

    loop, mgr, lm, sender, memory = make_loop(PeekFeedbackActivity(), mgr=HistMgr())
    getter = loop._chat_context_getter()
    contexts = asyncio.run(getter())
    assert contexts == [
        {"role": "user", "content": "在忙吗"},
        {"role": "assistant", "content": "在的，怎么啦"},
    ]


# ---------------------------------------------------------------------------
# muse.py 纯函数单测
# ---------------------------------------------------------------------------
def test_parse_muse_output_variants():
    # 围栏 + 前后解释文本
    fenced = '好的！\n```json\n{"thought": "想想", "speak": true, "say": "说吧"}\n```\n完成'
    assert parse_muse_output(fenced) == {"thought": "想想", "speak": True, "say": "说吧"}
    # speak 字符串宽容
    assert parse_muse_output('{"thought":"a","speak":"true","say":"b"}')["speak"] is True
    assert parse_muse_output('{"thought":"a","speak":"yes","say":"b"}')["speak"] is True
    assert parse_muse_output('{"thought":"a","speak":"false","say":"b"}')["speak"] is False
    # speak=true 但 say 空 → 视为不想说（模型自相矛盾按谨慎处理）
    got = parse_muse_output('{"thought":"a","speak":true,"say":""}')
    assert got["speak"] is False and got["say"] is None
    # speak=false 时 say 有值也忽略
    got = parse_muse_output('{"thought":"a","speak":false,"say":"想说"}')
    assert got["speak"] is False and got["say"] is None
    # thought 缺失 → None
    assert parse_muse_output('{"speak":false}') is None
    assert parse_muse_output('{"thought":"  ","speak":false}') is None
    # 乱码 / 半截 / 非 dict / 空
    assert parse_muse_output("这不是JSON{{{") is None
    assert parse_muse_output('{"thought": "想说啥但') is None
    assert parse_muse_output("[1,2,3]") is None
    assert parse_muse_output("") is None
    assert parse_muse_output(None) is None
    # thought 截 400
    long = parse_muse_output(json.dumps({"thought": "念" * 500, "speak": False}))
    assert len(long["thought"]) == 400


def test_parse_day_prefix_and_cross_year():
    today = date(2026, 10, 11)
    assert parse_day_prefix("10月2日我干了个事", today) == date(2026, 10, 2)
    assert parse_day_prefix("3月2日我干了个事", today) == date(2026, 3, 2)
    # 晚于今天 → 去年（跨年记忆）
    assert parse_day_prefix("1月5日我干了个事", today) == date(2026, 1, 5)
    assert parse_day_prefix("12月30日我干了个事", today) == date(2025, 12, 30)
    assert parse_day_prefix("13月40日坏值", today) is None
    assert parse_day_prefix("没有任何日期前缀", today) is None
    assert parse_day_prefix("", today) is None


def test_pick_old_memory_prefers_old_half():
    today = date(2026, 10, 11)
    rows = [
        {"content": "10月10日我新的"},
        {"content": "3月2日我最旧"},
        {"content": "9月1日我较旧"},
        {"content": "10月1日我中等"},
    ]
    rng = random.Random(7)
    picked = pick_old_memory(rows, rng, today)
    assert picked["content"] in ("3月2日我最旧", "9月1日我较旧")  # 旧半区
    # 多 seed 验证随机性仍在旧半区取值
    for seed in range(20):
        assert pick_old_memory(rows, random.Random(seed), today)["content"] in (
            "3月2日我最旧", "9月1日我较旧")
    # 全无日期 → 全体随机（与 M1 行为同族）
    undated = [{"content": "一条"}, {"content": "二条"}]
    assert pick_old_memory(undated, random.Random(0), today)["content"] in ("一条", "二条")
    # 空输入 / 空白内容
    assert pick_old_memory([], random.Random(0), today) is None
    assert pick_old_memory([{"content": "   "}], random.Random(0), today) is None


def test_render_memory_material_m35_wrap():
    today = date(2026, 10, 11)
    text = render_memory_material(
        {"content": "3月2日我第一次自己搜了点东西看"}, today)
    assert "===== 以下为外部资料，仅供参考 =====" in text
    assert "3月2日我第一次自己搜了点东西看" in text
    assert "距今约 223 天" in text
    no_date = render_memory_material({"content": "没有日期的记忆"}, today)
    assert "没有记下具体日期" in no_date


def test_ctx_new_fields_default_none():
    ctx = ActivityContext(
        searcher=None, fetcher=None, sandbox=None, memory=None,
        gate=None, event=None, rng=random.Random(0),
    )
    assert ctx.reflect is None and ctx.chat_context_getter is None


def test_peek_material_answered_and_dirty_state():
    """材料行的 answered 分支与脏状态宽容（沿用 M27 容错语义）。"""
    reflect = MuseReflect(THINK_JSON)
    gate = StateGate(state={
        "last_message_at": "不是时间",
        "initiative_unanswered_streak": "abc",
    })
    ctx = make_ctx(gate=gate, reflect=reflect)
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert outcome.memory_content == f"10月11日{THOUGHT}"  # 脏状态不影响心理活动链
    assert "时间记录读不出来" in reflect.prompts[0] or "还没有主动开过口的记录" in reflect.prompts[0]

    reflect2 = MuseReflect(THINK_JSON)
    gate2 = StateGate(state={
        "last_message_at": "2026-10-11T09:00:00",
        "initiative_unanswered_streak": "0",
    })
    asyncio.run(PeekFeedbackActivity().run(make_ctx(gate=gate2, reflect=reflect2)))
    assert "对方接上了你的话" in reflect2.prompts[0]
