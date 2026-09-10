"""M3 补丁 III 测试：图谱条目补全——活动产出带 topics，metadata 进记忆。"""

import asyncio
from datetime import datetime
from typing import Any

from core.activities import (
    ActivityContext,
    ActivityOutcome,
    MemoryBrowsingActivity,
    MiniGameActivity,
    ReadArticleActivity,
    SurfActivity,
)
from core.living_loop import LivingLoop

NOW = datetime(2026, 9, 9, 14, 0, 0)

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 45, "max_run_seconds": 300},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": "", "quiet_hours": ""},
}


class FakeSearcher:
    def __init__(self):
        self.queries = []

    async def search(self, query, count=5, **kw):
        self.queries.append(query)
        return [{"title": f"{query}相关", "url": "https://e.com/1", "summary": "s"}]


class FakeFetcher:
    async def fetch(self, url):
        return {"title": "好文章", "text": "正文内容", "status": 200}


class RecordingMemory:
    """捕获 add 的全部参数——metadata 是本补丁的验证重点。"""

    def __init__(self):
        self.calls = []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.calls.append({"content": content, "importance": importance,
                           "metadata": metadata})
        return len(self.calls)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


def make_ctx(params=None, sandbox=None):
    import random

    from core.sandbox import Sandbox

    return ActivityContext(
        searcher=FakeSearcher(),
        fetcher=FakeFetcher(),
        sandbox=sandbox or Sandbox(),
        memory=RecordingMemory(),
        gate=None,
        event=None,
        rng=random.Random(7),
        now=NOW,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# 活动产出带 topics
# ---------------------------------------------------------------------------
def test_surf_outcome_topics_match_search_topic():
    """surf：topics = 实际搜索的主题词（不是随机池里的别的词）。"""
    ctx = make_ctx(params={"topic": "深海生物"})
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert ctx.searcher.queries == ["深海生物"]
    assert outcome.topics == ["深海生物"]


def test_read_outcome_topics_match_search_topic():
    ctx = make_ctx(params={"topic": "咖啡文化"})
    outcome = asyncio.run(ReadArticleActivity().run(ctx))
    assert outcome.topics == ["咖啡文化"]


def test_game_outcome_topics_include_game_name():
    """game：topics = ["小游戏", 具体游戏名]。"""
    ctx = make_ctx()
    outcome = asyncio.run(MiniGameActivity().run(ctx))
    assert outcome.topics is not None
    assert outcome.topics[0] == "小游戏"
    assert len(outcome.topics) == 2  # 第二个是具体游戏名（如"二分猜数字"）


def test_reminisce_outcome_topics():
    ctx = make_ctx()
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert outcome.topics == ["记忆整理"]


def test_agent_mode_outcome_topics_from_params():
    """agent 模式：topics 取决策 params 里的偏好。"""

    async def agent(intent):
        return AgentResultStub(ok=True, text="看完了", tokens_used=100,
                               max_steps=8, steps_used=1)

    ctx = make_ctx(params={"topic": "系外行星"})
    ctx.agent = agent
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.agent_mode is True
    assert outcome.topics == ["系外行星"]


class AgentResultStub:
    def __init__(self, ok, text, tokens_used, max_steps, steps_used):
        self.ok = ok
        self.text = text
        self.tokens_used = tokens_used
        self.max_steps = max_steps
        self.steps_used = steps_used

    @property
    def budget_exceeded(self):
        return False

    @property
    def capped_at_max_steps(self):
        return False


# ---------------------------------------------------------------------------
# _write_memory 传 metadata
# ---------------------------------------------------------------------------
class FakeGate2:
    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_send_message(self, now=None):
        return False, "blocked"

    async def note_activity_started(self, now=None):
        pass

    async def note_activity_finished(self, now=None):
        pass

    async def close(self):
        pass


def _loop_with(activity, memory):
    return LivingLoop(
        gate=FakeGate2(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: BASE_CONFIG,
        abilities={"searcher": FakeSearcher(), "fetcher": FakeFetcher()},
        activities=[activity],
    )


class ParamsInjectingActivity:
    """把决策 params 注入上下文后跑真实 surf（模拟 decider → params 链路）。"""

    name = "surf"

    def __init__(self, params):
        self.params = params
        self._surf = SurfActivity()

    async def run(self, ctx):
        ctx.params = dict(self.params)
        return await self._surf.run(ctx)


def test_write_memory_metadata_contains_topics():
    """活动成功 → memory.add 的 metadata 带 topics 字段。"""
    memory = RecordingMemory()
    activity = ParamsInjectingActivity({"topic": "冷知识"})
    loop = _loop_with(activity, memory)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert memory.calls, "记忆应被写入"
    topics = memory.calls[0]["metadata"].get("topics")
    assert topics == ["冷知识"]


def test_failure_path_metadata_has_no_topics():
    """失败路径：outcome 为 None → metadata 为空 dict（裸 fact 可接受）。"""
    memory = RecordingMemory()

    class FailingActivity:
        name = "read"

        async def run(self, ctx):
            raise RuntimeError("搜索挂了")

    loop = _loop_with(FailingActivity(), memory)
    asyncio.run(loop.run_activity_cycle(NOW))
    assert len(memory.calls) == 1
    record = memory.calls[0]
    assert "没成" in record["content"]  # 失败记忆照写
    assert record["metadata"] == {}  # 无 topics 可挂
