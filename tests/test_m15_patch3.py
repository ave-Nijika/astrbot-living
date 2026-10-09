"""M15-补丁3：搜索开关贯彻到底 + free 活动修复。

对应任务书 T1-T6（T7 全量基线由本地全量回归保证，不在本文件）。
测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import json
import random
import types
from pathlib import Path

from core.activities import (
    FreeActivity,
    ActivityContext,
    SurfActivity,
    activities_excluding_search,
    default_activities,
)
from core.agent_loop import AgentRunResult
from core.autonomy import build_tool_manifest
from core.living_loop import DEFAULT_AGENT_ACTIVITIES, LivingLoop

MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份


class FakeSandbox:
    def __init__(self, result=None):
        self._result = result or {"ok": True, "stdout": "得分 3", "stderr": ""}
        self.got_code = None

    async def run(self, code, timeout=10):
        self.got_code = code
        return dict(self._result)


def _ctx(agent=None, *, searcher=object(), fetcher=object(),
         sandbox=object(), memory=object(), search_enabled=True, params=None):
    return ActivityContext(
        searcher=searcher, fetcher=fetcher, sandbox=sandbox,
        memory=memory, gate=None, event=None, rng=random.Random(7),
        params=params or {}, agent=agent, search_enabled=search_enabled,
    )


# ---------------------------------------------------------------------------
# T1：agent 通道可用时 free 走 agent 模式（不再降级）
# ---------------------------------------------------------------------------
def test_free_uses_agent_channel_when_available():
    intents = []

    async def agent(intent):
        intents.append(intent)
        return AgentRunResult(ok=True, text="随手记了条想法", tokens_used=100,
                              max_steps=8)

    outcome = asyncio.run(FreeActivity().run(_ctx(agent=agent)))
    assert outcome.agent_mode is True
    assert outcome.name == "free"
    assert len(intents) == 1  # 只跑一次 agent，不回退脚本


# ---------------------------------------------------------------------------
# T2：agent 通道不可用 → 绝不落回 surf，无误导性经历
# ---------------------------------------------------------------------------
def test_free_fallback_without_agent_never_hits_surf():
    # 搜索关闭 + 沙箱可用 → 降级 game（真实自娱经历，不提搜索）
    outcome = asyncio.run(FreeActivity().run(
        _ctx(agent=None, sandbox=FakeSandbox(), search_enabled=False)))
    assert outcome.name == "game"
    assert outcome.agent_mode is False
    assert outcome.memory_content and "搜索" not in outcome.memory_content
    assert "搜索" not in outcome.summary

    # 搜索开着也一样不落 surf（free 的兜底语义与搜索开关无关）
    outcome2 = asyncio.run(FreeActivity().run(
        _ctx(agent=None, sandbox=FakeSandbox(), search_enabled=True)))
    assert outcome2.name == "game"


def test_free_fallback_without_any_tool_admits_nothing_done():
    """搜索关闭且无合适非搜索降级目标（连沙箱都没有）→ 老实承认没做成。"""
    outcome = asyncio.run(FreeActivity().run(
        _ctx(agent=None, sandbox=None, search_enabled=False)))
    assert outcome.name == "free"
    assert "没做成" in outcome.memory_content
    assert "搜索" not in outcome.memory_content
    assert "搜索" not in outcome.summary
    assert outcome.importance < 0.3


# ---------------------------------------------------------------------------
# T3：agent_intent 的工具清单按实际能力动态生成
# ---------------------------------------------------------------------------
def test_free_intent_tools_line_follows_capabilities():
    off = FreeActivity().agent_intent(_ctx(search_enabled=False))
    assert "web_search" not in off
    assert "fetch_page" in off and "run_python" in off and "remember" in off

    on = FreeActivity().agent_intent(_ctx(search_enabled=True))
    assert "web_search" in on

    # 各能力全缺 → 清单为空时的措辞自洽
    none_avail = FreeActivity().agent_intent(_ctx(
        searcher=None, fetcher=None, sandbox=None, memory=None,
        search_enabled=True))
    assert "web_search" not in none_avail and "fetch_page" not in none_avail
    assert "没有可用工具" in none_avail


# ---------------------------------------------------------------------------
# T4：description 中性化（方案二），契约不破
# ---------------------------------------------------------------------------
def test_free_description_is_neutral_and_contract_intact():
    d = FreeActivity().description
    assert "搜索" not in d and "读文章" not in d and "写代码" not in d
    # decider 活动清单（- name: description）仍按静态属性渲染
    lines = [f"- {a.name}: {a.description}" for a in default_activities()]
    assert any(line.startswith("- free: ") and "自由时间" in line
               for line in lines)


# ---------------------------------------------------------------------------
# T5：默认白名单含 free（常量与 schema default 一致）+ 接线生效
# ---------------------------------------------------------------------------
def test_default_whitelist_includes_free():
    assert DEFAULT_AGENT_ACTIVITIES == ("surf", "read", "game", "free")
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json")
        .read_text(encoding="utf-8")
    )
    default = (schema["advanced"]["items"]["decision"]["items"]
               ["agent_activities"]["default"])
    assert default == list(DEFAULT_AGENT_ACTIVITIES)


def test_agent_callable_resolves_free_by_default():
    """未配置 agent_activities 时 free 能拿到 agent 通道（默认值生效）。"""
    sentinel_run = lambda intent: None  # noqa: E731
    loop = LivingLoop(
        gate=types.SimpleNamespace(),
        memory_getter=lambda: asyncio.sleep(0, result=object()),
        config_getter=lambda: {}, activities=[],
        agent_loop=types.SimpleNamespace(run=sentinel_run),
    )
    assert loop._agent_callable("free") is sentinel_run


# ---------------------------------------------------------------------------
# T6：回归——活动池剔除 / surf·read 优雅降级语义 / manifest 同口径
# ---------------------------------------------------------------------------
def test_pool_exclusion_unchanged():
    cfg = {"capabilities": {"web_search_enabled": False}}
    names = [a.name for a in activities_excluding_search(default_activities(), cfg)]
    assert "surf" not in names and "read" not in names
    assert "free" in names and "game" in names

    cfg_on = {"capabilities": {"web_search_enabled": True}}
    names_on = [a.name for a in
                activities_excluding_search(default_activities(), cfg_on)]
    assert "surf" in names_on and "read" in names_on


def test_surf_read_graceful_skip_semantics_unchanged():
    """surf 的 search_enabled 优雅降级保持（M15-补丁1 E4 不回退）：
    agent 无产出回退脚本后，脚本按未搜索优雅收场、不报错。"""
    async def dead_agent(intent):
        return None  # agent 模式无产出 → 回退脚本

    ctx = _ctx(agent=dead_agent, search_enabled=False)
    outcome = asyncio.run(SurfActivity().run(ctx))
    assert outcome.name == "surf"
    assert "搜索功能关着" in outcome.summary
    assert "没搜成" in outcome.memory_content  # 优雅收场文案仍在（不是报错）


def test_tool_manifest_honors_search_switch():
    """顺手项：清单 vs 实际挂载同口径——搜索关闭时清单不列 web_search。"""
    assert "web_search" in build_tool_manifest(0)
    off = build_tool_manifest(0, has_search=False)
    assert "web_search" not in off
    assert "fetch_page" in off and "run_python" in off and "remember" in off
    # 其余档位清单不受影响（M23-补丁1：shell 只在 tier>=4；M29-补丁1 起
    # 挂载与 write_level 解耦，清单不再收 write_level）
    assert "local_shell" in build_tool_manifest(4, has_search=False)
    assert "local_shell" not in build_tool_manifest(3, has_search=False)
