"""活动池测试（任务书 M1-D）：每个活动可独立测试（fake 能力注入）。"""

import asyncio
import random
from datetime import datetime

import pytest

from core.activities import (
    MemoryBrowsingActivity,
    MiniGameActivity,
    PeekFeedbackActivity,
    ReadArticleActivity,
    SurfActivity,
    ActivityContext,
    default_activities,
)
from core.sandbox import Sandbox


# ---------------------------------------------------------------------------
# 能力替身
# ---------------------------------------------------------------------------
class FakeSearcher:
    def __init__(self, results=None, error=None):
        # results=None 表示"用默认结果"；results=[] 表示"真的一无所获"
        self.results = results
        self.error = error
        self.queries = []

    async def search(self, query, count=5, **kw):
        self.queries.append(query)
        if self.error:
            raise self.error
        if self.results is not None:
            return self.results
        return [
            {"title": f"关于{query}的文章", "url": "https://example.com/a", "summary": "s"}
        ]


class FakeFetcher:
    def __init__(self, page=None, error=None):
        self.page = page or {
            "title": "一篇有意思的文章",
            "text": "正文第一段讲了很多东西。\n第二段继续讲。",
            "status": 200,
        }
        self.error = error

    async def fetch(self, url):
        if self.error:
            raise self.error
        return self.page


class FakeSandbox:
    def __init__(self, ok=True, stdout="猜中了！答案是 42，用了 7 次"):
        self.ok = ok
        self.stdout = stdout

    async def run(self, code, timeout=10):
        return {
            "ok": self.ok,
            "stdout": self.stdout if self.ok else "",
            "stderr": "",
            "refused_reason": None if self.ok else "拒绝",
        }


class FakeMemory:
    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []

    async def add(self, content, importance=0.5, metadata=None):
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return self.rows[:k]

    async def close(self):
        pass


class FakeGate:
    def __init__(self, allow=True):
        self.allow = allow
        self.calls = 0

    async def should_send_message(self, now=None):
        self.calls += 1
        return self.allow, "ok" if self.allow else "blocked"


NOW = datetime(2026, 9, 7, 14, 0, 0)
RNG = random.Random(20260907)


def make_ctx(searcher=None, fetcher=None, sandbox=None, memory=None, gate=None):
    return ActivityContext(
        searcher=searcher or FakeSearcher(),
        fetcher=fetcher or FakeFetcher(),
        sandbox=sandbox or FakeSandbox(),
        memory=memory or FakeMemory(),
        gate=gate or FakeGate(),
        event=None,  # 活动层不使用幽灵事件，M2 agent 循环才接
        rng=random.Random(7),
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 各活动
# ---------------------------------------------------------------------------
def test_surf_activity_produces_summary_and_memory():
    ctx = make_ctx(searcher=FakeSearcher(results=[
        {"title": "Next.js 16 发布", "url": "https://e.com/1", "summary": ""},
        {"title": "另见", "url": "https://e.com/2", "summary": ""},
    ]))
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert "搜了「" in outcome.summary
    assert outcome.memory_content
    assert "9月7日" in outcome.memory_content  # 带日期感
    assert outcome.memory_content.startswith("9月7日我搜了")


def test_surf_activity_raises_on_empty_results():
    ctx = make_ctx(searcher=FakeSearcher(results=[]))
    with pytest.raises(RuntimeError):
        asyncio.run(SurfActivity().run(ctx))


def test_read_activity_fetches_and_digests():
    fetcher = FakeFetcher()
    ctx = make_ctx(fetcher=fetcher)
    outcome = asyncio.run(ReadArticleActivity().run(ctx))
    assert "读了《一篇有意思的文章》" == outcome.summary
    assert "印象最深的是" in outcome.memory_content


def test_read_activity_raises_without_url():
    ctx = make_ctx(searcher=FakeSearcher(results=[{"title": "t", "url": "", "summary": ""}]))
    with pytest.raises(RuntimeError):
        asyncio.run(ReadArticleActivity().run(ctx))


def test_game_activity_runs_in_real_sandbox():
    """真沙箱集成：模板只用白名单库，应当真实执行并返回 stdout。"""
    ctx = make_ctx(sandbox=Sandbox())
    outcome = asyncio.run(MiniGameActivity().run(ctx))
    assert "试玩" in outcome.summary
    assert outcome.memory_content


def test_game_activity_raises_when_sandbox_rejects():
    ctx = make_ctx(sandbox=FakeSandbox(ok=False))
    with pytest.raises(RuntimeError, match="没跑起来"):
        asyncio.run(MiniGameActivity().run(ctx))


def test_reminisce_activity_with_rows():
    mem = FakeMemory(rows=[{"id": 1, "content": "9月1日我看了场日落", "score": 0}])
    ctx = make_ctx(memory=mem)
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert "翻到一条：9月1日我看了场日落" in outcome.memory_content


def test_reminisce_activity_empty_memory():
    ctx = make_ctx(memory=FakeMemory(rows=[]))
    outcome = asyncio.run(MemoryBrowsingActivity().run(ctx))
    assert "一片空白" in outcome.summary
    assert "空" in outcome.memory_content  # 记忆里也要记下"今天没翻到东西"


def test_peek_activity_exercises_gate_but_stays_silent():
    """看评价（弱触发）：只空走闸门，无产出、不写记忆。"""
    gate = FakeGate(allow=True)
    ctx = make_ctx(gate=gate)
    outcome = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert gate.calls == 1
    assert outcome.summary is None
    assert outcome.memory_content is None


def test_activity_pool_has_five_activities():
    pool = default_activities()
    assert len(pool) == 5
    assert len({a.name for a in pool}) == 5


def test_game_templates_pass_static_scan():
    """小游戏模板必须能过沙箱静态扫描（白名单库），否则活动必败。"""
    from core.sandbox import static_scan

    from core.activities import _GAME_TEMPLATES

    for name, code in _GAME_TEMPLATES:
        static_scan(code)  # 不抛即通过
