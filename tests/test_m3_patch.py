"""M3 补丁测试：模型故障转移链、LLM 错误过滤、图谱会话上下文。"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from core.agent_loop import AgentRunResult, LivingAgentLoop
from core.llm_failover import (
    build_provider_chain,
    is_retryable_llm_error,
    looks_like_llm_error_output,
)
from core.living_loop import LivingLoop

NOW = datetime(2026, 9, 8, 14, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 45, "max_run_seconds": 300,
                 "decision_mode": "rules"},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": ""},
    "model": {"provider_id": "", "fallback_chain": []},
}


# ---------------------------------------------------------------------------
# 错误分类（问题 1）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error, expect",
    [
        ("NotFoundError: model gone", True),
        ("Error code: 404", True),
        ("Error code: 429 rate limited", True),
        ("Request timed out after 30s", True),
        ("Connection error while calling API", True),
        ("HTTP 503 unavailable", True),
        ("Error code: 401 Unauthorized", False),  # 换模型解决不了
        ("invalid api key", False),
        ("Error code: 400 bad request", False),
        ("", False),
        (None, False),
    ],
)
def test_error_classification(error, expect):
    assert is_retryable_llm_error(error) is expect


def test_error_output_detection():
    """问题 2 的过滤依据：错误串被包成"正常产出"的形态。"""
    assert looks_like_llm_error_output("All chat models failed: NotFoundError...")
    assert looks_like_llm_error_output("Error code: 429")
    assert looks_like_llm_error_output("HTTP 503 from upstream")
    assert not looks_like_llm_error_output("今天看了《三体》，很有意思")
    assert not looks_like_llm_error_output("")


# ---------------------------------------------------------------------------
# provider 链构建（问题 1）
# ---------------------------------------------------------------------------
class FakeProvider:
    def __init__(self, pid, chat=True):
        self._pid = pid
        self.has_chat = chat

    def meta(self):
        return types_namespace(id=self._pid)

    @property
    def text_chat(self):
        return object() if self.has_chat else None


def types_namespace(**kw):
    return type("Meta", (), kw)()


class FakePM:
    def __init__(self, providers):
        self._by_id = {p._pid: p for p in providers}

    async def get_provider_by_id(self, pid):
        return self._by_id.get(pid)


class FakeCtxAll:
    def __init__(self, providers, pm):
        self._providers = providers
        self.provider_manager = pm

    def get_all_providers(self):
        return self._providers


def test_provider_chain_order_and_dedupe():
    """链序 = provider_id 优先 → fallback_chain → 自动兜底；按 id 去重。"""
    p_default = FakeProvider("default-chat")
    p_custom = FakeProvider("my-llm")
    p_fallback = FakeProvider("backup-llm")
    ctx = FakeCtxAll([p_default, p_custom, p_fallback], FakePM([p_default, p_custom, p_fallback]))
    config = {"model": {"provider_id": "my-llm", "fallback_chain": ["backup-llm"]}}

    chain = asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["my-llm", "backup-llm", "default-chat"]


def test_provider_chain_skips_unresolvable_config_entries():
    """配置里写了不存在的 provider：跳过不炸。"""
    p_default = FakeProvider("default-chat")
    ctx = FakeCtxAll([p_default], FakePM([p_default]))
    config = {"model": {"provider_id": "", "fallback_chain": ["ghost-provider"]}}

    chain = asyncio.run(build_provider_chain(ctx, lambda: config))
    assert [pid for pid, _ in chain] == ["default-chat"]


# ---------------------------------------------------------------------------
# agent 循环故障转移（问题 1 核心行为）
# ---------------------------------------------------------------------------
def make_agent_loop(chain, config=None):
    pm = FakePM([p for _, p in chain])
    ctx = FakeCtxAll([p for _, p in chain], pm)
    return LivingAgentLoop(
        context=ctx,
        config_getter=lambda: BASE_CONFIG if config is None else config,
        tools=None,
    )


def _patch_runs(loop, behaviors):
    """behaviors: [(provider_id, AgentRunResult 或 异常), ...] 按调用序弹。"""
    calls = []

    async def fake_run_with(provider, provider_id, intent, budget, max_steps, align=None):
        calls.append(provider_id)
        behavior = behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    loop._run_with_provider = fake_run_with
    return calls


def test_failover_switches_until_success():
    """404 → 429 → 成功：按序切换且最终成功（任务书验收场景）。"""
    p1, p2, p3 = FakeProvider("a"), FakeProvider("b"), FakeProvider("c")
    loop = make_agent_loop([("a", p1), ("b", p2), ("c", p3)])
    calls = _patch_runs(loop, [
        AgentRunResult(ok=False, error="NotFoundError: gone", tokens_used=100),
        AgentRunResult(ok=False, error="429 rate limited", tokens_used=80),
        AgentRunResult(ok=True, text="玩好了", tokens_used=500, max_steps=8),
    ])

    result = asyncio.run(loop.run("测试意图"))
    assert calls == ["a", "b", "c"]
    assert result.ok is True
    assert result.text == "玩好了"


def test_failover_all_fail_is_activity_failure():
    """链上全部失败 → 失败路径（活动层写失败记忆）。"""
    p1, p2 = FakeProvider("a"), FakeProvider("b")
    loop = make_agent_loop([("a", p1), ("b", p2)])
    _patch_runs(loop, [
        AgentRunResult(ok=False, error="404 not found", tokens_used=10),
        AgentRunResult(ok=False, error="timeout", tokens_used=10),
    ])

    result = asyncio.run(loop.run("测试意图"))
    assert result.ok is False
    assert result.error


def test_failover_non_retryable_stops_immediately():
    """401 不切换：换模型也解决不了 key 无效。"""
    p1, p2 = FakeProvider("a"), FakeProvider("b")
    loop = make_agent_loop([("a", p1), ("b", p2)])
    calls = _patch_runs(loop, [
        AgentRunResult(ok=False, error="Error code: 401 Unauthorized", tokens_used=5),
        AgentRunResult(ok=True, text="不该被走到"),
    ])

    result = asyncio.run(loop.run("测试意图"))
    assert calls == ["a"]  # 没有尝试 b
    assert result.ok is False


def test_failover_budget_exceeded_does_not_switch():
    """预算耗尽不换链（红线：硬闸语义不变——重试不能变成绕过预算的后门）。"""
    p1, p2 = FakeProvider("a"), FakeProvider("b")
    loop = make_agent_loop([("a", p1), ("b", p2)])
    calls = _patch_runs(loop, [
        AgentRunResult(ok=False, error="超时中断", tokens_used=20000,
                       budget_exceeded=True, max_steps=8),
        AgentRunResult(ok=True, text="不该被走到"),
    ])

    result = asyncio.run(loop.run("测试意图"))
    assert calls == ["a"]
    assert result.budget_exceeded is True


def test_failover_error_text_output_switches():
    """VM 现场形态：provider 把错误包成"正常文本"→ 也要切下一个。"""
    p1, p2 = FakeProvider("a"), FakeProvider("b")
    loop = make_agent_loop([("a", p1), ("b", p2)])
    calls = _patch_runs(loop, [
        AgentRunResult(ok=True, text="All chat models failed: NotFoundError",
                       tokens_used=100, max_steps=8),
        AgentRunResult(ok=True, text="读完了，挺好", tokens_used=400, max_steps=8),
    ])

    result = asyncio.run(loop.run("测试意图"))
    assert calls == ["a", "b"]
    assert result.ok is True and result.text == "读完了，挺好"


def test_failover_empty_chain_is_failure():
    ctx = FakeCtxAll([], FakePM([]))
    loop = LivingAgentLoop(context=ctx, config_getter=lambda: BASE_CONFIG, tools=None)
    result = asyncio.run(loop.run("测试意图"))
    assert result.ok is False
    assert "provider" in result.error


# ---------------------------------------------------------------------------
# 决策调用的故障转移（main._decision_llm_call）
# ---------------------------------------------------------------------------
def test_decision_llm_failover(tmp_path):
    """三个 provider 依次 404/异常文本/成功 → 最终拿到干净文本。"""
    plugin_main = _load_plugin_main()

    async def flow():
        plugin = _make_plugin_with_ctx(tmp_path)
        responses = iter([
            RuntimeError("NotFoundError: gone"),          # 异常可重试 → 切
            "All chat models failed: NotFoundError",       # 错误文本 → 切
            "记得去阳台浇花。",                              # 成功
        ])
        attempts = []

        class Ctx:
            class provider_manager:
                pass

            @staticmethod
            def get_all_providers():
                return []

        plugin.context = Ctx()
        # 直接替换单 provider 的 llm_generate 行为
        async def llm_generate(**kw):
            attempt = next(responses)
            attempts.append(kw["chat_provider_id"])
            if isinstance(attempt, Exception):
                raise attempt
            return types_namespace(completion_text=attempt, result_chain=None)

        chain = [
            ("a", type("P", (), {"meta": staticmethod(lambda: types_namespace(id="a")),
                                 "text_chat": object()})),
            ("b", type("P", (), {"meta": staticmethod(lambda: types_namespace(id="b")),
                                 "text_chat": object()})),
            ("c", type("P", (), {"meta": staticmethod(lambda: types_namespace(id="c")),
                                 "text_chat": object()})),
        ]
        plugin.context = type("Ctx2", (), {
            "provider_manager": type("PM", (), {
                "get_provider_by_id": staticmethod(
                    lambda pid: next(p for i, p in chain if i == pid)
                )
            })(),
            "get_all_providers": staticmethod(lambda: [p for _, p in chain]),
            "llm_generate": staticmethod(llm_generate),
        })()
        text = await plugin._decision_llm_call("提示", None)
        return text, attempts

    text, attempts = asyncio.run(flow())
    assert text == "记得去阳台浇花。"
    assert attempts == ["a", "b", "c"]


def _load_plugin_main():
    import importlib
    import sys
    import types
    from pathlib import Path

    workdir = Path(__file__).resolve().parents[1]
    pkg_name = "living_plugin_under_test"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(workdir)]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    return importlib.import_module(f"{pkg_name}.main")


def _make_plugin_with_ctx(tmp_path):
    main_module = _load_plugin_main()
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.config = dict(BASE_CONFIG)
    plugin.context = None
    plugin.loop = None
    plugin.gate = None
    plugin.mood = main_module.MoodState(db_path=str(tmp_path / "mood.db"))
    plugin._lazy_memory = main_module.LazyMemory(
        context=None, mode_getter=lambda: "simple",
        db_path_getter=lambda: str(tmp_path / "mem.db"),
    )
    return plugin


# ---------------------------------------------------------------------------
# 问题 2：LLM 错误文本不进记忆
# ---------------------------------------------------------------------------
class FakeGate2:
    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


class RecordingMemory:
    def __init__(self):
        self.added = []

    async def add(self, content, importance=0.5, metadata=None,
                  session_id=None, persona_id=None):
        self.added.append({
            "content": content, "importance": importance,
            "session_id": session_id, "persona_id": persona_id,
        })
        return len(self.added)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class ScriptedActivity:
    def __init__(self, name="surf", outcome=None):
        self.name = name
        self.description = "测试活动"
        self.outcome = outcome
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        return self.outcome


def _outcome(summary, memory=None):
    from core.activities import ActivityOutcome

    return ActivityOutcome(name="surf", summary=summary,
                           memory_content=memory or summary, importance=0.5)


# M13-补丁1：活动经历写进对话上下文的观测替身（假号 10001，测试先例）
MASTER_UMO = "aiocqhttp:FriendMessage:10001"
CTX_CONFIG = {
    **BASE_CONFIG,
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": MASTER_UMO},
}


class FakeCtxMgr:
    """AstrBot ConversationManager 替身：记录 add_message_pair 写入对。"""

    def __init__(self):
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return "cid-1"

    async def new_conversation(self, umo):
        return "cid-1"

    async def add_message_pair(self, cid, user_msg, assistant_msg):
        self.pairs.append((cid, user_msg, assistant_msg))


def _m3_loop(activity, memory, mgr=None):
    loop = LivingLoop(
        gate=FakeGate2(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: CTX_CONFIG if mgr is not None else BASE_CONFIG,
        activities=[activity],
        conversation_manager=mgr,
    )
    return loop


def test_llm_error_outcome_replaced_by_failure_narration():
    """问题 2：产出是错误串 → 自述替换为专属失败文案，原错误不落任何落点。

    M13-补丁1：直塞记忆移除，观测点从 memory.add 平移到对话上下文自述。
    """
    memory = RecordingMemory()
    activity = ScriptedActivity(outcome=_outcome(
        "All chat models failed: NotFoundError"))
    mgr = FakeCtxMgr()
    loop = _m3_loop(activity, memory, mgr=mgr)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    assert len(mgr.pairs) == 1
    narration = mgr.pairs[0][2]["content"]
    assert "脑子转不动" in narration
    assert "模型全挂了" in narration
    assert "All chat models failed" not in narration
    assert memory.added == []  # 直塞已移除


def test_clean_outcome_still_written_normally():
    """正常产出不受过滤器影响：自述原样进入上下文。"""
    memory = RecordingMemory()
    activity = ScriptedActivity(outcome=_outcome("今天看了《三体》，很有意思"))
    mgr = FakeCtxMgr()
    loop = _m3_loop(activity, memory, mgr=mgr)

    asyncio.run(loop.run_activity_cycle(NOW))
    narration = mgr.pairs[0][2]["content"]
    assert narration == "今天看了《三体》，很有意思"
    assert memory.added == []


# ---------------------------------------------------------------------------
# 问题 3：记忆写入携带会话与人格上下文（M13-补丁1 后由睡前回顾/梦等
# 直写路径消费，活动直塞已移除——persona 回落语义保留）
# ---------------------------------------------------------------------------
def test_persona_id_falls_back_to_default():
    """persona 取不到 → "default"，读取不阻塞。"""
    memory = RecordingMemory()
    loop = _m3_loop(ScriptedActivity(), memory)

    async def broken():
        raise RuntimeError("persona gone")

    loop._persona_id_getter = broken
    assert asyncio.run(loop._persona_id()) == "default"


def test_persona_id_none_returns_default():
    loop = _m3_loop(ScriptedActivity(), RecordingMemory())
    assert asyncio.run(loop._persona_id()) == "default"  # 未注入 getter
