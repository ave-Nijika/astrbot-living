"""M15-补丁1：晚安双档 + 聊天中不入睡 + 截图能看 + 工具复用 + 搜索开关。

对应任务书 T1-T10（T11 全量基线由本地全量回归保证，不在本文件）。
测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import base64
import json
import logging
import re
import types
from datetime import datetime
from pathlib import Path

import pytest

from core.activities import (
    SEARCH_DEPENDENT_ACTIVITIES,
    ActivityContext,
    default_activities,
    web_search_enabled,
)
from core.decider import ActivityDecider
from core.living_loop import LivingLoop
from core.living_state import LivingGate
from core.living_tools import (
    BrowserScreenshotTool,
    build_living_tools,
    provider_supports_image,
)
from core.mood import MoodState
from core.sleep import SleepManager

NIGHT = datetime(2026, 9, 22, 5, 50, 0)   # 昼夜窗内 → 长睡路径
NOON = datetime(2026, 9, 22, 14, 0, 0)    # 昼夜窗外 → 小睡路径
MASTER_SESSION = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份


# ---------------------------------------------------------------------------
# 复用件（与 test_m6_patch1 同款最小装配）
# ---------------------------------------------------------------------------
class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        return True


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5, **kwargs):
        return []

    async def add(self, content, importance=0.5, metadata=None, **kwargs):
        self.added.append(content)
        return len(self.added)


class RecordingGate(LivingGate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_kinds = []

    async def enter_autonomous_sleep(self, until, kind, now=None):
        self.entered_kinds.append(kind)
        await super().enter_autonomous_sleep(until, kind, now)


async def _make_loop(tmp_path, config, gate, sender, persona=None):
    mood = MoodState(db_path=str(tmp_path / "mood.db"))
    await mood.load()
    mood.energy = 0.05
    manager = SleepManager(
        config_getter=lambda: config, gate=gate, mood=mood, rng=lambda: 0.5
    )

    async def _none():
        return None

    loop = LivingLoop(
        gate=gate,
        memory_getter=lambda: asyncio.sleep(0, result=FakeMemory()),
        config_getter=lambda: config,
        sleep_manager=manager,
        mood=mood,
        sender=sender,
        rng=lambda: 0.0,
        persona_getter=persona,
    )
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop, mood


async def _teardown(loop, mood, gate):
    await mood.close()
    await gate.close()


SLEEP_CONFIG = {
    "decision": {"daily_impulse_limit": 3},
    "sleep": {
        "sleepiness_threshold": 0.0, "sleepiness_jitter": 0.0,
        "min_awake_minutes": 0, "circadian_hint": "23:00-07:00",
        "sleep_farewell_message": "我先去睡了，晚安。",
        "farewell_probability": 1.0,
    },
}


# ---------------------------------------------------------------------------
# T1：A2 概率档（命中/未命中/边界）
# ---------------------------------------------------------------------------
def test_farewell_probability_hit_and_miss(tmp_path):
    config = json.loads(json.dumps(SLEEP_CONFIG))
    config["sleep"]["farewell_probability"] = 0.5

    async def flow(roll):
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / f"g{roll}.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        loop._rng = lambda: roll
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._send_sleep_farewell(NIGHT)
        await _teardown(loop, mood, gate)
        return sender.sent

    assert asyncio.run(flow(0.1)) == [(MASTER_SESSION, "我先去睡了，晚安。")]
    assert asyncio.run(flow(0.9)) == []  # 未命中不发


def test_farewell_probability_boundaries(tmp_path):
    """概率 0 永不发（即使掷点 0）；概率 1 必发（即使掷点 0.99）。"""
    config = json.loads(json.dumps(SLEEP_CONFIG))

    async def flow(prob, roll):
        config["sleep"]["farewell_probability"] = prob
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / f"g{prob}-{roll}.db"),
                             rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        loop._rng = lambda: roll
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._send_sleep_farewell(NIGHT)
        await _teardown(loop, mood, gate)
        return sender.sent

    assert asyncio.run(flow(0.0, 0.0)) == []
    assert asyncio.run(flow(1.0, 0.99)) == [(MASTER_SESSION, "我先去睡了，晚安。")]


def test_farewell_mode_off_and_unknown(tmp_path):
    """off 档从不发；未知档位回落 probability（默认投骰）。"""
    config = json.loads(json.dumps(SLEEP_CONFIG))

    async def flow(mode):
        config["sleep"]["farewell_mode"] = mode
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / f"m{mode}.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._send_sleep_farewell(NIGHT)
        await _teardown(loop, mood, gate)
        return sender.sent

    assert asyncio.run(flow("off")) == []
    # 未知档 → 概率档，probability=1.0 + rng=0.0 → 必发
    assert asyncio.run(flow("whatever")) == [(MASTER_SESSION, "我先去睡了，晚安。")]


# ---------------------------------------------------------------------------
# T2：A3/A4 LLM 档（SKIP/正常/异常 + 双写 + prompt 材料）
# ---------------------------------------------------------------------------
def _wire_llm(loop, reply, captured, fail=False):
    async def fake_llm(prompt, system=None):
        captured["prompt"] = prompt
        captured["system"] = system
        if fail:
            raise RuntimeError("llm down")
        return reply

    loop._dream_llm_call = fake_llm
    loop._resolve_target_sessions = lambda: ([MASTER_SESSION], "测试")

    async def fake_ctx(sessions):
        return [
            {"role": "user", "content": "今天聊宇宙超开心"},
            {"role": "assistant", "content": "是呀"},
        ]

    loop._load_chat_contexts = fake_ctx


def test_farewell_llm_send_and_double_write(tmp_path):
    config = json.loads(json.dumps(SLEEP_CONFIG))
    config["sleep"]["farewell_mode"] = "llm"

    async def flow():
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / "g.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)

        async def persona():
            return "你是测试人格。"

        captured = {}
        writes = []

        async def fake_write(text, key, user_msg, label="活动经历"):
            writes.append((text, key, user_msg, label))

        loop._write_speech_to_stores = fake_write
        loop._persona_getter = persona
        _wire_llm(loop, "晚安，今天聊得很开心，好梦。", captured)
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._send_sleep_farewell(NIGHT)
        await _teardown(loop, mood, gate)
        return sender.sent, writes, captured

    sent, writes, captured = asyncio.run(flow())
    assert sent == [(MASTER_SESSION, "晚安，今天聊得很开心，好梦。")]
    # A4：LLM 档发出即双写（M13 落点），标签与幂等键形态
    assert len(writes) == 1
    text, key, user_msg, label = writes[0]
    assert text == "晚安，今天聊得很开心，好梦。"
    assert label == "晚安道别"
    assert key.startswith("#farewell:")
    # A3：prompt 含心境摘要、今天聊天上下文、当前时间；system 带人格
    assert "你现在的状态" in captured["prompt"]
    assert "今天聊宇宙超开心" in captured["prompt"]
    assert "2026-09-22" in captured["prompt"]
    assert "气头上" in captured["prompt"] and "SKIP" in captured["prompt"]
    assert captured["system"] == "你是测试人格。"


def test_farewell_llm_skip_and_exception_silent(tmp_path):
    config = json.loads(json.dumps(SLEEP_CONFIG))
    config["sleep"]["farewell_mode"] = "llm"

    async def flow(reply, fail):
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / f"g{fail}.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        captured = {}
        writes = []

        async def fake_write(text, key, user_msg, label="活动经历"):
            writes.append(text)

        loop._write_speech_to_stores = fake_write
        _wire_llm(loop, reply, captured, fail=fail)
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._send_sleep_farewell(NIGHT)
        await _teardown(loop, mood, gate)
        return sender.sent, writes

    # SKIP / 空 / 异常：一律静默不发、不双写
    assert asyncio.run(flow("SKIP", False)) == ([], [])
    assert asyncio.run(flow("", False)) == ([], [])
    sent, writes = asyncio.run(flow("随便", True))
    assert sent == [] and writes == []


# ---------------------------------------------------------------------------
# T3：小睡不触发告别（现状回归）
# ---------------------------------------------------------------------------
def test_farewell_not_sent_on_nap(tmp_path):
    nap_config = json.loads(json.dumps(SLEEP_CONFIG))
    nap_config["sleep"].update({
        "sleepiness_threshold": 0.99,  # 长睡不可达
        "circadian_hint": "03:00-04:00",  # NOON 在窗外 → 小睡放行
        "nap_cooldown_minutes": 0,
        "nap_max_per_day": 2,
    })

    async def flow():
        gate = RecordingGate(config_getter=lambda: nap_config,
                             db_path=str(tmp_path / "g.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, nap_config, gate, sender)
        loop._sleep_manager.last_active_session = MASTER_SESSION
        await loop._autonomous_sleep_tick(NOON)  # 小睡 enter
        sent = list(sender.sent)
        await _teardown(loop, mood, gate)
        return gate.entered_kinds, sent

    kinds, sent = asyncio.run(flow())
    assert kinds == ["nap"]
    assert sent == []  # 小睡不告别（长睡才会）


# ---------------------------------------------------------------------------
# T4：B 组待机期保护三路
# ---------------------------------------------------------------------------
def test_standby_blocks_long_sleep(tmp_path):
    config = json.loads(json.dumps(SLEEP_CONFIG))

    async def flow(standby):
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / f"s{standby}.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        loop._sleep_manager.last_active_session = MASTER_SESSION
        if standby:
            await gate.refresh_awake_until(30, NIGHT)
        await loop._autonomous_sleep_tick(NIGHT)
        asleep = gate.sleep_state(NIGHT)["asleep"]
        sent = list(sender.sent)
        await _teardown(loop, mood, gate)
        return gate.entered_kinds, asleep, sent

    # 待机活跃：不入睡（长睡不入），也不告别
    kinds, asleep, sent = asyncio.run(flow(True))
    assert kinds == [] and asleep is False and sent == []
    # 待机过期：正常入睡 + 概率必中 → 告别照发
    kinds, asleep, sent = asyncio.run(flow(False))
    assert kinds == ["long"] and asleep is True
    assert sent == [(MASTER_SESSION, "我先去睡了，晚安。")]


def test_standby_blocks_nap(tmp_path):
    nap_config = json.loads(json.dumps(SLEEP_CONFIG))
    nap_config["sleep"].update({
        "sleepiness_threshold": 0.99,
        "circadian_hint": "03:00-04:00",
        "nap_cooldown_minutes": 0,
        "nap_max_per_day": 2,
    })

    async def flow(standby):
        gate = RecordingGate(config_getter=lambda: nap_config,
                             db_path=str(tmp_path / f"n{standby}.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, nap_config, gate, sender)
        if standby:
            await gate.refresh_awake_until(30, NOON)
        await loop._autonomous_sleep_tick(NOON)
        kinds, asleep = gate.entered_kinds, gate.sleep_state(NOON)["asleep"]
        await _teardown(loop, mood, gate)
        return kinds, asleep

    # 待机活跃：长睡与小睡都不入（B1）
    kinds, asleep = asyncio.run(flow(True))
    assert kinds == [] and asleep is False
    # 待机过期：小睡恢复
    kinds, asleep = asyncio.run(flow(False))
    assert kinds == ["nap"] and asleep is True


def test_standby_blocks_sleep_switch_off(tmp_path):
    """B2：开关关闭 → 回到现状（待机也照睡，可回退）。"""
    config = json.loads(json.dumps(SLEEP_CONFIG))
    config["sleep"]["standby_blocks_sleep"] = False

    async def flow():
        gate = RecordingGate(config_getter=lambda: config,
                             db_path=str(tmp_path / "g.db"), rng=lambda: 0.5)
        sender = FakeSender()
        loop, mood = await _make_loop(tmp_path, config, gate, sender)
        await gate.refresh_awake_until(30, NIGHT)  # 待机活跃，但保护已关
        await loop._autonomous_sleep_tick(NIGHT)
        asleep = gate.sleep_state(NIGHT)["asleep"]
        await _teardown(loop, mood, gate)
        return gate.entered_kinds, asleep

    kinds, asleep = asyncio.run(flow())
    assert kinds == ["long"] and asleep is True


# ---------------------------------------------------------------------------
# T5：C1/C2 文档存在性断言（README 五要素 + 面板同源措辞）
# ---------------------------------------------------------------------------
README_REQUIRED = [
    "浏览器能力（可选安装）",
    "Chromium",                     # ① 需要装什么
    "打开网页", "截图",              # ② 有什么作用
    "不挂载", "搜索 + 读文本",        # ③ 不装会怎样
    "playwright install chromium",  # ④ 怎么装
    "playwright uninstall chromium",  # ⑤ 怎么卸载 + 自动回落
    "自动回落",
]

PANEL_REQUIRED = [
    "浏览器能力（可选安装）",
    "Chromium",
    "搜索 + 读文本",
    "playwright install chromium",
    "playwright uninstall chromium",
    "已安装",
    "未安装（浏览器工具不可用）",
]


def test_readme_browser_section():
    text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(
        encoding="utf-8"
    )
    for keyword in README_REQUIRED:
        assert keyword in text, f"README 缺少浏览器章节要素: {keyword}"


def test_panel_browser_wording_same_source():
    app_js = (
        Path(__file__).resolve().parents[1] / "pages" / "config" / "app.js"
    ).read_text(encoding="utf-8")
    for keyword in PANEL_REQUIRED:
        assert keyword in app_js, f"面板说明缺少同源措辞: {keyword}"


# ---------------------------------------------------------------------------
# T6/T7：D 组三模式装配 + 同名冲突
# ---------------------------------------------------------------------------
def _fake_agent_tool(name):
    from astrbot.core.agent.tool import FunctionTool

    # 直接构造本体 FunctionTool（ToolSet.get_light_tool_set 同款用法）
    return FunctionTool(
        name=name,
        description=f"fake {name}",
        parameters={"type": "object", "properties": {}},
    )


class FakeToolMgr:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get_full_tool_set(self):
        from astrbot.core.agent.tool import ToolSet

        ts = ToolSet()
        for t in self._tools.values():
            ts.add_tool(t)
        return ts

    def get_func(self, name):
        return self._tools.get(name)


class FakePersonaManager:
    def __init__(self, persona):
        self._persona = persona

    async def get_default_persona_v3(self, umo):
        return self._persona


def _plugin_for_tools(tmp_path, config, mgr, persona):
    from test_main_wiring import make_plugin

    plugin = make_plugin(tmp_path / "tools.db")
    plugin.config = config
    plugin.context = types.SimpleNamespace(
        get_llm_tool_manager=lambda: mgr,
        persona_manager=FakePersonaManager(persona),
    )
    return plugin


def test_agent_tools_mode_off_unchanged(tmp_path):
    """off（默认）与现状逐字一致：不追加任何本体工具。"""
    from astrbot.core.agent.tool import ToolSet

    config = {"capabilities": {"agent_tools_mode": "off"}}
    plugin = _plugin_for_tools(tmp_path, config, None, None)
    tools = ToolSet()
    base_names = ["web_search", "fetch_page"]

    class T:
        def __init__(self, n):
            self.name = n

    for n in base_names:
        tools.add_tool(T(n))
    before = [t.name for t in tools.tools]
    asyncio.run(plugin._append_agent_tools(tools))
    assert [t.name for t in tools.tools] == before


def test_agent_tools_persona_filtering(tmp_path):
    """persona 档：tools=None → 全量去 inactive；列表 → 白名单；[] → 空。"""
    from astrbot.core.agent.tool import ToolSet

    t1, t2 = _fake_agent_tool("t1"), _fake_agent_tool("t2")
    t2.active = False
    mgr = FakeToolMgr([t1, t2])

    async def flow(persona):
        config = {"capabilities": {"agent_tools_mode": "persona"}}
        plugin = _plugin_for_tools(tmp_path, config, mgr, persona)
        tools = ToolSet()
        tools.add_tool(_fake_agent_tool("living_only"))
        await plugin._append_agent_tools(tools)
        return [t.name for t in tools.tools]

    # tools=None（未筛选）→ 全量工具集去 inactive
    assert asyncio.run(flow({"tools": None})) == ["living_only", "t1"]
    # 无人格 → 同上（本体同款分支）
    assert asyncio.run(flow(None)) == ["living_only", "t1"]
    # 白名单（inactive 的 t2 跳过；living_only 不在 mgr → 不加）
    assert asyncio.run(flow({"tools": ["t2", "living_only"]})) == ["living_only"]
    # tools=[] → 人格里明确禁用 → 空追加
    assert asyncio.run(flow({"tools": []})) == ["living_only"]


def test_agent_tools_custom_whitelist(tmp_path):
    from astrbot.core.agent.tool import ToolSet

    mgr = FakeToolMgr([_fake_agent_tool("t1"), _fake_agent_tool("t2")])

    async def flow(whitelist):
        config = {
            "capabilities": {"agent_tools_mode": "custom", "agent_tools": whitelist},
        }
        plugin = _plugin_for_tools(tmp_path, config, mgr, None)
        tools = ToolSet()
        tools.add_tool(_fake_agent_tool("living_only"))
        await plugin._append_agent_tools(tools)
        return [t.name for t in tools.tools]

    assert asyncio.run(flow("t1, t2")) == ["living_only", "t1", "t2"]
    assert asyncio.run(flow("")) == ["living_only"]  # 留空 = 不加任何本体工具


def test_agent_tools_name_conflict_living_first(tmp_path, caplog):
    """D3：同名冲突以 living 自带优先，冲突逐条 INFO。"""
    from astrbot.core.agent.tool import ToolSet

    mgr = FakeToolMgr([_fake_agent_tool("web_search"), _fake_agent_tool("t1")])
    config = {"capabilities": {"agent_tools_mode": "custom",
                               "agent_tools": "web_search,t1"}}
    plugin = _plugin_for_tools(tmp_path, config, mgr, None)
    tools = ToolSet()
    tools.add_tool(_fake_agent_tool("web_search"))

    with caplog.at_level(logging.INFO, logger="astrbot"):
        asyncio.run(plugin._append_agent_tools(tools))
    names = [t.name for t in tools.tools]
    assert names.count("web_search") == 1  # 不重复挂载
    assert names == ["web_search", "t1"]
    assert any("以 living 自带优先" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# D4：执行环境实测——ghost event 下本体工具执行链（MCP + 本地工具）
# ---------------------------------------------------------------------------
def test_ghost_env_executes_mcp_and_local_tool():
    """工具调用链实证：真实 FunctionToolExecutor + 幽灵事件 run_context 下，
    MCP 工具（_execute_mcp 分支）与本地工具（_execute_local 分支）都能执行。
    生产中 persona/custom 档追加的本体工具（含 MCP）走的就是这两条分支。"""
    import mcp.types
    from astrbot.core.agent.mcp_client import MCPTool
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    from core.ghost_event import build_ghost_event

    class StubMcpClient:
        async def call_tool_with_reconnect(
            self, tool_name, arguments, read_timeout_seconds=None
        ):
            return mcp.types.CallToolResult(
                content=[mcp.types.TextContent(
                    type="text", text=f"echo:{tool_name}"
                )]
            )

    mcp_tool = MCPTool(
        mcp_tool=mcp.types.Tool(
            name="mcp_echo", inputSchema={"type": "object", "properties": {}}
        ),
        mcp_client=StubMcpClient(),
        mcp_server_name="srv",
    )

    # AstrAgentContext 是 pydantic dataclass（isinstance 校验 Context）——
    # 用空壳子类满足校验即可（MCP/本地工具执行链不触碰 host context）
    from astrbot.core.star.context import Context

    class _GhostHostContext(Context):
        def __init__(self):  # 跳过基类的重构造；本测试不调用其任何方法
            pass

    agent_ctx = AstrAgentContext(
        context=_GhostHostContext(), event=build_ghost_event()
    )
    run_ctx = ContextWrapper(context=agent_ctx, tool_call_timeout=5)

    async def flow():
        results = []
        async for r in FunctionToolExecutor.execute(tool=mcp_tool, run_context=run_ctx):
            results.append(r)
        # 本地分支：living 自带 remember 工具走 _execute_local（生产同路径）
        from core.living_tools import RememberTool

        memory = FakeMemory()
        remember = RememberTool().bind(
            lambda: asyncio.sleep(0, result=memory)
        )
        local_results = []
        async for r in FunctionToolExecutor.execute(
            tool=remember, run_context=run_ctx, text="今天记一笔"
        ):
            local_results.append(r)
        return results, local_results, memory

    results, local_results, memory = asyncio.run(flow())
    assert len(results) == 1
    assert results[0].content[0].text == "echo:mcp_echo"
    assert len(local_results) == 1
    assert "今天记一笔" in local_results[0].content[0].text
    assert memory.added  # 本地工具真实写入了记忆


# ---------------------------------------------------------------------------
# T8：E1/E2 搜索开关（工具集 + 活动池）
# ---------------------------------------------------------------------------
class FakeSearcher:
    async def search(self, query, count=5, **kw):
        return [{"title": "t", "url": "https://e.com", "summary": "s"}]


class FakeFetcher:
    async def fetch(self, url):
        return {"title": "t", "text": "正文"}


def test_web_search_toggle_build():
    base = dict(searcher=FakeSearcher(), fetcher=FakeFetcher(), sandbox=None,
                memory_getter=None)
    default_names = build_living_tools(**base).names()
    assert "web_search" in default_names and "fetch_page" in default_names
    off_names = build_living_tools(**base, web_search_enabled=False).names()
    assert "web_search" not in off_names  # E2：不挂载
    assert "fetch_page" in off_names      # 其他冲浪能力保留


def test_pool_excludes_search_activities(caplog):
    """E3：web_search_enabled=false 时 surf/read 从两处可选池剔除（热读）。"""
    config = {"capabilities": {"web_search_enabled": False}}
    decider = ActivityDecider(
        activities=default_activities(), config_getter=lambda: config,
    )
    loop = LivingLoop(
        gate=None, memory_getter=None, config_getter=lambda: config,
        activities=default_activities(),
    )
    with caplog.at_level(logging.INFO, logger="astrbot"):
        d_names = [a.name for a in decider._effective_activities()]
        l_names = [a.name for a in loop._effective_activities()]
    for names in (d_names, l_names):
        assert "surf" not in names and "read" not in names
        assert "game" in names and "reminisce" in names and "peek" in names
        assert "free" in names
    assert any("活动池摘除" in r.getMessage() for r in caplog.records)

    # 热生效：改回 true（或键缺失）→ 池恢复
    config["capabilities"]["web_search_enabled"] = True
    assert "surf" in [a.name for a in decider._effective_activities()]
    config["capabilities"].pop("web_search_enabled")
    assert "read" in [a.name for a in loop._effective_activities()]
    assert web_search_enabled(config) is True


# ---------------------------------------------------------------------------
# T9：E4 搜索关闭时的活动优雅降级
# ---------------------------------------------------------------------------
class ExplodingSearcher:
    async def search(self, query, count=5, **kw):
        raise AssertionError("搜索关闭时不应再调用搜索 API")


class DeadFetcher:
    async def fetch(self, url):
        raise AssertionError("搜索关闭时不应再抓取")


def _ctx(search_enabled, tmp_path):
    import random

    return ActivityContext(
        searcher=ExplodingSearcher(),
        fetcher=DeadFetcher(),
        sandbox=None,
        memory=FakeMemory(),
        gate=None,
        event=None,
        rng=random.Random(7),  # 脚本路径 pick_topic 需要 Random 实例（.choice）
        now=NOON,
        search_enabled=search_enabled,
    )


def test_search_disabled_graceful_skip(tmp_path):
    from core.activities import ReadArticleActivity, SurfActivity

    for activity in (SurfActivity(), ReadArticleActivity()):
        outcome = asyncio.run(activity._run_script(_ctx(False, tmp_path)))
        assert outcome.name == activity.name
        assert outcome.summary  # 优雅收场有话可说，不报错
        assert "搜索" in (outcome.memory_content or "")


def test_search_enabled_keeps_script_path(tmp_path):
    """开关开着：脚本路径照旧调搜索（现状零回退）。"""
    import random

    from core.activities import SurfActivity

    ctx = ActivityContext(
        searcher=FakeSearcher(),
        fetcher=FakeFetcher(),
        sandbox=None,
        memory=FakeMemory(),
        gate=None,
        event=None,
        rng=random.Random(7),
        now=NOON,
        search_enabled=True,
    )
    outcome = asyncio.run(SurfActivity()._run_script(ctx))
    assert "搜了" in (outcome.summary or "")


# ---------------------------------------------------------------------------
# T10：C0 截图三路 + 模态判定
# ---------------------------------------------------------------------------
class ShotPage:
    def __init__(self):
        self.saved_to = None

    async def screenshot(self, path):
        self.saved_to = path
        Path(path).write_bytes(b"\x89PNG-fake-image")


def _shot(session, probe=None, captioner=None):
    tool = BrowserScreenshotTool()
    tool._session_ref = types.SimpleNamespace(session=session)
    tool.bind_image_channel(probe, captioner)
    return tool


def _session(tmp_path, page):
    from test_m3_patchXV import _fake_session

    return types.SimpleNamespace(
        _workspace=str(tmp_path), _ensure_page=_fake_session(page)._ensure_page
    )


def test_screenshot_returns_image_content(tmp_path):
    """C0-1/C0-2：默认（支持或未知）返回图片内容；文件照常落盘。"""
    page = ShotPage()
    result = asyncio.run(_shot(_session(tmp_path, page)).call(None))
    assert result.content[0].type == "image"
    assert result.content[0].mimeType == "image/png"
    assert base64.b64decode(result.content[0].data) == b"\x89PNG-fake-image"
    assert "截图已保存" in result.content[1].text
    assert Path(page.saved_to).parent == tmp_path / "screenshots"  # 落盘语义保留


def test_screenshot_caption_when_no_image_support(tmp_path):
    """C0-3-①：非多模态 + 配了转述 → <image_caption> 文本。"""
    page = ShotPage()

    async def captioner(path):
        return "一张深色的星空照片"

    result = asyncio.run(
        _shot(_session(tmp_path, page), probe=lambda: False, captioner=captioner).call(None)
    )
    assert result.content[0].type == "text"
    assert "<image_caption>一张深色的星空照片</image_caption>" in result.content[0].text
    assert "截图已保存" in result.content[0].text


def test_screenshot_removed_without_captioner(tmp_path, caplog):
    """C0-3-②：非多模态 + 未配转述 → 图片移除（仅存盘）且有 DEBUG。"""
    page = ShotPage()
    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        result = asyncio.run(
            _shot(_session(tmp_path, page), probe=lambda: False).call(None)
        )
    assert isinstance(result, str)
    assert "截图已保存" in result and "不支持查看图片" in result
    assert any(
        r.levelno == logging.DEBUG and "截图仅存盘" in r.getMessage()
        for r in caplog.records
    )


def test_provider_supports_image_matrix():
    class P:
        def __init__(self, modalities):
            self.provider_config = {"modalities": modalities}

    assert provider_supports_image(P(["image"])) is True
    assert provider_supports_image(P(["text"])) is False
    assert provider_supports_image(P([])) is True   # 空 = 未配置 → 支持
    assert provider_supports_image(None) is True    # 未知 → 交本体 runner 裁决
    assert provider_supports_image(object()) is True  # 无 modalities 字段


def test_main_caption_closure_reuses_core(tmp_path, monkeypatch):
    """C0-4：main._caption_screenshot 复用本体 _ensure_img_caption（同款
    语义），剥出内文；未配置转述模型 → None。"""
    from test_main_wiring import make_plugin

    calls = {}

    async def fake_caption(event, req, cfg, ctx, provider_id):
        calls["provider_id"] = provider_id
        calls["image_urls"] = list(req.image_urls)
        req.extra_user_content_parts = [
            types.SimpleNamespace(text="<image_caption>本体转述结果</image_caption>")
        ]

    import astrbot.core.astr_main_agent as core_agent

    monkeypatch.setattr(core_agent, "_ensure_img_caption", fake_caption)
    plugin = make_plugin(tmp_path / "m.db")
    plugin.config = {}
    plugin.context = types.SimpleNamespace(
        get_config=lambda umo=None: {"provider_settings": {
            "default_image_caption_provider_id": "caption-provider",
        }},
    )
    caption = asyncio.run(plugin._caption_screenshot("C:/tmp/x.png"))
    assert caption == "本体转述结果"
    assert calls["provider_id"] == "caption-provider"
    assert calls["image_urls"] == ["C:/tmp/x.png"]

    # 未配置转述模型 → None（工具侧按"图片移除+DEBUG"兜底）
    plugin.context.get_config = lambda umo=None: {"provider_settings": {}}
    assert asyncio.run(plugin._caption_screenshot("C:/tmp/x.png")) is None


# ---------------------------------------------------------------------------
# F 组：后端键 ↔ 面板入口逐项对照（铁律 4b 的源码断言）
# ---------------------------------------------------------------------------
def test_schema_new_keys_and_defaults():
    schema = json.loads(
        Path(__file__).resolve().parents[1].joinpath("_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    advanced = schema["advanced"]["items"]
    sleep = advanced["sleep"]["items"]
    caps = advanced["capabilities"]["items"]
    assert sleep["farewell_mode"]["default"] == "probability"
    assert sleep["farewell_mode"]["options"] == ["probability", "llm", "off"]
    assert sleep["farewell_probability"]["default"] == 0.5
    assert sleep["standby_blocks_sleep"]["default"] is True
    assert caps["web_search_enabled"]["default"] is True
    assert caps["agent_tools_mode"]["default"] == "off"
    assert caps["agent_tools_mode"]["options"] == ["off", "persona", "custom"]
    assert caps["agent_tools"]["default"] == ""


def test_panel_covers_every_new_key():
    """F4：每条新后端配置键在面板源码里有读写入口（缺一即未完成）。"""
    app_js = (
        Path(__file__).resolve().parents[1] / "pages" / "config" / "app.js"
    ).read_text(encoding="utf-8")
    required = [
        # 晚安卡：三档 + 概率滑块
        ("farewell_mode", "farewellCard"),
        ("farewell_probability", "farewellCard"),
        ("probability", "晚安三选之一"),
        # 聊天保护
        ("standby_blocks_sleep", "chatGuardCard"),
        # 浏览器说明 + 实时状态端点
        ("browser_status", "refreshBrowserStatus"),
        # 搜索开关
        ("web_search_enabled", "searchToggleCard"),
        # 本体工具（persona/off 两态 + 专家 custom 白名单）
        ("agent_tools_mode", "agentToolsCard"),
        ("persona", "本体工具允许档"),
        # 专家区（schema 驱动，键名经 app.js 的专家组渲染逐字呈现）
        ("agent_tools", "custom 白名单"),
    ]
    for key, note in required:
        assert key in app_js, f"面板缺少 {key} 的入口（{note}）"
    # 五张新卡都接到新手网格
    for card in ("farewellCard()", "chatGuardCard()", "browserCard()",
                 "searchToggleCard()", "agentToolsCard()"):
        assert f"grid.appendChild({card})" in app_js
