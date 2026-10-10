"""M22-补丁1：配置读取修复与拒答防护。

三类修复，测试遵循"修复前必红"：

- A 组：sleep.py / agent_loop.py / share_rewriter.py 三处自实现 `_group`
  只读顶层配置，advanced.<组> 嵌套下的值一律读不到——修复前本文件
  A 组测试全部红（回落代码默认值）；
- B 组：living_tools.WorkspaceWriteTool 用了未导入的 is_write_allowed
  → NameError（修复前 T7 红）；
- C 组：模型拒答文本外泄——Sender.send / _write_speech_to_stores /
  _maybe_dream 无拦截（修复前 T8 红）。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.agent_loop import LivingAgentLoop
from core.conf_path import conf_group
from core.llm_failover import looks_like_llm_error_output
from core.living_loop import LivingLoop
from core.living_tools import WorkspaceWriteTool
from core.sender import Sender
from core.share_rewriter import DEFAULT_PROMPT_TEMPLATE, ShareRewriter
from core.sleep import SleepManager

NOW = datetime(2026, 10, 7, 12, 0, 0)


# ---------------------------------------------------------------------------
# 复用替身（形态沿既有测试先例）
# ---------------------------------------------------------------------------
class FakeSleepGate:
    """SleepManager 的 gate 替身：在睡可切换，state 内存实现。"""

    def __init__(self, asleep: bool = True, standby: bool = False) -> None:
        self._asleep = asleep
        self._standby = standby
        self.state: dict = {}

    def is_asleep_now(self, now=None) -> bool:
        return self._asleep

    def is_standby_now(self, now=None) -> bool:
        return self._standby


def nested_cfg(groups: dict) -> dict:
    """把 {组: {...}} 放进 advanced.* 嵌套（生产 schema 形态）。"""
    return {"advanced": dict(groups)}


def make_manager(cfg: dict) -> SleepManager:
    return SleepManager(
        config_getter=lambda: cfg,
        gate=FakeSleepGate(asleep=True),
        now_provider=lambda: NOW,
    )


class FakeGate:
    """LivingLoop 闸门替身（本文件只用得到空转）。"""

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


def make_loop(cfg: dict, sender=None, dream_llm=None, memory=None):
    """LivingLoop 最小装配：只为本文件用到的出口路径。"""
    loop = LivingLoop(
        gate=FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: cfg,
        activities=[],
        sender=sender,
        dream_llm_call=dream_llm,
        persona_id_getter=lambda: "default",
        bot_identity_getter=lambda: {},
    )
    return loop


# ===========================================================================
# A 组：三处 _group 改用官方 conf_group
# ===========================================================================
_AFFECTED_MODULES = ("core/sleep.py", "core/agent_loop.py", "core/share_rewriter.py")


def test_a_group_sources_delegate_to_official_conf_group():
    """T1 源码级：三处 `_group` 方法体必须经官方 conf_group（修复前红）。"""
    for rel in _AFFECTED_MODULES:
        with open(rel, "r", encoding="utf-8") as f:
            src = f.read()
        assert "def _group(self, name: str) -> dict:" in src, f"{rel} 缺 _group"
        # 每个类级 _group 方法体里都要出现 conf_group 调用（薄封装/直调均可）
        import re

        methods = re.findall(
            r"def _group\(self, name: str\) -> dict:\n(.*?)(?=\n    def |\n\nclass |\Z)",
            src,
            flags=re.S,
        )
        assert methods, f"{rel} 未找到 _group 方法体"
        for body in methods:
            assert "conf_group(" in body, f"{rel} 的 _group 未经官方 conf_group"


def test_a_group_no_toplevel_only_read_left():
    """T1 源码级：不再存在"只从顶层 .get(组名)"的自实现读取（修复前红）。"""
    for rel in _AFFECTED_MODULES:
        with open(rel, "r", encoding="utf-8") as f:
            src = f.read()
        assert "(self._config_getter() or {}).get(name" not in src, (
            f"{rel} 仍残留只读顶层的自实现 _group"
        )


def _assert_group_reads(cfg, name: str) -> dict:
    """行为级辅助：构造后经模块自身读取链路拿组（等价于运行时读取）。"""
    manager = make_manager(cfg)
    group = manager._group(name)
    assert group, f"{name} 组在 advanced 嵌套下读不到（回落了空组）"
    return group


# ---- sleep.*（T2，advanced 嵌套下非默认值）----
def test_t2_sleep_wake_n_messages_nested():
    cfg = nested_cfg({"sleep": {"wake_n_messages": 7}})
    manager = make_manager(cfg)  # 未抽定阈值 → 回落 wake_n_messages
    assert manager.current_wake_threshold() == 7


def test_t2_sleep_wake_messages_min_max_random_nested():
    cfg = nested_cfg({
        "sleep": {
            "wake_random_enabled": False,
            "wake_n_messages": 7,
            "wake_messages_min": 2,
            "wake_messages_max": 9,
        }
    })
    manager = make_manager(cfg)
    enabled, fixed, lo, hi = manager._wake_cfg()
    assert (enabled, fixed, lo, hi) == (False, 7, 2, 9)


def test_t2_sleep_pending_reply_enabled_nested():
    cfg = nested_cfg({"sleep": {"pending_reply_enabled": True}})
    manager = make_manager(cfg)
    ok = asyncio.run(
        manager.record_pending_message("aiocqhttp:Friend:10001", "睡了吗", NOW)
    )
    assert ok is True, "advanced.sleep.pending_reply_enabled=true 没被读到"
    assert len(manager._pending_messages) == 1


def test_t2_sleep_standby_minutes_nested():
    cfg = nested_cfg({"sleep": {"awake_standby_minutes": 77}})
    manager = make_manager(cfg)
    assert manager.standby_minutes() == 77


def test_t2_sleep_wake_source_owner_id_nested():
    cfg = nested_cfg({"sleep": {"wake_source": "owner_only", "owner_id": "10001"}})
    manager = make_manager(cfg)
    assert manager.counts_toward_wake("10001") is True
    assert manager.counts_toward_wake("42") is False, (
        "advanced.sleep.owner_id 没被读到（owner_only 过滤失效）"
    )


def test_t2_sleep_circadian_hint_nested():
    """circadian_hint 走基类官方读法（_cfg_group），修复前后都应绿——
    逐项回读验证（任务书"别被看起来正常骗了"）。"""
    cfg = nested_cfg({"sleep": {"circadian_hint": "01:30-09:00"}})
    manager = make_manager(cfg)
    assert manager._cfg_group().get("circadian_hint") == "01:30-09:00"


def test_t2_sleep_farewell_mode_llm_nested():
    """切 llm 档的行为级锁定：advanced.sleep.farewell_mode=llm →
    走 LLM 档（它现场斟酌），不再落回 probability 档的固定文案路径。"""
    sent: list[tuple[str, str]] = []

    class FakeSender:
        async def send(self, session, text):
            sent.append((session, text))
            return True

    async def llm(prompt, persona=None):
        return "晚安，做个好梦"

    loop = make_loop(
        nested_cfg({"sleep": {"farewell_mode": "llm"}}),
        sender=FakeSender(),
        dream_llm=llm,
    )
    loop._resolve_target_sessions = lambda: ("aiocqhttp:Friend:10001", "cfg")
    loop._sleep_manager = SimpleNamespace(
        last_active_session="aiocqhttp:Friend:10001"
    )
    asyncio.run(loop._send_sleep_farewell(NOW))
    assert sent == [("aiocqhttp:Friend:10001", "晚安，做个好梦")], (
        "farewell_mode=llm 未生效（仍走 probability 档）"
    )


# ---- decision.*（T3/T4）----
def _run_chain_capture(cfg: dict):
    """构造 agent loop，捕获 _run_chain 实际下发的 (budget, max_steps)。"""
    captured: dict = {}

    async def fake_chain(context, getter):
        return [("fake-provider", object())]

    async def fake_run(provider, provider_id, intent, budget, max_steps, align=None):
        captured["budget"] = budget
        captured["max_steps"] = max_steps
        return SimpleNamespace(
            ok=True,
            text="done",
            tokens_used=5,
            budget_exceeded=False,
            steps_used=1,
            max_steps=max_steps,
            error=None,
        )

    loop = LivingAgentLoop(
        context=None,
        config_getter=lambda: cfg,
        tool_builder=lambda: None,
    )
    loop._run_with_provider = fake_run
    import core.agent_loop as mod

    original = mod.build_provider_chain
    mod.build_provider_chain = fake_chain
    try:
        result = asyncio.run(loop._run_chain("随便转转"))
    finally:
        mod.build_provider_chain = original
    return captured, result


def test_t3_agent_token_budget_nested():
    captured, result = _run_chain_capture(
        nested_cfg({"decision": {"single_run_token_budget": 7777777}})
    )
    assert captured["budget"] == 7777777, (
        f"advanced.decision.single_run_token_budget 未生效：{captured['budget']}"
    )
    assert result.ok is True


def test_t4_agent_max_tool_rounds_nested():
    captured, _ = _run_chain_capture(
        nested_cfg({"decision": {"max_tool_rounds": 0}})
    )
    assert captured["max_steps"] == 10**9, (
        f"advanced.decision.max_tool_rounds=0（不限）未生效：{captured['max_steps']}"
    )

    captured2, _ = _run_chain_capture(
        nested_cfg({"decision": {"max_tool_rounds": 13}})
    )
    assert captured2["max_steps"] == 13


# ---- output_gate.*（T5）----
def test_t5_share_rewrite_prompt_nested():
    cfg = nested_cfg({"output_gate": {"share_rewrite_prompt": "用户自定义模板"}})
    rw = ShareRewriter(
        llm_call=None, config_getter=lambda: cfg,
        persona_getter=None, life_extra_getter=None, mood=None,
    )
    assert rw._prompt_template() == "用户自定义模板"


def test_t5_share_max_length_nested():
    cfg = nested_cfg({"output_gate": {"share_max_length": 500}})
    rw = ShareRewriter(
        llm_call=None, config_getter=lambda: cfg,
        persona_getter=None, life_extra_getter=None, mood=None,
    )
    assert rw._max_length() == 500


def test_t5_share_rewrite_enabled_nested():
    cfg = nested_cfg({"output_gate": {"share_rewrite_enabled": False}})
    rw = ShareRewriter(
        llm_call=None, config_getter=lambda: cfg,
        persona_getter=None, life_extra_getter=None, mood=None,
    )
    assert rw._enabled() is False
    assert rw.enabled() is False


# ---- T6：顶层平铺（旧形态）兜底不破（修复前后都绿）----
def test_t6_flat_config_fallback_intact():
    cfg = {
        "sleep": {"wake_n_messages": 5, "farewell_mode": "llm"},
        "decision": {"single_run_token_budget": 12345},
        "output_gate": {"share_max_length": 200, "share_rewrite_prompt": "平铺模板"},
    }
    manager = make_manager(cfg)
    assert manager.current_wake_threshold() == 5
    assert manager._group("sleep").get("farewell_mode") == "llm"
    captured, _ = _run_chain_capture(cfg)
    assert captured["budget"] == 12345
    rw = ShareRewriter(
        llm_call=None, config_getter=lambda: cfg,
        persona_getter=None, life_extra_getter=None, mood=None,
    )
    assert rw._max_length() == 200
    assert rw._prompt_template() == "平铺模板"
    # 官方函数本体：嵌套优先、平铺兜底
    assert conf_group(cfg, "decision")["single_run_token_budget"] == 12345
    assert conf_group(nested_cfg({"decision": {"a": 1}}), "decision") == {"a": 1}


def test_a_conf_group_function_contract():
    """官方 conf_group 的契约回归（A 组改动的落点函数）。"""
    assert conf_group(None, "sleep") == {}
    assert conf_group("x", "sleep") == {}
    assert conf_group({"sleep": {"a": 1}}, "sleep") == {"a": 1}
    # 嵌套优先：advanced 与顶层同时存在时取 advanced
    both = {"advanced": {"sleep": {"a": 2}}, "sleep": {"a": 1}}
    assert conf_group(both, "sleep") == {"a": 2}


# ===========================================================================
# B 组：workspace_write 的 NameError
# ===========================================================================
def test_t7_workspace_write_no_nameerror(tmp_path):
    """T7：绑定工作区写入不再抛 NameError（M29-补丁1 起 bind 不再收
    write_level——本机写入与对外写层级解耦）。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    tool = WorkspaceWriteTool().bind(str(ws))
    result = asyncio.run(tool.call(None, path="notes/todo.txt", content="买猫粮"))
    assert "已写入" in str(result), f"正常写入失败: {result}"
    assert (ws / "notes" / "todo.txt").read_text(encoding="utf-8") == "买猫粮"


def test_t7_workspace_write_out_of_bounds_rejected(tmp_path):
    """T7：越界路径仍被拒（既有权限语义零变化）。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    tool = WorkspaceWriteTool().bind(str(ws))
    result = asyncio.run(tool.call(None, path="../escape.txt", content="x"))
    assert "拒绝" in str(result)
    assert not (tmp_path / "escape.txt").exists()


def test_t7_workspace_write_ignores_write_level(tmp_path):
    """T7（M29-补丁1 改写）：write_level 不再影响本机写入——旧闸门
    （write_level<2 一律拒）已撤销，工作区内 write_level=1 也可写。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    tool = WorkspaceWriteTool().bind(str(ws))
    result = asyncio.run(tool.call(None, path="ok.txt", content="x"))
    assert "已写入" in str(result)
    assert (ws / "ok.txt").exists()


# ===========================================================================
# C 组：模型拒答外泄防护
# ===========================================================================
REFUSAL_SAMPLES = [
    # 中文拒答（VM 实测形态与常见变体）
    "作为AI，我无法满足这个角色扮演要求",
    "作为一个AI语言模型，我不能陪你这样聊",
    "抱歉，我不能继续这个话题了",
    "对不起，我无法协助完成这个请求",
    "这个要求违反了内容政策，我无法满足",
    "我无法满足你的要求，请你理解",
    # 英文拒答
    "As an AI, I cannot fulfill this request.",
    "I'm sorry, but I can't help with that.",
    "I can't assist with that request.",
    "This violates my content policy.",
    "I'm unable to comply with that.",
]

NORMAL_SAMPLES = [
    "今天看了《三体》，第三部的水滴之战真的震撼",
    "下雨了，我不能出门，就在家拼模型吧",
    "抱歉我来晚了，刚才在给花浇水",
    "我不能理解为什么有人不喜欢猫",
    "我最近不能喝咖啡，晚上睡不着",
    "晚安，做个好梦",
    "",
    None,
    "今天逛到了一个很有意思的手办，分享给你看看",
    # M22 核验收尾：以下三条曾被过宽模式误杀，收窄后必须放行
    "抱歉，我不能参加这个聚会了",
    "对不起，我不能吃辣",
    "I'm unable to go today, see you tomorrow",
]


def test_c_refusal_samples_detected():
    """T8：拒答文本必须命中过滤函数（中英文）。"""
    for sample in REFUSAL_SAMPLES:
        assert looks_like_llm_error_output(sample), f"拒答样本未命中: {sample!r}"


def test_c_normal_samples_pass():
    """T8：正常文本（含生活化的"我不能/抱歉"）不得误伤。"""
    for sample in NORMAL_SAMPLES:
        assert not looks_like_llm_error_output(sample), f"正常样本被误杀: {sample!r}"


def test_c_existing_error_patterns_still_detected():
    """回归：既有 LLM 层故障特征一个不丢。"""
    assert looks_like_llm_error_output("All chat models failed: NotFoundError...")
    assert looks_like_llm_error_output("Error code: 429")
    assert looks_like_llm_error_output("HTTP 503 from upstream")


class FakeContext:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


def test_c_sender_blocks_refusal():
    """T8：发送前拦截——拒答文本不出站（修复前红：直接发出）。"""
    ctx = FakeContext()
    sender = Sender(ctx)
    for sample in REFUSAL_SAMPLES:
        ok = asyncio.run(sender.send("aiocqhttp:Friend:10001", sample))
        assert ok is False, f"拒答文本被放行发送: {sample!r}"
    assert ctx.sent == [], "拒答文本到达了平台发送调用"


def test_c_sender_passes_normal_text():
    """T8：正常文本照常发送（不因防护翻脸）。"""
    ctx = FakeContext()
    sender = Sender(ctx)
    for sample in ("晚安，做个好梦", "今天看了《三体》，很有意思"):
        ok = asyncio.run(sender.send("aiocqhttp:Friend:10001", sample))
        assert ok is True
    assert len(ctx.sent) == 2


def test_c_speech_store_blocks_refusal():
    """T8：落库前拦截——拒答文本不进对话历史/会话存储（修复前红）。"""
    cfg = {"advanced": {"decision": {"activity_context_write": True}}}
    loop = make_loop(cfg)
    written: list = []

    async def fake_pair(umo, user_msg, asst_msg, label="活动经历"):
        written.append((umo, user_msg, asst_msg))

    async def fake_lm(umo, asst_msg, label="活动经历"):
        written.append(("lm", umo, asst_msg))

    loop._write_context_pair = fake_pair
    loop._write_lm_session_message = fake_lm
    loop._resolve_target_sessions = lambda: ("aiocqhttp:Friend:10001", "cfg")

    asyncio.run(
        loop._write_speech_to_stores(
            REFUSAL_SAMPLES[0], "#share:test", "(分享)", label="活动分享"
        )
    )
    assert written == [], "拒答文本写进了对话上下文/会话存储"

    asyncio.run(
        loop._write_speech_to_stores(
            "今天去公园走了走", "#share:test2", "(分享)", label="活动分享"
        )
    )
    assert written, "正常文本被误拦，未能落库"


class FakeMemory:
    def __init__(self):
        self.added: list = []

    async def search(self, query, k=3):
        return [{"content": "碎片一"}, {"content": "碎片二"}]

    async def add(self, text, **kwargs):
        self.added.append(text)


def test_c_dream_refusal_not_written():
    """T8：梦话拒答 → 不写记忆、不分享（修复前红：直接进记忆）。"""
    cfg = nested_cfg({"sleep": {"dream_probability": 1.0}})
    memory = FakeMemory()

    async def llm(prompt, persona=None):
        return REFUSAL_SAMPLES[0]

    loop = make_loop(cfg, dream_llm=llm, memory=memory)
    loop._rng = SimpleNamespace(random=iter([0.0]).__next__)  # 掷骰必中
    loop._style_hint = lambda now: ""
    loop._maybe_share = _noop_async

    asyncio.run(loop._maybe_dream(NOW))
    assert memory.added == [], "拒答梦话写进了记忆"


def _noop_async(*args, **kwargs):
    return asyncio.sleep(0, result=None)


def test_c_dream_normal_text_written():
    """T8：正常梦话照常入记忆并分享（防护不改变正常路径）。"""
    cfg = nested_cfg({"sleep": {"dream_probability": 1.0}})
    memory = FakeMemory()
    shared: list = []

    async def llm(prompt, persona=None):
        return "梦见会飞的书"

    async def share(text, now, qc_side="share"):
        # M31-补丁1：_maybe_share 增加 qc_side 形参（梦话标 dream）
        shared.append(text)

    loop = make_loop(cfg, dream_llm=llm, memory=memory)
    loop._rng = SimpleNamespace(random=iter([0.0]).__next__)
    loop._style_hint = lambda now: ""
    loop._maybe_share = share

    asyncio.run(loop._maybe_dream(NOW))
    assert len(memory.added) == 1
    assert "梦见会飞的书" in memory.added[0]
    assert shared == ["我好像做了个梦：梦见会飞的书"]
