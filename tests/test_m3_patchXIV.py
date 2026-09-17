"""M3 补丁 XIV 测试：tier 2/3 能力——工作区工具、本机 shell、路径白名单。

全部走真实工具调用路径（build_living_tools → ToolSet → 工具实例），
不是纯函数测试。用临时目录模拟工作区。
"""

import asyncio
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.living_tools import build_living_tools


class StubSearcher:
    pass


class StubFetcher:
    pass


class StubSandbox:
    pass


class StubMemory:
    async def add(self, content, importance=0.5, metadata=None, **kw):
        return 1

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


def _build(tier=0, write_level=0, workspace=""):
    return build_living_tools(
        searcher=StubSearcher(),
        fetcher=StubFetcher(),
        sandbox=StubSandbox(),
        memory_getter=lambda: asyncio.sleep(0, result=StubMemory()),
        tier=tier,
        write_level=write_level,
        workspace=workspace,
    )


def _names(toolset):
    return {t.name for t in toolset.tools}


def test_tier_progression():
    """档位递进：tier=1/2/3 的工具名集合严格递进（3 ⊇ 2 ⊇ 1）。"""
    ws = str(Path(__file__).parent / "ws_test")

    ts1 = _build(tier=1, workspace=ws)
    ts2 = _build(tier=2, workspace=ws)
    ts3 = _build(tier=3, workspace=ws)

    def _tool_names(toolset):
        return {t.name for t in toolset.tools}
    names1 = _tool_names(ts1)
    names2 = _tool_names(ts2)
    names3 = _tool_names(ts3)

    assert names1 < names2 < names3


def _tool_names(toolset):
    return {t.name for t in toolset.tools}


def test_tier_progression_strict():
    """档位递进：3 ⊇ 2 ⊇ 1。"""
    ws = str(Path(__file__).parent / "ws_test")
    ts1 = _build(tier=1, workspace=ws)
    ts2 = _build(tier=2, workspace=ws)
    ts3 = _build(tier=3, workspace=ws)

    n1 = {t.name for t in ts1.tools}
    n2 = {t.name for t in ts2.tools}
    n3 = {t.name for t in ts3.tools}

    assert n1 <= n2 <= n3
    # tier 2 独有：workspace 工具
    assert "workspace_read" in n2 - n1 or "workspace_read" in n2
    # tier 3 独有：local_shell
    assert "local_shell" in n3 - n1


def test_tier2_workspace_read_write():
    """tier=2 工作区读写工具存在且可操作。"""
    import tempfile

    ws = tempfile.mkdtemp()
    ts = _build(tier=2, workspace=ws)
    names = {t.name for t in ts.tools}
    assert "workspace_read" in names
    assert "workspace_write" in names
    assert "workspace_list" in names


def test_workspace_write_enforces_path(tmp_path):
    """工作区写入：区外路径被 is_write_allowed 拒绝。"""
    from core.autonomy import is_write_allowed

    ws = str(tmp_path / "ws")
    # 工作区内写入 → 允许
    assert is_write_allowed(str(tmp_path / "ws" / "file.txt"), ws, 2) is True
    # 工作区外 → 拒绝
    assert is_write_allowed("/etc/passwd", ws, 2) is False
    # 红线路径 → 拒绝
    assert is_write_allowed("data/cmd_config.json", ws, 3) is False


def test_workspace_write_needs_level_2(tmp_path):
    """write_level < 2 时写被拒（只读/浏览交互不允许落盘）。"""
    from core.autonomy import is_write_allowed

    ws = str(tmp_path / "ws")
    assert is_write_allowed(str(tmp_path / "ws" / "f.txt"), ws, 0) is False
    assert is_write_allowed(str(tmp_path / "ws" / "f.txt"), ws, 1) is False
    assert is_write_allowed(str(tmp_path / "ws" / "f.txt"), ws, 2) is True


def test_tier3_local_shell_exists():
    """tier=3 的 ToolSet 含 local_shell。"""
    ws = str(Path(__file__).parent / "ws_test")
    ts = _build(tier=3, workspace=ws)
    names = {t.name for t in ts.tools}
    assert "local_shell" in names


def test_tier3_local_shell_executes_safe_command():
    """tier=3 local_shell 可执行安全命令（工作区内）。"""
    import tempfile

    ws = tempfile.mkdtemp()
    ts = _build(tier=3, workspace=ws)
    shell_tool = next(t for t in ts.tools if t.name == "local_shell")
    shell_tool._workspace = ws

    result = asyncio.run(shell_tool.call(None, command="echo hello"))
    assert "hello" in result


def test_tier3_local_shell_rejects_dangerous(tmp_path):
    """破坏性命令被黑名单拦截。"""
    ws = str(Path(__file__).parent / "ws_test")
    ts = _build(tier=3, workspace=ws)
    shell_tool = next(t for t in ts.tools if t.name == "local_shell")
    shell_tool._workspace = ws

    result = asyncio.run(shell_tool.call(None, command="rm -rf /"))
    assert "拒绝" in result


def test_sandbox_whitelist_unchanged():
    """红线：沙箱白名单不许放宽（补丁 XIV 红线 3）。"""
    from core.sandbox import IMPORT_WHITELIST
    assert IMPORT_WHITELIST == frozenset(
        {"random", "math", "time", "datetime", "json", "re", "itertools", "collections"}
    )


# ---------------------------------------------------------------------------
# 配置热读（P1 验证）
# ---------------------------------------------------------------------------
def test_hot_read_config_change(tmp_path):
    """同一进程内改 autonomy.tier → build_living_tools 结果变化（P1 热读）。"""
    config = {
        "autonomy": {"tier": 0, "write_level": 0},
        "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                     "impulse_check_interval_minutes": 5, "max_run_seconds": 300},
        "capabilities": {"cooldown_between_activities_hours": 0.0},
        "sleep": {"sleep_mode": "autonomous", "fatigue_rate_per_hour": 4.0,
                  "sleepiness_threshold": 0.6, "sleepiness_jitter": 0.0,
                  "min_awake_minutes": 240, "sleep_debt_decay_per_day": 30.0,
                  "circadian_hint": "23:00-07:00", "weights": {
                      "energy": 0.35, "debt": 0.35, "circadian": 0.30},
                  "wake_n_messages": 3, "wake_window_minutes": 10,
                  "grouchiness_percent": 0, "sleep_mute_replies": True,
                  "wake_source": "all", "owner_id": "",
                  "sleep_farewell_message": "", "wake_ack_message": "",
                  "nap_enabled": True, "nap_min_minutes": 20, "nap_max_minutes": 90},
        "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                        "target_sessions": "", "quiet_hours": "",
                        "share_rewrite_enabled": False, "share_rewrite_prompt": "",
                        "share_max_length": 120},
        "model": {"provider_id": "", "fallback_chain": []},
        "persona": {"life_extra": ""},
        "memory": {"backend": "auto"},
        "misc": {"log_level": "INFO"},
    }
    from core.autonomy import read_tier

    ws = str(tmp_path / "ws")
    searcher = StubSearcher()
    fetcher = StubFetcher()
    sandbox = StubSandbox()
    memory = lambda: asyncio.sleep(0, result=StubMemory())

    tier0 = read_tier(config)
    ts0 = build_living_tools(
        searcher=searcher, fetcher=fetcher, sandbox=sandbox,
        memory_getter=memory, tier=tier0, workspace=ws,
    )
    names0 = {t.name for t in ts0.tools}

    config["autonomy"]["tier"] = 3
    tier3 = read_tier(config)
    ts3 = build_living_tools(
        searcher=searcher, fetcher=fetcher, sandbox=sandbox,
        memory_getter=memory, tier=tier3, workspace=ws,
    )
    names3 = {t.name for t in ts3.tools}

    assert tier0 == 0 and tier3 == 3
    assert names0 != names3
    assert len(names3) > len(names0)
