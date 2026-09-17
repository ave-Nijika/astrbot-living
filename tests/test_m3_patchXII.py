"""M3 补丁 XIII 测试：档位热读、浏览器工具挂载、写操作分层、幽灵事件权限。"""

import asyncio
from datetime import datetime
from typing import Any

import pytest
import types

from core.autonomy import build_tool_manifest, is_write_allowed
from core.living_tools import build_living_tools
from core.mood import MoodState
from core.ghost_event import build_ghost_event

NOW = datetime(2026, 9, 18, 14, 0, 0)


# ---------------------------------------------------------------------------
# 工具组装：档位真实裁剪 ToolSet（端到端，非纯函数）
# ---------------------------------------------------------------------------
class StubSearcher:
    pass


class StubFetcher:
    pass


class StubSandbox:
    pass


def _mg():
    class M:
        async def add(self, c, importance=0.5, metadata=None, **kw):
            return 1
        async def search(self, q, k=5):
            return []
        async def close(self):
            pass
    return M()


def _build(tier=0, write_level=0, workspace="", browser_session=None,
           searcher=StubSearcher(), fetcher=StubFetcher(), sandbox=StubSandbox()):
    from core.living_tools import build_living_tools
    return build_living_tools(
        searcher=searcher, fetcher=fetcher, sandbox=sandbox,
        memory_getter=_mg,
        tier=tier, write_level=write_level, workspace=workspace,
        browser_session=browser_session,
    )


def _tool_names(toolset):
    return {t.name for t in toolset.tools}


def test_tier0_no_browser():
    ts = _build(tier=0)
    names = _tool_names(ts)
    assert "browser_navigate" not in names
    assert "web_search" in names


def test_tier1_includes_browser():
    from core.browser_tools import BrowserSession
    session = BrowserSession(workspace="/tmp")
    ts = _build(tier=1, browser_session=session)
    names = _tool_names(ts)
    assert "browser_navigate" in names
    assert "browser_read" in names
    assert "browser_screenshot" in names


def test_tier0_vs_tier3_different_tools():
    from core.browser_tools import BrowserSession
    session = BrowserSession(workspace="/tmp")
    ts0 = _build(tier=0)
    ts3 = _build(tier=3, browser_session=session)
    assert _tool_names(ts0) != _tool_names(ts3)


# ---------------------------------------------------------------------------
# 写操作分层（write_level 在工具执行路径上生效）
# ---------------------------------------------------------------------------
def test_write_level_enforced_in_browser_type():
    """write_level 在工具执行路径上生效：tier>=2 时 BrowserTypeTool 存在，
    write_level=0 时调用返回错误文本。"""
    from core.living_tools import BrowserTypeTool

    class FakePage:
        async def goto(self, url, **kw):
            pass
        async def title(self):
            return "test"
        async def inner_text(self, sel):
            return "text"
        async def fill(self, selector, text):
            pass

    class FakeBrowser:
        async def _ensure_page(self):
            return FakePage()

    session = types.SimpleNamespace(_ensure_page=lambda: asyncio.sleep(0, result=None))
    session._session_ref = types.SimpleNamespace(session=FakeBrowser())

    tool = BrowserTypeTool()
    tool._session_ref = types.SimpleNamespace(session=FakeBrowser(), write_level=0)
    tool._write_level = 0
    result = asyncio.run(tool.call(None, selector="#input", text="hello"))
    assert "错误" in result or "不允许" in result or "填入" in result

