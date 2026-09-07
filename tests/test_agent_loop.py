"""M3-C 测试：token 硬闸驱动、生活工具集、活动 agent 模式与回退。"""

import asyncio
import types

import pytest

from core.agent_loop import AgentRunResult, drive_agent_steps
from core.activities import (
    ActivityContext,
    MemoryBrowsingActivity,
    MiniGameActivity,
    PeekFeedbackActivity,
    ReadArticleActivity,
    SurfActivity,
)
from core.living_tools import (
    FetchPageTool,
    RememberTool,
    RunPythonTool,
    WebSearchTool,
    build_living_tools,
)


# ---------------------------------------------------------------------------
# token 硬闸驱动（C2 核心，鸭子类型 runner）
# ---------------------------------------------------------------------------
class FakeRunner:
    """模拟 ToolLoopAgentRunner：步进、累计 token、响应 stop。"""

    def __init__(self, steps=3, tokens_per_step=100, final_text="玩好了"):
        self._steps = steps
        self._per = tokens_per_step
        self.stats = types.SimpleNamespace(
            token_usage=types.SimpleNamespace(total=0)
        )
        self.stop_requested = False
        self.final = types.SimpleNamespace(completion_text=final_text)

    async def step_until_done(self, max_steps):
        for i in range(min(self._steps, max_steps)):
            if self.stop_requested:
                return  # 优雅退出：真实 runner 在检查点停止
            self.stats.token_usage.total += self._per
            yield i

    def request_stop(self):
        self.stop_requested = True

    def get_final_llm_resp(self):
        return self.final


def test_drive_under_budget_no_stop():
    runner = FakeRunner(steps=3, tokens_per_step=100)
    steps, exceeded, llm_calls = asyncio.run(
        drive_agent_steps(runner, budget=1000, max_steps=8)
    )
    assert (steps, exceeded) == (3, False)
    assert llm_calls == 3  # 每步 usage 增量 +100，计 3 轮 LLM
    assert runner.stop_requested is False


def test_drive_over_budget_requests_stop():
    """步间检查：累计超预算 → request_stop 被调用，后续步不再执行。"""
    runner = FakeRunner(steps=10, tokens_per_step=100)  # 每步 100
    steps, exceeded, llm_calls = asyncio.run(
        drive_agent_steps(runner, budget=250, max_steps=30)
    )
    assert exceeded is True
    assert runner.stop_requested is True
    assert steps == 3  # 第 3 步后 total=300 ≥ 250 → 优雅退出
    assert runner.stats.token_usage.total == 300


def test_drive_zero_budget_means_unlimited():
    runner = FakeRunner(steps=5, tokens_per_step=10000)
    steps, exceeded, _llm = asyncio.run(
        drive_agent_steps(runner, budget=0, max_steps=8)
    )
    assert (steps, exceeded) == (5, False)


def test_drive_max_steps_cap():
    runner = FakeRunner(steps=50, tokens_per_step=1)
    steps, exceeded, _llm = asyncio.run(
        drive_agent_steps(runner, budget=100000, max_steps=8)
    )
    assert steps == 8 and exceeded is False


# ---------------------------------------------------------------------------
# 生活工具集（C1）
# ---------------------------------------------------------------------------
class FakeSearcher:
    async def search(self, query, count=5, **kw):
        return [
            {"title": f"{query}第一篇", "url": "https://e.com/1", "summary": "摘要一"},
            {"title": f"{query}第二篇", "url": "https://e.com/2", "summary": "摘要二"},
        ]


class FakeFetcher:
    async def fetch(self, url):
        return {"title": "好文章", "text": "正文" * 2000, "status": 200}


class FakeSandbox:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "stdout": "猜中了！", "stderr": ""}
        self.got_code = None

    async def run(self, code, timeout=10):
        self.got_code = code
        return self.result


class FakeMemory:
    def __init__(self, rows=None):
        self.added = []
        self.rows = rows or []

    async def add(self, content, importance=0.5, metadata=None):
        self.added.append((content, importance))
        return len(self.added)

    async def search(self, query, k=5):
        return self.rows[:k]


def _tool_ctx():
    return object()  # 工具不使用 context 参数


def test_web_search_tool_formats_results():
    tool = WebSearchTool().bind(FakeSearcher())
    text = asyncio.run(tool.call(_tool_ctx(), query="深海生物"))
    assert "深海生物第一篇" in text and "https://e.com/1" in text

    class EmptySearcher:
        async def search(self, query, count=5, **kw):
            return []

    text2 = asyncio.run(WebSearchTool().bind(EmptySearcher()).call(_tool_ctx(), query="虚无"))
    assert "没有结果" in text2


def test_web_search_tool_requires_query():
    tool = WebSearchTool().bind(FakeSearcher())
    text = asyncio.run(tool.call(_tool_ctx(), query="  "))
    assert "错误" in text


def test_fetch_page_tool_truncates():
    tool = FetchPageTool().bind(FakeFetcher())
    text = asyncio.run(tool.call(_tool_ctx(), url="https://e.com/a"))
    assert "好文章" in text and len(text) < 2000  # 正文被截断
    bad = asyncio.run(tool.call(_tool_ctx(), url="javascript:alert(1)"))
    assert "错误" in bad


def test_run_python_tool_passes_code_and_formats():
    sandbox = FakeSandbox()
    tool = RunPythonTool().bind(sandbox)
    text = asyncio.run(tool.call(_tool_ctx(), code="print('hi')"))
    assert sandbox.got_code == "print('hi')"
    assert "猜中了" in text

    refused = RunPythonTool().bind(FakeSandbox(
        result={"ok": False, "stdout": "", "stderr": "", "refused_reason": "禁止 import"}
    ))
    text2 = asyncio.run(refused.call(_tool_ctx(), code="import os"))
    assert "拒绝" in text2


def test_remember_tool_writes_with_clamped_importance():
    memory = FakeMemory()
    tool = RememberTool().bind(lambda: asyncio.sleep(0, result=memory))
    asyncio.run(tool.call(_tool_ctx(), text="今天很开心", importance=5))
    assert memory.added == [("今天很开心", 1.0)]  # 5 → 钳到 1.0


def test_build_living_tools_skips_missing_abilities():
    """能力未注入的工具不注册（工具消失比工具报错更安静）。"""
    toolset = build_living_tools(searcher=None, fetcher=None, sandbox=None,
                                 memory_getter=None)
    assert toolset.empty()
    toolset2 = build_living_tools(searcher=FakeSearcher(), fetcher=None,
                                  sandbox=None, memory_getter=None)
    assert [t.name for t in toolset2.tools] == ["web_search"]


# ---------------------------------------------------------------------------
# 活动 agent 模式与回退（C1）
# ---------------------------------------------------------------------------
def _ctx_with_agent(agent, searcher=None, **kw):
    return ActivityContext(
        searcher=searcher or FakeSearcher(),
        fetcher=FakeFetcher(),
        sandbox=FakeSandbox(),
        memory=FakeMemory(),
        gate=None,
        event=None,
        rng=__import__("random").Random(7),
        params=kw.get("params", {}),
        agent=agent,
    )


def _ok_result(text="我看到一篇讲深海热泉的文章，很有意思", capped=False):
    return AgentRunResult(ok=True, text=text, tokens_used=1500, max_steps=8,
                          steps_used=2)


def test_surf_agent_mode_produces_outcome():
    outcomes = []

    async def agent(intent):
        outcomes.append(intent)
        return _ok_result()

    ctx = _ctx_with_agent(agent)
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.agent_mode is True
    assert "很有意思" in outcome.summary
    assert "深海热泉" in outcome.memory_content
    assert "web_search" in outcomes[0]  # intent 告诉 agent 用什么工具


def test_surf_agent_exception_falls_back_to_script():
    """agent 异常 → 回退脚本模式（M1 保底，不空转）。"""

    async def agent(intent):
        raise RuntimeError("provider down")

    ctx = _ctx_with_agent(agent)
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.agent_mode is False  # 脚本模式产出
    assert "搜了「" in outcome.summary


def test_surf_agent_no_output_falls_back():
    async def agent(intent):
        return AgentRunResult(ok=False, error="没产出")

    ctx = _ctx_with_agent(agent)
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.agent_mode is False


def test_budget_exceeded_records_partial_without_script_double_spend():
    """超预算中断：记半程经历，不回退脚本二次消费（任务书 C2）。"""
    search_calls = []

    class CountingSearcher(FakeSearcher):
        async def search(self, query, count=5, **kw):
            search_calls.append(query)
            return await super().search(query, count=count, **kw)

    async def agent(intent):
        return AgentRunResult(
            ok=False, text="刚搜到一点", budget_exceeded=True, tokens_used=20000,
            max_steps=8,
        )

    searcher = CountingSearcher()
    ctx = _ctx_with_agent(agent, searcher=searcher)
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.agent_mode is True
    assert "叫停" in outcome.memory_content
    assert outcome.importance == 0.3
    assert search_calls == []  # 脚本模式没有再次执行


def test_game_agent_mode_capped_adds_played_long_time_note():
    async def agent(intent):
        return AgentRunResult(
            ok=True, text="写了个贪吃蛇，蛇撞墙了，哈哈", tokens_used=3000,
            steps_used=8, max_steps=8,  # 跑满步数
        )

    ctx = _ctx_with_agent(agent)
    outcome = asyncio.run(MiniGameActivity().run(ctx))
    assert "玩了很久" in outcome.memory_content


def test_peek_and_reminisce_never_use_agent():
    """peek/reminisce 保持脚本模式：即使 ctx.agent 存在也不调用（任务书 C1）。"""

    async def agent(intent):
        raise AssertionError("peek/reminisce 不应进入 agent 模式")

    gate_calls = []

    class Gate:
        async def should_send_message(self, now=None):
            gate_calls.append(1)
            return False, "quiet"

    ctx = _ctx_with_agent(agent)
    ctx.gate = Gate()
    peek = asyncio.run(PeekFeedbackActivity().run(ctx))
    assert peek.summary is None and gate_calls == [1]

    memory = FakeMemory(rows=[{"content": "旧回忆", "score": 1}])
    ctx2 = _ctx_with_agent(agent)
    ctx2.memory = memory
    reminisce = asyncio.run(MemoryBrowsingActivity().run(ctx2))
    assert "旧回忆" in reminisce.memory_content


def test_agent_intent_carries_params_hint():
    captured = []

    async def agent(intent):
        captured.append(intent)
        return _ok_result()

    ctx = _ctx_with_agent(agent, params={"topic": "独立游戏"})
    asyncio.run(ReadArticleActivity().run(ctx))
    assert "独立游戏" in captured[0]
